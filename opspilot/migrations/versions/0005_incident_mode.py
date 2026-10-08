"""0005 incident mode: the persisted ``automatic`` / ``human_owned`` control mode.

Revision ID: 0005_incident_mode
Revises: 0004_target_identity
Create Date: 2026-10-07

M1-02 step 3b (issue #121; C3 section 10 "接管默认停止自动观察；人工可在
human_owned 状态下单独授权观察，但该授权不恢复 Agent 自动调查"). The domain
``ControlState.mode`` (``opspilot.domain.control.ObservationMode``) had no
column; a takeover now persists ``human_owned`` on the incident row, and
every path that would start automatic investigation (claim, new Run,
resume, a renewal) reads it. Values equal the domain Literal
(``tests/test_schema_incident_mode.py`` compares ``CHECKS``). Existing rows
are ``automatic``, which is what they were. The domain defines no transition
back to ``automatic``, so none exists here either (#124). ``downgrade()``
refuses while any incident is ``human_owned``: dropping the column would turn
a human takeover back into automation.
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

from opspilot.schema import HumanOwnershipWouldBeLost

revision: str = "0005_incident_mode"
down_revision: str | Sequence[str] | None = "0004_target_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (table, column) -> allowed values, verbatim from the domain Literal named
# in the comment (same discipline as 0002/0003: literal tuple, compared by test).
CHECKS: dict[tuple[str, str], tuple[str, ...]] = {
    # opspilot.domain.control.ObservationMode
    ("opspilot_incidents", "mode"): ("automatic", "human_owned"),
}


def upgrade() -> None:
    values = ", ".join(f"'{value}'" for value in CHECKS[("opspilot_incidents", "mode")])
    op.execute(
        "ALTER TABLE opspilot_incidents ADD COLUMN mode text NOT NULL DEFAULT 'automatic' "
        f"CONSTRAINT opspilot_incidents_mode_check CHECK (mode IN ({values}))"
    )


def downgrade() -> None:
    # Fail closed: a human takeover is a control decision the schema must not
    # erase (bot review of PR #123, P1). Take the table lock first: a takeover
    # in flight (uncommitted) would otherwise be invisible to the count and
    # then be erased by the DROP that waits behind it (final recheck). With
    # ACCESS EXCLUSIVE held, every concurrent control transaction has either
    # committed before the count or waits until this transaction ends.
    op.execute("LOCK TABLE opspilot_incidents IN ACCESS EXCLUSIVE MODE")
    owned: int = int(
        op.get_bind()
        .execute(
            text("SELECT count(*) FROM opspilot_incidents WHERE mode='human_owned'")
        )
        .scalar_one()
    )
    if owned:
        raise HumanOwnershipWouldBeLost(owned)
    op.execute("ALTER TABLE opspilot_incidents DROP COLUMN mode")
