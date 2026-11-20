"""Shared helpers for the #666 PR-E contract tests (the eval driver's
TodoWrite switch, question 283, the C1-U2 comparison table and the Holmes
side for 283).

Written from docs/tasks/2026-11-18-holmes-evals-666.md (section "PR-E 接口")
and the C1 record (docs/tasks/2026-11-20-tool-expansion-c1.md, sections 2,
4, 6, 10, 11) without reading the PR-E implementation.

Answer isolation (R8-6): upstream 283's ``before_test``/``after_test`` hold
the values the question asks for, and no question's ``expected_output`` is
read. Question 283 here is synthetic: an upstream-shaped prompt of our own,
``enable_todo: true``, namespace commands over invented ``app-283-*`` names
and a sentinel ``expected_output``. Stored TodoWrite results come from the
product's own pure ``todo_write_view`` (C1 section 1-2), so their shape is
the product's, not a guess.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from opspilot.investigation.todo import TOOL_NAME, todo_write_view
from tests.holmes_evals_support import write_question

Q283 = "283_todowrite_multistep_audit"
Q184 = "184_elasticsearch_index_explosion"
Q185 = "185_elasticsearch_cross_region_search"
Q193 = "193_elasticsearch_large_mapping_search"
Q280 = "280_es_multi_quirk"
Q275 = "275_es_field_name_correction"
C1_ALL = (Q283,)
HARD = (Q184, Q185, Q193, Q280)
PREFIX = "app-283-"
# Invented names (not upstream's): only the prefix is the contract.
NS_283 = ("app-283-alpha", "app-283-beta")
TODO_ENV = "OPSPILOT_TODO_WRITE"
REMINDER = "TODO_UPSTREAM_REMINDER_NOT_COPIED"
FORCED = "TODO_FORCED"
assert TOOL_NAME == "TodoWrite"

PROMPT_283 = (
    "Audit the checkout services across the app-283 namespaces step by step "
    "and tell me which one is misconfigured."
)
TODO_SENTINEL = "TODO-CONTENT-SENTINEL compare replicas"


def write_283(upstream: Path, *, expected: str, enable_todo: bool = True) -> Path:
    """A synthetic 283 directory (never upstream's content)."""
    create = "\n".join(f"kubectl create namespace {n} || true" for n in NS_283)
    delete = "\n".join(
        f"kubectl delete namespace {n} --ignore-not-found" for n in NS_283
    )
    doc: dict[str, Any] = {
        "user_prompt": PROMPT_283,
        "tags": ["kubernetes"],
        "enable_todo": enable_todo,
        "before_test": create + "\n",
        "after_test": delete + "\n",
        "expected_output": [expected],
    }
    return write_question(upstream, Q283, doc)


def todos(*statuses: str, prefix: str = "task") -> list[dict[str, str]]:
    return [
        {"id": str(i + 1), "content": f"{prefix} {i + 1}", "status": s}
        for i, s in enumerate(statuses)
    ]


def todo_result(ordinal: int, items: Any, op: str | None = None) -> dict[str, Any]:
    """A stored ``tool_results`` entry for a TodoWrite call (ok or denied),
    built by the product's pure function."""
    return {
        "ordinal": ordinal,
        "result": todo_write_view(op or f"op-todo-{ordinal}", {"todos": items}),
    }


def query_ok(ordinal: int, ev: str, tool: str = "elasticsearch_search") -> dict:
    return {
        "ordinal": ordinal,
        "result": {
            "operation_id": f"op-{ev}",
            "evidence_id": ev,
            "status": "ok",
            "adopted": True,
            "tool": tool,
        },
    }


def query_refused(
    ordinal: int, tool: str, status: str = "error", reason: str = "INVALID_PARAMS"
) -> dict:
    return {
        "ordinal": ordinal,
        "result": {
            "operation_id": f"op-r{ordinal}-{status}",
            "status": status,
            "tool": tool,
            "reason": reason,
        },
    }


def response(*calls: tuple[str, Any]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": f"call-{i}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
            for i, (name, args) in enumerate(calls)
        ],
    }


def step(
    sequence: int,
    step_id: str,
    results: list[dict[str, Any]],
    calls: list[tuple[str, Any]] | None = None,
    status: str = "completed",
) -> dict[str, Any]:
    if calls is None:
        calls = [(r["result"]["tool"], {"i": r["ordinal"]}) for r in results]
        calls.sort(key=lambda c: c[1].get("i", 0))
    return {
        "sequence": sequence,
        "step_id": step_id,
        "status": status,
        "response": response(*calls),
        "tool_results": results,
    }


def stats(
    ok: int = 0,
    refused: int = 0,
    queries: int = 0,
    last: dict[str, int] | None = None,
) -> dict[str, Any]:
    return {
        "calls_ok": ok,
        "calls_refused": refused,
        "queries": queries,
        "last": last,
    }


def todo_block(
    enabled: bool,
    source: str = "question",
    mode: str = "question",
    st: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Revision r2 (lead, 2026-11-20): ``offered`` only when the pass was
    checked (enabled or forced), ``stats`` only when enabled."""
    block: dict[str, Any] = {
        "mode": mode,
        "enabled": enabled,
        "source": source,
        "prompt_revision": "prompt-rev-x",
        "tool_schema_revision": "tools-rev-x",
    }
    if enabled or source == "forced":
        block["offered"] = enabled
    if enabled:
        block["stats"] = st if st is not None else stats()
    return block
