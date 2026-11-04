"""The read-only queries the runner and the final export run, and the
scripted operator's close rule (S-R3).

Only ``SELECT``s (the session is ``default_transaction_read_only``); the one
write the soak makes to the product is the workbench ``close`` action over
HTTP, the same path a human uses.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from scripts.soak.common import iso

ACTIVE_RUN_STATES = ("queued", "running", "waiting_human")
TERMINAL_RUN_STATES = ("completed", "failed", "cancelled", "budget_exhausted")
UNCONFIRMED = "observation_ended_unconfirmed"


def _since(t0: datetime) -> str:
    # iso() of an aware datetime: digits, '-', ':', '.', 'T', 'Z' only.
    return f"'{iso(t0)}'::timestamptz"


def _active() -> str:
    return ",".join(f"'{state}'" for state in ACTIVE_RUN_STATES)


def close_candidates_sql(t0: datetime) -> str:
    return (
        "SELECT i.incident_id, i.lifecycle, i.control_generation,"
        " (SELECT e.transition FROM opspilot_observation_endings e"
        "   WHERE e.incident_id = i.incident_id ORDER BY e.recorded_at DESC LIMIT 1)"
        "   AS last_transition,"
        " (SELECT count(*) FROM opspilot_runs r WHERE r.incident_id = i.incident_id"
        f"   AND r.state IN ({_active()})) AS active_runs"
        " FROM opspilot_incidents i"
        " WHERE i.lifecycle IN ('open', 'resolved')"
        " AND i.current_run_id IS NOT NULL AND i.merged_into_incident_id IS NULL"
        f" AND i.created_at >= {_since(t0)}"
        " ORDER BY i.created_at"
    )


def run_states_sql(t0: datetime) -> str:
    return (
        "SELECT r.run_id, r.incident_id, r.state FROM opspilot_runs r"
        " JOIN opspilot_incidents i ON i.incident_id = r.incident_id"
        f" WHERE i.created_at >= {_since(t0)} ORDER BY r.run_id"
    )


def should_close(row: Mapping[str, Any], *, faults_active: bool) -> str | None:
    """S-R3: why the scripted operator closes this incident now, or ``None``.

    ``resolved`` (an independent healthy observation confirmed recovery)
    closes at once. ``open`` closes only after its last observation ended
    unconfirmed, no Run of it is still active and no injected fault is on
    any more. Anything else stays for a human.
    """
    lifecycle = row.get("lifecycle")
    if lifecycle == "resolved":
        return "resolved"
    if (
        lifecycle == "open"
        and row.get("last_transition") == UNCONFIRMED
        and int(row.get("active_runs") or 0) == 0
        and not faults_active
    ):
        return "observation_ended_unconfirmed_fault_cleared"
    return None


def _num(path: str) -> str:
    return (
        f"sum(CASE WHEN jsonb_typeof(s.response->'usage'->'{path}') = 'number'"
        f" THEN (s.response->'usage'->>'{path}')::numeric END)"
    )


def export_queries(t0: datetime) -> dict[str, str]:
    """The tables ``summarize`` reads, scoped to incidents created since T0."""
    since = _since(t0)
    scoped = f"i.created_at >= {since}"
    return {
        "incidents": (
            "SELECT i.incident_id, i.lifecycle, i.state, i.created_at,"
            " i.current_run_id, i.related_incident_id, i.merged_into_incident_id,"
            " a.handoff_reason FROM opspilot_incidents i"
            " LEFT JOIN opspilot_alert_identities a ON a.incident_id = i.incident_id"
            f" WHERE {scoped} ORDER BY i.created_at"
        ),
        "runs": (
            "SELECT r.run_id, r.incident_id, r.state, r.deadline,"
            " (SELECT min(s.observed_at) FROM opspilot_steps s WHERE s.run_id = r.run_id)"
            "   AS first_step_at,"
            " (SELECT max(s.observed_at) FROM opspilot_steps s WHERE s.run_id = r.run_id)"
            "   AS last_step_at,"
            " (SELECT count(*) FROM opspilot_steps s WHERE s.run_id = r.run_id"
            "   AND s.response ? 'usage') AS model_steps,"
            f" (SELECT {_num('prompt_tokens')} FROM opspilot_steps s"
            "   WHERE s.run_id = r.run_id) AS prompt_tokens,"
            f" (SELECT {_num('completion_tokens')} FROM opspilot_steps s"
            "   WHERE s.run_id = r.run_id) AS completion_tokens,"
            f" (SELECT {_num('prompt_cache_hit_tokens')} FROM opspilot_steps s"
            "   WHERE s.run_id = r.run_id) AS prompt_cache_hit_tokens,"
            f" (SELECT {_num('prompt_cache_miss_tokens')} FROM opspilot_steps s"
            "   WHERE s.run_id = r.run_id) AS prompt_cache_miss_tokens"
            " FROM opspilot_runs r JOIN opspilot_incidents i ON i.incident_id = r.incident_id"
            f" WHERE {scoped} ORDER BY r.run_id"
        ),
        "deliveries": (
            "SELECT d.fingerprint, d.status, d.outcome, d.incident_id, d.starts_at,"
            " d.received_at, CASE WHEN d.truncated THEN NULL"
            " ELSE (d.alert_json::jsonb)->'labels'->>'alertname' END AS alertname"
            f" FROM opspilot_alert_deliveries d WHERE d.received_at >= {since}"
            " ORDER BY d.received_at"
        ),
        "sessions": (
            "SELECT o.session_id, o.incident_id, o.state, o.ended_reason,"
            " o.authorized_at, o.deadline_at, o.max_samples, o.sample_interval_seconds,"
            " (SELECT min(e.recorded_at) FROM opspilot_observation_endings e"
            "   WHERE e.session_id = o.session_id) AS ended_at,"
            " (SELECT count(DISTINCT p.sequence) FROM opspilot_observation_samples p"
            "   WHERE p.session_id = o.session_id AND p.disposition = 'adopted')"
            "   AS adopted_samples,"
            " (SELECT count(*) FROM opspilot_observation_samples p"
            "   WHERE p.session_id = o.session_id) AS recorded_samples,"
            " (SELECT count(*) FROM opspilot_observation_samples p"
            "   WHERE p.session_id = o.session_id AND p.disposition = 'adopted'"
            "   AND (p.outcome IN ('no_data', 'stale', 'timeout', 'failed')"
            "   OR NOT p.required_signals_present)) AS signal_missing_or_stale"
            " FROM opspilot_observation_sessions o"
            " JOIN opspilot_incidents i ON i.incident_id = o.incident_id"
            f" WHERE {scoped} ORDER BY o.authorized_at"
        ),
        "endings": (
            "SELECT e.incident_id, e.session_id, e.ended_reason, e.transition,"
            " e.lifecycle_before, e.lifecycle_after, e.recorded_at"
            " FROM opspilot_observation_endings e"
            " JOIN opspilot_incidents i ON i.incident_id = e.incident_id"
            f" WHERE {scoped} ORDER BY e.recorded_at"
        ),
        "controls": (
            "SELECT c.incident_id, c.action, c.actor, c.created_at"
            " FROM opspilot_controls c JOIN opspilot_incidents i"
            f" ON i.incident_id = c.incident_id WHERE {scoped} ORDER BY c.created_at"
        ),
        "postmortem_tokens": (
            "SELECT coalesce(sum(prompt_tokens), 0) AS prompt_tokens,"
            " coalesce(sum(completion_tokens), 0) AS completion_tokens,"
            " count(*) AS attempts FROM opspilot_postmortem_generation_attempts"
            f" WHERE started_at >= {since}"
        ),
        "database": "SELECT pg_database_size('opspilot') AS bytes",
    }
