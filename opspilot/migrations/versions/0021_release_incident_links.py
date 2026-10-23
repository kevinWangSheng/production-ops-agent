"""0021 release anomaly consumption: incident links (F11 PR-4b).

Revision ID: 0021_release_incident_links
Revises: 0020_release_comparisons
Create Date: 2026-10-22

F11 PR-4 contract P4-02/P4-03 (and R1, R7, R9): the web process consumes
each anomalous release's unique anomaly ending once, in one transaction that
opens or correlates the incident and records the outcome here.

* ``opspilot_release_incident_links``: the consumption record, one per
  observation (and so one per anomaly ending and ``anomaly_key``). The
  disposition says what the consumption did -- ``created`` (a new incident
  with its first Run), ``attached`` (one event on an open incident of the
  same target, no Run), ``created_related`` (a new incident related to the
  latest closed one) or ``handoff_created`` (no resolvable target: an
  incident handed to a human, no Run, with its ``handoff_reason``). The
  insert guard binds the row to the release's anomaly ending and to the
  incident, Run or event it names, and to the before/after comparison the
  incident's input was built with (``comparison_id``; none when the
  consumption stopped waiting for one); ``consumed_at`` is the consuming
  transaction's database time, the instant the Run's frame ends at.
  Append-only for every role, the owner included.
* ``opspilot_release_observations.incident_id``: the one edge a terminal
  release still takes -- ``anomalous`` with no incident to an incident, every
  other column unchanged (``updated_at`` is restamped), and only with this
  transaction's link row naming the same incident (a deferred evidence
  trigger). The PR-2 update guard and evidence triggers now fire only when
  ``incident_id`` is unchanged, so every earlier edge keeps its rules.

Grants: the web may SELECT and INSERT the link's business columns and UPDATE
``incident_id``; the Observer and the worker get nothing new.

``downgrade()`` refuses while a link exists or a release names an incident.
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0021_release_incident_links"
down_revision: str | Sequence[str] | None = "0020_release_comparisons"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen: equal to ``RUNTIME_GRANTS`` in ``opspilot.persistence.grants``.
LINK_INSERT = "anomaly_key,comparison_id,disposition,ending_id,handoff_reason,incident_id,input_id,observation_id,related_incident_id,run_id"
DISPOSITIONS = ("created", "attached", "created_related", "handoff_created")
_UNCHANGED = "WHEN (OLD.incident_id IS NOT DISTINCT FROM NEW.incident_id)"
_CHANGED = "WHEN (OLD.incident_id IS DISTINCT FROM NEW.incident_id)"


def upgrade() -> None:
    dispositions = ", ".join(f"'{d}'" for d in DISPOSITIONS)
    op.execute(f"""
    CREATE TABLE opspilot_release_incident_links (
      observation_id uuid PRIMARY KEY REFERENCES opspilot_release_observations,
      ending_id uuid NOT NULL UNIQUE REFERENCES opspilot_observation_endings,
      anomaly_key text NOT NULL UNIQUE,
      disposition text NOT NULL CHECK (disposition IN ({dispositions})),
      incident_id uuid NOT NULL REFERENCES opspilot_incidents,
      related_incident_id uuid REFERENCES opspilot_incidents,
      run_id uuid REFERENCES opspilot_runs,
      input_id uuid REFERENCES opspilot_inputs,
      handoff_reason text CHECK (handoff_reason IS NULL OR handoff_reason <> ''),
      comparison_id uuid REFERENCES opspilot_release_comparisons,
      consumed_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
      CHECK (anomaly_key = 'release-anomaly:' || observation_id::text),
      CHECK ((disposition = 'created_related') = (related_incident_id IS NOT NULL)),
      CHECK ((disposition IN ('created','created_related')) = (run_id IS NOT NULL)),
      CHECK ((disposition = 'attached') = (input_id IS NOT NULL)),
      CHECK ((disposition = 'handoff_created') = (handoff_reason IS NOT NULL))
    );
    CREATE INDEX release_links_incident ON opspilot_release_incident_links(incident_id);
    CREATE FUNCTION opspilot_release_link_guard() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
    DECLARE r record; i record;
    BEGIN
      NEW.consumed_at := transaction_timestamp();
      SELECT status,incident_id,target_id INTO r FROM public.opspilot_release_observations WHERE observation_id=NEW.observation_id;
      IF NOT FOUND OR r.status<>'anomalous' OR r.incident_id IS NOT NULL
        OR NOT EXISTS(SELECT 1 FROM public.opspilot_observation_endings e WHERE e.ending_id=NEW.ending_id AND e.release_observation_id=NEW.observation_id
          AND e.ended_reason='anomaly_detected' AND e.anomaly_key=NEW.anomaly_key)
        OR (NEW.comparison_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM public.opspilot_release_comparisons c WHERE c.comparison_id=NEW.comparison_id AND c.ending_id=NEW.ending_id))
      THEN RAISE insufficient_privilege USING MESSAGE='release link ending refused'; END IF;
      SELECT intake_key,current_run_id,target_id,related_incident_id INTO i FROM public.opspilot_incidents WHERE incident_id=NEW.incident_id;
      IF NOT FOUND
        OR (NEW.disposition<>'attached' AND (i.intake_key IS DISTINCT FROM 'intake:' || NEW.anomaly_key
          OR i.related_incident_id IS DISTINCT FROM NEW.related_incident_id OR i.current_run_id IS DISTINCT FROM NEW.run_id))
        OR (NEW.disposition='handoff_created' AND i.target_id IS NOT NULL)
        OR (NEW.disposition<>'handoff_created' AND i.target_id IS DISTINCT FROM r.target_id)
        OR (NEW.disposition='attached' AND NOT EXISTS(SELECT 1 FROM public.opspilot_inputs n WHERE n.input_id=NEW.input_id AND n.incident_id=NEW.incident_id AND n.kind='event'))
      THEN RAISE insufficient_privilege USING MESSAGE='release link incident refused'; END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER release_link_guard BEFORE INSERT ON opspilot_release_incident_links FOR EACH ROW EXECUTE FUNCTION opspilot_release_link_guard();
    CREATE FUNCTION opspilot_release_link_evidence() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$ BEGIN
      IF NOT EXISTS(SELECT 1 FROM public.opspilot_release_observations r WHERE r.observation_id=NEW.observation_id AND r.incident_id=NEW.incident_id)
      THEN RAISE insufficient_privilege USING MESSAGE='release link without release incident refused'; END IF; RETURN NULL;
    END $$;
    CREATE CONSTRAINT TRIGGER release_link_evidence AFTER INSERT ON opspilot_release_incident_links DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION opspilot_release_link_evidence();
    CREATE TRIGGER release_link_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON opspilot_release_incident_links FOR EACH STATEMENT EXECUTE FUNCTION opspilot_release_immutable_guard();

    CREATE FUNCTION opspilot_release_incident_guard() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$ BEGIN
      IF OLD.status<>'anomalous' OR OLD.incident_id IS NOT NULL OR NEW.incident_id IS NULL
        OR (to_jsonb(NEW)-ARRAY['incident_id','updated_at']) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['incident_id','updated_at'])
      THEN RAISE insufficient_privilege USING MESSAGE='release incident link refused'; END IF;
      IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname=current_user) AND current_user<>(SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid=TG_RELID)
        AND NOT pg_has_role(current_user,'opspilot_web','MEMBER')
      THEN RAISE insufficient_privilege USING MESSAGE='release incident link role refused'; END IF;
      NEW.updated_at := clock_timestamp();
      RETURN NEW;
    END $$;
    CREATE FUNCTION opspilot_release_incident_evidence() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$ BEGIN
      IF NOT EXISTS(SELECT 1 FROM public.opspilot_release_incident_links l WHERE l.observation_id=NEW.observation_id AND l.incident_id=NEW.incident_id AND l.xmin=pg_current_xact_id()::xid)
      THEN RAISE insufficient_privilege USING MESSAGE='release incident link evidence refused'; END IF; RETURN NULL;
    END $$;
    DROP TRIGGER release_update_guard ON opspilot_release_observations;
    DROP TRIGGER release_update_evidence ON opspilot_release_observations;
    CREATE TRIGGER release_update_guard BEFORE UPDATE ON opspilot_release_observations FOR EACH ROW {_UNCHANGED} EXECUTE FUNCTION opspilot_release_update_guard();
    CREATE CONSTRAINT TRIGGER release_update_evidence AFTER UPDATE ON opspilot_release_observations DEFERRABLE INITIALLY DEFERRED FOR EACH ROW {_UNCHANGED} EXECUTE FUNCTION opspilot_release_update_evidence();
    CREATE TRIGGER release_incident_guard BEFORE UPDATE ON opspilot_release_observations FOR EACH ROW {_CHANGED} EXECUTE FUNCTION opspilot_release_incident_guard();
    CREATE CONSTRAINT TRIGGER release_incident_evidence AFTER UPDATE ON opspilot_release_observations DEFERRABLE INITIALLY DEFERRED FOR EACH ROW {_CHANGED} EXECUTE FUNCTION opspilot_release_incident_evidence();
    GRANT SELECT ON opspilot_release_incident_links TO opspilot_web;
    GRANT INSERT({LINK_INSERT}) ON opspilot_release_incident_links TO opspilot_web;
    GRANT UPDATE(incident_id) ON opspilot_release_observations TO opspilot_web;
    """)


def downgrade() -> None:
    op.execute(
        "LOCK TABLE opspilot_release_observations, opspilot_release_incident_links IN ACCESS EXCLUSIVE MODE"
    )
    count = int(
        op.get_bind()
        .execute(
            text(
                "SELECT (SELECT count(*) FROM opspilot_release_incident_links) + (SELECT count(*) FROM opspilot_release_observations WHERE incident_id IS NOT NULL)"
            )
        )
        .scalar_one()
    )
    if count:
        raise RuntimeError(
            f"{count} release incident link(s) or linked release(s); the schema "
            "before 0021 cannot represent them, refusing to downgrade"
        )
    op.execute("""
    REVOKE UPDATE(incident_id) ON opspilot_release_observations FROM opspilot_web;
    DROP TRIGGER release_incident_evidence ON opspilot_release_observations;
    DROP TRIGGER release_incident_guard ON opspilot_release_observations;
    DROP TRIGGER release_update_evidence ON opspilot_release_observations;
    DROP TRIGGER release_update_guard ON opspilot_release_observations;
    CREATE TRIGGER release_update_guard BEFORE UPDATE ON opspilot_release_observations FOR EACH ROW EXECUTE FUNCTION opspilot_release_update_guard();
    CREATE CONSTRAINT TRIGGER release_update_evidence AFTER UPDATE ON opspilot_release_observations DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION opspilot_release_update_evidence();
    DROP FUNCTION opspilot_release_incident_evidence();
    DROP FUNCTION opspilot_release_incident_guard();
    DROP TABLE opspilot_release_incident_links;
    DROP FUNCTION opspilot_release_link_evidence();
    DROP FUNCTION opspilot_release_link_guard();
    """)
