"""Contract tests for M1-01 3b end to end on the lab PostgreSQL.

Written from the contract only (ROADMAP M1-01 3b, ADR-0005, C3 §5-7/§13) by
an author who did not read the implementation. Real ``DurableStore``, real
``InvestigationRunner`` with the fixture tool profile, the durable event log,
and ``WorkerLoop``; the model is scripted. The lab database keeps rows from
other suites, so every worker here sees only this test's incidents through
``_Scoped`` (listing and sweep filtered to them).
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from uuid import UUID, uuid4

import pytest

from opspilot.investigation.context import InvestigationInput
from opspilot.investigation.limits import M1_FROZEN_LIMITS
from opspilot.investigation.runner import InvestigationRunner
from opspilot.persistence import DurableStore
from opspilot.tools.fixture import (
    FIXTURE_TARGET,
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

QUESTION = "Why is checkout erroring?"
TERMINAL = ("run_completed", "run_handoff")


# -- harness -------------------------------------------------------------------


class _Stack:
    def __init__(self) -> None:
        self.store = DurableStore(DSN)
        self.store.install()
        self.events = DurableEventLog(self.store)
        self.events.install()
        self.evidence = DurableEvidenceStore(self.store)
        self.evidence.install()
        ledger = DurableWebLedger(self.store)
        ledger.install()
        self.workbench = Workbench(
            incidents=DurableIncidentStore(self.store),
            events=self.events,
            evidence=self.evidence,
            ledger=ledger,
            run_versions=fixture_versions(),
            run_seconds=600,
            tool_face=fixture_face(),
        )
        config = AuthConfig(
            ui_users={UI_USER: hash_password(UI_PASSWORD)},
            event_tokens={token_digest(EVENT_TOKEN): "alertmanager-prod"},
            auth_revision="auth-rev-pg-worker-contract",
            allowed_origins=frozenset({ORIGIN}),
        )
        self.app = create_app(
            self.workbench,
            Authenticator(config),
            DurableClock(self.store),
            sse_poll_seconds=0.02,
            sse_idle_seconds=0.3,
        )

    def submit(self, tag: str) -> tuple[UUID, UUID]:
        response = post_form(
            self.app,
            "/intake/ui",
            {
                "target_id": FIXTURE_TARGET,
                "question": QUESTION,
                "idempotency_key": f"m1-worker-contract-{tag}-{uuid4()}",
            },
            headers={**basic(), **same_origin()},
        )
        assert response.status == 201, response.text
        body = response.json()
        return UUID(body["incident_id"]), UUID(body["run_id"])

    def control(self, incident: UUID, fields: dict[str, str]):
        return post_form(
            self.app,
            f"/incidents/{incident}/control",
            fields,
            headers={**basic(), **same_origin()},
        )

    def runner(self, replies, *, lease_seconds: int = 60, executor_factory=None):
        model = ScriptedModel(list(replies))
        clock = DurableClock(self.store)
        runner = InvestigationRunner(
            store=self.store,
            worker=Worker.create(self.store, dict(fixture_versions())),
            model=model,
            executor_factory=executor_factory
            or fixture_executor_factory(
                self.store, evidence=self.evidence, clock=clock
            ),
            clock=clock,
            lease_seconds=lease_seconds,
            events=self.events,
            evidence=self.evidence,
        )
        return runner, model

    def loop(self, runner, *incidents: UUID, stop=None, poll=0.05) -> WorkerLoop:
        scoped = _Scoped(self.store, incidents)
        return WorkerLoop(
            queue=scoped,
            runner=runner,
            sweeper=scoped,
            events=self.events,
            stop=stop or threading.Event(),
            poll_seconds=poll,
            batch=20,
        )

    def kinds(self, incident: UUID) -> list[str]:
        return [e.kind for e in self.events.read_after(incident, 0, limit=1000)]

    def payloads(self, incident: UUID, kind: str) -> list[dict]:
        return [
            dict(e.payload)
            for e in self.events.read_after(incident, 0, limit=1000)
            if e.kind == kind
        ]

    def current_run(self, incident: UUID) -> dict:
        return self.store.rebuild(incident)["run"]


class _Scoped:
    """Queue and sweeper limited to this test's incidents on a shared lab DB."""

    def __init__(self, store: DurableStore, incidents) -> None:
        self._store = store
        self._ids = tuple(incidents)

    def claimable_incidents(self, *, limit: int = 20):
        listed = self._store.claimable_incidents(limit=10_000)
        return tuple(i for i in listed if i in self._ids)[:limit]

    def sweep_expired_runs(self, *, incident_id=None, limit: int = 100):
        parked: list = []
        for subject in self._ids:
            if incident_id is None or incident_id == subject:
                parked.extend(
                    self._store.sweep_expired_runs(incident_id=subject, limit=limit)
                )
        return tuple(parked)


def _tool_round():
    return reply(
        tool_calls=[
            tool_call(
                call_id="call-1",
                name=FIXTURE_TOOL,
                arguments=json.dumps({"expr": "rate(http_errors[5m])"}),
            )
        ],
        finish="tool_calls",
    )


def _slow(item, seconds):
    def delayed(call_):
        time.sleep(seconds)
        return item(call_) if callable(item) else item

    return delayed


def _assert_snapshot(row_input, *, run_id: UUID) -> InvestigationInput:
    assert row_input is not None, "Run was created without an input snapshot"
    rebuilt = InvestigationInput.from_json(row_input)
    assert rebuilt.question.startswith(QUESTION)
    assert rebuilt.evidence_context is not None
    assert rebuilt.evidence_context["run_id"] == str(run_id)
    assert rebuilt.limits.within(M1_FROZEN_LIMITS)
    assert rebuilt.bound_target_id == FIXTURE_TARGET
    return rebuilt


def _assert_one_terminal(kinds: list[str], expected: str) -> None:
    terminals = [k for k in kinds if k in TERMINAL]
    assert terminals == [expected], kinds
    assert kinds[-1] == expected, kinds


class _Died(BaseException):
    """A worker process dying mid-attempt: not an error any handler recovers."""


# -- 1. snapshots on every path, on the durable store -------------------------


def test_every_workbench_run_records_its_input_snapshot_on_postgres():
    """Clause 1: intake, cancel + new_run and the #47 renewal each record a rebuildable input."""
    stack = _Stack()
    incident, first = stack.submit("snapshots")
    run = stack.current_run(incident)
    assert UUID(str(run["run_id"])) == first
    _assert_snapshot(run["input"], run_id=first)

    assert (
        stack.control(
            incident,
            {"action": "cancel", "expected_generation": "0", "idempotency_key": "c"},
        ).status
        == 200
    )
    renewed = stack.control(
        incident,
        {"action": "new_run", "expected_generation": "1", "idempotency_key": "n"},
    )
    assert renewed.status == 200, renewed.text
    run = stack.current_run(incident)
    assert UUID(str(run["run_id"])) != first
    _assert_snapshot(run["input"], run_id=UUID(str(run["run_id"])))

    other, stuck = stack.submit("snapshots-renewal")
    stack.store.claim(other, stuck, uuid4(), fixture_versions(), lease_seconds=300)
    _expire(stack.store, stuck)
    assert stack.store.sweep_expired_runs(incident_id=other) == ((other, stuck),)
    noted = stack.control(
        other,
        {
            "action": "follow_up",
            "expected_generation": "0",
            "idempotency_key": "f",
            "text": "Also check the dependency.",
        },
    )
    assert noted.status == 200, noted.text
    run = stack.current_run(other)
    assert UUID(str(run["run_id"])) != stuck
    _assert_snapshot(run["input"], run_id=UUID(str(run["run_id"])))


# -- 2. worker loop end to end --------------------------------------------------


def test_a_web_submitted_incident_is_investigated_by_the_worker_to_a_conclusion():
    """Clause 2: submit -> worker poll -> real runner publishes; page shows claim, rows, one terminal."""
    stack = _Stack()
    incident, run_id = stack.submit("publish")
    runner, model = stack.runner([_tool_round(), report_from_transcript])
    results = stack.loop(runner, incident).poll_once()
    assert [(i, o.status) for i, o in results] == [(incident, "published")]
    assert len(model.calls) == 2

    kinds = stack.kinds(incident)
    assert kinds.index("run_claimed") < kinds.index("step_committed")
    assert kinds.count("step_committed") == 2
    assert kinds.count("tool_committed") == 1
    _assert_one_terminal(kinds, "run_completed")
    rebuilt = stack.store.rebuild(incident)
    assert rebuilt["run"]["state"] == "completed"
    assert rebuilt["conclusion"] is not None
    assert stack.store.run_usage(run_id)["tool_operations_used"] == 1

    streamed = stream(
        stack.app,
        f"/incidents/{incident}/events?cursor=0",
        headers=basic(),
        until_events=len(kinds),
    )
    assert [k for _, k, _ in streamed.sse_events()] == kinds

    # Clause 4: nothing credential-bearing reaches the page or the event log.
    page = call(stack.app, "GET", f"/incidents/{incident}", headers=basic())
    assert page.status == 200
    events = json.dumps(
        [dict(e.payload) for e in stack.events.read_after(incident, 0, limit=1000)],
        default=str,
    )
    token = base64.b64encode(f"{UI_USER}:{UI_PASSWORD}".encode()).decode()
    for secret in (UI_PASSWORD, token, EVENT_TOKEN):
        assert secret not in page.text and secret not in events
        assert secret not in streamed.text


def test_several_worker_instances_execute_a_run_exactly_once():
    """Clause 2: concurrent workers polling the same Run -> one executes it (lease fence)."""
    stack = _Stack()
    incident, _ = stack.submit("concurrent")
    workers = []
    for _ in range(2):
        runner, model = stack.runner(
            [_slow(_tool_round(), 0.5), report_from_transcript]
        )
        workers.append((stack.loop(runner, incident), model))
    barrier = threading.Barrier(len(workers))
    results: list = []
    errors: list = []

    def poll(loop):
        barrier.wait()
        try:
            results.extend(loop.poll_once())
        except Exception as error:  # pragma: no cover - reported below
            errors.append(error)

    threads = [threading.Thread(target=poll, args=(loop,)) for loop, _ in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert errors == []
    assert sum(len(model.calls) for _, model in workers) == 2
    assert [o.status for _, o in results].count("published") == 1
    kinds = stack.kinds(incident)
    assert kinds.count("run_claimed") == 1
    _assert_one_terminal(kinds, "run_completed")


def test_a_killed_worker_leaves_committed_rows_and_the_next_worker_resumes_them():
    """Clause 2 (C3 §7): pending tool replayed, committed round not re-asked, tool count kept."""
    stack = _Stack()
    incident, run_id = stack.submit("killed")
    clock = DurableClock(stack.store)
    base = fixture_executor_factory(stack.store, evidence=stack.evidence, clock=clock)

    def dying_factory(lease, input_):
        executor = base(lease, input_)
        real_execute = executor.execute

        def execute(*args, **kwargs):
            real_execute(*args, **kwargs)
            raise _Died()

        executor.execute = execute
        return executor

    first, first_model = stack.runner(
        [_tool_round()], lease_seconds=2, executor_factory=dying_factory
    )
    try:
        first.resume(incident)
    except _Died:
        pass
    assert len(first_model.calls) == 1
    rebuilt = stack.store.rebuild(incident)
    assert rebuilt["run"]["state"] == "running"
    assert len(rebuilt["pending_tools"]) == 1
    before = stack.store.run_usage(run_id)["tool_operations_used"]
    assert before == 1

    time.sleep(3.0)  # the dead worker's 2 s lease lapses; nobody renews it
    second, second_model = stack.runner([report_from_transcript], lease_seconds=2)
    results = stack.loop(second, incident).poll_once()
    assert [(i, o.status) for i, o in results] == [(incident, "published")]
    assert results[0][1].replayed_tools == 1
    assert len(second_model.calls) == 1
    assert stack.store.run_usage(run_id)["tool_operations_used"] >= before
    assert stack.store.rebuild(incident)["run"]["state"] == "completed"
    _assert_one_terminal(stack.kinds(incident), "run_completed")


def test_every_poll_sweeps_an_overdue_running_run_into_a_timeout_handoff():
    """Clause 2 (ADR-0005 d2): an overdue running Run is parked as DEADLINE_EXCEEDED, once."""
    stack = _Stack()
    incident, run_id = stack.submit("sweep")
    stack.store.claim(incident, run_id, uuid4(), fixture_versions(), lease_seconds=300)
    _expire(stack.store, run_id)
    runner, model = stack.runner([])
    loop = stack.loop(runner, incident)
    loop.poll_once()
    row = _run_row(stack.store, run_id)
    assert row["state"] == "waiting_human"
    assert row["owner"] is None and row["lease_until"] is None
    assert model.calls == []
    handoffs = stack.payloads(incident, "run_handoff")
    assert len(handoffs) == 1
    assert handoffs[0]["run_id"] == str(run_id)
    assert handoffs[0]["reasons"] == ["DEADLINE_EXCEEDED"]
    assert stack.store.rebuild(incident)["conclusion"] is None
    loop.poll_once()
    assert len(stack.payloads(incident, "run_handoff")) == 1
    assert "run_completed" not in stack.kinds(incident)


def test_the_resident_loop_investigates_until_stopped():
    """Clause 2: run() keeps polling, drives the submitted Run to a terminal, and exits on stop."""
    stack = _Stack()
    incident, _ = stack.submit("resident")
    runner, _ = stack.runner([_tool_round(), report_from_transcript])
    stop = threading.Event()
    loop = stack.loop(runner, incident, stop=stop, poll=0.05)
    returned: dict = {}
    thread = threading.Thread(target=lambda: returned.update(value=loop.run()))
    thread.start()
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if any(k in TERMINAL for k in stack.kinds(incident)):
                break
            time.sleep(0.05)
    finally:
        stop.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert isinstance(returned.get("value"), int)
    _assert_one_terminal(stack.kinds(incident), "run_completed")


# -- 3. resume on an overdue Run --------------------------------------------------


def test_resume_on_an_overdue_run_is_refused_and_a_note_starts_a_run_the_worker_completes():
    """Clause 3: resume -> 409 ILLEGAL_TRANSITION, row untouched; follow_up renews (#47) and runs."""
    stack = _Stack()
    incident, old = stack.submit("overdue-resume")
    stack.store.claim(incident, old, uuid4(), fixture_versions(), lease_seconds=300)
    _expire(stack.store, old)
    idle, _ = stack.runner([])
    stack.loop(idle, incident).poll_once()
    before = _run_row(stack.store, old)
    assert before["state"] == "waiting_human"

    refused = stack.control(
        incident,
        {"action": "resume", "expected_generation": "0", "idempotency_key": "r"},
    )
    assert refused.status == 409
    assert refused.json() == {"code": "ILLEGAL_TRANSITION"}
    assert _run_row(stack.store, old) == before
    rebuilt = stack.store.rebuild(incident)
    assert rebuilt["control_generation"] == 0
    assert UUID(str(rebuilt["run"]["run_id"])) == old
    stack.loop(idle, incident).poll_once()
    assert "run_claim_refused" not in stack.kinds(incident)

    noted = stack.control(
        incident,
        {
            "action": "follow_up",
            "expected_generation": "0",
            "idempotency_key": "f",
            "text": "Also check the dependency.",
        },
    )
    assert noted.status == 200, noted.text
    fresh = UUID(str(stack.current_run(incident)["run_id"]))
    assert fresh != old
    _assert_snapshot(stack.current_run(incident)["input"], run_id=fresh)

    runner, _ = stack.runner([_tool_round(), report_from_transcript])
    results = stack.loop(runner, incident).poll_once()
    assert [(i, o.status) for i, o in results] == [(incident, "published")]
    assert stack.store.run_usage(fresh)["tool_operations_used"] == 1
    assert _run_row(stack.store, old)["state"] != "running"


def test_resume_on_a_paused_run_within_its_deadline_requeues_it_for_the_worker():
    """Clause 3: resume on a parked, not-overdue Run re-queues it and the worker completes it."""
    stack = _Stack()
    incident, run_id = stack.submit("paused-resume")
    stack.store.claim(incident, run_id, uuid4(), fixture_versions(), lease_seconds=300)
    paused = stack.control(
        incident,
        {"action": "pause", "expected_generation": "0", "idempotency_key": "p"},
    )
    assert paused.status == 200, paused.text
    assert _run_row(stack.store, run_id)["state"] == "paused"
    resumed = stack.control(
        incident,
        {"action": "resume", "expected_generation": "1", "idempotency_key": "r"},
    )
    assert resumed.status == 200, resumed.text
    assert resumed.json()["generation"] == 2
    assert _run_row(stack.store, run_id)["state"] == "queued"

    runner, _ = stack.runner([_tool_round(), report_from_transcript])
    results = stack.loop(runner, incident).poll_once()
    assert [(i, o.status) for i, o in results] == [(incident, "published")]
    assert stack.store.rebuild(incident)["run"]["state"] == "completed"
