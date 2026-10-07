"""The workbench side of "register remediation" without PostgreSQL (M1-02 step 3).

The in-memory ``IncidentStore`` mirrors the durable semantics the PG test
proves (``tests/integration/test_m1_02_register_remediation_postgres.py``);
this file covers the web contract: the action vocabulary, the ``revision``
field, the HealthProfile requirement, idempotency and the page projection,
plus the registry-resolved intake target.
"""

from __future__ import annotations

from uuid import uuid4

from opspilot.web.store import MappingTargetRegistry, TargetIdentity
from tests.m1_web_support import (
    basic,
    build_workbench,
    call,
    post_form,
    same_origin,
    submit_incident,
)


def _control(app, incident, fields):
    return post_form(
        app,
        f"/incidents/{incident}/control",
        fields,
        headers={**basic(), **same_origin()},
    )


def test_register_remediation_starts_observation_once_per_key():
    app, workbench, _ = build_workbench()
    incident = submit_incident(app).json()["incident_id"]
    fields = {
        "action": "register_remediation",
        "expected_generation": "0",
        "idempotency_key": "rem-1",
        "revision": "checkout:v2",
    }
    first = _control(app, incident, fields)
    assert first.status == 200, first.text
    assert first.json() == {
        "incident_id": incident,
        "action": "register_remediation",
        "generation": 1,
        "replayed": False,
        "sequence": first.json()["sequence"],
    }
    replay = _control(app, incident, fields)
    assert replay.status == 200
    assert replay.json()["generation"] == 1 and replay.json()["replayed"] is True
    store = workbench.incidents
    assert len(store.sessions) == 1, "the same key never creates a second session"
    row = next(iter(store.incidents.values()))
    assert row["lifecycle"] == "observing_recovery"
    assert row["control_generation"] == 1
    assert [(c["action"], c["expected"], c["resulting"]) for c in store.controls] == [
        ("register_remediation", 0, 1)
    ]
    assert store.controls[0]["payload"]["revision"] == "checkout:v2"
    # Parameters come from the HealthProfile, not from the form.
    session = store.sessions[0]
    profile = workbench.health_profile
    assert session["max_samples"] == profile.session.max_samples
    assert session["health_profile_revision"] == profile.revision
    stale = _control(app, incident, {**fields, "idempotency_key": "rem-2"})
    assert stale.status == 409
    assert stale.json() == {"code": "CONTROL_CONFLICT", "current_generation": 1}
    page = call(app, "GET", f"/incidents/{incident}", headers=basic())
    assert page.status == 200
    assert 'id="observation-sessions"' in page.text
    assert "checkout:v2" in page.text
    snapshot = workbench.snapshot(next(iter(store.incidents)))
    assert snapshot["observation_sessions"][0]["state"] == "authorized"
    assert snapshot["controls"][0]["revision"] == "checkout:v2"


def test_revision_belongs_to_register_remediation_only():
    app, _, _ = build_workbench()
    incident = submit_incident(app).json()["incident_id"]
    missing = _control(
        app,
        incident,
        {
            "action": "register_remediation",
            "expected_generation": "0",
            "idempotency_key": "rem-1",
        },
    )
    assert (missing.status, missing.json()["code"]) == (400, "REVISION_REQUIRED")
    misplaced = _control(
        app,
        incident,
        {
            "action": "pause",
            "expected_generation": "0",
            "idempotency_key": "p-1",
            "revision": "checkout:v2",
        },
    )
    assert (misplaced.status, misplaced.json()["code"]) == (400, "REVISION_NOT_ALLOWED")
    with_text = _control(
        app,
        incident,
        {
            "action": "register_remediation",
            "expected_generation": "0",
            "idempotency_key": "rem-1",
            "revision": "checkout:v2",
            "text": "handled",
        },
    )
    assert (with_text.status, with_text.json()["code"]) == (400, "TEXT_NOT_ALLOWED")
    page = call(app, "GET", f"/incidents/{incident}", headers=basic())
    assert 'id="observation-none"' in page.text


def test_without_a_health_profile_the_action_is_refused():
    app, workbench, _ = build_workbench(health_profile=None)
    incident = submit_incident(app).json()["incident_id"]
    refused = _control(
        app,
        incident,
        {
            "action": "register_remediation",
            "expected_generation": "0",
            "idempotency_key": "rem-1",
            "revision": "checkout:v2",
        },
    )
    assert (refused.status, refused.json()["code"]) == (409, "HEALTH_PROFILE_REQUIRED")
    assert workbench.incidents.sessions == []
    assert workbench.incidents.controls == []
    page = call(app, "GET", f"/incidents/{incident}", headers=basic())
    assert "no HealthProfile configured" in page.text


def test_pause_and_cancel_withdraw_the_authorization():
    app, workbench, _ = build_workbench()
    incident = submit_incident(app).json()["incident_id"]
    assert (
        _control(
            app,
            incident,
            {
                "action": "register_remediation",
                "expected_generation": "0",
                "idempotency_key": "rem-1",
                "revision": "checkout:v2",
            },
        ).status
        == 200
    )
    cancelled = _control(
        app,
        incident,
        {"action": "cancel", "expected_generation": "1", "idempotency_key": "c-1"},
    )
    assert cancelled.status == 200 and cancelled.json()["generation"] == 2
    (session,) = workbench.incidents.sessions
    assert (session["state"], session["ended_reason"]) == (
        "revoked",
        "authority_revoked",
    )
    page = call(app, "GET", f"/incidents/{incident}", headers=basic())
    assert "revoked" in page.text and "authority_revoked" in page.text


def test_intake_resolves_the_target_through_the_registry_only():
    app, workbench, _ = build_workbench()
    workbench.targets = MappingTargetRegistry(
        {
            "checkout-prod": {
                "integration_id": "otel",
                "cluster_uid": "kind",
                "namespace": "demo",
            }
        }
    )
    accepted = submit_incident(app, key=f"k-{uuid4()}")
    assert accepted.status == 201
    assert workbench.incidents.targets["checkout-prod"] == TargetIdentity(
        integration_id="otel",
        cluster_uid="kind",
        namespace="demo",
        resource_uid="checkout-prod",
    )
    unknown = post_form(
        app,
        "/intake/ui",
        {
            "target_id": "checkout-canary",
            "question": "Why is checkout erroring?",
            "idempotency_key": f"k-{uuid4()}",
        },
        headers={**basic(), **same_origin()},
    )
    assert (unknown.status, unknown.json()) == (400, {"code": "UNKNOWN_TARGET"})
    assert len(workbench.list_incidents()) == 1
    # The refused key is not poisoned: the same key with a known target goes through.
    key = f"k-{uuid4()}"
    refused = post_form(
        app,
        "/intake/ui",
        {"target_id": "nope", "question": "q", "idempotency_key": key},
        headers={**basic(), **same_origin()},
    )
    assert refused.status == 400
    retried = post_form(
        app,
        "/intake/ui",
        {"target_id": "checkout-prod", "question": "q", "idempotency_key": key},
        headers={**basic(), **same_origin()},
    )
    assert retried.status == 201
