"""Release observation authority, neutral subject bindings and durable evidence."""

from collections.abc import Sequence

from alembic import op

revision = "0015_release_observation_sessions"
down_revision = "0014_release_observations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

WEB_RELEASE_UPDATE = "status,paused,control_generation,observation_generation,ended_reason,ended_at,updated_at,calibrated_policy_revision,calibrated_policy_content,calibrated_policy_sha256"
OBSERVER_RELEASE_UPDATE = "status,used_sample_count,ended_reason,ended_at,updated_at"
SESSION_INSERT = "release_observation_id,release_policy_revision,release_budget_used_at_authorization"
SAMPLE_INSERT = "current_subject_state,current_subject_control_generation,current_subject_observation_generation,decision_at,release_evidence_sha256,release_evaluation,release_receipt"
ENDING_INSERT = (
    "release_observation_id,subject_state_before,subject_state_after,anomaly_key"
)
QUERY_INSERT = "sample_id,query_key,query,source,evaluated_at,sent_at,status,http_status,detail,body_complete,raw,raw_sha256"
AUDIT_INSERT = "audit_id,observation_id,sequence,idempotency_key,request_sha256,action,principal_kind,actor,outcome,reason,expected_control_generation,control_generation_before,control_generation_after,observation_generation_before,observation_generation_after,status_before,status_after,paused_before,paused_after,session_id,ending_id,result"


def upgrade() -> None:
    op.execute("""
    ALTER TABLE alembic_version ALTER COLUMN version_num TYPE varchar(64);
    DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM opspilot_observation_sessions WHERE purpose <> 'incident_recovery')
      THEN RAISE EXCEPTION 'LEGACY_RELEASE_SESSION_UNBOUND'; END IF;
      IF EXISTS(SELECT 1 FROM opspilot_observation_endings e JOIN opspilot_observation_sessions s ON s.session_id=e.session_id WHERE e.incident_id IS DISTINCT FROM s.incident_id) THEN RAISE EXCEPTION 'LEGACY_OBSERVATION_SUBJECT_MISMATCH'; END IF;
    END $$;
    ALTER TABLE opspilot_release_observations
      ADD COLUMN used_sample_count integer NOT NULL DEFAULT 0 CHECK (used_sample_count BETWEEN 0 AND max_samples),
      ADD COLUMN calibrated_policy_revision text,
      ADD COLUMN calibrated_policy_content text,
      ADD COLUMN calibrated_policy_sha256 text,
      ADD CONSTRAINT release_policy_snapshot CHECK (
        (calibrated_policy_revision IS NULL AND calibrated_policy_content IS NULL AND calibrated_policy_sha256 IS NULL)
        OR (calibrated_policy_revision IS NOT NULL AND calibrated_policy_content IS NOT NULL
          AND calibrated_policy_sha256 IS NOT NULL AND calibrated_policy_sha256 ~ '^[0-9a-f]{64}$'
          AND encode(sha256(convert_to(calibrated_policy_content,'UTF8')),'hex') = calibrated_policy_sha256
          AND right(calibrated_policy_revision,13) = '@' || left(calibrated_policy_sha256,12))),
      ADD CONSTRAINT release_terminal_fields CHECK (
        (status IN ('pending','observing') AND ended_reason IS NULL AND ended_at IS NULL)
        OR (status IN ('healthy','anomalous','unknown','cancelled') AND ended_reason IS NOT NULL AND ended_at IS NOT NULL));
    ALTER TABLE opspilot_observation_sessions
      ALTER COLUMN incident_id DROP NOT NULL,
      ADD COLUMN release_observation_id uuid REFERENCES opspilot_release_observations,
      ADD COLUMN subject_kind text,
      ADD COLUMN subject_id uuid,
      ADD COLUMN release_policy_revision text,
      ADD COLUMN release_budget_used_at_authorization integer,
      ADD COLUMN tracking_since timestamptz,
      ADD COLUMN tracking_until timestamptz;
    UPDATE opspilot_observation_sessions SET subject_kind='incident',subject_id=incident_id;
    ALTER TABLE opspilot_observation_sessions
      ALTER COLUMN subject_kind SET NOT NULL, ALTER COLUMN subject_id SET NOT NULL,
      ADD CONSTRAINT sessions_subject CHECK (
        (purpose='incident_recovery' AND incident_id IS NOT NULL AND release_observation_id IS NULL AND subject_kind='incident' AND subject_id=incident_id AND release_policy_revision IS NULL AND release_budget_used_at_authorization IS NULL AND tracking_since IS NULL AND tracking_until IS NULL)
        OR (purpose='release_observation' AND incident_id IS NULL AND release_observation_id IS NOT NULL AND subject_kind='release_observation' AND subject_id=release_observation_id AND release_policy_revision IS NOT NULL AND release_budget_used_at_authorization >= 0)),
      ADD CONSTRAINT sessions_tracking CHECK ((tracking_since IS NULL AND tracking_until IS NULL) OR (tracking_since IS NOT NULL AND tracking_until IS NOT NULL AND tracking_since<=tracking_until)),
      ADD CONSTRAINT sessions_subject_key UNIQUE(session_id,purpose,subject_kind,subject_id);
    CREATE UNIQUE INDEX sessions_release_authorized ON opspilot_observation_sessions(release_observation_id) WHERE state='authorized';
    ALTER TABLE opspilot_observation_sessions DROP CONSTRAINT opspilot_observation_sessions_ended_reason_check;
    ALTER TABLE opspilot_observation_sessions ADD CONSTRAINT sessions_reason CHECK (
      ended_reason IS NULL OR CASE purpose WHEN 'incident_recovery' THEN ended_reason IN ('recovery_confirmed','deadline_expired','max_samples_exhausted','authority_revoked','binding_stale','scope_suspended')
      ELSE ended_reason IN ('healthy_confirmed','anomaly_detected','deadline_expired','max_samples_exhausted','authority_revoked','binding_stale','scope_suspended','cancelled') END);
    ALTER TABLE opspilot_observation_samples
      ALTER COLUMN subject_lifecycle DROP NOT NULL,
      ALTER COLUMN incident_control_generation DROP NOT NULL,
      ALTER COLUMN incident_observation_generation DROP NOT NULL,
      ADD COLUMN purpose text, ADD COLUMN subject_kind text, ADD COLUMN subject_id uuid,
      ADD COLUMN current_subject_state text,
      ADD COLUMN current_subject_control_generation integer,
      ADD COLUMN current_subject_observation_generation integer,
      ADD COLUMN decision_at timestamptz,
      ADD COLUMN release_evidence_sha256 text,
      ADD COLUMN release_evaluation jsonb, ADD COLUMN release_receipt jsonb;
    UPDATE opspilot_observation_samples m SET purpose=s.purpose,subject_kind=s.subject_kind,subject_id=s.subject_id,
      current_subject_state=m.subject_lifecycle,current_subject_control_generation=m.incident_control_generation,current_subject_observation_generation=m.incident_observation_generation
      FROM opspilot_observation_sessions s WHERE s.session_id=m.session_id;
    ALTER TABLE opspilot_observation_samples
      ALTER COLUMN purpose SET NOT NULL, ALTER COLUMN subject_kind SET NOT NULL, ALTER COLUMN subject_id SET NOT NULL,
      ALTER COLUMN current_subject_state SET NOT NULL, ALTER COLUMN current_subject_control_generation SET NOT NULL, ALTER COLUMN current_subject_observation_generation SET NOT NULL,
      ADD CONSTRAINT samples_subject FOREIGN KEY(session_id,purpose,subject_kind,subject_id) REFERENCES opspilot_observation_sessions(session_id,purpose,subject_kind,subject_id),
      ADD CONSTRAINT samples_neutral CHECK (CASE purpose WHEN 'incident_recovery' THEN
        subject_lifecycle IS NOT NULL AND incident_control_generation IS NOT NULL AND incident_observation_generation IS NOT NULL
        AND current_subject_state=subject_lifecycle AND current_subject_control_generation=incident_control_generation AND current_subject_observation_generation=incident_observation_generation
        AND decision_at IS NULL AND release_evidence_sha256 IS NULL AND release_evaluation IS NULL AND release_receipt IS NULL
      ELSE subject_lifecycle IS NULL AND incident_control_generation IS NULL AND incident_observation_generation IS NULL
        AND current_subject_state IN ('pending','observing','healthy','anomalous','unknown','cancelled') AND decision_at IS NOT NULL
        AND release_evidence_sha256 ~ '^[0-9a-f]{64}$' AND release_evidence_sha256 IS NOT NULL AND release_evaluation IS NOT NULL AND release_receipt IS NOT NULL END);
    ALTER TABLE opspilot_observation_samples DROP CONSTRAINT opspilot_observation_samples_transition_check;
    ALTER TABLE opspilot_observation_samples ADD CONSTRAINT samples_transition CHECK (transition IS NULL OR CASE purpose WHEN 'incident_recovery' THEN transition IN ('recovery_confirmed','observation_ended_unconfirmed') ELSE transition IN ('release_healthy','release_anomalous','release_unknown') END);
    ALTER TABLE opspilot_observation_endings
      ALTER COLUMN incident_id DROP NOT NULL, ALTER COLUMN lifecycle_before DROP NOT NULL, ALTER COLUMN lifecycle_after DROP NOT NULL,
      ADD COLUMN release_observation_id uuid REFERENCES opspilot_release_observations,
      ADD COLUMN purpose text, ADD COLUMN subject_kind text, ADD COLUMN subject_id uuid,
      ADD COLUMN subject_state_before text, ADD COLUMN subject_state_after text, ADD COLUMN anomaly_key text;
    UPDATE opspilot_observation_endings e SET purpose=s.purpose,subject_kind=s.subject_kind,subject_id=s.subject_id,subject_state_before=e.lifecycle_before,subject_state_after=e.lifecycle_after FROM opspilot_observation_sessions s WHERE s.session_id=e.session_id;
    ALTER TABLE opspilot_observation_endings
      ALTER COLUMN purpose SET NOT NULL, ALTER COLUMN subject_kind SET NOT NULL, ALTER COLUMN subject_id SET NOT NULL,
      ALTER COLUMN subject_state_before SET NOT NULL, ALTER COLUMN subject_state_after SET NOT NULL,
      ADD CONSTRAINT endings_subject FOREIGN KEY(session_id,purpose,subject_kind,subject_id) REFERENCES opspilot_observation_sessions(session_id,purpose,subject_kind,subject_id),
      ADD CONSTRAINT endings_neutral CHECK (CASE purpose WHEN 'incident_recovery' THEN
        incident_id IS NOT NULL AND release_observation_id IS NULL AND lifecycle_before IS NOT NULL AND lifecycle_after IS NOT NULL AND subject_state_before=lifecycle_before AND subject_state_after=lifecycle_after AND anomaly_key IS NULL
      ELSE incident_id IS NULL AND release_observation_id=subject_id AND release_observation_id IS NOT NULL AND lifecycle_before IS NULL AND lifecycle_after IS NULL
        AND subject_state_before IN ('pending','observing','healthy','anomalous','unknown','cancelled') AND subject_state_after IN ('pending','observing','healthy','anomalous','unknown','cancelled') END);
    ALTER TABLE opspilot_observation_endings DROP CONSTRAINT opspilot_observation_endings_ended_reason_check, DROP CONSTRAINT opspilot_observation_endings_transition_check;
    ALTER TABLE opspilot_observation_endings ADD CONSTRAINT endings_reason CHECK (CASE purpose WHEN 'incident_recovery' THEN ended_reason IN ('recovery_confirmed','deadline_expired','max_samples_exhausted','authority_revoked','binding_stale','scope_suspended') ELSE ended_reason IN ('healthy_confirmed','anomaly_detected','deadline_expired','max_samples_exhausted','authority_revoked','binding_stale','scope_suspended','cancelled') END),
      ADD CONSTRAINT endings_transition CHECK (transition IS NULL OR CASE purpose WHEN 'incident_recovery' THEN transition IN ('recovery_confirmed','observation_ended_unconfirmed') ELSE transition IN ('release_healthy','release_anomalous','release_unknown') END),
      ADD CONSTRAINT endings_anomaly CHECK ((anomaly_key IS NOT NULL) = (purpose='release_observation' AND ended_reason='anomaly_detected') AND (anomaly_key IS NULL OR anomaly_key='release-anomaly:' || subject_id::text));
    CREATE UNIQUE INDEX endings_release_session ON opspilot_observation_endings(session_id) WHERE purpose='release_observation';
    CREATE UNIQUE INDEX endings_anomaly_key ON opspilot_observation_endings(anomaly_key) WHERE anomaly_key IS NOT NULL;
    CREATE TABLE opspilot_release_query_results (
      sample_id uuid NOT NULL REFERENCES opspilot_observation_samples,
      query_key text NOT NULL CHECK (query_key<>''), query text NOT NULL CHECK (query<>''), source text NOT NULL CHECK(source='prometheus'),
      evaluated_at timestamptz NOT NULL, sent_at timestamptz,
      status text NOT NULL CHECK(status IN ('ok','no_data','timeout','failed')), http_status integer CHECK(http_status>=0),
      detail text NOT NULL, body_complete boolean NOT NULL, raw bytea NOT NULL, raw_sha256 text NOT NULL,
      PRIMARY KEY(sample_id,query_key), CHECK(octet_length(raw)<=30720 AND raw_sha256=encode(sha256(raw),'hex'))
    );
    CREATE TABLE opspilot_release_audit (
      audit_id uuid PRIMARY KEY, observation_id uuid NOT NULL REFERENCES opspilot_release_observations,
      sequence integer NOT NULL CHECK(sequence>0), idempotency_key text NOT NULL CHECK(length(idempotency_key) BETWEEN 1 AND 256),
      request_sha256 text NOT NULL CHECK(request_sha256 ~ '^[0-9a-f]{64}$'),
      action text NOT NULL CHECK(action IN ('authorize','pause','resume','cancel','deadline_expired')),
      principal_kind text NOT NULL CHECK(principal_kind IN ('system','basic_auth','observer')), actor text NOT NULL CHECK(actor<>''),
      outcome text NOT NULL CHECK(outcome IN ('applied','rejected')), reason text,
      expected_control_generation integer NOT NULL CHECK(expected_control_generation>=0),
      control_generation_before integer NOT NULL, control_generation_after integer NOT NULL,
      observation_generation_before integer NOT NULL, observation_generation_after integer NOT NULL,
      status_before text NOT NULL,status_after text NOT NULL,paused_before boolean NOT NULL,paused_after boolean NOT NULL,
      session_id uuid REFERENCES opspilot_observation_sessions,ending_id uuid REFERENCES opspilot_observation_endings,
      result jsonb NOT NULL,recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      UNIQUE(observation_id,sequence),UNIQUE(observation_id,idempotency_key),
      CHECK ((outcome='rejected' AND reason IS NOT NULL AND control_generation_before=control_generation_after AND observation_generation_before=observation_generation_after AND status_before=status_after AND paused_before=paused_after) OR outcome='applied'),
      CHECK ((action='authorize' AND principal_kind='system') OR (action IN ('pause','resume','cancel') AND principal_kind='basic_auth') OR (action='deadline_expired' AND principal_kind='observer' AND actor='observer' AND idempotency_key='system:deadline'))
    );
    CREATE FUNCTION opspilot_observation_subject_fill() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
    DECLARE s record;
    BEGIN
      IF TG_TABLE_NAME='opspilot_observation_sessions' THEN
        IF NEW.purpose='release_observation' AND NEW.authorization_source IS NOT NULL THEN RAISE check_violation USING MESSAGE='release authorization_source must be NULL'; END IF;
        NEW.subject_kind := COALESCE(NEW.subject_kind,CASE NEW.purpose WHEN 'incident_recovery' THEN 'incident' ELSE 'release_observation' END);
        NEW.subject_id := COALESCE(NEW.subject_id,CASE NEW.purpose WHEN 'incident_recovery' THEN NEW.incident_id ELSE NEW.release_observation_id END);
      ELSE
        SELECT purpose,subject_kind,subject_id INTO STRICT s FROM public.opspilot_observation_sessions WHERE session_id=NEW.session_id;
        NEW.purpose:=COALESCE(NEW.purpose,s.purpose); NEW.subject_kind:=COALESCE(NEW.subject_kind,s.subject_kind); NEW.subject_id:=COALESCE(NEW.subject_id,s.subject_id);
        IF NEW.purpose='incident_recovery' THEN
          IF TG_TABLE_NAME='opspilot_observation_samples' THEN
            NEW.current_subject_state:=COALESCE(NEW.current_subject_state,NEW.subject_lifecycle);
            NEW.current_subject_control_generation:=COALESCE(NEW.current_subject_control_generation,NEW.incident_control_generation);
            NEW.current_subject_observation_generation:=COALESCE(NEW.current_subject_observation_generation,NEW.incident_observation_generation);
          ELSE
            NEW.subject_state_before:=COALESCE(NEW.subject_state_before,NEW.lifecycle_before); NEW.subject_state_after:=COALESCE(NEW.subject_state_after,NEW.lifecycle_after);
          END IF;
        END IF;
      END IF; RETURN NEW;
    END $$;
    CREATE TRIGGER sessions_subject_fill BEFORE INSERT ON opspilot_observation_sessions FOR EACH ROW EXECUTE FUNCTION opspilot_observation_subject_fill();
    CREATE TRIGGER samples_subject_fill BEFORE INSERT ON opspilot_observation_samples FOR EACH ROW EXECUTE FUNCTION opspilot_observation_subject_fill();
    CREATE TRIGGER endings_subject_fill BEFORE INSERT ON opspilot_observation_endings FOR EACH ROW EXECUTE FUNCTION opspilot_observation_subject_fill();
    CREATE FUNCTION opspilot_release_query_guard() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$ BEGIN
      IF NOT EXISTS(SELECT 1 FROM public.opspilot_observation_samples WHERE sample_id=NEW.sample_id AND purpose='release_observation') THEN RAISE insufficient_privilege USING MESSAGE='release query subject refused'; END IF; RETURN NEW;
    END $$;
    CREATE TRIGGER release_query_guard BEFORE INSERT ON opspilot_release_query_results FOR EACH ROW EXECUTE FUNCTION opspilot_release_query_guard();
    CREATE TRIGGER release_query_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON opspilot_release_query_results FOR EACH STATEMENT EXECUTE FUNCTION opspilot_release_immutable_guard();
    CREATE TRIGGER release_audit_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON opspilot_release_audit FOR EACH STATEMENT EXECUTE FUNCTION opspilot_release_immutable_guard();
    DROP TRIGGER opspilot_release_observations_immutable_guard ON opspilot_release_observations;
    CREATE TRIGGER opspilot_release_observations_immutable_guard BEFORE DELETE OR TRUNCATE ON opspilot_release_observations FOR EACH STATEMENT EXECUTE FUNCTION opspilot_release_immutable_guard();
    """)
    op.execute(INCIDENT_EVIDENCE_0015)
    _guards()
    op.execute(f"""
    GRANT UPDATE({WEB_RELEASE_UPDATE}) ON opspilot_release_observations TO opspilot_web;
    GRANT SELECT ON opspilot_release_observations,opspilot_release_query_results,opspilot_release_audit TO opspilot_observer;
    GRANT UPDATE({OBSERVER_RELEASE_UPDATE}) ON opspilot_release_observations TO opspilot_observer;
    GRANT INSERT({SESSION_INSERT}) ON opspilot_observation_sessions TO opspilot_web;
    GRANT UPDATE(tracking_since,tracking_until) ON opspilot_observation_sessions TO opspilot_observer;
    GRANT INSERT({SAMPLE_INSERT}) ON opspilot_observation_samples TO opspilot_observer;
    GRANT INSERT({ENDING_INSERT}) ON opspilot_observation_endings TO opspilot_web,opspilot_observer;
    GRANT SELECT ON opspilot_release_query_results,opspilot_release_audit TO opspilot_web;
    GRANT INSERT({QUERY_INSERT}) ON opspilot_release_query_results TO opspilot_observer;
    GRANT INSERT({AUDIT_INSERT}) ON opspilot_release_audit TO opspilot_web,opspilot_observer;
    """)


def _guards() -> None:
    op.execute("""
    CREATE FUNCTION opspilot_release_session_guard() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
    DECLARE r record;
    BEGIN
      IF NEW.purpose='release_observation' AND NEW.release_observation_id IS NOT NULL THEN
        IF TG_OP='INSERT' THEN NEW.authorized_at:=clock_timestamp(); NEW.active_sample_due_at:=NEW.authorized_at+NEW.sample_interval_seconds*interval '1 second'; IF NEW.authorized_at>=NEW.deadline_at THEN RAISE insufficient_privilege USING MESSAGE='release authorization deadline refused'; END IF; END IF;
        IF TG_OP='UPDATE' AND OLD.state<>'authorized' AND ROW(NEW.state,NEW.ended_reason) IS DISTINCT FROM ROW(OLD.state,OLD.ended_reason) THEN RAISE insufficient_privilege USING MESSAGE='release session terminal refused'; END IF;
        SELECT * INTO STRICT r FROM public.opspilot_release_observations WHERE observation_id=NEW.release_observation_id;
        IF TG_OP='UPDATE' AND (to_jsonb(NEW)-ARRAY['state','ended_reason','adopted_sequence','adopted_window_end','adopted_count','healthy_since','tracking_since','tracking_until','issued_sequence','active_sample_job_id','active_sample_sequence','active_sample_due_at','active_sample_owner','active_sample_epoch','active_sample_lease_until','updated_at']) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['state','ended_reason','adopted_sequence','adopted_window_end','adopted_count','healthy_since','tracking_since','tracking_until','issued_sequence','active_sample_job_id','active_sample_sequence','active_sample_due_at','active_sample_owner','active_sample_epoch','active_sample_lease_until','updated_at'])
        THEN RAISE insufficient_privilege USING MESSAGE='release session authorization frozen'; END IF;
        IF NEW.target_id<>r.target_id OR NEW.target IS DISTINCT FROM (r.target_snapshot-'workload') OR NEW.health_profile_revision<>r.health_profile_revision OR NEW.deadline_at<>r.deadline_at OR NEW.max_samples<>r.max_samples OR NEW.sample_interval_seconds<>r.sample_interval_seconds OR NEW.sustained_window_seconds<>r.sustained_window_seconds
        THEN RAISE insufficient_privilege USING MESSAGE='release session policy refused'; END IF;
      END IF; RETURN NEW;
    END $$;
    CREATE TRIGGER release_session_guard BEFORE INSERT OR UPDATE ON opspilot_observation_sessions FOR EACH ROW EXECUTE FUNCTION opspilot_release_session_guard();
    CREATE FUNCTION opspilot_release_session_evidence() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$ BEGIN
      IF NEW.purpose='release_observation' AND NOT EXISTS(SELECT 1 FROM public.opspilot_release_audit a JOIN public.opspilot_release_observations r ON r.observation_id=a.observation_id
        WHERE a.session_id=NEW.session_id AND a.observation_id=NEW.release_observation_id AND a.action IN ('authorize','resume') AND a.outcome='applied' AND a.xmin=pg_current_xact_id()::xid AND r.calibrated_policy_revision=NEW.release_policy_revision AND r.used_sample_count=NEW.release_budget_used_at_authorization AND r.control_generation=NEW.subject_control_generation AND r.observation_generation=NEW.observation_generation)
      THEN RAISE insufficient_privilege USING MESSAGE='release session authority evidence refused'; END IF; RETURN NULL;
    END $$;
    CREATE CONSTRAINT TRIGGER release_session_evidence AFTER INSERT ON opspilot_observation_sessions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION opspilot_release_session_evidence();

    CREATE FUNCTION opspilot_release_update_guard() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
    DECLARE observer boolean; web boolean;
    BEGIN
      IF (to_jsonb(NEW)-ARRAY['status','paused','control_generation','observation_generation','ended_reason','ended_at','updated_at','used_sample_count','calibrated_policy_revision','calibrated_policy_content','calibrated_policy_sha256']) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['status','paused','control_generation','observation_generation','ended_reason','ended_at','updated_at','used_sample_count','calibrated_policy_revision','calibrated_policy_content','calibrated_policy_sha256'])
        OR OLD.status NOT IN ('pending','observing')
        OR (OLD.calibrated_policy_revision IS NOT NULL AND ROW(NEW.calibrated_policy_revision,NEW.calibrated_policy_content,NEW.calibrated_policy_sha256) IS DISTINCT FROM ROW(OLD.calibrated_policy_revision,OLD.calibrated_policy_content,OLD.calibrated_policy_sha256))
        OR (OLD.calibrated_policy_revision IS NULL AND NEW.calibrated_policy_revision IS NOT NULL AND (OLD.status<>'pending' OR NEW.status<>'observing'))
      THEN RAISE insufficient_privilege USING MESSAGE='release frozen fields refused'; END IF;
      IF OLD.calibrated_policy_revision IS NULL AND NEW.calibrated_policy_revision IS NOT NULL AND (
        (NEW.calibrated_policy_content::jsonb->>'format_version') IS DISTINCT FROM '1'
        OR (NEW.calibrated_policy_content::jsonb->'target') IS DISTINCT FROM NEW.target_snapshot
        OR (NEW.calibrated_policy_content::jsonb->>'health_profile_revision') IS DISTINCT FROM NEW.health_profile_revision
        OR (NEW.calibrated_policy_content::jsonb->>'min_tracking_seconds') IS DISTINCT FROM NEW.min_tracking_seconds::text
        OR (NEW.calibrated_policy_content::jsonb->>'evaluator') IS DISTINCT FROM 'release-after-v1'
        OR (NEW.calibrated_policy_content::jsonb->>'mapping') IS DISTINCT FROM 'pod-uid-replicaset-image-v1'
        OR NOT EXISTS(SELECT 1 FROM public.opspilot_health_profiles p WHERE p.health_profile_revision=NEW.health_profile_revision AND p.content_sha256=NEW.calibrated_policy_content::jsonb->>'health_profile_sha256'))
      THEN RAISE insufficient_privilege USING MESSAGE='release calibration snapshot refused'; END IF;
      IF NEW.status='observing' AND (NEW.calibrated_policy_revision IS NULL OR NEW.used_sample_count>=NEW.max_samples) THEN RAISE insufficient_privilege USING MESSAGE='release observation authority refused'; END IF;
      IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname=current_user) AND current_user<>(SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid=TG_RELID) THEN
        observer:=pg_has_role(current_user,'opspilot_observer','MEMBER'); web:=pg_has_role(current_user,'opspilot_web','MEMBER');
        IF observer AND (ROW(NEW.paused,NEW.control_generation,NEW.observation_generation,NEW.calibrated_policy_revision,NEW.calibrated_policy_content,NEW.calibrated_policy_sha256) IS DISTINCT FROM ROW(OLD.paused,OLD.control_generation,OLD.observation_generation,OLD.calibrated_policy_revision,OLD.calibrated_policy_content,OLD.calibrated_policy_sha256) OR NOT ((OLD.status='observing' AND NEW.status IN ('observing','healthy','anomalous','unknown')) OR (OLD.status='pending' AND NEW.status='unknown')))
          OR web AND (NEW.status IN ('healthy','anomalous','unknown') OR NEW.used_sample_count<>OLD.used_sample_count)
        THEN RAISE insufficient_privilege USING MESSAGE='release role transition refused'; END IF;
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER release_update_guard BEFORE UPDATE ON opspilot_release_observations FOR EACH ROW EXECUTE FUNCTION opspilot_release_update_guard();
    CREATE FUNCTION opspilot_release_audit_guard() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$ BEGIN
      IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname=current_user) AND current_user<>(SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid=TG_RELID) THEN
        IF pg_has_role(current_user,'opspilot_observer','MEMBER') AND NEW.action<>'deadline_expired'
          OR pg_has_role(current_user,'opspilot_web','MEMBER') AND NEW.action='deadline_expired'
        THEN RAISE insufficient_privilege USING MESSAGE='release audit principal refused'; END IF;
      END IF; RETURN NEW;
    END $$;
    CREATE TRIGGER release_audit_guard BEFORE INSERT ON opspilot_release_audit FOR EACH ROW EXECUTE FUNCTION opspilot_release_audit_guard();
    CREATE FUNCTION opspilot_release_update_evidence() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
    DECLARE valid boolean;
    BEGIN
      IF NEW.used_sample_count<>OLD.used_sample_count AND (NEW.used_sample_count<>OLD.used_sample_count+1 OR NEW.used_sample_count<>(SELECT count(*) FROM public.opspilot_observation_samples m WHERE m.purpose='release_observation' AND m.subject_id=NEW.observation_id AND m.disposition='adopted')) THEN RAISE insufficient_privilege USING MESSAGE='release sample budget evidence refused'; END IF;
      IF NEW.status IN ('healthy','anomalous','unknown') THEN
        valid:=EXISTS(SELECT 1 FROM public.opspilot_observation_endings e JOIN public.opspilot_observation_sessions s ON s.session_id=e.session_id
          LEFT JOIN public.opspilot_observation_samples m ON m.sample_id=e.sample_id
          WHERE e.release_observation_id=NEW.observation_id AND e.purpose='release_observation' AND s.subject_control_generation=NEW.control_generation AND s.observation_generation=NEW.observation_generation
            AND e.xmin=pg_current_xact_id()::xid AND e.subject_state_before=OLD.status AND e.subject_state_after=NEW.status
            AND ((NEW.status IN ('healthy','anomalous') AND e.transition='release_' || NEW.status AND e.ended_reason=CASE NEW.status WHEN 'healthy' THEN 'healthy_confirmed' ELSE 'anomaly_detected' END AND s.state='completed' AND s.ended_reason=e.ended_reason AND m.transition=e.transition AND (NEW.status<>'anomalous' OR e.anomaly_key='release-anomaly:' || NEW.observation_id::text) AND m.xmin=pg_current_xact_id()::xid AND m.session_id=e.session_id AND m.purpose=e.purpose AND m.subject_id=NEW.observation_id AND m.disposition='adopted' AND m.subject_control_generation=NEW.control_generation AND m.observation_generation=NEW.observation_generation AND m.decision_at<NEW.deadline_at AND (m.release_evaluation->>'evidence_sufficient')::boolean AND m.outcome=CASE NEW.status WHEN 'healthy' THEN 'healthy' ELSE 'degraded' END
              AND (NEW.status<>'healthy' OR (m.confirms_health AND s.tracking_since>=s.authorized_at AND s.healthy_since>=s.authorized_at AND s.tracking_until=m.window_end AND m.window_end<=m.decision_at AND m.window_start>=s.authorized_at AND s.tracking_until>=NEW.released_at+NEW.min_tracking_seconds*interval '1 second' AND s.tracking_until-s.tracking_since>=NEW.min_tracking_seconds*interval '1 second' AND s.tracking_until-s.healthy_since>=NEW.sustained_window_seconds*interval '1 second')))
              OR (NEW.status='unknown' AND e.transition='release_unknown' AND ((e.ended_reason='deadline_expired' AND clock_timestamp()>=NEW.deadline_at) OR (e.ended_reason='max_samples_exhausted' AND NEW.used_sample_count=NEW.max_samples AND m.disposition='adopted' AND m.xmin=pg_current_xact_id()::xid)))));
        IF NEW.status='unknown' AND clock_timestamp()>=NEW.deadline_at THEN
          valid:=valid OR EXISTS(SELECT 1 FROM public.opspilot_release_audit a WHERE a.observation_id=NEW.observation_id AND a.action='deadline_expired' AND a.outcome='applied' AND a.xmin=pg_current_xact_id()::xid AND a.status_before=OLD.status AND a.status_after=NEW.status);
        END IF;
      ELSIF ROW(NEW.status,NEW.paused,NEW.control_generation,NEW.observation_generation,NEW.calibrated_policy_revision) IS DISTINCT FROM ROW(OLD.status,OLD.paused,OLD.control_generation,OLD.observation_generation,OLD.calibrated_policy_revision) THEN
        valid:=EXISTS(SELECT 1 FROM public.opspilot_release_audit a WHERE a.observation_id=NEW.observation_id AND a.xmin=pg_current_xact_id()::xid AND a.outcome='applied'
          AND a.expected_control_generation=OLD.control_generation AND a.control_generation_before=OLD.control_generation AND a.control_generation_after=NEW.control_generation AND a.observation_generation_before=OLD.observation_generation AND a.observation_generation_after=NEW.observation_generation AND a.status_before=OLD.status AND a.status_after=NEW.status AND a.paused_before=OLD.paused AND a.paused_after=NEW.paused
          AND ((a.action IN ('authorize','resume') AND NEW.status='observing' AND NOT NEW.paused AND NEW.observation_generation=OLD.observation_generation+1 AND NEW.control_generation=OLD.control_generation+CASE a.action WHEN 'authorize' THEN 0 ELSE 1 END
            AND EXISTS(SELECT 1 FROM public.opspilot_observation_sessions s WHERE s.session_id=a.session_id AND s.release_observation_id=NEW.observation_id AND s.purpose='release_observation' AND s.xmin=pg_current_xact_id()::xid AND s.state='authorized' AND s.active_sample_job_id IS NOT NULL AND s.active_sample_sequence=1 AND s.active_sample_due_at=s.authorized_at+s.sample_interval_seconds*interval '1 second' AND s.release_policy_revision=NEW.calibrated_policy_revision AND s.subject_control_generation=NEW.control_generation AND s.observation_generation=NEW.observation_generation AND s.deadline_at=NEW.deadline_at))
            OR (a.action IN ('pause','cancel') AND NEW.control_generation=OLD.control_generation+1 AND NEW.observation_generation=OLD.observation_generation+1 AND ((a.action='pause' AND NEW.status=OLD.status AND NEW.paused) OR (a.action='cancel' AND NEW.status='cancelled' AND NOT NEW.paused))) )
          AND NOT EXISTS(SELECT 1 FROM public.opspilot_observation_sessions s WHERE s.release_observation_id=NEW.observation_id AND s.state='authorized' AND s.observation_generation<>NEW.observation_generation));
      ELSE
        valid:= NEW.used_sample_count=OLD.used_sample_count OR (NEW.used_sample_count=OLD.used_sample_count+1 AND EXISTS(SELECT 1 FROM public.opspilot_observation_samples m WHERE m.subject_id=NEW.observation_id AND m.purpose='release_observation' AND m.disposition='adopted' AND m.current_subject_control_generation=NEW.control_generation AND m.current_subject_observation_generation=NEW.observation_generation AND m.xmin=pg_current_xact_id()::xid));
      END IF;
      IF valid IS NOT TRUE THEN RAISE insufficient_privilege USING MESSAGE='release transition evidence refused'; END IF;
      RETURN NULL;
    END $$;
    CREATE CONSTRAINT TRIGGER release_update_evidence AFTER UPDATE ON opspilot_release_observations DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION opspilot_release_update_evidence();
    """)

    op.execute(EXTRA_GUARDS)


def downgrade() -> None:
    op.execute(INCIDENT_EVIDENCE_0003)
    op.execute("""
    LOCK TABLE opspilot_release_observations,opspilot_observation_sessions,opspilot_observation_samples,opspilot_observation_endings,opspilot_release_audit,opspilot_release_query_results IN ACCESS EXCLUSIVE MODE;
    DO $$ BEGIN
      IF EXISTS(SELECT 1 FROM opspilot_observation_sessions WHERE purpose='release_observation') OR EXISTS(SELECT 1 FROM opspilot_release_audit) OR EXISTS(SELECT 1 FROM opspilot_release_query_results) OR EXISTS(SELECT 1 FROM opspilot_release_observations WHERE calibrated_policy_revision IS NOT NULL OR used_sample_count<>0 OR control_generation<>0 OR observation_generation<>0 OR status NOT IN ('pending','unknown') OR (status='unknown' AND ended_reason<>'late_delivery')) THEN RAISE EXCEPTION 'RELEASE_OBSERVATION_DATA_WOULD_BE_LOST'; END IF;
    END $$;
    DROP TRIGGER release_session_end_evidence ON opspilot_observation_sessions;
    DROP FUNCTION opspilot_release_session_end_evidence();
    ALTER TABLE opspilot_observation_sessions DROP CONSTRAINT release_session_ended_slot;
    DROP TRIGGER observation_legacy_update_sync ON opspilot_observation_samples;
    DROP TRIGGER observation_legacy_update_sync ON opspilot_observation_endings;
    DROP TRIGGER endings_incident_subject_evidence ON opspilot_observation_endings;
    DROP TRIGGER release_initial_fields_guard ON opspilot_release_observations;
    DROP FUNCTION opspilot_observation_legacy_update_sync();
    DROP FUNCTION opspilot_endings_incident_subject_evidence();
    DROP FUNCTION opspilot_release_initial_fields_guard();
    ALTER TABLE opspilot_observation_sessions DROP CONSTRAINT release_session_states;
    ALTER TABLE opspilot_observation_samples DROP CONSTRAINT release_sample_generations;
    ALTER TABLE opspilot_observation_endings DROP CONSTRAINT release_ending_edges;
    DROP TRIGGER IF EXISTS release_session_evidence ON opspilot_observation_sessions;
    DROP TRIGGER IF EXISTS release_session_guard ON opspilot_observation_sessions;
    DROP FUNCTION IF EXISTS opspilot_release_session_evidence(); DROP FUNCTION IF EXISTS opspilot_release_session_guard();
    DROP TRIGGER release_update_evidence ON opspilot_release_observations;
    DROP TRIGGER release_update_guard ON opspilot_release_observations;
    DROP FUNCTION opspilot_release_update_evidence(); DROP FUNCTION opspilot_release_update_guard();
    DROP TABLE opspilot_release_audit; DROP TABLE opspilot_release_query_results;
    DROP FUNCTION opspilot_release_audit_guard(); DROP FUNCTION opspilot_release_query_guard();
    DROP TRIGGER sessions_subject_fill ON opspilot_observation_sessions;
    DROP TRIGGER samples_subject_fill ON opspilot_observation_samples;
    DROP TRIGGER endings_subject_fill ON opspilot_observation_endings;
    DROP FUNCTION opspilot_observation_subject_fill();
    DROP INDEX sessions_release_authorized; DROP INDEX endings_release_session; DROP INDEX endings_anomaly_key;
    ALTER TABLE opspilot_observation_samples DROP CONSTRAINT samples_subject;
    ALTER TABLE opspilot_observation_endings DROP CONSTRAINT endings_subject;
    """)
    op.execute(f"""
    REVOKE UPDATE({WEB_RELEASE_UPDATE}) ON opspilot_release_observations FROM opspilot_web;
    REVOKE UPDATE({OBSERVER_RELEASE_UPDATE}), SELECT ON opspilot_release_observations FROM opspilot_observer;
    REVOKE INSERT({SESSION_INSERT}) ON opspilot_observation_sessions FROM opspilot_web;
    REVOKE UPDATE(tracking_since,tracking_until) ON opspilot_observation_sessions FROM opspilot_observer;
    REVOKE INSERT({SAMPLE_INSERT}) ON opspilot_observation_samples FROM opspilot_observer;
    REVOKE INSERT({ENDING_INSERT}) ON opspilot_observation_endings FROM opspilot_web,opspilot_observer;
    ALTER TABLE opspilot_release_observations DROP CONSTRAINT release_policy_snapshot,DROP CONSTRAINT release_terminal_fields,DROP COLUMN used_sample_count,DROP COLUMN calibrated_policy_revision,DROP COLUMN calibrated_policy_content,DROP COLUMN calibrated_policy_sha256;
    ALTER TABLE opspilot_observation_sessions DROP CONSTRAINT sessions_subject,DROP CONSTRAINT sessions_tracking,DROP CONSTRAINT sessions_subject_key,DROP CONSTRAINT sessions_reason,
      DROP COLUMN release_observation_id,DROP COLUMN subject_kind,DROP COLUMN subject_id,DROP COLUMN release_policy_revision,DROP COLUMN release_budget_used_at_authorization,DROP COLUMN tracking_since,DROP COLUMN tracking_until,ALTER COLUMN incident_id SET NOT NULL;
    ALTER TABLE opspilot_observation_samples DROP CONSTRAINT samples_neutral,DROP CONSTRAINT samples_transition,
      DROP COLUMN purpose,DROP COLUMN subject_kind,DROP COLUMN subject_id,DROP COLUMN current_subject_state,DROP COLUMN current_subject_control_generation,DROP COLUMN current_subject_observation_generation,DROP COLUMN decision_at,DROP COLUMN release_evidence_sha256,DROP COLUMN release_evaluation,DROP COLUMN release_receipt,
      ALTER COLUMN subject_lifecycle SET NOT NULL,ALTER COLUMN incident_control_generation SET NOT NULL,ALTER COLUMN incident_observation_generation SET NOT NULL;
    ALTER TABLE opspilot_observation_endings DROP CONSTRAINT endings_neutral,DROP CONSTRAINT endings_reason,DROP CONSTRAINT endings_transition,DROP CONSTRAINT endings_anomaly,
      DROP COLUMN release_observation_id,DROP COLUMN purpose,DROP COLUMN subject_kind,DROP COLUMN subject_id,DROP COLUMN subject_state_before,DROP COLUMN subject_state_after,DROP COLUMN anomaly_key,
      ALTER COLUMN incident_id SET NOT NULL,ALTER COLUMN lifecycle_before SET NOT NULL,ALTER COLUMN lifecycle_after SET NOT NULL;
    ALTER TABLE opspilot_observation_sessions ADD CONSTRAINT opspilot_observation_sessions_ended_reason_check CHECK(ended_reason IS NULL OR ended_reason IN ('recovery_confirmed','deadline_expired','max_samples_exhausted','authority_revoked','binding_stale','scope_suspended'));
    ALTER TABLE opspilot_observation_samples ADD CONSTRAINT opspilot_observation_samples_transition_check CHECK(transition IS NULL OR transition IN ('recovery_confirmed','observation_ended_unconfirmed'));
    ALTER TABLE opspilot_observation_endings ADD CONSTRAINT opspilot_observation_endings_ended_reason_check CHECK(ended_reason IN ('recovery_confirmed','deadline_expired','max_samples_exhausted','authority_revoked','binding_stale','scope_suspended')),
      ADD CONSTRAINT opspilot_observation_endings_transition_check CHECK(transition IS NULL OR transition IN ('recovery_confirmed','observation_ended_unconfirmed'));
    DROP TRIGGER opspilot_release_observations_immutable_guard ON opspilot_release_observations;
    CREATE TRIGGER opspilot_release_observations_immutable_guard BEFORE UPDATE OR DELETE OR TRUNCATE ON opspilot_release_observations FOR EACH STATEMENT EXECUTE FUNCTION opspilot_release_immutable_guard();
    """)


INCIDENT_EVIDENCE_0003 = "CREATE OR REPLACE FUNCTION opspilot_observer_lifecycle_evidence() RETURNS trigger\n        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$\n        DECLARE\n          expected text;\n        BEGIN\n          IF NEW.lifecycle IS DISTINCT FROM OLD.lifecycle\n            AND pg_catalog.pg_has_role(current_user, 'opspilot_observer', 'MEMBER')\n            AND NOT (SELECT r.rolsuper FROM pg_catalog.pg_roles r WHERE r.rolname = current_user)\n            AND current_user <> (SELECT pg_catalog.pg_get_userbyid(c.relowner) FROM pg_catalog.pg_class c WHERE c.oid = TG_RELID)\n          THEN\n            expected := CASE NEW.lifecycle\n              WHEN 'resolved' THEN 'recovery_confirmed'\n              WHEN 'open' THEN 'observation_ended_unconfirmed'\n            END;\n            IF expected IS NULL OR NOT EXISTS (\n              SELECT 1 FROM public.opspilot_observation_endings e\n              JOIN public.opspilot_observation_sessions s ON s.session_id = e.session_id\n              WHERE e.incident_id = NEW.incident_id\n                AND s.incident_id = NEW.incident_id\n                AND e.transition = expected\n                AND e.lifecycle_before = OLD.lifecycle\n                AND e.lifecycle_after = NEW.lifecycle\n                -- written by this very transaction (a record another\n                -- connection committed meanwhile is not this change's evidence)\n                AND e.xmin = pg_catalog.pg_current_xact_id()::xid\n                AND (expected <> 'recovery_confirmed' OR EXISTS (\n                  SELECT 1 FROM public.opspilot_observation_samples m\n                  WHERE m.sample_id = e.sample_id\n                    AND m.session_id = e.session_id\n                    AND m.transition = 'recovery_confirmed'\n                    AND m.disposition = 'adopted'\n                    AND m.xmin = pg_catalog.pg_current_xact_id()::xid\n                ))\n            ) THEN\n              RAISE insufficient_privilege USING MESSAGE =\n                pg_catalog.format('opspilot_observer changed lifecycle %s -> %s without a recorded observation ending', OLD.lifecycle, NEW.lifecycle);\n            END IF;\n          END IF;\n          RETURN NULL;\n        END\n        $$;\n"
INCIDENT_EVIDENCE_0015 = "CREATE OR REPLACE FUNCTION opspilot_observer_lifecycle_evidence() RETURNS trigger\n        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$\n        DECLARE\n          expected text;\n        BEGIN\n          IF NEW.lifecycle IS DISTINCT FROM OLD.lifecycle\n            AND pg_catalog.pg_has_role(current_user, 'opspilot_observer', 'MEMBER')\n            AND NOT (SELECT r.rolsuper FROM pg_catalog.pg_roles r WHERE r.rolname = current_user)\n            AND current_user <> (SELECT pg_catalog.pg_get_userbyid(c.relowner) FROM pg_catalog.pg_class c WHERE c.oid = TG_RELID)\n          THEN\n            expected := CASE NEW.lifecycle\n              WHEN 'resolved' THEN 'recovery_confirmed'\n              WHEN 'open' THEN 'observation_ended_unconfirmed'\n            END;\n            IF expected IS NULL OR NOT EXISTS (\n              SELECT 1 FROM public.opspilot_observation_endings e\n              JOIN public.opspilot_observation_sessions s ON s.session_id = e.session_id\n              WHERE e.purpose='incident_recovery' AND s.purpose='incident_recovery' AND e.incident_id = NEW.incident_id\n                AND s.incident_id = NEW.incident_id\n                AND e.transition = expected\n                AND e.lifecycle_before = OLD.lifecycle\n                AND e.lifecycle_after = NEW.lifecycle\n                -- written by this very transaction (a record another\n                -- connection committed meanwhile is not this change's evidence)\n                AND e.xmin = pg_catalog.pg_current_xact_id()::xid\n                AND (expected <> 'recovery_confirmed' OR EXISTS (\n                  SELECT 1 FROM public.opspilot_observation_samples m\n                  WHERE m.purpose='incident_recovery' AND m.subject_id=NEW.incident_id AND m.sample_id = e.sample_id\n                    AND m.session_id = e.session_id\n                    AND m.transition = 'recovery_confirmed'\n                    AND m.disposition = 'adopted'\n                    AND m.xmin = pg_catalog.pg_current_xact_id()::xid\n                ))\n            ) THEN\n              RAISE insufficient_privilege USING MESSAGE =\n                pg_catalog.format('opspilot_observer changed lifecycle %s -> %s without a recorded observation ending', OLD.lifecycle, NEW.lifecycle);\n            END IF;\n          END IF;\n          RETURN NULL;\n        END\n        $$;\n"


EXTRA_GUARDS = """
CREATE FUNCTION opspilot_release_session_end_evidence() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$ BEGIN
  IF NEW.purpose='release_observation' AND NEW.state IS DISTINCT FROM OLD.state AND NOT EXISTS(SELECT 1 FROM public.opspilot_observation_endings e WHERE e.session_id=NEW.session_id AND e.purpose=NEW.purpose AND e.subject_id=NEW.subject_id AND e.ended_reason=NEW.ended_reason AND e.xmin=pg_current_xact_id()::xid) THEN RAISE insufficient_privilege USING MESSAGE='release session ending evidence refused'; END IF; RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER release_session_end_evidence AFTER UPDATE OF state,ended_reason ON opspilot_observation_sessions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION opspilot_release_session_end_evidence();
ALTER TABLE opspilot_observation_sessions ADD CONSTRAINT release_session_ended_slot CHECK(purpose='incident_recovery' OR state='authorized' OR (active_sample_job_id IS NULL AND active_sample_owner IS NULL AND active_sample_lease_until IS NULL));
CREATE FUNCTION opspilot_observation_legacy_update_sync() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$ BEGIN
  IF NEW.purpose='incident_recovery' THEN
    IF TG_TABLE_NAME='opspilot_observation_samples' THEN
      IF NEW.current_subject_state IS NOT DISTINCT FROM OLD.current_subject_state THEN NEW.current_subject_state:=NEW.subject_lifecycle; END IF;
      IF NEW.current_subject_control_generation IS NOT DISTINCT FROM OLD.current_subject_control_generation THEN NEW.current_subject_control_generation:=NEW.incident_control_generation; END IF;
      IF NEW.current_subject_observation_generation IS NOT DISTINCT FROM OLD.current_subject_observation_generation THEN NEW.current_subject_observation_generation:=NEW.incident_observation_generation; END IF;
    ELSE
      IF NEW.subject_state_before IS NOT DISTINCT FROM OLD.subject_state_before THEN NEW.subject_state_before:=NEW.lifecycle_before; END IF;
      IF NEW.subject_state_after IS NOT DISTINCT FROM OLD.subject_state_after THEN NEW.subject_state_after:=NEW.lifecycle_after; END IF;
    END IF;
  END IF; RETURN NEW;
END $$;
CREATE TRIGGER observation_legacy_update_sync BEFORE UPDATE ON opspilot_observation_samples FOR EACH ROW EXECUTE FUNCTION opspilot_observation_legacy_update_sync();
CREATE TRIGGER observation_legacy_update_sync BEFORE UPDATE ON opspilot_observation_endings FOR EACH ROW EXECUTE FUNCTION opspilot_observation_legacy_update_sync();
CREATE FUNCTION opspilot_endings_incident_subject_evidence() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$ BEGIN
  IF NEW.purpose='incident_recovery' AND NEW.incident_id IS DISTINCT FROM NEW.subject_id THEN RAISE insufficient_privilege USING MESSAGE='incident ending subject refused'; END IF; RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER endings_incident_subject_evidence AFTER INSERT OR UPDATE ON opspilot_observation_endings DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION opspilot_endings_incident_subject_evidence();
CREATE FUNCTION opspilot_release_initial_fields_guard() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$ BEGIN
  IF NEW.used_sample_count<>0 OR NEW.calibrated_policy_revision IS NOT NULL OR NEW.calibrated_policy_content IS NOT NULL OR NEW.calibrated_policy_sha256 IS NOT NULL THEN RAISE insufficient_privilege USING MESSAGE='release initial calibration refused'; END IF; RETURN NEW;
END $$;
CREATE TRIGGER release_initial_fields_guard BEFORE INSERT ON opspilot_release_observations FOR EACH ROW EXECUTE FUNCTION opspilot_release_initial_fields_guard();
ALTER TABLE opspilot_observation_sessions ADD CONSTRAINT release_session_states CHECK (purpose='incident_recovery' OR (state='authorized' AND ended_reason IS NULL) OR (state='completed' AND ended_reason IN ('healthy_confirmed','anomaly_detected')) OR (state='expired' AND ended_reason IN ('deadline_expired','max_samples_exhausted')) OR (state='revoked' AND ended_reason IN ('authority_revoked','binding_stale','scope_suspended','cancelled')));
ALTER TABLE opspilot_observation_samples ADD CONSTRAINT release_sample_generations CHECK (current_subject_control_generation>=0 AND current_subject_observation_generation>=0);
ALTER TABLE opspilot_observation_endings ADD CONSTRAINT release_ending_edges CHECK (purpose='incident_recovery' OR
  (ended_reason='healthy_confirmed' AND transition IS NOT DISTINCT FROM 'release_healthy' AND sample_id IS NOT NULL AND subject_state_before='observing' AND subject_state_after='healthy') OR
  (ended_reason='anomaly_detected' AND transition IS NOT DISTINCT FROM 'release_anomalous' AND sample_id IS NOT NULL AND subject_state_before='observing' AND subject_state_after='anomalous') OR
  (ended_reason IN ('deadline_expired','max_samples_exhausted') AND transition IS NOT DISTINCT FROM 'release_unknown' AND subject_state_before IN ('pending','observing') AND subject_state_after='unknown') OR
  (ended_reason IN ('authority_revoked','binding_stale','scope_suspended') AND transition IS NULL AND subject_state_before=subject_state_after) OR
  (ended_reason='cancelled' AND transition IS NULL AND subject_state_before IN ('pending','observing') AND subject_state_after='cancelled'));
"""
