"""A note on a timed-out Run starts a new Run (user decision 2026-09-25).

After the deadline sweep parks a Run (``waiting_human``, ``DEADLINE_EXCEEDED``),
a follow_up or correct cannot continue that Run: its deadline fences every
claim. Mirroring upstream HolmesGPT (a message after TIMEOUT is a new
request), ``control()`` records the note and starts a fresh Run with a new
deadline exactly as ``new_run`` does; the new Run sees every prior input.
A note on a Run that is not overdue keeps today's behaviour (re-queue).
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from opspilot.investigation.context import InvestigationInput, continuation_context
from opspilot.persistence import DurableStore, PersistenceError
from opspilot.web import (
    DurableEventLog,
    DurableEvidenceStore,
    DurableIncidentStore,
    DurableWebLedger,
    Workbench,
)
from scripts.m0.postgres_lab import DSN
from tests.integration.test_m1_deadline_sweep_postgres import (
    VERSIONS,
    _accepted,
    _expire,
    _handoffs,
    _run_row,
)
from tests.integration.test_m1_loop_resume_postgres import Harness, _tool_rounds
from tests.m1_investigation_support import report_from_transcript

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)


def _store() -> DurableStore:
    store = DurableStore(DSN)
    store.install()
    return store


def _fresh_deadline() -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=5)


def _successor_input(store: DurableStore, incident: UUID, run_id: UUID) -> dict:
    """The fresh Run's own input: C3 continuation of the timed-out Run's
    context (carried evidence re-bound to the new run id, handoff note)."""
    snapshot = store.rebuild(incident)
    previous = InvestigationInput.from_json(snapshot["run"]["input"])
    cont = continuation_context(
        snapshot,
        new_run_id=str(run_id),
        authorized_targets=frozenset({"checkout-prod"}),
    )
    return replace(
        previous,
        question=f"{previous.question}\n\n{cont.handoff_note}",
        evidence_context=cont.evidence_context,
    ).as_json()


def _note(store: DurableStore, incident: UUID, action: str, generation: int) -> UUID:
    """Apply ``action`` with a note and the renewal a timed-out Run needs."""
    renewal = uuid4()
    assert (
        store.control(
            incident,
            generation,
            action,
            "operator",
            {"text": "Also compare against the previous hour.", "channel": "web"},
            renew_run_id=renewal,
            renew_deadline=_fresh_deadline(),
            renew_input=_successor_input(store, incident, renewal),
        )
        == generation + 1
    )
    return renewal


@pytest.mark.parametrize("action", ["follow_up", "correct"])
def test_a_note_on_a_timed_out_run_starts_a_new_run_that_sees_it(action):
    h = Harness()
    store, incident, old = h.store, h.incident, h.run
    store.claim(incident, old, uuid4(), VERSIONS, lease_seconds=300)
    _expire(store, old)
    assert store.sweep_expired_runs(incident_id=incident) == ((incident, old),)
    fresh = _note(store, incident, action, 0)
    rows = store.rebuild(incident)
    assert rows["run"]["run_id"] == fresh and rows["run"]["state"] == "queued"
    assert rows["run"]["control_generation"] == 1
    assert rows["run"]["deadline"] > datetime.now(timezone.utc)
    assert rows["run"]["input"]["evidence_context"]["run_id"] == str(fresh)
    assert rows["conclusion"] is None and rows["state"] == "running"
    old_row = _run_row(store, old)
    assert old_row["state"] == "cancelled"
    assert old_row["owner"] is None and old_row["lease_until"] is None
    inputs = store.read_inputs(incident)
    assert [(i["kind"], i["control_generation"]) for i in inputs] == [(action, 1)]
    # No dead row: the new Run is claimed and investigates, and the note is
    # in the round's frozen inputs (the new Run sees every prior input).
    outcome = h.runner([*_tool_rounds(1), report_from_transcript]).resume(incident)
    assert outcome.status == "published", outcome
    with store.transaction(snapshot=True) as conn:
        frozen = conn.execute(
            "SELECT input_watermark FROM opspilot_input_rounds WHERE run_id=%s",
            (fresh,),
        ).fetchall()
    assert frozen and all(int(r["input_watermark"]) >= 1 for r in frozen)
    assert store.rebuild(incident)["run"]["state"] == "completed"


def test_a_note_on_a_handoff_that_is_not_overdue_requeues_the_same_run():
    store = _store()
    incident, run = _accepted(store, "not-overdue")
    lease = store.claim(incident, run, uuid4(), VERSIONS, lease_seconds=300)
    store.hand_off(lease)
    assert _run_row(store, run)["state"] == "waiting_human"
    renewal = uuid4()
    assert (
        store.control(
            incident,
            0,
            "follow_up",
            "operator",
            {"text": "x", "channel": "web"},
            renew_run_id=renewal,
            renew_deadline=_fresh_deadline(),
        )
        == 1
    )
    rows = store.rebuild(incident)
    assert rows["run"]["run_id"] == run and rows["run"]["state"] == "queued"
    with store.transaction(snapshot=True) as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM opspilot_runs WHERE run_id=%s", (renewal,)
            ).fetchone()
            is None
        )


def test_cancel_still_works_after_a_timeout_and_after_a_renewal():
    store = _store()
    incident, old = _accepted(store, "cancel")
    store.claim(incident, old, uuid4(), VERSIONS, lease_seconds=300)
    _expire(store, old)
    store.sweep_expired_runs(incident_id=incident)
    fresh = _note(store, incident, "follow_up", 0)
    assert store.control(incident, 1, "cancel", "operator") == 2
    assert _run_row(store, fresh)["state"] == "cancelled"
    assert _run_row(store, old)["state"] == "cancelled"
    assert store.rebuild(incident)["state"] == "cancelled"


def test_a_note_racing_the_sweep_ends_in_one_new_run_either_way():
    store = _store()
    incident, old = _accepted(store, "race")
    store.claim(incident, old, uuid4(), VERSIONS, lease_seconds=300)
    _expire(store, old)
    fresh = uuid4()

    def note(_):
        try:
            return store.control(
                incident,
                0,
                "follow_up",
                "operator",
                {"text": "x", "channel": "web"},
                renew_run_id=fresh,
                renew_deadline=_fresh_deadline(),
            )
        except PersistenceError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        sweep = pool.submit(store.sweep_expired_runs, incident_id=incident)
        applied = pool.submit(note, None)
        parked, generation = sweep.result(), applied.result()
    assert generation == 1, generation
    assert parked in ((), ((incident, old),))
    rows = store.rebuild(incident)
    assert rows["run"]["run_id"] == fresh and rows["run"]["state"] == "queued"
    assert _run_row(store, old)["state"] == "cancelled"
    assert store.sweep_expired_runs(incident_id=incident) == ()


def test_a_renewal_id_that_already_exists_is_refused_without_moving_anything():
    store = _store()
    incident, old = _accepted(store, "renewal-conflict")
    store.claim(incident, old, uuid4(), VERSIONS, lease_seconds=300)
    _expire(store, old)
    store.sweep_expired_runs(incident_id=incident)
    with pytest.raises(PersistenceError, match="^IDENTITY_CONFLICT$"):
        store.control(
            incident,
            0,
            "follow_up",
            "operator",
            {"text": "x", "channel": "web"},
            renew_run_id=old,
            renew_deadline=_fresh_deadline(),
        )
    assert _run_row(store, old)["state"] == "waiting_human"
    assert store.rebuild(incident)["control_generation"] == 0


def test_the_workbench_note_after_a_timeout_shows_a_new_run_and_no_refusals():
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
    incident, old = _accepted(store, "workbench")
    store.claim(incident, old, uuid4(), VERSIONS, lease_seconds=300)
    _expire(store, old)
    assert workbench.snapshot(incident)["outcome"]["reasons"] == ["DEADLINE_EXCEEDED"]
    result = workbench.control(
        incident,
        actor_id="alice",
        action="follow_up",
        expected_generation=0,
        idempotency_key="after-timeout",
        text="Also compare against the previous hour.",
    )
    assert result.generation == 1
    snapshot = workbench.snapshot(incident)
    assert snapshot["run"]["run_id"] != str(old)
    assert snapshot["run"]["state"] == "queued"
    assert snapshot["run"]["control_generation"] == 1
    assert snapshot["outcome"] is None
    applied = [e for e in log.read_after(incident, 0) if e.kind == "control_applied"]
    assert applied[-1].payload["run_id"] == snapshot["run"]["run_id"]
    assert len(_handoffs(log, incident)) == 1
    # The old Run's park stays on record; nothing is refused per poll.
    assert (
        store.claim(
            incident, UUID(snapshot["run"]["run_id"]), uuid4(), VERSIONS, 300
        ).control_generation
        == 1
    )
    assert not [e for e in log.read_after(incident, 0) if e.kind == "run_claim_refused"]
    # The same key replays the same decision, not a second Run.
    replay = workbench.control(
        incident,
        actor_id="alice",
        action="follow_up",
        expected_generation=0,
        idempotency_key="after-timeout",
        text="Also compare against the previous hour.",
    )
    assert replay.generation == 1 and replay.replayed is True
