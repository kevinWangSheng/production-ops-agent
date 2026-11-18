"""0025: the web rebinds a recovery session when it cancels a continuation.

Revision ID: 0025_web_session_rebind
Revises: 0024_trace_outbox
Create Date: 2026-11-17

Workbench redesign contract A11 r4 (user approved the grant 2026-11-17):
cancelling a continuation Run is a human decision, so it steps the
incident's control generation and fences stale forms; the recovery
observation must survive it. In the cancel's own transaction, under the
incident lock, the web rebinds the authorized session's
``subject_control_generation`` to the new generation and releases its
sample lease (already-granted columns), so a sample in flight under the old
generation is filed as lease-invalid history instead of ending the session
as ``binding_stale``. One column-level UPDATE for ``opspilot_web``; no table
or row changes. ``downgrade()`` revokes it.
"""

from alembic import op

revision = "0025_web_session_rebind"
down_revision = "0024_trace_outbox"
branch_labels = None
depends_on = None

# Frozen incremental grant: do not import the evolving RUNTIME_GRANTS.
WEB_GRANTS = (
    ("opspilot_observation_sessions", "UPDATE", "subject_control_generation"),
)


def upgrade() -> None:
    for table, privilege, columns in WEB_GRANTS:
        op.execute(f"GRANT {privilege} ({columns}) ON {table} TO opspilot_web")


def downgrade() -> None:
    for table, privilege, columns in reversed(WEB_GRANTS):
        op.execute(f"REVOKE {privilege} ({columns}) ON {table} FROM opspilot_web")
