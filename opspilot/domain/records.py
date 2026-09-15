"""Subject events, audit records and the export outbox (C3 section 4).

Subject events carry the durable per-subject sequence the UI cursor reads.
Audit records keep the operator, the ``expected_version`` they supplied and the
control generation the operation produced. The outbox is for external export
only; it never replaces in-database job scheduling, and a bounded drop is
recorded as a drop rather than silently losing the entry.
"""

from typing import Literal

from pydantic import AwareDatetime

from .base import DTO, Count, DomainError, Positive, StateMachine, Text
from .control import ControlAction
from .subjects import SubjectRef

OutboxState = Literal["pending", "delivered", "dropped"]

EXPORT_OUTBOX = StateMachine(
    "export_outbox",
    {
        "pending": {"delivered": "delivered", "bounded_drop": "dropped"},
        "delivered": {},
        "dropped": {},
    },
)


class SubjectEvent(DTO):
    """One durable event on a subject's own sequence."""

    event_id: Text
    subject: SubjectRef
    sequence: Positive
    kind: Text
    occurred_at: AwareDatetime
    run_id: Text | None = None


class AuditRecord(DTO):
    """One recorded human operation and the control version it produced."""

    audit_id: Text
    subject: SubjectRef
    actor: Text
    action: ControlAction
    expected_version: Count
    resulting_control_generation: Count
    recorded_at: AwareDatetime
    idempotency_key: Text


class ExportOutboxEntry(DTO):
    """One at-least-once external export, identified idempotently.

    Payloads are allow-listed DTOs; full Agent state and credentials never
    become an export payload.
    """

    entry_id: Text
    idempotency_key: Text
    payload_kind: Literal["trace", "eval"]
    payload_hash: Text
    created_at: AwareDatetime
    state: OutboxState = "pending"
    attempts: Count = 0


def advance_outbox(entry: ExportOutboxEntry, trigger: str) -> ExportOutboxEntry:
    """Fire one outbox trigger, or raise ``ILLEGAL_TRANSITION``."""
    if not isinstance(entry, ExportOutboxEntry):
        raise DomainError("INVALID_INPUT", "outbox entry is required")
    return entry.model_copy(update={"state": EXPORT_OUTBOX.fire(entry.state, trigger)})


def next_sequence(events: tuple[SubjectEvent, ...], subject: SubjectRef) -> int:
    """The next durable sequence for one subject; sequences never repeat."""
    if not isinstance(events, tuple) or not isinstance(subject, SubjectRef):
        raise DomainError("INVALID_INPUT", "events and subject are required")
    mine = [event.sequence for event in events if event.subject == subject]
    return max(mine, default=0) + 1
