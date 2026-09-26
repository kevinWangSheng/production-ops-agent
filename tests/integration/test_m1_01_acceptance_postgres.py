"""M1-01 durable acceptance scenarios produced by the real product on PostgreSQL.

Every row here is written by product code -- ``DurableStore``,
``InvestigationRunner`` / ``Worker`` / ``RecoverySession``, ``control()`` and
the deadline sweep -- and then read back through ``DurableStore.rebuild``,
``DurableIncidentStore.control_audit`` and ``DurableEventLog`` before the
external ``IncidentScenario -> IncidentOutcome`` seam projects it. No run
state or marker is set by hand: ``late_result_rejected`` comes from the
``late_result`` rows ``commit_tool`` writes, ``worker_resumed`` from the Run's
claim epoch, ``STALE_CONTROL_GENERATION`` from a late row's generation, and a
timeout's reason from the ``run_handoff`` event the sweep records. Model and
tool calls are deterministic doubles; only the storage is real.
"""

from __future__ import annotations

import os
from dataclasses import asdict
from uuid import uuid4

import pytest

from opspilot.acceptance import IncidentScenario, outcome_from_durable
from opspilot.investigation.loop import ModelError
from opspilot.investigation.runner import InvestigationRunner
from opspilot.web import DurableEventLog, DurableIncidentStore
from opspilot.worker import Worker
from tests.integration.test_m1_deadline_sweep_postgres import _expire
from tests.integration.test_m1_loop_resume_postgres import (
    LEASE,
    VERSIONS,
    Crash,
    Harness,
    _tool_rounds,
)
from tests.m1_investigation_support import ScriptedModel, report_from_transcript

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)


def scenario(kind: str) -> IncidentScenario:
    return IncidentScenario(
        scenario_id=f"m1-01-{kind}",
        feature_id="F2",
        acceptance_step="external IncidentScenario -> IncidentOutcome (PostgreSQL)",
        kind=kind,
        subject_id="incident-acceptance-pg",
    )


def _runner(h: Harness, replies, *, events=None, versions=VERSIONS):
    return InvestigationRunner(
        store=h.store,
        worker=Worker.create(h.store, dict(versions)),
        model=ScriptedModel(replies),
        executor_factory=h.executor_factory,
        clock=h.clock,
        lease_seconds=LEASE,
        events=events,
    )


def project(h: Harness, kind: str, *, events: DurableEventLog | None = None):
    """Read the product's own records back and project them; nothing added."""
    snapshot = h.store.rebuild(h.incident)
    controls = [
        asdict(item) for item in DurableIncidentStore(h.store).control_audit(h.incident)
    ]
    handoffs = (
        []
        if events is None
        else [
            dict(event.payload)
            for event in events.read_after(h.incident, 0, limit=1000)
            if event.kind == "run_handoff"
        ]
    )
    outcome = outcome_from_durable(
        scenario(kind), snapshot, controls=controls, handoff_events=handoffs
    )
    return outcome, snapshot


def test_durable_handoff_parks_the_run_for_a_human():
    """ADR-0005: a handoff is parked (``waiting_human``), never published."""
    h = Harness()
    result = h.runner(
        [ModelError("MODEL_UNAVAILABLE"), ModelError("MODEL_UNAVAILABLE")]
    ).resume(h.incident)
    assert result.status == "handed_off" and result.reason == "MODEL_UNAVAILABLE"
    outcome, rows = project(h, "durable-handoff-pg")
    assert rows["run"]["state"] == "waiting_human" and rows["conclusion"] is None
    assert outcome.final_state == "failed"
    assert outcome.decision == "handoff"
    assert outcome.human_interaction == "handoff"
    assert outcome.handoff_reasons == ("MODEL_UNAVAILABLE",)
    assert outcome.evidence_ids == ()
    assert outcome.report_available is False
    # The incident stays open to human control: a follow-up re-queues it.
    assert h.store.control(h.incident, 0, "follow_up", "operator", {"q": "x"}) == 1
    assert h.store.rebuild(h.incident)["run"]["state"] == "queued"


def test_deadline_exceeded_run_is_swept_into_a_visible_timeout_handoff():
    """ADR-0005 decision 2: the sweep parks an overdue running Run."""
    h = Harness()
    log = DurableEventLog(h.store)
    log.install()
    h.store.claim(h.incident, h.run, uuid4(), VERSIONS, lease_seconds=300)
    # The only hand of the test: the database deadline is moved into the past
    # (the same clock manipulation the sweep suite uses).
    _expire(h.store, h.run)
    result = _runner(h, [], events=log).resume(h.incident)
    assert result.status == "handed_off" and result.reason == "DEADLINE_EXCEEDED"
    outcome, rows = project(h, "deadline-exceeded", events=log)
    assert rows["run"]["state"] == "waiting_human" and rows["conclusion"] is None
    assert outcome.final_state == "failed"
    assert outcome.decision == "handoff"
    assert outcome.handoff_reasons == ("DEADLINE_EXCEEDED",)
    assert outcome.report_available is False
    # The rows alone cannot name why the Run was parked; without the sweep's
    # event the seam reports the park with no reason rather than guessing.
    bare = outcome_from_durable(scenario("deadline-exceeded"), rows)
    assert bare.decision == "handoff" and bare.handoff_reasons == ()


def test_human_pause_and_cancel_are_the_observable_final_authority():
    h = Harness()
    with pytest.raises(Crash):
        h.runner([*_tool_rounds(1), Crash("in flight")]).resume(h.incident)
    # The worker died holding its lease; a human pauses, then cancels.
    assert h.store.control(h.incident, 0, "pause", "operator") == 1
    paused, rows = project(h, "pause")
    assert rows["run"]["state"] == "paused"
    assert paused.final_state == "paused"
    assert paused.human_interaction == "pause"
    assert paused.decision == "human_control"
    assert paused.evidence_ids != ()  # the committed tool result stays visible
    assert paused.report_available is False
    h.wait_lease()
    assert h.runner([]).resume(h.incident).status == "control_denied"
    assert h.store.control(h.incident, 1, "cancel", "operator") == 2
    cancelled, rows = project(h, "cancel")
    assert rows["run"]["state"] == "cancelled"
    assert cancelled.final_state == "cancelled"
    assert cancelled.human_interaction == "cancel"
    assert cancelled.report_available is False
    assert h.runner([]).resume(h.incident).status == "already_completed"


def test_late_result_is_rejected_after_newer_human_decision():
    h = Harness()

    def cancel_while_the_query_is_out(request):
        h.executor_hook = None
        assert h.store.control(h.incident, 0, "cancel", "operator") == 1

    h.executor_hook = cancel_while_the_query_is_out
    result = h.runner([*_tool_rounds(1), report_from_transcript]).resume(h.incident)
    assert result.status == "control_denied" and result.reason == "CONTROL_DENIED"
    assert h.transport_requests == 1  # the read really went out
    outcome, rows = project(h, "late-result")
    late = [s for s in rows["steps"] if s["status"] == "late_result"]
    assert len(late) == 1 and late[0]["logical_key"].startswith("late_result:tool:")
    assert late[0]["control_generation"] == 0 and rows["control_generation"] == 1
    assert outcome.final_state == "cancelled"
    assert outcome.human_interaction == "cancel"
    assert "late_result_rejected" in outcome.actions
    assert "STALE_CONTROL_GENERATION" in outcome.handoff_reasons
    assert outcome.evidence_ids == ()  # the late result was never adopted
    assert outcome.report_available is False


def test_worker_restart_resumes_from_committed_evidence():
    h = Harness()
    with pytest.raises(Crash):
        h.runner([*_tool_rounds(1), Crash("in flight")]).resume(h.incident)
    h.wait_lease()
    # A fresh Worker (new owner) claims the Run: epoch 2, no re-query.
    queries = h.transport_requests
    result = h.runner([report_from_transcript]).resume(h.incident)
    assert result.status == "published" and result.epoch == 2
    assert h.transport_requests == queries
    outcome, rows = project(h, "worker-restart")
    assert rows["run"]["state"] == "completed" and rows["run"]["epoch"] == 2
    assert outcome.final_state == "completed"
    assert "worker_resumed" in outcome.actions
    assert outcome.evidence_ids == tuple(
        rows["conclusion"]["conclusion"]["evidence_ids"]
    )
    assert outcome.evidence_ids != ()
    assert outcome.report_available is True
    assert outcome.handoff_reasons == ()
    # A Run finished by its first attempt records no restart.
    first = Harness()
    assert (
        first.runner([*_tool_rounds(1), report_from_transcript])
        .resume(first.incident)
        .status
        == "published"
    )
    assert "worker_resumed" not in project(first, "first-attempt")[0].actions


def test_incompatible_state_is_a_blocked_handoff():
    h = Harness()
    log = DurableEventLog(h.store)
    log.install()
    other = {**VERSIONS, "tool_schema_revision": "t2"}
    result = _runner(h, [], events=log, versions=other).resume(h.incident)
    assert result.status == "blocked" and result.reason == "INCOMPATIBLE_STATE"
    outcome, rows = project(h, "incompatible", events=log)
    assert rows["run"]["state"] == "blocked"
    assert outcome.final_state == "blocked"
    assert outcome.decision == "handoff"
    assert outcome.handoff_reasons == ("INCOMPATIBLE_STATE",)
    assert outcome.report_available is False
    # A compatible worker does not silently resume a blocked Run.
    assert h.runner([]).resume(h.incident).status == "control_denied"
    # Human control is still the exit.
    assert h.store.control(h.incident, 0, "cancel", "operator") == 1
