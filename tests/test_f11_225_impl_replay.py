"""Offline ending-edge fault injection for #225; no PostgreSQL required."""

import copy
from datetime import timedelta
from uuid import uuid4, uuid5

import pytest

from opspilot.observer.health_profile import canonical_content
from opspilot.observer.release_replay import replay_release_history
from tests.f11_release_support import PROFILE, sha256_hex
from tests.test_f11_release_evaluation import AUTHORIZED_AT, _policy, _record


def history_for(reason="authority_revoked"):
    record = _record(policy=_policy())
    sid, job = uuid4(), uuid4()
    ended_at = AUTHORIZED_AT + timedelta(seconds=10)
    audit_at = ended_at + timedelta(milliseconds=1)
    terminal = reason in ("deadline_expired", "cancelled")
    paused = reason == "authority_revoked"
    after = (
        "unknown"
        if reason == "deadline_expired"
        else "cancelled"
        if terminal
        else "observing"
    )
    if reason == "deadline_expired":
        ended_at = record.deadline_at
        audit_at = ended_at + timedelta(milliseconds=1)
    record = record.model_copy(
        update={
            "status": after,
            "paused": paused,
            "control_generation": int(paused or reason == "cancelled"),
            "observation_generation": 1 + int(paused or reason == "cancelled"),
            "ended_reason": reason if terminal else None,
            "ended_at": ended_at if terminal else None,
            "updated_at": audit_at,
        }
    )
    session = {
        "session_id": sid,
        "purpose": "release_observation",
        "subject_kind": "release_observation",
        "subject_id": record.observation_id,
        "release_observation_id": record.observation_id,
        "incident_id": None,
        "authorization_source": None,
        "subject_control_generation": 0,
        "observation_generation": 1,
        "authorized_at": AUTHORIZED_AT,
        "release_policy_revision": record.calibrated_policy_revision,
        "health_profile_revision": record.health_profile_revision,
        "deadline_at": record.deadline_at,
        "max_samples": record.max_samples,
        "sample_interval_seconds": record.sample_interval_seconds,
        "sustained_window_seconds": record.sustained_window_seconds,
        "release_budget_used_at_authorization": 0,
        "issued_sequence": 1,
        "state": "expired" if reason == "deadline_expired" else "revoked",
        "ended_reason": reason,
        "tracking_since": None,
        "tracking_until": None,
        "healthy_since": None,
        "adopted_count": 0,
        "adopted_sequence": 0,
        "adopted_window_end": None,
    }
    ending = {
        "ending_id": uuid5(record.observation_id, "session-ending:" + str(sid)),
        "session_id": sid,
        "purpose": "release_observation",
        "subject_kind": "release_observation",
        "subject_id": record.observation_id,
        "release_observation_id": record.observation_id,
        "ended_reason": reason,
        "transition": "release_unknown" if reason == "deadline_expired" else None,
        "subject_state_before": "observing",
        "subject_state_after": after,
        "sample_id": None,
        "anomaly_key": None,
        "job_id": job,
        "job_sequence": 1,
        "recorded_at": ended_at,
    }
    authorize = {
        "audit_id": uuid4(),
        "observation_id": record.observation_id,
        "sequence": 1,
        "recorded_at": AUTHORIZED_AT,
        "action": "authorize",
        "outcome": "applied",
        "status_before": "pending",
        "status_after": "observing",
        "control_generation_before": 0,
        "control_generation_after": 0,
        "observation_generation_before": 0,
        "observation_generation_after": 1,
        "paused_before": False,
        "paused_after": False,
        "session_id": sid,
        "ending_id": None,
    }
    audits = [authorize]
    if paused or terminal:
        audits.append(
            {
                **authorize,
                "audit_id": uuid4(),
                "sequence": 2,
                "recorded_at": audit_at,
                "action": "pause"
                if paused
                else "cancel"
                if reason == "cancelled"
                else "deadline_expired",
                "status_before": "observing",
                "status_after": after,
                "control_generation_after": record.control_generation,
                "observation_generation_before": 1,
                "observation_generation_after": record.observation_generation,
                "paused_after": paused,
                "session_id": sid if reason == "deadline_expired" else None,
                "ending_id": ending["ending_id"],
            }
        )
    content = canonical_content(PROFILE)
    return {
        "format_version": 1,
        "release": record,
        "sessions": [session],
        "samples": [],
        "endings": [ending],
        "audit": audits,
        "health_profile": {
            "revision": PROFILE.revision,
            "content": content,
            "content_sha256": sha256_hex(content),
        },
    }


@pytest.mark.parametrize(
    "reason",
    [
        "authority_revoked",
        "binding_stale",
        "scope_suspended",
        "cancelled",
        "deadline_expired",
    ],
)
def test_valid_reason_edges_remain_consistent(reason):
    replay = replay_release_history(history_for(reason))
    assert replay.consistent, replay.integrity


@pytest.mark.parametrize(
    "reason",
    [
        "authority_revoked",
        "binding_stale",
        "scope_suspended",
        "cancelled",
        "deadline_expired",
    ],
)
@pytest.mark.parametrize(
    "field,value",
    [
        ("transition", "release_healthy"),
        ("subject_state_before", "healthy"),
        ("subject_state_after", "anomalous"),
        ("anomaly_key", "foreign"),
    ],
)
def test_reason_edge_tampering_fails_closed(reason, field, value):
    history = history_for(reason)
    assert replay_release_history(history).consistent
    corrupted = copy.deepcopy(history)
    corrupted["endings"][0][field] = value
    replay = replay_release_history(corrupted)
    assert not replay.consistent
    assert replay.verdict == "unknown"
    assert "ENDING_MISMATCH" in replay.integrity


def test_unknown_ending_reason_fails_closed():
    history = history_for()
    history["sessions"][0]["ended_reason"] = "invented"
    history["endings"][0]["ended_reason"] = "invented"
    replay = replay_release_history(history)
    assert not replay.consistent
    assert replay.verdict == "unknown"
    assert "ENDING_MISMATCH" in replay.integrity


@pytest.mark.parametrize(
    "reason",
    [
        "authority_revoked",
        "binding_stale",
        "scope_suspended",
        "cancelled",
        "deadline_expired",
    ],
)
def test_plausible_edge_must_match_subject_history(reason):
    history = history_for(reason)
    assert replay_release_history(history).consistent
    history["endings"][0]["subject_state_before"] = "pending"
    if reason in ("authority_revoked", "binding_stale", "scope_suspended"):
        history["endings"][0]["subject_state_after"] = "pending"
    replay = replay_release_history(history)
    assert not replay.consistent
    assert replay.verdict == "unknown"
    assert "ENDING_MISMATCH" in replay.integrity


@pytest.mark.parametrize(
    "reason", ["authority_revoked", "binding_stale", "scope_suspended"]
)
def test_missing_transition_field_is_not_a_null_transition(reason):
    history = history_for(reason)
    del history["endings"][0]["transition"]
    replay = replay_release_history(history)
    assert not replay.consistent
    assert replay.verdict == "unknown"
    assert "ENDING_MISMATCH" in replay.integrity


@pytest.mark.parametrize(
    "reason",
    [
        "authority_revoked",
        "binding_stale",
        "scope_suspended",
        "cancelled",
        "deadline_expired",
    ],
)
@pytest.mark.parametrize(
    "field",
    ["ended_reason", "transition", "subject_state_before", "subject_state_after"],
)
def test_missing_edge_fields_report_ending_mismatch(reason, field):
    history = history_for(reason)
    del history["endings"][0][field]
    replay = replay_release_history(history)
    assert not replay.consistent
    assert replay.verdict == "unknown"
    assert "ENDING_MISMATCH" in replay.integrity
