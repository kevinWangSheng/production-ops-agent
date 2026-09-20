"""Worker-side recovery coordinator; persistence remains the fence authority."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from .persistence import DurableStore, Lease, PersistenceError
from .recovery import RecoveryPlan, recover

# Borrowed from #33/#35's model-request-timeout-derived lease length, for a
# single default shared by the initial claim and every renewal: an initial
# claim shorter than this would leave the *first* pending tool's own
# execution covered only by that short claim, since renewal only runs after
# a tool executes (see execute_pending). RecoverySession itself never calls
# a model -- it replays already-committed tool calls -- so this value is not
# independently re-derived from a tool-execution time cap here; no such cap
# is enforced in this branch's code.
DEFAULT_LEASE_SECONDS = 420


@dataclass(frozen=True)
class RecoverySession:
    plan: RecoveryPlan
    lease: Lease
    store: DurableStore
    renew_seconds: int = DEFAULT_LEASE_SECONDS

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
        self,
        incident_id: UUID,
        run_id: UUID,
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> Lease:
        return self.store.claim(
            incident_id, run_id, self.owner, self.versions, lease_seconds
        )

    def resume(
        self,
        incident_id: UUID,
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        renew_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> RecoverySession:
        plan = self.recover(incident_id)
        if not plan.candidate:
            raise PersistenceError("CONTROL_DENIED")
        lease = self.claim(incident_id, plan.run_id, lease_seconds=lease_seconds)
        # The snapshot used to choose the Run can become stale even when the
        # control generation is unchanged: another worker may commit one of
        # the pending tools and then lose its lease before this claim. Rebuild
        # after acquiring the new lease so execution starts from current
        # committed business rows, not the pre-claim plan.
        try:
            current = self.recover(incident_id)
        except PersistenceError:
            # A failed post-claim read must not strand this lease until its
            # full expiry. Preserve the refresh error while best-effort
            # releasing only this owner/epoch/generation lease.
            try:
                self.store.abandon(lease)
            except PersistenceError:
                pass
            raise
        if (
            not current.candidate
            or current.run_id != lease.run_id
            or current.control_generation != lease.control_generation
        ):
            self.store.abandon(lease)
            raise PersistenceError("CONTROL_DENIED")
        return RecoverySession(current, lease, self.store, renew_seconds)
