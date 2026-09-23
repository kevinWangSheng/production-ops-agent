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


_LOOP_STATES = frozenset({"completed", "failed", "blocked", "budget_exhausted"})
# Human control and an incompatible rebuild are the final authority over a
# Run row; a committed conclusion never overrides them.
_HUMAN_OR_BLOCKED = frozenset({"paused", "cancelled", "blocked"})


def _string_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)) or not all(
        isinstance(item, str) for item in value
    ):
        raise ValueError("INVALID_DURABLE_SNAPSHOT")
    return tuple(value)


_MISSING = object()


def _committed_conclusion(
    snapshot: Mapping[str, object],
) -> Mapping[str, object] | None:
    """The payload the loop committed in ``_finish`` and ``publish()`` stored.

    ``DurableStore.rebuild`` (and ``MemoryStepStore.snapshot``) place the
    published row under ``snapshot["conclusion"]`` as
    ``{"kind": "conclusion", "assistant": ..., "conclusion": {...}}``; the
    observable result lives in the inner mapping. Anything else under that
    key is not a product conclusion and is refused rather than guessed at.
    """
    row = snapshot.get("conclusion")
    if row is None:
        return None
    if not isinstance(row, Mapping) or row.get("kind") != "conclusion":
        raise ValueError("INVALID_DURABLE_SNAPSHOT")
    payload = row.get("conclusion")
    if not isinstance(payload, Mapping) or payload.get("execution") not in _LOOP_STATES:
        raise ValueError("INVALID_DURABLE_SNAPSHOT")
    # Every field ``_finish`` always writes must be present with its type;
    # a partial payload is refused, never projected as a guessed decision.
    schema = payload.get("report_schema_version", _MISSING)
    if (
        type(payload.get("handoff")) is not bool
        or not isinstance(payload.get("handoff_reasons"), list)
        or not isinstance(payload.get("evidence_ids"), list)
        or not (schema is None or isinstance(schema, str))
    ):
        raise ValueError("INVALID_DURABLE_SNAPSHOT")
    return payload


def _committed_evidence(snapshot: Mapping[str, object]) -> tuple[str, ...]:
    """Evidence ids from committed tool_result rows of adopted steps."""
    ids: list[str] = []
    steps = snapshot.get("steps") or ()
    if not isinstance(steps, (tuple, list)):
        raise ValueError("INVALID_DURABLE_SNAPSHOT")
    for step in steps:
        if not isinstance(step, Mapping) or step.get("status") == "late_result":
            continue
        for item in step.get("tool_results") or ():
            result = item.get("result") if isinstance(item, Mapping) else None
            evidence_id = (
                result.get("evidence_id") if isinstance(result, Mapping) else None
            )
            if isinstance(evidence_id, str) and evidence_id not in ids:
                ids.append(evidence_id)
    return tuple(ids)


def outcome_from_durable(
    scenario: IncidentScenario,
    snapshot: Mapping[str, object],
    *,
    action: str | None = None,
) -> IncidentOutcome:
    """Project a ``DurableStore.rebuild`` snapshot, including human control.

    ``publish()`` marks the Run row ``completed`` for every published
    conclusion, handoff or not, so the loop's own ``execution``,
    ``handoff_reasons``, ``evidence_ids`` and report presence are read from
    the committed conclusion payload, never from the row state alone. A Run
    without a conclusion projects its committed tool_result evidence.

    ``late_result_rejected`` and ``worker_resumed`` are optional markers no
    product path emits yet; acceptance tests set them by hand to name the
    action they simulated. They add observable actions/reasons only.
    """
    run = snapshot.get("run")
    if not isinstance(run, Mapping) or not isinstance(run.get("state"), str):
        raise ValueError("INVALID_DURABLE_SNAPSHOT")
    row_state = run["state"]
    if row_state not in _LOOP_STATES | _HUMAN_OR_BLOCKED:
        raise ValueError("UNKNOWN_DURABLE_STATE")
    conclusion = _committed_conclusion(snapshot)

    handoff = False
    reasons: list[str] = []
    report_available = False
    if conclusion is not None:
        loop_state = conclusion["execution"]
        evidence = _string_list(conclusion.get("evidence_ids", ()))
        handoff = conclusion.get("handoff") is True
        reasons = list(_string_list(conclusion.get("handoff_reasons", ())))
        report_available = loop_state == "completed" and isinstance(
            conclusion.get("report_schema_version"), str
        )
        state: OutcomeState = (
            row_state if row_state in _HUMAN_OR_BLOCKED else loop_state  # type: ignore[assignment]
        )
    else:
        evidence = _committed_evidence(snapshot)
        state = row_state

    late_rejected = snapshot.get("late_result_rejected") is True
    resumed = snapshot.get("worker_resumed") is True
    actions = [action] if action else []
    if late_rejected:
        actions.append("late_result_rejected")
    if resumed:
        actions.append("worker_resumed")
    if state == "blocked" and "INCOMPATIBLE_STATE" not in reasons:
        reasons.append("INCOMPATIBLE_STATE")
    if late_rejected:
        reasons.append("STALE_CONTROL_GENERATION")
    if action:
        decision = "human_control"
    elif conclusion is not None:
        decision = "handoff" if handoff else "report_available"
    else:
        decision = "durable_state"
    return IncidentOutcome(
        scenario_id=scenario.scenario_id,
        final_state=state,
        evidence_ids=evidence,
        decision=decision,
        actions=tuple(actions),
        permissions=("read_only", "human_control"),
        human_interaction=action,
        handoff_reasons=tuple(reasons),
        report_available=report_available,
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
        # A report is visible only when the ledger itself recorded one.
        report_available=isinstance(report, Mapping)
        and isinstance(ledger.get("report_schema_version"), str),
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
