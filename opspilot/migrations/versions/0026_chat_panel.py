"""0026: the incident page's Chat panel (workbench redesign P7, contract E1 r10).

Revision ID: 0026_chat_panel
Revises: 0025_web_session_rebind
Create Date: 2026-11-18

User approved the grants (U-P7, option A, 2026-11-18). Two new tables;
no existing table or grant changes.

* ``opspilot_chat_sessions``: one Chat conversation on one incident
  (``incident_id NOT NULL``, P7-3), who started it and its title (the first
  question, cut to 200 characters).
* ``opspilot_chat_turns``: one row per exchange (P7-2). The web inserts the
  question, its author, the idempotency key and the snapshot of the record
  the model will read (``context`` plus its sha256, P7-4); the worker writes
  the answer, the ``steer`` flag, the cited evidence ids, token counts and
  its own claim/attempt columns; the web then writes the forward result
  (P7-9). Column-level grants keep each writer to its own columns, so every
  answer shown was written by the worker, and the worker cannot touch the
  question or the forward. ``seq`` is an identity column (no sequence
  grant). One ``pending`` turn per session (partial unique index,
  ``CHAT_BUSY``); ``(session_id, idempotency_key)`` is unique.
  ``forward_state`` can only be set on a turn the worker marked ``steer``.

No role can DELETE. Retention and Observer roles get nothing (retention's
matrix is ``NONE`` for every web relation; the Observer's is an allowlist).

``downgrade()`` never refuses (0024 precedent): it reports the session and
turn counts (``RAISE NOTICE``, counts only, forwarded to the
``opspilot.migrations`` logger), revokes the grants and drops both tables
-- all chat history is lost. No other table changes.
"""

import logging
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0026_chat_panel"
down_revision: str | Sequence[str] | None = "0025_web_session_rebind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_log = logging.getLogger("opspilot.migrations")

SESSIONS = "opspilot_chat_sessions"
TURNS = "opspilot_chat_turns"

# Frozen: equal to the chat entries of ``RUNTIME_GRANTS`` in
# ``opspilot.persistence.grants`` at this revision (U-P7).
GRANTS: tuple[tuple[str, str, str], ...] = (
    ("opspilot_web", SESSIONS, "SELECT"),
    ("opspilot_web", SESSIONS, "INSERT (created_by,incident_id,session_id,title)"),
    ("opspilot_web", TURNS, "SELECT"),
    (
        "opspilot_web",
        TURNS,
        "INSERT (author,context,context_sha256,idempotency_key,question,session_id,"
        "turn_id)",
    ),
    (
        "opspilot_web",
        TURNS,
        "UPDATE (forward_code,forward_state,forwarded_at,forwarded_input_sequence)",
    ),
    ("opspilot_worker", SESSIONS, "SELECT"),
    ("opspilot_worker", TURNS, "SELECT"),
    (
        "opspilot_worker",
        TURNS,
        "UPDATE (answer,answered_at,attempts,claim_owner,claim_until,"
        "completion_tokens,error_code,prompt_tokens,refs,status,steer)",
    ),
)

_CODE = "'^[A-Z][A-Z0-9_]{1,63}$'"

UPGRADE = f"""
CREATE TABLE {SESSIONS} (
  session_id uuid PRIMARY KEY,
  incident_id uuid NOT NULL REFERENCES opspilot_incidents (incident_id),
  created_by text NOT NULL CHECK (length(created_by) BETWEEN 1 AND 256),
  title text NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX {SESSIONS}_incident_idx ON {SESSIONS} (incident_id, created_at DESC);

CREATE TABLE {TURNS} (
  turn_id uuid PRIMARY KEY,
  session_id uuid NOT NULL REFERENCES {SESSIONS} (session_id),
  seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
  author text NOT NULL CHECK (length(author) BETWEEN 1 AND 256),
  question text NOT NULL CHECK (length(question) BETWEEN 1 AND 20000),
  idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 256),
  context jsonb NOT NULL CHECK (jsonb_typeof(context) = 'object')
    CHECK (octet_length(context::text) <= 262144),
  context_sha256 text NOT NULL CHECK (context_sha256 ~ '^[0-9a-f]{{64}}$'),
  asked_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'answered', 'failed')),
  answer text CHECK (answer IS NULL OR length(answer) BETWEEN 1 AND 4000),
  steer boolean NOT NULL DEFAULT false,
  refs jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(refs) = 'array'),
  error_code text CHECK (error_code IS NULL OR error_code ~ {_CODE}),
  prompt_tokens integer CHECK (prompt_tokens IS NULL OR prompt_tokens >= 0),
  completion_tokens integer CHECK (completion_tokens IS NULL OR completion_tokens >= 0),
  answered_at timestamptz,
  attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  claim_owner uuid,
  claim_until timestamptz,
  forward_state text CHECK (forward_state IS NULL OR forward_state IN ('sent', 'refused')),
  forward_code text CHECK (forward_code IS NULL OR forward_code ~ {_CODE}),
  forwarded_at timestamptz,
  forwarded_input_sequence bigint,
  UNIQUE (session_id, idempotency_key),
  CHECK ((status = 'answered') = (answer IS NOT NULL)),
  CHECK ((status = 'failed') = (error_code IS NOT NULL)),
  CHECK ((status = 'pending') = (answered_at IS NULL)),
  -- only the worker's steer flag opens a forward; a sent forward names its input
  CHECK (forward_state IS NULL OR (steer AND status = 'answered')),
  CHECK ((forward_state = 'sent') = (forwarded_input_sequence IS NOT NULL)),
  CHECK ((forward_state IS NULL) = (forwarded_at IS NULL)),
  CHECK (forward_state IS DISTINCT FROM 'refused' OR forward_code IS NOT NULL)
);
CREATE UNIQUE INDEX {TURNS}_one_pending_idx ON {TURNS} (session_id)
  WHERE status = 'pending';
CREATE INDEX {TURNS}_pending_idx ON {TURNS} (seq) WHERE status = 'pending';
CREATE INDEX {TURNS}_author_asked_idx ON {TURNS} (author, asked_at);
CREATE INDEX {TURNS}_forward_idx ON {TURNS} (seq)
  WHERE steer AND forward_state IS NULL;
"""


def upgrade() -> None:
    statements = [UPGRADE]
    statements.extend(
        f"GRANT {privilege} ON {table} TO {role};" for role, table, privilege in GRANTS
    )
    op.execute("\n".join(statements))


def downgrade() -> None:
    bind = op.get_bind()
    op.execute(f"LOCK TABLE {SESSIONS}, {TURNS} IN ACCESS EXCLUSIVE MODE")
    counts = (
        bind.execute(
            text(
                f"SELECT (SELECT count(*) FROM {SESSIONS}) AS sessions, "
                f"(SELECT count(*) FROM {TURNS}) AS turns"
            )
        )
        .mappings()
        .one()
    )
    sessions, turns = int(counts["sessions"]), int(counts["turns"])
    # Counts only, never a question or an answer. psycopg drops server
    # notices unless a handler is registered (0024).
    raw = getattr(bind.connection, "dbapi_connection", None)

    def forward(diagnostic: object) -> None:
        _log.warning("%s", getattr(diagnostic, "message_primary", diagnostic))

    if raw is not None and hasattr(raw, "add_notice_handler"):
        raw.add_notice_handler(forward)
    try:
        op.execute(
            "DO $$ BEGIN RAISE NOTICE "
            "'chat history dropped by downgrade: sessions=%, turns=%', "
            f"{sessions}, {turns}; END $$;"
        )
    finally:
        if raw is not None and hasattr(raw, "remove_notice_handler"):
            raw.remove_notice_handler(forward)
    statements = [
        f"REVOKE {privilege} ON {table} FROM {role};"
        for role, table, privilege in reversed(GRANTS)
    ]
    statements.append(f"DROP TABLE {TURNS};")
    statements.append(f"DROP TABLE {SESSIONS};")
    op.execute("\n".join(statements))
