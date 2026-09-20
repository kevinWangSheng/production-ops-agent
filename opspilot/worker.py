"""Worker-side recovery coordinator; persistence remains the fence authority."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
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
    # One lease is one execution attempt (C3 §6), so dispatch is serialized
    # within the session: the pending check and the callback are several
    # non-atomic steps apart, and two in-process callers could otherwise both
    # see an ordinal as pending and issue the same external query. Duplication
    # by a *different* worker is already prevented by commit_tool's
    # owner/epoch fence; this closes the one gap that fence cannot see.
    _dispatch_lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    def _assert_current(self) -> Mapping[str, Any]:
        # A read that *fails* and a fence that *rejects* need opposite
        # handling, so every read here is wrapped and every rejection is not.
        # A failed read leaves this session possibly still holding a live
        # lease: unwinding without releasing it blocks every other worker
        # until the lease expires, turning a brief storage outage into a
        # renew_seconds-long recovery outage. A rejection needs no release --
        # abandon() matches on owner+epoch+generation, so the row is either
        # already another worker's or an expired lease claim() treats as free.
        try:
            fenced = self.store.lease_current(self.lease)
        except PersistenceError:
            self._abandon_best_effort()
            raise
        if not fenced:
            raise PersistenceError("CONTROL_DENIED")
        try:
            current = self.store.rebuild(self.plan.incident_id)
        except PersistenceError:
            self._abandon_best_effort()
            # A Run swapped out by human control is a control outcome, not
            # corrupt business state; separate the two before surfacing this.
            self._raise_if_superseded()
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

    def _raise_if_superseded(self) -> None:
        """Report a Run replaced by human control as control, not corruption.

        A cancel/new_run landing between the fence read and the rebuild makes
        this session decode a Run it never held, possibly under a schema this
        version does not understand. The decode then fails as
        ``INCONSISTENT_STATE`` when the truthful answer is that control moved
        on. Read identity only -- ``recovery_metadata`` decodes no versioned
        payload -- to tell the two apart, and leave a genuinely corrupt Run of
        our own reported as corrupt.

        This deliberately does not claim or block the replacement. A session
        holds exactly one lease and carries no ``versions``; blocking a Run it
        never claimed would take authority outside that lease. The
        incompatible-version handoff belongs to the claim path, which
        ``Worker.resume`` runs before decoding anything.
        """
        reader = getattr(self.store, "recovery_metadata", None)
        if reader is None:
            return
        try:
            metadata = reader(self.plan.incident_id)
        except PersistenceError:
            # Keep the original decode error rather than replacing it with a
            # failure of this diagnostic read.
            return
        if (
            metadata["run_id"] != self.lease.run_id
            or metadata["control_generation"] != self.lease.control_generation
        ):
            raise PersistenceError("CONTROL_DENIED")

    def _renew_after_call(self) -> None:
        """Keep the lease alive for the commit, but never replace it as fence.

        Renewing after the callback is an optimisation: it stops a long call
        from losing a lease that is still rightfully held. ``commit_tool`` is
        the authority on whether the result may be admitted, and it is also
        what records a rejected one as a ``late_result`` history row. So a
        denied or failed renewal must not short-circuit the commit -- doing
        that drops a query that really did reach the outside world, leaving
        no durable evidence it ran.
        """
        try:
            self._renew()
        except PersistenceError:
            pass

    def _abandon_best_effort(self) -> None:
        try:
            self.store.abandon(self.lease)
        except Exception:
            pass

    def execute_pending(
        self, execute: Callable[[Mapping[str, Any]], Mapping[str, Any]]
    ) -> int:
        with self._dispatch_lock:
            return self._execute_pending_locked(execute)

    def _execute_pending_locked(
        self, execute: Callable[[Mapping[str, Any]], Mapping[str, Any]]
    ) -> int:
        count = 0
        for item in self.plan.pending_tools:
            current = self._assert_current()
            if not _still_pending(current, item):
                # The plan is a snapshot; the committed rows are the
                # authority.  This call has since been committed under this
                # same lease -- by an earlier pass over this session, or by a
                # concurrent caller that held the dispatch lock first -- so
                # dispatching it again would repeat an external query only for
                # ``commit_tool`` to drop the result as a duplicate.  (A
                # *different* worker cannot
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
                # Best-effort, unlike the pre-dispatch renewal above: by now
                # the external call has happened, so the commit must be
                # attempted whatever the lease says (see _renew_after_call).
                self._renew_after_call()
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
        try:
            return self.store.publish(self.lease, conclusion, step_id=step_id)
        except PersistenceError:
            # Release on any PersistenceError, matching the tool-commit path;
            # nothing in this package branches on error codes. That
            # deliberately includes store.publish's deterministic rejections
            # (FINAL_STEP_REQUIRED, UNKNOWN_IDENTITY), not just transient
            # storage failures, so the session is finished either way: the
            # lease is gone afterwards and a caller who wants another attempt
            # must resume() for a fresh epoch rather than reuse this one.
            # Whether FINAL_STEP_REQUIRED deserves to keep its lease is worth
            # revisiting when the investigation loop actually calls this.
            # A *revoked* lease is not an exception here -- store.publish
            # returns False for that -- so human control never lands here.
            # Best-effort: releasing must never replace the original error.
            self._abandon_best_effort()
            raise


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

    def _gate_versions(self, incident_id: UUID, lease_seconds: int) -> None:
        """Route a version-incompatible Run to the durable blocked handoff.

        Read identity and versions without decoding any version-specific step
        payload, so an incompatible Run is persisted as blocked instead of
        being rejected by validators that do not understand its rows.
        """
        metadata_reader = getattr(self.store, "recovery_metadata", None)
        if metadata_reader is None:
            return
        metadata = metadata_reader(incident_id)
        if metadata["versions"] != self.versions:
            self.claim(incident_id, metadata["run_id"], lease_seconds=lease_seconds)
            raise PersistenceError("INCOMPATIBLE_STATE")

    def resume(
        self,
        incident_id: UUID,
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        renew_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> RecoverySession:
        self._gate_versions(incident_id, lease_seconds)
        try:
            plan = self.recover(incident_id)
        except PersistenceError:
            # The gate above and this decode are separate transactions, so the
            # Run can be replaced in between -- a cancel followed by a new_run
            # whose steps a different-version worker already committed. Old
            # validators then meet new rows and fail before the incompatible
            # handoff is persisted. Re-run the gate: if the current Run is now
            # version-incompatible, blocked/INCOMPATIBLE_STATE is the right
            # outcome rather than this decode error. A compatible Run still
            # surfaces its decode error as itself. If the re-gate *itself*
            # fails, its error replaces the decode error (which survives as
            # __context__); the next retry runs the ordinary gate first, so
            # this converges rather than latching.
            self._gate_versions(incident_id, lease_seconds)
            raise
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
            # The same replacement race as the pre-decode gate, one step
            # later: a cancel followed by a new_run can swap in a Run whose
            # payload this version cannot decode. Re-gate once the stale
            # lease is released, so an incompatible replacement reaches the
            # durable blocked handoff instead of being reported as corrupt
            # state. A compatible Run still surfaces its decode error as
            # itself; if the re-gate fails, its error replaces this one
            # (kept as __context__) and the next retry gates first.
            self._gate_versions(incident_id, lease_seconds)
            raise
        if (
            not current.candidate
            or current.run_id != lease.run_id
            or current.control_generation != lease.control_generation
        ):
            self.store.abandon(lease)
            raise PersistenceError("CONTROL_DENIED")
        return RecoverySession(current, lease, self.store, renew_seconds)
