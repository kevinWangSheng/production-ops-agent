"""M1-01 acceptance on real PostgreSQL: ``IncidentScenario -> IncidentOutcome``.

Fresh-context acceptance tests (AGENTS.md 独立审查 / 验证与汇报). Every Run
state and every marker asserted here is produced by product code paths --
``DurableStore``, ``InvestigationRunner`` + ``Worker``, ``control()``,
``set_global_suspension()`` and the deadline sweep -- and read back through
the product's own readers (``DurableStore.rebuild``,
``DurableIncidentStore.control_audit``, ``DurableEventLog.read_after``). The
seam ``outcome_from_durable`` is treated as a black box: it may only project
what those rows say (ADR-0003: rows are the authority; ADR-0005: a handoff is
never published, a parked Run is never "completed").

The only state a test touches by hand is a Run's ``deadline`` (scenario 2),
moved into the past with SQL as a clock move; every other row is written by
the product.

Deterministic doubles only (scripted model, fake transport); no model HTTP.
"""

from __future__ import annotations

import os
from dataclasses import asdict
from uuid import uuid4

import pytest

from opspilot.acceptance import IncidentScenario, outcome_from_durable
from opspilot.investigation.loop import ModelError
from opspilot.persistence import DurableStore, PersistenceError
from opspilot.web.events import DurableEventLog
from opspilot.web.store import DurableIncidentStore
from opspilot.worker import Worker
from tests.integration.test_m1_deadline_sweep_postgres import _expire
from tests.integration.test_m1_loop_resume_postgres import (
    LEASE,
    VERSIONS,
    Crash,
    Harness,
    _tool_rounds,
)
from tests.m1_investigation_support import report_from_transcript

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

# The runner claims with LEASE seconds; a crashed attempt keeps its lease to
# expiry (runner docstring), so a restart waits it out like a real worker.
assert LEASE >= 1


# --------------------------------------------------------------------------
# helpers: read the product rows through the product's readers, then project
# --------------------------------------------------------------------------


def _scenario(h: Harness, *, feature_id: str, step: str, scenario_id: str):
    return IncidentScenario(
        scenario_id=scenario_id,
        feature_id=feature_id,
        acceptance_step=step,
        kind="incident",
        subject_id=str(h.incident),
    )


def _controls(store: DurableStore, incident):
    """``opspilot_controls`` rows as the seam takes them (asdict of ControlAudit)."""
    return tuple(
        asdict(row) for row in DurableIncidentStore(store).control_audit(incident)
    )


def _handoff_events(log: DurableEventLog | None, incident):
    if log is None:
        return ()
    return tuple(
        dict(event.payload)
        for event in log.read_after(incident, 0, limit=1000)
        if event.kind == "run_handoff"
    )


def _project(h: Harness, scenario, *, log: DurableEventLog | None = None):
    snapshot = h.store.rebuild(h.incident)
    return (
        outcome_from_durable(
            scenario,
            snapshot,
            controls=_controls(h.store, h.incident),
            handoff_events=_handoff_events(log, h.incident),
        ),
        snapshot,
    )


def _committed_evidence_ids(snapshot) -> tuple[str, ...]:
    """Evidence the product adopted: ``tool_results`` of live (non-late) steps."""
    found: list[str] = []
    for step in snapshot["steps"]:
        if step["status"] == "late_result":
            continue
        for entry in step["tool_results"] or ():
            evidence_id = entry["result"].get("evidence_id")
            if isinstance(evidence_id, str) and evidence_id:
                found.append(evidence_id)
    return tuple(dict.fromkeys(found))


def _late_rows(snapshot):
    return [s for s in snapshot["steps"] if s["status"] == "late_result"]


def _conclusion_steps(snapshot):
    return [
        s
        for s in snapshot["steps"]
        if s["status"] != "late_result" and s["response"].get("kind") == "conclusion"
    ]


def _event_log(store: DurableStore) -> DurableEventLog:
    log = DurableEventLog(store)
    log.install()
    return log


def _global_gate(store: DurableStore) -> tuple[bool, int]:
    """Current state of the singleton global suspension gate (read only)."""
    with store.transaction(snapshot=True) as conn:
        row = conn.execute(
            "SELECT global_suspended,global_generation FROM opspilot_scope_controls WHERE scope_id=1"
        ).fetchone()
    assert row is not None
    return bool(row["global_suspended"]), int(row["global_generation"])


def _assert_read_only(outcome) -> None:
    # PRODUCT-CONSTRAINTS: the Agent never holds a mutation right over the
    # investigated system; the projection must not claim one either.
    assert all(isinstance(p, str) for p in outcome.permissions)
    assert not any(
        token in p.lower() for p in outcome.permissions for token in ("write", "mutat")
    ), outcome.permissions


def _assert_no_report(outcome, snapshot) -> None:
    """ADR-0005: nothing is published unless the product published it."""
    assert snapshot["conclusion"] is None
    assert outcome.report_available is False
    assert outcome.final_state != "completed"
    assert outcome.decision != "report_available"


# --------------------------------------------------------------------------
# 1. model-provider outage -> durable handoff, parked for a human (F8 step 2)
# --------------------------------------------------------------------------


def test_durable_handoff_parks_the_run_for_a_human():
    h = Harness()
    scenario = _scenario(
        h,
        feature_id="F8",
        step="Simulate provider outage and Agent unavailability and verify external detection plus bounded degradation and human handoff",
        scenario_id="m1-01-provider-outage-handoff",
    )
    runner = h.runner(
        [ModelError("MODEL_UNAVAILABLE"), ModelError("MODEL_UNAVAILABLE")]
    )
    result = runner.resume(h.incident)
    # Bounded retries (C3 §13: 2), then the product parks the Run itself.
    assert result.status == "handed_off", result
    assert result.reason == "MODEL_UNAVAILABLE"
    assert h.transport_requests == 0  # no read ever went out

    outcome, snapshot = _project(h, scenario)

    # Rows: parked, lease released, incident open, nothing published.
    assert snapshot["run"]["state"] == "waiting_human"
    assert snapshot["run"]["owner"] is None and snapshot["run"]["lease_until"] is None
    assert snapshot["state"] not in {"completed", "cancelled"}
    committed = _conclusion_steps(snapshot)
    assert len(committed) == 1
    assert committed[0]["response"]["conclusion"]["execution"] == "failed"
    assert committed[0]["response"]["conclusion"]["handoff"] is True
    assert committed[0]["response"]["conclusion"]["handoff_reasons"] == [
        "MODEL_UNAVAILABLE"
    ]
    assert _late_rows(snapshot) == []

    # Outcome: the seam projects exactly that.
    assert outcome.scenario_id == scenario.scenario_id
    _assert_no_report(outcome, snapshot)
    assert outcome.final_state == "failed"
    assert outcome.decision == "handoff"
    assert outcome.handoff_reasons == ("MODEL_UNAVAILABLE",)
    assert outcome.evidence_ids == ()
    # A parked Run with no human action recorded yet: "a human is required"
    # (user decision 2026-09-26).
    assert outcome.human_interaction == "handoff"
    assert "late_result_rejected" not in outcome.actions
    assert "worker_resumed" not in outcome.actions  # first attempt, epoch 1
    assert snapshot["run"]["epoch"] == 1
    _assert_read_only(outcome)

    # The park is durable: a further poll neither claims nor calls the model.
    again = h.runner([]).resume(h.incident)
    assert again.status == "handed_off" and again.reason == "AWAITING_HUMAN"
    assert again.epoch is None
    assert h.store.rebuild(h.incident)["run"]["state"] == "waiting_human"
    # ...and a human can still act on it (ADR-0005 decision 1).
    assert h.store.control(h.incident, 0, "follow_up", "operator", {"q": "x"}) == 1
    assert h.store.rebuild(h.incident)["run"]["state"] == "queued"


# --------------------------------------------------------------------------
# 2. overdue running Run -> swept into a visible timeout handoff (F2 step 3)
# --------------------------------------------------------------------------


def test_deadline_exceeded_run_is_swept_into_a_visible_timeout_handoff():
    h = Harness()
    log = _event_log(h.store)
    scenario = _scenario(
        h,
        feature_id="F2",
        step="Stall a query and verify bounded timeout, child-work cleanup, preserved partial evidence and explicit retry or handoff status",
        scenario_id="m1-01-deadline-exceeded",
    )
    # A real worker claims the Run (product path) and then never settles it.
    lease = h.store.claim(h.incident, h.run, uuid4(), VERSIONS, lease_seconds=300)
    assert lease.epoch == 1
    assert h.store.rebuild(h.incident)["run"]["state"] == "running"

    # CLOCK MOVE (the only hand-set state in this module): the Run's deadline
    # is moved into the past with SQL so the database clock sees it overdue.
    _expire(h.store, h.run)

    def never(lease, input):  # pragma: no cover - the sweep must come first
        raise AssertionError("an overdue Run must not be claimed")

    runner = h.runner([])
    runner.events = log
    runner.executor_factory = never
    result = runner.resume(h.incident)
    assert result.status == "handed_off" and result.reason == "DEADLINE_EXCEEDED"

    # Rows: parked exactly like a handoff, no conclusion step, no publish.
    snapshot = h.store.rebuild(h.incident)
    assert snapshot["run"]["state"] == "waiting_human"
    assert snapshot["run"]["owner"] is None and snapshot["run"]["lease_until"] is None
    assert snapshot["conclusion"] is None
    assert _conclusion_steps(snapshot) == []
    assert _late_rows(snapshot) == []
    assert h.transport_requests == 0

    # The product announced the park once, with the fixed reason.
    events = _handoff_events(log, h.incident)
    assert len(events) == 1
    assert events[0]["run_id"] == str(h.run)
    assert events[0]["parked"] is True
    assert events[0]["reasons"] == ["DEADLINE_EXCEEDED"]
    assert events[0]["published"] is False

    # (a) projected with the recorded event: the timeout is visible by name.
    with_event, _ = _project(h, scenario, log=log)
    assert with_event.scenario_id == scenario.scenario_id
    _assert_no_report(with_event, snapshot)
    assert with_event.final_state == "failed"
    assert with_event.decision == "handoff"
    assert with_event.handoff_reasons == ("DEADLINE_EXCEEDED",)
    assert with_event.evidence_ids == ()
    # Parked, no human action recorded yet (user decision 2026-09-26).
    assert with_event.human_interaction == "handoff"
    assert "worker_resumed" not in with_event.actions
    assert "late_result_rejected" not in with_event.actions
    _assert_read_only(with_event)

    # (b) projected from rows alone: still parked, still not completed, but
    # the rows cannot say *why* (ADR-0003), so no reason is invented.
    without_event, _ = _project(h, scenario, log=None)
    _assert_no_report(without_event, snapshot)
    assert without_event.final_state == "failed"
    assert without_event.decision == "handoff"
    assert without_event.handoff_reasons == ()
    # Parked, no human action recorded yet (user decision 2026-09-26).
    assert without_event.human_interaction == "handoff"

    # A dead lease can no longer write anything but history.
    with pytest.raises(PersistenceError, match="^CONTROL_DENIED$"):
        h.store.commit_step(lease, "ctx0:round-1", {"tool_calls": []})
    after = h.store.rebuild(h.incident)
    assert after["run"]["state"] == "waiting_human" and after["conclusion"] is None
    assert len(_late_rows(after)) == 1
    late_outcome, _ = _project(h, scenario, log=log)
    assert "late_result_rejected" in late_outcome.actions
    assert late_outcome.final_state == "failed"
    assert late_outcome.report_available is False
    # Still parked, still no human action recorded (user decision 2026-09-26).
    assert late_outcome.human_interaction == "handoff"


# --------------------------------------------------------------------------
# 3. pause then cancel while work is in flight: rows are the final authority
#    and committed evidence stays visible (F12 step 3, F2 step 5)
# --------------------------------------------------------------------------


def test_human_pause_and_cancel_are_the_observable_final_authority():
    h = Harness()
    scenario = _scenario(
        h,
        feature_id="F12",
        step="Pause, cancel, take over, close and reopen an incident and verify explicit status with authorized actor and timestamps",
        scenario_id="m1-01-pause-then-cancel",
    )
    # One committed tool round, then the worker dies mid-round 2.
    with pytest.raises(Crash):
        h.runner([*_tool_rounds(1), Crash("worker died")]).resume(h.incident)
    before = h.store.rebuild(h.incident)
    assert before["run"]["state"] == "running"  # the crashed attempt holds its lease
    evidence = _committed_evidence_ids(before)
    assert len(evidence) == 1 and h.transport_requests == 1

    # --- pause -----------------------------------------------------------
    assert h.store.control(h.incident, 0, "pause", "operator-a") == 1
    paused, snapshot = _project(h, scenario)
    assert snapshot["state"] == "paused" and snapshot["run"]["state"] == "paused"
    assert snapshot["run"]["owner"] is None and snapshot["run"]["lease_until"] is None
    assert snapshot["control_generation"] == 1
    assert _conclusion_steps(snapshot) == []
    controls = _controls(h.store, h.incident)
    assert [(c["action"], c["actor"], c["resulting_generation"]) for c in controls] == [
        ("pause", "operator-a", 1)
    ]

    assert paused.scenario_id == scenario.scenario_id
    _assert_no_report(paused, snapshot)
    assert paused.final_state == "paused"
    assert paused.decision == "human_control"
    assert paused.human_interaction == "pause"
    assert paused.evidence_ids == evidence  # evidence is not erased by the pause
    assert "read_only_query" in paused.actions
    assert paused.handoff_reasons == ()
    assert "late_result_rejected" not in paused.actions
    assert "worker_resumed" not in paused.actions
    _assert_read_only(paused)

    # While paused no worker may run it (PRODUCT-CONSTRAINTS: human control
    # is scoped outside the model's authority).
    denied = h.runner([report_from_transcript]).resume(h.incident)
    assert denied.status == "control_denied"
    assert h.transport_requests == 1

    # --- cancel ----------------------------------------------------------
    assert h.store.control(h.incident, 1, "cancel", "operator-b") == 2
    cancelled, snapshot = _project(h, scenario)
    assert snapshot["state"] == "cancelled" and snapshot["run"]["state"] == "cancelled"
    assert snapshot["control_generation"] == 2
    controls = _controls(h.store, h.incident)
    assert [
        (c["action"], c["actor"], c["expected_generation"], c["resulting_generation"])
        for c in controls
    ] == [
        ("pause", "operator-a", 0, 1),
        ("cancel", "operator-b", 1, 2),
    ]

    _assert_no_report(cancelled, snapshot)
    assert cancelled.final_state == "cancelled"
    assert cancelled.decision == "human_control"
    assert cancelled.human_interaction == "cancel"  # the decision in force
    assert cancelled.evidence_ids == evidence  # still visible after cancel
    assert "read_only_query" in cancelled.actions
    _assert_read_only(cancelled)

    # Cancelled is terminal for this Run: nothing resumes it.
    assert (
        h.runner([report_from_transcript]).resume(h.incident).status
        == "already_completed"
    )
    assert h.store.rebuild(h.incident)["run"]["state"] == "cancelled"
    assert h.transport_requests == 1


# --------------------------------------------------------------------------
# 4. a query that completes after a newer human decision is history only
#    (F2 step 5)
# --------------------------------------------------------------------------


def test_late_result_is_rejected_after_newer_human_decision():
    h = Harness()
    scenario = _scenario(
        h,
        feature_id="F2",
        step="Cancel or pause an incident while work is in flight and verify late completions cannot overwrite the newer human decision",
        scenario_id="m1-01-late-result-after-cancel",
    )

    def cancel_while_in_flight(request):
        # The round-1 plan is committed and the read is about to go out; the
        # operator cancels first. The read still completes afterwards.
        h.executor_hook = None
        assert h.store.control(h.incident, 0, "cancel", "operator") == 1

    h.executor_hook = cancel_while_in_flight
    result = h.runner([*_tool_rounds(1), report_from_transcript]).resume(h.incident)
    # The fenced attempt can neither adopt its result nor park the Run it no
    # longer holds; it reports a control outcome, not a crash.
    assert result.status == "control_denied", result
    assert result.loop is not None and result.loop.handoff_reasons == (
        "CONTROL_DENIED",
    )
    assert h.transport_requests == 1  # the query really went out

    outcome, snapshot = _project(h, scenario)

    # Rows: cancelled at generation 1; the round-1 step (generation 0) adopted
    # nothing; the late result is a history row stamped with the old generation.
    assert snapshot["state"] == "cancelled" and snapshot["run"]["state"] == "cancelled"
    assert snapshot["control_generation"] == 1
    assert snapshot["conclusion"] is None
    assert _conclusion_steps(snapshot) == []  # store refusals commit no conclusion
    live = [s for s in snapshot["steps"] if s["status"] != "late_result"]
    assert [s["logical_key"] for s in live] == ["ctx0:round-1"]
    assert live[0]["status"] == "response_committed"
    assert live[0]["tool_results"] == []
    assert live[0]["control_generation"] == 0
    late = _late_rows(snapshot)
    assert len(late) == 1
    assert late[0]["logical_key"] == f"late_result:tool:{live[0]['step_id']}:0:e1"
    assert late[0]["control_generation"] == 0  # older than the cancel (1)
    assert late[0]["response"].get("evidence_id")  # the read did produce a view
    assert snapshot["pending_tools"] == []  # nothing left for a worker to adopt
    assert _committed_evidence_ids(snapshot) == ()

    # Outcome: the human decision is final; the late read is a rejected marker.
    assert outcome.scenario_id == scenario.scenario_id
    _assert_no_report(outcome, snapshot)
    assert outcome.final_state == "cancelled"
    assert outcome.decision == "human_control"
    assert outcome.human_interaction == "cancel"
    assert "late_result_rejected" in outcome.actions
    assert "STALE_CONTROL_GENERATION" in outcome.handoff_reasons
    assert outcome.evidence_ids == ()  # the late view was never adopted
    assert "worker_resumed" not in outcome.actions
    _assert_read_only(outcome)

    # A restarted worker cannot revive it either.
    assert (
        h.runner([report_from_transcript]).resume(h.incident).status
        == "already_completed"
    )
    assert h.transport_requests == 1


# --------------------------------------------------------------------------
# 5. worker restart resumes from committed evidence without re-querying
#    (F2 step 2)
# --------------------------------------------------------------------------


def test_worker_restart_resumes_from_committed_evidence():
    h = Harness()
    scenario = _scenario(
        h,
        feature_id="F2",
        step="Terminate an investigation worker and verify the same incident resumes with retained evidence and bounded repeated work",
        scenario_id="m1-01-worker-restart",
    )
    with pytest.raises(Crash):
        h.runner([*_tool_rounds(1), Crash("worker killed")]).resume(h.incident)
    crashed = h.store.rebuild(h.incident)
    assert crashed["run"]["state"] == "running" and crashed["run"]["epoch"] == 1
    evidence = _committed_evidence_ids(crashed)
    assert len(evidence) == 1
    queries = h.transport_requests
    assert queries == 1

    # Mid-crash rows: evidence retained, nothing published, no restart yet.
    assert crashed["conclusion"] is None and _conclusion_steps(crashed) == []

    h.wait_lease()
    # A fresh Worker (new owner) takes over: new epoch, same rows.
    result = h.runner([report_from_transcript]).resume(h.incident)
    assert result.status == "published", result
    assert result.epoch == 2 and result.replayed_tools == 0
    assert h.transport_requests == queries  # nothing re-queried

    outcome, snapshot = _project(h, scenario)
    assert snapshot["run"]["state"] == "completed" and snapshot["run"]["epoch"] == 2
    assert snapshot["run"]["owner"] is None and snapshot["run"]["lease_until"] is None
    assert snapshot["conclusion"] is not None
    assert snapshot["conclusion"]["conclusion"]["execution"] == "completed"
    assert snapshot["conclusion"]["conclusion"]["handoff"] is False
    assert tuple(snapshot["conclusion"]["conclusion"]["evidence_ids"]) == evidence
    assert _late_rows(snapshot) == []
    assert _controls(h.store, h.incident) == ()

    assert outcome.scenario_id == scenario.scenario_id
    assert outcome.final_state == "completed"
    assert outcome.decision == "report_available"
    assert outcome.report_available is True
    assert outcome.evidence_ids == evidence
    assert "worker_resumed" in outcome.actions  # claim epoch 2 > 1
    assert "read_only_query" in outcome.actions
    assert "late_result_rejected" not in outcome.actions
    assert outcome.human_interaction is None
    assert outcome.handoff_reasons == ()
    _assert_read_only(outcome)

    # Control: a first-attempt Run carries no restart marker.
    single = Harness()
    first = single.runner([*_tool_rounds(1), report_from_transcript]).resume(
        single.incident
    )
    assert first.status == "published" and first.epoch == 1
    single_outcome, single_rows = _project(
        single,
        _scenario(
            single,
            feature_id="F2",
            step="control: a first-attempt Run has no restart marker",
            scenario_id="m1-01-worker-restart-control",
        ),
    )
    assert single_rows["run"]["epoch"] == 1
    assert single_outcome.final_state == "completed"
    assert single_outcome.report_available is True
    assert "worker_resumed" not in single_outcome.actions


# --------------------------------------------------------------------------
# 6. version-incompatible continuation -> blocked(INCOMPATIBLE_STATE) (C3 §7,
#    F8 step 3)
# --------------------------------------------------------------------------


def test_incompatible_state_is_a_blocked_handoff():
    h = Harness()
    scenario = _scenario(
        h,
        feature_id="F8",
        step="Exercise a versioned configuration or state-schema upgrade and its documented rollback or compatible recovery, including in-flight incidents",
        scenario_id="m1-01-incompatible-state",
    )
    runner = h.runner([report_from_transcript])
    runner.worker = Worker.create(h.store, {**VERSIONS, "prompt_revision": "other"})
    result = runner.resume(h.incident)
    assert result.status == "blocked" and result.reason == "INCOMPATIBLE_STATE"
    assert runner.model.calls == []  # never ran the model
    assert h.transport_requests == 0

    outcome, snapshot = _project(h, scenario)
    assert snapshot["run"]["state"] == "blocked"
    assert snapshot["run"]["owner"] is None and snapshot["run"]["lease_until"] is None
    assert snapshot["run"]["versions"] == VERSIONS  # the rows keep their version
    assert snapshot["conclusion"] is None
    assert snapshot["steps"] == []
    assert _controls(h.store, h.incident) == ()

    assert outcome.scenario_id == scenario.scenario_id
    _assert_no_report(outcome, snapshot)
    assert outcome.final_state == "blocked"
    assert outcome.decision == "handoff"
    assert outcome.handoff_reasons == ("INCOMPATIBLE_STATE",)
    assert outcome.evidence_ids == ()
    # Blocked with no human action recorded yet: "a human is required"
    # (user decision 2026-09-26).
    assert outcome.human_interaction == "handoff"
    assert "worker_resumed" not in outcome.actions
    assert "late_result_rejected" not in outcome.actions
    _assert_read_only(outcome)

    # A compatible worker does not silently resume a blocked Run (C3 §7:
    # explicit migration or a new Run, never a silent version switch).
    compatible = h.runner([report_from_transcript])
    again = compatible.resume(h.incident)
    assert again.status == "control_denied", again
    assert compatible.model.calls == [] and h.transport_requests == 0
    still, snapshot = _project(h, scenario)
    assert snapshot["run"]["state"] == "blocked"
    assert still.final_state == "blocked" and still.decision == "handoff"
    assert still.handoff_reasons == ("INCOMPATIBLE_STATE",)
    # Still blocked, still no human action recorded (user decision 2026-09-26).
    assert still.human_interaction == "handoff"

    # Human control remains the exit: cancel works on a blocked Run.
    assert h.store.control(h.incident, 0, "cancel", "operator") == 1
    cancelled, snapshot = _project(h, scenario)
    assert snapshot["state"] == "cancelled" and snapshot["run"]["state"] == "cancelled"
    assert snapshot["conclusion"] is None
    assert cancelled.final_state == "cancelled"
    assert cancelled.decision == "human_control"
    assert cancelled.human_interaction == "cancel"
    assert cancelled.report_available is False
    assert h.transport_requests == 0


# --------------------------------------------------------------------------
# 7. a global scope suspension pauses the Run with no controls row and is
#    visible as human control (C3 §4 全局与目标级暂停; F12 step 3)
# --------------------------------------------------------------------------


def test_a_scope_suspension_is_visible_as_human_control():
    h = Harness()
    scenario = _scenario(
        h,
        feature_id="F12",
        step="Pause, cancel, take over, close and reopen an incident and verify explicit status with authorized actor and timestamps",
        scenario_id="m1-01-scope-suspension",
    )
    with pytest.raises(Crash):
        h.runner([*_tool_rounds(1), Crash("worker died")]).resume(h.incident)
    before = h.store.rebuild(h.incident)
    assert before["run"]["state"] == "running"
    evidence = _committed_evidence_ids(before)
    assert len(evidence) == 1 and h.transport_requests == 1

    # The global gate is a singleton shared by every test on the lab
    # instance: read it, release it if someone left it suspended, and always
    # release what this test set in ``finally``.
    suspended, generation = _global_gate(h.store)
    if suspended:
        generation = h.store.set_global_suspension(
            False, expected_generation=generation, actor="acceptance-reset"
        )
    held: int | None = None
    try:
        held = h.store.set_global_suspension(
            True, expected_generation=generation, actor="operator"
        )
        assert held == generation + 1

        outcome, snapshot = _project(h, scenario)
        # Rows: paused by the scope, lease revoked, no controls row written.
        assert snapshot["state"] == "paused" and snapshot["run"]["state"] == "paused"
        assert (
            snapshot["run"]["owner"] is None and snapshot["run"]["lease_until"] is None
        )
        assert (
            snapshot["control_generation"] == 0
        )  # a scope gate is not an incident control
        assert _controls(h.store, h.incident) == ()
        assert snapshot["conclusion"] is None
        assert _conclusion_steps(snapshot) == []
        assert _committed_evidence_ids(snapshot) == evidence

        assert outcome.scenario_id == scenario.scenario_id
        _assert_no_report(outcome, snapshot)
        assert outcome.final_state == "paused"
        assert outcome.decision == "human_control"
        assert outcome.human_interaction == "scope_suspension"
        assert outcome.evidence_ids == evidence
        assert "read_only_query" in outcome.actions
        assert outcome.handoff_reasons == ()
        assert "worker_resumed" not in outcome.actions
        assert "late_result_rejected" not in outcome.actions
        _assert_read_only(outcome)

        # No worker may claim or query while the scope is suspended.
        h.wait_lease()
        denied = h.runner([report_from_transcript]).resume(h.incident)
        assert denied.status == "control_denied", denied
        assert h.transport_requests == 1
        assert h.store.rebuild(h.incident)["run"]["state"] == "paused"
    finally:
        if held is not None:
            h.store.set_global_suspension(
                False, expected_generation=held, actor="operator"
            )

    # Releasing the gate does not silently resume the Run (C3 §4: 解除暂停只
    # 移除该层阻挡，不自动恢复旧任务); the row stays paused until a human acts.
    released, snapshot = _project(h, scenario)
    assert snapshot["run"]["state"] == "paused" and snapshot["state"] == "paused"
    assert released.final_state == "paused"
    assert released.human_interaction == "scope_suspension"
    assert released.report_available is False
    assert _controls(h.store, h.incident) == ()
