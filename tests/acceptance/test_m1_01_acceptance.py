"""External M1-01 scenarios.  All model/tool cases use deterministic doubles."""

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from opspilot.acceptance import (
    IncidentScenario,
    outcome_from_durable,
    outcome_from_live_record,
    outcome_from_loop,
)
from opspilot.investigation.loop import ModelError
from opspilot.investigation.store import MemoryStepStore
from tests.m1_investigation_support import (
    assemble,
    reply,
    report_from_transcript,
    tool_call,
)
from tests.m1_tool_support import NOW


def scenario(kind: str) -> IncidentScenario:
    return IncidentScenario(
        scenario_id=f"m1-01-{kind}",
        feature_id="F3" if kind in {"normal", "fault"} else "F2",
        acceptance_step="external IncidentScenario -> IncidentOutcome",
        kind=kind,
        subject_id="incident-acceptance",
    )


def test_normal_report_is_observable_through_external_seam():
    loop, request, _model, transport, _store, _sink = assemble(
        replies=[
            reply(tool_calls=[tool_call()], finish="tool_calls"),
            report_from_transcript,
        ]
    )
    outcome = outcome_from_loop(scenario("normal"), loop.run(request))
    assert transport.called
    assert outcome.final_state == "completed"
    assert outcome.report_available and outcome.evidence_ids
    assert outcome.permissions == ("read_only",)


def test_provider_failure_is_a_visible_handoff_not_a_report():
    loop, request, _model, _transport, _store, _sink = assemble(
        replies=[ModelError("MODEL_UNAVAILABLE"), ModelError("MODEL_UNAVAILABLE")]
    )
    outcome = outcome_from_loop(scenario("fault"), loop.run(request))
    assert outcome.final_state == "failed"
    assert outcome.decision == "handoff"
    assert outcome.handoff_reasons == ("MODEL_UNAVAILABLE",)
    assert not outcome.report_available


def test_budget_refusal_never_looks_completed():
    loop, request, _model, _transport, store, _sink = assemble(
        replies=[reply(content="unused")], model_requests=1
    )
    loop.store = MemoryStepStore(
        budget_limit=0, deadline=store.deadline, clock=loop.clock, run_id=request.run_id
    )
    outcome = outcome_from_loop(scenario("budget"), loop.run(request))
    assert outcome.final_state == "budget_exhausted"
    assert not outcome.report_available


def test_deadline_refusal_never_dispatches_a_model_request():
    loop, request, model, _transport, _store, _sink = assemble(
        replies=[reply(content="unused")]
    )
    outcome = outcome_from_loop(
        scenario("deadline"),
        loop.run(
            replace(
                request,
                scope=replace(request.scope, deadline=NOW - timedelta(seconds=1)),
            )
        ),
    )
    assert outcome.final_state == "failed"
    assert outcome.handoff_reasons == ("DEADLINE_EXCEEDED",)
    assert model.calls == []


def durable_snapshot(replies, *, publish=True):
    """A ``DurableStore.rebuild``-shaped snapshot from a real loop attempt.

    The loop runs against ``MemoryStepStore`` (documented as rendering the
    same shape ``rebuild`` returns); the committed conclusion row and the
    tool_result rows are the product's own, not hand-written.
    """
    loop, request, _model, _transport, store, _sink = assemble(replies=replies)
    result = loop.run(request)
    if publish:
        assert store.publish(result.conclusion, step_id=result.final_step_id)
    return result, store.snapshot()


def test_durable_completed_run_projects_its_committed_conclusion():
    result, snapshot = durable_snapshot(
        [reply(tool_calls=[tool_call()], finish="tool_calls"), report_from_transcript]
    )
    assert snapshot["run"]["state"] == "completed"
    outcome = outcome_from_durable(scenario("durable-completed"), snapshot)
    assert outcome.final_state == "completed"
    assert outcome.evidence_ids == result.evidence_ids != ()
    assert outcome.decision == "report_available"
    assert outcome.report_available is True
    assert outcome.handoff_reasons == ()


def test_durable_handoff_run_is_not_reported_as_a_completed_report():
    result, snapshot = durable_snapshot(
        [ModelError("MODEL_UNAVAILABLE"), ModelError("MODEL_UNAVAILABLE")]
    )
    # publish() marks the Run row completed even for a handoff conclusion;
    # the seam must read the conclusion the loop committed, not the row.
    assert snapshot["run"]["state"] == "completed"
    assert result.execution == "failed"
    outcome = outcome_from_durable(scenario("durable-handoff"), snapshot)
    assert outcome.final_state == "failed"
    assert outcome.decision == "handoff"
    assert outcome.handoff_reasons == ("MODEL_UNAVAILABLE",)
    assert outcome.evidence_ids == ()
    assert outcome.report_available is False


def test_durable_snapshot_with_a_malformed_conclusion_is_refused():
    with pytest.raises(ValueError, match="INVALID_DURABLE_SNAPSHOT"):
        outcome_from_durable(
            scenario("durable-malformed"),
            {"run": {"state": "completed"}, "conclusion": {"summary": "ok"}},
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"execution": "completed"},
        {
            "execution": "completed",
            "handoff": False,
            "handoff_reasons": [],
            "evidence_ids": [],
        },
        {
            "execution": "failed",
            "handoff": "yes",
            "handoff_reasons": [],
            "report_schema_version": None,
            "evidence_ids": [],
        },
        {
            "execution": "failed",
            "handoff": True,
            "handoff_reasons": "BUDGET",
            "report_schema_version": None,
            "evidence_ids": [],
        },
        {
            "execution": "completed",
            "handoff": False,
            "handoff_reasons": [],
            "report_schema_version": 2,
            "evidence_ids": [],
        },
        {
            "execution": "completed",
            "handoff": False,
            "handoff_reasons": [],
            "report_schema_version": "m0-report-v2",
            "evidence_ids": "e1",
        },
    ],
)
def test_durable_conclusion_missing_or_mistyped_fields_is_refused(payload):
    """Codex review: an incomplete committed conclusion must not be turned
    into an invented ``report_available`` decision."""
    with pytest.raises(ValueError, match="INVALID_DURABLE_SNAPSHOT"):
        outcome_from_durable(
            scenario("durable-incomplete"),
            {
                "run": {"state": "completed"},
                "conclusion": {"kind": "conclusion", "conclusion": payload},
            },
        )


def test_human_pause_and_cancel_are_the_observable_final_authority():
    paused = outcome_from_durable(
        scenario("pause"),
        {"run": {"state": "paused"}, "conclusion": None},
        action="pause",
    )
    cancelled = outcome_from_durable(
        scenario("cancel"),
        {"run": {"state": "cancelled"}, "conclusion": None},
        action="cancel",
    )
    assert paused.final_state == "paused" and paused.human_interaction == "pause"
    assert (
        cancelled.final_state == "cancelled" and cancelled.human_interaction == "cancel"
    )


def test_incompatible_state_is_a_blocked_handoff():
    outcome = outcome_from_durable(
        scenario("incompatible"), {"run": {"state": "blocked"}, "conclusion": None}
    )
    assert outcome.final_state == "blocked"
    assert outcome.handoff_reasons == ("INCOMPATIBLE_STATE",)


def test_late_result_is_rejected_after_newer_human_decision():
    # Real committed tool_result rows from a Run whose conclusion was never
    # published. MemoryStepStore has no control endpoint, so the cancelled
    # state and the ``late_result_rejected`` marker are set by the test.
    result, snapshot = durable_snapshot(
        [reply(tool_calls=[tool_call()], finish="tool_calls"), report_from_transcript],
        publish=False,
    )
    assert snapshot["conclusion"] is None
    snapshot["run"]["state"] = "cancelled"
    snapshot["late_result_rejected"] = True
    outcome = outcome_from_durable(scenario("late-result"), snapshot, action="cancel")
    assert outcome.final_state == "cancelled"
    assert outcome.evidence_ids == result.evidence_ids != ()
    assert "late_result_rejected" in outcome.actions
    assert "STALE_CONTROL_GENERATION" in outcome.handoff_reasons
    assert outcome.report_available is False


def test_worker_restart_resumes_from_committed_evidence():
    # Real committed conclusion; only the ``worker_resumed`` marker is
    # hand-set (no product path emits it yet).
    result, snapshot = durable_snapshot(
        [reply(tool_calls=[tool_call()], finish="tool_calls"), report_from_transcript]
    )
    snapshot["worker_resumed"] = True
    outcome = outcome_from_durable(scenario("worker-restart"), snapshot)
    assert outcome.final_state == "completed"
    assert outcome.evidence_ids == result.evidence_ids != ()
    assert "worker_resumed" in outcome.actions
    assert outcome.report_available is True


EVIDENCE_DIR = Path(__file__).parents[2] / "docs/evidence/m1-01-acceptance"
# The two 2026-09-17 Runs from the harness before its time policy was fixed
# (REPORT_INVALID), plus every per-Run directory written since.
REAL_RUN_RECORDS = sorted(
    [
        (EVIDENCE_DIR / "real-run-ledger.json", EVIDENCE_DIR / "real-run-report.json"),
        (
            EVIDENCE_DIR / "real-run-ledger-2.json",
            EVIDENCE_DIR / "real-run-report-2.json",
        ),
    ]
    + [
        (path, path.with_name("report-parsed.json"))
        for path in (EVIDENCE_DIR / "live-runs").glob("*/ledger.json")
    ],
    key=lambda pair: str(pair[0]),
)
# The first Run whose model report passed the loop's v4 bind end to end.
POSITIVE_LIVE_RUN = "bbf10e0e-0b0e-489c-9efd-98b80ef4aa3b"


def _live_record(ledger_path: Path, report_path: Path):
    ledger = json.loads(ledger_path.read_text())
    report = (
        json.loads(report_path.read_text())
        if ledger.get("report_schema_version") and report_path.exists()
        else None
    )
    return ledger, report


@pytest.mark.parametrize(
    "ledger_path, report_path", REAL_RUN_RECORDS, ids=lambda p: p.parent.name
)
def test_recorded_real_deepseek_runs_project_exactly_what_their_ledger_says(
    ledger_path, report_path
):
    """The seam mirrors the ledger; it neither upgrades nor hides a Run."""
    ledger, report = _live_record(ledger_path, report_path)
    outcome = outcome_from_live_record(scenario("real-deepseek"), ledger, report)
    assert outcome.permissions == ("read_only",)
    assert outcome.final_state == ledger["execution"]
    assert outcome.handoff_reasons == tuple(ledger["handoff_reasons"])
    assert outcome.evidence_ids == tuple(ledger["evidence_ids"])
    if ledger["handoff"]:
        assert outcome.decision == "handoff"
        assert outcome.human_interaction == "handoff"
    else:
        assert outcome.decision == "report_available"
        assert outcome.human_interaction is None
    # A validated report is visible exactly when the ledger recorded one.
    assert outcome.report_available is (ledger["report_schema_version"] is not None)


def test_a_real_deepseek_run_has_produced_a_bound_report_without_handoff():
    ledger_path = EVIDENCE_DIR / "live-runs" / POSITIVE_LIVE_RUN / "ledger.json"
    ledger, report = _live_record(
        ledger_path, ledger_path.with_name("report-parsed.json")
    )
    assert ledger["prompt_revision"] == "prompt-replay-candidate-017c81744c26"
    outcome = outcome_from_live_record(scenario("real-deepseek"), ledger, report)
    assert outcome.final_state == "completed"
    assert outcome.decision == "report_available"
    assert outcome.handoff_reasons == ()
    assert outcome.report_available is True
    assert report is not None and report["schema_version"] == "m0-report-v2"
    assert report["assessment_status"] == "completed"


def test_a_live_record_without_a_recorded_schema_exposes_no_report():
    """Codex review: a parsed report passed alongside a ledger whose
    ``report_schema_version`` is null must not be reported as available."""
    ledger_path = EVIDENCE_DIR / "live-runs" / POSITIVE_LIVE_RUN / "ledger.json"
    ledger, report = _live_record(
        ledger_path, ledger_path.with_name("report-parsed.json")
    )
    assert report is not None
    ledger = {**ledger, "report_schema_version": None}
    outcome = outcome_from_live_record(scenario("real-deepseek"), ledger, report)
    assert outcome.report_available is False
