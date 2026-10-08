"""Control audit rows bind to their request key on the real DurableStore (#128).

The in-memory double proves the workbench rule in
``tests/test_m1_control_audit_key.py``; here the key is read back from
``opspilot_controls.payload`` and the A/B crash recovery runs through the
durable ledger and store.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from opspilot.persistence import DurableStore, PersistenceError
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


def _build():
    store = DurableStore(DSN)
    store.install()
    events = DurableEventLog(store)
    events.install()
    evidence = DurableEvidenceStore(store)
    evidence.install()
    ledger = DurableWebLedger(store)
    ledger.install()
    workbench = Workbench(
        incidents=DurableIncidentStore(store),
        events=events,
        evidence=evidence,
        ledger=ledger,
        run_versions={"state": "v1"},
        run_seconds=600,
    )
    config = AuthConfig(
        ui_users={UI_USER: hash_password(UI_PASSWORD)},
        event_tokens={token_digest(EVENT_TOKEN): "alertmanager-prod"},
        auth_revision="auth-rev-pg",
        allowed_origins=frozenset({ORIGIN}),
    )
    app = create_app(workbench, Authenticator(config), DurableClock(store))
    return app, workbench, store


def _submit(app):
    accepted = post_json(
        app,
        "/intake/events",
        {
            "source": "alertmanager",
            "external_event_id": f"pg-audit-key-{uuid4()}",
            "target_id": "checkout-prod",
            "question": "CheckoutErrorRateHigh firing",
        },
        headers=bearer(),
    )
    assert accepted.status == 201
    return accepted.json()["incident_id"]


def _control(app, incident, action, generation, key, **extra):
    return post_form(
        app,
        f"/incidents/{incident}/control",
        {
            "action": action,
            "expected_generation": str(generation),
            "idempotency_key": key,
            **extra,
        },
        headers={**basic(), **same_origin()},
    )


def _audit_rows(store, incident):
    with store.transaction(snapshot=True) as conn:
        return conn.execute(
            "SELECT action,resulting_generation,payload FROM opspilot_controls WHERE incident_id=%s ORDER BY resulting_generation",
            (incident,),
        ).fetchall()


@pytest.mark.parametrize(
    ("action", "prerequisites", "extra"),
    [
        ("takeover", (), {}),
        ("pause", (), {}),
        ("cancel", (), {}),
        ("resume", (), {}),
        ("new_run", ("cancel",), {}),
        ("correct", (), {"text": "Same note twice."}),
    ],
)
def test_crash_recovery_on_postgres_confirms_only_the_key_on_the_audit_row(
    action, prerequisites, extra
):
    app, workbench, store = _build()
    incident = _submit(app)
    subject = workbench.list_incidents()[0].incident_id
    generation = 0
    for pre in prerequisites:
        done = _control(app, incident, pre, generation, f"pre-{pre}")
        assert done.status == 200, done.text
        generation = done.json()["generation"]
    applied = generation + 1

    real_put = workbench.ledger.put

    def crash_before_confirm(namespace, key, value):
        if namespace == "control" and key.endswith(":A"):
            raise PersistenceError("STORAGE_UNAVAILABLE")
        return real_put(namespace, key, value)

    workbench.ledger.put = crash_before_confirm
    try:
        crashed = _control(app, incident, action, generation, "A", **extra)
    finally:
        workbench.ledger.put = real_put
    assert crashed.status == 503, crashed.text
    last = _audit_rows(store, subject)[-1]
    assert (last["action"], last["resulting_generation"]) == (action, applied)
    assert last["payload"]["idempotency_key"] == f"{subject}:A"
    assert last["payload"]["channel"] == "web"

    intent = {"action": action, "actor_id": UI_USER, "expected_generation": generation}
    intent.update(extra)
    workbench.ledger.put("control_intent", f"{subject}:B", intent)
    retry_b = _control(app, incident, action, generation, "B", **extra)
    assert retry_b.status == 409, retry_b.text
    assert retry_b.json() == {"code": "CONTROL_CONFLICT", "current_generation": applied}
    assert workbench.ledger.get("control", f"{subject}:B") is None

    retry_a = _control(app, incident, action, generation, "A", **extra)
    assert retry_a.status == 200, retry_a.text
    assert retry_a.json()["generation"] == applied and retry_a.json()["replayed"]
    assert [r["action"] for r in _audit_rows(store, subject)].count(action) == 1
    assert workbench.list_incidents()[0].control_generation == applied


def test_a_pre_128_textless_audit_row_on_postgres_is_not_a_replay():
    """A row without a key (written by an older version) never confirms a
    pending textless intent; the retry is refused with the live generation."""
    app, workbench, store = _build()
    incident = _submit(app)
    subject = workbench.list_incidents()[0].incident_id
    workbench.ledger.put(
        "control_intent",
        f"{subject}:A",
        {"action": "pause", "actor_id": UI_USER, "expected_generation": 0},
    )
    assert store.control(subject, 0, "pause", UI_USER) == 1
    assert _audit_rows(store, subject)[-1]["payload"] is None
    retry = _control(app, incident, "pause", 0, "A")
    assert retry.status == 409, retry.text
    assert retry.json() == {"code": "CONTROL_CONFLICT", "current_generation": 1}
    assert workbench.ledger.get("control", f"{subject}:A") is None
