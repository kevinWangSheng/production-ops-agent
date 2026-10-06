"""PostgreSQL business-state authority for the first durable M1 slice."""

from __future__ import annotations

from opspilot.persistence.base import Connection, Lease, PersistenceError
from opspilot.persistence.budgets import _BudgetOps
from opspilot.persistence.controls import _ControlOps
from opspilot.persistence.incidents import _IncidentOps
from opspilot.persistence.runs import _RunOps
from opspilot.persistence.steps import _StepOps, _tool_plan

__all__ = ["Connection", "DurableStore", "Lease", "PersistenceError", "_tool_plan"]


class DurableStore(_IncidentOps, _RunOps, _ControlOps, _BudgetOps, _StepOps):
    """Small transactional store; callers only observe committed business rows."""
