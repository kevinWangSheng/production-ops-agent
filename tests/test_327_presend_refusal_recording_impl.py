"""#327 / #283: every refusal before dispatch is a recordable outcome.

Contract (V2-R8 and the lead's 2026-10-31 rulings): ``check_result`` mirrors
the executor. A not-dispatched result carries a window iff the executor got
past the point where ``_authorize`` sets it (``executor.py``: after the window
checks, before ``_accept_params``); that window equals the expected one
(request -> default -> authorized) inside the authorized window. Refusals
before that point carry none. Replay compares status and reason. Drives the
shipped otel-demo executor (fake opener, in-memory evidence) through each
refusal path, then the eval validator.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import timedelta

import pytest

from opspilot.evaluation.codec import StorageError
from opspilot.evaluation.recording import _tool_integrity, unwire, wire
from opspilot.tools import ReadOnlyToolExecutor
from opspilot.tools.evidence_read import EVIDENCE_TOOL
from opspilot.tools.executor import (
    ToolBudgetExhausted,
    ToolControlDenied,
    ToolRequest,
    ToolTimeBudgetExhausted,
)
from opspilot.tools.otel_demo import METRICS_TOOL, TRACES_TOOL
from opspilot.tools.outcomes import ToolOutcome, Window
from opspilot.tools.registry import TargetRegistry
from tests.test_308_pr2_read_evidence_impl import (
    _check,
    _eval_context,
    _over_limit,
)
from tests.test_m1_otel_demo_contract import GOOD_EXPR, NOW, WINDOW, _call

EVENT_CLOCK = NOW + timedelta(minutes=30)


def _rebuild(executor, *, targets=None, **scope_changes):
    """The same executor wiring under a changed scope or target registry."""
    return ReadOnlyToolExecutor(
        scope=dataclasses.replace(executor.scope, **scope_changes),
        tools=executor._tools,
        targets=targets or executor._targets,
        transport=executor._transport,
        evidence=executor._evidence,
        control=executor._control,
        clock=executor._clock,
        ledger=executor._ledger,
        token_counter=executor._count_tokens,
        overflow_summary=executor._overflow_summary,
    )


def _metrics(params=None, **over) -> ToolRequest:
    return _call(METRICS_TOOL, {"expr": GOOD_EXPR, **(params or {})}, index=1, **over)


def _on_charge(executor, effect: Callable[[], None]) -> None:
    """Run ``effect`` instead of (or after) the durable charge."""
    original = executor._ledger.charge

    def charge(*args, **kwargs):
        effect()
        return original(*args, **kwargs)

    executor._ledger.charge = charge


def _raise(error: Exception) -> Callable[[], None]:
    def effect() -> None:
        raise error

    return effect


# Each path: setup(executor, store, parent_outcome) -> (executor, request).
Setup = Callable[..., tuple[ReadOnlyToolExecutor, ToolRequest]]


def _registry_changed(executor, store, parent):
    return _rebuild(executor, registry_revision="other-revision"), _metrics()


def _tool_registry_changed(executor, store, parent):
    return _rebuild(executor, tool_registry_revision="other-revision"), _metrics()


def _tool_not_in_scope(executor, store, parent):
    names = executor.scope.tool_names - {METRICS_TOOL}
    return _rebuild(executor, tool_names=names), _metrics()


def _target_not_authorized(executor, store, parent):
    return _rebuild(executor, target_ids=frozenset({"another-target"})), _metrics()


def _target_source_mismatch(executor, store, parent):
    (target,) = executor._targets._entries.values()
    targets = TargetRegistry([dataclasses.replace(target, source="other-source")])
    rebuilt = _rebuild(executor, targets=targets, registry_revision=targets.revision)
    return rebuilt, _metrics()


def _unparseable_window(executor, store, parent):
    return executor, dataclasses.replace(_metrics(), window={"start": "x"})


def _window_out_of_scope(executor, store, parent):
    wide = Window(WINDOW.start - timedelta(hours=1), WINDOW.end)
    return executor, _metrics(window=wide)


def _window_too_large(executor, store, parent):
    frame = Window(WINDOW.end - timedelta(days=2), WINDOW.end)
    return _rebuild(executor, window=frame), _metrics(window=frame)


def _param_not_allowed(executor, store, parent):
    return executor, _metrics({"bogus": 1})


def _param_wrong_type(executor, store, parent):
    return executor, _call(METRICS_TOOL, {"expr": 1}, index=1)


def _param_unencodable(executor, store, parent):
    return executor, _metrics({"step_seconds": 10**4300})


def _suspended(executor, store, parent):
    store.control = {**store.control, "global_suspended": True}
    return executor, _metrics()


def _generation_changed(executor, store, parent):
    store.control = {
        **store.control,
        "incident_generation": store.control["incident_generation"] + 1,
    }
    return executor, _metrics()


def _control_unavailable(executor, store, parent):
    store.control = RuntimeError("control store down")
    return executor, _metrics()


def _deadline_at_reserve(executor, store, parent):
    executor._clock.advance((store.deadline - NOW).total_seconds())
    return executor, _metrics()


def _operation_budget(executor, store, parent):
    _on_charge(executor, _raise(ToolBudgetExhausted()))
    return executor, _metrics()


def _time_budget(executor, store, parent):
    _on_charge(executor, _raise(ToolTimeBudgetExhausted()))
    return executor, _metrics()


def _charge_control_denied(executor, store, parent):
    _on_charge(executor, _raise(ToolControlDenied()))
    return executor, _metrics()


def _charge_failed(executor, store, parent):
    _on_charge(executor, _raise(RuntimeError("ledger down")))
    return executor, _metrics()


def _suspended_after_charge(executor, store, parent):
    def suspend() -> None:
        store.control = {**store.control, "global_suspended": True}

    _on_charge(executor, suspend)
    return executor, _metrics()


def _deadline_after_charge(executor, store, parent):
    _on_charge(
        executor,
        lambda: executor._clock.advance((store.deadline - NOW).total_seconds()),
    )
    return executor, _metrics()


_OUTSIDE = {
    "start": (WINDOW.start - timedelta(hours=1)).isoformat(),
    "end": WINDOW.end.isoformat(),
}


def _query_out_of_window(executor, store, parent):
    return executor, _metrics(_OUTSIDE)


def _invalid_step(executor, store, parent):
    return executor, _metrics({"step_seconds": 1})


def _evidence_not_readable(executor, store, parent):
    return executor, _call(
        EVIDENCE_TOOL, {"evidence_id": "nope:1", "limit": 1}, index=1
    )


def _evidence_invalid_params(executor, store, parent):
    params = {"evidence_id": parent.model_view["evidence_id"], "limit": 0}
    return executor, _call(EVIDENCE_TOOL, params, index=1)


def _traces_out_of_window(executor, store, parent):
    return executor, _call(TRACES_TOOL, {"service": "checkout", **_OUTSIDE}, index=1)


# name -> (setup, status, reason, carries the planned window, transport stage)
PATHS: dict[str, tuple[Setup, str, str, bool, bool]] = {
    # _authorize, before the window is set
    "TARGET_REGISTRY_CHANGED": (
        _registry_changed,
        "denied",
        "TARGET_REGISTRY_CHANGED",
        False,
        False,
    ),
    "TOOL_REGISTRY_CHANGED": (
        _tool_registry_changed,
        "denied",
        "TOOL_REGISTRY_CHANGED",
        False,
        False,
    ),
    "TOOL_NOT_IN_SCOPE": (
        _tool_not_in_scope,
        "denied",
        "TOOL_NOT_IN_SCOPE",
        False,
        False,
    ),
    "TARGET_NOT_AUTHORIZED": (
        _target_not_authorized,
        "denied",
        "TARGET_NOT_AUTHORIZED",
        False,
        False,
    ),
    "TARGET_SOURCE_MISMATCH": (
        _target_source_mismatch,
        "denied",
        "TARGET_SOURCE_MISMATCH",
        False,
        False,
    ),
    "window-INVALID_PARAMS": (
        _unparseable_window,
        "error",
        "INVALID_PARAMS",
        False,
        False,
    ),
    "WINDOW_OUT_OF_SCOPE": (
        _window_out_of_scope,
        "denied",
        "WINDOW_OUT_OF_SCOPE",
        False,
        False,
    ),
    "WINDOW_TOO_LARGE": (
        _window_too_large,
        "denied",
        "WINDOW_TOO_LARGE",
        False,
        False,
    ),
    # _authorize, after the window is set: parameter acceptance
    "PARAM_NOT_ALLOWED": (
        _param_not_allowed,
        "denied",
        "PARAM_NOT_ALLOWED",
        True,
        False,
    ),
    "params-INVALID_PARAMS": (
        _param_wrong_type,
        "error",
        "INVALID_PARAMS",
        True,
        False,
    ),
    "canonical-INVALID_PARAMS": (
        _param_unencodable,
        "error",
        "INVALID_PARAMS",
        True,
        False,
    ),
    # _reserve
    "reserve-SUSPENDED": (_suspended, "denied", "SUSPENDED", True, False),
    "reserve-CONTROL_GENERATION_CHANGED": (
        _generation_changed,
        "denied",
        "CONTROL_GENERATION_CHANGED",
        True,
        False,
    ),
    "reserve-CONTROL_UNAVAILABLE": (
        _control_unavailable,
        "denied",
        "CONTROL_UNAVAILABLE",
        True,
        False,
    ),
    "reserve-DEADLINE_EXCEEDED": (
        _deadline_at_reserve,
        "denied",
        "DEADLINE_EXCEEDED",
        True,
        False,
    ),
    # _run, the durable charge and the re-check before the transport
    "charge-OPERATION_BUDGET_EXHAUSTED": (
        _operation_budget,
        "denied",
        "OPERATION_BUDGET_EXHAUSTED",
        True,
        False,
    ),
    "charge-TIME_BUDGET_EXHAUSTED": (
        _time_budget,
        "denied",
        "TIME_BUDGET_EXHAUSTED",
        True,
        False,
    ),
    "charge-control-denied": (
        _charge_control_denied,
        "denied",
        "CONTROL_UNAVAILABLE",
        True,
        False,
    ),
    "charge-failed": (
        _charge_failed,
        "denied",
        "CONTROL_UNAVAILABLE",
        True,
        False,
    ),
    "recheck-SUSPENDED": (
        _suspended_after_charge,
        "denied",
        "SUSPENDED",
        True,
        False,
    ),
    "recheck-DEADLINE_EXCEEDED": (
        _deadline_after_charge,
        "denied",
        "DEADLINE_EXCEEDED",
        True,
        False,
    ),
    # the transport refuses without sending (sent=false)
    "QUERY_OUT_OF_WINDOW": (
        _query_out_of_window,
        "error",
        "INVALID_PARAMS",
        True,
        True,
    ),
    "INVALID_STEP": (_invalid_step, "error", "INVALID_PARAMS", True, True),
    "traces-QUERY_OUT_OF_WINDOW": (
        _traces_out_of_window,
        "error",
        "INVALID_PARAMS",
        True,
        True,
    ),
    "EVIDENCE_NOT_READABLE": (
        _evidence_not_readable,
        "denied",
        "EVIDENCE_NOT_READABLE",
        True,
        True,
    ),
    "evidence-INVALID_PARAMS": (
        _evidence_invalid_params,
        "error",
        "INVALID_PARAMS",
        True,
        True,
    ),
}

WINDOWED = sorted(name for name, path in PATHS.items() if path[3])
PLAN_STAGE = sorted(name for name, path in PATHS.items() if not path[3])
TRANSPORT = sorted(name for name, path in PATHS.items() if path[4])


def _refusal(monkeypatch, name):
    setup, status, reason, windowed, transport = PATHS[name]
    executor, opener, store, parent = _over_limit_parts(monkeypatch)
    executor, call = setup(executor, store, parent)
    ctx = _eval_context(executor)
    calls = len(opener.requests)
    outcome = executor.execute(call)
    assert (outcome.status, outcome.reason) == (status, reason)
    assert outcome.source_contact == "none" and outcome.operation.sent is False
    assert len(opener.requests) == calls  # nothing reached the source
    assert outcome.operation.window == (Window.parse(call.window) if windowed else None)
    assert (outcome.operation.finished_at is not None) is transport
    return executor, ctx, call, outcome


def _over_limit_parts(monkeypatch):
    executor, _, opener, store, parent = _over_limit(monkeypatch)
    return executor, opener, store, parent


def _with_op(outcome: ToolOutcome, **changes) -> ToolOutcome:
    return dataclasses.replace(
        outcome, operation=dataclasses.replace(outcome.operation, **changes)
    )


def _binding(ctx, call):
    return next(item for item in ctx.bindings if item.tool == call.tool_name)


def _narrower() -> Window:
    return Window(WINDOW.start + timedelta(minutes=1), WINDOW.end)


@pytest.mark.parametrize("name", sorted(PATHS))
def test_every_refusal_before_dispatch_records_and_replays(monkeypatch, name):
    executor, ctx, call, outcome = _refusal(monkeypatch, name)
    _check(executor, ctx, call, outcome)
    frozen = unwire(ToolOutcome, wire(outcome))
    binding = _binding(ctx, call)
    _tool_integrity(ctx, call, binding, frozen, EVENT_CLOCK)
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
    _tool_integrity(replayed, request, binding, frozen, EVENT_CLOCK)
    assert (frozen.status, frozen.reason, frozen.source_contact) == (
        outcome.status,
        outcome.reason,
        "none",
    )


@pytest.mark.parametrize("window", ["narrower", None])
@pytest.mark.parametrize("name", WINDOWED)
def test_a_window_bearing_refusal_with_another_window_is_rejected(
    monkeypatch, name, window
):
    executor, ctx, call, outcome = _refusal(monkeypatch, name)
    forged = _with_op(outcome, window=_narrower() if window else None)
    with pytest.raises(StorageError, match="IDENTITY_MISMATCH"):
        _check(executor, ctx, call, forged)


@pytest.mark.parametrize("name", PLAN_STAGE)
def test_a_refusal_before_the_window_is_set_carries_none(monkeypatch, name):
    executor, ctx, call, outcome = _refusal(monkeypatch, name)
    with pytest.raises(StorageError, match="IDENTITY_MISMATCH"):
        _check(executor, ctx, call, _with_op(outcome, window=WINDOW))


@pytest.mark.parametrize("name", TRANSPORT)
@pytest.mark.parametrize(
    "tamper",
    [
        {"authorized_at": None},
        {"finished_at": None},
        {"finished_at": None, "elapsed_seconds": None},
        {"timeout_seconds": None},
    ],
    ids=["no-authorized", "no-finished", "no-phase", "no-timeout"],
)
def test_a_transport_refusal_without_its_phase_times_is_rejected(
    monkeypatch, name, tamper
):
    executor, ctx, call, outcome = _refusal(monkeypatch, name)
    with pytest.raises(StorageError, match="IDENTITY_MISMATCH"):
        _check(executor, ctx, call, _with_op(outcome, **tamper))


@pytest.mark.parametrize("name", sorted(PATHS))
def test_a_refusal_before_dispatch_claiming_source_contact_is_rejected(
    monkeypatch, name
):
    executor, ctx, call, outcome = _refusal(monkeypatch, name)
    forged = dataclasses.replace(outcome, source_contact="possible")
    with pytest.raises(StorageError, match="IDENTITY_MISMATCH"):
        _check(executor, ctx, call, forged)


def test_evidence_not_readable_is_only_an_evidence_read_refusal(monkeypatch):
    executor, ctx, call, outcome = _refusal(monkeypatch, "INVALID_STEP")
    forged = dataclasses.replace(
        outcome, status="denied", reason="EVIDENCE_NOT_READABLE"
    )
    with pytest.raises(StorageError, match="IDENTITY_MISMATCH"):
        _check(executor, ctx, call, forged)
