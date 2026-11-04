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


#: Intake outcome -> the summarizer's delivery outcome. A replay repeats an
#: alert identity that already opened its incident (with or without a Run).
DELIVERY_OUTCOMES = {
    "created": "run",
    "replayed": "run",
    "handoff_created": "handoff",
    "handoff_replayed": "handoff",
    "attached": "attached",
}


def export_queries(t0: datetime) -> dict[str, str]:
    """What ``finish`` reads to write the raw PG files, scoped to T0."""
    since = _since(t0)
    scoped = f"i.created_at >= {since}"
    return {
        # firing only: a resolved notification is no injection's destination
        "deliveries": (
            "SELECT d.delivery_id, d.fingerprint, d.outcome, d.incident_id,"
            " d.received_at, a.run_id FROM opspilot_alert_deliveries d"
            " LEFT JOIN opspilot_alert_identities a ON a.incident_id = d.incident_id"
            f" WHERE d.status = 'firing' AND d.received_at >= {since}"
            " ORDER BY d.received_at, d.delivery_id"
        ),
        "runs": (
            "SELECT r.run_id, r.incident_id, r.state, r.deadline,"
            " a.created_at AS intake_created_at,"
            " (SELECT min(s.observed_at) FROM opspilot_steps s WHERE s.run_id = r.run_id)"
            "   AS first_step_at,"
            " (SELECT max(s.observed_at) FROM opspilot_steps s WHERE s.run_id = r.run_id)"
            "   AS last_step_at,"
            f" (SELECT {_num('prompt_tokens')} FROM opspilot_steps s"
            "   WHERE s.run_id = r.run_id) AS prompt_tokens,"
            f" (SELECT {_num('completion_tokens')} FROM opspilot_steps s"
            "   WHERE s.run_id = r.run_id) AS completion_tokens"
            " FROM opspilot_runs r JOIN opspilot_incidents i ON i.incident_id = r.incident_id"
            " LEFT JOIN opspilot_alert_identities a ON a.run_id = r.run_id"
            f" WHERE {scoped} ORDER BY r.run_id"
        ),
        "sessions": (
            "SELECT o.session_id, o.incident_id, o.authorized_at, o.deadline_at,"
            " o.sample_interval_seconds,"
            " (SELECT min(e.recorded_at) FROM opspilot_observation_endings e"
            "   WHERE e.session_id = o.session_id) AS ended_at,"
            " (SELECT e.transition FROM opspilot_observation_endings e"
            "   WHERE e.session_id = o.session_id ORDER BY e.recorded_at LIMIT 1)"
            "   AS transition"
            " FROM opspilot_observation_sessions o"
            " JOIN opspilot_incidents i ON i.incident_id = o.incident_id"
            f" WHERE {scoped} ORDER BY o.authorized_at"
        ),
        "samples": (
            "SELECT p.submitted_at, p.session_id, p.outcome, p.required_signals_present"
            " FROM opspilot_observation_samples p"
            " JOIN opspilot_observation_sessions o ON o.session_id = p.session_id"
            " JOIN opspilot_incidents i ON i.incident_id = o.incident_id"
            f" WHERE {scoped} AND p.disposition = 'adopted'"
            " ORDER BY p.submitted_at, p.sample_id"
        ),
        "postmortem_tokens": (
            "SELECT coalesce(sum(prompt_tokens), 0) AS prompt_tokens,"
            " coalesce(sum(completion_tokens), 0) AS completion_tokens,"
            " count(*) AS attempts FROM opspilot_postmortem_generation_attempts"
            f" WHERE started_at >= {since}"
        ),
        "database": "SELECT pg_database_size('opspilot') AS bytes",
    }


def signal_status(outcome: str, required_signals_present: bool) -> str:
    """samples.csv ``signal_status``: ``stale`` for a stale window, ``missing``
    for no data / timeout / failure or a required signal absent, else ``ok``."""
    if outcome == "stale":
        return "stale"
    if outcome in {"no_data", "timeout", "failed"} or not required_signals_present:
        return "missing"
    return "ok"
