"""The incident list page's view (web UX spec 2026-11-16, section 3).

Pure functions over one page the store read
(``DurableIncidentStore.list_page``): no store access, so the page costs the
same three queries whatever the row count. ``status_of`` is the shared
incident status (``status.incident_status``, redesign contract A8); the
store's SQL ``CASE`` mirrors its group.

Nothing here depends on the request time: two renders of the same rows are
identical text (``tests/acceptance/test_m1_04_alert_intake.py``, D13). Ages
are absolute ``<time>`` values; the browser writes the relative words.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from opspilot.web.status import IN_PROGRESS, NEEDS_HUMAN, incident_status
from opspilot.web.store import (
    LIST_PER_PAGE,
    LIST_QUERY_CHARS,
    LIST_TABS,
    IncidentListItem,
    IncidentPage,
)

TAB_TITLES = {
    "needs_human": "Needs attention",
    "in_progress": "In progress",
    "resolved": "Resolved",
    "closed": "Closed",
    "all": "All",
}

#: Spec section 2: long text is clamped to two lines on the page and kept
#: whole, up to this many characters, in the ``title`` attribute.
TITLE_CHARS = 500

_PAGE = re.compile(r"[0-9]+")
#: The largest page a query string asks for; longer numbers saturate here.
PAGE_MAX = 999_999_999


def status_of(item: IncidentListItem) -> dict[str, Any]:
    """Group, the one to-do label (or ``None``), the status and a short
    detail of a row.

    The status is the page header's (``status.incident_status``, contract
    A8); the store's ``_PAGE_GROUP`` SQL repeats its group. A closed
    incident whose postmortem waits for review is grouped under Needs a
    human with that to-do.
    """
    s = item.summary
    status = incident_status(
        merged=s.merged_into_incident_id is not None,
        lifecycle=s.lifecycle,
        state=s.state,
        run_state=s.run_state,
        mode=s.mode,
        concluded=s.concluded,
    )
    group = status.group
    todo: str | None = None
    detail: str | None = None
    key = status.key
    if key in ("closed", "merged"):
        if item.postmortem_state == "under_review" and item.postmortem_id:
            group = NEEDS_HUMAN
            todo = "Review postmortem"
            detail = f"postmortem version {item.postmortem_version} waits for review"
        elif s.merged_into_incident_id is not None:
            detail = f"into {str(s.merged_into_incident_id)[:8]}"
    elif s.handoff_only:
        todo = "Handle outside OpsPilot"
        detail = "no single registered target matched, no Run"
    elif key == "resolved":
        todo = "Close or reopen"
        detail = "recovery confirmed"
    elif key == "canceled":
        todo = "Re-investigate or close"
        detail = "the Run was canceled"
    elif key == "paused":
        todo = "Resume"
        detail = "no investigation until resumed"
    elif key == "human_owned":
        todo = "Mark remediated or close"
        detail = "the Agent does not investigate"
    elif key == "investigating":
        detail = "waiting for a worker" if s.run_state == "queued" else "in progress"
    elif key == "watching":
        detail = (
            f"{item.recovery_samples} samples so far"
            if item.recovery_samples
            else "the Observer samples health"
        )
    elif key == "root_cause":
        todo = "Mark remediated or close"
        detail = "no recovery watch running"
    else:
        todo = "Re-investigate or take over"
        detail = {
            "waiting_human": "the Agent stopped and handed it back",
            "blocked": "the Run was blocked",
        }.get(s.run_state or "", "no Run in progress")
    return {
        "group": group,
        "todo": todo,
        "words": status.label,
        "detail": detail,
        "status": status,
    }


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
    # NUL cannot reach a Postgres text parameter; drop it rather than fail.
    service = (params.get("service") or "").replace("\x00", "").strip() or None
    text = (params.get("q") or "").replace("\x00", "")
    q = text.strip()[:LIST_QUERY_CHARS].strip() or None
    raw = params.get("page") or ""
    # ASCII digits only: str.isdigit() and int() also take fullwidth and
    # superscript digits. Leading zeros are ignored; a number too long to
    # be a page saturates, so it reads like any page past the end, which
    # the store clamps to the last page.
    if _PAGE.fullmatch(raw):
        digits = raw.lstrip("0")
        page = PAGE_MAX if len(digits) > 9 else int(digits or "0")
    else:
        page = 1
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
        # Workbench P6 L4: a survivor's row says how many incidents it holds.
        "linked": item.linked_count,
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
            "page": f"{result.page:,}",
            "pages": f"{result.pages:,}",
            "many": result.pages > 1,
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
