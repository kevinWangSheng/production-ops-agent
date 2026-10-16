"""Snapshot-only hints for Human control; persistence remains authoritative."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from opspilot.web.service import available_actions
from opspilot.web.store import IncidentSummary


def _snapshot(
    lifecycle="open",
    mode="automatic",
    state="queued",
    run_state="queued",
    concluded=False,
    profile="p1",
):
    return {
        "incident": IncidentSummary(
            uuid4(),
            "key",
            state,
            lifecycle,
            0,
            uuid4(),
            concluded,
            datetime.now(timezone.utc),
            mode=mode,
        ),
        "run": {"state": run_state},
        "health_profile_revision": profile,
    }


@pytest.mark.parametrize(
    "lifecycle,close,reopen",
    [
        ("open", True, False),
        ("observing_recovery", True, False),
        ("resolved", True, True),
        ("closed", False, True),
    ],
)
@pytest.mark.parametrize("mode", ["automatic", "human_owned"])
def test_lifecycle_actions(lifecycle, close, reopen, mode):
    snapshot = _snapshot(lifecycle, mode, "cancelled", "cancelled")
    actions = available_actions(snapshot)
    assert ("close" in actions) is close
    assert ("reopen" in actions) is reopen
    assert "takeover" in actions
    if lifecycle == "closed":
        assert not {
            "new_run",
            "register_remediation",
            "pause",
            "resume",
            "cancel",
        } & set(actions)
        assert ("follow_up" in actions) is (mode == "human_owned")
        assert ("correct" in actions) is (mode == "human_owned")
    # Registration starts an observation: only open/observing_recovery take it.
    assert ("register_remediation" in actions) is (
        lifecycle in {"open", "observing_recovery"}
    )


def test_mirrors_and_profile_filter_only_known_refusals():
    assert "new_run" in available_actions(_snapshot(state="cancelled"))
    assert "new_run" not in available_actions(
        _snapshot(state="cancelled", mode="human_owned")
    )
    assert "register_remediation" not in available_actions(_snapshot(profile=None))
    actions = available_actions(_snapshot(state="paused", run_state="paused"))
    assert "pause" not in actions
    assert {"resume", "cancel", "follow_up", "correct"} <= set(actions)
    actions = available_actions(
        _snapshot("resolved", concluded=True, run_state="completed")
    )
    assert not {"pause", "resume", "cancel", "follow_up", "correct"} & set(actions)
    assert available_actions({"handoff_only": True}) == []


@pytest.mark.parametrize("action", ["close", "reopen"])
@pytest.mark.parametrize(
    "extra,error",
    [
        ({"text": "note"}, "TEXT_NOT_ALLOWED"),
        ({"revision": "r1"}, "REVISION_NOT_ALLOWED"),
    ],
)
def test_non_text_actions_reject_text_and_revision(action, extra, error):
    from opspilot.web.service import WorkbenchError
    from tests.m1_web_support import build_workbench

    _, workbench, _ = build_workbench()
    with pytest.raises(WorkbenchError, match=error):
        workbench.control(
            uuid4(),
            actor_id="operator",
            action=action,
            expected_generation=0,
            idempotency_key=str(uuid4()),
            **extra,
        )
