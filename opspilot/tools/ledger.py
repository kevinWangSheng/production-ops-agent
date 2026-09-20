"""Durable tool budget ledger backed by the product DurableStore.

The executor's per-Run tool ceilings (20 operations / 240 s, frozen for
M1-01) must survive a worker restart (technical plan section 13). This
adapter binds one claimed lease to :class:`~opspilot.persistence.DurableStore`:
``usage()`` reads what every earlier attempt of the Run already spent from
the committed run row, and ``charge`` records each dispatched operation
through the lease-fenced ``charge_tool`` write path.
"""

from __future__ import annotations

from uuid import UUID

from opspilot.persistence import DurableStore, Lease, PersistenceError

from .executor import ToolBudgetExhausted, ToolControlDenied, ToolUsage

__all__ = ["DurableToolLedger"]


class DurableToolLedger:
    """``ToolUsageLedger`` over committed PostgreSQL rows for one lease.

    ``max_operations`` is required, not defaulted to the frozen global
    ceiling: a caller wiring this ledger to a specific Run must pass that
    Run's own ``QueryScope.max_operations``, which may be narrower than the
    global cap. A silent default here would let the durable charge path
    enforce the wrong (wider) ceiling for a Run authorized under a tighter
    one -- the same shape of gap ``charge_tool``'s own ``max_operations``
    kwarg was made required to close (bot review finding).
    """

    def __init__(
        self,
        store: DurableStore,
        lease: Lease,
        *,
        max_operations: int,
    ) -> None:
        self._store = store
        self._lease = lease
        self._max_operations = max_operations

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
                dispatch_id=dispatch_id,
            )
        except PersistenceError as exc:
            # OPERATION_BUDGET_EXHAUSTED is charge_tool's own fixed code
            # (never vendor/storage text), so it is safe to translate into
            # the abstract ToolUsageLedger contract's dedicated exception
            # rather than the generic opaque failure every other
            # PersistenceError collapses to (bot review finding).
            if str(exc) == "OPERATION_BUDGET_EXHAUSTED":
                raise ToolBudgetExhausted(str(exc)) from exc
            if str(exc) == "CONTROL_DENIED":
                # Also a fixed code from charge_tool, and also authoritative:
                # the Run was revoked by a human decision, a newer control
                # generation or an expired lease. Reported as its own typed
                # signal so the executor keeps the observation as history and
                # names the real reason instead of a generic ledger outage
                # (bot review finding).
                raise ToolControlDenied(str(exc)) from exc
            raise
