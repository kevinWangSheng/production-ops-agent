"""Contract tests for M1-01 3b (web submit -> resident worker -> real runner).

Written from the contract only (ROADMAP M1-01 3b, ADR-0005, C3 §5-7/§13,
PRODUCT-CONSTRAINTS) by an author who did not read the implementation:

1. every Run the workbench creates records an input snapshot the real driver
   can rebuild from (intake, cancel + new_run, and the #47 timeout renewal);
2. the worker loop sweeps on every poll before it claims, resumes what the
   queue lists, survives one Run's failure and stops before the next claim;
3. ``resume`` on an overdue Run is refused with ``ILLEGAL_TRANSITION`` and
   changes nothing; on a parked Run within its deadline it re-queues.

The memory doubles and fakes here never call a model or PostgreSQL; the
end-to-end half lives in
``tests/integration/test_m1_web_worker_contract_postgres.py``.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from uuid import UUID, uuid4

import pytest

from opspilot.investigation.context import InvestigationInput
from opspilot.investigation.limits import M1_FROZEN_LIMITS
from opspilot.investigation.runner import RunnerOutcome
from opspilot.persistence import PersistenceError
from opspilot.tools.fixture import FIXTURE_TARGET, FIXTURE_TOOL, fixture_face
from opspilot.web import MemoryEventLog
from opspilot.worker_main import WorkerLoop
from tests.m1_web_support import (
    EVENT_TOKEN,
    UI_PASSWORD,
    UI_USER,
    basic,
    build_workbench,
    call,
    post_form,
    same_origin,
)

QUESTION = "Why is checkout erroring?"


# -- helpers ------------------------------------------------------------------


def _workbench():
    return build_workbench(tool_face=fixture_face())


def _submit(app, *, key, target=FIXTURE_TARGET, question=QUESTION):
    response = post_form(
        app,
        "/intake/ui",
        {"target_id": target, "question": question, "idempotency_key": key},
        headers={**basic(), **same_origin()},
    )
    assert response.status == 201, response.text
    body = response.json()
    return UUID(body["incident_id"]), UUID(body["run_id"])


def _control(app, incident, fields):
    return post_form(
        app,
        f"/incidents/{incident}/control",
        fields,
        headers={**basic(), **same_origin()},
    )


def _current(workbench, incident):
    summary = workbench.incidents.find_incident(incident)
    assert summary is not None
    return summary


def _assert_snapshot(row_input, *, run_id, question, target):
    """The recorded input rebuilds and names this Run, this question, this target."""
    assert row_input is not None, "Run was created without an input snapshot"
    rebuilt = InvestigationInput.from_json(row_input)
    assert rebuilt.question.startswith(question)
    assert rebuilt.evidence_context is not None
    assert rebuilt.evidence_context["run_id"] == str(run_id)
    assert rebuilt.limits.within(M1_FROZEN_LIMITS)
    assert rebuilt.bound_target_id == target
    return rebuilt


def _overdue_parked(app, workbench, clock, *, key):
    """A Run claimed by a worker that never finished, swept past its deadline."""
    incident, run_id = _submit(app, key=key)
    versions = workbench.incidents.runs[run_id]["versions"]
    workbench.incidents.claim(incident, run_id, uuid4(), versions, 600)
    clock.advance(601)
    snapshot = workbench.snapshot(incident)
    assert snapshot["run"]["state"] == "waiting_human"
    return incident, run_id


# -- 1. input snapshot on every Run the workbench creates ---------------------


def test_intake_run_records_a_rebuildable_input_snapshot():
    """Clause 1: POST /intake/ui records an input the real driver rebuilds from."""
    app, workbench, _ = _workbench()
    incident, run_id = _submit(app, key="snap-intake")
    assert _current(workbench, incident).current_run_id == run_id
    rebuilt = _assert_snapshot(
        workbench.incidents.runs[run_id]["input"],
        run_id=run_id,
        question=QUESTION,
        target=FIXTURE_TARGET,
    )
    assert rebuilt.question == QUESTION


def test_intake_snapshot_binds_the_submitted_target_verbatim():
    """Clause 1: bound_target_id is the intake target_id, never silently rewritten."""
    app, workbench, _ = _workbench()
    _, run_id = _submit(app, key="snap-other-target", target="payments-prod")
    _assert_snapshot(
        workbench.incidents.runs[run_id]["input"],
        run_id=run_id,
        question=QUESTION,
        target="payments-prod",
    )


def test_new_run_successor_records_its_own_input_snapshot():
    """Clause 1: control new_run creates a Run whose input names the new Run."""
    app, workbench, _ = _workbench()
    incident, first = _submit(app, key="snap-new-run")
    cancelled = _control(
        app,
        incident,
        {"action": "cancel", "expected_generation": "0", "idempotency_key": "c"},
    )
    assert cancelled.status == 200, cancelled.text
    renewed = _control(
        app,
        incident,
        {"action": "new_run", "expected_generation": "1", "idempotency_key": "n"},
    )
    assert renewed.status == 200, renewed.text
    fresh = _current(workbench, incident).current_run_id
    assert fresh != first
    _assert_snapshot(
        workbench.incidents.runs[fresh]["input"],
        run_id=fresh,
        question=QUESTION,
        target=FIXTURE_TARGET,
    )


@pytest.mark.parametrize("action", ["follow_up", "correct"])
def test_timeout_renewal_successor_records_its_own_input_snapshot(action):
    """Clause 1/3 (#47): a note on a timed-out Run starts a Run with a recorded input."""
    app, workbench, clock = _workbench()
    incident, old = _overdue_parked(app, workbench, clock, key=f"snap-renew-{action}")
    noted = _control(
        app,
        incident,
        {
            "action": action,
            "expected_generation": "0",
            "idempotency_key": f"note-{action}",
            "text": "Also check the dependency.",
        },
    )
    assert noted.status == 200, noted.text
    fresh = _current(workbench, incident).current_run_id
    assert fresh != old
    assert workbench.incidents.runs[fresh]["state"] == "queued"
    rebuilt = _assert_snapshot(
        workbench.incidents.runs[fresh]["input"],
        run_id=fresh,
        question=QUESTION,
        target=FIXTURE_TARGET,
    )
    # The successor's deadline is its own, not the expired one it replaces.
    assert workbench.incidents.runs[fresh]["deadline"] > clock.now()
    assert rebuilt.evidence_context["run_id"] != str(old)


def test_recorded_inputs_carry_only_the_read_only_tool_face_and_no_credentials():
    """Clause 4: snapshots, events and pages expose no tool beyond the read-only face and no credential."""
    app, workbench, clock = _workbench()
    incident, run_id = _overdue_parked(app, workbench, clock, key="snap-secrets")
    _control(
        app,
        incident,
        {
            "action": "follow_up",
            "expected_generation": "0",
            "idempotency_key": "secret-note",
            "text": "Look again.",
        },
    )
    runs = [
        r for r in workbench.incidents.runs.values() if r["incident_id"] == incident
    ]
    assert len(runs) == 2
    for run in runs:
        rebuilt = InvestigationInput.from_json(run["input"])
        names = [schema["function"]["name"] for schema in rebuilt.tool_schemas]
        assert names == [FIXTURE_TOOL]
    events = workbench.events.read_after(incident, 0, limit=1000)
    page = call(app, "GET", f"/incidents/{incident}", headers=basic())
    assert page.status == 200
    basic_token = base64.b64encode(f"{UI_USER}:{UI_PASSWORD}".encode()).decode()
    exposed = (
        json.dumps(
            {
                "inputs": [r["input"] for r in runs],
                "events": [dict(e.payload) for e in events],
            },
            default=str,
        )
        + page.text
    )
    for secret in (UI_PASSWORD, basic_token, EVENT_TOKEN):
        assert secret not in exposed
    assert run_id in {r["run_id"] for r in runs}


# -- 3. resume on an overdue Run ----------------------------------------------


def _resume(app, incident, generation, key):
    return _control(
        app,
        incident,
        {
            "action": "resume",
            "expected_generation": str(generation),
            "idempotency_key": key,
        },
    )


def test_resume_on_a_swept_overdue_run_is_refused_and_changes_nothing():
    """Clause 3: resume on an overdue (swept) Run -> 409 ILLEGAL_TRANSITION, row untouched."""
    app, workbench, clock = _workbench()
    incident, run_id = _overdue_parked(app, workbench, clock, key="resume-swept")
    before = dict(workbench.incidents.runs[run_id])
    refused = _resume(app, incident, 0, "r-swept")
    assert refused.status == 409
    assert refused.json() == {"code": "ILLEGAL_TRANSITION"}
    summary = _current(workbench, incident)
    assert summary.control_generation == 0
    assert summary.current_run_id == run_id
    assert workbench.incidents.runs[run_id] == before
    assert not [a for a in workbench.incidents.controls if a["action"] == "resume"]


def test_resume_on_an_overdue_paused_run_is_refused_and_it_stays_paused():
    """Clause 3: a paused Run whose deadline passed is not re-queued by resume."""
    app, workbench, clock = _workbench()
    incident, run_id = _submit(app, key="resume-paused-overdue")
    versions = workbench.incidents.runs[run_id]["versions"]
    workbench.incidents.claim(incident, run_id, uuid4(), versions, 600)
    paused = _control(
        app,
        incident,
        {"action": "pause", "expected_generation": "0", "idempotency_key": "p"},
    )
    assert paused.status == 200, paused.text
    clock.advance(601)
    refused = _resume(app, incident, 1, "r-paused")
    assert refused.status == 409
    assert refused.json() == {"code": "ILLEGAL_TRANSITION"}
    assert _current(workbench, incident).control_generation == 1
    assert workbench.incidents.runs[run_id]["state"] == "paused"


def test_resume_on_an_overdue_running_run_is_refused():
    """Clause 3: resume on a still-running Run past its deadline never re-queues it."""
    app, workbench, clock = _workbench()
    incident, run_id = _submit(app, key="resume-running-overdue")
    versions = workbench.incidents.runs[run_id]["versions"]
    workbench.incidents.claim(incident, run_id, uuid4(), versions, 600)
    clock.advance(601)
    refused = _resume(app, incident, 0, "r-running")
    assert refused.status == 409
    assert refused.json() == {"code": "ILLEGAL_TRANSITION"}
    assert _current(workbench, incident).control_generation == 0
    assert workbench.incidents.runs[run_id]["state"] != "queued"


def test_resume_on_a_paused_run_within_its_deadline_requeues_it():
    """Clause 3: resume on a parked Run that is not overdue re-queues it as before."""
    app, workbench, clock = _workbench()
    incident, run_id = _submit(app, key="resume-paused-fresh")
    versions = workbench.incidents.runs[run_id]["versions"]
    workbench.incidents.claim(incident, run_id, uuid4(), versions, 600)
    paused = _control(
        app,
        incident,
        {"action": "pause", "expected_generation": "0", "idempotency_key": "p"},
    )
    assert paused.status == 200
    clock.advance(60)
    resumed = _resume(app, incident, 1, "r-fresh")
    assert resumed.status == 200, resumed.text
    assert resumed.json()["generation"] == 2
    assert workbench.incidents.runs[run_id]["state"] == "queued"
    assert _current(workbench, incident).current_run_id == run_id


def test_resume_on_a_handed_off_run_within_its_deadline_requeues_it():
    """Clause 3: resume on a waiting_human Run within its deadline re-queues it."""
    app, workbench, clock = _workbench()
    incident, run_id = _submit(app, key="resume-handoff-fresh")
    versions = workbench.incidents.runs[run_id]["versions"]
    lease = workbench.incidents.claim(incident, run_id, uuid4(), versions, 600)
    workbench.incidents.hand_off(lease)
    assert workbench.incidents.runs[run_id]["state"] == "waiting_human"
    clock.advance(60)
    resumed = _resume(app, incident, 0, "r-handoff")
    assert resumed.status == 200, resumed.text
    assert resumed.json()["generation"] == 1
    assert workbench.incidents.runs[run_id]["state"] == "queued"


# -- 2. worker loop: ordering, sweep, failure tolerance, stop -----------------


class _Journal:
    def __init__(self):
        self.entries: list[tuple] = []
        self.lock = threading.Lock()

    def add(self, *entry):
        with self.lock:
            self.entries.append(entry)

    def kinds(self):
        return [e[0] for e in self.entries]


class FakeQueue:
    def __init__(self, journal, listing=(), *, fail_times=0):
        self.journal = journal
        self.listing = tuple(listing)
        self.fail_times = fail_times
        self.limits: list[int] = []

    def claimable_incidents(self, *, limit=20):
        self.journal.add("list", limit)
        self.limits.append(limit)
        if self.fail_times:
            self.fail_times -= 1
            raise PersistenceError("LOCK_TIMEOUT")
        return self.listing[:limit]


class FakeSweeper:
    def __init__(self, journal, parked=(), *, fail_times=0):
        self.journal = journal
        self.parked = tuple(parked)
        self.fail_times = fail_times
        self.calls = 0

    def sweep_expired_runs(self, *, incident_id=None, limit=100):
        self.calls += 1
        self.journal.add("sweep", incident_id)
        if self.fail_times:
            self.fail_times -= 1
            raise PersistenceError("LOCK_TIMEOUT")
        parked, self.parked = self.parked, ()
        return parked


class FakeRunner:
    def __init__(self, journal, *, fail=None, on_resume=None):
        self.journal = journal
        self.fail = dict(fail or {})
        self.on_resume = on_resume
        self.resumed: list[UUID] = []

    def resume(self, incident_id):
        self.journal.add("resume", incident_id)
        self.resumed.append(incident_id)
        if self.on_resume is not None:
            self.on_resume(incident_id)
        error = self.fail.pop(incident_id, None)
        if error is not None:
            raise error
        return RunnerOutcome(status="published")


def _loop(queue, runner, sweeper, *, events=None, stop=None, poll=0.01, batch=20):
    return WorkerLoop(
        queue=queue,
        runner=runner,
        sweeper=sweeper,
        events=events,
        stop=stop or threading.Event(),
        poll_seconds=poll,
        batch=batch,
    )


def _run_in_thread(loop):
    result: dict = {}

    def target():
        result["value"] = loop.run()

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, result


def test_poll_once_sweeps_before_it_resumes_anything():
    """Clause 2 (ADR-0005 d2): each poll sweeps overdue Runs before any claim."""
    journal = _Journal()
    a, b = uuid4(), uuid4()
    loop = _loop(FakeQueue(journal, [a, b]), FakeRunner(journal), FakeSweeper(journal))
    loop.poll_once()
    kinds = journal.kinds()
    assert "sweep" in kinds and "resume" in kinds
    assert kinds.index("sweep") < kinds.index("resume")


def test_every_poll_sweeps_even_with_nothing_to_claim():
    """Clause 2: the sweep runs on every poll, not only when work is listed."""
    journal = _Journal()
    sweeper = FakeSweeper(journal)
    runner = FakeRunner(journal)
    loop = _loop(FakeQueue(journal, []), runner, sweeper)
    assert loop.poll_once() == []
    assert loop.poll_once() == []
    assert sweeper.calls >= 2
    assert runner.resumed == []


def test_poll_once_resumes_listed_incidents_in_order_and_returns_their_outcomes():
    """Clause 2: every listed Run is driven through runner.resume, bounded by batch."""
    journal = _Journal()
    ids = [uuid4(), uuid4(), uuid4()]
    queue = FakeQueue(journal, ids)
    runner = FakeRunner(journal)
    loop = _loop(queue, runner, FakeSweeper(journal), batch=2)
    results = loop.poll_once()
    assert queue.limits and queue.limits[-1] == 2
    assert runner.resumed == ids[:2]
    assert [incident for incident, _ in results] == ids[:2]
    assert all(outcome.status == "published" for _, outcome in results)


def test_a_swept_run_is_announced_as_a_deadline_exceeded_handoff_once():
    """Clause 2: a Run the poll's sweep parked shows one DEADLINE_EXCEEDED run_handoff."""
    journal = _Journal()
    incident, run_id = uuid4(), uuid4()
    events = MemoryEventLog()
    sweeper = FakeSweeper(journal, parked=[(incident, run_id)])
    loop = _loop(FakeQueue(journal, []), FakeRunner(journal), sweeper, events=events)
    loop.poll_once()
    loop.poll_once()
    handoffs = [
        e.payload for e in events.read_after(incident, 0) if e.kind == "run_handoff"
    ]
    assert len(handoffs) == 1
    assert handoffs[0]["run_id"] == str(run_id)
    assert handoffs[0]["reasons"] == ["DEADLINE_EXCEEDED"]
    assert handoffs[0]["published"] is False and handoffs[0]["handoff"] is True


@pytest.mark.parametrize(
    "error", [PersistenceError("LOCK_TIMEOUT"), RuntimeError("attempt crashed")]
)
def test_one_failing_run_does_not_stop_the_rest_of_the_batch(error):
    """Clause 2: one Run's failed attempt does not starve the other listed Runs."""
    journal = _Journal()
    bad, good = uuid4(), uuid4()
    runner = FakeRunner(journal, fail={bad: error})
    loop = _loop(FakeQueue(journal, [bad, good]), runner, FakeSweeper(journal))
    results = loop.poll_once()
    assert runner.resumed == [bad, good]
    assert (good, "published") in [(i, o.status) for i, o in results]


def test_run_keeps_polling_after_a_failed_sweep_and_listing():
    """Clause 2: a transient store failure in one poll does not end the resident loop."""
    journal = _Journal()
    incident = uuid4()
    stop = threading.Event()
    runner = FakeRunner(journal, on_resume=lambda _: stop.set())
    loop = _loop(
        FakeQueue(journal, [incident], fail_times=1),
        runner,
        FakeSweeper(journal, fail_times=1),
        stop=stop,
    )
    thread, result = _run_in_thread(loop)
    thread.join(timeout=5.0)
    stop.set()
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert runner.resumed == [incident]
    assert isinstance(result.get("value"), int)


def test_run_returns_without_claiming_when_stop_is_already_set():
    """Clause 2: a stop request ends the loop before the next claim."""
    journal = _Journal()
    stop = threading.Event()
    stop.set()
    runner = FakeRunner(journal)
    loop = _loop(FakeQueue(journal, [uuid4()]), runner, FakeSweeper(journal), stop=stop)
    thread, result = _run_in_thread(loop)
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert runner.resumed == []
    assert isinstance(result.get("value"), int)


def test_a_stop_request_during_an_attempt_prevents_the_next_claim():
    """Clause 2: stop set while one Run is being driven ends the loop before the next Run."""
    journal = _Journal()
    stop = threading.Event()
    first, second = uuid4(), uuid4()
    runner = FakeRunner(journal, on_resume=lambda _: stop.set())
    loop = _loop(
        FakeQueue(journal, [first, second]), runner, FakeSweeper(journal), stop=stop
    )
    thread, _ = _run_in_thread(loop)
    thread.join(timeout=2.0)
    stop.set()
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert runner.resumed == [first]


def test_stop_wakes_an_idle_loop_promptly():
    """Clause 2: an idle loop waiting out poll_seconds exits promptly on stop."""
    journal = _Journal()
    stop = threading.Event()
    loop = _loop(
        FakeQueue(journal, []),
        FakeRunner(journal),
        FakeSweeper(journal),
        stop=stop,
        poll=30.0,
    )
    thread, _ = _run_in_thread(loop)
    deadline = time.monotonic() + 2.0
    while "list" not in journal.kinds() and time.monotonic() < deadline:
        time.sleep(0.01)
    started = time.monotonic()
    stop.set()
    thread.join(timeout=3.0)
    assert not thread.is_alive()
    assert time.monotonic() - started < 3.0
