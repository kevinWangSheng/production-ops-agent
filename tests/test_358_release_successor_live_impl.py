"""#358: offline tests for the kind-window driver and collector
(``scripts/r358_live.py``). Inputs are built by the product's own faces and
``continuation_input``; no PostgreSQL, network, kind or model."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from opspilot.investigation.context import RELEASE_OBSERVATION, InvestigationInput
from opspilot.investigation.inputs import (
    ReleaseAnchor,
    continuation_input,
    release_observation_fact,
)
from opspilot.tools import Window
from opspilot.tools.otel_demo import METRICS_TOOL, TARGET_ID, otel_demo_face
from scripts import r358_live as live
from tests.m1_tool_support import FakeClock
from tests.test_f11_pr4b_impl import _ending, _release

RELEASED = datetime(2026, 11, 1, 20, 0, 3, 250000, tzinfo=timezone.utc)
CONSUMED = RELEASED + timedelta(minutes=13, seconds=7)
EPISODE = CONSUMED + timedelta(minutes=4, seconds=30)
CREATED = EPISODE + timedelta(seconds=9, microseconds=4321)
RUN_SECONDS = 7200.0
CONSUMPTION = ReleaseAnchor(released_at=RELEASED, consumed_at=CONSUMED)


def _first() -> InvestigationInput:
    fresh = otel_demo_face(FakeClock(start=CONSUMED)).input_for(
        run_id="run-1",
        question="q",
        target_id=TARGET_ID,
        deadline=CONSUMED + timedelta(seconds=RUN_SECONDS),
        model_requests=100,
        release=CONSUMPTION,
    )
    fact = release_observation_fact(_release(), _ending(), None, CONSUMPTION)
    return InvestigationInput(
        **{
            **fresh.__dict__,
            "scope_facts": {**fresh.scope_facts, RELEASE_OBSERVATION: fact},
        }
    )


def _successor(
    first: InvestigationInput, created: datetime = CREATED
) -> InvestigationInput:
    return continuation_input(
        {"run": {"run_id": "run-1", "input": first.as_json()}, "steps": []},
        new_run_id="run-2",
        deadline=created + timedelta(seconds=RUN_SECONDS),
        authorized_targets=frozenset({TARGET_ID}),
        release=ReleaseAnchor(
            released_at=RELEASED, consumed_at=CONSUMED, frame_end=created
        ),
    )


def _run(
    run_id: str, run_input: InvestigationInput, deadline: datetime
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "state": "completed",
        "deadline": deadline,
        "input": json.loads(json.dumps(run_input.as_json())),
        "versions": {"prompt_revision": "p"},
        "budget_spent": 5,
        "tool_operations_used": 3,
    }


def _state(successor_created: datetime = CREATED) -> dict[str, Any]:
    first = _first()
    return {
        "incident": {"incident_id": "inc-1", "control_generation": 2},
        "link": {
            "observation_id": "obs-1",
            "run_id": "run-1",
            "consumed_at": CONSUMED,
            "released_at": RELEASED,
        },
        "runs": [
            _run("run-1", first, CONSUMED + timedelta(seconds=RUN_SECONDS)),
            _run(
                "run-2",
                _successor(first, successor_created),
                successor_created + timedelta(seconds=RUN_SECONDS),
            ),
        ],
        "deliveries": [
            {
                "delivery_id": 7,
                "delivery_key": "k1",
                "status": "firing",
                "outcome": "attached",
                "received_at": EPISODE,
                "starts_at": EPISODE - timedelta(minutes=2),
            },
            {
                "delivery_id": 8,
                "delivery_key": "k1",
                "status": "firing",
                "outcome": "attached",
                "received_at": EPISODE + timedelta(minutes=5),
                "starts_at": EPISODE - timedelta(minutes=2),
            },
        ],
        "controls": [],
    }


def _step(*views: dict[str, Any]) -> dict[str, Any]:
    return {
        "sequence": 1,
        "tool_results": [{"ordinal": i, "result": v} for i, v in enumerate(views)],
    }


def _view(
    start: datetime, end: datetime, status: str = "ok", reason: str | None = None
):
    return {
        "tool": METRICS_TOOL,
        "status": status,
        "reason": reason,
        "window": {"start": start.isoformat(), "end": end.isoformat()},
    }


def test_box_assertions_hold_for_the_products_successor() -> None:
    state = _state()
    first, successor = state["runs"]
    box = live.box_assertions(first, successor, RELEASED, CONSUMED, RUN_SECONDS)
    assert box["ok"], box["checks"]
    end = CREATED.replace(microsecond=0)
    assert box["successor_box"]["end"] == end.isoformat()
    assert box["successor_anchor"] == RELEASED.replace(microsecond=0).isoformat()
    assert box["first_box"]["end"] == CONSUMED.replace(microsecond=0).isoformat()


def test_box_assertions_catch_a_wrong_wall_or_an_unmoved_box() -> None:
    state = _state()
    first, successor = state["runs"]
    wrong_wall = live.box_assertions(
        first, successor, RELEASED, CONSUMED, RUN_SECONDS - 60
    )
    assert not wrong_wall["checks"]["successor_box_end_is_creation"]
    unmoved = live.box_assertions(
        first, {**successor, "input": first["input"]}, RELEASED, CONSUMED, RUN_SECONDS
    )
    assert not unmoved["ok"]


def test_episode_is_the_first_attached_firing_after_the_box() -> None:
    deliveries = _state()["deliveries"]
    assert live.first_attached_episode(deliveries, CONSUMED)["delivery_id"] == 7
    assert live.first_attached_episode(deliveries, EPISODE + timedelta(hours=1)) is None
    resolved = [{**d, "status": "resolved"} for d in deliveries]
    assert live.first_attached_episode(resolved, CONSUMED) is None


def test_offline_gateway_refuses_on_the_first_run_and_admits_on_the_successor() -> None:
    state = _state()
    first, successor = state["runs"]
    probe = Window(EPISODE - timedelta(minutes=5), EPISODE + timedelta(seconds=1))
    refused = live.gateway_judgment(first["input"], probe)
    assert (refused["status"], refused["reason"], refused["requests_built"]) == (
        "denied",
        "WINDOW_OUT_OF_SCOPE",
        0,
    )
    admitted = live.gateway_judgment(successor["input"], probe)
    assert (
        admitted["status"] != "denied" and admitted["reason"] != "WINDOW_OUT_OF_SCOPE"
    )
    assert admitted["requests_built"] == 1
    assert admitted["network"] is False and admitted["model"] is False


def test_summarize_records_observed_or_not_observed() -> None:
    state = _state()
    first_end = CONSUMED.replace(microsecond=0)
    covering = _view(EPISODE - timedelta(minutes=10), EPISODE + timedelta(seconds=5))
    early = _view(RELEASED - timedelta(hours=1), first_end)
    summary = live.summarize(
        state, [_step(early)], [_step(early, covering)], RUN_SECONDS
    )
    assert summary["box"]["ok"]
    assert summary["successor_episode_queries"]["observed"] == "observed"
    assert len(summary["successor_episode_queries"]["ok"]) == 1
    assert summary["offline_gateway_judgment"]["first_run_refused"] is True
    assert summary["offline_gateway_judgment"]["successor_admitted"] is True
    assert summary["episode"]["after_first_box_end"] is True
    quiet = live.summarize(state, [], [_step(early)], RUN_SECONDS)
    assert quiet["successor_episode_queries"]["observed"] == "未观察到"
    assert quiet["offline_gateway_judgment"]["probe_window_source"].startswith(
        "synthetic"
    )
    assert quiet["offline_gateway_judgment"]["first_run_refused"] is True


def test_gateway_records_find_views_in_nested_or_string_results() -> None:
    view = _view(
        EPISODE, EPISODE + timedelta(seconds=1), "denied", "WINDOW_OUT_OF_SCOPE"
    )
    steps = [
        {"sequence": 3, "tool_results": [{"ordinal": 0, "result": json.dumps(view)}]}
    ]
    assert live.gateway_records(steps) == [
        {
            "sequence": 3,
            "tool": METRICS_TOOL,
            "status": "denied",
            "reason": "WINDOW_OUT_OF_SCOPE",
            "window": view["window"],
        }
    ]


class _Conn:
    def __init__(self, states: list[dict[str, Any]]) -> None:
        self.states = states

    def __enter__(self) -> _Conn:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


def _drive_args(**kw: Any) -> Any:
    import argparse

    base = dict(
        batch="358-test",
        observation="obs-1",
        silence_id="sil-1",
        alertmanager="http://am",
        round="1",
        incident_wait=5.0,
        attach_wait=5.0,
        every=0.0,
    )
    return argparse.Namespace(**{**base, **kw})


def _wire(
    monkeypatch: pytest.MonkeyPatch, states: list[dict[str, Any]], calls: list[Any]
) -> None:
    seq = iter(states)
    last: dict[str, Any] = {}

    def state(conn: Any, incident_id: str) -> dict[str, Any]:
        nonlocal last
        last = next(seq, last)
        return last

    monkeypatch.setattr(live.lab, "page", lambda obs: {"incident_id": "inc-1"})
    monkeypatch.setattr(live, "connect", lambda: _Conn(states))
    monkeypatch.setattr(live, "incident_state", state)
    monkeypatch.setattr(
        live, "append", lambda batch, record: calls.append(("ledger", record["kind"]))
    )

    def delete(batch: str, base: str, sid: str) -> int:
        calls.append(("silence_delete", sid))
        return 200

    monkeypatch.setattr(live, "silence_delete", delete)

    def http(method: str, path: str, form: dict[str, str] | None = None, **_: Any):
        assert form is not None
        calls.append((form["action"], int(form["expected_generation"])))
        gen = int(form["expected_generation"]) + 1
        body = {
            "generation": gen,
            **({"run_id": "run-2"} if form["action"] == "new_run" else {}),
        }
        return 200, json.dumps(body).encode(), {}

    monkeypatch.setattr(live.lab, "http", http)


def test_drive_deletes_the_silence_then_cancels_and_renews(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    full = _state()
    no_run = {**full, "runs": [], "deliveries": []}
    one_run = {**full, "runs": full["runs"][:1], "deliveries": []}
    attached = {**full, "runs": full["runs"][:1]}
    calls: list[Any] = []
    _wire(monkeypatch, [no_run, one_run, one_run, attached], calls)
    assert live.drive(_drive_args()) == 0
    actions = [c for c in calls if c[0] != "ledger"]
    assert actions == [("silence_delete", "sil-1"), ("cancel", 2), ("new_run", 3)]


def test_drive_records_an_environment_timeout_without_attach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    full = _state()
    one_run = {**full, "runs": full["runs"][:1], "deliveries": []}
    calls: list[Any] = []
    _wire(monkeypatch, [one_run], calls)
    assert live.drive(_drive_args(attach_wait=0.05)) == live.ENVIRONMENT_TIMEOUT
    assert ("ledger", "environment_timeout") in calls
    assert not any(c[0] in ("cancel", "new_run") for c in calls)


def test_silence_body_targets_only_the_checkout_rule() -> None:
    at = datetime(2026, 11, 1, tzinfo=timezone.utc)
    body = live.silence_body(2, at)
    assert body["matchers"] == [
        {
            "name": "alertname",
            "value": "CheckoutPlaceOrderErrorRatioHigh",
            "isRegex": False,
        }
    ]
    assert body["endsAt"] == (at + timedelta(hours=2)).isoformat()


def test_plan_makes_no_external_call(capsys: pytest.CaptureFixture[str]) -> None:
    assert live.main(["plan"]) == 0
    assert "silence-create" in capsys.readouterr().out
