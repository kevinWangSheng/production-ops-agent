"""#327 / #283: a transport's pre-send refusal is its own recorded outcome kind.

Contract (V2-R8, lead pre-review 2026-10-31): a refusal the transport decides
before sending (``sent=false``) carries the window it would have read (the
requested or default window, inside the authorized one), ``authorized_at`` and
``finished_at``, and ``source_contact=none``; a plan-stage refusal still
carries no window. Replay compares status and reason. Drives the shipped
otel-demo executor (fake opener, in-memory evidence) and the eval validator.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest

from opspilot.evaluation.codec import StorageError
from opspilot.evaluation.recording import _tool_integrity, unwire, wire
from opspilot.tools.evidence_read import EVIDENCE_TOOL
from opspilot.tools.otel_demo import METRICS_TOOL
from opspilot.tools.outcomes import ToolOutcome, Window
from tests.test_308_pr2_read_evidence_impl import (
    _check,
    _eval_context,
    _over_limit,
)
from tests.test_m1_otel_demo_contract import GOOD_EXPR, NOW, WINDOW, _call

_OUTSIDE = {
    "expr": GOOD_EXPR,
    "start": (WINDOW.start - timedelta(hours=1)).isoformat(),
    "end": WINDOW.end.isoformat(),
}

PRESEND = {
    # The issue's case: start/end outside the authorized frame (#283).
    "QUERY_OUT_OF_WINDOW": (METRICS_TOOL, _OUTSIDE, "error", "INVALID_PARAMS"),
    "INVALID_STEP": (
        METRICS_TOOL,
        {"expr": GOOD_EXPR, "step_seconds": 1},
        "error",
        "INVALID_PARAMS",
    ),
    "EVIDENCE_NOT_READABLE": (
        EVIDENCE_TOOL,
        {"evidence_id": "nope:1", "limit": 1},
        "denied",
        "EVIDENCE_NOT_READABLE",
    ),
}


def _presend(monkeypatch, case):
    executor, _, opener, _, _ = _over_limit(monkeypatch)
    ctx = _eval_context(executor)
    tool, params, status, reason = PRESEND[case]
    call = _call(tool, params, index=1)
    calls = len(opener.requests)
    outcome = executor.execute(call)
    # The executor shape the issue describes; nothing reached the source.
    assert (outcome.status, outcome.reason) == (status, reason)
    assert outcome.source_contact == "none" and outcome.operation.sent is False
    assert len(opener.requests) == calls
    return executor, ctx, call, outcome


def _with_op(outcome: ToolOutcome, **changes) -> ToolOutcome:
    return dataclasses.replace(
        outcome, operation=dataclasses.replace(outcome.operation, **changes)
    )


def _binding(ctx, call):
    return next(item for item in ctx.bindings if item.tool == call.tool_name)


@pytest.mark.parametrize("case", sorted(PRESEND))
def test_a_presend_refusal_is_recordable(monkeypatch, case):
    executor, ctx, call, outcome = _presend(monkeypatch, case)
    op = outcome.operation
    assert op.window == Window.parse(call.window)
    assert op.authorized_at is not None and op.finished_at is not None
    _check(executor, ctx, call, outcome)
    # What the recorder writes and re-reads is the same shape.
    frozen = unwire(ToolOutcome, wire(outcome))
    _tool_integrity(ctx, call, _binding(ctx, call), frozen, NOW + timedelta(minutes=30))


@pytest.mark.parametrize("case", sorted(PRESEND))
def test_replay_of_a_presend_refusal_keeps_its_status_and_reason(monkeypatch, case):
    executor, ctx, call, outcome = _presend(monkeypatch, case)
    frozen = unwire(ToolOutcome, wire(outcome))
    # The replay package maps the new request onto the frozen operation.
    replayed = dataclasses.replace(
        ctx,
        initial_state={
            "replay_source": {
                "run_id": outcome.operation.run_id,
                "recording_hash": "0" * 64,
                "id_map": {"step-2-t1": outcome.operation.operation_id},
            }
        },
    )
    request = dataclasses.replace(call, step_id="step-2")
    _tool_integrity(
        replayed, request, _binding(ctx, call), frozen, NOW + timedelta(minutes=30)
    )
    assert (frozen.status, frozen.reason, frozen.source_contact) == (
        outcome.status,
        outcome.reason,
        "none",
    )


def _narrower() -> Window:
    return Window(WINDOW.start + timedelta(minutes=1), WINDOW.end)


@pytest.mark.parametrize("case", sorted(PRESEND))
@pytest.mark.parametrize(
    "tamper",
    [
        {"window": None},
        {"window": "narrower"},
        {"authorized_at": None},
        {"finished_at": None},
        {"finished_at": None, "elapsed_seconds": None},
        {"timeout_seconds": None},
    ],
    ids=[
        "no-window",
        "other-window",
        "no-authorized",
        "no-finished",
        "no-phase",
        "no-timeout",
    ],
)
def test_a_presend_refusal_with_another_shape_is_rejected(monkeypatch, case, tamper):
    executor, ctx, call, outcome = _presend(monkeypatch, case)
    changes = {
        key: _narrower() if value == "narrower" else value
        for key, value in tamper.items()
    }
    with pytest.raises(StorageError, match="IDENTITY_MISMATCH"):
        _check(executor, ctx, call, _with_op(outcome, **changes))


@pytest.mark.parametrize("case", sorted(PRESEND))
def test_a_presend_refusal_claiming_source_contact_is_rejected(monkeypatch, case):
    executor, ctx, call, outcome = _presend(monkeypatch, case)
    with pytest.raises(StorageError, match="IDENTITY_MISMATCH"):
        _check(
            executor, ctx, call, dataclasses.replace(outcome, source_contact="possible")
        )


def test_evidence_not_readable_is_only_an_evidence_read_refusal(monkeypatch):
    executor, ctx, call, outcome = _presend(monkeypatch, "INVALID_STEP")
    forged = dataclasses.replace(
        outcome, status="denied", reason="EVIDENCE_NOT_READABLE"
    )
    with pytest.raises(StorageError, match="IDENTITY_MISMATCH"):
        _check(executor, ctx, call, forged)


def test_a_plan_stage_refusal_still_carries_no_window(monkeypatch):
    executor, _, _, _, _ = _over_limit(monkeypatch)
    ctx = _eval_context(executor)
    wide = Window(WINDOW.start - timedelta(hours=1), WINDOW.end)
    call = _call(METRICS_TOOL, {"expr": GOOD_EXPR}, index=1, window=wide)
    outcome = executor.execute(call)
    assert (outcome.status, outcome.reason) == ("denied", "WINDOW_OUT_OF_SCOPE")
    assert outcome.operation.window is None
    _check(executor, ctx, call, outcome)
    with pytest.raises(StorageError, match="IDENTITY_MISMATCH"):
        _check(executor, ctx, call, _with_op(outcome, window=WINDOW))
