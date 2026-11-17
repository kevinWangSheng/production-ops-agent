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
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from opspilot.web.incident_list import stamp, status_of
from opspilot.web.store import IncidentListItem

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
    "cancel": "Cancel Run",
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

#: Spec 4.7: rounds shown around the folded middle of a long Run.
ROUNDS_HEAD = 3
ROUNDS_TAIL = 5
#: Spec 4.7: Key evidence items shown before "Show N more".
EVIDENCE_SHOWN = 8

#: Header buttons (spec 4.4): actions that need no extra input. The rest of
#: the offered actions go into the "More" menu.
HEADER = ("resume", "pause", "cancel", "new_run", "takeover", "reopen", "close")
HEADER_MAX = 4
#: Actions the browser confirms in place before submitting (D-B9).
CONFIRM = frozenset({"cancel", "close", "takeover", "merge_into", "rebind_target"})
#: Labels in the "More" menu.
MORE_LABEL = {
    "register_remediation": "Register remediation",
    "rebind_target": "Retarget",
    "merge_into": "Merge into another incident",
    "resume": "Resume",
    "takeover": "Take over",
}

#: Spec 4.6 (D-B12): the report's own conclusion level, in words.
LEVEL = {
    "supported": "Supported by evidence",
    "partial": "Partly supported",
    "inconclusive": "Inconclusive",
}

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
                # Spec 4.7: the first ROUNDS_HEAD and last ROUNDS_TAIL rounds
                # show; the middle stays in the DOM behind one button.
                "rest": ROUNDS_HEAD < number <= len(steps) - ROUNDS_TAIL
                and len(steps) > ROUNDS_HEAD + ROUNDS_TAIL,
            }
        )
    times = [
        clock(step.get("observed_at"), day)
        for step in steps
        if step.get("observed_at") and not step.get("history_only")
    ]
    hidden = sum(1 for r in rounds if r["rest"])
    return {
        "rounds": rounds,
        "hidden": hidden,
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
    integrity = list(replay.get("integrity") or [])
    verified = bool(replay.get("recovery_confirmed") and replay.get("consistent"))
    if view.get("recovery_statement"):
        headline = view["recovery_statement"]
        headline = headline[0].upper() + headline[1:]
    elif (
        view.get("state") != "authorized"
        and not replay.get("consistent")
        and (integrity or replay.get("available") is False)
    ):
        # The verdict block's own words: the stored verdict is not repeated
        # when the offline replay cannot confirm it (contract 3.1, rule 3).
        if "STORED_OBSERVATION_DATA_CLEANED" in integrity:
            headline = "Raw data cleaned, cannot be re-verified"
        else:
            headline = "Integrity mismatch"
        detail = "the stored verdict is not repeated"
    elif verified and snapshot["incident"].lifecycle in (
        "resolved",
        "closed",
    ):
        when = clock(confirmed_at, day)
        headline = "Recovery confirmed" + (f" {when}" if when else "")
    elif verified:
        # The incident moved on (reopened or renewed) after this watch ended:
        # the confirmation is history, not the current state (the same
        # lifecycle guard as ``_with_recovery_status``).
        when = clock(confirmed_at, day)
        headline = "No current Recovery watch"
        detail = (
            "an earlier watch confirmed recovery"
            + (f" at {when}" if when else "")
            + "; the incident has been reopened since"
        )
    elif view.get("state") == "authorized":
        headline = "Observing; recovery not confirmed yet"
    else:
        headline = "Watch ended without confirming recovery"
        detail = " · ".join(
            str(v) for v in (view.get("state"), view.get("ended_reason")) if v
        )
    if verified and detail is None:
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
    if alert:
        # The first delivery's database receipt time; the incident row's
        # creation follows it within the same intake.
        outcome = alert.get("outcome")
        deliveries = getattr(outcome, "deliveries", None) or ()
        received = (
            getattr(deliveries[0], "received_at", None)
            if deliveries
            else (alert.get("latest") or {}).get("received_at")
        )
        add(received or incident.created_at, "alert", "Alert received, incident opened")
    else:
        intake = next(
            (
                e.get("recorded_at")
                for e in snapshot.get("events") or []
                if e.get("kind") == "intake_accepted"
            ),
            None,
        )
        add(
            intake or incident.created_at, "human", "Question received, incident opened"
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
        elif kind == "run_handoff" and payload.get("parked") is not False:
            # ``parked: false`` is an attempt fenced by human control or a
            # crash: the Run was not parked for a human (``_select_snapshot_outcome``
            # excludes it the same way), so the timeline does not claim it.
            add(event.get("recorded_at"), "code", "Handed to a human")
        elif kind == "run_claimed" and int(payload.get("epoch") or 1) > 1:
            add(
                event.get("recorded_at"),
                "code",
                f"Run claimed again (epoch {payload.get('epoch')})",
            )
    for row in snapshot.get("decision_log") or []:
        if row.get("action") == "auto_authorize_observation":
            continue  # shown from the session row below, once per session
        add(
            row.get("decided_at"),
            "code" if row.get("system") else "human",
            DONE.get(row.get("action"), str(row.get("action"))),
        )
    for session in snapshot.get("observation_sessions") or []:
        automatic = session.get("authorization_source") == "automatic"
        add(
            session.get("authorized_at"),
            "code" if automatic else "human",
            "Recovery watch authorized" + ("" if automatic else " (after remediation)"),
        )
    for view in snapshot.get("recovery") or []:
        replay_view = view.get("replay") or {}
        verified = replay_view.get("recovery_confirmed") and replay_view.get(
            "consistent"
        )
        for sample in view.get("samples") or []:
            if sample.get("transition") == "recovery_confirmed":
                add(
                    sample.get("submitted_at") or sample.get("window_end"),
                    "code",
                    "Recovery confirmed"
                    if verified
                    else "Recovery confirmation stored, not re-verified",
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
    human_owned = incident.mode == "human_owned"
    if action in ("follow_up", "correct"):
        noun = "question" if action == "follow_up" else "correction"
        if human_owned:
            return (
                f"Your {noun} is recorded on the incident only: it is human-owned, "
                "so no Run is queued or started and the Agent does not answer it."
            )
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
    if human_owned and action == "pause":
        return (
            "Mark this human-owned incident paused and withdraw any Recovery watch "
            "authorization; the Run stays with the human."
        )
    if human_owned and action == "resume":
        return (
            "Lift the pause marker on this human-owned incident; the Run stays "
            "with the human and the Agent does not start again."
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


def hm(value: Any, day: str | None = None) -> str | None:
    """``HH:MM`` UTC; prefixed with the date when it is not ``day``."""
    moment = _dt(value)
    if moment is None:
        return None
    if day is not None and moment.strftime("%Y-%m-%d") != day:
        return moment.strftime("%m-%d %H:%M")
    return moment.strftime("%H:%M")


def _run_started(snapshot: Mapping[str, Any]) -> str | None:
    """The current Run's start: its first committed step (the same rule as
    ``investigation_history.started_at``)."""
    for step in snapshot.get("steps") or []:
        moment = _dt(step.get("observed_at"))
        if moment is not None:
            return moment.isoformat()
    return None


def run_numbers(snapshot: Mapping[str, Any]) -> dict[str, int]:
    """Spec 4.3 (D-B11): display numbers from 1 for every Run, history plus
    the current one, in ``investigation_history`` order (``started_at``
    nulls last, ``control_generation``, ``run_id``). Display only."""
    run = snapshot.get("run") or {}
    rows = [
        (h.get("started_at"), int(h.get("control_generation") or 0), str(h["run_id"]))
        for h in snapshot.get("history") or []
    ]
    if run.get("run_id"):
        rows.append(
            (
                _run_started(snapshot),
                int(run.get("control_generation") or 0),
                str(run["run_id"]),
            )
        )

    def key(row: tuple[Any, int, str]) -> tuple[Any, ...]:
        moment = _dt(row[0])
        return (
            moment is None,
            moment or datetime.min.replace(tzinfo=timezone.utc),
            row[1],
            row[2],
        )

    return {row[2]: n for n, row in enumerate(sorted(rows, key=key), start=1)}


def _header(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Spec 4.1 #1: the title (same order as the list's ``title_of``) and
    where the incident came from."""
    incident = snapshot["incident"]
    head = _alert_header(snapshot)
    question = snapshot.get("question")
    title = None
    for text in (
        head["summary"],
        head["alertname"],
        None if snapshot.get("alert") else question,
    ):
        if isinstance(text, str) and text.strip():
            title = " ".join(text.split())
            break
    if title is None:
        title = f"Incident {str(incident.incident_id)[:8]}"
    return {
        **head,
        "title": title,
        "asked_by": None if snapshot.get("alert") else snapshot.get("question_actor"),
        "opened": stamp(_dt(incident.created_at)),
    }


def _status(
    snapshot: Mapping[str, Any], postmortem: Mapping[str, Any] | None
) -> dict[str, Any]:
    """The list's words for this incident (spec 4.1 #3): ``status_of`` on an
    item assembled from the snapshot, so both pages say the same thing."""
    incident = snapshot["incident"]
    run_state = (snapshot.get("run") or {}).get("state")
    latest = (postmortem or {}).get("latest") or {}
    item = IncidentListItem(
        summary=replace(incident, run_state=run_state),
        postmortem_id=(postmortem or {}).get("postmortem_id"),
        postmortem_version=latest.get("version"),
        postmortem_state=(postmortem or {}).get("status"),
    )
    return status_of(item)


def _recovery_line(
    snapshot: Mapping[str, Any], rec: Mapping[str, Any], day: str | None
) -> str | None:
    """The status line's recovery part; ``None`` without a watch."""
    view = rec.get("view")
    if not view:
        return None
    session = rec.get("session") or {}
    samples = view.get("samples") or []
    replay = view.get("replay") or {}
    if view.get("state") == "authorized":
        healthy = sum(1 for s in samples if s.get("stored_outcome") == "healthy")
        since = hm(session.get("authorized_at"), day)
        total = session.get("max_samples") or len(samples)
        counted = (
            f"{healthy} of {total} samples healthy"
            if samples
            else f"no samples yet (up to {total})"
        )
        return f"Watching recovery · {counted}" + (f" · since {since}" if since else "")
    verified = bool(replay.get("recovery_confirmed") and replay.get("consistent"))
    lifecycle = snapshot["incident"].lifecycle
    if verified and lifecycle == "resolved":
        at = next(
            (
                s.get("submitted_at") or s.get("window_end")
                for s in samples
                if s.get("transition") == "recovery_confirmed"
            ),
            None,
        )
        when = hm(at, day)
        how = (
            "no handling recorded"
            if view.get("recovery_status") == "recovered_no_remediation"
            else "after a registered remediation"
            if view.get("authorization_source") != "automatic"
            else None
        )
        return " · ".join(
            x for x in ("Recovered" + (f" {when}" if when else ""), how) if x
        )
    if verified and lifecycle == "closed":
        return "Recovery confirmed"
    return str(rec.get("headline"))


def _header_actions(
    actions: Sequence[str], snapshot: Mapping[str, Any]
) -> dict[str, list[str]]:
    """Spec 4.4 (D-B10): up to ``HEADER_MAX`` buttons that need no input, in
    ``ORDER``; actions needing input, and any overflow, go into More.
    ``resume`` is a header button only while paused (on a Run that is not
    paused it re-queues the same Run, which is not a usual next step), and
    ``takeover`` only while the incident is not human-owned already."""
    incident = snapshot["incident"]
    paused = (
        incident.control == "paused"
        or (snapshot.get("run") or {}).get("state") == "paused"
    )
    ordered = sorted(actions, key=lambda a: ORDER.index(a) if a in ORDER else 99)
    owned = incident.mode == "human_owned"
    main = [
        a
        for a in ordered
        if a in HEADER and (a != "resume" or paused) and (a != "takeover" or not owned)
    ][:HEADER_MAX]
    more = [a for a in ordered if a not in main and a not in ("follow_up", "correct")]
    return {"main": main, "more": more}


def _shown_report(
    snapshot: Mapping[str, Any], numbers: Mapping[str, int], live: bool
) -> dict[str, Any] | None:
    """Spec 4.1 #6 and D-B15: the conclusion slot. With a Run in flight it
    holds the last concluded, parseable report from the history as the
    previous conclusion; otherwise the incident's report."""
    run = snapshot.get("run") or {}
    history = snapshot.get("history") or []
    parsed = [
        h
        for h in history
        if h.get("report") is not None and (h["report"] or {}).get("parsed")
    ]
    if live:
        if not parsed:
            return None
        entry = parsed[-1]
        earlier = parsed[:-1]
        return {
            "report": entry["report"],
            "run_no": numbers.get(str(entry["run_id"])),
            "at": entry.get("concluded_at"),
            "previous": True,
            "revises": numbers.get(str(earlier[-1]["run_id"])) if earlier else None,
        }
    report = snapshot.get("report")
    if report is None:
        return None
    completed = next(
        (
            e.get("recorded_at")
            for e in reversed(snapshot.get("events") or [])
            if e.get("kind") == "run_completed"
            and (e.get("payload") or {}).get("run_id") == run.get("run_id")
        ),
        None,
    )
    return {
        "report": report,
        "run_no": numbers.get(str(run.get("run_id"))),
        "at": completed,
        "previous": False,
        "revises": numbers.get(str(parsed[-1]["run_id"])) if parsed else None,
    }


def _sources(
    item: Mapping[str, Any], labels: Mapping[str, str], fallback: str
) -> list[str]:
    """A statement's source labels (``R3 · metrics.range_query``), each once."""
    out: list[str] = []
    for eid in item["claim"].get("evidence_ids") or []:
        label = labels.get(str(eid), fallback)
        if label not in out:
            out.append(label)
    return out


def _owner(snapshot: Mapping[str, Any]) -> str | None:
    """Who took a human-owned incident over (the latest takeover row)."""
    if snapshot["incident"].mode != "human_owned":
        return None
    row = next(
        (
            r
            for r in reversed(snapshot.get("decision_log") or [])
            if r.get("action") == "takeover"
        ),
        None,
    )
    return None if row is None else row.get("actor_id")


def _no_conclusion(snapshot: Mapping[str, Any], number: int | None) -> str:
    """Spec 4.6: the single line of an empty conclusion slot."""
    run = snapshot.get("run") or {}
    incident = snapshot["incident"]
    state = run.get("state")
    label = f"Run {number}" if number else "The Run"
    if incident.mode == "human_owned":
        takeover = next(
            (
                row
                for row in reversed(snapshot.get("decision_log") or [])
                if row.get("action") == "takeover"
            ),
            None,
        )
        actor = takeover.get("actor_id") if takeover else None
        return f"{label} stopped when {actor or 'a human'} took over."
    if state == "cancelled" or incident.control == "cancelled":
        return f"{label} was cancelled."
    if state == "waiting_human":
        return f"{label} handed this incident to a human."
    return "No conclusion yet."


def _history_rows(
    snapshot: Mapping[str, Any], numbers: Mapping[str, int], day: str | None
) -> list[dict[str, Any]]:
    """Spec 4.1 #10: one folded row per earlier Run, newest first."""
    history = list(snapshot.get("history") or [])

    def parsed(report: Mapping[str, Any] | None) -> bool:
        return report is not None and bool(report.get("parsed"))

    reported = [
        numbers.get(str(h["run_id"])) or 0 for h in history if parsed(h.get("report"))
    ]
    if parsed(snapshot.get("report")):
        reported.append(
            numbers.get(str((snapshot.get("run") or {}).get("run_id"))) or 0
        )
    latest = max(reported) if reported else None
    rows = []
    for entry in history:
        n = numbers.get(str(entry["run_id"]))
        report = entry.get("report")
        state = entry.get("state")
        if entry.get("concluded_at"):
            what = "concluded " + (hm(entry["concluded_at"], day) or "")
        elif state == "cancelled":
            what = "cancelled"
        elif state == "waiting_human":
            what = "handed off"
        else:
            what = "no conclusion"
        superseded = latest if parsed(report) and latest and n and n < latest else None
        rows.append(
            {
                "entry": entry,
                "no": n,
                "short": str(entry["run_id"])[:8],
                "what": what.strip(),
                "superseded": superseded,
            }
        )
    rows.sort(key=lambda r: r["no"] or 0, reverse=True)
    return rows


def _notes(snapshot: Mapping[str, Any], day: str | None) -> list[dict[str, Any]]:
    """Spec 4.1 #9: text recorded after a takeover (no Run reads it)."""
    incident = snapshot["incident"]
    if incident.mode != "human_owned":
        return []
    controls = snapshot.get("controls") or []
    takeover = max(
        (int(c["generation"]) for c in controls if c.get("action") == "takeover"),
        default=None,
    )
    return [
        {
            "actor": c.get("actor_id"),
            "time": hm(c.get("decided_at"), day),
            "text": c.get("text"),
            "correct": c.get("action") == "correct",
        }
        for c in controls
        if c.get("action") in ("follow_up", "correct")
        and c.get("text")
        and (takeover is None or int(c["generation"]) > takeover)
    ]


def _blind(snapshot: Mapping[str, Any]) -> list[dict[str, str]]:
    """Spec 4.6 "Could not see": one line per source with a gap (counted by
    code from the committed tool results)."""
    coverage = snapshot.get("coverage") or {}
    words = {
        "not_integrated": "not integrated",
        "failed": "no query succeeded",
        "partial_failure": "some queries failed",
        "not_queried": "not queried",
        "not_authorized": "not authorized",
    }
    return [
        {"name": row["name"], "words": words[row["state"]]}
        for row in coverage.get("sources") or []
        if row.get("state") in words
    ]


def page_view(
    snapshot: Mapping[str, Any],
    postmortem: Mapping[str, Any] | None,
    actions: Sequence[str],
) -> dict[str, Any]:
    """Everything the incident page derives, from data already on it."""
    day = _day(snapshot)
    run = snapshot.get("run") or {}
    live = snapshot.get("report") is None and run.get("state") in (
        "queued",
        "running",
        "paused",
    )
    numbers = run_numbers(snapshot)
    current_no = numbers.get(str(run.get("run_id")))
    shown = _shown_report(snapshot, numbers, live)
    claim_source = (
        shown["report"]
        if shown
        else snapshot.get("handoff_report")
        if (snapshot.get("handoff_report") or {}).get("parsed")
        else None
    )
    claims = _claims(claim_source)
    how = _rounds(snapshot.get("steps") or [], claims["cited"], day)
    recovery = _recovery(snapshot, day)
    status = dict(_status(snapshot, postmortem))
    if incident_owner := _owner(snapshot):
        status["words"] = f"{status['words']} by {incident_owner}"
    recovery_line = _recovery_line(snapshot, recovery, day)
    if recovery_line and recovery_line.startswith(status["words"] + " · "):
        # "Watching recovery" is already the status words.
        recovery_line = recovery_line[len(status["words"]) + 3 :]
    header_actions = _header_actions(actions, snapshot)
    incident = snapshot["incident"]
    primary = (
        "resume"
        if incident.control == "paused" and "resume" in actions
        else "reopen"
        if incident.lifecycle == "closed" and "reopen" in actions
        else "close"
        if incident.lifecycle == "resolved" and "close" in actions
        else None
    )
    steps = [s for s in snapshot.get("steps") or [] if not s.get("history_only")]
    report = shown["report"] if shown else None
    offered = [
        {
            "action": a,
            "button": BUTTON.get(a, a),
            "hint": _copy(a, snapshot),
            "primary": a == primary,
        }
        for a in sorted(actions, key=lambda a: ORDER.index(a) if a in ORDER else 99)
    ]
    groups = {g["key"]: g["items"] for g in claims["groups"]}
    fallback = (
        f"Run {shown['run_no']} evidence" if shown and shown["previous"] else "evidence"
    )
    return {
        "day": day,
        "live": live,
        "header": _header(snapshot),
        "status": status,
        "recovery_line": recovery_line,
        "run_no": current_no,
        "numbers": numbers,
        "started": _run_started(snapshot),
        "round": len(steps),
        "shown": shown,
        "level": None
        if report is None or not report.get("parsed")
        else LEVEL.get(str(report.get("conclusion")), str(report.get("conclusion"))),
        "at": None if shown is None else hm(shown.get("at"), day),
        "lede": None if report is None else opening(report.get("summary"), 480),
        "handoff_lede": opening(
            (snapshot.get("handoff_report") or {}).get("summary"), 480
        ),
        "no_conclusion": _no_conclusion(snapshot, current_no),
        "key_evidence": [
            dict(item, counter=counter, sources=_sources(item, how["labels"], fallback))
            for counter, key in ((False, "facts"), (True, "counter_evidence"))
            for item in groups.get(key, [])
        ],
        "evidence_shown": EVIDENCE_SHOWN,
        "level_words": LEVEL,
        "groups": groups,
        "blind": _blind(snapshot),
        "history": _history_rows(snapshot, numbers, day),
        "notes": _notes(snapshot, day),
        "recovery": recovery,
        "timeline": _timeline(snapshot, postmortem, day),
        "claims": claims,
        "how": how,
        "header_actions": header_actions,
        "confirm": CONFIRM,
        "more_label": MORE_LABEL,
        "actions": offered,
        "by_action": {item["action"]: item for item in offered},
        "clock": lambda value: clock(value, day),
        "hm": lambda value: hm(value, day),
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
