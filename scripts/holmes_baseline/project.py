"""Project one HolmesGPT run onto the IncidentOutcome-shaped summary (U7-a).

Only observable facts: the terminal state, whether a report exists, counts of
tool calls, successful proxied Prometheus queries and successful model calls.
The diagnosis is not scored (decision Q9). Holmes has no handoff, so
``handoff_reasons`` is always empty.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

QUERY_PATHS = frozenset({"/api/v1/query", "/api/v1/query_range"})
MAX_STEPS_MARKER = "exceeded max_steps"


def proxied_queries_ok(proxy_log_lines: Iterable[str]) -> int:
    """2xx query/query_range lines of the holmes account in the proxy log.

    Line format (``kind_lab.proxy_nginx_config`` ``log_format opspilot_holmes``):
    ``<time> <user> <method> <uri> <status> <request_length> <bytes> <seconds>``.
    Anything else (other accounts, kubectl prefixes, error lines) is skipped.
    """
    count = 0
    for line in proxy_log_lines:
        fields = line.split()
        if len(fields) < 5 or fields[1] != "holmes" or fields[3] not in QUERY_PATHS:
            continue
        if fields[4].isdigit() and 200 <= int(fields[4]) < 300:
            count += 1
    return count


def final_state(holmes_output: Mapping[str, Any]) -> str:
    """R11: wall-clock kill or max_steps -> budget_exhausted; a non-zero exit
    or no report text -> failed; otherwise completed."""
    texts = (holmes_output.get("error"), holmes_output.get("result"))
    if holmes_output.get("timed_out") or any(
        isinstance(text, str) and MAX_STEPS_MARKER in text for text in texts
    ):
        return "budget_exhausted"
    if holmes_output.get("exit_code", 0) != 0 or not _report(holmes_output):
        return "failed"
    return "completed"


def _report(holmes_output: Mapping[str, Any]) -> bool:
    result = holmes_output.get("result")
    return isinstance(result, str) and bool(result.strip())


def project_outcome(
    holmes_output: Mapping[str, Any], proxy_log_lines: Iterable[str]
) -> dict[str, Any]:
    """``holmes_output`` is Holmes' ``--json-output-file`` dict (LLMResult) plus
    the harness keys ``exit_code``, ``timed_out`` and ``error`` when known."""
    tool_calls = holmes_output.get("tool_calls") or []
    return {
        "final_state": final_state(holmes_output),
        "report_available": _report(holmes_output),
        "tool_calls": len(tool_calls),
        "proxied_queries_ok": proxied_queries_ok(proxy_log_lines),
        "model_calls_ok": int(holmes_output.get("num_llm_calls") or 0),
        "handoff_reasons": [],
    }
