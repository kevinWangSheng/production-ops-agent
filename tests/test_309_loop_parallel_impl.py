"""#309 implementer tests: compaction resets dedupe; one turn's tools in bounded
parallel (docs/tasks/2026-10-22-309-loop-dedupe-parallel-contract.md).

The independent acceptance tests live on their own branch; these cover the
mechanics the implementation relies on, through the loop's public ``run``.
"""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from uuid import uuid4

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from opspilot import tracing
from opspilot.investigation import loop as loop_module
from opspilot.investigation.store import StepStoreError
from opspilot.tools import TransportResponse
from tests.m1_investigation_support import (
    assemble,
    reply,
    report_from_transcript,
    tool_call,
)
from tests.m1_tool_support import WINDOW_START, body
from tests.test_m1_investigation_compaction import _compacting_run, _summary
from tests.test_tracing import VERSIONS, lab_env


def _args(step):
    return json.dumps({"expr": "rate(http_errors[5m])", "step_seconds": step})


def _turn(*steps, ids=None):
    calls = [
        tool_call(call_id=(ids[i] if ids else f"call-{i}"), arguments=_args(step))
        for i, step in enumerate(steps)
    ]
    return reply(tool_calls=calls, finish="tool_calls")


class _BarrierTransport:
    """Every fetch waits until ``parties`` fetches are inside at once."""

    def __init__(self, parties):
        self.barrier = threading.Barrier(parties, timeout=5)
        self.requests = []
        self._lock = threading.Lock()
        self.response = TransportResponse(
            body=body([{"metric": "checkout", "value": 3}]), data_as_of=WINDOW_START
        )

    def fetch(self, request):
        with self._lock:
            self.requests.append(request)
        self.barrier.wait()
        return self.response


def _tool_rows(store):
    rows = []
    for results in store.tool_results.values():
        rows.extend(results)
    return rows


def _parallel(*, turn, cap, transport=None, model_requests=2):
    loop, request, model, fake, store, sink = assemble(
        replies=[turn, report_from_transcript], model_requests=model_requests
    )
    loop.tool_parallelism = cap
    if transport is not None:
        loop.executor._transport = transport
    return loop, request, model, fake, store


def test_cap_two_overlaps_calls_and_keeps_index_order():
    # The barrier only opens when both fetches are inside at once: serial
    # dispatch would break it (SOURCE_ERROR) instead of returning ok.
    transport = _BarrierTransport(2)
    loop, request, model, _, store = _parallel(
        turn=_turn(30, 60), cap=2, transport=transport
    )
    outcome = loop.run(request)
    assert outcome.execution == "completed", outcome.handoff_reasons
    assert len(transport.requests) == 2
    rows = _tool_rows(store)
    assert [row["ordinal"] for row in rows] == [0, 1]
    assert all(row["result"]["status"] == "ok" for row in rows)
    tool_messages = [m for m in model.calls[1].messages if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_messages] == ["call-0", "call-1"]


def test_cap_one_never_builds_a_pool(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("cap=1 must not create a thread pool")

    monkeypatch.setattr(loop_module, "ThreadPoolExecutor", refuse)
    loop, request, model, transport, store = _parallel(turn=_turn(30, 60), cap=1)
    outcome = loop.run(request)
    assert outcome.execution == "completed"
    assert [row["ordinal"] for row in _tool_rows(store)] == [0, 1]
    assert len(transport.requests) == 2


_ID_KEYS = ("operation_id", "evidence_id", "duplicate_of_evidence_id", "message")


def _strip(views):
    """Views without the per-Run ids (step ids are fresh uuids per Run)."""
    return [{k: v for k, v in view.items() if k not in _ID_KEYS} for view in views]


def test_cap_two_commits_what_cap_one_commits():
    runs = {}
    for cap in (1, 2):
        loop, request, model, transport, store = _parallel(
            turn=_turn(30, 30, 60, 30, 90), cap=cap
        )
        outcome = loop.run(request)
        assert outcome.execution == "completed"
        runs[cap] = (
            [row["ordinal"] for row in _tool_rows(store)],
            _strip([row["result"] for row in _tool_rows(store)]),
            len(transport.requests),
            [
                m["tool_call_id"]
                for m in model.calls[1].messages
                if m.get("role") == "tool"
            ],
        )
    assert runs[1][0] == runs[2][0] == [0, 1, 2, 3, 4]
    assert runs[1][1:] == runs[2][1:]
    # Index 1 and 3 repeat index 0, which produced evidence: not dispatched.
    assert runs[2][2] == 3
    assert [view.get("reason") for view in runs[2][1]] == [
        None,
        "DUPLICATE_TOOL_CALL",
        None,
        "DUPLICATE_TOOL_CALL",
        None,
    ]


def test_a_repeat_of_a_pure_refusal_in_the_same_turn_runs_again():
    runs = {}
    for cap in (1, 2):
        # A string step is refused by the gateway before any dispatch.
        loop, request, model, transport, store = _parallel(
            turn=_turn("x", "x", 60), cap=cap
        )
        loop.run(request)
        rows = [row["result"] for row in _tool_rows(store)]
        assert not rows[0].get("evidence_id")
        assert rows[1].get("reason") != "DUPLICATE_TOOL_CALL"
        assert rows[1].get("reason") == rows[0].get("reason")
        runs[cap] = (_strip(rows), len(transport.requests))
    assert runs[1] == runs[2]


class _FailingRenewal:
    def __init__(self, store, fail_on):
        self._store = store
        self._fail_on = fail_on
        self.calls = 0

    def __getattr__(self, name):
        return getattr(self._store, name)

    def renew(self):
        self.calls += 1
        if self.calls == self._fail_on:
            raise StepStoreError("CONTROL_DENIED")
        return self._store.renew()


def test_a_fence_failure_mid_turn_keeps_the_committed_prefix():
    loop, request, model, transport, store = _parallel(turn=_turn(30, 60, 90), cap=2)
    # renew() runs once per dispatch only: fail the third dispatch's renewal.
    loop.store = _FailingRenewal(store, fail_on=3)
    outcome = loop.run(request)
    assert outcome.handoff and "CONTROL_DENIED" in outcome.handoff_reasons
    assert [row["ordinal"] for row in _tool_rows(store)] == [0, 1]
    assert len(transport.requests) == 2


def test_an_unexpected_exception_commits_earlier_results_and_reraises():
    loop, request, model, transport, store = _parallel(turn=_turn(30, 60, 90), cap=2)
    original_execute = loop.executor.execute

    def explode_on_second(req):
        if req.tool_index == 1:
            raise RuntimeError("boom")
        return original_execute(req)

    loop.executor.execute = explode_on_second
    with pytest.raises(RuntimeError, match="boom"):
        loop.run(request)
    assert [row["ordinal"] for row in _tool_rows(store)] == [0]


def test_an_accepted_compaction_lets_the_same_call_run_again():
    repeat = reply(
        tool_calls=[
            tool_call(
                call_id="call-again", arguments='{"expr":"rate(http_errors[1m])"}'
            )
        ],
        finish="tool_calls",
    )
    # The repeat round is followed by a second compaction before the final.
    loop, request, model, transport, store = _compacting_run(
        extra_replies=[repeat, _summary()]
    )
    outcome = loop.run(request)
    assert outcome.execution == "completed", outcome.handoff_reasons
    views = [row["result"] for row in _tool_rows(store)]
    assert len(views) == 3
    assert views[2].get("reason") != "DUPLICATE_TOOL_CALL"
    assert views[2]["status"] == "ok"
    assert views[2]["evidence_id"] != views[0]["evidence_id"]
    assert len(transport.requests) == 3
    # The same call as round 1, run again after the accepted compaction.
    assert views[0]["query"] == views[2]["query"]


def test_worker_tool_spans_hang_under_the_run_span():
    exporter = InMemorySpanExporter()
    tracer = tracing.configure(lab_env(), exporter=exporter)
    try:
        transport = _BarrierTransport(2)
        loop, request, _, _, _ = _parallel(
            turn=_turn(30, 60), cap=2, transport=transport
        )
        with tracer.run(incident_id=uuid4(), run_id=uuid4(), versions=VERSIONS):
            outcome = loop.run(request)
        assert outcome.execution == "completed"
        tracer.shutdown()
        spans = exporter.get_finished_spans()
        run = next(s for s in spans if s.name == "opspilot.run")
        tools = [s for s in spans if s.name.startswith("tool:")]
        assert len(tools) == 2
        assert {s.parent.span_id for s in tools} == {run.context.span_id}
        assert sorted(
            s.attributes["langsmith.metadata.tool_call_seq"] for s in tools
        ) == [
            1,
            2,
        ]
        assert run.attributes["opspilot.run.tool_calls"] == 2
    finally:
        tracing.reset()


@pytest.mark.parametrize("value", ["0", "17", "two", "-1"])
def test_worker_refuses_an_invalid_tool_parallelism(monkeypatch, value):
    from opspilot import worker_main

    monkeypatch.setenv("OPSPILOT_TOOL_PARALLELISM", value)
    with pytest.raises(SystemExit):
        worker_main._tool_parallelism()


def test_worker_tool_parallelism_defaults_to_two(monkeypatch):
    from opspilot import worker_main

    monkeypatch.delenv("OPSPILOT_TOOL_PARALLELISM", raising=False)
    assert worker_main._tool_parallelism() == 2
    monkeypatch.setenv("OPSPILOT_TOOL_PARALLELISM", "16")
    assert worker_main._tool_parallelism() == 16


@pytest.mark.parametrize("value", [0, 17, True, 2.0])
def test_the_loop_refuses_an_invalid_tool_parallelism(value):
    loop, *_ = assemble(replies=[])
    with pytest.raises(ValueError, match="INVALID_TOOL_PARALLELISM"):
        replace(loop, tool_parallelism=value)


def test_executor_counters_are_exact_under_concurrency():
    loop, request, _, transport, _, _ = assemble(replies=[])
    executor = loop.executor
    before = executor.operations_used
    from tests.m1_tool_support import request as tool_request

    barrier = threading.Barrier(8)

    def go(i):
        barrier.wait()
        executor.execute(tool_request(step_id=f"s-{i}"))

    threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert executor.operations_used - before == 8
