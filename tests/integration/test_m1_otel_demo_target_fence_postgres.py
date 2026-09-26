"""A workbench-submitted incident is bound to its target, so a target
suspension fences its Runs under the ``otel-demo`` profile.

PR #54 bot review P1: ``Workbench.submit()`` used to call ``accept()``
without a target identity, so the incident row carried ``target_id NULL``
and ``control_state`` never saw a target suspension -- the profile's
``DurableControl`` reported the target as unsuspended whatever a human had
decided. Human control priority (PRODUCT-CONSTRAINTS, "Runtime and human
control requirements"). Run in isolation: the suspension touches every
incident bound to that target on the instance.
"""

from __future__ import annotations

import os
import urllib.request
from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from opspilot.persistence import DurableStore, PersistenceError
from opspilot.tools.otel_demo import (
    METRICS_TOOL,
    TARGET_ID,
    DurableControl,
    OtelDemoConfig,
    otel_demo_executor_factory,
    otel_demo_face,
    otel_demo_versions,
)
from opspilot.tools.profiles import PROFILE_ENV, select_profile
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
from tests.m1_tool_support import FakeClock, RecordingSink
from tests.m1_web_support import (
    EVENT_TOKEN,
    ORIGIN,
    UI_PASSWORD,
    UI_USER,
    basic,
    post_form,
    same_origin,
)
from tests.test_m1_otel_demo_contract import (
    GOOD_EXPR,
    JAEGER_URL,
    NOW,
    PROMETHEUS_URL,
    WINDOW,
    WINDOW_END,
    FakeOpener,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)


def _workbench():
    store = DurableStore(DSN)
    store.install()
    events = DurableEventLog(store)
    events.install()
    evidence = DurableEvidenceStore(store)
    evidence.install()
    ledger = DurableWebLedger(store)
    ledger.install()
    profile = select_profile({PROFILE_ENV: "otel-demo"})
    workbench = Workbench(
        incidents=DurableIncidentStore(store),
        events=events,
        evidence=evidence,
        ledger=ledger,
        run_versions=profile.versions(),
        run_seconds=600,
        tool_face=profile.face(FakeClock(start=WINDOW_END)),
    )
    config = AuthConfig(
        ui_users={UI_USER: hash_password(UI_PASSWORD)},
        event_tokens={token_digest(EVENT_TOKEN): "alertmanager-prod"},
        auth_revision="auth-rev-pg-otel-fence",
        allowed_origins=frozenset({ORIGIN}),
    )
    app = create_app(workbench, Authenticator(config), DurableClock(store))
    return app, store, evidence


def _submit(app) -> tuple[UUID, UUID]:
    response = post_form(
        app,
        "/intake/ui",
        {
            "target_id": TARGET_ID,
            "question": "Why is checkout erroring?",
            "idempotency_key": f"otel-fence-{uuid4()}",
        },
        headers={**basic(), **same_origin()},
    )
    assert response.status == 201, response.text
    return UUID(response.json()["incident_id"]), UUID(response.json()["run_id"])


def _release(store: DurableStore, target: UUID) -> None:
    with store.transaction() as conn:
        row = conn.execute(
            "SELECT suspended,generation FROM opspilot_target_suspensions WHERE target_id=%s",
            (target,),
        ).fetchone()
    if row and row["suspended"]:
        store.set_target_suspension(
            target, False, expected_generation=int(row["generation"]), actor="operator"
        )


def test_a_submitted_incident_is_bound_to_its_registered_target(monkeypatch):
    app, store, evidence = _workbench()
    target = store.register_target(TARGET_ID)
    _release(store, target)
    incident, run = _submit(app)
    with store.transaction() as conn:
        row = conn.execute(
            "SELECT target_id FROM opspilot_incidents WHERE incident_id=%s", (incident,)
        ).fetchone()
    assert row["target_id"] == target
    # A second registration of the same resource uid is the same identity.
    assert store.register_target(TARGET_ID) == target


def test_a_target_suspension_fences_a_live_otel_run(monkeypatch):
    """Claim first, then a human suspends the target: the profile's own
    control source reports it and the executor denies without a request."""
    app, store, evidence = _workbench()
    target = store.register_target(TARGET_ID)
    _release(store, target)
    incident, run = _submit(app)
    lease = store.claim(incident, run, uuid4(), otel_demo_versions())
    opener = FakeOpener(
        by_query={GOOD_EXPR: b'{"status":"success","data":{"result":[]}}'}
    )
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: opener)
    factory = otel_demo_executor_factory(
        store,
        evidence=RecordingSink(),
        clock=FakeClock(start=NOW),
        config=OtelDemoConfig(prometheus_url=PROMETHEUS_URL, jaeger_url=JAEGER_URL),
    )
    input = otel_demo_face(FakeClock(start=WINDOW_END)).input_for(
        run_id=str(run),
        question="Why is checkout erroring?",
        target_id=TARGET_ID,
        deadline=NOW + timedelta(minutes=10),
        model_requests=2,
    )
    executor = factory(lease, input)
    assert DurableControl(store).snapshot(executor.scope).suspended is False

    with store.transaction() as conn:
        current = conn.execute(
            "SELECT generation FROM opspilot_target_suspensions WHERE target_id=%s",
            (target,),
        ).fetchone()["generation"]
    generation = store.set_target_suspension(
        target, True, expected_generation=int(current), actor="operator"
    )
    try:
        snapshot = DurableControl(store).snapshot(executor.scope)
        assert snapshot.suspended is True
        assert snapshot.target_suspension_generation == generation
        from opspilot.tools import ToolRequest

        outcome = executor.execute(
            ToolRequest(
                step_id="step-1",
                tool_index=0,
                tool_name=METRICS_TOOL,
                target_ref=TARGET_ID,
                params={"expr": GOOD_EXPR},
                window=WINDOW.as_json(),
            )
        )
        assert (outcome.status, outcome.reason) == ("denied", "SUSPENDED")
        assert not opener.called and outcome.operation.sent is False
        # The durable write fence agrees with the executor's own check.
        with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
            store.renew_lease(lease, 30)
    finally:
        _release(store, target)


def test_a_target_suspended_before_claim_keeps_the_run_off_the_queue():
    app, store, evidence = _workbench()
    target = store.register_target(TARGET_ID)
    _release(store, target)
    with store.transaction() as conn:
        row = conn.execute(
            "SELECT generation FROM opspilot_target_suspensions WHERE target_id=%s",
            (target,),
        ).fetchone()
    store.set_target_suspension(
        target, True, expected_generation=int(row["generation"]), actor="operator"
    )
    try:
        incident, run = _submit(app)
        assert store.rebuild(incident)["run"]["state"] == "paused"
        with pytest.raises(PersistenceError):
            store.claim(incident, run, uuid4(), otel_demo_versions())
    finally:
        _release(store, target)
