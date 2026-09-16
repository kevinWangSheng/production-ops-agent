import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import errors, sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from opspilot.domain import RUN_EXECUTION
from opspilot.persistence import DurableStore, Lease, PersistenceError
from opspilot.worker import Worker
from scripts.m0.postgres_lab import DSN

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)


def test_commit_visibility_restart_control_late_and_budget():
    store = DurableStore(DSN)
    store.install()
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
        budget_limit=10,
        versions={"state": "v1"},
    )
    owner = uuid4()
    lease = store.claim(incident, run, owner, {"state": "v1"})
    reservation = uuid4()
    store.reserve_budget(lease, reservation, 7)
    with pytest.raises(PersistenceError, match="BUDGET_EXHAUSTED"):
        store.reserve_budget(lease, uuid4(), 4)
    step = store.commit_step(lease, "round-0", {"role": "assistant", "tool_calls": []})
    assert step
    assert store.rebuild(incident)["steps"][0]["status"] == "response_committed"
    generation = store.control(incident, 0, "cancel", "operator")
    assert generation == 1
    assert store.publish(lease, {"result": "supported"}, step_id=step) is False
    rebuilt = store.rebuild(incident)
    assert rebuilt["control_generation"] == 1
    assert rebuilt["conclusion"] is None


def test_worker_subprocess_kill_then_resume_from_business_rows():
    """A killed process leaves only PG state; a fresh Worker claims a new epoch."""
    store = DurableStore(DSN)
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-worker-kill-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
        budget_limit=10,
        versions={"state": "v1"},
    )
    code = (
        "import time,sys; from uuid import UUID; "
        "from opspilot.persistence import DurableStore; "
        "s=DurableStore(sys.argv[1]); s.claim(UUID(sys.argv[2]), UUID(sys.argv[3]), UUID(sys.argv[4]), {'state':'v1'}, lease_seconds=1); print('CLAIMED', flush=True); time.sleep(30)"
    )
    child = subprocess.Popen(
        [
            os.fspath(__import__("sys").executable),
            "-c",
            code,
            DSN,
            str(incident),
            str(run),
            str(uuid4()),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None and child.stdout.readline().strip() == "CLAIMED"
    child.kill()
    child.wait(timeout=5)
    time.sleep(1.2)
    session = Worker.create(store, {"state": "v1"}).resume(incident, lease_seconds=5)
    assert session.lease.epoch == 2


def test_rebuild_rejects_malformed_persisted_tool_calls():
    store = DurableStore(DSN)
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-malformed-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
        budget_limit=10,
        versions={"state": "v1"},
    )
    lease = store.claim(incident, run, uuid4(), {"state": "v1"})
    step = store.commit_step(lease, "malformed", {"tool_calls": []})
    with store.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_steps SET response=%s WHERE step_id=%s",
            (Jsonb({"tool_calls": "abc"}), step),
        )
    with pytest.raises(PersistenceError, match="INCONSISTENT_STATE"):
        store.rebuild(incident)


def test_recovered_session_checks_epoch_before_dispatch():
    store = DurableStore(DSN)
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-session-fence-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
        budget_limit=10,
        versions={"state": "v1"},
    )
    first = store.claim(incident, run, uuid4(), {"state": "v1"}, lease_seconds=1)
    step = store.commit_step(
        first, "round", {"tool_calls": [{"name": "query", "arguments": {}}]}
    )
    store.commit_tool(first, step, 0, {"ok": True})
    # A new worker can claim only after the old lease expires.
    time.sleep(1.2)
    second = store.claim(incident, run, uuid4(), {"state": "v1"}, lease_seconds=30)
    assert not store.lease_current(first)
    assert store.lease_current(second)


def test_incompatible_versions_block_without_silent_resume():
    store = DurableStore(DSN)
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-incompat-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
        budget_limit=10,
        versions={"state": "v1"},
    )
    with pytest.raises(PersistenceError, match="INCOMPATIBLE_STATE"):
        store.claim(incident, run, uuid4(), {"state": "v2"})
    assert store.rebuild(incident)["run"]["state"] == "blocked"


def test_partial_tool_checkpoint_and_lease_fencing():
    store = DurableStore(DSN)
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-tools-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
        budget_limit=10,
        versions={"state": "v1"},
    )
    lease = store.claim(incident, run, uuid4(), {"state": "v1"}, lease_seconds=1)
    step = store.commit_step(
        lease, "round-1", {"tool_calls": [{"id": "a"}, {"id": "b"}]}
    )
    store.commit_tool(lease, step, 0, {"ok": True})
    assert store.rebuild(incident)["pending_tools"][0]["ordinal"] == 1


def test_pause_resume_fences_run_and_terminal_incident_cannot_reclaim():
    store = DurableStore(DSN)
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-control-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
        budget_limit=10,
        versions={"state": "v1"},
    )
    lease = store.claim(incident, run, uuid4(), {"state": "v1"})
    assert store.control(incident, 0, "pause", "operator") == 1
    assert store.control(incident, 1, "resume", "operator") == 2
    resumed = store.claim(incident, run, uuid4(), {"state": "v1"})
    assert resumed.control_generation == 2
    final_step = store.commit_step(resumed, "final", {"result": "supported"})
    assert store.publish(resumed, {"result": "supported"}, step_id=final_step) is True
    with pytest.raises(PersistenceError, match="ILLEGAL_TRANSITION"):
        store.control(incident, 2, "resume", "operator")
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.claim(incident, run, uuid4(), {"state": "v1"})
    assert store.publish(lease, {"result": "late"}, step_id=uuid4()) is False


def test_expired_lease_cannot_publish_or_reserve():
    store = DurableStore(DSN)
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-expiry-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
        budget_limit=10,
        versions={"state": "v1"},
    )
    lease = store.claim(incident, run, uuid4(), {"state": "v1"})
    with store.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_runs SET lease_until=clock_timestamp()-interval '1 second' WHERE run_id=%s",
            (run,),
        )
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.reserve_budget(lease, uuid4(), 1)
    assert store.publish(lease, {"result": "expired"}, step_id=uuid4()) is False


def test_follow_up_and_correction_advance_generation_and_fence_old_lease():
    store = DurableStore(DSN)
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-human-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
        budget_limit=10,
        versions={"state": "v1"},
    )
    lease = store.claim(incident, run, uuid4(), {"state": "v1"})
    assert store.control(incident, 0, "follow_up", "operator") == 1
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.commit_step(lease, "old", {"result": "stale"})
    assert store.control(incident, 1, "correct", "operator") == 2


def test_correction_rejects_late_publish_and_keeps_history_only():
    """纠正后的旧代际结果只能进入 late_result 历史，不能成为结论。"""
    store = DurableStore(DSN)
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-late-correction-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
        budget_limit=10,
        versions={"state": "v1"},
    )
    stale = store.claim(incident, run, uuid4(), {"state": "v1"})
    step = store.commit_step(stale, "old-final", {"result": "old"})
    assert store.control(incident, 0, "correct", "operator") == 1
    assert store.publish(stale, {"result": "old"}, step_id=step) is False
    assert store.publish(stale, {"result": "old-retry"}, step_id=step) is False
    rebuilt = store.rebuild(incident)
    assert rebuilt["conclusion"] is None
    late = [item for item in rebuilt["steps"] if item["status"] == "late_result"]
    assert len(late) == 1
    assert late[0]["logical_key"] == f"late_result:publish:{step}"
    assert late[0]["sequence"] >= 0
    assert late[0]["observed_at"] is not None
    assert late[0]["response"] == {"result": "old"}


def test_late_step_and_tool_results_are_recorded_as_history():
    store = DurableStore(DSN)
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-late-writes-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
        budget_limit=10,
        versions={"state": "v1"},
    )
    stale = store.claim(incident, run, uuid4(), {"state": "v1"})
    step = store.commit_step(stale, "round", {"tool_calls": [{"id": "x"}]})
    assert store.control(incident, 0, "follow_up", "operator") == 1
    fresh = store.claim(incident, run, uuid4(), {"state": "v1"})
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.commit_step(stale, "late-round", {"result": "late"})
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.commit_tool(fresh, step, 0, {"result": "late-tool"})
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.commit_step(stale, "late-round", {"result": "late-retry"})
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.commit_tool(fresh, step, 0, {"result": "late-tool-retry"})
    steps = store.rebuild(incident)["steps"]
    original = next(item for item in steps if item["step_id"] == step)
    late = [item for item in steps if item["status"] == "late_result"]
    assert len(late) == 2
    assert {item["logical_key"] for item in late} == {
        "late_result:step:late-round",
        f"late_result:tool:{step}:0",
    }
    assert all(item["sequence"] > original["sequence"] for item in late)
    assert all(item["observed_at"] is not None for item in late)
    assert [item["logical_key"] for item in late] == [
        "late_result:step:late-round",
        f"late_result:tool:{step}:0",
    ]
    assert late[0]["response"] == {"result": "late"}
    assert late[1]["response"] == {"result": "late-tool"}


def test_commit_tool_rejects_unknown_step_without_history():
    store = DurableStore(DSN)
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-unknown-tool-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
        budget_limit=10,
        versions={"state": "v1"},
    )
    lease = store.claim(incident, run, uuid4(), {"state": "v1"})
    with pytest.raises(PersistenceError, match="UNKNOWN_IDENTITY"):
        store.commit_tool(lease, uuid4(), 0, {"ok": True})
    assert store.rebuild(incident)["steps"] == []


def test_expired_late_step_is_history_and_not_pending_work():
    store = DurableStore(DSN)
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-expired-late-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
        budget_limit=10,
        versions={"state": "v1"},
    )
    stale = store.claim(incident, run, uuid4(), {"state": "v1"}, lease_seconds=1)
    with store.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_runs SET lease_until=clock_timestamp()-interval '1 second' WHERE run_id=%s",
            (run,),
        )
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.commit_step(stale, "round-0", {"tool_calls": [{"id": "a"}]})
    rebuilt = store.rebuild(incident)
    late = [item for item in rebuilt["steps"] if item["status"] == "late_result"]
    assert len(late) == 1
    assert rebuilt["pending_tools"] == []
    fresh = store.claim(incident, run, uuid4(), {"state": "v1"})
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.commit_tool(fresh, late[0]["step_id"], 0, {"ok": True})
    still = next(
        item
        for item in store.rebuild(incident)["steps"]
        if item["step_id"] == late[0]["step_id"]
    )
    assert still["status"] == "late_result"
    assert still["tool_results"] == []
    assert store.rebuild(incident)["pending_tools"] == []


def test_live_steps_cannot_use_the_late_result_key_namespace():
    store = DurableStore(DSN)
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-late-namespace-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
        budget_limit=10,
        versions={"state": "v1"},
    )
    stale = store.claim(incident, run, uuid4(), {"state": "v1"})
    colliding = store.commit_step(stale, "late-step:foo", {"result": "live"})
    assert store.control(incident, 0, "follow_up", "operator") == 1
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.commit_step(stale, "foo", {"result": "late"})
    with pytest.raises(PersistenceError, match="INVALID_INPUT"):
        store.commit_step(stale, "late_result:step:foo", {"result": "blocked"})
    rebuilt = store.rebuild(incident)
    live = next(item for item in rebuilt["steps"] if item["step_id"] == colliding)
    late = [item for item in rebuilt["steps"] if item["status"] == "late_result"]
    assert live["logical_key"] == "late-step:foo"
    assert live["status"] == "response_committed"
    assert [item["logical_key"] for item in late] == ["late_result:step:foo"]
    assert late[0]["response"] == {"result": "late"}


def test_new_run_is_refused_until_the_incident_is_cancelled():
    store = DurableStore(DSN)
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-new-run-uncancelled-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
        budget_limit=10,
        versions={"state": "v1"},
    )
    with pytest.raises(PersistenceError, match="ILLEGAL_TRANSITION"):
        store.new_run(
            incident,
            run,
            deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
            budget_limit=10,
            versions={"state": "v1"},
            actor="operator",
        )
    rebuilt = store.rebuild(incident)
    assert rebuilt["state"] == "queued"
    assert rebuilt["control_generation"] == 0
    assert rebuilt["run"]["run_id"] == run
    assert store.control(incident, 0, "follow_up", "operator") == 1
    with pytest.raises(PersistenceError, match="ILLEGAL_TRANSITION"):
        store.new_run(
            incident,
            run,
            deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
            budget_limit=10,
            versions={"state": "v1"},
            actor="operator",
        )
    assert store.rebuild(incident)["control_generation"] == 1


def test_cancelled_incident_can_continue_with_a_new_run():
    store = DurableStore(DSN)
    incident, run, next_run = uuid4(), uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-new-run-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
        budget_limit=10,
        versions={"state": "v1"},
    )
    assert store.control(incident, 0, "cancel", "operator") == 1
    with pytest.raises(PersistenceError, match="IDENTITY_CONFLICT"):
        store.new_run(
            incident,
            run,
            deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
            budget_limit=10,
            versions={"state": "v1"},
            actor="operator",
        )
    generation = store.new_run(
        incident,
        next_run,
        deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
        budget_limit=10,
        versions={"state": "v1"},
        actor="operator",
    )
    assert generation == 2
    rebuilt = store.rebuild(incident)
    assert rebuilt["state"] == "queued"
    assert rebuilt["run"]["run_id"] == next_run
    assert (
        store.claim(incident, next_run, uuid4(), {"state": "v1"}).control_generation
        == 2
    )
    assert (
        store.new_run(
            incident,
            next_run,
            deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
            budget_limit=10,
            versions={"state": "v1"},
            actor="operator",
        )
        == 2
    )
    with pytest.raises(PersistenceError, match="ILLEGAL_TRANSITION"):
        store.new_run(
            incident,
            uuid4(),
            deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
            budget_limit=10,
            versions={"state": "v1"},
            actor="operator",
        )
    with pytest.raises(PersistenceError, match="IDENTITY_CONFLICT"):
        store.new_run(
            incident,
            run,
            deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
            budget_limit=10,
            versions={"state": "v1"},
            actor="operator",
        )
    with store.transaction(snapshot=True) as conn:
        audits = conn.execute(
            "SELECT action FROM opspilot_controls WHERE incident_id=%s ORDER BY resulting_generation, created_at",
            (incident,),
        ).fetchall()
    assert [item["action"] for item in audits] == ["cancel", "new_run"]


def test_concurrent_follow_up_and_cancel_have_one_winner_generation():
    """同一 expected_generation 的并发人工操作必须只有一个提交成功。"""
    store = DurableStore(DSN)
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-concurrent-control-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
        budget_limit=10,
        versions={"state": "v1"},
    )
    barrier = threading.Barrier(2)

    def apply(action):
        barrier.wait()
        try:
            return ("ok", store.control(incident, 0, action, "operator"))
        except PersistenceError as exc:
            return (exc.args[0], None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(apply, ("follow_up", "cancel")))
    assert [result[0] for result in results].count("ok") == 1
    assert [result[0] for result in results].count("CONTROL_CONFLICT") == 1
    assert store.rebuild(incident)["control_generation"] == 1


def test_paused_incident_cannot_be_claimed_or_published():
    """人工暂停期间不得领取新租约，也不得发布结论。"""
    store = DurableStore(DSN)
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-paused-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
        budget_limit=10,
        versions={"state": "v1"},
    )
    lease = store.claim(incident, run, uuid4(), {"state": "v1"})
    step = store.commit_step(lease, "round-0", {"result": "supported"})
    assert store.control(incident, 0, "pause", "operator") == 1

    rebuilt = store.rebuild(incident)
    assert rebuilt["state"] == "paused"
    assert rebuilt["run"]["state"] == "paused"

    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.claim(incident, run, uuid4(), {"state": "v1"})
    assert store.publish(lease, {"result": "supported"}, step_id=step) is False
    assert store.rebuild(incident)["conclusion"] is None

    assert store.control(incident, 1, "resume", "operator") == 2
    resumed = store.claim(incident, run, uuid4(), {"state": "v1"})
    assert resumed.control_generation == 2


def test_never_claimed_run_cannot_be_claimed_while_paused():
    """Run 从未被领取时，pause 不改写 Run 状态，incident 状态判定是唯一闸门。"""
    store = DurableStore(DSN)
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-paused-queued-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
        budget_limit=10,
        versions={"state": "v1"},
    )
    assert store.control(incident, 0, "pause", "operator") == 1

    rebuilt = store.rebuild(incident)
    assert rebuilt["state"] == "paused"
    assert rebuilt["run"]["state"] == "queued"

    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.claim(incident, run, uuid4(), {"state": "v1"})

    assert store.control(incident, 1, "resume", "operator") == 2
    assert store.claim(incident, run, uuid4(), {"state": "v1"}).control_generation == 2


def test_paused_run_reaches_terminal_state_on_cancel():
    """暂停中的 Run 被取消时必须进入终态，不得留下 incident/run 互相矛盾的记录。"""
    store = DurableStore(DSN)
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-paused-cancel-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
        budget_limit=10,
        versions={"state": "v1"},
    )
    store.claim(incident, run, uuid4(), {"state": "v1"})
    assert store.control(incident, 0, "pause", "operator") == 1
    assert store.control(incident, 1, "cancel", "operator") == 2

    rebuilt = store.rebuild(incident)
    assert rebuilt["state"] == "cancelled"
    assert rebuilt["run"]["state"] == "cancelled"


def test_follow_up_and_correct_cannot_silently_lift_a_pause():
    """追问与纠正不解除人工暂停；恢复必须由显式 resume 完成。"""
    store = DurableStore(DSN)
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-paused-followup-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
        budget_limit=10,
        versions={"state": "v1"},
    )
    store.claim(incident, run, uuid4(), {"state": "v1"})
    assert store.control(incident, 0, "pause", "operator") == 1

    for action in ("follow_up", "correct", "pause"):
        with pytest.raises(PersistenceError, match="ILLEGAL_TRANSITION"):
            store.control(incident, 1, action, "operator")

    rebuilt = store.rebuild(incident)
    assert rebuilt["state"] == "paused"
    assert rebuilt["control_generation"] == 1

    assert store.control(incident, 1, "resume", "operator") == 2


def test_waiting_human_run_is_paused_and_cancelled_consistently():
    store = DurableStore(DSN)
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-waiting-human-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
        budget_limit=10,
        versions={"state": "v1"},
    )
    with store.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_runs SET state='waiting_human' WHERE run_id=%s", (run,)
        )
    assert store.control(incident, 0, "pause", "operator") == 1
    assert store.rebuild(incident)["run"]["state"] == "paused"
    assert store.control(incident, 1, "resume", "operator") == 2
    assert store.rebuild(incident)["run"]["state"] == "queued"
    with store.transaction() as conn:
        conn.execute("UPDATE opspilot_runs SET state='blocked' WHERE run_id=%s", (run,))
    for action in ("pause", "resume", "follow_up", "correct"):
        with pytest.raises(PersistenceError, match="ILLEGAL_TRANSITION"):
            store.control(incident, 2, action, "operator")
    assert store.control(incident, 2, "cancel", "operator") == 3
    assert store.rebuild(incident)["run"]["state"] == "cancelled"


def test_queued_follow_up_rebinds_generation_before_claim():
    store = DurableStore(DSN)
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-queued-follow-up-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
        budget_limit=10,
        versions={"state": "v1"},
    )
    assert store.control(incident, 0, "follow_up", "operator") == 1
    lease = store.claim(incident, run, uuid4(), {"state": "v1"})
    assert lease.control_generation == 1


def test_old_generation_step_and_tool_writes_are_fenced():
    store = DurableStore(DSN)
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-step-generation-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
        budget_limit=10,
        versions={"state": "v1"},
    )
    lease = store.claim(incident, run, uuid4(), {"state": "v1"})
    step = store.commit_step(lease, "final", {"result": "old"})
    assert store.control(incident, 0, "follow_up", "operator") == 1
    fresh = store.claim(incident, run, uuid4(), {"state": "v1"})
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.commit_tool(lease, step, 0, {"ok": True})
    with pytest.raises(PersistenceError, match="FINAL_STEP_REQUIRED"):
        store.publish(fresh, {"result": "old"}, step_id=step)


def test_fresh_lease_cannot_commit_tools_into_pre_follow_up_step():
    """追问之后的新租约不得把工具结果写回追问之前那一轮的步骤。

    rebuild() 的 pending_tools 已按代际过滤（见
    test_rebuild_drops_pending_tools_from_a_superseded_generation），
    但读路径的过滤不是写路径的授权：调用方可能拿着人工决定之前重建的断点，
    因此 commit_tool 必须在写入处按 step 的 control_generation 独立拦截。
    """
    store = DurableStore(DSN)
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-stale-step-tools-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
        budget_limit=10,
        versions={"state": "v1"},
    )
    stale = store.claim(incident, run, uuid4(), {"state": "v1"})
    assert stale.control_generation == 0
    step = store.commit_step(stale, "round-0", {"tool_calls": [{"id": "a"}]})

    assert store.control(incident, 0, "follow_up", "operator") == 1
    fresh = store.claim(incident, run, uuid4(), {"state": "v1"})
    assert fresh.control_generation == 1

    assert store.rebuild(incident)["pending_tools"] == []
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.commit_tool(fresh, step, 0, {"ok": True})

    fenced = next(s for s in store.rebuild(incident)["steps"] if s["step_id"] == step)
    assert fenced["control_generation"] == 0
    assert fenced["status"] == "response_committed"
    assert fenced["tool_results"] == []


def test_rebuild_reads_a_consistent_snapshot():
    """rebuild() 横跨三条查询，必须看到同一个状态。

    默认的 READ COMMITTED 逐语句取快照，并发的人工操作会让 incident 与 run
    的 control_generation 读出不一致的组合，重建出从未存在过的断点。
    """
    store = DurableStore(DSN)
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-snapshot-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=5),
        budget_limit=10**6,
        versions={"state": "v1"},
    )

    stop = threading.Event()
    generation = [0]

    def advance():
        while not stop.is_set():
            try:
                generation[0] = store.control(
                    incident, generation[0], "resume", "operator"
                )
            except PersistenceError:
                pass

    worker = threading.Thread(target=advance, daemon=True)
    worker.start()
    try:
        for _ in range(120):
            rebuilt = store.rebuild(incident)
            assert (
                rebuilt["control_generation"] == rebuilt["run"]["control_generation"]
            ), "rebuild() 返回了 incident 与 run 代际不一致的撕裂快照"
    finally:
        stop.set()
        worker.join(timeout=5)


def test_concurrent_accept_of_the_same_identity_stays_idempotent():
    """同一身份并发重投是幂等成功，不是冲突，也不是存储故障。

    单轮 8 线程只有约一半的概率让两个 INSERT 真正重叠，对回归而言是
    flaky-green。重复多轮把漏检概率压到可忽略；对正确实现每轮都稳定通过。
    """
    store = DurableStore(DSN)
    deadline = datetime.now(timezone.utc) + timedelta(minutes=5)

    for _ in range(12):
        incident, run = uuid4(), uuid4()
        intake_key = f"m1-idempotent-{incident}"

        def submit(_: int, incident: UUID = incident, run: UUID = run) -> str:
            try:
                store.accept(
                    incident,
                    run,
                    intake_key,
                    deadline=deadline,
                    budget_limit=10,
                    versions={"state": "v1"},
                )
                return "accepted"
            except PersistenceError as exc:
                return str(exc)

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(submit, range(8)))

        assert set(outcomes) == {"accepted"}, outcomes
        assert store.rebuild(incident)["run"]["state"] == "queued"


def test_storage_failures_map_to_distinguishable_codes():
    """存储失败按调用方该做什么区分，不压成单一的存储不可用。

    重试无用的唯一键冲突与可重试的瞬时故障必须是不同的码，否则调用方
    只能一律重试或一律告警。断言走 transaction() 的真实映射路径。
    """
    assert DurableStore._error_code(errors.DeadlockDetected()) == "RETRY"
    assert DurableStore._error_code(errors.SerializationFailure()) == "RETRY"
    assert DurableStore._error_code(errors.LockNotAvailable()) == "TIMEOUT"
    assert DurableStore._error_code(errors.QueryCanceled()) == "TIMEOUT"
    assert DurableStore._error_code(errors.OperationalError()) == "STORAGE_UNAVAILABLE"

    store = DurableStore(DSN)
    incident, run = uuid4(), uuid4()
    intake_key = f"m1-errcode-{incident}"
    store.accept(
        incident,
        run,
        intake_key,
        deadline=datetime.now(timezone.utc) + timedelta(minutes=5),
        budget_limit=10,
        versions={"state": "v1"},
    )
    with pytest.raises(PersistenceError, match="IDENTITY_CONFLICT"):
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO opspilot_incidents(incident_id,intake_key,state,lifecycle,control_generation) VALUES(%s,%s,'queued','open',0)",
                (uuid4(), intake_key),
            )


def test_write_paths_lock_the_incident_before_the_run():
    """写路径统一按 incidents -> runs 加锁，否则与 control()/publish() 交叉即死锁。

    `control()` 只拿到 incident_id，结构上必须先读 incident，因此以它为规范
    顺序。本测试确定性地探测顺序：占住 incident 行后调用写方法，若该方法先锁
    run，则第三方对 run 的 NOWAIT 探测会失败。
    """
    store = DurableStore(DSN)
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-lockorder-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=5),
        budget_limit=10**6,
        versions={"state": "v1"},
    )
    lease = store.claim(incident, run, uuid4(), {"state": "v1"}, lease_seconds=300)
    seeded = store.commit_step(lease, "seed", {"tool_calls": [{"id": "a"}]})

    for name, call in (
        ("reserve_budget", lambda: store.reserve_budget(lease, uuid4(), 1)),
        ("commit_step", lambda: store.commit_step(lease, f"k-{uuid4()}", {})),
        ("commit_tool", lambda: store.commit_tool(lease, seeded, 0, {"ok": True})),
    ):
        blocked = threading.Event()
        with psycopg.connect(DSN, row_factory=dict_row) as holder:
            holder.execute(
                "SELECT 1 FROM opspilot_incidents WHERE incident_id=%s FOR UPDATE",
                (incident,),
            )

            def attempt() -> None:
                blocked.set()
                try:
                    call()
                except PersistenceError:
                    pass

            worker = threading.Thread(target=attempt, daemon=True)
            worker.start()
            assert blocked.wait(timeout=5)
            time.sleep(0.3)

            with psycopg.connect(DSN, row_factory=dict_row) as probe:
                try:
                    probe.execute(
                        "SELECT 1 FROM opspilot_runs WHERE run_id=%s FOR UPDATE NOWAIT",
                        (run,),
                    )
                except errors.LockNotAvailable:
                    raise AssertionError(
                        f"{name} 在锁 incident 之前先锁了 run，与 control()/publish() "
                        "的顺序相反，交叉会死锁"
                    ) from None
        worker.join(timeout=5)


def test_require_row_reports_inconsistent_state_not_a_transient_failure():
    """业务记录缺行是不一致，重试必然再失败，不能与瞬时故障共用一个码。

    两个剩余调用点都是保证单行的查询，该分支经公开接口不可达，因此直接断言
    helper 的契约，避免这个对外可见的错误码无人守护。
    """
    store = DurableStore(DSN)
    with store.transaction() as conn:
        empty = conn.execute(
            "SELECT 1 FROM opspilot_incidents WHERE incident_id=%s", (uuid4(),)
        )
        with pytest.raises(PersistenceError, match="INCONSISTENT_STATE"):
            store._require_row(empty)


def test_reusing_an_incident_id_under_a_new_key_is_an_identity_conflict():
    """复用 incident_id 换一个 intake_key 是调用方的身份冲突，不是存储问题。

    不限定目标的 `ON CONFLICT` 会把主键冲突一并吞掉，随后按新 key 查不到行。
    这既不是存储故障也不是记录不一致，报成那两者会把运维引去查数据库。
    """
    store = DurableStore(DSN)
    incident = uuid4()
    deadline = datetime.now(timezone.utc) + timedelta(minutes=5)
    store.accept(
        incident,
        uuid4(),
        f"m1-reuse-a-{incident}",
        deadline=deadline,
        budget_limit=10,
        versions={"state": "v1"},
    )
    with pytest.raises(PersistenceError, match="^IDENTITY_CONFLICT$"):
        store.accept(
            incident,
            uuid4(),
            f"m1-reuse-b-{incident}",
            deadline=deadline,
            budget_limit=10,
            versions={"state": "v1"},
        )


def test_snapshot_transactions_refuse_writes():
    """一致快照事务是只读的：取行锁或写入会被直接拒绝，而不是偶发 RETRY。"""
    store = DurableStore(DSN)
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-readonly-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=5),
        budget_limit=10,
        versions={"state": "v1"},
    )
    with pytest.raises(PersistenceError, match="^READ_ONLY_PATH$"):
        with store.transaction(snapshot=True) as conn:
            conn.execute(
                "UPDATE opspilot_incidents SET state='queued' WHERE incident_id=%s",
                (incident,),
            )


def test_generational_fence_is_keyed_to_the_incident_not_the_run_copy():
    """三条写路径的代际栅栏必须读事故代际，不是 run 行里的那份副本。

    人工决定只递增 `opspilot_incidents.control_generation`；run 行上的同名列是
    claim()/control() 盖下的副本。原实现写成 `SELECT r.*,i.control_generation`，
    结果集里 `control_generation` 出现两次，`dict_row` 静默取最后一个——取到的
    恰好是事故代际，但那是书写顺序的副产物：把 `i.control_generation` 挪到
    `r.*` 之前，栅栏就改读副本，而整个套件、mypy strict 与 ruff 都无感。

    两份代际在当前公开接口下不会分叉（control() 同时推进两者），所以这里直接
    安排存储状态，与 test_require_row_reports_inconsistent_state_not_a_transient_failure
    同一思路：对外可见的契约需要有人守护，不能因为暂时不可达就无人认领。
    """
    store = DurableStore(DSN)
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-fence-key-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=5),
        budget_limit=10,
        versions={"state": "v1"},
    )
    claimed = store.claim(incident, run, uuid4(), {"state": "v1"}, lease_seconds=300)
    step_at_incident = store.commit_step(claimed, "a", {"tool_calls": [{"id": "a"}]})
    step_at_run = store.commit_step(claimed, "b", {"tool_calls": [{"id": "b"}]})

    with store.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_runs SET control_generation=11 WHERE run_id=%s", (run,)
        )
        conn.execute(
            "UPDATE opspilot_incidents SET control_generation=77 WHERE incident_id=%s",
            (incident,),
        )
        conn.execute(
            "UPDATE opspilot_steps SET control_generation=77 WHERE step_id=%s",
            (step_at_incident,),
        )
        conn.execute(
            "UPDATE opspilot_steps SET control_generation=11 WHERE step_id=%s",
            (step_at_run,),
        )

    at_incident = replace(claimed, control_generation=77)
    at_run = replace(claimed, control_generation=11)

    # 与事故代际一致：没有更新的人工决定，放行。
    store.reserve_budget(at_incident, uuid4(), 1)
    store.commit_step(at_incident, "c", {})
    store.commit_tool(at_incident, step_at_incident, 0, {"ok": True})

    # 只与 run 行里的副本一致：栅栏若读副本就会放行，必须拒绝。
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.reserve_budget(at_run, uuid4(), 1)
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.commit_step(at_run, "d", {})
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.commit_tool(at_run, step_at_run, 0, {"ok": True})
    assert store.publish(at_run, {"result": "stale"}, step_id=step_at_run) is False


def test_a_cleared_lease_cannot_be_used_even_when_owner_and_epoch_still_match():
    """`lease_until IS NULL` 是「这个 run 没有租约」，不是「租约没过期」。

    control() 收回 worker 权限时同时清空 owner 与 lease_until。四份租约守卫
    里只有 publish 把 NULL 判为拒绝，另三份判为通过——它们能挡住人工操作，
    靠的是 owner 也一起被清空。本测试只清 lease_until，把守卫单独暴露出来。
    """
    store = DurableStore(DSN)
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-null-lease-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=5),
        budget_limit=10,
        versions={"state": "v1"},
    )
    lease = store.claim(incident, run, uuid4(), {"state": "v1"}, lease_seconds=300)
    step = store.commit_step(lease, "round-0", {"tool_calls": [{"id": "a"}]})
    with store.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_runs SET lease_until=NULL WHERE run_id=%s", (run,)
        )

    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.reserve_budget(lease, uuid4(), 1)
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.commit_step(lease, "round-1", {})
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.commit_tool(lease, step, 0, {"ok": True})
    assert store.publish(lease, {"result": "no-lease"}, step_id=step) is False


def test_rebuild_drops_pending_tools_from_a_superseded_generation():
    """人工决定之后，旧代际的待办工具调用不再出现在断点里。

    commit_tool 会按 step 的 control_generation 拒绝它们，所以照旧列出的待办
    永远做不完；调用方每次重建都会拿到同一批做不成的工作。步骤本身仍留在
    steps 里——那是业务记录，不因代际推进而消失。
    """
    store = DurableStore(DSN)
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-pending-generation-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=5),
        budget_limit=10,
        versions={"state": "v1"},
    )
    stale = store.claim(incident, run, uuid4(), {"state": "v1"})
    old_step = store.commit_step(
        stale, "round-0", {"tool_calls": [{"id": "a"}, {"id": "b"}]}
    )
    store.commit_tool(stale, old_step, 0, {"ok": True})
    pending = store.rebuild(incident)["pending_tools"]
    assert pending[0]["step_id"] == old_step and pending[0]["ordinal"] == 1
    assert pending[0]["operation_id"].endswith(":1")
    assert pending[0]["tool_call"] == {"id": "b"}

    assert store.control(incident, 0, "follow_up", "operator") == 1
    rebuilt = store.rebuild(incident)
    assert rebuilt["pending_tools"] == []
    assert [step["step_id"] for step in rebuilt["steps"]] == [old_step]

    fresh = store.claim(incident, run, uuid4(), {"state": "v1"})
    new_step = store.commit_step(fresh, "round-1", {"tool_calls": [{"id": "c"}]})
    pending = store.rebuild(incident)["pending_tools"]
    assert pending[0]["step_id"] == new_step and pending[0]["ordinal"] == 0
    assert pending[0]["tool_call"] == {"id": "c"}


def test_install_indexes_the_incident_foreign_keys_on_an_existing_database():
    """两张表的 `incident_id` 必须有索引，且 install() 对已建好的库也补得上。

    PostgreSQL 不为外键列建索引。control() 的 `UPDATE opspilot_runs ...
    WHERE incident_id=%s` 因此走 Seq Scan，而三条写路径都排在它持有的
    incident 行锁之后，全表扫描的时间直接变成写路径的排队时间。

    「补建」这一半在一次性 schema 里验证，不碰默认 schema 的产品索引：
    `DROP INDEX` 取 ACCESS EXCLUSIVE 表锁，而本地 lab 由多个 worktree 共用，
    删到一半失败会把 lab 留在「索引已删、未重建」状态并改变别人的查询计划。
    默认 schema 这边只做只读断言。
    """
    expected = {
        ("opspilot_runs", "opspilot_runs_incident_id_idx"),
        ("opspilot_controls", "opspilot_controls_incident_id_idx"),
    }
    names = sorted(index for _, index in expected)

    def present(store: DurableStore, schema: str) -> dict[tuple[str, str], str]:
        with store.transaction(snapshot=True) as conn:
            return {
                (row["tablename"], row["indexname"]): row["indexdef"]
                for row in conn.execute(
                    "SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname=%s AND indexname = ANY(%s)",
                    (schema, names),
                ).fetchall()
            }

    # 默认 schema：只读断言产品索引确实在，不做任何 DDL。
    base = DurableStore(DSN)
    base.install()
    live = present(base, "public")
    assert set(live) == expected, (
        f"默认 schema 缺少 incident_id 索引：{expected - set(live)}"
    )

    # 一次性 schema：删掉索引后再 install()，断言的是「已有数据库上也会补建」，
    # 而不是「建表时顺手建了」。
    schema = "m1_index_backfill_" + uuid4().hex
    with base.transaction() as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        scoped = DurableStore(f"{DSN} options=-csearch_path={schema}")
        scoped.install()
        with scoped.transaction() as conn:
            for index in names:
                conn.execute(
                    sql.SQL("DROP INDEX IF EXISTS {}").format(sql.Identifier(index))
                )
        assert present(scoped, schema) == {}, (
            "一次性 schema 的索引未被删掉，补建无从验证"
        )

        scoped.install()
        backfilled = present(scoped, schema)
    finally:
        with base.transaction() as conn:
            conn.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema)
                )
            )
    assert set(backfilled) == expected, (
        f"install() 未在已有数据库上补建：{expected - set(backfilled)}"
    )
    for definition in backfilled.values():
        assert "(incident_id)" in definition, definition


def test_control_distinguishes_unknown_identity_from_a_retryable_conflict():
    """CONTROL_CONFLICT 只用于「代际过期，重读后重试」，不兜底身份与不一致。

    原实现用一条 JOIN 取 incident 与 run，取不到行时无法区分事故不存在与
    run 行缺失，两者都报 CONTROL_CONFLICT；调用方按该码的约定重读重试，
    这两种情况重试多少次都不会成功。
    """
    store = DurableStore(DSN)
    with pytest.raises(PersistenceError, match="^UNKNOWN_IDENTITY$"):
        store.control(uuid4(), 0, "cancel", "operator")

    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-control-codes-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=5),
        budget_limit=10,
        versions={"state": "v1"},
    )
    for missing in (None, uuid4()):
        with store.transaction() as conn:
            conn.execute(
                "UPDATE opspilot_incidents SET current_run_id=%s WHERE incident_id=%s",
                (missing, incident),
            )
        with pytest.raises(PersistenceError, match="^INCONSISTENT_STATE$"):
            store.control(incident, 0, "cancel", "operator")

    with store.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_incidents SET current_run_id=%s WHERE incident_id=%s",
            (run, incident),
        )
    with pytest.raises(PersistenceError, match="^CONTROL_CONFLICT$"):
        store.control(incident, 9, "cancel", "operator")
    assert store.control(incident, 0, "cancel", "operator") == 1


def test_non_cancel_control_is_refused_from_every_unlisted_run_state():
    """非 cancel 的人工动作按放行名单判定，新状态不会静默变成「允许」。

    原实现只点名 `blocked`。`failed` 与 `budget_exhausted` 是 RUN_EXECUTION 的
    终态（`blocked` 不是，它还有 human_cancel / handoff_failed 两条出边），本模块
    目前不写入这两个，因此没有测试会发现这个不对称；一旦写入路径出现，
    pause/resume/follow_up/correct 会直接放行。

    遍历的是「RUN_EXECUTION 全集减去四个放行状态」而不是实现里的常量：
    往领域里加一个新 run 状态，它会自动落进这里并要求给出决定。
    cancel 始终放行——它是人工控制的兜底出口。
    """
    store = DurableStore(DSN)
    closed = RUN_EXECUTION.states - {"queued", "running", "paused", "waiting_human"}
    assert closed == {"blocked", "budget_exhausted", "cancelled", "completed", "failed"}

    for run_state in sorted(closed):
        incident, run = uuid4(), uuid4()
        store.accept(
            incident,
            run,
            f"m1-closed-{run_state}-{incident}",
            deadline=datetime.now(timezone.utc) + timedelta(minutes=5),
            budget_limit=10,
            versions={"state": "v1"},
        )
        store.claim(incident, run, uuid4(), {"state": "v1"})
        with store.transaction() as conn:
            conn.execute(
                "UPDATE opspilot_runs SET state=%s WHERE run_id=%s", (run_state, run)
            )
        for action in ("pause", "resume", "follow_up", "correct"):
            with pytest.raises(PersistenceError, match="^ILLEGAL_TRANSITION$"):
                store.control(incident, 0, action, "operator")
        assert store.rebuild(incident)["control_generation"] == 0
        assert store.control(incident, 0, "cancel", "operator") == 1


def test_rebuild_filters_pending_tools_by_the_incident_generation_not_the_run_copy():
    """`rebuild()` 的代际过滤读的是事故代际，不是 run 行上的副本。

    与三条写路径的栅栏同一条契约（见
    test_generational_fence_is_keyed_to_the_incident_not_the_run_copy）：人工决定
    只递增 `opspilot_incidents.control_generation`。读路径若改读 run 行副本，
    断点会把已经被 commit_tool 拒绝的旧代际待办重新列出来，而公开接口下两份
    代际不会分叉，没有任何现有用例会转红——所以这里直接安排存储状态。
    """
    store = DurableStore(DSN)
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-rebuild-generation-source-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=5),
        budget_limit=10,
        versions={"state": "v1"},
    )
    lease = store.claim(incident, run, uuid4(), {"state": "v1"}, lease_seconds=300)
    at_incident = store.commit_step(lease, "current", {"tool_calls": [{"id": "a"}]})
    at_run = store.commit_step(lease, "superseded", {"tool_calls": [{"id": "b"}]})

    with store.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_incidents SET control_generation=77 WHERE incident_id=%s",
            (incident,),
        )
        conn.execute(
            "UPDATE opspilot_runs SET control_generation=11 WHERE run_id=%s", (run,)
        )
        conn.execute(
            "UPDATE opspilot_steps SET control_generation=77 WHERE step_id=%s",
            (at_incident,),
        )
        conn.execute(
            "UPDATE opspilot_steps SET control_generation=11 WHERE step_id=%s",
            (at_run,),
        )

    rebuilt = store.rebuild(incident)
    assert rebuilt["control_generation"] == 77
    assert len(rebuilt["pending_tools"]) == 1
    assert rebuilt["pending_tools"][0]["step_id"] == at_incident
    assert rebuilt["pending_tools"][0]["tool_call"] == {"id": "a"}
    # 旧代际的步骤仍留在业务记录里，只是不再作为待办派发。
    assert {step["step_id"] for step in rebuilt["steps"]} == {at_incident, at_run}


def test_lease_identity_and_deadline_each_fence_all_four_write_paths():
    """收敛后的租约守卫里，owner / epoch / deadline 三条子句各自都承重。

    `control()` 收回权限时 owner、lease_until、代际是一起变的，所以既有用例即使
    删掉 owner 与 epoch 判定也不会转红（在 main 上同样如此）。A3 把四份守卫合成
    一份之后，这三条子句只有一个实现，逐条钉住的成本已经降到最低。
    """
    store = DurableStore(DSN)

    def arrange(tag: str) -> tuple[UUID, UUID, Lease, UUID]:
        incident, run = uuid4(), uuid4()
        store.accept(
            incident,
            run,
            f"m1-guard-{tag}-{incident}",
            deadline=datetime.now(timezone.utc) + timedelta(minutes=5),
            budget_limit=10,
            versions={"state": "v1"},
        )
        lease = store.claim(incident, run, uuid4(), {"state": "v1"}, lease_seconds=300)
        step = store.commit_step(lease, "round-0", {"tool_calls": [{"id": "a"}]})
        return incident, run, lease, step

    def assert_all_paths_denied(lease: Lease, step: UUID) -> None:
        with pytest.raises(PersistenceError, match="^CONTROL_DENIED$"):
            store.reserve_budget(lease, uuid4(), 1)
        with pytest.raises(PersistenceError, match="^CONTROL_DENIED$"):
            store.commit_step(lease, f"k-{uuid4()}", {})
        with pytest.raises(PersistenceError, match="^CONTROL_DENIED$"):
            store.commit_tool(lease, step, 0, {"ok": True})
        assert store.publish(lease, {"result": "denied"}, step_id=step) is False

    # owner：另一个 worker 拿着同一个 run 的身份，其余全部有效。
    _, _, lease, step = arrange("owner")
    assert_all_paths_denied(replace(lease, owner=uuid4()), step)

    # epoch：同一个 owner，但租约来自上一个执行轮次。
    _, _, lease, step = arrange("epoch")
    assert_all_paths_denied(replace(lease, epoch=lease.epoch - 1), step)

    # deadline：租约本身没过期，但 Run 的 deadline 已过。
    _, run, lease, step = arrange("deadline")
    with store.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_runs SET deadline=clock_timestamp()-interval '1 second' WHERE run_id=%s",
            (run,),
        )
    assert_all_paths_denied(lease, step)


def test_control_refuses_a_current_run_pointer_into_another_incident():
    """`current_run_id` 指向别的 incident 的 run 时，状态判定不得读那一行。

    状态判定读 `current_run_id` 指向的 run，而状态推进按 `incident_id` 作用于本
    incident 真正的 run。两者指向不同的行时，一个外来的 running run 会把 blocked
    run 的保护顶开：实测（修复前，且 main 行为相同）victim 自己的 run 是 blocked，
    `pause` 仍返回 generation 1，事后 incident 为 paused/1 而它自己的 run 仍是
    blocked/0——incident 与它的 run 就此不一致。

    该状态公开接口构造不出（`current_run_id` 只在 accept() 里写成同一事务创建的
    那个 run，此后无人改写），因此直接安排存储状态。
    """
    store = DurableStore(DSN)

    def accepted(tag: str) -> tuple[UUID, UUID]:
        incident, run = uuid4(), uuid4()
        store.accept(
            incident,
            run,
            f"m1-cross-incident-{tag}-{incident}",
            deadline=datetime.now(timezone.utc) + timedelta(minutes=5),
            budget_limit=10,
            versions={"state": "v1"},
        )
        return incident, run

    victim, victim_run = accepted("victim")
    other, other_run = accepted("other")
    store.claim(other, other_run, uuid4(), {"state": "v1"}, lease_seconds=300)

    with store.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_runs SET state='blocked' WHERE run_id=%s", (victim_run,)
        )
        conn.execute(
            "UPDATE opspilot_incidents SET current_run_id=%s WHERE incident_id=%s",
            (other_run, victim),
        )

    # blocked 的 run 本就不接受非 cancel 动作；外来的 running run 不得代它放行。
    for action in ("pause", "resume", "follow_up", "correct", "cancel"):
        with pytest.raises(PersistenceError, match="^INCONSISTENT_STATE$"):
            store.control(victim, 0, action, "operator")

    # 拒绝必须发生在推进任何状态之前：代际、incident 状态、审计行都不得变动。
    with store.transaction(snapshot=True) as conn:
        incident_row = conn.execute(
            "SELECT state,control_generation FROM opspilot_incidents WHERE incident_id=%s",
            (victim,),
        ).fetchone()
        audits = conn.execute(
            "SELECT count(*) AS n FROM opspilot_controls WHERE incident_id=%s",
            (victim,),
        ).fetchone()["n"]
        own = conn.execute(
            "SELECT state,control_generation FROM opspilot_runs WHERE run_id=%s",
            (victim_run,),
        ).fetchone()
        foreign = conn.execute(
            "SELECT state,control_generation FROM opspilot_runs WHERE run_id=%s",
            (other_run,),
        ).fetchone()
    assert incident_row == {"state": "queued", "control_generation": 0}
    assert audits == 0
    assert own == {"state": "blocked", "control_generation": 0}
    assert foreign == {"state": "running", "control_generation": 0}
