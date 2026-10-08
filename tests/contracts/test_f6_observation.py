"""C3 §4/§10 contracts, authored independently of M1-02 implementation.

adopt_sample is the watermark operation AFTER evaluate_sample accepts; it is
not the atomic persistence boundary. Transaction/lease/scope checks need wiring.
"""

from datetime import UTC, datetime, timedelta

import pytest

from opspilot.domain.base import DomainError
from opspilot.domain.evidence import QueryWindow
from opspilot.domain.intake import Target
from opspilot.domain.observation import (
    HealthSample,
    ObservationSession,
    adopt_sample,
    confirms_health,
    evaluate_sample,
    extends_healthy_window,
    may_schedule_sample,
)
from opspilot.domain.subjects import Incident, SubjectRef, advance_incident
from tests.f6_boundary_support import ContractInterfaceConflict

END = datetime(2026, 10, 7, 12, tzinfo=UTC)
TARGET = Target(
    integration_id="otel-demo",
    cluster_uid="lab-cluster",
    namespace="demo",
    resource_uid="checkout-uid",
    revision="handled-revision",
)


@pytest.fixture
def session():
    return ObservationSession(
        session_id="recovery-1",
        purpose="incident_recovery",
        subject=SubjectRef(kind="incident", id="incident-1"),
        target=TARGET,
        subject_control_generation=3,
        observation_generation=2,
        health_profile_revision="checkout-v1",
        authorized=True,
        adopted_sequence=4,
        adopted_window_end=END,
    )


@pytest.fixture
def sample():
    return HealthSample(
        sample_id="sample-5",
        session_id="recovery-1",
        sequence=5,
        window=QueryWindow(start=END, end=END + timedelta(minutes=1)),
        outcome="healthy",
        subject_control_generation=3,
        observation_generation=2,
        health_profile_revision="checkout-v1",
        required_signals_present=True,
    )


@pytest.mark.parametrize("state", ["open", "observing_recovery"])
def test_current_sample_is_adopted_and_advances_watermarks(session, sample, state):
    before = session.model_dump()
    result = evaluate_sample(session, sample, subject_state=state)
    assert result.accepted and result.disposition == "adopted"
    active = session.model_copy(update={"active_sample_job_id": "job-5"})
    adopted = adopt_sample(active, sample)
    assert adopted.adopted_sequence == sample.sequence
    assert adopted.adopted_window_end == sample.window.end
    assert adopted.active_sample_job_id is None
    assert may_schedule_sample(adopted)
    assert confirms_health(adopted, sample)
    assert extends_healthy_window(adopted, sample)
    assert session.model_dump() == before


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"session_id": "another-session"}, "session_mismatch"),
        ({"subject_control_generation": 2}, "control_generation_stale"),
        ({"subject_control_generation": 4}, "control_generation_stale"),
        ({"observation_generation": 1}, "observation_generation_stale"),
        ({"observation_generation": 3}, "observation_generation_stale"),
        (
            {"health_profile_revision": "checkout-v0"},
            "health_profile_revision_mismatch",
        ),
        ({"sequence": 4}, "sequence_not_advancing"),
        ({"sequence": 3}, "sequence_not_advancing"),
        (
            {
                "window": QueryWindow(
                    start=END - timedelta(minutes=2), end=END - timedelta(minutes=1)
                )
            },
            "window_regressed",
        ),
    ],
    ids=[
        "foreign-session",
        "old-control",
        "future-control",
        "old-generation",
        "future-generation",
        "old-profile",
        "retry-sequence",
        "old-sequence",
        "regressed-window",
    ],
)
def test_invalid_sample_is_history_only_without_advancing(
    session, sample, change, reason
):
    before = session.model_dump()
    result = evaluate_sample(
        session, sample.model_copy(update=change), subject_state="observing_recovery"
    )
    assert not result.accepted
    assert result.disposition == "history_only"
    assert result.reason == reason
    assert session.model_dump() == before


@pytest.mark.parametrize("state", ["resolved", "closed"])
def test_only_open_lifecycles_can_adopt(session, sample, state):
    result = evaluate_sample(session, sample, subject_state=state)
    assert not result.accepted and result.disposition == "history_only"
    assert result.reason == "subject_state_not_adoptable"


@pytest.mark.parametrize(
    "flags,reason",
    [
        ({"suspension_blocks": True}, "suspended"),
        ({"within_deadline": False}, "deadline_expired"),
    ],
)
def test_suspension_and_original_deadline_block_adoption(
    session, sample, flags, reason
):
    result = evaluate_sample(
        session, sample, subject_state="observing_recovery", **flags
    )
    assert not result.accepted and result.disposition == "history_only"
    assert result.reason == reason


@pytest.mark.parametrize(
    "change",
    [
        {"authorized": False},
        {"state": "revoked"},
        {"state": "completed"},
        {"state": "expired"},
    ],
)
def test_revoked_or_ended_authority_blocks_adoption_and_scheduling(
    session, sample, change
):
    stopped = session.model_copy(update=change)
    result = evaluate_sample(stopped, sample, subject_state="observing_recovery")
    assert not result.accepted and result.disposition == "history_only"
    assert result.reason == "session_not_authorized"
    assert not may_schedule_sample(stopped)


def test_equal_window_end_is_allowed_but_duplicate_sequence_is_not(session, sample):
    same_end = sample.model_copy(
        update={"window": QueryWindow(start=END - timedelta(minutes=1), end=END)}
    )
    assert evaluate_sample(session, same_end, subject_state="open").accepted
    adopted = adopt_sample(session, same_end)
    assert not evaluate_sample(adopted, same_end, subject_state="open").accepted
    with pytest.raises(DomainError, match="STALE_RESULT"):
        adopt_sample(adopted, same_end)


def test_at_most_one_active_sampling_job(session):
    assert may_schedule_sample(session)
    assert not may_schedule_sample(
        session.model_copy(update={"active_sample_job_id": "job"})
    )


def test_no_profile_can_save_unknown_but_cannot_confirm_health(session, sample):
    unbound = session.model_copy(update={"health_profile_revision": None})
    unbound_sample = sample.model_copy(update={"health_profile_revision": None})
    assert evaluate_sample(unbound, unbound_sample, subject_state="open").accepted
    assert not confirms_health(unbound, unbound_sample)
    assert not extends_healthy_window(unbound, unbound_sample)


def test_missing_required_signals_cannot_confirm_health(session, sample):
    incomplete = sample.model_copy(update={"required_signals_present": False})
    assert evaluate_sample(session, incomplete, subject_state="open").accepted
    assert not confirms_health(session, incomplete)
    assert not extends_healthy_window(session, incomplete)


@pytest.mark.parametrize(
    "outcome", ["no_data", "stale", "timeout", "failed", "degraded"]
)
def test_nonhealthy_samples_are_adoptable_but_do_not_extend_health(
    session, sample, outcome
):
    observation = sample.model_copy(update={"outcome": outcome})
    assert evaluate_sample(
        session, observation, subject_state="observing_recovery"
    ).accepted
    adopted = adopt_sample(session, observation)
    assert adopted.adopted_sequence == observation.sequence
    assert adopted.adopted_window_end == observation.window.end
    assert not confirms_health(adopted, observation)
    assert not extends_healthy_window(adopted, observation)


@pytest.mark.parametrize("confirmed,expected", [(True, "resolved"), (False, "open")])
def test_observation_ends_with_confirmed_recovery_or_open_incident(confirmed, expected):
    incident = Incident(incident_id="incident-1", target=TARGET, opened_at=END)
    observing = advance_incident(incident, "start_recovery_observation")
    assert observing.lifecycle == "observing_recovery"
    trigger = "recovery_confirmed" if confirmed else "observation_ended_unconfirmed"
    assert advance_incident(observing, trigger).lifecycle == expected
    assert incident.lifecycle == "open"


@pytest.mark.parametrize("state", ["open", "observing_recovery", "resolved", "closed"])
@pytest.mark.parametrize("trigger", ["human_close", "human_reopen"])
def test_human_actions_never_directly_resolve_incident(state, trigger):
    incident = Incident(
        incident_id="incident-1", target=TARGET, opened_at=END, lifecycle=state
    )
    try:
        result = advance_incident(incident, trigger)
    except DomainError as error:
        assert error.code == "ILLEGAL_TRANSITION"
    else:
        assert result.lifecycle != "resolved"


@pytest.mark.parametrize("state", ["open", "resolved", "closed"])
def test_recovery_confirmation_cannot_bypass_observation_stage(state):
    incident = Incident(
        incident_id="incident-1", target=TARGET, opened_at=END, lifecycle=state
    )
    with pytest.raises(DomainError, match="ILLEGAL_TRANSITION"):
        advance_incident(incident, "recovery_confirmed")


def test_human_close_is_closed_and_reopen_needs_a_new_stage():
    incident = Incident(incident_id="incident-1", target=TARGET, opened_at=END)
    closed = advance_incident(incident, "human_close")
    assert closed.lifecycle == "closed"
    reopened = advance_incident(closed, "human_reopen")
    assert reopened.lifecycle == "open"
    assert (
        advance_incident(reopened, "start_recovery_observation").lifecycle
        == "observing_recovery"
    )


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="合同/接口冲突: 结束会话丢失无样本任务身份，公开存储缺全部已发放任务清单 见 opspilot/observation/revocation.py:53、opspilot/observation/store.py:1569",
)
@pytest.mark.parametrize("fault", ["revoke", "expire"])
def test_persisted_authority_guard_keeps_late_result_only_as_history(
    recovery_driver, f6_profile, fault
):
    from copy import deepcopy

    from tests.acceptance.test_f6_recovery import (
        HANDLED,
        assert_readonly,
        assert_signal_basis,
        observations,
        scenario,
        with_raw_payloads,
    )

    requested = scenario(1, f"persisted-authority-{fault}")
    started = recovery_driver.run(
        requested,
        profile=f6_profile,
        handled_at=HANDLED,
        observations=[],
        until=HANDLED,
    )
    assert started.incident_lifecycle == "observing_recovery"
    recovery_driver.prepare_submission(requested.subject_id)
    # Finish real telemetry collection, then revoke/expire the persisted
    # authority immediately BEFORE calling the public submit_sample API.
    recovery_driver.fault_at_submission(fault)
    supplied = with_raw_payloads(observations(1))
    outcome = recovery_driver.continue_observation(
        requested,
        observations=deepcopy(supplied),
        until=supplied[-1]["window_end"],
    )
    before, after = recovery_driver.submission_snapshots()
    assert before["observation_authorization"]["authorized"] is False
    for field in (
        "target",
        "incident_lifecycle",
        "control_state",
        "adopted_sequence",
        "adopted_window_end",
        "healthy_window_seconds",
        "used_sample_count",
        "observation_authorization",
        "observation_sessions",
        "handling_audit",
    ):
        assert after[field] == before[field], field
    assert before["recovery_samples"] == ()
    assert len(after["recovery_samples"]) == 1
    sample = after["recovery_samples"][0]
    assert sample["disposition"] == "history_only"
    assert sample["session_id"] == before["observation_authorization"]["session_id"]
    assert sample["subject_id"] == requested.subject_id
    assert_signal_basis(sample, supplied[0], recovery_driver)
    assert outcome.recovery_confirmed is False
    assert_readonly(outcome, recovery_driver)
    try:
        assert after["sample_jobs"] == before["sample_jobs"], "sample_jobs"
    except AssertionError as exc:
        # The external lease witness identifies the OLD job. It is never
        # inserted into, or described as, the persistent snapshot.
        if before["sample_jobs"] == () and after["sample_jobs"] == (
            recovery_driver.submitted_job_witness(),
        ):
            raise ContractInterfaceConflict(
                "ended job identity absent until history is stored"
            ) from exc
        raise
