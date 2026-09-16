"""Deterministic doubles for the investigation loop. No network, no model HTTP."""

from __future__ import annotations

import json

from opspilot.investigation.loop import (
    InvestigationLoop,
    InvestigationRequest,
    ModelError,
    ModelReply,
)
from opspilot.investigation.store import MemoryStepStore
from opspilot.tools import TransportResponse
from tests.m1_tool_support import NOW, WINDOW_START, body, build

TOOL_SCHEMAS = (
    {
        "type": "function",
        "function": {
            "name": "metrics.range_query",
            "description": (
                "Return a projected Prometheus range query for one registered "
                "target inside the authorized window. The view is sampled: a "
                "missing series is unknown, not zero. Listing a series does "
                "not prove health. Available expressions: "
                '["rate(http_errors[5m])"]; any other value returns an error. '
                "At most 512 view bytes; truncated views set truncated true."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "expr": {
                        "type": "string",
                        "description": "Exact PromQL string from the available list.",
                    }
                },
                "required": ["expr"],
            },
        },
    },
)


class ScriptedModel:
    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = []

    def complete(self, call):
        self.calls.append(call)
        if not self._replies:
            raise ModelError("MODEL_UNAVAILABLE")
        item = self._replies.pop(0)
        if callable(item):
            item = item(call)
        if isinstance(item, Exception):
            raise item
        return item


def reply(
    *,
    content=None,
    reasoning="reasoned",
    tool_calls=(),
    finish="stop",
    model="deepseek-flash",
    raw=None,
):
    return ModelReply(
        content=content,
        reasoning_content=reasoning,
        tool_calls=tuple(tool_calls),
        finish_reason=finish,
        response_model=model,
        usage={"prompt_tokens": 8, "completion_tokens": 16},
        raw={} if raw is None else raw,
    )


def tool_call(
    call_id="call-1",
    name="metrics.range_query",
    arguments='{"expr":"rate(http_errors[5m])"}',
):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def report_json(
    *,
    evidence_id,
    target_ref="checkout-prod",
    time_scope="policy-window-1",
    status="completed",
    conclusion="supported",
    summary="Checkout errors increased in the authorized window.",
):
    payload = {
        "schema_version": "m0-report-v2",
        "assessment_status": status,
        "conclusion": conclusion,
        "summary": summary,
        "claims": [
            {
                "kind": "fact",
                "text": "The authorized metrics query returned a sample.",
                "evidence_ids": [evidence_id],
                "target_refs": [target_ref],
                "time_scope_ref": time_scope,
            }
        ],
        "gaps": ["No HealthProfile was supplied."],
        "next_steps": ["Have a human compare the sample against the service SLO."],
    }
    if status == "incomplete":
        payload["conclusion"] = "inconclusive"
        payload["summary"] = "Visible evidence is insufficient to support a cause."
    return json.dumps(payload, ensure_ascii=False)


def report_from_transcript(call):
    """Final-round reply that cites the evidence_id from the last tool view."""
    evidence_id = None
    for message in reversed(call.messages):
        if message.get("role") == "tool":
            evidence_id = json.loads(message["content"])["evidence_id"]
            break
    return reply(content=report_json(evidence_id=evidence_id or "missing"))


def assemble(*, replies, budget_limit=4, deadline=None, model_requests=2, clock=None):
    executor, transport, sink, clock = build(clock=clock)
    transport.response = TransportResponse(
        body=body([{"metric": "checkout", "value": 3}]),
        data_as_of=WINDOW_START,
    )
    store = MemoryStepStore(
        budget_limit=budget_limit,
        deadline=deadline or (NOW.replace(year=NOW.year)),
        clock=clock,
    )
    # Default tool-support deadline is NOW+600s; keep the store in agreement.
    if deadline is None:
        store.deadline = executor.scope.deadline
    model = ScriptedModel(replies)
    loop = InvestigationLoop(model=model, executor=executor, store=store, clock=clock)
    request = InvestigationRequest(
        run_id=executor.scope.run_id,
        question="Why is checkout erroring?",
        scope=executor.scope,
        tool_schemas=TOOL_SCHEMAS,
        model_requests=model_requests,
        evidence_context={
            "type": "opspilot-evidence-context-v4",
            "time_policies": [{"id": "policy-window-1"}],
        },
    )
    return loop, request, model, transport, store, sink
