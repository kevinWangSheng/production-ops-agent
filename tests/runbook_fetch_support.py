"""PostgreSQL harness for the C2b contract tests (#566): ``fetch_skill`` and
the v7 runbook catalog on the product path.

Written by an independent test author from the C2 interface (sections 4-7,
「C2b 签名级细节」) without reading the implementation. A scratch database
migrated to head; the real workbench under the ``otel-demo-ext`` profile
with ``OPSPILOT_KNOWLEDGE_TOOL=1`` (runbook routes, the catalog source and
the F13 step 4 knowledge store wired in); the real ``InvestigationRunner``,
``DeepSeekClient`` and extension executor driven by a scripted provider
double, logged in as a throwaway ``opspilot_worker`` role so every read the
tool makes runs with the worker's grants only. Jaeger and Prometheus answer
from the recorded-response ``FakeOpener`` (F7 R4); no cluster is contacted.

Seams beyond the interface (``# ASSUMPTION:``): ``Workbench`` takes the
catalog source as ``runbook_catalog=`` and reads it with its
``knowledge_max_age_seconds``; ``create_app(..., runbooks=)`` as in C2a.
Adapting that wiring is not an assertion change.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from opspilot.investigation.client import DeepSeekClient
from opspilot.investigation.runner import InvestigationRunner
from opspilot.knowledge import KnowledgeStore
from opspilot.persistence import DurableStore, PoolConfig
from opspilot.tools.otel_demo import TARGET_ID
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
from tests.acceptance.f7_hostile_support import jaeger_payload, prometheus_payload
from tests.acceptance.f7_pg_support import (
    MODEL_KEY,
    ScriptedProvider,
    assistant,
    tool_views,
)
from tests.m1_tool_support import FakeClock
from tests.m1_web_support import (
    EVENT_TOKEN,
    ORIGIN,
    UI_PASSWORD,
    UI_USER,
    basic,
    post_form,
    same_origin,
)
from tests.runbook_support import REVIEWER, REVIEWER_PASSWORD, RUNBOOK_TABLES
from tests.test_m1_otel_demo_contract import (
    GOOD_EXPR,
    JAEGER_URL,
    PROMETHEUS_URL,
    WINDOW_END,
    FakeOpener,
)

SWITCH = "OPSPILOT_KNOWLEDGE_TOOL"
TOOL = "fetch_skill"
CATALOG = "runbook_catalog"
V7 = "opspilot-investigation-input-v7"
DAY = 86400
POOL = PoolConfig(min_size=0, max_size=4, timeout=5.0)
UI = {**basic(), **same_origin()}
TIME_REF = "policy-window-1"
OTHER_TARGET = "payments-prod"


def opener() -> FakeOpener:
    return FakeOpener(
        by_query={GOOD_EXPR: prometheus_payload()},
        routes={"/api/traces": jaeger_payload()},
    )


def profile_env(ca_file: Path, *, switch: str = "1") -> dict[str, str]:
    return {
        PROFILE_ENV: "otel-demo-ext",
        SWITCH: switch,
        "OPSPILOT_K8S_API_URL": "https://127.0.0.1:16443",
        "OPSPILOT_K8S_TOKEN": "k8s-ro-bbbbbbbb566",
        "OPSPILOT_K8S_CA_FILE": str(ca_file),
        "OPSPILOT_K8S_NAMESPACE": "otel-demo",
        "OPSPILOT_OTEL_PROMETHEUS_URL": PROMETHEUS_URL,
        "OPSPILOT_OTEL_JAEGER_URL": JAEGER_URL,
    }


@contextmanager
def bare_login(admin_dsn: str) -> Iterator[str]:
    """A LOGIN role with no grant at all: every runbook read it makes is
    refused by PostgreSQL (a storage failure for the catalog read)."""
    name = f"opspilot_bare_t_{uuid4().hex[:8]}"
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(name)))
    try:
        yield make_conninfo(admin_dsn, user=name)
    finally:
        from opspilot.persistence import close_pools

        close_pools()
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = %s",
                (name,),
            )
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))


def _auth() -> AuthConfig:
    return AuthConfig(
        ui_users={
            UI_USER: hash_password(UI_PASSWORD),
            REVIEWER: hash_password(REVIEWER_PASSWORD),
        },
        event_tokens={token_digest(EVENT_TOKEN): "alertmanager-prod"},
        auth_revision="auth-rev-c2b",
        allowed_origins=frozenset({ORIGIN}),
    )


@dataclass
class FetchHarness:
    dsn: str
    worker_dsn: str
    env: dict[str, str]
    store: DurableStore
    events: DurableEventLog
    evidence: DurableEvidenceStore
    knowledge: KnowledgeStore
    runbooks: Any
    workbench: Workbench
    app: Any
    profile: Any
    opener: FakeOpener
    patch: Any = None
    closers: list[Any] = field(default_factory=list)

    # -- web: incidents --------------------------------------------------

    def submit(
        self, question: str, *, target_id: str = TARGET_ID, app=None
    ) -> tuple[UUID, UUID]:
        response = post_form(
            app or self.app,
            "/intake/ui",
            {
                "target_id": target_id,
                "question": question,
                "idempotency_key": f"c2b-{uuid4()}",
            },
            headers=UI,
        )
        assert response.status == 201, response.text
        body = response.json()
        return UUID(body["incident_id"]), UUID(body["run_id"])

    def recorded(self, incident: UUID) -> dict[str, Any]:
        return self.store.rebuild(incident)["run"]["input"]

    def catalog(self, incident: UUID) -> dict[str, Any] | None:
        return self.recorded(incident)["scope_facts"].get(CATALOG)

    def control(self, incident: UUID, action: str, *, app=None):
        generation = self.store.control_state(incident)["incident_generation"]
        return post_form(
            app or self.app,
            f"/incidents/{incident}/control",
            {
                "action": action,
                "expected_generation": str(generation),
                "idempotency_key": f"c2b-{uuid4().hex}",
            },
            headers=UI,
        )

    def workbench_app(self, *, catalog: Any, switch: str):
        """Another web over the same store: a deployment whose switch is
        ``switch`` and whose catalog source is ``catalog``."""
        profile = select_profile({**self.env, SWITCH: switch})
        bench = _workbench(
            self.store, self.events, self.evidence, profile, self.knowledge, catalog
        )
        return create_app(
            bench,
            Authenticator(_auth()),
            DurableClock(self.store),
            runbooks=self.runbooks,
        )

    # -- worker ------------------------------------------------------------

    def run(self, incident: UUID, provider: ScriptedProvider):
        """One worker attempt: product runner, client and executor, as the
        worker login."""
        if self.patch is not None:
            # This harness's backend double, whichever harness was built last.
            self.patch.setattr(urllib.request, "build_opener", lambda *_: self.opener)
        store = DurableStore(self.worker_dsn, pool=POOL)
        self.closers.append(store)
        events = DurableEventLog(store)
        evidence = DurableEvidenceStore(store)
        clock = DurableClock(store)
        runner = InvestigationRunner(
            store=store,
            worker=Worker.create(
                store,
                self.profile.versions(),
                None
                if self.profile.standing_versions is None
                else self.profile.standing_versions(),
            ),
            model=DeepSeekClient(MODEL_KEY, opener=provider),
            executor_factory=self.profile.executor_factory(store, evidence, clock),
            clock=clock,
            lease_seconds=30,
            events=events,
            evidence=evidence,
        )
        return runner.resume(incident)

    # -- web: runbooks (C2a JSON API) --------------------------------------

    def post(self, path: str, fields: dict[str, Any], *, user: str = REVIEWER):
        headers = {"accept": "application/json", "origin": ORIGIN}
        headers.update(
            basic() if user == UI_USER else basic(REVIEWER, REVIEWER_PASSWORD)
        )
        clean = {k: str(v) for k, v in fields.items() if v is not None}
        return post_form(self.app, path, clean, headers=headers)

    def rb_submit(self, **overrides: Any):
        fields: dict[str, Any] = {
            "resource_uid": TARGET_ID,
            "name": f"runbook-{uuid4().hex[:10]}",
            "description": "Steps for checkout errors after a deploy.",
            "content": "# Checkout errors\n\n1. Read the error rate.\n",
            "source_label": "contract test",
            "source_ref": "tests/runbook_fetch_support.py",
            "idempotency_key": uuid4().hex,
        }
        fields.update(overrides)
        return self.post("/runbooks", fields, user=UI_USER)

    def rb_submitted(self, **overrides: Any) -> str:
        response = self.rb_submit(**overrides)
        assert response.status == 200, response.text
        return response.json()["submission_id"]

    def rb_review(self, submission_id: str, action: str = "approve", **overrides):
        fields: dict[str, Any] = {
            "action": action,
            "expected_generation": self.submission_generation(submission_id),
            "idempotency_key": uuid4().hex,
        }
        superseded = self.one(
            "SELECT supersedes_entry_id FROM opspilot_runbook_submissions "
            "WHERE submission_id::text = %s",
            (submission_id,),
        )["supersedes_entry_id"]
        if superseded is not None:
            fields["entry_generation"] = self.entry_generation(str(superseded))
        if action == "reject":
            fields.setdefault("reason", "not a real procedure")
        fields.update(overrides)
        return self.post(f"/runbooks/submissions/{submission_id}/review", fields)

    def rb_publish(self, **overrides: Any) -> dict[str, Any]:
        """Submit and approve; returns the approve result plus the fields."""
        fields = {
            "name": f"runbook-{uuid4().hex[:10]}",
            "content": f"# Runbook\n\n1. Step {uuid4().hex}.\n",
            **overrides,
        }
        submission_id = self.rb_submitted(**fields)
        response = self.rb_review(submission_id)
        assert response.status == 200, response.text
        return {**response.json(), "submission_id": submission_id, **fields}

    def rb_revoke(self, entry_id: str, revision: int, **overrides: Any):
        fields: dict[str, Any] = {
            "revision": revision,
            "reason": "contract test revoke",
            "expected_generation": self.entry_generation(entry_id),
            "idempotency_key": uuid4().hex,
        }
        fields.update(overrides)
        response = self.post(f"/runbooks/{entry_id}/revoke", fields)
        assert response.status == 200, response.text
        return response.json()

    # -- owner reads ---------------------------------------------------------

    def rows(self, query: Any, params: tuple = ()) -> list[dict[str, Any]]:
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            return list(conn.execute(query, params).fetchall())

    def one(self, query: Any, params: tuple = ()) -> dict[str, Any]:
        [row] = self.rows(query, params)
        return row

    def submission_generation(self, submission_id: str) -> int:
        return self.one(
            "SELECT max(resulting_generation) AS g FROM opspilot_runbook_audit "
            "WHERE object_kind = 'runbook_submission' AND object_id::text = %s",
            (submission_id,),
        )["g"]

    def entry_generation(self, entry_id: str) -> int:
        return self.one(
            "SELECT generation FROM opspilot_runbook_entries WHERE entry_id::text = %s",
            (entry_id,),
        )["generation"]

    def revision_row(self, entry_id: str, revision: int) -> dict[str, Any]:
        return self.one(
            "SELECT * FROM opspilot_runbook_revisions "
            "WHERE entry_id::text = %s AND revision = %s",
            (entry_id, revision),
        )

    def runbook_counts(self) -> dict[str, int]:
        with psycopg.connect(self.dsn) as conn:
            return {
                table: conn.execute(
                    sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
                ).fetchone()[0]
                for table in RUNBOOK_TABLES
            }

    def close(self) -> None:
        for closer in reversed(self.closers):
            closer.close()


def _workbench(store, events, evidence, profile, knowledge, catalog) -> Workbench:
    return Workbench(
        incidents=DurableIncidentStore(store),
        events=events,
        evidence=evidence,
        ledger=DurableWebLedger(store),
        run_versions=profile.versions(),
        standing_run_versions=None
        if profile.standing_versions is None
        else profile.standing_versions(),
        run_seconds=600,
        # The face clock fixes the policy frame to end at the recorded window
        # the replayed backend bodies answer for; the worker keeps real time.
        tool_face=profile.face(FakeClock(start=WINDOW_END)),
        knowledge=knowledge,
        knowledge_max_age_seconds=DAY,
        # ASSUMPTION: the keyword the interface names (``Workbench`` gains
        # ``runbook_catalog: RunbookCatalogSource | None``).
        runbook_catalog=catalog,
    )


def build_harness(
    dsn: str, worker_dsn: str, monkeypatch, ca_file: Path, *, fake=None
) -> FetchHarness:
    from opspilot.knowledge.runbooks import RunbookStore

    fake = fake or opener()
    monkeypatch.delenv("OPSPILOT_OTEL_TOKEN", raising=False)
    # The transports build their opener when the factory is built.
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: fake)
    env = profile_env(ca_file)
    profile = select_profile(env)
    store = DurableStore(dsn, pool=POOL)
    store.install()
    events = DurableEventLog(store)
    evidence = DurableEvidenceStore(store)
    knowledge = KnowledgeStore(dsn, pool=POOL)
    runbooks = RunbookStore(dsn)
    bench = _workbench(store, events, evidence, profile, knowledge, runbooks)
    app = create_app(
        bench, Authenticator(_auth()), DurableClock(store), runbooks=runbooks
    )
    closers: list[Any] = [store, knowledge]
    if callable(getattr(runbooks, "close", None)):
        closers.append(runbooks)
    return FetchHarness(
        dsn=dsn,
        worker_dsn=worker_dsn,
        env=env,
        store=store,
        events=events,
        evidence=evidence,
        knowledge=knowledge,
        runbooks=runbooks,
        workbench=bench,
        app=app,
        profile=profile,
        opener=fake,
        patch=monkeypatch,
        closers=closers,
    )


# -- scripted rounds --------------------------------------------------------------


def fetch(call_id: str, skill_id: Any, **extra: Any) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": TOOL,
            "arguments": json.dumps({"skill_id": skill_id, **extra}),
        },
    }


def traces(call_id: str = "traces", service: str = "checkout") -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "traces_search",
            "arguments": json.dumps({"service": service}),
        },
    }


def views_by_call(messages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["tool_call_id"]: item["view"] for item in tool_views(messages)}


def report_round(
    *, fact_call: str = "traces", recommend_call: str | None = None
) -> Callable[[list[dict[str, Any]]], dict[str, Any]]:
    """The final answer: a fact citing this Run's traces evidence and, when
    ``recommend_call`` is given, a recommendation citing that fetch_skill
    view's evidence (C2-8: allowed; a fact citing it is not)."""

    def build(messages):
        views = views_by_call(messages)
        claims = [
            {
                "kind": "fact",
                "text": "The authorized traces search returned an erroring span.",
                "evidence_ids": [views[fact_call]["evidence_id"]],
                "target_refs": [TARGET_ID],
                "time_scope_ref": TIME_REF,
            }
        ]
        if recommend_call is not None:
            claims.append(
                {
                    "kind": "recommendation",
                    "text": "Follow the reviewed runbook's steps with this Run's tools.",
                    "evidence_ids": [views[recommend_call]["evidence_id"]],
                    "target_refs": [TARGET_ID],
                    "time_scope_ref": TIME_REF,
                }
            )
        payload = {
            "schema_version": "m0-report-v2",
            "assessment_status": "completed",
            "conclusion": "supported",
            "summary": "Checkout traces show an erroring payment span.",
            "claims": claims,
            "gaps": ["No HealthProfile comparison was made."],
            "next_steps": ["Have a human compare the sample against the SLO."],
        }
        return assistant(content=json.dumps(payload))

    return build


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def gated() -> bool:
    return os.environ.get("M1_DURABLE_POSTGRES") == "1"
