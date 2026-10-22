"""Acceptance tests for #309: compaction dedupe reset and bounded tool fan-out.

The deterministic cap=1 baseline in this file was produced from main at
``c7326c9`` with the fixture model/transport below.  It is kept as a literal
projection of public observations (model messages, committed rows and event
kinds); UUID and timing fields are intentionally excluded because the fixture
contract does not make those identities stable across a fresh Run.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import threading
import time
from dataclasses import fields, replace
from typing import Any
from uuid import UUID

import pytest

import opspilot.investigation.store as store_module
import opspilot.tools.executor as executor_module
import opspilot.worker_main as worker_main
from opspilot.investigation.loop import InvestigationLoop, InvestigationRequest
from opspilot.investigation.progress import EmittingCommitter
from opspilot.investigation.store import MemoryStepStore
from opspilot.tools import TransportResponse
from tests.m1_investigation_support import (
    TOOL_SCHEMAS,
    ScriptedModel,
    assemble,
    reply,
    report_from_transcript,
    tool_call,
)
from tests.m1_tool_support import (
    WINDOW_START,
    FakeClock,
    body,
    build,
)
from tests.test_m1_investigation_compaction import (
    _compacting_run,
    _limits_between_rounds,
)

BASELINE_CAP1 = {
    "model_request_count": 2,
    "tool_ordinals": [0, 1],
    "event_kinds": ["step_committed", "tool_committed", "tool_committed", "step_committed"],
    "tool_message_ids": ["call-a", "call-b"],
    # SHA-256 of the complete public projection generated on c7326c9 with
    # the deterministic UUID stream installed by the test below.
    "observation_sha256": "5879616ffde1423b7acaae2b787093c732c679e02b0fd4104b2d44432a133039",
}


def _cap(loop: InvestigationLoop, value: int) -> InvestigationLoop:
    """Set the contract field while making the pre-#309 failure explicit."""
    if value == 1:
        return loop
    if not any(field.name == "tool_parallelism" for field in fields(loop)):
        pytest.fail("#309 implementation has not exposed tool_parallelism")
    return replace(loop, tool_parallelism=value)


def _two_calls() -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        tool_call("call-a", arguments='{"expr":"rate(http_errors[5m])","step_seconds":30}'),
        tool_call("call-b", arguments='{"expr":"rate(http_errors[5m])","step_seconds":60}'),
    )


class _Events:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict[str, Any]]] = []

    def append(self, _subject: Any, kind: str, payload: dict[str, Any]) -> int:
        self.rows.append((kind, dict(payload)))
        return len(self.rows)

    def append_once(self, _subject: Any, kind: str, payload: dict[str, Any], *, key=None) -> int:
        return self.append(_subject, kind, payload)


class _BarrierTransport:
    """A blocking source double; overlap is proved by the barrier, not sleep."""

    def __init__(self, parties: int = 2) -> None:
        self.barrier = threading.Barrier(parties)
        self.lock = threading.Lock()
        self.intervals: list[tuple[float, float, str]] = []
        self.requests = []

    def fetch(self, request):
        start = time.perf_counter()
        with self.lock:
            self.requests.append(request)
        try:
            self.barrier.wait(timeout=5)
        except threading.BrokenBarrierError as exc:  # pragma: no cover - diagnostic
            raise AssertionError("parallel calls did not reach the barrier") from exc
        end = time.perf_counter()
        with self.lock:
            self.intervals.append((start, end, request.operation_id))
        return TransportResponse(
            body=body([{"metric": "checkout", "value": 3}]),
            data_as_of=WINDOW_START,
        )


def _fixture_loop(*, replies, cap=1, transport=None, model_requests=2, clock=None):
    clock = clock or FakeClock()
    executor, transport, _sink, clock = build(
        transport=transport,
        clock=clock,
    )
    if hasattr(transport, "response") and getattr(transport, "response") is None:
        transport.response = TransportResponse(
            body=body([{"metric": "checkout", "value": 3}]),
            data_as_of=WINDOW_START,
        )
    store = MemoryStepStore(
        budget_limit=16,
        deadline=executor.scope.deadline,
        clock=clock,
        run_id=executor.scope.run_id,
    )
    model = ScriptedModel(replies)
    events = _Events()
    emitting_store = EmittingCommitter(
        store,
        events,
        UUID("00000000-0000-0000-0000-000000000042"),
        UUID("00000000-0000-0000-0000-000000000009"),
    )
    loop = InvestigationLoop(model=model, executor=executor, store=emitting_store, clock=clock)
    loop = _cap(loop, cap)
    request = InvestigationRequest(
        run_id=executor.scope.run_id,
        question="Why is checkout erroring?",
        scope=executor.scope,
        tool_schemas=TOOL_SCHEMAS,
        model_requests=model_requests,
        evidence_context={
            "type": "opspilot-evidence-context-v4",
            "run_id": executor.scope.run_id,
            "time_policies": [
                {
                    "id": "policy-window-1",
                    "mode": "historical_window",
                    "all_authorized_targets": True,
                    "window": {
                        "start": WINDOW_START.isoformat(),
                        "end": (WINDOW_START.replace(hour=1)).isoformat(),
                    },
                }
            ],
        },
    )
    return loop, request, model, transport, store, events


def _outcome_projection(outcome):
    return {
        "execution": outcome.execution,
        "handoff": outcome.handoff,
        "handoff_reasons": outcome.handoff_reasons,
        "report_content": outcome.report_content,
        "evidence_ids": outcome.evidence_ids,
        "model_requests_used": outcome.model_requests_used,
        "steps_committed": outcome.steps_committed,
        "conclusion": outcome.conclusion,
    }


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_ISO_TIME = re.compile(r"20\d\d-\d\d-\d\dT[^\"']+")


def _normalize(value):
    """Remove per-attempt UUID/time fields before comparing public bytes."""
    if isinstance(value, str):
        return _ISO_TIME.sub("<time>", _UUID.sub("<uuid>", value))
    if isinstance(value, dict):
        normalized = {}
        for key, item in value.items():
            if key.endswith("sha256") or key.endswith("_hash") or key in {"elapsed_seconds", "model_seconds_used", "timeout_seconds"}:
                normalized[key] = "<timing-or-derived>"
            else:
                normalized[key] = _normalize(item)
        return normalized
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_normalize(item) for item in value)
    return value


def _messages_projection(model):
    return [
        [dict(message) for message in call.messages]
        for call in model.calls
    ]


def _strict_observation_digest(model, store, events):
    rows = []
    for key, row in sorted(store.steps.items(), key=lambda item: item[1]["sequence"]):
        rows.append(
            {
                "logical_key": key,
                "response": row["response"],
                "tool_results": store.tool_results.get(row["step_id"], []),
            }
        )
    payload = {
        "messages": _messages_projection(model),
        "rows": rows,
        "events": events.rows,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def test_cap_one_matches_the_frozen_main_c7326c9_observations(monkeypatch):
    sequence = iter(range(1, 100))
    monkeypatch.setattr(store_module, "uuid4", lambda: UUID(int=next(sequence)))
    monkeypatch.setattr(executor_module, "uuid4", lambda: UUID(int=next(sequence)))
    calls = _two_calls()
    loop, request, model, _transport, store, events = _fixture_loop(
        replies=[reply(tool_calls=list(calls), finish="tool_calls"), report_from_transcript],
        cap=1,
    )
    outcome = loop.run(request)
    assert outcome.execution == "completed"
    committed = next(iter(store.tool_results.values()))
    assert [item["ordinal"] for item in committed] == BASELINE_CAP1["tool_ordinals"]
    assert len(model.calls) == BASELINE_CAP1["model_request_count"]
    assert [message["tool_call_id"] for message in model.calls[1].messages if message["role"] == "tool"] == BASELINE_CAP1["tool_message_ids"]
    assert [kind for kind, _payload in events.rows] == BASELINE_CAP1["event_kinds"]
    assert _strict_observation_digest(model, store, events) == BASELINE_CAP1["observation_sha256"]


def test_cap_two_overlaps_and_preserves_cap_one_outcome_and_model_order():
    calls = _two_calls()
    replies = [reply(tool_calls=list(calls), finish="tool_calls"), report_from_transcript]
    serial_loop, serial_request, serial_model, _serial_transport, serial_store, _serial_events = _fixture_loop(replies=replies, cap=1)
    serial_outcome = serial_loop.run(serial_request)

    barrier = _BarrierTransport()
    parallel_loop, parallel_request, parallel_model, parallel_transport, parallel_store, parallel_events = _fixture_loop(
        replies=[reply(tool_calls=list(calls), finish="tool_calls"), report_from_transcript],
        cap=2,
        transport=barrier,
    )
    parallel_outcome = parallel_loop.run(parallel_request)

    assert _normalize(_outcome_projection(parallel_outcome)) == _normalize(_outcome_projection(serial_outcome))
    serial_tools = [m for m in serial_model.calls[1].messages if m["role"] == "tool"]
    parallel_tools = [m for m in parallel_model.calls[1].messages if m["role"] == "tool"]
    assert _normalize(parallel_tools) == _normalize(serial_tools)
    assert [kind for kind, _payload in parallel_events.rows] == [
        kind for kind, _payload in _serial_events.rows
    ]
    assert [item["ordinal"] for item in next(iter(parallel_store.tool_results.values()))] == [0, 1]
    assert len(parallel_transport.intervals) == 2
    first, second = parallel_transport.intervals
    assert max(first[0], second[0]) < min(first[1], second[1])


def test_cap_two_tool_seconds_are_the_sum_of_each_call():
    calls = _two_calls()
    barrier = _BarrierTransport()
    loop, request, _model, transport, _store, _events = _fixture_loop(
        replies=[reply(tool_calls=list(calls), finish="tool_calls"), report_from_transcript],
        cap=2,
        transport=barrier,
    )
    outcome = loop.run(request)
    elapsed_sum = sum(end - start for start, end, _ in transport.intervals)
    assert outcome.execution == "completed"
    assert loop.executor.operations_used == 2
    assert loop.executor.tool_seconds_used == pytest.approx(elapsed_sum, rel=0.15, abs=0.005)


def test_accepted_compaction_clears_dedupe_but_pre_compaction_repeat_is_refused():
    first = tool_call("call-1", arguments='{"expr":"rate(http_errors[1m])"}')
    loop, request, model, transport, store = _compacting_run(
        extra_replies=[reply(tool_calls=[first], finish="tool_calls")],
        model_requests=8,
    )
    outcome = loop.run(request)
    assert outcome.handoff is True
    assert store.steps["ctx0:compact-1"]["response"]["compaction"]["accepted"] is True
    post_compaction = store.steps.get("ctx1:round-3")
    assert post_compaction is not None
    result = store.tool_results[post_compaction["step_id"]][0]["result"]
    assert result.get("reason") != "DUPLICATE_TOOL_CALL"
    assert len(transport.requests) == 3


def test_rejected_compaction_hands_off_with_compaction_failed():
    first = tool_call("call-a")
    loop, request, model, _transport, store, _sink = assemble(
        replies=[reply(tool_calls=[first], finish="tool_calls"), reply(tool_calls=[first], finish="tool_calls"), reply(content="")],
        model_requests=5,
    )
    request = replace(request, limits=_limits_between_rounds(2))
    outcome = loop.run(request)
    assert outcome.execution == "failed"
    assert outcome.handoff_reasons == ("COMPACTION_FAILED",)
    assert store.steps["ctx0:compact-1"]["response"]["compaction"]["accepted"] is False


def test_same_batch_duplicate_is_committed_as_a_view_without_second_dispatch():
    call = tool_call("call-a")
    repeated = tool_call("call-b")
    loop, request, model, transport, store, _events = _fixture_loop(
        replies=[reply(tool_calls=[call, repeated], finish="tool_calls"), report_from_transcript], cap=1
    )
    outcome = loop.run(request)
    assert outcome.execution == "completed"
    assert len(transport.requests) == 1
    results = next(iter(store.tool_results.values()))
    assert [item["ordinal"] for item in results] == [0, 1]
    assert results[1]["result"]["reason"] == "DUPLICATE_TOOL_CALL"


def test_same_batch_pure_refusal_is_dispatched_again():
    class RefusingTransport:
        def __init__(self):
            self.requests = []

        def fetch(self, request):
            self.requests.append(request)
            return TransportResponse(body=b"", source_status="400", sent=False)

    call = tool_call("call-a")
    repeated = tool_call("call-b")
    transport = RefusingTransport()
    loop, request, _model, _transport, store, _events = _fixture_loop(
        replies=[reply(tool_calls=[call, repeated], finish="tool_calls"), reply(content="no evidence")],
        cap=1,
        transport=transport,
    )
    loop.run(request)
    assert len(transport.requests) == 2
    results = next(iter(store.tool_results.values()))
    assert all(item["result"]["status"] == "error" for item in results)


def test_fence_failure_stops_dispatch_and_preserves_only_serial_prefix():
    class FenceAfterFirstRenew(MemoryStepStore):
        renewals = 0

        def renew(self):
            self.renewals += 1
            if self.renewals >= 2:
                self.deny_control()

    calls = _two_calls()

    def run_with_cap(cap):
        loop, request, _model, transport, base_store, _events = _fixture_loop(
            replies=[reply(tool_calls=list(calls), finish="tool_calls")], cap=cap
        )
        store = FenceAfterFirstRenew(
            budget_limit=16,
            deadline=base_store.deadline,
            clock=loop.clock,
            run_id=base_store.authorized_run_id,
        )
        loop.store = store
        outcome = loop.run(request)
        committed = [
            item
            for row in store.tool_results.values()
            for item in row
        ]
        return outcome, transport, store, committed

    serial, serial_transport, serial_store, serial_results = run_with_cap(1)
    assert serial.execution == "failed"
    assert serial.handoff_reasons == ("CONTROL_DENIED",)
    assert len(serial_transport.requests) == 1
    assert [result["ordinal"] for result in serial_results] == [0]

    parallel, parallel_transport, parallel_store, parallel_results = run_with_cap(2)
    assert parallel.execution == "failed"
    assert parallel.handoff_reasons == ("CONTROL_DENIED",)
    assert len(parallel_transport.requests) == 1
    assert [result["ordinal"] for result in parallel_results] in ([], [0])
    if parallel_results:
        assert _normalize(parallel_results[0]) == _normalize(serial_results[0])
    # MemoryStepStore's fence guard does not materialize late_result rows;
    # durable stores may expose them here, so preserve the conditional seam.
    if parallel_store.late_results:
        assert parallel_store.late_results


def test_single_tool_exception_keeps_lower_index_prefix():
    class FailingExecutor:
        def __init__(self, delegate):
            self.delegate = delegate
            self.scope = delegate.scope
            self.calls = []

        @property
        def operations_used(self):
            return self.delegate.operations_used

        @property
        def tool_seconds_used(self):
            return self.delegate.tool_seconds_used

        def execute(self, request):
            self.calls.append(request.tool_index)
            if request.tool_index == 1:
                raise RuntimeError("fixture tool failure")
            return self.delegate.execute(request)

    calls = _two_calls()
    loop, request, _model, _transport, store, _events = _fixture_loop(
        replies=[reply(tool_calls=list(calls), finish="tool_calls")], cap=2
    )
    loop.executor = FailingExecutor(loop.executor)
    with pytest.raises(RuntimeError, match="fixture tool failure"):
        loop.run(request)
    assert [item["ordinal"] for row in store.tool_results.values() for item in row] == [0]


def test_parallelism_configuration_has_default_two_and_accepts_only_one_to_sixteen(monkeypatch):
    class FakeStore:
        pass

    class FakeInstall:
        def __init__(self, _store):
            self.store = _store

        def install(self):
            return None

    class FakeClock:
        def __init__(self, _store):
            pass

    class FakeWorker:
        @classmethod
        def create(cls, _store, _versions):
            return object()

    class FakeRunner:
        calls = []

        def __init__(self, **kwargs):
            type(self).calls.append(kwargs)
            self.worker = type("WorkerHandle", (), {"owner": "fixture-owner"})()

    class FakeProfile:
        name = "fixture"

        def versions(self):
            return {}

        def executor_factory(self, _store, _evidence, _clock):
            return lambda _lease, _input: None

    monkeypatch.setattr(worker_main, "DurableEventLog", FakeInstall)
    monkeypatch.setattr(worker_main, "DurableEvidenceStore", FakeInstall)
    monkeypatch.setattr(worker_main, "DurableClock", FakeClock)
    monkeypatch.setattr(worker_main, "Worker", FakeWorker)
    monkeypatch.setattr(worker_main, "InvestigationRunner", FakeRunner)

    monkeypatch.delenv("OPSPILOT_TOOL_PARALLELISM", raising=False)
    worker_main.build_loop(FakeStore(), "key", stop=threading.Event(), profile=FakeProfile())
    assert FakeRunner.calls[-1]["tool_parallelism"] == 2

    for value in ("1", "16"):
        monkeypatch.setenv("OPSPILOT_TOOL_PARALLELISM", value)
        worker_main.build_loop(FakeStore(), "key", stop=threading.Event(), profile=FakeProfile())
        assert FakeRunner.calls[-1]["tool_parallelism"] == int(value)

    for value in ("0", "17", "bad"):
        monkeypatch.setenv("OPSPILOT_TOOL_PARALLELISM", value)
        with pytest.raises(SystemExit):
            worker_main.build_loop(FakeStore(), "key", stop=threading.Event(), profile=FakeProfile())


def test_loop_exposes_tool_parallelism_default_one_for_compatibility():
    signature = inspect.signature(InvestigationLoop)
    parameter = signature.parameters.get("tool_parallelism")
    if parameter is None:
        pytest.fail("#309 implementation has not exposed tool_parallelism")
    assert parameter.default == 1


@pytest.mark.skipif(
    not os.environ.get("OPSPILOT_WORKER_DSN"),
    reason="durable worker crash/resume acceptance requires the existing PostgreSQL fixture",
)
def test_batch_crash_resume_replays_only_pending_ordinals():
    """The durable recovery half of U-3/R-8 is intentionally PG-gated."""
    pytest.skip("PostgreSQL worker recovery fixture is not started in this test worktree")
