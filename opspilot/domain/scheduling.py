"""`Job / Schedule` (C3 section 4, scheduling rules from C3 section 6).

A job carries its due time, the schedule generation it was created under, its
lease and a unique execution key built from ``schedule / generation / due``.
A terminal job and a job whose schedule generation has been superseded must
never execute again; an expired lease requeues the job under a higher epoch.
"""

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime

from .base import DTO, Count, DomainError, StateMachine, Text
from .subjects import SubjectRef

JobState = Literal["pending", "leased", "succeeded", "failed", "blocked", "cancelled"]

JOB = StateMachine(
    "job",
    {
        "pending": {
            "claimed": "leased",
            "schedule_superseded": "cancelled",
            "human_cancel": "cancelled",
        },
        "leased": {
            "committed": "succeeded",
            "execution_failed": "failed",
            "retries_exhausted": "blocked",
            "lease_expired": "pending",
            "schedule_superseded": "cancelled",
            "human_cancel": "cancelled",
        },
        # Retries are exhausted; a human continues through a new job, and this
        # one stays visible rather than silently resuming.
        "blocked": {"human_cancel": "cancelled"},
        "succeeded": {},
        "failed": {},
        "cancelled": {},
    },
)


class Schedule(DTO):
    """A durable schedule for one subject, versioned by generation."""

    schedule_id: Text
    subject: SubjectRef
    kind: Text
    generation: Count = 0
    due_at: AwareDatetime
    cancelled: bool = False


class Job(DTO):
    """One due execution of a schedule, with its lease and execution identity."""

    job_id: Text
    schedule_id: Text
    subject: SubjectRef
    schedule_generation: Count
    due_at: AwareDatetime
    execution_key: Text
    state: JobState = "pending"
    epoch: Count = 0
    attempts: Count = 0
    owner: Text | None = None
    lease_expires_at: AwareDatetime | None = None


def execution_key(schedule_id: str, generation: int, due_at: datetime) -> str:
    """Unique execution key: schedule, schedule generation and due time."""
    if (
        not isinstance(schedule_id, str)
        or not schedule_id
        or type(generation) is not int
        or generation < 0
        or not isinstance(due_at, datetime)
    ):
        raise DomainError("INVALID_INPUT", "schedule, generation and due required")
    if due_at.tzinfo is None or due_at.utcoffset() is None:
        raise DomainError("INVALID_INPUT", "due_at must be timezone aware")
    canonical = json.dumps(
        {
            "schedule_id": schedule_id,
            "generation": generation,
            "due_at": due_at.isoformat(),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return "job:" + hashlib.sha256(canonical.encode()).hexdigest()


def advance_job(job: Job, trigger: str) -> Job:
    """Fire one job trigger, or raise ``ILLEGAL_TRANSITION``."""
    if not isinstance(job, Job):
        raise DomainError("INVALID_INPUT", "job is required")
    state = JOB.fire(job.state, trigger)
    update: dict[str, object] = {"state": state}
    if trigger == "lease_expired":
        update["owner"] = None
        update["lease_expires_at"] = None
        update["epoch"] = job.epoch + 1
    return job.model_copy(update=update)


def claim_job(
    job: Job, *, owner: str, epoch: int, lease_expires_at: datetime, schedule: Schedule
) -> Job:
    """Claim a due job for execution under the current schedule generation.

    ``epoch`` is the epoch the claimer observed, so a job requeued by lease
    expiry in between is refused. A job's epoch rises when an expired lease
    requeues it, unlike a Run's epoch, which rises on each execution attempt.
    """
    if not isinstance(job, Job) or not isinstance(schedule, Schedule):
        raise DomainError("INVALID_INPUT", "job and schedule are required")
    if not isinstance(owner, str) or not owner:
        raise DomainError("INVALID_INPUT", "owner is required")
    if not may_execute(job, schedule):
        raise DomainError("ILLEGAL_TRANSITION", "job may not execute")
    if type(epoch) is not int or epoch != job.epoch:
        raise DomainError("INVALID_INPUT", f"epoch must be {job.epoch}")
    claimed = advance_job(job, "claimed")
    return claimed.model_copy(
        update={
            "owner": owner,
            "lease_expires_at": lease_expires_at,
            "attempts": job.attempts + 1,
        }
    )


def check_execution_identity(
    job: Job, *, owner: str, epoch: int, now: datetime
) -> None:
    """Renewal and commit both verify execution identity and a live lease."""
    if not isinstance(job, Job) or not isinstance(now, datetime):
        raise DomainError("INVALID_INPUT", "job and current time are required")
    if job.state != "leased":
        raise DomainError("ILLEGAL_TRANSITION", f"{job.state} holds no lease")
    if job.owner != owner or job.epoch != epoch:
        raise DomainError("CONTROL_CONFLICT", "execution identity does not match")
    if job.lease_expires_at is None or job.lease_expires_at <= now:
        raise DomainError("CONTROL_CONFLICT", "lease expired")


def may_execute(job: Job, schedule: Schedule) -> bool:
    """A terminal job or a superseded schedule generation never executes."""
    if not isinstance(job, Job) or not isinstance(schedule, Schedule):
        raise DomainError("INVALID_INPUT", "job and schedule are required")
    if job.schedule_id != schedule.schedule_id:
        raise DomainError("INVALID_INPUT", "job belongs to another schedule")
    if JOB.terminal(job.state) or job.state != "pending":
        return False
    if schedule.cancelled or job.schedule_generation != schedule.generation:
        return False
    return True


def supersede_schedule(
    schedule: Schedule, *, due_at: datetime | None = None
) -> Schedule:
    """Change or cancel a schedule by bumping its generation.

    Jobs created under the old generation stop being executable, which is how
    the change atomically invalidates them.
    """
    if not isinstance(schedule, Schedule):
        raise DomainError("INVALID_INPUT", "schedule is required")
    update: dict[str, object] = {"generation": schedule.generation + 1}
    if due_at is not None:
        update["due_at"] = due_at
    return schedule.model_copy(update=update)
