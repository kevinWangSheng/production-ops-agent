"""Connection, transaction and the fences every business object shares.

`transaction()` is the single place that opens a connection (ADR-0007 PR-b
replaces it with a pool); `_lock_scope` is the single lock-ordering helper
(incident row first, then target scope -- PR #22).
"""

from __future__ import annotations

import atexit
import os
import threading
import weakref
from collections import deque
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

import psycopg
from psycopg import IsolationLevel, errors
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from opspilot import schema

Connection = psycopg.Connection[dict[str, Any]]


@dataclass(frozen=True)
class PoolConfig:
    """连接池参数（ADR-0007 决定 3）。`from_env()` 读 `OPSPILOT_POOL_*`。

    `min_size` 是空闲时保留的连接数，`max_size` 是并发事务上限（多出的
    请求排队等 `timeout` 秒，超时按 TIMEOUT 报给调用方）。`max_idle` 秒
    没用到的多余连接会关掉。默认值按单进程的并发量取：worker 只有一个
    调查线程，web 是小流量工作台；数据库侧 `max_connections` 要容得下
    web + worker 两个进程的 `max_size` 再加运维连接（lab 实例是 12）。
    """

    min_size: int = 1
    max_size: int = 4
    timeout: float = 5.0
    max_idle: float = 60.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> PoolConfig:
        env = os.environ if env is None else env
        return cls(
            min_size=int(env.get("OPSPILOT_POOL_MIN_SIZE", cls.min_size)),
            max_size=int(env.get("OPSPILOT_POOL_MAX_SIZE", cls.max_size)),
            timeout=float(env.get("OPSPILOT_POOL_TIMEOUT_SECONDS", cls.timeout)),
            max_idle=float(env.get("OPSPILOT_POOL_MAX_IDLE_SECONDS", cls.max_idle)),
        )


_PoolKey = tuple[int, str, PoolConfig]


@dataclass
class _SharedPool:
    """进程内按 (pid, dsn, 配置) 共享的池和还在用它的 store 数。"""

    pool: ConnectionPool[Connection]
    users: int = 0


_POOLS: dict[_PoolKey, _SharedPool] = {}
_POOLS_LOCK = threading.Lock()
# store 被 GC 回收时只把它的池登记到这里，不拿锁：finalizer 可能在
# `_connection_pool` 持锁分配对象触发的 GC 里运行，同线程再拿锁就是死锁。
# 真正的递减与关池在下一次 `_connection_pool()` / `close()` 持锁时做。
_RELEASED: deque[tuple[_PoolKey, _SharedPool]] = deque()


def _note_released(key: _PoolKey, shared: _SharedPool) -> None:
    _RELEASED.append((key, shared))


def _drain_released_locked() -> list[ConnectionPool[Connection]]:
    """持 `_POOLS_LOCK` 调用：结清已回收 store 的使用计数，返回该关的池。

    只结清本进程建的池。fork 继承来的池（键里的 pid 是父进程）留在表里、
    永不关闭也永不丢引用：关闭或回收它的连接都会在与父进程共享的 socket 上
    发 Terminate，杀掉父进程的会话。
    """
    pid = os.getpid()
    closing: list[ConnectionPool[Connection]] = []
    while _RELEASED:
        key, shared = _RELEASED.popleft()
        if key[0] != pid:
            continue
        shared.users -= 1
        if shared.users <= 0 and _POOLS.get(key) is shared:
            del _POOLS[key]
            closing.append(shared.pool)
    return closing


def _settle_released() -> None:
    """结清已回收 store 留下的释放登记，关掉没人用的池。

    在每次构造 store、用池和 `close()` 时调用，所以一个用过即弃的 store
    最晚在下一个 store 构造时释放连接，而不是等到 GC 之后某次恰好用池。
    """
    with _POOLS_LOCK:
        closing = _drain_released_locked()
    for pool in closing:
        pool.close()


def close_pools() -> None:
    """关掉本进程建的全部连接池；进程退出时由 atexit 调用，也可显式调用。

    之后任何 store 再开事务会重建池。继承自父进程的池不动（见
    `_drain_released_locked`）。
    """
    pid = os.getpid()
    with _POOLS_LOCK:
        _RELEASED.clear()
        mine = [key for key in _POOLS if key[0] == pid]
        closing = [_POOLS.pop(key).pool for key in mine]
    for pool in closing:
        pool.close()


atexit.register(close_pools)


def _reset_session(conn: Connection) -> None:
    """连接回池时抹掉 `transaction(snapshot=True)` 设的会话级属性。

    `isolation_level` / `read_only` 是 psycopg 连接对象上的属性，决定下一个
    BEGIN 怎么发；不复位的话一次快照事务会把这条连接永久变成只读。
    池在调用本函数前已 commit/rollback，连接处于 IDLE，赋值不发任何语句。
    """
    conn.isolation_level = None
    conn.read_only = None


# 存储层失败按调用方该做什么区分，而不是压成单一的「存储不可用」：
#   RETRY              串行化冲突或死锁，同一请求重放即可
#   TIMEOUT            语句或锁等待超时，重试前应退避
#   IDENTITY_CONFLICT  同一 intake_key 已绑定别的身份，重试无用
#   READ_ONLY_PATH     写落在只读事务或只读服务端上：可能是在一致快照事务里
#                      写入的实现错误，也可能是连到了备库/只读副本；都不该重试
#   STORAGE_UNAVAILABLE 连接级故障，重试取决于存储是否恢复
_ERROR_CODES: tuple[tuple[type[psycopg.Error], str], ...] = (
    (errors.UniqueViolation, "IDENTITY_CONFLICT"),
    (errors.ReadOnlySqlTransaction, "READ_ONLY_PATH"),
    (errors.DeadlockDetected, "RETRY"),
    (errors.SerializationFailure, "RETRY"),
    (errors.LockNotAvailable, "TIMEOUT"),
    (errors.QueryCanceled, "TIMEOUT"),
    # 池里 max_size 条连接都在忙、等了 PoolConfig.timeout 秒：和锁等待超时一样，
    # 退避后重试即可，不是存储不可用。
    (PoolTimeout, "TIMEOUT"),
)


class PersistenceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Lease:
    incident_id: UUID
    run_id: UUID
    owner: UUID
    epoch: int
    control_generation: int
    global_suspension_generation: int = 0
    target_suspension_generation: int = 0


class _StoreBase:
    def __init__(self, dsn: str, *, pool: PoolConfig | None = None):
        self.dsn = dsn
        self.pool_config = PoolConfig.from_env() if pool is None else pool
        self._shared: _SharedPool | None = None
        self._finalizer: weakref.finalize[Any, Any] | None = None
        _settle_released()

    def _connection_pool(self) -> ConnectionPool[Connection]:
        """本进程里同一 DSN + 配置的所有 store 共用一个池，首次事务时才建。

        池按进程共享而不是一个 store 一个：测试里每个用例都 `DurableStore(DSN)`，
        若各建一池，连接数会随对象数增长直到 GC 才回收（lab 实例
        `max_connections=12` 当场打满）。池记着用它的 store 数，最后一个 store
        被回收或 `close()` 时关池，所以指向临时库的 store 不会留下连接。
        键里带 pid：fork 出的子进程不能沿用父进程的 socket，自己另建；父进程
        的池只留在表里，`close()`/`close_pools()` 都不碰它。
        """
        key = (os.getpid(), self.dsn, self.pool_config)
        with _POOLS_LOCK:
            closing = _drain_released_locked()
            shared = self._shared
            if shared is None or _POOLS.get(key) is not shared:
                shared = self._acquire_locked(key)
        for pool in closing:
            pool.close()
        return shared.pool

    def _acquire_locked(self, key: _PoolKey) -> _SharedPool:
        """持 `_POOLS_LOCK` 调用：登记为 `key` 对应池的使用者，没有就建。"""
        shared = _POOLS.get(key)
        if shared is None:
            config = self.pool_config
            pool: ConnectionPool[Connection] = ConnectionPool(
                self.dsn,
                open=False,
                kwargs={"row_factory": dict_row},
                min_size=config.min_size,
                max_size=config.max_size,
                timeout=config.timeout,
                max_idle=config.max_idle,
                # 取出前先 ping：数据库重启后留在池里的死连接直接换新，
                # 调用方看不到一次性的 STORAGE_UNAVAILABLE。
                check=ConnectionPool.check_connection,
                reset=_reset_session,
                name=f"opspilot-{key[0]}-{len(_POOLS)}",
            )
            pool.open(wait=False)
            shared = _POOLS[key] = _SharedPool(pool)
        shared.users += 1
        self._shared = shared
        # 旧的 finalizer（fork 前或 close() 前的池）已失效，换成当前池的。
        if self._finalizer is not None:
            self._finalizer.detach()
        self._finalizer = weakref.finalize(self, _note_released, key, shared)
        return shared

    def close(self) -> None:
        """不再使用池；若本 store 是最后一个使用者则关池，之后再开事务会重建。

        进程退出前调用，让连接干净地断开。
        """
        finalizer, self._finalizer, self._shared = self._finalizer, None, None
        if finalizer is not None:
            finalizer()  # 登记释放；继承自父进程的池在结清时被跳过
        _settle_released()

    @staticmethod
    def _require_row(cursor: psycopg.Cursor[dict[str, Any]]) -> dict[str, Any]:
        """按不变量必定返回一行的查询；取不到行说明存储状态不一致。

        与 `STORAGE_UNAVAILABLE` 区分：那是瞬时故障、重试可能成功；
        这里是业务记录自身不一致，重试必然再次失败，需要人工介入。
        """
        row = cursor.fetchone()
        if row is None:
            raise PersistenceError("INCONSISTENT_STATE")
        return row

    @staticmethod
    def _db_now(conn: Connection) -> datetime:
        """唯一的当前时间来源：数据库时钟，避免应用与存储时钟不一致。"""
        row = _StoreBase._require_row(conn.execute("SELECT clock_timestamp() AS now"))
        return cast(datetime, row["now"])

    @staticmethod
    def _lease_revoked(row: dict[str, Any], lease: Lease, now: datetime) -> bool:
        """租约栅栏的唯一实现：四条写路径共用，语义不再逐份分叉。

        `row` 必须带 `incident_generation` —— 人工决定只递增
        `opspilot_incidents.control_generation`，`opspilot_runs` 上的同名列是
        `claim()`/`control()` 盖下的副本。栅栏要拦的是「租约早于一个更新的人工
        决定」，因此权威是事故代际，不是 run 行里的副本。

        `lease_until IS NULL` 判为撤销：`Lease` 是持有租约的凭据，而 run 行说
        它没有租约。`control()` 清空 `lease_until` 正是收回 worker 权限的动作，
        把 NULL 读成通过，等于让被收回权限的 worker 依赖 owner 一项守住。
        `publish()` 原本就是这个语义，另三条取它。
        """
        return (
            row["owner"] != lease.owner
            or row["epoch"] != lease.epoch
            or row["incident_generation"] != lease.control_generation
            or row["lease_until"] is None
            or row["lease_until"] <= now
            or row["deadline"] <= now
            or bool(row.get("global_suspended", False))
            or bool(row.get("target_suspended", False))
            or int(row.get("global_generation", 0))
            != lease.global_suspension_generation
            or int(row.get("target_generation", 0))
            != lease.target_suspension_generation
        )

    @contextmanager
    def transaction(self, *, snapshot: bool = False) -> Iterator[Connection]:
        """一个事务。`snapshot=True` 用 REPEATABLE READ 取一致快照。

        只读路径需要横跨多条语句看到同一个状态。默认的 READ COMMITTED
        逐语句取快照，会读出互相矛盾的行；REPEATABLE READ 在第一条语句
        固定快照，且不像 `FOR SHARE` 那样阻塞写入方。

        一并置为 read only：REPEATABLE READ 下取行锁或写入会在并发提交后
        概率性抛 `SerializationFailure`，而调用方没有重试循环。只读事务里
        这类语句直接被拒绝，把隐患变成确定性的失败而不是偶发的 RETRY。

        连接来自进程内的 `psycopg_pool` 池（ADR-0007 决定 3）。退出时池沿用
        `with psycopg.connect()` 的语义：正常退出 commit、异常 rollback，然后
        连接回池而不是关闭；`SET LOCAL` 随事务结束失效，快照属性由
        `_reset_session` 复位，所以下一个借到这条连接的事务看到的是干净会话。
        """
        try:
            with self._connection_pool().connection() as conn:
                if snapshot:
                    conn.isolation_level = IsolationLevel.REPEATABLE_READ
                    conn.read_only = True
                conn.execute("SET LOCAL statement_timeout='5000ms'")
                conn.execute("SET LOCAL lock_timeout='4000ms'")
                yield conn
        except psycopg.Error as exc:
            raise PersistenceError(self._error_code(exc)) from exc

    @staticmethod
    def _error_code(exc: psycopg.Error) -> str:
        """把存储失败映射成调用方能据以决策的码，而不是一律「不可用」。"""
        for error_type, code in _ERROR_CODES:
            if isinstance(exc, error_type):
                return code
        return "STORAGE_UNAVAILABLE"

    def install(self) -> None:
        """Refuse to start unless the schema is at the Alembic head.

        The DDL lives in ``opspilot/migrations`` (ADR-0007) and runs before
        startup through an owner connection (``make migrate``); the runtime
        role needs no DDL rights. The signature is kept for existing callers.
        """
        with self.transaction() as conn:
            try:
                schema.verify_head(conn)
            except schema.SchemaNotMigrated as exc:
                raise PersistenceError("SCHEMA_NOT_MIGRATED") from exc
            except schema.SchemaVersionUnreadable as exc:
                # Distinct from STORAGE_UNAVAILABLE: the fix is a GRANT on
                # alembic_version for the runtime role (message in the cause).
                raise PersistenceError("SCHEMA_VERSION_UNREADABLE") from exc

    def _lock_scope(
        self, conn: Connection, incident_id: UUID | None = None, *, lock: bool = True
    ) -> dict[str, Any]:
        # ``lock=False`` is the read-only snapshot path (``control_state``):
        # a REPEATABLE READ transaction is opened read-only and PostgreSQL
        # refuses ``FOR SHARE`` there; the snapshot itself is the consistency
        # guarantee (independent review P1).
        share = " FOR SHARE" if lock else ""
        scope = self._require_row(
            conn.execute(
                "SELECT global_suspended,global_generation FROM opspilot_scope_controls WHERE scope_id=1"
                + share
            )
        )
        scope.update(target_suspended=False, target_generation=0)
        if incident_id is not None:
            target = conn.execute(
                "SELECT target_id FROM opspilot_incidents WHERE incident_id=%s",
                (incident_id,),
            ).fetchone()
            if target and target["target_id"] is not None:
                row = conn.execute(
                    "SELECT suspended,generation FROM opspilot_target_suspensions WHERE target_id=%s"
                    + share,
                    (target["target_id"],),
                ).fetchone()
                if row is None:
                    raise PersistenceError("INCONSISTENT_STATE")
                scope.update(
                    target_suspended=row["suspended"],
                    target_generation=row["generation"],
                )
        return scope
