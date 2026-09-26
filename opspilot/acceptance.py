"""External, observable acceptance seam for the bounded M1-01 slice.

This module deliberately has no model prompt, transport, or persistence SQL.
It turns results already committed by the investigation loop and DurableStore
into the small ``IncidentScenario -> IncidentOutcome`` surface used by tests
and the command-line acceptance runner.  It is not a second runtime.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast

from opspilot.investigation.context import pending_conclusion
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
# Row states the product writes for a Run that is no longer being worked:
# ``publish()`` -> completed, ``hand_off()``/the sweep -> waiting_human,
# ``control()`` -> paused/cancelled, ``claim()``/``block()`` -> blocked.
# ``queued``/``running`` are not outcomes and are refused, as is anything
# no product path writes to the row.
_FINAL_ROW_STATES = frozenset({"completed", "waiting_human"}) | _HUMAN_OR_BLOCKED
_HUMAN_ACTIONS = frozenset({"pause", "cancel", "resume", "follow_up", "correct"})


def _string_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)) or not all(
        isinstance(item, str) for item in value
    ):
        raise ValueError("INVALID_DURABLE_SNAPSHOT")
    return tuple(value)


_MISSING = object()


def _conclusion_payload(row: object) -> Mapping[str, object]:
    """The inner mapping of a ``conclusion`` step the loop committed in
    ``_finish``; anything else is refused rather than guessed at.

    Every field ``_finish`` always writes must be present with its type; a
    partial payload is refused, never projected as a guessed decision.
    """
    if not isinstance(row, Mapping) or row.get("kind") != "conclusion":
        raise ValueError("INVALID_DURABLE_SNAPSHOT")
    payload = row.get("conclusion")
    if not isinstance(payload, Mapping) or payload.get("execution") not in _LOOP_STATES:
        raise ValueError("INVALID_DURABLE_SNAPSHOT")
    schema = payload.get("report_schema_version", _MISSING)
    if (
        type(payload.get("handoff")) is not bool
        or not isinstance(payload.get("handoff_reasons"), list)
        or not isinstance(payload.get("evidence_ids"), list)
        or not (schema is None or isinstance(schema, str))
    ):
        raise ValueError("INVALID_DURABLE_SNAPSHOT")
    return payload


def _published_conclusion(
    snapshot: Mapping[str, object],
) -> Mapping[str, object] | None:
    """The row ``publish()`` stored under ``snapshot["conclusion"]``.

    ``DurableStore.rebuild`` (and ``MemoryStepStore.snapshot``) place it
    there as ``{"kind": "conclusion", "assistant": ..., "conclusion":
    {...}}``; the observable result lives in the inner mapping.
    """
    row = snapshot.get("conclusion")
    return None if row is None else _conclusion_payload(row)


def _parked_conclusion(snapshot: Mapping[str, object]) -> Mapping[str, object] | None:
    """The conclusion step a handoff left committed but unpublished (ADR-0005).

    ``pending_conclusion`` is the product's own reader for that row (the
    runner settles from it after a restart); it only sees the current
    control generation, so a step superseded by a human decision is not
    read as the Run's result.
    """
    found = pending_conclusion(snapshot)
    return None if found is None else _conclusion_payload(found[1])


def _committed_evidence(snapshot: Mapping[str, object]) -> tuple[str, ...]:
    """Evidence ids from committed tool_result rows of adopted steps."""
    ids: list[str] = []
    for step in _steps(snapshot):
        if step.get("status") == "late_result":
            continue
        results = step.get("tool_results") or ()
        if not isinstance(results, (tuple, list)):
            raise ValueError("INVALID_DURABLE_SNAPSHOT")
        for item in results:
            result = item.get("result") if isinstance(item, Mapping) else None
            evidence_id = (
                result.get("evidence_id") if isinstance(result, Mapping) else None
            )
            if isinstance(evidence_id, str) and evidence_id not in ids:
                ids.append(evidence_id)
    return tuple(ids)


def _steps(snapshot: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    steps = snapshot.get("steps") or ()
    if not isinstance(steps, (tuple, list)) or not all(
        isinstance(step, Mapping) for step in steps
    ):
        raise ValueError("INVALID_DURABLE_SNAPSHOT")
    return tuple(steps)


def _current_human_action(
    controls: Sequence[Mapping[str, object]], generation: object
) -> str | None:
    """The decision that produced the current control generation, if any.

    ``controls`` are ``opspilot_controls`` rows (``DurableIncidentStore
    .control_audit``): only the row whose ``resulting_generation`` is the
    incident's current generation is the decision in force; earlier rows are
    history a later decision already superseded.
    """
    for row in reversed(list(controls)):
        if (
            row.get("resulting_generation") == generation
            and row.get("action") in _HUMAN_ACTIONS
        ):
            return str(row["action"])
    return None


def _event_reasons(
    handoff_events: Sequence[Mapping[str, object]], run_id: str, *, parked: bool
) -> tuple[str, ...]:
    """Reasons the product announced for this Run's park or block.

    ``handoff_events`` are ``run_handoff`` payloads from the incident's
    event log. The rows cannot say *why* a Run was parked without a
    conclusion step (the sweep's ``DEADLINE_EXCEEDED``) or blocked; that
    reason lives only in the announcement, which is a projection (ADR-0003),
    so it adds reasons and never changes the state read from the rows.
    """
    for event in handoff_events:
        if (
            str(event.get("run_id")) == run_id
            and bool(event.get("parked", False)) is parked
        ):
            return _string_list(event.get("reasons", ()))
    return ()


def outcome_from_durable(
    scenario: IncidentScenario,
    snapshot: Mapping[str, object],
    *,
    controls: Sequence[Mapping[str, object]] = (),
    handoff_events: Sequence[Mapping[str, object]] = (),
) -> IncidentOutcome:
    """Project a ``DurableStore.rebuild`` snapshot, including human control.

    Everything observable is read from what the product recorded:

    * ``completed`` rows carry the published conclusion (``publish()``); the
      loop's ``execution``, reasons, evidence and report presence come from
      that payload, never from the row state alone.
    * ``waiting_human`` rows are parked handoffs (ADR-0005): the conclusion
      step the loop committed (if any) gives the execution and reasons; a
      park without one (the deadline sweep) takes its reasons from the
      matching ``run_handoff`` event in ``handoff_events``, or reports none.
    * ``paused``/``cancelled``/``blocked`` are final whatever was committed.
    * ``late_result_rejected`` is derived from ``late_result`` rows
      (``commit_step``/``commit_tool``/``publish`` refused by the fence);
      ``STALE_CONTROL_GENERATION`` from such a row written under an older
      control generation than the incident's; ``worker_resumed`` from a
      claim epoch above 1.
    * ``human_interaction`` is the ``controls`` row in force for the current
      generation; nothing here is set by the caller.

    A report is available only when it was published.
    """
    run = snapshot.get("run")
    if not isinstance(run, Mapping) or not isinstance(run.get("state"), str):
        raise ValueError("INVALID_DURABLE_SNAPSHOT")
    row_state = run["state"]
    if row_state not in _FINAL_ROW_STATES:
        raise ValueError("UNKNOWN_DURABLE_STATE")
    generation = snapshot.get("control_generation")
    run_id = str(run.get("run_id"))
    steps = _steps(snapshot)

    published = _published_conclusion(snapshot)
    if row_state == "completed" and published is None:
        raise ValueError("INVALID_DURABLE_SNAPSHOT")
    conclusion = published if published is not None else _parked_conclusion(snapshot)

    reasons: list[str] = []
    report_available = False
    if conclusion is not None:
        loop_state = conclusion["execution"]
        evidence = _string_list(conclusion.get("evidence_ids", ()))
        reasons = list(_string_list(conclusion.get("handoff_reasons", ())))
        report_available = (
            published is not None
            and loop_state == "completed"
            and isinstance(conclusion.get("report_schema_version"), str)
        )
    else:
        loop_state = "failed"
        evidence = _committed_evidence(snapshot)
    state = cast(
        OutcomeState, row_state if row_state in _HUMAN_OR_BLOCKED else loop_state
    )

    if state == "blocked":
        reasons.extend(
            _event_reasons(handoff_events, run_id, parked=False)
            or ("INCOMPATIBLE_STATE",)
        )
    elif row_state == "waiting_human" and conclusion is None:
        reasons.extend(_event_reasons(handoff_events, run_id, parked=True))

    late = [step for step in steps if step.get("status") == "late_result"]
    if any(step.get("control_generation") != generation for step in late):
        reasons.append("STALE_CONTROL_GENERATION")
    epoch = run.get("epoch")
    actions = ["read_only_query"] if evidence else []
    if late:
        actions.append("late_result_rejected")
    if type(epoch) is int and epoch > 1:
        actions.append("worker_resumed")

    human = _current_human_action(controls, generation)
    if human is not None:
        decision = "human_control"
    elif published is not None:
        decision = "handoff" if published.get("handoff") is True else "report_available"
    elif row_state in {"waiting_human", "blocked"}:
        decision = "handoff"
    else:
        decision = "durable_state"
    return IncidentOutcome(
        scenario_id=scenario.scenario_id,
        final_state=state,
        evidence_ids=evidence,
        decision=decision,
        actions=tuple(actions),
        permissions=("read_only", "human_control"),
        human_interaction=human
        if human is not None
        else ("handoff" if decision == "handoff" else None),
        handoff_reasons=tuple(dict.fromkeys(reasons)),
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
