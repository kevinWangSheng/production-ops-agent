"""Implementer tests for the eval PR-C runner seams (A5) on PostgreSQL.

Same opt-in and database as the other M1 PG tests (``M1_DURABLE_POSTGRES=1``,
``OPSPILOT_LAB_DSN``). Deterministic doubles only, no model or data source.
"""

from __future__ import annotations

import os

import pytest

from tests.integration.test_m1_loop_resume_postgres import (
    Harness,
    _tool_rounds,
    report_from_transcript,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)


class CountingModel:
    def __init__(self, inner):
        self.inner = inner
        self.calls = 0

    def complete(self, call):
        self.calls += 1
        return self.inner.complete(call)


def test_capture_delivery_sees_every_tool_message_and_defaults_change_nothing():
    h = Harness()
    seen = []
    runner = h.runner([*_tool_rounds(2), report_from_transcript])
    runner.capture_delivery = lambda view, message: seen.append((view, message))
    outcome = runner.resume(h.incident)
    assert outcome.status == "published", outcome
    assert len(seen) == 2
    assert all(message["role"] == "tool" for _view, message in seen)
    # Default None: the same script publishes with no hook.
    h2 = Harness()
    plain = h2.runner([*_tool_rounds(2), report_from_transcript])
    assert plain.capture_delivery is None and plain.attempt_wrapper is None
    assert plain.resume(h2.incident).status == "published"


def test_attempt_wrapper_runs_once_with_the_real_lease_before_any_call():
    h = Harness()
    runner = h.runner([*_tool_rounds(1), report_from_transcript])
    model = CountingModel(runner.model)
    runner.model = model
    seen = {}

    def wrapper(lease, input, executor, proceed):
        seen.update(
            lease=lease,
            scope_run=executor.scope.run_id,
            calls_before=model.calls,
            tools_before=h.transport_requests,
        )
        result = proceed()
        with pytest.raises(RuntimeError, match="ATTEMPT_ALREADY_PROCEEDED"):
            proceed()
        return result

    runner.attempt_wrapper = wrapper
    outcome = runner.resume(h.incident)
    assert outcome.status == "published", outcome
    assert seen["lease"].run_id == h.run
    assert seen["scope_run"] == str(h.run)
    assert outcome.epoch == seen["lease"].epoch
    assert seen["calls_before"] == 0 and seen["tools_before"] == 0
    assert model.calls == 2


def test_a_wrapper_that_raises_before_proceeding_runs_nothing():
    h = Harness()
    runner = h.runner([report_from_transcript])
    model = CountingModel(runner.model)
    runner.model = model

    def wrapper(lease, input, executor, proceed):
        raise OSError("recording destination unavailable")

    runner.attempt_wrapper = wrapper
    with pytest.raises(OSError):
        runner.resume(h.incident)
    assert model.calls == 0
    assert h.rows()["run"]["state"] != "completed"
