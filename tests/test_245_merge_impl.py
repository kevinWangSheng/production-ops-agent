"""#245 PR-2 unit checks: the page hint and the domain vocabulary; the PG
behaviour is in ``tests/integration/test_245_merge_impl_postgres.py``."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from opspilot.domain.control import ControlState, apply_control
from opspilot.web.service import CONTROL_ACTIONS, available_actions
from opspilot.web.store import IncidentSummary


def _snapshot(lifecycle="open", merged_into=None):
    return {
        "incident": IncidentSummary(
            uuid4(),
            "key",
            "queued",
            lifecycle,
            0,
            uuid4(),
            False,
            datetime.now(timezone.utc),
            merged_into_incident_id=merged_into,
        ),
        "run": {"state": "queued"},
        "rebind_targets": ["payment-prod"],
    }


@pytest.mark.parametrize(
    "lifecycle", ["open", "observing_recovery", "resolved", "closed"]
)
def test_merge_is_offered_until_the_incident_is_merged(lifecycle):
    assert "merge_into" in available_actions(_snapshot(lifecycle))


def test_a_merged_incident_offers_no_reopen_rebind_or_merge():
    actions = available_actions(_snapshot("closed", merged_into=uuid4()))
    assert not {"merge_into", "reopen", "rebind_target"} & set(actions)


def test_vocabulary():
    assert "merge_into" in CONTROL_ACTIONS
    after = apply_control(ControlState(observation_authorized=True), "merge_into", 0)
    assert (after.control_generation, after.observation_authorized) == (1, False)
