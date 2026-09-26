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
from opspilot.persistence import DurableStore, PersistenceError
from opspilot.tools import ReadOnlyToolExecutor, ToolRequest
from opspilot.tools.otel_demo import (
    METRICS_TOOL,
    TARGET_ID,
    TRACES_TOOL,
    DurableControl,
    OtelDemoConfig,
    otel_demo_executor_factory,
    otel_demo_face,
    otel_demo_versions,
)
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
from opspilot.worker_main import WorkerLoop
from scripts.m0.postgres_lab import DSN
from tests.m1_investigation_support import ScriptedModel, reply, report_json, tool_call
from tests.m1_tool_support import FakeClock, RecordingSink
from tests.m1_web_support import (
    EVENT_TOKEN,
    ORIGIN,
    UI_PASSWORD,
    UI_USER,
    basic,
    post_form,
    same_origin,
)
from tests.test_m1_otel_demo_contract import (
    CHECKOUT_BODY,
    CHECKOUT_TRACES,
    GOOD_EXPR,
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


def test_control_state_reflects_incident_global_and_target_control():
    store = _store()
    target = store.register_target(f"otel-contract-target-{uuid4()}")
    incident, run = _accept(store, target=target)
    lease = store.claim(incident, run, uuid4(), otel_demo_versions())

    before = store.control_state(incident)
    assert set(before) == {
        "incident_generation",
        "incident_state",
        "global_suspended",
        "global_generation",
        "target_suspended",
        "target_generation",
    }
    assert before["incident_generation"] == lease.control_generation
    assert before["global_generation"] == lease.global_suspension_generation
    assert before["target_generation"] == lease.target_suspension_generation
    assert before["global_suspended"] is False and before["target_suspended"] is False
    assert before["incident_state"] == "running"

    # Global suspension: the flag and its generation move; the incident's
    # own generation does not.
    global_generation = store.set_global_suspension(
        True, expected_generation=lease.global_suspension_generation, actor="operator"
    )
    try:
        during = store.control_state(incident)
        assert during["global_suspended"] is True
        assert during["global_generation"] == global_generation
        assert during["global_generation"] == before["global_generation"] + 1
        assert during["target_suspended"] is False
        assert during["target_generation"] == before["target_generation"]
        assert during["incident_generation"] == before["incident_generation"]
        assert during["incident_state"] == "paused"
    finally:
        store.set_global_suspension(
            False, expected_generation=global_generation, actor="operator"
        )
    released = store.control_state(incident)
    assert released["global_suspended"] is False
    assert released["global_generation"] == global_generation  # release: same gen

    # Target suspension: only this incident's target moves.
    target_generation = store.set_target_suspension(
        target,
        True,
        expected_generation=lease.target_suspension_generation,
        actor="operator",
    )
    try:
        state = store.control_state(incident)
        assert state["target_suspended"] is True
        assert state["target_generation"] == target_generation
        assert state["target_generation"] == before["target_generation"] + 1
        assert state["global_suspended"] is False
        other_incident, _ = _accept(store, target=None)
        other = store.control_state(other_incident)
        assert other["target_suspended"] is False and other["target_generation"] == 0
    finally:
        store.set_target_suspension(
            target, False, expected_generation=target_generation, actor="operator"
        )
    assert store.control_state(incident)["target_suspended"] is False

    # A human decision on the incident bumps the incident generation.
    generation = store.control(
        incident, before["incident_generation"], "pause", "operator"
    )
    assert generation == before["incident_generation"] + 1
    assert store.control_state(incident)["incident_generation"] == generation

    with pytest.raises(PersistenceError, match="UNKNOWN_IDENTITY"):
        store.control_state(uuid4())
    with pytest.raises(PersistenceError, match="INVALID_INPUT"):
        store.control_state(str(incident))  # type: ignore[arg-type]


# -- (b) DurableControl through a real executor ----------------------------------


def _executor(store: DurableStore, lease, opener: FakeOpener, monkeypatch):
    _install_opener(monkeypatch, opener)
    factory = otel_demo_executor_factory(
        store, evidence=RecordingSink(), clock=FakeClock(start=NOW), config=CONFIG
    )
    executor = factory(lease, _input(lease.run_id))
    assert isinstance(executor, ReadOnlyToolExecutor)
    return executor


def test_durable_control_snapshot_mirrors_the_store(monkeypatch):
    store = _store()
    target = store.register_target(f"otel-contract-target-{uuid4()}")
    incident, run = _accept(store, target=target)
    lease = store.claim(incident, run, uuid4(), otel_demo_versions())
    executor = _executor(store, lease, FakeOpener(), monkeypatch)
    snapshot = DurableControl(store).snapshot(executor.scope)
    assert snapshot.control_generation == lease.control_generation
    assert snapshot.global_suspension_generation == lease.global_suspension_generation
    assert snapshot.target_suspension_generation == lease.target_suspension_generation
    assert snapshot.suspended is False
    generation = store.set_target_suspension(
        target,
        True,
        expected_generation=lease.target_suspension_generation,
        actor="operator",
    )
    try:
        suspended = DurableControl(store).snapshot(executor.scope)
        assert suspended.suspended is True
        assert suspended.target_suspension_generation == generation
    finally:
        store.set_target_suspension(
            target, False, expected_generation=generation, actor="operator"
        )
    released = DurableControl(store).snapshot(executor.scope)
    assert released.suspended is False
    assert released.target_suspension_generation == generation


def test_a_global_suspension_makes_the_executor_deny_without_contacting_the_source(
    monkeypatch,
):
    """The executor's *own* control check (not the store's write fence)
    fires on the durable control state: SUSPENDED while the gate is closed,
    CONTROL_GENERATION_CHANGED after the release, and the transport is never
    contacted for either."""
    store = _store()
    target = store.register_target(f"otel-contract-target-{uuid4()}")
    incident, run = _accept(store, target=target)
    lease = store.claim(incident, run, uuid4(), otel_demo_versions())
    opener = FakeOpener(by_query={GOOD_EXPR: CHECKOUT_BODY})
    executor = _executor(store, lease, opener, monkeypatch)

    first = executor.execute(_call(METRICS_TOOL, {"expr": GOOD_EXPR}, index=0))
    assert (first.status, first.reason) == ("ok", None), first.model_view
    assert first.evidence is not None and first.evidence.raw == CHECKOUT_BODY
    assert len(opener.requests) == 1
    assert store.run_usage(run)["tool_operations_used"] == 1

    generation = store.set_global_suspension(
        True, expected_generation=lease.global_suspension_generation, actor="operator"
    )
    try:
        denied = executor.execute(_call(METRICS_TOOL, {"expr": GOOD_EXPR}, index=1))
        assert (denied.status, denied.reason) == ("denied", "SUSPENDED")
        assert denied.source_contact == "none" and denied.operation.sent is False
        assert denied.evidence is None and denied.model_view["content"] is None
        assert len(opener.requests) == 1
    finally:
        store.set_global_suspension(
            False, expected_generation=generation, actor="operator"
        )
    after = executor.execute(_call(TRACES_TOOL, {"service": "checkout"}, index=2))
    assert (after.status, after.reason) == ("denied", "CONTROL_GENERATION_CHANGED")
    assert after.operation.sent is False
    assert len(opener.requests) == 1
    # Nothing was charged for the two refused calls.
    assert store.run_usage(run)["tool_operations_used"] == 1


def test_a_target_suspension_makes_the_executor_deny_this_target(monkeypatch):
    store = _store()
    target = store.register_target(f"otel-contract-target-{uuid4()}")
    incident, run = _accept(store, target=target)
    lease = store.claim(incident, run, uuid4(), otel_demo_versions())
    opener = FakeOpener(routes={"/api/traces": CHECKOUT_TRACES})
    executor = _executor(store, lease, opener, monkeypatch)
    generation = store.set_target_suspension(
        target,
        True,
        expected_generation=lease.target_suspension_generation,
        actor="operator",
    )
    try:
        denied = executor.execute(_call(TRACES_TOOL, {"service": "checkout"}, index=0))
        assert (denied.status, denied.reason) == ("denied", "SUSPENDED")
        assert not opener.called
    finally:
        store.set_target_suspension(
            target, False, expected_generation=generation, actor="operator"
        )
    after = executor.execute(_call(TRACES_TOOL, {"service": "checkout"}, index=1))
    assert (after.status, after.reason) == ("denied", "CONTROL_GENERATION_CHANGED")
    assert not opener.called


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


def test_a_workbench_run_under_the_otel_profile_records_real_shaped_evidence(
    monkeypatch,
):
    profile = select_profile({PROFILE_ENV: "otel-demo"})
    assert profile.name == "otel-demo"
    monkeypatch.setenv("OPSPILOT_OTEL_PROMETHEUS_URL", PROMETHEUS_URL)
    monkeypatch.setenv("OPSPILOT_OTEL_JAEGER_URL", JAEGER_URL)
    monkeypatch.delenv("OPSPILOT_OTEL_TOKEN", raising=False)
    opener = FakeOpener(
        by_query={GOOD_EXPR: CHECKOUT_BODY}, routes={"/api/traces": CHECKOUT_TRACES}
    )
    _install_opener(monkeypatch, opener)

    app, workbench, store, events, evidence = _build(profile)
    submitted = post_form(
        app,
        "/intake/ui",
        {
            "target_id": TARGET_ID,
            "question": QUESTION,
            "idempotency_key": f"otel-{uuid4()}",
        },
        headers={**basic(), **same_origin()},
    )
    assert submitted.status == 201, submitted.text
    subject, run_id = (
        UUID(submitted.json()["incident_id"]),
        UUID(submitted.json()["run_id"]),
    )
    recorded = store.rebuild(subject)["run"]["input"]
    assert recorded is not None
    assert (
        recorded["evidence_context"]["time_policies"][0]["window"] == WINDOW.as_json()
    )
    assert [s["function"]["name"] for s in recorded["tool_schemas"]] == [
        METRICS_TOOL,
        TRACES_TOOL,
    ]

    tool_round = reply(
        tool_calls=[
            tool_call(
                call_id="call-1",
                name=METRICS_TOOL,
                arguments=json.dumps({"expr": GOOD_EXPR, "step_seconds": 30}),
            )
        ],
        finish="tool_calls",
    )
    loop, model = _worker(
        profile, store, events, evidence, [tool_round, _final_report], subject=subject
    )
    results = loop.poll_once()
    assert [(i, o.status, o.reason) for i, o in results] == [
        (subject, "published", None)
    ]
    assert len(model.calls) == 2
    # The read really went to the (fake) Prometheus, as a GET, once.
    assert len(opener.requests) == 1
    assert opener.requests[0].get_method() == "GET"
    assert opener.requests[0].full_url.startswith(
        PROMETHEUS_URL + "/api/v1/query_range"
    )
    # The tool view the model saw resolves to committed durable evidence
    # holding the exact backend bytes.
    tool_messages = [
        m for call in model.calls for m in call.messages if m.get("role") == "tool"
    ]
    assert tool_messages, "no tool view reached the model"
    view = json.loads(tool_messages[-1]["content"])
    assert view["status"] == "ok" and view["adopted"] is True
    assert view["tool"] == METRICS_TOOL and view["target_id"] == TARGET_ID
    stored = evidence.get(view["evidence_id"])
    assert stored is not None
    assert stored.raw == CHECKOUT_BODY and stored.committed is True
    assert stored.run_id == str(run_id) and stored.adopted is True
    assert stored.view["content"] == json.loads(CHECKOUT_BODY)["data"]["result"]
    usage = store.run_usage(run_id)
    assert usage["tool_operations_used"] >= 1 and usage["tool_seconds_used"] >= 0
    rebuilt = store.rebuild(subject)
    assert rebuilt["run"]["state"] == "completed" and rebuilt["conclusion"] is not None
    snapshot = workbench.snapshot(subject)
    assert snapshot["run"]["state"] == "completed"
    assert snapshot["report"] is not None and snapshot["report"]["parsed"] is True
    kinds = [e.kind for e in events.read_after(subject, 0, limit=1000)]
    assert kinds[-1] == "run_completed" and "tool_committed" in kinds
    assert loop.poll_once() == []


def test_a_fixture_profile_worker_does_not_claim_an_otel_profile_run():
    """C3 §5: a Run recorded under the otel-demo versions is blocked on a
    claim by a worker composed with the fixture profile."""
    store = _store()
    incident, run = _accept(store, target=None, versions=otel_demo_versions())
    fixture = select_profile({})
    assert fixture.versions() != otel_demo_versions()
    with pytest.raises(PersistenceError, match="INCOMPATIBLE_STATE"):
        store.claim(incident, run, uuid4(), fixture.versions())
