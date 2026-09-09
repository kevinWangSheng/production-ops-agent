import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx2
import pytest

from scripts.m0.adapters import History, SyntheticAdapter
from scripts.m0.contracts import BudgetError, Reservation, RunContext
from scripts.m0.protocol import FIXTURE, ProtocolError, tool_result

NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)


def context():
    return RunContext(uuid4(), uuid4(), "deepseek", NOW + timedelta(seconds=60))


class RecordingBudget:
    def __init__(self, events, *, created=True, denied=False):
        self.events, self.created, self.denied = events, created, denied

    def reserve(self, request, upper_bound):
        self.events.append(("reserve", request, upper_bound))
        if self.denied:
            raise BudgetError("BUDGET_EXHAUSTED")
        return Reservation(request, upper_bound, "reserved", None, self.created)

    def settle(self, request, actual):
        self.events.append(("settle", request, actual))

    def retain_unknown(self, request):
        self.events.append(("unknown", request))


class Bytes(httpx2.AsyncByteStream):
    def __init__(self, data, fail=False):
        self.data, self.fail = data, fail

    async def __aiter__(self):
        for part in self.data:
            yield part
        if self.fail:
            raise OSError("SYNTHETIC_PRIVATE_EXCEPTION")


def sse(delta, finish=None, usage=None):
    return (
        "data: "
        + json.dumps(
            {
                "id": "synthetic",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "deepseek-v4-flash",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                "usage": usage,
            }
        )
        + "\n\n"
    ).encode()


def stream_parts(*, arguments=None, finish="tool_calls", usage=True):
    fixture = json.loads(FIXTURE.read_text())
    call = copy.deepcopy(fixture["assistant"]["tool_calls"][0])
    call["index"] = 0
    if arguments is not None:
        call["function"]["arguments"] = arguments
    parts = [
        sse(
            {
                "role": "assistant",
                "reasoning_content": "SYNTHETIC_PRIVATE_PROTOCOL_SENTINEL",
            }
        ),
        sse({"tool_calls": [call]}),
        sse({}, finish),
    ]
    if usage:
        parts.append(
            (
                "data: "
                + json.dumps(
                    {
                        "id": "synthetic",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "deepseek-v4-flash",
                        "choices": [],
                        "usage": {
                            "prompt_tokens": 20,
                            "completion_tokens": 10,
                            "total_tokens": 30,
                        },
                    }
                )
                + "\n\n"
            ).encode()
        )
    return parts + [b"data: [DONE]\n\n"]


def setup(parts=None, *, fail=False, budget_options=None, clock=None):
    events, bodies = [], []

    def handler(request):
        events.append(("send",))
        bodies.append(json.loads(request.content))
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=Bytes(parts or stream_parts(), fail),
        )

    budget = RecordingBudget(events, **(budget_options or {}))
    adapter = SyntheticAdapter(
        transport=httpx2.MockTransport(handler),
        budget=budget,
        clock=clock or (lambda: NOW),
    )
    run = context()
    return adapter, run, History(run), json.loads(FIXTURE.read_text()), events, bodies


def execute(adapter, run, history, fixture, **kwargs):
    return asyncio.run(
        adapter.round(
            run,
            history,
            fixture,
            upper_bound=100,
            synthetic_price=lambda usage: usage["total_tokens"],
            **kwargs,
        )
    )


def test_normal_roundtrip_compression_and_independent_requests():
    adapter, run, history, fixture, events, bodies = setup()
    tools = []

    def tool(call, data):
        tools.append(call["id"])
        assert [e[0] for e in events][-1] == "settle"
        return tool_result(call, data)

    first = execute(adapter, run, history, fixture, tool=tool)
    second = execute(adapter, run, history, fixture, tool=tool)
    assert first.request.request_id != second.request.request_id
    assert first.request.run == second.request.run == run
    assert tools == ["call_m0_a", "call_m0_a"]
    assert [e[0] for e in events] == ["reserve", "send", "settle"] * 2
    group = bodies[1]["messages"][-2:]
    assert group[0]["reasoning_content"] == "SYNTHETIC_PRIVATE_PROTOCOL_SENTINEL"
    assert group[0]["tool_calls"][0]["id"] == group[1]["tool_call_id"]
    assert json.loads(group[1]["content"])["source"] == fixture["evidence"]["source"]
    assert len(history.messages(run, keep_groups=1)) == 2
    assert history.messages(run, keep_groups=0) == []
    assert len(history.messages(run)) == 4


@pytest.mark.parametrize(
    "parts,fail",
    [
        (stream_parts()[:2], False),
        (stream_parts()[:2], True),
        (stream_parts(finish="length"), False),
    ],
)
def test_incomplete_stream_never_executes_tool(parts, fail):
    adapter, run, history, fixture, events, _ = setup(parts, fail=fail)
    calls = []
    with pytest.raises(ProtocolError, match="^MODEL_STREAM_FAILED$"):
        execute(adapter, run, history, fixture, tool=lambda *args: calls.append(args))
    assert calls == [] and history.messages(run) == []
    assert [e[0] for e in events] == ["reserve", "send", "unknown"]


@pytest.mark.parametrize(
    "arguments", ['{"target":"wrong"}', "{", '{"target":"m0-target-a","extra":true}']
)
def test_invalid_arguments_produce_paired_failure_without_tool(arguments):
    adapter, run, history, fixture, _, _ = setup(stream_parts(arguments=arguments))
    calls = []
    result = execute(
        adapter, run, history, fixture, tool=lambda *args: calls.append(args)
    )
    assert result.tool_statuses == ("TOOL_INPUT_DENIED",) and calls == []
    assert json.loads(history.messages(run)[1]["content"]) == {
        "error": "TOOL_INPUT_DENIED"
    }


def test_tool_failure_is_paired_and_can_continue():
    adapter, run, history, fixture, _, bodies = setup()

    def fail(*args):
        raise RuntimeError("SYNTHETIC_PRIVATE_EXCEPTION")

    assert execute(adapter, run, history, fixture, tool=fail).tool_statuses == (
        "TOOL_FAILED",
    )
    execute(adapter, run, history, fixture)
    assert "SYNTHETIC_PRIVATE_EXCEPTION" not in json.dumps(bodies)
    assert json.loads(bodies[1]["messages"][-1]["content"]) == {"error": "TOOL_FAILED"}


@pytest.mark.parametrize(
    "options,code",
    [({"created": False}, "REQUEST_REPLAYED"), ({"denied": True}, "BUDGET_EXHAUSTED")],
)
def test_budget_refusal_never_sends(options, code):
    adapter, run, history, fixture, events, _ = setup(budget_options=options)
    with pytest.raises((BudgetError, ProtocolError), match=code):
        execute(adapter, run, history, fixture)
    assert [e[0] for e in events] == ["reserve"]


def test_deadline_rechecked_after_reservation():
    times = iter([NOW, NOW + timedelta(seconds=61)])
    adapter, run, history, fixture, events, _ = setup(clock=lambda: next(times))
    with pytest.raises(BudgetError, match="DEADLINE_EXCEEDED"):
        execute(adapter, run, history, fixture)
    assert [e[0] for e in events] == ["reserve", "settle"]
    assert events[-1][2] == 0


def test_missing_usage_retains_full_reservation():
    adapter, run, history, fixture, events, _ = setup(stream_parts(usage=False))
    execute(adapter, run, history, fixture)
    assert [e[0] for e in events] == ["reserve", "send", "unknown"]


def test_run_state_isolation_and_unknown_field_filtering():
    run, other = context(), context()
    fixture = json.loads(FIXTURE.read_text())
    history = History(run)
    assistant = fixture["assistant"] | {"provider_metadata": "DO_NOT_FORWARD"}
    assistant["tool_calls"][0]["private"] = "DO_NOT_FORWARD"
    history.append(run, assistant, [tool_result(assistant["tool_calls"][0], fixture)])
    assert "DO_NOT_FORWARD" not in json.dumps(history.messages(run))
    with pytest.raises(ProtocolError, match="INCOMPATIBLE_STATE"):
        history.messages(other)
    with pytest.raises(ProtocolError, match="INCOMPATIBLE_STATE"):
        history.append(other, assistant, [])
    with pytest.raises(BudgetError):
        RunContext(uuid4(), uuid4(), "other-provider", NOW)


def test_sdk_http_error_has_no_hidden_retry():
    events = []

    def handler(request):
        events.append(("send",))
        return httpx2.Response(429, json={"error": {"message": "PRIVATE"}})

    run = context()
    adapter = SyntheticAdapter(
        transport=httpx2.MockTransport(handler),
        budget=RecordingBudget(events),
        clock=lambda: NOW,
    )
    for _ in range(2):
        with pytest.raises(ProtocolError, match="^MODEL_STREAM_FAILED$"):
            execute(adapter, run, History(run), json.loads(FIXTURE.read_text()))
    assert [e[0] for e in events] == ["reserve", "send", "unknown"] * 2
    assert events[0][1].request_id != events[3][1].request_id


def test_final_text_is_candidate_only():
    parts = [
        sse({"content": "synthetic candidate"}),
        sse({}, "stop"),
        b"data: [DONE]\n\n",
    ]
    adapter, run, history, fixture, _, _ = setup(parts)
    result = execute(adapter, run, history, fixture)
    assert result.status == "completed" and result.content == "synthetic candidate"
    assert result.tool_statuses == ()


def test_eof_after_finish_without_done_is_not_complete():
    adapter, run, history, fixture, events, _ = setup(stream_parts()[:-1])
    calls = []
    with pytest.raises(ProtocolError, match="MODEL_STREAM_FAILED"):
        execute(adapter, run, history, fixture, tool=lambda *args: calls.append(args))
    assert calls == []
    assert events[-1][0] == "unknown"


def test_duplicate_call_ids_reject_entire_plan():
    parts = stream_parts()
    fixture = json.loads(FIXTURE.read_text())
    call = fixture["assistant"]["tool_calls"][0] | {"index": 1}
    parts.insert(2, sse({"tool_calls": [call]}))
    adapter, run, history, fixture, events, _ = setup(parts)
    calls = []
    with pytest.raises(ProtocolError, match="TOOL_PAIRING_INVALID"):
        execute(adapter, run, history, fixture, tool=lambda *args: calls.append(args))
    assert calls == [] and history.messages(run) == []
    assert events[-1][0] == "settle"  # Known usage remains billed despite invalid plan.


def test_empty_or_partial_results_cannot_enter_history():
    run = context()
    history = History(run)
    fixture = json.loads(FIXTURE.read_text())
    with pytest.raises(ProtocolError, match="TOOL_PAIRING_INVALID"):
        history.append(run, fixture["assistant"], [])
    assert history.messages(run) == []


def test_physical_timeout_retains_unknown_and_no_tools():
    events = []

    async def handler(request):
        events.append(("send",))
        await asyncio.sleep(1)

    run = context()
    adapter = SyntheticAdapter(
        transport=httpx2.MockTransport(handler),
        budget=RecordingBudget(events),
        clock=lambda: NOW,
    )
    with pytest.raises(ProtocolError, match="MODEL_STREAM_FAILED"):
        execute(
            adapter, run, History(run), json.loads(FIXTURE.read_text()), timeout=0.01
        )
    assert [e[0] for e in events] == ["reserve", "send", "unknown"]


def test_fragmented_arguments_and_done_marker_are_reassembled():
    parts = stream_parts()
    call = json.loads(FIXTURE.read_text())["assistant"]["tool_calls"][0]
    original = call["function"]["arguments"]
    call["function"]["arguments"] = original[:12]
    parts[1:2] = [
        sse({"tool_calls": [call | {"index": 0}]}),
        sse({"tool_calls": [{"index": 0, "function": {"arguments": original[12:]}}]}),
    ]
    parts[-1:] = [b"data: [DO", b"NE]\n\n"]
    adapter, run, history, fixture, _, _ = setup(parts)
    assert execute(adapter, run, history, fixture).tool_statuses == ("completed",)
    assert (
        history.messages(run)[0]["tool_calls"][0]["function"]["arguments"] == original
    )


def test_tools_cannot_use_network_in_synthetic_round():
    import socket

    adapter, run, history, fixture, _, _ = setup()

    def tool(*args):
        socket.getaddrinfo("example.com", 443)

    assert execute(adapter, run, history, fixture, tool=tool).tool_statuses == (
        "TOOL_FAILED",
    )


@pytest.mark.parametrize("cancel_at,total", [(0, 1), (1, 3)])
def test_tool_cancellation_preserves_complete_group_and_signal(cancel_at, total):
    parts = stream_parts()
    fixture = json.loads(FIXTURE.read_text())
    calls = []
    for index in range(total):
        call = copy.deepcopy(fixture["assistant"]["tool_calls"][0])
        call.update(index=index, id=f"synthetic_call_{index}")
        calls.append(call)
    parts[1] = sse({"tool_calls": calls})
    adapter, run, history, fixture, events, _ = setup(parts)
    executed = []

    def tool(call, data):
        executed.append(call["id"])
        if len(executed) - 1 == cancel_at:
            raise asyncio.CancelledError("SYNTHETIC_PRIVATE_CANCEL")
        return tool_result(call, data)

    with pytest.raises(asyncio.CancelledError) as caught:
        execute(adapter, run, history, fixture, tool=tool)
    assert caught.value.args == ()
    assert executed == [f"synthetic_call_{i}" for i in range(cancel_at + 1)]
    assert [event[0] for event in events] == ["reserve", "send", "settle"]
    messages = history.messages(run)
    assert len(messages) == 1 + total
    assert [call["id"] for call in messages[0]["tool_calls"]] == [
        result["tool_call_id"] for result in messages[1:]
    ]
    for index, message in enumerate(messages[1:]):
        expected = (
            fixture["evidence"] if index < cancel_at else {"error": "TOOL_CANCELLED"}
        )
        assert json.loads(message["content"]) == expected
    assert "SYNTHETIC_PRIVATE_CANCEL" not in json.dumps(messages)
    assert history.messages(run, keep_groups=1) == messages


@pytest.mark.parametrize("cancel_at", ["request", "stream"])
@pytest.mark.parametrize("cleanup_failure", [None, "stream", "transport"])
def test_model_task_cancellation_propagates_and_cleans_up(cancel_at, cleanup_failure):
    async def scenario():
        events, tools, closed = [], [], []
        reached = asyncio.Event()
        blocked = asyncio.Event()

        class WaitingStream(httpx2.AsyncByteStream):
            async def __aiter__(self):
                yield stream_parts()[0]
                reached.set()
                await blocked.wait()
                for part in stream_parts()[1:]:
                    yield part

            async def aclose(self):
                closed.append("stream")
                if cleanup_failure == "stream":
                    raise OSError("SYNTHETIC_PRIVATE_CLEANUP")

        async def handler(request):
            events.append(("send",))
            if cancel_at == "request":
                reached.set()
                await blocked.wait()
            return httpx2.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=WaitingStream(),
            )

        class RecordingTransport(httpx2.MockTransport):
            async def aclose(self):
                closed.append("transport")
                await super().aclose()
                if cleanup_failure == "transport":
                    raise OSError("SYNTHETIC_PRIVATE_CLEANUP")

        run = context()
        history = History(run)
        adapter = SyntheticAdapter(
            transport=RecordingTransport(handler),
            budget=RecordingBudget(events),
            clock=lambda: NOW,
        )
        task = asyncio.create_task(
            adapter.round(
                run,
                history,
                json.loads(FIXTURE.read_text()),
                upper_bound=100,
                synthetic_price=lambda usage: usage["total_tokens"],
                tool=lambda *args: tools.append(args),
            )
        )
        await asyncio.wait_for(reached.wait(), timeout=2)
        task.cancel("SYNTHETIC_PRIVATE_CANCEL")
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert caught.value.args == ()
        assert caught.value.__suppress_context__
        assert task.cancelled()
        assert [event[0] for event in events] == ["reserve", "send", "unknown"]
        assert events[0][1] == events[-1][1]
        assert tools == [] and history.messages(run) == []
        assert "transport" in closed
        assert ("stream" in closed) == (cancel_at == "stream")

    asyncio.run(scenario())
