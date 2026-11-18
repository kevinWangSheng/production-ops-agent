"""Shared workbench acceptance driver: real PG, product runner, scripted model.

Incidents and P2 messages enter through HTTP; P2 invokes the real relay pass.
The original continuation/watch seeding helpers remain available to P1.
Assertions read persisted business records, never the loop's private state.
No model HTTP, observer worker, or PostgreSQL process is started here.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import os
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4, uuid5

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from opspilot import schema
from opspilot.acceptance import IncidentScenario, outcome_from_durable
from opspilot.acceptance_audit import audit_timeline
from opspilot.investigation.runner import InvestigationRunner
from opspilot.observation import ObservationStore
from opspilot.observer import PROFILE_DIRECTORY, load_health_profile
from opspilot.persistence import DurableStore, PoolConfig, close_pools
from opspilot.tools.fixture import (
    FIXTURE_TOOL,
    fixture_executor_factory,
    fixture_face,
    fixture_versions,
)
from opspilot.web import (
    AuthConfig,
    Authenticator,
    DurableClock,
    DurableEventLog,
    DurableEvidenceStore,
    DurableIncidentStore,
    DurableWebLedger,
    Workbench,
    create_app,
    hash_password,
    token_digest,
)
from opspilot.web.service import available_actions
from opspilot.worker import Worker
from scripts.m0.postgres_lab import DSN, binary
from tests.m1_investigation_support import (
    ScriptedModel,
    reply,
    report_from_transcript,
    tool_call,
)
from tests.target_support import ANY_TARGETS, IDENTITY, PROFILE_ID, WORKLOAD

TARGET = "checkout-prod"
UI_USER = "workbench-oncall"
UI_PASSWORD = "workbench-acceptance-password"
EVENT_TOKEN = "workbench-acceptance-event-token"
ORIGIN = "https://ops.example.test"


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def text(self):
        return self.body.decode("utf-8")

    def json(self):
        return json.loads(self.body)


def basic():
    token = base64.b64encode(f"{UI_USER}:{UI_PASSWORD}".encode()).decode()
    return {"authorization": f"Basic {token}"}


def bearer():
    return {"authorization": f"Bearer {EVENT_TOKEN}"}


def call(app, method, path, *, headers=None, body=b""):
    """In-process HTTP, ported from m1_web_support (retired by P4).

    The app handles a real ASGI request; no HTTP endpoint or business logic
    is mocked. Keep HTTP/auth support in this one shared redesign driver.
    """

    async def request():
        route, _, query = path.partition("?")
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method,
            "scheme": "https",
            "path": route,
            "raw_path": route.encode(),
            "query_string": query.encode(),
            "root_path": "",
            "headers": [
                (key.lower().encode(), value.encode())
                for key, value in (headers or {}).items()
            ]
            + [(b"content-length", str(len(body)).encode())],
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 443),
        }
        messages = []
        sent = False
        done = asyncio.Event()

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            await done.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)
            if message["type"] == "http.response.body" and not message.get(
                "more_body", False
            ):
                done.set()

        await app(scope, receive, send)
        start = next(item for item in messages if item["type"] == "http.response.start")
        return HTTPResponse(
            start["status"],
            {key.decode(): value.decode() for key, value in start["headers"]},
            b"".join(
                item.get("body", b"")
                for item in messages
                if item["type"] == "http.response.body"
            ),
        )

    return asyncio.run(request())


def post_json(app, path, payload, *, headers):
    return call(
        app,
        "POST",
        path,
        headers={"content-type": "application/json", **headers},
        body=json.dumps(payload).encode(),
    )


def step_contract(step: dict[str, Any]) -> dict[str, Any]:
    # contract C1/C6 field name: pin the JSON envelope here, not in assertions.
    # Tool-step replies/note are response top-level; terminal response fields
    # sit beside execution/report_content in response.conclusion. No aliases.
    payload = step["response"] or {}
    terminal = payload.get("kind") == "conclusion"
    fields = payload.get("conclusion", {}) if terminal else payload
    return {
        "terminal": terminal,
        "execution": fields.get("execution"),
        "handoff": fields.get("handoff"),
        "handoff_reasons": fields.get("handoff_reasons", []),
        "report_content": fields.get("report_content"),
        "report_rejected_reason": fields.get("report_rejected_reason"),
        "replies": fields.get("replies", []),
        "note": payload.get("note"),
        "assistant": payload.get("assistant", {}),
        "watermark": payload.get("context", {}).get("input_watermark"),
    }


@dataclass(frozen=True)
class Conversation:
    scenario: IncidentScenario
    incident_id: UUID
    run_id: UUID
    input_seq: int
    floor: int
    before: dict[str, Any]
    observation_before: dict[str, Any]


class WorkbenchDriver:
    def __init__(self, dsn: str):
        self.store = DurableStore(dsn, pool=PoolConfig(min_size=0, max_size=4))
        self.store.install()
        self.clock = DurableClock(self.store)
        self.events = DurableEventLog(self.store)
        self.evidence = DurableEvidenceStore(self.store)
        self.workbench = Workbench(
            incidents=DurableIncidentStore(self.store),
            events=self.events,
            evidence=self.evidence,
            ledger=DurableWebLedger(self.store),
            run_versions=fixture_versions(),
            run_seconds=600,
            tool_face=fixture_face(),
            targets=ANY_TARGETS,
        )
        auth = AuthConfig(
            ui_users={UI_USER: hash_password(UI_PASSWORD)},
            event_tokens={token_digest(EVENT_TOKEN): "alertmanager-prod"},
            auth_revision="workbench-acceptance",
            allowed_origins=frozenset({ORIGIN}),
        )
        self.app = create_app(self.workbench, Authenticator(auth), self.clock)

    def close(self):
        self.store.close()

    def control(self, incident_id: UUID, action: str, **fields) -> HTTPResponse:
        fields = {
            "action": action,
            "expected_generation": self.incident(incident_id)["control_generation"],
            "idempotency_key": f"workbench-{uuid4()}",
            **fields,
        }
        return call(
            self.app,
            "POST",
            f"/incidents/{incident_id}/control",
            headers={
                **basic(),
                "origin": ORIGIN,
                "content-type": "application/x-www-form-urlencoded",
            },
            body=urlencode(fields).encode(),
        )

    def message(self, incident_id: UUID, text: str, *, action="message", key=None):
        # contract B1/R2 interface: POST /incidents/{id}/control, action=message
        # (or its follow_up/correct HTTP aliases). The control helper's
        # expected_generation is optional and ignored by message admission.
        return self.control(
            incident_id,
            action,
            text=text,
            idempotency_key=key or f"workbench-message-{uuid4()}",
        )

    def relay(self):
        # contract B1/R2 interface: Workbench.relay_pass(limit=20) is the
        # public background step over Workbench.relay(incident_id).
        relay = self.workbench.relay_pass
        assert callable(relay), "P2 R2: the web relay loop step is not available"
        return relay(limit=10_000)

    def runs(self, incident_id: UUID):
        with self.store.transaction(snapshot=True) as conn:
            return conn.execute(
                "SELECT * FROM opspilot_runs WHERE incident_id=%s ORDER BY run_id",
                (incident_id,),
            ).fetchall()

    def inputs(self, incident_id: UUID):
        with self.store.transaction(snapshot=True) as conn:
            return conn.execute(
                "SELECT * FROM opspilot_inputs WHERE incident_id=%s ORDER BY sequence",
                (incident_id,),
            ).fetchall()

    def unread(self, incident_id: UUID):
        floor = max((r["input_watermark"] for r in self.runs(incident_id)), default=0)
        return [
            row
            for row in self.inputs(incident_id)
            if row["kind"] == "message" and row["sequence"] > floor
        ]

    def actions(self, incident_id: UUID):
        return available_actions(self.workbench.snapshot(incident_id))

    def audit(self, incident_id: UUID):
        with self.store.transaction(snapshot=True) as conn:
            return audit_timeline(conn, incident_id)

    def records(self, incident_id: UUID):
        """Business state for refusal/no-write assertions, including leases."""
        with self.store.transaction(snapshot=True) as conn:
            controls = conn.execute(
                "SELECT * FROM opspilot_controls WHERE incident_id=%s ORDER BY audit_id",
                (incident_id,),
            ).fetchall()
        return {
            "incident": self.incident(incident_id),
            "runs": self.runs(incident_id),
            "inputs": self.inputs(incident_id),
            "controls": controls,
            "observation": self.observation(incident_id),
        }

    def legacy_input(self, incident_id: UUID, kind: str):
        """Old-data fixture only: historical rows must not trigger relay."""
        assert kind in {"follow_up", "correct"}
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO opspilot_inputs "
                "(input_id,incident_id,sequence,kind,content,actor,control_generation) "
                "SELECT %s,%s,COALESCE(max(sequence),0)+1,%s,%s,%s,%s "
                "FROM opspilot_inputs WHERE incident_id=%s",
                (
                    uuid4(),
                    incident_id,
                    kind,
                    Jsonb({"text": f"Historical {kind}", "channel": "web"}),
                    UI_USER,
                    self.incident(incident_id)["control_generation"],
                    incident_id,
                ),
            )

    def enable_observation(self):
        self.workbench.health_profile = load_health_profile(
            PROFILE_DIRECTORY / f"{PROFILE_ID}.json"
        )

    def automatic_observation(self):
        return self.workbench.self_heal_pass(limit=10_000)

    def expire_watch(self, incident_id: UUID):
        """Fixture: manual observation ended without confirming recovery.

        No observer/source process is started. Ending the session exposes
        erroneous reauthorization that an active-session guard could mask.
        """
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE opspilot_observation_sessions SET state='expired',"
                "ended_reason='max_samples_exhausted',active_sample_job_id=NULL,"
                "active_sample_sequence=NULL,active_sample_due_at=NULL,"
                "active_sample_owner=NULL,active_sample_lease_until=NULL "
                "WHERE incident_id=%s AND state='authorized'",
                (incident_id,),
            )

    def resolved(self, incident_id: UUID):
        """Fixture lifecycle only; recovery itself is covered by F6."""
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE opspilot_incidents SET lifecycle='resolved' WHERE incident_id=%s",
                (incident_id,),
            )

    def incident(self, incident_id: UUID) -> dict[str, Any]:
        with self.store.transaction(snapshot=True) as conn:
            row = conn.execute(
                "SELECT *, convert_to(conclusion::text,'UTF8') AS conclusion_bytes "
                "FROM opspilot_incidents WHERE incident_id=%s",
                (incident_id,),
            ).fetchone()
        assert row is not None
        return row

    def steps(self, incident_id: UUID) -> list[dict[str, Any]]:
        return [
            step
            for step in self.store.rebuild(incident_id)["steps"]
            if step["status"] != "late_result"
        ]

    def terminal(self, incident_id: UUID) -> dict[str, Any]:
        steps = [s for s in self.steps(incident_id) if step_contract(s)["terminal"]]
        assert len(steps) == 1, "the current Run must persist one conclusion step"
        return step_contract(steps[0])

    def replies_for(self, incident_id: UUID, input_seq: int) -> list[dict[str, Any]]:
        return [
            item
            for step in self.steps(incident_id)
            for item in step_contract(step)["replies"]
            if item["to_input_seq"] == input_seq
        ]

    def observation(self, incident_id: UUID) -> dict[str, Any]:
        with self.store.transaction(snapshot=True) as conn:
            sessions = conn.execute(
                "SELECT * FROM opspilot_observation_sessions "
                "WHERE incident_id=%s ORDER BY session_id",
                (incident_id,),
            ).fetchall()
            authorizations = conn.execute(
                "SELECT * FROM opspilot_self_heal_authorizations "
                "WHERE incident_id=%s ORDER BY control_generation",
                (incident_id,),
            ).fetchall()
            samples = conn.execute(
                "SELECT s.* FROM opspilot_observation_samples s "
                "JOIN opspilot_observation_sessions o USING (session_id) "
                "WHERE o.incident_id=%s ORDER BY s.sample_id",
                (incident_id,),
            ).fetchall()
            endings = conn.execute(
                "SELECT * FROM opspilot_observation_endings "
                "WHERE incident_id=%s ORDER BY ending_id",
                (incident_id,),
            ).fetchall()
        return {
            "sessions": sessions,
            "authorizations": authorizations,
            "samples": samples,
            "endings": endings,
        }

    def from_alert(self, scenario_id: str, *, target=TARGET) -> IncidentScenario:
        if target is None:
            response = post_json(
                self.app,
                "/intake/alertmanager",
                {
                    "version": "4",
                    "status": "firing",
                    "alerts": [
                        {
                            "status": "firing",
                            "fingerprint": uuid4().hex,
                            "startsAt": self.clock.now().isoformat(),
                            "labels": {"alertname": "WorkbenchUnmapped"},
                            "annotations": {},
                        }
                    ],
                },
                headers=bearer(),
            )
            assert response.status == 200, response.text
            subject = response.json()["results"][0]["incident_id"]
            assert subject is not None, response.text
            return IncidentScenario(
                scenario_id=scenario_id,
                feature_id="F12",
                acceptance_step="Workbench P2 message admission",
                kind="incident",
                subject_id=subject,
            )
        response = post_json(
            self.app,
            "/intake/events",
            {
                "source": "alertmanager",
                "external_event_id": f"workbench-{uuid4()}",
                "target_id": target,
                "question": "CheckoutErrorRateHigh firing: why is checkout erroring?",
            },
            headers=bearer(),
        )
        assert response.status == 201, response.text
        return IncidentScenario(
            scenario_id=scenario_id,
            feature_id="F12",
            acceptance_step="Workbench P1 response contract",
            kind="incident",
            subject_id=response.json()["incident_id"],
        )

    def run(self, scenario: IncidentScenario, script):
        model = ScriptedModel(script)
        runner = InvestigationRunner(
            store=self.store,
            worker=Worker.create(self.store, fixture_versions()),
            model=model,
            executor_factory=fixture_executor_factory(
                self.store, evidence=self.evidence, clock=self.clock
            ),
            clock=self.clock,
            lease_seconds=30,
            events=self.events,
            evidence=self.evidence,
        )
        result = runner.resume(UUID(scenario.subject_id))
        return result, model

    def published(self, scenario_id: str) -> IncidentScenario:
        scenario = self.from_alert(scenario_id)
        result, _ = self.run(scenario, [tool_round(), report_from_transcript])
        incident_id = UUID(scenario.subject_id)
        rows = self.store.rebuild(incident_id)
        assert result.status == "published", result
        assert rows["run"]["state"] == "completed"
        assert rows["conclusion"] is not None
        return scenario

    def watch(self, incident_id: UUID):
        """Authorize a real, active session without a control-generation bump.

        The store authorization primitive is setup only. No observer worker
        runs, so all observation rows can be compared exactly after the reply.
        """
        # Fixture intake registers only resource_uid. As in the existing PG
        # observation fixture, complete the synthetic identity during setup;
        # do not manufacture a human remediation or advance the generation.
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE opspilot_targets SET "
                "integration_id=COALESCE(integration_id,%s),"
                "cluster_uid=COALESCE(cluster_uid,%s),"
                "namespace=COALESCE(namespace,%s),workload=COALESCE(workload,%s) "
                "WHERE target_id=(SELECT target_id FROM opspilot_incidents WHERE incident_id=%s)",
                (
                    IDENTITY["integration_id"],
                    IDENTITY["cluster_uid"],
                    IDENTITY["namespace"],
                    WORKLOAD,
                    incident_id,
                ),
            )
        observer = ObservationStore(
            self.store.dsn, pool=PoolConfig(min_size=0, max_size=2)
        )
        try:
            observer.authorize_session(
                incident_id,
                revision="workbench-fixture-r1",
                actor="acceptance-setup",
                deadline_at=self.clock.now() + timedelta(hours=1),
                max_samples=10,
                sample_interval_seconds=15,
                sustained_window_seconds=60,
            )
        finally:
            observer.close()
        rows = self.observation(incident_id)
        assert len(rows["sessions"]) == 1
        assert rows["sessions"][0]["state"] == "authorized"

    def continuation(
        self, scenario: IncidentScenario, text: str, *, mode="report_optional"
    ) -> Conversation:
        """Seed exactly the loop-facing A1/R4/B4 relay state; no P2 invocation.

        The existing successor-input builder carries the report and evidence.
        The transaction appends kind=message, inserts a queued current Run at
        the same generation, and preserves conclusion/lifecycle/observation.
        """
        assert mode in {"report_optional", "reply_only"}
        incident_id = UUID(scenario.subject_id)
        before = self.incident(incident_id)
        observation_before = self.observation(incident_id)
        snapshot = self.store.rebuild(incident_id)
        with self.store.transaction() as conn:
            conn.execute(
                "SELECT 1 FROM opspilot_scope_controls WHERE scope_id=1 FOR UPDATE"
            )
            conn.execute(
                "SELECT 1 FROM opspilot_incidents WHERE incident_id=%s FOR UPDATE",
                (incident_id,),
            )
            floor = conn.execute(
                "SELECT COALESCE(max(input_watermark),0) AS n FROM opspilot_runs "
                "WHERE incident_id=%s",
                (incident_id,),
            ).fetchone()["n"]
            seq = conn.execute(
                "SELECT COALESCE(max(sequence),0)+1 AS n FROM opspilot_inputs "
                "WHERE incident_id=%s",
                (incident_id,),
            ).fetchone()["n"]
            run_id = uuid5(incident_id, f"relay:{seq}")
            deadline = conn.execute(
                "SELECT clock_timestamp()+interval '600 seconds' AS deadline"
            ).fetchone()["deadline"]
            summary = self.workbench.incidents.find_incident(incident_id)
            run_input = self.workbench._successor_input(
                summary, run_id, deadline, snapshot=snapshot
            )
            assert run_input is not None
            run_input = copy.deepcopy(run_input)
            run_input.setdefault("scope_facts", {})["conversation"] = {
                "floor": floor,
                "mode": mode,
            }
            content = {
                "text": text,
                "channel": "investigation",
                "idempotency_key": f"workbench-message-{uuid4()}",
            }
            conn.execute(
                "INSERT INTO opspilot_inputs "
                "(input_id,incident_id,sequence,kind,content,actor,control_generation) "
                "VALUES (%s,%s,%s,'message',%s,%s,%s)",
                (
                    uuid4(),
                    incident_id,
                    seq,
                    Jsonb(content),
                    UI_USER,
                    before["control_generation"],
                ),
            )
            conn.execute(
                "INSERT INTO opspilot_runs "
                "(run_id,incident_id,state,control_generation,budget_limit,deadline,versions,input) "
                "VALUES (%s,%s,'queued',%s,%s,%s,%s,%s)",
                (
                    run_id,
                    incident_id,
                    before["control_generation"],
                    snapshot["run"]["budget_limit"],
                    deadline,
                    Jsonb(fixture_versions()),
                    Jsonb(run_input),
                ),
            )
            conn.execute(
                "UPDATE opspilot_incidents SET current_run_id=%s WHERE incident_id=%s",
                (run_id, incident_id),
            )
        rows = self.store.rebuild(incident_id)
        assert rows["run"]["run_id"] == run_id
        assert rows["run"]["state"] == "queued"
        assert rows["run"]["control_generation"] == before["control_generation"]
        assert rows["control_generation"] == before["control_generation"]
        assert (
            self.incident(incident_id)["conclusion_bytes"] == before["conclusion_bytes"]
        )
        assert self.observation(incident_id) == observation_before
        assert rows["inputs"][-1]["kind"] == "message"
        assert rows["inputs"][-1]["content"] == content
        return Conversation(
            scenario, incident_id, run_id, seq, floor, before, observation_before
        )

    def project(self, scenario: IncidentScenario):
        return outcome_from_durable(
            scenario, self.store.rebuild(UUID(scenario.subject_id))
        )


def tool_round(*, content=None, call_id="workbench-read"):
    return reply(
        content=content,
        tool_calls=[tool_call(call_id=call_id, name=FIXTURE_TOOL)],
        finish="tool_calls",
    )


@pytest.fixture(name="workbench_database")
def workbench_database_fixture():
    if os.environ.get("M1_DURABLE_POSTGRES") != "1":
        pytest.skip("explicit PG opt-in required: M1_DURABLE_POSTGRES=1")
    # The fixture tool/report helpers deliberately share a logical target.
    # Give each test a UUID-named database on the supplied PG server, as the
    # existing F7 acceptance harness does. Leftover targets/sessions in the
    # reused lab DB cannot trigger TARGET_MISMATCH; no old rows are edited.
    name = f"workbench_{uuid4().hex}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    dsn = make_conninfo(DSN, dbname=name)
    try:
        # Acceptance is outside tests/integration's autouse migration scope.
        schema.migrate(
            dsn, pg_dump=os.environ.get("OPSPILOT_PG_DUMP") or binary("pg_dump")
        )
        yield dsn
    finally:
        close_pools()
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


@pytest.fixture(name="workbench")
def workbench_fixture(workbench_database):
    driver = WorkbenchDriver(workbench_database)
    try:
        yield driver
    finally:
        driver.close()
