"""租约续期 `DurableStore.renew_lease` 的 PostgreSQL 语义测试。

C3 第 6 节：「续租和提交均校验执行身份与租约」。续期是与提交同级的写路径：
同一栅栏（owner / epoch / 事故代际 / 未过期 / 未过 deadline），任一不满足即
拒绝且不改动 `lease_until`。过期即失效——只能重新 claim 取得新 epoch，不能续活。
"""

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

from opspilot.persistence import DurableStore, Lease, PersistenceError
from scripts.m0.postgres_lab import DSN

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

VERSIONS = {"state": "v1"}


@pytest.fixture(scope="module", autouse=True)
def _installed() -> None:
    if os.environ.get("M1_DURABLE_POSTGRES") == "1":
        DurableStore(DSN).install()


def _accept(store: DurableStore, *, deadline_seconds: float = 120) -> tuple[UUID, UUID]:
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-renew-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(seconds=deadline_seconds),
        budget_limit=10,
        versions=VERSIONS,
    )
    return incident, run


def _run_row(store: DurableStore, run: UUID) -> dict:
    with store.transaction(snapshot=True) as conn:
        row = conn.execute(
            "SELECT owner,epoch,state,lease_until,deadline,clock_timestamp() AS now FROM opspilot_runs WHERE run_id=%s",
            (run,),
        ).fetchone()
    assert row is not None
    return row


def _expire(store: DurableStore, run: UUID) -> None:
    with store.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_runs SET lease_until=clock_timestamp()-interval '1 second' WHERE run_id=%s",
            (run,),
        )


def test_holder_renewal_extends_without_changing_identity():
    store = DurableStore(DSN)
    incident, run = _accept(store)
    lease = store.claim(incident, run, uuid4(), VERSIONS, lease_seconds=2)
    before = _run_row(store, run)

    until = store.renew_lease(lease, 60)

    after = _run_row(store, run)
    assert until == after["lease_until"]
    assert after["lease_until"] > before["lease_until"]
    assert after["lease_until"] - after["now"] > timedelta(seconds=50)
    assert after["lease_until"] <= after["deadline"]
    # 续期不换身份：同一 owner/epoch 的凭据继续有效，无需新的 Lease 对象。
    assert (after["owner"], after["epoch"]) == (lease.owner, lease.epoch)
    time.sleep(2.2)  # 原 2 s 租约此时已过；续期后的租约仍能提交。
    assert store.commit_step(lease, "after-renewal", {"tool_calls": []})


def test_renewal_never_shortens_a_longer_remaining_lease():
    store = DurableStore(DSN)
    incident, run = _accept(store)
    lease = store.claim(incident, run, uuid4(), VERSIONS, lease_seconds=90)
    before = _run_row(store, run)["lease_until"]
    until = store.renew_lease(lease, 5)
    assert until == before
    assert _run_row(store, run)["lease_until"] == before


@pytest.mark.parametrize("extend", [0, -1])
def test_non_positive_extension_is_invalid_input(extend):
    store = DurableStore(DSN)
    incident, run = _accept(store)
    lease = store.claim(incident, run, uuid4(), VERSIONS, lease_seconds=30)
    before = _run_row(store, run)["lease_until"]
    with pytest.raises(PersistenceError, match="INVALID_INPUT"):
        store.renew_lease(lease, extend)
    assert _run_row(store, run)["lease_until"] == before


def test_expired_lease_cannot_be_revived_only_reclaimed_with_a_new_epoch():
    store = DurableStore(DSN)
    incident, run = _accept(store)
    lease = store.claim(incident, run, uuid4(), VERSIONS, lease_seconds=30)
    _expire(store, run)
    stale_until = _run_row(store, run)["lease_until"]

    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.renew_lease(lease, 60)
    assert _run_row(store, run)["lease_until"] == stale_until
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.commit_step(lease, "after-expiry", {})

    successor = store.claim(incident, run, uuid4(), VERSIONS, lease_seconds=30)
    assert successor.epoch == lease.epoch + 1
    # 新 epoch 发出后，旧凭据即使再来续期也不能夺回。
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.renew_lease(lease, 60)
    assert _run_row(store, run)["epoch"] == successor.epoch
    assert store.renew_lease(successor, 60) > _run_row(store, run)["now"]


@pytest.mark.parametrize("action", ["cancel", "pause", "correct", "follow_up"])
def test_human_control_revokes_the_lease_and_renewal_is_refused(action):
    store = DurableStore(DSN)
    incident, run = _accept(store)
    lease = store.claim(incident, run, uuid4(), VERSIONS, lease_seconds=30)
    assert store.control(incident, 0, action, "operator") == 1

    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.renew_lease(lease, 60)
    row = _run_row(store, run)
    assert row["lease_until"] is None
    assert row["owner"] is None
    if action in {"correct", "follow_up"}:
        # 代际前进后同一 owner 重新 claim 才拿到新凭据；旧凭据的代际已过时。
        renewed = store.claim(incident, run, lease.owner, VERSIONS, lease_seconds=30)
        assert renewed.control_generation == 1
        with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
            store.renew_lease(lease, 60)
        assert store.renew_lease(renewed, 60) <= _run_row(store, run)["deadline"]


def test_generation_change_with_same_owner_and_epoch_is_refused():
    """栅栏必须比对事故代际，而不是只看 owner/epoch。"""
    store = DurableStore(DSN)
    incident, run = _accept(store)
    lease = store.claim(incident, run, uuid4(), VERSIONS, lease_seconds=30)
    with store.transaction() as conn:
        # 模拟一个仅推进事故代际、未清 run 行的人工决定。
        conn.execute(
            "UPDATE opspilot_incidents SET control_generation=control_generation+1 WHERE incident_id=%s",
            (incident,),
        )
    before = _run_row(store, run)["lease_until"]
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.renew_lease(lease, 60)
    assert _run_row(store, run)["lease_until"] == before


def test_renewal_is_capped_at_the_run_deadline_and_refused_after_it():
    store = DurableStore(DSN)
    incident, run = _accept(store, deadline_seconds=4)
    lease = store.claim(incident, run, uuid4(), VERSIONS, lease_seconds=2)
    deadline = _run_row(store, run)["deadline"]

    until = store.renew_lease(lease, 600)
    assert until == deadline
    assert _run_row(store, run)["lease_until"] == deadline

    time.sleep(4.2)
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.renew_lease(lease, 600)
    assert _run_row(store, run)["lease_until"] == deadline
    with pytest.raises(PersistenceError, match="DEADLINE_EXCEEDED"):
        store.claim(incident, run, uuid4(), VERSIONS)


def test_deadline_passed_is_refused_even_when_the_lease_itself_is_still_unexpired():
    """claim() 把租约封顶到 deadline，过期后直接按 deadline 拒绝。"""
    store = DurableStore(DSN)
    incident, run = _accept(store, deadline_seconds=3)
    lease = store.claim(incident, run, uuid4(), VERSIONS, lease_seconds=600)
    before = _run_row(store, run)
    assert before["lease_until"] == before["deadline"]
    time.sleep(3.2)
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.renew_lease(lease, 600)
    assert _run_row(store, run)["lease_until"] == before["lease_until"]


@pytest.mark.parametrize("state", ["waiting_human", "blocked"])
def test_a_run_that_is_not_running_is_not_renewed_even_with_a_live_lease(state):
    """交给人工或版本阻塞的 Run 不再由 worker 持有；租约行未清也不得续期。"""
    store = DurableStore(DSN)
    incident, run = _accept(store)
    lease = store.claim(incident, run, uuid4(), VERSIONS, lease_seconds=30)
    with store.transaction() as conn:
        conn.execute("UPDATE opspilot_runs SET state=%s WHERE run_id=%s", (state, run))
    before = _run_row(store, run)["lease_until"]
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.renew_lease(lease, 60)
    assert _run_row(store, run)["lease_until"] == before


def test_a_lease_that_reached_the_deadline_is_not_extended_past_it():
    """已被封顶到 deadline 的租约再续也停在 deadline，不因 GREATEST 越过。"""
    store = DurableStore(DSN)
    incident, run = _accept(store, deadline_seconds=5)
    lease = store.claim(incident, run, uuid4(), VERSIONS, lease_seconds=600)
    deadline = _run_row(store, run)["deadline"]
    # claim() 与续期都在持久化边界封顶到 deadline。
    assert _run_row(store, run)["lease_until"] == deadline
    assert store.renew_lease(lease, 600) == deadline
    assert store.renew_lease(lease, 1) == deadline


def test_only_the_holder_can_renew_under_concurrency():
    store = DurableStore(DSN)
    incident, run = _accept(store)
    holder, intruder = uuid4(), uuid4()
    lease = store.claim(incident, run, holder, VERSIONS, lease_seconds=2)
    with pytest.raises(PersistenceError, match="LEASE_ACTIVE"):
        store.claim(incident, run, intruder, VERSIONS, lease_seconds=60)
    forged = Lease(incident, run, intruder, lease.epoch, lease.control_generation)
    wrong_epoch = Lease(
        incident, run, holder, lease.epoch + 1, lease.control_generation
    )

    def attempt(candidate: Lease) -> str:
        try:
            store.renew_lease(candidate, 60)
            return "ok"
        except PersistenceError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(attempt, [lease, forged, wrong_epoch] * 4))
    assert outcomes[0::3] == ["ok"] * 4
    assert outcomes[1::3] == ["CONTROL_DENIED"] * 4
    assert outcomes[2::3] == ["CONTROL_DENIED"] * 4
    row = _run_row(store, run)
    assert (row["owner"], row["epoch"]) == (holder, lease.epoch)
    assert row["lease_until"] - row["now"] > timedelta(seconds=50)

    # 持有者放弃续期并让租约到期后，另一 worker 才能接手，且从此只有它能续。
    _expire(store, run)
    taken = store.claim(incident, run, intruder, VERSIONS, lease_seconds=60)
    assert taken.epoch == lease.epoch + 1
    with pytest.raises(PersistenceError, match="CONTROL_DENIED"):
        store.renew_lease(lease, 60)
    assert store.renew_lease(taken, 60) > _run_row(store, run)["now"]


def test_renew_lease_locks_the_incident_before_the_run():
    """与其它写路径同序（incidents -> runs），否则与 control()/publish() 交叉死锁。"""
    store = DurableStore(DSN)
    incident, run = _accept(store, deadline_seconds=300)
    lease = store.claim(incident, run, uuid4(), VERSIONS, lease_seconds=300)
    blocked = threading.Event()
    with psycopg.connect(DSN, row_factory=dict_row) as holder:
        holder.execute(
            "SELECT 1 FROM opspilot_incidents WHERE incident_id=%s FOR UPDATE",
            (incident,),
        )

        def attempt() -> None:
            blocked.set()
            try:
                store.renew_lease(lease, 10)
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
                    "renew_lease 在锁 incident 之前先锁了 run，与 control()/publish() 的顺序相反"
                ) from None
    worker.join(timeout=5)
