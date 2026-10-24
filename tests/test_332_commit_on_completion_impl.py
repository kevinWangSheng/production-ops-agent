"""#332 implementer tests: under ``tool_parallelism`` a call's result is
committed as soon as the loop sees it finish, while the model-visible side
stays in index order (docs/tasks/2026-10-23-332-commit-on-completion.md,
#309 contract r2).

The independent acceptance tests live on their own branch; these cover the
mechanics through the loop's public ``run``.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from opspilot.investigation.store import StepStoreError
from tests.test_309_loop_parallel_impl import _parallel, _strip, _tool_rows, _turn

_WAIT_SECONDS = 2


def _committed(store):
    return {row["ordinal"] for row in _tool_rows(store)}


def _wait_for(predicate):
    deadline = time.monotonic() + _WAIT_SECONDS
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _tool_messages(model):
    return [
        (m["tool_call_id"], _strip([json.loads(m["content"])])[0])
        for m in model.calls[1].messages
        if m.get("role") == "tool"
    ]


def test_a_later_result_is_committed_while_an_earlier_call_is_in_flight():
    loop, request, model, transport, store = _parallel(turn=_turn(30, 60), cap=2)
    original_execute = loop.executor.execute
    saw_sibling = []

    def gated(req):
        if req.tool_index == 0:
            # Before #332 ordinal 1 waited behind ordinal 0 in memory.
            saw_sibling.append(_wait_for(lambda: 1 in _committed(store)))
        return original_execute(req)

    loop.executor.execute = gated
    outcome = loop.run(request)
    assert outcome.execution == "completed", outcome.handoff_reasons
    assert saw_sibling == [True]
    assert [row["ordinal"] for row in _tool_rows(store)] == [0, 1]

    serial, serial_request, serial_model, _, serial_store = _parallel(
        turn=_turn(30, 60), cap=1
    )
    serial_outcome = serial.run(serial_request)
    assert serial_outcome.execution == outcome.execution
    assert _strip([row["result"] for row in _tool_rows(store)]) == _strip(
        [row["result"] for row in _tool_rows(serial_store)]
    )
    assert _tool_messages(model) == _tool_messages(serial_model)


class _RefuseOrdinal:
    def __init__(self, store, ordinal):
        self._store = store
        self._ordinal = ordinal
        self.attempts = []

    def __getattr__(self, name):
        return getattr(self._store, name)

    def commit_tool(self, step_id, ordinal, result):
        self.attempts.append(ordinal)
        if ordinal == self._ordinal:
            raise StepStoreError("CONTROL_DENIED")
        return self._store.commit_tool(step_id, ordinal, result)


def test_an_early_commit_refused_by_the_store_halts_the_round():
    loop, request, model, transport, store = _parallel(turn=_turn(30, 60, 90), cap=2)
    refusing = _RefuseOrdinal(store, ordinal=1)
    loop.store = refusing
    original_execute = loop.executor.execute
    dispatched = []

    def gated(req):
        dispatched.append(req.tool_index)
        if req.tool_index == 0:
            _wait_for(lambda: 1 in refusing.attempts)
        return original_execute(req)

    loop.executor.execute = gated
    outcome = loop.run(request)
    assert outcome.handoff and "CONTROL_DENIED" in outcome.handoff_reasons
    # r2 (c): the first refused commit stops the round, here at index 1;
    # nothing is committed after it -- index 0 finishes inside the pool and
    # is dropped, as is index 2 if it took index 1's free slot in time.
    assert refusing.attempts == [1]
    assert sorted(dispatched[:2]) == [0, 1]
    assert _tool_rows(store) == []


def test_a_higher_result_seen_finished_stays_committed_when_a_lower_call_raises():
    loop, request, model, transport, store = _parallel(turn=_turn(30, 60), cap=2)
    original_execute = loop.executor.execute

    def scripted(req):
        if req.tool_index == 0:
            assert _wait_for(lambda: 1 in _committed(store))
            raise RuntimeError("boom")
        return original_execute(req)

    loop.executor.execute = scripted
    with pytest.raises(RuntimeError, match="boom"):
        loop.run(request)
    # r2 (b): recovery re-dispatches only the missing ordinal 0.
    assert [row["ordinal"] for row in _tool_rows(store)] == [1]


def test_tool_committed_reaches_the_store_once_per_ordinal():
    loop, request, model, transport, store = _parallel(turn=_turn(30, 60), cap=2)
    calls = []
    original_commit = store.commit_tool

    def recording(step_id, ordinal, result):
        calls.append(ordinal)
        return original_commit(step_id, ordinal, result)

    store.commit_tool = recording
    original_execute = loop.executor.execute
    release = threading.Event()

    def gated(req):
        if req.tool_index == 0:
            release.wait(timeout=_WAIT_SECONDS)
        else:
            try:
                return original_execute(req)
            finally:
                threading.Timer(0.2, release.set).start()
        return original_execute(req)

    loop.executor.execute = gated
    outcome = loop.run(request)
    assert outcome.execution == "completed", outcome.handoff_reasons
    # Completion order, and no second commit at ordinal 1's own turn.
    assert calls == [1, 0]
