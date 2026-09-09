"""Synthetic-only streaming adapter; no live entry point or durable product state."""

import asyncio
import copy
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable
from uuid import uuid4

import httpx2
from openai import AsyncOpenAI

from scripts.m0.contracts import (
    Budget,
    BudgetError,
    RequestIdentity,
    RunContext,
    amount,
    remaining_seconds,
)
from scripts.m0.protocol import ProtocolError, continuation, no_network, tool_result


@dataclass(repr=False)
class History:
    """Attempt-local private groups, never an export DTO or recovery authority."""

    owner: RunContext
    _groups: list = field(default_factory=list, repr=False)

    def messages(self, run: RunContext, *, keep_groups: int = 2) -> list:
        if run != self.owner:
            raise ProtocolError("INCOMPATIBLE_STATE")
        if type(keep_groups) is not int or keep_groups < 0:
            raise ProtocolError("INVALID_INPUT")
        groups = self._groups[-keep_groups:] if keep_groups else []
        return [copy.deepcopy(message) for group in groups for message in group]

    def append(self, run: RunContext, assistant: dict, results: list) -> None:
        if run != self.owner:
            raise ProtocolError("INCOMPATIBLE_STATE")
        group = continuation(
            assistant,
            results,
            provider=run.provider,
            run_id=str(run.run_id),
            expected_run_id=str(self.owner.run_id),
        )
        # Rebuild nested tool calls too: unknown provider fields never propagate.
        group[0]["tool_calls"] = [
            {
                "id": c["id"],
                "type": "function",
                "function": {
                    "name": c["function"]["name"],
                    "arguments": c["function"]["arguments"],
                },
            }
            for c in group[0]["tool_calls"]
        ]
        self._groups.append(group)


@dataclass(frozen=True)
class RoundResult:
    request: RequestIdentity
    status: str
    tool_statuses: tuple[str, ...]
    # Public candidate text is deliberately not interpreted as an accepted outcome.
    content: str | None = None


class SyntheticAdapter:
    """One complete SDK stream per call; explicit retry means a new physical request."""

    def __init__(
        self,
        *,
        transport: httpx2.MockTransport,
        budget: Budget,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        if not isinstance(transport, httpx2.MockTransport):
            raise ProtocolError("SYNTHETIC_TRANSPORT_REQUIRED")
        self.transport, self.budget, self.clock = transport, budget, clock

    async def round(
        self,
        run: RunContext,
        history: History,
        fixture: dict,
        *,
        upper_bound: int,
        synthetic_price: Callable[[dict], int],
        tool: Callable = tool_result,
        timeout: float = 5,
    ) -> RoundResult:
        messages = copy.deepcopy(fixture["messages"]) + history.messages(run)
        amount(upper_bound, positive=True)
        remaining_seconds(run, self.clock(), timeout)
        request = RequestIdentity(run, uuid4())
        reservation = self.budget.reserve(request, upper_bound)
        if not reservation.created:
            raise ProtocolError("REQUEST_REPLAYED")
        try:
            seconds = remaining_seconds(run, self.clock(), timeout)
        except BudgetError:
            self.budget.settle(request, 0)  # Definitely no transmission yet.
            raise
        sent = False
        task = asyncio.current_task()
        initial_cancels = task.cancelling()
        try:
            with no_network():
                async with httpx2.AsyncClient(
                    transport=self.transport, trust_env=False, follow_redirects=False
                ) as http:
                    async with AsyncOpenAI(
                        api_key="synthetic-model-auth",
                        base_url="https://model.invalid",
                        organization="",
                        project="",
                        max_retries=0,
                        timeout=seconds,
                        http_client=http,
                    ) as client:
                        seconds = remaining_seconds(run, self.clock(), timeout)
                        async with asyncio.timeout(seconds):
                            sent = True
                            stream = await client.chat.completions.create(
                                model="deepseek-v4-pro",
                                messages=messages,
                                tools=fixture["tools"],
                                stream=True,
                                stream_options={"include_usage": True},
                                max_tokens=256,
                                reasoning_effort="high",
                                extra_body={"thinking": {"type": "enabled"}},
                            )
                            wire = _WireCompletion(stream.response.stream)
                            stream.response.stream = wire
                            async with stream:
                                assistant, finish, usage = await _collect(stream)
                            if not wire.complete:
                                raise ProtocolError("STREAM_INCOMPLETE")
        except (Exception, asyncio.CancelledError) as error:
            # A failing context-manager cleanup can replace CancelledError. Pending
            # external Task.cancel requests still outrank that secondary exception.
            # asyncio.timeout withdraws its own request when its scope exits.
            cancelled = isinstance(error, asyncio.CancelledError) or (
                task.cancelling() > initial_cancels
            )
            try:
                if sent:
                    self.budget.retain_unknown(request)
                else:
                    self.budget.settle(request, 0)
            finally:
                if cancelled:
                    raise asyncio.CancelledError from None
            if isinstance(error, BudgetError):
                raise
            raise ProtocolError("MODEL_STREAM_FAILED") from None
        if usage is None:
            self.budget.retain_unknown(request)
        else:
            try:
                actual = amount(synthetic_price(usage))
            except Exception:
                self.budget.retain_unknown(request)
                raise ProtocolError("USAGE_INVALID") from None
            self.budget.settle(request, actual)
        if finish == "stop":
            return RoundResult(request, "completed", (), assistant["content"])
        # Validate the entire plan before any tool runs, including duplicate call IDs.
        placeholders = [
            {"role": "tool", "tool_call_id": c["id"], "content": ""}
            for c in assistant["tool_calls"]
        ]
        continuation(
            assistant,
            placeholders,
            provider=run.provider,
            run_id=str(run.run_id),
            expected_run_id=str(history.owner.run_id),
        )
        results, statuses = [], []
        for index, call in enumerate(assistant["tool_calls"]):
            try:
                tool_result(call, fixture)  # Exact synthetic tool and target allowlist.
            except ProtocolError:
                status, content = "TOOL_INPUT_DENIED", {"error": "TOOL_INPUT_DENIED"}
            else:
                try:
                    remaining_seconds(run, self.clock(), timeout)
                    with no_network():
                        result = tool(copy.deepcopy(call), copy.deepcopy(fixture))
                    if result != tool_result(call, fixture):
                        raise ProtocolError("TOOL_RESULT_INVALID")
                    status, content = "completed", None
                except asyncio.CancelledError:
                    # Keep completed observations and pair the entire accepted plan.
                    # Remaining tools are not executed; cancellation still reaches caller.
                    results.extend(
                        {
                            "role": "tool",
                            "tool_call_id": pending["id"],
                            "content": json.dumps({"error": "TOOL_CANCELLED"}),
                        }
                        for pending in assistant["tool_calls"][index:]
                    )
                    history.append(run, assistant, results)
                    raise asyncio.CancelledError from None
                except Exception:
                    status, content = "TOOL_FAILED", {"error": "TOOL_FAILED"}
            results.append(
                result
                if content is None
                else {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(content, sort_keys=True),
                }
            )
            statuses.append(status)
        history.append(run, assistant, results)
        return RoundResult(request, "tools_completed", tuple(statuses))


async def _collect(stream):
    assistant = {
        "role": "assistant",
        "content": "",
        "reasoning_content": "",
        "tool_calls": [],
    }
    calls, finish, usage = {}, None, None
    size = 0
    async for chunk in stream:
        size += len(chunk.model_dump_json())
        if size > 100_000:
            raise ProtocolError("RESPONSE_LIMIT")
        if chunk.usage is not None:
            usage = chunk.usage.model_dump()
        if not chunk.choices:
            continue
        if len(chunk.choices) != 1 or chunk.choices[0].index != 0 or finish is not None:
            raise ProtocolError("STREAM_INVALID")
        choice = chunk.choices[0]
        delta = choice.delta
        if delta.content:
            assistant["content"] += delta.content
        private = getattr(delta, "reasoning_content", None)
        if private is not None:
            if not isinstance(private, str):
                raise ProtocolError("PRIVATE_PROTOCOL_INVALID")
            assistant["reasoning_content"] += private
        for call in delta.tool_calls or []:
            if type(call.index) is not int or not 0 <= call.index < 8:
                raise ProtocolError("TOOL_LIMIT")
            current = calls.setdefault(
                call.index,
                {
                    "id": "",
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                },
            )
            if call.type not in (None, "function"):
                raise ProtocolError("TOOL_INPUT_DENIED")
            current["id"] += call.id or ""
            if call.function:
                current["function"]["name"] += call.function.name or ""
                current["function"]["arguments"] += call.function.arguments or ""
        if choice.finish_reason:
            finish = choice.finish_reason
    assistant["tool_calls"] = [calls[i] for i in sorted(calls)]
    if (
        finish not in ("stop", "tool_calls")
        or (
            finish == "tool_calls" and (not calls or not assistant["reasoning_content"])
        )
        or (finish == "stop" and calls)
    ):
        raise ProtocolError("STREAM_INCOMPLETE")
    return assistant, finish, usage


class _WireCompletion(httpx2.AsyncByteStream):
    """Observe the bounded synthetic SSE envelope: EOF alone is not completion."""

    def __init__(self, source):
        self.source, self.complete, self._wire = source, False, b""

    async def __aiter__(self):
        async for data in self.source:
            self._wire += data
            if len(self._wire) > 100_000:
                raise ProtocolError("RESPONSE_LIMIT")
            canonical = b"\n" + self._wire.replace(b"\r\n", b"\n")
            self.complete = b"\ndata: [DONE]\n\n" in canonical
            yield data

    async def aclose(self):
        await self.source.aclose()
