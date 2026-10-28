"""Implementer unit tests for #347: a release-anomaly Run's successor moves
its frame after an attached alert episode (#305 W1-B rule, anchor kept at
``released_at``, V2-A). PostgreSQL paths are in
``tests/integration/test_347_release_successor_reframe_impl_postgres.py``;
the independent acceptance tests live elsewhere.
"""

from __future__ import annotations

import io
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from opspilot.investigation.context import (
    INPUT_VERSION_RELEASE,
    RELEASE_OBSERVATION,
    ContextError,
    InvestigationInput,
)
from opspilot.investigation.inputs import (
    RELEASE_ANCHOR_RULE,
    ReleaseAnchor,
    continuation_input,
    release_observation_fact,
)
from opspilot.persistence import Lease
from opspilot.tools import ToolRequest, Window
from opspilot.tools.fixture import fixture_face
from opspilot.tools.otel_demo import (
    METRICS_TOOL,
    TARGET_ID,
    OtelDemoConfig,
    otel_demo_executor_factory,
    otel_demo_face,
    scope_window,
)
from tests.m1_tool_support import FakeClock, RecordingSink
from tests.test_f11_pr4b_impl import _ending, _release

RELEASED = datetime(2026, 10, 22, 10, 0, 0, 500000, tzinfo=timezone.utc)
CONSUMED = RELEASED + timedelta(minutes=7)
CREATED = CONSUMED + timedelta(hours=3, seconds=12, microseconds=345)
CONSUMPTION = ReleaseAnchor(released_at=RELEASED, consumed_at=CONSUMED)


def _first() -> InvestigationInput:
    fresh = fixture_face().input_for(
        run_id="run-1",
        question="q",
        target_id="uid",
        deadline=CONSUMED + timedelta(minutes=10),
        model_requests=3,
        release=CONSUMPTION,
    )
    fact = release_observation_fact(_release(), _ending(), None, CONSUMPTION)
    return InvestigationInput(
        **{
            **fresh.__dict__,
            "scope_facts": {**fresh.scope_facts, RELEASE_OBSERVATION: fact},
        }
    )


def _snapshot(previous: InvestigationInput):
    return {"run": {"run_id": "run-1", "input": previous.as_json()}, "steps": []}


def _continue(previous: InvestigationInput, **kwargs):
    return continuation_input(
        _snapshot(previous),
        new_run_id="run-2",
        deadline=CREATED + timedelta(minutes=10),
        authorized_targets=frozenset({"uid"}),
        **kwargs,
    )


def test_frame_end_moves_the_release_frame_and_keeps_the_released_at_anchor():
    moved = ReleaseAnchor(released_at=RELEASED, consumed_at=CONSUMED, frame_end=CREATED)
    end = CREATED.replace(microsecond=0)
    released = RELEASED.replace(microsecond=0)
    assert moved.frame == (end - timedelta(hours=24), end)
    assert moved.anchor == (released, None)
    assert moved.default_query_window == (released - timedelta(hours=1), end)
    # The consumption anchor (the one the release fact records) is untouched.
    assert CONSUMPTION.frame[1] == CONSUMED.replace(microsecond=0)
    with pytest.raises(ContextError):
        ReleaseAnchor(
            released_at=RELEASED,
            consumed_at=CONSUMED,
            frame_end=CREATED.replace(tzinfo=None),
        )


def test_a_successor_after_released_at_plus_24h_clamps_the_anchor_to_the_frame():
    late = RELEASED + timedelta(hours=30)
    moved = ReleaseAnchor(released_at=RELEASED, consumed_at=CONSUMED, frame_end=late)
    start, end = moved.frame
    assert moved.anchor == (start, "before_frame")
    assert moved.default_query_window == (start, end)


def test_continuation_with_a_release_anchor_moves_only_the_first_policy_frame():
    previous = _first()
    kept = _continue(previous)
    moved = _continue(
        previous,
        release=ReleaseAnchor(
            released_at=RELEASED, consumed_at=CONSUMED, frame_end=CREATED
        ),
    )
    assert moved.version == kept.version == previous.version == INPUT_VERSION_RELEASE
    assert moved.question == kept.question
    # The release fact (consumption anchor) and every other scope fact stay.
    assert moved.scope_facts == kept.scope_facts
    assert (
        moved.scope_facts[RELEASE_OBSERVATION]
        == (previous.scope_facts[RELEASE_OBSERVATION])
    )
    old = kept.evidence_context["time_policies"]
    new = moved.evidence_context["time_policies"]
    assert new[1:] == old[1:]
    assert set(new[0]) == set(old[0])
    for key in old[0]:
        if key not in ("window", "anchor", "default_query_window"):
            assert new[0][key] == old[0][key], key
    end = CREATED.replace(microsecond=0)
    released = RELEASED.replace(microsecond=0)
    assert new[0]["window"] == {
        "start": (end - timedelta(hours=24)).isoformat(),
        "end": end.isoformat(),
    }
    assert new[0]["anchor"] == old[0]["anchor"] == released.isoformat()
    assert new[0]["anchor_rule"] == RELEASE_ANCHOR_RULE
    assert new[0]["default_query_window"] == {
        "start": (released - timedelta(hours=1)).isoformat(),
        "end": end.isoformat(),
    }
    InvestigationInput.from_json(moved.as_json())


def test_only_a_release_run_takes_a_release_anchor():
    plain = fixture_face().input_for(
        run_id="run-1",
        question="q",
        target_id="uid",
        deadline=CONSUMED + timedelta(minutes=10),
        model_requests=3,
    )
    with pytest.raises(ContextError) as caught:
        _continue(
            plain,
            release=ReleaseAnchor(
                released_at=RELEASED, consumed_at=CONSUMED, frame_end=CREATED
            ),
        )
    assert caught.value.code == "INPUT_INVALID"


# -- the gateway over the two Runs' inputs ------------------------------------

EPISODE = CREATED - timedelta(minutes=10)
EMPTY_BODY = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "otel_demo"
    / "prometheus-range-empty.json"
).read_bytes()


class _Reply(io.BytesIO):
    status = 200


class _Opener:
    def __init__(self) -> None:
        self.requests: list[Any] = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        return _Reply(EMPTY_BODY)


class _RunStore:
    """What the factory's wiring reads for one Run: its deadline, the tool
    charge and an unchanged human control state."""

    def __init__(self, lease: Lease, deadline: datetime) -> None:
        self.lease, self.deadline = lease, deadline

    def rebuild(self, incident_id):
        return {
            "run": {
                "run_id": self.lease.run_id,
                "deadline": self.deadline,
                "tool_operations_used": 0,
                "tool_seconds_used": 0.0,
            }
        }

    def charge_tool(self, lease, operation_id, seconds, **_):
        return None

    def control_state(self, incident_id):
        return {
            "incident_generation": self.lease.control_generation,
            "incident_state": "running",
            "global_suspended": False,
            "global_generation": 0,
            "target_suspended": False,
            "target_generation": 0,
        }


def _otel_first() -> InvestigationInput:
    fresh = otel_demo_face(FakeClock(start=CONSUMED)).input_for(
        run_id="run-1",
        question="q",
        target_id=TARGET_ID,
        deadline=CONSUMED + timedelta(minutes=10),
        model_requests=3,
        release=CONSUMPTION,
    )
    fact = release_observation_fact(_release(), _ending(), None, CONSUMPTION)
    return InvestigationInput(
        **{
            **fresh.__dict__,
            "scope_facts": {**fresh.scope_facts, RELEASE_OBSERVATION: fact},
        }
    )


def _query(monkeypatch, run_input: InvestigationInput, deadline: datetime, window):
    opener = _Opener()
    monkeypatch.setattr(urllib.request, "build_opener", lambda *h: opener)
    lease = Lease(
        incident_id=uuid4(),
        run_id=uuid4(),
        owner=uuid4(),
        epoch=1,
        control_generation=1,
        global_suspension_generation=0,
        target_suspension_generation=0,
    )
    frame = scope_window(run_input)
    factory = otel_demo_executor_factory(
        _RunStore(lease, deadline),
        evidence=RecordingSink(),
        clock=FakeClock(start=frame.end + timedelta(seconds=1)),
        config=OtelDemoConfig(
            prometheus_url="http://127.0.0.1:19090",
            jaeger_url="http://127.0.0.1:16686/jaeger/ui",
        ),
    )
    outcome = factory(lease, run_input).execute(
        ToolRequest(
            step_id="step-1",
            tool_index=0,
            tool_name=METRICS_TOOL,
            target_ref=TARGET_ID,
            params={"expr": "sum(up)"},
            window=window.as_json(),
        )
    )
    return outcome, opener.requests


def test_the_gateway_takes_the_attached_episode_on_the_successor_only(monkeypatch):
    first = _otel_first()
    successor_deadline = CREATED + timedelta(minutes=10)
    successor = continuation_input(
        _snapshot(first),
        new_run_id="run-2",
        deadline=successor_deadline,
        authorized_targets=frozenset({TARGET_ID}),
        release=ReleaseAnchor(
            released_at=RELEASED, consumed_at=CONSUMED, frame_end=CREATED
        ),
    )
    # An interval over the attached episode, after the consumption.
    query = Window(EPISODE - timedelta(minutes=5), EPISODE + timedelta(seconds=1))
    assert query.start > scope_window(first).end

    refused, sent = _query(monkeypatch, first, CONSUMED + timedelta(minutes=10), query)
    assert (refused.status, refused.reason) == ("denied", "WINDOW_OUT_OF_SCOPE")
    assert sent == []

    accepted, sent = _query(monkeypatch, successor, successor_deadline, query)
    assert accepted.status != "denied", accepted
    assert accepted.reason != "WINDOW_OUT_OF_SCOPE", accepted
    assert len(sent) == 1
