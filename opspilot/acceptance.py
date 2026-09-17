"""External, observable acceptance seam for the bounded M1-01 slice.

This module deliberately has no model prompt, transport, or persistence SQL.
It turns results already committed by the investigation loop and DurableStore
into the small ``IncidentScenario -> IncidentOutcome`` surface used by tests
and the command-line acceptance runner.  It is not a second runtime.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from opspilot.investigation.loop import LoopOutcome

OutcomeState = Literal[
    "completed", "failed", "blocked", "budget_exhausted", "cancelled", "paused"
]


@dataclass(frozen=True)
class IncidentScenario:
    """Versioned external stimulus; no private prompt or call-order fields."""

    scenario_id: str
    feature_id: str
    acceptance_step: str
    kind: str
    subject_id: str


@dataclass(frozen=True)
class IncidentOutcome:
    """Only externally inspectable evidence, decisions, actions and state."""

    scenario_id: str
    final_state: OutcomeState
    evidence_ids: tuple[str, ...]
    decision: str
    actions: tuple[str, ...]
    permissions: tuple[str, ...]
    human_interaction: str | None
    handoff_reasons: tuple[str, ...]
    report_available: bool


def outcome_from_loop(
    scenario: IncidentScenario, result: LoopOutcome
) -> IncidentOutcome:
    """Project a loop result without interpreting its hidden reasoning."""
    state: OutcomeState = result.execution
    if state not in {"completed", "failed", "blocked", "budget_exhausted"}:
        raise ValueError("UNKNOWN_LOOP_STATE")
    return IncidentOutcome(
        scenario_id=scenario.scenario_id,
        final_state=state,
        evidence_ids=result.evidence_ids,
        decision="handoff" if result.handoff else "report_available",
        actions=("read_only_query",) if result.evidence_ids else (),
        permissions=("read_only",),
        human_interaction="handoff" if result.handoff else None,
        handoff_reasons=result.handoff_reasons,
        report_available=result.report is not None,
    )


def outcome_from_durable(
    scenario: IncidentScenario,
    snapshot: Mapping[str, object],
    *,
    action: str | None = None,
) -> IncidentOutcome:
    """Project committed DurableStore rows, including human-control outcomes."""
    run = snapshot.get("run")
    if not isinstance(run, Mapping) or not isinstance(run.get("state"), str):
        raise ValueError("INVALID_DURABLE_SNAPSHOT")
    state = run["state"]
    if state not in {
        "completed",
        "failed",
        "blocked",
        "budget_exhausted",
        "cancelled",
        "paused",
    }:
        raise ValueError("UNKNOWN_DURABLE_STATE")
    evidence = snapshot.get("evidence_ids", ())
    if not isinstance(evidence, (tuple, list)) or not all(
        isinstance(x, str) for x in evidence
    ):
        raise ValueError("INVALID_DURABLE_EVIDENCE")
    late_rejected = snapshot.get("late_result_rejected") is True
    resumed = snapshot.get("worker_resumed") is True
    actions = [action] if action else []
    if late_rejected:
        actions.append("late_result_rejected")
    if resumed:
        actions.append("worker_resumed")
    reasons = ["INCOMPATIBLE_STATE"] if state == "blocked" else []
    if late_rejected:
        reasons.append("STALE_CONTROL_GENERATION")
    return IncidentOutcome(
        scenario_id=scenario.scenario_id,
        final_state=state,
        evidence_ids=tuple(evidence),
        decision="human_control" if action else "durable_state",
        actions=tuple(actions),
        permissions=("read_only", "human_control"),
        human_interaction=action,
        handoff_reasons=tuple(reasons),
        report_available=snapshot.get("conclusion") is not None,
    )


def outcome_from_live_record(
    scenario: IncidentScenario,
    ledger: Mapping[str, object],
    report: Mapping[str, object] | None,
) -> IncidentOutcome:
    """Project a previously executed, ledger-backed provider Run.

    The record is evidence of one real Run; this adapter does not infer quality
    or turn an incomplete/handoff result into a successful investigation.
    """
    if ledger.get("model") != "deepseek-flash" or not isinstance(
        ledger.get("run_id"), str
    ):
        raise ValueError("INVALID_LIVE_RECORD")
    execution = ledger.get("execution")
    if execution not in {"completed", "failed", "blocked", "budget_exhausted"}:
        raise ValueError("INVALID_LIVE_RECORD")
    evidence = ledger.get("evidence_ids", ())
    if not isinstance(evidence, list) or not all(
        isinstance(item, str) for item in evidence
    ):
        raise ValueError("INVALID_LIVE_RECORD")
    handoff = ledger.get("handoff") is True
    reasons = ledger.get("handoff_reasons", ())
    if not isinstance(reasons, list) or not all(
        isinstance(item, str) for item in reasons
    ):
        raise ValueError("INVALID_LIVE_RECORD")
    return IncidentOutcome(
        scenario_id=scenario.scenario_id,
        final_state=execution,
        evidence_ids=tuple(evidence),
        decision="handoff" if handoff else "report_available",
        actions=("read_only_query",) if evidence else (),
        permissions=("read_only",),
        human_interaction="handoff" if handoff else None,
        handoff_reasons=tuple(reasons),
        report_available=report is not None and isinstance(report, Mapping),
    )


# Index from each PRODUCT-CONSTRAINTS section to the scenario ids that carry
# its observable assertions. This is a coverage map, not a behavioural
# assertion itself and not a claim that the product feature has passed.
PRODUCT_CONSTRAINT_SCENARIO_INDEX: dict[str, tuple[str, ...]] = {
    "explicit_exclusions": ("F7:readonly",),
    "workflow_uncertainty_visible": ("F3:fault",),
    "evidence_context": ("F3:normal", "F7:hostile-input"),
    "runtime_human_control": ("F2:pause-cancel", "F2:late-result"),
    "recovery_observations": ("F6:not_implemented",),
    "data_flow": ("F7:secret-scan", "F7:credential-boundary"),
}
