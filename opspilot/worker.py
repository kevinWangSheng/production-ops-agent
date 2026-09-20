"""Worker-side recovery coordinator; persistence remains the fence authority."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from .persistence import DurableStore, Lease, PersistenceError
from .recovery import RecoveryPlan, recover

# Borrowed from #33/#35's model-request-timeout-derived lease length, for a
# single default shared by the initial claim and every renewal. execute_pending
# renews before dispatching each tool and renew_lease never shortens a lease
# (LEAST(GREATEST(lease_until, now+extend), deadline)), so what covers a tool's
# own execution is renew_seconds, not this claim default. The initial claim
# still has to cover the window from claim to that first renewal -- resume's
# post-claim rebuild and the per-item fence read -- and is the only cover at
# all for a minimal store double that has no renew_lease. RecoverySession
# itself never calls a model -- it replays already-committed tool calls -- so
# this value is not independently re-derived from a tool-execution time cap
# here; no such cap is enforced in this branch's code.
DEFAULT_LEASE_SECONDS = 420


def _still_pending(snapshot: Mapping[str, Any], item: Mapping[str, Any]) -> bool:
    """Is this planned call still outstanding in the committed business rows?"""
    key = (item["step_id"], int(item["ordinal"]))
    return any(
        (pending["step_id"], int(pending["ordinal"])) == key
        for pending in snapshot["pending_tools"]
    )


@dataclass(frozen=True)
class RecoverySession:
    plan: RecoveryPlan
    lease: Lease
    store: DurableStore
    renew_seconds: int = DEFAULT_LEASE_SECONDS

    def _assert_current(self) -> Mapping[str, Any]:
        # A fence rejection needs no release: abandon() matches on
        # owner+epoch+generation, so the row is either already another
        # worker's or an expired lease that claim() treats as free anyway.
        if not self.store.lease_current(self.lease):
            raise PersistenceError("CONTROL_DENIED")
        try:
            current = self.store.rebuild(self.plan.incident_id)
        except PersistenceError:
            # A transient read failure is different: the fence above just
            # confirmed this lease is live, so returning without releasing it
            # would block every other worker until it expires. Mirror
            # Worker.resume's post-claim refresh and preserve the read error.
            self._abandon_best_effort()
            raise
        if (
            current["control_generation"] != self.lease.control_generation
            or current["run"]["run_id"] != self.lease.run_id
        ):
            raise PersistenceError("CONTROL_DENIED")
        return current

    def _renew(self) -> None:
        # DurableStore.renew_lease is present on the current main base. Keep
        # getattr for minimal store doubles and older callers; when absent,
        # this remains a no-op. When present it keeps the lease alive across a
        # tool call before the commit that depends on it, under the same fence
        # and rejection semantics (PersistenceError) as the rest of this
        # module.
        renew = getattr(self.store, "renew_lease", None)
        if renew is not None:
            renew(self.lease, self.renew_seconds)

    def _abandon_best_effort(self) -> None:
        try:
            self.store.abandon(self.lease)
        except Exception:
            pass

    def execute_pending(
        self, execute: Callable[[Mapping[str, Any]], Mapping[str, Any]]
    ) -> int:
        count = 0
        for item in self.plan.pending_tools:
            current = self._assert_current()
            if not _still_pending(current, item):
                # The plan is a snapshot; the committed rows are the
                # authority.  This call has since been committed under this
                # same lease -- an earlier pass over this session, or a
                # parallel caller holding it -- so dispatching it again would
                # repeat an external query only for ``commit_tool`` to drop
                # the result as a duplicate.  (A *different* worker cannot
                # retire it while this lease is live: commit_tool fences on
                # owner and epoch, and if this lease were gone the
                # lease_current check above would have failed first.)
                continue
            try:
                # Renew before dispatch as well as after it.  The lease
                # remaining at entry can be shorter than the callback (a
                # caller may claim with a short ``lease_seconds``); if it
                # lapsed mid-call, another worker could claim the Run and
                # run the same operation concurrently while this session's
                # own result is later rejected.  This protects callbacks up
                # to ``renew_seconds``.  Bounding the callback itself with a
                # wall clock belongs to the tool executor, which owns the
                # timeout; nothing here can cap an opaque callable.
                self._renew()
                result = execute(item)
                # The lease must still be live for the commit the callback's
                # result depends on, so renew again after a long call.
                self._renew()
                self.store.commit_tool(
                    self.lease, item["step_id"], int(item["ordinal"]), dict(result)
                )
            except BaseException:
                # A callback may have performed an external query before it
                # failed, and a successful callback still has no durable
                # outcome until renewal and commit both return.  Keep the
                # original failure visible, but relinquish this exact lease
                # so a retry can take over immediately instead of waiting for
                # its full expiry.  The cleanup is deliberately best-effort:
                # a storage failure must never replace the original
                # executor/persistence exception.
                self._abandon_best_effort()
                raise
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
        metadata_reader = getattr(self.store, "recovery_metadata", None)
        if metadata_reader is not None:
            metadata = metadata_reader(incident_id)
            if metadata["versions"] != self.versions:
                # Let the durable claim path persist the incompatible handoff
                # before any version-specific step payload is decoded.
                self.claim(
                    incident_id,
                    metadata["run_id"],
                    lease_seconds=lease_seconds,
                )
                raise PersistenceError("INCOMPATIBLE_STATE")
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
