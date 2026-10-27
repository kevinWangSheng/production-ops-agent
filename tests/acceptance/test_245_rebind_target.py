"""Issue #245 PR-1 independent acceptance tests.

Written from the Agent Brief and public workbench/storage interfaces. These
tests observe HTTP responses, pages and committed rows; they do not assert
internal call order.

Signature assumptions:
* POST /incidents/{id}/control accepts action=rebind_target, to_target,
  optional text, expected_generation and idempotency_key.
* The existing opspilot_controls audit row carries actor and JSON payload
  keys from_target, to_target, channel, idempotency_key and optional text.
* A rebound successor is a fresh InvestigationInput JSON snapshot observed
  through bound_target_id, scope_facts.target_ids and its own
  evidence_context.run_id.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from opspilot import schema
from opspilot.observer.health_profile import PROFILE_DIRECTORY, load_health_profile
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
from tests.m1_web_support import (
    EVENT_TOKEN,
    ORIGIN,
    UI_PASSWORD,
    UI_USER,
    basic,
    bearer,
    call,
    post_form,
    post_json,
)
from tests.runtime_roles_support import catalog_grants

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required: M1_DURABLE_POSTGRES=1",
)

TARGET_A = "checkout-prod"
TARGET_B = "payments-prod"
UNKNOWN_TARGET = "not-in-target-identities"
QUESTION = "Why is checkout erroring?"
ACTOR = UI_USER
UI = {**basic(), "origin": ORIGIN, "accept": "application/json"}

_TARGET_ENTRY = {
    "integration_id": "m0-otel-20260909",
    "cluster_uid": "opspilot-m1",
    "namespace": "otel-demo",
    "workload": "checkout",
    "health_profile_id": "otel-demo-checkout",
    "match": {
        "version": 1,
        "labels": {"namespace": "otel-demo", "service": "checkout"},
    },
}
_OTHER_ENTRY = {
    **_TARGET_ENTRY,
    "namespace": "payments",
    "workload": "payments",
    "match": {
        "version": 1,
        "labels": {"namespace": "payments", "service": "payments"},
    },
}


def _new_database() -> tuple[str, str]:
    name = f"issue245_rebind_{uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    dsn = make_conninfo(DSN, dbname=name)
    schema.migrate(dsn, pg_dump=os.environ.get("OPSPILOT_PG_DUMP", "pg_dump"))
    return name, dsn


def _drop_database(name: str) -> None:
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
def app(database, tmp_path, monkeypatch):
    path = tmp_path / f"issue245-identities-{uuid4().hex}.json"
    path.write_text(
        json.dumps({TARGET_A: _TARGET_ENTRY, TARGET_B: _OTHER_ENTRY}),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPSPILOT_TARGET_IDENTITIES", str(path))
    store = DurableStore(database)
    store.install()
    events = DurableEventLog(store)
    events.install()
    evidence = DurableEvidenceStore(store)
    evidence.install()
    ledger = DurableWebLedger(store)
    ledger.install()
    clock = DurableClock(store)
    profile = select_profile({})
    health_profile = load_health_profile(PROFILE_DIRECTORY / "otel-demo-checkout.json")
    workbench = Workbench(
        incidents=DurableIncidentStore(store),
        events=events,
        evidence=evidence,
        ledger=ledger,
        run_versions=profile.versions(),
        tool_face=profile.face(clock),
        health_profile=health_profile,
        run_seconds=600,
        targets=MappingTargetRegistry.from_file(str(path)),
    )
    auth = AuthConfig(
        ui_users={UI_USER: hash_password(UI_PASSWORD)},
        event_tokens={token_digest(EVENT_TOKEN): "alertmanager"},
        auth_revision="issue-245-pr1-acceptance",
        allowed_origins=frozenset({ORIGIN}),
    )
    try:
        yield create_app(workbench, Authenticator(auth), clock)
    finally:
        store.close()


def _submit(app, *, target: str = TARGET_A, key: str | None = None) -> dict:
    response = post_form(
        app,
        "/intake/ui",
        {
            "target_id": target,
            "question": QUESTION,
            "idempotency_key": key or f"issue245-{uuid4().hex}",
        },
        headers=UI,
    )
    assert response.status == 201, response.text
    return response.json()


def _incident_row(database: str, incident_id: str) -> dict:
    with psycopg.connect(database, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT i.lifecycle,i.state,i.control_generation,i.current_run_id,"
            "t.resource_uid FROM opspilot_incidents i "
            "LEFT JOIN opspilot_targets t ON t.target_id=i.target_id "
            "WHERE i.incident_id=%s",
            (UUID(incident_id),),
        ).fetchone()
    assert row is not None
    return dict(row)


def _run_rows(database: str, incident_id: str) -> list[dict]:
    with psycopg.connect(database, row_factory=dict_row) as conn:
        return list(
            conn.execute(
                "SELECT run_id,state,control_generation,input FROM opspilot_runs "
                "WHERE incident_id=%s ORDER BY run_id",
                (UUID(incident_id),),
            ).fetchall()
        )


def _controls(database: str, incident_id: str) -> list[dict]:
    with psycopg.connect(database, row_factory=dict_row) as conn:
        return list(
            conn.execute(
                "SELECT action,actor,expected_generation,resulting_generation,payload "
                "FROM opspilot_controls WHERE incident_id=%s "
                "ORDER BY resulting_generation",
                (UUID(incident_id),),
            ).fetchall()
        )


_app_dsn: dict[int, str] = {}


@pytest.fixture(autouse=True)
def _remember_dsn(app, database):
    _app_dsn[id(app)] = database
    yield
    _app_dsn.pop(id(app), None)


def _sessions(app, incident_id: str) -> tuple[dict, ...]:
    store = DurableStore(_app_dsn[id(app)])
    try:
        return tuple(store.incident_sessions(UUID(incident_id)))
    finally:
        store.close()


def _control(
    app,
    incident_id: str,
    action: str,
    generation: int,
    *,
    to_target: str | None = None,
    text: str | None = None,
    revision: str | None = None,
    key: str | None = None,
):
    fields = {
        "action": action,
        "expected_generation": str(generation),
        "idempotency_key": key or f"{action}-{uuid4().hex}",
    }
    if to_target is not None:
        fields["to_target"] = to_target
    if text is not None:
        fields["text"] = text
    if revision is not None:
        fields["revision"] = revision
    return post_form(
        app,
        f"/incidents/{incident_id}/control",
        fields,
        headers=UI,
    )


def _rebind(app, database: str, incident_id: str, target: str, *, text=None):
    generation = _incident_row(database, incident_id)["control_generation"]
    return _control(
        app,
        incident_id,
        "rebind_target",
        generation,
        to_target=target,
        text=text,
    )


def test_pr1_open_running_rebind_cancels_revokes_audits_and_updates_page(app, database):
    """PR-1 AC1: open/running rebind is one observable state transition."""
    first = _submit(app)
    incident = first["incident_id"]
    old_run = UUID(first["run_id"])
    registered = _control(
        app,
        incident,
        "register_remediation",
        0,
        revision="handled-revision",
        key=f"remediation-{uuid4().hex}",
    )
    assert registered.status == 200, registered.text
    with psycopg.connect(database) as conn:
        conn.execute(
            "UPDATE opspilot_incidents SET lifecycle='open',state='running' "
            "WHERE incident_id=%s",
            (UUID(incident),),
        )

    before = _incident_row(database, incident)
    response = _rebind(app, database, incident, TARGET_B, text="move ownership")
    assert response.status == 200, response.text
    after = _incident_row(database, incident)
    assert after["lifecycle"] == "open"
    assert after["resource_uid"] == TARGET_B
    assert after["control_generation"] == before["control_generation"] + 1
    runs = _run_rows(database, incident)
    assert next(row for row in runs if row["run_id"] == old_run)["state"] == "cancelled"
    sessions = _sessions(app, incident)
    assert sessions and all(row["state"] == "revoked" for row in sessions)

    audit = _controls(database, incident)[-1]
    assert audit["action"] == "rebind_target"
    assert audit["actor"] == ACTOR
    payload = dict(audit["payload"] or {})
    assert payload["from_target"] == TARGET_A
    assert payload["to_target"] == TARGET_B
    assert payload["channel"] == "web"
    assert payload["idempotency_key"]
    assert payload["text"] == "move ownership"

    page = call(app, "GET", f"/incidents/{incident}", headers=basic())
    assert page.status == 200, page.text
    assert 'id="current-target"' in page.text
    assert TARGET_B in page.text
    assert '<p class="rebind">' in page.text
    assert "Rebound from" in page.text
    assert TARGET_A in page.text
    assert 'name="to_target"' in page.text


def test_pr1_observing_recovery_rebind_returns_open_and_revokes_session(app, database):
    """PR-1 AC2: observing_recovery start returns to open and revokes."""
    first = _submit(app)
    incident = first["incident_id"]
    registered = _control(
        app,
        incident,
        "register_remediation",
        0,
        revision="handled-revision",
        key=f"recovery-{uuid4().hex}",
    )
    assert registered.status == 200, registered.text
    assert _incident_row(database, incident)["lifecycle"] == "observing_recovery"
    response = _rebind(app, database, incident, TARGET_B)
    assert response.status == 200, response.text
    assert _incident_row(database, incident)["lifecycle"] == "open"
    sessions = _sessions(app, incident)
    assert sessions and all(row["state"] == "revoked" for row in sessions)


@pytest.mark.parametrize("action", ["follow_up", "new_run"])
def test_pr1_rebind_successors_use_fresh_new_target_input(app, database, action):
    """PR-1 AC3: follow_up/new_run after rebind use fresh target-bound input."""
    first = _submit(app)
    incident = first["incident_id"]
    old_run = first["run_id"]
    rebound = _rebind(app, database, incident, TARGET_B)
    assert rebound.status == 200, rebound.text
    generation = _incident_row(database, incident)["control_generation"]
    response = _control(
        app,
        incident,
        action,
        generation,
        text="What changed?" if action == "follow_up" else None,
    )
    assert response.status == 200, response.text
    current = _incident_row(database, incident)["current_run_id"]
    assert str(current) != old_run
    rows = _run_rows(database, incident)
    current_row = next(row for row in rows if row["run_id"] == current)
    value = dict(current_row["input"] or {})
    assert value["bound_target_id"] == TARGET_B
    assert value["scope_facts"]["target_ids"] == [TARGET_B]
    assert value["question"] == QUESTION
    assert value["evidence_context"]["run_id"] == str(current)
    assert old_run not in json.dumps(value, sort_keys=True)


@pytest.mark.parametrize("lifecycle", ["resolved", "closed"])
def test_pr1_terminal_rebind_requires_reopen_and_writes_nothing(
    app, database, lifecycle
):
    """PR-1 AC4: resolved/closed rebind returns REOPEN_REQUIRED atomically."""
    first = _submit(app)
    incident = first["incident_id"]
    if lifecycle == "closed":
        closed = _control(app, incident, "close", 0)
        assert closed.status == 200, closed.text
    else:
        with psycopg.connect(database) as conn:
            conn.execute(
                "UPDATE opspilot_incidents SET lifecycle='resolved' "
                "WHERE incident_id=%s",
                (UUID(incident),),
            )
    before = (
        _incident_row(database, incident),
        _run_rows(database, incident),
        _controls(database, incident),
    )
    response = _rebind(app, database, incident, TARGET_B)
    assert response.status == 409
    assert response.json()["code"] == "REOPEN_REQUIRED"
    assert (
        _incident_row(database, incident),
        _run_rows(database, incident),
        _controls(database, incident),
    ) == before


@pytest.mark.parametrize(
    ("target", "status", "code"),
    [(TARGET_A, 409, "TARGET_UNCHANGED"), (UNKNOWN_TARGET, 404, "UNKNOWN_TARGET")],
)
def test_pr1_same_or_unregistered_target_is_rejected_without_writes(
    app, database, target, status, code
):
    """PR-1 AC5: registry membership and target change are both required."""
    first = _submit(app)
    incident = first["incident_id"]
    before = (
        _incident_row(database, incident),
        _run_rows(database, incident),
        _controls(database, incident),
    )
    response = _rebind(app, database, incident, target)
    assert response.status == status
    assert response.json()["code"] == code
    assert (
        _incident_row(database, incident),
        _run_rows(database, incident),
        _controls(database, incident),
    ) == before


def test_pr1_rebound_incident_does_not_absorb_old_target_firing(app, database):
    """PR-1 AC6/U5-1: old-target same-fingerprint firing opens a new incident."""
    evidence = Path(__file__).resolve().parents[2] / "docs/evidence/m1-04-lab"
    alert = json.loads((evidence / "webhook-firing.json").read_text())["alerts"][0]
    alert["fingerprint"] = uuid4().hex[:16]
    alert["startsAt"] = "2026-10-10T10:41:54Z"
    first_response = post_json(
        app,
        "/intake/alertmanager",
        {"version": "4", "status": "firing", "alerts": [alert]},
        headers=bearer(),
    )
    assert first_response.status == 200, first_response.text
    first = first_response.json()["results"][0]
    assert first["outcome"] == "created"
    incident = first["incident_id"]
    rebound = _rebind(app, database, incident, TARGET_B)
    assert rebound.status == 200, rebound.text
    alert["startsAt"] = "2026-10-10T10:41:55Z"
    second_response = post_json(
        app,
        "/intake/alertmanager",
        {"version": "4", "status": "firing", "alerts": [alert]},
        headers=bearer(),
    )
    assert second_response.status == 200, second_response.text
    second = second_response.json()["results"][0]
    assert second["outcome"] == "created"
    assert second["incident_id"] != incident


def test_pr1_rebind_without_target_returns_target_required_without_writes(
    app, database
):
    """PR-1 interface: rebind_target requires the to_target field."""
    first = _submit(app)
    incident = first["incident_id"]
    before = (
        _incident_row(database, incident),
        _run_rows(database, incident),
        _controls(database, incident),
    )
    response = _control(app, incident, "rebind_target", 0)
    assert response.status == 400
    assert response.json()["code"] == "TARGET_REQUIRED"
    assert (
        _incident_row(database, incident),
        _run_rows(database, incident),
        _controls(database, incident),
    ) == before


def test_pr1_other_action_with_to_target_is_rejected_without_writes(app, database):
    """PR-1 interface: to_target is accepted only by rebind_target."""
    first = _submit(app)
    incident = first["incident_id"]
    before = (
        _incident_row(database, incident),
        _run_rows(database, incident),
        _controls(database, incident),
    )
    response = _control(
        app,
        incident,
        "follow_up",
        0,
        to_target=TARGET_B,
        text="follow-up",
    )
    assert response.status == 400
    assert response.json()["code"] == "TARGET_NOT_ALLOWED"
    assert (
        _incident_row(database, incident),
        _run_rows(database, incident),
        _controls(database, incident),
    ) == before


def test_pr1_unrebound_incident_keeps_alert_attach_close_reopen_and_new_run_behavior(
    app, database
):
    """PR-1 AC7: an untouched incident keeps existing F2/F12 behavior."""
    evidence = Path(__file__).resolve().parents[2] / "docs/evidence/m1-04-lab"
    alert = json.loads((evidence / "webhook-firing.json").read_text())["alerts"][0]
    alert["fingerprint"] = uuid4().hex[:16]
    alert["startsAt"] = "2026-10-10T10:41:54Z"

    def fire() -> dict:
        response = post_json(
            app,
            "/intake/alertmanager",
            {"version": "4", "status": "firing", "alerts": [deepcopy(alert)]},
            headers=bearer(),
        )
        assert response.status == 200, response.text
        return response.json()["results"][0]

    first = fire()
    assert first["outcome"] == "created"
    incident = first["incident_id"]
    alert["startsAt"] = "2026-10-10T10:41:55Z"
    attached = fire()
    assert attached["outcome"] == "attached"
    assert attached["incident_id"] == incident

    closed = _control(app, incident, "close", 0)
    assert closed.status == 200, closed.text
    reopened = _control(app, incident, "reopen", 1)
    assert reopened.status == 200, reopened.text
    assert _incident_row(database, incident)["lifecycle"] == "open"

    new_run = _control(app, incident, "new_run", 2)
    assert new_run.status == 200, new_run.text
    current = _incident_row(database, incident)["current_run_id"]
    current_row = next(
        row for row in _run_rows(database, incident) if row["run_id"] == current
    )
    assert dict(current_row["input"])["bound_target_id"] == TARGET_A


EXPECTED_WEB_INCIDENT_GRANTS = frozenset(
    {
        "SELECT",
        "INSERT(current_run_id,incident_id,intake_key,lifecycle,priority,related_incident_id,state,target_id)",
        "UPDATE(conclusion,control_generation,current_run_id,lifecycle,merged_into_incident_id,mode,observation_generation,priority,state,target_id)",
    }
)
EXPECTED_WORKER_INCIDENT_GRANTS = frozenset({"SELECT", "UPDATE(conclusion)"})
EXPECTED_OBSERVER_INCIDENT_GRANTS = frozenset(
    {
        "SELECT(control_generation,incident_id,lifecycle,observation_generation,target_id)",
        "UPDATE(lifecycle)",
    }
)


def test_pr1_runtime_grants_add_only_web_incident_update_columns(app, database):
    """PR-1 AC8: web gains exactly the two incident UPDATE columns."""
    with psycopg.connect(database) as conn:
        grants = catalog_grants(conn, WEB_ROLE)
        assert grants["opspilot_incidents"] == EXPECTED_WEB_INCIDENT_GRANTS
        assert (
            catalog_grants(conn, WORKER_ROLE)["opspilot_incidents"]
            == EXPECTED_WORKER_INCIDENT_GRANTS
        )
        assert (
            catalog_grants(conn, "opspilot_observer")["opspilot_incidents"]
            == EXPECTED_OBSERVER_INCIDENT_GRANTS
        )


def test_pr1_head_is_0022_and_clean_downgrade_upgrade_round_trips(isolated_database):
    """PR-1 AC8: head is 0022 and a clean downgrade/upgrade is reversible."""
    pg_dump = os.environ["OPSPILOT_PG_DUMP"]
    with psycopg.connect(isolated_database) as conn:
        assert schema.current_revision(conn) == "0022_incident_association"
    before = schema.schema_dump(isolated_database, pg_dump=pg_dump)
    command.downgrade(schema._config(isolated_database), "0021_release_incident_links")
    with psycopg.connect(isolated_database) as conn:
        assert schema.current_revision(conn) == "0021_release_incident_links"
    schema.upgrade_head(isolated_database)
    with psycopg.connect(isolated_database) as conn:
        assert schema.current_revision(conn) == "0022_incident_association"
    assert schema.schema_dump(isolated_database, pg_dump=pg_dump) == before


def test_pr1_downgrade_refuses_nonnull_merge_pointer(isolated_database):
    """PR-1 AC8: downgrade refuses data the pre-0022 schema cannot represent."""
    first_id = uuid4()
    second_id = uuid4()
    with psycopg.connect(isolated_database) as conn:
        assert schema.current_revision(conn) == "0022_incident_association"
        conn.execute(
            "INSERT INTO opspilot_incidents(incident_id,intake_key,state,lifecycle,"
            "control_generation,observation_generation) "
            "VALUES(%s,%s,'waiting_human','open',0,0),"
            "(%s,%s,'waiting_human','open',0,0)",
            (first_id, f"fixture:{first_id}", second_id, f"fixture:{second_id}"),
        )
        conn.execute(
            "UPDATE opspilot_incidents SET merged_into_incident_id=%s "
            "WHERE incident_id=%s",
            (second_id, first_id),
        )
    with pytest.raises(Exception):
        command.downgrade(
            schema._config(isolated_database), "0021_release_incident_links"
        )
    with psycopg.connect(isolated_database) as conn:
        assert schema.current_revision(conn) == "0022_incident_association"
