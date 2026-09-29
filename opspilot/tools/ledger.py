"""Durable tool budget ledger backed by the product DurableStore.

This adapter binds one claimed lease to
:class:`~opspilot.persistence.DurableStore`: ``usage()`` reads what every
earlier attempt of the Run already spent from the committed run row, and
``charge`` records each dispatched operation through the lease-fenced
``charge_tool`` write path.

M1-01 (2026-09-28 user decision, docs/tasks/2026-09-28-m1-01-loop-limits.md,
L2): ``opspilot_runs.tool_operations_used``/``tool_seconds_used`` keep
accumulating without limit -- accounting and audit, not a per-Run ceiling.
The loop's single anti-loop ceiling is the model-request count (L1).
"""

from __future__ import annotations

from uuid import UUID

from opspilot.persistence import DurableStore, Lease, PersistenceError

from .executor import ToolControlDenied, ToolUsage

__all__ = ["DurableToolLedger"]


class DurableToolLedger:
    """``ToolUsageLedger`` over committed PostgreSQL rows for one lease.

    **Known limit — durable suspension fencing.** The lease this adapter
    carries fences on the *incident* control generation only. The global and
    target suspension generations the executor now compares
    (``QueryScope``/``ControlSnapshot``) stop at those snapshots: the durable
    store has no suspension state and no target identities at all, so there is
    nothing for ``charge_tool`` to compare them against. A global or target
    suspension landing between the executor's last snapshot and this write is
    therefore not caught atomically (bot review finding). Closing it requires
    persisting ``opspilot.domain.control.SuspensionState`` -- global and
    per-target generations plus resolved target identities -- which is the
    Controller's work, not this adapter's.

    ``max_operations``/``max_tool_seconds`` stay required constructor
    parameters purely to keep ``charge_tool``'s input-validation shape
    unchanged for every existing caller (M1-01, 2026-09-28 user decision,
    L2): the durable write path no longer refuses a charge on either value,
    so any positive number works and neither is enforced as a ceiling any
    more.
    """

    def __init__(
        self,
        store: DurableStore,
        lease: Lease,
        *,
        max_operations: int,
        max_tool_seconds: float,
    ) -> None:
        self._store = store
        self._lease = lease
        self._max_operations = max_operations
        self._max_tool_seconds = max_tool_seconds

    def usage(self) -> ToolUsage:
        run = self._store.rebuild(self._lease.incident_id)["run"]
        if run["run_id"] != self._lease.run_id:
            raise RuntimeError("INCONSISTENT_STATE")
        return ToolUsage(
            operations_used=int(run["tool_operations_used"]),
            tool_seconds_used=float(run["tool_seconds_used"]),
        )

    def charge(self, operation_id: str, seconds: float, *, dispatch_id: UUID) -> None:
        try:
            self._store.charge_tool(
                self._lease,
                operation_id,
                seconds,
                max_operations=self._max_operations,
                max_tool_seconds=self._max_tool_seconds,
                dispatch_id=dispatch_id,
            )
        except PersistenceError as exc:
            # M1-01 (2026-09-28 user decision, L2): ``charge_tool`` no longer
            # raises OPERATION_BUDGET_EXHAUSTED/TIME_BUDGET_EXHAUSTED (the
            # ceiling checks are gone), so there is nothing left to translate
            # for those two codes -- only CONTROL_DENIED remains a fixed code
            # worth its own typed signal here.
            if str(exc) == "CONTROL_DENIED":
                # A fixed code from charge_tool, and authoritative: the Run
                # was revoked by a human decision, a newer control generation
                # or an expired lease. Reported as its own typed signal so the
                # executor keeps the observation as history and names the
                # real reason instead of a generic ledger outage (bot review
                # finding).
                raise ToolControlDenied(str(exc)) from exc
            raise
