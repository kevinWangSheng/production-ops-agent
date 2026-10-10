"""Implementer tests for ``POST /intake/alertmanager`` on PostgreSQL (M1-04 step 2).

The independent acceptance tests live elsewhere; these cover what the
implementation itself must hold: one transaction per alert, identity dedup
under concurrency, handoff-only incidents, resolved attachment, annotation
revisions, the redacted bounded record and the projection the page uses.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import threading
from uuid import UUID, uuid4, uuid5

import psycopg
import pytest

from opspilot.acceptance import (
    IncidentScenario,
    alert_deliveries_for_identity,
    alert_delivery_count,
    alert_intake_outcome,
    alert_intake_records,
)
from opspilot.alertmanager import parse_alert
from opspilot.investigation.context import InvestigationInput
from opspilot.persistence import DurableStore
from opspilot.tools.fixture import fixture_face, fixture_versions
from opspilot.tools.registry import canonical, canonical_hash
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
from opspilot.web.service import _INTAKE_NAMESPACE
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
    same_origin,
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


def test_first_firing_opens_incident_and_run_replays_keep_it():
    app, workbench, store = _build()
    alert = _alert()
    first = _post(app, _webhook(alert))
    assert first.status == 200
    [result] = first.json()["results"]
    assert result["outcome"] == "created" and result["reason"] is None
    assert result["status"] == "firing"
    assert result["starts_at"] == "2026-10-10T10:41:54Z"
    assert result["fingerprint"] == alert["fingerprint"]
    incident, run = result["incident_id"], result["run_id"]
    assert incident and run and result["delivery_key"].startswith("evt:")

    changed = dict(alert, annotations={"summary": "now 61%"})
    again = _post(app, _webhook(changed)).json()["results"][0]
    assert again["outcome"] == "replayed"
    assert (again["incident_id"], again["run_id"]) == (incident, run)
    # Same annotations as the latest revision: no new revision.
    third = _post(app, _webhook(changed)).json()["results"][0]
    assert third["outcome"] == "replayed"

    outcome = _outcome(store, incident)
    assert outcome.target_id == "checkout-prod" and outcome.run_id == run
    assert not outcome.handoff and outcome.handoff_reason is None
    assert outcome.affected_service == ("otel-demo", "checkout")
    assert [d.outcome for d in outcome.deliveries] == [
        "created",
        "replayed",
        "replayed",
    ]
    assert [d.annotation_revision for d in outcome.deliveries] == [1, 2, 2]
    assert {d.actor for d in outcome.deliveries} == {ACTOR}
    assert outcome.deliveries[0].raw_sha256 == canonical_hash(alert)
    # r4 fields.
    assert (
        outcome.deliveries[0].raw_sha256
        == hashlib.sha256(canonical(alert).encode("utf-8")).hexdigest()
    )
    assert outcome.lifecycle == "open"
    assert outcome.run_ids == (run,)
    assert outcome.observation_session_ids == ()
    assert outcome.run_input is not None
    assert outcome.run_input["scope_facts"]["affected_service"] == {
        "namespace": "otel-demo",
        "workload": "checkout",
    }
    assert outcome.run_input["bound_target_id"] == "checkout-prod"
    first = outcome.deliveries[0]
    assert first.original_starts_at == "2026-10-10T10:41:54.365Z"
    assert first.received_at.endswith("Z")
    assert json.loads(first.audit_json)["fingerprint"] == alert["fingerprint"]
    assert outcome.deliveries[1].received_at >= first.received_at
    assert (
        alert_deliveries_for_identity(
            store, alert["fingerprint"], "2026-10-10T10:41:54Z"
        )
        == outcome.deliveries
    )
    assert (
        _count(
            "SELECT count(*) FROM opspilot_runs WHERE incident_id=%s", UUID(incident)
        )
        == 1
    )

    # The Run input: v3 (r5 J3: an alert Run also carries its anchor) with
    # the affected service, authorization unchanged, the question built from
    # labels only.
    with psycopg.connect(DSN) as conn:
        raw = conn.execute(
            "SELECT input FROM opspilot_runs WHERE run_id=%s", (UUID(run),)
        ).fetchone()[0]
    parsed = InvestigationInput.from_json(raw)
    assert raw["version"] == "opspilot-investigation-input-v3"
    assert parsed.scope_facts["affected_service"] == {
        "namespace": "otel-demo",
        "workload": "checkout",
    }
    assert parsed.scope_facts["target_ids"] == ["checkout-prod"]
    assert parsed.bound_target_id == "checkout-prod"
    assert "CheckoutPlaceOrderErrorRatioHigh" in parsed.question
    assert "rate(x[5m]) > 0.5" in parsed.question
    assert "error ratio above" not in parsed.question

    # Same incident page as any intake, with the alert section.
    page = call(app, "GET", f"/incidents/{incident}", headers=basic())
    assert page.status == 200
    assert 'id="alert-affected-service">otel-demo/checkout' in page.text
    assert "now 61%" in page.text
    # The intake was announced once, like ``/intake/ui``.
    kinds = [e.kind for e in workbench.events.read_after(UUID(incident), 0)]
    assert kinds.count("intake_accepted") == 1


def test_unresolved_and_ambiguous_alerts_are_handoff_only_incidents():
    app, workbench, store = _build()
    unresolved = _alert(labels={"alertname": "X", "namespace": "elsewhere"})
    ambiguous = _alert(
        labels={
            "alertname": "Y",
            "namespace": "otel-demo",
            "service": "checkout",
            "track": "canary",
        }
    )
    response = _post(app, _webhook(unresolved, ambiguous))
    assert response.status == 200
    first, second = response.json()["results"]
    assert (first["outcome"], first["reason"]) == (
        "handoff_created",
        "TARGET_UNRESOLVED",
    )
    assert (second["outcome"], second["reason"]) == (
        "handoff_created",
        "TARGET_AMBIGUOUS",
    )
    assert first["run_id"] is None and second["run_id"] is None
    replay = _post(app, _webhook(unresolved)).json()["results"][0]
    assert replay["outcome"] == "handoff_replayed"
    assert replay["incident_id"] == first["incident_id"]
    assert replay["reason"] == "TARGET_UNRESOLVED"

    for result in (first, second):
        subject = UUID(result["incident_id"])
        assert (
            _count("SELECT count(*) FROM opspilot_runs WHERE incident_id=%s", subject)
            == 0
        )
        assert subject not in store.claimable_incidents(limit=1000)
    outcome = _outcome(store, first["incident_id"])
    assert outcome.handoff and outcome.run_id is None and outcome.target_id is None
    assert outcome.run_input is None and outcome.run_ids == ()
    assert outcome.affected_service is None
    assert [d.outcome for d in outcome.deliveries] == [
        "handoff_created",
        "handoff_replayed",
    ]

    listed = {str(i.incident_id): i for i in workbench.list_incidents()}
    assert listed[first["incident_id"]].handoff_reason == "TARGET_UNRESOLVED"
    index = call(app, "GET", "/", headers=basic())
    assert "TARGET_AMBIGUOUS" in index.text
    page = call(app, "GET", f"/incidents/{second['incident_id']}", headers=basic())
    assert page.status == 200 and 'id="handoff-reason">TARGET_AMBIGUOUS' in page.text
    # No Run to steer: every control is refused, nothing is written.
    refused = post_form(
        app,
        f"/incidents/{first['incident_id']}/control",
        {"action": "new_run", "expected_generation": "0", "idempotency_key": "k1"},
        headers={**basic(), **same_origin()},
    )
    assert refused.status == 409 and refused.json()["code"] == "HANDOFF_ONLY"
    events = call(
        app, "GET", f"/incidents/{first['incident_id']}/events", headers=basic()
    )
    assert events.status == 200


def test_resolved_attaches_to_the_same_identity_or_is_only_recorded():
    app, workbench, store = _build()
    alert = _alert()
    created = _post(app, _webhook(alert)).json()["results"][0]
    resolved = dict(
        alert,
        status="resolved",
        endsAt="2026-10-10T10:46:54.365Z",
        annotations={"summary": "recovered"},
    )
    attached = _post(app, _webhook(resolved, status="resolved")).json()["results"][0]
    assert attached["outcome"] == "resolved_attached"
    assert (attached["incident_id"], attached["run_id"]) == (
        created["incident_id"],
        created["run_id"],
    )
    incidents_before = _count("SELECT count(*) FROM opspilot_incidents")
    orphan = _alert(status="resolved")
    recorded = _post(app, _webhook(orphan, status="resolved")).json()["results"][0]
    assert recorded["outcome"] == "resolved_recorded"
    assert recorded["incident_id"] is None and recorded["run_id"] is None
    assert _count("SELECT count(*) FROM opspilot_incidents") == incidents_before
    assert (
        _count(
            "SELECT count(*) FROM opspilot_alert_deliveries WHERE fingerprint=%s AND incident_id IS NULL",
            orphan["fingerprint"],
        )
        == 1
    )

    with psycopg.connect(DSN) as conn:
        [orphaned] = alert_deliveries_for_identity(
            conn, orphan["fingerprint"], "2026-10-10T10:41:54Z"
        )
    assert orphaned.outcome == "resolved_recorded"
    outcome = _outcome(store, created["incident_id"])
    assert [
        (d.status, d.outcome, d.annotation_revision) for d in outcome.deliveries
    ] == [
        ("firing", "created", 1),
        ("resolved", "resolved_attached", 2),
    ]
    # Resolved does not move the incident or the Run.
    with psycopg.connect(DSN) as conn:
        lifecycle, run_state = conn.execute(
            "SELECT i.lifecycle, r.state FROM opspilot_incidents i JOIN opspilot_runs r ON r.run_id=i.current_run_id WHERE i.incident_id=%s",
            (UUID(created["incident_id"]),),
        ).fetchone()
    assert (lifecycle, run_state) == ("open", "queued")
    page = call(app, "GET", f"/incidents/{created['incident_id']}", headers=basic())
    assert (
        'id="alert-resolved"' in page.text and "2026-10-10T10:46:54.365Z" in page.text
    )


def test_a_group_is_answered_per_alert_in_payload_order_and_invalid_writes_nothing():
    app, workbench, store = _build()
    good = _alert()
    no_fingerprint = _alert()
    del no_fingerprint["fingerprint"]
    naive = _alert(startsAt="2026-10-10T10:41:54")
    with psycopg.connect(DSN) as conn:
        deliveries_before = alert_delivery_count(conn)
    response = _post(app, _webhook(good, no_fingerprint, naive, "not-an-alert"))
    assert response.status == 200
    results = response.json()["results"]
    assert [(r["outcome"], r["reason"]) for r in results] == [
        ("created", None),
        ("invalid", "FINGERPRINT_MISSING"),
        ("invalid", "STARTS_AT_INVALID"),
        ("invalid", "ALERT_NOT_OBJECT"),
    ]
    assert set(results[0]) == {
        "fingerprint",
        "starts_at",
        "status",
        "outcome",
        "incident_id",
        "run_id",
        "delivery_key",
        "reason",
    }
    assert _count("SELECT count(*) FROM opspilot_alert_deliveries") == (
        deliveries_before + 1
    )


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        json.dumps({"version": "3", "alerts": []}).encode(),
        json.dumps({"version": "4", "alerts": {}}).encode(),
        json.dumps({"alerts": []}).encode(),
    ],
)
def test_malformed_payloads_are_400_and_write_nothing(body):
    app, _, _ = _build()
    before = _count("SELECT count(*) FROM opspilot_alert_deliveries")
    response = call(
        app,
        "POST",
        "/intake/alertmanager",
        headers={**bearer(), "content-type": "application/json"},
        body=body,
    )
    assert response.status == 400
    assert response.json() == {"error": "INVALID_ALERTMANAGER_PAYLOAD"}
    assert _count("SELECT count(*) FROM opspilot_alert_deliveries") == before


def test_only_the_event_token_channel_is_accepted():
    app, _, _ = _build()
    assert _post(app, _webhook(_alert()), headers={}).status == 401
    ui = _post(app, _webhook(_alert()), headers=basic())
    assert ui.status == 401 and "www-authenticate" not in ui.headers


def test_concurrent_first_deliveries_open_one_incident():
    app, _, store = _build()
    alert = _alert()
    results: list[dict] = []
    lock = threading.Lock()

    def deliver():
        body = _post(app, _webhook(alert)).json()["results"][0]
        with lock:
            results.append(body)

    threads = [threading.Thread(target=deliver) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert sorted(r["outcome"] for r in results) == ["created"] + ["replayed"] * 5
    assert len({r["incident_id"] for r in results}) == 1
    assert (
        _count(
            "SELECT count(*) FROM opspilot_alert_identities WHERE fingerprint=%s",
            alert["fingerprint"],
        )
        == 1
    )


@pytest.mark.parametrize("unresolved", [False, True])
def test_a_storage_failure_is_503_and_the_retry_replays_what_committed(unresolved):
    app, _, store = _build()
    labels = {"alertname": "X", "namespace": "nowhere"} if unresolved else None
    ok, boom = _alert(), _alert(f"boom{uuid4().hex[:12]}", labels=labels)
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            "CREATE OR REPLACE FUNCTION impl_test_boom() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.fingerprint LIKE 'boom%' THEN RAISE EXCEPTION 'injected'; END IF; RETURN NEW; END $$"
        )
        conn.execute(
            "CREATE TRIGGER impl_test_boom BEFORE INSERT ON opspilot_alert_deliveries FOR EACH ROW EXECUTE FUNCTION impl_test_boom()"
        )
    try:
        failed = _post(app, _webhook(ok, boom))
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("DROP TRIGGER impl_test_boom ON opspilot_alert_deliveries")
            conn.execute("DROP FUNCTION impl_test_boom()")
    assert failed.status == 503
    first, second = failed.json()["results"]
    assert first["outcome"] == "created"
    assert second["outcome"] == "failed" and second["reason"]
    assert second["incident_id"] is None
    # Nothing of the failed alert committed: no identity, no incident.
    assert (
        _count(
            "SELECT count(*) FROM opspilot_alert_identities WHERE fingerprint=%s",
            boom["fingerprint"],
        )
        == 0
    )
    for table in ("opspilot_alert_identities", "opspilot_alert_deliveries"):
        assert (
            _count(
                f"SELECT count(*) FROM {table} WHERE fingerprint=%s",
                boom["fingerprint"],
            )
            == 0
        )
    intake_key = f"intake:{parse_alert(boom).delivery_key}"
    assert (
        _count(
            "SELECT count(*) FROM opspilot_incidents WHERE intake_key=%s", intake_key
        )
        == 0
    )
    assert (
        _count(
            "SELECT count(*) FROM opspilot_runs WHERE incident_id=%s",
            uuid5(_INTAKE_NAMESPACE, intake_key),
        )
        == 0
    )
    retry = _post(app, _webhook(ok, boom))
    assert retry.status == 200
    assert [r["outcome"] for r in retry.json()["results"]] == [
        "replayed",
        "handoff_created" if unresolved else "created",
    ]


def test_record_is_redacted_bounded_and_rendered_as_text():
    app, _, store = _build()
    alert = _alert(
        annotations={
            "token": "s3cr3t-value",
            "description": "use Authorization: Bearer abcdefghijklmnop " + "x" * 40000,
            "summary": "<script>alert(1)</script>",
        }
    )
    result = _post(app, _webhook(alert)).json()["results"][0]
    with psycopg.connect(DSN) as conn:
        text, truncated, raw_sha = conn.execute(
            "SELECT alert_json, truncated, raw_sha256 FROM opspilot_alert_deliveries WHERE fingerprint=%s",
            (alert["fingerprint"],),
        ).fetchone()
    assert truncated is True and len(text.encode()) <= 16384
    assert "s3cr3t-value" not in text and "abcdefghijklmnop" not in text
    assert raw_sha == canonical_hash(alert)
    outcome = _outcome(store, result["incident_id"])
    assert outcome.deliveries[0].truncated is True
    page = call(app, "GET", f"/incidents/{result['incident_id']}", headers=basic())
    assert "<script>alert(1)</script>" not in page.text
    assert "s3cr3t-value" not in page.text


def test_short_record_keeps_whole_annotations_and_escapes_them():
    app, _, _ = _build()
    alert = _alert(annotations={"summary": "<b>bold</b> api_key=abc123"})
    result = _post(app, _webhook(alert)).json()["results"][0]
    page = call(app, "GET", f"/incidents/{result['incident_id']}", headers=basic())
    assert "&lt;b&gt;bold&lt;/b&gt;" in page.text
    assert "abc123" not in page.text


def test_lab_webhooks_firing_then_resolved():
    app, _, store = _build()
    firing = json.loads((EVIDENCE / "webhook-firing.json").read_text())
    resolved = json.loads((EVIDENCE / "webhook-resolved.json").read_text())
    # A fresh identity per run: the shared database keeps earlier runs.
    fingerprint = uuid4().hex[:16]
    for payload in (firing, resolved):
        payload["alerts"][0]["fingerprint"] = fingerprint
    first = _post(app, firing).json()["results"][0]
    second = _post(app, resolved).json()["results"][0]
    assert first["outcome"] == "created"
    assert second["outcome"] == "resolved_attached"
    assert second["incident_id"] == first["incident_id"]
    outcome = _outcome(store, first["incident_id"])
    assert outcome.target_id == "checkout-prod"
    assert [d.annotation_revision for d in outcome.deliveries] == [1, 2]


def test_without_a_tool_face_the_identity_still_records_the_service():
    app, _, store = _build(face=False)
    result = _post(app, _webhook(_alert())).json()["results"][0]
    assert result["outcome"] == "created"
    assert _outcome(store, result["incident_id"]).affected_service == (
        "otel-demo",
        "checkout",
    )


def test_projection_refuses_another_subject():
    app, _, store = _build()
    result = _post(app, _webhook(_alert())).json()["results"][0]
    with psycopg.connect(DSN) as conn:
        records = alert_intake_records(conn, result["incident_id"])
    scenario = IncidentScenario("impl", "F1", "2", "alert_intake", str(uuid4()))
    with pytest.raises(ValueError, match="SUBJECT_MISMATCH"):
        alert_intake_outcome(scenario, records)
    # A DurableStore works as the reader as well.
    assert alert_intake_records(store, result["incident_id"]).identity is not None


def test_the_worker_claims_and_completes_an_alert_run():
    """The v2 input (affected service) is runnable by the product worker."""
    from tests.integration.test_m1_web_worker_postgres import _tool_round, _worker
    from tests.m1_investigation_support import report_from_transcript

    app, workbench, store = _build()
    result = _post(app, _webhook(_alert())).json()["results"][0]
    subject = UUID(result["incident_id"])
    assert subject in store.claimable_incidents(limit=1000)
    loop, model = _worker(
        store,
        workbench.events,
        workbench.evidence,
        [_tool_round(), report_from_transcript],
        subject=subject,
    )
    assert [(i, o.status) for i, o in loop.poll_once()] == [(subject, "published")]
    assert "CheckoutPlaceOrderErrorRatioHigh" in str(model.calls[0])
    snapshot = workbench.snapshot(subject)
    assert snapshot["run"]["state"] == "completed"
    assert snapshot["alert"]["outcome"].run_id == result["run_id"]


def test_a_failed_alert_leaves_no_target_registration():
    """Independent review P1: the target row commits with the alert or not at all."""
    uid = f"fresh-{uuid4().hex[:12]}"
    store = DurableStore(DSN)
    store.install()
    workbench = Workbench(
        incidents=DurableIncidentStore(store),
        events=DurableEventLog(store),
        evidence=DurableEvidenceStore(store),
        ledger=DurableWebLedger(store),
        run_versions=fixture_versions(),
        run_seconds=600,
        targets=MappingTargetRegistry(
            {uid: {**IDENTITY, "match": {"version": 1, "labels": {"fresh": uid}}}}
        ),
    )
    config = AuthConfig(
        ui_users={UI_USER: hash_password(UI_PASSWORD)},
        event_tokens={token_digest(EVENT_TOKEN): ACTOR},
        auth_revision="auth-rev-pg",
        allowed_origins=frozenset({ORIGIN}),
    )
    app = create_app(workbench, Authenticator(config), DurableClock(store))
    boom = _alert(f"boom{uuid4().hex[:12]}", labels={"alertname": "F", "fresh": uid})
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            "CREATE OR REPLACE FUNCTION impl_test_boom() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.fingerprint LIKE 'boom%' THEN RAISE EXCEPTION 'injected'; END IF; RETURN NEW; END $$"
        )
        conn.execute(
            "CREATE TRIGGER impl_test_boom BEFORE INSERT ON opspilot_alert_deliveries FOR EACH ROW EXECUTE FUNCTION impl_test_boom()"
        )
    try:
        failed = _post(app, _webhook(boom))
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("DROP TRIGGER impl_test_boom ON opspilot_alert_deliveries")
            conn.execute("DROP FUNCTION impl_test_boom()")
    assert failed.status == 503
    assert failed.json()["results"][0]["outcome"] == "failed"
    assert (
        _count("SELECT count(*) FROM opspilot_targets WHERE resource_uid=%s", uid) == 0
    )
    retry = _post(app, _webhook(boom))
    assert retry.json()["results"][0]["outcome"] == "created"
    assert (
        _count("SELECT count(*) FROM opspilot_targets WHERE resource_uid=%s", uid) == 1
    )
    assert (
        _count(
            "SELECT count(*) FROM opspilot_target_suspensions s JOIN opspilot_targets t USING (target_id) WHERE t.resource_uid=%s",
            uid,
        )
        == 1
    )


def test_a_worker_without_v2_support_blocks_a_v2_run_as_incompatible(monkeypatch):
    """R12: a Run whose input version this worker does not know is blocked
    ``INCOMPATIBLE_STATE``, not run and not reported as malformed."""
    from opspilot.investigation import context
    from tests.integration.test_m1_loop_resume_postgres import Harness, _input
    from tests.m1_investigation_support import report_from_transcript

    v2 = {
        **_input(uuid4()),
        "version": "opspilot-investigation-input-v2",
        "scope_facts": {
            "target_ids": ["checkout-prod"],
            "affected_service": {"namespace": "otel-demo", "workload": "checkout"},
        },
    }
    monkeypatch.setattr(context, "KNOWN_INPUT_VERSIONS", (context.INPUT_VERSION,))
    h = Harness(input=v2)
    outcome = h.runner([report_from_transcript]).resume(h.incident)
    assert outcome.status == "blocked" and outcome.reason == "INCOMPATIBLE_STATE"
    assert h.rows()["run"]["state"] == "blocked"
