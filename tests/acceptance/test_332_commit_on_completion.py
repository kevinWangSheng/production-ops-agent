"""Acceptance tests for #332: a settled sibling result is committed on completion.

Contract: the #332 pre-review (U-1 A, U-2, R-1..R-8; user ruling 2026-10-23)
and #309 contract r2.  Durable commits follow completion order; what the model
sees (tool messages, coverage message, next request) stays in index order and
equal to cap=1.  Section 3 items 1 and 3 of the pre-review are observed here
through the in-memory store; item 2 (crash/resume) lives in
``tests/integration/test_332_commit_on_completion_postgres.py``.

Every wait in these doubles is bounded (2 s); a timeout is recorded and
asserted instead of hanging the suite.
"""

from __future__ import annotations

import threading
import time

import pytest

from opspilot.investigation.store import MemoryStepStore
from opspilot.tools import TransportResponse
from tests.acceptance.test_309_loop_dedupe_parallel import (
    _fixture_loop,
    _messages_projection,
    _normalize,
    _outcome_projection,
    _two_calls,
)
from tests.m1_investigation_support import reply, report_from_transcript
from tests.m1_tool_support import WINDOW_START, body

WAIT_SECONDS = 2.0


def _index(request) -> int:
    return int(request.operation_id.rsplit("-t", 1)[1])


def _committed_ordinals(store: MemoryStepStore) -> list[int]:
    return [
        item["ordinal"]
        for rows in list(store.tool_results.values())
        for item in list(rows)
    ]


def _wait_for_ordinal(store: MemoryStepStore, ordinal: int) -> bool:
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        if ordinal in _committed_ordinals(store):
            return True
        time.sleep(0.005)
    return False


class _WaitForSiblingCommit:
    """t0 returns only once ordinal 1 is in the store; t1 returns at once.

    Before #332 the main thread waits for t0 before committing t1, so t0's
    wait times out (``t0_saw_sibling`` stays False).  With
    ``deny_before_t0_returns`` the fence lands after that wait, before t0's
    result reaches the main thread.
    """

    def __init__(self, *, deny_before_t0_returns: bool = False) -> None:
        self.store: MemoryStepStore | None = None
        self.deny_before_t0_returns = deny_before_t0_returns
        self.lock = threading.Lock()
        self.requests = []
        self.t0_saw_sibling: bool | None = None

    def fetch(self, request):
        with self.lock:
            self.requests.append(request)
        if _index(request) == 0:
            self.t0_saw_sibling = _wait_for_ordinal(self.store, 1)
            if self.deny_before_t0_returns:
                self.store.deny_control()
        return TransportResponse(
            body=body([{"metric": "checkout", "value": 3}]),
            data_as_of=WINDOW_START,
        )


def _replies():
    return [
        reply(tool_calls=list(_two_calls()), finish="tool_calls"),
        report_from_transcript,
    ]


class _RecordingStore(MemoryStepStore):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.commit_attempts: list[int] = []

    def commit_tool(self, step_id, ordinal, result):
        self.commit_attempts.append(ordinal)
        super().commit_tool(step_id, ordinal, result)


def _swap_store(loop, base_store, transport):
    """Replace the fixture store with a recording one (same limits)."""
    store = _RecordingStore(
        budget_limit=16,
        deadline=base_store.deadline,
        clock=loop.clock,
        run_id=base_store.authorized_run_id,
    )
    transport.store = store
    loop.store = store
    return store


# -- pre-review §3 item 1 ----------------------------------------------------


def test_settled_sibling_is_committed_while_lower_index_is_in_flight():
    serial_loop, serial_request, serial_model, _t, serial_store, serial_events = (
        _fixture_loop(replies=_replies(), cap=1)
    )
    serial_outcome = serial_loop.run(serial_request)

    transport = _WaitForSiblingCommit()
    loop, request, model, _t, store, events = _fixture_loop(
        replies=_replies(), cap=2, transport=transport
    )
    transport.store = store
    outcome = loop.run(request)

    assert transport.t0_saw_sibling is True, (
        "ordinal 1 was not committed while ordinal 0 was still in flight "
        f"(waited {WAIT_SECONDS} s; #332 U-1 A)"
    )
    assert sorted(_index(r) for r in transport.requests) == [0, 1]

    # The model-visible face is unchanged from cap=1 (#309 r2: index order).
    assert len(model.calls) == len(serial_model.calls) == 2
    assert _normalize(_messages_projection(model)) == _normalize(
        _messages_projection(serial_model)
    )
    # The next request ends with the run coverage message (C3 §5 / B3).
    coverage = model.calls[1].messages[-1]
    serial_coverage = serial_model.calls[1].messages[-1]
    assert coverage["role"] == "user"
    assert _normalize(coverage) == _normalize(serial_coverage)
    assert _normalize(_outcome_projection(outcome)) == _normalize(
        _outcome_projection(serial_outcome)
    )
    assert outcome.execution == "completed"

    # Rows read back in ordinal order regardless of commit order (R-2).
    rows = next(iter(store.tool_results.values()))
    assert [item["ordinal"] for item in rows] == [0, 1]
    serial_rows = next(iter(serial_store.tool_results.values()))
    assert _normalize(rows) == _normalize(serial_rows)
    # r2 (a): tool_committed may arrive out of ordinal order; the set of kinds
    # is unchanged.
    assert sorted(kind for kind, _ in events.rows) == sorted(
        kind for kind, _ in serial_events.rows
    )


# -- pre-review §3 item 3: fence and exception under r2 -----------------------


def test_fence_after_early_commit_keeps_the_committed_row_and_stops():
    """t1 commits early; the fence lands before t0 returns (r2 (c), R-4)."""
    transport = _WaitForSiblingCommit(deny_before_t0_returns=True)
    loop, request, _model, _t, base_store, _events = _fixture_loop(
        replies=[reply(tool_calls=list(_two_calls()), finish="tool_calls")],
        cap=2,
        transport=transport,
    )
    store = _swap_store(loop, base_store, transport)
    outcome = loop.run(request)

    assert transport.t0_saw_sibling is True, (
        "ordinal 1 was not committed while ordinal 0 was still in flight"
    )
    assert outcome.execution == "failed"
    assert outcome.handoff_reasons == ("CONTROL_DENIED",)
    # One refused commit (ordinal 0), then nothing more is attempted.
    assert store.commit_attempts == [1, 0]
    rows = next(iter(store.tool_results.values()))
    assert [item["ordinal"] for item in rows] == [1]
    assert sorted(_index(r) for r in transport.requests) == [0, 1]


def test_fence_failure_stops_at_first_denied_commit_of_any_index():
    """Fence while both are in flight: the first commit attempt (either index)
    is refused, nothing is committed and nothing more is attempted (r2 (c))."""

    class FenceBothInFlight:
        def __init__(self) -> None:
            self.barrier = threading.Barrier(2)
            self.store = None
            self.lock = threading.Lock()
            self.fence_set = threading.Event()
            self.requests = []

        def fetch(self, request):
            with self.lock:
                self.requests.append(request)
            self.barrier.wait(timeout=WAIT_SECONDS)
            if _index(request) == 0:
                self.store.deny_control()
                self.fence_set.set()
            else:
                self.fence_set.wait(timeout=WAIT_SECONDS)
            return TransportResponse(
                body=body([{"metric": "checkout", "value": 3}]),
                data_as_of=WINDOW_START,
            )

    transport = FenceBothInFlight()
    loop, request, _model, _t, base_store, _events = _fixture_loop(
        replies=[reply(tool_calls=list(_two_calls()), finish="tool_calls")],
        cap=2,
        transport=transport,
    )
    store = _swap_store(loop, base_store, transport)
    outcome = loop.run(request)

    assert outcome.execution == "failed"
    assert outcome.handoff_reasons == ("CONTROL_DENIED",)
    assert len(store.commit_attempts) == 1
    assert store.commit_attempts[0] in (0, 1)
    assert _committed_ordinals(store) == []


def test_lower_index_exception_after_early_commit_keeps_the_higher_row():
    """t0 raises after t1 was observed complete: t1's row stays, the exception
    surfaces, and recovery has only ordinal 0 to re-dispatch (r2 (b))."""

    class FailingAfterSiblingExecutor:
        def __init__(self, delegate, store_ref):
            self.delegate = delegate
            self.scope = delegate.scope
            self.store_ref = store_ref
            self.calls: list[int] = []
            self.saw_sibling: bool | None = None

        @property
        def operations_used(self):
            return self.delegate.operations_used

        @property
        def tool_seconds_used(self):
            return self.delegate.tool_seconds_used

        def execute(self, request):
            self.calls.append(request.tool_index)
            if request.tool_index == 0:
                self.saw_sibling = _wait_for_ordinal(self.store_ref(), 1)
                raise RuntimeError("fixture tool failure")
            return self.delegate.execute(request)

    loop, request, _model, _transport, store, _events = _fixture_loop(
        replies=[reply(tool_calls=list(_two_calls()), finish="tool_calls")], cap=2
    )
    executor = FailingAfterSiblingExecutor(loop.executor, lambda: store)
    loop.executor = executor
    with pytest.raises(RuntimeError, match="fixture tool failure"):
        loop.run(request)
    assert executor.saw_sibling is True, (
        "ordinal 1 was not committed while ordinal 0 was still in flight"
    )
    assert sorted(executor.calls) == [0, 1]
    assert _committed_ordinals(store) == [1]
