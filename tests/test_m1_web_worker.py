"""Workbench -> worker wiring (ROADMAP M1-01 item 3b), on the memory doubles.

Every Run the workbench creates carries the investigation input the real
driver rebuilds from (``InvestigationRunner`` refuses ``INPUT_MISSING``);
a ``resume`` on an overdue Run is refused instead of re-queueing a row no
claim can take; and the polling worker loop sweeps, claims in order and
stops cleanly. The PostgreSQL end-to-end runs live in
``tests/integration/test_m1_web_worker_postgres.py``.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from uuid import UUID, uuid4

from opspilot.investigation.context import InvestigationInput
from opspilot.investigation.inputs import ToolFace, continuation_input
from opspilot.investigation.limits import M1_FROZEN_LIMITS
from opspilot.investigation.runner import RunnerOutcome
from opspilot.persistence import PersistenceError
from opspilot.tools.fixture import (
    FIXTURE_TARGET,
    FIXTURE_TOOL,
    fixture_face,
    fixture_versions,
)
from opspilot.web.events import MemoryEventLog
from opspilot.worker_main import WorkerLoop
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


# -- the recorded investigation input ----------------------------------------


def test_submit_records_the_input_the_runner_rebuilds_from():
    app, workbench, clock = build_workbench(tool_face=fixture_face())
    response = submit_incident(app, key="input-1", question="Why is checkout erroring?")
    assert response.status == 201
    run_id = UUID(response.json()["run_id"])
    recorded = workbench.incidents.runs[run_id]["input"]
    assert recorded is not None
    input = InvestigationInput.from_json(recorded)
    assert input.question == "Why is checkout erroring?"
    assert input.bound_target_id == FIXTURE_TARGET
    assert input.model_requests == workbench.budget_limit
    assert input.limits.within(M1_FROZEN_LIMITS)
    assert input.evidence_context is not None
    assert input.evidence_context["run_id"] == str(run_id)
    assert [s["function"]["name"] for s in input.tool_schemas] == [FIXTURE_TOOL]
    deadline = workbench.incidents.runs[run_id]["deadline"]
    assert input.scope_facts["deadline"] == deadline.isoformat()
    assert input.scope_facts["target_ids"] == [FIXTURE_TARGET]


def test_a_workbench_without_a_tool_face_records_no_input():
    """The test-only ``run_once`` path keeps working without a face."""
    app, workbench, _ = build_workbench()
    run_id = UUID(submit_incident(app, key="no-face").json()["run_id"])
    assert workbench.incidents.runs[run_id]["input"] is None


def test_a_note_on_a_timed_out_run_and_new_run_carry_a_successor_input():
    app, workbench, clock = build_workbench(tool_face=fixture_face())
    incident = submit_incident(app, key="successor").json()["incident_id"]
    subject = UUID(incident)
    old = workbench.list_incidents()[0].current_run_id
    workbench.incidents.claim(subject, old, uuid4(), {"state": "v1"}, 600)
    clock.advance(601)
    assert workbench.snapshot(subject)["outcome"]["reasons"] == ["DEADLINE_EXCEEDED"]
    follow = _control(
        app,
        incident,
        {
            "action": "follow_up",
            "expected_generation": "0",
            "idempotency_key": "after-timeout",
            "text": "Check the dependency too.",
        },
    )
    assert follow.status == 200 and follow.json()["generation"] == 1
    renewed = workbench.list_incidents()[0].current_run_id
    assert renewed != old
    successor = InvestigationInput.from_json(workbench.incidents.runs[renewed]["input"])
    # C3 continuation: the context is re-bound to the new Run and the
    # question carries the deterministic handoff note.
    assert successor.evidence_context["run_id"] == str(renewed)
    assert f"Continuation of investigation Run {old}" in successor.question
    assert successor.question.startswith("Why is checkout erroring?")
    assert successor.scope_facts["deadline"] == (
        workbench.incidents.runs[renewed]["deadline"].isoformat()
    )
    # cancel + new_run: the same successor rule.
    assert (
        _control(
            app,
            incident,
            {"action": "cancel", "expected_generation": "1", "idempotency_key": "c"},
        ).status
        == 200
    )
    assert (
        _control(
            app,
            incident,
            {"action": "new_run", "expected_generation": "2", "idempotency_key": "n"},
        ).status
        == 200
    )
    third = workbench.list_incidents()[0].current_run_id
    assert third not in {old, renewed}
    again = InvestigationInput.from_json(workbench.incidents.runs[third]["input"])
    assert again.evidence_context["run_id"] == str(third)
    assert f"Continuation of investigation Run {renewed}" in again.question


def test_new_run_after_a_run_without_input_falls_back_to_a_fresh_input():
    """Rows created before the face existed have no snapshot to continue;
    the successor still gets a runnable input over the original question."""
    app, workbench, _ = build_workbench(tool_face=fixture_face())
    incident = submit_incident(app, key="legacy").json()["incident_id"]
    old = workbench.list_incidents()[0].current_run_id
    workbench.incidents.runs[old]["input"] = None
    _control(
        app,
        incident,
        {"action": "cancel", "expected_generation": "0", "idempotency_key": "c"},
    )
    assert (
        _control(
            app,
            incident,
            {"action": "new_run", "expected_generation": "1", "idempotency_key": "n"},
        ).status
        == 200
    )
    fresh = workbench.list_incidents()[0].current_run_id
    input = InvestigationInput.from_json(workbench.incidents.runs[fresh]["input"])
    assert input.question == "Why is checkout erroring?"
    assert input.evidence_context["run_id"] == str(fresh)


def test_continuation_input_rebinds_deadline_and_context():
    face = fixture_face()
    old, new = uuid4(), uuid4()
    previous = face.input_for(
        run_id=str(old),
        question="q",
        target_id=FIXTURE_TARGET,
        deadline=datetime(2026, 9, 14, 1, 5, tzinfo=timezone.utc),
        model_requests=2,
    )
    snapshot = {
        "run": {"run_id": old, "input": previous.as_json()},
        "steps": [],
        "conclusion": None,
    }
    later = datetime(2026, 9, 14, 2, 0, tzinfo=timezone.utc)
    successor = continuation_input(
        snapshot,
        new_run_id=str(new),
        deadline=later,
        authorized_targets=frozenset({FIXTURE_TARGET}),
    )
    assert successor.evidence_context["run_id"] == str(new)
    assert successor.scope_facts["deadline"] == later.isoformat()
    assert successor.scope_facts["target_ids"] == [FIXTURE_TARGET]
    assert successor.model_requests == 2


def test_fixture_face_and_versions_are_what_the_worker_compares():
    face = fixture_face()
    assert isinstance(face, ToolFace)
    versions = fixture_versions()
    assert {"prompt_revision", "tool_schema_revision"} <= set(versions)
    assert all(isinstance(v, str) and v for v in versions.values())
    assert fixture_versions() == versions


# -- resume on an overdue Run -------------------------------------------------


def test_resume_on_an_overdue_run_is_refused_and_leaves_the_park_alone():
    """Item 3b decision: ``resume`` carries no new input, so a timed-out Run
    has nothing to resume into; follow_up/correct (renewal) or cancel +
    new_run are the ways forward. Refusing keeps one renewal path and never
    leaves a queued row no claim can take (#47's rule for bare notes)."""
    app, workbench, clock = build_workbench(tool_face=fixture_face())
    incident = submit_incident(app, key="resume-overdue").json()["incident_id"]
    subject = UUID(incident)
    run_id = workbench.list_incidents()[0].current_run_id
    workbench.incidents.claim(subject, run_id, uuid4(), {"state": "v1"}, 600)
    clock.advance(601)
    assert workbench.snapshot(subject)["run"]["state"] == "waiting_human"
    refused = _control(
        app,
        incident,
        {"action": "resume", "expected_generation": "0", "idempotency_key": "r"},
    )
    assert refused.status == 409
    assert refused.json()["code"] == "ILLEGAL_TRANSITION"
    run = workbench.incidents.runs[run_id]
    assert run["state"] == "waiting_human"
    assert workbench.incidents.incidents[subject]["control_generation"] == 0
    # A refused intent does not block a corrected resubmission under the key.
    assert workbench.ledger.get("control_intent", f"{subject}:r") is None
    # The ways forward still work.
    follow = _control(
        app,
        incident,
        {
            "action": "follow_up",
            "expected_generation": "0",
            "idempotency_key": "f",
            "text": "Look again.",
        },
    )
    assert follow.status == 200 and follow.json()["generation"] == 1


def test_resume_on_a_parked_run_that_is_not_overdue_still_requeues_it():
    app, workbench, clock = build_workbench(tool_face=fixture_face())
    incident = submit_incident(app, key="resume-ok").json()["incident_id"]
    subject = UUID(incident)
    run_id = workbench.list_incidents()[0].current_run_id
    lease = workbench.incidents.claim(subject, run_id, uuid4(), {"state": "v1"}, 600)
    workbench.incidents.hand_off(lease)
    assert workbench.incidents.runs[run_id]["state"] == "waiting_human"
    resumed = _control(
        app,
        incident,
        {"action": "resume", "expected_generation": "0", "idempotency_key": "r"},
    )
    assert resumed.status == 200 and resumed.json()["generation"] == 1
    assert workbench.incidents.runs[run_id]["state"] == "queued"


# -- the polling worker loop --------------------------------------------------


class _Queue:
    def __init__(self, batches):
        self.batches = list(batches)
        self.limits = []

    def claimable_incidents(self, *, limit=20):
        self.limits.append(limit)
        return tuple(self.batches.pop(0)) if self.batches else ()


class _Sweeper:
    def __init__(self, parked=()):
        self.calls = 0
        self.parked = tuple(parked)

    def sweep_expired_runs(self, *, incident_id=None, limit=100):
        self.calls += 1
        assert incident_id is None
        return self.parked


class _Runner:
    def __init__(self, outcomes=None, stop=None, stop_after=None):
        self.outcomes = dict(outcomes or {})
        self.resumed = []
        self.stop = stop
        self.stop_after = stop_after

    def resume(self, incident_id):
        self.resumed.append(incident_id)
        if self.stop is not None and len(self.resumed) == self.stop_after:
            self.stop.set()
        outcome = self.outcomes.get(incident_id, RunnerOutcome("published"))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _loop(queue, runner, sweeper=None, events=None, stop=None, **kwargs):
    return WorkerLoop(
        queue=queue,
        runner=runner,
        sweeper=sweeper if sweeper is not None else _Sweeper(),
        events=events,
        stop=stop if stop is not None else threading.Event(),
        poll_seconds=0.01,
        **kwargs,
    )


def test_one_poll_sweeps_then_resumes_every_claimable_incident_in_order():
    a, b = uuid4(), uuid4()
    events = MemoryEventLog()
    sweeper = _Sweeper(parked=((a, uuid4()),))
    runner = _Runner({b: RunnerOutcome("handed_off", reason="AWAITING_HUMAN")})
    loop = _loop(_Queue([[a, b]]), runner, sweeper, events, batch=7)
    results = loop.poll_once()
    assert sweeper.calls == 1
    assert [e.kind for e in events.read_after(a, 0)] == ["run_handoff"]
    assert runner.resumed == [a, b]
    assert [(i, o.status) for i, o in results] == [(a, "published"), (b, "handed_off")]
    assert loop.queue.limits == [7]


def test_a_failing_attempt_does_not_stop_the_loop():
    a, b, c = uuid4(), uuid4(), uuid4()
    runner = _Runner(
        {a: PersistenceError("STORAGE_UNAVAILABLE"), b: RuntimeError("boom")}
    )
    loop = _loop(_Queue([[a, b, c]]), runner)
    results = loop.poll_once()
    assert runner.resumed == [a, b, c]
    assert [i for i, _ in results] == [c]


def test_a_sweep_or_listing_failure_is_survived():
    class Broken:
        def sweep_expired_runs(self, *, incident_id=None, limit=100):
            raise PersistenceError("LOCK_TIMEOUT")

    class BrokenQueue:
        def claimable_incidents(self, *, limit=20):
            raise PersistenceError("STORAGE_UNAVAILABLE")

    loop = _loop(BrokenQueue(), _Runner(), Broken())
    assert loop.poll_once() == []


def test_stop_ends_the_batch_before_the_next_claim_and_the_loop_returns():
    a, b, c = uuid4(), uuid4(), uuid4()
    stop = threading.Event()
    runner = _Runner(stop=stop, stop_after=2)
    loop = _loop(_Queue([[a, b, c], [a]]), runner, stop=stop)
    polls = loop.run()
    assert polls == 1
    assert runner.resumed == [a, b]


def test_run_polls_until_stopped():
    stop = threading.Event()
    queue = _Queue([[], [], []])
    loop = _loop(queue, _Runner(), stop=stop)
    thread = threading.Thread(target=loop.run)
    thread.start()
    thread.join(0.2)
    assert thread.is_alive()
    stop.set()
    thread.join(2)
    assert not thread.is_alive()
    assert len(queue.limits) >= 2


def test_the_workbench_page_keeps_working_with_the_face():
    """Smoke: the snapshot renders for a Run that carries an input."""
    app, workbench, _ = build_workbench(tool_face=fixture_face())
    incident = submit_incident(app, key="page").json()["incident_id"]
    page = call(app, "GET", f"/incidents/{incident}", headers=basic())
    assert page.status == 200
