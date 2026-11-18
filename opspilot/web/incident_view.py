"""Display-only derivations for the incident page (workbench redesign P4).

Everything here is read from the snapshot the page already renders
(``Workbench.snapshot``) and the postmortem projection; nothing queries a
store, a model or telemetry, and nothing here decides a state the server
enforces. The page layout is prototype v2: result first, then the
investigation log, recovery watch and raw records, with a Chat panel shell
(contract E7).

Contract points this module implements (``docs/tasks/2026-11-17-workbench-
redesign-contract.md``): A8/A9 status (``status.incident_status``), the D
table for the message composer, U1 (no report yet: evidence so far and the
latest step), U3 (replies visible without expanding their step).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from html import unescape as html_unescape
from typing import Any

from opspilot.web.status import IncidentStatus, incident_status

__all__ = ["page_view", "handoff_view", "clock", "opening"]

#: Claim groups of a report and the id letter each claim gets on the page.
CLAIM_GROUPS = (
    ("facts", "F", "Facts"),
    ("hypotheses", "H", "Hypotheses"),
    ("counter_evidence", "C", "Counter-evidence"),
    ("rejected_hypotheses", "X", "Ruled out"),
    ("recommendations", "A", "Recommendations"),
)

#: What a committed control decision reads as in the log.
DONE = {
    "close": "closed the incident",
    "reopen": "reopened the incident",
    "takeover": "took over",
    "pause": "paused",
    "resume": "resumed",
    "cancel": "canceled the Run",
    "new_run": "started a new investigation",
    "reinvestigate": "started a new investigation",
    "register_remediation": "marked it remediated",
    "merge_into": "merged it into another incident",
    "rebind_target": "changed the target",
    "auto_authorize_observation": "started the recovery watch",
}

#: Inputs that are a person's words to the investigation. ``follow_up`` and
#: ``correct`` are the replaced control actions; their rows are shown as
#: messages and never count as unread (contract B3).
MESSAGE_ACTIONS = ("follow_up", "correct")
#: Actions the message box posts, never drawn as header buttons.
COMPOSER_ACTIONS = ("message", *MESSAGE_ACTIONS)

#: Button text per control; the posted ``action`` value is unchanged.
BUTTON = {
    "close": "Close",
    "reopen": "Reopen",
    "takeover": "Take over",
    "pause": "Pause",
    "resume": "Resume",
    "cancel": "Cancel Run",
    "new_run": "Re-investigate",
    "reinvestigate": "Re-investigate",
    "register_remediation": "Mark remediated",
    "merge_into": "Merge",
    "rebind_target": "Change target",
}

#: Design 2.5 per status, adjusted by the contract D table: at most two
#: primary buttons, the rest under More. Only actions the server offers
#: for this record are drawn; any other offered action joins More.
LAYOUT: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "investigating": (
        ("pause", "takeover"),
        ("cancel", "close", "rebind_target", "merge_into"),
    ),
    "paused": (("resume", "takeover"), ("cancel", "close")),
    "needs_attention": (
        ("reinvestigate", "new_run", "takeover"),
        ("register_remediation", "cancel", "close", "rebind_target", "merge_into"),
    ),
    "canceled": (
        ("new_run", "reinvestigate", "takeover"),
        ("register_remediation", "close", "rebind_target", "merge_into"),
    ),
    "root_cause": (
        ("register_remediation", "takeover"),
        ("close", "rebind_target", "merge_into"),
    ),
    "watching": (("takeover", "close"), ("pause", "rebind_target", "merge_into")),
    "human_owned": (("register_remediation", "close"), ("resume",)),
    "resolved": (("close",), ("reopen",)),
    "closed": (("reopen",), ()),
    "merged": ((), ()),
}

#: The one primary-styled button per status, when offered.
LEAD = {
    "paused": "resume",
    "needs_attention": "reinvestigate",
    "canceled": "new_run",
    "root_cause": "register_remediation",
    "human_owned": "register_remediation",
    "resolved": "close",
    "closed": "reopen",
}

#: What an offered control does, in one line (shown as the button's title
#: and under More). Current server behaviour, said plainly.
HINT = {
    "pause": "Stop the Agent now; Resume continues the same Run. Withdraws a recovery watch.",
    "resume": "Put the Run back in the queue; it continues from its committed steps.",
    "cancel": "Stop this Run for good without a conclusion. Re-investigate then starts a new one.",
    "new_run": "Start a fresh investigation for this incident; earlier Runs stay in the log.",
    "reinvestigate": "Cancel this investigation and start a new Run that reads every message so far.",
    "takeover": "Make the incident human-owned: the Agent stops and does not start again.",
    "close": "Close the incident. Unfinished Runs are canceled and a recovery watch is withdrawn.",
    "reopen": "Set the incident back to open. Nothing restarts by itself.",
    "register_remediation": "Record that a human fixed it outside OpsPilot; an independent recovery watch starts.",
    "merge_into": "Fold this incident into another incident of the same target. Cannot be undone.",
    "rebind_target": "Bind the incident to another registered target; a recovery watch is withdrawn.",
}

#: Handoff and failure codes in words; any other code is shown as is.
REASON_WORDS = {
    "INCOMPLETE_INVESTIGATION": "the Agent said its investigation is incomplete",
    "BUDGET_EXHAUSTED": "the model-request budget ran out",
    "WALL_TIME_EXHAUSTED": "the time limit ran out",
    "DEADLINE_EXCEEDED": "the Run's deadline passed",
    "MODEL_UNAVAILABLE": "the model was unavailable",
    "OUTPUT_LENGTH": "the model's answer was cut off",
    "CONTEXT_EXHAUSTED": "the context ran out",
    "CONTROL_DENIED": "a human control stopped it",
    "INCOMPATIBLE_STATE": "the Run's versions no longer match",
    "REPORT_INVALID": "the report failed validation",
    "RESPONSE_INVALID": "the reply and report both failed validation",
}

_COVERAGE_WORDS = {
    "not_integrated": "not integrated",
    "failed": "every query failed",
    "partial_failure": "some queries failed",
    "not_queried": "not queried",
    "not_authorized": "not authorized",
    "no_data_only": "returned no data (not evidence of health)",
}

_OK_STATUSES = {"ok", "no_data"}

#: The longest result text drawn inline; the evidence record has the rest.
RESULT_CHARS = 20_000


def _dt(value: Any) -> datetime | None:
    """An aware UTC instant from a datetime or ISO text; ``None`` otherwise."""
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str) and value:
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def clock(value: Any, day: str | None = None) -> str | None:
    """``HH:MM:SS`` UTC; prefixed with the date when it is not ``day``."""
    moment = _dt(value)
    if moment is None:
        return None
    text = moment.strftime("%H:%M:%S")
    if day is not None and moment.strftime("%Y-%m-%d") != day:
        return moment.strftime("%m-%d ") + text
    return text


def stamp(value: Any) -> str | None:
    """``YYYY-MM-DD HH:MM:SS`` UTC, for raw records."""
    moment = _dt(value)
    return None if moment is None else moment.strftime("%Y-%m-%d %H:%M:%S")


def iso(value: Any) -> str | None:
    moment = _dt(value)
    return None if moment is None else moment.isoformat()


def opening(text: str | None, limit: int = 220) -> str | None:
    """The first sentence, cut at a word boundary."""
    if not text:
        return None
    text = " ".join(text.split())
    match = re.search(r"(?<=[.!?])\s", text)
    sentence = text[: match.start()] if match else text
    if len(sentence) <= limit:
        return sentence
    cut = sentence[:limit].rsplit(" ", 1)[0]
    return cut + " …"


def _plural(count: int, one: str, many: str | None = None) -> str:
    return f"{count} {one if count == 1 else (many or one + 's')}"


def _age(seconds: Any) -> str | None:
    if not isinstance(seconds, (int, float)) or seconds < 0:
        return None
    s = int(seconds)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        return f"{s // 3600} h {s % 3600 // 60} min"
    return f"{s // 86400} d"


def _took(ms: Any) -> str | None:
    if not isinstance(ms, int):
        return None
    if ms < 1000:
        return f"{ms} ms"
    return f"{ms / 1000:.1f} s"


def _reason_words(code: str) -> str:
    return REASON_WORDS.get(code, code)


# -- evidence and tool calls -------------------------------------------------


def _pretty(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=False)
    except (TypeError, ValueError):
        return str(value)


def _table(content: Any) -> dict[str, Any] | None:
    """A list of flat objects as columns and rows; anything else ``None``."""
    if not isinstance(content, list) or not content or len(content) > 200:
        return None
    columns: list[str] = []
    for row in content:
        if not isinstance(row, Mapping):
            return None
        for key, value in row.items():
            if isinstance(value, (dict, list)):
                return None
            if key not in columns:
                columns.append(str(key))
    if len(columns) > 12:
        return None
    rows = [
        ["" if row.get(c) is None else str(row.get(c)) for c in columns]
        for row in content
    ]
    return {"columns": columns, "rows": rows}


def _query_text(tool: Mapping[str, Any]) -> str:
    """One line for a call: its query expression, else its arguments."""
    for source in (tool.get("query"), tool.get("arguments")):
        if isinstance(source, Mapping) and source:
            if len(source) == 1:
                (value,) = source.values()
                if isinstance(value, str):
                    return value
            return json.dumps(source, ensure_ascii=False, sort_keys=True)
        if isinstance(source, str) and source:
            return source
    return ""


def _expression(value: Any) -> str | None:
    """A query or argument object as text: a lone string field is shown as
    the expression itself, anything else as indented JSON."""
    if value is None:
        return None
    if isinstance(value, Mapping) and len(value) == 1:
        (only,) = value.values()
        if isinstance(only, str):
            return only
    return _pretty(value)


def _call(tool: Mapping[str, Any], day: str | None) -> dict[str, Any]:
    status = str(tool.get("status") or "unknown")
    window = tool.get("window") or {}
    start, end = clock(window.get("start"), day), clock(window.get("end"), day)
    content = tool.get("content")
    text = _pretty(content)
    cut = len(text) > RESULT_CHARS
    as_of = clock(tool.get("data_as_of"), day)
    age = _age(tool.get("freshness_seconds"))
    problem = status not in _OK_STATUSES
    return {
        "tool": tool.get("tool") or "tool",
        "status": status,
        "ok": not problem,
        "problem": problem,
        "reason": tool.get("reason"),
        "evidence_id": tool.get("evidence_id"),
        "query": _query_text(tool),
        "query_full": _expression(tool.get("query")),
        "arguments": _expression(tool.get("arguments")),
        "source": tool.get("source"),
        "target": tool.get("target_id"),
        "window": f"{start} – {end} UTC" if start and end else None,
        "freshness": (
            " · ".join(
                p
                for p in (
                    f"data as of {as_of} UTC" if as_of else None,
                    f"{age} old when read" if age else None,
                )
                if p
            )
            or None
        ),
        "took": _took(tool.get("duration_ms")),
        "table": _table(content),
        "result_text": text[:RESULT_CHARS] if text else "",
        "result_cut": cut,
        "has_result": content is not None,
        "message": tool.get("message"),
        "counts": _counts(tool),
        "adopted": tool.get("adopted"),
        "duplicate_of": tool.get("duplicate_of_evidence_id"),
        "http_status": tool.get("http_status"),
        "source_error": tool.get("source_error"),
        "source_error_truncated": tool.get("source_error_truncated"),
        "source_contact": tool.get("source_contact"),
    }


def _counts(tool: Mapping[str, Any]) -> str | None:
    parts = []
    returned, total = tool.get("returned_count"), tool.get("result_count")
    if isinstance(returned, int) and isinstance(total, int):
        parts.append(
            f"{returned} of {total} rows"
            if returned != total
            else _plural(total, "row")
        )
    omitted = tool.get("omitted_rows")
    if isinstance(omitted, int) and omitted:
        parts.append(f"{omitted} omitted")
    if tool.get("truncated"):
        parts.append("truncated")
    if tool.get("incomplete"):
        parts.append("incomplete")
    return " · ".join(parts) or None


# -- report ------------------------------------------------------------------


def _claims(report: Mapping[str, Any] | None) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {}
    cited: dict[str, list[str]] = {}
    if not report or not report.get("parsed"):
        return {"groups": groups, "cited": cited}
    for key, letter, _ in CLAIM_GROUPS:
        items = []
        for index, claim in enumerate(report.get(key) or [], start=1):
            cid = f"{letter}{index}"
            ids = [str(e) for e in claim.get("evidence_ids") or []]
            items.append(
                {
                    "id": cid,
                    "text": claim.get("text") or "",
                    "evidence_ids": ids,
                    "claim": claim,
                }
            )
            for eid in ids:
                cited.setdefault(eid, []).append(cid)
        groups[key] = items
    return {"groups": groups, "cited": cited}


def _report_block(
    report: Mapping[str, Any], kind: str, calls: Mapping[str, Any]
) -> dict[str, Any]:
    claims = _claims(report)
    groups = claims["groups"]

    def with_calls(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for item in items:
            refs = [{"eid": e, "call": calls.get(e)} for e in item["evidence_ids"]]
            out.append({**item, "refs": refs})
        return out

    level = str(report.get("conclusion") or "")
    return {
        "kind": kind,  # "published" or "incomplete"
        "level": level,
        "level_words": {
            "supported": "Supported by evidence",
            "partially_supported": "Partly supported",
            "inconclusive": "Inconclusive",
        }.get(level, level.replace("_", " ").capitalize()),
        "lede": opening(report.get("summary"), 320),
        "summary": report.get("summary") or "",
        "incomplete": bool(report.get("incomplete")),
        "facts": with_calls(groups.get("facts", [])),
        "hypotheses": with_calls(groups.get("hypotheses", [])),
        "counter": with_calls(groups.get("counter_evidence", [])),
        "ruled_out": with_calls(groups.get("rejected_hypotheses", [])),
        "recommendations": with_calls(groups.get("recommendations", [])),
        "unknown": list(report.get("unknown") or []),
        "next_steps": list(report.get("next_steps") or []),
        "sha256": report.get("content_sha256"),
        "cited": claims["cited"],
    }


def _could_not_see(snapshot: Mapping[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen = set()
    for name in snapshot.get("not_integrated") or []:
        rows.append({"name": str(name), "words": "not integrated"})
        seen.add(str(name))
    # A9: what the shown report's Run could see, when that is an earlier Run.
    coverage = snapshot.get("report_coverage") or snapshot.get("coverage") or {}
    for row in coverage.get("sources") or []:
        name, state = str(row.get("name")), row.get("state")
        if name in seen or state not in _COVERAGE_WORDS:
            continue
        words = _COVERAGE_WORDS[state]
        reasons = ", ".join(f"{r} ×{n}" for r, n in row.get("reasons") or [])
        rows.append(
            {"name": name, "words": words + (f" ({reasons})" if reasons else "")}
        )
    return rows


# -- recovery watch ------------------------------------------------------------


def _recovery(snapshot: Mapping[str, Any], day: str | None) -> dict[str, Any]:
    """The Recovery watch card: the latest session's verdict in words, with
    the replay's agreement. A stored verdict the offline replay cannot
    confirm is not repeated (integrity mismatch)."""
    views = snapshot.get("recovery") or []
    if not views:
        return {"session": None}
    view = views[-1]
    sessions = {s["session_id"]: s for s in snapshot.get("observation_sessions") or []}
    session = sessions.get(view.get("session_id"), {})
    replay = view.get("replay") or {}
    samples = view.get("samples") or []
    confirmed_at = next(
        (
            s.get("submitted_at") or s.get("window_end")
            for s in samples
            if s.get("transition") == "recovery_confirmed"
        ),
        None,
    )
    detail = None
    integrity = list(replay.get("integrity") or [])
    verified = bool(replay.get("recovery_confirmed") and replay.get("consistent"))
    tone = "run"
    if view.get("recovery_statement"):
        headline = view["recovery_statement"]
        headline = headline[0].upper() + headline[1:]
        tone = (
            "ok" if view.get("recovery_status") == "recovered_no_remediation" else "run"
        )
    elif (
        view.get("state") != "authorized"
        and not replay.get("consistent")
        and (integrity or replay.get("available") is False)
    ):
        if "STORED_OBSERVATION_DATA_CLEANED" in integrity:
            headline = "Raw data cleaned, cannot be re-verified"
        else:
            headline = "Integrity mismatch"
        detail = "the stored verdict is not repeated"
        tone = "err"
    elif verified and snapshot["incident"].lifecycle in ("resolved", "closed"):
        when = clock(confirmed_at, day)
        headline = "Recovery confirmed" + (f" {when} UTC" if when else "")
        tone = "ok"
    elif verified:
        when = clock(confirmed_at, day)
        headline = "No current recovery watch"
        detail = (
            "an earlier watch confirmed recovery"
            + (f" at {when} UTC" if when else "")
            + "; the incident has been reopened since"
        )
        tone = "muted"
    elif view.get("state") == "authorized":
        headline = "Watching; recovery not confirmed yet"
    else:
        headline = "Watch ended without confirming recovery"
        detail = " · ".join(
            str(v) for v in (view.get("state"), view.get("ended_reason")) if v
        )
        tone = "warn"
    max_samples = int(session.get("max_samples") or 0)
    healthy = sum(1 for s in samples if s.get("stored_outcome") == "healthy")
    points = []
    for sample in samples:
        outcome = sample.get("stored_outcome")
        points.append(
            {
                "kind": "ok"
                if outcome == "healthy"
                else "nd"
                if outcome in (None, "no_data", "unknown")
                else "bad",
                "outcome": outcome or "unknown",
                "at": clock(sample.get("window_end"), day),
            }
        )
    if replay.get("consistent"):
        replay_text = "offline recomputation consistent"
    elif replay.get("available"):
        replay_text = "integrity mismatch"
    else:
        replay_text = "not recomputed"
    return {
        "headline": headline,
        "detail": detail,
        "tone": tone,
        "view": view,
        "session": session,
        "points": points,
        "ongoing": view.get("state") == "authorized",
        "samples": len(samples),
        "healthy": healthy,
        "max_samples": max_samples,
        "authorized": clock(session.get("authorized_at"), day),
        "automatic": view.get("authorization_source") == "automatic",
        "replay": replay_text,
    }


# -- log -------------------------------------------------------------------


def _run_trigger(snapshot: Mapping[str, Any]) -> str:
    alert = snapshot.get("alert") or {}
    name = (alert.get("latest") or {}).get("alertname")
    if name:
        return f"from alert {name}"
    if snapshot.get("alert"):
        return "from an alert"
    if snapshot.get("question"):
        return "from a question"
    return "for this incident"


def _log(
    snapshot: Mapping[str, Any],
    calls_by_eid: dict[str, dict[str, Any]],
    report: dict[str, Any] | None,
    day: str | None,
) -> dict[str, Any]:
    """Log entries in time order: Run starts, steps with their tool calls,
    the Agent's replies, people's messages and controls, report and
    recovery-watch moments. Entries without a time keep their place after
    the dated ones of their Run."""
    run = snapshot.get("run") or {}
    run_id = run.get("run_id")
    entries: list[tuple[datetime | None, int, dict[str, Any]]] = []

    def add(at: Any, entry: dict[str, Any]) -> None:
        entries.append((_dt(at), len(entries), entry))

    events = snapshot.get("events") or []
    claimed = [
        e
        for e in events
        if e.get("kind") == "run_claimed"
        and (e.get("payload") or {}).get("run_id") == run_id
    ]
    started_at = claimed[0].get("recorded_at") if claimed else None
    for entry in snapshot.get("history") or []:
        words = {
            "completed": "ended with a report",
            "cancelled": "was canceled",
            "waiting_human": "stopped and handed back",
            "blocked": "was blocked",
        }.get(entry.get("state"), str(entry.get("state")))
        add(
            entry.get("started_at") or entry.get("concluded_at"),
            {
                "kind": "run",
                "time": clock(
                    entry.get("started_at") or entry.get("concluded_at"), day
                ),
                "title": f"Earlier investigation {words}",
                "detail": opening((entry.get("report") or {}).get("summary"), 160)
                if (entry.get("report") or {}).get("parsed")
                else None,
            },
        )
    first_step_at = next(
        (
            s.get("observed_at")
            for s in snapshot.get("steps") or []
            if s.get("observed_at")
        ),
        None,
    )
    begun = started_at or first_step_at
    # Contract A1/B4: a continuation Run carries a conversation mode other
    # than the first Run's report_required.
    conversation = run.get("conversation")
    if not isinstance(conversation, Mapping) or conversation.get("mode") in (
        None,
        "report_required",
    ):
        conversation = None
    add(
        begun or snapshot["incident"].created_at,
        {
            "kind": "run",
            "time": clock(begun or snapshot["incident"].created_at, day),
            "title": (
                "Investigation resumed" if conversation else "Investigation started"
            )
            if begun
            else ("Investigation resuming" if conversation else "Investigation queued"),
            "detail": (
                "after a message · reply only, no new queries"
                if (conversation or {}).get("mode") == "reply_only"
                else "after a message · same investigation, earlier report kept"
                if conversation
                else _run_trigger(snapshot)
            )
            + (
                f" · up to {run.get('budget_limit')} model requests"
                if run.get("budget_limit")
                else ""
            ),
        },
    )
    messages = {m["sequence"]: m for m in snapshot.get("messages") or []}
    shown_replies: set[tuple[Any, str]] = set()

    def reply_entry(
        reply: Mapping[str, Any], at: Any, step: int | None, final: bool
    ) -> None:
        to = reply.get("to_input_seq")
        text = reply.get("text") or ""
        shown_replies.add((to, text))
        asked = messages.get(to) if isinstance(to, int) else None
        add(
            at,
            {
                "kind": "reply",
                "time": clock(at, day),
                "step": step,
                "final": final,
                "text": text,
                "to": to,
                "to_author": None if asked is None else asked.get("author"),
                "evidence_ids": reply.get("evidence_ids") or [],
                "unsupported": reply.get("unsupported"),
            },
        )

    def add_replies(step: Mapping[str, Any], at_step: int, *, final: bool) -> None:
        for reply in step.get("replies") or []:
            reply_entry(reply, step.get("observed_at"), at_step or None, final)

    number = 0
    steps = snapshot.get("steps") or []
    for step in steps:
        if step.get("conclusion") is not None:
            c = step["conclusion"]
            if c.get("handoff"):
                reasons = c.get("reasons") or []
                add(
                    step.get("observed_at"),
                    {
                        "kind": "end",
                        "problem": True,
                        "time": clock(step.get("observed_at"), day),
                        "title": "Run stopped",
                        "detail": "; ".join(_reason_words(r) for r in reasons)
                        or "handed to a human",
                        "codes": reasons,
                    },
                )
            elif c.get("execution") == "completed":
                shown = report is not None and report.get("kind") == "published"
                add(
                    step.get("observed_at"),
                    {
                        "kind": "report",
                        "time": clock(step.get("observed_at"), day),
                        "title": "Report published" if shown else "Report written",
                        "detail": (
                            f"{_plural(len(report['facts']), 'fact')}, "
                            f"{_plural(len(report['hypotheses']), 'hypothesis', 'hypotheses')}, "
                            f"{len(report['ruled_out'])} ruled out"
                        )
                        if shown and report is not None
                        else None,
                        "anchor": "#conclusion" if shown else None,
                    },
                )
            elif c.get("execution") == "answered":
                # Contract A3/A4: replied only; the report stays as it was.
                add(
                    step.get("observed_at"),
                    {
                        "kind": "end",
                        "problem": False,
                        "time": clock(step.get("observed_at"), day),
                        "title": "Replied",
                        "detail": "the report is unchanged",
                        "codes": [],
                    },
                )
            else:
                add(
                    step.get("observed_at"),
                    {
                        "kind": "end",
                        "problem": c.get("execution") != "completed",
                        "time": clock(step.get("observed_at"), day),
                        "title": "Run ended",
                        "detail": str(c.get("execution") or ""),
                        "codes": c.get("reasons") or [],
                    },
                )
            add_replies(step, number, final=True)
            continue
        number += 1
        calls = []
        for tool in step.get("tools") or []:
            call = _call(tool, day)
            eid = call["evidence_id"]
            if eid and eid in calls_by_eid and calls_by_eid[eid].get("step") == number:
                call = calls_by_eid[eid]
            call["cited_by"] = (
                (report or {}).get("cited", {}).get(eid, [])
                if eid and call["ok"]
                else []
            )
            calls.append(call)
        problems = sum(1 for c in calls if c["problem"])
        note = step.get("note")
        if note is None and step.get("final_round"):
            note = "Wrote the final answer"
        add(
            step.get("observed_at"),
            {
                "kind": "step",
                "number": number,
                "id": f"step-{number}",
                "time": clock(step.get("observed_at"), day),
                "note": note,
                "calls": calls,
                "problem": problems > 0,
                "problems": problems,
                "history_only": bool(step.get("history_only")),
                "planned": step.get("planned_tools") or 0,
            },
        )
        add_replies(step, number, final=False)
    for row in snapshot.get("decision_log") or []:
        action = row.get("action")
        if action == "auto_authorize_observation":
            continue  # drawn from the session row below
        if action in MESSAGE_ACTIONS:
            add(
                row.get("decided_at"),
                {
                    "kind": "human",
                    "time": clock(row.get("decided_at"), day),
                    "author": row.get("actor_id"),
                    "text": row.get("text") or "",
                    "label": "correction" if action == "correct" else None,
                },
            )
            continue
        add(
            row.get("decided_at"),
            {
                "kind": "control",
                "time": clock(row.get("decided_at"), day),
                "title": f"{row.get('actor_id')} {DONE.get(action, str(action))}",
                "system": bool(row.get("system")),
                "detail": row.get("text") or None,
            },
        )
    answered = {
        r.get("to_input_seq") for r in snapshot.get("conversation_replies") or []
    }
    for message in messages.values():
        if message["unread"]:
            words = "Queued · read before the Agent's next step"
        elif message["sequence"] in answered:
            words = "Read and answered"
        else:
            words = "Read"
        add(
            message.get("received_at"),
            {
                "kind": "human",
                "time": clock(message.get("received_at"), day),
                "author": message.get("author"),
                "text": message.get("text") or "",
                "label": "from Chat"
                if message.get("channel") == "chat_forward"
                else None,
                "status": words,
            },
        )
        # Replies a Run other than the current one gave (contract B8) sit
        # right after the message they answer.
        for reply in snapshot.get("conversation_replies") or []:
            if reply.get("to_input_seq") != message["sequence"]:
                continue
            if (reply.get("to_input_seq"), reply.get("text") or "") in shown_replies:
                continue
            reply_entry(reply, message.get("received_at"), None, False)
    for session in snapshot.get("observation_sessions") or []:
        automatic = session.get("authorization_source") == "automatic"
        add(
            session.get("authorized_at"),
            {
                "kind": "watch",
                "time": clock(session.get("authorized_at"), day),
                "title": "Recovery watch started",
                "detail": (
                    "automatically after the root cause"
                    if automatic
                    else "after a human marked it remediated"
                )
                + f" · up to {session.get('max_samples')} samples",
            },
        )
    for view in snapshot.get("recovery") or []:
        replay = view.get("replay") or {}
        verified = replay.get("recovery_confirmed") and replay.get("consistent")
        for sample in view.get("samples") or []:
            if sample.get("transition") == "recovery_confirmed":
                at = sample.get("submitted_at") or sample.get("window_end")
                add(
                    at,
                    {
                        "kind": "watch",
                        "time": clock(at, day),
                        "title": "Recovery confirmed"
                        if verified
                        else "Recovery confirmation stored, not re-verified",
                        "detail": None,
                    },
                )
    floor = datetime.min.replace(tzinfo=timezone.utc)
    ordered = [e for _, _, e in sorted(entries, key=lambda x: (x[0] or floor, x[1]))]
    state = run.get("state")
    if state in ("queued", "running"):
        ordered.append(
            {
                "kind": "live",
                "title": "Waiting for a worker to pick up the Run"
                if state == "queued"
                else f"Step {number + 1} running",
                "since": iso(started_at) if state == "running" else None,
            }
        )
    step_entries = [e for e in ordered if e["kind"] == "step"]
    total_calls = sum(len(e["calls"]) for e in step_entries)
    bad = sum(e["problems"] for e in step_entries)
    return {
        "entries": ordered,
        "steps": len(step_entries),
        "calls": total_calls,
        "problems": bad,
        "started_at": iso(started_at),
        "messages": sum(1 for e in ordered if e["kind"] in ("human", "reply")),
    }


def _calls_by_eid(
    steps: Sequence[Mapping[str, Any]], day: str | None
) -> dict[str, dict[str, Any]]:
    """Each evidence id's first successful call, with its step number."""
    out: dict[str, dict[str, Any]] = {}
    number = 0
    for step in steps:
        if step.get("conclusion") is not None:
            continue
        number += 1
        for tool in step.get("tools") or []:
            eid = tool.get("evidence_id")
            if not eid or eid in out or tool.get("duplicate_of_evidence_id"):
                continue
            call = _call(tool, day)
            call["step"] = number
            out[str(eid)] = call
    return out


# -- header ------------------------------------------------------------------


def _title(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    alert = snapshot.get("alert") or {}
    latest = alert.get("latest") or {}
    annotations = latest.get("annotations") or {}
    summary = annotations.get("summary")
    outcome = alert.get("outcome")
    service = getattr(outcome, "affected_service", None) if outcome else None
    question = snapshot.get("question")
    incident = snapshot["incident"]
    if isinstance(summary, str) and summary.strip():
        title, origin = " ".join(summary.split()), "alert"
    elif latest.get("alertname"):
        title, origin = str(latest["alertname"]), "alert"
    elif question:
        title, origin = " ".join(str(question).split()), "question"
    else:
        title, origin = f"Incident {str(incident.incident_id)[:8]}", None
    if len(title) > 300:
        title = title[:299].rstrip() + "…"
    return {
        "title": title,
        "origin": origin,
        "alertname": latest.get("alertname"),
        "service": "/".join(service) if service else None,
    }


def _status_facts(
    status: IncidentStatus,
    snapshot: Mapping[str, Any],
    log: Mapping[str, Any],
    recovery: Mapping[str, Any],
    day: str | None,
) -> list[dict[str, Any]]:
    """The few facts next to the status: what the Agent or the watch is at,
    and who decided the current state. Each is a short phrase."""
    run = snapshot.get("run") or {}
    incident = snapshot["incident"]
    facts: list[dict[str, Any]] = []
    humans = [r for r in snapshot.get("decision_log") or [] if not r.get("system")]

    def last(*actions: str) -> Mapping[str, Any] | None:
        return next((r for r in reversed(humans) if r.get("action") in actions), None)

    def by(row: Mapping[str, Any] | None, verb: str) -> None:
        if row is not None:
            when = clock(row.get("decided_at"), day)
            facts.append(
                {
                    "text": f"{verb} by {row.get('actor_id')}"
                    + (f" at {when} UTC" if when else "")
                }
            )

    key = status.key
    if key == "investigating":
        if run.get("state") == "queued":
            facts.append({"text": "Waiting for a worker"})
        else:
            facts.append({"text": f"Step {log['steps'] + 1}"})
        if log.get("started_at") and run.get("state") == "running":
            facts.append({"text": "elapsed", "since": log["started_at"]})
        facts.append(
            {
                "text": f"{run.get('budget_spent', 0)} of {run.get('budget_limit', 0)} model requests",
                "muted": True,
            }
        )
    elif key == "watching" and recovery.get("session"):
        facts.append(
            {
                "text": f"{recovery['healthy']} of {recovery['max_samples'] or recovery['samples']} samples healthy",
                "strong": True,
            }
        )
        if recovery.get("authorized"):
            facts.append({"text": f"since {recovery['authorized']} UTC", "muted": True})
    elif key == "paused":
        by(last("pause"), "Paused")
        if incident.state != "paused":
            facts.append({"text": "a suspension holds this target"})
        facts.append(
            {"text": "the Agent makes no progress until resumed", "muted": True}
        )
    elif key == "human_owned":
        by(last("takeover"), "Taken over")
        facts.append({"text": "the Agent does not investigate", "muted": True})
    elif key == "canceled":
        by(last("cancel"), "Canceled")
    elif key == "needs_attention":
        outcome = snapshot.get("outcome") or {}
        reasons = outcome.get("reasons") or []
        if run.get("state") == "waiting_human":
            facts.append(
                {
                    "text": "The Agent stopped: "
                    + (
                        "; ".join(_reason_words(r) for r in reasons)
                        or "it handed the Run back"
                    )
                }
            )
        elif run.get("state") == "blocked":
            facts.append(
                {
                    "text": "The Run was blocked: "
                    + (
                        "; ".join(_reason_words(r) for r in reasons)
                        or "versions do not match"
                    )
                }
            )
        elif run.get("state"):
            facts.append({"text": f"Run {run.get('state')}"})
    elif key == "resolved":
        facts.append({"text": recovery.get("headline") or "Recovery confirmed"})
    elif key == "closed":
        by(last("close"), "Closed")
    elif key == "root_cause":
        facts.append({"text": "no recovery watch running", "muted": True})
    return facts


def _actions(
    status: IncidentStatus, offered: Sequence[str], snapshot: Mapping[str, Any]
) -> dict[str, Any]:
    allowed = [a for a in offered if a not in COMPOSER_ACTIONS]
    primary_keys, more_keys = LAYOUT.get(status.key, ((), ()))
    primary = [a for a in primary_keys if a in allowed][:2]
    more = [a for a in more_keys if a in allowed and a not in primary]
    more += sorted(a for a in allowed if a not in primary and a not in more)
    lead = LEAD.get(status.key)

    def item(action: str) -> dict[str, Any]:
        return {
            "action": action,
            "button": BUTTON.get(action, action),
            "hint": HINT.get(action, ""),
            "lead": action == lead or (lead == "reinvestigate" and action == "new_run"),
            "input": action in ("register_remediation", "rebind_target", "merge_into"),
        }

    return {"primary": [item(a) for a in primary], "more": [item(a) for a in more]}


def _composer(
    status: IncidentStatus,
    snapshot: Mapping[str, Any],
    report_kind: str | None,
    message_available: bool,
    offered: Sequence[str] = (),
) -> dict[str, Any]:
    """Contract D1-D10: whether the investigation takes a message in this
    state and what the box says. ``message_available`` is whether the
    server has the ``message`` action (P2). With it, the server's own
    admission rule (``available_actions`` offering ``message``) decides;
    the per-state wording stays. A refused state offers Re-investigate when
    the server does (D11)."""
    incident = snapshot["incident"]
    concluded = bool(incident.concluded)
    key = status.key
    label = "Message the investigation"
    hint = "It reads this before its next step and replies here."
    placeholder = (
        "New information or a direction, e.g. “check the payment DB connection pool”"
    )
    accepts = True
    refusal = None
    if key == "merged":
        accepts, refusal = (
            False,
            "This incident was merged. Message the incident it was merged into.",
        )
    elif key == "canceled":
        accepts, refusal = (
            False,
            "This investigation was canceled. Re-investigate to start a new one; it will read your message.",
        )
    elif key in ("resolved", "closed"):
        if concluded:
            hint = (
                "Reply only: the Agent answers from the record and runs no new queries."
            )
            placeholder = (
                "A question about what was found, e.g. “summarise the root cause”"
            )
        else:
            accepts, refusal = (
                False,
                "This incident closed without a conclusion. Reopen, then Re-investigate.",
            )
    elif key == "paused":
        hint = "Paused: the Agent reads this after the investigation is resumed."
    elif key == "human_owned":
        label = "Add a note"
        hint = "The Agent is not investigating; your note is recorded on the incident."
        placeholder = "What you found or did"
    elif key == "needs_attention" and report_kind != "incomplete":
        accepts, refusal = (
            False,
            "This investigation ended without an answer. Re-investigate to start a new one.",
        )
    if message_available:
        offered_message = "message" in offered
        if offered_message and not accepts:
            accepts, refusal = True, None
        elif not offered_message and accepts:
            accepts = False
            refusal = "This investigation is not taking messages now." + (
                " Re-investigate to start a new one."
                if "reinvestigate" in offered
                else ""
            )
    reinvestigate = next(
        (a for a in ("reinvestigate", "new_run") if a in offered), None
    )
    return {
        "label": label,
        "hint": hint,
        "placeholder": placeholder,
        "accepts": accepts,
        "refusal": refusal,
        "enabled": accepts and message_available,
        "unavailable": accepts and not message_available,
        "reinvestigate": None if accepts else reinvestigate,
    }


def _day(snapshot: Mapping[str, Any]) -> str | None:
    created = _dt(snapshot["incident"].created_at)
    return None if created is None else created.strftime("%Y-%m-%d")


def _shown_report(
    snapshot: Mapping[str, Any],
) -> tuple[Mapping[str, Any] | None, str | None]:
    """The report the result area shows: the published one, else (A8) the
    newest validated report the Agent marked incomplete -- the current
    Run's, or an earlier Run's when the current one has none."""
    report = snapshot.get("report")
    if report is not None:
        return report, "published"
    handoff = snapshot.get("handoff_report")
    reasons = (snapshot.get("outcome") or {}).get("reasons") or []
    if (
        handoff
        and handoff.get("parsed")
        and list(reasons) == ["INCOMPLETE_INVESTIGATION"]
    ):
        return handoff, "incomplete"
    # A8/D7a look-back: the current Run has none, an earlier one does.
    earlier = snapshot.get("incomplete_report")
    if earlier and (earlier.get("report") or {}).get("parsed"):
        return earlier["report"], "incomplete"
    return None, None


def page_view(
    snapshot: Mapping[str, Any],
    postmortem: Mapping[str, Any] | None,
    actions: Sequence[str],
    *,
    message_available: bool = False,
) -> dict[str, Any]:
    """Everything the incident page derives, from data already on it."""
    day = _day(snapshot)
    incident = snapshot["incident"]
    run = snapshot.get("run") or {}
    status = incident_status(
        merged=getattr(incident, "merged_into_incident_id", None) is not None,
        lifecycle=incident.lifecycle,
        state=incident.state,
        run_state=run.get("state"),
        mode=incident.mode,
        concluded=bool(incident.concluded),
    )
    steps = snapshot.get("steps") or []
    calls = _calls_by_eid(steps, day)
    # A9: the shown report's evidence may be an earlier Run's steps.
    for eid, call in _calls_by_eid(snapshot.get("report_steps") or [], day).items():
        call["step"] = None
        calls.setdefault(eid, call)
    shown, kind = _shown_report(snapshot)
    report = None
    unparsed = None
    if shown is not None and shown.get("parsed"):
        report = _report_block(shown, kind or "published", calls)
    elif shown is not None:
        unparsed = {
            "reason": shown.get("reason"),
            "sha256": shown.get("content_sha256"),
        }
    log = _log(snapshot, calls, report, day)
    recovery = _recovery(snapshot, day)
    step_entries = [e for e in log["entries"] if e["kind"] == "step"]
    latest = step_entries[-1] if step_entries else None
    # U1: without a report, what the Run has gathered so far.
    collected = [] if report is not None else list(calls.values())
    outcome = snapshot.get("outcome") or {}
    return {
        "day": day,
        "status": status,
        "header": _title(snapshot),
        "facts": _status_facts(status, snapshot, log, recovery, day),
        "actions": _actions(status, actions, snapshot),
        "report": report,
        "unparsed": unparsed,
        # A9: an earlier report stays while a new Run works.
        "revising": report is not None and status.key == "investigating",
        "collected": collected,
        "latest_step": latest,
        "ended": {
            "reasons": [_reason_words(r) for r in outcome.get("reasons") or []],
            "codes": list(outcome.get("reasons") or []),
            "execution": outcome.get("execution"),
        }
        if outcome and report is None
        else None,
        "could_not_see": _could_not_see(snapshot),
        "log": log,
        # Contract B7: messages above every Run's read watermark.
        "queued": [
            {
                "author": m.get("author"),
                "time": clock(m.get("received_at"), day),
                "text": m.get("text") or "",
            }
            for m in snapshot.get("messages") or []
            if m.get("unread")
        ],
        "composer": _composer(status, snapshot, kind, message_available, actions),
        "recovery": recovery,
        "charts": _charts_by_eid(snapshot.get("charts")),
        "clock": lambda value: clock(value, day),
        "iso": iso,
        "stamp": stamp,
    }


_CHART_EID = re.compile(r'data-evidence-id="([^"]+)"')


def _charts_by_eid(charts: Mapping[str, Any] | None) -> dict[str, Any]:
    """Each cited metrics chart (``charts.evidence_chart``, already safe
    markup) under its evidence id, so the evidence detail can show it."""
    out: dict[str, Any] = {}
    for item in (charts or {}).get("items") or []:
        match = _CHART_EID.search(str(item))
        if match:
            out.setdefault(html_unescape(match.group(1)), item)
    return out


def handoff_view(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """The header of the handoff-only page (no Run, contract D9)."""
    day = _day(snapshot)
    incident = snapshot["incident"]
    status = incident_status(
        merged=getattr(incident, "merged_into_incident_id", None) is not None,
        lifecycle=incident.lifecycle,
        state=incident.state,
        run_state=None,
        mode=incident.mode,
        concluded=bool(incident.concluded),
    )
    return {
        "day": day,
        "status": status,
        "header": _title(snapshot),
        "clock": lambda value: clock(value, day),
        "iso": iso,
    }
