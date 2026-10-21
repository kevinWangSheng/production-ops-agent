"""F2 step 1 (PR-C): alert correlation policy U1-A, opt-in real PostgreSQL.

Independent acceptance tests written from docs/tasks/2026-10-21-f2-c-contract.md
(C1-A, C2-A, C3, R-C1..R-C10), U1-A in docs/tasks/2026-10-17-f2-contract.md and
F2 step 1 in feature_list.json. No implementation branch was read.

Observable surface only: the ``/intake/alertmanager`` response, committed rows
through the public projection (``AlertIntakeOutcome``, ``read_inputs``), the
workbench pages and the catalog grants. No internal call order is asserted.

Readings of an ambiguous contract (strictest reasonable one chosen, each also
marked ``AMBIGUOUS`` at the assertion):

* A1 event ``content`` keys: a subset of the R-C4 whitelist, and ``channel``,
  ``text``, ``fingerprint``, ``starts_at``, ``status`` must be present.
* A2 ``text`` carries the fingerprint, the normalized startsAt and the status
  (R-C4 "固定英文模板，只含 fingerprint、规范化 startsAt、status").
* A3 resending an attached episode's delivery after its incident closed is still
  ``attached`` to that incident (C1-A: delivery_key lookup by earliest attached
  delivery), never a new incident.
* A4 the list-page marker text for R-C6 item 3 is unspecified; the page must
  contain "re-fired" (case-insensitive), the one marker phrase the contract names.
* A5 the revision of the correlation migration is not asserted by name; only the
  documented down_revision ``0017_web_suspension_grants`` and the column.
* A6 a downgrade refusal raises; the exact error text is not specified.
"""

from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import schema
from opspilot.persistence import DurableStore
from opspilot.persistence.grants import WEB_ROLE, WORKER_ROLE
from opspilot.tools.profiles import select_profile
from opspilot.web import (
    AuthConfig,
    Authenticator,
    DurableClock,
    DurableEventLog,
    DurableEvidenceStore,
    DurableIncidentStore,
    DurableWebLedger,
    Workbench,
    create_app,
    hash_password,
    token_digest,
)
from opspilot.web.store import MappingTargetRegistry
from scripts.m0.postgres_lab import DSN
from tests.acceptance.test_m1_04_alert_intake import (
    ENTRY,
    EVIDENCE,
    TARGET,
    project,
    results,
    send,
)
from tests.m1_web_support import (
    EVENT_TOKEN,
    ORIGIN,
    UI_PASSWORD,
    UI_USER,
    basic,
    call,
    post_form,
)
from tests.runtime_roles_support import login

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required: M1_DURABLE_POSTGRES=1",
)

T1 = "2026-10-10T10:41:54Z"
T2 = "2026-10-10T10:41:55Z"
T3 = "2026-10-10T10:41:56Z"
T4 = "2026-10-10T10:41:57Z"
EVENT_KEYS = {
    "channel",
    "text",
    "fingerprint",
    "starts_at",
    "status",
    "annotation_revision",
    "delivery_key",
}
REQUIRED_EVENT_KEYS = {"channel", "text", "fingerprint", "starts_at", "status"}


_APP_DSN = {}  # id(app) -> dsn the app was built on


# -- fixtures --------------------------------------------------------------


def _new_database():
    name = f"f2_correlation_{uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    dsn = make_conninfo(DSN, dbname=name)
    schema.migrate(dsn, pg_dump=os.environ.get("OPSPILOT_PG_DUMP", "pg_dump"))
    return name, dsn


def _drop_database(name):
    from opspilot.persistence import close_pools

    close_pools()
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
        )


@pytest.fixture(scope="module")
def database():
    name, dsn = _new_database()
    try:
        yield dsn
    finally:
        _drop_database(name)


@pytest.fixture
def isolated_database():
    name, dsn = _new_database()
    try:
        yield dsn
    finally:
        _drop_database(name)


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    """``make_app(dsn, entries=None)``: a web app on ``dsn`` (any role)."""
    stores = []

    def build(dsn, entries=None):
        path = tmp_path / f"identities-{uuid4().hex}.json"
        path.write_text(
            json.dumps(entries if entries is not None else {TARGET: ENTRY}),
            encoding="utf-8",
        )
        monkeypatch.setenv("OPSPILOT_TARGET_IDENTITIES", str(path))
        store = DurableStore(dsn)
        stores.append(store)
        store.install()
        events = DurableEventLog(store)
        events.install()
        evidence = DurableEvidenceStore(store)
        evidence.install()
        ledger = DurableWebLedger(store)
        ledger.install()
        profile = select_profile({})
        workbench = Workbench(
            incidents=DurableIncidentStore(store),
            events=events,
            evidence=evidence,
            ledger=ledger,
            run_versions=profile.versions(),
            tool_face=profile.face(DurableClock(store)),
            run_seconds=600,
            targets=MappingTargetRegistry.from_file(str(path)),
        )
        auth = AuthConfig(
            ui_users={UI_USER: hash_password(UI_PASSWORD)},
            event_tokens={token_digest(EVENT_TOKEN): "alertmanager"},
            auth_revision="f2-correlation-acceptance-v1",
            allowed_origins=frozenset({ORIGIN}),
        )
        app = create_app(workbench, Authenticator(auth), DurableClock(store))
        _APP_DSN[id(app)] = dsn
        return app

    yield build
    for store in stores:
        store.close()


@pytest.fixture
def harness(database, make_app):
    return lambda entries=None: make_app(database, entries)


@pytest.fixture
def alert():
    result = json.loads((EVIDENCE / "webhook-firing.json").read_text())["alerts"][0]
    result["fingerprint"] = uuid4().hex[:16]
    result["startsAt"] = T1
    return result


# -- helpers ---------------------------------------------------------------


def fire(app, alert, starts_at, **changes):
    """One firing notification of ``alert`` at ``starts_at``; the single result."""
    (item,) = results(send(app, {**deepcopy(alert), **changes, "startsAt": starts_at}))
    return item


def resolve(app, alert, starts_at):
    resolved = json.loads((EVIDENCE / "webhook-resolved.json").read_text())["alerts"][0]
    resolved["fingerprint"] = alert["fingerprint"]
    resolved["startsAt"] = starts_at
    (item,) = results(send(app, resolved))
    return item


def events(dsn, incident_id):
    store = DurableStore(dsn)
    try:
        rows = store.read_inputs(UUID(str(incident_id)))
    finally:
        store.close()
    return [row for row in rows if row["kind"] == "event"]


def incident_row(dsn, incident_id):
    with psycopg.connect(dsn) as conn:
        row = conn.execute(
            "SELECT lifecycle,mode,control_generation,current_run_id "
            "FROM opspilot_incidents WHERE incident_id=%s",
            (UUID(str(incident_id)),),
        ).fetchone()
    return row


def run_count(dsn, incident_id):
    return len(project(dsn, str(incident_id)).run_ids)


def close_incident(app, incident_id):
    return control(app, incident_id, "close")


def control(app, incident_id, action, generation=None):
    if generation is None:
        # Generation of the incident right now, read from the committed row.
        generation = incident_row(app_dsn(app), incident_id)[2]
    response = post_form(
        app,
        f"/incidents/{incident_id}/control",
        {
            "action": action,
            "expected_generation": str(generation),
            "idempotency_key": uuid4().hex,
        },
        headers={
            **basic(),
            "origin": ORIGIN,
            "accept": "application/json",
        },
    )
    assert response.status == 200, (action, response.text)
    return response.json()


def app_dsn(app):
    return _APP_DSN[id(app)]


@pytest.fixture
def web(harness):
    return harness


# -- tests -----------------------------------------------------------------


def test_first_firing_is_created_with_run_and_no_correlation(web, database, alert):
    app = web()
    first = fire(app, alert, T1)
    assert first["outcome"] == "created"
    assert first["incident_id"] and first["run_id"]
    outcome = project(database, first["incident_id"])
    assert outcome.related_incident_id is None
    assert [d.outcome for d in outcome.deliveries] == ["created"]
    assert events(database, first["incident_id"]) == []


def test_open_incident_attaches_new_episode_with_one_whitelisted_event(
    web, database, alert
):
    app = web()
    first = fire(app, alert, T1)
    before = incident_row(database, first["incident_id"])
    # Annotations of the re-fire must not leak into the event (R-C4 / K2).
    marker = "ANNOTATION-SECRET-MARKER-7731"
    refire = {"annotations": {"description": marker, "summary": marker}}
    second = fire(app, alert, T2, **refire)
    assert second["outcome"] == "attached"
    assert second["incident_id"] == first["incident_id"]
    assert second["run_id"] is None
    assert second["starts_at"] == T2
    assert second["delivery_key"] != first["delivery_key"]
    # No second incident, no second Run, no state change (U1-A, F11 U8).
    assert run_count(database, first["incident_id"]) == 1
    assert incident_row(database, first["incident_id"]) == before
    outcome = project(database, first["incident_id"])
    assert [d.outcome for d in outcome.deliveries] == ["created", "attached"]
    assert [d.starts_at for d in outcome.deliveries] == [T1, T2]
    (event,) = events(database, first["incident_id"])
    content = event["content"]
    # AMBIGUOUS A1: subset of the R-C4 whitelist, required keys present.
    assert set(content) <= EVENT_KEYS, content
    assert REQUIRED_EVENT_KEYS <= set(content), content
    assert content["channel"] == "alertmanager"
    assert content["fingerprint"] == alert["fingerprint"]
    assert content["starts_at"] == T2
    assert content["status"] == "firing"
    # AMBIGUOUS A2: fixed text with fingerprint, normalized startsAt, status.
    for needle in (alert["fingerprint"], T2, "firing"):
        assert needle in content["text"], content["text"]
    assert marker not in json.dumps(event["content"])
    assert event["actor"] == "alertmanager"


def test_duplicate_firing_of_attached_episode_adds_no_event(web, database, alert):
    app = web()
    first = fire(app, alert, T1)
    attached = fire(app, alert, T2)
    assert attached["outcome"] == "attached"
    # Exact repeat and a repeat with changed annotations (repeat_interval, R-C3).
    again = fire(app, alert, T2)
    changed = fire(
        app, alert, T2, annotations={"description": "revised", "summary": "revised"}
    )
    for item in (again, changed):
        assert item["outcome"] == "attached"
        assert item["incident_id"] == first["incident_id"]
        assert item["run_id"] is None
        assert item["delivery_key"] == attached["delivery_key"]
    assert len(events(database, first["incident_id"])) == 1
    assert run_count(database, first["incident_id"]) == 1
    outcome = project(database, first["incident_id"])
    assert [d.outcome for d in outcome.deliveries] == [
        "created",
        "attached",
        "attached",
        "attached",
    ]


def test_resolved_for_attached_episode_is_resolved_attached(web, database, alert):
    app = web()
    first = fire(app, alert, T1)
    fire(app, alert, T2)
    before = incident_row(database, first["incident_id"])
    done = resolve(app, alert, T2)
    assert done["outcome"] == "resolved_attached"
    assert done["incident_id"] == first["incident_id"]
    assert done["run_id"] is None and done["status"] == "resolved"
    assert incident_row(database, first["incident_id"]) == before
    assert len(events(database, first["incident_id"])) == 1  # R-C3: no event
    assert [d.outcome for d in project(database, first["incident_id"]).deliveries] == [
        "created",
        "attached",
        "resolved_attached",
    ]
    # An episode that never fired stays a bare record (M1-04 E8 unchanged).
    orphan = resolve(app, alert, T4)
    assert orphan["outcome"] == "resolved_recorded"
    assert orphan["incident_id"] is None


def test_attach_reaches_resolved_and_observing_incidents_without_changing_them(
    web, database, alert
):
    app = web()
    first = fire(app, alert, T1)
    for lifecycle, starts in (("observing_recovery", T2), ("resolved", T3)):
        with psycopg.connect(database) as conn:
            conn.execute(
                "UPDATE opspilot_incidents SET lifecycle=%s WHERE incident_id=%s",
                (lifecycle, UUID(first["incident_id"])),
            )
        before = incident_row(database, first["incident_id"])
        item = fire(app, alert, starts)
        assert item["outcome"] == "attached", lifecycle
        assert item["incident_id"] == first["incident_id"]
        assert incident_row(database, first["incident_id"]) == before
    assert len(events(database, first["incident_id"])) == 2
    assert run_count(database, first["incident_id"]) == 1


def test_all_closed_opens_new_incident_related_to_most_recent_closed(
    web, database, alert
):
    app = web()
    a = fire(app, alert, T1)
    close_incident(app, a["incident_id"])
    b = fire(app, alert, T2)
    assert b["outcome"] == "created" and b["incident_id"] != a["incident_id"]
    assert b["run_id"] and b["run_id"] != a["run_id"]
    assert project(database, a["incident_id"]).related_incident_id is None
    assert project(database, b["incident_id"]).related_incident_id == a["incident_id"]
    # The closed incident is not reopened and receives nothing.
    assert incident_row(database, a["incident_id"])[0] == "closed"
    assert [d.outcome for d in project(database, a["incident_id"]).deliveries] == [
        "created"
    ]
    # Chain: the most recent closed one is the relation, not the first.
    close_incident(app, b["incident_id"])
    c = fire(app, alert, T3)
    assert c["outcome"] == "created"
    assert project(database, c["incident_id"]).related_incident_id == b["incident_id"]


def test_replay_of_attached_episode_after_close_stays_attached(web, database, alert):
    # AMBIGUOUS A3 (C1-A: the delivery_key resolves through the attached row).
    app = web()
    a = fire(app, alert, T1)
    attached = fire(app, alert, T2)
    close_incident(app, a["incident_id"])
    again = fire(app, alert, T2)
    assert again["outcome"] == "attached"
    assert again["incident_id"] == a["incident_id"] == attached["incident_id"]
    assert again["run_id"] is None
    assert len(events(database, a["incident_id"])) == 1


def test_several_open_incidents_attach_to_latest_created(web, database, alert):
    app = web()
    a = fire(app, alert, T1)
    close_incident(app, a["incident_id"])
    b = fire(app, alert, T2)
    control(app, a["incident_id"], "reopen")  # both A and B are open now
    assert incident_row(database, a["incident_id"])[0] == "open"
    assert incident_row(database, b["incident_id"])[0] == "open"
    third = fire(app, alert, T3)
    assert third["outcome"] == "attached"
    assert third["incident_id"] == b["incident_id"]
    assert len(events(database, b["incident_id"])) == 1
    assert events(database, a["incident_id"]) == []


def test_different_fingerprints_stay_separate_incidents(web, database, alert):
    app = web()
    other = {**deepcopy(alert), "fingerprint": uuid4().hex[:16]}
    a = fire(app, alert, T1)
    b = fire(app, other, T1)
    assert a["outcome"] == b["outcome"] == "created"
    assert a["incident_id"] != b["incident_id"]
    # Each fingerprint's re-fire joins only its own incident.
    a2 = fire(app, alert, T2)
    b2 = fire(app, other, T2)
    assert (a2["outcome"], a2["incident_id"]) == ("attached", a["incident_id"])
    assert (b2["outcome"], b2["incident_id"]) == ("attached", b["incident_id"])
    assert len(events(database, a["incident_id"])) == 1
    assert len(events(database, b["incident_id"])) == 1
    assert project(database, b["incident_id"]).related_incident_id is None


def test_handoff_incidents_are_never_candidates(web, database, alert):
    # Unresolved target first: a handoff incident (no target, no Run).
    no_registry = web({})
    handoff = fire(no_registry, alert, T1)
    assert handoff["outcome"] == "handoff_created"
    # The target is now resolvable: a new episode must not attach to the handoff
    # incident and must not be related to it (it has no target to match).
    registry = web()
    created = fire(registry, alert, T2)
    assert created["outcome"] == "created"
    assert created["incident_id"] != handoff["incident_id"]
    assert project(database, created["incident_id"]).related_incident_id is None
    assert events(database, handoff["incident_id"]) == []
    # Reverse: an open resolved-target incident does not absorb an unresolved
    # delivery; it opens its own handoff incident without a relation.
    other = {**deepcopy(alert), "fingerprint": uuid4().hex[:16]}
    first = fire(registry, other, T1)
    second = fire(no_registry, other, T2)
    assert second["outcome"] == "handoff_created"
    assert second["incident_id"] != first["incident_id"]
    assert project(database, second["incident_id"]).related_incident_id is None
    assert events(database, first["incident_id"]) == []


def test_concurrent_distinct_episodes_open_one_incident(web, database):
    app = web()
    for _round in range(3):
        base = json.loads((EVIDENCE / "webhook-firing.json").read_text())["alerts"][0]
        base["fingerprint"] = uuid4().hex[:16]
        starts = [T1, T2, T3, T4]
        with ThreadPoolExecutor(max_workers=len(starts)) as pool:
            responses = list(
                pool.map(lambda s: send(app, {**deepcopy(base), "startsAt": s}), starts)
            )
        items = [results(r)[0] for r in responses]
        assert sorted(i["outcome"] for i in items) == [
            "attached",
            "attached",
            "attached",
            "created",
        ]
        assert len({i["incident_id"] for i in items}) == 1
        incident = items[0]["incident_id"]
        assert run_count(database, incident) == 1
        assert len(events(database, incident)) == 3
        with psycopg.connect(database) as conn:
            (incidents,) = conn.execute(
                "SELECT count(DISTINCT incident_id) FROM opspilot_alert_deliveries "
                "WHERE fingerprint=%s",
                (base["fingerprint"],),
            ).fetchone()
        assert incidents == 1


def test_concurrent_duplicates_of_attached_episode_add_one_event(web, database, alert):
    app = web()
    first = fire(app, alert, T1)
    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(
            pool.map(lambda _: send(app, deepcopy(alert) | {"startsAt": T2}), range(3))
        )
    items = [results(r)[0] for r in responses]
    assert [i["outcome"] for i in items] == ["attached"] * 3
    assert {i["incident_id"] for i in items} == {first["incident_id"]}
    assert len(events(database, first["incident_id"])) == 1


def test_related_incident_link_and_refired_marker_on_pages(web, database, alert):
    app = web()
    a = fire(app, alert, T1)
    attached = fire(app, alert, T2)
    page = call(app, "GET", f"/incidents/{a['incident_id']}", headers=basic())
    assert page.status == 200
    # The delivery table shows the episode's startsAt and marks the re-fire.
    assert T2 in page.text
    assert "re-fired episode" in page.text
    assert page.text.count("re-fired episode") == 1  # one attached row
    assert 'id="related-incident"' not in page.text
    close_incident(app, a["incident_id"])
    b = fire(app, alert, T3)
    related = call(app, "GET", f"/incidents/{b['incident_id']}", headers=basic())
    assert related.status == 200
    match = re.search(r'<[^>]*id="related-incident"[^>]*>', related.text)
    assert match, "related-incident element missing"
    link = re.search(
        r'<a[^>]*href="(/incidents/[^"]+)"[^>]*id="related-incident"|'
        r'<a[^>]*id="related-incident"[^>]*href="(/incidents/[^"]+)"|'
        r'id="related-incident"[^>]*>\s*<a[^>]*href="(/incidents/[^"]+)"',
        related.text,
    )
    assert link, "related-incident must link to the earlier incident"
    assert next(g for g in link.groups() if g) == f"/incidents/{a['incident_id']}"
    # The unrelated first incident shows no relation.
    first_page = call(app, "GET", f"/incidents/{a['incident_id']}", headers=basic())
    assert 'id="related-incident"' not in first_page.text
    assert attached["outcome"] == "attached"


def test_list_page_keeps_one_row_and_marks_refired_resolved_incident(
    web, database, alert
):
    app = web()
    a = fire(app, alert, T1)
    with psycopg.connect(database) as conn:
        conn.execute(
            "UPDATE opspilot_incidents SET lifecycle='resolved' WHERE incident_id=%s",
            (UUID(a["incident_id"]),),
        )
    needle = f'href="/incidents/{a["incident_id"]}"'
    index = call(app, "GET", "/", headers=basic()).text
    assert index.count(needle) == 1
    assert "re-fired" not in index.lower()
    attached = fire(app, alert, T2)
    assert attached["outcome"] == "attached"
    index = call(app, "GET", "/", headers=basic()).text
    assert index.count(needle) == 1  # still one row per incident (C1-A)
    # AMBIGUOUS A4: marker wording for the list is not specified.
    assert "re-fired" in index.lower()


# -- migration, downgrade and grants --------------------------------------


def _column_exists(dsn):
    with psycopg.connect(dsn) as conn:
        return (
            conn.execute(
                "SELECT 1 FROM information_schema.columns WHERE table_name="
                "'opspilot_incidents' AND column_name='related_incident_id'"
            ).fetchone()
            is not None
        )


def test_downgrade_refused_when_attached_rows_exist(isolated_database, make_app, alert):
    app = make_app(isolated_database)
    first = fire(app, alert, T1)
    assert fire(app, alert, T2)["outcome"] == "attached"
    assert _column_exists(isolated_database)
    with psycopg.connect(isolated_database) as conn:
        revision = schema.current_revision(conn)
    # AMBIGUOUS A6: refusal is an exception; its text is unspecified.
    with pytest.raises(Exception):
        command.downgrade(
            schema._config(isolated_database), "0017_web_suspension_grants"
        )
    with psycopg.connect(isolated_database) as conn:
        assert schema.current_revision(conn) == revision
    assert _column_exists(isolated_database)
    assert project(isolated_database, first["incident_id"]).deliveries[-1].outcome == (
        "attached"
    )


def test_downgrade_refused_when_relations_exist(isolated_database, make_app, alert):
    app = make_app(isolated_database)
    a = fire(app, alert, T1)
    close_incident(app, a["incident_id"])
    b = fire(app, alert, T2)
    assert (
        project(isolated_database, b["incident_id"]).related_incident_id
        == (a["incident_id"])
    )
    with psycopg.connect(isolated_database) as conn:
        revision = schema.current_revision(conn)
    with pytest.raises(Exception):
        command.downgrade(
            schema._config(isolated_database), "0017_web_suspension_grants"
        )
    with psycopg.connect(isolated_database) as conn:
        assert schema.current_revision(conn) == revision
    assert (
        project(isolated_database, b["incident_id"]).related_incident_id
        == (a["incident_id"])
    )


def test_clean_downgrade_restores_previous_schema_and_upgrades_again(
    isolated_database,
):
    pg_dump = os.environ["OPSPILOT_PG_DUMP"]
    assert _column_exists(isolated_database)  # head carries the new column
    head = schema.schema_dump(isolated_database, pg_dump=pg_dump)
    command.downgrade(schema._config(isolated_database), "0017_web_suspension_grants")
    assert not _column_exists(isolated_database)
    with psycopg.connect(isolated_database) as conn:
        assert schema.current_revision(conn) == "0017_web_suspension_grants"
    schema.upgrade_head(isolated_database)
    assert schema.schema_dump(isolated_database, pg_dump=pg_dump) == head


def test_no_runtime_role_can_update_related_incident_id(isolated_database):
    with psycopg.connect(isolated_database) as conn:
        for role in (WEB_ROLE, WORKER_ROLE):
            (update,) = conn.execute(
                "SELECT has_column_privilege(%s,'opspilot_incidents',"
                "'related_incident_id','UPDATE')",
                (role,),
            ).fetchone()
            assert update is False, role
        (insert,) = conn.execute(
            "SELECT has_column_privilege(%s,'opspilot_incidents',"
            "'related_incident_id','INSERT')",
            (WEB_ROLE,),
        ).fetchone()
        assert insert is True
        (worker_insert,) = conn.execute(
            "SELECT has_column_privilege(%s,'opspilot_incidents',"
            "'related_incident_id','INSERT')",
            (WORKER_ROLE,),
        ).fetchone()
        assert worker_insert is False


def test_web_login_cannot_rewrite_relation_but_runs_whole_correlation_flow(
    isolated_database, make_app, alert
):
    # Whole flow under the least-privilege web login (grants gaps surface here).
    with login(isolated_database, WEB_ROLE) as web_dsn:
        app = make_app(web_dsn)
        _APP_DSN[id(app)] = isolated_database  # row reads need no web privileges
        a = fire(app, alert, T1)
        attached = fire(app, alert, T2)
        assert attached["outcome"] == "attached"
        assert attached["incident_id"] == a["incident_id"]
        close_incident(app, a["incident_id"])
        b = fire(app, alert, T3)
        assert b["outcome"] == "created"
        assert (
            project(isolated_database, b["incident_id"]).related_incident_id
            == (a["incident_id"])
        )
        with psycopg.connect(web_dsn) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(
                    "UPDATE opspilot_incidents SET related_incident_id=NULL "
                    "WHERE incident_id=%s",
                    (UUID(b["incident_id"]),),
                )
    with login(isolated_database, WORKER_ROLE) as worker_dsn:
        with psycopg.connect(worker_dsn) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(
                    "UPDATE opspilot_incidents SET related_incident_id=NULL "
                    "WHERE incident_id=%s",
                    (UUID(b["incident_id"]),),
                )
