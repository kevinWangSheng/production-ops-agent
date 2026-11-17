"""Incident list tabs, counts, filters and pagination (#684, web UX spec 3.3-3.7).

The SQL ``CASE`` of ``DurableIncidentStore.list_page`` must give every row
the group ``incident_list.status_of`` gives it (spec 3.7, consistency
guard). ``test_sql_group_matches_status_of_for_every_status`` evaluates
that exact SQL fragment on SQLite over all 13 status rows, so the guard runs
without Postgres; the PG-gated test at the end checks the full statements.
"""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from uuid import UUID, uuid4

import pytest

from opspilot.web import store as web_store
from opspilot.web.incident_list import parse_query, status_of
from opspilot.web.store import IncidentListItem, IncidentSummary
from tests.m1_web_support import basic, build_workbench, call, submit_incident

T0 = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def _summary(**fields):
    base = dict(
        incident_id=uuid4(),
        intake_key="k",
        state="queued",
        lifecycle="open",
        control_generation=0,
        current_run_id=None,
        concluded=False,
        created_at=T0,
    )
    base.update(fields)
    return IncidentSummary(**base)


#: One row per D11 status (task record ``2026-11-15-web-ui-redesign.md``):
#: name, IncidentSummary fields, IncidentListItem extras.
STATUSES = [
    (
        "closed_review",
        {"lifecycle": "closed", "state": "closed"},
        {"pm": "under_review"},
    ),
    ("closed", {"lifecycle": "closed", "state": "closed"}, {"pm": "approved"}),
    ("handoff", {"handoff_reason": "TARGET_AMBIGUOUS"}, {}),
    ("paused_control", {"state": "paused", "run_state": "running"}, {}),
    ("paused_run", {"run_state": "paused"}, {}),
    ("blocked", {"run_state": "blocked"}, {}),
    ("taken_over", {"mode": "human_owned", "run_state": "waiting_human"}, {}),
    ("waiting_human", {"run_state": "waiting_human"}, {}),
    ("resolved", {"lifecycle": "resolved", "run_state": "completed"}, {}),
    ("observing", {"lifecycle": "observing_recovery", "run_state": "completed"}, {}),
    ("investigating", {"run_state": "running"}, {}),
    ("cancelled", {"state": "cancelled", "run_state": "cancelled"}, {}),
    ("concluded", {"run_state": "completed", "concluded": True}, {}),
    ("open", {"run_state": "completed"}, {}),
]


def _status_item(fields, extras):
    summary = _summary(**fields)
    pm = extras.get("pm")
    return IncidentListItem(
        summary=summary,
        postmortem_id=uuid4() if pm else None,
        postmortem_version=1 if pm else None,
        postmortem_state=pm,
    )


def test_sql_group_matches_status_of_for_every_status():
    """Spec 3.7 consistency guard: the store's ``_PAGE_GROUP`` SQL, run as
    written, against ``status_of`` for every status row."""
    db = sqlite3.connect(":memory:")
    db.executescript(
        """
        CREATE TABLE opspilot_incidents(incident_id TEXT, state TEXT, lifecycle TEXT, mode TEXT);
        CREATE TABLE opspilot_runs(incident_id TEXT, state TEXT);
        CREATE TABLE opspilot_alert_identities(incident_id TEXT, handoff_reason TEXT);
        CREATE TABLE opspilot_release_incident_links(incident_id TEXT, handoff_reason TEXT, disposition TEXT);
        CREATE TABLE pm(incident_id TEXT, postmortem_id TEXT, state TEXT);
        """
    )
    expected = {}
    for name, fields, extras in STATUSES:
        item = _status_item(fields, extras)
        s = item.summary
        key = str(s.incident_id)
        expected[key] = (name, status_of(item)["group"])
        db.execute(
            "INSERT INTO opspilot_incidents VALUES (?,?,?,?)",
            (key, s.state, s.lifecycle, s.mode),
        )
        if s.run_state is not None:
            db.execute("INSERT INTO opspilot_runs VALUES (?,?)", (key, s.run_state))
        if s.handoff_reason is not None:
            db.execute(
                "INSERT INTO opspilot_alert_identities VALUES (?,?)",
                (key, s.handoff_reason),
            )
        if item.postmortem_state is not None:
            db.execute(
                "INSERT INTO pm VALUES (?,?,?)",
                (key, str(item.postmortem_id), item.postmortem_state),
            )
    # A release-anomaly handoff (F11 R7) has no alert identity.
    release = _status_item({"run_state": None}, {})
    rkey = str(release.summary.incident_id)
    db.execute(
        "INSERT INTO opspilot_incidents VALUES (?,?,?,?)",
        (rkey, "queued", "open", "automatic"),
    )
    db.execute(
        "INSERT INTO opspilot_release_incident_links VALUES (?,?,?)",
        (rkey, "TARGET_UNRESOLVED", "handoff_created"),
    )
    expected[rkey] = ("release_handoff", "needs_human")

    rows = db.execute(
        f"SELECT i.incident_id, {web_store._PAGE_GROUP} AS grp"
        " FROM opspilot_incidents i"
        " LEFT JOIN opspilot_runs r ON r.incident_id=i.incident_id"
        " LEFT JOIN opspilot_alert_identities a ON a.incident_id=i.incident_id"
        " LEFT JOIN pm ON pm.incident_id=i.incident_id"
    ).fetchall()
    assert len(rows) == len(STATUSES) + 1
    mismatched = {
        expected[key][0]: (grp, expected[key][1])
        for key, grp in rows
        if grp != expected[key][1]
    }
    assert mismatched == {}
    # Every group appears, so the comparison is not vacuous.
    assert {g for _, g in expected.values()} == {"needs_human", "in_progress", "closed"}


def test_status_rows_cover_thirteen_statuses_and_each_to_do():
    todos = {status_of(_status_item(f, e))["todo"] for _, f, e in STATUSES}
    words = {status_of(_status_item(f, e))["words"] for _, f, e in STATUSES}
    assert len(words | todos) >= 13


@pytest.mark.parametrize(
    "params, expected",
    [
        ({}, {"tab": "needs_human", "service": None, "q": None, "page": 1}),
        ({"tab": "bogus", "page": "-3"}, {"tab": "needs_human", "page": 1}),
        ({"tab": "closed", "page": "2"}, {"tab": "closed", "page": 2}),
        ({"page": "x"}, {"page": 1}),
        ({"page": "0"}, {"page": 1}),
        ({"q": "  checkout  "}, {"q": "checkout"}),
        ({"q": "a" * 300}, {"q": "a" * 200}),
        ({"q": "   ", "service": " "}, {"q": None, "service": None}),
    ],
)
def test_parse_query_fallbacks(params, expected):
    parsed = parse_query(params)
    for key, value in expected.items():
        assert parsed[key] == value


# -- the page over the in-memory store --------------------------------------


class _Rows(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ths: list[str] = []
        self.links: list[str] = []
        self.forms: list[dict] = []
        self._th = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "th":
            self._th = True
            self.ths.append("")
        if tag == "a" and (a.get("href") or "").startswith("/incidents/"):
            self.links.append(a["href"])
        if tag == "form":
            self.forms.append(a)

    def handle_endtag(self, tag):
        if tag == "th":
            self._th = False

    def handle_data(self, data):
        if self._th:
            self.ths[-1] += data.strip()


def _parse(html):
    page = _Rows()
    page.feed(html)
    return page


def _get(app, path):
    response = call(app, "GET", path, headers=basic())
    assert response.status == 200, response.text
    return response.text


def _many(n, *, start=0):
    app, workbench, _ = build_workbench()
    ids = []
    for index in range(start, start + n):
        ids.append(
            submit_incident(
                app, key=f"many-{index}", question=f"question number {index:03d}"
            ).json()["incident_id"]
        )
    # Distinct creation times, newest last submitted.
    for offset, incident_id in enumerate(ids):
        workbench.incidents.incidents[UUID(incident_id)]["created_at"] = T0 + timedelta(
            minutes=offset
        )
    return app, workbench, ids


def _set(workbench, incident_id, *, lifecycle=None, run=None, state=None):
    row = workbench.incidents.incidents[UUID(incident_id)]
    if lifecycle:
        row["lifecycle"] = lifecycle
    if state:
        row["state"] = state
    if run:
        workbench.incidents.runs[row["current_run_id"]]["state"] = run


def test_tabs_count_the_whole_store_and_pages_hold_25_rows():
    app, workbench, ids = _many(40)
    # 30 need a human (waiting), 5 closed, 3 resolved, 2 still queued.
    for incident_id in ids[:30]:
        _set(workbench, incident_id, run="waiting_human")
    for incident_id in ids[30:35]:
        _set(workbench, incident_id, lifecycle="closed")
    for incident_id in ids[35:38]:
        _set(workbench, incident_id, lifecycle="resolved", run="completed")

    listing = workbench.incident_list()
    counts = {t["key"]: t["count"] for t in listing["tabs"]}
    assert counts == {
        "needs_human": "33",
        "in_progress": "2",
        "resolved": "3",
        "closed": "5",
        "all": "40",
    }
    assert listing["tab"] == "needs_human"
    assert len(listing["rows"]) == 25
    assert listing["pager"]["total"] == "33"
    assert listing["pager"]["prev"] is None
    assert listing["pager"]["next"] == "/?tab=needs_human&page=2"

    second = workbench.incident_list(page=2)
    assert len(second["rows"]) == 8
    assert second["pager"]["first"] == "26" and second["pager"]["last"] == "33"
    assert second["pager"]["next"] is None
    shown = {r["id"] for r in listing["rows"]} | {r["id"] for r in second["rows"]}
    # Every incident that needs a human is reachable, including the oldest.
    assert set(ids[:30]) <= shown
    # Past the last page shows the last page.
    assert workbench.incident_list(page=99)["rows"] == second["rows"]

    html = _get(app, "/?tab=needs_human&page=2")
    assert "26–33 of 33" in html
    assert _parse(html).ths == ["Title", "Status", "Service", "Age"]
    assert len(_parse(html).links) == 8


def test_newest_first_and_oldest_waiting_incident_on_the_last_page():
    app, workbench, ids = _many(30)
    for incident_id in ids:
        _set(workbench, incident_id, lifecycle="closed")
    _set(workbench, ids[0], lifecycle="open", run="waiting_human")
    listing = workbench.incident_list()
    assert [r["id"] for r in listing["rows"]] == [ids[0]]
    closed = workbench.incident_list(tab="closed")
    assert closed["rows"][0]["id"] == ids[-1]
    assert closed["pager"]["total"] == "29"


def test_search_and_service_filters_narrow_counts_and_keep_the_tab():
    app, workbench, ids = _many(3)
    for incident_id in ids:
        _set(workbench, incident_id, run="waiting_human")
    listing = workbench.incident_list(q="number 001")
    assert [r["id"] for r in listing["rows"]] == [ids[1]]
    assert {t["key"]: t["count"] for t in listing["tabs"]}["all"] == "1"
    assert listing["tabs"][4]["href"] == "/?tab=all&q=number+001"
    # Short-id prefix.
    assert [r["id"] for r in workbench.incident_list(q=ids[2][:8])["rows"]] == [ids[2]]
    # Wildcards are literal.
    assert workbench.incident_list(q="%")["empty"] == "filtered"
    # A Service value not in the list is ignored; a listed one filters.
    services = workbench.incident_list()["filters"]["services"]
    assert services == ("checkout-prod",)
    assert len(workbench.incident_list(service="nope")["rows"]) == 3
    assert workbench.incident_list(service="nope")["filters"]["service"] is None
    assert len(workbench.incident_list(service="checkout-prod")["rows"]) == 3

    html = _get(app, "/?q=no+such+thing")
    assert "No incidents match these filters." in html
    assert 'href="/?tab=needs_human"' in html


def test_empty_states():
    app, workbench, _ = build_workbench()
    html = _get(app, "/")
    assert "No incidents yet." in html and 'href="#ask"' in html
    submit_incident(app, key="queued-one")
    html = _get(app, "/")
    assert "Nothing needs a human right now." in html
    assert "No incidents here." in _get(app, "/?tab=closed")


def test_rows_carry_title_status_service_and_absolute_age_only():
    app, workbench, ids = _many(1)
    _set(workbench, ids[0], run="waiting_human")
    html = _get(app, "/")
    assert html.count(f'href="/incidents/{ids[0]}"') == 1
    assert 'class="svc clamp2"' in html and 'title="question number 000"' in html
    assert '<time class="rel" datetime="2026-11-16T12:00:00+00:00"' in html
    assert "2026-11-16 12:00 UTC" in html
    assert "Ask or take over" in html
    assert "checkout-prod" in html
    # The raw stored-value columns left the list (spec 3.2).
    for header in ("Run", "Control", "Lifecycle", "Concluded", "Agent conclusion"):
        assert f"<th>{header}</th>" not in html


def test_ask_form_moved_into_the_ask_details_unchanged_and_get_filter_form():
    app, _, _ = build_workbench()
    submit_incident(app, key="form")
    html = _get(app, "/?tab=all")
    forms = _parse(html).forms
    assert [f.get("method") for f in forms] == ["post", "get"]
    assert forms[0].get("action") == "/intake/ui"
    ask = re.search(r'<details class="askd" id="ask">(.*?)</details>', html, re.S)
    assert ask and 'action="/intake/ui"' in ask.group(1)
    for name in ("target_id", "question", "idempotency_key"):
        assert f'name="{name}"' in ask.group(1)
    assert 'id="intake-key"' in ask.group(1) and "value=" not in re.search(
        r'<input name="idempotency_key"[^>]*>', html
    ).group(0)


@pytest.mark.parametrize("path", ["/", "/?tab=all", "/?tab=closed&q=x&page=3"])
def test_repeated_gets_are_byte_identical(path):
    app, workbench, ids = _many(3)
    _set(workbench, ids[0], run="waiting_human")
    assert _get(app, path) == _get(app, path)


def test_nav_marks_incidents_current_on_the_list():
    app, _, _ = build_workbench()
    html = _get(app, "/")
    assert '<a href="/" aria-current="page">Incidents</a>' in html
    assert 'href="/version-changes"' in html


# -- Postgres: the three statements ----------------------------------------

PG = os.environ.get("M1_DURABLE_POSTGRES") == "1"


@pytest.mark.skipif(not PG, reason="explicit PG opt-in required")
def test_pg_list_page_groups_counts_and_pages_match_the_python_view():
    """The real statements: ``base.grp`` equals ``status_of`` for every
    incident in the database, and the counts and the page agree with the
    in-memory mirror over the same rows."""
    import psycopg

    from opspilot.web.incident_list import page_of_items
    from tests.integration.test_m1_web_postgres import DSN, _build
    from tests.m1_web_support import post_form, same_origin

    app, workbench, store = _build()
    created = []
    for index in range(12):
        result = post_form(
            app,
            "/intake/ui",
            {
                "target_id": "checkout-prod",
                "question": f"pg list {index} {uuid4()}",
                "idempotency_key": f"pg-list-{uuid4()}",
            },
            headers={**basic(), **same_origin()},
        )
        assert result.status == 201, result.text
        created.append(UUID(result.json()["incident_id"]))
    changes = [
        ("UPDATE opspilot_incidents SET state='paused' WHERE incident_id=%s", None),
        (None, "paused"),
        (None, "blocked"),
        (
            "UPDATE opspilot_incidents SET mode='human_owned' WHERE incident_id=%s",
            "waiting_human",
        ),
        (None, "waiting_human"),
        (
            "UPDATE opspilot_incidents SET lifecycle='resolved' WHERE incident_id=%s",
            "completed",
        ),
        (
            "UPDATE opspilot_incidents SET lifecycle='observing_recovery' WHERE incident_id=%s",
            "completed",
        ),
        (None, "running"),
        (
            "UPDATE opspilot_incidents SET state='cancelled' WHERE incident_id=%s",
            "cancelled",
        ),
        (
            "UPDATE opspilot_incidents SET conclusion='{}'::jsonb WHERE incident_id=%s",
            "completed",
        ),
        (
            "UPDATE opspilot_incidents SET lifecycle='closed' WHERE incident_id=%s",
            "completed",
        ),
        (None, "completed"),
    ]
    with psycopg.connect(DSN) as conn:
        for incident_id, (statement, run) in zip(created, changes, strict=True):
            if statement:
                conn.execute(statement, (incident_id,))
            if run:
                conn.execute(
                    "UPDATE opspilot_runs SET state=%s WHERE run_id=(SELECT current_run_id FROM opspilot_incidents WHERE incident_id=%s)",
                    (run, incident_id),
                )
    incidents = workbench.incidents
    groups = incidents.page_groups()
    items = incidents.list_incident_items(limit=100_000)
    assert {i.summary.incident_id: status_of(i)["group"] for i in items} == groups
    for tab in ("needs_human", "in_progress", "resolved", "closed", "all"):
        for q in (None, "pg list", str(created[3])[:8]):
            sql = incidents.list_page(tab=tab, q=q, page=1)
            mirror = page_of_items(items, tab=tab, q=q, page=1)
            assert dict(sql.counts) == dict(mirror.counts), (tab, q)
            assert [i.summary.incident_id for i in sql.items] == [
                i.summary.incident_id for i in mirror.items
            ], (tab, q)
            assert sql.pages == mirror.pages
