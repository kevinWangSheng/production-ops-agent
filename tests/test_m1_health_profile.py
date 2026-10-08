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


#: Every unit-profile selector names the subject under these labels (rule 4).
SCOPE = {"namespace_label": "namespace", "workload_label": "service"}
SEL = "{namespace='ns',service='svc'}"


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
                "query": "sum(rate(x{namespace='ns',service='svc'}[5m]))",
                "coverage_query": "max(count_over_time(x{namespace='ns',service='svc'}[5m]))",
                "freshness_query": "max(timestamp(x{namespace='ns',service='svc'}))",
                "traffic_dependent": False,
                "healthy": {"min": 0},
                "scope": SCOPE,
            },
            {
                "name": "errors",
                "description": "error ratio",
                "query": "e{namespace='ns',service='svc'} / t{namespace='ns',service='svc'}",
                "coverage_query": "max(count_over_time(t{namespace='ns',service='svc'}[5m]))",
                "freshness_query": "max(timestamp(t{namespace='ns',service='svc'}))",
                "minimum_samples": 3,
                "traffic_dependent": True,
                "healthy": {"min": 0, "max": 0.01},
                "scope": SCOPE,
            },
            {
                "name": "ready",
                "description": "replicas",
                "query": "min(ready{namespace='ns',service='svc'})",
                "coverage_query": "min(count_over_time(ready{namespace='ns',service='svc'}[5m]))",
                "freshness_query": "max(timestamp(ready{namespace='ns',service='svc'}))",
                "traffic_dependent": False,
                "healthy": {"min": 1},
                "scope": SCOPE,
            },
            {
                "name": "hint",
                "description": "optional context",
                "required": False,
                "query": "hint{namespace='ns',service='svc'}",
                "coverage_query": "count_over_time(hint{namespace='ns',service='svc'}[5m])",
                "freshness_query": "max(timestamp(hint{namespace='ns',service='svc'}))",
                "traffic_dependent": False,
                "healthy": {"max": 10},
                "scope": SCOPE,
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
        "deployment_ready_replicas",
        "deployment_available_replicas_min_in_window",
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
        "deployment_ready_replicas",
        "deployment_available_replicas_min_in_window",
        "dependency_deployments_available",
    ):
        assert "kube_" in prof.signal(name).query
        assert 'namespace="otel-demo"' in prof.signal(name).query


HEALTHY_CHECKOUT = {
    "deployment_available_replicas": 1.0,
    "request_rate_per_second": 0.0125,
    "error_ratio": 0.0,
    "latency_p95_milliseconds": 120.0,
    "deployment_ready_replicas": 1.0,
    "deployment_available_replicas_min_in_window": 1.0,
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
    query["signals"][0]["query"] = f"sum(rate(y{SEL}[5m]))"
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
        # compound durations are parsed in full, not skipped (codex round 3)
        lambda p: p["signals"][0].update(query="sum(rate(x[1m30s]))"),
        lambda p: p["signals"][0].update(query="sum(rate(x[4m59s]))"),
        # a bracket that is not a duration is refused rather than unchecked
        lambda p: p["signals"][0].update(query="sum(rate(x[abc]))"),
        lambda p: p["signals"][0].update(query="sum(rate(x[5m:abc]))"),
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


def test_range_durations_are_parsed_in_full_and_quoted_brackets_are_ignored():
    """``[1m30s]`` is 90 s, not an unparsable bracket to skip; a regex label
    value may contain brackets without being a range selector."""
    from opspilot.observer.health_profile import _range_selector_seconds

    assert _range_selector_seconds("sum(rate(x[5m]))") == [300]
    assert _range_selector_seconds("max_over_time(x[1m30s:15s])") == [90]
    assert _range_selector_seconds('rate(x{pod=~"c[0-9]+"}[1h5m10s500ms])') == [
        3600 + 300 + 10 + 0.5
    ]
    with pytest.raises(ValueError, match="RANGE_SELECTOR_UNPARSABLE"):
        _range_selector_seconds("rate(x[1m 30s])")
    ninety = minimal_profile(evaluation_window_seconds=90)
    for signal in ninety["signals"]:
        for key in ("query", "coverage_query"):
            signal[key] = signal[key].replace("[5m]", "[1m30s]")
    assert profile(**ninety).evaluation_window_seconds == 90


# 7. Query scope is bound to the subject (issue #122; PRODUCT-CONSTRAINTS
#    "target-specific HealthProfile", C3 section 10).


def shipped_payload(**subject) -> dict:
    payload = json.loads(SHIPPED.read_text(encoding="utf-8"))
    payload["subject"].update(subject)
    return payload


def test_issue_122_a_subject_edited_apart_from_its_queries_is_refused(tmp_path):
    """The reproduction: the shipped checkout profile with only ``subject``
    changed to ``payment`` / ``payments-prod``; every query still selects
    ``namespace="otel-demo",deployment="checkout"``. It does not load, so it
    can never authorize a payment incident with checkout's readings."""
    payload = shipped_payload(service="payment", kubernetes_namespace="payments-prod")
    with pytest.raises(HealthProfileError) as excinfo:
        load_health_profile(write(tmp_path, payload))
    assert excinfo.value.code == "PROFILE_INVALID"
    rendered = str(excinfo.value) + repr(excinfo.value.errors)
    assert "SCOPE_SELECTOR_MISMATCH signals/0/query" in rendered
    # the rejected file values stay out of the error
    assert "payment" not in rendered and "otel-demo" not in rendered
    # each half alone is a mismatch too
    for change in ({"service": "payment"}, {"kubernetes_namespace": "payments-prod"}):
        with pytest.raises(HealthProfileError, match="SCOPE_SELECTOR_MISMATCH"):
            load_health_profile(write(tmp_path, shipped_payload(**change)))


def test_the_shipped_profile_declares_a_scope_for_every_signal():
    prof = load_health_profile(SHIPPED)
    by_name = {s.name: s.scope for s in prof.signals}
    assert by_name["deployment_available_replicas"].namespace_label == "namespace"
    assert by_name["deployment_available_replicas"].workload_label == "deployment"
    # span series carry k8s_namespace_name next to service_name
    # (docs/evidence/m1-02-lab/regression/normal-1/observe-pre.json)
    for name in ("request_rate_per_second", "error_ratio", "latency_p95_milliseconds"):
        assert by_name[name].namespace_label == "k8s_namespace_name"
        assert by_name[name].workload_label == "service_name"
    # pod health is read at Deployment level: no pod-name prefix anywhere
    assert {s.scope.workload_label for s in prof.signals} == {
        "deployment",
        "service_name",
    }
    assert {s.scope.workload_match for s in prof.signals} == {"exact", "dependencies"}
    assert prof.signal("pods_running") is None
    assert prof.signal("pod_restarts_in_window") is None
    for name in (
        "deployment_ready_replicas",
        "deployment_available_replicas_min_in_window",
    ):
        assert by_name[name].workload_label == "deployment"
    deps = by_name["dependency_deployments_available"]
    assert deps.workload_match == "dependencies" and len(deps.dependencies) == 8
    assert prof.subject.service not in deps.dependencies


def test_a_profile_without_a_scope_declaration_is_refused(tmp_path):
    """An older-format file (no ``scope``) is refused rather than loaded
    unchecked; the error names the field."""
    payload = shipped_payload()
    del payload["signals"][0]["scope"]
    with pytest.raises(HealthProfileError) as excinfo:
        load_health_profile(write(tmp_path, payload))
    assert "signals/0/scope" in str(excinfo.value)


def scoped(query: str, **scope) -> dict:
    """The unit profile with signal 0's three queries all set to ``query``
    (the check is per selector, the field's role does not matter) under
    ``SCOPE`` plus ``scope``; ``query`` is checked first, so an error
    locates at ``signals/0/query``."""
    payload = minimal_profile()
    for field in ("query", "coverage_query", "freshness_query"):
        payload["signals"][0][field] = query
    payload["signals"][0]["scope"] = {**SCOPE, **scope}
    return payload


@pytest.mark.parametrize(
    ("query", "code"),
    [
        # the namespace or workload matcher is simply left out
        ("sum(rate(x{service='svc'}[5m]))", "SCOPE_SELECTOR_UNBOUND"),
        ("sum(rate(x{namespace='ns'}[5m]))", "SCOPE_SELECTOR_UNBOUND"),
        ("sum(rate(x{}[5m]))", "SCOPE_SELECTOR_UNBOUND"),
        # a bare metric selects every namespace
        ("sum(rate(x[5m]))", "SCOPE_SELECTOR_UNBOUND"),
        ("up", "SCOPE_SELECTOR_UNBOUND"),
        (f"x{SEL} / y", "SCOPE_SELECTOR_UNBOUND"),
        (f"x{SEL} / y[5m]", "SCOPE_SELECTOR_UNBOUND"),
        # a query with no selector observes nothing
        ("vector(1)", "SCOPE_SELECTOR_UNBOUND"),
        # regex, negation, another value, or a second conflicting matcher
        ("x{namespace=~'ns',service='svc'}", "SCOPE_SELECTOR_MISMATCH"),
        ("x{namespace=~'.*',service='svc'}", "SCOPE_SELECTOR_MISMATCH"),
        ("x{namespace!='other',service='svc'}", "SCOPE_SELECTOR_MISMATCH"),
        ("x{namespace!~'other',service='svc'}", "SCOPE_SELECTOR_MISMATCH"),
        ("x{namespace='ns',service='payment'}", "SCOPE_SELECTOR_MISMATCH"),
        ("x{namespace='ns',service=~'svc'}", "SCOPE_SELECTOR_MISMATCH"),
        (
            "x{namespace='ns',service='svc',namespace='other'}",
            "SCOPE_SELECTOR_MISMATCH",
        ),
        ('x{namespace="ns",service="svc-canary"}', "SCOPE_SELECTOR_MISMATCH"),
        # the escape is decoded before comparing, so it cannot smuggle a value
        ('x{namespace="ns",service="sv\\x63"}', "SCOPE_SELECTOR_UNPARSABLE"),
        # anything the scanner cannot read is refused, not skipped
        ("x{namespace='ns',service='svc'", "SCOPE_SELECTOR_UNPARSABLE"),
        ("x{namespace='ns' service='svc'}", "SCOPE_SELECTOR_UNPARSABLE"),
        ("x{namespace='ns',service='svc',}", "SCOPE_SELECTOR_UNPARSABLE"),
        ("x{namespace=`ns`,service=`svc`}", "SCOPE_SELECTOR_UNPARSABLE"),
        ("x{namespace='ns',service='svc'}}", "SCOPE_SELECTOR_UNPARSABLE"),
        ("sum by le (x{namespace='ns',service='svc'})", "SCOPE_SELECTOR_UNPARSABLE"),
        ("x{namespace='ns',service='svc'} # note", "SCOPE_SELECTOR_UNPARSABLE"),
    ],
)
def test_unscoped_or_misscoped_selectors_are_refused(query, code):
    with pytest.raises(ValueError, match=f"{code} signals/0/query"):
        profile(**scoped(query))


def test_every_query_field_is_checked():
    for field in ("coverage_query", "freshness_query"):
        payload = minimal_profile()
        payload["signals"][2][field] = "max(timestamp(ready{service='svc'}))"
        with pytest.raises(
            ValueError, match=f"SCOPE_SELECTOR_UNBOUND signals/2/{field}"
        ):
            profile(**payload)


@pytest.mark.parametrize(
    "query",
    [
        # the shipped shapes: aggregation modifiers, functions, ``or vector``
        f"sum by (le) (rate(x{SEL}[5m]))",
        f"sum without (pod, instance) (rate(x{SEL}[5m]))",
        f"histogram_quantile(0.95, sum by (le) (rate(x{SEL}[5m])))",
        f"(sum(rate(x{SEL}[5m])) or vector(0)) / sum(rate(x{SEL}[5m]))",
        f"count(x{SEL} >= 1)",
        f"min(timestamp(x{SEL}))",
        f"x{SEL} offset 5m",
        f"x{SEL} @ start()",
        f"x{SEL} and on (pod) group_left (node) y{SEL}",
        # extra matchers on other labels only narrow the selection
        "x{namespace='ns',service='svc',span_kind=\"SPAN_KIND_SERVER\",span_name=~\".*/PlaceOrder\"}",
        'x{namespace="ns",service="svc",pod=~"c[0-9]+\\\\.z"}',
        # an escaped quote in a value is a value character
        'x{namespace="ns",service="svc",note="a\\"b"}',
        # a selector without a metric name is still a selector
        "{__name__='x',namespace='ns',service='svc'}",
        # label matchers may be spaced
        "x{ namespace = 'ns' , service = 'svc' }",
    ],
)
def test_bound_selectors_in_every_promql_shape_load(query):
    assert profile(**scoped(query)).signals[0].query == query


def test_every_signal_binds_a_namespace_label():
    """There is no namespace opt-out: ``null`` and an absent key are both
    refused, so a series family cannot be read across namespaces by
    declaring it namespace-less (codex review of PR #132, P1-2)."""
    for declaration in ({"namespace_label": None}, {}):
        payload = minimal_profile()
        payload["signals"][0]["scope"] = {"workload_label": "service", **declaration}
        with pytest.raises(ValueError, match="namespace_label"):
            profile(**payload)


@pytest.mark.parametrize("keyword", ["offset", "and", "or", "unless", "bool"])
@pytest.mark.parametrize("field", ["query", "coverage_query", "freshness_query"])
def test_a_metric_named_like_a_keyword_is_still_a_bare_metric(keyword, field):
    """Codex review of PR #132, P1-1: ``sum(offset) or sum(<bound>)`` is
    legal PromQL whose left side is an unbound metric named ``offset``;
    keywords are recognised only in the position PromQL reads them."""
    payload = minimal_profile()
    payload["signals"][0][field] = f"sum({keyword}) or sum(x{SEL})"
    with pytest.raises(ValueError, match=f"SCOPE_SELECTOR_UNBOUND signals/0/{field}"):
        profile(**payload)
    payload["signals"][0][field] = f"sum(x{SEL}) or {keyword}"
    with pytest.raises(ValueError, match="SCOPE_SELECTOR_UNBOUND"):
        profile(**payload)
    payload["signals"][0][field] = f"{keyword}[5m]"
    with pytest.raises(ValueError, match="SCOPE_SELECTOR_UNBOUND"):
        profile(**payload)


#: Positions after which PromQL reads the next token as the start of an
#: expression; a keyword there is a metric name (codex recheck of PR #132).
EXPRESSION_START_AFTER = {
    "on": f"x{SEL} or on(service) KW",
    "ignoring": f"x{SEL} or ignoring(service) KW",
    "group_left_empty": f"x{SEL} / on(service) group_left() KW",
    "group_left_labels": f"x{SEL} / on(service) group_left(x) KW",
    "group_right": f"x{SEL} / on(service) group_right(x) KW",
    "bool": f"x{SEL} > bool KW",
    "by": "sum by (service) (KW)",
    "without": "sum without (service) (KW)",
    "paren": f"(KW) or x{SEL}",
    "subquery": "max_over_time(KW[5m:15s])",
    "offset": f"x{SEL} offset 5m or KW",
    "at": f"x{SEL} @ 0 or KW",
    "call_argument": f"clamp_min(KW, 1) or x{SEL}",
    "unary": f"-KW or x{SEL}",
}


@pytest.mark.parametrize("keyword", ["offset", "and", "or", "unless", "bool"])
@pytest.mark.parametrize("position", sorted(EXPRESSION_START_AFTER))
@pytest.mark.parametrize("field", ["query", "coverage_query", "freshness_query"])
def test_a_keyword_at_an_expression_start_is_a_bare_metric(keyword, position, field):
    """Codex recheck of PR #132: ``x{} or on(service) or`` is legal PromQL
    whose right side is the metric ``or``. Whatever closed before it (a
    modifier label list, ``bool``, a paren), an identifier where an
    expression starts is a metric and is unbound."""
    payload = minimal_profile()
    payload["signals"][0][field] = EXPRESSION_START_AFTER[position].replace(
        "KW", keyword
    )
    with pytest.raises(ValueError, match=f"SCOPE_SELECTOR_UNBOUND signals/0/{field}"):
        profile(**payload)
    # the same shape with a bound selector in that position loads
    payload["signals"][0][field] = EXPRESSION_START_AFTER[position].replace(
        "KW", f"{keyword}{SEL}"
    )
    assert profile(**payload)


@pytest.mark.parametrize(
    "query",
    [
        f"x{SEL} offset 5m",
        f"x{SEL}[5m] offset 5m",
        f"x{SEL} and x{SEL}",
        f"x{SEL} unless on (pod) x{SEL}",
        f"x{SEL} > bool 1",
        f"x{SEL} == bool x{SEL}",
        f"vector(0) or x{SEL}",
        f"1 or x{SEL}",
        # a metric named like a keyword with a bound selector is a selector
        f"offset{SEL}",
        f"or{SEL} or and{SEL}",
        f"x{SEL} >= bool 1",
        f"x{SEL} != bool x{SEL}",
        f"sum(x{SEL}) by (service) or sum(x{SEL}) without (pod)",
        f"x{SEL} / ignoring(pod) group_right x{SEL}",
        f"x{SEL} @ end() offset 5m",
        f"-x{SEL} + +x{SEL}",
        f'label_replace(x{SEL}, "a", "$1", "b", "(.*)")',
        f"max_over_time((x{SEL})[5m:15s])",
    ],
)
def test_keywords_in_their_syntactic_position_still_load(query):
    assert profile(**scoped(query)).signals[0].query == query


def test_dependency_scopes_are_derived_from_the_subject_and_prefixes_are_refused():
    """A pod-name prefix (``checkout-.*``) is not a workload identity:
    ``checkout-canary-...`` matches too (codex review of PR #132, P1-3), so
    there is no ``prefix`` match kind and a prefix regex on the workload
    label is a mismatch like any other regex."""
    with pytest.raises(ValueError, match="literal_error"):
        profile(
            **scoped(
                "x{namespace='ns',pod=~'svc-.*'}",
                workload_label="pod",
                workload_match="prefix",
            )
        )
    with pytest.raises(ValueError, match="SCOPE_SELECTOR_MISMATCH"):
        profile(**scoped("x{namespace='ns',service=~'svc-.*'}"))
    deps = {
        "workload_label": "deployment",
        "workload_match": "dependencies",
        "dependencies": ["payment", "cart"],
    }
    assert profile(**scoped("x{namespace='ns',deployment=~'payment|cart'}", **deps))
    for bad in (
        "x{namespace='ns',deployment=~'payment|cart|svc'}",
        "x{namespace='ns',deployment=~'payment'}",
        "x{namespace='ns',deployment='payment'}",
        "x{namespace='ns',deployment=~'cart|payment'}",
        "x{namespace='ns',deployment=~'.*'}",
    ):
        with pytest.raises(ValueError, match="SCOPE_SELECTOR_MISMATCH"):
            profile(**scoped(bad, **deps))


@pytest.mark.parametrize(
    ("scope", "code"),
    [
        ({"namespace_label": "service"}, "SCOPE_LABELS_IDENTICAL"),
        ({"workload_match": "dependencies"}, "SCOPE_DEPENDENCIES_INCONSISTENT"),
        ({"dependencies": ["a"]}, "SCOPE_DEPENDENCIES_INCONSISTENT"),
        (
            {"workload_match": "dependencies", "dependencies": ["a", "a"]},
            "SCOPE_DEPENDENCY_DUPLICATE",
        ),
        (
            {"workload_match": "dependencies", "dependencies": ["a.*"]},
            "string_pattern_mismatch",
        ),
        ({"workload_match": "any"}, "literal_error"),
        ({"workload_label": "bad-label"}, "string_pattern_mismatch"),
        ({"extra": 1}, "extra_forbidden"),
    ],
)
def test_scope_declarations_are_validated(scope, code):
    with pytest.raises(ValueError, match=code):
        profile(**scoped(f"x{SEL}", **scope))


def test_subject_values_are_dns_subdomains_so_derived_regexes_are_literal():
    for subject in (
        {"service": "svc.*", "kubernetes_namespace": "ns"},
        {"service": "svc", "kubernetes_namespace": "ns|other"},
        {"service": "", "kubernetes_namespace": "ns"},
        {"service": "Svc", "kubernetes_namespace": "ns"},
        {"service": "svc.", "kubernetes_namespace": "ns"},
        {"service": "-svc", "kubernetes_namespace": "ns"},
        {"service": "a" * 254, "kubernetes_namespace": "ns"},
    ):
        with pytest.raises(ValueError, match="subject"):
            profile(subject=subject)


def test_dotted_workload_and_dependency_names_are_legal_and_matched_literally():
    """A Deployment name is a DNS-1123 subdomain (``checkout.prod`` is
    legal, PR #132 bot triage); the exact match uses it verbatim and the
    dependency regex escapes the dot so ``a.b`` cannot match ``axb``."""
    dotted = minimal_profile(
        subject={"service": "svc.prod", "kubernetes_namespace": "ns.east"}
    )
    for signal in dotted["signals"]:
        for key in ("query", "coverage_query", "freshness_query"):
            signal[key] = signal[key].replace(
                SEL, "{namespace='ns.east',service='svc.prod'}"
            )
    assert profile(**dotted).subject.service == "svc.prod"
    deps = {
        "workload_label": "deployment",
        "workload_match": "dependencies",
        "dependencies": ["a.b", "c"],
    }
    assert profile(**scoped('x{namespace="ns",deployment=~"a\\\\.b|c"}', **deps))
    for bad in (
        'x{namespace="ns",deployment=~"a.b|c"}',
        'x{namespace="ns",deployment=~"axb|c"}',
    ):
        with pytest.raises(ValueError, match="SCOPE_SELECTOR_MISMATCH"):
            profile(**scoped(bad, **deps))


def test_vector_selectors_are_extracted_with_decoded_values():
    from opspilot.observer.health_profile import _vector_selectors

    found = _vector_selectors(
        'sum by (le) (rate(x{a="1",b=~"c[0-9]+",d!="e\\"f"}[5m])) or vector(0)'
    )
    assert [[(m.label, m.op, m.value) for m in sel] for sel in found] == [
        [("a", "=", "1"), ("b", "=~", "c[0-9]+"), ("d", "!=", 'e"f')]
    ]
    assert _vector_selectors("vector(0) + 1e3") == []
    nameless = _vector_selectors("{__name__='x'} - {y='z'}")
    assert [[(m.label, m.op, m.value) for m in sel] for sel in nameless] == [
        [("__name__", "=", "x")],
        [("y", "=", "z")],
    ]


#: ``(query, accepted by Prometheus's own parser)``, recorded by running each
#: query through ``parser.ParseExpr`` of ``github.com/prometheus/prometheus``
#: v0.307.2 (Prometheus 3.7.2, ``promql/parser``; grammar in
#: ``generated_parser.y``: ``bin_expr`` ``ATAN2``, ``offset_expr``,
#: ``step_invariant_expr`` ``AT signed_or_unsigned_number``,
#: ``aggregate_expr`` and the lexer keywords ``inf`` / ``nan``). Every
#: selector is namespace-bound, so scope binding never decides the outcome:
#: the scanner must reject exactly what Prometheus rejects. Not covered:
#: keyword case (``SUM(...) BY (s)``) and chained ``offset`` signs
#: (``offset --5m`` is valid there; the scanner refuses any chain).
PROMQL_PARSER_ORACLE = [
    ('x{a="b"} atan2 y{a="b"}', True),
    ('x{a="b"} @ START()', True),
    ('x{a="b"} offset 1or y{a="b"}', False),
    ('x{a="b"} @ 1and y{a="b"}', False),
    ('x{a="b"} offset 1 or y{a="b"}', True),
    ('x{a="b"} offset 5m or y{a="b"}', True),
    ('x{a="b"} offset 5munless y{a="b"}', False),
    ('rate(x{a="b"}[5m] offset 5m)', True),
    ('x{a="b"} @ 1+y{a="b"}', True),
    ('x{a="b"} offset 5m+y{a="b"}', True),
    ('x{a="b"} offset 1s1h', False),
    ('x{a="b"} @ 1m1m', False),
    ('x{a="b"} offset 1h1m1s1ms', True),
    ('x{a="b"} offset 1y1w1d1h1m1s1ms', True),
    ('x{a="b"} offset 1ms1s', False),
    ('x{a="b"} offset 0s', True),
    ('x{a="b"} @ -start()', False),
    ('x{a="b"} @ +end()', False),
    ('x{a="b"} offset -1bogus', False),
    ('x{a="b"} offset 1bogus', False),
    ('x{a="b"} @ -1m', True),
    ('x{a="b"} @ 1bogus', False),
    ('x{a="b"} @ 1.5s', False),
    ('x{a="b"} @ 1m30s', True),
    ('x{a="b"} @ 1m30', False),
    ('x{a="b"} offset 5', True),
    ('x{a="b"} offset 1.5', True),
    ('x{a="b"} offset 1.5s', False),
    ('x{a="b"} offset 1h30m', True),
    ('x{a="b"} offset 1m30', False),
    ('x{a="b"} offset -1e3', True),
    ('x{a="b"} offset 1e3s', False),
    ('rate(x{a="b"}[5m] offset -1bogus)', False),
    ('x{a="b"} @ End()', True),
    ('x{a="b"} @ --1', False),
    ('x{a="b"} @ -+1', False),
    ('x{a="b"} atan2 on(s) y{a="b"}', True),
    ('x{a="b"} atan2 bool y{a="b"}', False),
    ('x{a="b"} ATAN2 y{a="b"}', True),
    ('x{a="b"} offset -5m', True),
    ('x{a="b"} offset 5m', True),
    ('x{a="b"} offset +5m', True),
    ('x{a="b"} offset - 5m', True),
    ('x{a="b"} @ -1', True),
    ('x{a="b"} @ 1', True),
    ('x{a="b"} @ +1', True),
    ('x{a="b"} @ -1.5', True),
    ('x{a="b"} @ - 1', True),
    ('x{a="b"} @ start()', True),
    ('x{a="b"} @ end()', True),
    ('x{a="b"} @ Inf', False),
    ('x{a="b"}[5m] @ -1', True),
    ('rate(x{a="b"}[5m] offset -1w)', True),
    ('rate(x{a="b"}[5m] offset 1w)', True),
    ('rate(x{a="b"}[5m] @ -1 offset -1m)', True),
    ('x{a="b"} by(service)', False),
    ('x{a="b"} without(service)', False),
    ('x{a="b"} + y{a="b"} by(service)', False),
    ('sum(x{a="b"}) by (s)', True),
    ('sum(x{a="b"}) without (s)', True),
    ('sum by (s) (x{a="b"})', True),
    ('sum by (s) (x{a="b"}) by (t)', False),
    ('sum(x{a="b"}) by (s) by (t)', False),
    ('sum(x{a="b"}) by (s) without (t)', False),
    ('rate(x{a="b"}[5m]) by (s)', False),
    ('sum(rate(x{a="b"}[5m])) by (s)', True),
    ('sum(rate(x{a="b"}[5m]) by (s))', False),
    ('(x{a="b"}) by (s)', False),
    ('topk(3, x{a="b"}) by (s)', True),
    ('count_values("v", x{a="b"}) by (s)', True),
    ('quantile(0.9, x{a="b"}) by (s)', True),
    ('quantile by (s) (0.9, x{a="b"})', True),
    ('clamp(x{a="b"}, 0, +Inf)', True),
    ('clamp(x{a="b"}, -Inf, 1)', True),
    ('clamp(x{a="b"}, 0, inf)', True),
    ('clamp(x{a="b"}, 0, INF)', True),
    ('x{a="b"} > NaN', True),
    ('x{a="b"} > nan', True),
    ('x{a="b"} > -NaN', True),
    ('Inf + x{a="b"}', True),
    ('x{a="b"} * Inf', True),
    ('inf{a="b"}', False),
    ('inf(x{a="b"})', False),
    ('Inf x{a="b"}', False),
    ('x{a="b"} Inf', False),
    ('x{a="b"} AND y{a="b"}', True),
    ('x{a="b"} Or y{a="b"}', True),
    ('x{a="b"} unLESS y{a="b"}', True),
    ('x{a="b"} + on(s) group_left y{a="b"}', True),
    ('sum(x{a="b"}) by (s) + sum(y{a="b"}) by (s)', True),
    ('sum(x{a="b"}) by (s) / on(s) sum(y{a="b"}) without (s)', True),
    ('x{a="b"} offset', False),
    ('x{a="b"} offset -', False),
    ('x{a="b"} @', False),
    ('x{a="b"} @ -', False),
    ('x{a="b"} offset -x', False),
]


@pytest.mark.parametrize(
    ("query", "accepted"),
    [
        ('rate(x{a="b"}[1h30m])', True),
        ('rate(x{a="b"}[1s1h])', False),  # Prometheus: out-of-order units
        ('rate(x{a="b"}[1m1m])', False),  # repeated unit
        ('x{a="b"}[5m:1s1m]', False),  # same rule for a subquery resolution
    ],
)
def test_range_selector_durations_follow_the_prometheus_unit_order(query, accepted):
    from opspilot.observer.health_profile import _range_selector_seconds

    if accepted:
        assert _range_selector_seconds(query)
    else:
        with pytest.raises(ValueError, match="RANGE_SELECTOR_UNPARSABLE"):
            _range_selector_seconds(query)


@pytest.mark.parametrize(("query", "accepted"), PROMQL_PARSER_ORACLE)
def test_vector_selector_scanner_agrees_with_prometheus_parser(query, accepted):
    from opspilot.observer.health_profile import _vector_selectors

    if accepted:
        found = _vector_selectors(query)
        assert found, query
        assert all(
            any(m.label == "a" and m.value == "b" for m in selector)
            for selector in found
        )
    else:
        # ``atan2 bool`` reads ``bool`` as a bare metric: still refused.
        with pytest.raises(ValueError, match="SCOPE_SELECTOR_(UNPARSABLE|UNBOUND)"):
            _vector_selectors(query)
