"""The one incident status the page header and the list row both show.

Workbench redesign contract (``docs/tasks/2026-11-17-workbench-redesign-contract.md``)
A8: nine ordered rules, the first that holds wins. A9: while a Run is
queued or running the incident reads Investigating even when an earlier
Run published a report (the page keeps showing that report).

The list's tab grouping in SQL (``store._PAGE_GROUP``) repeats ``group`` of
this order; change both together.
"""

from __future__ import annotations

from dataclasses import dataclass

#: List tab keys a status falls under (``store.LIST_TABS``).
NEEDS_HUMAN = "needs_human"
IN_PROGRESS = "in_progress"
CLOSED = "closed"

ACTIVE_RUN = frozenset({"queued", "running"})


@dataclass(frozen=True)
class IncidentStatus:
    #: One of ``merged closed resolved canceled paused human_owned
    #: investigating watching root_cause needs_attention``.
    key: str
    #: The words shown next to the icon (never colour alone).
    label: str
    #: A text glyph drawn before the label (``aria-hidden``).
    icon: str
    #: The tone the CSS colours the icon and label with.
    tone: str
    #: The list tab group.
    group: str


_STATUSES = {
    "merged": IncidentStatus("merged", "Merged", "→", "muted", CLOSED),
    "closed": IncidentStatus("closed", "Closed", "■", "muted", CLOSED),
    "resolved": IncidentStatus("resolved", "Resolved", "✓", "ok", NEEDS_HUMAN),
    "canceled": IncidentStatus(
        "canceled", "Needs attention · canceled", "!", "err", NEEDS_HUMAN
    ),
    "paused": IncidentStatus("paused", "Paused", "Ⅱ", "warn", NEEDS_HUMAN),
    "human_owned": IncidentStatus(
        "human_owned", "Human-owned", "◆", "human", NEEDS_HUMAN
    ),
    "investigating": IncidentStatus(
        "investigating", "Investigating", "●", "run", IN_PROGRESS
    ),
    "watching": IncidentStatus(
        "watching", "Watching recovery", "◔", "run", IN_PROGRESS
    ),
    "root_cause": IncidentStatus(
        "root_cause", "Root cause found", "✓", "ok", NEEDS_HUMAN
    ),
    "needs_attention": IncidentStatus(
        "needs_attention", "Needs attention", "!", "err", NEEDS_HUMAN
    ),
}


def incident_status(
    *,
    merged: bool,
    lifecycle: str,
    state: str,
    run_state: str | None,
    mode: str,
    concluded: bool,
) -> IncidentStatus:
    """A8, first match wins.

    ``state`` is the incident row's state (``paused``/``cancelled`` are the
    human-control markers); ``run_state`` the current Run's. A Run paused by
    a scope suspension reads Paused like an incident pause.
    """
    if merged:
        return _STATUSES["merged"]
    if lifecycle == "closed":
        return _STATUSES["closed"]
    if lifecycle == "resolved":
        return _STATUSES["resolved"]
    if state == "cancelled":
        return _STATUSES["canceled"]
    if state == "paused" or run_state == "paused":
        return _STATUSES["paused"]
    if mode == "human_owned":
        return _STATUSES["human_owned"]
    if run_state in ACTIVE_RUN:
        return _STATUSES["investigating"]
    if concluded:
        if lifecycle == "observing_recovery":
            return _STATUSES["watching"]
        return _STATUSES["root_cause"]
    return _STATUSES["needs_attention"]


def status_named(key: str) -> IncidentStatus:
    return _STATUSES[key]
