"""Opt-in real PG, actual spawned workers; no server lifecycle or live HTTP."""

import multiprocessing as mp
import os
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from scripts.m0.budget import PostgresBudget
from scripts.m0.contracts import BudgetError, RequestIdentity, RunContext
from scripts.m0.postgres_lab import DSN
from scripts.m0.step_store import StepStore

pytestmark = pytest.mark.skipif(
    os.environ.get("M0_STEP_POSTGRES") != "1", reason="explicit PG opt-in required"
)
VERSION = {"state": "v3", "provider": "deepseek", "tool": "fixture-v1"}
CTX = mp.get_context("spawn")


def response():
    return {
        "role": "assistant",
        "content": None,
        "reasoning_content": "SYNTHETIC_PRIVATE_SENTINEL",
        "tool_calls": [
            {
                "id": f"call-{i}",
                "type": "function",
                "function": {"name": "read_fixture", "arguments": "{}"},
            }
            for i in range(2)
        ],
    }


def result(i):
    return {
        "role": "tool",
        "tool_call_id": f"call-{i}",
        "content": '{"status":"ok","observed_at":"original"}',
    }


@pytest.fixture
def lab():
    ledger = PostgresBudget(DSN)
    ledger.install()
    store = StepStore(ledger)
    store.install()
    run = RunContext(
        uuid4(), uuid4(), "deepseek", datetime.now(timezone.utc) + timedelta(minutes=4)
    )
    ledger.initialize(run.experiment_id, 100, run.deadline)
    subject = store.accept(run, "intake", {"request": "synthetic"}, VERSION)
    return store, ledger, run, subject


def child_cut(run, subject, stage, output):
    store = StepStore(PostgresBudget(DSN))
    fence = store.claim(subject, run, uuid4(), VERSION, lease_seconds=0.2)
    rid = uuid4()
    if stage == "before_response":

        def die():
            os._exit(17)

        store.dispatch(fence, "a", 0, {"messages": []}, rid, 20, 4, die)
    step, _ = store.dispatch(fence, "a", 0, {"messages": []}, rid, 20, 4, lambda: None)
    store.commit_response(fence, step, response(), request_id=rid)
    if stage == "partial_tool":
        attempt = uuid4()
        store.dispatch_tool(fence, step, 0, attempt, 4, lambda: None)
        store.commit_tool(fence, step, 0, result(0), attempt_id=attempt)
    output.put((fence, step, rid))
    output.close()
    output.join_thread()
    os._exit(17)


def child_rebuild(run, subject, step, output):
    store = StepStore(PostgresBudget(DSN))
    fence = store.claim(subject, run, uuid4(), VERSION)
    rebuilt = store.rebuild(fence, step)
    output.put((fence, rebuilt["status"], [x["ordinal"] for x in rebuilt["pending"]]))


def test_actual_process_cut_partial_tools_rebuild(lab):
    store, ledger, run, subject = lab
    out = CTX.Queue()
    worker = CTX.Process(target=child_cut, args=(run, subject, "partial_tool", out))
    worker.start()
    old, step, rid = out.get(timeout=10)
    worker.join(10)
    assert worker.exitcode == 17
    time.sleep(0.25)
    recovery = CTX.Process(target=child_rebuild, args=(run, subject, step, out))
    recovery.start()
    fence, status, pending = out.get(timeout=10)
    recovery.join(10)
    assert recovery.exitcode == 0 and status == "pending_tools" and pending == [1]
    assert not store.commit_response(old, step, response(), request_id=rid)
    with pytest.raises(BudgetError, match="OPERATION_NOT_PENDING"):
        store.dispatch_tool(
            fence, step, 0, uuid4(), 4, lambda: pytest.fail("duplicate")
        )
    attempt = uuid4()
    store.dispatch_tool(fence, step, 1, attempt, 4, lambda: None)
    assert store.commit_tool(fence, step, 1, result(1), attempt_id=attempt)
    rebuilt = store.rebuild(fence, step)
    assert [m.get("tool_call_id") for m in rebuilt["messages"][1:]] == [
        "call-0",
        "call-1",
    ]
    assert ledger.snapshot(run.experiment_id)["reserved"] == 20
    assert "SYNTHETIC_PRIVATE_SENTINEL" not in str(store.summary(subject))
    with pytest.raises(BudgetError, match="STEP_ALREADY_COMMITTED"):
        store.dispatch(fence, "a", 0, {"messages": []}, uuid4(), 20, 4, lambda: None)


def test_actual_process_cut_before_response_retains_reservation(lab):
    store, ledger, run, subject = lab
    output = CTX.Queue()
    worker = CTX.Process(
        target=child_cut, args=(run, subject, "before_response", output)
    )
    worker.start()
    worker.join(10)
    assert worker.exitcode == 17
    assert ledger.snapshot(run.experiment_id)["reserved"] == 20
    time.sleep(0.25)
    fence = store.claim(subject, run, uuid4(), VERSION)
    from uuid import uuid5

    step = uuid5(run.run_id, "a:0")
    assert store.rebuild(fence, step)["status"] == "retry_model"
    store.dispatch(fence, "a", 0, {"messages": []}, uuid4(), 20, 4, lambda: None)
    assert ledger.snapshot(run.experiment_id)["reserved"] == 40


def child_barrier_dispatch(fence, ready, proceed, output):
    store = StepStore(PostgresBudget(DSN))

    def barrier():
        ready.set()
        assert proceed.wait(5)

    try:
        store.dispatch(
            fence,
            "barrier",
            0,
            {},
            uuid4(),
            10,
            4,
            lambda: output.put("SENT"),
            before_lock=barrier,
        )
    except BudgetError as exc:
        output.put(str(exc))


def test_cancel_between_preflight_and_launch_cross_process(lab):
    store, _, run, subject = lab
    fence = store.claim(subject, run, uuid4(), VERSION)
    ready, proceed, out = CTX.Event(), CTX.Event(), CTX.Queue()
    worker = CTX.Process(
        target=child_barrier_dispatch, args=(fence, ready, proceed, out)
    )
    worker.start()
    assert ready.wait(5)
    assert store.control(subject, 0, "cancel") == 1
    proceed.set()
    assert out.get(timeout=5) == "CONTROL_DENIED"
    worker.join(10)
    assert worker.exitcode == 0
    assert store.summary(subject)["state"]["state"] == "cancelled"


def test_fencing_intake_versions_and_publication(lab):
    store, ledger, run, subject = lab
    assert store.accept(run, "intake", {"request": "synthetic"}, VERSION) == subject
    with pytest.raises(BudgetError, match="IDENTITY_CONFLICT"):
        store.accept(run, "intake", {"request": "different"}, VERSION)
    fence = store.claim(subject, run, uuid4(), VERSION)
    rid = uuid4()
    step, _ = store.dispatch(fence, "final", 0, {}, rid, 10, 4, lambda: None)
    assert store.commit_response(
        fence,
        step,
        {"role": "assistant", "content": '{"supported": false}'},
        request_id=rid,
    )
    for wrong in (
        replace(fence, owner=uuid4()),
        replace(fence, epoch=0),
        replace(fence, generation=1),
        replace(fence, run=replace(run, run_id=uuid4())),
    ):
        assert not store.publish(wrong, {"supported": False}, step=step)
    assert store.publish(fence, {"supported": False}, step=step)
    assert store.publish(fence, {"supported": False}, step=step)
    assert not store.publish(fence, {"supported": True}, step=step)
    new = replace(run, run_id=uuid4())
    assert store.new_run(subject, 0, new, {"business": "only"}, VERSION) == 1
    assert not store.publish(fence, {"supported": False}, step=step)
    with pytest.raises(BudgetError, match="INCOMPATIBLE_STATE"):
        store.claim(subject, new, uuid4(), {"state": "incompatible"})
    assert store.summary(subject)["state"]["state"] == "blocked"
    assert ledger.snapshot(run.experiment_id)["reserved"] == 10


def test_request_count_unknown_and_absolute_deadline(lab):
    store, ledger, run, subject = lab
    fence = store.claim(subject, run, uuid4(), VERSION)
    rid = uuid4()
    store.dispatch(fence, "limit", 0, {}, rid, 10, 1, lambda: None)
    ledger.retain_unknown(RequestIdentity(run, rid))
    with pytest.raises(BudgetError, match="REQUEST_LIMIT"):
        store.dispatch(fence, "limit", 0, {}, uuid4(), 10, 1, lambda: None)
    assert ledger.snapshot(run.experiment_id)["unknown"] == 10
    expired = replace(
        fence,
        run=replace(run, deadline=datetime.now(timezone.utc) - timedelta(seconds=1)),
    )
    with pytest.raises(BudgetError, match="CONTROL_DENIED"):
        store.dispatch(expired, "limit", 1, {}, uuid4(), 10, 4, lambda: None)


def child_launch_barrier(fence, started, release, output):
    store = StepStore(PostgresBudget(DSN))

    def starter():
        started.set()
        assert release.wait(4)
        output.put("initiated")

    store.dispatch(fence, "serialize", 0, {}, uuid4(), 10, 4, starter)


def child_cancel(subject, done):
    StepStore(PostgresBudget(DSN)).control(subject, 0, "cancel")
    done.set()


def test_control_commit_serializes_with_actual_initiation(lab):
    store, _, run, subject = lab
    fence = store.claim(subject, run, uuid4(), VERSION)
    started, release, done, output = CTX.Event(), CTX.Event(), CTX.Event(), CTX.Queue()
    worker = CTX.Process(
        target=child_launch_barrier, args=(fence, started, release, output)
    )
    worker.start()
    assert started.wait(5)
    control = CTX.Process(target=child_cancel, args=(subject, done))
    control.start()
    assert not done.wait(0.3)
    release.set()
    assert output.get(timeout=5) == "initiated"
    assert done.wait(5)
    worker.join(10)
    control.join(10)
    assert worker.exitcode == control.exitcode == 0
    with pytest.raises(BudgetError, match="CONTROL_DENIED"):
        store.dispatch(
            fence,
            "serialize",
            1,
            {},
            uuid4(),
            10,
            4,
            lambda: pytest.fail("late launch"),
        )


def child_tool_before_commit(run, subject):
    store = StepStore(PostgresBudget(DSN))
    fence = store.claim(subject, run, uuid4(), VERSION, lease_seconds=0.2)
    rid = uuid4()
    step, _ = store.dispatch(fence, "cut", 0, {}, rid, 10, 4, lambda: None)
    store.commit_response(fence, step, response(), request_id=rid)
    store.dispatch_tool(fence, step, 0, uuid4(), 2, lambda: {"status": "returned"})
    os._exit(17)


def test_tool_returned_before_commit_is_bounded_repeat(lab):
    store, _, run, subject = lab
    worker = CTX.Process(target=child_tool_before_commit, args=(run, subject))
    worker.start()
    worker.join(10)
    assert worker.exitcode == 17
    time.sleep(0.25)
    fence = store.claim(subject, run, uuid4(), VERSION)
    from uuid import uuid5

    step = uuid5(run.run_id, "cut:0")
    assert [o["ordinal"] for o in store.rebuild(fence, step)["pending"]] == [0, 1]
    attempt = uuid4()
    store.dispatch_tool(fence, step, 0, attempt, 2, lambda: None)
    with pytest.raises(BudgetError, match="QUERY_LIMIT"):
        store.dispatch_tool(fence, step, 1, uuid4(), 2, lambda: None)
    assert store.commit_tool(
        fence, step, 0, result(0), attempt_id=attempt, status="failed"
    )


@pytest.mark.parametrize("kind", ["model", "tool"])
def test_lease_expiry_during_preparation_never_initiates(lab, monkeypatch, kind):
    store, ledger, run, subject = lab
    fence = store.claim(
        subject, run, uuid4(), VERSION, lease_seconds=0.15 if kind == "model" else 1
    )
    step = None
    if kind == "tool":
        rid = uuid4()
        step, _ = store.dispatch(fence, "expiry", 0, {}, rid, 10, 4, lambda: None)
        store.commit_response(fence, step, response(), request_id=rid)
        with ledger._transaction() as conn:
            conn.execute(
                "UPDATE m0_v3_subject SET lease_until=clock_timestamp()+interval '150 milliseconds' WHERE id=%s",
                (subject,),
            )
    original = ledger._experiment

    def delayed(conn, identity):
        value = original(conn, identity)
        conn.execute("SELECT pg_sleep(0.2)")
        return value

    monkeypatch.setattr(ledger, "_experiment", delayed)
    calls = []
    with pytest.raises(BudgetError, match="CONTROL_DENIED"):
        if kind == "model":
            store.dispatch(
                fence, "expiry", 0, {}, uuid4(), 10, 4, lambda: calls.append("sent")
            )
        else:
            store.dispatch_tool(
                fence, step, 0, uuid4(), 4, lambda: calls.append("sent")
            )
    assert calls == []
    assert ledger.snapshot(run.experiment_id)["reserved"] == 10


def test_cancelled_state_wins_over_incompatible_claim(lab):
    store, _, run, subject = lab
    store.control(subject, 0, "cancel")
    with pytest.raises(BudgetError, match="CONTROL_DENIED"):
        store.claim(subject, run, uuid4(), {"state": "wrong"})
    assert store.summary(subject)["state"]["state"] == "cancelled"


def test_tool_commit_requires_matching_latest_attempt(lab):
    store, _, run, subject = lab
    fence = store.claim(subject, run, uuid4(), VERSION)
    rid = uuid4()
    step, _ = store.dispatch(fence, "attempt", 0, {}, rid, 10, 4, lambda: None)
    store.commit_response(fence, step, response(), request_id=rid)
    with pytest.raises(BudgetError, match="UNKNOWN_TOOL_ATTEMPT"):
        store.commit_tool(fence, step, 0, result(0), attempt_id=uuid4())
    first, second = uuid4(), uuid4()
    store.dispatch_tool(fence, step, 0, first, 4, lambda: None)
    store.dispatch_tool(fence, step, 0, second, 4, lambda: None)
    with pytest.raises(BudgetError, match="UNKNOWN_TOOL_ATTEMPT"):
        store.commit_tool(fence, step, 0, result(0), attempt_id=first)
    with pytest.raises(BudgetError, match="UNKNOWN_TOOL_ATTEMPT"):
        store.commit_tool(fence, step, 1, result(1), attempt_id=second)
    assert store.commit_tool(fence, step, 0, result(0), attempt_id=second)


def test_model_response_requires_current_physical_request(lab):
    store, _, run, subject = lab
    fence = store.claim(subject, run, uuid4(), VERSION)
    first, second = uuid4(), uuid4()
    step, _ = store.dispatch(fence, "retry", 0, {}, first, 10, 4, lambda: None)
    store.dispatch(fence, "retry", 0, {}, second, 10, 4, lambda: None)
    with pytest.raises(BudgetError, match="UNKNOWN_MODEL_ATTEMPT"):
        store.commit_response(fence, step, response(), request_id=first)
    assert store.commit_response(fence, step, response(), request_id=second)


def child_guarded_http(fence, request_id, ready, allow, output, port):
    import socket

    from scripts.m0.step_store import digest

    ready.set()
    assert allow.wait(10)
    try:
        with StepStore(PostgresBudget(DSN)).send_guard(
            fence, request_id, input_hash=digest({})
        ) as (release, check):
            check()
            with socket.create_connection(("127.0.0.1", port), timeout=2) as stream:
                stream.sendall(b"GET /probe HTTP/1.1\r\nHost: localhost\r\n\r\n")
            release()
        output.put("SENT")
    except BudgetError as exc:
        output.put(str(exc))
    output.close()
    output.join_thread()


def parent_prepares_then_waits(run, subject, ready, allow, output, port):
    store = StepStore(PostgresBudget(DSN))
    fence = store.claim(subject, run, uuid4(), VERSION)
    rid = uuid4()
    store.prepare_request(fence, "orphan", 0, {}, rid, 10, 4)
    child = CTX.Process(
        target=child_guarded_http, args=(fence, rid, ready, allow, output, port)
    )
    child.start()
    output.put((str(rid), child.pid))
    output.close()
    output.join_thread()
    time.sleep(30)


def test_parent_sigkill_then_cancel_orphan_transport_sends_zero_http(lab):
    import socket

    store, ledger, run, subject = lab
    ready, allow, output = CTX.Event(), CTX.Event(), CTX.Queue()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(0.2)
        parent = CTX.Process(
            target=parent_prepares_then_waits,
            args=(run, subject, ready, allow, output, listener.getsockname()[1]),
        )
        parent.start()
        request_id, child_pid = output.get(timeout=10)
        assert ready.wait(10)
        parent.kill()
        parent.join(5)
        assert parent.exitcode < 0
        store.control(subject, 0, "cancel")
        allow.set()
        assert output.get(timeout=10) == "CONTROL_DENIED"
        with pytest.raises(TimeoutError):
            listener.accept()
        assert ledger.snapshot(run.experiment_id)["reserved"] == 10


def test_transport_grant_one_use_and_snapshot_binding(lab):
    from scripts.m0.step_store import digest

    store, _, run, subject = lab
    fence = store.claim(subject, run, uuid4(), VERSION)
    rid = uuid4()
    store.prepare_request(fence, "grant", 0, {}, rid, 10, 4)
    with pytest.raises(BudgetError, match="DELIVERY_INPUT_MISMATCH"):
        with store.send_guard(fence, rid, input_hash=digest({"tampered": True})):
            pytest.fail("wrong body")
    with store.send_guard(fence, rid, input_hash=digest({})) as (release, check):
        check()
        release()
    with pytest.raises(BudgetError, match="SEND_GRANT_CONSUMED"):
        with store.send_guard(fence, rid, input_hash=digest({})):
            pytest.fail("duplicate send")


@pytest.mark.parametrize("prior_action", [None, "cancel", "correct"])
def test_current_control_snapshot_crosses_v3_seam_after_new_run(lab, prior_action):
    import json

    from scripts.m0.outcomes_v3 import IncidentOutcome, IncidentScenario, check_outcome

    store, _, run, subject = lab
    old = store.claim(subject, run, uuid4(), VERSION)
    generation = 0
    if prior_action:
        generation = store.control(
            subject, 0, prior_action, payload={"text": "CONTROL_PAYLOAD_NOT_PUBLIC"}
        )
    fresh = replace(run, run_id=uuid4())
    generation = store.new_run(
        subject, generation, fresh, {"business": "allowed facts"}, VERSION
    )
    current = store.claim(subject, fresh, uuid4(), VERSION)
    # Exercise independently stale Run and generation, not only the old combined fence.
    assert not store.publish(replace(current, run=run), {}, step=uuid4())
    assert not store.publish(
        replace(current, generation=generation - 1), {}, step=uuid4()
    )
    assert not store.publish(old, {}, step=uuid4())
    # Non-control accepted audit events and rejected control events must not
    # appear in the public generation history or create duplicate generations.
    with store.ledger._transaction() as conn:
        for event in ("claim", "model_response", "publish"):
            store._audit(conn, subject, event, True, generation)
        store._audit(conn, subject, "new_run", False, generation)
    snapshot = store.control_snapshot(subject)
    expected_actions = ([prior_action] if prior_action else []) + ["new_run"]
    assert [e["action"] for e in snapshot["controls"]] == expected_actions
    assert [e["generation"] for e in snapshot["controls"]] == list(
        range(1, generation + 1)
    )
    assert snapshot["current_run"] == str(fresh.run_id)
    assert set(snapshot) == {"current_run", "final_generation", "controls"}
    assert all(set(e) == {"generation", "action", "at"} for e in snapshot["controls"])
    assert "CONTROL_PAYLOAD_NOT_PUBLIC" not in json.dumps(snapshot)
    target = {
        "kind": "compose",
        "integration_id": "test",
        "deployment_instance": "fixture",
        "service": "test",
        "container_id": "fixture-cid",
        "image_digest": "sha256:fixture",
        "telemetry_instance": "fixture-host",
        "mapping_revision": "fixture-v1",
        "config_revision": "fixture-v1",
    }
    subject_dto = {"kind": "incident", "id": str(subject), "target": target}
    scenario = IncidentScenario.model_validate_json(
        json.dumps(
            {
                "schema_version": "m0-public-v3",
                "scenario_id": str(subject),
                "versions": VERSION,
                "agent_input": {
                    "subject": subject_dto,
                    "request": "Continue the investigation",
                    "initial_views": [],
                },
                "trusted": {
                    **snapshot,
                    "scope": {
                        "revision": "fixture-v1",
                        "targets": [target],
                        "interfaces": [],
                        "window": {
                            "start": "2026-09-10T00:00:00Z",
                            "end": "2026-09-10T00:01:00Z",
                        },
                    },
                    "artifacts": [],
                    "deliveries": [],
                    "execution": "running",
                    "observed_actions": [],
                },
            }
        )
    )
    outcome = IncidentOutcome.model_validate_json(
        json.dumps(
            {
                "schema_version": "m0-public-v3",
                "scenario_id": str(subject),
                "versions": VERSION,
                "subject": subject_dto,
                "run_id": snapshot["current_run"],
                "report_step_id": "not_started",
                "report_request_id": "not_started",
                "control_generation": generation,
                "execution": "running",
                "assessment_status": "incomplete",
                "conclusion": "inconclusive",
                "claims": [],
                "evidence_ids": [],
                "gaps": ["Fresh Run has not queried evidence yet"],
                "handoff": True,
                "health": "unknown",
            }
        )
    )
    assert check_outcome(scenario, outcome) == []


def test_new_run_has_own_limits_without_resetting_experiment_budget(lab):
    store, ledger, run, subject = lab
    fence = store.claim(subject, run, uuid4(), VERSION)
    requests = []
    for _ in range(4):
        rid = uuid4()
        step, _ = store.dispatch(fence, "limit", 0, {}, rid, 10, 4, lambda: None)
        requests.append(rid)
    ledger.retain_unknown(RequestIdentity(run, requests[0]))
    store.commit_response(fence, step, response(), request_id=requests[-1])
    for _ in range(20):
        store.dispatch_tool(fence, step, 0, uuid4(), 20, lambda: None)
    with pytest.raises(BudgetError, match="REQUEST_LIMIT"):
        store.dispatch(fence, "limit", 1, {}, uuid4(), 10, 4, lambda: None)
    with pytest.raises(BudgetError, match="QUERY_LIMIT"):
        store.dispatch_tool(fence, step, 0, uuid4(), 20, lambda: None)
    # A new worker/epoch on this same Run must not reset either Run counter.
    with ledger._transaction() as conn:
        conn.execute(
            "UPDATE m0_v3_subject SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",
            (subject,),
        )
    recovered = store.claim(subject, run, uuid4(), VERSION)
    with pytest.raises(BudgetError, match="REQUEST_LIMIT"):
        store.dispatch(recovered, "limit", 1, {}, uuid4(), 10, 4, lambda: None)
    with pytest.raises(BudgetError, match="QUERY_LIMIT"):
        store.dispatch_tool(recovered, step, 0, uuid4(), 20, lambda: None)
    fresh = replace(run, run_id=uuid4())
    store.new_run(subject, 0, fresh, {"business": "continued"}, VERSION)
    current = store.claim(subject, fresh, uuid4(), VERSION)
    rid = uuid4()
    fresh_step, _ = store.dispatch(current, "fresh", 0, {}, rid, 10, 4, lambda: None)
    store.commit_response(current, fresh_step, response(), request_id=rid)
    store.dispatch_tool(current, fresh_step, 0, uuid4(), 20, lambda: None)
    totals = ledger.snapshot(run.experiment_id)
    assert totals["unknown"] == 10 and totals["reserved"] == 40
    # Fresh Run has local headroom, but prior reservations still consume ceiling.
    with pytest.raises(BudgetError, match="BUDGET_EXHAUSTED"):
        store.dispatch(current, "fresh", 1, {}, uuid4(), 60, 4, lambda: None)
    with pytest.raises(BudgetError, match="IDENTITY_CONFLICT"):
        store.new_run(
            subject,
            1,
            replace(
                fresh, run_id=uuid4(), deadline=run.deadline + timedelta(seconds=1)
            ),
            {},
            VERSION,
        )
    with ledger._transaction() as conn:
        count = conn.execute(
            "SELECT count(*) AS n FROM m0_v3_dispatch d JOIN m0_v3_step s ON s.id=d.step JOIN m0_runs r ON r.id=s.run_id WHERE r.experiment_id=%s",
            (run.experiment_id,),
        ).fetchone()["n"]
        queries = conn.execute(
            "SELECT count(*) AS n FROM m0_v3_tool_attempt a JOIN m0_v3_step s ON s.id=a.step JOIN m0_runs r ON r.id=s.run_id WHERE r.experiment_id=%s",
            (run.experiment_id,),
        ).fetchone()["n"]
        deadline = conn.execute(
            "SELECT deadline FROM m0_experiments WHERE id=%s", (run.experiment_id,)
        ).fetchone()["deadline"]
    assert (count, queries) == (5, 21)
    assert deadline == run.deadline == fresh.deadline


def test_prepared_unclaimed_request_cannot_commit_or_publish(lab):
    store, ledger, run, subject = lab
    fence = store.claim(subject, run, uuid4(), VERSION)
    request_id = uuid4()
    step = store.prepare_request(fence, "unclaimed", 0, {}, request_id, 10, 4)
    with pytest.raises(BudgetError, match="MODEL_REQUEST_NOT_INITIATED"):
        store.commit_response(
            fence,
            step,
            {"role": "assistant", "content": '{"ok":true}'},
            request_id=request_id,
        )
    with pytest.raises(BudgetError, match="UNCOMMITTED_CANDIDATE"):
        store.publish(fence, {"ok": True}, step=step)
    assert store.rebuild(fence, step)["status"] == "retry_model"
    assert ledger.snapshot(run.experiment_id)["reserved"] == 10


def test_failed_prepare_grant_insert_leaves_no_direct_dispatch_lookalike(
    lab, monkeypatch
):
    from contextlib import contextmanager

    import psycopg

    import scripts.m0.step_store as module

    store, ledger, run, subject = lab
    fence = store.claim(subject, run, uuid4(), VERSION)
    request_id = uuid4()
    connect = module.psycopg.connect

    class FailGrant:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, key):
            return getattr(self.conn, key)

        @property
        def autocommit(self):
            return self.conn.autocommit

        @autocommit.setter
        def autocommit(self, value):
            self.conn.autocommit = value

        def execute(self, sql, params=None):
            if "INSERT INTO m0_v3_send_grant" in sql:
                raise psycopg.OperationalError("synthetic grant persistence failure")
            return self.conn.execute(sql, params)

    @contextmanager
    def failing_connect(*args, **kwargs):
        with connect(*args, **kwargs) as conn:
            yield FailGrant(conn)

    with monkeypatch.context() as patch:
        patch.setattr(module.psycopg, "connect", failing_connect)
        with pytest.raises(BudgetError, match="STORAGE_UNAVAILABLE"):
            store.prepare_request(fence, "atomic-grant", 0, {}, request_id, 10, 4)
    with ledger._transaction() as conn:
        dispatches = conn.execute(
            "SELECT count(*) AS n FROM m0_v3_dispatch WHERE request=%s", (request_id,)
        ).fetchone()["n"]
        grants = conn.execute(
            "SELECT count(*) AS n FROM m0_v3_send_grant WHERE request=%s", (request_id,)
        ).fetchone()["n"]
    assert (dispatches, grants, ledger.snapshot(run.experiment_id)["reserved"]) == (
        0,
        0,
        0,
    )


@pytest.mark.parametrize("prepared", [False, True])
def test_claimed_prepare_and_direct_dispatch_both_can_commit_and_publish(lab, prepared):
    from scripts.m0.step_store import digest

    store, _, run, subject = lab
    fence = store.claim(subject, run, uuid4(), VERSION)
    request_id = uuid4()
    if prepared:
        step = store.prepare_request(fence, "positive", 0, {}, request_id, 10, 4)
        with store.send_guard(fence, request_id, input_hash=digest({})) as (
            release,
            check,
        ):
            check()
            release()  # Simulated initiation, never a provider request.
    else:
        step, _ = store.dispatch(
            fence, "positive", 0, {}, request_id, 10, 4, lambda: None
        )
    assert store.commit_response(
        fence,
        step,
        {"role": "assistant", "content": '{"ok":true}'},
        request_id=request_id,
    )
    assert store.publish(fence, {"ok": True}, step=step)


def test_claimed_grant_never_replaces_latest_attempt_or_fence(lab):
    from scripts.m0.step_store import digest

    store, _, run, subject = lab
    fence = store.claim(subject, run, uuid4(), VERSION)
    first, latest = uuid4(), uuid4()
    step = store.prepare_request(fence, "fences", 0, {}, first, 10, 4)
    with store.send_guard(fence, first, input_hash=digest({})) as (release, check):
        check()
        release()
    store.prepare_request(fence, "fences", 0, {}, latest, 10, 4)
    assistant = {"role": "assistant", "content": '{"ok":true}'}
    with pytest.raises(BudgetError, match="UNKNOWN_MODEL_ATTEMPT"):
        store.commit_response(fence, step, assistant, request_id=first)
    with store.send_guard(fence, latest, input_hash=digest({})) as (release, check):
        check()
        release()
    for wrong in (
        replace(fence, run=replace(run, run_id=uuid4())),
        replace(fence, owner=uuid4()),
        replace(fence, epoch=fence.epoch + 1),
        replace(fence, generation=fence.generation + 1),
    ):
        assert not store.commit_response(wrong, step, assistant, request_id=latest)
    assert store.commit_response(fence, step, assistant, request_id=latest)
    with store.ledger._transaction() as conn:
        conn.execute(
            "UPDATE m0_v3_subject SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",
            (subject,),
        )
    assert not store.commit_response(fence, step, assistant, request_id=latest)
    assert not store.publish(fence, {"ok": True}, step=step)


def child_crash_after_prepare(run, subject, request_id, output):
    store = StepStore(PostgresBudget(DSN))
    fence = store.claim(subject, run, uuid4(), VERSION)
    step = store.prepare_request(fence, "crashed-prepare", 0, {}, request_id, 10, 4)
    output.put((fence, step))
    output.close()
    output.join_thread()
    os._exit(23)


def test_process_exit_after_prepare_preserves_unclaimed_barrier(lab):
    store, ledger, run, subject = lab
    request_id = uuid4()
    output = CTX.Queue()
    child = CTX.Process(
        target=child_crash_after_prepare, args=(run, subject, request_id, output)
    )
    child.start()
    fence, step = output.get(timeout=10)
    child.join(10)
    assert child.exitcode == 23
    with ledger._transaction() as conn:
        row = conn.execute(
            "SELECT g.claimed FROM m0_v3_dispatch d JOIN m0_v3_send_grant g ON g.request=d.request WHERE d.request=%s",
            (request_id,),
        ).fetchone()
    assert row == {"claimed": False}
    with pytest.raises(BudgetError, match="MODEL_REQUEST_NOT_INITIATED"):
        store.commit_response(
            fence,
            step,
            {"role": "assistant", "content": '{"ok":true}'},
            request_id=request_id,
        )
    assert ledger.snapshot(run.experiment_id)["reserved"] == 10


@pytest.mark.parametrize("action", ["cancel", "correct"])
def test_control_clears_active_final_and_preserves_report_history(lab, action):
    store, ledger, run, subject = lab
    fence = store.claim(subject, run, uuid4(), VERSION)
    request_id = uuid4()
    step, _ = store.dispatch(fence, "published", 0, {}, request_id, 10, 4, lambda: None)
    candidate = {"summary": "historical synthetic report"}
    import json

    assert store.commit_response(
        fence,
        step,
        {"role": "assistant", "content": json.dumps(candidate)},
        request_id=request_id,
    )
    assert store.publish(fence, candidate, step=step)
    with ledger._transaction() as conn:
        before = conn.execute(
            "SELECT * FROM m0_v3_report WHERE run_id=%s", (run.run_id,)
        ).fetchone()
    assert store.summary(subject)["state"]["published"] is True
    assert store.control(subject, 0, action) == 1
    assert store.summary(subject)["state"]["published"] is False
    assert not store.publish(fence, candidate, step=step)
    with ledger._transaction() as conn:
        after = conn.execute(
            "SELECT * FROM m0_v3_report WHERE run_id=%s", (run.run_id,)
        ).fetchone()
        current = conn.execute(
            "SELECT state,generation,final FROM m0_v3_subject WHERE id=%s", (subject,)
        ).fetchone()
    assert after == before
    assert current == {
        "state": "cancelled" if action == "cancel" else "waiting_human",
        "generation": 1,
        "final": None,
    }
    fresh = replace(run, run_id=uuid4())
    store.new_run(subject, 1, fresh, {"business": "allowed follow-up"}, VERSION)
    assert store.claim(subject, fresh, uuid4(), VERSION).generation == 2
    assert store.summary(subject)["state"]["published"] is False


def test_new_run_empty_versions_rejects_without_any_durable_transition(lab):
    store, ledger, run, subject = lab
    old = store.claim(subject, run, uuid4(), VERSION)
    fresh = replace(run, run_id=uuid4())
    before = store.summary(subject)
    budget_before = ledger.snapshot(run.experiment_id)
    with pytest.raises(BudgetError, match="INVALID_INPUT"):
        store.new_run(subject, 0, fresh, {"business": "new request"}, {})
    assert store.summary(subject) == before
    assert ledger.snapshot(run.experiment_id) == budget_before
    with ledger._transaction() as conn:
        assert (
            conn.execute(
                "SELECT id FROM m0_runs WHERE id=%s", (fresh.run_id,)
            ).fetchone()
            is None
        )
        assert (
            conn.execute(
                "SELECT run_id FROM m0_v3_run_input WHERE run_id=%s", (fresh.run_id,)
            ).fetchone()
            is None
        )
        assert (
            conn.execute(
                "SELECT count(*) AS count FROM m0_v3_audit WHERE subject=%s AND event='new_run'",
                (subject,),
            ).fetchone()["count"]
            == 0
        )
    assert store.new_run(subject, 0, fresh, {"business": "new request"}, VERSION) == 1
    current = store.claim(subject, fresh, uuid4(), VERSION)
    assert current.generation == 1
    assert not store.publish(old, {}, step=uuid4())
    assert ledger.snapshot(run.experiment_id) == budget_before
