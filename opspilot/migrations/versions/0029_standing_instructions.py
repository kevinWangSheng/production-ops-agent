"""0029: standing operator instructions, a kind of reviewed runbook (#780).

Revision ID: 0029_standing_instructions
Revises: 0028_runbook_imports
Create Date: 2026-11-24

User rulings B4-U3=a / B4-U4=a (2026-11-22) and the pre-review defaults
780-1/780-2 (``docs/tasks/2026-11-24-round-prereview-a-eval.md``): standing
instructions reuse the runbook submission, approval, rejection and
revocation of 0028; what differs is that the approved text is given to the
model at the start of every Run of the target instead of on demand.

* ``kind text NOT NULL DEFAULT 'runbook'`` on ``opspilot_runbook_submissions``
  and ``opspilot_runbook_revisions``, CHECK ``kind IN ('runbook',
  'standing_instructions')``. Every existing row is a runbook; a statement
  that names no ``kind`` writes a runbook as before.
* A standing-instructions row has the fixed name ``standing-instructions``
  and at most 4096 bytes of content (CHECK). Since 0028 keeps a name unique
  among the active revisions of one target, a target has at most one active
  standing-instructions revision.
* ``opspilot_runbook_revision_kind_guard``: a revision has its submission's
  kind, and a later revision the kind of the one it supersedes.
* ``opspilot_web`` may insert the new column of both tables (the worker
  keeps its table-wide SELECT on the revisions; the audit table is
  unchanged -- its ``object_kind`` stays submission/entry).

``downgrade()`` never refuses (0024-0028 precedent): it reports the
standing-instructions row counts (``RAISE NOTICE``, counts only), drops the
guard, the grants and the columns. The rows stay, as runbooks named
``standing-instructions`` that ``fetch_skill`` of the older version can read.
"""

import logging
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0029_standing_instructions"
down_revision: str | Sequence[str] | None = "0028_runbook_imports"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_log = logging.getLogger("opspilot.migrations")

SUBMISSIONS = "opspilot_runbook_submissions"
REVISIONS = "opspilot_runbook_revisions"
STANDING_NAME = "standing-instructions"
STANDING_MAX_BYTES = 4096

# Frozen: the ``kind`` part of the runbook entries of ``RUNTIME_GRANTS`` in
# ``opspilot.persistence.grants`` at this revision.
GRANTS: tuple[tuple[str, str, str], ...] = (
    ("opspilot_web", SUBMISSIONS, "INSERT (kind)"),
    ("opspilot_web", REVISIONS, "INSERT (kind)"),
)

_KIND_CHECK = "kind IN ('runbook', 'standing_instructions')"
_STANDING_CHECK = (
    f"kind <> 'standing_instructions' OR (name = '{STANDING_NAME}' "
    f"AND octet_length(content) <= {STANDING_MAX_BYTES})"
)

UPGRADE = f"""
ALTER TABLE {SUBMISSIONS}
  ADD COLUMN kind text NOT NULL DEFAULT 'runbook'
    CONSTRAINT {SUBMISSIONS}_kind_check CHECK ({_KIND_CHECK}),
  ADD CONSTRAINT {SUBMISSIONS}_standing_check CHECK ({_STANDING_CHECK});
ALTER TABLE {REVISIONS}
  ADD COLUMN kind text NOT NULL DEFAULT 'runbook'
    CONSTRAINT {REVISIONS}_kind_check CHECK ({_KIND_CHECK}),
  ADD CONSTRAINT {REVISIONS}_standing_check CHECK ({_STANDING_CHECK});

CREATE FUNCTION opspilot_runbook_revision_kind_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM public.{SUBMISSIONS} s
    WHERE s.submission_id = NEW.submission_id AND s.kind = NEW.kind
  ) THEN
    RAISE check_violation USING MESSAGE = 'a runbook revision has the kind of its submission';
  END IF;
  IF NEW.revision > 1 AND NOT EXISTS (
    SELECT 1 FROM public.{REVISIONS} r
    WHERE r.entry_id = NEW.entry_id AND r.revision = NEW.revision - 1
      AND r.kind = NEW.kind
  ) THEN
    RAISE check_violation USING MESSAGE = 'a runbook revision has the kind of the revision it supersedes';
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER {REVISIONS}_kind_guard BEFORE INSERT ON {REVISIONS}
  FOR EACH ROW EXECUTE FUNCTION opspilot_runbook_revision_kind_guard();
"""


def upgrade() -> None:
    statements = [UPGRADE]
    statements.extend(
        f"GRANT {privilege} ON {table} TO {role};" for role, table, privilege in GRANTS
    )
    op.execute("\n".join(statements))


def downgrade() -> None:
    bind = op.get_bind()
    counts = {
        table: int(
            bind.execute(
                text(
                    f"SELECT count(*) FROM {table} WHERE kind = 'standing_instructions'"
                )
            ).scalar_one()
        )
        for table in (SUBMISSIONS, REVISIONS)
    }
    # Counts only, never a name or a text (0028).
    raw = getattr(bind.connection, "dbapi_connection", None)

    def forward(diagnostic: object) -> None:
        _log.warning("%s", getattr(diagnostic, "message_primary", diagnostic))

    if raw is not None and hasattr(raw, "add_notice_handler"):
        raw.add_notice_handler(forward)
    try:
        op.execute(
            "DO $$ BEGIN RAISE NOTICE "
            "'standing instructions become runbooks on downgrade: "
            "submissions=%, revisions=%', "
            f"{counts[SUBMISSIONS]}, {counts[REVISIONS]}; END $$;"
        )
    finally:
        if raw is not None and hasattr(raw, "remove_notice_handler"):
            raw.remove_notice_handler(forward)
    statements = [
        f"REVOKE {privilege} ON {table} FROM {role};"
        for role, table, privilege in reversed(GRANTS)
    ]
    statements.extend(
        [
            f"DROP TRIGGER {REVISIONS}_kind_guard ON {REVISIONS};",
            "DROP FUNCTION opspilot_runbook_revision_kind_guard();",
            f"ALTER TABLE {REVISIONS} DROP CONSTRAINT {REVISIONS}_standing_check, "
            "DROP COLUMN kind;",
            f"ALTER TABLE {SUBMISSIONS} DROP CONSTRAINT {SUBMISSIONS}_standing_check, "
            "DROP COLUMN kind;",
        ]
    )
    op.execute("\n".join(statements))
