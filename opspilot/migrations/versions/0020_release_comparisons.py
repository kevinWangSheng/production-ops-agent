"""0020 release before/after comparisons (F11 PR-4a).

Revision ID: 0020_release_comparisons
Revises: 0019_incident_priority
Create Date: 2026-10-22

F11 PR-4 contract P4-01=A and r2 S1-S9: after an anomaly ending the
Observer collects one bounded before/after comparison and stores it here.

* ``opspilot_release_comparisons``: at most one record per observation and
  per anomaly ending; the derived ``comparison_id`` makes a resubmission
  return the stored record. The insert guard binds the row to the release's
  anomaly ending, its sample's after window and ``released_at`` as the end
  of the before window; the Observer cannot write it for any other ending.
* ``opspilot_release_comparison_attempts``: one append-only row per
  collection attempt, at most three per ending (r3). An attempt is claimed
  by inserting its row: the primary key and the guard make the claim
  exclusive across Observers -- the next attempt needs every earlier one to
  be the claimer's own or past its lease -- so the queries spent on one
  ending stay within three plans whatever the number of Observers or
  crashes. Claims and records of one comparison take the same transaction
  advisory lock (``'release-comparison:' || comparison_id``), in the guards
  as well as in the store, so a lease check never races another claim. A record must come from the holder of the latest attempt inside its lease
  (r4 fencing); a spent, lapsed third attempt may only be filed unavailable.
* ``opspilot_release_comparison_queries``: the raw before-window responses,
  evaluated at ``released_at`` (S8), as ``opspilot_release_query_results``
  stores a sample's.

Both tables are append-only for every role, the owner included (0014's
``opspilot_release_immutable_guard``). Grants: the Observer may SELECT and
INSERT the columns it writes; the web may SELECT; the worker has nothing.

The guards recompute what the database can derive: the ``comparison_id``
(``uuid5(observation_id, 'comparison:' || ending_id)``, by a plain PL/pgSQL
SHA-1 so no extension is needed), the before window from ``released_at``
and the frozen profile's ``evaluation_window_seconds``, and the after window
from the anomaly sample. They fence code and SQL errors; replay owns
integrity.

``downgrade()`` refuses while a comparison exists.
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0020_release_comparisons"
down_revision: str | Sequence[str] | None = "0019_incident_priority"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen: equal to ``OBSERVER_GRANTS`` in ``opspilot.acceptance_recovery``.
COMPARISON_INSERT = "comparison_id,observation_id,ending_id,session_id,sample_id,owner,format_version,status,attempts,window_before_start,window_before_end,window_after_start,window_after_end,decision_at,evidence_sha256,comparison"
ATTEMPT_INSERT = "comparison_id,attempt,observation_id,ending_id,owner"
# The claim lease the guard stamps; the Observer's attempt budget is shorter.
ATTEMPT_LEASE_SECONDS = 150
QUERY_INSERT = "comparison_id,query_key,query,source,evaluated_at,sent_at,status,http_status,detail,body_complete,raw,raw_sha256"


def upgrade() -> None:
    op.execute(f"""
    CREATE FUNCTION opspilot_sha1(msg bytea) RETURNS bytea LANGUAGE plpgsql IMMUTABLE STRICT SET search_path=pg_catalog,pg_temp AS $$
    DECLARE
      m constant bigint := 4294967295;
      h0 bigint := 1732584193; h1 bigint := 4023233417; h2 bigint := 2562383102; h3 bigint := 271733878; h4 bigint := 3285377520;
      padded bytea := msg || '\\x80'::bytea;
      w bigint[];
      a bigint; b bigint; c bigint; d bigint; e bigint; f bigint; k bigint; t bigint;
      chunk int; i int; o int;
    BEGIN
      WHILE length(padded) % 64 <> 56 LOOP padded := padded || '\\x00'::bytea; END LOOP;
      padded := padded || int8send(length(msg)::bigint * 8);
      FOR chunk IN 0 .. length(padded) / 64 - 1 LOOP
        w := array_fill(0::bigint, ARRAY[80]);
        FOR i IN 0 .. 15 LOOP
          o := chunk * 64 + i * 4;
          w[i + 1] := (get_byte(padded, o)::bigint << 24) | (get_byte(padded, o + 1)::bigint << 16) | (get_byte(padded, o + 2)::bigint << 8) | get_byte(padded, o + 3)::bigint;
        END LOOP;
        FOR i IN 16 .. 79 LOOP
          t := w[i - 2] # w[i - 7] # w[i - 13] # w[i - 15];
          w[i + 1] := ((t << 1) | (t >> 31)) & m;
        END LOOP;
        a := h0; b := h1; c := h2; d := h3; e := h4;
        FOR i IN 0 .. 79 LOOP
          IF i < 20 THEN f := (b & c) | ((~b & m) & d); k := 1518500249;
          ELSIF i < 40 THEN f := b # c # d; k := 1859775393;
          ELSIF i < 60 THEN f := (b & c) | (b & d) | (c & d); k := 2400959708;
          ELSE f := b # c # d; k := 3395469782; END IF;
          t := ((((a << 5) | (a >> 27)) & m) + f + e + k + w[i + 1]) & m;
          e := d; d := c; c := ((b << 30) | (b >> 2)) & m; b := a; a := t;
        END LOOP;
        h0 := (h0 + a) & m; h1 := (h1 + b) & m; h2 := (h2 + c) & m; h3 := (h3 + d) & m; h4 := (h4 + e) & m;
      END LOOP;
      RETURN substring(int8send(h0) from 5) || substring(int8send(h1) from 5) || substring(int8send(h2) from 5) || substring(int8send(h3) from 5) || substring(int8send(h4) from 5);
    END $$;
    CREATE FUNCTION opspilot_comparison_id(observation_id uuid, ending_id uuid) RETURNS uuid LANGUAGE plpgsql IMMUTABLE STRICT SET search_path=pg_catalog,pg_temp AS $$
    DECLARE h bytea;
    BEGIN
      h := substring(public.opspilot_sha1(uuid_send(observation_id) || convert_to('comparison:' || ending_id::text, 'UTF8')) from 1 for 16);
      h := set_byte(h, 6, (get_byte(h, 6) & 15) | 80);
      h := set_byte(h, 8, (get_byte(h, 8) & 63) | 128);
      RETURN encode(h, 'hex')::uuid;
    END $$;
    CREATE TABLE opspilot_release_comparison_attempts (
      comparison_id uuid NOT NULL,
      attempt integer NOT NULL CHECK (attempt BETWEEN 1 AND 3),
      observation_id uuid NOT NULL REFERENCES opspilot_release_observations,
      ending_id uuid NOT NULL REFERENCES opspilot_observation_endings,
      owner uuid NOT NULL,
      lease_until timestamptz NOT NULL,
      recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      PRIMARY KEY (comparison_id, attempt)
    );
    CREATE TABLE opspilot_release_comparisons (
      comparison_id uuid PRIMARY KEY,
      observation_id uuid NOT NULL UNIQUE REFERENCES opspilot_release_observations,
      ending_id uuid NOT NULL UNIQUE REFERENCES opspilot_observation_endings,
      session_id uuid NOT NULL REFERENCES opspilot_observation_sessions,
      sample_id uuid NOT NULL REFERENCES opspilot_observation_samples,
      owner uuid NOT NULL,
      format_version integer NOT NULL CHECK (format_version = 1),
      status text NOT NULL CHECK (status IN ('complete','partial','unavailable')),
      attempts integer NOT NULL CHECK (attempts BETWEEN 1 AND 3),
      window_before_start timestamptz NOT NULL,
      window_before_end timestamptz NOT NULL,
      window_after_start timestamptz NOT NULL,
      window_after_end timestamptz NOT NULL,
      decision_at timestamptz NOT NULL,
      evidence_sha256 text NOT NULL CHECK (evidence_sha256 ~ '^[0-9a-f]{{64}}$'),
      comparison jsonb NOT NULL,
      recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      FOREIGN KEY (comparison_id, attempts) REFERENCES opspilot_release_comparison_attempts (comparison_id, attempt),
      CHECK (window_before_start < window_before_end AND window_after_start < window_after_end AND window_before_end <= decision_at),
      CHECK (comparison->>'status' = status AND comparison->>'comparison_id' = comparison_id::text
        AND comparison->>'ending_id' = ending_id::text AND comparison->>'observation_id' = observation_id::text
        AND comparison->>'sample_id' = sample_id::text AND (comparison->>'attempts')::integer = attempts)
    );
    CREATE TABLE opspilot_release_comparison_queries (
      comparison_id uuid NOT NULL REFERENCES opspilot_release_comparisons,
      query_key text NOT NULL CHECK (query_key<>''), query text NOT NULL CHECK (query<>''), source text NOT NULL CHECK (source='prometheus'),
      evaluated_at timestamptz NOT NULL, sent_at timestamptz,
      status text NOT NULL CHECK (status IN ('ok','no_data','timeout','failed')), http_status integer CHECK (http_status>=0),
      detail text NOT NULL, body_complete boolean NOT NULL, raw bytea NOT NULL, raw_sha256 text NOT NULL,
      PRIMARY KEY (comparison_id, query_key), CHECK (octet_length(raw)<=30720 AND raw_sha256=encode(sha256(raw),'hex'))
    );
    CREATE FUNCTION opspilot_release_comparison_attempt_guard() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
    BEGIN
      -- r5: claims and records of one comparison serialize on one xact lock
      PERFORM pg_advisory_xact_lock(hashtextextended('release-comparison:' || NEW.comparison_id::text, 0));
      IF NEW.comparison_id IS DISTINCT FROM public.opspilot_comparison_id(NEW.observation_id, NEW.ending_id)
        OR NOT EXISTS (
          SELECT 1 FROM public.opspilot_observation_endings e
            JOIN public.opspilot_release_observations r ON r.observation_id=e.release_observation_id
          WHERE e.ending_id=NEW.ending_id AND e.release_observation_id=NEW.observation_id
            AND e.purpose='release_observation' AND e.anomaly_key IS NOT NULL AND r.status='anomalous')
        OR EXISTS (SELECT 1 FROM public.opspilot_release_comparisons c WHERE c.comparison_id=NEW.comparison_id)
        OR (SELECT count(*) FROM public.opspilot_release_comparison_attempts a WHERE a.comparison_id=NEW.comparison_id) <> NEW.attempt - 1
        OR EXISTS (SELECT 1 FROM public.opspilot_release_comparison_attempts a WHERE a.comparison_id=NEW.comparison_id AND a.owner<>NEW.owner AND a.lease_until>clock_timestamp())
      THEN RAISE insufficient_privilege USING MESSAGE='release comparison attempt refused'; END IF;
      NEW.recorded_at := clock_timestamp();
      NEW.lease_until := NEW.recorded_at + interval '{ATTEMPT_LEASE_SECONDS} seconds';
      RETURN NEW;
    END $$;
    CREATE FUNCTION opspilot_release_comparison_guard() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
    DECLARE latest record;
    BEGIN
      PERFORM pg_advisory_xact_lock(hashtextextended('release-comparison:' || NEW.comparison_id::text, 0));
      IF NEW.decision_at > clock_timestamp()
        OR NEW.comparison_id IS DISTINCT FROM public.opspilot_comparison_id(NEW.observation_id, NEW.ending_id)
        OR NOT EXISTS (
          SELECT 1 FROM public.opspilot_observation_endings e
            JOIN public.opspilot_release_observations r ON r.observation_id=e.release_observation_id
            JOIN public.opspilot_observation_samples m ON m.sample_id=e.sample_id
            JOIN public.opspilot_health_profiles p ON p.health_profile_revision=r.health_profile_revision
          WHERE e.ending_id=NEW.ending_id AND e.release_observation_id=NEW.observation_id
            AND e.purpose='release_observation' AND e.anomaly_key IS NOT NULL
            AND e.session_id=NEW.session_id AND e.sample_id=NEW.sample_id
            AND r.status='anomalous' AND r.released_at=NEW.window_before_end
            AND NEW.window_before_start = r.released_at - ((p.content::jsonb->>'evaluation_window_seconds')::integer * interval '1 second')
            AND m.window_start=NEW.window_after_start AND m.window_end=NEW.window_after_end
            AND NEW.decision_at >= m.window_end)
      THEN RAISE insufficient_privilege USING MESSAGE='release comparison subject refused'; END IF;
      -- r4 fencing: only the holder of the latest attempt, inside its lease
      -- (DB clock), files the record; a spent third attempt whose lease
      -- lapsed may only be filed unavailable.
      SELECT * INTO latest FROM public.opspilot_release_comparison_attempts a
        WHERE a.comparison_id=NEW.comparison_id ORDER BY a.attempt DESC LIMIT 1;
      IF latest.attempt IS NULL OR NEW.attempts <> latest.attempt OR NOT (
        (latest.owner = NEW.owner AND latest.lease_until > clock_timestamp())
        OR (latest.attempt = 3 AND latest.lease_until <= clock_timestamp() AND NEW.status = 'unavailable'))
      THEN RAISE insufficient_privilege USING MESSAGE='release comparison lease refused'; END IF;
      NEW.recorded_at := clock_timestamp();
      RETURN NEW;
    END $$;
    CREATE TRIGGER release_comparison_attempt_guard BEFORE INSERT ON opspilot_release_comparison_attempts FOR EACH ROW EXECUTE FUNCTION opspilot_release_comparison_attempt_guard();
    CREATE TRIGGER release_comparison_guard BEFORE INSERT ON opspilot_release_comparisons FOR EACH ROW EXECUTE FUNCTION opspilot_release_comparison_guard();
    CREATE TRIGGER release_comparison_attempt_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON opspilot_release_comparison_attempts FOR EACH STATEMENT EXECUTE FUNCTION opspilot_release_immutable_guard();
    CREATE TRIGGER release_comparison_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON opspilot_release_comparisons FOR EACH STATEMENT EXECUTE FUNCTION opspilot_release_immutable_guard();
    CREATE TRIGGER release_comparison_query_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON opspilot_release_comparison_queries FOR EACH STATEMENT EXECUTE FUNCTION opspilot_release_immutable_guard();
    GRANT SELECT ON opspilot_release_comparison_attempts,opspilot_release_comparisons,opspilot_release_comparison_queries TO opspilot_observer,opspilot_web;
    GRANT INSERT({ATTEMPT_INSERT}) ON opspilot_release_comparison_attempts TO opspilot_observer;
    GRANT INSERT({COMPARISON_INSERT}) ON opspilot_release_comparisons TO opspilot_observer;
    GRANT INSERT({QUERY_INSERT}) ON opspilot_release_comparison_queries TO opspilot_observer;
    """)


def downgrade() -> None:
    op.execute(
        "LOCK TABLE opspilot_release_comparison_attempts, opspilot_release_comparisons, opspilot_release_comparison_queries IN ACCESS EXCLUSIVE MODE"
    )
    count = int(
        op.get_bind()
        .execute(
            text(
                "SELECT (SELECT count(*) FROM opspilot_release_comparison_attempts) + (SELECT count(*) FROM opspilot_release_comparisons)"
            )
        )
        .scalar_one()
    )
    if count:
        raise RuntimeError(
            f"{count} release comparison attempt(s) or record(s); the schema "
            "before 0020 cannot represent them, refusing to downgrade"
        )
    op.execute("""
    DROP TABLE opspilot_release_comparison_queries;
    DROP TABLE opspilot_release_comparisons;
    DROP TABLE opspilot_release_comparison_attempts;
    DROP FUNCTION opspilot_release_comparison_guard();
    DROP FUNCTION opspilot_release_comparison_attempt_guard();
    DROP FUNCTION opspilot_comparison_id(uuid, uuid);
    DROP FUNCTION opspilot_sha1(bytea);
    """)
