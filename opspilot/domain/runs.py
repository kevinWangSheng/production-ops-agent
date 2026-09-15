"""`Run` and `ModelStep` (C3 section 4, "调查状态与调查结果分开").

Execution status and investigation result are two independent values. Reaching
``completed`` records only that the attempt finished; whether the cause was
found is a separate ``InvestigationResult``, and whether the service recovered
belongs to an observation session, not here.

A conclusion is owned by the subject, not by whichever Run produces it. Only the
subject's current Run, bound to that same subject and holding the current
control version, updates the current conclusion; anything else is history.
"""

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime

from .base import DTO, Count, DomainError, StateMachine, Text
from .subjects import SubjectRef

RunExecution = Literal[
    "queued",
    "running",
    "waiting_human",
    "paused",
    "blocked",
    "completed",
    "failed",
    "cancelled",
    "budget_exhausted",
]
InvestigationResult = Literal["supported", "inconclusive", "partial"]
BlockedReason = Literal["INCOMPATIBLE_STATE", "RETRIES_EXHAUSTED", "HANDOFF_REQUIRED"]

RUN_EXECUTION = StateMachine(
    "run",
    {
        "queued": {
            "claimed": "running",
            "human_pause": "paused",
            "human_cancel": "cancelled",
        },
        "running": {
            "finished": "completed",
            "awaiting_human_input": "waiting_human",
            "human_pause": "paused",
            "human_cancel": "cancelled",
            "execution_failed": "failed",
            "retries_exhausted": "blocked",
            "incompatible_state": "blocked",
            "budget_exhausted": "budget_exhausted",
            "lease_expired": "queued",
        },
        "waiting_human": {
            # A reply resumes as a new execution attempt, not in place.
            "human_reply": "queued",
            "human_pause": "paused",
            "human_cancel": "cancelled",
            "deadline_expired": "failed",
        },
        "paused": {
            # Resuming a paused Run uses a new execution attempt.
            "human_resume": "queued",
            "human_cancel": "cancelled",
        },
        # A blocked Run is continued by a new Run built on committed business
        # facts, never by silently resuming this one on another version.
        "blocked": {"human_cancel": "cancelled", "handoff_failed": "failed"},
        "completed": {},
        "failed": {},
        "cancelled": {},
        "budget_exhausted": {},
    },
)

ACTIVE_RUN_EXECUTIONS = frozenset({"queued", "running", "waiting_human"})


class ModelProfile(DTO):
    """Effective model configuration recorded per Run."""

    provider: Text
    model: Text
    endpoint_mode: Text
    prompt_revision: Text
    adapter_revision: Text
    tool_schema_revision: Text


class BudgetLedger(DTO):
    """Accumulated budget for one Run, including retained unknown cost."""

    deadline: AwareDatetime
    model_requests: Count = 0
    tool_operations: Count = 0
    known_cost_micros: Count = 0
    unknown_reserved_micros: Count = 0


class Run(DTO):
    """One bounded investigation attempt, explicitly bound to one subject."""

    run_id: Text
    subject: SubjectRef
    model_profile: ModelProfile
    budget: BudgetLedger
    execution: RunExecution = "queued"
    epoch: Count = 0
    input_watermark: Count = 0
    owner: Text | None = None
    lease_expires_at: AwareDatetime | None = None
    blocked_reason: BlockedReason | None = None


class Conclusion(DTO):
    """An investigation result produced by one Run for one subject."""

    run_id: Text
    subject: SubjectRef
    result: InvestigationResult
    recorded_at: AwareDatetime
    control_generation: Count


ConclusionReason = Literal[
    "accepted",
    "subject_mismatch",
    "not_current_run",
    "control_generation_stale",
]


class ConclusionAcceptance(DTO):
    """Whether a submitted conclusion becomes current or is kept as history."""

    accepted: bool
    disposition: Literal["current", "history_only"]
    reason: ConclusionReason


def advance_run(run: Run, trigger: str, *, reason: BlockedReason | None = None) -> Run:
    """Fire one execution trigger. Never sets or clears an investigation result."""
    if not isinstance(run, Run):
        raise DomainError("INVALID_INPUT", "run is required")
    execution = RUN_EXECUTION.fire(run.execution, trigger)
    update: dict[str, object] = {"execution": execution}
    if execution == "blocked":
        if reason is None:
            reason = (
                "INCOMPATIBLE_STATE"
                if trigger == "incompatible_state"
                else "RETRIES_EXHAUSTED"
            )
        update["blocked_reason"] = reason
    if trigger in ("lease_expired", "human_pause", "human_resume", "human_reply"):
        update["owner"] = None
        update["lease_expires_at"] = None
    return run.model_copy(update=update)


def claim_run(run: Run, *, owner: str, epoch: int, lease_expires_at: datetime) -> Run:
    """Claim a queued Run for a new execution attempt.

    Every attempt takes a strictly greater epoch, so a late result carrying an
    older epoch can be recognised and refused.
    """
    if not isinstance(run, Run):
        raise DomainError("INVALID_INPUT", "run is required")
    if not isinstance(owner, str) or not owner:
        raise DomainError("INVALID_INPUT", "owner is required")
    if type(epoch) is not int or epoch != run.epoch + 1:
        raise DomainError("INVALID_INPUT", f"epoch must be {run.epoch + 1}")
    claimed = advance_run(run, "claimed")
    return claimed.model_copy(
        update={"owner": owner, "epoch": epoch, "lease_expires_at": lease_expires_at}
    )


def advance_watermark(run: Run, to: int) -> Run:
    """Fix the input watermark this attempt actually processed.

    The watermark never moves backwards, and it only moves while the Run is
    running, so input received while paused is never displayed as analysed.
    """
    if not isinstance(run, Run):
        raise DomainError("INVALID_INPUT", "run is required")
    if type(to) is not int or to < 0:
        raise DomainError("INVALID_INPUT", "watermark must be a count")
    if run.execution != "running":
        raise DomainError(
            "ILLEGAL_TRANSITION", f"{run.execution} does not process input"
        )
    if to < run.input_watermark:
        raise DomainError("STALE_RESULT", "watermark cannot move backwards")
    return run.model_copy(update={"input_watermark": to})


def evaluate_conclusion(
    conclusion: Conclusion,
    *,
    subject: SubjectRef,
    current_run_id: str | None,
    control_generation: int,
) -> ConclusionAcceptance:
    """Decide whether a conclusion becomes the subject's current conclusion.

    A rejected conclusion is never discarded: it is kept as history, which is
    why this returns a disposition instead of raising.
    """
    if not isinstance(conclusion, Conclusion) or not isinstance(subject, SubjectRef):
        raise DomainError("INVALID_INPUT", "conclusion and subject are required")
    if type(control_generation) is not int or control_generation < 0:
        raise DomainError("INVALID_INPUT", "control_generation must be a count")
    if conclusion.subject != subject:
        return _history_only("subject_mismatch")
    if current_run_id is None or conclusion.run_id != current_run_id:
        return _history_only("not_current_run")
    if conclusion.control_generation != control_generation:
        return _history_only("control_generation_stale")
    return ConclusionAcceptance(accepted=True, disposition="current", reason="accepted")


def _history_only(reason: ConclusionReason) -> ConclusionAcceptance:
    return ConclusionAcceptance(
        accepted=False, disposition="history_only", reason=reason
    )


class ToolPlanEntry(DTO):
    """One planned tool call inside a committed model response."""

    tool_index: Count
    tool_name: Text
    tool_version: Text
    arguments_hash: Text


class ModelStep(DTO):
    """Stable step id, input snapshot, full model response and tool plan."""

    step_id: Text
    run_id: Text
    context_segment: Text
    logical_round: Count
    input_snapshot_hash: Text
    response_committed: bool = False
    response_hash: Text | None = None
    tool_plan: tuple[ToolPlanEntry, ...] = ()


def step_id(run_id: str, context_segment: str, logical_round: int) -> str:
    """Stable step key: the same attempt input always rebuilds the same id."""
    if (
        not isinstance(run_id, str)
        or not run_id
        or not isinstance(context_segment, str)
        or not context_segment
        or type(logical_round) is not int
        or logical_round < 0
    ):
        raise DomainError("INVALID_INPUT", "run, context segment and round required")
    canonical = json.dumps(
        {"run_id": run_id, "context_segment": context_segment, "round": logical_round},
        ensure_ascii=False,
        sort_keys=True,
    )
    return "step:" + hashlib.sha256(canonical.encode()).hexdigest()


def tool_operation_id(step: str, tool_index: int) -> str:
    """Stable tool operation id derived from the step id and tool index."""
    if not isinstance(step, str) or not step:
        raise DomainError("INVALID_INPUT", "step id is required")
    if type(tool_index) is not int or tool_index < 0:
        raise DomainError("INVALID_INPUT", "tool_index must be a count")
    return f"{step}#{tool_index}"


def may_execute_tools(step: ModelStep) -> bool:
    """Tools run only after the full model response and its plan are committed."""
    if not isinstance(step, ModelStep):
        raise DomainError("INVALID_INPUT", "model step is required")
    return step.response_committed


def pending_tool_indices(step: ModelStep, completed: frozenset[int]) -> tuple[int, ...]:
    """On recovery, re-run only the tool operations that never completed."""
    if not isinstance(step, ModelStep) or not isinstance(completed, frozenset):
        raise DomainError("INVALID_INPUT", "step and completed indices required")
    if not step.response_committed:
        raise DomainError("ILLEGAL_TRANSITION", "response not committed")
    return tuple(
        entry.tool_index
        for entry in step.tool_plan
        if entry.tool_index not in completed
    )
