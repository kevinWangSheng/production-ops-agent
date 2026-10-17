"""Shared doubles for the F3 PR-1 acceptance tests (steps 1, 2 and 4).

Written from the F3 contract pre-review (section seven, PR-1 interface draft
and the four-class mapping, user ruling r1 on 2026-10-17), ``feature_list.json``
F3 steps 1, 2 and 4, and the public workbench / loop / executor interfaces --
not from an implementation.

One scripted Run drives the real ``InvestigationLoop`` and the real
``ReadOnlyToolExecutor`` through a transport double that answers each tool
call by its expression, so a single Run commits one tool result of each class
the acceptance step names:

* ``ok`` -- a sample, the only citable observation;
* input error -- the source answers HTTP 400 (``error/INVALID_PARAMS``);
* connectivity failure -- the transport cannot reach the source
  (``error/SOURCE_UNAVAILABLE``);
* timeout -- the transport times out (``timeout/TOOL_TIMEOUT``);
* no data -- the source answers with an empty result (``no_data/NO_DATA``).

The final report carries one claim of each of the five ``m0-report-v2``
kinds; the evidence-bearing ones cite the ``ok`` observation.
"""

from __future__ import annotations

import json
from typing import Any

from opspilot.investigation.loop import InvestigationLoop, InvestigationRequest
from opspilot.tools import (
    TransportResponse,
    TransportTimeout,
    TransportUnavailable,
    Window,
)
from tests.m1_investigation_support import TOOL_SCHEMAS, ScriptedModel, reply
from tests.m1_tool_support import (
    WINDOW_END,
    WINDOW_START,
    FixedControl,
    body,
    build,
    historical_window_context,
)

TOOL = "metrics.range_query"
TARGET = "checkout-prod"
TIME_REF = "policy-window-1"

OK_EXPR = "rate(http_errors[5m])"
INVALID_EXPR = "rate(http_errors[5m]"
UNREACHABLE_EXPR = "rate(http_errors[1m])"
TIMEOUT_EXPR = "rate(http_errors[2m])"
NO_DATA_EXPR = "rate(no_such_metric_total[5m])"

#: Call order inside the one tool round; also the committed ordinal order.
EXPRS = (OK_EXPR, INVALID_EXPR, UNREACHABLE_EXPR, TIMEOUT_EXPR, NO_DATA_EXPR)

#: (status, reason) each call must end in. Pre-review section seven, point 4.
EXPECTED = {
    OK_EXPR: ("ok", None),
    INVALID_EXPR: ("error", "INVALID_PARAMS"),
    UNREACHABLE_EXPR: ("error", "SOURCE_UNAVAILABLE"),
    TIMEOUT_EXPR: ("timeout", "TOOL_TIMEOUT"),
    NO_DATA_EXPR: ("no_data", "NO_DATA"),
}

CLAIM_TEXT = {
    "fact": "F3-FACT checkout errors were sampled in the authorized window.",
    "hypothesis": "F3-HYPOTHESIS a dependency of checkout may be failing.",
    "recommendation": "F3-RECOMMENDATION compare against the payment error rate.",
    "counter_evidence": "F3-COUNTER the sampled error rate did not rise before the window.",
    "rejected_hypothesis": "F3-REJECTED a checkout deploy caused the errors.",
}
EVIDENCE_KINDS = ("fact", "counter_evidence", "rejected_hypothesis")
UNCITED_KINDS = ("hypothesis", "recommendation")


class ByExpressionTransport:
    """Answers each read by its ``expr``: bytes, a source status or a raise."""

    def __init__(self) -> None:
        self.requests: list[Any] = []

    def fetch(self, request):
        self.requests.append(request)
        expr = request.params.get("expr")
        if expr == OK_EXPR:
            return TransportResponse(
                body=body([{"metric": "checkout", "value": 3}]),
                data_as_of=WINDOW_START,
            )
        if expr == INVALID_EXPR:
            return TransportResponse(
                body=b'{"status":"error","errorType":"bad_data"}',
                source_status="400",
            )
        if expr == UNREACHABLE_EXPR:
            raise TransportUnavailable("UNREACHABLE")
        if expr == TIMEOUT_EXPR:
            raise TransportTimeout("TIMEOUT")
        if expr == NO_DATA_EXPR:
            return TransportResponse(body=body([]))
        raise AssertionError(f"unexpected expression {expr!r}")


def five_class_tool_calls(name: str = TOOL) -> list[dict[str, Any]]:
    return [
        {
            "id": f"call-{index}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps({"expr": expr})},
        }
        for index, expr in enumerate(EXPRS)
    ]


def ok_evidence_id(messages) -> str:
    """The evidence id of the one citable view the tool round delivered."""
    for message in messages:
        if message.get("role") != "tool":
            continue
        view = json.loads(message["content"])
        if view.get("status") == "ok" and view.get("citable_as_fact"):
            return view["evidence_id"]
    raise AssertionError("no citable ok view was delivered to the model")


def five_kind_report(evidence_id: str, *, target: str = TARGET) -> str:
    claims = []
    for kind, text in CLAIM_TEXT.items():
        claim: dict[str, Any] = {"kind": kind, "text": text, "evidence_ids": []}
        if kind in EVIDENCE_KINDS:
            claim.update(
                evidence_ids=[evidence_id],
                target_refs=[target],
                time_scope_ref=TIME_REF,
            )
        claims.append(claim)
    return json.dumps(
        {
            "schema_version": "m0-report-v2",
            "assessment_status": "completed",
            "conclusion": "supported",
            "summary": "Checkout errors were sampled; one cause was tested and rejected.",
            "claims": claims,
            "gaps": [
                "The traces source was not consulted.",
            ],
            "next_steps": ["Have a human confirm the payment dependency."],
        }
    )


def five_kind_report_from_transcript(call):
    return reply(content=five_kind_report(ok_evidence_id(call.messages)))


class FiveClassInvestigator:
    """Same wiring as ``tests.m1_web_support.ScriptedInvestigator``, but one
    tool round that commits one result of each class, then a five-kind report.
    """

    def __init__(self, clock) -> None:
        self.clock = clock
        self.transports: list[ByExpressionTransport] = []
        self.models: list[ScriptedModel] = []

    def investigate(self, context, committer, evidence):
        transport = ByExpressionTransport()
        self.transports.append(transport)
        executor, _, _, _ = build(
            clock=self.clock,
            sink=evidence,
            transport=transport,
            control=FixedControl(generation=context.control_generation),
            scope_overrides={
                "run_id": str(context.run_id),
                "subject_id": str(context.incident_id),
                "control_generation": context.control_generation,
                "deadline": context.deadline,
                "window": Window(WINDOW_START, WINDOW_END),
            },
        )
        model = ScriptedModel(
            [
                reply(tool_calls=five_class_tool_calls(), finish="tool_calls"),
                five_kind_report_from_transcript,
            ]
        )
        self.models.append(model)
        loop = InvestigationLoop(
            model=model, executor=executor, store=committer, clock=self.clock
        )
        return loop.run(
            InvestigationRequest(
                run_id=str(context.run_id),
                question=context.question,
                scope=executor.scope,
                tool_schemas=TOOL_SCHEMAS,
                model_requests=2,
                evidence_context=historical_window_context(str(context.run_id)),
            )
        )
