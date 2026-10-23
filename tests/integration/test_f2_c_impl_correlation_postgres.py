"""F2 PR-C implementation tests on real PostgreSQL: alert correlation through
``/intake/alertmanager``, the pages, the acceptance projection and migration
0018. Opt-in like the M1-04 tests (``M1_DURABLE_POSTGRES=1``)."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import schema
from opspilot.acceptance_alert import alert_intake_outcome, alert_intake_records
from opspilot.persistence import DurableStore
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
from tests.m1_web_support import (
    EVENT_TOKEN,
    ORIGIN,
    UI_PASSWORD,
    UI_USER,
    basic,
    bearer,
    call,
    post_json,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required: M1_DURABLE_POSTGRES=1",
)

PG_DUMP = os.environ.get("OPSPILOT_PG_DUMP", "pg_dump")
EVIDENCE = Path(__file__).resolve().parents[2] / "docs/evidence/m1-04-lab"


def _entry(namespace: str) -> dict:
    return {
        "integration_id": "m0-otel-20260909",
        "cluster_uid": "opspilot-m1",
        "namespace": namespace,
        "workload": "checkout",
        "health_profile_id": "otel-demo-checkout",
        "match": {
            "version": 1,
            "labels": {"namespace": namespace, "service": "checkout"},
        },
    }


TARGETS = {"checkout-prod": _entry("otel-demo"), "checkout-b": _entry("other")}


def _create_db(prefix: str) -> str:
    name = f"{prefix}_{uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    return name


def _drop_db(name: str) -> None:
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
        )


@pytest.fixture(scope="module")
def database():
    name = _create_db("f2_c_impl")
    dsn = make_conninfo(DSN, dbname=name)
    try:
        schema.migrate(dsn, pg_dump=PG_DUMP)
        yield dsn
    finally:
        _drop_db(name)


@pytest.fixture
def app(database, tmp_path, monkeypatch):
    path = tmp_path / "identities.json"
    path.write_text(json.dumps(TARGETS), encoding="utf-8")
    monkeypatch.setenv("OPSPILOT_TARGET_IDENTITIES", str(path))
    store = DurableStore(database)
    store.install()
    events = DurableEventLog(store)
    events.install()
    evidence = DurableEvidenceStore(store)
    evidence.install()
    ledger = DurableWebLedger(store)
    ledger.install()
    profile = select_profile({})
    clock = DurableClock(store)
    workbench = Workbench(
        incidents=DurableIncidentStore(store),
        events=events,
        evidence=evidence,
        ledger=ledger,
        run_versions=profile.versions(),
        tool_face=profile.face(clock),
        run_seconds=600,
        targets=MappingTargetRegistry.from_file(str(path)),
    )
    auth = AuthConfig(
        ui_users={UI_USER: hash_password(UI_PASSWORD)},
        event_tokens={token_digest(EVENT_TOKEN): "alertmanager"},
        auth_revision="f2-c-impl-v1",
        allowed_origins=frozenset({ORIGIN}),
    )
    yield create_app(workbench, Authenticator(auth), clock)
    store.close()


@pytest.fixture
def alert():
    result = json.loads((EVIDENCE / "webhook-firing.json").read_text())["alerts"][0]
    result["fingerprint"] = uuid4().hex[:16]
    result["startsAt"] = "2026-10-21T10:00:00Z"
    return result


def send(app, item):
    response = post_json(
        app,
        "/intake/alertmanager",
        {"version": "4", "status": item["status"], "alerts": [item]},
        headers=bearer(),
    )
    assert response.status == 200, response.text
    (result,) = response.json()["results"]
    return result


def episode(alert, second: int, **changes):
    return {**alert, "startsAt": f"2026-10-21T10:00:{second:02d}Z", **changes}


def project(database, incident_id):
    with psycopg.connect(database) as conn:
        records = alert_intake_records(conn, incident_id)
    return alert_intake_outcome(
        SimpleNamespace(scenario_id="f2-c-impl", subject_id=incident_id), records
    )


def rows(database, query, *args):
    with psycopg.connect(database) as conn:
        return conn.execute(query, args).fetchall()


def incident_row(database, incident_id):
    (row,) = rows(
        database,
        "SELECT lifecycle,mode,control_generation,current_run_id,state,related_incident_id FROM opspilot_incidents WHERE incident_id=%s",
        incident_id,
    )
    return row


def events(database, incident_id):
    return rows(
        database,
        "SELECT input_id,content,actor,control_generation FROM opspilot_inputs WHERE incident_id=%s AND kind='event' ORDER BY sequence",
        incident_id,
    )


def set_lifecycle(database, incident_id, lifecycle):
    with psycopg.connect(database) as conn:
        conn.execute(
            "UPDATE opspilot_incidents SET lifecycle=%s WHERE incident_id=%s",
            (lifecycle, incident_id),
        )


def test_new_episode_attaches_to_the_open_incident_without_a_run(app, database, alert):
    first = send(app, alert)
    assert first["outcome"] == "created"
    before = incident_row(database, first["incident_id"])

    second = send(app, episode(alert, 1))
    assert second["outcome"] == "attached"
    assert second["incident_id"] == first["incident_id"]
    assert second["run_id"] is None
    assert incident_row(database, first["incident_id"]) == before
    outcome = project(database, first["incident_id"])
    assert outcome.run_ids == (first["run_id"],)
    assert [d.outcome for d in outcome.deliveries] == ["created", "attached"]
    assert outcome.deliveries[1].starts_at == "2026-10-21T10:00:01Z"
    assert outcome.related_incident_id is None
    (event,) = events(database, first["incident_id"])
    content = event[1]
    assert content["channel"] == "alertmanager"
    assert alert["fingerprint"] in content["text"] and "10:00:01Z" in content["text"]
    assert set(content) == {
        "channel",
        "text",
        "fingerprint",
        "starts_at",
        "status",
        "annotation_revision",
        "delivery_key",
    }
    assert content["delivery_key"] == second["delivery_key"]
    assert event[2] == "alertmanager"

    # Repeats, an annotation change and the resolved notification of the
    # attached episode add delivery rows only.
    again = send(app, episode(alert, 1))
    changed = send(
        app, episode(alert, 1, annotations={**alert["annotations"], "x": "y"})
    )
    resolved = send(
        app, episode(alert, 1, status="resolved", endsAt="2026-10-21T10:05:00Z")
    )
    assert [again["outcome"], changed["outcome"], resolved["outcome"]] == [
        "attached",
        "attached",
        "resolved_attached",
    ]
    assert {r["incident_id"] for r in (again, changed, resolved)} == {
        first["incident_id"]
    }
    assert len(events(database, first["incident_id"])) == 1
    assert incident_row(database, first["incident_id"]) == before
    revisions = [
        d.annotation_revision
        for d in project(database, first["incident_id"]).deliveries
    ]
    # The resolved notification carries the original annotations again.
    assert revisions == [1, 1, 1, 2, 3]

    page = call(app, "GET", f"/incidents/{first['incident_id']}", headers=basic())
    assert page.status == 200
    assert "re-fired episode" in page.text
    assert "2026-10-21T10:00:01Z" in page.text


@pytest.mark.parametrize("lifecycle", ["observing_recovery", "resolved"])
def test_non_closed_lifecycles_attach_and_keep_state(app, database, alert, lifecycle):
    first = send(app, alert)
    set_lifecycle(database, first["incident_id"], lifecycle)
    before = incident_row(database, first["incident_id"])
    second = send(app, episode(alert, 2))
    assert second["outcome"] == "attached"
    assert second["incident_id"] == first["incident_id"]
    assert incident_row(database, first["incident_id"]) == before
    index = call(app, "GET", "/", headers=basic())
    marked = "alert re-fired after resolved" in index.text
    assert marked == (lifecycle == "resolved")


def test_closed_incident_gets_a_related_new_incident(app, database, alert):
    first = send(app, alert)
    set_lifecycle(database, first["incident_id"], "closed")
    second = send(app, episode(alert, 3))
    assert second["outcome"] == "created"
    assert second["incident_id"] != first["incident_id"]
    assert second["run_id"]
    assert incident_row(database, first["incident_id"])[0] == "closed"
    assert (
        project(database, second["incident_id"]).related_incident_id
        == first["incident_id"]
    )
    page = call(app, "GET", f"/incidents/{second['incident_id']}", headers=basic())
    assert 'id="related-incident"' in page.text
    assert f"/incidents/{first['incident_id']}" in page.text
    # The new incident is now the open one the next episode attaches to.
    third = send(app, episode(alert, 4))
    assert (third["outcome"], third["incident_id"]) == (
        "attached",
        second["incident_id"],
    )


def test_all_closed_relates_to_the_latest(app, database, alert):
    first = send(app, alert)
    set_lifecycle(database, first["incident_id"], "closed")
    second = send(app, episode(alert, 5))
    set_lifecycle(database, second["incident_id"], "closed")
    third = send(app, episode(alert, 6))
    assert third["outcome"] == "created"
    assert (
        project(database, third["incident_id"]).related_incident_id
        == second["incident_id"]
    )


def test_other_target_fingerprint_or_handoff_does_not_attach(app, database, alert):
    first = send(app, alert)
    other_target = send(
        app, episode(alert, 7, labels={**alert["labels"], "namespace": "other"})
    )
    assert other_target["outcome"] == "created"
    assert other_target["incident_id"] != first["incident_id"]
    other_fp = send(app, {**alert, "fingerprint": uuid4().hex[:16]})
    assert other_fp["outcome"] == "created"
    handoff = send(
        app, episode(alert, 8, labels={**alert["labels"], "namespace": "nowhere"})
    )
    assert handoff["outcome"] == "handoff_created"
    assert project(database, handoff["incident_id"]).related_incident_id is None
    assert len(events(database, first["incident_id"])) == 0


def test_concurrent_first_deliveries_of_two_episodes_open_one_incident(
    app, database, alert
):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda s: send(app, episode(alert, s)), (10, 11)))
    assert sorted(r["outcome"] for r in results) == ["attached", "created"]
    assert len({r["incident_id"] for r in results}) == 1


def test_close_and_attach_race_never_attaches_to_a_closed_incident(
    database, alert, app
):
    first = send(app, alert)
    incident = first["incident_id"]
    # Hold the incident row the way a human close does, then commit the close
    # while the intake waits on it.
    with psycopg.connect(database) as closer:
        closer.execute(
            "SELECT 1 FROM opspilot_scope_controls WHERE scope_id=1 FOR SHARE"
        )
        closer.execute(
            "SELECT 1 FROM opspilot_incidents WHERE incident_id=%s FOR UPDATE",
            (incident,),
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(send, app, episode(alert, 12))
            closer.execute(
                "UPDATE opspilot_incidents SET lifecycle='closed' WHERE incident_id=%s",
                (incident,),
            )
            closer.commit()
            result = pending.result(timeout=30)
    assert result["outcome"] == "created"
    assert project(database, result["incident_id"]).related_incident_id == incident


def test_web_role_cannot_update_related_incident(database):
    from opspilot.persistence.grants import RUNTIME_GRANTS, WEB_ROLE, WORKER_ROLE

    web = RUNTIME_GRANTS[WEB_ROLE]["opspilot_incidents"]
    assert any("related_incident_id" in g for g in web if g.startswith("INSERT"))
    assert not any("related_incident_id" in g for g in web if g.startswith("UPDATE"))
    worker = RUNTIME_GRANTS[WORKER_ROLE]["opspilot_incidents"]
    assert not any("related_incident_id" in g for g in worker)
    with psycopg.connect(database) as conn:
        for role, privilege, expected in (
            (WEB_ROLE, "INSERT", True),
            (WEB_ROLE, "UPDATE", False),
            (WORKER_ROLE, "INSERT", False),
            (WORKER_ROLE, "UPDATE", False),
        ):
            (granted,) = conn.execute(
                "SELECT has_column_privilege(%s,'opspilot_incidents','related_incident_id',%s)",
                (role, privilege),
            ).fetchone()
            assert granted is expected, (role, privilege)


def test_self_relation_is_refused(database):
    incident = uuid4()
    with psycopg.connect(database) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO opspilot_incidents(incident_id,intake_key,state,lifecycle,related_incident_id) VALUES(%s,%s,'waiting_human','open',%s)",
                (incident, f"self:{incident}", incident),
            )


@pytest.fixture
def scratch():
    name = _create_db("f2_c_impl_mig")
    try:
        yield make_conninfo(DSN, dbname=name)
    finally:
        _drop_db(name)


def _seed(dsn: str, *, attached: bool, related: bool) -> None:
    old, new = uuid4(), uuid4()
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO opspilot_incidents(incident_id,intake_key,state,lifecycle) VALUES(%s,%s,'waiting_human','closed')",
            (old, f"seed:{old}"),
        )
        if related:
            conn.execute(
                "INSERT INTO opspilot_incidents(incident_id,intake_key,state,lifecycle,related_incident_id) VALUES(%s,%s,'waiting_human','open',%s)",
                (new, f"seed:{new}", old),
            )
        if attached:
            conn.execute(
                "INSERT INTO opspilot_alert_deliveries(delivery_key,fingerprint,starts_at,starts_at_raw,status,outcome,incident_id,actor,raw_sha256,annotations_sha256,annotation_revision,alert_json,truncated) VALUES('k','fp',now(),'x','firing','attached',%s,'a',repeat('0',64),repeat('0',64),1,'{}',false)",
                (old,),
            )


@pytest.mark.parametrize(("attached", "related"), [(True, False), (False, True)])
def test_0018_downgrade_refuses_with_correlation(scratch, attached, related):
    schema.migrate(scratch, pg_dump=PG_DUMP)
    _seed(scratch, attached=attached, related=related)
    with pytest.raises(RuntimeError, match="refusing to downgrade"):
        schema.command.downgrade(schema._config(scratch), "0017_web_suspension_grants")
    with psycopg.connect(scratch) as conn:
        assert schema.current_revision(conn) == "0018_alert_correlation"


def test_0018_downgrade_restores_the_0017_schema(scratch):
    schema.migrate(scratch, pg_dump=PG_DUMP)
    head = schema.schema_dump(scratch, pg_dump=PG_DUMP)
    _seed(scratch, attached=False, related=False)
    schema.command.downgrade(schema._config(scratch), "0017_web_suspension_grants")
    assert schema.schema_dump(scratch, pg_dump=PG_DUMP) == schema.fresh_dump(
        scratch, "0017_web_suspension_grants", pg_dump=PG_DUMP
    )
    schema.migrate(scratch, pg_dump=PG_DUMP)
    assert schema.schema_dump(scratch, pg_dump=PG_DUMP) == head
