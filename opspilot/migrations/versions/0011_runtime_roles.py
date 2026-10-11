"""0011 runtime roles: web and worker stop connecting as the migration owner.

Revision ID: 0011_runtime_roles
Revises: 0010_alert_intake
Create Date: 2026-10-11

#169 (contract r1, interface r2 in the task record). Creates two cluster
roles, ``opspilot_web`` and ``opspilot_worker``, as ``NOLOGIN`` groups --
an existing one is reused untouched only when it is a plain group (no
LOGIN, SUPERUSER, CREATEROLE, CREATEDB, REPLICATION or BYPASSRLS; the
upgrade fails naming the attributes otherwise) -- and grants each exactly the
privileges of ``RUNTIME_GRANTS`` below: ``SELECT`` on ``alembic_version``,
``USAGE`` on schema ``public``, and per relation what the process's own SQL
needs (``opspilot.persistence.grants`` explains the matrix and is the
declaration later migrations keep in step; the catalog test compares the
two). The login roles that are members, and their credentials, are made
outside the repository like the Observer's (``scripts/m0/postgres_lab.py
runtime-roles`` for the lab).

Nothing here is a default privilege (no ``ALTER DEFAULT PRIVILEGES``): a
table a later migration adds is closed to both roles until that migration
grants it. No function is ``SECURITY DEFINER``; the guard triggers of 0003
and 0008 run as the invoking role and read only relations it may read.
Neither role gets schema ``CREATE``, database ``TEMP``, ``TRUNCATE``,
``REFERENCES``, ``TRIGGER`` or ownership of anything. Revoking the PUBLIC
defaults (``TEMP``) is a deployment decision and stays out.

This is the investigation side's incremental split, not C3 section 3's full
Controller/Worker/Gateway isolation (#197): the worker still calls the
persistence write primitives itself.

``downgrade()`` drops the guard triggers and revokes exactly the
privileges ``upgrade()`` granted (no ``DROP OWNED BY``: a role that existed
before 0011 keeps what it owned and was granted elsewhere), then drops each
role only when nothing references it any more -- no object it owns, no
other grant, in this or another database (``pg_shdepend``). A role that would be dropped while
login roles are still members refuses the downgrade instead: drop or
re-home those logins first.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011_runtime_roles"
down_revision: str | Sequence[str] | None = "0010_alert_intake"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLES = ("opspilot_web", "opspilot_worker")

# Frozen copy of ``opspilot.persistence.grants.RUNTIME_GRANTS`` as of this
# revision (a migration never imports application code that moves on).
RUNTIME_GRANTS: dict[str, dict[str, tuple[str, ...]]] = {
    "opspilot_web": {
        "alembic_version": ("SELECT",),
        "opspilot_alert_deliveries": (
            "INSERT(actor,alert_json,annotation_revision,annotations_sha256,delivery_key,fingerprint,incident_id,outcome,raw_sha256,received_at,starts_at,starts_at_raw,status,truncated)",
            "SELECT",
        ),
        "opspilot_alert_identities": (
            "INSERT(delivery_key,fingerprint,handoff_reason,incident_id,namespace,run_id,starts_at,starts_at_raw,target_id,workload)",
            "SELECT",
        ),
        "opspilot_budget_reservations": (),
        "opspilot_controls": (
            "INSERT(action,actor,audit_id,expected_generation,incident_id,payload,resulting_generation)",
            "SELECT",
        ),
        "opspilot_evidence": ("SELECT",),
        "opspilot_f13_audit": (
            "INSERT(action,actor_id,event_id,expected_generation,idempotency_key,object_id,object_kind,principal_kind,reason,resulting_generation,revision,version)",
            "SELECT",
        ),
        "opspilot_f13_requests": (
            "INSERT(action,idempotency_key,request_sha256,result)",
            "SELECT",
        ),
        "opspilot_health_profiles": (
            "INSERT(content,content_sha256,health_profile_revision,profile_id)",
            "SELECT",
        ),
        "opspilot_incidents": (
            "INSERT(current_run_id,incident_id,intake_key,lifecycle,state,target_id)",
            "SELECT",
            "UPDATE(conclusion,control_generation,current_run_id,lifecycle,mode,observation_generation,state)",
        ),
        "opspilot_input_rounds": ("SELECT",),
        "opspilot_inputs": (
            "INSERT(actor,content,control_generation,incident_id,input_id,kind,sequence)",
            "SELECT",
        ),
        "opspilot_knowledge_entries": (
            "INSERT(entry_id)",
            "SELECT",
            "UPDATE(generation,latest_revision,updated_at)",
        ),
        "opspilot_knowledge_revision_states": ("SELECT",),
        "opspilot_knowledge_revisions": (
            "INSERT(approved_by,approved_by_kind,content,content_sha256,entry_id,event_id,name,revision,source_postmortem_id,source_proposal_key,source_version,supersedes_revision,tags)",
            "SELECT",
        ),
        "opspilot_knowledge_revocations": (
            "INSERT(entry_id,event_id,reason,revision,revoked_by,revoked_by_kind)",
            "SELECT",
        ),
        "opspilot_observation_endings": (
            "INSERT(ended_reason,ending_id,incident_id,job_id,job_sequence,lifecycle_after,lifecycle_before,sample_id,session_id,transition)",
            "SELECT",
        ),
        "opspilot_observation_samples": ("SELECT",),
        "opspilot_observation_sessions": (
            "INSERT(active_sample_due_at,active_sample_job_id,active_sample_sequence,authorized_by,authorized_global_generation,authorized_target_generation,deadline_at,health_profile_revision,incident_id,issued_sequence,max_samples,observation_generation,purpose,sample_interval_seconds,session_id,subject_control_generation,sustained_window_seconds,target,target_id)",
            "SELECT",
            "UPDATE(active_sample_due_at,active_sample_job_id,active_sample_lease_until,active_sample_owner,active_sample_sequence,adopted_count,adopted_sequence,adopted_window_end,ended_reason,healthy_since,state,updated_at)",
        ),
        "opspilot_observation_signal_readings": ("SELECT",),
        "opspilot_postmortem_conclusions": ("SELECT",),
        "opspilot_postmortem_disputes": ("SELECT",),
        "opspilot_postmortem_generation_attempts": ("SELECT",),
        "opspilot_postmortem_generation_jobs": (
            "INSERT(incident_id,pending_regeneration_version)",
            "SELECT",
            "UPDATE(consecutive_failures,failure_watermark,last_error_code,next_attempt_at,pending_regeneration_version,updated_at)",
        ),
        "opspilot_postmortem_proposals": ("SELECT",),
        "opspilot_postmortem_versions": (
            "SELECT",
            "UPDATE(stale_reason,state)",
        ),
        "opspilot_postmortems": (
            "SELECT",
            "UPDATE(generation,updated_at)",
        ),
        "opspilot_runs": (
            "INSERT(budget_limit,control_generation,deadline,incident_id,input,run_id,state,versions)",
            "SELECT",
            "UPDATE(control_generation,lease_until,owner,state)",
        ),
        "opspilot_scope_controls": (
            "SELECT",
            "UPDATE(scope_id)",
        ),
        "opspilot_steps": ("SELECT",),
        "opspilot_subject_events": (
            "INSERT(kind,payload,sequence,subject_id)",
            "SELECT",
        ),
        "opspilot_suspension_audit": (),
        "opspilot_target_suspensions": (
            "INSERT(target_id)",
            "SELECT",
            "UPDATE(updated_at)",
        ),
        "opspilot_targets": (
            "INSERT(resource_uid,target_id)",
            "SELECT",
            "UPDATE(cluster_uid,integration_id,namespace,workload)",
        ),
        "opspilot_tool_charges": (),
        "opspilot_web_ledger": (
            "DELETE",
            "INSERT(key,namespace,value)",
            "SELECT",
        ),
    },
    "opspilot_worker": {
        "alembic_version": ("SELECT",),
        "opspilot_alert_deliveries": (),
        "opspilot_alert_identities": (),
        "opspilot_budget_reservations": (
            "INSERT(amount,reservation_id,reserved_seconds,run_id)",
            "SELECT",
            "UPDATE(seconds,state)",
        ),
        "opspilot_controls": ("SELECT",),
        "opspilot_evidence": (
            "INSERT(adopted,data_as_of,evidence_id,observed_at,projection_revision,raw,raw_sha256,run_id,status,subject_id,view,view_sha256)",
            "SELECT",
            "UPDATE(adopted,committed,data_as_of,observed_at,projection_revision,status,view,view_sha256)",
        ),
        "opspilot_f13_audit": (
            "INSERT(action,actor_id,event_id,expected_generation,idempotency_key,object_id,object_kind,principal_kind,reason,resulting_generation,revision,version)",
            "SELECT",
        ),
        "opspilot_f13_requests": (
            "INSERT(action,idempotency_key,request_sha256,result)",
            "SELECT",
        ),
        "opspilot_health_profiles": (),
        "opspilot_incidents": (
            "SELECT",
            "UPDATE(conclusion)",
        ),
        "opspilot_input_rounds": (
            "INSERT(control_generation,input_watermark,logical_key,run_id)",
            "SELECT",
            "UPDATE(committed,control_generation,input_watermark)",
        ),
        "opspilot_inputs": ("SELECT",),
        "opspilot_knowledge_entries": (),
        "opspilot_knowledge_revision_states": ("SELECT",),
        "opspilot_knowledge_revisions": ("SELECT",),
        "opspilot_knowledge_revocations": (),
        "opspilot_observation_endings": ("SELECT",),
        "opspilot_observation_samples": ("SELECT",),
        "opspilot_observation_sessions": ("SELECT",),
        "opspilot_observation_signal_readings": ("SELECT",),
        "opspilot_postmortem_conclusions": (
            "INSERT(author,body,certainty,citations_valid,conclusion_key,evidence_refs,ordinal,postmortem_id,section,version)",
            "SELECT",
        ),
        "opspilot_postmortem_disputes": (
            "INSERT(conclusion_key,dispute_id,event_id,postmortem_id,raised_by,raised_by_kind,reason,version)",
            "SELECT",
        ),
        "opspilot_postmortem_generation_attempts": (
            "INSERT(attempt_id,incident_id,input_policy_version,lease_epoch,lease_owner,max_model_requests,model,model_profile,output_schema_version,prompt_version,revises_version,watermark)",
            "SELECT",
            "UPDATE(completion_tokens,error_code,finished_at,input_bytes,input_sha256,model_requests,prompt_tokens,response_model,status,version)",
        ),
        "opspilot_postmortem_generation_jobs": (
            "INSERT(incident_id)",
            "SELECT",
            "UPDATE(consecutive_failures,failure_watermark,last_error_code,lease_epoch,lease_owner,lease_until,next_attempt_at,pending_regeneration_version,updated_at)",
        ),
        "opspilot_postmortem_proposals": (
            "INSERT(content,content_sha256,name,postmortem_id,proposal_key,supersedes_entry_id,tags,version)",
            "SELECT",
        ),
        "opspilot_postmortem_versions": (
            "INSERT(content,content_sha256,evidence_snapshot_sha256,generated_by,generated_by_kind,incident_control_generation,input_watermark,last_run_id,observation_ending_id,observation_generation,observation_session_id,postmortem_id,revises_version,run_count,version)",
            "SELECT",
            "UPDATE(stale_reason,state)",
        ),
        "opspilot_postmortems": (
            "INSERT(incident_id,postmortem_id)",
            "SELECT",
            "UPDATE(generation,latest_version,updated_at)",
        ),
        "opspilot_runs": (
            "SELECT",
            "UPDATE(budget_reserved,budget_spent,budget_unknown,control_generation,epoch,input_watermark,lease_until,owner,state,tool_operations_used,tool_seconds_used)",
        ),
        "opspilot_scope_controls": (
            "SELECT",
            "UPDATE(scope_id)",
        ),
        "opspilot_steps": (
            "INSERT(control_generation,logical_key,observed_at,response,run_id,sequence,status,step_id)",
            "SELECT",
            "UPDATE(status,tool_results)",
        ),
        "opspilot_subject_events": (
            "INSERT(kind,payload,sequence,subject_id)",
            "SELECT",
        ),
        "opspilot_suspension_audit": (),
        "opspilot_target_suspensions": (
            "SELECT",
            "UPDATE(updated_at)",
        ),
        "opspilot_targets": ("SELECT",),
        "opspilot_tool_charges": (
            "INSERT(dispatch_id,epoch,operation_id,reserved,run_id,seconds)",
            "SELECT",
            "UPDATE(seconds)",
        ),
        "opspilot_web_ledger": (),
    },
}


def _grant(privilege: str, relation: str, role: str) -> str:
    kind, _, columns = privilege.partition("(")
    on = f"({columns.rstrip(')')}) " if columns else ""
    return f"GRANT {kind} {on}ON {relation} TO {role};"


def _revoke(privilege: str, relation: str, role: str) -> str:
    kind, _, columns = privilege.partition("(")
    on = f"({columns.rstrip(')')}) " if columns else ""
    return f"REVOKE {kind} {on}ON {relation} FROM {role};"


def upgrade() -> None:
    statements = []
    for role in ROLES:
        statements.append(
            f"""
            DO $$
            DECLARE
              unsafe text;
            BEGIN
              IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                CREATE ROLE {role} NOLOGIN;
              ELSE
                -- reused only as a plain group, never altered: a role that
                -- can log in or holds elevated attributes fails the upgrade
                SELECT pg_catalog.concat_ws(', ',
                  CASE WHEN rolcanlogin THEN 'LOGIN' END,
                  CASE WHEN rolsuper THEN 'SUPERUSER' END,
                  CASE WHEN rolcreaterole THEN 'CREATEROLE' END,
                  CASE WHEN rolcreatedb THEN 'CREATEDB' END,
                  CASE WHEN rolreplication THEN 'REPLICATION' END,
                  CASE WHEN rolbypassrls THEN 'BYPASSRLS' END)
                INTO unsafe FROM pg_roles WHERE rolname = '{role}';
                IF unsafe <> '' THEN
                  RAISE EXCEPTION 'existing role {role} is not a plain NOLOGIN group (has %); fix or rename it before migrating to 0011', unsafe;
                END IF;
              END IF;
            END
            $$;
            GRANT USAGE ON SCHEMA public TO {role};
            """
        )
        for relation, privileges in RUNTIME_GRANTS[role].items():
            statements.extend(_grant(p, relation, role) for p in privileges)
    op.execute("\n".join(statements))
    op.execute(GUARDS)


# Grants cannot tell a review write from a generation write on the F13
# tables both processes write (audit, requests, versions), nor a lease the
# web clears from one it would issue. These guards check the role instead,
# like the Observer guards of 0003: membership of the group, direct or
# inherited, superusers and the table owner excluded (pg_has_role is always
# true for them), search_path pinned, every name qualified, no SECURITY
# DEFINER. Refusals are SQLSTATE 42501 (insufficient_privilege), the code a
# missing GRANT raises, so the store maps both to INTEGRITY_REFUSED.
#
# * opspilot_worker writes only worker-principal audit rows and request rows
#   of the worker actions (generate, submit, mark_stale), and moves a
#   version only to under_review or stale. Every review action needs a
#   basic_auth audit row in its own transaction (0008), so each review
#   primitive called by the worker fails and leaves no row.
# * opspilot_web may clear a Run's owner/lease_until (every human control
#   and the deadline sweep do) but never set them: with no epoch grant
#   either, it cannot issue a lease.
GUARDS = """
    CREATE FUNCTION opspilot_worker_review_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
    DECLARE
      review boolean;
    BEGIN
      IF NOT pg_catalog.pg_has_role(current_user, 'opspilot_worker', 'MEMBER')
        OR (SELECT r.rolsuper FROM pg_catalog.pg_roles r WHERE r.rolname = current_user)
        OR current_user = (SELECT pg_catalog.pg_get_userbyid(c.relowner) FROM pg_catalog.pg_class c WHERE c.oid = TG_RELID)
      THEN
        RETURN NEW;
      END IF;
      IF TG_TABLE_NAME = 'opspilot_f13_audit' THEN
        review := NEW.principal_kind IS DISTINCT FROM 'worker';
      ELSIF TG_TABLE_NAME = 'opspilot_f13_requests' THEN
        review := NEW.action NOT IN ('generate', 'submit', 'mark_stale');
      ELSE
        review := NEW.state IS DISTINCT FROM OLD.state
          AND NEW.state NOT IN ('under_review', 'stale');
      END IF;
      IF review THEN
        RAISE insufficient_privilege USING MESSAGE =
          pg_catalog.format('opspilot_worker may not write a review action to %s', TG_TABLE_NAME);
      END IF;
      RETURN NEW;
    END
    $$;
    CREATE TRIGGER opspilot_f13_audit_worker_guard
      BEFORE INSERT ON opspilot_f13_audit
      FOR EACH ROW EXECUTE FUNCTION opspilot_worker_review_guard();
    CREATE TRIGGER opspilot_f13_requests_worker_guard
      BEFORE INSERT ON opspilot_f13_requests
      FOR EACH ROW EXECUTE FUNCTION opspilot_worker_review_guard();
    CREATE TRIGGER opspilot_postmortem_versions_worker_guard
      BEFORE UPDATE OF state ON opspilot_postmortem_versions
      FOR EACH ROW EXECUTE FUNCTION opspilot_worker_review_guard();
    CREATE FUNCTION opspilot_web_lease_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
    BEGIN
      IF pg_catalog.pg_has_role(current_user, 'opspilot_web', 'MEMBER')
        AND NOT (SELECT r.rolsuper FROM pg_catalog.pg_roles r WHERE r.rolname = current_user)
        AND current_user <> (SELECT pg_catalog.pg_get_userbyid(c.relowner) FROM pg_catalog.pg_class c WHERE c.oid = TG_RELID)
        AND ((NEW.owner IS NOT NULL AND NEW.owner IS DISTINCT FROM OLD.owner)
          OR (NEW.lease_until IS NOT NULL AND NEW.lease_until IS DISTINCT FROM OLD.lease_until))
      THEN
        RAISE insufficient_privilege USING MESSAGE =
          'opspilot_web may clear a Run lease but not set one';
      END IF;
      RETURN NEW;
    END
    $$;
    CREATE TRIGGER opspilot_runs_web_lease_guard
      BEFORE UPDATE OF owner, lease_until ON opspilot_runs
      FOR EACH ROW EXECUTE FUNCTION opspilot_web_lease_guard();
"""


def downgrade() -> None:
    statements = [
        """
        DROP TRIGGER opspilot_runs_web_lease_guard ON opspilot_runs;
        DROP FUNCTION opspilot_web_lease_guard();
        DROP TRIGGER opspilot_postmortem_versions_worker_guard ON opspilot_postmortem_versions;
        DROP TRIGGER opspilot_f13_requests_worker_guard ON opspilot_f13_requests;
        DROP TRIGGER opspilot_f13_audit_worker_guard ON opspilot_f13_audit;
        DROP FUNCTION opspilot_worker_review_guard();
        """,
    ]
    # Exactly what upgrade() granted, never DROP OWNED: a role that existed
    # before 0011 keeps its objects and its other grants.
    for role in ROLES:
        statements.append(f"REVOKE USAGE ON SCHEMA public FROM {role};")
        for relation, privileges in RUNTIME_GRANTS[role].items():
            statements.extend(_revoke(p, relation, role) for p in privileges)
    for role in ROLES:
        statements.append(
            f"""
            DO $$
            BEGIN
              IF NOT EXISTS (
                SELECT 1 FROM pg_shdepend d
                JOIN pg_roles r ON r.oid = d.refobjid
                WHERE d.refclassid = 'pg_authid'::regclass AND r.rolname = '{role}'
              ) THEN
                IF EXISTS (
                  SELECT 1 FROM pg_auth_members m
                  JOIN pg_roles g ON g.oid = m.roleid
                  WHERE g.rolname = '{role}'
                ) THEN
                  RAISE EXCEPTION '{role} still has members; drop or re-home its login roles before downgrading 0011';
                END IF;
                DROP ROLE IF EXISTS {role};
              END IF;
            END
            $$;
            """
        )
    op.execute("\n".join(statements))
