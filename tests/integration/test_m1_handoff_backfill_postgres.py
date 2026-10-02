"""M1-01 contract tests for repairing a missing parked handoff projection."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from opspilot.acceptance import IncidentScenario, outcome_from_durable
from opspilot.investigation.loop import DISCIPLINE_VARIANT, prompt_revision_versions
from opspilot.investigation.progress import announce_deadline_exceeded, announce_handoff
from opspilot.persistence import DurableStore, PersistenceError
from opspilot.web import DurableEventLog
from tests.integration.test_m1_loop_resume_postgres import _input
from tests.integration.test_m1_web_postgres import _build
from tests.m1_web_support import basic, call

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

VERSIONS = {
    **prompt_revision_versions(DISCIPLINE_VARIANT),
    "tool_schema_revision": "t1",
}


def _accepted(store: DurableStore, tag: str):
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-handoff-backfill-{tag}-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=5),
        budget_limit=10,
        versions=VERSIONS,
        input=_input(run),
    )
    return incident, run


def _events(log: DurableEventLog, incident: UUID, run: UUID):
    return [
        dict(event.payload)
        for event in log.read_after(incident, 0, limit=1000)
        if event.kind == "run_handoff" and event.payload.get("run_id") == str(run)
    ]


def _acceptance_reasons(store: DurableStore, log: DurableEventLog, incident: UUID):
    outcome = outcome_from_durable(
        IncidentScenario(
            scenario_id=f"m1-01-handoff-backfill-{incident}",
            feature_id="M1-01",
            acceptance_step="external IncidentScenario -> IncidentOutcome",
            kind="handoff-backfill",
            subject_id=str(incident),
        ),
        store.rebuild(incident),
        handoff_events=tuple(
            dict(event.payload)
            for event in log.read_after(incident, 0, limit=1000)
            if event.kind == "run_handoff"
        ),
    )
    return outcome.handoff_reasons


def _missing_handoff(stack, *, tag: str, sweep: bool = False):
    incident, run = _accepted(stack.store, tag)
    if sweep:
        with stack.store.transaction() as conn:
            conn.execute(
                "UPDATE opspilot_runs SET deadline=clock_timestamp()-interval '1 second' WHERE run_id=%s",
                (run,),
            )
        assert stack.store.sweep_expired_runs(incident_id=incident) == (
            (incident, run),
        )
    else:
        lease = stack.store.claim(incident, run, uuid4(), VERSIONS, lease_seconds=60)
        stack.store.hand_off(lease)
    assert stack.store.rebuild(incident)["run"]["state"] == "waiting_human"
    assert _events(stack.events, incident, run) == []
    return incident, run


def test_reconcile_backfills_a_runner_parked_run_with_unknown_provenance():
    app, workbench, store = _build()
    stack = type(
        "Stack",
        (),
        {
            "app": app,
            "workbench": workbench,
            "store": store,
            "events": workbench.events,
        },
    )
    incident, run = _missing_handoff(stack, tag="runner")

    before = store.rebuild(incident)["run"]
    snapshot = workbench.snapshot(incident)
    page = call(app, "GET", f"/incidents/{incident}", headers=basic())
    repaired = _events(workbench.events, incident, run)
    assert len(repaired) == 1
    expected = {
        "run_id": str(run),
        "published": False,
        "execution": "unknown",
        "handoff": True,
        "parked": True,
        "reasons": [],
        "report_sha256": None,
        "evidence_ids": [],
        "reconciled": True,
    }
    assert {key: repaired[0][key] for key in expected} == expected
    assert snapshot["run"]["state"] == "waiting_human"
    assert snapshot["outcome"] == repaired[0]
    assert store.rebuild(incident)["run"] == before
    assert page.status == 200
    assert (
        "waiting_human" in page.text
        and "Last attempt: <code>unknown</code>" in page.text
    )


def test_runner_announcer_after_reconcile_shares_one_new_generation_key():
    app, workbench, store = _build()
    stack = type("Stack", (), {"store": store, "events": workbench.events})
    incident, run = _missing_handoff(stack, tag="runner-announcer-race-first")
    workbench.snapshot(incident)
    assert store.control(incident, 0, "resume", "operator") == 1
    lease = store.claim(incident, run, uuid4(), VERSIONS, lease_seconds=60)
    store.hand_off(lease)
    assert store.rebuild(incident)["run"]["state"] == "waiting_human"
    assert len(_events(workbench.events, incident, run)) == 1
    generation = store.rebuild(incident)["run"]["control_generation"]
    assert generation >= 1

    first_page = call(app, "GET", f"/incidents/{incident}", headers=basic())
    assert first_page.status == 200
    announce_handoff(
        workbench.events,
        incident,
        run,
        "failed",
        ("MODEL_UNAVAILABLE",),
        control_generation=generation,
    )

    repaired = _events(workbench.events, incident, run)
    current = [
        event for event in repaired if event.get("control_generation") == generation
    ]
    assert len(current) == 2
    assert all(event["parked"] is True for event in current)
    announcer = next(event for event in current if event["reasons"])
    assert announcer.get("reconciled", False) is False
    assert announcer["reasons"] == ["MODEL_UNAVAILABLE"]
    page = call(app, "GET", f"/incidents/{incident}", headers=basic())
    assert page.status == 200
    assert page.text.count("Last attempt:") == 1
    assert "MODEL_UNAVAILABLE" in page.text
    assert workbench.snapshot(incident)["outcome"]["reasons"] == ["MODEL_UNAVAILABLE"]
    assert _acceptance_reasons(store, workbench.events, incident) == (
        "MODEL_UNAVAILABLE",
    )


def test_reconcile_backfills_a_swept_run_and_is_idempotent():
    app, workbench, store = _build()
    stack = type(
        "Stack",
        (),
        {
            "app": app,
            "workbench": workbench,
            "store": store,
            "events": workbench.events,
        },
    )
    incident, run = _missing_handoff(stack, tag="sweep", sweep=True)

    workbench.snapshot(incident)
    workbench.snapshot(incident)
    repaired = _events(workbench.events, incident, run)
    assert len(repaired) == 1
    assert isinstance(repaired[0]["reasons"], list)
    assert repaired[0]["reasons"] == []
    assert repaired[0]["execution"] == "unknown"


def test_sweeper_announcer_after_reconcile_shares_one_generation_key():
    app, workbench, store = _build()
    stack = type("Stack", (), {"store": store, "events": workbench.events})
    incident, run = _missing_handoff(stack, tag="sweep-announcer-race", sweep=True)
    generation = store.rebuild(incident)["run"]["control_generation"]

    first_page = call(app, "GET", f"/incidents/{incident}", headers=basic())
    assert first_page.status == 200
    announce_deadline_exceeded(workbench.events, incident, run, generation)

    repaired = _events(workbench.events, incident, run)
    assert len(repaired) == 2
    announcer = next(event for event in repaired if event["reasons"])
    backfill = next(event for event in repaired if not event["reasons"])
    assert backfill["reconciled"] is True
    assert announcer.get("reconciled", False) is False
    assert announcer["reasons"] == ["DEADLINE_EXCEEDED"]
    page = call(app, "GET", f"/incidents/{incident}", headers=basic())
    assert page.status == 200
    assert page.text.count("Last attempt:") == 1
    assert "DEADLINE_EXCEEDED" in page.text
    assert workbench.snapshot(incident)["outcome"]["reasons"] == ["DEADLINE_EXCEEDED"]
    assert _acceptance_reasons(store, workbench.events, incident) == (
        "DEADLINE_EXCEEDED",
    )


def test_reconcile_backfills_second_park_for_new_generation_only_once():
    app, workbench, store = _build()
    stack = type("Stack", (), {"store": store, "events": workbench.events})
    incident, run = _missing_handoff(stack, tag="new-generation-first")
    workbench.snapshot(incident)
    assert len(_events(workbench.events, incident, run)) == 1

    assert store.control(incident, 0, "resume", "operator") == 1
    lease = store.claim(incident, run, uuid4(), VERSIONS, lease_seconds=60)
    store.hand_off(lease)
    assert store.rebuild(incident)["run"]["state"] == "waiting_human"
    assert store.rebuild(incident)["run"]["control_generation"] >= 1
    assert len(_events(workbench.events, incident, run)) == 1

    workbench.snapshot(incident)
    workbench.snapshot(incident)
    workbench.snapshot(incident)
    repaired = _events(workbench.events, incident, run)
    assert len(repaired) == 2
    assert all(event["parked"] is True for event in repaired)
    assert repaired[1]["execution"] == "unknown"
    assert repaired[1]["reasons"] == []
    assert repaired[1]["report_sha256"] is None
    assert repaired[1]["evidence_ids"] == []
    assert repaired[1]["reconciled"] is True


def test_reconcile_ignores_current_generation_conclusion_provenance():
    app, workbench, store = _build()
    incident, run = _accepted(store, "current-generation-conclusion")
    lease = store.claim(incident, run, uuid4(), VERSIONS, lease_seconds=60)
    store.commit_step(
        lease, "final", {"kind": "conclusion", "content": "handoff context"}
    )
    store.hand_off(lease)
    assert store.rebuild(incident)["run"]["state"] == "waiting_human"

    workbench.snapshot(incident)
    repaired = _events(workbench.events, incident, run)
    assert len(repaired) == 1
    assert repaired[0]["execution"] == "unknown"
    assert repaired[0]["reasons"] == []
    assert repaired[0]["report_sha256"] is None
    assert repaired[0]["evidence_ids"] == []


def test_reconcile_does_not_duplicate_an_already_announced_park():
    app, workbench, store = _build()
    stack = type(
        "Stack",
        (),
        {
            "app": app,
            "workbench": workbench,
            "store": store,
            "events": workbench.events,
        },
    )
    incident, run = _missing_handoff(stack, tag="already-announced")
    generation = store.rebuild(incident)["run"]["control_generation"]
    announce_handoff(
        workbench.events,
        incident,
        run,
        "failed",
        ("MODEL_UNAVAILABLE",),
        control_generation=generation,
    )

    workbench.snapshot(incident)
    announce_handoff(
        workbench.events,
        incident,
        run,
        "failed",
        ("MODEL_UNAVAILABLE",),
        control_generation=generation,
    )
    workbench.snapshot(incident)
    repaired = _events(workbench.events, incident, run)
    assert len(repaired) == 1
    assert repaired[0].get("reconciled", False) is False
    assert repaired[0]["reasons"] == ["MODEL_UNAVAILABLE"]


@pytest.mark.parametrize("state", ["queued", "running", "cancelled", "completed"])
def test_reconcile_does_not_backfill_a_non_waiting_run(state):
    app, workbench, store = _build()
    incident, run = _accepted(store, f"state-{state}")
    if state == "running":
        store.claim(incident, run, uuid4(), VERSIONS, lease_seconds=60)
    elif state == "cancelled":
        store.control(incident, 0, "cancel", "operator")
    elif state == "completed":
        lease = store.claim(incident, run, uuid4(), VERSIONS, lease_seconds=60)
        step = store.commit_step(
            lease, "final", {"kind": "conclusion", "content": "done"}
        )
        assert store.publish(
            lease, {"kind": "conclusion", "content": "done"}, step_id=step
        )
    workbench.snapshot(incident)
    assert store.rebuild(incident)["run"]["state"] == state
    assert _events(workbench.events, incident, run) == []


def test_reconcile_failure_does_not_make_the_incident_page_fail(monkeypatch):
    app, workbench, store = _build()
    incident, run = _missing_handoff(
        type(
            "Stack",
            (),
            {
                "app": app,
                "workbench": workbench,
                "store": store,
                "events": workbench.events,
            },
        ),
        tag="write-failure",
    )
    real_append = workbench.events.append_once

    def fail(*args, **kwargs):
        raise PersistenceError("append unavailable")

    monkeypatch.setattr(workbench.events, "append_once", fail)
    response = call(app, "GET", f"/incidents/{incident}", headers=basic())
    assert response.status == 200
    assert "waiting_human" in response.text
    monkeypatch.setattr(workbench.events, "append_once", real_append)


def test_page_reconcile_preserves_deadline_reason_after_parked_run_generation_advances():
    app, workbench, store = _build()
    stack = type("Stack", (), {"store": store, "events": workbench.events})
    incident, run = _missing_handoff(stack, tag="generation-deadline")

    assert store.control(incident, 0, "resume", "operator") == 1
    assert store.rebuild(incident)["run"]["state"] == "queued"
    assert store.control(incident, 1, "pause", "operator") == 2
    assert store.rebuild(incident)["run"]["state"] == "queued"

    with store.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_runs SET deadline=clock_timestamp()-interval '1 second' WHERE run_id=%s",
            (run,),
        )

    page = call(app, "GET", f"/incidents/{incident}", headers=basic())
    assert page.status == 200
    handoffs = _events(workbench.events, incident, run)
    # The gen0 park's event was lost and the Run left waiting_human (resume)
    # before any reconcile, so contract 1 never backfills it: only the
    # deadline park remains, and the page reads its real reason.
    assert len(handoffs) == 1
    assert handoffs[-1]["reasons"] == ["DEADLINE_EXCEEDED"]
    assert handoffs[-1]["parked"] is True


def test_sweeper_generation_is_shared_by_reconcile_backfill_and_late_announcer():
    app, workbench, store = _build()
    incident, run = _accepted(store, "generation-sweeper")
    assert store.control(incident, 0, "pause", "operator") == 1
    assert store.rebuild(incident)["run"]["state"] == "queued"

    with store.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_runs SET deadline=clock_timestamp()-interval '1 second' WHERE run_id=%s",
            (run,),
        )
    parked = store.sweep_expired_runs_with_generations(incident_id=incident)
    assert parked == ((incident, run, 1),)
    assert _events(workbench.events, incident, run) == []

    page = call(app, "GET", f"/incidents/{incident}", headers=basic())
    assert page.status == 200
    announce_deadline_exceeded(workbench.events, incident, run, parked[0][2])
    announce_deadline_exceeded(workbench.events, incident, run, parked[0][2])
    handoffs = _events(workbench.events, incident, run)
    # r3-A: the backfill keyed by the sweeper's generation does not block the
    # late sweeper, and the sweeper's own write is still deduplicated.
    assert len(handoffs) == 2
    assert [h.get("reconciled") for h in handoffs] == [True, False]
    assert handoffs[-1]["reasons"] == ["DEADLINE_EXCEEDED"]
    assert all(h["parked"] is True for h in handoffs)
