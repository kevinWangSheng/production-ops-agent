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
