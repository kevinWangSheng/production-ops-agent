"""Migration 0008: state CHECKs equal the domain Literals and the store's
vocabularies (no PG needed)."""

import importlib.util
import pathlib
from typing import get_args

from opspilot.domain.knowledge import POSTMORTEM, PostmortemState
from opspilot.knowledge import store

ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATION = (
    ROOT / "opspilot" / "migrations" / "versions" / "0008_postmortem_knowledge.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0008", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_version_states_equal_the_domain_literal() -> None:
    checks = _load_migration().CHECKS
    allowed = checks[("opspilot_postmortem_versions", "state")]
    assert len(set(allowed)) == len(allowed)
    assert set(allowed) == set(get_args(PostmortemState)) == POSTMORTEM.states


def test_store_and_migration_share_their_vocabularies() -> None:
    migration = _load_migration()
    assert migration.revision == "0008_postmortem_knowledge"
    assert migration.down_revision == "0007_ending_job_identity"
    assert set(migration.WORKER_ACTIONS) == store.WORKER_ACTIONS
    assert set(migration.STALE_REASONS) == set(get_args(store.StaleReason))
    assert set(migration.CERTAINTIES) == set(get_args(store.Certainty))
    assert set(migration.PRINCIPAL_KINDS) == set(get_args(store.PrincipalKind))
    assert set(store.WORKER_ACTIONS) < set(migration.ACTIONS)


def test_every_review_action_maps_to_a_domain_edge() -> None:
    for action, trigger in store._TRIGGERS.items():
        assert any(trigger in POSTMORTEM.triggers(s) for s in POSTMORTEM.states), action


def test_terminal_states_are_exactly_the_reviewed_or_superseded_ones() -> None:
    terminal = {s for s in POSTMORTEM.states if POSTMORTEM.terminal(s)}
    assert terminal == {"approved", "rejected", "returned", "stale"}


def test_refusals_map_to_integrity_refused_and_transients_keep_their_codes() -> None:
    from psycopg import errors

    code = store.KnowledgeStore._error_code
    for refused in (
        errors.CheckViolation,
        errors.ForeignKeyViolation,
        errors.NotNullViolation,
        errors.InsufficientPrivilege,
        errors.UniqueViolation,
    ):
        assert code(refused()) == "INTEGRITY_REFUSED", refused
    assert code(errors.SerializationFailure()) == "RETRY"
    assert code(errors.DeadlockDetected()) == "RETRY"
    assert code(errors.QueryCanceled()) == "TIMEOUT"
    assert code(errors.LockNotAvailable()) == "TIMEOUT"
