"""Adapter negative checks, not PostgreSQL product acceptance evidence."""

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
    for kind, expr in (
        ("query", signal.query),
        ("coverage", signal.coverage_query),
        ("freshness", signal.freshness_query),
    ):
        result = source.instant(expr, at=supplied[0]["window_end"], timeout_seconds=1)
        assert (
            result.body
            == supplied[0]["signals"][signal.name][
                "raw_payload" if kind == "query" else f"{kind}_payload"
            ]
        )
    assert {row[3] for row in boundaries.telemetry_calls} == {
        "query",
        "coverage",
        "freshness",
    }
    assert all(
        row[1:3] == (supplied[0]["window_start"], supplied[0]["window_end"])
        for row in boundaries.telemetry_calls
    )


def projected_sample_fixture():
    from opspilot.acceptance import RecoverySample, RecoverySignal

    native = encode_profile(profile.__wrapped__(), HANDLED)
    supplied = with_raw_payloads(observations(1))[0]
    name = "deployment"
    payload = supplied["signals"][name]["raw_payload"]
    identity = uuid4()
    signal = RecoverySignal(
        signal_name=name,
        status="ok",
        value=1.0,
        sample_count=1,
        query=native.signal(name).query,
        source=native.source,
        window_start=supplied["window_start"],
        window_end=supplied["window_end"],
        evaluated_at=supplied["window_end"],
        sample_time=supplied["window_end"],
        observed_at=supplied["signals"][name]["observed_at"],
        evidence_id="fixture:deployment",
        raw_sha256="0" * 64,
        body_sha256=sha256(payload).hexdigest(),
        verdict="healthy",
        reason="fixture",
    )
    sample = RecoverySample(
        subject_id=str(identity),
        sample_id=str(uuid4()),
        session_id=str(uuid4()),
        sequence=1,
        target=deepcopy(supplied["target"]),
        subject_control_generation=1,
        observation_generation=1,
        health_profile_revision=native.revision,
        window_start=supplied["window_start"],
        window_end=supplied["window_end"],
        disposition="adopted",
        reason="adopted",
        outcome="healthy",
        required_signals_present=True,
        confirms_health=False,
        health_basis="confirmed",
        transition=None,
        signals={name: signal},
    )
    runtime = ProductRecoveryRuntime.__new__(ProductRecoveryRuntime)
    runtime.ids = {"incident-f6": identity}
    runtime.read_raw_payload = lambda _: payload
    return runtime, native, sample, payload


@pytest.mark.parametrize(
    "field,bad",
    [("query", "foreign-query"), ("source", "foreign-source"), ("value", 999)],
)
def test_saved_reading_mismatch_cannot_be_hidden_by_raw_stimulus(field, bad):
    from dataclasses import replace

    runtime, native, sample, _ = projected_sample_fixture()
    signal = replace(sample.signals["deployment"], **{field: bad})
    sample = replace(sample, signals={"deployment": signal})
    with pytest.raises(AssertionError):
        runtime._mapped_samples(
            "incident-f6", SimpleNamespace(recovery_samples=(sample,)), native
        )


def test_mapped_sample_target_comes_from_product_not_telemetry():
    runtime, native, sample, payload = projected_sample_fixture()
    altered = json.loads(payload)
    altered["target"] = {**sample.target, "namespace": "another-demo"}
    runtime.read_raw_payload = lambda _: json.dumps(altered).encode()
    mapped = runtime._mapped_samples(
        "incident-f6", SimpleNamespace(recovery_samples=(sample,)), native
    )
    assert mapped[0]["target"] == sample.target
    assert mapped[0]["target"] != altered["target"]


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


def test_unrelated_runtime_error_is_failure_in_all_seven_authority_scenarios(tmp_path):
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
    test_f6_step1_target_identity_reregistration_is_rejected_and_sampling_stays_bound as test_target_identity,
    test_f6_step1_revision_rehandling_fences_old_session_result as test_revision,
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


def test_permissions_are_measured_and_extra_temp_is_not_filtered(
    recovery_driver, f6_profile
):
    from psycopg import sql

    from tests.acceptance.test_f6_recovery import assert_readonly, scenario

    requested = scenario(1, "permissions-not-filtered")
    baseline = recovery_driver.run(
        requested,
        profile=f6_profile,
        handled_at=HANDLED,
        observations=[],
        until=HANDLED,
    )
    assert baseline.permissions == ("read_only", "human_control")
    owner = recovery_driver.runtime.owner
    with owner.transaction() as conn:
        db = conn.info.dbname
        conn.execute(
            sql.SQL("GRANT TEMP ON DATABASE {} TO PUBLIC").format(sql.Identifier(db))
        )
    try:
        measured = recovery_driver.runtime.outcome(requested.subject_id)
        assert "database_temp" in measured.permissions
        assert (
            measured.permissions
            == recovery_driver.runtime._product_projection(
                requested.subject_id
            ).permissions
        )
        with pytest.raises(AssertionError):
            assert_readonly(measured, recovery_driver)
    finally:
        with owner.transaction() as conn:
            conn.execute(
                sql.SQL("REVOKE TEMP ON DATABASE {} FROM PUBLIC").format(
                    sql.Identifier(db)
                )
            )


@pytest.mark.parametrize(
    "mutation",
    [
        "session_subject",
        "authorization_subject",
        "authorization_subject_id",
        "session_revision",
        "authorization_revision",
        "persistent_digest",
        "projection_content",
        "session_existing_revision",
        "authorization_existing_revision",
        "missing_projection_profile",
    ],
)
def test_mapping_rejects_tampered_product_subject_or_profile(
    recovery_driver, f6_profile, mutation
):
    from dataclasses import replace

    from tests.acceptance.test_f6_recovery import scenario

    requested = scenario(1, "mapping-binding-guard")
    recovery_driver.run(
        requested,
        profile=f6_profile,
        handled_at=HANDLED,
        observations=[],
        until=HANDLED,
    )
    runtime = recovery_driver.runtime
    if mutation in {"session_existing_revision", "authorization_existing_revision"}:
        updated = deepcopy(f6_profile)
        updated["max_error_ratio"] = 0.005
        recovery_driver.run(
            requested,
            profile=updated,
            handled_at=HANDLED,
            observations=[],
            until=HANDLED,
        )
    records = runtime.controller.incident_records(runtime.ids[requested.subject_id])
    projected = deepcopy(runtime._product_projection(requested.subject_id, records))
    if mutation == "session_subject":
        projected.observation_sessions[0]["subject"]["id"] = str(uuid4())
    elif mutation == "authorization_subject":
        projected.observation_authorization["subject"]["id"] = str(uuid4())
    elif mutation == "authorization_subject_id":
        projected.observation_authorization["subject_id"] = str(uuid4())
    elif mutation == "session_revision":
        projected.observation_sessions[0]["health_profile_revision"] = (
            "unknown@revision"
        )
    elif mutation == "authorization_revision":
        projected.observation_authorization["health_profile_revision"] = (
            "unknown@revision"
        )
    elif mutation == "session_existing_revision":
        projected.observation_sessions[-1]["health_profile_revision"] = records[
            "sessions"
        ][0]["session"]["health_profile_revision"]
    elif mutation == "authorization_existing_revision":
        projected.observation_authorization["health_profile_revision"] = records[
            "sessions"
        ][0]["session"]["health_profile_revision"]
    elif mutation == "persistent_digest":
        records["sessions"][0]["health_profile"]["content_sha256"] = "0" * 64
    elif mutation == "missing_projection_profile":
        projected = replace(
            projected, recovery_profile_content=None, recovery_profile_revision=None
        )
    elif mutation == "projection_content":
        projected = replace(
            projected, recovery_profile_content=projected.recovery_profile_content + " "
        )
    with pytest.raises(AssertionError):
        runtime._normalize(requested.subject_id, projected, records=records)


def test_replay_earlier_session_excludes_later_handling_and_lifecycle(
    recovery_driver, f6_profile
):
    from datetime import timedelta

    from tests.acceptance.test_f6_recovery import TARGET, assert_readonly, scenario

    requested = scenario(5, "earlier-session-isolated")
    first_rows = with_raw_payloads(observations(2))
    first = recovery_driver.run(
        requested,
        profile=f6_profile,
        handled_at=HANDLED,
        observations=deepcopy(first_rows),
        until=first_rows[-1]["window_end"],
    )
    session_id = first.recovery_samples[0]["session_id"]
    updated = deepcopy(f6_profile)
    updated["target"] = {**TARGET, "revision": "second-revision"}
    later_rows = observations(3)
    for row in later_rows:
        row["target"] = updated["target"]
        for key in ("window_start", "window_end"):
            row[key] += timedelta(seconds=120)
        for signal in row["signals"].values():
            signal["observed_at"] += timedelta(seconds=120)
    later_rows = with_raw_payloads(later_rows)
    current = recovery_driver.run(
        requested,
        profile=updated,
        handled_at=HANDLED + timedelta(seconds=120),
        observations=deepcopy(later_rows),
        until=later_rows[-1]["window_end"],
    )
    assert current.incident_lifecycle == "resolved"
    assert current.actions.count("record_handling") == 2
    persisted = recovery_driver.persisted_replay_input(requested.subject_id, session_id)
    replayed = recovery_driver.replay(
        persisted=persisted, allow_telemetry=False, allow_model=False
    )
    assert replayed.incident_lifecycle == "observing_recovery"
    assert replayed.recorded_lifecycle == "observing_recovery"
    assert replayed.recovery_confirmed is False
    assert replayed.target == TARGET
    assert len(replayed.observation_sessions) == 1
    assert len(replayed.handling_audit) == 1
    assert replayed.handling_audit[0]["payload"]["session_id"] == session_id
    assert replayed.actions.count("record_handling") == 1
    assert replayed.actions.count("persist_observation") == 2
    assert replayed.actions.count("advance_incident_lifecycle") == 1
    assert replayed.actions.count("read_only_query") == 36
    assert replayed.recovery_samples == first.recovery_samples
    assert_readonly(replayed, recovery_driver)


@pytest.mark.parametrize(
    "removed",
    [
        "empty",
        "record_handling",
        "read_only_query",
        "persist_observation",
        "advance_incident_lifecycle",
        "human_handoff",
        "truncate_persist",
        "truncate_query",
    ],
)
def test_action_contract_rejects_empty_or_truncated_audit(
    recovery_driver, f6_profile, removed
):
    from tests.acceptance.test_f6_recovery import assert_readonly, scenario

    handoff = removed == "human_handoff"
    rows = with_raw_payloads(
        observations(5 if handoff else 3, traffic=0 if handoff else 200)
    )
    outcome = recovery_driver.run(
        scenario(1, "action-audit-missing"),
        profile=f6_profile,
        handled_at=HANDLED,
        observations=rows,
        until=rows[-1]["window_end"],
    )
    assert_readonly(outcome, recovery_driver)
    altered = deepcopy(outcome)
    actions = list(outcome.actions)
    if removed == "empty":
        actions = []
    elif removed.startswith("truncate_"):
        actions.remove(
            "persist_observation"
            if removed == "truncate_persist"
            else "read_only_query"
        )
    else:
        assert removed in actions
        actions = [action for action in actions if action != removed]
    altered.actions = tuple(actions)
    with pytest.raises(AssertionError):
        assert_readonly(altered, recovery_driver)
