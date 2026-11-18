"""0027: feedback on an investigation's report (workbench redesign P5, contract r12).

Revision ID: 0027_investigation_feedback
Revises: 0026_chat_panel
Create Date: 2026-11-18

User approved the table and its grants (F1, 2026-11-18). One new table; no
existing table or grant changes.

``opspilot_investigation_feedback``: one row per incident (primary key
``incident_id``), replaced as a whole by the newest submission (O3/F6, last
write wins). The verdict on the report the page showed, the actual root
cause (only with an ``incorrect`` verdict), whether steering was needed,
whether the next steps were right and what they should have been (only with
an ``incorrect`` next-steps verdict), notes, the Run and report sha256 the
rating was given on (F9), and who saved it last and when.

Feedback is not a control action and not an input: nothing the worker
reads sees it (the worker, Observer and retention roles get nothing), it
does not move ``control_generation`` and it never becomes knowledge (F3,
F4, F12). Only the web reads and writes it; no role can DELETE.

``downgrade()`` never refuses (0024/0026 precedent): it reports the row
count (``RAISE NOTICE``, a count only), revokes the grants and drops the
table -- all feedback is lost. No other table changes.
"""

import logging
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0027_investigation_feedback"
down_revision: str | Sequence[str] | None = "0026_chat_panel"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_log = logging.getLogger("opspilot.migrations")

FEEDBACK = "opspilot_investigation_feedback"

# Frozen: equal to the feedback entry of ``RUNTIME_GRANTS`` in
# ``opspilot.persistence.grants`` at this revision (F1).
GRANTS: tuple[tuple[str, str, str], ...] = (
    ("opspilot_web", FEEDBACK, "SELECT"),
    (
        "opspilot_web",
        FEEDBACK,
        "INSERT (actual_root_cause,correct_next_steps,incident_id,next_steps_verdict,"
        "notes,rated_report_sha256,rated_run_id,steering_needed,updated_by,verdict)",
    ),
    (
        "opspilot_web",
        FEEDBACK,
        "UPDATE (actual_root_cause,correct_next_steps,next_steps_verdict,notes,"
        "rated_report_sha256,rated_run_id,steering_needed,updated_at,updated_by,"
        "verdict)",
    ),
)

UPGRADE = f"""
CREATE TABLE {FEEDBACK} (
  incident_id uuid PRIMARY KEY REFERENCES opspilot_incidents (incident_id),
  verdict text NOT NULL CHECK (verdict IN ('correct', 'incorrect')),
  actual_root_cause text
    CHECK (actual_root_cause IS NULL OR length(actual_root_cause) BETWEEN 1 AND 8192),
  steering_needed boolean,
  next_steps_verdict text
    CHECK (next_steps_verdict IS NULL
           OR next_steps_verdict IN ('correct', 'incorrect', 'not_applicable')),
  correct_next_steps text
    CHECK (correct_next_steps IS NULL OR length(correct_next_steps) BETWEEN 1 AND 8192),
  notes text CHECK (notes IS NULL OR length(notes) BETWEEN 1 AND 8192),
  rated_run_id uuid NOT NULL REFERENCES opspilot_runs (run_id),
  rated_report_sha256 text CHECK (rated_report_sha256 IS NULL
                                  OR rated_report_sha256 ~ '^[0-9a-f]{{64}}$'),
  updated_by text NOT NULL CHECK (length(updated_by) BETWEEN 1 AND 256),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  -- F5: the actual root cause only with an incorrect verdict, the correct
  -- next steps only with incorrect next steps
  CHECK (actual_root_cause IS NULL OR verdict = 'incorrect'),
  CHECK (correct_next_steps IS NULL OR next_steps_verdict = 'incorrect')
);
CREATE INDEX {FEEDBACK}_updated_idx ON {FEEDBACK} (updated_at);
"""


def upgrade() -> None:
    statements = [UPGRADE]
    statements.extend(
        f"GRANT {privilege} ON {table} TO {role};" for role, table, privilege in GRANTS
    )
    op.execute("\n".join(statements))


def downgrade() -> None:
    bind = op.get_bind()
    op.execute(f"LOCK TABLE {FEEDBACK} IN ACCESS EXCLUSIVE MODE")
    rows = int(bind.execute(text(f"SELECT count(*) FROM {FEEDBACK}")).scalar_one())
    # A count only, never a verdict or a text. psycopg drops server notices
    # unless a handler is registered (0024).
    raw = getattr(bind.connection, "dbapi_connection", None)

    def forward(diagnostic: object) -> None:
        _log.warning("%s", getattr(diagnostic, "message_primary", diagnostic))

    if raw is not None and hasattr(raw, "add_notice_handler"):
        raw.add_notice_handler(forward)
    try:
        op.execute(
            "DO $$ BEGIN RAISE NOTICE "
            f"'investigation feedback dropped by downgrade: rows=%', {rows}; END $$;"
        )
    finally:
        if raw is not None and hasattr(raw, "remove_notice_handler"):
            raw.remove_notice_handler(forward)
    statements = [
        f"REVOKE {privilege} ON {table} FROM {role};"
        for role, table, privilege in reversed(GRANTS)
    ]
    statements.append(f"DROP TABLE {FEEDBACK};")
    op.execute("\n".join(statements))
