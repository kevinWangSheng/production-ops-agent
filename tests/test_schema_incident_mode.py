"""Migration 0005: the mode CHECK equals the domain Literal (no PG needed)."""

import importlib.util
import pathlib
from typing import get_args

from opspilot.domain.control import ObservationMode

ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "opspilot" / "migrations" / "versions" / "0005_incident_mode.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0005", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_mode_check_equals_the_domain_literal() -> None:
    module = _load_migration()
    assert set(module.CHECKS[("opspilot_incidents", "mode")]) == set(
        get_args(ObservationMode)
    )
    assert module.revision == "0005_incident_mode"
    assert module.down_revision == "0004_target_identity"
