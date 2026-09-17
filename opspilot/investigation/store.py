"""Step-commit seam used by the investigation loop.

The product DurableStore is the cross-process authority (technical plan
section 7). Tests drive the loop through an in-memory adapter that speaks
the same three methods, so pairing, budget and deadline behaviour can be
asserted without PostgreSQL.
"""

from __future__ import annotations

from collections.abc import Mapping
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


class StepCommitter(Protocol):
    @property
    def authorized_run_id(self) -> str: ...

    def reserve_budget(self, reservation_id: UUID, amount: int) -> None: ...

    def settle_budget(self, reservation_id: UUID, outcome: str) -> None: ...

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
    """Attempt-local store with the same refusal codes as DurableStore."""

    def __init__(
        self,
        *,
        budget_limit: int,
        deadline: datetime,
        clock: Clock,
        run_id: str,
        control_denied: bool = False,
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
        self.budget_reserved = 0
        self.budget_spent = 0
        self.budget_unknown = 0
        self.reservations: dict[UUID, int] = {}
        self.settled: dict[UUID, str] = {}
        self.steps: dict[str, dict[str, Any]] = {}
        self.step_ids: dict[str, UUID] = {}
        self.tool_results: dict[UUID, list[dict[str, Any]]] = {}
        self.late_results: list[dict[str, Any]] = []

    def deny_control(self) -> None:
        self._control_denied = True

    def _guard(self) -> None:
        if self._control_denied:
            raise StepStoreError("CONTROL_DENIED")
        if self._clock.now() >= self.deadline:
            raise StepStoreError("CONTROL_DENIED")

    def reserve_budget(self, reservation_id: UUID, amount: int) -> None:
        if amount <= 0:
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
        self.budget_reserved += amount

    def settle_budget(self, reservation_id: UUID, outcome: str) -> None:
        if outcome not in ("spent", "unknown"):
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
            "logical_key": logical_key,
            "status": "response_committed",
            "response": dict(response),
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


class DurableStepStore:
    """Forwards the three loop methods to a claimed DurableStore lease."""

    def __init__(self, store: DurableStore, lease: Lease) -> None:
        self._store = store
        self._lease = lease

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

    def reserve_budget(self, reservation_id: UUID, amount: int) -> None:
        try:
            self._store.reserve_budget(
                self._lease, self._attempt_reservation(reservation_id), amount
            )
        except PersistenceError as exc:
            raise StepStoreError(str(exc)) from None

    def settle_budget(self, reservation_id: UUID, outcome: str) -> None:
        try:
            self._store.settle_budget(
                self._lease, self._attempt_reservation(reservation_id), outcome
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
