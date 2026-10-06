"""`DurableStore.transaction()` 走 `psycopg_pool` 后的可观察语义（ADR-0007 决定 3，#77）。

池是实现细节，调用方只应看到：事务提交/回滚语义不变；一次 `snapshot=True`
不会把借到的连接永久变成只读；连接确实被复用；池满等待超时按 TIMEOUT 报出；
`close()` 之后 store 仍可用。
"""

import os
import threading
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from opspilot.persistence import DurableStore, PersistenceError, PoolConfig
from scripts.m0.postgres_lab import DSN

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

# 一条连接：每个事务都必然拿到同一条，池的复位逻辑藏不住。
SINGLE = PoolConfig(min_size=1, max_size=1, timeout=0.5)


@pytest.fixture
def store():
    store = DurableStore(DSN, pool=SINGLE)
    store.install()
    yield store
    store.close()


def _backend_pid(store: DurableStore, *, snapshot: bool = False) -> int:
    with store.transaction(snapshot=snapshot) as conn:
        row = conn.execute("SELECT pg_backend_pid() AS pid").fetchone()
    assert row is not None
    return row["pid"]


def test_transactions_reuse_the_pooled_connection(store: DurableStore) -> None:
    pids = {_backend_pid(store) for _ in range(5)}
    assert len(pids) == 1


def test_snapshot_does_not_leave_the_connection_read_only(store: DurableStore) -> None:
    assert _backend_pid(store, snapshot=True) == _backend_pid(store)
    with store.transaction(snapshot=True) as conn:
        row = conn.execute("SHOW transaction_read_only").fetchone()
        assert row is not None and row["transaction_read_only"] == "on"
        row = conn.execute("SHOW transaction_isolation").fetchone()
        assert row is not None and row["transaction_isolation"] == "repeatable read"
    # 同一条连接回池后再借出：默认 READ COMMITTED、可写。
    incident, run = uuid4(), uuid4()
    store.accept(
        incident,
        run,
        f"m1-pool-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=5),
        budget_limit=10,
        versions={"state": "v1"},
    )
    with store.transaction() as conn:
        row = conn.execute("SHOW transaction_read_only").fetchone()
        assert row is not None and row["transaction_read_only"] == "off"
        row = conn.execute("SHOW transaction_isolation").fetchone()
        assert row is not None and row["transaction_isolation"] == "read committed"
    assert store.rebuild(incident)["incident_id"] == incident


def test_set_local_timeouts_apply_per_transaction(store: DurableStore) -> None:
    for _ in range(2):
        with store.transaction() as conn:
            row = conn.execute("SHOW statement_timeout").fetchone()
            assert row is not None and row["statement_timeout"] == "5s"
            row = conn.execute("SHOW lock_timeout").fetchone()
            assert row is not None and row["lock_timeout"] == "4s"


def test_rollback_on_error_then_the_connection_is_clean(store: DurableStore) -> None:
    incident, run = uuid4(), uuid4()
    with pytest.raises(PersistenceError, match="STORAGE_UNAVAILABLE"):
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO opspilot_incidents (incident_id, intake_key, state, control_generation, created_at) "
                "VALUES (%s, %s, 'open', 0, clock_timestamp())",
                (incident, f"m1-pool-rollback-{incident}"),
            )
            conn.execute("SELECT 1/0")
    with store.transaction(snapshot=True) as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM opspilot_incidents WHERE incident_id=%s", (incident,)
            ).fetchone()
            is None
        )
    # 连接没有卡在失败事务里：下一次写照常提交。
    store.accept(
        incident,
        run,
        f"m1-pool-{incident}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=5),
        budget_limit=10,
        versions={"state": "v1"},
    )
    assert store.rebuild(incident)["incident_id"] == incident


def test_exhausted_pool_reports_timeout(store: DurableStore) -> None:
    holding = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with store.transaction():
            holding.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert holding.wait(5)
        with pytest.raises(PersistenceError, match="TIMEOUT"):
            with store.transaction():
                pass
    finally:
        release.set()
        holder.join(5)
    # 连接归还后立刻可用。
    _backend_pid(store)


def test_close_then_reuse_opens_a_fresh_pool(store: DurableStore) -> None:
    first = _backend_pid(store)
    store.close()
    assert _backend_pid(store) != first


def test_pool_config_from_env_overrides_defaults() -> None:
    assert PoolConfig.from_env({}) == PoolConfig()
    assert PoolConfig.from_env(
        {
            "OPSPILOT_POOL_MIN_SIZE": "0",
            "OPSPILOT_POOL_MAX_SIZE": "3",
            "OPSPILOT_POOL_TIMEOUT_SECONDS": "1.5",
            "OPSPILOT_POOL_MAX_IDLE_SECONDS": "30",
        }
    ) == PoolConfig(min_size=0, max_size=3, timeout=1.5, max_idle=30.0)


def test_stores_with_the_same_dsn_share_one_pool_until_the_last_one_goes() -> None:
    import gc

    import psycopg

    config = PoolConfig(min_size=1, max_size=1, timeout=0.5)
    first, second = DurableStore(DSN, pool=config), DurableStore(DSN, pool=config)
    pid = _backend_pid(first)
    assert _backend_pid(second) == pid  # 同一条池化连接
    first.close()
    assert _backend_pid(second) == pid  # 还有使用者，池不关
    del second
    gc.collect()
    # 回收只登记释放；结清与关池发生在下一次任何 store 用池或 close() 时。
    DurableStore(DSN, pool=config).close()
    with psycopg.connect(DSN) as probe:
        row = probe.execute(
            "SELECT 1 FROM pg_stat_activity WHERE pid=%s", (pid,)
        ).fetchone()
    assert row is None  # 最后一个 store 回收后池关闭，连接断开


def _sessions(application_name_prefix: str) -> int:
    import psycopg

    with psycopg.connect(DSN) as probe:
        row = probe.execute(
            "SELECT count(*) AS n FROM pg_stat_activity WHERE application_name LIKE %s",
            (application_name_prefix + "%",),
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_dead_stores_with_distinct_dsns_do_not_keep_pools_alive() -> None:
    """审查发现 1：store 用过即弃、之后再没有池操作，池不能永远留在表里。"""
    import gc

    from opspilot.persistence import base as persistence

    tag = f"m1-pool-leak-{uuid4().hex[:8]}"
    config = PoolConfig(min_size=1, max_size=1, timeout=0.5)
    for i in range(6):
        store = DurableStore(f"{DSN} application_name={tag}-{i}", pool=config)
        _backend_pid(store)
        del store
        gc.collect()
    # 之后只构造 store、不开事务：构造本身就得结清已回收的池。
    DurableStore(DSN, pool=config)
    assert _sessions(tag) == 0
    # 进程退出路径：关掉本进程全部池，不留连接。
    persistence.close_pools()
    assert _sessions(tag) == 0
    assert not [key for key in persistence._POOLS if key[0] == os.getpid()]


def test_forked_child_close_does_not_kill_the_parents_connection(
    store: DurableStore,
) -> None:
    """审查发现 2：子进程继承的池不属于它，close() 不得在共享 socket 上发 Terminate。"""
    import psycopg

    pid = _backend_pid(store)
    # 连接是异步归还的；等它真的回到池里，子进程关池时才会碰到这条 socket。
    pool = store._connection_pool()
    deadline = time.monotonic() + 5
    while pool.get_stats()["pool_available"] < 1:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    child = os.fork()
    if child == 0:  # pragma: no cover - runs in the forked child
        code = 1
        try:
            store.close()
            DurableStore(DSN, pool=store.pool_config).close()
            # 子进程自己开事务要走自己的新池，和父进程的 backend 不同。
            code = 0 if _backend_pid(store) != pid else 2
        finally:
            os._exit(code)
    _, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    # 父进程的池化连接还活着，而且就是原来那条 backend。
    assert _backend_pid(store) == pid
    with psycopg.connect(DSN) as probe:
        assert (
            probe.execute(
                "SELECT 1 FROM pg_stat_activity WHERE pid=%s", (pid,)
            ).fetchone()
            is not None
        )
