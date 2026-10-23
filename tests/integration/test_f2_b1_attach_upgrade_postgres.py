"""Independent acceptance tests for F2 PR-B1, U-B1-2: priority of an incident
that later alerts attach to (PR-C ``attached`` / ``resolved_attached``).

Contract: the incident priority is set when the incident is created (0
critical, 1 warning, 2 other) and an attached alert with a higher severity
raises it -- never lowers it, and a resolved notification never changes it.
Everything is driven through ``/intake/alertmanager`` and observed through the
delivery outcomes, the Run rows, the human-state columns and
``opspilot_incidents.priority``. Opt-in like the other PG tests
(``M1_DURABLE_POSTGRES=1``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import schema
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
    bearer,
    post_json,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required: M1_DURABLE_POSTGRES=1",
)

PG_DUMP = os.environ.get("OPSPILOT_PG_DUMP", "pg_dump")
EVIDENCE = Path(__file__).resolve().parents[2] / "docs/evidence/m1-04-lab"
TARGETS = {
    "checkout-prod": {
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
}


@pytest.fixture(scope="module")
def database():
    name = f"f2_b1_attach_{uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    dsn = make_conninfo(DSN, dbname=name)
    try:
        schema.migrate(dsn, pg_dump=PG_DUMP)
        yield dsn
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


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
        auth_revision="f2-b1-attach-v1",
        allowed_origins=frozenset({ORIGIN}),
    )
    yield create_app(workbench, Authenticator(auth), DurableClock(store))
    store.close()


def base_alert(severity: str | None) -> dict:
    alert = json.loads((EVIDENCE / "webhook-firing.json").read_text())["alerts"][0]
    alert["fingerprint"] = uuid4().hex[:16]
    alert["startsAt"] = "2026-10-21T10:00:00Z"
    labels = {k: v for k, v in alert["labels"].items() if k != "severity"}
    if severity is not None:
        labels["severity"] = severity
    alert["labels"] = labels
    return alert


def episode(alert: dict, second: int, severity: str | None, **changes) -> dict:
    labels = {k: v for k, v in alert["labels"].items() if k != "severity"}
    if severity is not None:
        labels["severity"] = severity
    return {
        **alert,
        "labels": labels,
        "startsAt": f"2026-10-21T10:00:{second:02d}Z",
        **changes,
    }


def send(app, item: dict) -> dict:
    response = post_json(
        app,
        "/intake/alertmanager",
        {"version": "4", "status": item["status"], "alerts": [item]},
        headers=bearer(),
    )
    assert response.status == 200, response.text
    (result,) = response.json()["results"]
    return result


def priority(database, incident_id) -> int:
    with psycopg.connect(database) as conn:
        (value,) = conn.execute(
            "SELECT priority FROM opspilot_incidents WHERE incident_id=%s",
            (incident_id,),
        ).fetchone()
    return value


def human_state(database, incident_id):
    with psycopg.connect(database) as conn:
        return conn.execute(
            "SELECT lifecycle,mode,control_generation,current_run_id,state,"
            "related_incident_id FROM opspilot_incidents WHERE incident_id=%s",
            (incident_id,),
        ).fetchone()


def run_ids(database, incident_id) -> list:
    with psycopg.connect(database) as conn:
        return [
            row[0]
            for row in conn.execute(
                "SELECT run_id FROM opspilot_runs WHERE incident_id=%s ORDER BY 1",
                (incident_id,),
            )
        ]


def opened(app, database, severity: str | None, expected: int):
    alert = base_alert(severity)
    first = send(app, alert)
    assert first["outcome"] == "created" and first["run_id"], first
    assert priority(database, first["incident_id"]) == expected
    return alert, first


@pytest.mark.parametrize(
    ("opened_as", "opening", "attached_as", "after"),
    [
        ("warning", 1, "critical", 0),
        ("none", 2, "warning", 1),
        ("none", 2, "critical", 0),
        (None, 2, "warning", 1),
        ("critical", 0, "warning", 0),
        ("critical", 0, "critical", 0),
        ("warning", 1, "warning", 1),
        ("warning", 1, "none", 1),
        ("none", 2, "none", 2),
    ],
)
def test_attached_alert_moves_priority_up_only(
    app, database, opened_as, opening, attached_as, after
):
    alert, first = opened(app, database, opened_as, opening)
    before = human_state(database, first["incident_id"])
    runs = run_ids(database, first["incident_id"])

    second = send(app, episode(alert, 1, attached_as))
    assert second["outcome"] == "attached", second
    assert second["incident_id"] == first["incident_id"]
    assert second["run_id"] is None

    assert priority(database, first["incident_id"]) == after
    # Attaching opens no Run and touches no human-controlled state.
    assert run_ids(database, first["incident_id"]) == runs
    assert human_state(database, first["incident_id"]) == before


def test_repeated_and_later_attachments_never_lower_the_priority(app, database):
    alert, first = opened(app, database, "warning", 1)
    send(app, episode(alert, 1, "critical"))
    assert priority(database, first["incident_id"]) == 0
    # A repeat of the critical episode, then a lower-severity episode.
    assert send(app, episode(alert, 1, "critical"))["outcome"] == "attached"
    assert send(app, episode(alert, 2, "warning"))["outcome"] == "attached"
    assert send(app, episode(alert, 3, None))["outcome"] == "attached"
    assert priority(database, first["incident_id"]) == 0


@pytest.mark.parametrize(
    ("opened_as", "opening", "resolved_as"),
    [
        ("warning", 1, "critical"),
        ("none", 2, "critical"),
        ("critical", 0, "warning"),
    ],
)
def test_resolved_attached_does_not_change_priority(
    app, database, opened_as, opening, resolved_as
):
    alert, first = opened(app, database, opened_as, opening)
    before = human_state(database, first["incident_id"])
    # Attach an episode with the same severity as the opening one, then
    # resolve it carrying a (possibly higher) severity label.
    assert send(app, episode(alert, 1, opened_as))["outcome"] == "attached"
    assert priority(database, first["incident_id"]) == opening
    resolved = send(
        app,
        episode(
            alert,
            1,
            resolved_as,
            status="resolved",
            endsAt="2026-10-21T10:05:00Z",
        ),
    )
    assert resolved["outcome"] == "resolved_attached", resolved
    assert resolved["incident_id"] == first["incident_id"]
    assert priority(database, first["incident_id"]) == opening
    assert human_state(database, first["incident_id"]) == before
    assert len(run_ids(database, first["incident_id"])) == 1


@pytest.mark.parametrize("lifecycle", ["observing_recovery", "resolved"])
def test_upgrade_applies_to_non_closed_incidents_without_state_change(
    app, database, lifecycle
):
    alert, first = opened(app, database, "warning", 1)
    with psycopg.connect(database) as conn:
        conn.execute(
            "UPDATE opspilot_incidents SET lifecycle=%s WHERE incident_id=%s",
            (lifecycle, first["incident_id"]),
        )
    before = human_state(database, first["incident_id"])
    second = send(app, episode(alert, 2, "critical"))
    assert second["outcome"] == "attached"
    assert priority(database, first["incident_id"]) == 0
    assert human_state(database, first["incident_id"]) == before
