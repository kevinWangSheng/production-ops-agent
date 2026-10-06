"""0002 state checks: CHECK constraints on the state columns (ADR-0007 decision 4).

Revision ID: 0002_state_checks
Revises: 0001_baseline
Create Date: 2026-10-05

Hardening record C2 (2026-09-15): ``UPDATE opspilot_runs SET state='banana'``
succeeded because the state columns were bare ``text NOT NULL``. Each
constraint here allows exactly the values of the matching ``Literal`` in
``opspilot/domain``; ``tests/test_schema_state_checks.py`` compares
``CHECKS`` against ``typing.get_args`` of those Literals so they cannot drift.

Only columns whose written values are a subset of a domain Literal are
constrained. The other enum-like text columns (``opspilot_incidents.state``,
``opspilot_steps.status``, ``opspilot_controls.action``,
``opspilot_inputs.kind``, ``opspilot_budget_reservations.state``,
``opspilot_evidence.status``) have no domain Literal or diverge from it; the
task record 2026-10-05 lists each one. They stay unconstrained on purpose.

Before adding a constraint the migration counts rows that would violate it.
Any hit raises :class:`opspilot.schema.IllegalStateValues` with every
table/column/value count; ``transaction_per_migration`` rolls the revision
back and nothing is altered (a legacy database stays stamped at 0001).
"""

import re
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

from opspilot.schema import IllegalStateValues

revision: str = "0002_state_checks"
down_revision: str | Sequence[str] | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (table, column) -> allowed values, verbatim from the domain Literal named
# in the comment. Keep the tuples literal: the unit test compares them with
# the Literals; importing the Literals here would make that test circular.
CHECKS: dict[tuple[str, str], tuple[str, ...]] = {
    # opspilot.domain.runs.RunExecution
    ("opspilot_runs", "state"): (
        "queued",
        "running",
        "waiting_human",
        "paused",
        "blocked",
        "completed",
        "failed",
        "cancelled",
        "budget_exhausted",
    ),
    # opspilot.domain.subjects.IncidentLifecycle
    ("opspilot_incidents", "lifecycle"): (
        "open",
        "observing_recovery",
        "resolved",
        "closed",
    ),
}

_WORD = re.compile(r"^[a-z_]+$")


def constraint_name(table: str, column: str) -> str:
    return f"{table}_{column}_check"


def _in_list(values: Sequence[str]) -> str:
    for value in values:
        if not _WORD.match(value):
            raise ValueError(f"state value {value!r} is not a plain lowercase word")
    return ", ".join(f"'{value}'" for value in values)


def _illegal_rows() -> list[tuple[str, str, str, int]]:
    bind = op.get_bind()
    found: list[tuple[str, str, str, int]] = []
    for (table, column), values in CHECKS.items():
        rows = bind.execute(
            text(
                f"SELECT {column}, count(*) FROM {table} "
                f"WHERE {column} NOT IN ({_in_list(values)}) "
                f"GROUP BY {column} ORDER BY {column}"
            )
        ).fetchall()
        found.extend((table, column, str(value), int(count)) for value, count in rows)
    return found


def upgrade() -> None:
    illegal = _illegal_rows()
    if illegal:
        raise IllegalStateValues(illegal)
    for (table, column), values in CHECKS.items():
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {constraint_name(table, column)} "
            f"CHECK ({column} IN ({_in_list(values)}))"
        )


def downgrade() -> None:
    for table, column in CHECKS:
        op.execute(
            f"ALTER TABLE {table} DROP CONSTRAINT {constraint_name(table, column)}"
        )
