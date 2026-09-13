"""Cumulative tool wall-time cap: refuses further dispatch and reaps the pending child.

Fixed clock for the budget arithmetic; a short real child only for the cleanup
path. Reproduces the dispatch order used by ``holmes_baseline.guarded_send``
(``tool_remaining`` before ``run_child``) with the real functions, without any
provider or environment call.
"""

import sys
import time

import pytest

from scripts.m0_environment.holmes_baseline import tool_remaining
from scripts.m0_environment.round02 import PROFILE, run_child

NOW = 1_000.0
SCOPE_DEADLINE = NOW + 3_600
RUN_STOP = NOW + 1_800
IGNORE_TERM_CHILD = [
    sys.executable,
    "-c",
    "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(90)",
]


class Dispatcher:
    """Same sequence as the wrapper: bound first, spawn second, always account."""

    def __init__(self, clock):
        self.tool_elapsed = 0.0
        self.spawned = 0
        self.clock = clock

    def dispatch(self, command, *, grace_seconds=2):
        remaining = tool_remaining(
            scope_deadline=SCOPE_DEADLINE,
            run_stop=RUN_STOP,
            tool_elapsed=self.tool_elapsed,
            profile=PROFILE,
            now=NOW,
        )
        started = self.clock()
        self.spawned += 1
        try:
            return run_child(
                command, b"", wall_seconds=remaining, grace_seconds=grace_seconds
            )
        finally:
            self.tool_elapsed += self.clock() - started


def test_single_tool_bound_never_exceeds_profile_or_remaining_budget():
    assert (
        tool_remaining(
            scope_deadline=SCOPE_DEADLINE,
            run_stop=RUN_STOP,
            tool_elapsed=0.0,
            profile=PROFILE,
            now=NOW,
        )
        == PROFILE.tool_seconds
    )
    assert (
        tool_remaining(
            scope_deadline=SCOPE_DEADLINE,
            run_stop=RUN_STOP,
            tool_elapsed=PROFILE.tool_total_seconds - 10,
            profile=PROFILE,
            now=NOW,
        )
        == 10
    )


def test_cumulative_cap_refuses_dispatch_before_spawning(monkeypatch):
    ticks = iter([0.0, 0.0])
    dispatcher = Dispatcher(lambda: next(ticks))
    dispatcher.tool_elapsed = PROFILE.tool_total_seconds - 4  # 4 s cannot cover cleanup
    with pytest.raises(TimeoutError, match="tool total deadline"):
        dispatcher.dispatch(IGNORE_TERM_CHILD)
    assert dispatcher.spawned == 0
    assert dispatcher.tool_elapsed == PROFILE.tool_total_seconds - 4


def test_exhausted_cap_is_reported_as_tool_boundary_not_run_or_scope():
    with pytest.raises(TimeoutError, match="tool total deadline"):
        tool_remaining(
            scope_deadline=SCOPE_DEADLINE,
            run_stop=RUN_STOP,
            tool_elapsed=PROFILE.tool_total_seconds,
            profile=PROFILE,
            now=NOW,
        )


def test_pending_child_is_killed_reaped_and_counted_then_next_dispatch_refused():
    # Budget leaves a 5 s single-tool bound: child ignores SIGTERM, must be killed and reaped.
    dispatcher = Dispatcher(time.monotonic)
    dispatcher.tool_elapsed = PROFILE.tool_total_seconds - 5
    start = time.monotonic()
    with pytest.raises(TimeoutError, match="child wall deadline"):
        dispatcher.dispatch(IGNORE_TERM_CHILD, grace_seconds=0.5)
    assert time.monotonic() - start < 6
    assert dispatcher.spawned == 1
    # Elapsed is charged even on failure, so the cap now refuses any further tool.
    assert dispatcher.tool_elapsed > PROFILE.tool_total_seconds - 5
    with pytest.raises(TimeoutError, match="tool total deadline"):
        dispatcher.dispatch(IGNORE_TERM_CHILD)
    assert dispatcher.spawned == 1
