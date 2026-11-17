"""Independent HTTP contract tests for issue #684 (web UX spec §3 and §7).

Expected memberships are fixture facts, never taken from status_of, list_page,
or the SQL. The memory adapter below only supplies joined alert/postmortem
records absent from the shared memory support; filtering/rendering stay real.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import UUID, uuid4

import pytest

from tests.m1_incident_control_support import list_row
from tests.m1_web_support import (
    MemoryIncidentStore,
    basic,
    build_workbench,
    call,
    post_form,
    same_origin,
)

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
TABS = ("needs_human", "in_progress", "resolved", "closed", "all")
LABELS = ("Needs a human", "In progress", "Resolved", "Closed", "All")
SERVICES = ("shop/checkout", "shop/payment", "database-prod")


@dataclass
class _Element:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)

    @property
    def text(self):
        return "".join(c.text if isinstance(c, _Element) else c for c in self.children)

    @property
    def words(self):
        return " ".join(self.text.split())

    def find(self, tag=None, **attrs):
        found = []
        for child in self.children:
            if isinstance(child, _Element):
                if (tag is None or child.tag == tag) and all(
                    child.attrs.get(k) == v for k, v in attrs.items()
                ):
                    found.append(child)
                found.extend(child.find(tag, **attrs))
        return found


class _HTML(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.root = _Element("document")
        self.stack = [self.root]
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = _Element(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in {
            "area",
            "base",
            "br",
            "col",
            "hr",
            "img",
            "input",
            "link",
            "meta",
        }:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _get(app, path="/", **params):
    if params:
        path = "/?" + urlencode(params)
    response = call(app, "GET", path, headers=basic())
    assert response.status == 200, response.text
    return response, _HTML(response.text).root


def _tabs(html):
    tabs = {}
    for link in html.find("a"):
        for tab, label in zip(TABS, LABELS, strict=True):
            count = re.fullmatch(re.escape(label) + r"\s*\(([\d,]+)\)", link.words)
            if count:
                assert tab not in tabs, f"duplicate tab: {tab}"
                assert parse_qs(urlsplit(link.attrs["href"]).query)["tab"] == [tab]
                tabs[tab] = (int(count[1].replace(",", "")), link)
    assert set(tabs) == set(TABS)
    return tabs


def _ids(html):
    return [
        link.attrs["href"].removeprefix("/incidents/")
        for body in html.find("tbody")
        for link in body.find("a")
        if re.fullmatch(r"/incidents/[0-9a-f-]{36}", link.attrs.get("href", ""))
    ]


def _pager_link(html, label):
    links = [link for link in html.find("a") if label in link.words]
    assert len(links) <= 1
    return links[0] if links else None


class _FixtureStore(MemoryIncidentStore):
    def __init__(self, clock):
        super().__init__(clock)
        self.joined = {}
        self.summary_fields = {}

    def list_incident_items(self, *, limit=50):
        return tuple(
            replace(
                item,
                summary=replace(
                    item.summary,
                    **self.summary_fields.get(item.summary.incident_id, {}),
                ),
                **self.joined.get(item.summary.incident_id, {}),
            )
            for item in super().list_incident_items(limit=limit)
        )


def _build():
    app, workbench, clock = build_workbench()
    store = _FixtureStore(clock)
    store.ledger = workbench.ledger
    workbench.incidents = store
    return app, store, clock


def _seed(
    app,
    store,
    *,
    key,
    question="Investigate checkout",
    created_at=T0,
    run="waiting_human",
    lifecycle="open",
    state="queued",
    mode="automatic",
    service="shop/checkout",
    joined=None,
    summary=None,
):
    target = "database-prod" if service == "database-prod" else "checkout-prod"
    response = post_form(
        app,
        "/intake/ui",
        {"target_id": target, "question": question, "idempotency_key": key},
        headers={**basic(), **same_origin()},
    )
    assert response.status == 201, response.text
    incident_id = response.json()["incident_id"]
    subject = UUID(incident_id)
    row = store.incidents[subject]
    row.update(created_at=created_at, lifecycle=lifecycle, state=state, mode=mode)
    if run is None:
        row["current_run_id"] = None
    else:
        store.runs[row["current_run_id"]]["state"] = run
    fields = dict(joined or {})
    if "conclusion" in fields:
        row["conclusion"] = fields["conclusion"]
    if service != "database-prod":
        fields.update(zip(("alert_namespace", "alert_workload"), service.split("/")))
    store.joined[subject] = fields
    store.summary_fields[subject] = summary or {}
    return incident_id


@dataclass(frozen=True)
class _Incident:
    id: str
    question: str
    service: str
    tabs: frozenset[str]
    created_at: datetime


@pytest.fixture(scope="module")
def large_list():
    app, store, clock = _build()
    records = []
    for index in range(140):
        joined = {}
        if index < 30:
            tabs, lifecycle, run = {"needs_human"}, "open", "waiting_human"
        elif index < 34:
            tabs, lifecycle, run = {"needs_human", "resolved"}, "resolved", "completed"
        elif index < 37:
            tabs, lifecycle, run = {"needs_human", "closed"}, "closed", "completed"
            joined = {
                "postmortem_id": uuid4(),
                "postmortem_version": 1,
                "postmortem_state": "under_review",
            }
        elif index < 57:
            tabs, lifecycle, run = (
                {"in_progress"},
                "open",
                "running" if index % 2 else "queued",
            )
        else:
            tabs, lifecycle, run = {"closed"}, "closed", "completed"
        question = f"Investigate {'latency' if index % 2 == 0 else 'capacity'} incident {index:03d}"
        if index == 0:
            question = "Investigate latency ancient-operator-request"
        service = SERVICES[index % len(SERVICES)]
        created_at = T0 + timedelta(minutes=index)
        incident_id = _seed(
            app,
            store,
            key=f"large-list-{index}",
            question=question,
            created_at=created_at,
            run=run,
            lifecycle=lifecycle,
            service=service,
            joined=joined,
        )
        records.append(
            _Incident(
                incident_id, question, service, frozenset(tabs | {"all"}), created_at
            )
        )
    return app, clock, records


def _assert_counts(html, records, current):
    tabs = _tabs(html)
    assert {tab: value[0] for tab, value in tabs.items()} == {
        tab: sum(tab in record.tabs for record in records) for tab in TABS
    }
    assert [
        tab for tab, (_, link) in tabs.items() if link.attrs.get("aria-current")
    ] == [current]


@pytest.mark.parametrize("tab", TABS)
def test_whole_database_counts_and_every_tab_are_reachable(large_list, tab):
    app, _, records = large_list
    expected = [record.id for record in reversed(records) if tab in record.tabs]
    found = []
    page = 1
    while True:
        response, html = _get(app, tab=tab, page=page)
        _assert_counts(html, records, tab)
        assert {key: count for key, (count, _) in _tabs(html).items()} == {
            "needs_human": 37,
            "in_progress": 20,
            "resolved": 4,
            "closed": 86,
            "all": 140,
        }
        ids = _ids(html)
        assert ids == expected[(page - 1) * 25 : page * 25]
        assert (
            f"{(page - 1) * 25 + 1}–{min(page * 25, len(expected))} of {len(expected)}"
            in html.words
        )
        assert len(ids) <= 25
        assert len(response.body) < 120_000
        found.extend(ids)
        next_link = _pager_link(html, "Next")
        assert (_pager_link(html, "Prev") is not None) == (page > 1)
        if not next_link:
            break
        assert parse_qs(urlsplit(next_link.attrs["href"]).query) == {
            "tab": [tab],
            "page": [str(page + 1)],
        }
        page += 1
        assert page <= 6
    assert found == expected
    assert len(found) == len(set(found))


def test_default_finds_an_incident_older_than_100_others_and_search_finds_it(
    large_list,
):
    app, _, records = large_list
    oldest = records[0]
    assert sum(record.created_at > oldest.created_at for record in records) == 139
    default, first = _get(app)
    explicit, _ = _get(app, tab="needs_human")
    assert default.body == explicit.body
    _assert_counts(first, records, "needs_human")
    assert oldest.id not in _ids(first)
    _, last = _get(app, tab="needs_human", page=2)
    assert oldest.id in _ids(last)
    _, searched = _get(app, q="AnCiEnT-OPERATOR-request")
    assert _ids(searched) == [oldest.id]
    _assert_counts(searched, [oldest], "needs_human")


@pytest.mark.parametrize(
    "tab,last_page,start,total", [("needs_human", 2, 26, 37), ("all", 6, 126, 140)]
)
def test_past_end_is_the_last_page_and_pager_boundaries_are_disabled(
    large_list, tab, last_page, start, total
):
    app, _, records = large_list
    _, first = _get(app, tab=tab)
    assert _pager_link(first, "Prev") is None
    assert any("Prev" in node.words for node in first.find(**{"aria-disabled": "true"}))
    last_response, last = _get(app, tab=tab, page=last_page)
    past, _ = _get(app, tab=tab, page=999)
    assert past.body == last_response.body
    assert f"{start}–{total} of {total}" in last.words
    assert _pager_link(last, "Next") is None
    assert any("Next" in node.words for node in last.find(**{"aria-disabled": "true"}))
    previous = _pager_link(last, "Prev")
    assert previous is not None
    _, back = _get(app, previous.attrs["href"])
    expected = [record.id for record in reversed(records) if tab in record.tabs]
    assert _ids(back) == expected[(last_page - 2) * 25 : (last_page - 1) * 25]


@pytest.mark.parametrize("service", SERVICES)
@pytest.mark.parametrize("q", ["", "latency"])
def test_filter_combinations_update_all_counts_and_tabs_preserve_filters(
    large_list, service, q
):
    app, _, records = large_list
    matches = [
        record
        for record in records
        if record.service == service and q in record.question
    ]
    _, html = _get(app, tab="all", service=service, q=q, page=2)
    _assert_counts(html, matches, "all")
    expected = [record.id for record in reversed(matches)]
    page = 2 if len(expected) > 25 else 1
    assert _ids(html) == expected[(page - 1) * 25 : page * 25]
    selects = html.find("select", name="service")
    assert len(selects) == 1
    options = selects[0].find("option")
    assert (
        options[0].words == "All services" and options[0].attrs.get("value", "") == ""
    )
    assert [node.attrs["value"] for node in options[1:]] == sorted(SERVICES)
    assert [node.attrs["value"] for node in options if "selected" in node.attrs] == [
        service
    ]
    for tab, (_, link) in _tabs(html).items():
        params = parse_qs(urlsplit(link.attrs["href"]).query)
        assert params == {
            "tab": [tab],
            "service": [service],
            **({"q": [q]} if q else {}),
        }
        _, switched = _get(app, link.attrs["href"])
        _assert_counts(switched, matches, tab)
        assert (
            _ids(switched)
            == [record.id for record in reversed(matches) if tab in record.tabs][:25]
        )
    next_link = _pager_link(html, "Next")
    previous = _pager_link(html, "Prev")
    for link in (next_link, previous):
        if link:
            params = parse_qs(urlsplit(link.attrs["href"]).query)
            assert params["service"] == [service]
            assert params.get("q", []) == ([q] if q else [])


@pytest.mark.parametrize("page", ["bogus", "0", "-2", "1.5", ""])
def test_invalid_page_falls_back_to_first_page(large_list, page):
    app, _, _ = large_list
    expected, _ = _get(app)
    actual, _ = _get(app, page=page)
    assert actual.body == expected.body


def test_unknown_tab_and_service_and_trimmed_search_have_safe_fallbacks(large_list):
    app, _, _ = large_list
    expected, _ = _get(app, tab="needs_human", page=2)
    actual, _ = _get(app, tab="bogus", service="unknown/service", page=999)
    assert actual.body == expected.body
    expected, _ = _get(app, tab="all", q="latency")
    actual, html = _get(app, tab="all", q=" \tlatency\n ")
    assert actual.body == expected.body
    assert html.find("input", name="q")[0].attrs["value"] == "latency"
    blank, _ = _get(app, q="   ")
    default, _ = _get(app)
    assert blank.body == default.body


def test_search_is_trimmed_then_capped_at_200_characters():
    app, store, _ = _build()
    term = "界" * 200
    wanted = _seed(app, store, key="long-search", question=term + " correct result")
    _seed(app, store, key="long-search-decoy", question="界" * 199 + " different")
    expected, _ = _get(app, q=term)
    actual, html = _get(app, q="  " + term + "discarded-tail  ")
    assert _ids(html) == [wanted]
    assert actual.body == expected.body
    assert html.find("input", name="q")[0].attrs["value"] == term


@pytest.mark.parametrize(
    "term,question,decoy",
    [
        ("%", "latency 95% threshold", "latency 95X threshold"),
        ("_", "latency under_score", "latency underXscore"),
        ("\\", "latency path\\leaf", "latency pathleaf"),
        ("a%_\\b", "latency a%_\\b literal", "latency aXXb wildcard decoy"),
    ],
)
def test_search_metacharacters_are_literal(term, question, decoy):
    app, store, _ = _build()
    wanted = _seed(app, store, key="literal", question=question)
    _seed(app, store, key="wildcard-decoy", question=decoy)
    _seed(app, store, key="unrelated", question="ordinary question")
    _, html = _get(app, q=term)
    assert _ids(html) == [wanted]
    assert {tab: count for tab, (count, _) in _tabs(html).items()} == {
        "needs_human": 1,
        "in_progress": 0,
        "resolved": 0,
        "closed": 0,
        "all": 1,
    }


def test_search_covers_question_latest_alert_name_summary_service_and_id_prefix():
    app, store, _ = _build()
    manual = _seed(app, store, key="question", question="Manual needle-question")
    alert = _seed(
        app,
        store,
        key="alert",
        joined={
            "alert_name": "NeedleAlertName",
            "alert_summary": "Current needle-alert-summary",
        },
    )
    # The projection represents the newest delivery, not a historical title.
    store.joined[UUID(alert)].update(
        alert_name="NewestAlert", alert_summary="Latest needle-alert-summary"
    )
    database = _seed(app, store, key="database", service="database-prod")
    for term, wanted in [
        ("NEEDLE-QUESTION", manual),
        ("newestalert", alert),
        ("NEEDLE-ALERT-SUMMARY", alert),
        ("database-PROD", database),
        (manual[:8], manual),
    ]:
        _, html = _get(app, q=term)
        assert _ids(html) == [wanted], term
    _, old = _get(app, q="NeedleAlertName")
    assert _ids(old) == []
    _, middle = _get(app, q=manual[9:17])
    assert _ids(middle) == []  # Short IDs match a prefix, not an arbitrary substring.


def test_empty_states_and_clear_filters_link(large_list):
    app, _, _ = _build()
    _, empty = _get(app)
    assert "No incidents yet." in empty.words
    assert "Ask OpsPilot" in empty.words
    app, store, _ = _build()
    _seed(app, store, key="active-only", run="queued")
    _, empty = _get(app)
    assert "Nothing needs a human right now." in empty.words
    _, empty = _get(app, tab="resolved")
    assert "No incidents here." in empty.words
    app, _, _ = large_list
    for tab in TABS:
        _, empty = _get(app, tab=tab, service="shop/checkout", q="unfindable-needle")
        assert _ids(empty) == []
        assert "No incidents match these filters." in empty.words
        assert {
            option.attrs["value"]
            for option in empty.find("select", name="service")[0].find("option")
            if option.attrs.get("value")
        } == set(SERVICES)
        clear = [link for link in empty.find("a") if link.words == "Clear filters"]
        assert clear
        assert all(link.attrs["href"] == f"/?tab={tab}" for link in clear)
        _, cleared = _get(app, clear[0].attrs["href"])
        assert _ids(cleared)


def test_four_visible_columns_raw_cells_time_and_size_are_preserved(large_list):
    app, clock, records = large_list
    first, html = _get(app)
    clock.advance(7201)
    second, _ = _get(app)
    assert first.body == second.body  # D13 also across a change in server time.
    assert len(_ids(html)) == 25
    assert len(first.body) < 120_000
    headers = html.find("th")
    assert [
        node.words
        for node in headers
        if "rawc" not in node.attrs.get("class", "").split()
    ] == ["Title", "Status", "Service", "Age"]
    assert [
        node.words for node in headers if "rawc" in node.attrs.get("class", "").split()
    ] == ["Run", "Control", "Lifecycle", "Concluded"]
    assert len(html.find("thead")) == 1 and len(html.find("thead")[0].find("tr")) == 1
    assert re.search(r"\.rawc\s*\{[^}]*display\s*:\s*none", first.text)
    assert re.search(r"\.clamp2\s*\{[^}]*-webkit-line-clamp\s*:\s*2", first.text)
    legends = [
        details
        for details in html.find("details")
        if any(
            summary.words == "What each status means"
            for summary in details.find("summary")
        )
    ]
    assert len(legends) == 1 and "open" not in legends[0].attrs
    assert first.text.index("What each status means") > first.text.index("1–25 of 37")
    for row in html.find("tbody")[0].find("tr"):
        cells = row.find("td")
        assert len(cells) == 8
        assert all("rawc" in cell.attrs.get("class", "").split() for cell in cells[4:])
        links = cells[0].find("a")
        assert len(links) == 1
        link = links[0]
        assert {"svc", "clamp2"} <= set(link.attrs.get("class", "").split())
        assert 0 < len(link.attrs["title"]) <= 500
        incident_id = link.attrs["href"].removeprefix("/incidents/")
        record = next(record for record in records if record.id == incident_id)
        times = cells[3].find("time")
        assert len(times) == 1
        time = times[0]
        assert datetime.fromisoformat(time.attrs["datetime"]) == record.created_at
        assert time.words == record.created_at.strftime("%Y-%m-%d %H:%M UTC")
        assert time.attrs["title"] == record.created_at.strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
        assert list_row(first.text, incident_id)["Lifecycle"] == (
            "closed"
            if "closed" in record.tabs
            else "resolved"
            if "resolved" in record.tabs
            else "open"
        )


def test_titles_follow_priority_escape_untrusted_text_and_cap_hover_text():
    app, store, _ = _build()
    long_title = '<img src=x onerror="alert(1)"> ' + "long title " * 60
    cases = [
        (
            {
                "question": "Manual fallback",
                "joined": {"alert_summary": long_title, "alert_name": "AlertFallback"},
            },
            long_title,
        ),
        (
            {"question": "Manual fallback", "joined": {"alert_name": "AlertFallback"}},
            "AlertFallback",
        ),
        (
            {"question": "Full manual question with <untrusted> & text"},
            "Full manual question with <untrusted> & text",
        ),
        ({"question": "Full hover " + "x" * 489}, "Full hover " + "x" * 489),
        ({"question": "Temporary intake", "joined": {"question": None}}, None),
    ]
    for index, (kwargs, expected) in enumerate(cases):
        incident_id = _seed(app, store, key=f"title-{index}", **kwargs)
        _, html = _get(app, q=incident_id[:8])
        links = html.find("a", href=f"/incidents/{incident_id}")
        assert len(links) == 1
        link = links[0]
        expected = expected if expected is not None else f"Incident {incident_id[:8]}"
        if len(expected) <= 500:
            assert link.text.strip() == expected
            assert link.attrs["title"] == expected
        else:
            # The contract bounds hover text to 500 characters; it does not
            # prescribe whether a truncated value ends with an ellipsis.
            assert 0 < len(link.attrs["title"]) <= 500
            assert expected.startswith(link.attrs["title"].rstrip("…"))
            assert link.text.strip().startswith(expected[:48])
        assert "clamp2" in link.attrs.get("class", "").split()
        assert not link.find("img") and not link.find("untrusted")


def test_a_filtered_url_is_byte_identical_on_refresh_or_copy(large_list):
    app, clock, _ = large_list
    path = "/?tab=closed&service=shop%2Fcheckout&q=latency&page=2"
    first, _ = _get(app, path)
    clock.advance(3600)
    second, _ = _get(app, path)
    assert first.body == second.body


def test_header_omits_the_authentication_channel(large_list):
    """PR 1 includes removing ui_basic from the signed-in header (§7)."""
    app, _, _ = large_list
    _, html = _get(app)
    assert "ui_basic" not in html.find("header")[0].words


def test_ask_is_collapsed_before_filter_form_and_submits_unchanged_fields(large_list):
    app, _, _ = large_list
    _, html = _get(app)
    asks = html.find("details", id="ask")
    assert len(asks) == 1 and "open" not in asks[0].attrs
    assert asks[0].find("summary")[0].words == "Ask OpsPilot"
    assert "Ask OpsPilot to investigate" in asks[0].words
    forms = html.find("form")
    intake = asks[0].find("form")
    assert len(intake) == 1 and forms[0] is intake[0]
    assert (
        forms[0].attrs["action"] == "/intake/ui" and forms[0].attrs["method"] == "post"
    )
    names = {node.attrs["name"] for node in forms[0].find() if "name" in node.attrs}
    assert names == {"target_id", "question", "idempotency_key"}
    assert (
        forms[0].find("input", name="idempotency_key")[0].attrs.get("value", "") == ""
    )
    assert len(forms) == 2 and forms[1].attrs["method"] == "get"
    assert forms[1].find("select", name="service")
    assert (
        forms[1].find("input", name="q")[0].attrs["placeholder"]
        == "Search title, alert or question"
    )
    assert not html.find(id="ask-jump")
    assert not [
        link
        for link in html.find("a")
        if "ask-open" in link.attrs.get("class", "").split()
    ]
    # A separate empty app exercises submission without changing the large fixture.
    app, _, _ = _build()
    response = post_form(
        app,
        "/intake/ui",
        {
            "target_id": "checkout-prod",
            "question": "A new manual investigation",
            "idempotency_key": "ask-contract",
        },
        headers={**basic(), **same_origin(), "accept": "text/html"},
    )
    assert response.status == 303
    location = response.headers["location"]
    assert re.fullmatch(r"/incidents/[0-9a-f-]{36}", location)
    detail, _ = _get(app, location)
    assert "A new manual investigation" in detail.text


def test_membership_preserves_status_precedence_and_d_l1_overlap():
    app, store, _ = _build()
    # Explicit contract examples, including overrides of a queued/running Run.
    cases = [
        (
            "review",
            {
                "lifecycle": "closed",
                "run": "running",
                "joined": {
                    "postmortem_id": uuid4(),
                    "postmortem_version": 1,
                    "postmortem_state": "under_review",
                },
            },
            {"needs_human", "closed"},
        ),
        (
            "closed",
            {"lifecycle": "closed", "run": "running", "state": "paused"},
            {"closed"},
        ),
        (
            "handoff",
            {"summary": {"handoff_reason": "TARGET_AMBIGUOUS"}, "run": None},
            {"needs_human"},
        ),
        ("pause-control", {"state": "paused", "run": "running"}, {"needs_human"}),
        ("pause-run", {"run": "paused"}, {"needs_human"}),
        ("blocked", {"run": "blocked"}, {"needs_human"}),
        ("owned", {"mode": "human_owned", "run": "queued"}, {"needs_human"}),
        ("waiting", {"run": "waiting_human"}, {"needs_human"}),
        (
            "resolved",
            {"lifecycle": "resolved", "run": "running"},
            {"needs_human", "resolved"},
        ),
        (
            "observing",
            {"lifecycle": "observing_recovery", "run": "completed"},
            {"in_progress"},
        ),
        ("queued", {"run": "queued"}, {"in_progress"}),
        ("running", {"run": "running"}, {"in_progress"}),
        ("cancelled", {"state": "cancelled", "run": "cancelled"}, {"needs_human"}),
        (
            "published",
            {"run": "completed", "joined": {"conclusion": {}}},
            {"needs_human"},
        ),
        ("no-run", {"run": None}, {"needs_human"}),
    ]
    expected = {tab: set() for tab in TABS}
    ids = {}
    for key, kwargs, tabs in cases:
        incident_id = _seed(app, store, key=key, **kwargs)
        ids[key] = incident_id
        for tab in tabs | {"all"}:
            expected[tab].add(incident_id)
    for tab in TABS:
        response, html = _get(app, tab=tab)
        assert set(_ids(html)) == expected[tab]
        assert {key: count for key, (count, _) in _tabs(html).items()} == {
            key: len(ids) for key, ids in expected.items()
        }
        if tab == "needs_human":
            assert (
                "TARGET_AMBIGUOUS" in list_row(response.text, ids["handoff"])["Status"]
            )
            handoff_link = html.find("a", href=f"/incidents/{ids['handoff']}")[0]
            # The raw reason code remains a code element in this incident's row.
            row = next(row for row in html.find("tr") if handoff_link in row.find("a"))
            assert any(code.words == "TARGET_AMBIGUOUS" for code in row.find("code"))
            review_link = html.find("a", href=f"/incidents/{ids['review']}")[0]
            review_row = next(
                row for row in html.find("tr") if review_link in row.find("a")
            )
            actions = [
                link
                for link in review_row.find("a")
                if link.words == "Review postmortem"
            ]
            assert len(actions) == 1
            assert actions[0].attrs.get("href", "").startswith("/")
