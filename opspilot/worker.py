"""Worker-side recovery coordinator; persistence remains the fence authority."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from .persistence import DurableStore, Lease, PersistenceError
from .recovery import RecoveryPlan, recover


@dataclass(frozen=True)
class RecoverySession:
    plan: RecoveryPlan
    lease: Lease
    store: DurableStore
    renew_seconds: int = 420

    def _assert_current(self) -> None:
        if not self.store.lease_current(self.lease):
            raise PersistenceError("CONTROL_DENIED")
        current = self.store.rebuild(self.plan.incident_id)
        if (
            current["control_generation"] != self.lease.control_generation
            or current["run"]["run_id"] != self.lease.run_id
        ):
            raise PersistenceError("CONTROL_DENIED")

    def _renew(self) -> None:
        # Optional capability: DurableStore.renew_lease ships with #35, not yet
        # merged into this branch. Absent, this is a no-op and behavior is
        # unchanged; once present it keeps the lease alive across a tool call
        # (which can run as long as the tool timeout) before the commit that
        # depends on it, under the same fence and rejection semantics
        # (PersistenceError) as the rest of this module.
        renew = getattr(self.store, "renew_lease", None)
        if renew is not None:
            renew(self.lease, self.renew_seconds)

    def execute_pending(
        self, execute: Callable[[Mapping[str, Any]], Mapping[str, Any]]
    ) -> int:
        count = 0
        for item in self.plan.pending_tools:
            self._assert_current()
            result = execute(item)
            self._renew()
            self.store.commit_tool(
                self.lease, item["step_id"], int(item["ordinal"]), dict(result)
            )
            count += 1
        return count

    def publish(self, conclusion: dict[str, Any], *, step_id: UUID) -> bool:
        return self.store.publish(self.lease, conclusion, step_id=step_id)


@dataclass
class Worker:
    store: DurableStore
    versions: dict[str, str]
    owner: UUID

    @classmethod
    def create(cls, store: DurableStore, versions: dict[str, str]) -> "Worker":
        return cls(store, dict(versions), uuid4())

    def recover(self, incident_id: UUID) -> RecoveryPlan:
        return recover(self.store, incident_id)

    def claim(
        self, incident_id: UUID, run_id: UUID, *, lease_seconds: int = 30
    ) -> Lease:
        return self.store.claim(
            incident_id, run_id, self.owner, self.versions, lease_seconds
        )

    def resume(
        self,
        incident_id: UUID,
        *,
        lease_seconds: int = 30,
        renew_seconds: int = 420,
    ) -> RecoverySession:
        plan = self.recover(incident_id)
        if not plan.candidate:
            raise PersistenceError("CONTROL_DENIED")
        lease = self.claim(incident_id, plan.run_id, lease_seconds=lease_seconds)
        if plan.control_generation != lease.control_generation:
            self.store.abandon(lease)
            raise PersistenceError("CONTROL_DENIED")
        return RecoverySession(plan, lease, self.store, renew_seconds)
