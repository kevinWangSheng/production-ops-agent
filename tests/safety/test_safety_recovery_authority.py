"""Recovery is confirmed only by the independent HealthProfile verdict
(PRODUCT-CONSTRAINTS "Recovery observations": missing required signals cannot
confirm recovery; no profile permits investigation but yields unknown).

Table-driven over every shipped profile and each of its required signals:
taking one signal away, or giving it any non-``ok`` status, too few points,
a stale reading or an out-of-bound value, never yields a sample that
confirms health, and a session without a bound profile never confirms. The Observer that
produces those verdicts is independent of the investigator (C3 section 3,
D3): it imports nothing from the investigation side.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

from opspilot.domain import HealthSample, ObservationSession, QueryWindow
from opspilot.domain.intake import Target
from opspilot.domain.observation import confirms_health
from opspilot.domain.subjects import SubjectRef
from opspilot.observer import (
    PROFILE_DIRECTORY,
    SignalReading,
    evaluate_readings,
    load_health_profile,
)

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
PROFILES = sorted(PROFILE_DIRECTORY.glob("*.json"))


def _healthy_value(signal) -> float:
    bounds = signal.healthy
    low = bounds.min if bounds.min is not None else 0.0
    high = bounds.max if bounds.max is not None else low + 10.0
    return (low + high) / 2 if high > low else float(low)


def _reading(profile, signal, **overrides) -> SignalReading:
    fields = {
        "signal_name": signal.name,
        "status": "ok",
        "value": _healthy_value(signal),
        "sample_count": max(signal.minimum_samples, 3) + 2,
        "query": signal.query,
        "window_start": NOW - timedelta(seconds=profile.evaluation_window_seconds),
        "window_end": NOW,
        "source": "prometheus",
        "raw_sha256": "0" * 64,
        "latest_sample_at": NOW - timedelta(seconds=15),
    }
    fields.update(overrides)
    if fields["status"] != "ok":
        fields["value"] = None
        fields["sample_count"] = None
    return SignalReading(**fields)


def _healthy(profile) -> list[SignalReading]:
    return [_reading(profile, signal) for signal in profile.signals]


def _confirms(profile, readings, *, bound=True, present=None) -> bool:
    evaluation = evaluate_readings(profile, readings, sample_time=NOW)
    session = ObservationSession(
        session_id="safety-session",
        purpose="incident_recovery",
        subject=SubjectRef(kind="incident", id="safety-incident"),
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
        health_profile_revision=profile.revision if bound else None,
    )
    sample = HealthSample(
        sample_id="safety-sample",
        session_id="safety-session",
        sequence=1,
        window=QueryWindow(
            start=NOW - timedelta(seconds=profile.evaluation_window_seconds), end=NOW
        ),
        outcome=evaluation.outcome,
        subject_control_generation=0,
        observation_generation=0,
        health_profile_revision=evaluation.health_profile_revision,
        required_signals_present=evaluation.required_signals_present
        if present is None
        else present,
    )
    return confirms_health(session, sample)


def _cases():
    for path in PROFILES:
        profile = load_health_profile(path)
        for signal in profile.signals:
            if signal.required:
                yield pytest.param(path, signal.name, id=f"{path.stem}:{signal.name}")


def test_profiles_are_shipped():
    assert PROFILES


@pytest.mark.parametrize("path", PROFILES, ids=lambda p: p.stem)
def test_all_required_signals_in_bounds_confirm(path):
    """The control: the complete healthy reading set does confirm."""
    profile = load_health_profile(path)
    assert _confirms(profile, _healthy(profile))


@pytest.mark.parametrize("path", PROFILES, ids=lambda p: p.stem)
def test_without_a_bound_profile_recovery_is_never_confirmed(path):
    profile = load_health_profile(path)
    assert not _confirms(profile, _healthy(profile), bound=False)


@pytest.mark.parametrize(
    "spoil",
    [
        "missing",
        "no_data",
        "stale",
        "timeout",
        "failed",
        "too_few",
        "old",
        "late_window",
        "bound",
    ],
)
@pytest.mark.parametrize(("path", "name"), list(_cases()))
def test_one_spoiled_required_signal_never_confirms(path, name, spoil):
    profile = load_health_profile(path)
    readings = []
    for signal in profile.signals:
        if signal.name != name:
            readings.append(_reading(profile, signal))
        elif spoil == "missing":
            continue
        elif spoil in {"no_data", "stale", "timeout", "failed"}:
            readings.append(_reading(profile, signal, status=spoil))
        elif spoil == "too_few":
            readings.append(_reading(profile, signal, sample_count=0))
        elif spoil == "late_window":
            ended = NOW - timedelta(hours=6)
            readings.append(
                _reading(
                    profile,
                    signal,
                    window_start=ended
                    - timedelta(seconds=profile.evaluation_window_seconds),
                    window_end=ended,
                )
            )
        elif spoil == "old":
            readings.append(
                _reading(profile, signal, latest_sample_at=NOW - timedelta(days=1))
            )
        else:
            bounds = signal.healthy
            outside = bounds.max + 1.0 if bounds.max is not None else bounds.min - 1.0
            readings.append(_reading(profile, signal, value=outside))
    assert not _confirms(profile, readings)


@pytest.mark.parametrize("path", PROFILES, ids=lambda p: p.stem)
def test_a_healthy_sample_without_its_required_signals_never_confirms(path):
    """Confirmation checks the signal flag itself, not only the outcome word."""
    profile = load_health_profile(path)
    assert not _confirms(profile, _healthy(profile), present=False)


# -- independence: the Observer is not the investigator (C3 section 3, D3) --

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
INVESTIGATION_SIDE = (
    "opspilot.tools",
    "opspilot.investigation",
    "opspilot.worker",
    "opspilot.worker_main",
    "opspilot.web",
    "opspilot.tracing",
    "opspilot.acceptance",
    "opspilot.intake",
    "opspilot.recovery",
)


def _imports(path: pathlib.Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_observer_packages_import_nothing_from_the_investigation_side():
    offenders = []
    for package in ("observer", "observation"):
        for path in sorted((REPO_ROOT / "opspilot" / package).rglob("*.py")):
            bad = sorted(n for n in _imports(path) if n.startswith(INVESTIGATION_SIDE))
            if bad:
                offenders.append((path.relative_to(REPO_ROOT).as_posix(), bad))
    assert not offenders, offenders


def test_the_observer_process_loads_no_gateway_worker_model_or_web_module():
    script = (
        "import sys, opspilot.observer.__main__, opspilot.observer.loop\n"
        "print(sorted(m for m in sys.modules if m.startswith('opspilot')))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO_ROOT,
    )
    loaded = set(eval(proc.stdout.strip()))  # noqa: S307 - our own print
    leaked = sorted(m for m in loaded if m.startswith(INVESTIGATION_SIDE))
    assert not leaked, leaked
    assert "opspilot.observer.sampler" in loaded
