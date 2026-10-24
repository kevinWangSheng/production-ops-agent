"""Implementer unit tests for #305: re-framing a successor after an attached
episode (contract r1 W1-B, R1, R2, R7). PostgreSQL paths are in
``tests/integration/test_305_successor_window_impl_postgres.py``; the
independent acceptance tests live elsewhere.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from opspilot.investigation.context import (
    KNOWN_INPUT_VERSIONS,
    ContextError,
    InvestigationInput,
)
from opspilot.investigation.inputs import AlertAnchor, continuation_input
from opspilot.tools import Window
from opspilot.tools.fixture import fixture_versions
from opspilot.tools.otel_demo import (
    otel_demo_face,
    otel_demo_versions,
    scope_default_query_window,
    scope_window,
)
from opspilot.web.service import _attached_anchor
from tests.m1_tool_support import FakeClock

RECEIVED = datetime(2026, 10, 10, 12, 0, 30, 987654, tzinfo=timezone.utc)
FRAME_END = RECEIVED.replace(microsecond=0)
DEADLINE = FRAME_END + timedelta(minutes=10)
CREATED = datetime(2026, 10, 10, 14, 5, 7, 123456, tzinfo=timezone.utc)
EPISODE = datetime(2026, 10, 10, 13, 58, 0, tzinfo=timezone.utc)


def _face(run_id="run-1", *, alert=None, question="Why is checkout erroring?"):
    face = otel_demo_face(FakeClock(start=RECEIVED + timedelta(hours=3)))
    return face.input_for(
        run_id=run_id,
        question=question,
        target_id="checkout-prod",
        deadline=DEADLINE,
        model_requests=4,
        alert=alert,
    )


def _first():
    return _face(
        alert=AlertAnchor(
            received_at=RECEIVED,
            starts_at=FRAME_END - timedelta(hours=2),
            original="2026-10-10T10:00:30Z",
        )
    )


def _snapshot(previous: InvestigationInput):
    return {"run": {"run_id": "run-1", "input": previous.as_json()}, "steps": []}


def _moved():
    return AlertAnchor(
        received_at=EPISODE + timedelta(minutes=1),
        starts_at=EPISODE,
        original="2026-10-10T13:58:00.000Z",
        frame_end=CREATED,
    )


def _row(key, received, *, outcome="attached", status="firing", starts=EPISODE):
    return {
        "delivery_key": key,
        "outcome": outcome,
        "status": status,
        "received_at": received,
        "starts_at": starts,
        "starts_at_raw": starts.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def test_frame_end_moves_the_frame_and_keeps_the_anchor_rule():
    anchor = _moved()
    end = CREATED.replace(microsecond=0)
    assert anchor.frame == (end - timedelta(hours=24), end)
    assert anchor.anchor == (EPISODE, None)
    assert anchor.default_query_window == (EPISODE - timedelta(hours=1), end)
    assert anchor.scope_fact() == {
        "anchor": EPISODE.isoformat(),
        "original": "2026-10-10T13:58:00.000Z",
        "adjusted": None,
    }
    # Without ``frame_end`` the frame is the receipt's, as before (J1).
    plain = AlertAnchor(received_at=RECEIVED, starts_at=EPISODE, original="x")
    assert plain.frame[1] == FRAME_END
    with pytest.raises(ContextError):
        AlertAnchor(
            received_at=RECEIVED,
            starts_at=EPISODE,
            original="x",
            frame_end=CREATED.replace(tzinfo=None),
        )


def test_continuation_with_an_attached_anchor_moves_only_the_frame_facts():
    previous = _first()
    later = CREATED + timedelta(minutes=10)
    kept = continuation_input(
        _snapshot(previous),
        new_run_id="run-2",
        deadline=later,
        authorized_targets=frozenset({"checkout-prod"}),
    )
    moved = continuation_input(
        _snapshot(previous),
        new_run_id="run-2",
        deadline=later,
        authorized_targets=frozenset({"checkout-prod"}),
        alert=_moved(),
    )
    # R2: the same version, keys and question; only the first policy's
    # window, anchor and default query window and ``alert_starts_at`` move.
    assert moved.version == kept.version == previous.version
    assert moved.question == kept.question
    assert set(moved.scope_facts) == set(kept.scope_facts)
    for key, value in kept.scope_facts.items():
        if key != "alert_starts_at":
            assert moved.scope_facts[key] == value, key
    assert moved.scope_facts["alert_starts_at"] == _moved().scope_fact()
    old_policy = kept.evidence_context["time_policies"][0]
    new_policy = moved.evidence_context["time_policies"][0]
    assert set(new_policy) == set(old_policy)
    for key in old_policy:
        if key not in ("window", "anchor", "default_query_window"):
            assert new_policy[key] == old_policy[key], key
    end = CREATED.replace(microsecond=0)
    assert scope_window(moved) == Window(end - timedelta(hours=24), end)
    assert scope_default_query_window(moved) == Window(
        EPISODE - timedelta(hours=1), end
    )
    for key in kept.evidence_context:
        if key != "time_policies":
            assert moved.evidence_context[key] == kept.evidence_context[key], key
    assert InvestigationInput.from_json(moved.as_json()) == moved


def test_only_an_alert_run_can_be_reframed():
    submitted = _face()
    with pytest.raises(ContextError) as caught:
        continuation_input(
            _snapshot(submitted),
            new_run_id="run-2",
            deadline=CREATED,
            authorized_targets=frozenset({"checkout-prod"}),
            alert=_moved(),
        )
    assert caught.value.code == "INPUT_INVALID"


def test_the_latest_first_attached_delivery_after_the_frame_is_taken():
    later = EPISODE + timedelta(minutes=30)
    rows = [
        _row("a", RECEIVED + timedelta(minutes=5)),
        _row("b", RECEIVED + timedelta(minutes=50), starts=later),
        # A repeat of ``a`` after ``b`` is no new event (R-C3).
        _row("a", RECEIVED + timedelta(minutes=55)),
        _row(
            "b",
            RECEIVED + timedelta(minutes=56),
            status="resolved",
            outcome="resolved_attached",
        ),
        _row(
            "first",
            RECEIVED + timedelta(minutes=57),
            outcome="replayed",
            starts=FRAME_END,
        ),
    ]
    anchor = _attached_anchor(rows, after=FRAME_END, created=CREATED)
    assert anchor is not None
    assert anchor.starts_at == later and anchor.frame_end == CREATED
    assert anchor.original == later.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.mark.parametrize(
    "rows",
    [
        [],
        # Received before (or at) the previous frame's end: already covered.
        [_row("a", FRAME_END - timedelta(seconds=1)), _row("b", FRAME_END)],
        # Only a repeat of an earlier episode arrives after the frame.
        [
            _row("a", FRAME_END - timedelta(seconds=1)),
            _row("a", FRAME_END + timedelta(minutes=5)),
        ],
        # Replays and resolved notifications are no attached episode.
        [
            _row("x", FRAME_END + timedelta(minutes=5), outcome="replayed"),
            _row(
                "y",
                FRAME_END + timedelta(minutes=5),
                status="resolved",
                outcome="resolved_attached",
            ),
        ],
    ],
    ids=["none", "before-frame-end", "repeat-only", "replay-and-resolved"],
)
def test_no_attached_episode_after_the_frame_keeps_it(rows):
    assert _attached_anchor(rows, after=FRAME_END, created=CREATED) is None


def test_revisions_are_unchanged():
    """R2, R7, C3 §5: #305 changes no input version, tool schema or prompt
    revision (values as on main 1904345)."""
    assert KNOWN_INPUT_VERSIONS[-1] == "opspilot-investigation-input-v6"
    assert len(KNOWN_INPUT_VERSIONS) == 6
    common = {
        "prompt_revision": "prompt-replay-candidate-bef70649a3a8",
        "context_policy_revision": "ctx-ctx-policy-v1-a30e65ea52f4",
    }
    assert otel_demo_versions() == {
        **common,
        "tool_schema_revision": "otel-demo-68ed2ca8f5d6",
    }
    assert fixture_versions() == {**common, "tool_schema_revision": "fixture-4"}
