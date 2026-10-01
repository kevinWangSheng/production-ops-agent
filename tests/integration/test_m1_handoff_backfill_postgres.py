"""M1-01 contract tests for repairing a missing parked handoff projection."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

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
    assert repaired[0] == {
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
    assert snapshot["run"]["state"] == "waiting_human"
    assert snapshot["outcome"] == repaired[0]
    assert store.rebuild(incident)["run"] == before
    assert page.status == 200
    assert (
        "waiting_human" in page.text
        and "Last attempt: <code>unknown</code>" in page.text
    )


def test_runner_announcer_after_reconcile_shares_one_generation_key():
    app, workbench, store = _build()
    stack = type("Stack", (), {"store": store, "events": workbench.events})
    incident, run = _missing_handoff(stack, tag="runner-announcer-race")
    generation = store.rebuild(incident)["run"]["control_generation"]

    workbench.snapshot(incident)
    announce_handoff(
        workbench.events,
        incident,
        run,
        "failed",
        ("MODEL_UNAVAILABLE",),
        control_generation=generation,
    )

    repaired = _events(workbench.events, incident, run)
    assert len(repaired) == 1
    assert repaired[0]["parked"] is True


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

    workbench.snapshot(incident)
    announce_deadline_exceeded(workbench.events, incident, run, generation)

    repaired = _events(workbench.events, incident, run)
    assert len(repaired) == 1
    assert repaired[0]["parked"] is True


def test_reconcile_backfills_second_park_for_new_generation():
    app, workbench, store = _build()
    stack = type("Stack", (), {"store": store, "events": workbench.events})
    incident, run = _missing_handoff(stack, tag="new-generation-first")
    workbench.snapshot(incident)
    assert len(_events(workbench.events, incident, run)) == 1

    assert store.control(incident, 0, "resume", "operator") == 1
    lease = store.claim(incident, run, uuid4(), VERSIONS, lease_seconds=60)
    store.hand_off(lease)
    assert store.rebuild(incident)["run"]["state"] == "waiting_human"

    workbench.snapshot(incident)
    repaired = _events(workbench.events, incident, run)
    assert len(repaired) == 2
    assert all(event["parked"] is True for event in repaired)


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
    assert len(_events(workbench.events, incident, run)) == 1


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
