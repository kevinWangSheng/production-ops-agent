"""OTel Demo tool profile on PostgreSQL (M1-01): the durable control state
the profile's ``DurableControl`` reads, the executor's own control check
firing on it, and one workbench -> worker Run under the ``otel-demo``
profile with a scripted model and a fake opener serving the recorded
backend responses.

Written from the C3 contract and the public interfaces, not from the
implementation. Same opt-in and lab database as the other M1 PG tests
(``M1_DURABLE_POSTGRES=1``, ``scripts/m0/postgres_lab.py``, port 55431).
No network: every backend read is answered by the fake opener.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from opspilot.investigation.runner import InvestigationRunner
from opspilot.persistence import DurableStore
from opspilot.tools import ReadOnlyToolExecutor, ToolRequest
from opspilot.tools.otel_demo import (
    TARGET_ID,
    OtelDemoConfig,
    otel_demo_executor_factory,
    otel_demo_face,
    otel_demo_versions,
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
from opspilot.worker import Worker
from opspilot.worker_main import WorkerLoop
from scripts.m0.postgres_lab import DSN
from tests.m1_investigation_support import ScriptedModel, reply, report_json
from tests.m1_tool_support import FakeClock, RecordingSink
from tests.m1_web_support import (
    EVENT_TOKEN,
    ORIGIN,
    UI_PASSWORD,
    UI_USER,
)
from tests.test_m1_otel_demo_contract import (
    JAEGER_URL,
    NOW,
    PROMETHEUS_URL,
    WINDOW,
    WINDOW_END,
    FakeOpener,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

QUESTION = "Why is checkout erroring?"
CONFIG = OtelDemoConfig(prometheus_url=PROMETHEUS_URL, jaeger_url=JAEGER_URL)


def _store() -> DurableStore:
    """The lab store with the global gate open (an earlier test may have
    left it closed)."""
    store = DurableStore(DSN)
    store.install()
    with store.transaction() as conn:
        row = conn.execute(
            "SELECT global_suspended,global_generation FROM opspilot_scope_controls WHERE scope_id=1"
        ).fetchone()
    if row["global_suspended"]:
        store.set_global_suspension(
            False, expected_generation=row["global_generation"], actor="operator"
        )
    return store


def _accept(store: DurableStore, *, target: UUID | None, versions=None):
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"otel-contract-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=5),
        budget_limit=5,
        versions=versions or otel_demo_versions(),
        target_id=target,
    )
    return incident, run


def _install_opener(monkeypatch, opener: FakeOpener) -> None:
    """The factory takes no opener: replace the default builder."""
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: opener)


def _input(run_id: UUID):
    """A Run input whose ``policy-window-1`` is exactly the recorded window."""
    return otel_demo_face(FakeClock(start=WINDOW_END)).input_for(
        run_id=str(run_id),
        question=QUESTION,
        target_id=TARGET_ID,
        deadline=NOW + timedelta(minutes=10),
        model_requests=2,
    )


def _call(tool: str, params: dict, *, index: int) -> ToolRequest:
    return ToolRequest(
        step_id="step-1",
        tool_index=index,
        tool_name=tool,
        target_ref=TARGET_ID,
        params=params,
        window=WINDOW.as_json(),
    )


# -- (a) DurableStore.control_state -------------------------------------------


# -- (b) DurableControl through a real executor ----------------------------------


def _executor(store: DurableStore, lease, opener: FakeOpener, monkeypatch):
    _install_opener(monkeypatch, opener)
    factory = otel_demo_executor_factory(
        store, evidence=RecordingSink(), clock=FakeClock(start=NOW), config=CONFIG
    )
    executor = factory(lease, _input(lease.run_id))
    assert isinstance(executor, ReadOnlyToolExecutor)
    return executor


# -- (c) workbench -> worker Run under the otel-demo profile ---------------------


class _Only:
    """The store's listing narrowed to one incident (the lab database keeps
    every earlier test's rows)."""

    def __init__(self, store, subject):
        self.store, self.subject = store, subject

    def claimable_incidents(self, *, limit=20):
        return tuple(
            i for i in self.store.claimable_incidents(limit=1000) if i == self.subject
        )


def _build(profile):
    store = _store()
    events = DurableEventLog(store)
    events.install()
    evidence = DurableEvidenceStore(store)
    evidence.install()
    ledger = DurableWebLedger(store)
    ledger.install()
    workbench = Workbench(
        incidents=DurableIncidentStore(store),
        events=events,
        evidence=evidence,
        ledger=ledger,
        run_versions=profile.versions(),
        run_seconds=600,
        # The face's clock fixes the Run's policy window to the recorded
        # window the fixtures answer for; the worker keeps the real clock.
        tool_face=profile.face(FakeClock(start=WINDOW_END)),
    )
    config = AuthConfig(
        ui_users={UI_USER: hash_password(UI_PASSWORD)},
        event_tokens={token_digest(EVENT_TOKEN): "alertmanager-prod"},
        auth_revision="auth-rev-pg-otel",
        allowed_origins=frozenset({ORIGIN}),
    )
    app = create_app(workbench, Authenticator(config), DurableClock(store))
    return app, workbench, store, events, evidence


def _worker(profile, store, events, evidence, replies, *, subject):
    clock = DurableClock(store)
    model = ScriptedModel(replies)
    runner = InvestigationRunner(
        store=store,
        worker=Worker.create(store, profile.versions()),
        model=model,
        executor_factory=profile.executor_factory(store, evidence, clock),
        clock=clock,
        lease_seconds=5,
        events=events,
        evidence=evidence,
    )
    loop = WorkerLoop(
        queue=_Only(store, subject),
        runner=runner,
        sweeper=store,
        events=events,
        stop=threading.Event(),
        poll_seconds=0.05,
    )
    return loop, model


def _final_report(call):
    """Cite the evidence_id of the last tool view against the OTel target."""
    evidence_id = None
    for message in reversed(call.messages):
        if message.get("role") == "tool":
            evidence_id = json.loads(message["content"])["evidence_id"]
            break
    return reply(
        content=report_json(evidence_id=evidence_id or "missing", target_ref=TARGET_ID)
    )


# Re-exported for the kept acceptance tests (2026-11-18 test-suite slim).
# ruff: noqa: E402
from tests.test_m1_otel_demo_contract import (
    CHECKOUT_BODY,  # noqa: F401
    GOOD_EXPR,  # noqa: F401
)
