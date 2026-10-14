"""Require ended-session evidence for release deadline transitions (#225)."""

from alembic import op

revision = "0016_release_deadline_evidence"
down_revision = "0015_release_observation_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE OR REPLACE FUNCTION opspilot_release_update_evidence() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
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
              OR (NEW.status='unknown' AND e.transition='release_unknown' AND ((e.ended_reason='deadline_expired' AND clock_timestamp()>=NEW.deadline_at
                AND s.xmin=pg_current_xact_id()::xid AND s.state='expired' AND s.ended_reason=e.ended_reason
                AND s.release_observation_id=NEW.observation_id AND s.purpose=e.purpose AND s.subject_kind=e.subject_kind AND s.subject_id=NEW.observation_id
                AND s.deadline_at=NEW.deadline_at
                AND e.subject_kind='release_observation' AND e.subject_id=NEW.observation_id
                AND NOT EXISTS(SELECT 1 FROM public.opspilot_observation_sessions active WHERE active.release_observation_id=NEW.observation_id AND active.state='authorized')) OR (e.ended_reason='max_samples_exhausted' AND NEW.used_sample_count=NEW.max_samples AND m.disposition='adopted' AND m.xmin=pg_current_xact_id()::xid
                AND s.xmin=pg_current_xact_id()::xid AND s.state='expired' AND s.ended_reason=e.ended_reason
                AND s.release_observation_id=NEW.observation_id AND s.purpose=e.purpose AND s.subject_kind=e.subject_kind AND s.subject_id=NEW.observation_id
                AND m.session_id=e.session_id AND m.purpose=e.purpose AND m.subject_kind=e.subject_kind AND m.subject_id=NEW.observation_id
                AND m.subject_control_generation=NEW.control_generation AND m.observation_generation=NEW.observation_generation AND m.transition=e.transition)))));
        IF NEW.status='unknown' AND clock_timestamp()>=NEW.deadline_at THEN
          -- Deferred check: a sweep's referenced session uses the ending
          -- branch above. No authorized session permits subject-only audit.
          valid:=valid OR EXISTS(SELECT 1 FROM public.opspilot_release_audit a
            WHERE a.observation_id=NEW.observation_id AND a.action='deadline_expired' AND a.outcome='applied'
              AND a.xmin=pg_current_xact_id()::xid AND a.status_before=OLD.status AND a.status_after=NEW.status
              AND a.expected_control_generation=OLD.control_generation
              AND a.control_generation_before=OLD.control_generation AND a.control_generation_after=NEW.control_generation
              AND a.observation_generation_before=OLD.observation_generation AND a.observation_generation_after=NEW.observation_generation
              AND a.paused_before=OLD.paused AND a.paused_after=NEW.paused
              AND a.session_id IS NULL AND a.ending_id IS NULL
              AND NOT EXISTS(SELECT 1 FROM public.opspilot_observation_sessions s
                WHERE s.release_observation_id=NEW.observation_id
                  AND s.state='authorized')
              -- A session ended in this transaction must pass the matching
              -- deadline branch; NULL audit references cannot bypass it.
              AND NOT EXISTS(SELECT 1 FROM public.opspilot_observation_endings e
                WHERE e.release_observation_id=NEW.observation_id
                  AND e.xmin=pg_current_xact_id()::xid));
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
    """)


def downgrade() -> None:
    # Frozen 0015 function, restored without modifying historical migration.
    op.execute("""
    CREATE OR REPLACE FUNCTION opspilot_release_update_evidence() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
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
              OR (NEW.status='unknown' AND e.transition='release_unknown' AND ((e.ended_reason='deadline_expired' AND clock_timestamp()>=NEW.deadline_at) OR (e.ended_reason='max_samples_exhausted' AND NEW.used_sample_count=NEW.max_samples AND m.disposition='adopted' AND m.xmin=pg_current_xact_id()::xid
                AND s.xmin=pg_current_xact_id()::xid AND s.state='expired' AND s.ended_reason=e.ended_reason
                AND s.release_observation_id=NEW.observation_id AND s.purpose=e.purpose AND s.subject_kind=e.subject_kind AND s.subject_id=NEW.observation_id
                AND m.session_id=e.session_id AND m.purpose=e.purpose AND m.subject_kind=e.subject_kind AND m.subject_id=NEW.observation_id
                AND m.subject_control_generation=NEW.control_generation AND m.observation_generation=NEW.observation_generation AND m.transition=e.transition)))));
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
    """)
