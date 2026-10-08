"""The workbench side of "register remediation" without PostgreSQL (M1-02 step 3).

The in-memory ``IncidentStore`` mirrors the durable semantics the PG test
proves (``tests/integration/test_m1_02_register_remediation_postgres.py``);
this file covers the web contract: the action vocabulary, the ``revision``
field, the HealthProfile requirement, idempotency and the page projection,
plus the registry-resolved intake target.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

from opspilot.web.store import MappingTargetRegistry, TargetIdentity
from tests.m1_web_support import (
    basic,
    build_workbench,
    call,
    post_form,
    same_origin,
    submit_incident,
)
from tests.target_support import ANY_TARGETS, IDENTITY


def _control(app, incident, fields):
    return post_form(
        app,
        f"/incidents/{incident}/control",
        fields,
        headers={**basic(), **same_origin()},
    )


def test_register_remediation_starts_observation_once_per_key():
    app, workbench, _ = build_workbench(targets=ANY_TARGETS)
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
    app, _, _ = build_workbench(targets=ANY_TARGETS)
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
    app, workbench, _ = build_workbench(health_profile=None, targets=ANY_TARGETS)
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
    app, workbench, _ = build_workbench(targets=ANY_TARGETS)
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


def test_intake_needs_no_registry_and_registration_needs_the_identity():
    """User decision 2026-10-07 (PR #120 review item 4): intake keeps the
    M1-01 contract -- any target id is registered by uid, no identity file
    needed -- and the identity is required only when a remediation is
    registered: without a registry entry the registration is refused and
    nothing is written; with one, the registry row is completed once."""
    app, workbench, _ = build_workbench(targets=None)
    accepted = submit_incident(app, key=f"k-{uuid4()}")
    assert accepted.status == 201
    canary = post_form(
        app,
        "/intake/ui",
        {
            "target_id": "checkout-canary",
            "question": "Why is checkout erroring?",
            "idempotency_key": f"k-{uuid4()}",
        },
        headers={**basic(), **same_origin()},
    )
    assert canary.status == 201
    assert len(workbench.list_incidents()) == 2
    incident = accepted.json()["incident_id"]
    fields = {
        "action": "register_remediation",
        "expected_generation": "0",
        "idempotency_key": "rem-1",
        "revision": "checkout:v2",
    }
    refused = _control(app, incident, fields)
    assert (refused.status, refused.json()["code"]) == (409, "TARGET_IDENTITY_MISSING")
    assert workbench.incidents.sessions == [] and workbench.incidents.controls == []
    row = workbench.incidents.incidents[UUID(incident)]
    assert (row["lifecycle"], row["control_generation"]) == ("open", 0)
    # A registry that lists only checkout-prod: the registration completes
    # that row; checkout-canary stays refused.
    workbench.targets = MappingTargetRegistry(
        {
            "checkout-prod": {
                "integration_id": "otel",
                "cluster_uid": "kind",
                "namespace": "demo",
            }
        }
    )
    accepted_registration = _control(
        app, incident, {**fields, "idempotency_key": "rem-2"}
    )
    assert accepted_registration.status == 200, accepted_registration.text
    (session,) = workbench.incidents.sessions
    assert session["target"] == {
        "resource_uid": "checkout-prod",
        "integration_id": "otel",
        "cluster_uid": "kind",
        "namespace": "demo",
        "revision": "checkout:v2",
    }
    other = _control(
        app,
        canary.json()["incident_id"],
        {**fields, "idempotency_key": "rem-3"},
    )
    assert (other.status, other.json()["code"]) == (409, "TARGET_IDENTITY_MISSING")


def test_a_paused_incident_takes_no_registration_until_resumed():
    """Independent review of PR #120, P1: registering a remediation must not
    bypass or lift a pause; resume is its own explicit decision."""
    app, workbench, _ = build_workbench(targets=ANY_TARGETS)
    incident = submit_incident(app).json()["incident_id"]
    paused = _control(
        app,
        incident,
        {"action": "pause", "expected_generation": "0", "idempotency_key": "p-1"},
    )
    assert paused.status == 200 and paused.json()["generation"] == 1
    refused = _control(
        app,
        incident,
        {
            "action": "register_remediation",
            "expected_generation": "1",
            "idempotency_key": "rem-1",
            "revision": "checkout:v2",
        },
    )
    assert (refused.status, refused.json()["code"]) == (409, "ILLEGAL_TRANSITION")
    row = next(iter(workbench.incidents.incidents.values()))
    assert (row["state"], row["lifecycle"], row["control_generation"]) == (
        "paused",
        "open",
        1,
    )
    assert workbench.incidents.sessions == []
    assert [c["action"] for c in workbench.incidents.controls] == ["pause"]
    resumed = _control(
        app,
        incident,
        {"action": "resume", "expected_generation": "1", "idempotency_key": "r-1"},
    )
    assert resumed.status == 200 and resumed.json()["generation"] == 2
    accepted = _control(
        app,
        incident,
        {
            "action": "register_remediation",
            "expected_generation": "2",
            "idempotency_key": "rem-2",
            "revision": "checkout:v2",
        },
    )
    assert accepted.status == 200 and accepted.json()["generation"] == 3
    assert row["lifecycle"] == "observing_recovery"


def test_crash_recovery_confirms_a_registration_only_against_its_own_key():
    """Independent review of PR #120, P2: two unconfirmed intents (keys A and
    B, revisions rev-A and rev-B) for the same incident, operator and
    generation; only A's store transaction committed before the crash. B must
    not be confirmed as that decision; A must."""
    app, workbench, _ = build_workbench(targets=ANY_TARGETS)
    incident_id = submit_incident(app).json()["incident_id"]
    subject = workbench.list_incidents()[0].incident_id
    for key, revision in (("A", "rev-A"), ("B", "rev-B")):
        workbench.ledger.put(
            "control_intent",
            f"{subject}:{key}",
            {
                "action": "register_remediation",
                "actor_id": "alice",
                "expected_generation": 0,
                "revision": revision,
            },
        )
    # A's transaction committed (audit row bound to A's key); no confirm row.
    profile = workbench.health_profile
    workbench.incidents.register_remediation(
        subject,
        expected_generation=0,
        actor="alice",
        revision="rev-A",
        deadline_at=workbench.incidents.now() + timedelta(hours=1),
        max_samples=profile.session.max_samples,
        sample_interval_seconds=profile.session.sample_interval_seconds,
        sustained_window_seconds=profile.session.sustained_window_seconds,
        health_profile_revision=profile.revision,
        health_profile="{}",
        session_id=uuid4(),
        identity=TargetIdentity(resource_uid="checkout-prod", **IDENTITY),
        payload={"channel": "web", "idempotency_key": f"{subject}:A"},
    )
    retry_b = _control(
        app,
        incident_id,
        {
            "action": "register_remediation",
            "expected_generation": "0",
            "idempotency_key": "B",
            "revision": "rev-B",
        },
    )
    assert retry_b.status == 409
    assert retry_b.json() == {"code": "CONTROL_CONFLICT", "current_generation": 1}
    assert workbench.ledger.get("control", f"{subject}:B") is None
    retry_a = _control(
        app,
        incident_id,
        {
            "action": "register_remediation",
            "expected_generation": "0",
            "idempotency_key": "A",
            "revision": "rev-A",
        },
    )
    assert retry_a.status == 200
    assert retry_a.json()["generation"] == 1 and retry_a.json()["replayed"] is True
    (session,) = workbench.incidents.sessions
    assert session["target"]["revision"] == "rev-A"
    assert [c["revision"] for c in workbench.snapshot(subject)["controls"]] == ["rev-A"]
