"""Display-only derivations for the incident page (#572, UI redesign PR 2).

Everything here is read from the snapshot the page already renders
(``Workbench.snapshot``) and the postmortem projection; nothing queries a
store, a model or telemetry, and nothing here decides a state. The words it
returns land in the page header, which the incident-control tests read up to
the first ``<h2>``: they must not contain ``state``, ``queued`` or
``running`` (contract r2, section 6), so Run states are never echoed here.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

__all__ = ["page_view", "handoff_view"]

#: Claim groups of an ``m0-report-v2`` report and the id letter each claim
#: gets on the page (citations, the drawer and the round list link to them).
CLAIM_GROUPS = (
    ("facts", "F", "Facts"),
    ("hypotheses", "H", "Hypotheses"),
    ("counter_evidence", "C", "Counter-evidence"),
    ("rejected_hypotheses", "X", "Rejected hypotheses"),
    ("recommendations", "A", "Recommendations"),
)

#: What a committed decision reads as in the summary band and timeline.
DONE = {
    "close": "Closed",
    "reopen": "Reopened",
    "takeover": "Taken over",
    "pause": "Paused",
    "resume": "Resumed",
    "cancel": "Cancelled",
    "new_run": "New Run started",
    "follow_up": "Follow-up asked",
    "correct": "Correction added",
    "register_remediation": "Remediation registered",
    "merge_into": "Merged into another incident",
    "rebind_target": "Retargeted",
    "auto_authorize_observation": "Recovery watch authorized",
}

#: Button text per control (D7: the hidden ``action`` value is unchanged).
BUTTON = {
    "close": "Close",
    "reopen": "Reopen",
    "takeover": "Take over",
    "pause": "Pause",
    "resume": "Resume",
    "cancel": "Cancel",
    "new_run": "Start a new Run",
    "follow_up": "Ask",
    "correct": "Add a correction",
    "register_remediation": "Register",
    "merge_into": "Merge",
    "rebind_target": "Retarget",
}

#: Control order in the rail: the usual next step first.
ORDER = (
    "resume",
    "follow_up",
    "correct",
    "pause",
    "cancel",
    "new_run",
    "takeover",
    "register_remediation",
    "reopen",
    "close",
    "rebind_target",
    "merge_into",
)

_SOURCE_WORDS = {
    "partial_failure": "partial",
    "failed": "failed",
    "not_queried": "not queried",
    "not_authorized": "not authorized",
}


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


def opening(text: str | None, limit: int = 220) -> str | None:
    """The report summary's first sentence, cut at a word boundary."""
    if not text:
        return None
    text = " ".join(text.split())
    match = re.search(r"(?<=[.!?])\s", text)
    sentence = text[: match.start()] if match else text
    if len(sentence) <= limit:
        return sentence if sentence == text else sentence + " …"
    cut = sentence[:limit].rsplit(" ", 1)[0]
    return cut + " …"


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _claims(report: Mapping[str, Any] | None) -> dict[str, Any]:
    """Claim ids per group and, per evidence id, the claims citing it."""
    groups: list[dict[str, Any]] = []
    cited: dict[str, list[dict[str, str]]] = {}
    if not report or not report.get("parsed"):
        return {"groups": groups, "cited": cited}
    for key, letter, title in CLAIM_GROUPS:
        items = []
        for index, claim in enumerate(report.get(key) or [], start=1):
            cid = f"{letter}{index}"
            items.append({"id": cid, "claim": claim})
            for eid in claim.get("evidence_ids") or []:
                cited.setdefault(str(eid), []).append(
                    {"id": cid, "text": opening(claim.get("text"), 90) or ""}
                )
        groups.append({"key": key, "letter": letter, "title": title, "items": items})
    return {"groups": groups, "cited": cited}


def _rounds(
    steps: Sequence[Mapping[str, Any]], cited: Mapping[str, Any], day: str | None
) -> dict[str, Any]:
    rounds, labels = [], {}
    total = cited_count = problems = unused = 0
    for number, step in enumerate(steps, start=1):
        calls = []
        for tool in step.get("tools") or []:
            eid = tool.get("evidence_id")
            claims = cited.get(str(eid), []) if eid else []
            problem = tool.get("status") != "ok"
            history = bool(step.get("history_only"))
            if not history:
                total += 1
                cited_count += bool(claims)
                problems += problem
                unused += not claims
            if eid:
                labels[str(eid)] = f"R{number} · {tool.get('tool') or 'tool'}"
            calls.append(
                {
                    "tool": tool,
                    "claims": claims,
                    "problem": problem,
                    "unused": not claims,
                }
            )
        rounds.append(
            {
                "label": f"R{number}",
                "anchor": f"round-{number}",
                "time": clock(step.get("observed_at"), day),
                "step": step,
                "calls": calls,
            }
        )
    times = [
        clock(step.get("observed_at"), day)
        for step in steps
        if step.get("observed_at") and not step.get("history_only")
    ]
    return {
        "rounds": rounds,
        "labels": labels,
        "total": total,
        "cited": cited_count,
        "problems": problems,
        "unused": unused,
        "first": times[0] if times else None,
        "last": times[-1] if times else None,
    }


def _recovery(snapshot: Mapping[str, Any], day: str | None) -> dict[str, Any]:
    """The rail's Recovery watch headline (the authoritative place) and the
    band repeats ``headline`` word for word (design rule 3)."""
    views = snapshot.get("recovery") or []
    if not views:
        return {"headline": "No Recovery watch authorized", "session": None}
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
    if view.get("recovery_statement"):
        headline = view["recovery_statement"]
        headline = headline[0].upper() + headline[1:]
    elif replay.get("recovery_confirmed"):
        when = clock(confirmed_at, day)
        headline = "Recovery confirmed" + (f" {when}" if when else "")
    elif view.get("state") == "authorized":
        headline = "Observing; recovery not confirmed yet"
    else:
        headline = "Watch ended without confirming recovery"
        detail = " · ".join(
            str(v) for v in (view.get("state"), view.get("ended_reason")) if v
        )
    if replay.get("recovery_confirmed"):
        detail = f"healthy window {replay.get('healthy_window_seconds')} s"
    max_samples = int(session.get("max_samples") or 0) or len(samples) or 1
    ongoing = view.get("state") == "authorized"
    width = 100 / (max_samples if ongoing else max(len(samples), 1))
    strip = []
    for sample in samples:
        outcome = sample.get("stored_outcome")
        kind = (
            "ok"
            if outcome == "healthy"
            else "nd"
            if outcome in (None, "no_data", "unknown")
            else "deg"
        )
        strip.append({"kind": kind, "width": round(width, 3), "outcome": outcome})
    if replay.get("consistent"):
        replay_text = "offline recomputation consistent"
    elif replay.get("available"):
        replay_text = "integrity mismatch"
    else:
        replay_text = None
    return {
        "headline": headline,
        "detail": detail,
        "view": view,
        "session": session,
        "strip": strip,
        "ongoing": ongoing,
        "first": clock(samples[0].get("window_end"), day) if samples else None,
        "last": clock(samples[-1].get("window_end"), day) if samples else None,
        "authorized": clock(session.get("authorized_at"), day),
        "replay": replay_text,
    }


def _human(snapshot: Mapping[str, Any], day: str | None) -> dict[str, Any]:
    incident = snapshot["incident"]
    log = snapshot.get("decision_log") or snapshot.get("controls") or []
    humans = [row for row in log if not row.get("system")]
    if humans:
        last = humans[-1]
        when = clock(last.get("decided_at"), day)
        text = f"{DONE.get(last.get('action'), last.get('action'))} by {last.get('actor_id')}"
        return {"text": text + (f" {when}" if when else ""), "kind": "human"}
    if snapshot.get("run", {}).get("state") == "waiting_human":
        return {"text": "Waiting for a human · no action yet", "kind": "human"}
    mode = "human-owned" if incident.mode == "human_owned" else "automatic"
    return {"text": f"No human action · {mode}", "kind": "code"}


def _conclusion(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Band cell 1 and the conclusion slot's heading."""
    report = snapshot.get("report")
    run_state = (snapshot.get("run") or {}).get("state")
    if report is not None and not report.get("parsed"):
        return {
            "heading": "Investigation conclusion",
            "answer": "Published, but the report could not be parsed",
            "who": "Agent (model)",
        }
    if report is not None:
        level = str(report.get("conclusion") or "")
        return {
            "heading": "Investigation conclusion",
            "answer": opening(report.get("summary")),
            "lede": opening(report.get("summary"), 480),
            "quoted": True,
            "who": "Agent (model) · " + (level[:1].upper() + level[1:]),
            "note": "Incomplete investigation, not an uncertain finding."
            if report.get("incomplete")
            else None,
        }
    handoff = snapshot.get("handoff_report")
    if run_state in ("queued", "running"):
        answer, who, heading = (
            "None yet: the Run is still investigating",
            "Agent (model) · in progress",
            "Investigation in progress",
        )
    elif run_state == "paused":
        answer, who, heading = (
            "None yet: the Run is paused",
            "Agent (model) · Run paused",
            "Investigation paused",
        )
    elif run_state == "waiting_human":
        answer, who, heading = (
            "None: the Agent handed this incident to a human",
            "Agent (model) · handed to a human",
            "Investigation conclusion",
        )
    elif run_state == "cancelled":
        answer, who, heading = (
            "None: the Run was cancelled",
            "Agent (model) · no conclusion",
            "Investigation conclusion",
        )
    else:
        answer, who, heading = (
            "No published conclusion yet",
            "Agent (model)",
            "Investigation conclusion",
        )
    note = None
    if handoff and handoff.get("parsed"):
        note = "An unpublished handoff report is shown below; it is not the conclusion."
    return {"heading": heading, "answer": answer, "who": who, "note": note}


def _blind_spots(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    coverage = snapshot.get("coverage")
    if not coverage or not coverage.get("sources"):
        return {"answer": None, "note": "No tool results committed yet."}
    rows = [r for r in coverage["sources"] if r.get("state") != "internal"]
    missing = [r for r in rows if r.get("state") == "not_integrated"]
    parts = []
    if missing:
        parts.append(f"{len(missing)} of {len(rows)} sources not integrated")
    words = [
        f"{r['name']} {_SOURCE_WORDS[r['state']]}"
        for r in rows
        if r.get("state") in _SOURCE_WORDS
    ]
    if words:
        parts.append(", ".join(words))
    if not parts:
        no_data = [r["name"] for r in rows if r.get("state") == "no_data_only"]
        parts.append(
            "returned no data: " + ", ".join(no_data)
            if no_data
            else "every queried source answered"
        )
    return {"answer": "; ".join(parts), "note": None}


def _grounds(snapshot: Mapping[str, Any], claims: Mapping[str, Any]) -> str:
    report = snapshot.get("report")
    if report is None or not report.get("parsed"):
        if (snapshot.get("run") or {}).get("state") in ("queued", "running", "paused"):
            return "No statements yet"
        return "No report, so no statements"
    sizes = {g["key"]: len(g["items"]) for g in claims["groups"]}
    gaps = len(report.get("unknown") or [])
    hyp = sizes.get("hypotheses", 0)
    return " · ".join(
        [
            _plural(sizes.get("facts", 0), "fact", "facts"),
            _plural(hyp, "hypothesis", "hypotheses") + (" (not proven)" if hyp else ""),
            _plural(gaps, "gap", "gaps"),
        ]
    )


def _timeline(
    snapshot: Mapping[str, Any], postmortem: Mapping[str, Any] | None, day: str | None
) -> dict[str, Any]:
    """Event moments from the records the page already holds (r2-M5)."""
    items: list[tuple[datetime, int, str, str, str | None]] = []

    def add(moment: Any, kind: str, text: str, end: str | None = None) -> None:
        at = _dt(moment)
        if at is not None:
            items.append((at, len(items), kind, text, end))

    incident = snapshot["incident"]
    alert = snapshot.get("alert")
    add(
        incident.created_at,
        "alert" if alert else "human",
        "Alert received, incident opened"
        if alert
        else "Question received, incident opened",
    )
    steps = [s for s in snapshot.get("steps") or [] if not s.get("history_only")]
    stamped = [s for s in steps if _dt(s.get("observed_at"))]
    if stamped:
        add(
            stamped[0]["observed_at"],
            "agent",
            "Investigated (" + _plural(len(steps), "round", "rounds") + ")",
            clock(stamped[-1]["observed_at"], day) if len(stamped) > 1 else None,
        )
    for event in snapshot.get("events") or []:
        kind = event.get("kind")
        payload = event.get("payload") or {}
        if kind == "run_completed" and payload.get("published"):
            add(event.get("recorded_at"), "agent", "Report published")
        elif kind == "run_handoff":
            add(event.get("recorded_at"), "code", "Handed to a human")
        elif kind == "run_claimed" and int(payload.get("epoch") or 1) > 1:
            add(
                event.get("recorded_at"),
                "code",
                f"Run claimed again (epoch {payload.get('epoch')})",
            )
    for row in snapshot.get("decision_log") or snapshot.get("controls") or []:
        add(
            row.get("decided_at"),
            "code" if row.get("system") else "human",
            DONE.get(row.get("action"), str(row.get("action"))),
        )
    for view in snapshot.get("recovery") or []:
        for sample in view.get("samples") or []:
            if sample.get("transition") == "recovery_confirmed":
                add(
                    sample.get("submitted_at") or sample.get("window_end"),
                    "code",
                    "Recovery confirmed",
                )
    for row in (alert or {}).get("resolved") or []:
        add(row.get("received_at"), "alert", "Alert resolved (recorded only)")
    for version in (postmortem or {}).get("versions") or []:
        add(
            version.get("created_at"),
            "agent",
            f"Postmortem v{version.get('version')} drafted",
        )
    items.sort(key=lambda item: (item[0], item[1]))
    rows = []
    for at, _, kind, text, end in items:
        start = clock(at, day)
        rows.append(
            {"kind": kind, "time": f"{start} – {end}" if end else start, "text": text}
        )
    events = snapshot.get("events") or []
    truncated = bool(events) and int(events[0].get("sequence") or 0) > 1
    return {"items": rows, "truncated": truncated}


def _copy(action: str, snapshot: Mapping[str, Any]) -> str:
    """What each offered control does, in the words of the server rules
    (U1 4; U4: current behaviour shown as is)."""
    incident = snapshot["incident"]
    paused = incident.control == "paused"
    concluded = bool(incident.concluded)
    profile = snapshot.get("health_profile_revision")
    if action in ("follow_up", "correct"):
        noun = "question" if action == "follow_up" else "correction"
        if paused:
            return (
                f"Your {noun} is recorded as input; the incident stays paused and "
                "the Agent reads it once the Run is resumed."
            )
        if concluded:
            return (
                f"The Run has concluded, so your {noun} starts a new Run with it as "
                "input: the published conclusion is cleared and any Recovery "
                "watch is withdrawn."
            )
        return (
            f"Your {noun} is recorded as input and the Run is queued again; the "
            "Agent reads it at its next round. Once the Run's deadline has "
            "passed, a new Run is started with your text instead."
        )
    return {
        "pause": "Stop the Agent now: the Run is paused and makes no progress, and "
        "any Recovery watch authorization is withdrawn. Resume continues the "
        "same Run.",
        "resume": "Put the Run back in the queue: the worker holding it loses it, "
        "and the same Run is claimed again and continues from its committed "
        "steps. It does not bring back a Recovery watch a pause withdrew. "
        "Refused once the Run's deadline has passed.",
        "cancel": "Stop the Run for good without a conclusion and withdraw any "
        "Recovery watch authorization. A New Run action then appears here.",
        "new_run": "Start a fresh Run for this incident; the cancelled Run stays "
        "in the history below.",
        "takeover": "Make the incident human-owned: an unfinished Run is handed to "
        "a human, any Recovery watch authorization is withdrawn, and the Agent "
        "does not start again for this incident. Register remediation can "
        "still start a Recovery watch.",
        "close": "Close the incident: unfinished Runs are cancelled without a "
        "conclusion and any Recovery watch authorization is withdrawn.",
        "reopen": "Set the incident back to open: nothing restarts by itself. A "
        "follow-up after that starts a new Run. Any postmortem version still in "
        "draft or under review is marked stale.",
        "register_remediation": "Record that a human handled it outside OpsPilot: "
        "an independent Recovery watch starts"
        + (f" under HealthProfile {profile}" if profile else "")
        + ", bound to this decision. It does not touch the investigated system.",
        "merge_into": "Fold this incident into a surviving incident of the same "
        "target: this one is closed and points to the survivor, which receives "
        "one note and keeps its own Run and Recovery watch. This cannot be "
        "undone.",
        "rebind_target": "Bind this incident to another registered target: any "
        "Recovery watch authorization is withdrawn and the incident returns to "
        "open.",
    }.get(action, "")


def _sources(snapshot: Mapping[str, Any]) -> list[str]:
    coverage = snapshot.get("coverage") or {}
    return [
        r["name"]
        for r in coverage.get("sources") or []
        if r.get("state") not in ("not_integrated", "internal")
    ]


def _day(snapshot: Mapping[str, Any]) -> str | None:
    created = _dt(snapshot["incident"].created_at)
    return None if created is None else created.strftime("%Y-%m-%d")


def _alert_header(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    alert = snapshot.get("alert") or {}
    latest = alert.get("latest") or {}
    annotations = latest.get("annotations") or {}
    summary = annotations.get("summary")
    outcome = alert.get("outcome")
    service = getattr(outcome, "affected_service", None) if outcome else None
    return {
        "summary": summary if isinstance(summary, str) and summary.strip() else None,
        "alertname": latest.get("alertname"),
        "service": "/".join(service) if service else None,
    }


def page_view(
    snapshot: Mapping[str, Any],
    postmortem: Mapping[str, Any] | None,
    actions: Sequence[str],
) -> dict[str, Any]:
    """Everything the incident page derives, from data already on it."""
    day = _day(snapshot)
    claims = _claims(snapshot.get("report") or snapshot.get("handoff_report"))
    how = _rounds(snapshot.get("steps") or [], claims["cited"], day)
    recovery = _recovery(snapshot, day)
    ordered = sorted(actions, key=lambda a: ORDER.index(a) if a in ORDER else 99)
    incident = snapshot["incident"]
    primary = (
        "resume"
        if incident.control == "paused" and "resume" in actions
        else "reopen"
        if incident.lifecycle in ("closed", "resolved") and "reopen" in actions
        else None
    )
    return {
        "day": day,
        "header": _alert_header(snapshot),
        "conclusion": _conclusion(snapshot),
        "grounds": _grounds(snapshot, claims),
        "blind": _blind_spots(snapshot),
        "recovery": recovery,
        "human": _human(snapshot, day),
        "timeline": _timeline(snapshot, postmortem, day),
        "claims": claims,
        "how": how,
        "sources": _sources(snapshot),
        "actions": [
            {
                "action": a,
                "button": BUTTON.get(a, a),
                "hint": _copy(a, snapshot),
                "primary": a == primary,
            }
            for a in ordered
        ],
        "clock": lambda value: clock(value, day),
    }


def handoff_view(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """The header parts of the handoff-only page (no Run)."""
    day = _day(snapshot)
    return {
        "day": day,
        "header": _alert_header(snapshot),
        "timeline": _timeline(snapshot, None, day),
        "clock": lambda value: clock(value, day),
    }
