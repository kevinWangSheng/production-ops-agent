"""0023 retention raw cleanup: payload state, close time, retention role (#244 PR-1).

Revision ID: 0023_retention_raw_cleanup
Revises: 0022_incident_association
Create Date: 2026-10-27

C3 section 11 (raw material is kept 30 days after close), contract r1 of
``docs/tasks/2026-10-27-244-retention-cleanup.md``. Schema and guards only;
the cleanup CLI (PR-2) and the cleaned read side (PR-3) come later.

* ``opspilot_incidents.closed_at``: written by trigger with the database
  clock when the lifecycle becomes ``closed``, cleared when it leaves
  ``closed`` (reopen), kept while it stays. A trigger and not the close/reopen
  SQL, because the web's column grants (``RUNTIME_GRANTS``) do not change.
  Incidents closed before this revision keep NULL: no reliable close time,
  and the cleanup never touches them.
* The five raw payloads (``evidence.raw``, Observer ``readings.raw``, release
  ``query_results.raw`` and ``comparison_queries.raw``,
  ``alert_deliveries.alert_json``) become nullable and each table gets
  ``payload_state`` ``present | absent | cleaned`` with ``present`` exactly
  when the payload is there. Backfill: Observer readings stored without raw
  are ``absent`` (never ``cleaned``), everything else ``present``. The hash /
  length CHECKs apply only while the payload exists.
* An insert trigger derives ``payload_state`` from the row: no writer names
  the column (their column grants could not), and a bare ``DEFAULT 'present'``
  would turn the Observer's and the seed SQL's raw=NULL readings into a
  CHECK violation (pre-review probe). The four payloads that were NOT NULL
  still refuse a NULL on insert (23502); only the cleanup produces one.
* ``opspilot_retention``: a ``NOLOGIN`` group, reused untouched only when it is
  a plain group (same rule as 0011), granted ``RETENTION_GRANTS``: SELECT for
  the determination, UPDATE of the payload column and ``payload_state`` on
  the five tables, lock-only UPDATE of one column of incidents, Runs and
  observation sessions (contract r2: the cleanup locks incident -> Run /
  session and rechecks; ``retention_lock_guard`` refuses every real update by
  a member), nothing else -- no F13 lock or write. A guard trigger narrows the
  payload UPDATE to
  present -> cleaned with the payload set to NULL and every other column
  unchanged (42501 otherwise); superusers and the owner pass on the three
  runtime tables. The release tables stay append-only for every role, the
  owner included, except that one cleaning update by a retention member.

``downgrade()`` refuses while any row is ``cleaned`` (the schema before 0023
cannot hold a missing payload), then revokes exactly what was granted and
drops the role when nothing references it, as 0011 does.
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0023_retention_raw_cleanup"
down_revision: str | Sequence[str] | None = "0022_incident_association"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE = "opspilot_retention"

# Frozen: equal to ``RETENTION_PAYLOADS`` / ``RETENTION_GRANTS`` in
# ``opspilot.persistence.grants`` (relations with no grant are omitted).
PAYLOADS = {
    "opspilot_alert_deliveries": "alert_json",
    "opspilot_evidence": "raw",
    "opspilot_observation_signal_readings": "raw",
    "opspilot_release_comparison_queries": "raw",
    "opspilot_release_query_results": "raw",
}
# lock-only grants (contract r2): the cleanup locks these rows FOR UPDATE
LOCKED = ("opspilot_incidents", "opspilot_runs", "opspilot_observation_sessions")
RELEASE = {
    "opspilot_release_query_results": "release_query",
    "opspilot_release_comparison_queries": "release_comparison_query",
}
GRANTS: dict[str, tuple[str, ...]] = {
    "alembic_version": ("SELECT",),
    "opspilot_incidents": ("SELECT", "UPDATE(incident_id)"),
    "opspilot_runs": ("SELECT", "UPDATE(run_id)"),
    "opspilot_observation_sessions": ("SELECT", "UPDATE(session_id)"),
    "opspilot_observation_samples": ("SELECT",),
    "opspilot_observation_endings": ("SELECT",),
    "opspilot_postmortems": ("SELECT",),
    "opspilot_postmortem_versions": ("SELECT",),
    "opspilot_postmortem_conclusions": ("SELECT",),
    "opspilot_postmortem_generation_jobs": ("SELECT",),
    "opspilot_postmortem_generation_attempts": ("SELECT",),
    "opspilot_knowledge_entries": ("SELECT",),
    "opspilot_knowledge_revisions": ("SELECT",),
    "opspilot_knowledge_revision_states": ("SELECT",),
    "opspilot_release_observations": ("SELECT",),
    "opspilot_release_incident_links": ("SELECT",),
    "opspilot_release_comparisons": ("SELECT",),
    "opspilot_alert_deliveries": ("SELECT", "UPDATE(alert_json,payload_state)"),
    "opspilot_evidence": ("SELECT", "UPDATE(payload_state,raw)"),
    "opspilot_observation_signal_readings": ("SELECT", "UPDATE(payload_state,raw)"),
    "opspilot_release_comparison_queries": ("SELECT", "UPDATE(payload_state,raw)"),
    "opspilot_release_query_results": ("SELECT", "UPDATE(payload_state,raw)"),
}

# CHECKs that read the payload: (table, name, before 0023, from 0023).
HASH_CHECKS = (
    (
        "opspilot_release_query_results",
        "opspilot_release_query_results_check",
        "octet_length(raw)<=30720 AND raw_sha256=encode(sha256(raw),'hex')",
        "raw IS NULL OR (octet_length(raw)<=30720 AND raw_sha256=encode(sha256(raw),'hex'))",
    ),
    (
        "opspilot_release_comparison_queries",
        "opspilot_release_comparison_queries_check",
        "octet_length(raw)<=30720 AND raw_sha256=encode(sha256(raw),'hex')",
        "raw IS NULL OR (octet_length(raw)<=30720 AND raw_sha256=encode(sha256(raw),'hex'))",
    ),
    (
        "opspilot_alert_deliveries",
        "opspilot_alert_deliveries_alert_json_check",
        "octet_length(alert_json) <= 16384",
        "alert_json IS NULL OR octet_length(alert_json) <= 16384",
    ),
)

FUNCTIONS = """
    CREATE FUNCTION opspilot_incident_closed_at() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
    BEGIN
      IF NEW.lifecycle <> 'closed' THEN
        NEW.closed_at := NULL;
      ELSIF TG_OP = 'INSERT' THEN
        NEW.closed_at := COALESCE(NEW.closed_at, pg_catalog.clock_timestamp());
      ELSIF OLD.lifecycle IS DISTINCT FROM 'closed' THEN
        NEW.closed_at := pg_catalog.clock_timestamp();
      END IF;
      RETURN NEW;
    END
    $$;
    CREATE FUNCTION opspilot_payload_state_insert() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
    DECLARE
      missing boolean;
    BEGIN
      -- a field reference, not to_jsonb(NEW): no copy of a large payload
      IF TG_TABLE_NAME = 'opspilot_alert_deliveries' THEN
        missing := NEW.alert_json IS NULL;
      ELSE
        missing := NEW.raw IS NULL;
      END IF;
      IF missing AND TG_TABLE_NAME <> 'opspilot_observation_signal_readings' THEN
        RAISE not_null_violation USING
          MESSAGE = pg_catalog.format('null value in column "%s" of relation "%s" violates not-null constraint', TG_ARGV[0], TG_TABLE_NAME),
          COLUMN = TG_ARGV[0], TABLE = TG_TABLE_NAME;
      END IF;
      NEW.payload_state := CASE WHEN missing THEN 'absent' ELSE 'present' END;
      RETURN NEW;
    END
    $$;
    CREATE FUNCTION opspilot_payload_clean_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
    DECLARE
      privileged boolean := (SELECT r.rolsuper FROM pg_catalog.pg_roles r WHERE r.rolname = current_user)
        OR current_user = (SELECT pg_catalog.pg_get_userbyid(c.relowner) FROM pg_catalog.pg_class c WHERE c.oid = TG_RELID);
      before jsonb := pg_catalog.to_jsonb(OLD);
      after jsonb := pg_catalog.to_jsonb(NEW);
    BEGIN
      -- the release tables are append-only for the owner too (0015, 0020)
      IF privileged AND TG_ARGV[1] = 'runtime' THEN
        RETURN NEW;
      END IF;
      IF NOT privileged
        AND pg_catalog.pg_has_role(current_user, 'opspilot_retention', 'MEMBER')
        AND before ->> 'payload_state' = 'present'
        AND after ->> 'payload_state' = 'cleaned'
        AND after -> TG_ARGV[0] = 'null'::jsonb
        AND (after - TG_ARGV[0] - 'payload_state') = (before - TG_ARGV[0] - 'payload_state')
      THEN
        RETURN NEW;
      END IF;
      RAISE insufficient_privilege USING MESSAGE = pg_catalog.format(
        'only opspilot_retention may clean a present payload of %s, and nothing else', TG_TABLE_NAME);
    END
    $$;
    CREATE FUNCTION opspilot_retention_lock_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
    BEGIN
      IF pg_catalog.pg_has_role(current_user, 'opspilot_retention', 'MEMBER')
        AND NOT (SELECT r.rolsuper FROM pg_catalog.pg_roles r WHERE r.rolname = current_user)
        AND current_user <> (SELECT pg_catalog.pg_get_userbyid(c.relowner) FROM pg_catalog.pg_class c WHERE c.oid = TG_RELID)
      THEN
        RAISE insufficient_privilege USING MESSAGE = pg_catalog.format(
          'opspilot_retention may lock %s rows but not update them', TG_TABLE_NAME);
      END IF;
      RETURN NEW;
    END
    $$;
    CREATE FUNCTION opspilot_release_update_gate() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
    BEGIN
      IF NOT pg_catalog.pg_has_role(current_user, 'opspilot_retention', 'MEMBER')
        OR (SELECT r.rolsuper FROM pg_catalog.pg_roles r WHERE r.rolname = current_user)
        OR current_user = (SELECT pg_catalog.pg_get_userbyid(c.relowner) FROM pg_catalog.pg_class c WHERE c.oid = TG_RELID)
      THEN
        RAISE insufficient_privilege USING MESSAGE = 'release records are immutable';
      END IF;
      RETURN NULL;
    END
    $$;
"""


def _role() -> str:
    return f"""
    DO $$
    DECLARE
      unsafe text;
    BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') THEN
        CREATE ROLE {ROLE} NOLOGIN;
      ELSE
        -- reused only as a plain group, never altered (0011's rule)
        SELECT pg_catalog.concat_ws(', ',
          CASE WHEN rolcanlogin THEN 'LOGIN' END,
          CASE WHEN rolsuper THEN 'SUPERUSER' END,
          CASE WHEN rolcreaterole THEN 'CREATEROLE' END,
          CASE WHEN rolcreatedb THEN 'CREATEDB' END,
          CASE WHEN rolreplication THEN 'REPLICATION' END,
          CASE WHEN rolbypassrls THEN 'BYPASSRLS' END)
        INTO unsafe FROM pg_roles WHERE rolname = '{ROLE}';
        IF unsafe <> '' THEN
          RAISE EXCEPTION 'existing role {ROLE} is not a plain NOLOGIN group (has %); fix or rename it before migrating to 0023', unsafe;
        END IF;
      END IF;
    END
    $$;
    GRANT USAGE ON SCHEMA public TO {ROLE};
    """


def _privilege(verb: str, privilege: str, relation: str) -> str:
    kind, _, columns = privilege.partition("(")
    on = f"({columns.rstrip(')')}) " if columns else ""
    direction = "TO" if verb == "GRANT" else "FROM"
    return f"{verb} {kind} {on}ON {relation} {direction} {ROLE};"


def upgrade() -> None:
    statements = [
        _role(),
        FUNCTIONS,
        """
        ALTER TABLE opspilot_incidents ADD COLUMN closed_at timestamptz;
        ALTER TABLE opspilot_incidents ADD CONSTRAINT opspilot_incidents_closed_at_check
          CHECK (closed_at IS NULL OR lifecycle = 'closed');
        CREATE TRIGGER opspilot_incident_closed_at BEFORE INSERT OR UPDATE ON opspilot_incidents
          FOR EACH ROW EXECUTE FUNCTION opspilot_incident_closed_at();
        """,
    ]
    for table, column in PAYLOADS.items():
        statements.append(f"""
        ALTER TABLE {table} ALTER COLUMN {column} DROP NOT NULL;
        ALTER TABLE {table} ADD COLUMN payload_state text NOT NULL DEFAULT 'present';
        """)
    statements.append(
        "UPDATE opspilot_observation_signal_readings SET payload_state='absent' WHERE raw IS NULL;"
    )
    for table, name, _, after in HASH_CHECKS:
        statements.append(
            f"ALTER TABLE {table} DROP CONSTRAINT {name}, ADD CONSTRAINT {name} CHECK ({after});"
        )
    for table, column in PAYLOADS.items():
        statements.append(f"""
        ALTER TABLE {table} ALTER COLUMN payload_state DROP DEFAULT;
        ALTER TABLE {table} ADD CONSTRAINT {table}_payload_state_check
          CHECK (payload_state IN ('present', 'absent', 'cleaned'));
        ALTER TABLE {table} ADD CONSTRAINT {table}_payload_present_check
          CHECK ((payload_state = 'present') = ({column} IS NOT NULL));
        CREATE TRIGGER payload_state_insert BEFORE INSERT ON {table}
          FOR EACH ROW EXECUTE FUNCTION opspilot_payload_state_insert('{column}');
        """)
        if table in RELEASE:
            prefix = RELEASE[table]
            statements.append(f"""
            DROP TRIGGER {prefix}_immutable ON {table};
            CREATE TRIGGER {prefix}_immutable BEFORE DELETE OR TRUNCATE ON {table}
              FOR EACH STATEMENT EXECUTE FUNCTION opspilot_release_immutable_guard();
            CREATE TRIGGER {prefix}_update_gate BEFORE UPDATE ON {table}
              FOR EACH STATEMENT EXECUTE FUNCTION opspilot_release_update_gate();
            CREATE TRIGGER payload_clean_guard BEFORE UPDATE ON {table}
              FOR EACH ROW EXECUTE FUNCTION opspilot_payload_clean_guard('{column}', 'release');
            """)
        else:
            statements.append(f"""
            CREATE TRIGGER payload_clean_guard BEFORE UPDATE ON {table} FOR EACH ROW
              WHEN (OLD.{column} IS DISTINCT FROM NEW.{column} OR OLD.payload_state IS DISTINCT FROM NEW.payload_state)
              EXECUTE FUNCTION opspilot_payload_clean_guard('{column}', 'runtime');
            """)
    for table in LOCKED:
        statements.append(f"""
        CREATE TRIGGER retention_lock_guard BEFORE UPDATE ON {table} FOR EACH ROW
          WHEN (pg_catalog.pg_has_role(current_user, '{ROLE}', 'MEMBER'))
          EXECUTE FUNCTION opspilot_retention_lock_guard();
        """)
    for relation, privileges in GRANTS.items():
        statements.extend(_privilege("GRANT", p, relation) for p in privileges)
    op.execute("\n".join(statements))


def downgrade() -> None:
    op.execute(f"LOCK TABLE {', '.join(PAYLOADS)} IN ACCESS EXCLUSIVE MODE")
    cleaned = int(
        op.get_bind()
        .execute(
            text(
                "SELECT "
                + " + ".join(
                    f"(SELECT count(*) FROM {table} WHERE payload_state='cleaned')"
                    for table in PAYLOADS
                )
            )
        )
        .scalar_one()
    )
    if cleaned:
        raise RuntimeError(
            f"{cleaned} cleaned payload row(s); the schema before 0023 cannot "
            "represent a cleaned payload, refusing to downgrade"
        )
    statements = [f"REVOKE USAGE ON SCHEMA public FROM {ROLE};"]
    for relation, privileges in GRANTS.items():
        statements.extend(_privilege("REVOKE", p, relation) for p in privileges)
    for table in LOCKED:
        statements.append(f"DROP TRIGGER retention_lock_guard ON {table};")
    for table, column in PAYLOADS.items():
        statements.append(f"""
        DROP TRIGGER payload_clean_guard ON {table};
        DROP TRIGGER payload_state_insert ON {table};
        """)
        if table in RELEASE:
            prefix = RELEASE[table]
            statements.append(f"""
            DROP TRIGGER {prefix}_update_gate ON {table};
            DROP TRIGGER {prefix}_immutable ON {table};
            CREATE TRIGGER {prefix}_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON {table}
              FOR EACH STATEMENT EXECUTE FUNCTION opspilot_release_immutable_guard();
            """)
    for table, name, before, _ in HASH_CHECKS:
        statements.append(
            f"ALTER TABLE {table} DROP CONSTRAINT {name}, ADD CONSTRAINT {name} CHECK ({before});"
        )
    for table, column in PAYLOADS.items():
        statements.append(f"ALTER TABLE {table} DROP COLUMN payload_state;")
        if table != "opspilot_observation_signal_readings":
            statements.append(
                f"ALTER TABLE {table} ALTER COLUMN {column} SET NOT NULL;"
            )
    statements.append(f"""
    DROP TRIGGER opspilot_incident_closed_at ON opspilot_incidents;
    ALTER TABLE opspilot_incidents DROP COLUMN closed_at;
    DROP FUNCTION opspilot_retention_lock_guard();
    DROP FUNCTION opspilot_release_update_gate();
    DROP FUNCTION opspilot_payload_clean_guard();
    DROP FUNCTION opspilot_payload_state_insert();
    DROP FUNCTION opspilot_incident_closed_at();
    DO $$
    BEGIN
      IF NOT EXISTS (
        SELECT 1 FROM pg_shdepend d
        JOIN pg_roles r ON r.oid = d.refobjid
        WHERE d.refclassid = 'pg_authid'::regclass AND r.rolname = '{ROLE}'
      ) THEN
        IF EXISTS (
          SELECT 1 FROM pg_auth_members m
          JOIN pg_roles g ON g.oid = m.roleid
          WHERE g.rolname = '{ROLE}'
        ) THEN
          RAISE EXCEPTION '{ROLE} still has members; drop or re-home its login roles before downgrading 0023';
        END IF;
        DROP ROLE IF EXISTS {ROLE};
      END IF;
    END
    $$;
    """)
    op.execute("\n".join(statements))
