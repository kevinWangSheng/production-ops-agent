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

__all__ = ["page_view", "handoff_view", "clock", "opening", "LIVE_KINDS"]

#: Event kinds the incident page refreshes on (P3-14/17): every kind is a
#: "something changed" signal; the page then reads the live view.
LIVE_KINDS = (
    "intake_accepted",
    "run_claimed",
    "run_claim_refused",
    "run_resumed",
    "step_committed",
    "tool_committed",
    "run_completed",
    "run_handoff",
    "control_applied",
    "message_received",
    "observation_authorized",
    "auto_observation_handed_off",
    "reload",
)

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
        # P3-6: an earlier Run's raw result is on its evidence page only.
        "elided": bool(tool.get("elided")),
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
        **_lede(report.get("summary")),
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


def _lede(summary: Any) -> dict[str, str]:
    """The summary verbatim (whitespace folded), split after its first
    sentence: the page sets the first sentence large and keeps the rest
    beside it, so nothing of the published summary is hidden."""
    text = " ".join(str(summary or "").split())
    match = re.search(r"(?<=[.!?])\s", text)
    if match is None:
        return {"lede": text, "rest": ""}
    return {"lede": text[: match.start()], "rest": text[match.end() :]}


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


#: Run states in which a Run may still read or answer a message (C8).
_ACTIVE = ("queued", "running", "paused")

#: ``report_rejected_reason`` values in words (A3/A6); others as is.
_REJECTED_WORDS = {
    "REPLY_ONLY": "a reply-only Run keeps no report",
    "INCOMPLETE_INVESTIGATION": "the Agent said the new report is incomplete",
}


def _runs_of(snapshot: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The incident's Runs oldest first (P3-1); a snapshot without the
    per-Run read holds only the current Run."""
    runs = snapshot.get("runs")
    if runs:
        return list(runs)
    run = snapshot.get("run") or {}
    conversation = run.get("conversation")
    mode = conversation.get("mode") if isinstance(conversation, Mapping) else None
    return [
        {
            "run_id": run.get("run_id"),
            "state": run.get("state"),
            "current": True,
            "origin": {"kind": "resumed", "mode": mode}
            if mode in ("report_optional", "reply_only")
            else {"kind": "started"},
            "steps": snapshot.get("steps") or [],
            "inline": True,
            "floor": 0,
            "watermark": int(snapshot.get("read_floor") or 0),
            "budget_limit": run.get("budget_limit"),
        }
    ]


def _numbered(
    runs: Sequence[Mapping[str, Any]],
) -> list[tuple[Mapping[str, Any], Mapping[str, Any], int | None]]:
    """Each Run's steps with one step count across the incident (P3-5):
    ``(run, step, number)``, ``number`` ``None`` for a conclusion step and
    for a late result (history only), so the count never depends on whether
    a Run's late rows are read (P3-1: earlier Runs carry none)."""
    out: list[tuple[Mapping[str, Any], Mapping[str, Any], int | None]] = []
    number = 0
    for run in runs:
        for step in run.get("steps") or []:
            if step.get("conclusion") is not None or step.get("history_only"):
                out.append((run, step, None))
                continue
            number += 1
            out.append((run, step, number))
    return out


def _message_status(
    snapshot: Mapping[str, Any],
    runs: Sequence[Mapping[str, Any]],
    numbered: Sequence[tuple[Mapping[str, Any], Mapping[str, Any], int | None]],
) -> dict[int, dict[str, Any]]:
    """Per message: read at which step, answered, or not answered (P3-9,
    C8 per Run: a message in a Run's range ``floor < seq <= watermark`` that
    none of the Run's steps answered once the Run is no longer active).
    Computed on read, never stored."""
    answered: set[int] = set()
    numbered = [item for item in numbered if not item[1].get("history_only")]
    for _, step, _ in numbered:
        for reply in step.get("replies") or []:
            to = reply.get("to_input_seq")
            if isinstance(to, int):
                answered.add(to)
    out: dict[int, dict[str, Any]] = {}
    for message in snapshot.get("messages") or []:
        seq = message["sequence"]
        read_at = None
        last = None
        for _, step, number in numbered:
            if number is not None:
                last = number
            mark = step.get("input_watermark")
            if isinstance(mark, int) and mark >= seq:
                read_at = number if number is not None else last
                break
        covering = [
            r
            for r in runs
            if int(r.get("floor") or 0) < seq <= int(r.get("watermark") or 0)
        ]
        if message.get("unread"):
            state = "queued"
        elif seq in answered:
            state = "answered"
        elif any(r.get("state") in _ACTIVE for r in covering):
            state = "read"
        elif covering:
            state = "not_answered"
        else:
            state = "read"
        out[seq] = {"state": state, "step": read_at}
    return out


def _log(
    snapshot: Mapping[str, Any],
    calls_by_eid: dict[str, dict[str, Any]],
    report: dict[str, Any] | None,
    day: str | None,
    unread_words: str | None = None,
    composer: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One log for the whole incident (P3-1), in time order: each Run's
    start, its steps with their tool calls, the Agent's replies and the
    Run's end; people's messages and controls; report and recovery-watch
    moments. An entry without a time takes the time of the entry before it
    in its Run (P3-4). Every entry has a stable ``key`` for live updates."""
    incident = snapshot["incident"]
    run = snapshot.get("run") or {}
    runs = _runs_of(snapshot)
    numbered = _numbered(runs)
    entries: list[tuple[datetime | None, int, dict[str, Any]]] = []

    def add(at: Any, entry: dict[str, Any]) -> None:
        entries.append((_dt(at), len(entries), entry))

    claimed: dict[str, Any] = {}
    for event in snapshot.get("events") or []:
        if event.get("kind") == "run_claimed":
            rid = str((event.get("payload") or {}).get("run_id"))
            claimed.setdefault(rid, event.get("recorded_at"))
    current_id = str(run.get("run_id"))
    started_at = claimed.get(current_id)
    messages = {m["sequence"]: m for m in snapshot.get("messages") or []}
    # P3-7: the newest published report is the one the result area shows.
    published = [
        step
        for _, step, number in numbered
        if number is None
        and step.get("conclusion") is not None
        and not step.get("history_only")
        and step["conclusion"].get("execution") == "completed"
        and not step["conclusion"].get("handoff")
    ]
    shown_step = (
        published[-1]
        if published and report is not None and report.get("kind") == "published"
        else None
    )
    concluded = bool(incident.concluded)

    def reply_entry(
        run_entries: list[tuple[Any, dict[str, Any]]],
        reply: Mapping[str, Any],
        at: Any,
        step: int | None,
        final: bool,
        key: str,
    ) -> None:
        to = reply.get("to_input_seq")
        asked = messages.get(to) if isinstance(to, int) else None
        run_entries.append(
            (
                at,
                {
                    "kind": "reply",
                    "key": key,
                    "step": step,
                    "final": final,
                    "text": reply.get("text") or "",
                    "to": to,
                    "to_author": None if asked is None else asked.get("author"),
                    "evidence_ids": reply.get("evidence_ids") or [],
                    # Provenance: each cited id, linked when this page knows
                    # the observation.
                    "evidence": [
                        {"eid": str(eid), "call": calls_by_eid.get(str(eid))}
                        for eid in reply.get("evidence_ids") or []
                    ],
                    "unsupported": reply.get("unsupported"),
                },
            )
        )

    first_run = True
    seen_published = 0
    for item in runs:
        rid = str(item.get("run_id"))
        is_current = bool(item.get("current"))
        steps = [(s, n) for r, s, n in numbered if r is item]
        run_entries: list[tuple[Any, dict[str, Any]]] = []
        first_step_at = next(
            (s.get("observed_at") for s, _ in steps if s.get("observed_at")), None
        )
        begun_at = claimed.get(rid) or first_step_at
        begun = bool(begun_at) or bool(steps) or item.get("state") not in _ACTIVE
        origin = item.get("origin") or {}
        kind = origin.get("kind")
        budget = (
            f" · up to {item.get('budget_limit')} model requests"
            if item.get("budget_limit")
            else ""
        )
        if kind == "resumed":
            title = "Investigation resumed" if begun else "Investigation resuming"
            detail = (
                "after a message · reply only, no new queries"
                if origin.get("mode") == "reply_only"
                else "after a message · same investigation, earlier report kept"
            )
        elif kind == "reinvestigated":
            title = f"Re-investigated by {origin.get('actor')}"
            detail = "a new investigation that reads every message so far"
        elif kind == "continued":
            title = "Investigation continued"
            detail = "after a follow-up"
        else:
            title = "Investigation started" if begun else "Investigation queued"
            detail = _run_trigger(snapshot)
        boundary_at = (
            begun_at
            or item.get("opened_at")
            or (incident.created_at if first_run else None)
        )
        first_run = False
        run_entries.append(
            (
                boundary_at,
                {
                    "kind": "run",
                    "key": f"run-{rid}",
                    "title": title,
                    "detail": detail + budget,
                    "run_id": rid,
                },
            )
        )
        for step, number in steps:
            at = step.get("observed_at")
            sid = step.get("step_id") or f"{rid}-{len(run_entries)}"
            late = bool(step.get("history_only"))
            c = None if late else step.get("conclusion")
            if c is not None:
                reasons = c.get("reasons") or []
                end: dict[str, Any]
                if c.get("handoff") and "RESPONSE_INVALID" in reasons:
                    end = {
                        "kind": "end",
                        "problem": True,
                        "title": "Agent couldn't respond",
                        "detail": _reason_words("RESPONSE_INVALID")
                        + (" · the report is unchanged" if concluded else ""),
                        "codes": reasons,
                    }
                elif (
                    c.get("handoff")
                    and c.get("execution") == "completed"
                    and list(reasons) == ["INCOMPLETE_INVESTIGATION"]
                ):
                    end = {
                        "kind": "report",
                        "title": "Report written · incomplete",
                        "detail": "the Agent said its investigation is incomplete; not published",
                        "anchor": "#conclusion"
                        if report is not None and report.get("kind") == "incomplete"
                        else None,
                    }
                elif c.get("handoff"):
                    end = {
                        "kind": "end",
                        "problem": True,
                        "title": "Run stopped",
                        "detail": "; ".join(_reason_words(r) for r in reasons)
                        or "handed to a human",
                        "codes": reasons,
                    }
                elif c.get("execution") == "completed":
                    seen_published += 1
                    shown = step is shown_step
                    summary = ((item.get("report") or {}) if not shown else {}).get(
                        "summary"
                    )
                    end = {
                        "kind": "report",
                        "title": "Report published"
                        if seen_published == 1
                        else "Report updated",
                        "detail": (
                            f"{_plural(len(report['facts']), 'fact')}, "
                            f"{_plural(len(report['hypotheses']), 'hypothesis', 'hypotheses')}, "
                            f"{len(report['ruled_out'])} ruled out"
                        )
                        if shown and report is not None
                        else opening(summary, 160),
                        "anchor": "#conclusion" if shown else None,
                    }
                elif c.get("execution") == "answered":
                    # Contract A3/A4: replied only; the report stays as it was.
                    rejected = c.get("report_rejected")
                    if rejected:
                        end = {
                            "kind": "end",
                            "problem": False,
                            "title": "Report update rejected",
                            "detail": _REJECTED_WORDS.get(
                                rejected, _reason_words(rejected)
                            )
                            + " · the report is unchanged",
                            "codes": [rejected],
                        }
                    else:
                        end = {
                            "kind": "end",
                            "problem": False,
                            "title": "Replied",
                            "detail": "the report is unchanged",
                            "codes": [],
                        }
                else:
                    end = {
                        "kind": "end",
                        "problem": True,
                        "title": "Run ended",
                        "detail": str(c.get("execution") or ""),
                        "codes": reasons,
                    }
                end["key"] = f"end-{sid}"
                run_entries.append((at, end))
                # C7: the final output answers; it belongs to the Run's
                # last numbered step (the final round).
                last = max((n for _, n in steps if n is not None), default=None)
                for index, reply in enumerate(step.get("replies") or []):
                    reply_entry(
                        run_entries, reply, at, last, True, f"reply-{sid}-{index}"
                    )
                continue
            calls = []
            for tool in step.get("tools") or []:
                call = _call(tool, day)
                eid = call["evidence_id"]
                if (
                    eid
                    and eid in calls_by_eid
                    and number is not None
                    and calls_by_eid[eid].get("step") == number
                ):
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
            anchor = f"late-{sid}" if late else f"step-{number}"
            run_entries.append(
                (
                    at,
                    {
                        "kind": "step",
                        "key": anchor,
                        "number": number,
                        "id": anchor,
                        "note": note,
                        "calls": calls,
                        "problem": problems > 0,
                        "problems": problems,
                        "history_only": bool(step.get("history_only")),
                        "planned": step.get("planned_tools") or 0,
                        "earlier_run": not is_current,
                    },
                )
            )
            # A late result's replies were fenced off: never an answer.
            for index, reply in enumerate([] if late else step.get("replies") or []):
                reply_entry(
                    run_entries, reply, at, number, False, f"reply-{sid}-{index}"
                )
        # P3-4: an entry without a time takes the time of the one before it
        # in its Run; a Run start without one takes its first dated entry.
        carry = next((at for at, _ in run_entries if at), None)
        for at, entry in run_entries:
            if at:
                carry = at
            add(carry, entry)
    for row in snapshot.get("decision_log") or []:
        action = row.get("action")
        if action == "auto_authorize_observation":
            continue  # drawn from the session row below
        generation = row.get("generation")
        if action in MESSAGE_ACTIONS:
            # B3: a legacy note; never queued, never "not answered".
            add(
                row.get("decided_at"),
                {
                    "kind": "human",
                    "key": f"note-{generation}-{action}",
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
                "key": f"control-{generation}-{action}",
                "title": f"{row.get('actor_id')} {DONE.get(action, str(action))}",
                "system": bool(row.get("system")),
                "detail": row.get("text") or None,
            },
        )
    # Workbench P6 L3: system events, drawn from system-written fields only;
    # never messages, never queued.
    for linked in (snapshot.get("linked") or {}).get("rows") or []:
        detail = f"“{linked['title']}”"
        if linked.get("actor"):
            detail += f" · by {linked['actor']}"
        add(
            linked.get("merged_at"),
            {
                "kind": "event",
                "key": f"merged-{linked['incident_id']}",
                "title": f"Incident {linked['incident_id'][:8]} merged into this",
                "detail": detail,
                "anchor": f"/incidents/{linked['incident_id']}",
            },
        )
    for refire in snapshot.get("refires") or []:
        add(
            refire.get("received_at"),
            {
                "kind": "event",
                "key": f"refire-{refire['delivery_key']}",
                "title": "Alert fired",
                "detail": "attached to this incident"
                + (
                    f" · startsAt {refire['starts_at']}"
                    if refire.get("starts_at")
                    else ""
                ),
            },
        )
    for link in snapshot.get("release_links") or []:
        if link.get("disposition") != "attached":
            continue  # the opening link is in the header, not an event
        observation = str(link.get("observation_id"))
        add(
            link.get("consumed_at"),
            {
                "kind": "event",
                "key": f"release-{observation}",
                "title": f"Version change {observation[:8]} joined",
                "detail": "attached to this incident",
                "anchor": f"/version-changes/{observation}",
            },
        )
    statuses = _message_status(snapshot, runs, numbered)
    composer = composer or {}
    retry_ok = bool(composer.get("enabled")) and incident.mode != "human_owned"
    for message in messages.values():
        seq = message["sequence"]
        status = statuses.get(seq) or {"state": "read", "step": None}
        retry = None
        if status["state"] == "queued":
            words = unread_words or "Queued · read before the Agent's next step"
        elif status["state"] == "answered":
            words = "Answered"
        elif status["state"] == "not_answered":
            words = "Not answered"
            if message.get("retried"):
                words += " · sent again"
            elif retry_ok:
                retry = seq
            elif composer.get("reinvestigate"):
                words += " · send it again after Re-investigate"
        elif status["step"]:
            words = f"Read at step {status['step']}"
        else:
            words = "Read"
        add(
            message.get("received_at"),
            {
                "kind": "human",
                "key": f"msg-{seq}",
                "author": message.get("author"),
                "text": message.get("text") or "",
                "label": "sent again"
                if message.get("retry_of") is not None
                else "from Chat"
                if message.get("channel") == "chat_forward"
                else None,
                "status": words,
                "state": status["state"],
                # P3-5: stays after the message is answered.
                "read_at_step": status["step"],
                "sequence": seq,
                "retry": retry,
            },
        )
    for session in snapshot.get("observation_sessions") or []:
        automatic = session.get("authorization_source") == "automatic"
        add(
            session.get("authorized_at"),
            {
                "kind": "watch",
                "key": f"watch-{session.get('session_id')}",
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
                        "key": f"confirmed-{view.get('session_id')}-{sample.get('sequence')}",
                        "title": "Recovery confirmed"
                        if verified
                        else "Recovery confirmation stored, not re-verified",
                        "detail": None,
                    },
                )
    floor = datetime.min.replace(tzinfo=timezone.utc)
    ordered = []
    for at, _, entry in sorted(entries, key=lambda x: (x[0] or floor, x[1])):
        entry["time"] = clock(at, day)
        ordered.append(entry)
    state = run.get("state")
    total_steps = sum(1 for _, _, n in numbered if n is not None)
    if state in ("queued", "running"):
        ordered.append(
            {
                "kind": "live",
                "key": "live",
                "title": "Waiting for a worker to pick up the Run"
                if state == "queued"
                else f"Step {total_steps + 1} running",
                "since": iso(started_at) if state == "running" else None,
            }
        )
    step_entries = [e for e in ordered if e["kind"] == "step"]
    numbered_entries = [e for e in step_entries if e["number"] is not None]
    total_calls = sum(len(e["calls"]) for e in step_entries)
    bad = sum(e["problems"] for e in step_entries)
    return {
        "entries": ordered,
        "steps": len(numbered_entries),
        "calls": total_calls,
        "problems": bad,
        "started_at": iso(started_at),
        "messages": sum(1 for e in ordered if e["kind"] in ("human", "reply")),
        "runs": len(runs),
    }


def _calls_by_eid(
    runs: Sequence[Mapping[str, Any]], day: str | None
) -> dict[str, dict[str, Any]]:
    """Each evidence id's first call, with its step number in the one count
    across the incident (P3-5) and whether its Run's raw results are drawn
    on this page (P3-6)."""
    out: dict[str, dict[str, Any]] = {}
    for run, step, number in _numbered(runs):
        if number is None:
            continue
        for tool in step.get("tools") or []:
            eid = tool.get("evidence_id")
            if not eid or eid in out or tool.get("duplicate_of_evidence_id"):
                continue
            call = _call(tool, day)
            call["step"] = number
            call["inline"] = bool(run.get("inline"))
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


#: Workbench P6 L7: what a release link did to this incident, in words.
RELEASE_WORDS = {
    "created": "opened this incident",
    "created_related": "opened this incident after a closed one",
    "attached": "joined this incident",
    "handoff_created": "opened this incident for a person to handle",
}
#: A merged source's lifecycle at the merge (``_merge_event``), in words.
_LIFECYCLE_WORDS = {
    "open": "open",
    "observing_recovery": "watching recovery",
    "resolved": "resolved",
    "closed": "closed",
}


def _links(snapshot: Mapping[str, Any], day: str | None) -> dict[str, Any]:
    """Workbench P6: the identity area's links -- merged sources with their
    titles and reasons (L1/L2), the reason this incident was merged (L2),
    the incidents opened after it (L6), release links and target changes
    in words with their times (L7)."""
    linked = snapshot.get("linked") or {}
    rows = [
        {
            **row,
            "short": row["incident_id"][:8],
            "was": _LIFECYCLE_WORDS.get(
                str(row.get("lifecycle_at_merge")), row.get("lifecycle_at_merge")
            ),
            "opened": clock(row.get("created_at"), day),
            "merged": clock(row.get("merged_at"), day),
            "merged_iso": iso(row.get("merged_at")),
        }
        for row in linked.get("rows") or []
    ]
    followed = snapshot.get("followed_by") or {}
    merge_reason = None
    for row in snapshot.get("decision_log") or []:
        if row.get("action") == "merge_into":
            text = row.get("text")
            merge_reason = text if isinstance(text, str) and text.strip() else None
    return {
        "linked": rows,
        "linked_cut": bool(linked.get("truncated")),
        "followed_by": [
            {**row, "short": row["incident_id"][:8]}
            for row in followed.get("rows") or []
        ],
        "followed_cut": bool(followed.get("truncated")),
        "merge_reason": merge_reason,
        "release_words": RELEASE_WORDS,
        "rebinds": [
            {
                **rebind,
                "time": clock(rebind.get("decided_at"), day),
                "iso": iso(rebind.get("decided_at")),
            }
            for rebind in snapshot.get("rebinds") or []
        ],
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
    elif incident.mode == "human_owned":
        # D8 before any status: a human-owned incident's Agent reads nothing,
        # paused, canceled, resolved or closed alike; the note is recorded.
        label = "Add a note"
        hint = "Add a note — the agent is not investigating. It is recorded on the incident."
        placeholder = "What you found or did"
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


def shown_report_ref(snapshot: Mapping[str, Any]) -> tuple[str, str | None] | None:
    """Workbench P5 F8/F9: the Run and report sha256 a feedback rates -- the
    report the result area shows, when it parses; ``None`` otherwise (the
    page offers no feedback and the server refuses ``NO_REPORT``)."""
    if snapshot.get("handoff_only"):
        return None
    shown, kind = _shown_report(snapshot)
    if shown is None or not shown.get("parsed"):
        return None
    report: Mapping[str, Any] = shown
    run_id = (snapshot.get("run") or {}).get("run_id")
    if kind == "published":
        # A9: an earlier Run's conclusion while a continuation Run is current.
        run_id = snapshot.get("report_run_id") or run_id
    elif report is not snapshot.get("handoff_report"):
        earlier = snapshot.get("incomplete_report") or {}
        run_id = earlier.get("run_id") or run_id
    if run_id is None:
        return None
    sha = report.get("content_sha256")
    return str(run_id), None if sha is None else str(sha)


def feedback_offer(
    status: IncidentStatus, snapshot: Mapping[str, Any]
) -> dict[str, Any] | None:
    """F8: offered while the page shows a report and the incident is not
    merged; Resolved and Closed put it among the primary buttons, every other
    status under More."""
    if status.key == "merged" or shown_report_ref(snapshot) is None:
        return None
    return {"primary": status.key in ("resolved", "closed")}


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
    runs = _runs_of(snapshot)
    calls = _calls_by_eid(runs, day)
    # A9: the shown report's evidence may be an earlier Run's steps; without
    # the per-Run read they have no number on this page.
    earlier = [{"steps": snapshot.get("report_steps") or [], "inline": True}]
    for eid, call in _calls_by_eid(earlier, day).items():
        if eid not in calls:
            call["step"] = None
            calls[eid] = call
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
    composer = _composer(status, snapshot, kind, message_available, actions)
    # D8/D12: an unread message no Run will read says so instead of "queued".
    unread_words = None
    if incident.mode == "human_owned":
        unread_words = "Recorded on the incident — the agent is not investigating"
    elif not composer["accepts"] and run.get("state") not in (
        "queued",
        "running",
        "paused",
    ):
        unread_words = (
            "Not read — the investigation ended. Re-investigate will include it."
        )
    log = _log(snapshot, calls, report, day, unread_words, composer)
    recovery = _recovery(snapshot, day)
    step_entries = [
        e for e in log["entries"] if e["kind"] == "step" and e["number"] is not None
    ]
    latest = step_entries[-1] if step_entries else None
    # U1: without a report, what the Run has gathered so far.
    collected = (
        [] if report is not None else [c for c in calls.values() if c.get("inline")]
    )
    outcome = snapshot.get("outcome") or {}
    links = _links(snapshot, day)
    return {
        "day": day,
        "status": status,
        "header": _title(snapshot),
        "links": links,
        # Workbench P6 L1/L2: the merged-in incidents (also ``links.linked``).
        "linked_incidents": links["linked"],
        "facts": _status_facts(status, snapshot, log, recovery, day),
        "actions": _actions(status, actions, snapshot),
        "feedback_offer": feedback_offer(status, snapshot),
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
        "composer": composer,
        "queued_note": unread_words,
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
