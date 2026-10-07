"""0003 observation store: sessions, samples, signal readings, Observer role.

Revision ID: 0003_observation_store
Revises: 0002_state_checks
Create Date: 2026-10-07

M1-02 step 2 (issue #84; C3 section 10 "观察主体与授权" / "采样与提交";
task record 2026-10-03). Three tables:

* ``opspilot_health_profiles`` -- the canonical content of every
  HealthProfile revision a session was authorized under, keyed by the
  revision (``<profile_id>@<sha256 prefix>``), so a replay can read the
  coverage queries and thresholds the revision stood for (F6 step 5).
* ``opspilot_observation_sessions`` -- one explicitly authorized observation
  period per incident, with the parameters fixed at authorization (deadline,
  sample budget, cadence, sustained window), the adopted watermark and the
  single active sampling job (at most one per session, by construction: the
  job lives in the session row).
* ``opspilot_observation_samples`` -- every submitted sample, adopted or
  history only, with the conditions the decision was taken under so the
  decision can be replayed from storage alone (F6 step 5).
* ``opspilot_observation_signal_readings`` -- one row per signal per sample:
  the query, window, source, returned value and the sha256 of the raw result.

``opspilot_incidents.observation_generation`` is the column the domain
``Incident.observation_generation`` never had; authorizing a session
increments it.

The Observer runs under its own PostgreSQL role (C3 section 3, decision D3).
This revision creates ``opspilot_observer`` as ``NOLOGIN`` -- the login role
that is a member of it is created outside the repository -- and grants only
what claiming and submitting a sample needs: reading the control scope,
reading the identity/lifecycle columns of incidents and updating
``lifecycle`` alone, updating the watermark/job/state columns of sessions,
and inserting samples and readings. No privilege on runs, steps, evidence,
controls, inputs, reports (``conclusion``) or the session parameters.

State columns carry CHECK constraints equal to the domain Literals
(``tests/test_schema_observation_store.py`` compares ``CHECKS``). The role is
cluster-wide, so ``downgrade()`` drops it only when no other database still
references it (``pg_shdepend``), after ``DROP OWNED BY`` has revoked every
privilege in this database.
"""

import re
from collections.abc import Sequence

from alembic import op

revision: str = "0003_observation_store"
down_revision: str | Sequence[str] | None = "0002_state_checks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OBSERVER_ROLE = "opspilot_observer"

# (table, column) -> allowed values, verbatim from the domain Literal named
# in the comment (same discipline as 0002: literal tuples, compared by test).
CHECKS: dict[tuple[str, str], tuple[str, ...]] = {
    # opspilot.domain.observation.ObservationPurpose
    ("opspilot_observation_sessions", "purpose"): (
        "incident_recovery",
        "release_observation",
    ),
    # opspilot.domain.observation.ObservationSessionState
    ("opspilot_observation_sessions", "state"): (
        "authorized",
        "completed",
        "revoked",
        "expired",
    ),
    # opspilot.domain.observation.SampleOutcome
    ("opspilot_observation_samples", "outcome"): (
        "healthy",
        "degraded",
        "no_data",
        "stale",
        "timeout",
        "failed",
    ),
    # opspilot.domain.observation.SampleReason
    ("opspilot_observation_samples", "reason"): (
        "adopted",
        "session_not_authorized",
        "session_mismatch",
        "control_generation_stale",
        "observation_generation_stale",
        "health_profile_revision_mismatch",
        "sequence_not_advancing",
        "window_regressed",
        "subject_state_not_adoptable",
        "deadline_expired",
        "suspended",
        "lease_revoked",
    ),
    # opspilot.domain.subjects.IncidentLifecycle
    ("opspilot_observation_samples", "subject_lifecycle"): (
        "open",
        "observing_recovery",
        "resolved",
        "closed",
    ),
}

# Persistence-level vocabularies (no domain Literal): the way a session
# ended, how a sample was filed, the incident trigger a submission fired and
# the status of one signal reading (interface contract item 3).
ENDED_REASONS = (
    "recovery_confirmed",
    "deadline_expired",
    "max_samples_exhausted",
    "authority_revoked",
    # a sample proved the session's bindings stale (control or observation
    # generation moved on, subject no longer adoptable): nothing to retry
    "binding_stale",
)
DISPOSITIONS = ("adopted", "history_only")
TRANSITIONS = ("recovery_confirmed", "observation_ended_unconfirmed")
READING_STATUSES = ("ok", "no_data", "stale", "timeout", "failed")
# Why a sample did or did not confirm health (C3 section 10 "恢复观察使用
# 整改后的新数据": a window that starts before the authorization is adopted
# as an observation but never counts as healthy).
HEALTH_BASES = (
    "confirmed",
    "not_adopted",
    "no_health_profile",
    "outcome_not_healthy",
    "required_signals_missing",
    "window_before_authorization",
    # the window starts before the latest control-scope change (a release
    # from suspension): the paused interval is not observed time
    "window_before_scope_change",
)
# Raw reading payloads are stored for hash verification on replay; one
# signal's Prometheus result is bounded like the M0 response limit (128 KiB).
READING_RAW_LIMIT = 131072

# Columns the Observer may change on a session: watermark, job slot, state.
# Everything fixed at authorization (identity, generations, profile revision,
# deadline, budgets) stays read-only for it.
OBSERVER_SESSION_COLUMNS = (
    "state",
    "ended_reason",
    "adopted_sequence",
    "adopted_window_end",
    "adopted_count",
    "healthy_since",
    "healthy_since_global_generation",
    "healthy_since_target_generation",
    "issued_sequence",
    "active_sample_job_id",
    "active_sample_sequence",
    "active_sample_due_at",
    "active_sample_owner",
    "active_sample_epoch",
    "active_sample_lease_until",
    "updated_at",
)
OBSERVER_INCIDENT_COLUMNS = (
    "incident_id",
    "lifecycle",
    "control_generation",
    "observation_generation",
    "target_id",
)

_WORD = re.compile(r"^[a-z_]+$")


def _in_list(values: Sequence[str]) -> str:
    for value in values:
        if not _WORD.match(value):
            raise ValueError(f"value {value!r} is not a plain lowercase word")
    return ", ".join(f"'{value}'" for value in values)


def _check(
    table: str, column: str, values: Sequence[str], *, nullable: bool = False
) -> str:
    allow_null = f"{column} IS NULL OR " if nullable else ""
    return (
        f"CONSTRAINT {table}_{column}_check "
        f"CHECK ({allow_null}{column} IN ({_in_list(values)}))"
    )


def upgrade() -> None:
    profiles = "opspilot_health_profiles"
    sessions = "opspilot_observation_sessions"
    samples = "opspilot_observation_samples"
    readings = "opspilot_observation_signal_readings"
    op.execute(
        f"""
        ALTER TABLE opspilot_incidents ADD COLUMN observation_generation integer NOT NULL DEFAULT 0;
        CREATE TABLE {profiles} (
          -- <profile_id>@<first 12 hex of sha256(content)>; content is the
          -- canonical JSON text exactly as hashed (jsonb would re-serialize it)
          health_profile_revision text PRIMARY KEY,
          profile_id text NOT NULL,
          content_sha256 text NOT NULL,
          content text NOT NULL,
          created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          CONSTRAINT {profiles}_revision_check CHECK (
            health_profile_revision = profile_id || '@' || left(content_sha256, 12)
          ),
          CONSTRAINT {profiles}_sha256_check CHECK (content_sha256 ~ '^[0-9a-f]{{64}}$')
        );
        CREATE TABLE {sessions} (
          session_id uuid PRIMARY KEY,
          incident_id uuid NOT NULL REFERENCES opspilot_incidents,
          purpose text NOT NULL,
          target_id uuid NOT NULL REFERENCES opspilot_targets,
          -- the immutable Target identity the session was authorized on
          target jsonb NOT NULL,
          subject_control_generation integer NOT NULL,
          observation_generation integer NOT NULL,
          authorized boolean NOT NULL DEFAULT true,
          authorized_by text NOT NULL,
          authorized_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          state text NOT NULL DEFAULT 'authorized',
          ended_reason text,
          health_profile_revision text REFERENCES {profiles},
          -- fixed at authorization (interface contract item 5)
          deadline_at timestamptz NOT NULL,
          max_samples integer NOT NULL CHECK (max_samples > 0),
          sample_interval_seconds integer NOT NULL CHECK (sample_interval_seconds > 0),
          sustained_window_seconds integer NOT NULL CHECK (sustained_window_seconds > 0),
          -- adopted watermark (C3 section 10) and the healthy streak
          adopted_sequence integer NOT NULL DEFAULT 0,
          adopted_window_end timestamptz,
          adopted_count integer NOT NULL DEFAULT 0,
          healthy_since timestamptz,
          -- the control scope generations the healthy streak started under; a
          -- suspension in between (generation moved) restarts the streak
          healthy_since_global_generation integer,
          healthy_since_target_generation integer,
          -- the single active sampling job; a lease retry keeps the sequence
          issued_sequence integer NOT NULL DEFAULT 0,
          active_sample_job_id uuid,
          active_sample_sequence integer,
          active_sample_due_at timestamptz,
          active_sample_owner uuid,
          active_sample_epoch integer NOT NULL DEFAULT 0,
          active_sample_lease_until timestamptz,
          created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          {_check(sessions, "purpose", CHECKS[(sessions, "purpose")])},
          {_check(sessions, "state", CHECKS[(sessions, "state")])},
          {_check(sessions, "ended_reason", ENDED_REASONS, nullable=True)},
          CONSTRAINT {sessions}_ended_check CHECK ((state = 'authorized') = (ended_reason IS NULL)),
          CONSTRAINT {sessions}_streak_check CHECK (
            (healthy_since IS NULL) = (healthy_since_global_generation IS NULL)
            AND (healthy_since IS NULL) = (healthy_since_target_generation IS NULL)
          ),
          CONSTRAINT {sessions}_job_check CHECK (
            (active_sample_job_id IS NULL) = (active_sample_sequence IS NULL)
            AND (active_sample_job_id IS NULL) = (active_sample_due_at IS NULL)
            AND (active_sample_job_id IS NOT NULL OR active_sample_owner IS NULL)
          )
        );
        CREATE INDEX {sessions}_incident_id_idx ON {sessions}(incident_id);
        -- one authorized session per incident
        CREATE UNIQUE INDEX {sessions}_one_authorized_idx ON {sessions}(incident_id) WHERE state = 'authorized';
        -- the claim scan
        CREATE INDEX {sessions}_due_idx ON {sessions}(active_sample_due_at) WHERE state = 'authorized' AND active_sample_job_id IS NOT NULL;
        CREATE TABLE {samples} (
          sample_id uuid PRIMARY KEY,
          session_id uuid NOT NULL REFERENCES {sessions},
          job_id uuid NOT NULL,
          sequence integer NOT NULL CHECK (sequence > 0),
          epoch integer NOT NULL,
          window_start timestamptz NOT NULL,
          window_end timestamptz NOT NULL,
          outcome text NOT NULL,
          required_signals_present boolean NOT NULL,
          subject_control_generation integer NOT NULL,
          observation_generation integer NOT NULL,
          health_profile_revision text,
          -- the decision and the conditions it was taken under (replay reads
          -- only these; it never re-queries telemetry)
          disposition text NOT NULL,
          reason text NOT NULL,
          confirms_health boolean NOT NULL,
          health_basis text NOT NULL,
          subject_lifecycle text NOT NULL,
          incident_control_generation integer NOT NULL,
          incident_observation_generation integer NOT NULL,
          -- scope blocked: suspended, or the generations moved since the claim
          scope_suspended boolean NOT NULL,
          global_generation integer NOT NULL,
          target_generation integer NOT NULL,
          -- when the current scope generations were set (NULL: never changed)
          scope_changed_at timestamptz,
          within_deadline boolean NOT NULL,
          lease_valid boolean NOT NULL,
          transition text,
          submitted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          CONSTRAINT {samples}_window_check CHECK (window_end > window_start),
          {_check(samples, "outcome", CHECKS[(samples, "outcome")])},
          {_check(samples, "reason", CHECKS[(samples, "reason")])},
          {_check(samples, "subject_lifecycle", CHECKS[(samples, "subject_lifecycle")])},
          {_check(samples, "disposition", DISPOSITIONS)},
          {_check(samples, "health_basis", HEALTH_BASES)},
          CONSTRAINT {samples}_health_basis_check2 CHECK (confirms_health = (health_basis = 'confirmed')),
          {_check(samples, "transition", TRANSITIONS, nullable=True)},
          CONSTRAINT {samples}_adopted_check CHECK ((disposition = 'adopted') = (reason = 'adopted'))
        );
        CREATE INDEX {samples}_session_idx ON {samples}(session_id, submitted_at, sample_id);
        -- a sequence is adopted at most once per session
        CREATE UNIQUE INDEX {samples}_adopted_sequence_idx ON {samples}(session_id, sequence) WHERE disposition = 'adopted';
        CREATE TABLE {readings} (
          sample_id uuid NOT NULL REFERENCES {samples},
          signal_name text NOT NULL,
          status text NOT NULL,
          value double precision,
          sample_count integer,
          query text NOT NULL,
          window_start timestamptz NOT NULL,
          window_end timestamptz NOT NULL,
          source text NOT NULL,
          raw_sha256 text,
          raw bytea,
          PRIMARY KEY (sample_id, signal_name),
          CONSTRAINT {readings}_raw_check CHECK (raw IS NULL OR (raw_sha256 IS NOT NULL AND octet_length(raw) <= {READING_RAW_LIMIT})),
          {_check(readings, "status", READING_STATUSES)},
          CONSTRAINT {readings}_value_check CHECK (status = 'ok' OR value IS NULL),
          CONSTRAINT {readings}_window_check CHECK (window_end >= window_start)
        );
        """
    )
    # Observer role (D3): NOLOGIN, created once per cluster, least privilege.
    op.execute(
        f"""
        DO $$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{OBSERVER_ROLE}') THEN
            CREATE ROLE {OBSERVER_ROLE} NOLOGIN;
          END IF;
        END
        $$;
        GRANT SELECT ON alembic_version TO {OBSERVER_ROLE};
        GRANT SELECT ON opspilot_targets, opspilot_scope_controls, opspilot_target_suspensions, opspilot_suspension_audit TO {OBSERVER_ROLE};
        GRANT SELECT ({", ".join(OBSERVER_INCIDENT_COLUMNS)}) ON opspilot_incidents TO {OBSERVER_ROLE};
        GRANT UPDATE (lifecycle) ON opspilot_incidents TO {OBSERVER_ROLE};
        GRANT SELECT ON {profiles}, {sessions} TO {OBSERVER_ROLE};
        GRANT UPDATE ({", ".join(OBSERVER_SESSION_COLUMNS)}) ON {sessions} TO {OBSERVER_ROLE};
        GRANT SELECT, INSERT ON {samples}, {readings} TO {OBSERVER_ROLE};
        """
    )
    # The Observer may move an incident's lifecycle only along the two edges
    # its sampling fires (C3 section 10): observing_recovery -> resolved and
    # observing_recovery -> open. The column grant alone would let it write
    # any value; this trigger checks membership of the role, direct or
    # inherited (``pg_has_role``), excluding superusers and the table owner,
    # for whom pg_has_role is always true and who are not the Observer.
    op.execute(
        f"""
        CREATE FUNCTION opspilot_observer_lifecycle_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.lifecycle IS DISTINCT FROM OLD.lifecycle
            AND pg_has_role(current_user, '{OBSERVER_ROLE}', 'MEMBER')
            AND NOT (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)
            AND current_user <> (SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = TG_RELID)
          THEN
            IF NOT (OLD.lifecycle = 'observing_recovery' AND NEW.lifecycle IN ('resolved', 'open')) THEN
              RAISE insufficient_privilege USING MESSAGE =
                format('{OBSERVER_ROLE} may not move lifecycle %s -> %s', OLD.lifecycle, NEW.lifecycle);
            END IF;
          END IF;
          RETURN NEW;
        END
        $$;
        CREATE TRIGGER opspilot_incidents_observer_lifecycle_guard
          BEFORE UPDATE OF lifecycle ON opspilot_incidents
          FOR EACH ROW EXECUTE FUNCTION opspilot_observer_lifecycle_guard();
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        DROP OWNED BY {OBSERVER_ROLE};
        DROP TRIGGER opspilot_incidents_observer_lifecycle_guard ON opspilot_incidents;
        DROP FUNCTION opspilot_observer_lifecycle_guard();
        DROP TABLE opspilot_observation_signal_readings;
        DROP TABLE opspilot_observation_samples;
        DROP TABLE opspilot_observation_sessions;
        DROP TABLE opspilot_health_profiles;
        ALTER TABLE opspilot_incidents DROP COLUMN observation_generation;
        DO $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_shdepend d
            JOIN pg_roles r ON r.oid = d.refobjid
            WHERE d.refclassid = 'pg_authid'::regclass AND r.rolname = '{OBSERVER_ROLE}'
          ) THEN
            DROP ROLE IF EXISTS {OBSERVER_ROLE};
          END IF;
        END
        $$;
        """
    )
