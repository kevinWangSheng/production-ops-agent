"""`ToolOperation / Evidence` (C3 section 4).

Every tool operation records success, failure, cancellation or unknown state.
Unknown is never treated as success: it requires an explicit re-query or a
handoff. Evidence carries its own adoption state, because only adopted evidence
may be consumed, and revoking an authorization must stop old restricted
evidence from entering a new model input while keeping its history.
"""

from typing import Literal

from pydantic import AwareDatetime, model_validator

from .base import DTO, Count, DomainError, StateMachine, Text
from .intake import Target

ToolOperationState = Literal[
    "planned", "dispatched", "succeeded", "failed", "cancelled", "unknown"
]
EvidenceAdoption = Literal["recorded", "adopted", "history_only", "revoked"]

TOOL_OPERATION = StateMachine(
    "tool_operation",
    {
        "planned": {"dispatched": "dispatched", "cancelled": "cancelled"},
        "dispatched": {
            "result_committed": "succeeded",
            "error_committed": "failed",
            "cancelled": "cancelled",
            "state_unknown": "unknown",
        },
        "succeeded": {},
        "failed": {},
        "cancelled": {},
        "unknown": {},
    },
)

EVIDENCE_ADOPTION = StateMachine(
    "evidence",
    {
        "recorded": {"adopt": "adopted", "reject": "history_only"},
        "adopted": {"revoke": "revoked"},
        "history_only": {},
        "revoked": {},
    },
)


class QueryWindow(DTO):
    """Absolute time window a query covered."""

    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def ordered(self) -> "QueryWindow":
        if self.end <= self.start:
            raise ValueError("INVALID_WINDOW")
        return self


class QueryScope(DTO):
    """The authorized scope one operation was allowed to read."""

    tool_name: Text
    tool_version: Text
    integration_id: Text
    target: Target
    window: QueryWindow
    result_bytes_limit: Count
    deadline_seconds: Count


class ToolOperation(DTO):
    """Operation identity, query scope, time window and outcome."""

    operation_id: Text
    run_id: Text
    step_id: Text
    tool_index: Count
    scope: QueryScope
    state: ToolOperationState = "planned"
    dispatched_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    error_class: Text | None = None
    truncated: bool = False


class Evidence(DTO):
    """An observation produced by one tool operation, with its adoption state."""

    evidence_id: Text
    operation_id: Text
    run_id: Text
    source: Text
    target: Target
    window: QueryWindow
    observed_at: AwareDatetime
    freshness_seconds: Count
    raw_hash: Text
    view_hash: Text
    adoption: EvidenceAdoption = "recorded"
    restricted: bool = False


def advance_tool_operation(operation: ToolOperation, trigger: str) -> ToolOperation:
    """Fire one tool operation trigger, or raise ``ILLEGAL_TRANSITION``."""
    if not isinstance(operation, ToolOperation):
        raise DomainError("INVALID_INPUT", "tool operation is required")
    return operation.model_copy(
        update={"state": TOOL_OPERATION.fire(operation.state, trigger)}
    )


def advance_evidence(evidence: Evidence, trigger: str) -> Evidence:
    """Fire one evidence adoption trigger, or raise ``ILLEGAL_TRANSITION``."""
    if not isinstance(evidence, Evidence):
        raise DomainError("INVALID_INPUT", "evidence is required")
    return evidence.model_copy(
        update={"adoption": EVIDENCE_ADOPTION.fire(evidence.adoption, trigger)}
    )


def counts_as_observed(operation: ToolOperation) -> bool:
    """Only a committed successful result is an observation.

    ``unknown`` is explicitly not success; it needs a new query or a handoff.
    """
    if not isinstance(operation, ToolOperation):
        raise DomainError("INVALID_INPUT", "tool operation is required")
    return operation.state == "succeeded"


def may_enter_model_input(evidence: Evidence) -> bool:
    """Only adopted evidence may be consumed by a later model call."""
    if not isinstance(evidence, Evidence):
        raise DomainError("INVALID_INPUT", "evidence is required")
    return evidence.adoption == "adopted"


def revoke_restricted(evidence: tuple[Evidence, ...]) -> tuple[Evidence, ...]:
    """Revoke adopted restricted evidence after an authorization is withdrawn.

    History is preserved; only the adoption state changes, so the items can no
    longer enter a new model input.
    """
    if not isinstance(evidence, tuple) or any(
        not isinstance(item, Evidence) for item in evidence
    ):
        raise DomainError("INVALID_INPUT", "evidence tuple is required")
    return tuple(
        advance_evidence(item, "revoke")
        if item.restricted and item.adoption == "adopted"
        else item
        for item in evidence
    )
