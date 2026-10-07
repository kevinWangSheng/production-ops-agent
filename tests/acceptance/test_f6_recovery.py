"""F6 external scenarios — proposed seam, NOT an implemented harness.

Minimum external interface proposal (M1-02 steps 4/5/6):
* recovery_driver.run(IncidentScenario, *, profile, handled_at, observations,
  until) -> IncidentOutcome. The authenticated human handling authorizes an
  observation; inputs below are telemetry stimuli, never desired verdicts.
* IncidentOutcome adds incident_lifecycle, recovery_verdict (healthy/degraded/
  unknown), recovery_reasons, healthy_window_seconds, recovery_samples,
  recovery_profile (frozen profile), recovery_handled_at, observation_ended,
  model_requests (complete audit; must be empty for original and replay).
  Existing handoff_reasons/human_interaction/actions/permissions remain.
* recovery_samples are persisted records with sample_id, sequence, target,
  subject_control_generation, observation_generation, health_profile_revision,
  window_start/end, signals, outcome, disposition. Each signal carries value,
  source, query, observed_at, evidence_id, raw_sha256. Frozen profile includes
  thresholds, required signals, traffic/freshness/coverage rules and budgets.
* recovery_driver.replay(*, profile, handled_at, samples,
  allow_telemetry=False, allow_model=False) -> the same observable outcome,
  adding external_queries and model_requests (audit records, both empty).
  Replay receives persisted artifacts only, no investigator narrative.
* actions is the complete product action audit; excludes engineer fault setup
  and permits only read queries and OpsPilot's own record/control persistence.

The fixture fails loudly if skips are removed before a real driver is wired.
No fake successful outcomes or implementation adapter are provided here.
Numbers are synthetic test boundaries, not calibrated lab HealthProfile values.
"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from opspilot.acceptance import IncidentScenario

HANDLED = datetime(2026, 10, 7, 12, tzinfo=UTC)
REQUIRED = ("deployment", "request_volume", "errors", "latency", "pods", "dependencies")
TARGET = {
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
def recovery_driver():
    pytest.fail(
        "待实现真实 IncidentScenario -> IncidentOutcome 驱动；不能以预制结果替代接线"
    )


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


def scenario(step, kind):
    return IncidentScenario(
        scenario_id=f"F6:{step}:{kind}",
        feature_id="F6",
        acceptance_step=str(step),
        kind=kind,
        subject_id="incident-f6",
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


def assert_readonly(outcome):
    assert outcome.model_requests == ()
    assert "read_only" in outcome.permissions
    assert set(outcome.permissions) <= {"read_only", "human_control"}
    assert set(outcome.actions) <= READONLY_ACTIONS


def assert_saved_basis(outcome, supplied, profile):
    assert outcome.recovery_profile == profile
    assert outcome.recovery_handled_at == HANDLED
    saved = outcome.recovery_samples
    assert len(saved) == len(supplied) > 0
    assert len({row["sample_id"] for row in saved}) == len(saved)
    for row, original in zip(saved, supplied, strict=True):
        assert row["sequence"] == original["sequence"]
        assert row["target"] == original["target"]
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
        assert set(row["signals"]) == set(original["signals"])
        for name, signal in row["signals"].items():
            for key in ("value", "source", "query", "observed_at"):
                assert signal[key] == original["signals"][name][key]
            assert signal["evidence_id"]
            assert len(signal["raw_sha256"]) == 64
            assert set(signal["raw_sha256"]) <= set("0123456789abcdef")


@pytest.mark.skip(reason="待 M1-02 第 4 步接线")
@pytest.mark.parametrize("count,expected", [(2, "observing_recovery"), (3, "resolved")])
def test_f6_step1_requires_all_signals_and_sustained_post_handling_window(
    recovery_driver, profile, count, expected
):
    supplied = observations(count)
    outcome = recovery_driver.run(
        scenario(1, "handled-recovery"),
        profile=profile,
        handled_at=HANDLED,
        observations=deepcopy(supplied),
        until=supplied[-1]["window_end"],
    )
    assert outcome.incident_lifecycle == expected
    assert outcome.recovery_verdict == "healthy"
    assert outcome.healthy_window_seconds == count * 60
    assert outcome.observation_ended is (expected == "resolved")
    assert outcome.human_interaction != "handoff"
    assert not outcome.handoff_reasons
    assert_saved_basis(outcome, supplied, profile)
    assert_readonly(outcome)


@pytest.mark.skip(reason="待 M1-02 第 4 步接线")
@pytest.mark.parametrize("count", [3, 5])
@pytest.mark.parametrize("traffic", [0, 99])
def test_f6_step2_falling_errors_with_withdrawn_traffic_never_confirms_recovery(
    recovery_driver, profile, count, traffic
):
    supplied = observations(count, traffic=traffic, error_ratio=0)
    outcome = recovery_driver.run(
        scenario(2, "traffic-withdrawn"),
        profile=profile,
        handled_at=HANDLED,
        observations=deepcopy(supplied),
        until=supplied[-1]["window_end"],
    )
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
    assert_saved_basis(outcome, supplied, profile)
    assert_readonly(outcome)


@pytest.mark.skip(reason="待 M1-02 第 4 步接线")
@pytest.mark.parametrize("missing", REQUIRED)
def test_f6_step3_each_missing_required_signal_is_unknown_and_handed_off(
    recovery_driver, profile, missing
):
    supplied = observations(missing=missing)
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
    assert_saved_basis(outcome, supplied, profile)
    assert_readonly(outcome)


@pytest.mark.skip(reason="待 M1-02 第 4 步接线")
@pytest.mark.parametrize("count", [3, 5])
def test_f6_step4_continued_dependency_degradation_stays_open_without_actuation(
    recovery_driver, profile, count
):
    supplied = observations(count, degraded=True)
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
    assert outcome.recovery_verdict == "degraded"
    assert outcome.healthy_window_seconds == 0
    assert "DEPENDENCY_UNHEALTHY" in outcome.recovery_reasons
    if count == 5:
        assert outcome.human_interaction == "handoff"
        assert "CONTINUED_DEGRADATION" in outcome.handoff_reasons
        assert outcome.observation_ended
    assert_saved_basis(outcome, supplied, profile)
    assert_readonly(outcome)


@pytest.mark.skip(reason="待 M1-02 第 5 步接线")
@pytest.mark.parametrize(
    "kind,options,count,verdict,lifecycle",
    [
        ("recovered", {}, 3, "healthy", "resolved"),
        ("no-traffic", {"traffic": 0, "error_ratio": 0}, 5, "unknown", "open"),
        ("missing", {"missing": "pods"}, 5, "unknown", "open"),
        ("degraded", {"degraded": True}, 5, "degraded", "open"),
    ],
)
def test_f6_step5_replay_reconstructs_basis_without_telemetry_or_investigator(
    recovery_driver, profile, kind, options, count, verdict, lifecycle
):
    supplied = observations(count, **options)
    original = recovery_driver.run(
        scenario(5, kind),
        profile=profile,
        handled_at=HANDLED,
        observations=deepcopy(supplied),
        until=supplied[-1]["window_end"],
    )
    assert original.recovery_verdict == verdict
    assert original.incident_lifecycle == lifecycle
    assert_saved_basis(original, supplied, profile)
    replayed = recovery_driver.replay(
        profile=deepcopy(original.recovery_profile),
        handled_at=original.recovery_handled_at,
        samples=deepcopy(original.recovery_samples),
        allow_telemetry=False,
        allow_model=False,
    )
    assert replayed.external_queries == ()
    assert replayed.model_requests == ()
    for field in (
        "incident_lifecycle",
        "recovery_verdict",
        "recovery_reasons",
        "healthy_window_seconds",
        "handoff_reasons",
        "observation_ended",
    ):
        assert getattr(replayed, field) == getattr(original, field)
    assert_saved_basis(replayed, supplied, profile)
    assert_readonly(original)
    assert_readonly(replayed)


@pytest.mark.skip(reason="待 M1-02 第 4 步接线")
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
    assert_saved_basis(outcome, supplied, profile)
    assert_readonly(outcome)


@pytest.mark.skip(reason="待 M1-02 第 4 步接线")
@pytest.mark.parametrize("kind", ["stale", "coverage-gap", "before-handling"])
def test_f6_step1_old_or_discontinuous_data_cannot_cover_healthy_window(
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
    else:
        for row in supplied:
            row["window_start"] -= timedelta(seconds=180)
            row["window_end"] -= timedelta(seconds=180)
            for signal in row["signals"].values():
                signal["observed_at"] -= timedelta(seconds=180)
    outcome = recovery_driver.run(
        scenario(1, kind),
        profile=profile,
        handled_at=HANDLED,
        observations=deepcopy(supplied),
        until=profile["deadline"],
    )
    assert outcome.incident_lifecycle == "open"
    assert outcome.recovery_verdict == "unknown"
    assert outcome.healthy_window_seconds < profile["healthy_window_seconds"]
    assert outcome.observation_ended
    assert outcome.human_interaction == "handoff"
    assert outcome.handoff_reasons
    assert_readonly(outcome)


@pytest.mark.skip(reason="待 M1-02 第 5 步接线")
def test_f6_step5_replay_recomputes_instead_of_trusting_saved_verdict(
    recovery_driver, profile
):
    supplied = observations(3)
    original = recovery_driver.run(
        scenario(5, "verdict-integrity"),
        profile=profile,
        handled_at=HANDLED,
        observations=deepcopy(supplied),
        until=supplied[-1]["window_end"],
    )
    assert original.incident_lifecycle == "resolved"
    assert_saved_basis(original, supplied, profile)
    samples = deepcopy(original.recovery_samples)
    # Judgment metadata is not raw evidence authority. Alter ONLY judgments;
    # frozen signals, profile and captured evidence references stay identical.
    for row in samples:
        row["outcome"] = "degraded"
    replayed = recovery_driver.replay(
        profile=deepcopy(original.recovery_profile),
        handled_at=HANDLED,
        samples=samples,
        allow_telemetry=False,
        allow_model=False,
    )
    assert replayed.external_queries == ()
    assert replayed.model_requests == ()
    assert replayed.incident_lifecycle == "resolved"
    assert replayed.recovery_verdict == "healthy"
    assert replayed.healthy_window_seconds == 180
    assert replayed.recovery_samples == original.recovery_samples
    assert_readonly(original)
    assert_readonly(replayed)
