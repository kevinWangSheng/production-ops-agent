"""Migration 0003: CHECK values are the domain Literals; the Observer grant is
the documented minimum (no PG needed)."""

import importlib.util
import pathlib
from typing import get_args

from opspilot.domain.observation import (
    ObservationPurpose,
    ObservationSessionState,
    SampleOutcome,
    SampleReason,
)
from opspilot.domain.subjects import IncidentLifecycle

ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "opspilot" / "migrations" / "versions" / "0003_observation_store.py"

EXPECTED = {
    ("opspilot_observation_sessions", "purpose"): ObservationPurpose,
    ("opspilot_observation_sessions", "state"): ObservationSessionState,
    ("opspilot_observation_samples", "outcome"): SampleOutcome,
    ("opspilot_observation_samples", "reason"): SampleReason,
    ("opspilot_observation_samples", "subject_lifecycle"): IncidentLifecycle,
}


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0003", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_check_values_equal_the_domain_literals() -> None:
    checks = _load_migration().CHECKS
    assert set(checks) == set(EXPECTED)
    for column, literal in EXPECTED.items():
        allowed = checks[column]
        assert len(set(allowed)) == len(allowed), f"{column}: duplicate values"
        assert set(allowed) == set(get_args(literal)), column


def test_revision_chain() -> None:
    module = _load_migration()
    assert module.revision == "0003_observation_store"
    assert module.down_revision == "0002_state_checks"


def test_observer_may_not_touch_session_parameters_or_investigation_columns() -> None:
    """The Observer updates the watermark, the job slot and the state -- never
    what was fixed at authorization -- and reads only identity/lifecycle
    columns of an incident (no ``conclusion``, no ``current_run_id``)."""
    module = _load_migration()
    fixed_at_authorization = {
        "session_id",
        "incident_id",
        "purpose",
        "target_id",
        "target",
        "subject_control_generation",
        "observation_generation",
        "authorized",
        "authorized_by",
        "health_profile_revision",
        "deadline_at",
        "max_samples",
        "sample_interval_seconds",
        "sustained_window_seconds",
        "created_at",
    }
    assert not fixed_at_authorization & set(module.OBSERVER_SESSION_COLUMNS)
    assert set(module.OBSERVER_INCIDENT_COLUMNS) == {
        "incident_id",
        "lifecycle",
        "control_generation",
        "observation_generation",
        "target_id",
    }
    assert module.OBSERVER_ROLE == "opspilot_observer"
