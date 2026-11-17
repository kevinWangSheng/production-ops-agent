"""The incident list page's view (web UX spec 2026-11-16, section 3).

Pure functions over one page the store read
(``DurableIncidentStore.list_page``): no store access, so the page costs the
same three queries whatever the row count. ``status_of`` is the D11 mapping
(task record ``docs/tasks/2026-11-15-web-ui-redesign.md``); the store's SQL
``CASE`` mirrors its group, and a contract test holds the two together.

Nothing here depends on the request time: two renders of the same rows are
identical text (``tests/acceptance/test_m1_04_alert_intake.py``, D13). Ages
are absolute ``<time>`` values; the browser writes the relative words.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from opspilot.web.store import (
    LIST_PER_PAGE,
    LIST_QUERY_CHARS,
    LIST_TABS,
    IncidentListItem,
    IncidentPage,
)

NEEDS_HUMAN = "needs_human"
IN_PROGRESS = "in_progress"
CLOSED = "closed"

TAB_TITLES = {
    "needs_human": "Needs a human",
    "in_progress": "In progress",
    "resolved": "Resolved",
    "closed": "Closed",
    "all": "All",
}

#: Spec section 2: long text is clamped to two lines on the page and kept
#: whole, up to this many characters, in the ``title`` attribute.
TITLE_CHARS = 500

_ACTIVE_RUN = {"queued", "running"}


def status_of(item: IncidentListItem) -> dict[str, Any]:
    """Group, the one to-do label (or ``None``) and the status words of a row.

    First match wins (D11 mapping table in the task record). Closed comes
    first so a cancelled-then-closed incident reads as closed; ownership is
    checked before ``waiting_human`` because a take-over parks the Run there.
    The store's ``_PAGE_GROUP`` SQL repeats the group column of this order.
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


def service_of(item: IncidentListItem) -> str | None:
    """``namespace/workload`` of the alert, else the bound target's
    ``resource_uid`` (store ``_PAGE_SERVICE``)."""
    if item.alert_namespace and item.alert_workload:
        return f"{item.alert_namespace}/{item.alert_workload}"
    return item.summary.target_uid or None


def title_of(item: IncidentListItem) -> str:
    """The row's title (spec 3.2): the alert's ``summary``, its
    ``alertname``, the question, or ``Incident <short id>``; whitespace
    folded, at most ``TITLE_CHARS`` characters."""
    for text in (item.alert_summary, item.alert_name, item.question):
        if text and text.strip():
            return _clip(" ".join(text.split()), TITLE_CHARS)
    return f"Incident {str(item.summary.incident_id)[:8]}"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def stamp(value: datetime | None) -> dict[str, str] | None:
    """A time for ``ui.time``: ISO value, full and minute UTC text."""
    if value is None:
        return None
    t = _utc(value)
    return {
        "iso": t.isoformat(),
        "full": t.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "short": t.strftime("%Y-%m-%d %H:%M UTC"),
    }


# -- request parameters (spec 3.4) -------------------------------------------


def parse_query(params: Mapping[str, str]) -> dict[str, Any]:
    """The list's URL state with the fallbacks of 3.4: an unknown ``tab``
    is the default, ``q`` is stripped and cut to 200 characters, ``page``
    that is not a positive integer is 1. ``service`` is checked against the
    store's values by the store."""
    tab = params.get("tab") or LIST_TABS[0]
    if tab not in LIST_TABS:
        tab = LIST_TABS[0]
    service = (params.get("service") or "").strip() or None
    q = (params.get("q") or "").strip()[:LIST_QUERY_CHARS].strip() or None
    raw = (params.get("page") or "").strip()
    page = int(raw) if raw.isdigit() and 0 < len(raw) <= 9 else 1
    return {"tab": tab, "service": service, "q": q, "page": max(page, 1)}


# -- the same page without SQL (stores without ``list_page``) ----------------


def _matches(item: IncidentListItem, q: str) -> bool:
    needle = q.lower()
    for text in (
        item.question,
        item.alert_name,
        item.alert_summary,
        service_of(item),
    ):
        if text and needle in text.lower():
            return True
    return str(item.summary.incident_id).startswith(needle)


def _in_tab(item: IncidentListItem, tab: str) -> bool:
    if tab in (NEEDS_HUMAN, IN_PROGRESS):
        return bool(status_of(item)["group"] == tab)
    if tab in ("resolved", "closed"):
        return item.summary.lifecycle == tab
    return True


def page_of_items(
    items: Iterable[IncidentListItem],
    *,
    tab: str = LIST_TABS[0],
    service: str | None = None,
    q: str | None = None,
    page: int = 1,
    per_page: int = LIST_PER_PAGE,
) -> IncidentPage:
    """``DurableIncidentStore.list_page`` over rows already in memory, with
    the same fallbacks; ``items`` is every incident, newest first."""
    rows = list(items)
    tab = tab if tab in LIST_TABS else LIST_TABS[0]
    services = tuple(sorted({s for s in map(service_of, rows) if s}))[:200]
    service = service if service in services else None
    kept = [
        item
        for item in rows
        if (service is None or service_of(item) == service)
        and (not q or _matches(item, q))
    ]
    counts = {key: sum(1 for item in kept if _in_tab(item, key)) for key in LIST_TABS}
    pages = max(1, -(-counts[tab] // per_page))
    page = min(max(page, 1), pages)
    shown = [item for item in kept if _in_tab(item, tab)]
    start = (page - 1) * per_page
    return IncidentPage(
        items=tuple(shown[start : start + per_page]),
        counts=counts,
        services=services,
        tab=tab,
        service=service,
        page=page,
        pages=pages,
    )


# -- the page ------------------------------------------------------------------


def _href(tab: str, service: str | None, q: str | None, page: int = 1) -> str:
    params: list[tuple[str, str]] = [("tab", tab)]
    if service:
        params.append(("service", service))
    if q:
        params.append(("q", q))
    if page > 1:
        params.append(("page", str(page)))
    return "/?" + urlencode(params)


def row_of(item: IncidentListItem) -> dict[str, Any]:
    s = item.summary
    status = status_of(item)
    return {
        "item": s,
        "id": str(s.incident_id),
        "short_id": str(s.incident_id)[:8],
        "title": title_of(item),
        "alert_name": item.alert_name,
        "status": status,
        "service": service_of(item),
        "related_short": None
        if s.related_incident_id is None
        else str(s.related_incident_id)[:8],
        "postmortem_href": None
        if status["todo"] != "Review postmortem"
        else f"/postmortems/{item.postmortem_id}/versions/{item.postmortem_version}",
        "created": stamp(s.created_at),
    }


def list_page(
    result: IncidentPage,
    *,
    q: str | None = None,
    targets: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Rows, tabs, pager and filters for ``index.html`` (spec 3.1-3.5)."""
    service = result.service
    tab = result.tab
    counts = result.counts
    total = counts[tab]
    first = 0 if total == 0 else (result.page - 1) * LIST_PER_PAGE + 1
    last = min(result.page * LIST_PER_PAGE, total)
    filtered = bool(service or q)
    if counts["all"] == 0 and not filtered:
        empty = "none"
    elif total == 0 and filtered:
        empty = "filtered"
    elif total == 0:
        empty = "tab"
    else:
        empty = None
    return {
        "rows": [row_of(item) for item in result.items],
        "tabs": [
            {
                "key": key,
                "title": TAB_TITLES[key],
                "count": f"{counts[key]:,}",
                "href": _href(key, service, q),
                "current": key == tab,
            }
            for key in LIST_TABS
        ],
        "tab": tab,
        "tab_title": TAB_TITLES[tab],
        "counts": dict(counts),
        "empty": empty,
        "pager": {
            "first": f"{first:,}",
            "last": f"{last:,}",
            "total": f"{total:,}",
            "prev": None
            if result.page <= 1
            else _href(tab, service, q, result.page - 1),
            "next": None
            if result.page >= result.pages
            else _href(tab, service, q, result.page + 1),
        },
        "filters": {
            "service": service,
            "q": q or "",
            "services": result.services,
            "active": filtered,
            "clear_href": _href(tab, None, None),
        },
        "targets": tuple(targets),
    }
