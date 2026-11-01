"""0024 trace outbox: the product-mode trace outbox, write side (#243 PR-1, #427).

Revision ID: 0024_trace_outbox
Revises: 0023_retention_raw_cleanup
Create Date: 2026-10-31

C3 section 11 ("the business transaction and the outbox commit together"),
contract r3 of ``docs/tasks/2026-10-27-m3-f8-operability-contract.md``
(OB-R1, R2, R3, R11, Q14, Q16, Q18). Nothing here delivers or leaves the
host; delivery is PR-2.

* ``opspilot_trace_outbox`` (OB-R1): one row per model step, tool result,
  postmortem generation attempt and Run state change. ``payload`` holds only
  the Q14 allowlist keys (built by ``opspilot.persistence.outbox
  .build_payload`` for the application rows, by the trigger below for Run
  state rows), at most 4096 bytes. ``idempotency_key`` is unique and every
  writer inserts ``ON CONFLICT DO NOTHING``.
* Run state rows come from an ``AFTER UPDATE OF state`` trigger on
  ``opspilot_runs`` that fires only when the state really changes, so the
  web's human controls and deadline sweep and the worker's writes are all
  covered. The function is not ``SECURITY DEFINER`` (0011 convention): it
  runs as the updating role, so both runtime roles hold a column-level
  INSERT on the outbox. The web has no SELECT there, so the ordinal in
  ``run_state:<run_id>:<n>`` is found by probing n = 1, 2, ... with a bare
  ``ON CONFLICT DO NOTHING`` until one row lands. Bare, because naming the
  conflict target ``(idempotency_key)`` needs SELECT on that column
  (measured: 42501 for the web); the only other unique key is the identity
  ``entry_id``, so the two forms are equivalent here. The updater holds the
  Run's row lock, so two changes of one Run never race.
  A Run's initial state at INSERT writes no row; Runs from before this
  revision are not backfilled.
* Grants: web INSERT of the five writer columns; worker SELECT, the same
  INSERT, ``UPDATE(attempts,drop_reason,next_attempt_at,state)`` and DELETE
  (the 7-day cleanup, Q16); Observer and retention nothing.

``downgrade()`` never refuses (Q18): it reports the pending / delivered /
dropped row counts (``RAISE NOTICE``, counts only, forwarded to the
``opspilot.migrations`` logger), then drops the trigger and its function,
revokes the grants and drops the table, unexported rows included. No
business table row changes.
"""

import logging
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0024_trace_outbox"
down_revision: str | Sequence[str] | None = "0023_retention_raw_cleanup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_log = logging.getLogger("opspilot.migrations")

TABLE = "opspilot_trace_outbox"

# Frozen: equal to the ``opspilot_trace_outbox`` entries of
# ``RUNTIME_GRANTS`` in ``opspilot.persistence.grants`` at this revision.
WRITER_COLUMNS = "idempotency_key,incident_id,kind,payload,run_id"
GRANTS: tuple[tuple[str, str], ...] = (
    ("opspilot_web", f"INSERT ({WRITER_COLUMNS})"),
    ("opspilot_worker", "SELECT"),
    ("opspilot_worker", f"INSERT ({WRITER_COLUMNS})"),
    ("opspilot_worker", "UPDATE (attempts,drop_reason,next_attempt_at,state)"),
    ("opspilot_worker", "DELETE"),
)

# Frozen copy of ``outbox.IDENTIFIER`` (a version / revision value).
IDENTIFIER = "^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"

UPGRADE = f"""
CREATE TABLE {TABLE} (
  entry_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  idempotency_key text NOT NULL UNIQUE CHECK (octet_length(idempotency_key) <= 256),
  incident_id uuid NOT NULL,
  run_id uuid,
  kind text NOT NULL CHECK (kind IN ('run_state', 'model_step', 'tool_result', 'generation')),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object' AND octet_length(payload::text) <= 4096),
  state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'delivered', 'dropped')),
  attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  next_attempt_at timestamptz,
  drop_reason text CHECK (drop_reason IS NULL OR drop_reason ~ '^[A-Z][A-Z0-9_]{{1,63}}$'),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK ((kind = 'generation') = (run_id IS NULL))
);
CREATE INDEX {TABLE}_state_next_attempt_idx ON {TABLE} (state, next_attempt_at);
CREATE INDEX {TABLE}_created_at_idx ON {TABLE} (created_at);

CREATE FUNCTION opspilot_trace_outbox_run_state() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  -- the Q14 allowlist, built from typed columns only; never NEW.input
  body jsonb := pg_catalog.jsonb_strip_nulls(pg_catalog.jsonb_build_object(
    'incident_id', NEW.incident_id::text,
    'run_id', NEW.run_id::text,
    'span_kind', 'run_state',
    'run_state', NEW.state,
    'ended_at', pg_catalog.to_char(pg_catalog.clock_timestamp() AT TIME ZONE 'UTC',
                                   'YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"'),
    'prompt_revision', CASE WHEN NEW.versions ->> 'prompt_revision' IS NULL THEN NULL
      WHEN NEW.versions ->> 'prompt_revision' ~ '{IDENTIFIER}' THEN NEW.versions ->> 'prompt_revision'
      ELSE 'UNCLASSIFIED' END,
    'tool_schema_revision', CASE WHEN NEW.versions ->> 'tool_schema_revision' IS NULL THEN NULL
      WHEN NEW.versions ->> 'tool_schema_revision' ~ '{IDENTIFIER}' THEN NEW.versions ->> 'tool_schema_revision'
      ELSE 'UNCLASSIFIED' END,
    'context_policy_revision', CASE WHEN NEW.versions ->> 'context_policy_revision' IS NULL THEN NULL
      WHEN NEW.versions ->> 'context_policy_revision' ~ '{IDENTIFIER}' THEN NEW.versions ->> 'context_policy_revision'
      ELSE 'UNCLASSIFIED' END
  ));
  n integer := 0;
  landed integer;
BEGIN
  LOOP
    n := n + 1;
    INSERT INTO public.{TABLE} (idempotency_key, incident_id, run_id, kind, payload)
    VALUES ('run_state:' || NEW.run_id::text || ':' || n::text, NEW.incident_id, NEW.run_id,
            'run_state', body)
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS landed = ROW_COUNT;
    EXIT WHEN landed > 0;
  END LOOP;
  RETURN NULL;
END
$$;
CREATE TRIGGER opspilot_runs_trace_outbox
  AFTER UPDATE OF state ON opspilot_runs
  FOR EACH ROW WHEN (OLD.state IS DISTINCT FROM NEW.state)
  EXECUTE FUNCTION opspilot_trace_outbox_run_state();
"""


def upgrade() -> None:
    statements = [UPGRADE]
    statements.extend(
        f"GRANT {privilege} ON {TABLE} TO {role};" for role, privilege in GRANTS
    )
    op.execute("\n".join(statements))


def downgrade() -> None:
    bind = op.get_bind()
    op.execute(f"LOCK TABLE {TABLE} IN ACCESS EXCLUSIVE MODE")
    counts = (
        bind.execute(
            text(
                f"SELECT count(*) FILTER (WHERE state='pending') AS pending, "
                f"count(*) FILTER (WHERE state='delivered') AS delivered, "
                f"count(*) FILTER (WHERE state='dropped') AS dropped FROM {TABLE}"
            )
        )
        .mappings()
        .one()
    )
    pending, delivered, dropped = (
        int(counts["pending"]),
        int(counts["delivered"]),
        int(counts["dropped"]),
    )
    # Counts only, never a payload (Q18). psycopg drops server notices unless
    # a handler is registered, so forward them to a logger the operator sees.
    raw = getattr(bind.connection, "dbapi_connection", None)

    def forward(diagnostic: object) -> None:
        _log.warning("%s", getattr(diagnostic, "message_primary", diagnostic))

    if raw is not None and hasattr(raw, "add_notice_handler"):
        raw.add_notice_handler(forward)
    try:
        op.execute(
            "DO $$ BEGIN RAISE NOTICE "
            f"'{TABLE} dropped by downgrade: pending=%, delivered=%, dropped=%', "
            f"{pending}, {delivered}, {dropped}; END $$;"
        )
    finally:
        if raw is not None and hasattr(raw, "remove_notice_handler"):
            raw.remove_notice_handler(forward)
    statements = [
        "DROP TRIGGER opspilot_runs_trace_outbox ON opspilot_runs;",
        "DROP FUNCTION opspilot_trace_outbox_run_state();",
    ]
    statements.extend(
        f"REVOKE {privilege} ON {TABLE} FROM {role};"
        for role, privilege in reversed(GRANTS)
    )
    statements.append(f"DROP TABLE {TABLE};")
    op.execute("\n".join(statements))
