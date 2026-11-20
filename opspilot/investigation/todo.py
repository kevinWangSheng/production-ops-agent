"""``TodoWrite``: the investigation's own task list (tool expansion C1, #565).

Interface: ``docs/tasks/2026-11-20-tool-expansion-c1.md`` "公开接口" (r11
C1-U1=a; upstream HolmesGPT ``a0009195``
``holmes/plugins/toolsets/investigator/core_investigation.py``).

* Only in the ``otel-demo-ext`` profile and only when both processes set
  ``OPSPILOT_TODO_WRITE=1``; the profile then appends :data:`TODO_WRITE_SCHEMA`
  to the Run's tools.
* Not a gateway tool: the investigation loop answers the call itself with
  :func:`todo_write_view` (serial, parallel and recovery replay alike), and
  commits that view as the step's tool result under the same control check
  as any other. It reads no source and sends no request.
* The view has no ``evidence_id`` and ``adopted`` is false, so it grants no
  citation (``context.delivered_view``): a report cannot cite the plan.
* The view is a pure function of the call's arguments (no clock, no
  counter), so a replayed call commits the same bytes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from opspilot.tools.registry import canonical, redact_credentials

TOOL_NAME: Final = "TodoWrite"
ENV: Final = "OPSPILOT_TODO_WRITE"
INVALID_CODE: Final = "TODO_WRITE_INVALID"
REFUSAL: Final = "INVALID_TODOS"

STATUSES: Final = ("pending", "in_progress", "completed", "failed")
MAX_TASKS: Final = 50
MAX_ID_CHARS: Final = 64
MAX_CONTENT_CHARS: Final = 500
MAX_ARGUMENT_BYTES: Final = 32 * 1024

#: Pre-review C1-8 (rewritten from upstream ``core_investigation.py:40``):
#: no promise that the list reappears in later prompts.
DESCRIPTION: Final = (
    "Save this investigation's task list to break a multi-step investigation "
    "into concrete sub-tasks and track their status. Always send the COMPLETE "
    "list, not only the tasks that changed; each call replaces the previous "
    "list. The list is shown to the operator as progress. It is not evidence: "
    "it has no evidence_id and cannot be cited. A list that breaks the limits "
    "(50 tasks, 500 characters per task, unique ids) is refused whole with "
    "INVALID_TODOS and the previous list stays."
)

TODO_WRITE_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": (
                        "COMPLETE list of ALL tasks on the task list. Each task "
                        "has: id (string), content (string), status "
                        "(pending/in_progress/completed/failed)."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "content": {"type": "string"},
                            "status": {"type": "string", "enum": list(STATUSES)},
                        },
                        "required": ["id", "content", "status"],
                    },
                }
            },
            "required": ["todos"],
        },
    },
}


def todo_write_enabled(environ: Mapping[str, str]) -> bool:
    """``1`` is on; unset, empty or ``0`` off; anything else is a startup
    error (``TODO_WRITE_INVALID``), the same reading as B3-1 and C2b."""
    value = environ.get(ENV)
    if value is None or value in ("", "0"):
        return False
    if value == "1":
        return True
    raise SystemExit(INVALID_CODE)


def offers_todo_write(tool_schemas: object) -> bool:
    """Whether a Run's recorded tools include ``TodoWrite``: the loop and the
    recovery replay decide from the Run input, never from the environment."""
    if not isinstance(tool_schemas, (list, tuple)):
        return False
    for schema in tool_schemas:
        function = schema.get("function") if isinstance(schema, Mapping) else None
        if isinstance(function, Mapping) and function.get("name") == TOOL_NAME:
            return True
    return False


def is_todo_write_call(call: object) -> bool:
    function = call.get("function") if isinstance(call, Mapping) else None
    return isinstance(function, Mapping) and function.get("name") == TOOL_NAME


def _text(value: object, limit: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit and value.isprintable()


def _problem(arguments: object) -> str | None:
    """Why the list is refused, in words that never quote its content."""
    if not isinstance(arguments, Mapping):
        return "the arguments must be an object with a todos array"
    todos = arguments.get("todos")
    if not isinstance(todos, list):
        return "todos must be an array of tasks"
    if len(todos) > MAX_TASKS:
        return f"todos has {len(todos)} tasks; at most {MAX_TASKS} are allowed"
    seen: set[str] = set()
    for number, item in enumerate(todos, start=1):
        if not isinstance(item, Mapping):
            return f"task {number} must be an object"
        task_id = item.get("id")
        if not _text(task_id, MAX_ID_CHARS):
            return f"task {number}: id must be 1-{MAX_ID_CHARS} printable characters"
        if task_id in seen:
            return f"task {number}: id repeats an earlier task's id"
        seen.add(task_id)
        if not _text(item.get("content"), MAX_CONTENT_CHARS):
            return (
                f"task {number}: content must be 1-{MAX_CONTENT_CHARS} "
                "printable characters"
            )
        if item.get("status") not in STATUSES:
            return f"task {number}: status must be one of {', '.join(STATUSES)}"
    try:
        size = len(canonical(arguments).encode("utf-8"))
    except ValueError:
        return "the arguments cannot be encoded"
    if size > MAX_ARGUMENT_BYTES:
        return f"the list is {size} bytes; at most {MAX_ARGUMENT_BYTES} are allowed"
    return None


def todo_write_view(operation_id: str, arguments: object) -> dict[str, Any]:
    """The tool result for one ``TodoWrite`` call: what is stored with the
    step and returned to the model. ``arguments`` is the parsed call
    arguments (``None`` when they were not JSON)."""
    problem = _problem(arguments)
    if problem is not None:
        return {
            "operation_id": operation_id,
            "tool": TOOL_NAME,
            "status": "denied",
            "adopted": False,
            "citable_as_fact": False,
            "reason": REFUSAL,
            "message": (
                f"Task list refused: {problem}. The previous list stays; send "
                "the complete corrected list."
            ),
        }
    assert isinstance(arguments, Mapping)
    todos = [
        {
            "id": item["id"],
            # Stored in PG and shown on the page: the same credential rules
            # as the question text and the replies.
            "content": redact_credentials(item["content"]),
            "status": item["status"],
        }
        for item in arguments["todos"]
    ]
    counts = {status: 0 for status in STATUSES}
    for item in todos:
        counts[item["status"]] += 1
    return {
        "operation_id": operation_id,
        "tool": TOOL_NAME,
        "status": "ok",
        "adopted": False,
        "citable_as_fact": False,
        "todos": todos,
        "counts": counts,
        "message": (
            f"Task list saved: {len(todos)} tasks. This replaces the previous "
            "list. It is your plan, not evidence; it has no evidence_id."
        ),
    }
