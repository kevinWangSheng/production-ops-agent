"""0019 release before/after comparisons (F11 PR-4a).

Revision ID: 0019_release_comparisons
Revises: 0018_alert_correlation
Create Date: 2026-10-22

F11 PR-4 contract P4-01=A and r2 S1-S9: after an anomaly ending the
Observer collects one bounded before/after comparison and stores it here.

* ``opspilot_release_comparisons``: at most one record per observation and
  per anomaly ending; the derived ``comparison_id`` makes a resubmission
  return the stored record. The insert guard binds the row to the release's
  anomaly ending, its sample's after window and ``released_at`` as the end
  of the before window; the Observer cannot write it for any other ending.
* ``opspilot_release_comparison_queries``: the raw before-window responses,
  evaluated at ``released_at`` (S8), as ``opspilot_release_query_results``
  stores a sample's.

Both tables are append-only for every role, the owner included (0014's
``opspilot_release_immutable_guard``). Grants: the Observer may SELECT and
INSERT the columns it writes; the web may SELECT; the worker has nothing.

The guard only fences code and SQL errors; replay owns integrity.

``downgrade()`` refuses while a comparison exists.
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0019_release_comparisons"
down_revision: str | Sequence[str] | None = "0018_alert_correlation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen: equal to ``OBSERVER_GRANTS`` in ``opspilot.acceptance_recovery``.
COMPARISON_INSERT = "comparison_id,observation_id,ending_id,session_id,sample_id,format_version,status,attempts,window_before_start,window_before_end,window_after_start,window_after_end,decision_at,evidence_sha256,comparison"
QUERY_INSERT = "comparison_id,query_key,query,source,evaluated_at,sent_at,status,http_status,detail,body_complete,raw,raw_sha256"


def upgrade() -> None:
    op.execute(f"""
    CREATE TABLE opspilot_release_comparisons (
      comparison_id uuid PRIMARY KEY,
      observation_id uuid NOT NULL UNIQUE REFERENCES opspilot_release_observations,
      ending_id uuid NOT NULL UNIQUE REFERENCES opspilot_observation_endings,
      session_id uuid NOT NULL REFERENCES opspilot_observation_sessions,
      sample_id uuid NOT NULL REFERENCES opspilot_observation_samples,
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
    CREATE FUNCTION opspilot_release_comparison_guard() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
    BEGIN
      IF NEW.decision_at > clock_timestamp() OR NOT EXISTS (
        SELECT 1 FROM public.opspilot_observation_endings e
          JOIN public.opspilot_release_observations r ON r.observation_id=e.release_observation_id
          JOIN public.opspilot_observation_samples m ON m.sample_id=e.sample_id
        WHERE e.ending_id=NEW.ending_id AND e.release_observation_id=NEW.observation_id
          AND e.purpose='release_observation' AND e.anomaly_key IS NOT NULL
          AND e.session_id=NEW.session_id AND e.sample_id=NEW.sample_id
          AND r.status='anomalous' AND r.released_at=NEW.window_before_end
          AND m.window_start=NEW.window_after_start AND m.window_end=NEW.window_after_end)
      THEN RAISE insufficient_privilege USING MESSAGE='release comparison subject refused'; END IF;
      NEW.recorded_at := clock_timestamp();
      RETURN NEW;
    END $$;
    CREATE TRIGGER release_comparison_guard BEFORE INSERT ON opspilot_release_comparisons FOR EACH ROW EXECUTE FUNCTION opspilot_release_comparison_guard();
    CREATE TRIGGER release_comparison_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON opspilot_release_comparisons FOR EACH STATEMENT EXECUTE FUNCTION opspilot_release_immutable_guard();
    CREATE TRIGGER release_comparison_query_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON opspilot_release_comparison_queries FOR EACH STATEMENT EXECUTE FUNCTION opspilot_release_immutable_guard();
    GRANT SELECT ON opspilot_release_comparisons,opspilot_release_comparison_queries TO opspilot_observer,opspilot_web;
    GRANT INSERT({COMPARISON_INSERT}) ON opspilot_release_comparisons TO opspilot_observer;
    GRANT INSERT({QUERY_INSERT}) ON opspilot_release_comparison_queries TO opspilot_observer;
    """)


def downgrade() -> None:
    op.execute(
        "LOCK TABLE opspilot_release_comparisons, opspilot_release_comparison_queries IN ACCESS EXCLUSIVE MODE"
    )
    count = int(
        op.get_bind()
        .execute(text("SELECT count(*) FROM opspilot_release_comparisons"))
        .scalar_one()
    )
    if count:
        raise RuntimeError(
            f"{count} release comparison(s); the schema before 0019 cannot "
            "represent them, refusing to downgrade"
        )
    op.execute("""
    DROP TABLE opspilot_release_comparison_queries;
    DROP TABLE opspilot_release_comparisons;
    DROP FUNCTION opspilot_release_comparison_guard();
    """)
