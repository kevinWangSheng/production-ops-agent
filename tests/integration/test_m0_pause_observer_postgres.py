"""Opt-in real PG contracts for round-07 work package 3: pause/resume, observer
authorization, persistent observation stream and the short DB outage chain.

No model or telemetry API is ever called. ``M0_CONTROL_POSTGRES=1`` opts in;
the outage test additionally needs ``M0_PG_OUTAGE=1`` because it stops and
restarts the dedicated 55431 lab server.
"""

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from scripts.m0 import postgres_lab
from scripts.m0.budget import PostgresBudget
from scripts.m0.contracts import BudgetError, RunContext
from scripts.m0.outcomes import (
    HealthProfile,
    IndependentObservation,
    Signal,
    Subject,
    Target,
    Window,
)
from scripts.m0.postgres_lab import DSN
from scripts.m0.step_store import Fence, StepStore

pytestmark = pytest.mark.skipif(
    os.environ.get("M0_CONTROL_POSTGRES") != "1",
    reason="explicit control-contract PG opt-in required",
)

VERSION = {"state": "v3", "provider": "deepseek", "tool": "fixture-v1"}


def response():
    return {
        "role": "assistant",
        "content": None,
        "reasoning_content": "SYNTHETIC_PRIVATE_SENTINEL",
        "tool_calls": [
            {
                "id": "call-0",
                "type": "function",
                "function": {"name": "read_fixture", "arguments": "{}"},
            }
        ],
    }


def tool_result():
    return {"role": "tool", "tool_call_id": "call-0", "content": '{"status":"ok"}'}


@pytest.fixture
def lab():
    ledger = PostgresBudget(DSN)
    ledger.install()
    store = StepStore(ledger)
    store.install()
    # Each test starts from an unpaused world; stale pauses from a killed run
    # would otherwise deny every other subject.
    for scope, target in {
        (r["scope"], r["target_key"])
        for r in store.pause_snapshot()["states"]
        if r["active"]
    }:
        store.resume(scope, target=target or None)
    run = RunContext(
        uuid4(), uuid4(), "deepseek", datetime.now(timezone.utc) + timedelta(minutes=4)
    )
    ledger.initialize(run.experiment_id, 100, run.deadline)
    target = f"svc-{uuid4()}"
    subject = store.accept(
        run, "pause-contract", {"request": "synthetic"}, VERSION, target=target
    )
    return store, ledger, run, subject, target


def audit_events(store, subject):
    return [(a["event"], a["accepted"]) for a in store.summary(subject)["audit"]]


def test_target_pause_denies_dispatch_and_resume_requires_new_run(lab):
    store, ledger, run, subject, target = lab
    fence = store.claim(subject, run, uuid4(), VERSION, lease_seconds=60)
    rid = uuid4()
    step, _ = store.dispatch(fence, "a", 0, {"messages": []}, rid, 20, 4, lambda: None)
    assert store.commit_response(fence, step, response(), request_id=rid)
    before = ledger.snapshot(run.experiment_id)

    control = store.pause("target", target=target, reason="human pause")
    assert control["subjects"] == [str(subject)]
    state = store.summary(subject)["state"]
    assert state["state"] == "paused" and state["generation"] == 1

    # New model dispatch, tool query and old-fence commits are all denied.
    with pytest.raises(BudgetError, match="PAUSED"):
        store.dispatch(fence, "a", 1, {"messages": []}, uuid4(), 20, 4, lambda: None)
    started = []
    with pytest.raises(BudgetError, match="PAUSED"):
        store.dispatch_tool(fence, step, 0, uuid4(), 4, lambda: started.append(1))
    assert started == []
    assert not store.commit_tool(fence, step, 0, tool_result(), attempt_id=uuid4())
    with pytest.raises(BudgetError, match="CONTROL_DENIED"):
        store.claim(subject, run, uuid4(), VERSION)
    # Explicit new Run is also denied while the pause is active.
    later = RunContext(run.experiment_id, uuid4(), "deepseek", run.deadline)
    with pytest.raises(BudgetError, match="PAUSED"):
        store.new_run(subject, 1, later, {"request": "synthetic"}, VERSION)

    assert store.resume("target", target=target)["subjects"] == [str(subject)]
    # Resume: old Run stays history, state/generation unchanged, no implicit claim.
    state = store.summary(subject)["state"]
    assert state["state"] == "paused" and state["generation"] == 1
    with pytest.raises(BudgetError, match="CONTROL_DENIED"):
        store.claim(subject, run, uuid4(), VERSION)
    assert ledger.snapshot(run.experiment_id) == before

    # Only an explicit new Run with the same experiment deadline/budget proceeds.
    assert store.new_run(subject, 1, later, {"request": "synthetic"}, VERSION) == 2
    new_fence = store.claim(subject, later, uuid4(), VERSION, lease_seconds=60)
    new_step, _ = store.dispatch(
        new_fence, "a", 0, {"messages": []}, uuid4(), 20, 4, lambda: None
    )
    assert new_step != step
    with pytest.raises(BudgetError, match="CONTROL_DENIED"):
        store.rebuild(fence, step)
    events = audit_events(store, subject)
    assert ("pause", True) in events and ("resume", True) in events
    assert events.count(("PAUSED", False)) == 3
    snapshot = store.pause_snapshot()
    assert [e["action"] for e in snapshot["events"] if e["target_key"] == target] == [
        "pause",
        "resume",
    ]
    assert (
        store.ledger.snapshot(run.experiment_id)["reserved"] == before["reserved"] + 20
    )


def test_global_pause_covers_untargeted_subjects_and_is_idempotent_safe(lab):
    store, ledger, run, subject, _ = lab
    plain = store.accept(run, "plain", {"request": "synthetic"}, VERSION)
    other_run = RunContext(run.experiment_id, uuid4(), "deepseek", run.deadline)
    store.pause("global")
    with pytest.raises(BudgetError, match="CONTROL_CONFLICT"):
        store.pause("global")
    assert store.summary(plain)["state"]["state"] == "paused"
    assert store.summary(subject)["state"]["state"] == "paused"
    with pytest.raises(BudgetError, match="PAUSED"):
        store.new_run(plain, 1, other_run, {"request": "synthetic"}, VERSION)
    store.resume("global")
    with pytest.raises(BudgetError, match="CONTROL_CONFLICT"):
        store.resume("global")
    assert store.new_run(plain, 1, other_run, {"request": "synthetic"}, VERSION) == 2


def test_observer_authorization_is_independent_of_investigation(lab):
    store, ledger, run, subject, target = lab
    fence = store.claim(subject, run, uuid4(), VERSION, lease_seconds=60)
    now = datetime.now(timezone.utc)
    observer_exp = uuid4()
    ledger.initialize(observer_exp, 10, run.deadline)
    observer_run = RunContext(observer_exp, uuid4(), "deepseek", run.deadline)
    same_experiment = RunContext(run.experiment_id, uuid4(), "deepseek", run.deadline)
    with pytest.raises(BudgetError, match="AUTHORIZATION_SCOPE_CONFLICT"):
        store.authorize_observer(
            subject,
            same_experiment,
            window_start=now,
            window_end=now + timedelta(minutes=2),
            query_limit=2,
        )
    store.authorize_observer(
        subject,
        observer_run,
        window_start=now,
        window_end=now + timedelta(minutes=2),
        query_limit=2,
    )
    # Observer queries run under their own window/limit and touch no fence.
    assert store.observe(subject, observer_run, uuid4(), lambda: "q1") == "q1"
    with pytest.raises(BudgetError, match="OBSERVATION_WINDOW_CLOSED"):
        store.observe(
            subject,
            observer_run,
            uuid4(),
            lambda: None,
            now=now + timedelta(minutes=3),
        )
    assert store.observe(subject, observer_run, uuid4(), lambda: "q2") == "q2"
    with pytest.raises(BudgetError, match="QUERY_LIMIT"):
        store.observe(subject, observer_run, uuid4(), lambda: None)
    # The observer identity can never claim or fence the investigation.
    with pytest.raises(BudgetError, match="IDENTITY_CONFLICT"):
        store.claim(subject, observer_run, uuid4(), VERSION)
    forged = Fence(subject, observer_run, fence.generation, fence.owner, fence.epoch)
    with pytest.raises(BudgetError, match="CONTROL_DENIED"):
        store.dispatch(forged, "a", 0, {"messages": []}, uuid4(), 5, 4, lambda: None)
    with pytest.raises(BudgetError, match="UNKNOWN_IDENTITY"):
        store.observe(subject, run, uuid4(), lambda: None)
    # Budgets stay separate: the investigation ledger saw no observer traffic.
    assert ledger.snapshot(run.experiment_id)["reserved"] == 0
    assert ledger.snapshot(observer_exp)["reserved"] == 0

    # A cancelled investigation is not revived by an active observer authorization.
    store.control(subject, 0, "cancel")
    with pytest.raises(BudgetError, match="CONTROL_DENIED"):
        store.claim(subject, run, uuid4(), VERSION)
    with pytest.raises(BudgetError, match="CONTROL_DENIED"):
        store.dispatch(fence, "a", 0, {"messages": []}, uuid4(), 5, 4, lambda: None)
    # Pause also denies observer queries with an audited denial.
    later_run = RunContext(observer_exp, uuid4(), "deepseek", run.deadline)
    store.authorize_observer(
        subject,
        later_run,
        window_start=now,
        window_end=now + timedelta(minutes=2),
        query_limit=1,
    )
    store.pause("target", target=target)
    with pytest.raises(BudgetError, match="PAUSED"):
        store.observe(subject, later_run, uuid4(), lambda: None)
    store.resume("target", target=target)
    events = audit_events(store, subject)
    assert events.count(("observer_authorized", True)) == 2
    assert events.count(("observer_query", True)) == 2
    assert ("OBSERVATION_WINDOW_CLOSED", False) in events
    assert ("QUERY_LIMIT", False) in events
    assert ("PAUSED", False) in events


def observation(target, revision, evidence, verdict="healthy"):
    window = Window(
        start=datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc),
        end=datetime(2026, 9, 12, 10, 5, tzinfo=timezone.utc),
    )
    return IndependentObservation(
        subject=Subject(
            id="s",
            kind="release_observation",
            target=target,
            control_generation=0,
            before_revision="rev-0",
            release_id="release-1",
        ),
        profile_revision=revision,
        window=window,
        signals=[
            Signal(name="errors", evidence_id=evidence, samples=1, verdict=verdict)
        ],
    )


def test_observation_stream_rejects_out_of_order_and_unknown_revision(lab):
    store, _, _, subject, _ = lab
    target = Target(
        integration_id="m0-otel",
        cluster_uid="local",
        namespace="default",
        resource_uid="svc-1",
        revision="rev-1",
    )
    profile = HealthProfile(
        revision="profile-v1",
        required_signals=["errors"],
        min_samples=1,
        freshness_seconds=60,
        required_window=Window(
            start=datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc),
            end=datetime(2026, 9, 12, 10, 5, tzinfo=timezone.utc),
        ),
    )
    t0 = datetime(2026, 9, 12, 10, 1, tzinfo=timezone.utc)
    first = store.record_observation(
        subject, observation(target, "profile-v1", "e-1"), profile, captured_at=t0
    )
    assert first["accepted"] and first["verdict"] == "healthy"
    newer = store.record_observation(
        subject,
        observation(target, "profile-v1", "e-2", "degraded"),
        profile,
        captured_at=t0 + timedelta(seconds=30),
    )
    assert newer["accepted"] and newer["verdict"] == "degraded"
    late = store.record_observation(
        subject,
        observation(target, "profile-v1", "e-late"),
        profile,
        captured_at=t0 + timedelta(seconds=10),
    )
    assert late == {
        **late,
        "accepted": False,
        "code": "OUT_OF_ORDER",
        "verdict": "unknown",
    }
    duplicate = store.record_observation(
        subject,
        observation(target, "profile-v1", "e-dup"),
        profile,
        captured_at=t0 + timedelta(seconds=30),
    )
    assert duplicate["code"] == "DUPLICATE_CAPTURE" and not duplicate["accepted"]
    old_profile = store.record_observation(
        subject,
        observation(target, "profile-v0", "e-old"),
        profile,
        captured_at=t0 + timedelta(seconds=60),
    )
    assert old_profile["code"] == "PROFILE_REVISION_MISMATCH"
    assert old_profile["verdict"] == "unknown" and not old_profile["accepted"]
    stream = store.observation_stream(subject)
    assert [row["code"] for row in stream] == [
        "ACCEPTED",
        "ACCEPTED",
        "OUT_OF_ORDER",
        "DUPLICATE_CAPTURE",
        "PROFILE_REVISION_MISMATCH",
    ]
    assert [row["evidence_ids"] for row in stream] == [
        ["e-1"],
        ["e-2"],
        ["e-late"],
        ["e-dup"],
        ["e-old"],
    ]
    assert stream[2]["captured_at"] == "2026-09-12T10:01:10+00:00"
    # A later valid sample is still accepted after rejected ones.
    assert store.record_observation(
        subject,
        observation(target, "profile-v1", "e-3"),
        profile,
        captured_at=t0 + timedelta(seconds=90),
    )["accepted"]
    audits = audit_events(store, subject)
    assert audits.count(("observation", True)) == 3
    assert audits.count(("observation", False)) == 3


@pytest.mark.skipif(
    os.environ.get("M0_PG_OUTAGE") != "1", reason="stops/restarts the PG lab server"
)
def test_short_db_outage_resumes_from_committed_state_without_double_commit(lab):
    store, ledger, run, subject, _ = lab
    fence = store.claim(subject, run, uuid4(), VERSION, lease_seconds=120)
    rid = uuid4()
    step, _ = store.dispatch(fence, "a", 0, {"messages": []}, rid, 20, 4, lambda: None)
    assert store.commit_response(fence, step, response(), request_id=rid)
    attempt = uuid4()
    store.dispatch_tool(fence, step, 0, attempt, 4, lambda: None)
    committed = store.summary(subject)

    postgres_lab.stop()
    try:
        with pytest.raises(BudgetError, match="STORAGE_UNAVAILABLE"):
            store.commit_tool(fence, step, 0, tool_result(), attempt_id=attempt)
        with pytest.raises(BudgetError, match="STORAGE_UNAVAILABLE"):
            store.dispatch_tool(fence, step, 0, uuid4(), 4, lambda: None)
    finally:
        postgres_lab.start()

    # Committed state is intact; the outage left no partial writes.
    assert store.summary(subject) == committed
    rebuilt = store.rebuild(fence, step)
    assert rebuilt["status"] == "pending_tools" and len(rebuilt["pending"]) == 1
    # Re-committing the same response is idempotent, not a duplicate step.
    assert store.commit_response(fence, step, response(), request_id=rid)
    assert store.commit_tool(fence, step, 0, tool_result(), attempt_id=attempt)
    assert store.commit_tool(fence, step, 0, tool_result(), attempt_id=attempt)
    with pytest.raises(BudgetError, match="OPERATION_NOT_PENDING"):
        store.dispatch_tool(fence, step, 0, uuid4(), 4, lambda: None)
    ready = store.rebuild(fence, step)
    assert ready["status"] == "ready" and len(ready["messages"]) == 2
    with ledger._transaction() as conn:
        counts = conn.execute(
            "SELECT (SELECT count(*) FROM m0_v3_step WHERE run_id=%s) AS steps,"
            "(SELECT count(*) FROM m0_v3_operation WHERE step=%s) AS operations,"
            "(SELECT count(*) FROM m0_v3_tool_attempt WHERE step=%s) AS attempts,"
            "(SELECT count(*) FROM m0_v3_dispatch WHERE step=%s) AS dispatches",
            (run.run_id, step, step, step),
        ).fetchone()
    assert dict(counts) == {"steps": 1, "operations": 1, "attempts": 1, "dispatches": 1}
    assert ledger.snapshot(run.experiment_id)["reserved"] == 20
