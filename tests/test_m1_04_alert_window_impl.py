"""Implementer tests for M1-04 step 3: an alert Run's frame and anchor.

Contract r5 J1-J6 (``docs/tasks/2026-10-10-m1-04-alert-intake.md``). The
independent acceptance tests live elsewhere; these pin the pieces the
implementation is built from: the frame and clamp arithmetic, the v3 input,
the projection the model and continuation read, and the default query
window the transport falls back to.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from opspilot.investigation import context as context_module
from opspilot.investigation.context import (
    ContextError,
    InvestigationInput,
    initial_messages,
)
from opspilot.investigation.inputs import (
    ALERT_ANCHOR_RULE,
    AlertAnchor,
    continuation_input,
)
from opspilot.investigation.reports import evidence_context_projection
from opspilot.tools import QueryScope, ToolContractError, TransportRequest, Window
from opspilot.tools.fixture import (
    WINDOW_END,
    WINDOW_START,
    fixture_face,
)
from opspilot.tools.fixture import _scope_window as fixture_scope_window
from opspilot.tools.otel_demo import (
    CREDENTIAL_REF,
    METRICS_TOOL,
    SOURCE,
    TOOL_SCHEMAS,
    otel_demo_face,
    scope_default_query_window,
    scope_window,
)
from opspilot.tools.otel_demo import _query_window as otel_query_window
from tests.m1_tool_support import FakeClock

RECEIVED = datetime(2026, 10, 10, 12, 0, 30, 987654, tzinfo=timezone.utc)
FRAME_END = datetime(2026, 10, 10, 12, 0, 30, tzinfo=timezone.utc)
FRAME_START = FRAME_END - timedelta(hours=24)
DEADLINE = FRAME_END + timedelta(minutes=10)


def _anchor(starts_at, original="2026-10-10T10:41:54.365Z"):
    return AlertAnchor(received_at=RECEIVED, starts_at=starts_at, original=original)


def _alert_input(starts_at, *, face=None, run_id="run-1"):
    face = face or otel_demo_face(FakeClock(start=RECEIVED + timedelta(hours=3)))
    return face.input_for(
        run_id=run_id,
        question="Why is checkout erroring?",
        target_id="checkout-prod",
        deadline=DEADLINE,
        model_requests=4,
        alert=_anchor(starts_at),
    )


# -- J1/J2: frame and clamp ---------------------------------------------------


def test_frame_is_the_24h_ending_at_receipt_in_whole_utc_seconds():
    anchor = _anchor(datetime(2026, 10, 10, 10, 41, 54, 365000, tzinfo=timezone.utc))
    assert anchor.frame == (FRAME_START, FRAME_END)
    # Any offset in, UTC out.
    shifted = AlertAnchor(
        received_at=RECEIVED.astimezone(timezone(timedelta(hours=-7))),
        starts_at=anchor.starts_at,
        original="x",
    )
    assert shifted.frame == (FRAME_START, FRAME_END)


@pytest.mark.parametrize(
    ("starts_at", "anchor", "adjusted", "default_start"),
    [
        # Inside the frame: the alert's own start, truncated to the second.
        (
            datetime(2026, 10, 10, 10, 41, 54, 365000, tzinfo=timezone.utc),
            datetime(2026, 10, 10, 10, 41, 54, tzinfo=timezone.utc),
            None,
            datetime(2026, 10, 10, 9, 41, 54, tzinfo=timezone.utc),
        ),
        # In the future of the receipt: the frame's end.
        (
            FRAME_END + timedelta(minutes=5),
            FRAME_END,
            "future",
            FRAME_END - timedelta(hours=1),
        ),
        # Older than the frame: the frame's start; the default cannot start
        # before it.
        (
            FRAME_START - timedelta(days=3),
            FRAME_START,
            "before_frame",
            FRAME_START,
        ),
        # Within an hour of the frame's start: the default is clamped too.
        (
            FRAME_START + timedelta(minutes=20),
            FRAME_START + timedelta(minutes=20),
            None,
            FRAME_START,
        ),
        # Exactly the edges are inside.
        (FRAME_END, FRAME_END, None, FRAME_END - timedelta(hours=1)),
        (FRAME_START, FRAME_START, None, FRAME_START),
    ],
)
def test_anchor_clamps_into_the_frame(starts_at, anchor, adjusted, default_start):
    alert = _anchor(starts_at)
    assert alert.anchor == (anchor, adjusted)
    assert alert.default_query_window == (default_start, FRAME_END)


def test_subsecond_future_start_is_not_future_after_truncation():
    # Both instants are compared in whole seconds (F3).
    alert = _anchor(RECEIVED + timedelta(microseconds=1))
    assert alert.anchor == (FRAME_END, None)


def test_naive_times_are_refused():
    with pytest.raises(ContextError, match="INPUT_INVALID"):
        AlertAnchor(
            received_at=RECEIVED.replace(tzinfo=None), starts_at=RECEIVED, original=""
        )


# -- J3: the v3 input -----------------------------------------------------------


def test_alert_input_is_v3_with_anchor_facts_and_policy():
    starts = datetime(2026, 10, 10, 10, 41, 54, 365000, tzinfo=timezone.utc)
    fresh = _alert_input(starts)
    raw = fresh.as_json()
    assert raw["version"] == "opspilot-investigation-input-v3"
    assert raw["scope_facts"]["alert_starts_at"] == {
        "anchor": "2026-10-10T10:41:54+00:00",
        "original": "2026-10-10T10:41:54.365Z",
        "adjusted": None,
    }
    policy = raw["evidence_context"]["time_policies"][0]
    assert policy["window"] == {
        "start": FRAME_START.isoformat(),
        "end": FRAME_END.isoformat(),
    }
    assert policy["anchor"] == "2026-10-10T10:41:54+00:00"
    assert policy["anchor_rule"] == ALERT_ANCHOR_RULE == "alert_starts_at"
    assert policy["default_query_window"] == {
        "start": "2026-10-10T09:41:54+00:00",
        "end": FRAME_END.isoformat(),
    }
    # Evidence eligibility keeps the v4 reference instant.
    assert policy["reference_rule"] == "response_received_at"
    # The face's clock is not consulted for an alert frame.
    assert InvestigationInput.from_json(raw) == fresh


def test_alert_input_frame_does_not_depend_on_the_face_clock():
    starts = FRAME_END - timedelta(minutes=3)
    early = _alert_input(starts, face=otel_demo_face(FakeClock(start=RECEIVED)))
    late = _alert_input(
        starts, face=otel_demo_face(FakeClock(start=RECEIVED + timedelta(days=2)))
    )
    assert early.as_json() == late.as_json()


def test_non_alert_inputs_keep_their_versions_and_policy():
    face = otel_demo_face(FakeClock(start=RECEIVED))
    plain = face.input_for(
        run_id="run-1",
        question="q",
        target_id="checkout-prod",
        deadline=DEADLINE,
        model_requests=4,
    )
    raw = plain.as_json()
    assert raw["version"] == "opspilot-investigation-input-v1"
    policy = raw["evidence_context"]["time_policies"][0]
    assert set(policy) == {
        "id",
        "mode",
        "all_authorized_targets",
        "window",
        "reference_rule",
    }
    v2 = replace(
        plain,
        scope_facts={
            **plain.scope_facts,
            "affected_service": {"namespace": "otel-demo", "workload": "checkout"},
        },
    )
    assert v2.version == "opspilot-investigation-input-v2"
    assert InvestigationInput.from_json(v2.as_json()) == v2


def test_version_and_facts_must_agree():
    raw = _alert_input(FRAME_END).as_json()
    for older in ("opspilot-investigation-input-v1", "opspilot-investigation-input-v2"):
        with pytest.raises(ContextError, match="INPUT_INVALID"):
            InvestigationInput.from_json({**raw, "version": older})
    stripped = {
        **raw,
        "scope_facts": {
            k: v for k, v in raw["scope_facts"].items() if k != "alert_starts_at"
        },
    }
    with pytest.raises(ContextError, match="INPUT_INVALID"):
        InvestigationInput.from_json(stripped)


def test_an_unknown_later_version_is_incompatible(monkeypatch):
    raw = _alert_input(FRAME_END).as_json()
    with pytest.raises(ContextError, match="INCOMPATIBLE_STATE"):
        InvestigationInput.from_json(
            {**raw, "version": "opspilot-investigation-input-v4"}
        )
    # F4: a worker that predates v3 blocks the row instead of dropping facts.
    monkeypatch.setattr(
        context_module,
        "KNOWN_INPUT_VERSIONS",
        (context_module.INPUT_VERSION, context_module.INPUT_VERSION_AFFECTED_SERVICE),
    )
    with pytest.raises(ContextError, match="INCOMPATIBLE_STATE"):
        InvestigationInput.from_json(raw)


@pytest.mark.parametrize(
    "fact",
    [
        None,
        "2026-10-10T10:41:54+00:00",
        {"anchor": "a", "original": "o"},
        {"anchor": "a", "original": "o", "adjusted": "sideways"},
        {"anchor": "", "original": "o", "adjusted": None},
        {"anchor": "a", "original": 3, "adjusted": None},
        {"anchor": "a", "original": "o", "adjusted": None, "extra": 1},
    ],
)
def test_a_malformed_anchor_fact_is_invalid(fact):
    fresh = _alert_input(FRAME_END)
    with pytest.raises(ContextError, match="INPUT_INVALID"):
        replace(fresh, scope_facts={**fresh.scope_facts, "alert_starts_at": fact})


def test_a_face_without_a_time_policy_cannot_carry_an_anchor():
    face = otel_demo_face(FakeClock(start=RECEIVED))
    bare = replace(
        face, evidence_context=lambda run_id: {"type": "x", "run_id": run_id}
    )
    with pytest.raises(ContextError, match="INPUT_INVALID"):
        _alert_input(FRAME_END, face=bare)


# -- what the model and continuation read --------------------------------------


def test_projection_keeps_anchor_fields_and_the_model_sees_them():
    fresh = _alert_input(FRAME_END - timedelta(hours=2))
    projected = evidence_context_projection(fresh.evidence_context, run_id="run-1")
    assert projected is not None
    policy = projected["time_policies"][0]
    original = fresh.evidence_context["time_policies"][0]
    assert policy == original
    messages, _ = initial_messages(fresh, evidence_context=projected)
    shown = [m["content"] for m in messages if "time_policies" in str(m["content"])]
    assert shown and "default_query_window" in shown[0] and "anchor_rule" in shown[0]


def test_a_malformed_default_window_drops_the_whole_policy():
    fresh = _alert_input(FRAME_END)
    policy = dict(fresh.evidence_context["time_policies"][0])
    policy["default_query_window"] = {"start": 1, "end": "x"}
    context = {**fresh.evidence_context, "time_policies": [policy]}
    projected = evidence_context_projection(context, run_id="run-1")
    assert projected is not None and projected["time_policies"] == []


def test_continuation_keeps_the_previous_frame_and_anchor():
    previous = _alert_input(FRAME_END - timedelta(hours=2))
    snapshot = {
        "run": {"run_id": "run-1", "input": previous.as_json()},
        "steps": [],
    }
    later = DEADLINE + timedelta(hours=5)
    successor = continuation_input(
        snapshot,
        new_run_id="run-2",
        deadline=later,
        authorized_targets=frozenset({"checkout-prod"}),
    )
    assert successor.version == "opspilot-investigation-input-v3"
    assert (
        successor.scope_facts["alert_starts_at"]
        == previous.scope_facts["alert_starts_at"]
    )
    assert successor.scope_facts["deadline"] == later.isoformat()
    assert successor.evidence_context["run_id"] == "run-2"
    assert (
        successor.evidence_context["time_policies"]
        == previous.evidence_context["time_policies"]
    )
    assert InvestigationInput.from_json(successor.as_json()) == successor


# -- J4: the default query window -----------------------------------------------


def test_scope_reads_the_default_window_from_the_input():
    fresh = _alert_input(FRAME_END - timedelta(hours=2))
    assert scope_window(fresh) == Window(FRAME_START, FRAME_END)
    assert scope_default_query_window(fresh) == Window(
        FRAME_END - timedelta(hours=3), FRAME_END
    )
    plain = replace(
        fresh,
        scope_facts={
            k: v for k, v in fresh.scope_facts.items() if k != "alert_starts_at"
        },
        evidence_context=otel_demo_face(FakeClock(start=RECEIVED)).evidence_context(
            "run-1"
        ),
    )
    assert scope_default_query_window(plain) is None


@pytest.mark.parametrize(
    "default",
    [
        {"start": "x", "end": "y"},
        # Outside the frame.
        {
            "start": (FRAME_START - timedelta(hours=1)).isoformat(),
            "end": FRAME_END.isoformat(),
        },
    ],
)
def test_an_unusable_default_window_blocks_the_scope(default):
    fresh = _alert_input(FRAME_END)
    policy = {
        **fresh.evidence_context["time_policies"][0],
        "default_query_window": default,
    }
    broken = replace(
        fresh, evidence_context={**fresh.evidence_context, "time_policies": [policy]}
    )
    with pytest.raises(ContextError, match="SCOPE_WINDOW_MISSING"):
        scope_default_query_window(broken)


def _request(params, *, default=None):
    return TransportRequest(
        operation_id="step-1-t0",
        source=SOURCE,
        verb="query",
        endpoint="http://127.0.0.1:19090",
        selector={},
        params=params,
        window=Window(FRAME_START, FRAME_END),
        timeout_seconds=30.0,
        max_result_bytes=1024,
        credential_ref=CREDENTIAL_REF,
        tool=METRICS_TOOL,
        default_window=default,
    )


def test_transport_uses_the_alert_default_when_no_window_is_given():
    default = Window(FRAME_END - timedelta(hours=5), FRAME_END)
    window, refusal = otel_query_window(_request({"expr": "up"}, default=default))
    assert refusal is None and window == default


def test_transport_keeps_the_last_hour_without_an_alert_default():
    window, refusal = otel_query_window(_request({"expr": "up"}))
    assert refusal is None
    assert window == Window(FRAME_END - timedelta(hours=1), FRAME_END)


def test_an_explicit_window_still_wins_and_must_lie_in_the_frame():
    default = Window(FRAME_END - timedelta(hours=5), FRAME_END)
    inside = {
        "expr": "up",
        "start": (FRAME_START + timedelta(hours=1)).isoformat(),
        "end": (FRAME_START + timedelta(hours=2)).isoformat(),
    }
    window, refusal = otel_query_window(_request(inside, default=default))
    assert refusal is None
    assert window == Window(
        FRAME_START + timedelta(hours=1), FRAME_START + timedelta(hours=2)
    )
    outside = {
        "expr": "up",
        "start": (FRAME_START - timedelta(hours=1)).isoformat(),
        "end": FRAME_START.isoformat(),
    }
    window, refusal = otel_query_window(_request(outside, default=default))
    assert window is None and refusal is not None
    assert refusal.source_status == "QUERY_OUT_OF_WINDOW"
    assert json.loads(refusal.body)["authorized_window"] == {
        "start": FRAME_START.isoformat(),
        "end": FRAME_END.isoformat(),
    }


def _scope(**over):
    fields = {
        "scope_id": "s",
        "subject_kind": "incident",
        "subject_id": "i",
        "run_id": "r",
        "control_generation": 0,
        "global_suspension_generation": 0,
        "target_suspension_generation": 0,
        "registry_revision": "rr",
        "tool_registry_revision": "tr",
        "target_ids": frozenset({"t"}),
        "tool_names": frozenset({"x"}),
        "window": Window(FRAME_START, FRAME_END),
        "deadline": DEADLINE,
    }
    fields.update(over)
    return QueryScope(**fields)


def test_query_scope_refuses_a_default_outside_its_window():
    assert _scope().default_query_window is None
    inside = Window(FRAME_END - timedelta(hours=1), FRAME_END)
    assert _scope(default_query_window=inside).default_query_window == inside
    with pytest.raises(ToolContractError, match="INVALID_SCOPE_WINDOW"):
        _scope(default_query_window=Window(FRAME_START - timedelta(1), FRAME_END))
    with pytest.raises(ToolContractError, match="INVALID_SCOPE_WINDOW"):
        _scope(default_query_window={"start": "a", "end": "b"})


def test_tool_descriptions_state_the_default_query_window():
    text = json.dumps(TOOL_SCHEMAS)
    assert text.count("default_query_window") == 2


# -- the fixture profile ---------------------------------------------------------


def test_fixture_alert_runs_read_an_hour_of_their_own_frame():
    fixture = fixture_face()
    plain = fixture.input_for(
        run_id="run-1",
        question="q",
        target_id="checkout-prod",
        deadline=DEADLINE,
        model_requests=4,
    )
    assert fixture_scope_window(plain) == Window(WINDOW_START, WINDOW_END)
    # Anchored before the frame: the default spans the whole frame, the
    # fixture reads its first hour.
    old = _alert_input(FRAME_START - timedelta(days=1), face=fixture)
    assert fixture_scope_window(old) == Window(
        FRAME_START, FRAME_START + timedelta(hours=1)
    )
    recent = _alert_input(FRAME_END - timedelta(minutes=10), face=fixture)
    # The default (an hour before the anchor to the frame's end) is 70
    # minutes; the fixture reads its first hour.
    assert fixture_scope_window(recent) == Window(
        FRAME_END - timedelta(minutes=70), FRAME_END - timedelta(minutes=10)
    )


# -- the executor hands the default to the transport ---------------------------


@pytest.mark.parametrize(
    ("request_minutes", "passed"),
    [
        # The loop's request covers the whole frame: the default reaches the
        # transport.
        (60, True),
        # A narrower request window does not contain it: dropped, never
        # widened.
        (10, False),
    ],
)
def test_executor_passes_the_default_only_inside_the_request_window(
    request_minutes, passed
):
    from opspilot.tools import TransportResponse
    from tests.m1_tool_support import (
        WINDOW_END as SUPPORT_END,
    )
    from tests.m1_tool_support import (
        WINDOW_START as SUPPORT_START,
    )
    from tests.m1_tool_support import (
        body,
        build,
        request,
    )

    default = Window(SUPPORT_END - timedelta(minutes=30), SUPPORT_END)
    executor, transport, _, _ = build(scope_overrides={"default_query_window": default})
    transport.response = TransportResponse(
        body=body([{"metric": "checkout", "value": 1}]), data_as_of=SUPPORT_START
    )
    executor.execute(
        request(
            window={
                "start": (SUPPORT_END - timedelta(minutes=request_minutes)).isoformat(),
                "end": SUPPORT_END.isoformat(),
            }
        )
    )
    assert len(transport.requests) == 1
    sent = transport.requests[0].default_window
    assert sent == (default if passed else None)


def test_the_otel_demo_factory_scopes_an_alert_run_with_its_default(monkeypatch):
    import urllib.request

    from opspilot.tools.otel_demo import OtelDemoConfig, otel_demo_executor_factory
    from tests.m1_tool_support import RecordingSink
    from tests.test_m1_otel_demo_contract import FakeOpener, FakeStore, _lease

    opener = FakeOpener()
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: opener)
    store = FakeStore(_lease(), deadline=DEADLINE)
    factory = otel_demo_executor_factory(
        store,
        evidence=RecordingSink(),
        clock=FakeClock(start=RECEIVED),
        config=OtelDemoConfig(),
    )
    run_id = str(store.lease.run_id)
    alert = _alert_input(FRAME_END - timedelta(hours=2), run_id=run_id)
    scope = factory(store.lease, alert).scope
    assert scope.window == Window(FRAME_START, FRAME_END)
    assert scope.default_query_window == Window(
        FRAME_END - timedelta(hours=3), FRAME_END
    )
    plain = otel_demo_face(FakeClock(start=RECEIVED)).input_for(
        run_id=run_id,
        question="q",
        target_id="checkout-prod",
        deadline=DEADLINE,
        model_requests=4,
    )
    assert factory(store.lease, plain).scope.default_query_window is None


@pytest.mark.parametrize(
    "default",
    [
        {},
        {"start": FRAME_START.isoformat()},
        {"end": FRAME_END.isoformat()},
        {
            "start": FRAME_START.isoformat(),
            "end": FRAME_END.isoformat(),
            "step": "30s",
        },
        {"start": FRAME_START.isoformat(), "end": 1},
        {"start": "yesterday", "end": FRAME_END.isoformat()},
        # Naive instants are not absolute.
        {"start": "2026-10-10T00:00:00", "end": FRAME_END.isoformat()},
        [FRAME_START.isoformat(), FRAME_END.isoformat()],
    ],
)
def test_any_incomplete_or_malformed_default_window_drops_the_policy(default):
    """Independent review P2: never a partial default window."""
    fresh = _alert_input(FRAME_END)
    policy = {
        **fresh.evidence_context["time_policies"][0],
        "default_query_window": default,
    }
    context = {**fresh.evidence_context, "time_policies": [policy]}
    projected = evidence_context_projection(context, run_id="run-1")
    assert projected is not None and projected["time_policies"] == []


def test_a_well_formed_default_window_is_projected_verbatim():
    fresh = _alert_input(FRAME_END - timedelta(hours=2))
    projected = evidence_context_projection(fresh.evidence_context, run_id="run-1")
    assert projected is not None
    assert (
        projected["time_policies"][0]["default_query_window"]
        == fresh.evidence_context["time_policies"][0]["default_query_window"]
    )
