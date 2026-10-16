"""PostgreSQL harness for the F7 step 2/3 acceptance tests (product path).

A scratch database migrated from the lab DSN (``OPSPILOT_LAB_DSN``), the web
workbench under the ``otel-demo`` profile with the reviewed-knowledge store
wired in (F13 step 4), and the real ``InvestigationRunner`` driven by the real
``DeepSeekClient`` whose HTTPS opener is a scripted provider double. Backend
reads are served by the recorded-response ``FakeOpener`` (R4: replay
transport, no kind). Nothing here reads the implementation under change.

The provider double records the exact JSON request body the client would
send: that is the model input the step-3 assertions inspect.
"""

from __future__ import annotations

import json
import os
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import schema
from opspilot.investigation.client import DeepSeekClient
from opspilot.investigation.runner import InvestigationRunner
from opspilot.knowledge import (
    Actor,
    ConclusionDraft,
    KnowledgeStore,
    ProposalDraft,
    Watermark,
)
from opspilot.persistence import DurableStore, PoolConfig
from opspilot.tools.otel_demo import otel_demo_face
from opspilot.tools.profiles import PROFILE_ENV, select_profile
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
from opspilot.worker import Worker
from scripts.m0.postgres_lab import DSN
from tests.m1_tool_support import FakeClock
from tests.m1_web_support import (
    EVENT_TOKEN,
    ORIGIN,
    UI_PASSWORD,
    UI_USER,
    basic,
    call,
    post_form,
    same_origin,
)
from tests.test_m1_otel_demo_contract import (
    JAEGER_URL,
    PROMETHEUS_URL,
    WINDOW_END,
    FakeOpener,
)

PG_DUMP = os.environ.get("OPSPILOT_PG_DUMP", "pg_dump")
POOL = PoolConfig(min_size=0, max_size=4, timeout=5.0)
UI = {**basic(), **same_origin()}
REVIEWER = Actor("f7-reviewer", "basic_auth")
WORKER = Actor("f7-worker", "worker")
MODEL_KEY = "f7-provider-key-not-real-0000"  # handed to the client only


def scratch_database() -> Iterator[str]:
    """A migrated, throw-away database next to the lab one (dropped after)."""
    name = f"opspilot_f7b_{uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    scratch = make_conninfo(DSN, dbname=name)
    try:
        schema.migrate(scratch, pg_dump=PG_DUMP)
        yield scratch
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )


# -- provider double ------------------------------------------------------------


class _Response:
    def __init__(self, payload: bytes) -> None:
        self._chunks = [payload, b""]
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self, size=-1):
        return self._chunks.pop(0) if self._chunks else b""


def assistant(*, tool_calls=None, content=None) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": content,
        "reasoning_content": "scripted",
    }
    if tool_calls:
        message["tool_calls"] = list(tool_calls)
    return message


def fn(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


class ScriptedProvider:
    """The HTTPS opener behind ``DeepSeekClient``.

    ``script`` items are assistant messages, or callables taking the request's
    ``messages`` and returning one. ``requests`` keeps every request body the
    client sent, parsed: the model input as the provider would receive it.
    """

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []
        self.raw_requests: list[bytes] = []

    def open(self, request, timeout=None):
        data = request.data
        self.raw_requests.append(bytes(data))
        body = json.loads(data)
        self.requests.append(body)
        if not self.script:
            raise AssertionError("provider script exhausted")
        item = self.script.pop(0)
        message = item(body["messages"]) if callable(item) else item
        finish = "tool_calls" if message.get("tool_calls") else "stop"
        payload = {
            "id": f"resp-{len(self.requests)}",
            "model": "deepseek-flash",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
            },
        }
        return _Response(json.dumps(payload).encode("utf-8"))


def tool_views(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    views = []
    for message in messages:
        if message.get("role") == "tool":
            views.append(
                {
                    "tool_call_id": message.get("tool_call_id"),
                    "view": json.loads(message["content"]),
                }
            )
    return views


def report(evidence_id: str, target_ref: str) -> dict[str, Any]:
    payload = {
        "schema_version": "m0-report-v2",
        "assessment_status": "completed",
        "conclusion": "supported",
        "summary": "Checkout traces show an erroring payment span.",
        "claims": [
            {
                "kind": "fact",
                "text": "The authorized traces search returned an erroring span.",
                "evidence_ids": [evidence_id],
                "target_refs": [target_ref],
                "time_scope_ref": "policy-window-1",
            }
        ],
        "gaps": ["No HealthProfile comparison was made."],
        "next_steps": ["Have a human compare the sample against the SLO."],
    }
    return assistant(content=json.dumps(payload))


def final_report(target_ref: str) -> Callable[[list[dict[str, Any]]], dict]:
    """A report citing the first evidence id any tool view returned."""

    def build(messages):
        for item in tool_views(messages):
            evidence_id = item["view"].get("evidence_id")
            if evidence_id:
                return report(evidence_id, target_ref)
        return report("missing", target_ref)

    return build


# -- workbench + runner ------------------------------------------------------------


@dataclass
class Harness:
    dsn: str
    store: DurableStore
    events: DurableEventLog
    evidence: DurableEvidenceStore
    knowledge: KnowledgeStore
    app: Any
    workbench: Workbench
    profile: Any
    closers: list[Any] = field(default_factory=list)

    def submit(self, question: str, *, target_id: str, app=None) -> tuple[UUID, UUID]:
        response = post_form(
            app or self.app,
            "/intake/ui",
            {
                "target_id": target_id,
                "question": question,
                "idempotency_key": f"f7b-{uuid4()}",
            },
            headers=UI,
        )
        assert response.status == 201, response.text
        body = response.json()
        return UUID(body["incident_id"]), UUID(body["run_id"])

    def run(self, incident: UUID, provider: ScriptedProvider):
        """One worker attempt with the product runner, client and executor."""
        clock = DurableClock(self.store)
        runner = InvestigationRunner(
            store=self.store,
            worker=Worker.create(self.store, self.profile.versions()),
            model=DeepSeekClient(MODEL_KEY, opener=provider),
            executor_factory=self.profile.executor_factory(
                self.store, self.evidence, clock
            ),
            clock=clock,
            lease_seconds=30,
            events=self.events,
            evidence=self.evidence,
        )
        return runner.resume(incident)

    def get(self, path: str, *, html: bool = False):
        headers = {**basic(), "accept": "text/html" if html else "application/json"}
        return call(self.app, "GET", path, headers=headers)

    def workbench_app(self, run_versions: dict[str, str]):
        """A second web app on the same store whose Runs record ``run_versions``."""
        workbench = _workbench(self.store, self.events, self.evidence, run_versions)
        return create_app(workbench, Authenticator(_auth()), DurableClock(self.store))


def _auth() -> AuthConfig:
    return AuthConfig(
        ui_users={UI_USER: hash_password(UI_PASSWORD)},
        event_tokens={token_digest(EVENT_TOKEN): "alertmanager-prod"},
        auth_revision="auth-rev-f7b",
        allowed_origins=frozenset({ORIGIN}),
    )


def _workbench(store, events, evidence, run_versions, knowledge=None) -> Workbench:
    return Workbench(
        incidents=DurableIncidentStore(store),
        events=events,
        evidence=evidence,
        ledger=DurableWebLedger(store),
        run_versions=run_versions,
        run_seconds=600,
        # The face clock fixes the policy frame to end at the recorded window
        # the replayed backend bodies answer for; the worker keeps real time.
        tool_face=otel_demo_face(FakeClock(start=WINDOW_END)),
        knowledge=knowledge,
    )


def build_harness(dsn: str, monkeypatch, opener: FakeOpener) -> Harness:
    monkeypatch.setenv("OPSPILOT_OTEL_PROMETHEUS_URL", PROMETHEUS_URL)
    monkeypatch.setenv("OPSPILOT_OTEL_JAEGER_URL", JAEGER_URL)
    monkeypatch.delenv("OPSPILOT_OTEL_TOKEN", raising=False)
    # The transport builds its opener when the factory is built.
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: opener)
    profile = select_profile({PROFILE_ENV: "otel-demo"})
    store = DurableStore(dsn, pool=POOL)
    store.install()
    events = DurableEventLog(store)
    evidence = DurableEvidenceStore(store)
    knowledge = KnowledgeStore(dsn, pool=POOL)
    workbench = _workbench(store, events, evidence, profile.versions(), knowledge)
    app = create_app(workbench, Authenticator(_auth()), DurableClock(store))
    return Harness(
        dsn=dsn,
        store=store,
        events=events,
        evidence=evidence,
        knowledge=knowledge,
        app=app,
        workbench=workbench,
        profile=profile,
        closers=[knowledge, store],
    )


# -- reviewed knowledge (F13 step 4 channel, U6-a) ----------------------------------


def approve_knowledge(
    harness: Harness, incident_id: UUID, *, name: str, content: dict[str, Any]
) -> dict[str, Any]:
    """End ``incident_id``'s observation (SQL, as the F13 tests do), record a
    postmortem draft with one knowledge proposal and approve it as a human
    reviewer through the real ``KnowledgeStore``."""
    session_id, ending_id = uuid4(), uuid4()
    with psycopg.connect(harness.dsn) as conn:
        row = conn.execute(
            "SELECT target_id FROM opspilot_incidents WHERE incident_id=%s",
            (incident_id,),
        ).fetchone()
        assert row is not None
        conn.execute(
            "UPDATE opspilot_incidents SET observation_generation=1 WHERE incident_id=%s",
            (incident_id,),
        )
        conn.execute(
            "INSERT INTO opspilot_observation_sessions(session_id, incident_id, purpose, "
            "target_id, target, subject_control_generation, observation_generation, "
            "authorized_by, state, ended_reason, authorized_global_generation, "
            "authorized_target_generation, deadline_at, max_samples, "
            "sample_interval_seconds, sustained_window_seconds) VALUES "
            "(%s, %s, 'incident_recovery', %s, '{}', 0, 1, 'op', 'completed', "
            "'recovery_confirmed', 0, 0, clock_timestamp(), 1, 60, 60)",
            (session_id, incident_id, row[0]),
        )
        conn.execute(
            "INSERT INTO opspilot_observation_endings(ending_id, session_id, incident_id, "
            "ended_reason, lifecycle_before, lifecycle_after) VALUES "
            "(%s, %s, %s, 'deadline_expired', 'observing_recovery', 'observing_recovery')",
            (ending_id, session_id, incident_id),
        )
        current = conn.execute(
            "SELECT i.control_generation, i.current_run_id, "
            "(SELECT count(*) FROM opspilot_runs r WHERE r.incident_id=i.incident_id), "
            "(SELECT COALESCE(max(n.sequence), 0) FROM opspilot_inputs n "
            "WHERE n.incident_id=i.incident_id) "
            "FROM opspilot_incidents i WHERE i.incident_id=%s",
            (incident_id,),
        ).fetchone()
        assert current is not None
    watermark = Watermark(
        incident_control_generation=current[0],
        observation_generation=1,
        observation_session_id=session_id,
        observation_ending_id=ending_id,
        run_count=current[2],
        last_run_id=current[1],
        input_watermark=current[3],
        evidence_snapshot_sha256=harness.knowledge.evidence_snapshot_sha256(
            incident_id
        ),
    )
    draft = harness.knowledge.record_draft(
        incident_id,
        expected_generation=0,
        idempotency_key=f"f7b-{uuid4().hex}",
        actor=WORKER,
        content={"impact": "checkout degraded"},
        watermark=watermark,
        conclusions=[
            ConclusionDraft(
                key="timeline",
                section="timeline",
                body="alert received; Run started",
                author="code",
                certainty="deterministic",
                citations_valid=True,
            )
        ],
        proposals=[
            ProposalDraft(
                key="kb-1",
                name=name,
                tags=["checkout", "payment"],
                content=content,
            )
        ],
    )
    approved = harness.knowledge.approve(
        draft.object_id,
        draft.version,
        expected_generation=draft.generation,
        idempotency_key=f"f7b-{uuid4().hex}",
        actor=REVIEWER,
    )
    return approved.published["kb-1"]


# -- observable state snapshots (U8-a) ---------------------------------------------


def authority_snapshot(harness: Harness, incident: UUID, run: UUID) -> dict[str, Any]:
    """What a hostile instruction would have to change to widen the Run:
    its recorded scope facts and policy frame, target, budget, control
    generations, suspension gates and the registered target set."""
    snapshot = harness.store.rebuild(incident)
    recorded = snapshot["run"]["input"]
    control = harness.store.control_state(incident)
    with psycopg.connect(harness.dsn) as conn:
        run_row = conn.execute(
            "SELECT budget_limit, deadline, versions, control_generation "
            "FROM opspilot_runs WHERE run_id=%s",
            (run,),
        ).fetchone()
        incident_row = conn.execute(
            "SELECT target_id FROM opspilot_incidents WHERE incident_id=%s",
            (incident,),
        ).fetchone()
        targets = conn.execute(
            "SELECT target_id, resource_uid FROM opspilot_targets ORDER BY 1"
        ).fetchall()
        gates = conn.execute(
            "SELECT * FROM opspilot_scope_controls ORDER BY 1"
        ).fetchall()
    return {
        "scope_facts": {
            k: v for k, v in recorded["scope_facts"].items() if k != "knowledge"
        },
        "bound_target_id": recorded.get("bound_target_id"),
        "time_policies": recorded["evidence_context"]["time_policies"],
        "tool_names": [s["function"]["name"] for s in recorded["tool_schemas"]],
        "run_row": [str(v) for v in run_row],
        "incident_target": str(incident_row[0]),
        "control": {
            k: control[k]
            for k in (
                "incident_generation",
                "global_suspended",
                "global_generation",
                "target_suspended",
                "target_generation",
            )
        },
        "targets": [tuple(str(v) for v in t) for t in targets],
        "gates": [tuple(str(v) for v in g) for g in gates],
    }


def evidence_rows(harness: Harness, run: UUID) -> list[dict[str, Any]]:
    with psycopg.connect(harness.dsn) as conn:
        rows = conn.execute(
            "SELECT evidence_id, raw, raw_sha256, view::text, status, adopted, "
            "projection_revision FROM opspilot_evidence WHERE run_id=%s "
            "ORDER BY evidence_id",
            (str(run),),
        ).fetchall()
    return [
        {
            "evidence_id": r[0],
            "raw": bytes(r[1]),
            "raw_sha256": r[2],
            "view": r[3],
            "status": r[4],
            "adopted": r[5],
            "projection_revision": r[6],
        }
        for r in rows
    ]
