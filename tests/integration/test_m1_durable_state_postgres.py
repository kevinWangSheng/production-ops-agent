import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from opspilot.persistence import DurableStore, PersistenceError
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

    rebuild() 的 pending_tools 不按代际过滤，仍会原样列出旧代际步骤，
    调用方照着做就会把过期证据刷新成「当前已提交的证据」，
    因此 commit_tool 必须在写入处按 step 的 control_generation 拦截。
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

    assert {"step_id": step, "ordinal": 0} in store.rebuild(incident)["pending_tools"]
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.commit_tool(fresh, step, 0, {"ok": True})

    fenced = next(s for s in store.rebuild(incident)["steps"] if s["step_id"] == step)
    assert fenced["control_generation"] == 0
    assert fenced["status"] == "response_committed"
    assert fenced["tool_results"] == []
