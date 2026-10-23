"""F2 PR-B1 implementation tests: an attached alert episode raises its
incident's claim priority, never lowers it (contract U-B1-2), through the
real ``/intake/alertmanager`` path running as an ``opspilot_web`` login.
Opt-in like the M1-04 tests (``M1_DURABLE_POSTGRES=1``)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import schema
from opspilot.persistence import DurableStore
from opspilot.persistence.grants import WEB_ROLE
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
from tests.integration.test_f2_c_impl_correlation_postgres import (
    PG_DUMP,
    TARGETS,
    _create_db,
    _drop_db,
    alert,
    episode,
    pytestmark,
    rows,
    send,
)
from tests.m1_web_support import EVENT_TOKEN, ORIGIN, UI_PASSWORD, UI_USER

__all__ = ["alert", "pytestmark"]


@pytest.fixture(scope="module")
def database() -> Iterator[str]:
    name = _create_db("f2_b1_attach")
    dsn = make_conninfo(DSN, dbname=name)
    try:
        schema.migrate(dsn, pg_dump=PG_DUMP)
        yield dsn
    finally:
        _drop_db(name)


@pytest.fixture(scope="module")
def web_dsn(database: str) -> Iterator[str]:
    login = f"f2_b1_web_{uuid4().hex[:12]}"
    with psycopg.connect(database, autocommit=True) as conn:
        conn.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION IN ROLE {}"
            ).format(sql.Identifier(login), sql.Identifier(WEB_ROLE))
        )
    try:
        yield make_conninfo(database, user=login)
    finally:
        with psycopg.connect(database, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(login)))
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(login)))


@pytest.fixture
def app(web_dsn, tmp_path, monkeypatch):
    path = tmp_path / "identities.json"
    path.write_text(json.dumps(TARGETS), encoding="utf-8")
    monkeypatch.setenv("OPSPILOT_TARGET_IDENTITIES", str(path))
    store = DurableStore(web_dsn)
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
        auth_revision="f2-b1-attach-v1",
        allowed_origins=frozenset({ORIGIN}),
    )
    yield create_app(workbench, Authenticator(auth), clock)
    store.close()


def _with_severity(item: dict, severity: str | None) -> dict:
    labels = {k: v for k, v in item["labels"].items() if k != "severity"}
    if severity is not None:
        labels["severity"] = severity
    return {**item, "labels": labels}


def _incident(database: str, incident_id) -> tuple:
    (row,) = rows(
        database,
        "SELECT priority,lifecycle,mode,control_generation,current_run_id,state,related_incident_id FROM opspilot_incidents WHERE incident_id=%s",
        incident_id,
    )
    return tuple(row)


def _run_count(database: str, incident_id) -> int:
    ((count,),) = rows(
        database,
        "SELECT count(*) FROM opspilot_runs WHERE incident_id=%s",
        incident_id,
    )
    return int(count)


def test_a_higher_severity_episode_raises_priority_and_nothing_else(
    app, database, alert
):
    first = send(app, _with_severity(alert, "warning"))
    assert first["outcome"] == "created"
    incident = first["incident_id"]
    before = _incident(database, incident)
    assert before[0] == 1

    raised = send(app, _with_severity(episode(alert, 1), "critical"))
    assert (raised["outcome"], raised["incident_id"]) == ("attached", incident)
    after = _incident(database, incident)
    assert after[0] == 0
    # Only the priority moved: no Run, lifecycle, control or state change.
    assert after[1:] == before[1:]
    assert _run_count(database, incident) == 1

    # A lower severity episode, a repeat and its resolved notification
    # never lower it again.
    lower = send(app, _with_severity(episode(alert, 2), None))
    repeat = send(app, _with_severity(episode(alert, 2), "warning"))
    resolved = send(
        app,
        _with_severity(
            episode(alert, 1, status="resolved", endsAt="2026-10-21T10:05:00Z"),
            "warning",
        ),
    )
    assert [lower["outcome"], repeat["outcome"], resolved["outcome"]] == [
        "attached",
        "attached",
        "resolved_attached",
    ]
    assert _incident(database, incident) == after
    assert _run_count(database, incident) == 1


def test_a_lower_or_equal_severity_episode_leaves_priority(app, database, alert):
    first = send(app, _with_severity(alert, "warning"))
    incident = first["incident_id"]
    before = _incident(database, incident)
    for second, severity in ((1, "warning"), (2, None), (3, "info")):
        result = send(app, _with_severity(episode(alert, second), severity))
        assert (result["outcome"], result["incident_id"]) == ("attached", incident)
    assert _incident(database, incident) == before
    assert before[0] == 1


def test_resolved_first_delivery_of_an_episode_changes_nothing(app, database, alert):
    first = send(app, _with_severity(alert, None))
    incident = first["incident_id"]
    before = _incident(database, incident)
    assert before[0] == 2
    resolved = send(
        app,
        _with_severity(
            episode(alert, 5, status="resolved", endsAt="2026-10-21T10:05:00Z"),
            "critical",
        ),
    )
    assert resolved["outcome"] == "resolved_recorded"
    assert _incident(database, incident) == before
