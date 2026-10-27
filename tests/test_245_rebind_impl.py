"""#245 PR-1 unit checks: the page hint, the domain vocabulary and the
successor's target test (G7); the PG behaviour is in
``tests/integration/test_245_rebind_impl_postgres.py``."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from opspilot.domain.control import ControlState, apply_control
from opspilot.web.service import CONTROL_ACTIONS, _bound_elsewhere, available_actions
from opspilot.web.store import IncidentSummary


def _snapshot(lifecycle="open", rebind_targets=("payment-prod",), handoff=None):
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
            handoff_reason=handoff,
        ),
        "run": {"state": "queued"},
        "rebind_targets": list(rebind_targets),
        "handoff_only": handoff is not None,
    }


@pytest.mark.parametrize(
    ("lifecycle", "offered"),
    [
        ("open", True),
        ("observing_recovery", True),
        ("resolved", False),
        ("closed", False),
    ],
)
def test_rebind_is_offered_on_open_lifecycles_only(lifecycle, offered):
    assert ("rebind_target" in available_actions(_snapshot(lifecycle))) is offered


def test_rebind_needs_another_registry_key():
    assert "rebind_target" not in available_actions(_snapshot(rebind_targets=()))
    assert "rebind_target" not in available_actions(
        {"incident": None, "handoff_only": True}
    )


def test_vocabulary():
    assert "rebind_target" in CONTROL_ACTIONS
    assert "merge_into" not in CONTROL_ACTIONS  # PR-2
    state = ControlState(observation_authorized=True)
    after = apply_control(state, "rebind_target", 0)
    assert (after.control_generation, after.observation_authorized) == (1, False)


def _run(bound, targets):
    return {
        "run": {
            "input": {
                "bound_target_id": bound,
                "scope_facts": {} if targets is None else {"target_ids": targets},
            }
        }
    }


@pytest.mark.parametrize(
    ("snapshot", "elsewhere"),
    [
        (_run("checkout-prod", ["checkout-prod"]), False),
        (_run("checkout-prod", ["payment-prod"]), True),
        (_run("payment-prod", ["checkout-prod"]), True),
        (_run(None, ["checkout-prod"]), False),
        (_run("checkout-prod", None), False),
        (_run(None, None), False),
        ({"run": {"input": None}}, False),
    ],
)
def test_bound_elsewhere(snapshot, elsewhere):
    assert _bound_elsewhere(snapshot, "checkout-prod") is elsewhere
