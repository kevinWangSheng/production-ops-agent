"""Domain contracts from C3 section 4.

Three separations are checked directly: incident lifecycle against release
observation status, Run execution against investigation result, and human
control as an ``expected_version`` conditional update under global/target
suspension. The remaining tests cover the per-object invariants those three
depend on.
"""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from opspilot.domain import (
    AuditRecord,
    BudgetLedger,
    Conclusion,
    ControlState,
    DomainError,
    Evidence,
    ExportOutboxEntry,
    HealthSample,
    Incident,
    InputEvent,
    Integration,
    Job,
    KnowledgeRevision,
    ModelProfile,
    ModelStep,
    ObservationSession,
    Postmortem,
    QueryScope,
    QueryWindow,
    ReleaseIdentity,
    ReleaseObservation,
    ReleaseObservationPolicy,
    Run,
    Schedule,
    ScopeVersions,
    SubjectEvent,
    SubjectRef,
    SuspensionState,
    Target,
    ToolOperation,
    ToolPlanEntry,
    adopt_sample,
    advance_evidence,
    advance_incident,
    advance_job,
    advance_knowledge,
    advance_outbox,
    advance_postmortem,
    advance_release,
    advance_run,
    advance_session,
    advance_tool_operation,
    advance_watermark,
    apply_control,
    automatic_investigation_allowed,
    check_execution_identity,
    check_scope_versions,
    claim_job,
    claim_run,
    confirms_health,
    counts_as_observed,
    delivery_key,
    disposition_for_new_anomaly,
    evaluate_conclusion,
    evaluate_sample,
    event_intake_allowed,
    execution_key,
    extends_healthy_window,
    human_control_allowed,
    is_duplicate_delivery,
    may_enter_model_input,
    may_execute,
    may_execute_tools,
    may_schedule_sample,
    next_sequence,
    observation_sampling_allowed,
    pending_tool_indices,
    publish_knowledge,
    revoke_restricted,
    step_id,
    successor_observation,
    supersede_schedule,
    suspend_globally,
    suspend_targets,
    tool_operation_id,
)

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def target(uid: str = "svc-a") -> Target:
    return Target(
        integration_id="k8s-prod",
        cluster_uid="cluster-1",
        namespace="prod",
        resource_uid=uid,
        revision="rev-1",
    )


def incident(**overrides) -> Incident:
    return Incident(
        **{"incident_id": "inc-1", "target": target(), "opened_at": NOW, **overrides}
    )


def policy(**overrides) -> ReleaseObservationPolicy:
    return ReleaseObservationPolicy(
        **{
            "released_at": NOW,
            "min_tracking_seconds": 600,
            "sample_interval_seconds": 60,
            "healthy_window_seconds": 300,
            "max_samples": 20,
            "deadline": NOW + timedelta(hours=2),
            **overrides,
        }
    )


def release(**overrides) -> ReleaseObservation:
    identity = ReleaseIdentity(
        source="argocd",
        release_id="deploy-77",
        target=target(),
        release_revision="rev-2",
    )
    return ReleaseObservation(
        **{
            "observation_id": "rel-1",
            "identity": identity,
            "policy": policy(),
            **overrides,
        }
    )


def run(subject: SubjectRef | None = None, **overrides) -> Run:
    return Run(
        **{
            "run_id": "run-1",
            "subject": subject or SubjectRef(kind="incident", id="inc-1"),
            "model_profile": ModelProfile(
                provider="deepseek",
                model="deepseek-v4-flash",
                endpoint_mode="chat_completions",
                prompt_revision="p1",
                adapter_revision="a1",
                tool_schema_revision="t1",
            ),
            "budget": BudgetLedger(deadline=NOW + timedelta(minutes=10)),
            **overrides,
        }
    )


def window(minutes: int = 5) -> QueryWindow:
    return QueryWindow(start=NOW, end=NOW + timedelta(minutes=minutes))


def session(**overrides) -> ObservationSession:
    return ObservationSession(
        **{
            "session_id": "obs-1",
            "purpose": "incident_recovery",
            "subject": SubjectRef(kind="incident", id="inc-1"),
            "target": target(),
            "subject_control_generation": 0,
            "observation_generation": 0,
            "authorized": True,
            "health_profile_revision": "hp-1",
            **overrides,
        }
    )


def sample(**overrides) -> HealthSample:
    return HealthSample(
        **{
            "sample_id": "s-1",
            "session_id": "obs-1",
            "sequence": 1,
            "window": window(),
            "outcome": "healthy",
            "subject_control_generation": 0,
            "observation_generation": 0,
            "health_profile_revision": "hp-1",
            "required_signals_present": True,
            **overrides,
        }
    )


# 1. Incident lifecycle and release observation status are modelled separately.


def test_a_release_observation_completes_without_any_incident():
    observation = release()
    observation = advance_release(observation, "observation_started")
    assert observation.status == "observing"
    observation = advance_release(
        observation,
        "healthy_window_satisfied",
        now=observation.policy.earliest_healthy_at,
    )
    assert observation.status == "healthy"
    assert observation.incident_id is None


def test_an_abnormal_release_links_or_creates_an_incident():
    observation = advance_release(release(), "observation_started")
    with pytest.raises(DomainError) as excinfo:
        advance_release(observation, "anomaly_detected")
    assert excinfo.value.code == "INVALID_INPUT"
    linked = advance_release(observation, "anomaly_detected", incident_id="inc-9")
    assert (linked.status, linked.incident_id) == ("anomalous", "inc-9")


def test_healthy_before_the_minimum_tracking_span_stays_observing():
    observation = advance_release(release(), "observation_started")
    too_early = observation.policy.earliest_healthy_at - timedelta(seconds=1)
    with pytest.raises(DomainError) as excinfo:
        advance_release(observation, "healthy_window_satisfied", now=too_early)
    assert excinfo.value.code == "ILLEGAL_TRANSITION"
    assert observation.status == "observing"


def test_a_required_window_may_not_exceed_the_release_deadline():
    with pytest.raises(ValidationError, match="REQUIRED_WINDOW_EXCEEDS_DEADLINE"):
        policy(deadline=NOW + timedelta(seconds=60))


def test_pausing_a_release_keeps_its_status_and_deadline():
    observation = advance_release(release(), "observation_started")
    paused = observation.model_copy(
        update={"control": apply_control(observation.control, "pause", 0)}
    )
    assert paused.control.paused is True
    assert paused.status == "observing"
    assert paused.policy.deadline == observation.policy.deadline


def test_only_an_independent_recovery_observation_resolves_an_incident():
    opened = incident()
    closed = advance_incident(opened, "human_close")
    assert closed.lifecycle == "closed"
    with pytest.raises(DomainError, match="ILLEGAL_TRANSITION"):
        advance_incident(opened, "recovery_confirmed")
    observing = advance_incident(opened, "start_recovery_observation")
    assert advance_incident(observing, "recovery_confirmed").lifecycle == "resolved"


def test_continued_degradation_leaves_the_incident_open():
    observing = advance_incident(incident(), "start_recovery_observation")
    back = advance_incident(observing, "observation_ended_unconfirmed")
    assert back.lifecycle == "open"


def test_a_new_anomaly_on_a_closed_incident_defaults_to_a_new_incident():
    assert disposition_for_new_anomaly(incident()) == "attach_event"
    closed = advance_incident(incident(), "human_close")
    assert disposition_for_new_anomaly(closed) == "new_related_incident"
    assert advance_incident(closed, "human_reopen").lifecycle == "open"


def test_observing_again_after_a_terminal_status_creates_a_new_record():
    finished = advance_release(
        advance_release(release(), "observation_started"),
        "deadline_expired",
    )
    successor = successor_observation(
        finished,
        observation_id="rel-2",
        identity=finished.identity,
        policy=finished.policy,
    )
    assert successor.status == "pending"
    assert successor.supersedes_observation_id == "rel-1"
    with pytest.raises(DomainError, match="ILLEGAL_TRANSITION"):
        successor_observation(
            release(),
            observation_id="rel-3",
            identity=finished.identity,
            policy=finished.policy,
        )


def test_both_subjects_own_a_control_and_an_observation_generation():
    for subject in (incident(), release()):
        assert subject.control.control_generation == 0
        assert subject.observation_generation == 0
        assert subject.current_run_id is None
    assert incident().ref == SubjectRef(kind="incident", id="inc-1")
    assert release().ref == SubjectRef(kind="release_observation", id="rel-1")


def test_a_release_session_may_not_stand_on_an_incident_subject():
    with pytest.raises(ValidationError, match="SUBJECT_KIND_MISMATCH"):
        session(purpose="release_observation")


def test_an_incident_recovery_sample_is_not_adoptable_on_a_release_state():
    decision = evaluate_sample(session(), sample(), subject_state="observing")
    assert decision.disposition == "history_only"
    assert decision.reason == "subject_state_not_adoptable"


def test_recovery_results_never_rewrite_a_resolved_or_closed_incident():
    for state in ("resolved", "closed"):
        decision = evaluate_sample(session(), sample(), subject_state=state)
        assert decision.accepted is False


# 2. Run execution status and investigation result are separate.


def test_a_run_carries_no_investigation_result_field():
    finished = advance_run(claim_run_now(), "finished")
    assert finished.execution == "completed"
    assert "result" not in Run.model_fields


def test_a_completed_run_may_still_be_inconclusive():
    finished = advance_run(claim_run_now(), "finished")
    subject = finished.subject
    conclusion = Conclusion(
        run_id=finished.run_id,
        subject=subject,
        result="inconclusive",
        recorded_at=NOW,
        control_generation=0,
    )
    decision = evaluate_conclusion(
        conclusion,
        subject=subject,
        current_run_id=finished.run_id,
        control_generation=0,
    )
    assert (decision.accepted, decision.disposition) == (True, "current")


def test_a_late_run_conclusion_is_kept_as_history_only():
    subject = SubjectRef(kind="incident", id="inc-1")
    conclusion = Conclusion(
        run_id="run-old",
        subject=subject,
        result="supported",
        recorded_at=NOW,
        control_generation=0,
    )
    decision = evaluate_conclusion(
        conclusion, subject=subject, current_run_id="run-2", control_generation=0
    )
    assert decision.disposition == "history_only"
    assert decision.reason == "not_current_run"


def test_a_release_run_may_not_write_an_incident_conclusion():
    release_subject = SubjectRef(kind="release_observation", id="rel-1")
    conclusion = Conclusion(
        run_id="run-r",
        subject=release_subject,
        result="supported",
        recorded_at=NOW,
        control_generation=0,
    )
    decision = evaluate_conclusion(
        conclusion,
        subject=SubjectRef(kind="incident", id="inc-1"),
        current_run_id="run-r",
        control_generation=0,
    )
    assert decision.disposition == "history_only"
    assert decision.reason == "subject_mismatch"


def test_a_conclusion_from_a_stale_control_generation_is_history_only():
    subject = SubjectRef(kind="incident", id="inc-1")
    conclusion = Conclusion(
        run_id="run-1",
        subject=subject,
        result="partial",
        recorded_at=NOW,
        control_generation=0,
    )
    decision = evaluate_conclusion(
        conclusion, subject=subject, current_run_id="run-1", control_generation=1
    )
    assert decision.reason == "control_generation_stale"


def claim_run_now(**overrides) -> Run:
    return claim_run(
        run(**overrides),
        owner="worker-1",
        epoch=1,
        lease_expires_at=NOW + timedelta(minutes=5),
    )


def test_each_execution_attempt_takes_a_new_epoch():
    claimed = claim_run_now()
    assert (claimed.execution, claimed.epoch, claimed.owner) == (
        "running",
        1,
        "worker-1",
    )
    with pytest.raises(DomainError, match="INVALID_INPUT"):
        claim_run(
            run(execution="queued", epoch=3),
            owner="worker-1",
            epoch=3,
            lease_expires_at=NOW,
        )


def test_a_paused_run_resumes_as_a_new_attempt():
    paused = advance_run(claim_run_now(), "human_pause")
    assert (paused.execution, paused.owner) == ("paused", None)
    requeued = advance_run(paused, "human_resume")
    assert requeued.execution == "queued"
    resumed = claim_run(
        requeued, owner="worker-2", epoch=2, lease_expires_at=NOW + timedelta(minutes=5)
    )
    assert (resumed.epoch, resumed.execution) == (2, "running")


def test_a_blocked_run_is_continued_by_a_new_run():
    blocked = advance_run(claim_run_now(), "incompatible_state")
    assert (blocked.execution, blocked.blocked_reason) == (
        "blocked",
        "INCOMPATIBLE_STATE",
    )
    with pytest.raises(DomainError, match="ILLEGAL_TRANSITION"):
        advance_run(blocked, "claimed")
    assert advance_run(blocked, "human_cancel").execution == "cancelled"


def test_the_input_watermark_only_moves_forward_and_only_while_running():
    running = advance_watermark(claim_run_now(), 7)
    assert running.input_watermark == 7
    with pytest.raises(DomainError) as excinfo:
        advance_watermark(running, 6)
    assert excinfo.value.code == "STALE_RESULT"
    paused = advance_run(running, "human_pause")
    with pytest.raises(DomainError) as excinfo:
        advance_watermark(paused, 9)
    assert excinfo.value.code == "ILLEGAL_TRANSITION"
    assert paused.input_watermark == 7


# 3. Human control is an expected_version conditional update under suspension.


def test_a_control_operation_requires_the_expected_version():
    state = ControlState()
    with pytest.raises(DomainError) as excinfo:
        apply_control(state, "pause", 1)
    assert excinfo.value.code == "CONTROL_CONFLICT"
    assert state == ControlState()


def test_each_accepted_control_increments_the_control_generation():
    state = apply_control(ControlState(), "pause", 0)
    assert (state.control_generation, state.paused) == (1, True)
    state = apply_control(state, "resume", 1)
    assert (state.control_generation, state.paused) == (2, False)
    with pytest.raises(DomainError, match="CONTROL_CONFLICT"):
        apply_control(state, "resume", 1)


def test_takeover_stops_automatic_observation():
    state = apply_control(ControlState(observation_authorized=True), "takeover", 0)
    assert state.mode == "human_owned"
    assert state.observation_authorized is False


def test_separate_observation_authorization_does_not_restore_automatic_work():
    with pytest.raises(DomainError, match="ILLEGAL_TRANSITION"):
        apply_control(ControlState(), "authorize_observation", 0)
    taken = apply_control(ControlState(), "takeover", 0)
    authorized = apply_control(taken, "authorize_observation", 1)
    assert authorized.mode == "human_owned"
    assert authorized.observation_authorized is True
    assert not automatic_investigation_allowed(
        control=authorized,
        suspension=SuspensionState(),
        target=target(),
        lifecycle_allows=True,
    )
    assert observation_sampling_allowed(
        control=authorized,
        suspension=SuspensionState(),
        target=target(),
        lifecycle_allows=True,
    )


def test_resume_does_not_restore_an_observation_authorization():
    paused = apply_control(ControlState(mode="human_owned"), "pause", 0)
    resumed = apply_control(paused, "resume", 1)
    assert resumed.observation_authorized is False


@pytest.mark.parametrize("scope", ["global", "target"])
def test_suspension_outranks_resume_mode_and_observation_authorization(scope):
    control = ControlState(mode="human_owned", observation_authorized=True)
    control = apply_control(control, "resume", 0)
    base = SuspensionState()
    suspension = (
        suspend_globally(base, True)
        if scope == "global"
        else suspend_targets(base, [target()])
    )
    assert suspension.blocks(target())
    assert not automatic_investigation_allowed(
        control=control,
        suspension=suspension,
        target=target(),
        lifecycle_allows=True,
    )
    assert not observation_sampling_allowed(
        control=control,
        suspension=suspension,
        target=target(),
        lifecycle_allows=True,
    )
    assert event_intake_allowed(suspension)
    assert human_control_allowed(suspension)


def test_a_suspension_transaction_increments_its_scope_generation():
    state = suspend_globally(SuspensionState(), True)
    assert (state.global_generation, state.target_generation) == (1, 0)
    state = suspend_targets(state, [target("svc-b")])
    assert (state.global_generation, state.target_generation) == (1, 1)


def test_target_scope_resolves_to_registered_identities_not_names():
    for names in (["svc-a"], "svc-a", [], [{"resource_uid": "svc-a"}]):
        with pytest.raises(DomainError, match="INVALID_INPUT"):
            suspend_targets(SuspensionState(), names)


def test_an_adoption_rechecks_every_recorded_control_version():
    observed = ScopeVersions(
        subject_control_generation=1,
        global_suspension_generation=2,
        target_suspension_generation=3,
    )
    check_scope_versions(observed, observed)
    moved = observed.model_copy(update={"global_suspension_generation": 3})
    with pytest.raises(DomainError) as excinfo:
        check_scope_versions(observed, moved)
    assert excinfo.value.code == "CONTROL_CONFLICT"


# Per-object invariants the three separations depend on.


def test_an_integration_cannot_carry_a_credential():
    for field in ("token", "password", "credentials", "api_key"):
        with pytest.raises(ValidationError, match="extra_forbidden"):
            Integration(
                integration_id="k8s-prod",
                kind="kubernetes",
                revision="v1",
                endpoint_ref="cfg://k8s-prod",
                **{field: "secret"},
            )


def test_delivery_keys_prefer_a_stable_source_event_id():
    first = delivery_key(source="alertmanager", external_event_id="a-1")
    assert first == delivery_key(source="alertmanager", external_event_id="a-1")
    assert first != delivery_key(source="alertmanager", external_event_id="a-2")
    assert first != delivery_key(source="grafana", external_event_id="a-1")


def test_a_composite_delivery_key_refuses_to_dedupe_on_a_name_alone():
    with pytest.raises(DomainError, match="INVALID_INPUT"):
        delivery_key(source="k8s", object_identity="svc-a")
    complete = dict(
        source="k8s",
        source_instance="cluster-1",
        object_identity="svc-a",
        state_time=NOW,
        payload_hash="h1",
    )
    key = delivery_key(**complete)
    assert key != delivery_key(**{**complete, "state_time": NOW + timedelta(hours=1)})
    assert key != delivery_key(**{**complete, "payload_hash": "h2"})
    with pytest.raises(DomainError, match="INVALID_INPUT"):
        delivery_key(**{**complete, "state_time": NOW.replace(tzinfo=None)})


def test_duplicate_deliveries_are_detected_by_delivery_key():
    key = delivery_key(source="alertmanager", external_event_id="a-1")
    event = InputEvent(
        event_id="e-1",
        source="alertmanager",
        delivery_key=key,
        received_at=NOW,
        sequence=1,
        external_event_id="a-1",
    )
    assert is_duplicate_delivery(event, frozenset({key}))
    assert not is_duplicate_delivery(event, frozenset())


def test_a_stable_step_key_rebuilds_and_tools_wait_for_the_committed_response():
    first = step_id("run-1", "segment-a", 3)
    assert first == step_id("run-1", "segment-a", 3)
    assert first != step_id("run-1", "segment-a", 4)
    assert tool_operation_id(first, 0) != tool_operation_id(first, 1)
    plan = (
        ToolPlanEntry(
            tool_index=0, tool_name="k8s_get", tool_version="1", arguments_hash="h0"
        ),
        ToolPlanEntry(
            tool_index=1, tool_name="k8s_logs", tool_version="1", arguments_hash="h1"
        ),
    )
    step = ModelStep(
        step_id=first,
        run_id="run-1",
        context_segment="segment-a",
        logical_round=3,
        input_snapshot_hash="snap",
        tool_plan=plan,
    )
    assert not may_execute_tools(step)
    with pytest.raises(DomainError, match="ILLEGAL_TRANSITION"):
        pending_tool_indices(step, frozenset())
    committed = step.model_copy(update={"response_committed": True})
    assert may_execute_tools(committed)
    assert pending_tool_indices(committed, frozenset({0})) == (1,)


def tool_operation(**overrides) -> ToolOperation:
    scope = QueryScope(
        tool_name="k8s_logs",
        tool_version="1",
        integration_id="k8s-prod",
        target=target(),
        window=window(),
        result_bytes_limit=65536,
        deadline_seconds=30,
    )
    return ToolOperation(
        **{
            "operation_id": "op-1",
            "run_id": "run-1",
            "step_id": "step-1",
            "tool_index": 0,
            "scope": scope,
            **overrides,
        }
    )


def test_an_unknown_tool_state_is_never_treated_as_success():
    dispatched = advance_tool_operation(tool_operation(), "dispatched")
    unknown = advance_tool_operation(dispatched, "state_unknown")
    assert unknown.state == "unknown"
    assert not counts_as_observed(unknown)
    assert counts_as_observed(advance_tool_operation(dispatched, "result_committed"))
    for trigger in ("cancelled", "error_committed"):
        assert not counts_as_observed(advance_tool_operation(dispatched, trigger))


def evidence(**overrides) -> Evidence:
    return Evidence(
        **{
            "evidence_id": "ev-1",
            "operation_id": "op-1",
            "run_id": "run-1",
            "source": "kubernetes",
            "target": target(),
            "window": window(),
            "observed_at": NOW,
            "freshness_seconds": 30,
            "raw_hash": "r1",
            "view_hash": "v1",
            **overrides,
        }
    )


def test_only_adopted_evidence_may_enter_a_model_input():
    recorded = evidence()
    assert not may_enter_model_input(recorded)
    adopted = advance_evidence(recorded, "adopt")
    assert may_enter_model_input(adopted)
    rejected = advance_evidence(recorded, "reject")
    assert not may_enter_model_input(rejected)


def test_revoking_an_authorization_keeps_history_but_stops_reuse():
    adopted = advance_evidence(evidence(restricted=True), "adopt")
    unrestricted = advance_evidence(evidence(evidence_id="ev-2"), "adopt")
    after = revoke_restricted((adopted, unrestricted))
    assert after[0].adoption == "revoked"
    assert not may_enter_model_input(after[0])
    assert after[0].evidence_id == "ev-1"
    assert after[0].raw_hash == "r1"
    assert may_enter_model_input(after[1])


def schedule(**overrides) -> Schedule:
    return Schedule(
        **{
            "schedule_id": "sch-1",
            "subject": SubjectRef(kind="incident", id="inc-1"),
            "kind": "recovery_sample",
            "due_at": NOW,
            **overrides,
        }
    )


def job(sch: Schedule, **overrides) -> Job:
    return Job(
        **{
            "job_id": "job-1",
            "schedule_id": sch.schedule_id,
            "subject": sch.subject,
            "schedule_generation": sch.generation,
            "due_at": sch.due_at,
            "execution_key": execution_key(sch.schedule_id, sch.generation, sch.due_at),
            **overrides,
        }
    )


def test_the_execution_key_is_schedule_generation_and_due_time():
    key = execution_key("sch-1", 0, NOW)
    assert key == execution_key("sch-1", 0, NOW)
    assert key != execution_key("sch-1", 1, NOW)
    assert key != execution_key("sch-1", 0, NOW + timedelta(minutes=1))
    with pytest.raises(DomainError, match="INVALID_INPUT"):
        execution_key("sch-1", 0, NOW.replace(tzinfo=None))


def test_a_superseded_schedule_generation_never_executes_again():
    sch = schedule()
    pending = job(sch)
    assert may_execute(pending, sch)
    changed = supersede_schedule(sch)
    assert not may_execute(pending, changed)
    with pytest.raises(DomainError, match="ILLEGAL_TRANSITION"):
        claim_job(
            pending,
            owner="w-1",
            epoch=0,
            lease_expires_at=NOW + timedelta(minutes=1),
            schedule=changed,
        )


def test_a_terminal_job_never_re_executes_and_an_expired_lease_raises_the_epoch():
    sch = schedule()
    leased = claim_job(
        job(sch),
        owner="w-1",
        epoch=0,
        lease_expires_at=NOW + timedelta(minutes=1),
        schedule=sch,
    )
    assert (leased.state, leased.attempts) == ("leased", 1)
    done = advance_job(leased, "committed")
    assert not may_execute(done, sch)
    requeued = advance_job(leased, "lease_expired")
    assert (requeued.state, requeued.epoch, requeued.owner) == ("pending", 1, None)


def test_commit_verifies_execution_identity_and_a_live_lease():
    sch = schedule()
    leased = claim_job(
        job(sch),
        owner="w-1",
        epoch=0,
        lease_expires_at=NOW + timedelta(minutes=1),
        schedule=sch,
    )
    check_execution_identity(leased, owner="w-1", epoch=0, now=NOW)
    for kwargs in (
        {"owner": "w-2", "epoch": 0, "now": NOW},
        {"owner": "w-1", "epoch": 1, "now": NOW},
        {"owner": "w-1", "epoch": 0, "now": NOW + timedelta(minutes=2)},
    ):
        with pytest.raises(DomainError) as excinfo:
            check_execution_identity(leased, **kwargs)
        assert excinfo.value.code == "CONTROL_CONFLICT"


def test_a_sample_is_adopted_only_once_and_only_moving_forward():
    live = session()
    first = sample()
    assert evaluate_sample(live, first, subject_state="open").accepted
    live = adopt_sample(live, first)
    assert live.adopted_sequence == 1
    replay = evaluate_sample(live, first, subject_state="open")
    assert replay.reason == "sequence_not_advancing"
    regressed = sample(sample_id="s-2", sequence=2, window=window(minutes=1))
    assert (
        evaluate_sample(live, regressed, subject_state="open").reason
        == "window_regressed"
    )
    with pytest.raises(DomainError, match="STALE_RESULT"):
        adopt_sample(live, first)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"session_id": "other"}, "session_mismatch"),
        ({"subject_control_generation": 1}, "control_generation_stale"),
        ({"observation_generation": 1}, "observation_generation_stale"),
        ({"health_profile_revision": "hp-2"}, "health_profile_revision_mismatch"),
    ],
)
def test_a_sample_with_a_stale_binding_stays_history_only(overrides, reason):
    decision = evaluate_sample(session(), sample(**overrides), subject_state="open")
    assert (decision.accepted, decision.reason) == (False, reason)


def test_a_revoked_session_and_a_suspended_scope_stop_adoption():
    revoked = advance_session(session(), "authority_revoked")
    assert (
        evaluate_sample(revoked, sample(), subject_state="open").reason
        == "session_not_authorized"
    )
    assert (
        evaluate_sample(
            session(), sample(), subject_state="open", suspension_blocks=True
        ).reason
        == "suspended"
    )
    assert (
        evaluate_sample(
            session(), sample(), subject_state="open", within_deadline=False
        ).reason
        == "deadline_expired"
    )


def test_a_legal_no_data_sample_is_adoptable_but_never_healthy():
    live = session()
    missing = sample(outcome="no_data", required_signals_present=False)
    assert evaluate_sample(live, missing, subject_state="open").accepted
    assert not confirms_health(live, missing)
    assert not extends_healthy_window(live, missing)
    assert confirms_health(live, sample())


def test_without_a_health_profile_recovery_is_never_confirmed():
    unprofiled = session(health_profile_revision=None)
    unconfirmable = sample(health_profile_revision=None)
    assert evaluate_sample(unprofiled, unconfirmable, subject_state="open").accepted
    assert not confirms_health(unprofiled, unconfirmable)


def test_one_active_sampling_job_per_session():
    assert may_schedule_sample(session())
    assert not may_schedule_sample(session(active_sample_job_id="job-1"))
    assert not may_schedule_sample(session(authorized=False))
    assert not may_schedule_sample(advance_session(session(), "authority_revoked"))


def test_subject_event_sequences_are_per_subject_and_never_repeat():
    incident_ref = SubjectRef(kind="incident", id="inc-1")
    release_ref = SubjectRef(kind="release_observation", id="rel-1")
    events = (
        SubjectEvent(
            event_id="se-1",
            subject=incident_ref,
            sequence=1,
            kind="opened",
            occurred_at=NOW,
        ),
        SubjectEvent(
            event_id="se-2",
            subject=incident_ref,
            sequence=2,
            kind="run_started",
            occurred_at=NOW,
        ),
    )
    assert next_sequence(events, incident_ref) == 3
    assert next_sequence(events, release_ref) == 1


def test_an_audit_record_keeps_the_expected_version_and_its_result():
    record = AuditRecord(
        audit_id="aud-1",
        subject=SubjectRef(kind="incident", id="inc-1"),
        actor="operator-1",
        action="pause",
        expected_version=0,
        resulting_control_generation=1,
        recorded_at=NOW,
        idempotency_key="idem-1",
    )
    assert record.expected_version == 0
    assert record.resulting_control_generation == 1
    # An audit record can only carry a declared read-only control action.
    for action in ("deploy", "rollback", "restart", "scale"):
        with pytest.raises(ValidationError):
            AuditRecord.model_validate(record.model_dump() | {"action": action})


def test_an_export_entry_records_a_bounded_drop_rather_than_losing_it():
    entry = ExportOutboxEntry(
        entry_id="out-1",
        idempotency_key="idem-1",
        payload_kind="trace",
        payload_hash="h1",
        created_at=NOW,
    )
    assert advance_outbox(entry, "delivered").state == "delivered"
    assert advance_outbox(entry, "bounded_drop").state == "dropped"


def test_knowledge_needs_a_human_reviewed_postmortem():
    draft = Postmortem(
        postmortem_id="pm-1",
        subject=SubjectRef(kind="incident", id="inc-1"),
        content_hash="c1",
        created_at=NOW,
    )
    with pytest.raises(DomainError, match="ILLEGAL_TRANSITION"):
        publish_knowledge(
            draft, revision_id="kr-1", approved_by="operator-1", approved_at=NOW
        )
    under_review = advance_postmortem(draft, "submit_for_review")
    with pytest.raises(DomainError, match="INVALID_INPUT"):
        advance_postmortem(under_review, "human_approve")
    approved = advance_postmortem(under_review, "human_approve", reviewer="operator-1")
    revision = publish_knowledge(
        approved, revision_id="kr-1", approved_by="operator-1", approved_at=NOW
    )
    assert (revision.state, revision.approver_kind) == ("active", "human")
    assert advance_knowledge(revision, "revoke").state == "revoked"


def test_a_platform_or_model_approver_is_unrepresentable():
    for approver_kind in ("model", "platform", "judge"):
        with pytest.raises(ValidationError):
            KnowledgeRevision(
                revision_id="kr-1",
                postmortem_id="pm-1",
                content_hash="c1",
                approver_kind=approver_kind,
                approved_by="llm-judge",
                approved_at=NOW,
            )


def test_domain_objects_are_frozen():
    subject = incident()
    with pytest.raises(ValidationError):
        subject.lifecycle = "resolved"
