"""Workbench -> worker -> real driver on PostgreSQL (ROADMAP M1-01 item 3b).

An incident submitted through the authenticated intake is claimed by the
polling worker loop, driven through ``InvestigationRunner`` with the
durable event log (the page's stream shows every step) and the durable
tool ledger (tool counts survive a restart), and ends published or parked;
a follow_up after a handoff is picked up; a worker killed mid-run is
resumed by the next one; two workers never double-run a Run; ``resume``
on an overdue Run is refused. The model is scripted; the tool profile is
the product fixture one, exactly as ``python -m opspilot.worker_main``
composes it.
"""

from __future__ import annotations

import os
import threading
import time
from uuid import UUID, uuid4

import pytest

from opspilot.investigation.loop import ModelError
from opspilot.investigation.runner import InvestigationRunner
from opspilot.persistence import DurableStore
from opspilot.tools.fixture import (
    FIXTURE_TOOL,
    fixture_executor_factory,
    fixture_face,
    fixture_versions,
)
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
from opspilot.worker import Worker
from opspilot.worker_main import WorkerLoop
from scripts.m0.postgres_lab import DSN
from tests.integration.test_m1_deadline_sweep_postgres import _expire, _run_row
from tests.m1_investigation_support import (
    ScriptedModel,
    reply,
    report_from_transcript,
    tool_call,
)
from tests.m1_web_support import (
    EVENT_TOKEN,
    ORIGIN,
    UI_PASSWORD,
    UI_USER,
    basic,
    call,
    post_form,
    same_origin,
    stream,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

LEASE = 2
QUESTION = "Why is checkout erroring?"


def _build(*, run_seconds=600):
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
        run_versions=fixture_versions(),
        run_seconds=run_seconds,
        tool_face=fixture_face(),
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
    return app, workbench, store, events, evidence


class _Only:
    """The store's listing narrowed to one incident: the lab database keeps
    every earlier test's rows, and a stale claimable Run of another test
    would otherwise consume this test's scripted replies. Test 1 checks the
    unnarrowed listing itself."""

    def __init__(self, store, subject):
        self.store, self.subject = store, subject

    def claimable_incidents(self, *, limit=20):
        return tuple(
            i for i in self.store.claimable_incidents(limit=1000) if i == self.subject
        )


def _worker(
    store, events, evidence, replies, *, subject, executor_hook=None, stop=None
):
    """One worker instance: its own owner, the product composition otherwise."""
    clock = DurableClock(store)
    factory = fixture_executor_factory(store, evidence=evidence, clock=clock)
    if executor_hook is not None:
        inner = factory

        def factory(lease, input):  # type: ignore[misc]
            executor = inner(lease, input)

            class Hooked:
                scope = executor.scope
                tool_seconds_used = 0.0

                def execute(self, request):
                    outcome = executor.execute(request)
                    executor_hook(request)
                    return outcome

            return Hooked()

    model = ScriptedModel(replies)
    runner = InvestigationRunner(
        store=store,
        worker=Worker.create(store, fixture_versions()),
        model=model,
        executor_factory=factory,
        clock=clock,
        lease_seconds=LEASE,
        events=events,
        evidence=evidence,
    )
    loop = WorkerLoop(
        queue=_Only(store, subject),
        runner=runner,
        sweeper=store,
        events=events,
        stop=stop if stop is not None else threading.Event(),
        poll_seconds=0.05,
    )
    return loop, model


def _tool_round(n=1):
    return reply(
        tool_calls=[tool_call(call_id=f"call-{n}", name=FIXTURE_TOOL)],
        finish="tool_calls",
    )


def _submit(app, key):
    response = post_form(
        app,
        "/intake/ui",
        {"target_id": "checkout-prod", "question": QUESTION, "idempotency_key": key},
        headers={**basic(), **same_origin()},
    )
    assert response.status == 201
    return response.json()


def _control(app, incident, fields):
    return post_form(
        app,
        f"/incidents/{incident}/control",
        fields,
        headers={**basic(), **same_origin()},
    )


def _kinds(events, subject):
    return [e.kind for e in events.read_after(subject, 0, limit=1000)]


def test_a_workbench_incident_is_claimed_investigated_and_streamed():
    app, workbench, store, events, evidence = _build()
    submitted = _submit(app, f"web-worker-{uuid4()}")
    subject, run_id = UUID(submitted["incident_id"]), UUID(submitted["run_id"])
    # The row carries the runner's input, recorded by the workbench, and the
    # store's own listing offers the incident to any worker.
    assert store.rebuild(subject)["run"]["input"] is not None
    assert subject in store.claimable_incidents(limit=1000)
    loop, model = _worker(
        store,
        events,
        evidence,
        [_tool_round(), report_from_transcript],
        subject=subject,
    )
    results = loop.poll_once()
    assert [(i, o.status) for i, o in results] == [(subject, "published")]
    rebuilt = store.rebuild(subject)
    assert rebuilt["run"]["state"] == "completed" and rebuilt["conclusion"] is not None
    assert len(model.calls) == 2
    # Tool counts are durable: the ledger charged the one dispatch.
    usage = store.run_usage(run_id)
    assert usage["tool_operations_used"] == 1 and usage["tool_seconds_used"] >= 0
    assert _kinds(events, subject) == [
        "intake_accepted",
        "run_claimed",
        "step_committed",
        "tool_committed",
        "step_committed",
        "run_completed",
    ]
    # The page and its stream show the steps.
    snapshot = workbench.snapshot(subject)
    assert snapshot["run"]["state"] == "completed"
    assert snapshot["report"] is not None and snapshot["report"]["parsed"] is True
    page = call(app, "GET", f"/incidents/{subject}", headers=basic())
    assert page.status == 200 and "Facts (1)" in page.text
    streamed = stream(
        app, f"/incidents/{subject}/events?cursor=1", headers=basic(), until_events=5
    )
    assert [k for _, k, _ in streamed.sse_events()] == [
        "run_claimed",
        "step_committed",
        "tool_committed",
        "step_committed",
        "run_completed",
    ]
    # Nothing left to claim; a second poll is a no-op.
    assert loop.poll_once() == []


def test_a_follow_up_after_a_handoff_is_picked_up_by_the_worker():
    app, workbench, store, events, evidence = _build()
    submitted = _submit(app, f"web-worker-followup-{uuid4()}")
    subject = UUID(submitted["incident_id"])
    # The provider rejects the first request: a handoff, parked, with one
    # of the Run's four requests spent (a follow_up re-queues the same Run
    # and its budget).
    first, _ = _worker(
        store, events, evidence, [ModelError("MODEL_REJECTED")], subject=subject
    )
    ((_, outcome),) = first.poll_once()
    assert outcome.status == "handed_off" and outcome.reason == "MODEL_REJECTED"
    assert store.rebuild(subject)["run"]["state"] == "waiting_human"
    assert first.poll_once() == []  # parked: not claimable
    follow = _control(
        app,
        str(subject),
        {
            "action": "follow_up",
            "expected_generation": "0",
            "idempotency_key": "f1",
            "text": "Query the metric and cite it.",
        },
    )
    assert follow.status == 200 and follow.json()["generation"] == 1
    assert store.rebuild(subject)["run"]["state"] == "queued"
    second, model = _worker(
        store,
        events,
        evidence,
        [_tool_round(), report_from_transcript],
        subject=subject,
    )
    ((_, outcome),) = second.poll_once()
    assert outcome.status == "published", outcome
    # The note reached the second attempt's model context.
    assert any(
        "Query the metric and cite it." in str(m.get("content"))
        for call_ in model.calls
        for m in call_.messages
    )
    kinds = _kinds(events, subject)
    assert kinds.count("run_handoff") == 1 and kinds.count("run_completed") == 1
    assert kinds.index("control_applied") < kinds.index("run_completed")
    assert "run_claim_refused" not in kinds


def test_a_worker_killed_mid_run_is_resumed_by_the_next_worker():
    app, workbench, store, events, evidence = _build()
    submitted = _submit(app, f"web-worker-kill-{uuid4()}")
    subject, run_id = UUID(submitted["incident_id"]), UUID(submitted["run_id"])

    def die(request):
        # After the executor ran (and charged) the dispatch, before the
        # session could commit its result: the killed-worker window.
        raise RuntimeError("killed before the tool result committed")

    first, _ = _worker(
        store, events, evidence, [_tool_round()], subject=subject, executor_hook=die
    )
    assert first.poll_once() == []  # the crash is survived, nothing settled
    rows = store.rebuild(subject)
    assert rows["run"]["state"] == "running" and len(rows["pending_tools"]) == 1
    # The dead attempt's dispatch was charged durably before it died.
    assert store.run_usage(run_id)["tool_operations_used"] == 1
    assert first.poll_once() == []  # the lease is still live: LEASE_ACTIVE
    time.sleep(LEASE + 0.5)
    second, model = _worker(
        store, events, evidence, [report_from_transcript], subject=subject
    )
    ((_, outcome),) = second.poll_once()
    assert outcome.status == "published" and outcome.replayed_tools == 1
    assert len(model.calls) == 1  # the committed round was not re-asked
    # The replay is a second real read (C3 §13): counted on top of the
    # first attempt's charge, which a restart did not reset.
    assert store.run_usage(run_id)["tool_operations_used"] == 2
    kinds = _kinds(events, subject)
    assert kinds.count("run_claimed") == 2 and kinds[-1] == "run_completed"
    assert "tool_committed" in kinds


def test_two_workers_never_double_run_one_incident():
    app, workbench, store, events, evidence = _build()
    submitted = _submit(app, f"web-worker-race-{uuid4()}")
    subject, run_id = UUID(submitted["incident_id"]), UUID(submitted["run_id"])
    started = threading.Barrier(2)

    def slow_tool_round(call):
        time.sleep(0.5)
        return _tool_round()

    loops = [
        _worker(
            store,
            events,
            evidence,
            [slow_tool_round, report_from_transcript],
            subject=subject,
        )
        for _ in range(2)
    ]
    results = [None, None]

    def poll(index):
        started.wait()
        results[index] = loops[index][0].poll_once()

    threads = [threading.Thread(target=poll, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    statuses = sorted(o.status for r in results for _, o in r)
    assert statuses == ["published"] or statuses == ["control_denied", "published"]
    calls = sum(len(model.calls) for _, model in loops)
    assert calls == 2  # exactly one attempt ran the two rounds
    rebuilt = store.rebuild(subject)
    assert rebuilt["run"]["state"] == "completed" and rebuilt["run"]["epoch"] == 1
    assert store.run_usage(run_id)["tool_operations_used"] == 1
    assert _kinds(events, subject).count("run_claimed") == 1


def test_resume_on_an_overdue_run_is_refused_and_no_dead_row_is_polled():
    app, workbench, store, events, evidence = _build()
    submitted = _submit(app, f"web-worker-resume-{uuid4()}")
    subject, run_id = UUID(submitted["incident_id"]), UUID(submitted["run_id"])
    store.claim(subject, run_id, uuid4(), fixture_versions(), lease_seconds=300)
    _expire(store, run_id)
    loop, _ = _worker(store, events, evidence, [], subject=subject)
    assert loop.poll_once() == []  # swept, not claimed
    assert _run_row(store, run_id)["state"] == "waiting_human"
    refused = _control(
        app,
        str(subject),
        {"action": "resume", "expected_generation": "0", "idempotency_key": "r"},
    )
    assert refused.status == 409 and refused.json() == {"code": "ILLEGAL_TRANSITION"}
    assert _run_row(store, run_id)["state"] == "waiting_human"
    assert store.rebuild(subject)["control_generation"] == 0
    assert loop.poll_once() == []
    kinds = _kinds(events, subject)
    assert kinds.count("run_handoff") == 1 and "run_claim_refused" not in kinds
    # The ways forward: a note renews into a Run the worker then runs.
    follow = _control(
        app,
        str(subject),
        {
            "action": "follow_up",
            "expected_generation": "0",
            "idempotency_key": "f",
            "text": "Look again.",
        },
    )
    assert follow.status == 200
    renewed = store.rebuild(subject)["run"]
    assert renewed["run_id"] != run_id and renewed["input"] is not None
    assert renewed["input"]["evidence_context"]["run_id"] == str(renewed["run_id"])
    again, _ = _worker(
        store,
        events,
        evidence,
        [_tool_round(), report_from_transcript],
        subject=subject,
    )
    ((_, outcome),) = again.poll_once()
    assert outcome.status == "published"


def test_a_queued_run_the_worker_never_reached_is_swept_and_renewable():
    """Bot review (PR #52): worker down while the wall passes -- the next
    poll parks the still-queued Run as DEADLINE_EXCEEDED (one event), and a
    follow_up renews it into a Run the worker then completes (#47)."""
    app, workbench, store, events, evidence = _build()
    submitted = _submit(app, f"web-worker-queued-overdue-{uuid4()}")
    subject, run_id = UUID(submitted["incident_id"]), UUID(submitted["run_id"])
    _expire(store, run_id)  # no worker claimed it before its wall
    assert subject not in store.claimable_incidents(limit=1000)
    loop, model = _worker(store, events, evidence, [], subject=subject)
    assert loop.poll_once() == []
    assert _run_row(store, run_id)["state"] == "waiting_human"
    assert model.calls == []
    assert loop.poll_once() == []  # idempotent: not re-parked, not announced twice
    kinds = _kinds(events, subject)
    assert kinds == ["intake_accepted", "run_handoff"]
    handoff = next(e for e in events.read_after(subject, 0) if e.kind == "run_handoff")
    assert handoff.payload["reasons"] == ["DEADLINE_EXCEEDED"]
    assert handoff.payload["parked"] is True
    follow = _control(
        app,
        str(subject),
        {
            "action": "follow_up",
            "expected_generation": "0",
            "idempotency_key": "f",
            "text": "Look again.",
        },
    )
    assert follow.status == 200
    renewed = store.rebuild(subject)["run"]
    assert renewed["run_id"] != run_id and renewed["input"] is not None
    again, _ = _worker(
        store,
        events,
        evidence,
        [_tool_round(), report_from_transcript],
        subject=subject,
    )
    ((_, outcome),) = again.poll_once()
    assert outcome.status == "published"
    assert _kinds(events, subject)[-1] == "run_completed"
