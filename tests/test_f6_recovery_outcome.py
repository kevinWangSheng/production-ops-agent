"""The product's F6 projection: committed recovery records -> RecoveryOutcome
(issue #140; AGENTS.md "验收入口是外部 IncidentScenario -> IncidentOutcome").

No PostgreSQL: the records are the shapes ``ObservationStore.incident_records``
returns, built from genuine Observer samples by ``tests.m1_02_replay_support``.
Every field comes from a committed row or from the replay of those rows
(``opspilot.observer.replay``); nothing here recomputes a verdict of its own,
and permissions are derived from the grants the caller measured.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from opspilot.acceptance import (
    IncidentScenario,
    RecoveryOutcome,
    RecoveryRecords,
    permissions_from_grants,
    recovery_outcome,
)
from tests.m1_02_replay_support import (
    NOW,
    PROFILE,
    healthy_history,
    stored_history,
    take,
)

SCENARIO = IncidentScenario(
    scenario_id="F6:1:recovered",
    feature_id="F6",
    acceptance_step="1",
    kind="recovered",
    subject_id="incident-f6",
)

#: What ``observer_grants`` reports for the migration-0003 Observer role.
OBSERVER_GRANTS = {
    "opspilot_incidents": frozenset({"SELECT", "UPDATE"}),
    "opspilot_observation_sessions": frozenset({"SELECT", "UPDATE"}),
    "opspilot_observation_samples": frozenset({"SELECT", "INSERT"}),
    "opspilot_observation_signal_readings": frozenset({"SELECT", "INSERT"}),
    "opspilot_observation_endings": frozenset({"SELECT", "INSERT"}),
    "opspilot_health_profiles": frozenset({"SELECT"}),
    "opspilot_targets": frozenset({"SELECT"}),
    "opspilot_scope_controls": frozenset({"SELECT"}),
    "opspilot_target_suspensions": frozenset({"SELECT"}),
    "opspilot_runs": frozenset(),
    "opspilot_steps": frozenset(),
    "opspilot_controls": frozenset(),
}


def _control(generation: int = 1) -> dict:
    return {
        "action": "register_remediation",
        "expected_generation": generation - 1,
        "resulting_generation": generation,
        "actor": "operator",
        "payload": {"revision": "svc:v2"},
        "created_at": NOW - timedelta(seconds=600),
    }


def _records(history, *, controls=None, grants=OBSERVER_GRANTS) -> RecoveryRecords:
    return RecoveryRecords(
        incident={
            "incident_id": history["session"]["incident_id"],
            "lifecycle": history["incident_lifecycle"],
            "mode": "automatic",
            "control_generation": history["session"]["subject_control_generation"],
        },
        sessions=(history,),
        controls=tuple(controls or ()),
        grants=grants,
    )


def test_a_confirmed_recovery_is_projected_from_the_committed_rows():
    history = healthy_history()
    outcome = recovery_outcome(SCENARIO, _records(history, controls=[_control()]))
    assert isinstance(outcome, RecoveryOutcome)
    assert outcome.scenario_id == SCENARIO.scenario_id
    assert outcome.subject_id == "incident-f6"
    assert outcome.incident_lifecycle == "resolved"
    assert outcome.recovery_confirmed is True
    assert outcome.recovery_verdict == "healthy"
    assert outcome.latest_sample_verdict == "healthy"
    assert outcome.healthy_window_seconds == 600
    assert outcome.used_sample_count == 6
    assert outcome.observation_ended is True
    assert outcome.observation_ended_reason == "recovery_confirmed"
    assert outcome.human_interaction is None
    assert outcome.handoff_reasons == ()
    assert outcome.recovery_handled_at == history["session"]["authorized_at"]
    assert outcome.recovery_profile_revision == PROFILE.revision
    assert outcome.recovery_profile_content == history["health_profile"]["content"]
    assert outcome.replay is not None and outcome.replay.consistent
    assert outcome.model_requests == ()
    # the target is the session's immutable binding, never telemetry content
    assert outcome.target == history["session"]["target"]
    assert len(outcome.recovery_samples) == 6
    sample = outcome.recovery_samples[-1]
    assert sample.subject_id == "incident-f6"
    assert sample.sequence == 6 and sample.disposition == "adopted"
    assert sample.outcome == "healthy" and sample.confirms_health is True
    assert sample.target == history["session"]["target"]
    assert sample.health_profile_revision == PROFILE.revision
    assert sample.transition == "recovery_confirmed"
    errors = sample.signals["errors"]
    assert errors.evidence_id == f"{sample.sample_id}:errors"
    assert errors.value == 0.0 and errors.status == "ok" and errors.sample_count == 5
    assert errors.source == "prometheus"
    assert (
        errors.query
        == "e{namespace='ns',service='svc'} / t{namespace='ns',service='svc'}"
    )
    assert errors.window_end == sample.window_end
    assert len(errors.raw_sha256) == 64 and len(errors.body_sha256) == 64
    assert errors.evaluated_at == sample.window_end
    # actions are the audit of what the product did, in record order
    assert outcome.actions[:2] == ("record_handling", "advance_incident_lifecycle")
    assert outcome.actions.count("persist_observation") == 6
    assert outcome.actions.count("read_only_query") == 12
    assert outcome.actions[-1] == "advance_incident_lifecycle"
    assert "human_handoff" not in outcome.actions
    assert outcome.permissions == ("read_only", "human_control")
    # the session contract projection and the job set
    (session,) = outcome.observation_sessions
    assert session["state"] == "completed" and session["authorized"] is False
    assert session["health_profile_revision"] == PROFILE.revision
    assert set(session) == {
        "session_id",
        "purpose",
        "subject",
        "target",
        "subject_control_generation",
        "observation_generation",
        "state",
        "authorized",
        "health_profile_revision",
        "adopted_sequence",
        "adopted_window_end",
        "active_sample_job_id",
    }
    assert len(outcome.sample_jobs) == 6
    assert outcome.observation_authorization["session_id"] == session["session_id"]
    assert outcome.handling_audit[0]["action"] == "register_remediation"


def test_a_tampered_record_yields_unknown_with_the_integrity_reason():
    history = healthy_history()
    for row in history["samples"]:
        row["outcome"] = "degraded"
    outcome = recovery_outcome(SCENARIO, _records(history))
    assert outcome.recovery_verdict == "unknown"
    assert outcome.recovery_confirmed is False
    assert "STORED_OBSERVATION_INTEGRITY_MISMATCH" in outcome.recovery_reasons
    # the incident lifecycle is the committed row's, reported as is
    assert outcome.incident_lifecycle == "resolved"
    assert outcome.replay is not None and not outcome.replay.consistent


@pytest.mark.parametrize(
    "kind,verdict,reasons",
    [
        ("degraded", "degraded", {"CONTINUED_DEGRADATION", "DEGRADED_SIGNAL:errors"}),
        ("no-traffic", "unknown", {"INSUFFICIENT_TRAFFIC", "OBSERVATION_UNCONFIRMED"}),
    ],
)
def test_an_unconfirmed_session_hands_off_with_reasons_from_the_replay(
    kind, verdict, reasons
):
    from tests.m1_02_replay_support import profile_with

    session_id = uuid4()
    short = profile_with(max_samples=3, sustained_window_seconds=60)
    taken = [
        take(
            kind,
            sequence=i + 1,
            window_end=NOW + timedelta(seconds=60 * i),
            session_id=session_id,
            profile=short,
        )
        for i in range(3)
    ]
    history = stored_history(
        taken,
        profile=short,
        session_id=session_id,
        authorized_at=NOW - timedelta(seconds=600),
        max_samples=3,
        sustained_window_seconds=60,
    )
    outcome = recovery_outcome(SCENARIO, _records(history))
    assert outcome.incident_lifecycle == "open"
    assert outcome.recovery_confirmed is False
    assert outcome.recovery_verdict == verdict
    assert outcome.latest_sample_verdict == (
        "degraded" if kind == "degraded" else "no_data"
    )
    assert outcome.healthy_window_seconds == 0
    assert outcome.used_sample_count == 3
    assert outcome.observation_ended is True
    assert outcome.observation_ended_reason == "max_samples_exhausted"
    assert outcome.human_interaction == "handoff"
    assert "MAX_SAMPLES_EXHAUSTED" in outcome.handoff_reasons
    assert reasons <= set(outcome.recovery_reasons)
    assert set(outcome.handoff_reasons) >= reasons
    assert outcome.actions[-1] == "human_handoff"
    assert "advance_incident_lifecycle" in outcome.actions
    assert outcome.permissions == ("read_only",)


def test_a_missing_required_signal_names_it():
    session_id = uuid4()
    taken = [
        take("healthy", sequence=1, window_end=NOW, session_id=session_id),
    ]
    # drop the errors signal's reading rows: the stored outcome stays what
    # the Observer wrote, the replay reports the missing basis
    history = stored_history(
        taken, session_id=session_id, authorized_at=NOW - timedelta(seconds=600)
    )
    sample = history["samples"][0]
    sample["readings"] = [r for r in sample["readings"] if r["signal_name"] != "errors"]
    sample["outcome"], sample["required_signals_present"] = "no_data", False
    sample["confirms_health"], sample["health_basis"] = False, "outcome_not_healthy"
    history["session"]["healthy_since"] = None
    outcome = recovery_outcome(SCENARIO, _records(history))
    assert outcome.recovery_verdict == "unknown"
    assert "MISSING_SIGNAL:errors" in outcome.recovery_reasons
    assert "REQUIRED_TELEMETRY_MISSING" in outcome.recovery_reasons


def test_a_session_still_observing_is_not_ended_and_not_a_handoff():
    history = healthy_history(count=3)
    outcome = recovery_outcome(SCENARIO, _records(history))
    assert outcome.incident_lifecycle == "observing_recovery"
    assert outcome.observation_ended is False
    assert outcome.observation_ended_reason is None
    assert outcome.recovery_confirmed is False and outcome.recovery_verdict == "unknown"
    assert outcome.latest_sample_verdict == "healthy"
    assert outcome.healthy_window_seconds == 420
    assert outcome.human_interaction is None and outcome.handoff_reasons == ()


def test_no_session_means_no_observation():
    history = healthy_history(count=1)
    records = RecoveryRecords(
        incident={
            "incident_id": uuid4(),
            "lifecycle": "open",
            "mode": "automatic",
            "control_generation": 0,
        },
        sessions=(),
        controls=(),
        grants=OBSERVER_GRANTS,
    )
    outcome = recovery_outcome(SCENARIO, records)
    assert outcome.recovery_verdict == "unknown" and not outcome.recovery_confirmed
    assert outcome.recovery_samples == () and outcome.observation_sessions == ()
    assert outcome.observation_ended is False
    assert outcome.recovery_handled_at is None
    assert outcome.replay is None
    assert outcome.permissions == ("read_only",)
    assert outcome.actions == ()
    del history


def test_permissions_come_from_the_measured_grants_not_a_constant():
    assert permissions_from_grants(OBSERVER_GRANTS, human_control=False) == (
        "read_only",
    )
    assert permissions_from_grants(OBSERVER_GRANTS, human_control=True) == (
        "read_only",
        "human_control",
    )
    wider = {**OBSERVER_GRANTS, "opspilot_runs": frozenset({"SELECT", "UPDATE"})}
    assert permissions_from_grants(wider, human_control=False) == (
        "read_only",
        "investigation_write:opspilot_runs",
    )
    deleting = {
        **OBSERVER_GRANTS,
        "opspilot_observation_samples": frozenset({"SELECT", "INSERT", "DELETE"}),
    }
    assert "record_delete:opspilot_observation_samples" in permissions_from_grants(
        deleting, human_control=False
    )
    # a role that cannot even read the records it judges by is not read_only
    blind = {**OBSERVER_GRANTS, "opspilot_observation_samples": frozenset({"INSERT"})}
    assert "read_only" not in permissions_from_grants(blind, human_control=False)
    assert "unreadable:opspilot_observation_samples" in permissions_from_grants(
        blind, human_control=False
    )
    with pytest.raises(ValueError, match="GRANTS_REQUIRED"):
        permissions_from_grants({}, human_control=False)
