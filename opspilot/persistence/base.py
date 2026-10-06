"""Connection, transaction and the fences every business object shares.

`transaction()` is the single place that opens a connection (ADR-0007 PR-b
replaces it with a pool); `_lock_scope` is the single lock-ordering helper
(incident row first, then target scope -- PR #22).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

import psycopg
from psycopg import IsolationLevel, errors
from psycopg.rows import dict_row

from opspilot import schema

Connection = psycopg.Connection[dict[str, Any]]

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
    def __init__(self, dsn: str):
        self.dsn = dsn

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
        """
        try:
            with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
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
