"""Operator suspension writes for the web runtime role (F7)."""

from alembic import op

revision = "0017_web_suspension_grants"
down_revision = "0016_release_deadline_evidence"
branch_labels = None
depends_on = None

# Frozen incremental grants: do not import the evolving RUNTIME_GRANTS.
# target_id INSERT and updated_at UPDATE were already granted by 0011.
WEB_GRANTS = (
    ("opspilot_scope_controls", "UPDATE", "global_generation,global_suspended"),
    ("opspilot_target_suspensions", "INSERT", "generation,suspended"),
    ("opspilot_target_suspensions", "UPDATE", "generation,suspended"),
    ("opspilot_suspension_audit", "INSERT", "actor,generation,suspended,target_id"),
)


def upgrade() -> None:
    for table, privilege, columns in WEB_GRANTS:
        op.execute(f"GRANT {privilege} ({columns}) ON {table} TO opspilot_web")


def downgrade() -> None:
    for table, privilege, columns in reversed(WEB_GRANTS):
        op.execute(f"REVOKE {privilege} ({columns}) ON {table} FROM opspilot_web")
