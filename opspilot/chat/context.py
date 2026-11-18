"""What the Chat model reads about an incident (workbench contract E3, P7-4).

Built by the web at question time from the dict ``page_view`` makes for the
incident page -- the record the person already sees -- through an
allowlist: the shown report, each log entry's visible content (step notes,
tool queries and the shown, already-redacted results, replies, people's
messages, control history), the recovery verdict. Never a raw step row, so
``reasoning_content`` and the provider response cannot reach it
(PRODUCT-CONSTRAINTS: private protocol fields only go back to the same Run).
People's text goes through ``project_input_content`` again. Over
``MAX_CONTEXT_BYTES`` the oldest log entries are dropped and the cut is
marked; the report is always kept.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from opspilot.investigation.context import project_input_content
from opspilot.redaction import redact_credentials

#: P7-4: the stored snapshot's ceiling (the column CHECK allows 256 KiB).
MAX_CONTEXT_BYTES = 200_000
CONTEXT_VERSION = "chat-context-v1"
_TEXT = 4000
_RESULT = 2000


def _s(value: Any, limit: int = _TEXT) -> str | None:
    if value is None:
        return None
    text = redact_credentials(str(value))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _human(value: Any) -> str:
    projected = project_input_content({"text": value if isinstance(value, str) else ""})
    return str(projected.get("text") or "")


def _claims(items: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    return [
        {
            "id": item.get("id"),
            "text": _s(item.get("text")),
            "evidence_ids": [str(e) for e in item.get("evidence_ids") or []],
        }
        for item in items or []
    ]


def _report(report: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not report:
        return None
    return {
        "kind": report.get("kind"),
        "conclusion": report.get("level"),
        "summary": _s(report.get("summary")),
        "facts": _claims(report.get("facts")),
        "hypotheses": _claims(report.get("hypotheses")),
        "counter_evidence": _claims(report.get("counter")),
        "ruled_out": _claims(report.get("ruled_out")),
        "recommendations": _claims(report.get("recommendations")),
        "unknown": [_s(u) for u in report.get("unknown") or []],
        "next_steps": [_s(n) for n in report.get("next_steps") or []],
    }


def _call(call: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "tool": _s(call.get("tool"), 200),
        "status": _s(call.get("status"), 64),
        "evidence_id": call.get("evidence_id"),
        "query": _s(call.get("query")),
        "window": _s(call.get("window"), 200),
        "reason": _s(call.get("reason"), 200),
        # the page's shown result: the redacted view, cut as on the page
        "result": _s(call.get("result_text"), _RESULT),
        "result_cut": bool(call.get("result_cut")),
    }


def _entry(entry: Mapping[str, Any]) -> dict[str, Any] | None:
    kind = entry.get("kind")
    if kind == "step":
        return {
            "kind": "step",
            "number": entry.get("number"),
            "note": _s(entry.get("note")),
            "calls": [_call(c) for c in entry.get("calls") or []],
        }
    if kind == "human":
        return {
            "kind": "message",
            "author": _s(entry.get("author"), 256),
            "text": _human(entry.get("text")),
            "status": _s(entry.get("status"), 200),
        }
    if kind == "reply":
        return {
            "kind": "reply",
            "at_step": entry.get("step"),
            "to_author": _s(entry.get("to_author"), 256),
            "text": _s(entry.get("text")),
            "evidence_ids": [str(e) for e in entry.get("evidence_ids") or []],
        }
    if kind == "control":
        return {
            "kind": "control",
            "title": _s(entry.get("title"), 300),
            "detail": _human(entry.get("detail")) if entry.get("detail") else None,
        }
    if kind in ("run", "end", "report", "watch"):
        return {
            "kind": kind,
            "title": _s(entry.get("title"), 300),
            "detail": _s(entry.get("detail"), 600),
        }
    return None


def _size(value: Mapping[str, Any]) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def build_context(view: Mapping[str, Any]) -> dict[str, Any]:
    """The allowlisted record from ``page_view``'s dict, at most
    ``MAX_CONTEXT_BYTES`` of canonical JSON."""
    header = view.get("header") or {}
    status = view.get("status")
    recovery = view.get("recovery") or {}
    entries = [
        e
        for e in (_entry(x) for x in (view.get("log") or {}).get("entries") or [])
        if e is not None
    ]
    context: dict[str, Any] = {
        "version": CONTEXT_VERSION,
        "incident": {
            "title": _s(header.get("title"), 300),
            "service": _s(header.get("service"), 200),
            "status": getattr(status, "label", None),
        },
        "report": _report(view.get("report")),
        "recovery": {
            "headline": _s(recovery.get("headline"), 300),
            "detail": _s(recovery.get("detail"), 600),
        }
        if recovery.get("session") is not None
        else None,
        "log": entries,
        "log_omitted": 0,
    }
    omitted = 0
    while entries and _size(context) > MAX_CONTEXT_BYTES:
        drop = max(1, len(entries) // 10)
        entries = entries[drop:]
        omitted += drop
        context["log"] = entries
        context["log_omitted"] = omitted
    if _size(context) > MAX_CONTEXT_BYTES and context["report"] is not None:
        # a report alone over the ceiling: keep its summary and conclusion
        report = context["report"]
        context["report"] = {
            "kind": report["kind"],
            "conclusion": report["conclusion"],
            "summary": report["summary"],
            "cut": True,
        }
    return context


def context_sha256(context: Mapping[str, Any]) -> str:
    body = json.dumps(
        context, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def evidence_ids(context: Mapping[str, Any]) -> set[str]:
    """Every evidence id the stored record shows (the refs an answer may cite)."""
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key == "evidence_id" and isinstance(item, str):
                    found.add(item)
                elif key == "evidence_ids" and isinstance(item, list):
                    found.update(str(x) for x in item)
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(context)
    return found
