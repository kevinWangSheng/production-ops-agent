"""0007 ending job identity: the sampling job an ending closed.

Revision ID: 0007_ending_job_identity
Revises: 0006_healthy_streak_window_end
Create Date: 2026-10-09

Issue #164 (user decision 2026-10-08; C3 section 10, F6 persisted-authority
guard). ``end_session`` clears the session's task slot, so a job that was
claimed but never produced a sample (revoked or expired before its result
arrived) left no trace: the acceptance projection could not tell "no
follow-up job was scheduled" from "the old job is still out there", and the
old job's identity only appeared when its late result was filed as history.
The ending record (append-only, one per ended session) now carries the
closed slot's ``job_id`` and ``job_sequence`` (both NULL when no job was
active, and for endings recorded before this revision, whose slot is gone).
The Observer role may write the two columns like the others on that table.
"""

from collections.abc import Sequence

from alembic import op

from opspilot.observation.revocation import ENDING_JOB_IDENTITY_DDL

OBSERVER_ROLE = "opspilot_observer"

revision: str = "0007_ending_job_identity"
down_revision: str | Sequence[str] | None = "0006_healthy_streak_window_end"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # idempotent: 0006 already installed the columns on the way here when it
    # ended open sessions (its docstring); this covers databases at 0006
    op.execute(ENDING_JOB_IDENTITY_DDL)
    op.execute(
        f"GRANT INSERT (job_id, job_sequence) ON opspilot_observation_endings TO {OBSERVER_ROLE}"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE opspilot_observation_endings DROP COLUMN job_id, DROP COLUMN job_sequence"
    )
