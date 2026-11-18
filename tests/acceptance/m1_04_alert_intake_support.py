"""M1-04 step 2: HTTP stimuli -> committed I8 outcomes, opt-in real PostgreSQL.

Independent contract tests based on r1 A-D, r2 E/R, r3 I1-I10, r4 I8/I11/I12, C3 sections 6/9,
PRODUCT-CONSTRAINTS and F1 step 2 / F7 step 5; no alert implementation read.

Public-contract gaps remaining after r4 (do not substitute private SQL):
* r4 specifies the submitted run_input fields but does not define the exact
  serialized key names for timeframe and budget; assertions use public
  snapshot fields and compare timestamp offsets from each Run's receipt.
* r4 bounds audit_json but does not define its redaction marker or whitespace;
  tests assert parseable bounded JSON, secret absence, and raw_sha256 formula.
* I12 defines a PostgreSQL trigger seam but not a helper API; tests create only
  a temporary trigger/function on the named public table and remove it.
* No contract defines an observation-session creation path for alert intake;
  tests assert the exposed tuple is unchanged.
* I2 leaves the exact failed-item reason code open; tests require non-empty.


These tests do not claim full F1/F7 acceptance or flip passes. Fresh test-owned
DB is migrated like existing F6 fixtures, no model/worker/telemetry is started.
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
from opspilot.acceptance import IncidentScenario
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

EVIDENCE = Path(__file__).resolve().parents[2] / "docs/evidence/m1-04-lab"
TARGET = "checkout-prod"
NORMAL_START = "2026-10-10T10:41:54Z"
RESULT_FIELDS = {
    "fingerprint",
    "starts_at",
    "status",
    "outcome",
    "incident_id",
    "run_id",
    "delivery_key",
    "reason",
}
ENTRY = {
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


@pytest.fixture(scope="module")
def database():
    name = f"m1_04_alert_acceptance_{uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    dsn = make_conninfo(DSN, dbname=name)
    try:
        schema.migrate(dsn, pg_dump=os.environ.get("OPSPILOT_PG_DUMP", "pg_dump"))
        yield dsn
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.fixture
def harness(database, tmp_path, monkeypatch):
    stores = []

    def build(entries=None):
        path = tmp_path / f"identities-{uuid4().hex}.json"
        path.write_text(
            json.dumps(entries if entries is not None else {TARGET: ENTRY}),
            encoding="utf-8",
        )
        monkeypatch.setenv("OPSPILOT_TARGET_IDENTITIES", str(path))
        store = DurableStore(database)
        stores.append(store)
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
            auth_revision="m1-04-acceptance-v1",
            allowed_origins=frozenset({ORIGIN}),
        )
        return create_app(workbench, Authenticator(auth), clock)

    yield build
    for store in stores:
        store.close()


@pytest.fixture
def alert():
    result = json.loads((EVIDENCE / "webhook-firing.json").read_text())["alerts"][0]
    # Keep real wire fields but isolate identities across tests and shared DB.
    result["fingerprint"] = uuid4().hex[:16]
    return result


def send(app, *alerts, group_status="firing"):
    # Wrapper evidence includes capture metadata; send only the v4 wire fields.
    payload = {"version": "4", "status": group_status, "alerts": list(alerts)}
    return post_json(app, "/intake/alertmanager", payload, headers=bearer())


def results(response, *, status=200):
    assert response.status == status, response.text
    data = response.json()
    assert set(data) == {"results"}
    assert isinstance(data["results"], list)
    for item in data["results"]:
        assert RESULT_FIELDS <= item.keys()
    return data["results"]


def project(database, incident_id, *, kind="alert-intake"):
    # Deferred import: main lacks the new seam. Once opted in, missing exports
    # are a failure, never importorskip / a guessed replacement implementation.
    from opspilot.acceptance import alert_intake_outcome, alert_intake_records

    scenario = IncidentScenario(
        scenario_id=f"M1-04:{kind}:{incident_id}",
        feature_id="F1",
        acceptance_step="2",
        kind=kind,
        subject_id=incident_id,
    )
    with psycopg.connect(database) as conn:
        records = alert_intake_records(conn, incident_id)
    outcome = alert_intake_outcome(scenario, records)
    assert outcome.scenario_id == scenario.scenario_id
    assert str(outcome.incident_id) == incident_id
    return outcome


def same_subject(first, second):
    assert second["incident_id"] == first["incident_id"]
    assert second["run_id"] == first["run_id"]
    assert second["delivery_key"] == first["delivery_key"]


def _install_failure_trigger(database, fingerprint):
    """Install the r4 I12 test seam and return cleanup."""
    suffix = uuid4().hex
    function, trigger = f"m1_04_fail_{suffix}", f"m1_04_trigger_{suffix}"
    with psycopg.connect(database, autocommit=True) as conn:
        conn.execute(
            sql.SQL("""
            CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF NEW.fingerprint = {} THEN
                RAISE EXCEPTION 'M1_04_INJECTED_FAILURE';
              END IF;
              RETURN NEW;
            END $$
        """).format(sql.Identifier(function), sql.Literal(fingerprint))
        )
        conn.execute(
            sql.SQL("""
            CREATE TRIGGER {} BEFORE INSERT ON opspilot_alert_deliveries
            FOR EACH ROW EXECUTE FUNCTION {}()
        """).format(sql.Identifier(trigger), sql.Identifier(function))
        )

    def cleanup():
        with psycopg.connect(database, autocommit=True) as conn:
            conn.execute(
                sql.SQL(
                    "DROP TRIGGER IF EXISTS {} ON opspilot_alert_deliveries"
                ).format(sql.Identifier(trigger))
            )
            conn.execute(
                sql.SQL("DROP FUNCTION IF EXISTS {}()").format(sql.Identifier(function))
            )

    return cleanup
