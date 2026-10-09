"""M1-03 D26: the frozen interface agrees with the store, the domain and 0008."""

import importlib
from typing import get_args
from uuid import uuid4

from opspilot.domain.knowledge import PostmortemState
from opspilot.knowledge import contract, store

MIGRATION = importlib.import_module(
    "opspilot.migrations.versions.0008_postmortem_knowledge"
)


def test_enumerations_match_store_domain_and_migration() -> None:
    assert get_args(contract.VersionState) == get_args(PostmortemState)
    assert (
        get_args(contract.VersionState)
        == MIGRATION.CHECKS[("opspilot_postmortem_versions", "state")]
    )
    assert get_args(contract.StaleReason) == get_args(store.StaleReason)
    assert get_args(contract.StaleReason) == MIGRATION.STALE_REASONS
    assert get_args(contract.Certainty) == MIGRATION.CERTAINTIES
    assert get_args(contract.AuditAction) == MIGRATION.ACTIONS
    assert get_args(contract.PrincipalKind) == MIGRATION.PRINCIPAL_KINDS


def test_event_payload_keys_cover_every_event_kind() -> None:
    assert set(contract.EVENT_PAYLOAD_KEYS) == set(get_args(contract.EventKind))


def test_review_command_problems_per_action() -> None:
    pm, entry = uuid4(), uuid4()
    ok = {
        "approve": contract.ReviewCommand(
            "approve", "k", 2, postmortem_id=pm, version=1
        ),
        "supersede": contract.ReviewCommand(
            "supersede",
            "k",
            2,
            postmortem_id=pm,
            version=1,
            entry_generations={entry: 1},
        ),
        "reject": contract.ReviewCommand(
            "reject", "k", 2, postmortem_id=pm, version=1, reason="no"
        ),
        "return": contract.ReviewCommand(
            "return", "k", 2, postmortem_id=pm, version=1, reason="fix"
        ),
        "revoke": contract.ReviewCommand(
            "revoke", "k", 1, entry_id=entry, revision=1, reason="wrong"
        ),
    }
    assert set(ok) == set(contract.REVIEW_ACTIONS)
    for command in ok.values():
        assert command.problems() == ()
    assert (
        "entry_generations"
        in contract.ReviewCommand(
            "supersede", "k", 2, postmortem_id=pm, version=1
        ).problems()
    )
    assert (
        "reason"
        in contract.ReviewCommand(
            "return", "k", 2, postmortem_id=pm, version=1
        ).problems()
    )
    assert (
        "reason"
        in contract.ReviewCommand(
            "approve", "k", 2, postmortem_id=pm, version=1, reason="x"
        ).problems()
    )
    assert (
        "postmortem_id"
        in contract.ReviewCommand(
            "revoke", "k", 1, entry_id=entry, revision=1, reason="r", postmortem_id=pm
        ).problems()
    )
    assert contract.ReviewCommand("publish", "k", 0).problems() == ("action",)  # type: ignore[arg-type]
