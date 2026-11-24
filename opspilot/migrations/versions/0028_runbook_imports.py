"""0028: imported runbooks, a second reviewed knowledge source (C2a, #566).

Revision ID: 0028_runbook_imports
Revises: 0027_investigation_feedback
Create Date: 2026-11-19

User ruling r11 C2-U1=a (2026-11-19): a signed-in workbench user submits a
runbook (name, description, Markdown content, source label and reference)
for one target; a signed-in user approves or rejects it, and may later
revoke the approved revision. Only an approved, unrevoked, unsuperseded
revision is retrievable. Interface: ``docs/tasks/2026-11-19-tool-expansion-c2.md``
section 1-3.

Kept entirely apart from the postmortem knowledge of 0008: no 0008 table,
constraint, trigger or grant changes, and the F13 audit and request tables
are not reused (task record, implementation decision 1).

* ``opspilot_runbook_requests`` / ``opspilot_runbook_audit`` -- one row per
  idempotency key (fingerprint and result, for replay) and one audit row
  per object a request changed. Only ``basic_auth`` principals; there is no
  worker action.
* ``opspilot_runbook_submissions`` -- one immutable submission; pending
  while it has no decision.
* ``opspilot_runbook_decisions`` -- the one decision (approved/rejected) on
  a submission; the primary key makes it single.
* ``opspilot_runbook_entries`` -- a runbook's identity, its generation and
  latest revision number (the only rows ever updated).
* ``opspilot_runbook_revisions`` -- immutable approved revisions, equal
  column for column to the submission they came from.
* ``opspilot_runbook_revocations`` -- the tombstone of a revoked revision.
* view ``opspilot_runbook_revision_states`` -- ``active | superseded |
  revoked``.

Every table refuses DELETE and TRUNCATE for every role, and every table but
the entries refuses UPDATE. Every row is written in the same transaction as
its audit row (``xmin`` = the current transaction, the 0003/0008 test). A
name is unique among the active revisions of one ``resource_uid``; the
revision guard checks it under a transaction-level advisory lock on the
``resource_uid``. As in 0008 these guard against code and SQL errors, not a
compromised owner credential.

Grants (``opspilot.persistence.grants``): the web reads everything and
inserts the columns its statements name; the worker reads only the
revisions and the state view (what ``fetch_skill`` needs, C2b); the
Observer and the retention role get nothing.

``downgrade()`` never refuses (0024/0026/0027 precedent): it reports the
row counts (``RAISE NOTICE``, counts only), revokes the grants and drops
the tables -- every imported runbook is lost. No other table changes.
"""

import logging
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0028_runbook_imports"
down_revision: str | Sequence[str] | None = "0027_investigation_feedback"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_log = logging.getLogger("opspilot.migrations")

REQUESTS = "opspilot_runbook_requests"
AUDIT = "opspilot_runbook_audit"
SUBMISSIONS = "opspilot_runbook_submissions"
DECISIONS = "opspilot_runbook_decisions"
ENTRIES = "opspilot_runbook_entries"
REVISIONS = "opspilot_runbook_revisions"
REVOCATIONS = "opspilot_runbook_revocations"
STATES = "opspilot_runbook_revision_states"

TABLES = (REQUESTS, AUDIT, SUBMISSIONS, DECISIONS, ENTRIES, REVISIONS, REVOCATIONS)
FROZEN_TABLES = (REQUESTS, AUDIT, SUBMISSIONS, DECISIONS, REVISIONS, REVOCATIONS)

# Frozen: equal to the runbook entries of ``RUNTIME_GRANTS`` in
# ``opspilot.persistence.grants`` at this revision.
GRANTS: tuple[tuple[str, str, str], ...] = (
    *(("opspilot_web", relation, "SELECT") for relation in (*TABLES, STATES)),
    (
        "opspilot_web",
        REQUESTS,
        "INSERT (action,idempotency_key,request_sha256,result)",
    ),
    (
        "opspilot_web",
        AUDIT,
        "INSERT (action,actor_id,event_id,expected_generation,idempotency_key,"
        "object_id,object_kind,principal_kind,reason,resulting_generation,revision)",
    ),
    (
        "opspilot_web",
        SUBMISSIONS,
        "INSERT (content,content_sha256,description,event_id,name,resource_uid,"
        "source_label,source_ref,submission_id,submitted_by,supersedes_entry_id)",
    ),
    (
        "opspilot_web",
        DECISIONS,
        "INSERT (decided_by,decision,event_id,reason,submission_id)",
    ),
    ("opspilot_web", ENTRIES, "INSERT (entry_id,resource_uid)"),
    ("opspilot_web", ENTRIES, "UPDATE (generation,latest_revision,updated_at)"),
    (
        "opspilot_web",
        REVISIONS,
        "INSERT (approved_by,content,content_sha256,description,entry_id,event_id,"
        "name,resource_uid,revision,source_label,source_ref,submission_id,"
        "supersedes_revision)",
    ),
    (
        "opspilot_web",
        REVOCATIONS,
        "INSERT (entry_id,event_id,reason,revision,revoked_by)",
    ),
    ("opspilot_worker", REVISIONS, "SELECT"),
    ("opspilot_worker", STATES, "SELECT"),
)

_NAME_CHECK = "name ~ '^[a-z0-9]+(-[a-z0-9]+)*$' AND length(name) <= 100"
_HASH_CHECK = "content_sha256 = encode(sha256(convert_to(content, 'UTF8')), 'hex')"
_BODY_COLUMNS = f"""
  resource_uid text NOT NULL CHECK (resource_uid <> '' AND length(resource_uid) <= 512),
  name text NOT NULL CHECK ({_NAME_CHECK}),
  description text NOT NULL CHECK (length(description) BETWEEN 1 AND 300),
  content text NOT NULL CHECK (octet_length(content) BETWEEN 1 AND 32768),
  content_sha256 text NOT NULL CHECK ({_HASH_CHECK}),
  source_label text NOT NULL CHECK (length(source_label) BETWEEN 1 AND 100),
  source_ref text NOT NULL CHECK (length(source_ref) <= 500),"""

UPGRADE = f"""
CREATE TABLE {REQUESTS} (
  idempotency_key text PRIMARY KEY CHECK (idempotency_key <> ''),
  action text NOT NULL CHECK (action IN ('submit', 'approve', 'reject', 'revoke')),
  request_sha256 text NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{{64}}$'),
  result jsonb NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE {AUDIT} (
  event_id uuid PRIMARY KEY,
  -- deferred: the request row carries the result and is written last
  idempotency_key text NOT NULL REFERENCES {REQUESTS} DEFERRABLE INITIALLY DEFERRED,
  object_kind text NOT NULL CHECK (object_kind IN ('runbook_submission', 'runbook_entry')),
  object_id uuid NOT NULL,
  action text NOT NULL CHECK (action IN ('submit', 'approve', 'reject', 'revoke')),
  revision integer CHECK (revision IS NULL OR revision > 0),
  actor_id text NOT NULL CHECK (actor_id <> ''),
  principal_kind text NOT NULL CHECK (principal_kind = 'basic_auth'),
  reason text,
  expected_generation integer NOT NULL CHECK (expected_generation >= 0),
  resulting_generation integer NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT {AUDIT}_generation_check CHECK (resulting_generation = expected_generation + 1),
  CONSTRAINT {AUDIT}_reason_check CHECK (
    action NOT IN ('reject', 'revoke') OR (reason IS NOT NULL AND reason <> '')
  ),
  CONSTRAINT {AUDIT}_kind_action_check CHECK (
    (object_kind = 'runbook_submission' AND action IN ('submit', 'approve', 'reject'))
    OR (object_kind = 'runbook_entry' AND action IN ('approve', 'revoke'))
  ),
  UNIQUE (idempotency_key, object_kind, object_id, action),
  CONSTRAINT {AUDIT}_generation_key UNIQUE (object_kind, object_id, resulting_generation)
);
CREATE INDEX {AUDIT}_object_idx ON {AUDIT}(object_kind, object_id, recorded_at);
CREATE TABLE {ENTRIES} (
  entry_id uuid PRIMARY KEY,
  resource_uid text NOT NULL CHECK (resource_uid <> '' AND length(resource_uid) <= 512),
  generation integer NOT NULL DEFAULT 0 CHECK (generation >= 0),
  latest_revision integer NOT NULL DEFAULT 0 CHECK (latest_revision >= 0),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX {ENTRIES}_target_idx ON {ENTRIES}(resource_uid);
CREATE TABLE {SUBMISSIONS} (
  submission_id uuid PRIMARY KEY,{_BODY_COLUMNS}
  -- the entry this submission would supersede (NULL: a new entry)
  supersedes_entry_id uuid REFERENCES {ENTRIES},
  submitted_by text NOT NULL CHECK (submitted_by <> ''),
  event_id uuid NOT NULL REFERENCES {AUDIT},
  submitted_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX {SUBMISSIONS}_target_idx ON {SUBMISSIONS}(resource_uid, submitted_at);
CREATE TABLE {DECISIONS} (
  submission_id uuid PRIMARY KEY REFERENCES {SUBMISSIONS},
  decision text NOT NULL CHECK (decision IN ('approved', 'rejected')),
  reason text,
  decided_by text NOT NULL CHECK (decided_by <> ''),
  event_id uuid NOT NULL REFERENCES {AUDIT},
  decided_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT {DECISIONS}_reason_check CHECK (
    decision <> 'rejected' OR (reason IS NOT NULL AND reason <> '')
  )
);
CREATE TABLE {REVISIONS} (
  entry_id uuid NOT NULL REFERENCES {ENTRIES},
  revision integer NOT NULL CHECK (revision > 0),
  submission_id uuid NOT NULL UNIQUE REFERENCES {DECISIONS},{_BODY_COLUMNS}
  supersedes_revision integer,
  approved_by text NOT NULL CHECK (approved_by <> ''),
  event_id uuid NOT NULL REFERENCES {AUDIT},
  approved_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (entry_id, revision),
  FOREIGN KEY (entry_id, supersedes_revision) REFERENCES {REVISIONS},
  CONSTRAINT {REVISIONS}_supersedes_check CHECK (
    (revision = 1 AND supersedes_revision IS NULL)
    OR (revision > 1 AND supersedes_revision = revision - 1)
  )
);
CREATE INDEX {REVISIONS}_target_idx ON {REVISIONS}(resource_uid, name);
CREATE TABLE {REVOCATIONS} (
  entry_id uuid NOT NULL,
  revision integer NOT NULL,
  reason text NOT NULL CHECK (reason <> ''),
  revoked_by text NOT NULL CHECK (revoked_by <> ''),
  event_id uuid NOT NULL REFERENCES {AUDIT},
  revoked_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (entry_id, revision),
  FOREIGN KEY (entry_id, revision) REFERENCES {REVISIONS}
);
CREATE VIEW {STATES} AS
  SELECT r.entry_id, r.revision,
         CASE
           WHEN x.entry_id IS NOT NULL THEN 'revoked'
           WHEN s.entry_id IS NOT NULL THEN 'superseded'
           ELSE 'active'
         END AS state
  FROM {REVISIONS} r
  LEFT JOIN {REVOCATIONS} x ON x.entry_id = r.entry_id AND x.revision = r.revision
  LEFT JOIN {REVISIONS} s ON s.entry_id = r.entry_id AND s.supersedes_revision = r.revision;
"""

# Each guard names what it binds a row to; every check of "this
# transaction wrote the audit row" uses ``xmin`` like 0008.
GUARDS = f"""
CREATE FUNCTION opspilot_runbook_refuse() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  RAISE insufficient_privilege USING MESSAGE =
    pg_catalog.format('%s is append-only: %s refused', TG_TABLE_NAME, TG_OP);
END
$$;

-- The audit row of this transaction for one object and action.
CREATE FUNCTION opspilot_runbook_audited(
  p_event uuid, p_kind text, p_object uuid, p_action text, p_actor text,
  p_revision integer, p_reason text, p_expected integer
) RETURNS boolean
LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $$
  SELECT EXISTS (
    SELECT 1 FROM public.{AUDIT} a
    WHERE a.event_id = p_event AND a.object_kind = p_kind AND a.object_id = p_object
      AND a.action = p_action AND a.actor_id = p_actor
      AND a.principal_kind = 'basic_auth'
      AND a.revision IS NOT DISTINCT FROM p_revision
      AND (p_reason IS NULL OR a.reason = p_reason)
      AND (p_expected IS NULL OR a.expected_generation = p_expected)
      AND a.xmin = pg_catalog.pg_current_xact_id()::xid
  )
$$;

-- Entries: start at generation 0 and revision 0; afterwards the generation
-- moves by exactly one with an audit row of this transaction carrying the
-- resulting generation, the latest revision never goes back, identity is
-- fixed.
CREATE FUNCTION opspilot_runbook_entry_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.generation <> 0 OR NEW.latest_revision <> 0 THEN
      RAISE check_violation USING MESSAGE = 'a new runbook entry starts at generation 0 and revision 0';
    END IF;
    RETURN NEW;
  END IF;
  IF NEW.entry_id IS DISTINCT FROM OLD.entry_id
     OR NEW.resource_uid IS DISTINCT FROM OLD.resource_uid
     OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE insufficient_privilege USING MESSAGE = 'runbook entry identity columns are immutable';
  END IF;
  IF NEW.generation <> OLD.generation + 1 THEN
    RAISE check_violation USING MESSAGE =
      pg_catalog.format('runbook entry generation must advance by one (%s -> %s)', OLD.generation, NEW.generation);
  END IF;
  IF NEW.latest_revision < OLD.latest_revision
     OR NEW.latest_revision > OLD.latest_revision + 1 THEN
    RAISE check_violation USING MESSAGE = 'runbook entry latest revision moves by zero or one';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM public.{AUDIT} a
    WHERE a.object_kind = 'runbook_entry' AND a.object_id = NEW.entry_id
      AND a.expected_generation = OLD.generation
      AND a.resulting_generation = NEW.generation
      AND a.xmin = pg_catalog.pg_current_xact_id()::xid
  ) THEN
    RAISE insufficient_privilege USING MESSAGE =
      pg_catalog.format('runbook entry generation %s -> %s without an audit row', OLD.generation, NEW.generation);
  END IF;
  RETURN NEW;
END
$$;

CREATE FUNCTION opspilot_runbook_submission_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  IF NOT public.opspilot_runbook_audited(
    NEW.event_id, 'runbook_submission', NEW.submission_id, 'submit',
    NEW.submitted_by, NULL, NULL, 0
  ) THEN
    RAISE insufficient_privilege USING MESSAGE = 'a runbook submission needs its submit audit row';
  END IF;
  IF NEW.supersedes_entry_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM public.{ENTRIES} e
    WHERE e.entry_id = NEW.supersedes_entry_id AND e.resource_uid = NEW.resource_uid
  ) THEN
    RAISE check_violation USING MESSAGE = 'a runbook supersedes only an entry of the same target';
  END IF;
  RETURN NEW;
END
$$;

CREATE FUNCTION opspilot_runbook_decision_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  IF NOT public.opspilot_runbook_audited(
    NEW.event_id, 'runbook_submission', NEW.submission_id,
    CASE NEW.decision WHEN 'approved' THEN 'approve' ELSE 'reject' END,
    NEW.decided_by, NULL, NEW.reason, 1
  ) THEN
    RAISE insufficient_privilege USING MESSAGE = 'a runbook decision needs its approve/reject audit row';
  END IF;
  RETURN NEW;
END
$$;

-- Revisions: from a submission approved in this transaction, equal to it
-- column for column, at the entry's latest revision number, under an
-- approve audit row of the entry; a new entry starts at revision 1 for a
-- submission that supersedes nothing, a later revision supersedes the
-- previous one, which must still be active; the name is free among the
-- other active revisions of the target.
CREATE FUNCTION opspilot_runbook_revision_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  s public.{SUBMISSIONS}%ROWTYPE;
BEGIN
  SELECT * INTO s FROM public.{SUBMISSIONS} WHERE submission_id = NEW.submission_id;
  IF NOT EXISTS (
    SELECT 1 FROM public.{DECISIONS} d
    WHERE d.submission_id = NEW.submission_id AND d.decision = 'approved'
      AND d.decided_by = NEW.approved_by
      AND d.xmin = pg_catalog.pg_current_xact_id()::xid
  ) THEN
    RAISE check_violation USING MESSAGE = 'a runbook revision comes only from a submission approved in this transaction';
  END IF;
  IF s.resource_uid IS DISTINCT FROM NEW.resource_uid OR s.name IS DISTINCT FROM NEW.name
     OR s.description IS DISTINCT FROM NEW.description
     OR s.content_sha256 IS DISTINCT FROM NEW.content_sha256
     OR s.source_label IS DISTINCT FROM NEW.source_label
     OR s.source_ref IS DISTINCT FROM NEW.source_ref THEN
    RAISE check_violation USING MESSAGE = 'a runbook revision must equal its approved submission';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM public.{ENTRIES} e
    WHERE e.entry_id = NEW.entry_id AND e.latest_revision = NEW.revision
      AND e.resource_uid = NEW.resource_uid
  ) THEN
    RAISE check_violation USING MESSAGE = 'a runbook revision must be the latest revision of an entry of its target';
  END IF;
  IF (NEW.revision = 1) <> (s.supersedes_entry_id IS NULL)
     OR (NEW.revision > 1 AND s.supersedes_entry_id <> NEW.entry_id) THEN
    RAISE check_violation USING MESSAGE = 'a runbook revision must match what its submission supersedes';
  END IF;
  IF NOT public.opspilot_runbook_audited(
    NEW.event_id, 'runbook_entry', NEW.entry_id, 'approve', NEW.approved_by,
    NEW.revision, NULL, NULL
  ) THEN
    RAISE insufficient_privilege USING MESSAGE = 'a runbook revision needs its approve audit row';
  END IF;
  IF NEW.revision > 1 AND NOT EXISTS (
    SELECT 1 FROM public.{STATES} st
    WHERE st.entry_id = NEW.entry_id AND st.revision = NEW.revision - 1 AND st.state = 'active'
  ) THEN
    RAISE check_violation USING MESSAGE = 'only an active runbook revision can be superseded';
  END IF;
  PERFORM pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended('opspilot_runbook_target:' || NEW.resource_uid, 0)
  );
  IF EXISTS (
    SELECT 1 FROM public.{REVISIONS} r
    JOIN public.{STATES} st USING (entry_id, revision)
    WHERE r.resource_uid = NEW.resource_uid AND r.name = NEW.name
      AND r.entry_id <> NEW.entry_id AND st.state = 'active'
  ) THEN
    RAISE unique_violation USING MESSAGE = 'an active runbook of this target already has this name',
      CONSTRAINT = 'opspilot_runbook_active_name';
  END IF;
  RETURN NEW;
END
$$;

CREATE FUNCTION opspilot_runbook_revocation_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM public.{STATES} st
    WHERE st.entry_id = NEW.entry_id AND st.revision = NEW.revision AND st.state = 'active'
  ) THEN
    RAISE check_violation USING MESSAGE = 'only an active runbook revision can be revoked';
  END IF;
  IF NOT public.opspilot_runbook_audited(
    NEW.event_id, 'runbook_entry', NEW.entry_id, 'revoke', NEW.revoked_by,
    NEW.revision, NEW.reason, NULL
  ) THEN
    RAISE insufficient_privilege USING MESSAGE = 'a runbook revocation needs its revoke audit row';
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER {ENTRIES}_guard BEFORE INSERT OR UPDATE ON {ENTRIES}
  FOR EACH ROW EXECUTE FUNCTION opspilot_runbook_entry_guard();
CREATE TRIGGER {SUBMISSIONS}_guard BEFORE INSERT ON {SUBMISSIONS}
  FOR EACH ROW EXECUTE FUNCTION opspilot_runbook_submission_guard();
CREATE TRIGGER {DECISIONS}_guard BEFORE INSERT ON {DECISIONS}
  FOR EACH ROW EXECUTE FUNCTION opspilot_runbook_decision_guard();
CREATE TRIGGER {REVISIONS}_guard BEFORE INSERT ON {REVISIONS}
  FOR EACH ROW EXECUTE FUNCTION opspilot_runbook_revision_guard();
CREATE TRIGGER {REVOCATIONS}_guard BEFORE INSERT ON {REVOCATIONS}
  FOR EACH ROW EXECUTE FUNCTION opspilot_runbook_revocation_guard();
"""

FUNCTIONS = (
    "opspilot_runbook_revocation_guard()",
    "opspilot_runbook_revision_guard()",
    "opspilot_runbook_decision_guard()",
    "opspilot_runbook_submission_guard()",
    "opspilot_runbook_entry_guard()",
    "opspilot_runbook_audited(uuid, text, uuid, text, text, integer, text, integer)",
    "opspilot_runbook_refuse()",
)


def upgrade() -> None:
    statements = [UPGRADE, GUARDS]
    for table in TABLES:
        statements.append(
            f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION opspilot_runbook_refuse();\n"
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION opspilot_runbook_refuse();"
        )
    for table in FROZEN_TABLES:
        statements.append(
            f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION opspilot_runbook_refuse();"
        )
    statements.extend(
        f"GRANT {privilege} ON {table} TO {role};" for role, table, privilege in GRANTS
    )
    op.execute("\n".join(statements))


def downgrade() -> None:
    bind = op.get_bind()
    counts = {
        table: int(bind.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())
        for table in (SUBMISSIONS, REVISIONS)
    }
    # Counts only, never a name or a text. psycopg drops server notices
    # unless a handler is registered (0024).
    raw = getattr(bind.connection, "dbapi_connection", None)

    def forward(diagnostic: object) -> None:
        _log.warning("%s", getattr(diagnostic, "message_primary", diagnostic))

    if raw is not None and hasattr(raw, "add_notice_handler"):
        raw.add_notice_handler(forward)
    try:
        op.execute(
            "DO $$ BEGIN RAISE NOTICE "
            "'imported runbooks dropped by downgrade: submissions=%, revisions=%', "
            f"{counts[SUBMISSIONS]}, {counts[REVISIONS]}; END $$;"
        )
    finally:
        if raw is not None and hasattr(raw, "remove_notice_handler"):
            raw.remove_notice_handler(forward)
    statements = [
        f"REVOKE {privilege} ON {table} FROM {role};"
        for role, table, privilege in reversed(GRANTS)
    ]
    statements.append(f"DROP VIEW {STATES};")
    statements.extend(
        f"DROP TABLE {table};"
        for table in (
            REVOCATIONS,
            REVISIONS,
            DECISIONS,
            SUBMISSIONS,
            ENTRIES,
            AUDIT,
            REQUESTS,
        )
    )
    statements.extend(f"DROP FUNCTION {function};" for function in FUNCTIONS)
    op.execute("\n".join(statements))
