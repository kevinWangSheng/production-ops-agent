"""Implementer tests for ``POST /intake/alertmanager`` on PostgreSQL (M1-04 step 2).

The independent acceptance tests live elsewhere; these cover what the
implementation itself must hold: one transaction per alert, identity dedup
under concurrency, handoff-only incidents, resolved attachment, annotation
revisions, the redacted bounded record and the projection the page uses.
"""

from __future__ import annotations

import os
import pathlib
from uuid import uuid4

import psycopg
import pytest

from opspilot.acceptance import (
    IncidentScenario,
    alert_intake_outcome,
    alert_intake_records,
)
from opspilot.persistence import DurableStore
from opspilot.tools.fixture import fixture_face, fixture_versions
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
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

EVIDENCE = pathlib.Path(__file__).resolve().parents[2] / "docs/evidence/m1-04-lab"
IDENTITY = {
    "integration_id": "m0-otel-20260909",
    "cluster_uid": "opspilot-m1",
    "namespace": "otel-demo",
    "workload": "checkout",
    "health_profile_id": "otel-demo-checkout",
}
REGISTRY = {
    # The fixture tool face is bound to ``checkout-prod``.
    "checkout-prod": {
        **IDENTITY,
        "match": {
            "version": 1,
            "labels": {"namespace": "otel-demo", "service": "checkout"},
        },
    },
    "checkout-canary": {
        **IDENTITY,
        "workload": "checkout-canary",
        "match": {
            "version": 1,
            "labels": {"namespace": "otel-demo", "track": "canary"},
        },
    },
    # No match: never bound by an alert.
    "payment-prod": {**IDENTITY, "workload": "payment"},
}
ACTOR = "alertmanager"


def _build(*, face: bool = True):
    store = DurableStore(DSN)
    store.install()
    events = DurableEventLog(store)
    evidence = DurableEvidenceStore(store)
    ledger = DurableWebLedger(store)
    workbench = Workbench(
        incidents=DurableIncidentStore(store),
        events=events,
        evidence=evidence,
        ledger=ledger,
        run_versions=fixture_versions(),
        run_seconds=600,
        tool_face=fixture_face() if face else None,
        targets=MappingTargetRegistry(REGISTRY),
    )
    config = AuthConfig(
        ui_users={UI_USER: hash_password(UI_PASSWORD)},
        event_tokens={token_digest(EVENT_TOKEN): ACTOR},
        auth_revision="auth-rev-pg",
        allowed_origins=frozenset({ORIGIN}),
    )
    app = create_app(workbench, Authenticator(config), DurableClock(store))
    return app, workbench, store


def _alert(fingerprint=None, *, status="firing", labels=None, annotations=None, **kw):
    alert = {
        "status": status,
        "labels": labels
        or {
            "alertname": "CheckoutPlaceOrderErrorRatioHigh",
            "namespace": "otel-demo",
            "service": "checkout",
            "severity": "critical",
        },
        "annotations": annotations
        if annotations is not None
        else {"summary": "checkout PlaceOrder error ratio above 50%"},
        "startsAt": "2026-10-10T10:41:54.365Z",
        "endsAt": "0001-01-01T00:00:00Z",
        "fingerprint": fingerprint or uuid4().hex[:16],
        "generatorURL": "http://prometheus:9090/graph?g0.expr=rate%28x%5B5m%5D%29+%3E+0.5&g0.tab=1",
    }
    alert.update(kw)
    return alert


def _webhook(*alerts, status="firing"):
    return {
        "receiver": "opspilot",
        "status": status,
        "alerts": list(alerts),
        "groupLabels": {},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "http://alertmanager:9093",
        "version": "4",
        "groupKey": "{}:{}",
        "truncatedAlerts": 0,
    }


def _post(app, payload, headers=None):
    return post_json(
        app,
        "/intake/alertmanager",
        payload,
        headers=bearer() if headers is None else headers,
    )


def _outcome(store, incident_id):
    with psycopg.connect(DSN) as conn:
        records = alert_intake_records(conn, incident_id)
    scenario = IncidentScenario(
        scenario_id="impl",
        feature_id="F1",
        acceptance_step="2",
        kind="alert_intake",
        subject_id=str(incident_id),
    )
    return alert_intake_outcome(scenario, records)


def _count(sql, *args):
    with psycopg.connect(DSN) as conn:
        return conn.execute(sql, args).fetchone()[0]
