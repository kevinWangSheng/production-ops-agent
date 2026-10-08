"""Adapter negative checks, not PostgreSQL product acceptance evidence."""

import base64
import json
from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.acceptance.test_f6_recovery import (
    HANDLED,
    observations,
    profile,
    with_raw_payloads,
)
from tests.f6_boundary_support import GuardedRecoveryDriver, RecoveryBoundaries
from tests.f6_product_driver import (
    BoundarySource,
    ProductRecoveryRuntime,
    decode_profile,
    encode_profile,
)


def test_native_profile_preserves_frozen_fixture_contract():
    frozen = profile.__wrapped__()
    assert decode_profile(encode_profile(frozen, HANDLED)) == frozen


def test_source_queries_harness_for_value_coverage_and_freshness():
    frozen = profile.__wrapped__()
    native = encode_profile(frozen, HANDLED)
    boundaries = RecoveryBoundaries()
    supplied = with_raw_payloads(observations(1))
    boundaries.configure_telemetry(supplied)
    source = BoundarySource(boundaries, native)
    signal = native.signals[0]
    for expr in (signal.query, signal.coverage_query, signal.freshness_query):
        result = source.instant(expr, at=supplied[0]["window_end"], timeout_seconds=1)
        assert result.body == supplied[0]["signals"][signal.name]["raw_payload"]
    assert {row[3] for row in boundaries.telemetry_calls} == {
        "query",
        "coverage",
        "freshness",
    }
    assert all(
        row[1:3] == (supplied[0]["window_start"], supplied[0]["window_end"])
        for row in boundaries.telemetry_calls
    )


@pytest.mark.parametrize(
    "field,bad",
    [("query", "foreign-query"), ("source", "foreign-source"), ("value", 999)],
)
def test_saved_reading_mismatch_cannot_be_hidden_by_raw_stimulus(field, bad):
    native = encode_profile(profile.__wrapped__(), HANDLED)
    supplied = with_raw_payloads(observations(1))[0]
    name = "deployment"
    payload = supplied["signals"][name]["raw_payload"]
    query = dict(
        body_b64=base64.b64encode(payload).decode(),
        body_sha256=sha256(payload).hexdigest(),
    )
    raw = json.dumps(dict(query=query, freshness=query)).encode()
    reading = dict(
        signal_name=name,
        query=native.signal(name).query,
        source=native.source,
        value=1.0,
        status="ok",
        raw=raw,
        raw_sha256=sha256(raw).hexdigest(),
    )
    reading[field] = bad
    sample = dict(
        sample_id=uuid4(),
        sequence=1,
        subject_control_generation=1,
        observation_generation=1,
        window_start=supplied["window_start"],
        window_end=supplied["window_end"],
        disposition="adopted",
        outcome="healthy",
        readings=[reading],
    )
    history = dict(
        session=dict(session_id=uuid4(), target=supplied["target"]),
        health_profile=dict(content=native.model_dump_json()),
        samples=[sample],
    )
    runtime = ProductRecoveryRuntime.__new__(ProductRecoveryRuntime)
    with pytest.raises(AssertionError):
        runtime._samples("incident-f6", [history])


@pytest.mark.parametrize("raises", [False, True])
def test_replay_guard_detects_business_mutation_even_on_exception(raises):
    state = {"incident_lifecycle": "open"}

    def bad_replay(**kwargs):
        state["incident_lifecycle"] = "resolved"
        if raises:
            raise ValueError("replay failed after writing")
        return None

    runtime = SimpleNamespace(
        replay=bad_replay, snapshot_incident=lambda _: deepcopy(state)
    )
    boundaries = RecoveryBoundaries()
    driver = GuardedRecoveryDriver(runtime, boundaries)
    with pytest.raises(AssertionError):
        driver.replay(persisted=dict(subject_id="incident-f6"))
    assert boundaries.replaying is False


def test_driver_does_not_forward_structured_observations_and_rejects_missing_io():
    boundaries = RecoveryBoundaries()
    supplied = with_raw_payloads(observations(1))

    def bypass(scenario, **stimuli):
        assert "observations" not in stimuli
        assert set(stimuli["schedule"][0]) == {"sequence", "window_start", "window_end"}
        return SimpleNamespace(
            subject_id=scenario.subject_id, recovery_profile=profile.__wrapped__()
        )

    driver = GuardedRecoveryDriver(SimpleNamespace(run=bypass), boundaries)
    with pytest.raises(AssertionError):
        driver.run(SimpleNamespace(subject_id="incident-f6"), observations=supplied)


def test_unrelated_runtime_error_is_failure_in_all_seven_xfail_scenarios(tmp_path):
    """Run the actual marked functions with a broken transport, without PG."""
    import os
    import subprocess
    import sys
    import xml.etree.ElementTree as ET
    from pathlib import Path

    probe = tmp_path / "test_unrelated_transport.py"
    probe.write_text(
        """from types import SimpleNamespace
import pytest
from tests.acceptance.test_f6_recovery import (
    profile as original_profile,
    test_f6_step1_foreign_target_sample_is_history_only_without_advancing as test_foreign_target,
)
from tests.contracts.test_f6_observation import (
    test_persisted_authority_guard_keeps_late_result_only_as_history as test_persisted_authority,
)

@pytest.fixture
def profile():
    return original_profile.__wrapped__()

@pytest.fixture
def f6_profile(profile):
    return profile

@pytest.fixture
def recovery_driver():
    def broken_transport(*args, **kwargs):
        raise RuntimeError("F6_UNRELATED_TRANSPORT_FAILURE")
    return SimpleNamespace(run=broken_transport)
""",
        encoding="utf-8",
    )
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\n", encoding="utf-8")
    report = tmp_path / "report.xml"
    env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(probe),
            "-c",
            str(config),
            "--confcutdir",
            str(tmp_path),
            "-p",
            "no:cacheprovider",
            "--junitxml",
            str(report),
            "-q",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    cases = ET.parse(report).findall(".//testcase")
    assert len(cases) == 7
    for case in cases:
        failure = case.find("failure")
        assert failure is not None, ET.tostring(case).decode()
        assert "RuntimeError: F6_UNRELATED_TRANSPORT_FAILURE" in failure.text
        assert case.find("skipped") is None
        assert case.find("error") is None
