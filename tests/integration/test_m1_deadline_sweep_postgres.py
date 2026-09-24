"""ADR-0005 decision 2 on real PostgreSQL: an overdue ``running`` Run is swept.

A Run past its ``deadline`` can never settle on its own: every worker write
is fenced by the deadline, so without a sweep the row stays ``running`` for
ever. The sweep parks it exactly like ``hand_off`` (``waiting_human``, lease
released, incident open, conclusion NULL), using the database clock and an
in-transaction re-check, and never over a human decision that landed first.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from opspilot.investigation.loop import DISCIPLINE_VARIANT, prompt_revision_versions
from opspilot.investigation.progress import announce_deadline_exceeded, sweep_expired
from opspilot.investigation.runner import InvestigationRunner
from opspilot.persistence import DurableStore, PersistenceError
from opspilot.web import (
    DurableEventLog,
    DurableEvidenceStore,
    DurableIncidentStore,
    DurableWebLedger,
    Workbench,
)
from opspilot.worker import Worker
from scripts.m0.postgres_lab import DSN
from tests.integration.test_m1_loop_resume_postgres import SystemClock, _input
from tests.m1_investigation_support import ScriptedModel

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

VERSIONS = {
    **prompt_revision_versions(DISCIPLINE_VARIANT),
    "tool_schema_revision": "t1",
}


def _store() -> DurableStore:
    store = DurableStore(DSN)
    store.install()
    return store


def _accepted(store: DurableStore, tag: str, *, minutes: int = 5) -> tuple[UUID, UUID]:
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-sweep-{tag}-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=minutes),
        budget_limit=10,
        versions=VERSIONS,
        input=_input(run),
    )
    return incident, run


def _expire(store: DurableStore, run: UUID) -> None:
    with store.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_runs SET deadline=clock_timestamp()-interval '1 second' WHERE run_id=%s",
            (run,),
        )


def _run_row(store: DurableStore, run: UUID) -> dict:
    with store.transaction(snapshot=True) as conn:
        row = conn.execute(
            "SELECT state,owner,lease_until,deadline FROM opspilot_runs WHERE run_id=%s",
            (run,),
        ).fetchone()
    assert row is not None
    return row


def handoffs_seq(log: DurableEventLog, incident: UUID) -> int:
    return next(
        e.sequence
        for e in log.read_after(incident, 0, limit=1000)
        if e.kind == "run_handoff"
    )


def _handoffs(log: DurableEventLog, incident: UUID) -> list[dict]:
    return [
        dict(e.payload)
        for e in log.read_after(incident, 0, limit=1000)
        if e.kind == "run_handoff"
    ]


def test_an_overdue_running_run_is_parked_like_a_handoff():
    store = _store()
    incident, run = _accepted(store, "overdue")
    lease = store.claim(incident, run, uuid4(), VERSIONS, lease_seconds=300)
    step = store.commit_step(lease, "round-0", {"tool_calls": [{"id": "a"}]})
    _expire(store, run)
    assert store.sweep_expired_runs(incident_id=incident) == ((incident, run),)
    row = _run_row(store, run)
    assert row["state"] == "waiting_human"
    assert row["owner"] is None and row["lease_until"] is None
    rebuilt = store.rebuild(incident)
    assert rebuilt["conclusion"] is None and rebuilt["state"] != "completed"
    # The worker that still holds the (dead) lease lands as history only.
    with pytest.raises(PersistenceError, match="^CONTROL_DENIED$"):
        store.commit_step(lease, "late-after-sweep", {"tool_calls": []})
    late = [s for s in store.rebuild(incident)["steps"] if s["status"] == "late_result"]
    assert len(late) == 1 and late[0]["response"] == {"tool_calls": []}
    with pytest.raises(PersistenceError, match="^CONTROL_DENIED$"):
        store.hand_off(lease)
    assert store.publish(lease, {"result": "late"}, step_id=step) is False
    assert store.rebuild(incident)["conclusion"] is None
    assert _run_row(store, run)["state"] == "waiting_human"


def test_a_run_that_is_not_yet_due_is_untouched():
    store = _store()
    incident, run = _accepted(store, "not-due")
    store.claim(incident, run, uuid4(), VERSIONS, lease_seconds=300)
    assert store.sweep_expired_runs(incident_id=incident) == ()
    assert _run_row(store, run)["state"] == "running"


@pytest.mark.parametrize("action", ["cancel", "pause", "follow_up"])
def test_a_human_decision_that_landed_first_is_never_overwritten(action):
    store = _store()
    incident, run = _accepted(store, action)
    store.claim(incident, run, uuid4(), VERSIONS, lease_seconds=300)
    _expire(store, run)
    store.control(incident, 0, action, "operator")
    before = _run_row(store, run)["state"]
    assert before in {"cancelled", "paused", "queued"}
    assert store.sweep_expired_runs(incident_id=incident) == ()
    assert _run_row(store, run)["state"] == before
    assert store.rebuild(incident)["control_generation"] == 1


def test_a_new_generation_run_replaces_the_expired_one_and_is_not_swept():
    store = _store()
    incident, run = _accepted(store, "new-run")
    store.claim(incident, run, uuid4(), VERSIONS, lease_seconds=300)
    _expire(store, run)
    store.control(incident, 0, "cancel", "operator")
    fresh = uuid4()
    store.new_run(
        incident,
        fresh,
        expected_generation=1,
        deadline=datetime.now(timezone.utc) + timedelta(minutes=5),
        budget_limit=10,
        versions=VERSIONS,
        actor="operator",
    )
    store.claim(incident, fresh, uuid4(), VERSIONS, lease_seconds=300)
    assert store.sweep_expired_runs(incident_id=incident) == ()
    assert _run_row(store, run)["state"] == "cancelled"
    assert _run_row(store, fresh)["state"] == "running"


def test_the_sweep_is_idempotent_and_safe_to_run_concurrently():
    store = _store()
    incident, run = _accepted(store, "concurrent")
    store.claim(incident, run, uuid4(), VERSIONS, lease_seconds=300)
    _expire(store, run)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(lambda _: store.sweep_expired_runs(incident_id=incident), range(4))
        )
    assert sum(len(parked) for parked in results) == 1
    assert store.sweep_expired_runs(incident_id=incident) == ()
    assert _run_row(store, run)["state"] == "waiting_human"


def test_the_unscoped_sweep_parks_every_overdue_run_and_nothing_else():
    store = _store()
    overdue = [_accepted(store, f"all-{i}") for i in range(2)]
    fresh_incident, fresh_run = _accepted(store, "all-fresh")
    for incident, run in overdue:
        store.claim(incident, run, uuid4(), VERSIONS, lease_seconds=300)
        _expire(store, run)
    store.claim(fresh_incident, fresh_run, uuid4(), VERSIONS, lease_seconds=300)
    parked = set(store.sweep_expired_runs())
    assert set(overdue) <= parked and (fresh_incident, fresh_run) not in parked
    assert _run_row(store, fresh_run)["state"] == "running"
    for _, run in overdue:
        assert _run_row(store, run)["state"] == "waiting_human"


def test_follow_up_cancel_and_new_run_work_after_a_timeout_handoff():
    store = _store()
    incident, run = _accepted(store, "control-after")
    store.claim(incident, run, uuid4(), VERSIONS, lease_seconds=300)
    _expire(store, run)
    assert store.sweep_expired_runs(incident_id=incident) == ((incident, run),)
    # follow_up re-queues the parked Run under the next generation; the Run
    # keeps its deadline, so a claim is refused until a human starts a new Run.
    assert store.control(incident, 0, "follow_up", "operator", {"question": "x"}) == 1
    assert _run_row(store, run)["state"] == "queued"
    with pytest.raises(PersistenceError, match="^DEADLINE_EXCEEDED$"):
        store.claim(incident, run, uuid4(), VERSIONS, lease_seconds=300)
    assert store.sweep_expired_runs(incident_id=incident) == ()
    assert store.control(incident, 1, "cancel", "operator") == 2
    fresh = uuid4()
    assert (
        store.new_run(
            incident,
            fresh,
            expected_generation=2,
            deadline=datetime.now(timezone.utc) + timedelta(minutes=5),
            budget_limit=10,
            versions=VERSIONS,
            actor="operator",
        )
        == 3
    )
    lease = store.claim(incident, fresh, uuid4(), VERSIONS, lease_seconds=300)
    assert lease.run_id == fresh and _run_row(store, fresh)["state"] == "running"


def test_the_runner_sweeps_before_it_claims_and_announces_the_timeout_once():
    store = _store()
    log = DurableEventLog(store)
    log.install()
    incident, run = _accepted(store, "runner")
    store.claim(incident, run, uuid4(), VERSIONS, lease_seconds=300)
    _expire(store, run)

    def never(lease, input):  # pragma: no cover - the sweep must come first
        raise AssertionError("an overdue Run must not be claimed")

    runner = InvestigationRunner(
        store=store,
        worker=Worker.create(store, dict(VERSIONS)),
        model=ScriptedModel([]),
        executor_factory=never,
        clock=SystemClock(),
        lease_seconds=1,
        events=log,
    )
    outcome = runner.resume(incident)
    assert outcome.status == "handed_off" and outcome.reason == "DEADLINE_EXCEEDED"
    assert _run_row(store, run)["state"] == "waiting_human"
    handoffs = _handoffs(log, incident)
    assert len(handoffs) == 1
    assert handoffs[0]["run_id"] == str(run)
    assert handoffs[0]["parked"] is True
    assert handoffs[0]["reasons"] == ["DEADLINE_EXCEEDED"]
    assert handoffs[0]["published"] is False and handoffs[0]["handoff"] is True
    # A further poll neither claims nor announces again.
    again = runner.resume(incident)
    assert again.status == "handed_off" and again.reason == "AWAITING_HUMAN"
    assert len(_handoffs(log, incident)) == 1
    # A further sweep parks nothing and announces nothing; the announcement
    # is keyed by run so an announcer racing this one cannot duplicate it.
    assert sweep_expired(store, log, incident_id=incident) == ()
    assert announce_deadline_exceeded(log, incident, run) == handoffs_seq(log, incident)
    assert len(_handoffs(log, incident)) == 1


def test_the_workbench_reconciles_an_overdue_run_into_a_visible_timeout():
    store = _store()
    log = DurableEventLog(store)
    log.install()
    evidence = DurableEvidenceStore(store)
    evidence.install()
    ledger = DurableWebLedger(store)
    ledger.install()
    workbench = Workbench(
        incidents=DurableIncidentStore(store),
        events=log,
        evidence=evidence,
        ledger=ledger,
        run_versions=dict(VERSIONS),
        run_seconds=600,
    )
    incident, run = _accepted(store, "workbench")
    store.claim(incident, run, uuid4(), VERSIONS, lease_seconds=300)
    _expire(store, run)
    snapshot = workbench.snapshot(incident)
    assert snapshot["run"]["state"] == "waiting_human"
    assert snapshot["outcome"] is not None
    assert snapshot["outcome"]["handoff"] is True
    assert snapshot["outcome"]["reasons"] == ["DEADLINE_EXCEEDED"]
    assert len(_handoffs(log, incident)) == 1
    workbench.snapshot(incident)
    assert len(_handoffs(log, incident)) == 1
