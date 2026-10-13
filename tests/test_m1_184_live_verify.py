"""Independent acceptance-author tests: constructed records only, no DB."""

from __future__ import annotations

import copy
import json
import runpy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

VERIFY = runpy.run_path(
    str(
        Path(__file__).resolve().parents[1]
        / "docs/evidence/m1-184-self-heal-live/verify.py"
    )
)
judge = VERIFY["judge"]
INCIDENT = "00000000-0000-0000-0000-000000000184"
SESSION = "00000000-0000-0000-0000-000000000185"
RUN = "00000000-0000-0000-0000-000000000186"
NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)


def inputs(scenario: str) -> dict[str, Any]:
    recovered = scenario == "S1"
    state = "completed" if recovered else "expired"
    reason = "recovery_confirmed" if recovered else "deadline_expired"
    lifecycle = "resolved" if recovered else "open"
    replay = {
        "session_id": SESSION,
        "consistent": True,
        "integrity": [],
        "recovery_verdict": "healthy" if recovered else "degraded",
        "recovery_confirmed": recovered,
        "healthy_window_seconds": 600 if recovered else 0,
        "samples": [
            {
                "sample_id": "sample-1",
                "replayed_outcome": "healthy" if recovered else "degraded",
                "replayed_required_signals_present": True,
                "readings": [
                    {"signal_name": "traffic", "verdict": "healthy"},
                    {
                        "signal_name": "errors",
                        "verdict": "healthy" if recovered else "degraded",
                    },
                ],
            }
        ],
    }
    p: dict[str, Any] = {
        "authorization_source": "automatic",
        "actions": [
            "read_only_query",
            "persist_observation",
            "advance_incident_lifecycle",
        ],
        "permissions": ["read_only"],
        "incident_lifecycle": lifecycle,
        **replay,
        "replay": copy.deepcopy(replay),
        "recovery_reasons": [] if recovered else ["CONTINUED_DEGRADATION"],
        "observation_ended": True,
        "observation_ended_reason": reason,
        "human_interaction": None if recovered else "handoff",
        "handoff_reasons": [] if recovered else ["DEADLINE_EXPIRED"],
    }
    if not recovered:
        p["actions"].append("human_handoff")
    return {
        "checked_at": NOW.isoformat(),
        "incident": {
            "incident_id": INCIDENT,
            "current_run_id": RUN,
            "conclusion_present": True,
            "control_generation": 0,
            "lifecycle": lifecycle,
        },
        "run": {
            "run_id": RUN,
            "incident_id": INCIDENT,
            "state": "completed",
            "control_generation": 0,
        },
        "authorizations": [
            {
                "incident_id": INCIDENT,
                "control_generation": 0,
                "state": "authorized",
                "session_id": SESSION,
            }
        ],
        "controls": [
            {
                "incident_id": INCIDENT,
                "action": "auto_authorize_observation",
                "actor": "system",
                "expected_generation": 0,
                "resulting_generation": 0,
                "payload": {"session_id": SESSION},
            }
        ],
        "sessions": [
            {
                "session": {
                    "session_id": SESSION,
                    "incident_id": INCIDENT,
                    "subject_control_generation": 0,
                    "authorization_source": "automatic",
                    "target": {"revision": None},
                    "state": state,
                    "ended_reason": reason,
                    "deadline_at": NOW,
                    "sustained_window_seconds": 600,
                },
                "health_profile": {
                    "content": json.dumps(
                        {"session": {"sustained_window_seconds": 600}}
                    )
                },
                "samples": [
                    {
                        "sample_id": "sample-1",
                        "disposition": "adopted",
                        "window_end": NOW - timedelta(seconds=60),
                        "outcome": "healthy" if recovered else "degraded",
                        "required_signals_present": True,
                    }
                ],
                "endings": [
                    {
                        "recorded_at": NOW,
                        "ended_reason": reason,
                        "transition": "recovery_confirmed"
                        if recovered
                        else "observation_ended_unconfirmed",
                        "lifecycle_after": lifecycle,
                    }
                ],
            }
        ],
        "projection": p,
        "replays": [replay],
        "page": {
            "text": f"session {SESSION} source automatic "
            + (VERIFY["RECOVERED"] if recovered else "not confirmed"),
            "sources": ["automatic"],
            "statuses": ["recovered_no_remediation"] if recovered else [],
        },
    }


def failed(scenario: str, data: dict[str, Any]) -> set[str]:
    result = judge(scenario, INCIDENT, data)
    assert result["all_pass"] is False
    return {v["id"] for v in result["verdicts"] if not v["pass"]}


@pytest.mark.parametrize("scenario", ["S1", "S2"])
def test_pass(scenario: str) -> None:
    records = inputs(scenario)
    original = copy.deepcopy(records)
    result = judge(scenario, INCIDENT, records)
    assert result["all_pass"] is True, result["verdicts"]
    assert records == original
    assert len({v["id"] for v in result["verdicts"]}) == len(result["verdicts"])
    json.dumps(result)


@pytest.mark.parametrize("action", ["register_remediation", "takeover", "pause"])
def test_human_control_fails(action: str) -> None:
    data = inputs("S1")
    data["controls"].append({"action": action, "actor": "operator"})
    assert "C4" in failed("S1", data)


def test_post_remediation_fails() -> None:
    data = inputs("S1")
    data["sessions"][0]["session"]["authorization_source"] = "post_remediation"
    data["projection"]["authorization_source"] = "post_remediation"
    assert {"C5", "C6"} <= failed("S1", data)


def test_max_samples_cannot_pass_as_deadline() -> None:
    data = inputs("S2")
    data["sessions"][0]["session"]["ended_reason"] = "max_samples_exhausted"
    data["sessions"][0]["endings"][0]["ended_reason"] = "max_samples_exhausted"
    data["projection"]["observation_ended_reason"] = "max_samples_exhausted"
    assert {"S2-1", "S2-3", "S2-4", "S2-5"} <= failed("S2", data)


def test_missing_s1_statement_fails() -> None:
    data = inputs("S1")
    data["page"]["text"] = f"session {SESSION} source automatic"
    assert "S1-4" in failed("S1", data)


def test_authorization_failure_fails() -> None:
    data = inputs("S2")
    data["authorizations"][0]["state"] = "handed_off"
    assert {"C2", "S2-6"} <= failed("S2", data)


def test_unknown_telemetry_fails() -> None:
    data = inputs("S2")
    data["sessions"][0]["samples"][0].update(
        outcome="failed", required_signals_present=False
    )
    data["projection"]["recovery_verdict"] = "unknown"
    data["projection"]["recovery_reasons"] = ["REQUIRED_TELEMETRY_MISSING"]
    assert "S2-7" in failed("S2", data)


@pytest.mark.parametrize(
    "change",
    [
        "run_generation",
        "duplicate_session",
        "early_ending",
        "duplicate_ending",
        "inconsistent_replay",
        "human_permission",
        "short_window",
        "success_claim",
    ],
)
def test_other_contract_failures(change: str) -> None:
    scenario = "S1" if change == "short_window" else "S2"
    data = inputs(scenario)
    if change == "run_generation":
        data["run"]["control_generation"] = 1
    elif change == "duplicate_session":
        data["sessions"].append(copy.deepcopy(data["sessions"][0]))
    elif change == "early_ending":
        data["sessions"][0]["endings"][0]["recorded_at"] -= timedelta(seconds=1)
    elif change == "duplicate_ending":
        data["sessions"][0]["endings"].append(
            copy.deepcopy(data["sessions"][0]["endings"][0])
        )
    elif change == "inconsistent_replay":
        data["replays"][0]["consistent"] = False
    elif change == "human_permission":
        data["projection"]["permissions"].append("human_control")
    elif change == "short_window":
        data["projection"]["healthy_window_seconds"] = 599
    else:
        data["page"]["text"] += VERIFY["RECOVERED"]
    failed(scenario, data)


def test_s2_insufficient_traffic_even_with_degraded_other_signal() -> None:
    data = inputs("S2")
    data["replays"][0]["samples"][0]["readings"][0]["verdict"] = "below_traffic_gate"
    assert "S2-7" in failed("S2", data)


def test_hidden_success_text_is_not_visible_evidence() -> None:
    parser = VERIFY["RecoveryPage"]()
    parser.feed(
        f'<section id="recovery-verdict"><span class="authorization-source">automatic</span>session {SESSION}'
        f"<script>{VERIFY['RECOVERED']}</script>"
        f'<strong hidden data-status="recovered_no_remediation" class="recovery-statement">{VERIFY["RECOVERED"]}</strong></section>'
    )
    data = inputs("S1")
    data["page"] = parser.document()
    assert "S1-4" in failed("S1", data)


def test_visible_recovery_section_uses_real_template_fields() -> None:
    parser = VERIFY["RecoveryPage"]()
    parser.feed(
        f'<section id="recovery-verdict"><div class="recovery-session">session <code>{SESSION}</code>'
        '<span class="authorization-source">automatic</span>'
        f'<strong class="recovery-statement" data-status="recovered_no_remediation">{VERIFY["RECOVERED"]}</strong></div></section>'
    )
    data = inputs("S1")
    data["page"] = parser.document()
    assert judge("S1", INCIDENT, data)["all_pass"] is True
