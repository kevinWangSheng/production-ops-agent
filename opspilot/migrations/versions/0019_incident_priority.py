"""Incident claim priority (F2 PR-B1, contract U-B1-1).

``opspilot_incidents.priority``: 0 critical, 1 warning, 2 other (U5-A). Set
once when the incident is created; every Run of the incident inherits it and
the worker's claim listing orders by it before the deadline. Existing rows
take the default 2 (R-B1-6: no backfill), which keeps their current
deadline order. The web role may write the column; the worker already reads
the table and gets nothing new.
"""

from alembic import op

revision = "0019_incident_priority"
down_revision = "0018_alert_correlation"
branch_labels = None
depends_on = None

# Frozen incremental grants: do not import the evolving RUNTIME_GRANTS.
WEB_GRANTS = (
    ("opspilot_incidents", "INSERT", "priority"),
    ("opspilot_incidents", "UPDATE", "priority"),
)


def upgrade() -> None:
    op.execute(
        "ALTER TABLE opspilot_incidents ADD COLUMN priority smallint NOT NULL "
        "DEFAULT 2 CONSTRAINT opspilot_incidents_priority_check "
        "CHECK (priority IN (0,1,2))"
    )
    for table, privilege, columns in WEB_GRANTS:
        op.execute(f"GRANT {privilege} ({columns}) ON {table} TO opspilot_web")


def downgrade() -> None:
    for table, privilege, columns in reversed(WEB_GRANTS):
        op.execute(f"REVOKE {privilege} ({columns}) ON {table} FROM opspilot_web")
    # Dropping the column drops its column privileges and CHECK with it.
    op.execute("ALTER TABLE opspilot_incidents DROP COLUMN priority")
