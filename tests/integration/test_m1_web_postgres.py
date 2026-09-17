"""Workbench end to end on the real DurableStore: submit, run, control, read back.

Same flows as ``tests/test_m1_web_workbench.py`` but through the durable
adapters, so generation fences, late results and evidence hashes are proven
against committed PostgreSQL rows rather than the in-memory double.
"""

from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest

from opspilot.persistence import DurableStore
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
from tests.m1_investigation_support import reply, report_from_transcript, tool_call
from tests.m1_tool_support import FakeClock
from tests.m1_web_support import (
    EVENT_TOKEN,
    ORIGIN,
    UI_PASSWORD,
    UI_USER,
    ScriptedInvestigator,
    basic,
    bearer,
    call,
    post_form,
    post_json,
    same_origin,
    stream,
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
    incidents = DurableIncidentStore(store)
    workbench = Workbench(
        incidents=incidents,
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
    app = create_app(
        workbench,
        Authenticator(config),
        DurableClock(store),
        sse_poll_seconds=0.02,
        sse_idle_seconds=0.3,
    )
    return app, workbench, store


class _DbClock(FakeClock):
    """Tool/loop clock that tracks the database clock instead of a fixed NOW."""

    def __init__(self, store: DurableStore) -> None:
        super().__init__()
        self._clock = DurableClock(store)

    def now(self):
        return self._clock.now()

    def monotonic(self):
        return self._clock.monotonic()


def test_submit_run_and_read_back_on_postgres():
    app, workbench, store = _build()
    key = f"pg-e2e-{uuid4()}"
    first = post_form(
        app,
        "/intake/ui",
        {
            "target_id": "checkout-prod",
            "question": "Why is checkout erroring?",
            "idempotency_key": key,
        },
        headers={**basic(), **same_origin()},
    )
    assert first.status == 201
    incident = first.json()["incident_id"]
    replay = post_form(
        app,
        "/intake/ui",
        {
            "target_id": "checkout-prod",
            "question": "Why is checkout erroring?",
            "idempotency_key": key,
        },
        headers={**basic(), **same_origin()},
    )
    assert replay.status == 200 and replay.json()["incident_id"] == incident
    conflict = post_form(
        app,
        "/intake/ui",
        {"target_id": "checkout-prod", "question": "other", "idempotency_key": key},
        headers={**basic(), **same_origin()},
    )
    assert conflict.status == 409

    subject = workbench.list_incidents()[0].incident_id
    assert str(subject) == incident
    outcome = workbench.run_once(subject, ScriptedInvestigator(_DbClock(store)))
    assert outcome is not None and outcome.execution == "completed"

    kinds = [e.kind for e in workbench.events.read_after(subject, 0)]
    assert kinds == [
        "intake_accepted",
        "run_claimed",
        "step_committed",
        "tool_committed",
        "step_committed",
        "run_completed",
    ]
    rebuilt = store.rebuild(subject)
    assert rebuilt["run"]["state"] == "completed"
    assert rebuilt["conclusion"] is not None
    assert workbench.list_incidents()[0].concluded is True

    page = call(app, "GET", f"/incidents/{incident}", headers=basic())
    assert page.status == 200 and "Facts (1)" in page.text
    evidence_id = outcome.evidence_ids[0]
    record = call(
        app, "GET", f"/incidents/{incident}/evidence/{evidence_id}", headers=basic()
    )
    assert record.status == 200
    body = record.json()
    assert body["hashes_verified"] is True
    assert json.loads(body["raw_utf8"])["data"]["result"] == [
        {"metric": "checkout", "value": 3}
    ]
    assert "transport-only-marker" not in record.text

    resumed = stream(
        app, f"/incidents/{incident}/events?cursor=4", headers=basic(), until_events=2
    )
    assert [(i, k) for i, k, _ in resumed.sse_events()] == [
        ("5", "step_committed"),
        ("6", "run_completed"),
    ]
    # Concluded: no further control is accepted, generation stays 0.
    late = post_form(
        app,
        f"/incidents/{incident}/control",
        {
            "action": "follow_up",
            "expected_generation": "0",
            "idempotency_key": "l",
            "text": "?",
        },
        headers={**basic(), **same_origin()},
    )
    assert late.status == 409 and late.json() == {"code": "ILLEGAL_TRANSITION"}


def test_control_generations_and_late_results_on_postgres():
    app, workbench, store = _build()
    key = f"pg-ctl-{uuid4()}"
    accepted = post_json(
        app,
        "/intake/events",
        {
            "source": "alertmanager",
            "external_event_id": key,
            "target_id": "checkout-prod",
            "question": "CheckoutErrorRateHigh firing",
        },
        headers=bearer(),
    )
    assert accepted.status == 201
    incident = accepted.json()["incident_id"]
    subject = workbench.list_incidents()[0].incident_id
    assert str(subject) == incident

    def ctl(fields):
        return post_form(
            app,
            f"/incidents/{incident}/control",
            fields,
            headers={**basic(), **same_origin()},
        )

    assert (
        ctl(
            {
                "action": "follow_up",
                "expected_generation": "0",
                "idempotency_key": "f1",
                "text": "Check the canary too.",
            }
        ).json()["generation"]
        == 1
    )
    again = ctl(
        {
            "action": "follow_up",
            "expected_generation": "0",
            "idempotency_key": "f1",
            "text": "Check the canary too.",
        }
    ).json()
    assert again["generation"] == 1 and again["replayed"] is True
    stale = ctl(
        {"action": "pause", "expected_generation": "0", "idempotency_key": "p0"}
    )
    assert stale.status == 409 and stale.json()["current_generation"] == 1
    with store.transaction(snapshot=True) as conn:
        audit = conn.execute(
            "SELECT action,expected_generation,resulting_generation,actor FROM opspilot_controls WHERE incident_id=%s ORDER BY created_at",
            (subject,),
        ).fetchall()
    assert [
        (a["action"], a["expected_generation"], a["resulting_generation"], a["actor"])
        for a in audit
    ] == [("follow_up", 0, 1, UI_USER)]

    # Pause mid-run: the loop is fenced, the late final step is history only.
    def pause_then_answer(call_):
        workbench.control(
            subject,
            actor_id=UI_USER,
            action="pause",
            expected_generation=1,
            idempotency_key="mid",
        )
        return report_from_transcript(call_)

    investigator = ScriptedInvestigator(
        _DbClock(store),
        replies=[
            reply(tool_calls=[tool_call()], finish="tool_calls"),
            pause_then_answer,
        ],
    )
    outcome = workbench.run_once(subject, investigator)
    assert outcome is not None and outcome.execution == "failed"
    assert outcome.handoff_reasons == ("CONTROL_DENIED",)
    snapshot = workbench.snapshot(subject)
    assert snapshot["report"] is None and snapshot["incident"].state == "paused"
    assert [s["status"] for s in snapshot["steps"]] == [
        "tool_result_committed",
        "late_result",
    ]
    assert workbench.events.read_after(subject, 0)[-1].kind == "run_handoff"
    # Follow-up while paused is refused; resume then cancel then new Run.
    assert (
        ctl(
            {
                "action": "follow_up",
                "expected_generation": "2",
                "idempotency_key": "f2",
                "text": "?",
            }
        ).status
        == 409
    )
    assert (
        ctl(
            {"action": "resume", "expected_generation": "2", "idempotency_key": "r"}
        ).json()["generation"]
        == 3
    )
    assert (
        ctl(
            {"action": "cancel", "expected_generation": "3", "idempotency_key": "c"}
        ).json()["generation"]
        == 4
    )
    renewed = ctl(
        {"action": "new_run", "expected_generation": "4", "idempotency_key": "n"}
    )
    assert renewed.status == 200 and renewed.json()["generation"] == 5
    summary = workbench.list_incidents()[0]
    assert summary.state == "queued" and summary.control_generation == 5
    assert len(workbench.incidents.run_ids(subject)) == 2
    # The renewed Run sees the follow-up text and completes.
    fresh = ScriptedInvestigator(_DbClock(store))
    outcome = workbench.run_once(subject, fresh)
    assert outcome is not None and outcome.execution == "completed"
    assert "Check the canary too." in fresh.contexts[0].question
    assert workbench.snapshot(subject)["report"]["facts"]
