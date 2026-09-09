"""Shared identities and budget seam for synthetic M0 experiments only."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Protocol
from uuid import UUID


class BudgetError(Exception):
    """Only fixed codes may leave the experiment boundary."""


@dataclass(frozen=True)
class RunContext:
    experiment_id: UUID
    run_id: UUID
    provider: str
    deadline: datetime

    def __post_init__(self):
        if (
            not isinstance(self.experiment_id, UUID)
            or not isinstance(self.run_id, UUID)
            or self.provider != "deepseek"
            or not isinstance(self.deadline, datetime)
            or self.deadline.tzinfo is None
            or self.deadline.utcoffset() is None
        ):
            raise BudgetError("INVALID_INPUT")
        object.__setattr__(self, "deadline", self.deadline.astimezone(timezone.utc))


@dataclass(frozen=True)
class RequestIdentity:
    run: RunContext
    request_id: UUID

    def __post_init__(self):
        if not isinstance(self.run, RunContext) or not isinstance(
            self.request_id, UUID
        ):
            raise BudgetError("INVALID_INPUT")


@dataclass(frozen=True)
class Reservation:
    request: RequestIdentity
    reserved: int
    state: Literal["reserved", "unknown", "settled"]
    actual: int | None
    created: bool = False


class Budget(Protocol):
    def reserve(self, request: RequestIdentity, upper_bound: int) -> Reservation: ...

    def settle(self, request: RequestIdentity, actual: int) -> Reservation: ...

    def retain_unknown(self, request: RequestIdentity) -> Reservation: ...


def amount(value: int, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        raise BudgetError("INVALID_INPUT")
    return value


def remaining_seconds(run: RunContext, now: datetime, timeout: float) -> float:
    if (
        not isinstance(now, datetime)
        or now.tzinfo is None
        or now.utcoffset() is None
        or type(timeout) not in (int, float)
        or not 0 < timeout < float("inf")
    ):
        raise BudgetError("INVALID_INPUT")
    remaining = min(timeout, (run.deadline - now).total_seconds())
    if remaining <= 0:
        raise BudgetError("DEADLINE_EXCEEDED")
    return remaining
