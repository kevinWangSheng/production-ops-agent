"""HealthProfile format, revision and the per-sample verdict (M1-02 step 1, #83).

Contract under test: C3 §10「健康规则」(missing required signals never
confirm health; data coverage, freshness and minimum samples are necessary
for ``healthy`` but not for saving an unknown observation), F6 steps 1–3
and the M1-02 step 1/2 interface contract (reading DTO, outcome +
``required_signals_present``, four session parameters, content-bound
revision).
"""

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from opspilot.domain import DomainError, HealthSample, ObservationSession
from opspilot.domain.intake import Target
from opspilot.domain.observation import confirms_health
from opspilot.domain.subjects import SubjectRef
from opspilot.observer import (
    PROFILE_DIRECTORY,
    HealthProfile,
    HealthProfileError,
    SignalReading,
    evaluate_readings,
    load_health_profile,
    profile_revision,
)

SHIPPED = PROFILE_DIRECTORY / "otel-demo-checkout.json"
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
RAW = "0" * 64


def minimal_profile(**overrides) -> dict:
    base = {
        "format_version": 1,
        "profile_id": "unit",
        "description": "unit profile",
        "subject": {"service": "svc", "kubernetes_namespace": "ns"},
        "source": "prometheus",
        "calibration_source": "unit test",
        "evaluation_window_seconds": 300,
        "freshness_seconds": 60,
        "query_timeout_seconds": 10,
        "session": {
            "deadline_seconds": 3600,
            "max_samples": 20,
            "sample_interval_seconds": 60,
            "sustained_window_seconds": 600,
        },
        "effective_traffic": {"signal": "rate", "minimum": 0.5},
        "signals": [
            {
                "name": "rate",
                "description": "traffic",
                "query": "sum(rate(x[5m]))",
                "coverage_query": "max(count_over_time(x[5m]))",
                "freshness_query": "max(timestamp(x))",
                "traffic_dependent": False,
                "healthy": {"min": 0},
            },
            {
                "name": "errors",
                "description": "error ratio",
                "query": "e / t",
                "coverage_query": "max(count_over_time(t[5m]))",
                "freshness_query": "max(timestamp(t))",
                "minimum_samples": 3,
                "traffic_dependent": True,
                "healthy": {"min": 0, "max": 0.01},
            },
            {
                "name": "ready",
                "description": "replicas",
                "query": "min(ready)",
                "coverage_query": "min(count_over_time(ready[5m]))",
                "freshness_query": "max(timestamp(ready))",
                "traffic_dependent": False,
                "healthy": {"min": 1},
            },
            {
                "name": "hint",
                "description": "optional context",
                "required": False,
                "query": "hint",
                "coverage_query": "count_over_time(hint[5m])",
                "freshness_query": "max(timestamp(hint))",
                "traffic_dependent": False,
                "healthy": {"max": 10},
            },
        ],
    }
    base.update(overrides)
    return base


def write(tmp_path: Path, payload, name: str = "p.json") -> Path:
    path = tmp_path / name
    path.write_text(
        payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8"
    )
    return path


def profile(**overrides) -> HealthProfile:
    return HealthProfile.model_validate(minimal_profile(**overrides))


def reading(name: str, value=None, *, status="ok", count=5, **overrides):
    prof = profile()
    signal = prof.signal(name)
    fields = {
        "signal_name": name,
        "status": status,
        "value": value if status == "ok" else None,
        "sample_count": count if status == "ok" else None,
        "query": signal.query if signal else "unknown",
        "window_start": NOW - timedelta(minutes=5),
        "window_end": NOW,
        "source": "prometheus",
        "raw_sha256": RAW,
        # the newest raw sample behind the reading: one scrape before NOW
        "latest_sample_at": NOW - timedelta(seconds=30),
    }
    fields.update(overrides)
    return SignalReading(**fields)


def evaluate(prof, readings, sample_time=NOW):
    return evaluate_readings(prof, readings, sample_time=sample_time)


def healthy_readings():
    return [reading("rate", 2.0), reading("errors", 0.0), reading("ready", 1.0)]


# 1. Shipped profile: format, F6 step 1 coverage, session parameters.


def test_shipped_checkout_profile_loads_and_covers_f6_step_one():
    prof = load_health_profile(SHIPPED)
    assert prof.profile_id == "otel-demo-checkout"
    assert prof.subject.service == "checkout"
    names = {signal.name for signal in prof.signals}
    # deployment state, request volume, errors, latency, pod health, dependencies
    assert {
        "deployment_available_replicas",
        "request_rate_per_second",
        "error_ratio",
        "latency_p95_milliseconds",
        "pods_running",
        "pod_restarts_in_window",
        "dependency_deployments_available",
        "dependency_error_ratio",
    } <= names
    assert all(signal.required for signal in prof.signals)
    assert all("count_over_time" in s.coverage_query for s in prof.signals)
    assert all("timestamp(" in s.freshness_query for s in prof.signals)
    assert all(signal.minimum_samples >= 3 for signal in prof.signals)
    assert prof.effective_traffic.signal == "request_rate_per_second"
    assert prof.session.sustained_window_seconds <= prof.session.deadline_seconds
    assert re.fullmatch(r"otel-demo-checkout@[0-9a-f]{12}", prof.revision)


def test_shipped_profile_kubernetes_signals_come_from_kube_state_metrics():
    prof = load_health_profile(SHIPPED)
    for name in (
        "deployment_available_replicas",
        "pods_running",
        "pod_restarts_in_window",
        "dependency_deployments_available",
    ):
        assert "kube_" in prof.signal(name).query
        assert 'namespace="otel-demo"' in prof.signal(name).query


HEALTHY_CHECKOUT = {
    "deployment_available_replicas": 1.0,
    "request_rate_per_second": 0.0125,
    "error_ratio": 0.0,
    "latency_p95_milliseconds": 120.0,
    "pods_running": 1.0,
    "pod_restarts_in_window": 0.0,
    "dependency_error_ratio": 0.0,
}


def test_a_missing_dependency_series_cannot_read_as_available():
    """count() over the filtered vector exposes a Deployment with no series."""
    prof = load_health_profile(SHIPPED)
    signal = prof.signal("dependency_deployments_available")
    assert signal.query.startswith("count(") and ">= 1" in signal.query
    deployments = re.search(r'deployment=~"([^"]+)"', signal.query).group(1).split("|")
    assert len(deployments) == 8 and len(set(deployments)) == 8
    assert (signal.healthy.min, signal.healthy.max) == (8, 8)
    assert "min(" not in signal.query

    def dep(value):
        return SignalReading(
            signal_name=signal.name,
            status="ok",
            value=value,
            sample_count=5,
            query=signal.query,
            window_start=NOW - timedelta(minutes=5),
            window_end=NOW,
            source="prometheus",
            latest_sample_at=NOW - timedelta(seconds=30),
        )

    readings = [
        SignalReading(
            signal_name=s.name,
            status="ok",
            value=HEALTHY_CHECKOUT[s.name],
            sample_count=5,
            query=s.query,
            window_start=NOW - timedelta(minutes=5),
            window_end=NOW,
            source="prometheus",
            latest_sample_at=NOW - timedelta(seconds=30),
        )
        for s in prof.signals
        if s.name != signal.name
    ]
    assert evaluate(prof, readings + [dep(8.0)]).outcome == "healthy"
    seven = evaluate(prof, readings + [dep(7.0)])
    assert seven.outcome == "degraded"
    assert "dependency_deployments_available" in seven.reason


# 2. Revision follows content.


def test_revision_changes_with_content_not_with_formatting(tmp_path):
    original = minimal_profile()
    a = load_health_profile(write(tmp_path, original, "a.json"))
    reordered = dict(reversed(list(original.items())))
    pretty = json.dumps(reordered, indent=4, sort_keys=False)
    b = load_health_profile(write(tmp_path, pretty, "b.json"))
    assert profile_revision(a) == profile_revision(b)
    assert a.revision.startswith("unit@")

    threshold = minimal_profile()
    threshold["signals"][1]["healthy"]["max"] = 0.02
    query = minimal_profile()
    query["signals"][0]["query"] = "sum(rate(y[5m]))"
    window = minimal_profile()
    window["session"]["sustained_window_seconds"] = 900
    gate = minimal_profile(effective_traffic={"signal": "rate", "minimum": 0.6})
    revisions = {
        load_health_profile(write(tmp_path, variant, f"{i}.json")).revision
        for i, variant in enumerate((threshold, query, window, gate))
    }
    assert len(revisions) == 4 and a.revision not in revisions


def test_revision_is_opaque_to_the_session_but_binds_the_sample():
    """The store compares revisions for equality; a changed profile stops adoption."""
    prof = profile()
    session = ObservationSession(
        session_id="obs-1",
        purpose="incident_recovery",
        subject=SubjectRef(kind="incident", id="inc-1"),
        target=Target(
            integration_id="int-1",
            cluster_uid="c-1",
            namespace="ns",
            resource_uid="r-1",
            revision="rev-1",
        ),
        subject_control_generation=0,
        observation_generation=0,
        authorized=True,
        health_profile_revision=prof.revision,
    )
    evaluation = evaluate(prof, healthy_readings())
    sample = HealthSample(
        sample_id="s-1",
        session_id="obs-1",
        sequence=1,
        window=evaluation_window(),
        outcome=evaluation.outcome,
        subject_control_generation=0,
        observation_generation=0,
        health_profile_revision=evaluation.health_profile_revision,
        required_signals_present=evaluation.required_signals_present,
    )
    assert confirms_health(session, sample)


def evaluation_window():
    from opspilot.domain import QueryWindow

    return QueryWindow(start=NOW - timedelta(minutes=5), end=NOW)


# 3. Validation rejects missing fields and illegal values.


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.pop("session"),
        lambda p: p.pop("effective_traffic"),
        lambda p: p.pop("calibration_source"),
        lambda p: p["signals"][0].pop("query"),
        lambda p: p["signals"][0].pop("healthy"),
        lambda p: p["signals"][0].pop("traffic_dependent"),
        lambda p: p["signals"][0].pop("coverage_query"),
        lambda p: p.update(unexpected="field"),
        lambda p: p["signals"][0].update(threshold=1),
        lambda p: p.update(signals=[]),
        lambda p: p["signals"][1]["healthy"].update(min=1, max=0),
        lambda p: p["signals"][1].update(healthy={}),
        lambda p: p["signals"][1].update(minimum_samples=0),
        lambda p: p.update(freshness_seconds=0),
        lambda p: p.update(evaluation_window_seconds=-1),
        lambda p: p["effective_traffic"].update(minimum=0),
        lambda p: p["effective_traffic"].update(signal="nope"),
        lambda p: p["signals"][0].update(required=False),
        lambda p: p["session"].update(sustained_window_seconds=7200),
        lambda p: p["session"].update(max_samples=2),
        lambda p: p.update(query_timeout_seconds=120),
        lambda p: p["signals"][1].update(name="rate"),
        lambda p: [s.update(required=False) for s in p["signals"]],
        lambda p: p.update(profile_id="Bad Id"),
        lambda p: p.update(format_version=2),
        lambda p: p.update(format_version="1"),
        lambda p: p.update(format_version=True),
        lambda p: p["signals"][0].pop("freshness_query"),
        # PromQL ranges must be the evaluation window (issue #86, optional)
        lambda p: p["signals"][0].update(query="sum(rate(x[10m]))"),
        lambda p: p["signals"][0].update(coverage_query="max(count_over_time(x[1h:]))"),
        lambda p: p.update(evaluation_window_seconds=600),
    ],
)
def test_profile_validation_rejects_missing_fields_and_illegal_values(tmp_path, mutate):
    payload = minimal_profile()
    mutate(payload)
    with pytest.raises(HealthProfileError) as excinfo:
        load_health_profile(write(tmp_path, payload))
    assert excinfo.value.code == "PROFILE_INVALID"


def test_profile_error_reports_locations_without_values(tmp_path):
    payload = minimal_profile(calibration_source="synthetic-secret-value")
    payload["signals"][1]["healthy"] = {"min": 1, "max": 0}
    with pytest.raises(HealthProfileError) as excinfo:
        load_health_profile(write(tmp_path, payload))
    rendered = str(excinfo.value) + repr(excinfo.value.errors)
    assert "signals/1/healthy" in rendered
    assert "synthetic-secret-value" not in rendered


def test_rejected_format_version_is_not_echoed(tmp_path):
    payload = minimal_profile(format_version="synthetic-leak-9")
    with pytest.raises(HealthProfileError) as excinfo:
        load_health_profile(write(tmp_path, payload))
    rendered = str(excinfo.value) + repr(excinfo.value.errors)
    assert "format_version" in rendered
    assert "synthetic-leak-9" not in rendered


def test_unreadable_or_non_json_profiles_are_refused(tmp_path):
    with pytest.raises(HealthProfileError) as missing:
        load_health_profile(tmp_path / "absent.json")
    assert missing.value.code == "PROFILE_UNREADABLE"
    for text in ("{not json", "[]", "42"):
        with pytest.raises(HealthProfileError) as bad:
            load_health_profile(write(tmp_path, text))
        assert bad.value.code == "PROFILE_INVALID"
    with pytest.raises(DomainError):
        load_health_profile("not-a-path")  # type: ignore[arg-type]


# 4. Reading DTO keeps value and status consistent.


def test_reading_value_follows_status():
    with pytest.raises(ValueError):
        reading("rate", status="ok", value=None)
    with pytest.raises(ValueError):
        SignalReading(
            **{**reading("rate", status="no_data").model_dump(), "value": 1.0}
        )
    with pytest.raises(ValueError):
        reading("rate", value=float("nan"))
    with pytest.raises(ValueError):
        reading("rate", 1.0, count=None)
    with pytest.raises(ValueError):
        reading("rate", 1.0, window_end=NOW - timedelta(minutes=6))
    assert reading("rate", status="timeout").value is None


# 5. Verdict: healthy only with every required signal present and meaningful.


def test_all_required_signals_within_bounds_is_healthy():
    evaluation = evaluate(profile(), healthy_readings())
    assert evaluation.outcome == "healthy"
    assert evaluation.required_signals_present
    assert {v.signal_name: v.verdict for v in evaluation.verdicts} == {
        "rate": "healthy",
        "errors": "healthy",
        "ready": "healthy",
        "hint": "missing",
    }
    assert evaluation.health_profile_revision == profile().revision


def test_verdict_is_deterministic():
    first = evaluate(profile(), healthy_readings())
    second = evaluate(profile(), list(reversed(healthy_readings())))
    assert first == second


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("no_data", "no_data"),
        ("stale", "stale"),
        ("timeout", "timeout"),
        ("failed", "failed"),
    ],
)
def test_a_required_signal_that_is_not_ok_never_confirms_health(status, expected):
    readings = [
        reading("rate", 2.0),
        reading("errors", status=status),
        reading("ready", 1.0),
    ]
    evaluation = evaluate(profile(), readings)
    assert evaluation.outcome == expected
    assert not evaluation.required_signals_present


def test_a_missing_required_signal_is_no_data_and_not_present():
    evaluation = evaluate(profile(), [reading("rate", 2.0), reading("errors", 0.0)])
    assert (evaluation.outcome, evaluation.required_signals_present) == (
        "no_data",
        False,
    )
    assert "ready" in evaluation.reason


def test_unknown_outcomes_rank_failed_over_timeout_over_stale_over_no_data():
    evaluation = evaluate(
        profile(),
        [reading("rate", status="stale"), reading("errors", status="failed")],
    )
    assert evaluation.outcome == "failed"
    evaluation = evaluate(
        profile(),
        [reading("rate", status="timeout"), reading("errors", status="stale")],
    )
    assert evaluation.outcome == "timeout"


def test_too_few_points_is_no_data_even_when_the_value_looks_healthy():
    readings = [
        reading("rate", 2.0),
        reading("errors", 0.0, count=2),
        reading("ready", 1.0),
    ]
    evaluation = evaluate(profile(), readings)
    assert (evaluation.outcome, evaluation.required_signals_present) == (
        "no_data",
        False,
    )
    assert next(
        v for v in evaluation.verdicts if v.signal_name == "errors"
    ).verdict == ("insufficient_samples")


def test_traffic_below_the_gate_cannot_confirm_recovery_even_with_zero_errors():
    """F6 step 2: error rate falls because traffic was removed."""
    readings = [reading("rate", 0.1), reading("errors", 0.0), reading("ready", 1.0)]
    evaluation = evaluate(profile(), readings)
    assert evaluation.outcome == "no_data"
    assert evaluation.required_signals_present
    assert "effective-traffic" in evaluation.reason
    verdicts = {v.signal_name: v.verdict for v in evaluation.verdicts}
    assert verdicts["rate"] == "below_traffic_gate"
    assert verdicts["errors"] == "not_judged"


def test_a_state_fact_is_degraded_even_without_traffic():
    """Zero replicas is read, not inferred; low traffic does not hide it."""
    readings = [reading("rate", 0.1), reading("errors", 0.0), reading("ready", 0.0)]
    evaluation = evaluate(profile(), readings)
    assert (evaluation.outcome, evaluation.required_signals_present) == (
        "degraded",
        True,
    )
    verdicts = {v.signal_name: v.verdict for v in evaluation.verdicts}
    assert verdicts == {
        "rate": "below_traffic_gate",
        "errors": "not_judged",
        "ready": "degraded",
        "hint": "missing",
    }
    # Traffic reading failed outright: the state fact still counts.
    readings = [
        reading("rate", status="failed"),
        reading("errors", 0.9),
        reading("ready", 0.0),
    ]
    evaluation = evaluate(profile(), readings)
    assert (evaluation.outcome, evaluation.required_signals_present) == (
        "degraded",
        False,
    )
    assert {v.signal_name: v.verdict for v in evaluation.verdicts}[
        "errors"
    ] == "not_judged"


def test_a_ratio_is_not_judged_below_the_traffic_gate():
    """An error ratio over a handful of requests proves neither health nor harm."""
    readings = [reading("rate", 0.1), reading("errors", 0.9), reading("ready", 1.0)]
    evaluation = evaluate(profile(), readings)
    assert (evaluation.outcome, evaluation.required_signals_present) == (
        "no_data",
        True,
    )
    assert {v.signal_name: v.verdict for v in evaluation.verdicts}[
        "errors"
    ] == "not_judged"


def test_a_required_value_outside_its_bound_is_degraded():
    readings = [reading("rate", 2.0), reading("errors", 0.05), reading("ready", 1.0)]
    evaluation = evaluate(profile(), readings)
    assert (evaluation.outcome, evaluation.required_signals_present) == (
        "degraded",
        True,
    )
    assert "errors" in evaluation.reason
    readings = [reading("rate", 2.0), reading("errors", 0.0), reading("ready", 0.0)]
    assert evaluate(profile(), readings).outcome == "degraded"


def test_optional_signals_are_reported_but_never_move_the_outcome():
    readings = healthy_readings() + [reading("hint", 99.0)]
    evaluation = evaluate(profile(), readings)
    assert evaluation.outcome == "healthy"
    assert next(v for v in evaluation.verdicts if v.signal_name == "hint").verdict == (
        "degraded"
    )
    readings = healthy_readings() + [reading("hint", status="failed")]
    assert evaluate(profile(), readings).outcome == "healthy"


def test_a_reading_with_another_query_or_source_does_not_count():
    """The stored query is what the replay re-judges; a substitute is a failure."""
    readings = [
        reading("rate", 2.0),
        reading("errors", 0.0, query="1"),
        reading("ready", 1.0),
    ]
    evaluation = evaluate(profile(), readings)
    assert (evaluation.outcome, evaluation.required_signals_present) == (
        "failed",
        False,
    )
    readings = [
        reading("rate", 2.0),
        reading("errors", 0.0, source="jaeger"),
        reading("ready", 1.0),
    ]
    assert evaluate(profile(), readings).outcome == "failed"


def test_readings_outside_the_profile_are_listed_and_ignored():
    readings = healthy_readings() + [reading("extra", 1.0, query="q")]
    evaluation = evaluate(profile(), readings)
    assert evaluation.outcome == "healthy"
    assert next(v for v in evaluation.verdicts if v.signal_name == "extra").verdict == (
        "not_in_profile"
    )


def test_an_old_reading_is_stale_whatever_its_status_says():
    """Recovery needs data taken after remediation, not a fresh query over old data."""
    later = NOW + timedelta(seconds=61)
    evaluation = evaluate(profile(), healthy_readings(), sample_time=later)
    assert (evaluation.outcome, evaluation.required_signals_present) == ("stale", False)
    assert all(
        v.verdict == "stale" for v in evaluation.verdicts if v.signal_name != "hint"
    )
    # exactly at the freshness bound (window end and newest raw sample both
    # 60 s before the sample) is still fresh
    at_bound = [
        reading("rate", 2.0, latest_sample_at=NOW),
        reading("errors", 0.0, latest_sample_at=NOW),
        reading("ready", 1.0, latest_sample_at=NOW),
    ]
    assert (
        evaluate(profile(), at_bound, sample_time=NOW + timedelta(seconds=60)).outcome
        == "healthy"
    )


def test_a_reading_window_from_the_future_or_of_the_wrong_span_is_stale():
    future = [
        reading("rate", 2.0, window_start=NOW, window_end=NOW + timedelta(minutes=5)),
        reading("errors", 0.0),
        reading("ready", 1.0),
    ]
    assert evaluate(profile(), future).outcome == "stale"
    short = [
        reading("rate", 2.0, window_start=NOW - timedelta(minutes=2)),
        reading("errors", 0.0),
        reading("ready", 1.0),
    ]
    evaluation = evaluate(profile(), short)
    assert evaluation.outcome == "stale"
    assert "spans 120s" in next(
        v.reason for v in evaluation.verdicts if v.signal_name == "rate"
    )
    aligned = [
        reading("rate", 2.0, window_start=NOW - timedelta(seconds=312)),
        reading("errors", 0.0),
        reading("ready", 1.0),
    ]
    assert evaluate(profile(), aligned).outcome == "healthy"


def test_a_reading_whose_scrapes_stopped_is_stale_and_cannot_extend_health():
    """Issue #86: after scrapes stop, ``query`` keeps answering through the
    lookback delta and ``coverage_query`` still counts the points inside
    the range, so the window and the point count both look fine. Freshness
    is judged on the newest raw sample timestamp instead."""
    scrapes_stopped_at = NOW - timedelta(seconds=61)
    frozen = [
        reading("rate", 2.0, latest_sample_at=scrapes_stopped_at),
        reading("errors", 0.0),
        reading("ready", 1.0),
    ]
    evaluation = evaluate(profile(), frozen)
    assert (evaluation.outcome, evaluation.required_signals_present) == ("stale", False)
    rate = next(v for v in evaluation.verdicts if v.signal_name == "rate")
    assert rate.verdict == "stale" and "newest raw sample is 61s old" in rate.reason
    # one second inside the freshness bound is still fresh
    just_fresh = [
        reading("rate", 2.0, latest_sample_at=NOW - timedelta(seconds=60)),
        reading("errors", 0.0),
        reading("ready", 1.0),
    ]
    assert evaluate(profile(), just_fresh).outcome == "healthy"
    # without a raw sample timestamp the reading never counts
    unknown_age = [
        reading("rate", 2.0, latest_sample_at=None),
        reading("errors", 0.0),
        reading("ready", 1.0),
    ]
    assert evaluate(profile(), unknown_age).outcome == "stale"
    # a timestamp after the sample is not fresh either
    future = [
        reading("rate", 2.0, latest_sample_at=NOW + timedelta(seconds=5)),
        reading("errors", 0.0),
        reading("ready", 1.0),
    ]
    assert evaluate(profile(), future).outcome == "stale"
    # the stale sample never confirms health for the session
    session = ObservationSession(
        session_id="s",
        purpose="incident_recovery",
        subject=SubjectRef(kind="incident", id="i"),
        target=Target(
            integration_id="a",
            cluster_uid="b",
            namespace="c",
            resource_uid="d",
            revision="e",
        ),
        subject_control_generation=0,
        observation_generation=1,
        authorized=True,
        health_profile_revision=profile().revision,
    )
    sample = HealthSample(
        sample_id="x",
        session_id="s",
        sequence=1,
        window=evaluation_window(),
        outcome=evaluation.outcome,
        subject_control_generation=0,
        observation_generation=1,
        health_profile_revision=profile().revision,
        required_signals_present=evaluation.required_signals_present,
    )
    assert not confirms_health(session, sample)


def test_sample_time_must_be_timezone_aware():
    with pytest.raises(DomainError, match="timezone-aware"):
        evaluate(profile(), healthy_readings(), sample_time=NOW.replace(tzinfo=None))


def test_duplicate_readings_and_foreign_inputs_are_rejected():
    with pytest.raises(DomainError, match="duplicate"):
        evaluate(profile(), [reading("rate", 1.0), reading("rate", 2.0)])
    with pytest.raises(DomainError, match="INVALID_INPUT"):
        evaluate(profile(), [{"signal_name": "rate"}])  # type: ignore[list-item]
    with pytest.raises(DomainError, match="INVALID_INPUT"):
        evaluate(minimal_profile(), [])  # type: ignore[arg-type]
