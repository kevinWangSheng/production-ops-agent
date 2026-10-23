"""What the web and worker runtime roles may do to each relation (#169).

``RUNTIME_GRANTS`` is the declaration; migration 0011 (and every later
migration that adds or changes a relation) grants exactly this, and
``tests/integration/test_runtime_roles_postgres.py`` fails when the
database after ``make migrate`` holds anything else, or when a relation in
``public`` has no entry for a role (U4: no default privileges, so a new
table stays inaccessible until a migration decides).

Notation (interface P2): ``"SELECT"`` / ``"INSERT"`` / ``"UPDATE"`` /
``"DELETE"`` for a table-wide privilege, ``"UPDATE(col_a,col_b)"`` (sorted,
no spaces) for a column-level one; an empty set is an explicit "nothing".

The matrix is the SQL each process actually runs, read from the code and
from a full-suite trace of statements by the process that issued them:
SELECT is table-wide wherever a role reads a relation (``RETURNING``,
``WHERE`` and the guard triggers of 0008 read rows too); INSERT and UPDATE
are column-level, the columns the statements name. Two grants exist only
for row locks: PostgreSQL requires UPDATE on some column of a table read
``FOR SHARE``/``FOR UPDATE``, and the write paths lock the suspension rows
(``_lock_scope``). ``opspilot_scope_controls.scope_id`` (CHECK ``= 1``) and
``opspilot_target_suspensions.updated_at`` carry no suspension state.

Where a grant cannot separate a legitimate write from a forbidden one --
the worker's generation rows versus review rows on the shared F13 tables,
the web clearing a Run lease versus setting one -- a 0011 role guard
trigger refuses with SQLSTATE 42501; ``TRIGGER_GUARDED`` names them.

Identity columns (``opspilot_suspension_audit.audit_id``,
``opspilot_alert_deliveries.delivery_id``) need no sequence privilege:
PostgreSQL does not check one for an identity default.
"""

from __future__ import annotations

from collections.abc import Mapping

WEB_ROLE = "opspilot_web"
WORKER_ROLE = "opspilot_worker"

S = frozenset({"SELECT"})
NONE: frozenset[str] = frozenset()


def _g(*privileges: str) -> frozenset[str]:
    return frozenset(privileges)


#: Relations where a grant cannot separate a legitimate write from a
#: forbidden one, and the 0011 trigger that does (SQLSTATE 42501): table ->
#: (trigger, the role it restricts). The worker writes generation, submission
#: and staleness to the same F13 tables the review actions write; the web
#: clears Run leases but must never set one.
TRIGGER_GUARDED: Mapping[str, tuple[str, str]] = {
    "opspilot_observation_sessions": ("release_session_guard", "opspilot_observer"),
    "opspilot_release_audit": ("release_audit_guard", "opspilot_observer"),
    "opspilot_f13_audit": ("opspilot_f13_audit_worker_guard", WORKER_ROLE),
    "opspilot_f13_requests": ("opspilot_f13_requests_worker_guard", WORKER_ROLE),
    "opspilot_postmortem_versions": (
        "opspilot_postmortem_versions_worker_guard",
        WORKER_ROLE,
    ),
    "opspilot_runs": ("opspilot_runs_web_lease_guard", WEB_ROLE),
    "opspilot_release_observations": (
        "release_update_guard",
        WEB_ROLE,
    ),
    "opspilot_release_comparisons": ("release_comparison_guard", "opspilot_observer"),
    "opspilot_release_comparison_attempts": (
        "release_comparison_attempt_guard",
        "opspilot_observer",
    ),
}

RUNTIME_GRANTS: Mapping[str, Mapping[str, frozenset[str]]] = {
    WEB_ROLE: {
        "alembic_version": S,
        "opspilot_alert_deliveries": _g(
            "SELECT",
            "INSERT(actor,alert_json,annotation_revision,annotations_sha256,delivery_key,"
            "fingerprint,incident_id,outcome,raw_sha256,received_at,starts_at,"
            "starts_at_raw,status,truncated)",
        ),
        "opspilot_alert_identities": _g(
            "SELECT",
            "INSERT(delivery_key,fingerprint,handoff_reason,incident_id,namespace,run_id,"
            "starts_at,starts_at_raw,target_id,workload)",
        ),
        "opspilot_budget_reservations": NONE,
        "opspilot_controls": _g(
            "SELECT",
            "INSERT(action,actor,audit_id,expected_generation,incident_id,payload,"
            "resulting_generation)",
        ),
        "opspilot_evidence": S,
        "opspilot_f13_audit": _g(
            "SELECT",
            "INSERT(action,actor_id,event_id,expected_generation,idempotency_key,object_id,"
            "object_kind,principal_kind,reason,resulting_generation,revision,version)",
        ),
        "opspilot_f13_requests": _g(
            "SELECT", "INSERT(action,idempotency_key,request_sha256,result)"
        ),
        "opspilot_health_profiles": _g(
            "SELECT",
            "INSERT(content,content_sha256,health_profile_revision,profile_id)",
        ),
        "opspilot_incidents": _g(
            "SELECT",
            "INSERT(current_run_id,incident_id,intake_key,lifecycle,priority,"
            "related_incident_id,state,target_id)",
            "UPDATE(conclusion,control_generation,current_run_id,lifecycle,mode,"
            "observation_generation,priority,state)",
        ),
        "opspilot_input_rounds": S,
        "opspilot_inputs": _g(
            "SELECT",
            "INSERT(actor,content,control_generation,incident_id,input_id,kind,sequence)",
        ),
        "opspilot_knowledge_entries": _g(
            "SELECT",
            "INSERT(entry_id)",
            "UPDATE(generation,latest_revision,updated_at)",
        ),
        "opspilot_knowledge_revision_states": S,
        "opspilot_knowledge_revisions": _g(
            "SELECT",
            "INSERT(approved_by,approved_by_kind,content,content_sha256,entry_id,event_id,"
            "name,revision,source_postmortem_id,source_proposal_key,source_version,"
            "supersedes_revision,tags)",
        ),
        "opspilot_knowledge_revocations": _g(
            "SELECT",
            "INSERT(entry_id,event_id,reason,revision,revoked_by,revoked_by_kind)",
        ),
        "opspilot_observation_endings": _g(
            "INSERT(anomaly_key,ended_reason,ending_id,incident_id,job_id,job_sequence,lifecycle_after,lifecycle_before,release_observation_id,sample_id,session_id,subject_state_after,subject_state_before,transition)",
            "SELECT",
        ),
        "opspilot_observation_samples": S,
        "opspilot_observation_sessions": _g(
            "INSERT(active_sample_due_at,active_sample_job_id,active_sample_sequence,authorization_source,authorized_by,authorized_global_generation,authorized_target_generation,deadline_at,health_profile_revision,incident_id,issued_sequence,max_samples,observation_generation,purpose,release_budget_used_at_authorization,release_observation_id,release_policy_revision,sample_interval_seconds,session_id,subject_control_generation,sustained_window_seconds,target,target_id)",
            "SELECT",
            "UPDATE(active_sample_due_at,active_sample_job_id,active_sample_lease_until,active_sample_owner,active_sample_sequence,adopted_count,adopted_sequence,adopted_window_end,ended_reason,healthy_since,state,updated_at)",
        ),
        "opspilot_observation_signal_readings": S,
        "opspilot_postmortem_conclusions": S,
        "opspilot_postmortem_disputes": S,
        "opspilot_postmortem_generation_attempts": S,
        # a return for revision (D19) records the version to regenerate and
        # resets the failure count; the lease columns stay the worker's
        "opspilot_postmortem_generation_jobs": _g(
            "SELECT",
            "INSERT(incident_id,pending_regeneration_version)",
            "UPDATE(consecutive_failures,failure_watermark,last_error_code,next_attempt_at,"
            "pending_regeneration_version,updated_at)",
        ),
        "opspilot_postmortem_proposals": S,
        "opspilot_postmortem_versions": _g("SELECT", "UPDATE(stale_reason,state)"),
        "opspilot_postmortems": _g("SELECT", "UPDATE(generation,updated_at)"),
        "opspilot_release_events": _g(
            "SELECT",
            "INSERT(actor,audit_text,kind,observation_id,outcome,raw_sha256,reason,received_at,semantic_sha256,sequence,source,source_release_id,truncated)",
        ),
        "opspilot_release_observations": _g(
            "INSERT(after_revision,auxiliary,before_revision,deadline_at,deadline_seconds,ended_at,ended_reason,format_version,health_profile_revision,max_samples,min_tracking_seconds,observation_id,released_at,sample_interval_seconds,semantic_sha256,source,source_release_id,status,sustained_window_seconds,target_id,target_snapshot,timing_policy_version)",
            "SELECT",
            "UPDATE(calibrated_policy_content,calibrated_policy_revision,calibrated_policy_sha256,control_generation,ended_at,ended_reason,incident_id,observation_generation,paused,status,updated_at)",
        ),
        # human controls and the deadline sweep clear owner/lease_until; no
        # epoch, so the web cannot issue a lease of its own (claim() sets it)
        "opspilot_runs": _g(
            "SELECT",
            "INSERT(budget_limit,control_generation,deadline,incident_id,input,run_id,"
            "state,versions)",
            "UPDATE(control_generation,lease_until,owner,state)",
        ),
        # F7 operator entry: global suspension state and generation writes.
        "opspilot_scope_controls": _g(
            "SELECT", "UPDATE(global_generation,global_suspended,scope_id)"
        ),
        # the self-heal pass (#184, migration 0013) runs in the web process
        "opspilot_self_heal_authorizations": _g(
            "SELECT",
            "INSERT(attempts,control_generation,incident_id,next_attempt_at,reason,"
            "session_id,state)",
            "UPDATE(attempts,next_attempt_at,reason,session_id,state,updated_at)",
        ),
        "opspilot_steps": S,
        "opspilot_subject_events": _g(
            "SELECT", "INSERT(kind,payload,sequence,subject_id)"
        ),
        # F7 operator decisions append audit rows; the web cannot read them.
        "opspilot_suspension_audit": _g("INSERT(actor,generation,suspended,target_id)"),
        # F7 target suspension writes, plus the existing registration/lock columns.
        "opspilot_target_suspensions": _g(
            "SELECT",
            "INSERT(generation,suspended,target_id)",
            "UPDATE(generation,suspended,updated_at)",
        ),
        "opspilot_targets": _g(
            "SELECT",
            "INSERT(resource_uid,target_id)",
            "UPDATE(cluster_uid,integration_id,namespace,workload)",
        ),
        "opspilot_tool_charges": NONE,
        "opspilot_web_ledger": _g("SELECT", "INSERT(key,namespace,value)", "DELETE"),
        "opspilot_release_query_results": S,
        # F11 PR-4a: the Observer writes before/after comparisons; the web reads
        "opspilot_release_comparisons": S,
        "opspilot_release_comparison_attempts": S,
        "opspilot_release_comparison_queries": S,
        # F11 PR-4b: the web consumes anomaly endings into incident links
        "opspilot_release_incident_links": _g(
            "SELECT",
            "INSERT(anomaly_key,comparison_id,disposition,ending_id,handoff_reason,"
            "incident_id,input_id,observation_id,related_incident_id,run_id)",
        ),
        "opspilot_release_audit": _g(
            "SELECT",
            "INSERT(action,actor,audit_id,control_generation_after,control_generation_before,ending_id,expected_control_generation,idempotency_key,observation_generation_after,observation_generation_before,observation_id,outcome,paused_after,paused_before,principal_kind,reason,request_sha256,result,sequence,session_id,status_after,status_before)",
        ),
    },
    WORKER_ROLE: {
        "alembic_version": S,
        "opspilot_alert_deliveries": NONE,
        "opspilot_alert_identities": NONE,
        "opspilot_budget_reservations": _g(
            "SELECT",
            "INSERT(amount,reservation_id,reserved_seconds,run_id)",
            "UPDATE(seconds,state)",
        ),
        "opspilot_controls": S,
        "opspilot_evidence": _g(
            "SELECT",
            "INSERT(adopted,data_as_of,evidence_id,observed_at,projection_revision,raw,"
            "raw_sha256,run_id,status,subject_id,view,view_sha256)",
            "UPDATE(adopted,committed,data_as_of,observed_at,projection_revision,status,"
            "view,view_sha256)",
        ),
        # generate / submit / mark_stale audit rows (principal ``worker``)
        "opspilot_f13_audit": _g(
            "SELECT",
            "INSERT(action,actor_id,event_id,expected_generation,idempotency_key,object_id,"
            "object_kind,principal_kind,reason,resulting_generation,revision,version)",
        ),
        "opspilot_f13_requests": _g(
            "SELECT", "INSERT(action,idempotency_key,request_sha256,result)"
        ),
        "opspilot_health_profiles": NONE,
        "opspilot_incidents": _g("SELECT", "UPDATE(conclusion)"),
        "opspilot_input_rounds": _g(
            "SELECT",
            "INSERT(control_generation,input_watermark,logical_key,run_id)",
            "UPDATE(committed,control_generation,input_watermark)",
        ),
        "opspilot_inputs": S,
        "opspilot_knowledge_entries": NONE,
        "opspilot_knowledge_revision_states": S,
        "opspilot_knowledge_revisions": S,
        "opspilot_knowledge_revocations": NONE,
        "opspilot_observation_endings": S,
        "opspilot_observation_samples": S,
        "opspilot_observation_sessions": S,
        "opspilot_observation_signal_readings": S,
        "opspilot_postmortem_conclusions": _g(
            "SELECT",
            "INSERT(author,body,certainty,citations_valid,conclusion_key,evidence_refs,"
            "ordinal,postmortem_id,section,version)",
        ),
        "opspilot_postmortem_disputes": _g(
            "SELECT",
            "INSERT(conclusion_key,dispute_id,event_id,postmortem_id,raised_by,"
            "raised_by_kind,reason,version)",
        ),
        "opspilot_postmortem_generation_attempts": _g(
            "SELECT",
            "INSERT(attempt_id,incident_id,input_policy_version,lease_epoch,lease_owner,"
            "max_model_requests,model,model_profile,output_schema_version,prompt_version,"
            "revises_version,watermark)",
            "UPDATE(completion_tokens,error_code,finished_at,input_bytes,input_sha256,"
            "model_calls,model_requests,prompt_tokens,response_model,status,version)",
        ),
        "opspilot_postmortem_generation_jobs": _g(
            "SELECT",
            "INSERT(incident_id)",
            "UPDATE(consecutive_failures,failure_watermark,last_error_code,lease_epoch,"
            "lease_owner,lease_until,next_attempt_at,pending_regeneration_version,"
            "updated_at)",
        ),
        "opspilot_postmortem_proposals": _g(
            "SELECT",
            "INSERT(content,content_sha256,name,postmortem_id,proposal_key,"
            "supersedes_entry_id,tags,version)",
        ),
        "opspilot_postmortem_versions": _g(
            "SELECT",
            "INSERT(content,content_sha256,evidence_snapshot_sha256,generated_by,"
            "generated_by_kind,incident_control_generation,input_watermark,last_run_id,"
            "observation_ending_id,observation_generation,observation_session_id,"
            "postmortem_id,revises_version,run_count,version)",
            "UPDATE(stale_reason,state)",
        ),
        "opspilot_postmortems": _g(
            "SELECT",
            "INSERT(incident_id,postmortem_id)",
            "UPDATE(generation,latest_version,updated_at)",
        ),
        "opspilot_release_events": NONE,
        "opspilot_release_observations": NONE,
        "opspilot_runs": _g(
            "SELECT",
            "UPDATE(budget_reserved,budget_spent,budget_unknown,control_generation,epoch,"
            "input_watermark,lease_until,owner,state,tool_operations_used,"
            "tool_seconds_used)",
        ),
        # lock-only: FOR SHARE needs UPDATE on some column; scope_id is
        # CHECK (= 1), so the grant cannot change any suspension state
        "opspilot_scope_controls": _g("SELECT", "UPDATE(scope_id)"),
        "opspilot_self_heal_authorizations": NONE,
        "opspilot_steps": _g(
            "SELECT",
            "INSERT(control_generation,logical_key,observed_at,response,run_id,sequence,"
            "status,step_id)",
            "UPDATE(status,tool_results)",
        ),
        "opspilot_subject_events": _g(
            "SELECT", "INSERT(kind,payload,sequence,subject_id)"
        ),
        "opspilot_suspension_audit": NONE,
        # lock-only (see opspilot_scope_controls); no suspension writes
        "opspilot_target_suspensions": _g("SELECT", "UPDATE(updated_at)"),
        "opspilot_targets": S,
        "opspilot_tool_charges": _g(
            "SELECT",
            "INSERT(dispatch_id,epoch,operation_id,reserved,run_id,seconds)",
            "UPDATE(seconds)",
        ),
        "opspilot_web_ledger": NONE,
        "opspilot_release_query_results": NONE,
        "opspilot_release_comparisons": NONE,
        "opspilot_release_comparison_attempts": NONE,
        "opspilot_release_comparison_queries": NONE,
        "opspilot_release_incident_links": NONE,
        "opspilot_release_audit": NONE,
    },
}
