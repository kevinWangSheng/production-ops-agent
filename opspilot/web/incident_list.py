"""The incident list page's view (UI redesign contract r2, 3.2 and D11).

Pure functions over the rows one list query returned
(``DurableIncidentStore.list_incident_items``): no store access, so the page
costs one query whatever the row count (list contract 3). The human status
is added in front of the four stored words, never instead of them (U5); the
mapping table is in the task record ``docs/tasks/2026-11-15-web-ui-redesign.md``.

Nothing here depends on the request time: two renders of the same rows are
identical text (``tests/acceptance/test_m1_04_alert_intake.py``, D13).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from opspilot.web.store import IncidentListItem

NEEDS_HUMAN = "needs_human"
IN_PROGRESS = "in_progress"
CLOSED = "closed"

GROUP_TITLES = {
    NEEDS_HUMAN: "Needs a human",
    IN_PROGRESS: "In progress, no action needed",
    CLOSED: "Closed",
}

#: The Agent's own rating (``investigation/reports.py``) with the fixed
#: half sentence the list mock r2 uses; code checks citations, not the level.
LEVELS = {
    "supported": ("Supported", "strongest of three levels"),
    "partial": ("Partial", "findings, cause unresolved"),
    "inconclusive": ("Inconclusive", "weakest level"),
}

_ACTIVE_RUN = {"queued", "running"}
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"'`])")
_SUMMARY_CHARS = 320


def status_of(item: IncidentListItem) -> dict[str, Any]:
    """Group, the one to-do label (or ``None``) and the status words of a row.

    First match wins (D11 mapping table in the task record). Closed comes
    first so a cancelled-then-closed incident reads as closed; ownership is
    checked before ``waiting_human`` because a take-over parks the Run there.
    """
    s = item.summary
    run = s.run_state
    if s.lifecycle == "closed":
        if item.postmortem_state == "under_review" and item.postmortem_id:
            return _status(
                NEEDS_HUMAN,
                "Review postmortem",
                "Closed",
                f"postmortem version {item.postmortem_version} waits for review",
            )
        detail = None
        if s.merged_into_incident_id is not None:
            detail = f"merged into {str(s.merged_into_incident_id)[:8]}"
        return _status(CLOSED, None, "Closed", detail)
    if s.handoff_only:
        return _status(
            NEEDS_HUMAN,
            "Handle outside OpsPilot",
            "Handed to a human, no Run",
            "no single registered target matched",
        )
    if s.control == "paused" or run == "paused":
        return _status(
            NEEDS_HUMAN, "Resume", "Paused", "no investigation until resumed"
        )
    if run == "blocked":
        return _status(
            NEEDS_HUMAN, "Cancel or take over", "Blocked", "only cancel moves it on"
        )
    if s.mode == "human_owned":
        return _status(
            NEEDS_HUMAN, "Register or close", "Taken over", "a human owns this incident"
        )
    if run == "waiting_human":
        return _status(
            NEEDS_HUMAN,
            "Ask or take over",
            "Waiting for a human",
            "the Agent handed the Run back",
        )
    if s.lifecycle == "resolved":
        return _status(
            NEEDS_HUMAN,
            "Close or reopen",
            "Recovered",
            "recovery confirmed, not closed",
        )
    if s.lifecycle == "observing_recovery":
        return _status(
            IN_PROGRESS,
            None,
            "Watching recovery",
            "the Observer samples health",
        )
    if run in _ACTIVE_RUN:
        return _status(
            IN_PROGRESS,
            None,
            "Investigating",
            "Agent Run waiting for a worker"
            if run == "queued"
            else "Agent Run in progress",
        )
    if s.control == "cancelled":
        return _status(
            NEEDS_HUMAN, "New Run or close", "Open, Run cancelled", "no Run in progress"
        )
    if s.concluded:
        return _status(
            NEEDS_HUMAN,
            "Close or register",
            "Report published",
            "no recovery watch running",
        )
    return _status(NEEDS_HUMAN, "Take over or close", "Open", "no Run in progress")


def _status(
    group: str, todo: str | None, words: str, detail: str | None
) -> dict[str, Any]:
    return {"group": group, "todo": todo, "words": words, "detail": detail}


def subject_of(item: IncidentListItem) -> dict[str, Any]:
    """What the row is about: the service and alert name for an alert
    incident, the question and target for a submitted one."""
    s = item.summary
    service = None
    if item.alert_namespace and item.alert_workload:
        service = f"{item.alert_namespace}/{item.alert_workload}"
    if item.alert_name or service:
        return {
            "kind": "alert",
            "title": service or item.alert_name,
            "alert_name": item.alert_name if service else None,
            "target": None,
        }
    if item.question:
        return {
            "kind": "question",
            "title": _clip(" ".join(item.question.split()), 140),
            "alert_name": None,
            "target": s.target_uid,
        }
    return {
        "kind": "none",
        "title": f"Incident {str(s.incident_id)[:8]}",
        "alert_name": None,
        "target": s.target_uid,
    }


def conclusion_of(
    item: IncidentListItem,
    report_of: Callable[[Mapping[str, Any] | None], Mapping[str, Any] | None],
) -> dict[str, Any]:
    """Level, its half sentence and the summary's first sentence; a stored
    conclusion that does not parse says so instead of guessing a level."""
    if not item.summary.concluded:
        running = item.summary.run_state in _ACTIVE_RUN
        return {
            "level": None,
            "words": "No conclusion yet" if running else "No conclusion",
            "why": None,
            "summary": None,
        }
    report = report_of(item.conclusion) if item.conclusion is not None else None
    level = (
        LEVELS.get(str(report.get("conclusion")))
        if report and report.get("parsed")
        else None
    )
    if level is None or report is None:
        return {
            "level": None,
            "words": "Concluded",
            "why": "level not in the record",
            "summary": None,
        }
    summary = report.get("summary")
    return {
        "level": report.get("conclusion"),
        "words": level[0],
        "why": level[1],
        "summary": first_sentence(summary) if isinstance(summary, str) else None,
    }


def recovery_of(item: IncidentListItem) -> dict[str, Any]:
    """The latest recovery session in fixed words."""
    state = item.recovery_state
    if state is None:
        return {"words": "Not started", "detail": None, "active": False}
    if state == "authorized":
        count = item.recovery_samples or 0
        return {
            "words": "Watching",
            "detail": f"{count} sample{'' if count == 1 else 's'}",
            "active": True,
        }
    ended = clock(item.recovery_ended_at)
    if item.recovery_ended_reason == "recovery_confirmed":
        return {"words": "Confirmed", "detail": ended, "active": True}
    return {
        "words": "Ended, not confirmed",
        "detail": item.recovery_ended_reason,
        "active": False,
    }


def first_sentence(text: str) -> str:
    flat = " ".join(text.split())
    head = _SENTENCE_END.split(flat, maxsplit=1)[0]
    return _clip(head, _SUMMARY_CHARS)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",;:")
    return cut + "…"


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def clock(value: datetime | None) -> str | None:
    return None if value is None else _utc(value).strftime("%H:%M:%S")


def list_page(
    items: Iterable[IncidentListItem],
    *,
    report_of: Callable[[Mapping[str, Any] | None], Mapping[str, Any] | None],
    targets: tuple[str, ...] = (),
) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {key: [] for key in GROUP_TITLES}
    total = 0
    for item in items:
        total += 1
        s = item.summary
        status = status_of(item)
        created = None if s.created_at is None else _utc(s.created_at)
        groups[status["group"]].append(
            {
                "item": s,
                "id": str(s.incident_id),
                "short_id": str(s.incident_id)[:8],
                "status": status,
                "subject": subject_of(item),
                "conclusion": conclusion_of(item, report_of),
                "recovery": recovery_of(item),
                "related_short": None
                if s.related_incident_id is None
                else str(s.related_incident_id)[:8],
                "postmortem_href": None
                if status["todo"] != "Review postmortem"
                else f"/postmortems/{item.postmortem_id}/versions/{item.postmortem_version}",
                "opened_date": None
                if created is None
                else created.strftime("%Y-%m-%d"),
                "opened_time": None
                if created is None
                else created.strftime("%H:%M:%S"),
            }
        )
    return {
        "total": total,
        "groups": [
            {"key": key, "title": GROUP_TITLES[key], "rows": rows}
            for key, rows in groups.items()
        ],
        "counts": {key: len(rows) for key, rows in groups.items()},
        "targets": tuple(targets),
    }
