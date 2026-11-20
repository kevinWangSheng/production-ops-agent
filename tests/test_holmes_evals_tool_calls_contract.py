"""#666 PR-B contract: a Run's tool calls for the judge's attachment (H-13,
H-22).

Independent contract tests written from the task record's PR-B public
interface (``tool_calls.tool_calls_from_rows``) without reading the
implementation. Rows are synthetic, shaped like ``opspilot_steps`` rows
(``sequence``, ``step_id``, ``status``, ``response``, ``tool_results`` with
``ordinal`` and ``result``) and the evidence store's views.

Rules under test:

* every call of the Run, ordered by the step's ``(sequence, step_id)`` and
  the result's ``ordinal``, refused calls included;
* a call with evidence: ``params`` = the stored view's ``query``, ``output``
  = the whole stored view;
* an ``error``/``timeout``/``denied`` call: ``refused_code`` = its status,
  ``refused_reason`` = its reason, no output;
* the result is PR-A ``text.ToolCall`` and feeds ``format_tool_calls``.

Assumptions beyond the interface (also marked ``# ASSUMPTION:``):

* ``views`` is keyed by ``evidence_id``.
* The tool name comes from the stored result's ``tool`` (always present in
  these rows); a refused call's ``params`` are not asserted.
* Duplicate calls (``DUPLICATE_TOOL_CALL``) are not covered: the interface's
  "重复调用取被引用的那条" does not say whether such a call is listed as
  refused or with the referenced view (reported as a spec gap).
"""

from __future__ import annotations

import json
from typing import Any

from tests.holmes_evals_support import mod

STEP_0 = "00000000-0000-4000-8000-000000000001"
STEP_A = "aaaaaaaa-0000-4000-8000-000000000002"
STEP_B = "bbbbbbbb-0000-4000-8000-000000000003"


def _response(*names: str) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": f"call-{i}",
                "type": "function",
                "function": {"name": n, "arguments": json.dumps({"i": i})},
            }
            for i, n in enumerate(names)
        ],
    }


def _ok(ordinal: int, ev: str, tool: str, status: str = "ok") -> dict[str, Any]:
    return {
        "ordinal": ordinal,
        "result": {
            "operation_id": f"op-{ev}",
            "evidence_id": ev,
            "status": status,
            "adopted": True,
            "tool": tool,
        },
    }


def _refused(ordinal: int, tool: str, status: str, reason: str) -> dict[str, Any]:
    return {
        "ordinal": ordinal,
        "result": {
            "operation_id": f"op-r{ordinal}-{status}",
            "status": status,
            "tool": tool,
            "reason": reason,
        },
    }


def _view(ev: str, tool: str, query: dict[str, Any], content: Any) -> dict[str, Any]:
    return {
        "evidence_id": ev,
        "status": "ok",
        "adopted": True,
        "tool": tool,
        "tool_version": "1",
        "source": "elasticsearch",
        "target_id": "m0-otel-20260909",
        "query": query,
        "content": content,
    }


def _rows() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    steps = [
        {
            "sequence": 2,
            "step_id": STEP_B,
            "status": "completed",
            "response": _response("elasticsearch_search", "elasticsearch_search",
                                  "kubernetes_pods", "elasticsearch_mappings"),
            # Stored out of ordinal order on purpose.
            "tool_results": [
                _refused(1, "elasticsearch_search", "error", "INVALID_PARAMS"),
                _ok(0, "ev-3", "elasticsearch_search"),
                _refused(3, "elasticsearch_mappings", "denied", "TARGET_NOT_AUTHORIZED"),
                _refused(2, "kubernetes_pods", "timeout", "SOURCE_TIMEOUT"),
            ],
        },
        {
            "sequence": 1,
            "step_id": STEP_A,
            "status": "completed",
            "response": _response("elasticsearch_search"),
            "tool_results": [_ok(0, "ev-2", "elasticsearch_search", "no_data")],
        },
        {
            "sequence": 1,
            "step_id": STEP_0,
            "status": "completed",
            "response": _response("elasticsearch_list_indices"),
            "tool_results": [_ok(0, "ev-1", "elasticsearch_list_indices")],
        },
    ]  # fmt: skip
    views = {
        "ev-1": _view("ev-1", "elasticsearch_list_indices", {"pattern": "app-275-*"},
                      [{"index": "app-275-logs", "docs.count": "30"}]),
        "ev-2": {**_view("ev-2", "elasticsearch_search",
                         {"index": "app-275-*", "query": "level:ERROR"}, []),
                 "status": "no_data"},
        "ev-3": _view("ev-3", "elasticsearch_search",
                      {"index": "app-275-*", "query": "severity:ERROR", "size": 50},
                      {"hits": [{"message": "DB connection refused 1"}], "total": 10}),
        "ev-unused": _view("ev-unused", "elasticsearch_search", {"x": 1}, "other run"),
    }  # fmt: skip
    return steps, views


def test_calls_in_order_with_views_and_refusals() -> None:
    steps, views = _rows()
    calls = mod("tool_calls").tool_calls_from_rows(steps, views)
    text = mod("text")
    assert all(isinstance(c, text.ToolCall) for c in calls)
    assert [c.tool for c in calls] == [
        "elasticsearch_list_indices",
        "elasticsearch_search",
        "elasticsearch_search",
        "elasticsearch_search",
        "kubernetes_pods",
        "elasticsearch_mappings",
    ]
    first, second, third, bad, slow, denied = calls
    assert dict(first.params) == {"pattern": "app-275-*"}
    assert first.output == views["ev-1"]
    assert first.refused_code is None
    assert dict(second.params) == {"index": "app-275-*", "query": "level:ERROR"}
    assert second.output == views["ev-2"]
    assert second.refused_code is None
    assert dict(third.params) == views["ev-3"]["query"]
    assert third.output == views["ev-3"]
    assert (bad.refused_code, bad.refused_reason) == ("error", "INVALID_PARAMS")
    assert (slow.refused_code, slow.refused_reason) == ("timeout", "SOURCE_TIMEOUT")
    assert (denied.refused_code, denied.refused_reason) == (
        "denied",
        "TARGET_NOT_AUTHORIZED",
    )
    for refused in (bad, slow, denied):
        assert refused.output is None


def test_empty_run_has_no_calls() -> None:
    assert mod("tool_calls").tool_calls_from_rows([], {}) == []
    step = {
        "sequence": 1,
        "step_id": STEP_0,
        "status": "completed",
        "response": {"role": "assistant", "content": "{}"},
        "tool_results": [],
    }
    assert mod("tool_calls").tool_calls_from_rows([step], {}) == []


def test_feeds_pr_a_attachment_in_call_order() -> None:
    steps, views = _rows()
    calls = mod("tool_calls").tool_calls_from_rows(steps, views)
    block = mod("text").format_tool_calls(calls)
    lines = [ln for ln in block.splitlines() if ln.startswith("* Tool #")]
    assert [ln.split(":", 1)[0] for ln in lines] == [
        f"* Tool #{i}" for i in range(1, 7)
    ]
    assert lines[0].startswith("* Tool #1: elasticsearch_list_indices(")
    assert "INVALID_PARAMS" in block and "SOURCE_TIMEOUT" in block
    assert "DB connection refused 1" in block
    assert "other run" not in block
