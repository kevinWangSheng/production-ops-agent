"""Step-commit seam used by the investigation loop.

The product DurableStore is the cross-process authority (technical plan
section 7). Tests drive the loop through an in-memory adapter that speaks
the same methods, so pairing, budget, deadline and resume behaviour can be
asserted without PostgreSQL.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID, uuid4, uuid5

from opspilot.persistence import DurableStore, Lease, PersistenceError
from opspilot.tools.executor import Clock

_RESERVATION_NAMESPACE = UUID("6a0b1c2d-3e4f-5016-a7b8-c9d0e1f20314")


class StepStoreError(Exception):
    """Fixed-code failure of reserve/commit. ``code`` matches PersistenceError."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class BudgetUsage:
    """What earlier attempts of this Run already consumed (C3 §13)."""

    model_requests_used: int
    model_seconds_used: float


class StepCommitter(Protocol):
    @property
    def authorized_run_id(self) -> str: ...

    def usage(self) -> BudgetUsage: ...

    def reserve_budget(
        self, reservation_id: UUID, amount: int, *, seconds: float = 0.0
    ) -> None: ...

    def settle_budget(
        self, reservation_id: UUID, outcome: str, *, seconds: float | None = None
    ) -> None: ...

    def commit_step(self, logical_key: str, response: Mapping[str, Any]) -> UUID: ...

    def commit_tool(
        self, step_id: UUID, ordinal: int, result: Mapping[str, Any]
    ) -> None: ...


def reservation_id_for(run_id: str, logical_key: str) -> UUID:
    """Stable per-run reservation so a retried round does not double-charge.

    The key includes ``run_id``. A global key of only ``round-1`` would collide
    across investigations in DurableStore and abort the next Run with
    ``IDENTITY_CONFLICT``.
    """
    if (
        not isinstance(run_id, str)
        or not run_id
        or not isinstance(logical_key, str)
        or not logical_key
    ):
        raise StepStoreError("INVALID_INPUT")
    return uuid5(_RESERVATION_NAMESPACE, f"{run_id}:{logical_key}")


class MemoryStepStore:
    """Attempt-local store with the same refusal codes as DurableStore.

    It also renders a ``snapshot()`` in the shape of ``DurableStore.rebuild``
    so the transcript rebuild can be tested without PostgreSQL. Rows survive
    across loop instances, which is how a test simulates a worker restart.
    """

    def __init__(
        self,
        *,
        budget_limit: int,
        deadline: datetime,
        clock: Clock,
        run_id: str,
        control_denied: bool = False,
        control_generation: int = 0,
        input: Mapping[str, Any] | None = None,
    ) -> None:
        if type(budget_limit) is not int or budget_limit < 0:
            raise StepStoreError("INVALID_INPUT")
        if not isinstance(run_id, str) or not run_id:
            raise StepStoreError("INVALID_INPUT")
        self.authorized_run_id = run_id
        self.budget_limit = budget_limit
        self.deadline = deadline
        self._clock = clock
        self._control_denied = control_denied
        self.control_generation = control_generation
        self.input = None if input is None else dict(input)
        self.budget_reserved = 0
        self.budget_spent = 0
        self.budget_unknown = 0
        self.reservations: dict[UUID, int] = {}
        self.reserved_seconds: dict[UUID, float] = {}
        self.settled: dict[UUID, str] = {}
        self.settled_seconds: dict[UUID, float] = {}
        self.steps: dict[str, dict[str, Any]] = {}
        self.step_ids: dict[str, UUID] = {}
        self.tool_results: dict[UUID, list[dict[str, Any]]] = {}
        self.late_results: list[dict[str, Any]] = []
        self.conclusion: dict[str, Any] | None = None
        self.state = "running"

    def deny_control(self) -> None:
        self._control_denied = True

    def advance_generation(self) -> int:
        """Simulate a human control action: later commits carry the new generation."""
        self.control_generation += 1
        return self.control_generation

    def _guard(self) -> None:
        if self._control_denied:
            raise StepStoreError("CONTROL_DENIED")
        if self._clock.now() >= self.deadline:
            raise StepStoreError("CONTROL_DENIED")

    def usage(self) -> BudgetUsage:
        seconds = 0.0
        for reservation_id, upper in self.reserved_seconds.items():
            seconds += self.settled_seconds.get(reservation_id, upper)
        return BudgetUsage(
            model_requests_used=self.budget_reserved
            + self.budget_spent
            + self.budget_unknown,
            model_seconds_used=seconds,
        )

    def reserve_budget(
        self, reservation_id: UUID, amount: int, *, seconds: float = 0.0
    ) -> None:
        if amount <= 0 or not seconds >= 0:
            raise StepStoreError("INVALID_INPUT")
        self._guard()
        existing = self.reservations.get(reservation_id)
        if existing is not None:
            if existing != amount:
                raise StepStoreError("IDENTITY_CONFLICT")
            return
        if (
            self.budget_reserved + self.budget_spent + self.budget_unknown + amount
            > self.budget_limit
        ):
            raise StepStoreError("BUDGET_EXHAUSTED")
        self.reservations[reservation_id] = amount
        self.reserved_seconds[reservation_id] = float(seconds)
        self.budget_reserved += amount

    def settle_budget(
        self, reservation_id: UUID, outcome: str, *, seconds: float | None = None
    ) -> None:
        if outcome not in ("spent", "unknown"):
            raise StepStoreError("INVALID_INPUT")
        if seconds is not None and not seconds >= 0:
            raise StepStoreError("INVALID_INPUT")
        self._guard()
        if reservation_id not in self.reservations:
            raise StepStoreError("UNKNOWN_IDENTITY")
        previous = self.settled.get(reservation_id)
        if previous is not None:
            if previous != outcome:
                raise StepStoreError("IDENTITY_CONFLICT")
            return
        amount = self.reservations[reservation_id]
        self.settled[reservation_id] = outcome
        self.settled_seconds[reservation_id] = (
            self.reserved_seconds[reservation_id] if seconds is None else float(seconds)
        )
        self.budget_reserved -= amount
        if outcome == "spent":
            self.budget_spent += amount
        else:
            self.budget_unknown += amount

    def commit_step(self, logical_key: str, response: Mapping[str, Any]) -> UUID:
        if self._control_denied or self._clock.now() >= self.deadline:
            # Fenced: this reply is no longer authorized to become the
            # current step, but it must not vanish -- it becomes
            # non-adopted history, the same way a late ``publish()``
            # conclusion is retained rather than discarded (bot review
            # finding, PR #29).
            self.late_results.append(
                {"logical_key": logical_key, "response": dict(response)}
            )
            raise StepStoreError("CONTROL_DENIED")
        if logical_key in self.step_ids:
            return self.step_ids[logical_key]
        step_id = uuid4()
        self.step_ids[logical_key] = step_id
        self.steps[logical_key] = {
            "step_id": step_id,
            "run_id": self.authorized_run_id,
            "sequence": len(self.steps),
            "logical_key": logical_key,
            "status": "response_committed",
            "response": dict(response),
            "control_generation": self.control_generation,
        }
        self.tool_results[step_id] = []
        return step_id

    def commit_tool(
        self, step_id: UUID, ordinal: int, result: Mapping[str, Any]
    ) -> None:
        self._guard()
        if step_id not in self.tool_results:
            raise StepStoreError("CONTROL_DENIED")
        existing = self.tool_results[step_id]
        if any(item["ordinal"] == ordinal for item in existing):
            return
        existing.append({"ordinal": ordinal, "result": dict(result)})
        for step in self.steps.values():
            if step["step_id"] == step_id:
                step["status"] = "tool_result_committed"

    def publish(self, conclusion: Mapping[str, Any], *, step_id: UUID) -> bool:
        self._guard()
        if step_id not in self.tool_results:
            raise StepStoreError("UNKNOWN_IDENTITY")
        self.conclusion = dict(conclusion)
        self.state = "completed"
        return True

    def snapshot(self) -> dict[str, Any]:
        """The same shape ``DurableStore.rebuild`` returns, for transcript tests."""
        steps = []
        for row in sorted(self.steps.values(), key=lambda item: item["sequence"]):
            steps.append(
                {
                    **row,
                    "response": dict(row["response"]),
                    "tool_results": [
                        {"ordinal": item["ordinal"], "result": dict(item["result"])}
                        for item in self.tool_results[row["step_id"]]
                    ],
                }
            )
        return {
            "incident_id": None,
            "state": self.state,
            "control_generation": self.control_generation,
            "run": {
                "run_id": self.authorized_run_id,
                "state": self.state,
                "budget_limit": self.budget_limit,
                "deadline": self.deadline,
                "input": None if self.input is None else dict(self.input),
            },
            "steps": steps,
            "pending_tools": [],
            "conclusion": None if self.conclusion is None else dict(self.conclusion),
        }


class DurableStepStore:
    """Forwards the loop methods to a claimed DurableStore lease."""

    def __init__(
        self, store: DurableStore, lease: Lease, *, renew_seconds: int | None = None
    ) -> None:
        self._store = store
        self._lease = lease
        # Renewed before every physical model request so a long loop keeps the
        # lease it rightfully holds; ``None`` keeps the pre-renewal behaviour
        # for callers that manage the lease themselves.
        self._renew_seconds = renew_seconds

    @property
    def authorized_run_id(self) -> str:
        return str(self._lease.run_id)

    def _attempt_reservation(self, reservation_id: UUID) -> UUID:
        """Namespace the loop's reservation id by this attempt's epoch.

        The loop derives ids from ``run_id`` and the round key, which repeat
        when a new attempt re-claims the same Run (C3 §7: bounded retry).
        Without the epoch the retry would reuse the dead attempt's row and
        settling it as ``spent`` after an ``unknown`` would be refused as an
        identity conflict; the dead attempt's reservation stays occupied.
        """
        return uuid5(_RESERVATION_NAMESPACE, f"{reservation_id}:e{self._lease.epoch}")

    def usage(self) -> BudgetUsage:
        try:
            row = self._store.run_usage(self._lease.run_id)
        except PersistenceError as exc:
            raise StepStoreError(str(exc)) from None
        return BudgetUsage(
            model_requests_used=int(row["model_requests_used"]),
            model_seconds_used=float(row["model_seconds_used"]),
        )

    def reserve_budget(
        self, reservation_id: UUID, amount: int, *, seconds: float = 0.0
    ) -> None:
        try:
            if self._renew_seconds is not None:
                self._store.renew_lease(self._lease, self._renew_seconds)
            self._store.reserve_budget(
                self._lease,
                self._attempt_reservation(reservation_id),
                amount,
                seconds=seconds,
            )
        except PersistenceError as exc:
            raise StepStoreError(str(exc)) from None

    def settle_budget(
        self, reservation_id: UUID, outcome: str, *, seconds: float | None = None
    ) -> None:
        try:
            self._store.settle_budget(
                self._lease,
                self._attempt_reservation(reservation_id),
                outcome,
                seconds=seconds,
            )
        except PersistenceError as exc:
            raise StepStoreError(str(exc)) from None

    def commit_step(self, logical_key: str, response: Mapping[str, Any]) -> UUID:
        try:
            return self._store.commit_step(self._lease, logical_key, dict(response))
        except PersistenceError as exc:
            raise StepStoreError(str(exc)) from None

    def commit_tool(
        self, step_id: UUID, ordinal: int, result: Mapping[str, Any]
    ) -> None:
        try:
            self._store.commit_tool(self._lease, step_id, ordinal, dict(result))
        except PersistenceError as exc:
            raise StepStoreError(str(exc)) from None
