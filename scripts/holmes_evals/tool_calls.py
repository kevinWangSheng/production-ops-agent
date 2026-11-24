"""Every tool call of one Run, for the ``include_tool_calls`` block (#666 H-13, H-22).

``run_freeze`` keeps only the views of *cited* evidence; upstream gives its
judge every call. So the calls are read here, in a separate read-only
transaction on the same DSN, and only their hash and count reach the summary
(the full list goes to the git-ignored trial directory).

Order is the Run's own: steps by ``(sequence, step_id)``, results by
``ordinal``. A call that produced evidence shows the stored, redacted view the
evidence store keeps (what the model was given); its parameters are the
view's normalised ``query``. A refused call (``error``, ``timeout``,
``denied``) shows its status and reason. An accepted ``TodoWrite`` (PR-E)
shows its stored view as output.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from opspilot.redaction import redact_payload
from scripts.holmes_evals.text import ToolCall

REFUSED = frozenset({"error", "timeout", "denied"})
#: PR-E: the product's task list tool (#565), answered by the loop.
TODO_TOOL = "TodoWrite"


def _planned(response: Any, ordinal: Any) -> Mapping[str, Any] | None:
    assistant = response.get("assistant") if isinstance(response, Mapping) else None
    calls = assistant.get("tool_calls") if isinstance(assistant, Mapping) else None
    if not isinstance(calls, list) or not isinstance(ordinal, int):
        return None
    if not 0 <= ordinal < len(calls):
        return None
    function = (
        calls[ordinal].get("function") if isinstance(calls[ordinal], Mapping) else None
    )
    return function if isinstance(function, Mapping) else None


def _planned_arguments(function: Mapping[str, Any] | None) -> Mapping[str, Any]:
    raw = function.get("arguments") if function else None
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {"arguments": "<unparsed>"}
    redacted = redact_payload(parsed)
    return redacted if isinstance(redacted, Mapping) else {"arguments": redacted}


def tool_calls_from_rows(
    steps: Sequence[Mapping[str, Any]], views: Mapping[str, Mapping[str, Any]]
) -> list[ToolCall]:
    """All calls of the Run in call order (see the module docstring)."""
    ordered = sorted(
        steps, key=lambda s: (int(s.get("sequence") or 0), str(s.get("step_id")))
    )
    calls: list[ToolCall] = []
    for step in ordered:
        if step.get("status") == "late_result":
            continue
        items = [i for i in (step.get("tool_results") or []) if isinstance(i, Mapping)]
        items.sort(key=lambda i: int(i.get("ordinal") or 0))
        for item in items:
            result = item.get("result")
            if not isinstance(result, Mapping):
                continue
            function = _planned(step.get("response"), item.get("ordinal"))
            planned_name = function.get("name") if function else None
            tool = result.get("tool") or planned_name or "unknown"
            status = result.get("status")
            evidence_id = result.get("evidence_id") or result.get(
                "duplicate_of_evidence_id"
            )
            view = views.get(str(evidence_id)) if evidence_id else None
            if tool == TODO_TOOL and not evidence_id and status == "ok":
                # PR-E: an accepted task list (#565) is the loop's own
                # result, not evidence; upstream lists it as a call with
                # its output. Its stored view is already redacted.
                stored = redact_payload(dict(result))
                calls.append(
                    ToolCall(
                        tool=TODO_TOOL,
                        params=dict(_planned_arguments(function)),
                        output=dict(stored) if isinstance(stored, Mapping) else {},
                    )
                )
                continue
            if status in REFUSED or view is None:
                query = result.get("query")
                params = (
                    query
                    if isinstance(query, Mapping)
                    else _planned_arguments(function)
                )
                reason = result.get("reason")
                calls.append(
                    ToolCall(
                        tool=str(tool),
                        params=dict(params),
                        refused_code=str(status or "unknown"),
                        refused_reason=None if reason is None else str(reason),
                    )
                )
                continue
            query = view.get("query")
            calls.append(
                ToolCall(
                    tool=str(view.get("tool") or tool),
                    params=dict(query) if isinstance(query, Mapping) else {},
                    output=dict(view),
                )
            )
    return calls


def read_tool_calls(dsn: str, run_id: str) -> list[ToolCall]:
    """The calls of ``run_id`` from the product tables (SELECTs only)."""
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, connect_timeout=5) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        conn.execute("SET TRANSACTION READ ONLY")
        with conn.cursor(row_factory=dict_row) as cursor:
            steps = cursor.execute(
                "SELECT step_id,sequence,status,response,tool_results"
                " FROM opspilot_steps WHERE run_id=%s ORDER BY sequence,step_id",
                (run_id,),
            ).fetchall()
            rows = cursor.execute(
                "SELECT evidence_id,view FROM opspilot_evidence WHERE run_id=%s",
                (str(run_id),),
            ).fetchall()
    views = {
        str(r["evidence_id"]): r["view"] for r in rows if isinstance(r["view"], Mapping)
    }
    return tool_calls_from_rows(steps, views)
