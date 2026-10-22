"""#309 durable crash/recovery acceptance (U-3, R-9, R-12).

The first attempt runs a two-tool batch through the fixture profile, then a
controlled executor exception is raised after ordinal 1 has completed its
external read.  A fresh runner/worker resumes the same PostgreSQL Run.  The
test observes only durable rows, the tool ledger, the final outcome, and the
transport calls.
"""

from __future__ import annotations

import os
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

import opspilot.tools.fixture as fixture_module
from opspilot.investigation.runner import InvestigationRunner
from opspilot.persistence import DurableStore
from opspilot.tools import TransportResponse
from opspilot.tools.executor import TransportRequest
from opspilot.tools.fixture import (
    FIXTURE_TOOL,
    fixture_executor_factory,
    fixture_face,
    fixture_versions,
)
from opspilot.web import DurableClock, DurableEventLog, DurableEvidenceStore
from opspilot.worker import Worker
from scripts.m0.postgres_lab import DSN
from tests.m1_investigation_support import (
    ScriptedModel,
    reply,
    report_from_transcript,
    tool_call,
)
from tests.m1_tool_support import WINDOW_START, body

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required",
)

LEASE_SECONDS = 1


class Crash(RuntimeError):
    pass


class _TransportState:
    def __init__(self) -> None:
        self.requests: list[TransportRequest] = []
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.phase = "initial"
        self.initial_barrier = threading.Barrier(2)


class _BatchTransport:
    state: _TransportState

    def fetch(self, request: TransportRequest) -> TransportResponse:
        state = type(self).state
        with state.lock:
            state.requests.append(request)
            state.active += 1
            state.max_active = max(state.max_active, state.active)
            initial = state.phase == "initial" and len(state.requests) <= 2
        try:
            if initial:
                state.initial_barrier.wait(timeout=5)
            return TransportResponse(
                body=body([{"metric": "checkout", "value": 3}]),
                data_as_of=WINDOW_START,
            )
        finally:
            with state.lock:
                state.active -= 1


class _CrashAfterSecondExecutor:
    def __init__(self, delegate, state: _TransportState) -> None:
        self._delegate = delegate
        self._state = state
        self.scope = delegate.scope

    @property
    def operations_used(self):
        return self._delegate.operations_used

    @property
    def tool_seconds_used(self):
        return self._delegate.tool_seconds_used

    def execute(self, request):
        outcome = self._delegate.execute(request)
        if request.tool_index == 1 and self._state.phase == "initial":
            self._state.phase = "crashed"
            raise Crash("attempt interrupted after ordinal 1 completed")
        return outcome


def _batch_reply():
    return reply(
        tool_calls=[
            tool_call(
                "call-a",
                name=FIXTURE_TOOL,
                arguments='{"expr":"rate(http_errors[5m])","step_seconds":30}',
            ),
            tool_call(
                "call-b",
                name=FIXTURE_TOOL,
                arguments='{"expr":"rate(http_errors[5m])","step_seconds":60}',
            ),
        ],
        finish="tool_calls",
    )


def _runner(store, evidence, events, clock, factory, replies):
    return InvestigationRunner(
        store=store,
        worker=Worker.create(store, dict(fixture_versions())),
        model=ScriptedModel(replies),
        executor_factory=factory,
        clock=clock,
        lease_seconds=LEASE_SECONDS,
        events=events,
        evidence=evidence,
        tool_parallelism=2,
    )


def test_batch_crash_resume_replays_only_pending_ordinals(monkeypatch):
    state = _TransportState()
    _BatchTransport.state = state
    monkeypatch.setattr(fixture_module, "_CannedTransport", _BatchTransport)

    store = DurableStore(DSN)
    store.install()
    events = DurableEventLog(store)
    events.install()
    evidence = DurableEvidenceStore(store)
    evidence.install()
    clock = DurableClock(store)
    incident, run = uuid4(), uuid4()
    deadline = datetime.now(timezone.utc) + timedelta(minutes=5)
    input_snapshot = fixture_face().input_for(
        run_id=str(run),
        question="Why is checkout erroring?",
        target_id="checkout-prod",
        deadline=deadline,
        model_requests=5,
    )
    store.accept(
        incident,
        run,
        f"m1-309-crash-resume-{incident}",
        deadline=deadline,
        budget_limit=10,
        versions=fixture_versions(),
        input=input_snapshot.as_json(),
    )

    base_factory = fixture_executor_factory(store, evidence=evidence, clock=clock)

    def crashing_factory(lease, input):
        return _CrashAfterSecondExecutor(base_factory(lease, input), state)

    with pytest.raises(Crash, match="ordinal 1"):
        _runner(
            store, evidence, events, clock, crashing_factory, [_batch_reply()]
        ).resume(incident)

    after_crash = store.rebuild(incident)
    live_steps = [
        step for step in after_crash["steps"] if step["status"] != "late_result"
    ]
    tool_step = next(
        step for step in live_steps if step["response"].get("kind") is None
    )
    committed = {item["ordinal"]: item["result"] for item in tool_step["tool_results"]}
    pending = sorted(item["ordinal"] for item in after_crash["pending_tools"])
    assert sorted(committed) in ([], [0])
    assert set(committed) | set(pending) == {0, 1}
    assert set(committed).isdisjoint(pending)
    assert len(state.requests) == 2

    committed_usage = store.run_usage(run)
    assert committed_usage["tool_operations_used"] == 2

    time.sleep(LEASE_SECONDS + 0.3)
    state.phase = "replay"
    state.max_active = 0
    resumed = _runner(
        store,
        evidence,
        events,
        clock,
        base_factory,
        [report_from_transcript],
    ).resume(incident)
    assert resumed.status == "published", resumed
    assert resumed.replayed_tools == len(pending)
    assert resumed.replayed_tools <= 1  # at most N-1 repeated external reads
    assert state.max_active <= 1  # RecoverySession replay is serialized (R-9)

    final = store.rebuild(incident)
    assert final["pending_tools"] == []
    final_step = next(
        step
        for step in final["steps"]
        if step["status"] != "late_result" and step["response"].get("kind") is None
    )
    final_results = {
        item["ordinal"]: item["result"] for item in final_step["tool_results"]
    }
    assert sorted(final_results) == [0, 1]
    for ordinal, result in committed.items():
        assert final_results[ordinal] == result

    assert len(state.requests) == 2 + resumed.replayed_tools
    operation_ids = [request.operation_id for request in state.requests]
    assert max(Counter(operation_ids).values()) <= 2
    usage = store.run_usage(run)
    assert usage["tool_operations_used"] == len(state.requests)
    assert usage["tool_seconds_used"] >= 0
