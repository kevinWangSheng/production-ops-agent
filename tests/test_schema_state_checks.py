"""The CHECK values in migration 0002 are the domain Literals, exactly (no PG needed)."""

import importlib.util
import pathlib
from typing import get_args

from opspilot.domain.runs import RunExecution
from opspilot.domain.subjects import IncidentLifecycle

ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "opspilot" / "migrations" / "versions" / "0002_state_checks.py"

# Every constrained column and the Literal it must equal. Adding a column to
# the migration without adding it here fails the test, and vice versa.
EXPECTED = {
    ("opspilot_runs", "state"): RunExecution,
    ("opspilot_incidents", "lifecycle"): IncidentLifecycle,
}


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0002", MIGRATION)
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
    assert module.revision == "0002_state_checks"
    assert module.down_revision == "0001_baseline"
