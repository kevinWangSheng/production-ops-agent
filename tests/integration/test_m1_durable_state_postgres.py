import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import errors
from psycopg.rows import dict_row

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


def test_version_change_cannot_override_a_paused_cancelled_or_completed_run():
    """版本不符只能把可领取的 Run 记成 ``blocked``；人工决定先于版本判定。

    C3 第 5 节：``INCOMPATIBLE_STATE`` 与人工控制是两套语义，不得互相顶替。
    部署换版本后任何一次 claim 都不得把 ``paused``/``cancelled``/``completed`` 的
    Run 行改成 ``blocked``——否则人工 ``resume`` 会得到 ``ILLEGAL_TRANSITION``，
    人工取消会被投影成版本事故（ADR-0003：业务记录里的人工决定是权威）。
    """
    store = DurableStore(DSN)

    def accepted(tag):
        incident, run = uuid4(), uuid4()
        store.accept(
            incident,
            run,
            f"m1-version-vs-control-{tag}-{incident}",
            deadline=datetime.now(timezone.utc) + timedelta(minutes=2),
            budget_limit=10,
            versions={"state": "v1"},
        )
        return incident, run

    # paused：版本变了也不得改写 run 行；人工 resume 仍然合法。
    incident, run = accepted("paused")
    store.claim(incident, run, uuid4(), {"state": "v1"})
    assert store.control(incident, 0, "pause", "operator") == 1
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.claim(incident, run, uuid4(), {"state": "v2"})
    rebuilt = store.rebuild(incident)
    assert rebuilt["state"] == "paused"
    assert rebuilt["run"]["state"] == "paused"
    assert store.control(incident, 1, "resume", "operator") == 2
    # 解除暂停后再被新版本领取，才是版本事故：这时才允许 blocked。
    with pytest.raises(PersistenceError, match="INCOMPATIBLE_STATE"):
        store.claim(incident, run, uuid4(), {"state": "v2"})
    assert store.rebuild(incident)["run"]["state"] == "blocked"

    # cancelled：人工取消的终态不得被投影成 blocked/INCOMPATIBLE_STATE。
    incident, run = accepted("cancelled")
    store.claim(incident, run, uuid4(), {"state": "v1"})
    assert store.control(incident, 0, "cancel", "operator") == 1
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.claim(incident, run, uuid4(), {"state": "v2"})
    rebuilt = store.rebuild(incident)
    assert rebuilt["state"] == "cancelled"
    assert rebuilt["run"]["state"] == "cancelled"

    # completed：已发布结论的 Run 同样保持终态。
    incident, run = accepted("completed")
    lease = store.claim(incident, run, uuid4(), {"state": "v1"})
    step = store.commit_step(lease, "final", {"result": "supported"})
    assert store.publish(lease, {"result": "supported"}, step_id=step) is True
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.claim(incident, run, uuid4(), {"state": "v2"})
    rebuilt = store.rebuild(incident)
    assert rebuilt["conclusion"] == {"result": "supported"}
    assert rebuilt["run"]["state"] == "completed"
