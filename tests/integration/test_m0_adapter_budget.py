"""Actual PostgreSQL + synthetic SDK transport; no model/trace network traffic."""

import asyncio
import copy
import json
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx2
import pytest

from scripts.m0.adapters import History, SyntheticAdapter
from scripts.m0.budget import PostgresBudget
from scripts.m0.contracts import BudgetError, RunContext
from scripts.m0.postgres_lab import DSN
from scripts.m0.protocol import FIXTURE, ProtocolError
from scripts.m0.trace_adapter import TraceAdapter

pytestmark = pytest.mark.skipif(
    os.environ.get("M0_B_POSTGRES") != "1",
    reason="explicit synthetic PostgreSQL opt-in",
)


class Stream(httpx2.AsyncByteStream):
    def __init__(self, chunks, interrupt=False):
        self.chunks, self.interrupt = chunks, interrupt

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.interrupt:
            raise OSError("synthetic interruption")


def event(delta=None, finish=None, usage=None):
    return (
        "data: "
        + json.dumps(
            {
                "id": "synthetic-stream",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "deepseek-v4-pro",
                "choices": []
                if usage
                else [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
                "usage": usage,
            }
        )
        + "\n\n"
    ).encode()


def response(fixture, *, final=False, interrupted=False):
    if final:
        chunks = [event({"content": "synthetic candidate"}), event(finish="stop")]
    else:
        call = copy.deepcopy(fixture["assistant"]["tool_calls"][0])
        call["index"] = 0
        chunks = [
            event({"reasoning_content": "SYNTHETIC_PRIVATE_PROTOCOL_SENTINEL"}),
            event({"tool_calls": [call]}),
        ]
        if not interrupted:
            chunks.append(event(finish="tool_calls"))
    if not interrupted:
        chunks.extend(
            [
                event(
                    usage={
                        "prompt_tokens": 7,
                        "completion_tokens": 3,
                        "total_tokens": 10,
                    }
                ),
                b"data: [DONE]\n\n",
            ]
        )
    return httpx2.Response(
        200,
        headers={"content-type": "text/event-stream"},
        stream=Stream(chunks, interrupted),
    )


@pytest.fixture
def experiment():
    ledger = PostgresBudget(DSN)
    ledger.install()
    run = RunContext(
        uuid4(), uuid4(), "deepseek", datetime.now(timezone.utc) + timedelta(minutes=2)
    )
    ledger.initialize(run.experiment_id, 100, run.deadline)
    ledger.register_run(run)
    return ledger, run, json.loads(FIXTURE.read_text())


def test_two_rounds_settle_and_trace_does_not_change_ledger(experiment):
    ledger, run, fixture = experiment
    calls = []

    def transport(request):
        calls.append(json.loads(request.content))
        return response(fixture, final=len(calls) == 2)

    adapter = SyntheticAdapter(transport=httpx2.MockTransport(transport), budget=ledger)
    history = History(run)
    for _ in range(2):
        asyncio.run(
            adapter.round(
                run,
                history,
                fixture,
                upper_bound=50,
                synthetic_price=lambda usage: usage["total_tokens"],
            )
        )
    assert len(calls) == 2
    assert ledger.snapshot(run.experiment_id) == {
        "limit": 100,
        "settled": 20,
        "reserved": 0,
        "unknown": 0,
        "blocked": False,
    }
    assert (
        calls[1]["messages"][-2]["tool_calls"][0]["id"]
        == calls[1]["messages"][-1]["tool_call_id"]
    )

    class Trace:
        def upload(self, trace_id, payload):
            self.payload = payload

        def read(self, trace_id):
            return self.payload

    trace = Trace()
    result = TraceAdapter(trace).export(
        run,
        fixture
        | {"run_id": str(run.run_id), "status": "completed", "request_count": 2},
    )
    assert result.status == "completed"
    assert "SYNTHETIC_PRIVATE_PROTOCOL_SENTINEL" not in json.dumps(trace.payload)
    assert ledger.snapshot(run.experiment_id)["settled"] == 20


def test_interrupted_stream_retains_cost_across_new_run(experiment):
    ledger, run, fixture = experiment
    calls = []

    def transport(request):
        calls.append(request)
        return response(fixture, interrupted=True)

    adapter = SyntheticAdapter(transport=httpx2.MockTransport(transport), budget=ledger)
    history = History(run)
    with pytest.raises(ProtocolError, match="MODEL_STREAM_FAILED"):
        asyncio.run(
            adapter.round(
                run, history, fixture, upper_bound=80, synthetic_price=lambda usage: 1
            )
        )
    assert ledger.snapshot(run.experiment_id)["unknown"] == 80
    another = RunContext(run.experiment_id, uuid4(), run.provider, run.deadline)
    recovered = PostgresBudget(DSN)
    recovered.register_run(another)
    with pytest.raises(BudgetError, match="BUDGET_EXHAUSTED"):
        asyncio.run(
            adapter.round(
                another,
                History(another),
                fixture,
                upper_bound=21,
                synthetic_price=lambda usage: 1,
            )
        )
    assert len(calls) == 1
    assert history.messages(run) == []


def test_database_failure_prevents_transport(experiment):
    _, run, fixture = experiment
    calls = []
    # Same dedicated server, deterministically absent database; no other port is probed.
    missing = PostgresBudget(
        DSN.replace("dbname=m0_budget", "dbname=m0_absent_" + uuid4().hex)
    )
    adapter = SyntheticAdapter(
        transport=httpx2.MockTransport(lambda r: calls.append(r)), budget=missing
    )
    with pytest.raises(BudgetError, match="STORAGE_UNAVAILABLE"):
        asyncio.run(
            adapter.round(
                run,
                History(run),
                fixture,
                upper_bound=10,
                synthetic_price=lambda usage: 1,
            )
        )
    assert calls == []
