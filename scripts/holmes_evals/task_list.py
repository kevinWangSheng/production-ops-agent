"""The product's task list (``TodoWrite``, #565) in the eval driver (#666 PR-E).

* :func:`todo_for`: whether an attempt runs with ``OPSPILOT_TODO_WRITE=1``
  -- the question's ``enable_todo`` (upstream's own switch) or a forced
  ``on``/``off`` for the C1-U2 comparison rounds.
* :func:`run_tool_names`: the tool names the Run input recorded; the driver
  checks ``TodoWrite`` is there exactly when the attempt switched it on.
* :func:`task_list_stats_from_rows`: per-Run counts for the comparison table
  (accepted and refused lists, queries other than the task list, the last
  accepted list's statuses).

Read-only SELECTs on the attempt's own PG; nothing here reads answers.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

TOOL = "TodoWrite"
STATUSES = ("completed", "failed", "in_progress", "pending")
TodoMode = Literal["question", "on", "off"]
TodoSource = Literal["question", "forced"]
TODO_MODES: tuple[str, ...] = ("question", "on", "off")


def todo_for(mode: str, question: Any) -> tuple[bool, TodoSource]:
    """``question``: the question's ``enable_todo``; ``on``/``off``: forced."""
    if mode == "question":
        return bool(getattr(question, "enable_todo", False)), "question"
    if mode == "on":
        return True, "forced"
    if mode == "off":
        return False, "forced"
    raise ValueError(f"todo mode must be one of {', '.join(TODO_MODES)}")


def _is_task_list(result: Mapping[str, Any]) -> bool:
    return result.get("tool") == TOOL and not result.get("evidence_id")


def task_list_stats_from_rows(steps: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Counts over the Run's committed tool results (late results skipped),
    in call order: steps by ``(sequence, step_id)``, results by ordinal."""
    ordered = sorted(
        steps, key=lambda s: (int(s.get("sequence") or 0), str(s.get("step_id")))
    )
    calls_ok = calls_refused = queries = 0
    last: dict[str, int] | None = None
    for step in ordered:
        if step.get("status") == "late_result":
            continue
        items = [i for i in (step.get("tool_results") or []) if isinstance(i, Mapping)]
        items.sort(key=lambda i: int(i.get("ordinal") or 0))
        for item in items:
            result = item.get("result")
            if not isinstance(result, Mapping):
                continue
            if not _is_task_list(result):
                queries += 1
                continue
            todos = result.get("todos")
            if result.get("status") == "ok" and isinstance(todos, list):
                calls_ok += 1
                counts = {status: 0 for status in STATUSES}
                for todo in todos:
                    status = todo.get("status") if isinstance(todo, Mapping) else None
                    if status in counts:
                        counts[status] += 1
                last = {"total": len(todos), **counts}
            else:
                calls_refused += 1
    return {
        "calls_ok": calls_ok,
        "calls_refused": calls_refused,
        "queries": queries,
        "last": last,
    }


def _read_only(dsn: str) -> Any:
    import psycopg

    conn = psycopg.connect(dsn, connect_timeout=5)
    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
    conn.execute("SET TRANSACTION READ ONLY")
    return conn


def run_tool_names(owner_dsn: str, run_id: str) -> list[str]:
    """The function names in ``opspilot_runs.input -> 'tool_schemas'``."""
    from psycopg.rows import dict_row

    with _read_only(owner_dsn) as conn, conn.cursor(row_factory=dict_row) as cursor:
        row = cursor.execute(
            "SELECT input -> 'tool_schemas' AS tools FROM opspilot_runs"
            " WHERE run_id=%s",
            (run_id,),
        ).fetchone()
    tools = None if row is None else row["tools"]
    names: list[str] = []
    for schema in tools if isinstance(tools, list) else []:
        function = schema.get("function") if isinstance(schema, Mapping) else None
        name = function.get("name") if isinstance(function, Mapping) else None
        if isinstance(name, str):
            names.append(name)
    return names


def task_list_stats(owner_dsn: str, run_id: str) -> dict[str, Any]:
    """:func:`task_list_stats_from_rows` over the Run's ``opspilot_steps``."""
    from psycopg.rows import dict_row

    with _read_only(owner_dsn) as conn, conn.cursor(row_factory=dict_row) as cursor:
        steps = cursor.execute(
            "SELECT step_id,sequence,status,tool_results FROM opspilot_steps"
            " WHERE run_id=%s ORDER BY sequence,step_id",
            (run_id,),
        ).fetchall()
    return task_list_stats_from_rows(steps)


__all__ = [
    "TODO_MODES",
    "TOOL",
    "run_tool_names",
    "task_list_stats",
    "task_list_stats_from_rows",
    "todo_for",
]
