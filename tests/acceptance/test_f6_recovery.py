"""F6 external scenarios driven by public product APIs on opt-in PostgreSQL.

Minimum external interface proposal (M1-02 steps 4/5/6; full lists in docs):
* run(IncidentScenario, *, profile, handled_at, observations, until) returns
  an observable outcome BOUND to scenario.subject_id, never a preset verdict.
* Outcome adds subject_id, incident_lifecycle, recovery_confirmed (True only
  for resolved), latest_sample_verdict (not frozen for intermediate results),
  terminal recovery_verdict/reasons, healthy_window_seconds, recovery_samples,
  frozen recovery_profile, recovery_handled_at, observation_ended/model_requests,
  used_sample_count (persisted session budget consumption, including unknown).
* Persisted samples include subject_id, sample_id, sequence, target, both
  generations, profile revision, absolute window, signals/outcome/disposition.
  Signals retain value/source/query/observed_at and unique evidence_id/hash.
  Each stimulus supplies test-owned raw_payload bytes, frozen after mutations;
  the telemetry stub returns those bytes for query + absolute window. Retrieved
  evidence must equal them byte-for-byte as well as matching its SHA-256.
* replay uses ONLY frozen profile/handled_at/samples. It returns external_queries
  and model_requests empty; integrity mismatch may be reported explicitly as
  unknown instead of restoring the original verdict.
* recovery_runtime(boundaries) must inject harness-owned telemetry_query and
  environment_write/model_request stubs into EVERY transport, with no network fallback.
  GuardedRecoveryDriver owns replay mode, environment_writes and
  telemetry_calls_during_replay/model_calls; these do not come from product self-report.
* seed_incident/snapshot_incident expose OpsPilot setup and read-only lifecycle,
  observation_sessions/recovery_samples by id for the two-incidents scenario.
* read_raw_payload(evidence_id) MUST resolve every evidence id to captured bytes
  for SHA-256 verification; missing readers/payloads fail.
* The driver seeds and retains immutable incident targets; stored adopted samples
  must match that target and the frozen profile, not just the supplied stimulus.
* continue_observation submits to an existing authorized session without another
  handling action. Snapshots expose adopted_sequence/adopted_window_end, target,
  healthy_window_seconds and used_sample_count to verify rejected samples have
  no effect on lifecycle/watermarks/health/budget. Snapshots also retain
  observation_authorization (session identity/binding/versions/authorized) and
  handling_audit so re-authorization cannot masquerade as sample submission.
  Full observation_sessions and REQUIRED sample_jobs collections stay unchanged
  after a rejected foreign-target sample, not only the current authorization.
  sample_jobs compares all incident jobs as a set of job_id/session_id/sequence.
  due_at/state/lease fields are excluded: releasing or delaying the SAME task
  after rejection is not scheduling a subsequent logical sample.
  observation_sessions projects only session_id/purpose/subject/target, both
  generations/state/authorized/profile revision, adopted sequence/window end
  and active_sample_job_id; no auxiliary counts or creation/update timestamps.
* actions is the complete product action audit; excludes engineer setup and
  permits only read queries and OpsPilot's own record/control persistence.

The driver lives in tests/f6_product_driver.py; fixture setup is shared in
tests/f6_fixtures.py. PG setup errors are not product acceptance failures.
Numbers are synthetic test boundaries, not calibrated lab HealthProfile values.
"""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace

import pytest

from opspilot.acceptance import IncidentScenario
from tests.f6_boundary_support import (
    GuardedRecoveryDriver,
    RecoveryBoundaries,
)

HANDLED = datetime(2026, 10, 7, 12, tzinfo=UTC)
REQUIRED = ("deployment", "request_volume", "errors", "latency", "pods", "dependencies")
TARGET = {
    "integration_id": "otel-demo",
    "cluster_uid": "lab",
    "namespace": "demo",
    "resource_uid": "checkout",
    "revision": "handled-revision",
}
READONLY_ACTIONS = {
    "read_only_query",
    "record_handling",
    "persist_observation",
    "advance_incident_lifecycle",
    "human_handoff",
}


@pytest.fixture
def profile():
    return {
        "revision": "synthetic-checkout-v1",
        "target": TARGET,
        "required_signals": REQUIRED,
        "minimum_requests": 100,
        "max_error_ratio": 0.01,
        "max_latency_ms": 500,
        "required_deployment_available": True,
        "required_pods_ready": True,
        "required_dependencies_healthy": True,
        "freshness_seconds": 60,
        "sample_interval_seconds": 60,
        "max_coverage_gap_seconds": 0,
        "healthy_window_seconds": 180,
        "deadline": HANDLED + timedelta(seconds=300),
        "max_samples": 5,
    }


def scenario(step, kind, subject_id="incident-f6"):
    return IncidentScenario(
        scenario_id=f"F6:{step}:{kind}",
        feature_id="F6",
        acceptance_step=str(step),
        kind=kind,
        subject_id=subject_id,
    )


def observations(
    count=5, *, traffic=200, error_ratio=0.001, missing=None, degraded=False
):
    """Raw signal stimuli in contiguous, post-handling absolute windows."""
    rows = []
    for index in range(count):
        start = HANDLED + timedelta(seconds=index * 60)
        end = start + timedelta(seconds=60)
        values = {
            "deployment": True,
            "request_volume": traffic,
            "errors": error_ratio,
            "latency": 100,
            "pods": True,
            "dependencies": not degraded,
        }
        signals = {
            name: {
                "value": value,
                "source": "prometheus:synthetic-lab",
                "query": f"synthetic_{name}{{service='checkout'}}",
                "observed_at": end,
            }
            for name, value in values.items()
            if name != missing
        }
        rows.append(
            {
                "sequence": index + 1,
                "target": TARGET,
                "window_start": start,
                "window_end": end,
                "signals": signals,
            }
        )
    return rows


def with_raw_payloads(rows):
    """Freeze test-owned synthetic telemetry AFTER each scenario's mutations.

    The stub returns these exact bytes. This synthetic JSON wire format is a
    fixture format, not a requirement on product telemetry/evidence encoding.
    """
    for row in rows:
        for name, signal in row["signals"].items():
            captured = {
                "signal": name,
                "target": row["target"],
                "window_start": row["window_start"].isoformat(),
                "window_end": row["window_end"].isoformat(),
                "value": signal["value"],
                "source": signal["source"],
                "query": signal["query"],
                "observed_at": signal["observed_at"].isoformat(),
            }
            for kind, value in (
                ("query", float(signal["value"])),
                ("coverage", 1.0),
                ("freshness", signal["observed_at"].timestamp()),
            ):
                wire = {
                    **captured,
                    "status": "success",
                    "data": {
                        "resultType": "vector",
                        "result": [
                            {
                                "metric": {},
                                "value": [row["window_end"].timestamp(), str(value)],
                            }
                        ],
                    },
                }
                signal["raw_payload" if kind == "query" else f"{kind}_payload"] = (
                    json.dumps(wire, sort_keys=True, separators=(",", ":")).encode(
                        "utf-8"
                    )
                )
    return rows


def assert_readonly(outcome, driver, subject_id="incident-f6"):
    assert outcome.subject_id == subject_id
    assert outcome.recovery_confirmed is (outcome.incident_lifecycle == "resolved")
    assert driver.model_calls == 0
    assert driver.environment_writes == ()
    assert driver.telemetry_calls_during_replay == 0
    assert outcome.model_requests == ()
    assert "read_only" in outcome.permissions
    assert set(outcome.permissions) <= {"read_only", "human_control"}
    assert set(outcome.actions) <= READONLY_ACTIONS
    # Tightened frozen contract: a permitted but missing/truncated audit
    # is not evidence that the handling, queries or persistence happened.
    handling = sum(
        row["action"] == "register_remediation" for row in outcome.handling_audit
    )
    assert handling > 0
    assert outcome.actions.count("record_handling") == handling
    assert outcome.actions.count("persist_observation") == len(outcome.recovery_samples)
    emitted = driver.emitted_queries_for_samples(outcome.recovery_samples)
    assert outcome.actions.count("read_only_query") == emitted
    if outcome.recovery_samples:
        assert emitted > 0
    # Incidents are seeded open. Count each observed registration/ending move,
    # including historical sessions; repeated authorization while observing
    # and endings that retain the lifecycle are not additional transitions.
    lifecycle = "open"
    changes = 0
    for _, before, after in outcome.lifecycle_events:
        changes += (lifecycle if before is None else before) != after
        lifecycle = after
    actual = outcome.actions.count("advance_incident_lifecycle")
    assert actual == changes, (
        f"lifecycle action count: actual={actual}, expected={changes}"
    )
    if outcome.human_interaction == "handoff":
        assert outcome.actions.count("human_handoff") >= 1
    else:
        assert outcome.actions.count("human_handoff") == 0


def assert_signal_basis(row, original, driver):
    evidence_ids = []
    assert set(row["signals"]) == set(original["signals"])
    for name, signal in row["signals"].items():
        for key in ("value", "source", "query", "observed_at"):
            assert signal[key] == original["signals"][name][key]
        assert signal["evidence_id"]
        evidence_ids.append(signal["evidence_id"])
        payload = driver.read_raw_payload(signal["evidence_id"])
        assert isinstance(payload, bytes)
        assert payload == original["signals"][name]["raw_payload"]
        assert sha256(payload).hexdigest() == signal["raw_sha256"]
        assert len(signal["raw_sha256"]) == 64
        assert set(signal["raw_sha256"]) <= set("0123456789abcdef")
    return evidence_ids


def assert_saved_basis(outcome, supplied, profile, driver):
    assert outcome.recovery_profile == profile
    assert outcome.recovery_handled_at == HANDLED
    immutable_target = driver.incident_targets[outcome.subject_id]
    assert profile["target"] == immutable_target
    assert driver.snapshot_incident(outcome.subject_id)["target"] == immutable_target
    saved = outcome.recovery_samples
    assert len(saved) == len(supplied) > 0
    assert len({row["sample_id"] for row in saved}) == len(saved)
    evidence_ids = []
    for row, original in zip(saved, supplied, strict=True):
        assert row["subject_id"] == outcome.subject_id
        assert row["sequence"] == original["sequence"]
        assert row["target"] == original["target"] == immutable_target
        assert row["window_start"] == original["window_start"] >= HANDLED
        assert row["window_end"] == original["window_end"]
        assert row["health_profile_revision"] == profile["revision"]
        assert isinstance(row["subject_control_generation"], int)
        assert isinstance(row["observation_generation"], int)
        assert row["disposition"] == "adopted"
        assert row["outcome"] in {
            "healthy",
            "degraded",
            "no_data",
            "stale",
            "timeout",
            "failed",
        }
        evidence_ids.extend(assert_signal_basis(row, original, driver))

    assert len(set(evidence_ids)) == len(evidence_ids)


@pytest.mark.parametrize(
    "count,expected",
    [(1, "observing_recovery"), (3, "observing_recovery"), (4, "resolved")],
)  # #157
def test_f6_step1_requires_all_signals_and_sustained_post_handling_window(
    recovery_driver, profile, count, expected
):
    supplied = observations(count)
    supplied = with_raw_payloads(supplied)
    outcome = recovery_driver.run(
        scenario(1, "handled-recovery"),
        profile=profile,
        handled_at=HANDLED,
        observations=deepcopy(supplied),
        until=supplied[-1]["window_end"],
    )
    assert outcome.incident_lifecycle == expected
    assert outcome.recovery_confirmed is (expected == "resolved")
    if expected == "resolved":
        assert outcome.recovery_verdict == "healthy"
    assert outcome.healthy_window_seconds == (count - 1) * 60  # #157: end to end
    assert outcome.observation_ended is (expected == "resolved")
    assert outcome.human_interaction != "handoff"
    assert not outcome.handoff_reasons
    assert_saved_basis(outcome, supplied, profile, recovery_driver)
    assert_readonly(outcome, recovery_driver)


@pytest.mark.parametrize("count", [3, 5])
@pytest.mark.parametrize("traffic", [0, 99])
def test_f6_step2_falling_errors_with_withdrawn_traffic_never_confirms_recovery(
    recovery_driver, profile, count, traffic
):
    supplied = observations(count, traffic=traffic, error_ratio=0)
    supplied = with_raw_payloads(supplied)
    outcome = recovery_driver.run(
        scenario(2, "traffic-withdrawn"),
        profile=profile,
        handled_at=HANDLED,
        observations=deepcopy(supplied),
        until=supplied[-1]["window_end"],
    )
    if count == 5:
        assert outcome.recovery_verdict == "unknown"
    assert "INSUFFICIENT_TRAFFIC" in outcome.recovery_reasons
    assert outcome.healthy_window_seconds == 0
    assert outcome.incident_lifecycle == (
        "open" if count == 5 else "observing_recovery"
    )
    if count == 5:
        assert outcome.human_interaction == "handoff"
        assert "INSUFFICIENT_TRAFFIC" in outcome.handoff_reasons
        assert outcome.observation_ended
    else:
        assert outcome.observation_ended is False
        assert outcome.human_interaction != "handoff"
        assert not outcome.handoff_reasons
    assert_saved_basis(outcome, supplied, profile, recovery_driver)
    assert_readonly(outcome, recovery_driver)


@pytest.mark.parametrize("missing", REQUIRED)
def test_f6_step3_each_missing_required_signal_is_unknown_and_handed_off(
    recovery_driver, profile, missing
):
    supplied = observations(missing=missing)
    supplied = with_raw_payloads(supplied)
    outcome = recovery_driver.run(
        scenario(3, f"missing-{missing}"),
        profile=profile,
        handled_at=HANDLED,
        observations=deepcopy(supplied),
        until=profile["deadline"],
    )
    assert outcome.incident_lifecycle == "open"
    assert outcome.recovery_verdict == "unknown"
    assert outcome.healthy_window_seconds == 0
    assert outcome.human_interaction == "handoff"
    assert "REQUIRED_TELEMETRY_MISSING" in outcome.handoff_reasons
    assert f"MISSING_SIGNAL:{missing}" in outcome.recovery_reasons
    assert outcome.observation_ended
    assert_saved_basis(outcome, supplied, profile, recovery_driver)
    assert_readonly(outcome, recovery_driver)


@pytest.mark.parametrize("count", [3, 5])
def test_f6_step4_continued_dependency_degradation_stays_open_without_actuation(
    recovery_driver, profile, count
):
    supplied = observations(count, degraded=True)
    supplied = with_raw_payloads(supplied)
    outcome = recovery_driver.run(
        scenario(4, "dependency-degraded"),
        profile=profile,
        handled_at=HANDLED,
        observations=deepcopy(supplied),
        until=supplied[-1]["window_end"],
    )
    assert outcome.incident_lifecycle == (
        "open" if count == 5 else "observing_recovery"
    )
    if count == 5:
        assert outcome.recovery_verdict == "degraded"
    assert outcome.healthy_window_seconds == 0
    assert "DEPENDENCY_UNHEALTHY" in outcome.recovery_reasons
    if count == 5:
        assert outcome.human_interaction == "handoff"
        assert "CONTINUED_DEGRADATION" in outcome.handoff_reasons
        assert outcome.observation_ended
    else:
        assert outcome.observation_ended is False
        assert outcome.human_interaction != "handoff"
        assert not outcome.handoff_reasons
    assert_saved_basis(outcome, supplied, profile, recovery_driver)
    assert_readonly(outcome, recovery_driver)


@pytest.mark.parametrize(
    "kind,options,count,verdict,lifecycle",
    [
        ("recovered", {}, 4, "healthy", "resolved"),  # #157: three intervals
        ("no-traffic", {"traffic": 0, "error_ratio": 0}, 5, "unknown", "open"),
        ("missing", {"missing": "pods"}, 5, "unknown", "open"),
        ("degraded", {"degraded": True}, 5, "degraded", "open"),
    ],
)
def test_f6_step5_replay_reconstructs_basis_without_telemetry_or_investigator(
    recovery_driver, profile, kind, options, count, verdict, lifecycle
):
    supplied = observations(count, **options)
    supplied = with_raw_payloads(supplied)
    original = recovery_driver.run(
        scenario(5, kind),
        profile=profile,
        handled_at=HANDLED,
        observations=deepcopy(supplied),
        until=supplied[-1]["window_end"],
    )
    assert original.recovery_verdict == verdict
    assert original.incident_lifecycle == lifecycle
    assert_saved_basis(original, supplied, profile, recovery_driver)
    replayed = recovery_driver.replay(
        persisted=recovery_driver.persisted_replay_input(
            original.subject_id, original.recovery_samples[0]["session_id"]
        ),
        allow_telemetry=False,
        allow_model=False,
    )
    assert replayed.external_queries == ()
    assert replayed.model_requests == ()
    for field in (
        "subject_id",
        "incident_lifecycle",
        "recovery_confirmed",
        "recovery_verdict",
        "recovery_reasons",
        "healthy_window_seconds",
        "handoff_reasons",
        "observation_ended",
    ):
        assert getattr(replayed, field) == getattr(original, field)
    assert_saved_basis(replayed, supplied, profile, recovery_driver)
    assert_readonly(original, recovery_driver)
    assert_readonly(replayed, recovery_driver)


@pytest.mark.parametrize(
    "signal,value",
    [("deployment", False), ("pods", False), ("errors", 0.02), ("latency", 501)],
)
def test_f6_step1_each_required_signal_threshold_blocks_recovery(
    recovery_driver, profile, signal, value
):
    supplied = observations()
    for row in supplied:
        row["signals"][signal]["value"] = value
    supplied = with_raw_payloads(supplied)
    outcome = recovery_driver.run(
        scenario(1, f"unhealthy-{signal}"),
        profile=profile,
        handled_at=HANDLED,
        observations=deepcopy(supplied),
        until=profile["deadline"],
    )
    assert outcome.incident_lifecycle == "open"
    assert outcome.recovery_verdict == "degraded"
    assert outcome.healthy_window_seconds == 0
    assert outcome.observation_ended
    assert outcome.human_interaction == "handoff"
    assert "CONTINUED_DEGRADATION" in outcome.handoff_reasons
    assert_saved_basis(outcome, supplied, profile, recovery_driver)
    assert_readonly(outcome, recovery_driver)


@pytest.mark.parametrize("kind", ["stale", "coverage-gap"])
def test_f6_step1_stale_or_discontinuous_data_is_saved_without_confirming_recovery(
    recovery_driver, profile, kind
):
    supplied = observations(3)
    if kind == "stale":
        for row in supplied:
            for signal in row["signals"].values():
                signal["observed_at"] -= timedelta(seconds=61)
    elif kind == "coverage-gap":
        # Total healthy duration is 180 s, but no continuous 180 s span.
        for row in supplied[1:]:
            row["window_start"] += timedelta(seconds=60)
            row["window_end"] += timedelta(seconds=60)
            for signal in row["signals"].values():
                signal["observed_at"] += timedelta(seconds=60)
    supplied = with_raw_payloads(supplied)
    outcome = recovery_driver.run(
        scenario(1, kind),
        profile=profile,
        handled_at=HANDLED,
        observations=deepcopy(supplied),
        until=profile["deadline"],
    )
    assert outcome.incident_lifecycle == "open"
    assert outcome.recovery_verdict == "unknown"
    assert outcome.recovery_confirmed is False
    assert outcome.used_sample_count == len(supplied)
    assert_saved_basis(outcome, supplied, profile, recovery_driver)
    if kind == "stale":
        assert all(row["outcome"] == "stale" for row in outcome.recovery_samples)
        assert outcome.healthy_window_seconds == 0
    assert outcome.healthy_window_seconds < profile["healthy_window_seconds"]
    assert outcome.observation_ended
    assert outcome.human_interaction == "handoff"
    assert outcome.handoff_reasons
    assert_readonly(outcome, recovery_driver)


def test_f6_step5_replay_recomputes_instead_of_trusting_saved_verdict(
    recovery_driver, profile
):
    supplied = observations(4)  # #157: three intervals for 180 s
    supplied = with_raw_payloads(supplied)
    original = recovery_driver.run(
        scenario(5, "verdict-integrity"),
        profile=profile,
        handled_at=HANDLED,
        observations=deepcopy(supplied),
        until=supplied[-1]["window_end"],
    )
    assert original.incident_lifecycle == "resolved"
    assert_saved_basis(original, supplied, profile, recovery_driver)
    persisted = recovery_driver.persisted_replay_input(
        original.subject_id, original.recovery_samples[0]["session_id"]
    )
    samples = persisted["history"]["samples"]
    # Judgment metadata is not raw evidence authority. Alter ONLY judgments;
    # frozen signals, profile and captured evidence references stay identical.
    for row in samples:
        row["outcome"] = "degraded"
    replayed = recovery_driver.replay(
        persisted=persisted,
        allow_telemetry=False,
        allow_model=False,
    )
    assert replayed.external_queries == ()
    assert replayed.model_requests == ()
    if "STORED_OBSERVATION_INTEGRITY_MISMATCH" in replayed.recovery_reasons:
        assert replayed.recovery_verdict == "unknown"
        assert replayed.recovery_confirmed is False
        assert replayed.incident_lifecycle != "resolved"
    else:
        assert replayed.incident_lifecycle == "resolved"
        assert replayed.recovery_verdict == "healthy"
        assert replayed.recovery_confirmed is True
        assert replayed.healthy_window_seconds == 180
        assert replayed.recovery_samples == original.recovery_samples
    assert_readonly(original, recovery_driver)
    assert_readonly(replayed, recovery_driver)


@pytest.mark.parametrize("subject_id", ["incident-f6", "incident-other"])
@pytest.mark.parametrize("count", [3, 4])  # #157: before/at 180 s
def test_f6_step1_same_target_incidents_only_handled_subject_changes(
    recovery_driver, profile, subject_id, count
):
    ids = ("incident-f6", "incident-other")
    for incident_id in ids:
        recovery_driver.seed_incident(
            incident_id, target=deepcopy(TARGET), lifecycle="open"
        )
    other_id = next(incident_id for incident_id in ids if incident_id != subject_id)
    untouched = recovery_driver.snapshot_incident(other_id)
    assert untouched["incident_lifecycle"] == "open"
    assert untouched["recovery_samples"] == ()
    assert untouched["observation_sessions"] == ()
    supplied = observations(count)
    requested = scenario(1, "same-target-two-incidents", subject_id=subject_id)
    supplied = with_raw_payloads(supplied)
    outcome = recovery_driver.run(
        requested,
        profile=profile,
        handled_at=HANDLED,
        observations=deepcopy(supplied),
        until=supplied[-1]["window_end"],
    )
    assert outcome.subject_id == requested.subject_id
    assert outcome.incident_lifecycle == (
        "resolved" if count == 4 else "observing_recovery"
    )
    assert outcome.recovery_confirmed is (count == 4)
    assert_saved_basis(outcome, supplied, profile, recovery_driver)
    changed = recovery_driver.snapshot_incident(subject_id)
    assert changed["incident_lifecycle"] == outcome.incident_lifecycle
    assert changed["recovery_samples"] == outcome.recovery_samples
    assert changed["observation_sessions"]
    assert recovery_driver.snapshot_incident(other_id) == untouched
    assert_readonly(outcome, recovery_driver, subject_id=requested.subject_id)


@pytest.mark.parametrize("count", [3, 5])
def test_f6_step1_degradation_resets_continuous_healthy_window(
    recovery_driver, profile, count
):
    # Prefix H,H,D verifies reset immediately; H,H,D,H,H verifies no summing
    # of the two healthy runs across the degraded third window.
    supplied = observations(count)
    supplied[2]["signals"]["errors"]["value"] = 0.02
    supplied = with_raw_payloads(supplied)
    outcome = recovery_driver.run(
        scenario(1, "healthy-then-degraded"),
        profile=profile,
        handled_at=HANDLED,
        observations=deepcopy(supplied),
        until=supplied[-1]["window_end"],
    )
    assert outcome.incident_lifecycle == (
        "observing_recovery" if count == 3 else "open"
    )
    assert outcome.observation_ended is (count == 5)
    if count == 3:
        assert outcome.human_interaction != "handoff"
        assert not outcome.handoff_reasons
    else:
        assert outcome.human_interaction == "handoff"
        assert outcome.handoff_reasons
    assert outcome.recovery_confirmed is False
    assert outcome.healthy_window_seconds == (
        0 if count == 3 else 60
    )  # #157: two healthy samples span one interval
    assert_saved_basis(outcome, supplied, profile, recovery_driver)
    assert_readonly(outcome, recovery_driver)


def test_f6_step2_traffic_withdrawal_after_healthy_samples_never_confirms_recovery(
    recovery_driver, profile
):
    supplied = observations(error_ratio=0.005)
    for row in supplied[2:]:
        row["signals"]["request_volume"]["value"] = 0
        row["signals"]["errors"]["value"] = 0
    supplied = with_raw_payloads(supplied)
    outcome = recovery_driver.run(
        scenario(2, "healthy-then-traffic-withdrawn"),
        profile=profile,
        handled_at=HANDLED,
        observations=deepcopy(supplied),
        until=profile["deadline"],
    )
    assert outcome.incident_lifecycle == "open"
    assert outcome.recovery_confirmed is False
    assert outcome.recovery_verdict == "unknown"
    assert (
        outcome.healthy_window_seconds == 0
    )  # #157: traffic withdrawal resets the streak
    assert "INSUFFICIENT_TRAFFIC" in outcome.recovery_reasons
    assert "INSUFFICIENT_TRAFFIC" in outcome.handoff_reasons
    assert outcome.human_interaction == "handoff"
    assert_saved_basis(outcome, supplied, profile, recovery_driver)
    assert_readonly(outcome, recovery_driver)


# Harness guard checks execute NOW. They do not stand in for skipped F6 runs.
@pytest.mark.parametrize("operation", ["deployment", "rollback", "release-gate"])
def test_harness_environment_stub_records_and_rejects_every_write(operation):
    boundaries = RecoveryBoundaries()
    with pytest.raises(AssertionError, match="environment write"):
        boundaries.environment_write("POST", operation, {"target": TARGET})
    assert boundaries.environment_writes == (("POST", operation, {"target": TARGET}),)


def test_harness_replay_stub_records_queries_even_if_runtime_swallows_rejection():
    boundaries = RecoveryBoundaries()

    def bad_replay(**_artifacts):
        try:
            boundaries.telemetry_query("some-live-query")
        except AssertionError:
            return None

    driver = GuardedRecoveryDriver(SimpleNamespace(replay=bad_replay), boundaries)
    driver.replay()
    assert driver.telemetry_calls_during_replay == 1
    assert not boundaries.replaying


def test_harness_run_rejects_an_outcome_for_another_subject():
    runtime = SimpleNamespace(
        run=lambda _scenario, **_kwargs: SimpleNamespace(subject_id="another-incident")
    )
    driver = GuardedRecoveryDriver(runtime, RecoveryBoundaries())
    with pytest.raises(AssertionError):
        driver.run(scenario(1, "wrong-subject"))


def test_f6_step1_before_handling_data_cannot_confirm_recovery(
    recovery_driver, profile
):
    supplied = observations(3)
    for row in supplied:
        row["window_start"] -= timedelta(seconds=180)
        row["window_end"] -= timedelta(seconds=180)
        for signal in row["signals"].values():
            signal["observed_at"] -= timedelta(seconds=180)
    supplied = with_raw_payloads(supplied)
    outcome = recovery_driver.run(
        scenario(1, "before-handling"),
        profile=profile,
        handled_at=HANDLED,
        observations=deepcopy(supplied),
        until=profile["deadline"],
    )
    assert outcome.incident_lifecycle == "open"
    assert outcome.recovery_verdict == "unknown"
    assert outcome.recovery_confirmed is False
    assert outcome.healthy_window_seconds < profile["healthy_window_seconds"]
    assert outcome.observation_ended
    assert outcome.human_interaction == "handoff"
    assert outcome.handoff_reasons
    # No adoption expectation: these windows precede authorized handling.
    assert_readonly(outcome, recovery_driver)


@pytest.mark.parametrize("count", [6, 7])  # #157: three/four healthy samples after D
def test_f6_step1_healthy_window_rebuilds_after_degradation_and_can_resolve(
    recovery_driver, profile, count
):
    # Make room for H,H,D,H,H,H,H within (#157) the ORIGINAL frozen budget, without
    # extending deadline or resetting used samples once observation starts.
    longer_profile = deepcopy(profile)
    longer_profile["deadline"] = HANDLED + timedelta(seconds=420)
    longer_profile["max_samples"] = 7
    supplied = observations(count)
    supplied[2]["signals"]["errors"]["value"] = 0.02
    supplied = with_raw_payloads(supplied)
    outcome = recovery_driver.run(
        scenario(1, "recovery-after-degradation"),
        profile=longer_profile,
        handled_at=HANDLED,
        observations=deepcopy(supplied),
        until=supplied[-1]["window_end"],
    )
    assert outcome.incident_lifecycle == (
        "resolved" if count == 7 else "observing_recovery"
    )
    assert outcome.recovery_confirmed is (count == 7)
    assert outcome.observation_ended is (count == 7)
    assert outcome.healthy_window_seconds == (
        180 if count == 7 else 120
    )  # #157: only intervals after reset
    assert outcome.used_sample_count == count
    if count == 7:
        assert outcome.recovery_verdict == "healthy"
    assert outcome.human_interaction != "handoff"
    assert not outcome.handoff_reasons
    assert_saved_basis(outcome, supplied, longer_profile, recovery_driver)
    assert_readonly(outcome, recovery_driver)


@pytest.mark.parametrize("phase", ["run", "replay"])
def test_harness_model_stub_records_calls_even_if_runtime_swallows_rejection(phase):
    boundaries = RecoveryBoundaries()

    def attempt_model_call():
        try:
            boundaries.model_request(model="forbidden", messages=[])
        except AssertionError:
            return SimpleNamespace(subject_id="incident-f6")

    runtime = SimpleNamespace(
        run=lambda _scenario, **_kwargs: attempt_model_call(),
        replay=lambda **_kwargs: attempt_model_call(),
    )
    driver = GuardedRecoveryDriver(runtime, boundaries)
    if phase == "run":
        driver.run(scenario(1, "swallowed-model-rejection"))
    else:
        driver.replay()
    assert driver.model_calls == 1


@pytest.mark.parametrize(
    "identity_field", ("integration_id", "cluster_uid", "namespace", "resource_uid")
)
def test_f6_step1_target_identity_reregistration_is_rejected_and_sampling_stays_bound(
    recovery_driver, profile, identity_field
):
    # Contract change authorized by the user, 2026-10-08: target is bound
    # at registration/query time, not a caller-provided submit payload.
    from opspilot.persistence import PersistenceError

    requested = scenario(1, f"immutable-target-{identity_field}")
    started = recovery_driver.run(
        requested, profile=profile, handled_at=HANDLED, observations=[], until=HANDLED
    )
    assert started.incident_lifecycle == "observing_recovery"
    before = recovery_driver.snapshot_incident(requested.subject_id)
    identity = {key: value for key, value in TARGET.items() if key != "revision"}
    foreign_target = deepcopy(TARGET)
    alternate = {
        "integration_id": "another-integration",
        "cluster_uid": "another-cluster",
        "namespace": "another-demo",
        "resource_uid": "another-checkout",
    }
    identity[identity_field] = alternate[identity_field]
    foreign_target[identity_field] = alternate[identity_field]
    with pytest.raises(PersistenceError, match="TARGET_MISMATCH"):
        recovery_driver.register_again(
            requested, profile=profile, handled_at=HANDLED, identity=identity
        )
    assert recovery_driver.snapshot_incident(requested.subject_id) == before
    supplied = observations(1)
    supplied[0]["target"] = foreign_target
    supplied = with_raw_payloads(supplied)
    outcome = recovery_driver.continue_observation(
        requested, observations=deepcopy(supplied), until=supplied[0]["window_end"]
    )
    assert outcome.target == TARGET
    assert outcome.incident_lifecycle == "observing_recovery"
    assert outcome.recovery_confirmed is False
    assert outcome.observation_ended is False
    assert outcome.human_interaction != "handoff"
    assert not outcome.handoff_reasons
    assert outcome.healthy_window_seconds == 0  # #157: a single healthy sample
    assert outcome.used_sample_count == 1
    assert len(outcome.recovery_samples) == 1
    saved = outcome.recovery_samples[0]
    assert saved["target"] == TARGET != foreign_target
    assert saved["session_id"] == before["observation_authorization"]["session_id"]
    assert saved["subject_id"] == requested.subject_id
    assert saved["disposition"] == "adopted"
    assert saved["sequence"] == supplied[0]["sequence"]
    assert saved["window_start"] == supplied[0]["window_start"]
    assert saved["window_end"] == supplied[0]["window_end"]
    assert len(set(assert_signal_basis(saved, supplied[0], recovery_driver))) == len(
        REQUIRED
    )
    after = recovery_driver.snapshot_incident(requested.subject_id)
    assert after["target"] == TARGET
    assert after["handling_audit"] == before["handling_audit"]
    for key in (
        "session_id",
        "subject_id",
        "target",
        "subject_control_generation",
        "observation_generation",
        "health_profile_revision",
        "authorized",
    ):
        assert (
            after["observation_authorization"][key]
            == before["observation_authorization"][key]
        )
    assert (
        len(after["observation_sessions"]) == len(before["observation_sessions"]) == 1
    )
    assert saved in after["recovery_samples"]
    assert_readonly(started, recovery_driver)
    assert_readonly(outcome, recovery_driver)


def test_f6_step1_revision_rehandling_fences_old_session_result(
    recovery_driver, profile
):
    requested = scenario(1, "revision-new-session")
    recovery_driver.run(
        requested, profile=profile, handled_at=HANDLED, observations=[], until=HANDLED
    )
    original = recovery_driver.snapshot_incident(requested.subject_id)
    old_auth = original["observation_authorization"]
    recovery_driver.prepare_submission(requested.subject_id)
    updated = deepcopy(profile)
    updated["target"] = {**TARGET, "revision": "new-handled-revision"}
    recovery_driver.replace_revision_at_submission(
        requested, profile=updated, handled_at=HANDLED + timedelta(seconds=60)
    )
    supplied = with_raw_payloads(observations(1))
    outcome = recovery_driver.continue_observation(
        requested, observations=deepcopy(supplied), until=supplied[0]["window_end"]
    )
    before, after = recovery_driver.submission_snapshots()
    auth = before["observation_authorization"]
    assert auth["session_id"] != old_auth["session_id"]
    assert auth["target"] == updated["target"]
    assert auth["subject_control_generation"] > old_auth["subject_control_generation"]
    assert auth["observation_generation"] > old_auth["observation_generation"]
    assert len(before["observation_sessions"]) == 2
    old = next(
        row
        for row in before["observation_sessions"]
        if row["session_id"] == old_auth["session_id"]
    )
    assert old["state"] == "revoked" and old["authorized"] is False
    assert old["target"] == TARGET
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
    assert len(outcome.recovery_samples) == 1
    saved = outcome.recovery_samples[0]
    assert saved["disposition"] == "history_only"
    assert saved["subject_id"] == requested.subject_id
    assert saved["session_id"] == old_auth["session_id"]
    assert saved["target"] == TARGET
    assert saved["subject_control_generation"] == old_auth["subject_control_generation"]
    assert saved["observation_generation"] == old_auth["observation_generation"]
    assert len(set(assert_signal_basis(saved, supplied[0], recovery_driver))) == len(
        REQUIRED
    )
    # Old claimed job becomes visible when history is filed; no new logical
    # work is scheduled by that rejection (the latest active slot is stable).
    assert set(
        tuple(row[key] for key in ("job_id", "session_id", "sequence"))
        for row in after["sample_jobs"]
    ) == set(
        tuple(row[key] for key in ("job_id", "session_id", "sequence"))
        for row in before["sample_jobs"]
    ) | {
        tuple(
            recovery_driver.submitted_job_witness()[key]
            for key in ("job_id", "session_id", "sequence")
        )
    }
    assert outcome.recovery_confirmed is False
    assert outcome.used_sample_count == 0
    assert outcome.healthy_window_seconds == 0
    assert_readonly(outcome, recovery_driver)


@pytest.mark.parametrize("reader_state", ["missing", "none", "text"])
def test_harness_evidence_reader_rejects_unresolvable_or_nonbyte_payloads(reader_state):
    runtime = SimpleNamespace()
    if reader_state != "missing":
        runtime.read_raw_payload = lambda _id: (
            None if reader_state == "none" else "not-bytes"
        )
    driver = GuardedRecoveryDriver(runtime, RecoveryBoundaries())
    with pytest.raises(AssertionError, match="read_raw_payload|original bytes"):
        driver.read_raw_payload("unresolvable-evidence")


def test_harness_telemetry_returns_exact_test_owned_raw_signal_bytes():
    supplied = with_raw_payloads(observations(2))
    boundaries = RecoveryBoundaries()
    boundaries.configure_telemetry(supplied)
    for row in supplied:
        for signal in row["signals"].values():
            payload = boundaries.telemetry_query(
                signal["query"],
                window_start=row["window_start"],
                window_end=row["window_end"],
            )
            assert payload == signal["raw_payload"]
            decoded = json.loads(payload)
            assert decoded["target"] == row["target"]
            assert decoded["value"] == signal["value"]
            assert decoded["observed_at"] == signal["observed_at"].isoformat()


def test_f6_sample_budget_exhausts_before_original_deadline(recovery_driver, profile):
    bounded = deepcopy(profile)
    bounded["max_samples"] = 4
    bounded["deadline"] = HANDLED + timedelta(seconds=600)
    supplied = with_raw_payloads(observations(4, traffic=0, error_ratio=0))
    outcome = recovery_driver.run(
        scenario(2, "sample-budget-before-deadline"),
        profile=bounded,
        handled_at=HANDLED,
        observations=deepcopy(supplied),
        until=supplied[-1]["window_end"],
    )
    assert supplied[-1]["window_end"] < bounded["deadline"]
    assert outcome.incident_lifecycle == "open"
    assert outcome.recovery_verdict == "unknown"
    assert outcome.observation_ended
    assert outcome.human_interaction == "handoff"
    assert "INSUFFICIENT_TRAFFIC" in outcome.handoff_reasons
    assert outcome.used_sample_count == bounded["max_samples"]
    snapshot = recovery_driver.snapshot_incident(outcome.subject_id)
    assert snapshot["observation_authorization"]["active_sample_job_id"] is None
    calls = len(recovery_driver.boundaries.telemetry_calls)
    assert recovery_driver.claim_available(outcome.subject_id) == ()
    assert len(recovery_driver.boundaries.telemetry_calls) == calls
    assert recovery_driver.snapshot_incident(outcome.subject_id) == snapshot
    assert_saved_basis(outcome, supplied, bounded, recovery_driver)
    assert_readonly(outcome, recovery_driver)
