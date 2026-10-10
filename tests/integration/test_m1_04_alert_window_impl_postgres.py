"""Implementer tests for M1-04 step 3 on PostgreSQL: the alert Run's frame.

Contract r5 J1-J6: an alert Run is framed at the database instant the alert
was received, anchored at its ``startsAt`` (clamped into the frame), and a
renewal or fresh rebuild keeps that frame. The independent acceptance tests
live elsewhere.
"""

from __future__ import annotations

import os
from datetime import timedelta, timezone
from uuid import UUID, uuid4

import psycopg
import pytest

from opspilot.acceptance import (
    IncidentScenario,
    alert_intake_outcome,
    alert_intake_records,
)
from opspilot.investigation.context import InvestigationInput
from opspilot.persistence import DurableStore
from opspilot.tools.otel_demo import otel_demo_face, otel_demo_versions
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
    post_form,
    post_json,
    same_origin,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

REGISTRY = {
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
    },
}
LABELS = {
    "alertname": "CheckoutPlaceOrderErrorRatioHigh",
    "namespace": "otel-demo",
    "service": "checkout",
    "severity": "critical",
}


def _build():
    store = DurableStore(DSN)
    store.install()
    clock = DurableClock(store)
    workbench = Workbench(
        incidents=DurableIncidentStore(store),
        events=DurableEventLog(store),
        evidence=DurableEvidenceStore(store),
        ledger=DurableWebLedger(store),
        run_versions=otel_demo_versions(),
        run_seconds=600,
        tool_face=otel_demo_face(clock),
        targets=MappingTargetRegistry(REGISTRY),
    )
    config = AuthConfig(
        ui_users={UI_USER: hash_password(UI_PASSWORD)},
        event_tokens={token_digest(EVENT_TOKEN): "alertmanager"},
        auth_revision="auth-rev-window",
        allowed_origins=frozenset({ORIGIN}),
    )
    return create_app(workbench, Authenticator(config), clock), workbench, store


def _post_alert(app, starts_at, fingerprint=None):
    payload = {
        "receiver": "opspilot",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": LABELS,
                "annotations": {"summary": "checkout errors"},
                "startsAt": starts_at,
                "endsAt": "0001-01-01T00:00:00Z",
                "fingerprint": fingerprint or uuid4().hex[:16],
                "generatorURL": "http://prometheus:9090/graph?g0.expr=up",
            }
        ],
        "groupLabels": {},
        "commonLabels": {},
        "commonAnnotations": {},
        "externalURL": "http://alertmanager:9093",
        "version": "4",
        "groupKey": "{}:{}",
        "truncatedAlerts": 0,
    }
    response = post_json(app, "/intake/alertmanager", payload, headers=bearer())
    assert response.status == 200
    return response.json()["results"][0]


def _records(incident_id):
    with psycopg.connect(DSN) as conn:
        return alert_intake_records(conn, incident_id)


def _frame_end(records):
    received = records.deliveries[0]["received_at"]
    return received.astimezone(timezone.utc).replace(microsecond=0)


def _policy(raw):
    return raw["evidence_context"]["time_policies"][0]


def _iso(moment):
    return moment.isoformat()


def test_alert_run_is_framed_at_receipt_and_anchored_at_starts_at():
    app, workbench, store = _build()
    with store.transaction() as conn:
        now = conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
    starts = (now - timedelta(minutes=17)).astimezone(timezone.utc)
    original = starts.strftime("%Y-%m-%dT%H:%M:%S.123456789Z")
    result = _post_alert(app, original)
    assert result["outcome"] == "created"
    records = _records(result["incident_id"])
    raw = records.run["input"]
    end = _frame_end(records)
    # J1: the 24 h ending at the delivery's database receipt, whole seconds;
    # the receipt is the intake's database clock, not the webhook's.
    assert abs((end - now).total_seconds()) < 60
    anchor = starts.replace(microsecond=0)
    assert raw["version"] == "opspilot-investigation-input-v3"
    assert raw["scope_facts"]["alert_starts_at"] == {
        "anchor": _iso(anchor),
        "original": original,
        "adjusted": None,
    }
    policy = _policy(raw)
    assert policy["window"] == {
        "start": _iso(end - timedelta(hours=24)),
        "end": _iso(end),
    }
    assert policy["anchor"] == _iso(anchor)
    assert policy["anchor_rule"] == "alert_starts_at"
    assert policy["default_query_window"] == {
        "start": _iso(anchor - timedelta(hours=1)),
        "end": _iso(end),
    }
    # Authorization and focus as in step 2.
    assert raw["scope_facts"]["target_ids"] == ["checkout-prod"]
    assert raw["scope_facts"]["affected_service"] == {
        "namespace": "otel-demo",
        "workload": "checkout",
    }
    InvestigationInput.from_json(raw)
    # J6: the acceptance projection shows the committed input verbatim.
    scenario = IncidentScenario(
        scenario_id="window",
        feature_id="F1",
        acceptance_step="2",
        kind="alert_intake",
        subject_id=result["incident_id"],
    )
    assert alert_intake_outcome(scenario, records).run_input == raw


@pytest.mark.parametrize(
    ("starts_at", "adjusted"),
    [("2099-01-01T00:00:00Z", "future"), ("2020-01-01T00:00:00Z", "before_frame")],
)
def test_out_of_frame_starts_are_clamped_not_refused(starts_at, adjusted):
    app, _, _ = _build()
    result = _post_alert(app, starts_at)
    assert result["outcome"] == "created"
    records = _records(result["incident_id"])
    raw = records.run["input"]
    end = _frame_end(records)
    edge = end if adjusted == "future" else end - timedelta(hours=24)
    assert raw["scope_facts"]["alert_starts_at"] == {
        "anchor": _iso(edge),
        "original": starts_at,
        "adjusted": adjusted,
    }
    assert _policy(raw)["anchor"] == _iso(edge)
    assert _policy(raw)["default_query_window"]["start"] == _iso(
        max(edge - timedelta(hours=1), end - timedelta(hours=24))
    )


def test_submitted_runs_keep_their_frame_and_version():
    app, workbench, store = _build()
    response = post_form(
        app,
        "/intake/ui",
        {
            "target_id": "checkout-prod",
            "question": "Why is checkout erroring?",
            "idempotency_key": f"window-{uuid4()}",
        },
        headers={**basic(), **same_origin()},
    )
    assert response.status == 201
    raw = store.rebuild(UUID(response.json()["incident_id"]))["run"]["input"]
    assert raw["version"] == "opspilot-investigation-input-v1"
    assert "alert_starts_at" not in raw["scope_facts"]
    policy = _policy(raw)
    assert policy["reference_rule"] == "response_received_at"
    assert "anchor" not in policy and "default_query_window" not in policy


def _successor(workbench, incident_id):
    summary = workbench.incidents.find_incident(UUID(incident_id))
    deadline = workbench.incidents.now() + timedelta(hours=2)
    return workbench._successor_input(summary, uuid4(), deadline), deadline


def test_renewal_keeps_the_alert_frame_and_anchor():
    app, workbench, _ = _build()
    result = _post_alert(app, "2026-10-10T10:41:54.365Z")
    previous = _records(result["incident_id"]).run["input"]
    successor, deadline = _successor(workbench, result["incident_id"])
    assert successor["version"] == "opspilot-investigation-input-v3"
    assert (
        successor["scope_facts"]["alert_starts_at"]
        == (previous["scope_facts"]["alert_starts_at"])
    )
    assert successor["scope_facts"]["deadline"] == deadline.isoformat()
    assert (
        successor["evidence_context"]["time_policies"]
        == previous["evidence_context"]["time_policies"]
    )


def test_fresh_fallback_rebuilds_from_the_earliest_delivery():
    app, workbench, _ = _build()
    fingerprint = uuid4().hex[:16]
    starts = "2026-10-10T10:41:54.365Z"
    result = _post_alert(app, starts, fingerprint)
    records = _records(result["incident_id"])
    previous = records.run["input"]
    # A later replay must not move the frame.
    replay = _post_alert(app, starts, fingerprint)
    assert replay["outcome"] == "replayed"
    # A Run with no snapshot is the one case a fresh input is built (J5).
    with psycopg.connect(DSN) as conn:
        conn.execute(
            "UPDATE opspilot_runs SET input=NULL WHERE run_id=%s",
            (UUID(result["run_id"]),),
        )
    successor, _ = _successor(workbench, result["incident_id"])
    assert successor["version"] == "opspilot-investigation-input-v3"
    assert (
        successor["scope_facts"]["alert_starts_at"]
        == (previous["scope_facts"]["alert_starts_at"])
    )
    assert (
        successor["scope_facts"]["affected_service"]
        == (previous["scope_facts"]["affected_service"])
    )
    fresh_policy = _policy(successor)
    old_policy = _policy(previous)
    for key in ("window", "anchor", "anchor_rule", "default_query_window"):
        assert fresh_policy[key] == old_policy[key], key
    assert successor["question"] == previous["question"]


def _outcome(incident_id):
    scenario = IncidentScenario(
        scenario_id="window",
        feature_id="F1",
        acceptance_step="2",
        kind="alert_intake",
        subject_id=incident_id,
    )
    return alert_intake_outcome(scenario, _records(incident_id))


def test_run_inputs_list_every_runs_committed_input_including_a_renewal():
    """r7: ``run_inputs`` follows ``run_ids``; a timed-out Run's note renews
    it and the successor's input keeps the alert frame and anchor (J5)."""
    app, workbench, _ = _build()
    result = _post_alert(app, "2026-10-10T10:41:54.365Z")
    first = _outcome(result["incident_id"])
    assert first.run_ids == (result["run_id"],)
    assert first.run_inputs == (first.run_input,)
    with psycopg.connect(DSN) as conn:
        conn.execute(
            "UPDATE opspilot_runs SET deadline = clock_timestamp() - interval '1 second' WHERE run_id=%s",
            (UUID(result["run_id"]),),
        )
    generation = workbench.incidents.find_incident(
        UUID(result["incident_id"])
    ).control_generation
    renewed = post_form(
        app,
        f"/incidents/{result['incident_id']}/control",
        {
            "action": "follow_up",
            "text": "still failing?",
            "expected_generation": str(generation),
            "idempotency_key": f"renew-{uuid4()}",
        },
        headers={**basic(), **same_origin()},
    )
    assert renewed.status in (200, 201), renewed.text
    outcome = _outcome(result["incident_id"])
    assert len(outcome.run_ids) == 2 and outcome.run_ids[0] == result["run_id"]
    previous, successor = outcome.run_inputs
    # ``run_input`` stays the intake Run's.
    assert previous == outcome.run_input == first.run_input
    assert successor is not None and successor["version"] == (
        "opspilot-investigation-input-v3"
    )
    assert successor["evidence_context"]["run_id"] == outcome.run_ids[1]
    assert (
        successor["scope_facts"]["alert_starts_at"]
        == previous["scope_facts"]["alert_starts_at"]
    )
    assert (
        successor["evidence_context"]["time_policies"]
        == previous["evidence_context"]["time_policies"]
    )
    # A Run recorded without an input reads as ``None`` in its place.
    with psycopg.connect(DSN) as conn:
        conn.execute(
            "UPDATE opspilot_runs SET input=NULL WHERE run_id=%s",
            (UUID(outcome.run_ids[0]),),
        )
    assert _outcome(result["incident_id"]).run_inputs == (None, successor)
