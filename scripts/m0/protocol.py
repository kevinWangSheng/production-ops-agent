"""Bounded SDK wire rehearsal with synthetic data and network-denying transports."""

import asyncio
import copy
import json
import socket
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import httpx2
import requests
from langsmith import Client
from openai import AsyncOpenAI

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/fixtures/m0/protocol-v1.json"
RUN_ID = "454b8982-dee3-4117-aaf7-e1aa13eb5315"


class ProtocolError(Exception):
    pass


@contextmanager
def no_network():
    def denied(*args, **kwargs):
        raise ProtocolError("NETWORK_DISABLED")

    with ExitStack() as stack:
        for obj, name in (
            (socket.socket, "connect"),
            (socket.socket, "connect_ex"),
            (socket, "getaddrinfo"),
            (socket.socket, "sendto"),
        ):
            stack.enter_context(patch.object(obj, name, denied))
        yield


def tool_result(call, fixture):
    try:
        valid = (
            isinstance(call["id"], str)
            and bool(call["id"])
            and call["type"] == "function"
            and call["function"]["name"] == "read_fixture"
            and json.loads(call["function"]["arguments"]) == {"target": "m0-target-a"}
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ProtocolError("TOOL_INPUT_DENIED")
    return {
        "role": "tool",
        "tool_call_id": call["id"],
        "content": json.dumps(fixture["evidence"], sort_keys=True),
    }


def continuation(assistant, results, *, provider, run_id):
    if provider != "deepseek" or run_id != RUN_ID:
        raise ProtocolError("INCOMPATIBLE_STATE")
    try:
        if assistant["role"] != "assistant" or not isinstance(
            assistant["content"], (str, type(None))
        ):
            raise ValueError
        calls = assistant["tool_calls"]
        if not isinstance(calls, list) or not isinstance(results, list):
            raise ValueError
        for call in calls:
            if (
                not isinstance(call["id"], str)
                or not call["id"]
                or call["type"] != "function"
                or not isinstance(call["function"]["name"], str)
                or not isinstance(call["function"]["arguments"], str)
            ):
                raise ValueError
        for result in results:
            if (
                set(result) != {"role", "tool_call_id", "content"}
                or result["role"] != "tool"
                or not isinstance(result["tool_call_id"], str)
                or not isinstance(result["content"], str)
            ):
                raise ValueError
        ids = [c["id"] for c in calls]
        result_ids = [r["tool_call_id"] for r in results]
        if not ids or len(set(ids)) != len(ids) or sorted(ids) != sorted(result_ids):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ProtocolError("TOOL_PAIRING_INVALID") from None
    if not isinstance(assistant.get("reasoning_content"), str):
        raise ProtocolError("PRIVATE_PROTOCOL_MISSING")
    # Retain complete groups, not token slicing. Unknown provider fields are not forwarded.
    return [
        {
            k: copy.deepcopy(assistant[k])
            for k in ("role", "content", "reasoning_content", "tool_calls")
        },
        *copy.deepcopy(results),
    ]


def trace_dto(raw):
    # Only canonical IDs, fixed enums and bounded integers; no raw prose/metadata passthrough.
    try:
        run_id = str(UUID(raw["run_id"]))
        count = raw["request_count"]
        status = raw["status"]
        if (
            type(count) is not int
            or not 0 <= count <= 2
            or status not in ("completed", "failed")
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ProtocolError("TRACE_DTO_INVALID") from None
    return {
        "run_id": run_id,
        "attempt": 1,
        "subject": "m0-target-a",
        "fixture": "m0-protocol-v1",
        "adapter": "m0-offline-v1",
        "evidence_id": "m0-evidence-a",
        "request_count": count,
        "status": status,
    }


class CaptureSession(requests.Session):
    """SDK serialization sink, never a network session."""

    def __init__(self):
        super().__init__()
        self.trust_env = False
        self.bodies = []

    def send(self, request, **kwargs):
        if request.method != "POST" or request.url != "https://trace.invalid/runs":
            raise ProtocolError("TRACE_ROUTE_DENIED")
        self.bodies.append(json.loads(request.body))
        response = requests.Response()
        response.status_code = 200
        response._content = b"{}"
        return response


def trace_rehearsal(raw):
    dto = trace_dto(raw)
    with CaptureSession() as session:
        client = Client(
            api_url="https://trace.invalid",
            api_key="synthetic-trace-auth",
            session=session,
            auto_batch_tracing=False,
            omit_traced_runtime_info=True,
            tracing_mode="langsmith",
            tracing_sampling_rate=1,
            timeout_ms=1000,
            info={},
        )
        try:
            now = datetime.now(timezone.utc)
            client.create_run(
                name="m0-offline",
                run_type="chain",
                project_name="m0-offline",
                id=RUN_ID,
                inputs={"fixture": "m0-protocol-v1"},
                outputs=dto,
                start_time=now,
                end_time=now,
            )
        finally:
            client.close()
        if len(session.bodies) != 1:
            raise ProtocolError("TRACE_CAPTURE_MISSING")
        wire = session.bodies[0]
        if wire.get("outputs") != dto or wire.get("extra", {}):
            raise ProtocolError("TRACE_WIRE_INVALID")
        return dto


async def rehearse():
    fixture = json.loads(FIXTURE.read_text())
    requests_seen = []

    def respond(request):
        body = json.loads(request.content)
        requests_seen.append(body)
        if len(requests_seen) > 2:
            raise ProtocolError("REQUEST_LIMIT")
        if (
            body.get("thinking") != {"type": "enabled"}
            or body.get("reasoning_effort") != "high"
        ):
            raise ProtocolError("PROFILE_INVALID")
        if len(requests_seen) == 1:
            message = fixture["assistant"]
            finish = "tool_calls"
        else:
            group = body["messages"][-2:]
            if (
                group[0]["reasoning_content"]
                != fixture["assistant"]["reasoning_content"]
            ):
                raise ProtocolError("PRIVATE_PROTOCOL_LOST")
            if group[0]["tool_calls"][0]["id"] != group[1]["tool_call_id"]:
                raise ProtocolError("TOOL_PAIRING_INVALID")
            message = {
                "role": "assistant",
                "content": json.dumps(
                    {"target": "m0-target-a", "evidence_id": "m0-evidence-a"}
                ),
            }
            finish = "stop"
        return httpx2.Response(
            200,
            json={
                "id": "synthetic-response",
                "object": "chat.completion",
                "created": 1788825660,
                "model": "deepseek-v4-pro",
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 10,
                    "total_tokens": 30,
                },
            },
        )

    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(respond), trust_env=False, follow_redirects=False
    ) as http:
        async with AsyncOpenAI(
            api_key="synthetic-model-auth",
            base_url="https://api.deepseek.com",
            organization="",
            project="",
            max_retries=0,
            timeout=5,
            http_client=http,
        ) as client:
            messages = copy.deepcopy(fixture["messages"])
            async with asyncio.timeout(15):
                first = await client.chat.completions.create(
                    model="deepseek-v4-pro",
                    messages=messages,
                    tools=fixture["tools"],
                    max_tokens=256,
                    reasoning_effort="high",
                    extra_body={"thinking": {"type": "enabled"}},
                )
                assistant = first.choices[0].message.model_dump(exclude_none=True)
                results = [tool_result(c, fixture) for c in assistant["tool_calls"]]
                messages += continuation(
                    assistant, results, provider="deepseek", run_id=RUN_ID
                )
                final = await client.chat.completions.create(
                    model="deepseek-v4-pro",
                    messages=messages,
                    tools=fixture["tools"],
                    max_tokens=256,
                    reasoning_effort="high",
                    extra_body={"thinking": {"type": "enabled"}},
                )
    if json.loads(final.choices[0].message.content) != {
        "target": "m0-target-a",
        "evidence_id": "m0-evidence-a",
    }:
        raise ProtocolError("FINAL_CONTRACT_INVALID")
    raw = fixture | {
        "run_id": RUN_ID,
        "request_count": len(requests_seen),
        "status": "completed",
    }
    return trace_rehearsal(raw)
