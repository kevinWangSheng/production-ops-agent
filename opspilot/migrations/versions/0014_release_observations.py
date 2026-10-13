"""F11 PR-1: immutable release identities and append-only delivery audits."""

from collections.abc import Sequence

from alembic import op

revision: str = "0014_release_observations"
down_revision: str | Sequence[str] | None = "0013_self_heal_authorization"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATES = ("pending", "observing", "healthy", "anomalous", "unknown", "cancelled")
# Frozen column grants; never import the moving application declaration.
WEB_OBSERVATION_INSERT = (
    "after_revision,auxiliary,before_revision,deadline_at,deadline_seconds,ended_at,"
    "ended_reason,format_version,health_profile_revision,max_samples,min_tracking_seconds,"
    "observation_id,released_at,sample_interval_seconds,semantic_sha256,source,"
    "source_release_id,status,sustained_window_seconds,target_id,target_snapshot,"
    "timing_policy_version"
)
WEB_EVENT_INSERT = (
    "actor,audit_text,kind,observation_id,outcome,raw_sha256,reason,received_at,"
    "semantic_sha256,sequence,source,source_release_id,truncated"
)


def upgrade() -> None:
    op.execute(
        f"""
        CREATE FUNCTION opspilot_release_revision_valid(value jsonb) RETURNS boolean
        LANGUAGE plpgsql IMMUTABLE SET search_path = pg_catalog, public AS $$
        DECLARE item record;
        BEGIN
          IF jsonb_typeof(value) IS DISTINCT FROM 'object'
            OR NOT value ?& ARRAY['images','pod_template_digest']
            OR value - ARRAY['images','pod_template_digest'] <> '{{}}'::jsonb
            OR jsonb_typeof(value->'images') IS DISTINCT FROM 'object'
            OR value->'images' = '{{}}'::jsonb
            OR jsonb_typeof(value->'pod_template_digest') IS DISTINCT FROM 'string'
            OR value->>'pod_template_digest' !~ '^sha256:[0-9a-f]{{64}}$'
          THEN RETURN false; END IF;
          FOR item IN SELECT key, val FROM jsonb_each(value->'images') AS e(key,val) LOOP
            IF item.key = '' OR jsonb_typeof(item.val) <> 'string'
              OR item.val #>> '{{}}' !~ '^sha256:[0-9a-f]{{64}}$'
            THEN RETURN false; END IF;
          END LOOP;
          RETURN true;
        END $$;
        CREATE TABLE opspilot_release_observations (
          observation_id uuid PRIMARY KEY,
          format_version integer NOT NULL CHECK (format_version = 1),
          source text NOT NULL CHECK (source <> ''),
          source_release_id text NOT NULL CHECK (source_release_id <> ''),
          target_id uuid NOT NULL REFERENCES opspilot_targets,
          target_snapshot jsonb NOT NULL CHECK (jsonb_typeof(target_snapshot) = 'object'),
          before_revision jsonb NOT NULL CHECK (opspilot_release_revision_valid(before_revision)),
          after_revision jsonb NOT NULL CHECK (opspilot_release_revision_valid(after_revision)),
          auxiliary jsonb NOT NULL CHECK (jsonb_typeof(auxiliary) = 'object'),
          semantic_sha256 text NOT NULL CHECK (semantic_sha256 ~ '^[0-9a-f]{{64}}$'),
          health_profile_revision text NOT NULL REFERENCES opspilot_health_profiles,
          timing_policy_version integer NOT NULL CHECK (timing_policy_version = 1),
          released_at timestamptz NOT NULL,
          received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          deadline_at timestamptz NOT NULL,
          deadline_seconds integer NOT NULL CHECK (deadline_seconds > 0),
          min_tracking_seconds integer NOT NULL CHECK (min_tracking_seconds > 0),
          sample_interval_seconds integer NOT NULL CHECK (sample_interval_seconds > 0),
          sustained_window_seconds integer NOT NULL CHECK (sustained_window_seconds > 0),
          max_samples integer NOT NULL CHECK (max_samples > 0),
          status text NOT NULL DEFAULT 'pending' CHECK (status IN ({", ".join(repr(s) for s in STATES)})),
          control_generation integer NOT NULL DEFAULT 0 CHECK (control_generation >= 0),
          observation_generation integer NOT NULL DEFAULT 0 CHECK (observation_generation >= 0),
          paused boolean NOT NULL DEFAULT false,
          ended_reason text,
          ended_at timestamptz,
          incident_id uuid REFERENCES opspilot_incidents,
          supersedes_observation_id uuid REFERENCES opspilot_release_observations,
          created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          CHECK (greatest(min_tracking_seconds, sustained_window_seconds) < deadline_seconds),
          CHECK (deadline_at = released_at + deadline_seconds * interval '1 second'),
          CHECK (released_at <= received_at),
          CHECK (auxiliary->'deployment_generation' IS NULL
            OR auxiliary->'deployment_generation' = 'null'::jsonb
            OR (jsonb_typeof(auxiliary->'deployment_generation') = 'number'
              AND auxiliary->>'deployment_generation' ~ '^[0-9]+$'))
        );
        CREATE UNIQUE INDEX opspilot_release_observations_source_idx
          ON opspilot_release_observations(source,source_release_id)
          WHERE supersedes_observation_id IS NULL;
        CREATE UNIQUE INDEX opspilot_release_observations_successor_idx
          ON opspilot_release_observations(supersedes_observation_id)
          WHERE supersedes_observation_id IS NOT NULL;
        CREATE TABLE opspilot_release_events (
          event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          observation_id uuid REFERENCES opspilot_release_observations,
          sequence integer,
          kind text NOT NULL CHECK (kind = 'release_delivery'),
          outcome text NOT NULL CHECK (outcome IN ('created','replayed','rejected')),
          reason text CHECK (reason IS NULL OR reason <> ''),
          source text NOT NULL CHECK (source <> ''),
          source_release_id text NOT NULL CHECK (source_release_id <> ''),
          actor text NOT NULL CHECK (actor <> ''),
          received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          raw_sha256 text NOT NULL CHECK (raw_sha256 ~ '^[0-9a-f]{{64}}$'),
          semantic_sha256 text NOT NULL CHECK (semantic_sha256 ~ '^[0-9a-f]{{64}}$'),
          audit_text text NOT NULL CHECK (octet_length(audit_text) <= 16384),
          truncated boolean NOT NULL,
          CHECK ((observation_id IS NULL AND sequence IS NULL AND outcome = 'rejected')
            OR (observation_id IS NOT NULL AND sequence IS NOT NULL AND sequence > 0)),
          CHECK (outcome <> 'rejected' OR reason IS NOT NULL),
          UNIQUE (observation_id, sequence)
        );
        CREATE FUNCTION opspilot_release_initial_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
          -- The caller cannot backdate reception or initialize later lifecycle phases.
          NEW.received_at := clock_timestamp();
          NEW.created_at := NEW.received_at;
          NEW.updated_at := NEW.received_at;
          IF NEW.released_at > NEW.received_at
            OR NEW.control_generation <> 0 OR NEW.observation_generation <> 0
            OR NEW.paused OR NEW.incident_id IS NOT NULL
            OR NEW.supersedes_observation_id IS NOT NULL
            OR ((NEW.status = 'pending' AND NEW.ended_reason IS NULL
                AND NEW.ended_at IS NULL AND NEW.received_at < NEW.deadline_at)
              OR (NEW.status = 'unknown' AND NEW.ended_reason = 'late_delivery'
                AND NEW.received_at >= NEW.deadline_at)) IS NOT TRUE
          THEN RAISE EXCEPTION 'release initialization refused' USING ERRCODE = '42501'; END IF;
          IF NEW.status = 'unknown' THEN NEW.ended_at := NEW.received_at; END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER opspilot_release_observations_initial_guard
          BEFORE INSERT ON opspilot_release_observations
          FOR EACH ROW EXECUTE FUNCTION opspilot_release_initial_guard();
        CREATE FUNCTION opspilot_release_immutable_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN RAISE EXCEPTION 'release records are immutable' USING ERRCODE = '42501'; END $$;
        CREATE TRIGGER opspilot_release_observations_immutable_guard
          BEFORE UPDATE OR DELETE OR TRUNCATE ON opspilot_release_observations
          FOR EACH STATEMENT EXECUTE FUNCTION opspilot_release_immutable_guard();
        CREATE TRIGGER opspilot_release_events_immutable_guard
          BEFORE UPDATE OR DELETE OR TRUNCATE ON opspilot_release_events
          FOR EACH STATEMENT EXECUTE FUNCTION opspilot_release_immutable_guard();
        GRANT SELECT ON opspilot_release_observations,opspilot_release_events TO opspilot_web;
        GRANT INSERT ({WEB_OBSERVATION_INSERT}) ON opspilot_release_observations TO opspilot_web;
        GRANT INSERT ({WEB_EVENT_INSERT}) ON opspilot_release_events TO opspilot_web;
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        LOCK TABLE opspilot_release_observations,opspilot_release_events IN ACCESS EXCLUSIVE MODE;
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM opspilot_release_observations)
            OR EXISTS (SELECT 1 FROM opspilot_release_events)
          THEN RAISE EXCEPTION 'ReleaseRecordsWouldBeLost: release tables are not empty'; END IF;
        END $$;
        REVOKE INSERT ({WEB_EVENT_INSERT}) ON opspilot_release_events FROM opspilot_web;
        REVOKE INSERT ({WEB_OBSERVATION_INSERT}) ON opspilot_release_observations FROM opspilot_web;
        REVOKE SELECT ON opspilot_release_observations,opspilot_release_events FROM opspilot_web;
        DROP TABLE opspilot_release_events;
        DROP TABLE opspilot_release_observations;
        DROP FUNCTION opspilot_release_initial_guard();
        DROP FUNCTION opspilot_release_immutable_guard();
        DROP FUNCTION opspilot_release_revision_valid(jsonb);
        """
    )
