"""PostgreSQL business-state authority for the first durable M1 slice."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid4

import psycopg
from psycopg import IsolationLevel, errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

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

# 非 cancel 的人工动作只对这些 run 状态开放；其余一律拒绝（fail closed）。
_CONTROL_OPEN_RUN_STATES = frozenset({"queued", "running", "paused", "waiting_human"})
# 迟到历史占用 (run_id, logical_key) 唯一键。业务步骤不得使用此外缀，否则会
# 把迟到结果挤掉或把 late_result 行当成已提交步骤返回。
_LATE_RESULT_KEY_PREFIX = "late_result:"


class PersistenceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Lease:
    incident_id: UUID
    run_id: UUID
    owner: UUID
    epoch: int
    control_generation: int


def _tool_plan(response: Any) -> Any:
    """The tool plan a committed ModelStep carries (C3 §4/§7).

    The investigation loop commits the complete model response as
    ``{"assistant": {..., "tool_calls": [...]}, "finish_reason": ..., ...}``;
    the M0 harness and older tests commit the assistant message itself, with
    ``tool_calls`` at the top level. Recovery must see the plan in both
    shapes, otherwise ``pending_tools`` is empty and a new attempt re-runs
    the round instead of finishing the committed tools.
    """
    if not isinstance(response, dict):
        # A committed model response is always an object. A scalar, list or
        # null here is corrupted/legacy business data, so hand back the same
        # non-list sentinel used for a corrupt ``assistant`` rather than an
        # empty plan the caller would accept as "this step had no tools".
        return None
    if "assistant" in response:
        assistant = response["assistant"]
        if not isinstance(assistant, dict):
            # A loop-shaped step whose assistant message is corrupt: hand back
            # a non-list so the caller's validation fails closed.
            return None
        return _tool_calls(assistant)
    return _tool_calls(response)


def _tool_calls(container: dict[str, Any]) -> Any:
    """``tool_calls`` absent or ``None`` means no tools; any other non-list
    value (``{}``, ``""``, ``0``, ...) is corrupted business data, not an
    empty plan -- ``value or []`` would silently swallow a falsey one of
    those into a valid-looking empty list, so check the type explicitly and
    hand back a non-list for the caller to fail closed on.
    """
    calls = container.get("tool_calls")
    if calls is None:
        return []
    return calls if isinstance(calls, list) else None


def _completed_tool_ordinals(tool_results: Any, call_count: int) -> set[int]:
    """Validate and index durable tool-result checkpoints.

    Recovery must fail closed on corrupted business rows: treating a falsey
    mapping as an empty result list would replay an already-executed query,
    while malformed records could otherwise crash with an incidental
    ``AttributeError``.  ``commit_tool`` writes mapping results with a unique,
    in-range integer ordinal, so anything else is inconsistent state.
    """
    if not isinstance(tool_results, list):
        raise PersistenceError("INCONSISTENT_STATE")
    completed: set[int] = set()
    for item in tool_results:
        if not isinstance(item, Mapping):
            raise PersistenceError("INCONSISTENT_STATE")
        ordinal = item.get("ordinal")
        if (
            type(ordinal) is not int
            or ordinal < 0
            or ordinal >= call_count
            or ordinal in completed
            or not isinstance(item.get("result"), Mapping)
        ):
            raise PersistenceError("INCONSISTENT_STATE")
        completed.add(ordinal)
    return completed


class DurableStore:
    """Small transactional store; callers only observe committed business rows."""

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
        row = DurableStore._require_row(conn.execute("SELECT clock_timestamp() AS now"))
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
        with self.transaction() as conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS opspilot_incidents (
              incident_id uuid PRIMARY KEY, intake_key text UNIQUE NOT NULL,
              state text NOT NULL, lifecycle text NOT NULL DEFAULT 'open', control_generation integer NOT NULL DEFAULT 0,
              current_run_id uuid, conclusion jsonb, created_at timestamptz NOT NULL DEFAULT clock_timestamp()
            );
            ALTER TABLE opspilot_incidents ADD COLUMN IF NOT EXISTS lifecycle text NOT NULL DEFAULT 'open';
            CREATE TABLE IF NOT EXISTS opspilot_runs (
              run_id uuid PRIMARY KEY, incident_id uuid NOT NULL REFERENCES opspilot_incidents,
              state text NOT NULL, epoch integer NOT NULL DEFAULT 0, owner uuid,
              lease_until timestamptz, control_generation integer NOT NULL,
              budget_limit bigint NOT NULL, budget_reserved bigint NOT NULL DEFAULT 0,
              budget_spent bigint NOT NULL DEFAULT 0, budget_unknown bigint NOT NULL DEFAULT 0,
              deadline timestamptz NOT NULL, versions jsonb NOT NULL, input_watermark integer NOT NULL DEFAULT 0
            );
            -- PostgreSQL 不为外键列自动建索引。control() 按 incident_id 推进 run
            -- 状态，前置的 incident 行锁把 worker 写路径排在这条 UPDATE 之后，
            -- 全表扫描会随表增长直接变成写路径的排队时间。
            CREATE INDEX IF NOT EXISTS opspilot_runs_incident_id_idx ON opspilot_runs(incident_id);
            CREATE TABLE IF NOT EXISTS opspilot_steps (
              step_id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES opspilot_runs,
              sequence integer NOT NULL DEFAULT 0, logical_key text NOT NULL, status text NOT NULL, response jsonb,
              tool_results jsonb NOT NULL DEFAULT '[]'::jsonb, control_generation integer NOT NULL,
              observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
              UNIQUE(run_id, logical_key)
            );
            ALTER TABLE opspilot_steps ADD COLUMN IF NOT EXISTS sequence integer NOT NULL DEFAULT 0;
            ALTER TABLE opspilot_steps ADD COLUMN IF NOT EXISTS observed_at timestamptz NOT NULL DEFAULT clock_timestamp();
            CREATE TABLE IF NOT EXISTS opspilot_controls (
              audit_id uuid PRIMARY KEY, incident_id uuid NOT NULL REFERENCES opspilot_incidents,
              action text NOT NULL, expected_generation integer NOT NULL,
              resulting_generation integer NOT NULL, actor text NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp()
            );
            -- 同上：审计行按 incident 读取，且外键列无索引时父行的键变更要扫全表。
            CREATE INDEX IF NOT EXISTS opspilot_controls_incident_id_idx ON opspilot_controls(incident_id);
            CREATE TABLE IF NOT EXISTS opspilot_budget_reservations (
              reservation_id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES opspilot_runs,
              amount bigint NOT NULL, state text NOT NULL DEFAULT 'reserved', UNIQUE(run_id, reservation_id)
            );
            """)

    def accept(
        self,
        incident_id: UUID,
        run_id: UUID,
        intake_key: str,
        *,
        deadline: datetime,
        budget_limit: int,
        versions: dict[str, str],
    ) -> None:
        """Create incident/run atomically. Return only after commit."""
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT incident_id,current_run_id FROM opspilot_incidents WHERE intake_key=%s",
                (intake_key,),
            ).fetchone()
            if row:
                if row["incident_id"] != incident_id or row["current_run_id"] != run_id:
                    raise PersistenceError("IDENTITY_CONFLICT")
                return
            inserted = conn.execute(
                # 不限定冲突目标：同一身份并发重投会同时撞上 intake_key 唯一索引
                # 和 incident_id 主键，只声明前者会让后者漏成 UniqueViolation。
                "INSERT INTO opspilot_incidents(incident_id,intake_key,state,lifecycle,current_run_id) VALUES(%s,%s,'queued','open',%s) ON CONFLICT DO NOTHING RETURNING incident_id",
                (incident_id, intake_key, run_id),
            ).fetchone()
            existing = conn.execute(
                "SELECT incident_id,current_run_id FROM opspilot_incidents WHERE intake_key=%s",
                (intake_key,),
            ).fetchone()
            if existing is None:
                # 插入被主键冲突吞掉：这个 incident_id 已经绑定到别的 intake_key。
                # 存储本身一致，是调用方给了冲突的身份。
                raise PersistenceError("IDENTITY_CONFLICT")
            if (
                existing["incident_id"] != incident_id
                or existing["current_run_id"] != run_id
            ):
                raise PersistenceError("IDENTITY_CONFLICT")
            if inserted is None:
                return
            conn.execute(
                "INSERT INTO opspilot_runs(run_id,incident_id,state,control_generation,budget_limit,deadline,versions) VALUES(%s,%s,'queued',0,%s,%s,%s)",
                (run_id, incident_id, budget_limit, deadline, Jsonb(versions)),
            )

    def new_run(
        self,
        incident_id: UUID,
        run_id: UUID,
        *,
        expected_generation: int,
        deadline: datetime,
        budget_limit: int,
        versions: dict[str, str],
        actor: str,
    ) -> int:
        """Continue a cancelled incident with a fresh Run and control generation."""
        if type(expected_generation) is not int or expected_generation < 0:
            raise PersistenceError("INVALID_INPUT")
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT state,control_generation,current_run_id FROM opspilot_incidents WHERE incident_id=%s FOR UPDATE",
                (incident_id,),
            ).fetchone()
            if not row:
                raise PersistenceError("UNKNOWN_IDENTITY")
            # run_id 是幂等键，但 queued + 当前 Run 也是 accept()/follow_up 之后
            # 的状态。只有已经写过 new_run 审计的接续才能当作丢失确认后的重试。
            existing_run = conn.execute(
                "SELECT control_generation FROM opspilot_runs WHERE run_id=%s AND incident_id=%s",
                (run_id, incident_id),
            ).fetchone()
            if existing_run is not None:
                generation = int(existing_run["control_generation"])
                replay = conn.execute(
                    "SELECT 1 FROM opspilot_controls WHERE incident_id=%s AND action='new_run' AND resulting_generation=%s",
                    (incident_id, generation),
                ).fetchone()
                if (
                    replay is not None
                    and row["state"] == "queued"
                    and row["current_run_id"] == run_id
                    and int(row["control_generation"]) == generation
                    and expected_generation == generation - 1
                ):
                    return generation
                if row["current_run_id"] == run_id and row["state"] != "cancelled":
                    raise PersistenceError("ILLEGAL_TRANSITION")
                raise PersistenceError("IDENTITY_CONFLICT")
            if row["state"] != "cancelled":
                raise PersistenceError("ILLEGAL_TRANSITION")
            if int(row["control_generation"]) != expected_generation:
                raise PersistenceError("CONTROL_CONFLICT")
            nxt = int(row["control_generation"]) + 1
            conn.execute(
                "INSERT INTO opspilot_runs(run_id,incident_id,state,control_generation,budget_limit,deadline,versions) VALUES(%s,%s,'queued',%s,%s,%s,%s)",
                (run_id, incident_id, nxt, budget_limit, deadline, Jsonb(versions)),
            )
            conn.execute(
                "UPDATE opspilot_incidents SET state='queued',lifecycle='open',control_generation=%s,current_run_id=%s,conclusion=NULL WHERE incident_id=%s",
                (nxt, run_id, incident_id),
            )
            conn.execute(
                "INSERT INTO opspilot_controls(audit_id,incident_id,action,expected_generation,resulting_generation,actor) VALUES(%s,%s,'new_run',%s,%s,%s)",
                (uuid4(), incident_id, nxt - 1, nxt, actor),
            )
            return nxt

    @staticmethod
    def _late_result(
        conn: Connection,
        run_id: UUID,
        logical_key: str,
        payload: dict[str, Any],
        generation: int,
    ) -> None:
        """登记迟到结果。logical_key 必须落在保留前缀下，重放不得另写一行。"""
        if (
            conn.execute(
                "SELECT 1 FROM opspilot_runs WHERE run_id=%s",
                (run_id,),
            ).fetchone()
            is None
        ):
            return
        sequence = DurableStore._require_row(
            conn.execute(
                "SELECT COALESCE(MAX(sequence), -1) + 1 AS next_sequence FROM opspilot_steps WHERE run_id=%s",
                (run_id,),
            )
        )["next_sequence"]
        conn.execute(
            "INSERT INTO opspilot_steps(step_id,run_id,sequence,logical_key,status,response,control_generation,observed_at) VALUES(%s,%s,%s,%s,'late_result',%s,%s,clock_timestamp()) ON CONFLICT (run_id, logical_key) DO NOTHING",
            (uuid4(), run_id, sequence, logical_key, Jsonb(payload), generation),
        )

    def claim(
        self,
        incident_id: UUID,
        run_id: UUID,
        owner: UUID,
        versions: dict[str, str],
        lease_seconds: int = 30,
    ) -> Lease:
        incompatible = False
        lease = None
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT i.control_generation AS incident_generation,i.state AS incident_state,r.state AS run_state,r.epoch,r.lease_until,r.deadline,r.versions FROM opspilot_incidents i JOIN opspilot_runs r ON r.incident_id=i.incident_id WHERE i.incident_id=%s AND r.run_id=%s FOR UPDATE",
                (incident_id, run_id),
            ).fetchone()
            if not row:
                raise PersistenceError("UNKNOWN_IDENTITY")
            now = self._db_now(conn)
            active_lease = (
                row["run_state"] == "running"
                and row["lease_until"] is not None
                and row["lease_until"] > now
            )
            if active_lease:
                raise PersistenceError("LEASE_ACTIVE")
            if row["deadline"] <= now:
                raise PersistenceError("DEADLINE_EXCEEDED")
            # Human control takes precedence over version incompatibility. A
            # claim must not rewrite a paused or terminal run as blocked.
            if row["incident_state"] in {"completed", "cancelled", "paused"}:
                raise PersistenceError("CONTROL_DENIED")
            if row["run_state"] not in {"queued", "running", "blocked"}:
                raise PersistenceError("CONTROL_DENIED")
            if row["versions"] != versions:
                conn.execute(
                    "UPDATE opspilot_runs SET state='blocked' WHERE run_id=%s AND state IN ('queued','running')",
                    (run_id,),
                )
                incompatible = True
            elif row["run_state"] == "blocked":
                # A blocked run is not silently resumed by a matching version.
                raise PersistenceError("CONTROL_DENIED")
            elif (
                row["run_state"] == "running"
                and row["lease_until"] is not None
                and row["lease_until"] > now
            ):
                raise PersistenceError("LEASE_ACTIVE")
            else:
                epoch = int(row["epoch"]) + 1
                conn.execute(
                    "UPDATE opspilot_runs SET state='running',owner=%s,epoch=%s,control_generation=%s,lease_until=LEAST(clock_timestamp()+make_interval(secs=>%s),deadline) WHERE run_id=%s",
                    (owner, epoch, row["incident_generation"], lease_seconds, run_id),
                )
                lease = Lease(
                    incident_id, run_id, owner, epoch, int(row["incident_generation"])
                )
        if incompatible:
            raise PersistenceError("INCOMPATIBLE_STATE")
        assert lease is not None
        return lease

    def renew_lease(self, lease: Lease, extend_seconds: int) -> datetime:
        """把持有中的租约延至 `now + extend_seconds`，返回新的 `lease_until`。

        C3 第 6 节：续租与提交同样校验执行身份与租约。因此本方法过的是与
        写路径完全相同的栅栏：owner / epoch / 事故代际相符、run 仍 running、
        租约未过期、未过 deadline；任一不满足即 `CONTROL_DENIED`，且不改动
        `lease_until`。过期租约只能重新 `claim()` 得到新 epoch，不能续活——
        否则被硬杀的 worker 若晚些恢复，会夺回已由别的 worker 领走的 Run。

        续期不换身份：owner/epoch/generation 都不变，调用方继续用同一个
        `Lease`；返回值是数据库时钟下的新到期时间，供调用方安排下一次续期。
        新值取 `LEAST(GREATEST(lease_until, now + extend), deadline)`：
        除封顶到 deadline 外不缩短仍然更长的剩余租约，也永远不越过 Run deadline。
        """
        if extend_seconds <= 0:
            raise PersistenceError("INVALID_INPUT")
        with self.transaction() as conn:
            # 先锁 incident 再锁 run：与 reserve_budget()/commit_*() 同序，
            # 避免与 control()/publish() 交叉形成 ABBA 死锁。
            conn.execute(
                "SELECT 1 FROM opspilot_incidents WHERE incident_id=%s FOR UPDATE",
                (lease.incident_id,),
            )
            # 代际以事故行为准：人工决定只递增 opspilot_incidents.control_generation，
            # run 行上的同名列是 claim()/control() 盖下的副本。
            row = conn.execute(
                "SELECT r.state AS run_state,r.owner,r.epoch,r.lease_until,r.deadline,i.control_generation AS incident_generation FROM opspilot_runs r JOIN opspilot_incidents i ON i.incident_id=r.incident_id WHERE r.run_id=%s AND i.incident_id=%s FOR UPDATE",
                (lease.run_id, lease.incident_id),
            ).fetchone()
            now = self._db_now(conn)
            if (
                not row
                or row["run_state"] != "running"
                or row["owner"] != lease.owner
                or row["epoch"] != lease.epoch
                or row["incident_generation"] != lease.control_generation
                or row["lease_until"] is None
                or row["lease_until"] <= now
                or row["deadline"] <= now
            ):
                raise PersistenceError("CONTROL_DENIED")
            renewed = self._require_row(
                conn.execute(
                    # 检查与写入用同一个时钟值，返回值不会超过已校验的 now + extend。
                    "UPDATE opspilot_runs SET lease_until=LEAST(GREATEST(lease_until,%s+make_interval(secs=>%s)),deadline) WHERE run_id=%s RETURNING lease_until",
                    (now, extend_seconds, lease.run_id),
                )
            )
            return cast(datetime, renewed["lease_until"])

    def reserve_budget(self, lease: Lease, reservation_id: UUID, amount: int) -> None:
        if amount <= 0:
            raise PersistenceError("INVALID_INPUT")
        with self.transaction() as conn:
            # 先锁 incident 再锁 run：全模块统一这个顺序，避免与 control()/
            # publish() 交叉形成 ABBA 死锁（control 只拿到 incident_id，
            # 结构上必须先读 incident，因此以它为规范顺序）。
            conn.execute(
                "SELECT 1 FROM opspilot_incidents WHERE incident_id=%s FOR UPDATE",
                (lease.incident_id,),
            )
            row = conn.execute(
                "SELECT r.owner,r.epoch,r.lease_until,r.deadline,r.budget_limit,r.budget_reserved,r.budget_spent,r.budget_unknown,i.control_generation AS incident_generation FROM opspilot_runs r JOIN opspilot_incidents i ON i.incident_id=r.incident_id WHERE r.run_id=%s FOR UPDATE",
                (lease.run_id,),
            ).fetchone()
            if not row or self._lease_revoked(row, lease, self._db_now(conn)):
                raise PersistenceError("CONTROL_DENIED")
            existing = conn.execute(
                "SELECT amount,run_id FROM opspilot_budget_reservations WHERE reservation_id=%s",
                (reservation_id,),
            ).fetchone()
            if existing:
                if existing["amount"] != amount or existing["run_id"] != lease.run_id:
                    raise PersistenceError("IDENTITY_CONFLICT")
                return
            if (
                row["budget_reserved"]
                + row["budget_spent"]
                + row["budget_unknown"]
                + amount
                > row["budget_limit"]
            ):
                raise PersistenceError("BUDGET_EXHAUSTED")
            conn.execute(
                "INSERT INTO opspilot_budget_reservations(reservation_id,run_id,amount) VALUES(%s,%s,%s)",
                (reservation_id, lease.run_id, amount),
            )
            conn.execute(
                "UPDATE opspilot_runs SET budget_reserved=budget_reserved+%s WHERE run_id=%s",
                (amount, lease.run_id),
            )

    def lease_current(self, lease: Lease) -> bool:
        """Read the authoritative owner/epoch/generation/expiry fence."""
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT r.owner,r.epoch,r.lease_until,r.deadline,i.control_generation AS incident_generation FROM opspilot_runs r JOIN opspilot_incidents i ON i.incident_id=r.incident_id WHERE r.run_id=%s AND i.incident_id=%s",
                (lease.run_id, lease.incident_id),
            ).fetchone()
            return bool(row and not self._lease_revoked(row, lease, self._db_now(conn)))

    def abandon(self, lease: Lease) -> None:
        """Release only this exact lease after a recovery plan is rejected."""
        with self.transaction() as conn:
            conn.execute(
                "UPDATE opspilot_runs SET owner=NULL,lease_until=NULL WHERE run_id=%s AND owner=%s AND epoch=%s AND control_generation=%s",
                (lease.run_id, lease.owner, lease.epoch, lease.control_generation),
            )

    def commit_step(
        self, lease: Lease, logical_key: str, response: dict[str, Any]
    ) -> UUID:
        if logical_key.startswith(_LATE_RESULT_KEY_PREFIX):
            raise PersistenceError("INVALID_INPUT")
        with self.transaction() as conn:
            # 先锁 incident 再锁 run：全模块统一这个顺序，避免与 control()/
            # publish() 交叉形成 ABBA 死锁（control 只拿到 incident_id，
            # 结构上必须先读 incident，因此以它为规范顺序）。
            conn.execute(
                "SELECT 1 FROM opspilot_incidents WHERE incident_id=%s FOR UPDATE",
                (lease.incident_id,),
            )
            row = conn.execute(
                "SELECT r.owner,r.epoch,r.lease_until,r.deadline,i.control_generation AS incident_generation FROM opspilot_runs r JOIN opspilot_incidents i ON i.incident_id=r.incident_id WHERE r.run_id=%s FOR UPDATE",
                (lease.run_id,),
            ).fetchone()
            if not row or self._lease_revoked(row, lease, self._db_now(conn)):
                self._late_result(
                    conn,
                    lease.run_id,
                    f"{_LATE_RESULT_KEY_PREFIX}step:{logical_key}",
                    response,
                    lease.control_generation,
                )
                conn.commit()
                raise PersistenceError("CONTROL_DENIED")
            existing = conn.execute(
                "SELECT step_id FROM opspilot_steps WHERE run_id=%s AND logical_key=%s",
                (lease.run_id, logical_key),
            ).fetchone()
            if existing:
                return cast(UUID, existing["step_id"])
            step_id = uuid4()
            sequence = self._require_row(
                conn.execute(
                    "SELECT COALESCE(MAX(sequence), -1) + 1 AS next_sequence FROM opspilot_steps WHERE run_id=%s",
                    (lease.run_id,),
                )
            )["next_sequence"]
            conn.execute(
                "INSERT INTO opspilot_steps(step_id,run_id,sequence,logical_key,status,response,control_generation) VALUES(%s,%s,%s,%s,'response_committed',%s,%s)",
                (
                    step_id,
                    lease.run_id,
                    sequence,
                    logical_key,
                    Jsonb(response),
                    lease.control_generation,
                ),
            )
            return step_id

    def commit_tool(
        self, lease: Lease, step_id: UUID, ordinal: int, result: dict[str, Any]
    ) -> None:
        """Commit one tool result idempotently; late or expired leases are rejected."""
        with self.transaction() as conn:
            # 先锁 incident 再锁 run：全模块统一这个顺序，避免与 control()/
            # publish() 交叉形成 ABBA 死锁（control 只拿到 incident_id，
            # 结构上必须先读 incident，因此以它为规范顺序）。
            conn.execute(
                "SELECT 1 FROM opspilot_incidents WHERE incident_id=%s FOR UPDATE",
                (lease.incident_id,),
            )
            row = conn.execute(
                "SELECT r.owner,r.epoch,r.lease_until,r.deadline,i.control_generation AS incident_generation,s.control_generation AS step_generation,s.status AS step_status,s.response,s.tool_results FROM opspilot_runs r JOIN opspilot_incidents i ON i.incident_id=r.incident_id JOIN opspilot_steps s ON s.run_id=r.run_id WHERE r.run_id=%s AND s.step_id=%s FOR UPDATE",
                (lease.run_id, step_id),
            ).fetchone()
            if not row:
                raise PersistenceError("UNKNOWN_IDENTITY")
            tool_calls = _tool_plan(row["response"])
            if (
                type(ordinal) is not int
                or ordinal < 0
                or not isinstance(tool_calls, list)
                or ordinal >= len(tool_calls)
            ):
                raise PersistenceError("UNKNOWN_IDENTITY")
            if (
                row["step_generation"] != lease.control_generation
                or row["step_status"] == "late_result"
                or self._lease_revoked(row, lease, self._db_now(conn))
            ):
                if (
                    conn.execute(
                        "SELECT 1 FROM opspilot_steps WHERE step_id=%s AND run_id=%s",
                        (step_id, lease.run_id),
                    ).fetchone()
                    is None
                ):
                    raise PersistenceError("UNKNOWN_IDENTITY")
                self._late_result(
                    conn,
                    lease.run_id,
                    f"{_LATE_RESULT_KEY_PREFIX}tool:{step_id}:{ordinal}",
                    result,
                    lease.control_generation,
                )
                conn.commit()
                raise PersistenceError("CONTROL_DENIED")
            results = list(row["tool_results"] or [])
            if any(item.get("ordinal") == ordinal for item in results):
                return
            results.append({"ordinal": ordinal, "result": result})
            conn.execute(
                "UPDATE opspilot_steps SET tool_results=%s,status='tool_result_committed' WHERE step_id=%s",
                (Jsonb(results), step_id),
            )

    def control(
        self, incident_id: UUID, expected_generation: int, action: str, actor: str
    ) -> int:
        with self.transaction() as conn:
            # 分两步读，而不是一条 JOIN：JOIN 取不到行时无法区分「事故不存在」
            # 与「事故存在但 run 行缺失」，两者都会落到 CONTROL_CONFLICT，
            # 而该码的约定处置是「重读代际后重试」——两种情况重试都不会成功。
            # 顺序仍是先锁 incident 再锁 run，与三条写路径一致。
            row = conn.execute(
                "SELECT control_generation,state,conclusion,current_run_id FROM opspilot_incidents WHERE incident_id=%s FOR UPDATE",
                (incident_id,),
            ).fetchone()
            if not row:
                raise PersistenceError("UNKNOWN_IDENTITY")
            if row["control_generation"] != expected_generation:
                raise PersistenceError("CONTROL_CONFLICT")
            if action not in {"cancel", "pause", "resume", "follow_up", "correct"}:
                raise PersistenceError("INVALID_INPUT")
            # 连 incident_id 一起查：状态判定读的是这一行，而下面的状态推进按
            # incident_id 作用于本 incident 真正的 run。两者指向不同的行时，一个
            # 外来的 running run 会把 blocked run 的保护顶开——实测 incident 停在
            # paused 而它自己的 run 仍是 blocked。只按 run_id 查会让守卫读到不属于
            # 这个 incident 的状态，因此先判为不一致，不推进任何状态。
            run = (
                conn.execute(
                    "SELECT state AS run_state FROM opspilot_runs WHERE run_id=%s AND incident_id=%s FOR UPDATE",
                    (row["current_run_id"], incident_id),
                ).fetchone()
                if row["current_run_id"] is not None
                else None
            )
            if not run:
                raise PersistenceError("INCONSISTENT_STATE")
            if (
                row["state"] in {"cancelled", "completed"}
                or row["conclusion"] is not None
            ):
                raise PersistenceError("ILLEGAL_TRANSITION")
            # 暂停态只接受 resume 与 cancel，与 opspilot/domain/runs.py 的
            # RUN_EXECUTION（paused -> human_resume / human_cancel）一致。
            # 追问与纠正不得静默解除人工暂停。
            if row["state"] == "paused" and action in {"pause", "follow_up", "correct"}:
                raise PersistenceError("ILLEGAL_TRANSITION")
            # 按放行名单判定而不是点名 blocked：原写法只挡住当时想到的那一个
            # 状态，`failed`/`budget_exhausted` 这类终态一旦开始被写入就会静默
            # 变成「允许」。cancel 不受限，它是人工控制的兜底出口。
            if action != "cancel" and run["run_state"] not in _CONTROL_OPEN_RUN_STATES:
                raise PersistenceError("ILLEGAL_TRANSITION")
            nxt = expected_generation + 1
            state = (
                "cancelled"
                if action == "cancel"
                else ("paused" if action == "pause" else "running")
            )
            conn.execute(
                "UPDATE opspilot_incidents SET control_generation=%s,state=%s WHERE incident_id=%s",
                (nxt, state, incident_id),
            )
            if action == "cancel":
                conn.execute(
                    "UPDATE opspilot_runs SET state='cancelled',owner=NULL,lease_until=NULL,control_generation=%s WHERE incident_id=%s AND state IN ('queued','paused','running','waiting_human','blocked')",
                    (nxt, incident_id),
                )
            elif action == "pause":
                conn.execute(
                    "UPDATE opspilot_runs SET state='paused',owner=NULL,lease_until=NULL,control_generation=%s WHERE incident_id=%s AND state IN ('running','waiting_human')",
                    (nxt, incident_id),
                )
            elif action == "resume":
                conn.execute(
                    "UPDATE opspilot_runs SET state='queued',owner=NULL,lease_until=NULL,control_generation=%s WHERE incident_id=%s AND state IN ('queued','paused','running','waiting_human')",
                    (nxt, incident_id),
                )
            elif action in {"follow_up", "correct"}:
                conn.execute(
                    "UPDATE opspilot_runs SET state='queued',owner=NULL,lease_until=NULL,control_generation=%s WHERE incident_id=%s AND state IN ('queued','running','waiting_human')",
                    (nxt, incident_id),
                )
            conn.execute(
                "INSERT INTO opspilot_controls(audit_id,incident_id,action,expected_generation,resulting_generation,actor) VALUES(%s,%s,%s,%s,%s,%s)",
                (uuid4(), incident_id, action, expected_generation, nxt, actor),
            )
            return nxt

    def publish(
        self, lease: Lease, conclusion: dict[str, Any], *, step_id: UUID
    ) -> bool:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT i.current_run_id,i.conclusion,i.state AS incident_state,i.control_generation AS incident_generation,r.owner,r.epoch,r.lease_until,r.deadline,r.state AS run_state FROM opspilot_incidents i JOIN opspilot_runs r ON r.run_id=i.current_run_id WHERE i.incident_id=%s FOR UPDATE",
                (lease.incident_id,),
            ).fetchone()
            if (
                row
                and row["current_run_id"] == lease.run_id
                and row["conclusion"] == conclusion
            ):
                return True
            if (
                not row
                or row["current_run_id"] != lease.run_id
                or row["run_state"] != "running"
                # `paused` 在当前实现下是纵深防御而非承重判定：暂停必然递增
                # control_generation，且 claim() 已拒绝在暂停期间发放租约，
                # 因此 _lease_revoked() 的 generation 栅栏已经拦下同一批调用。
                # 变异测试确认去掉本项不会导致任何用例失败。保留它是为了在
                # 栅栏被削弱时仍然兜底。
                or row["incident_state"] in {"completed", "cancelled", "paused"}
                or self._lease_revoked(row, lease, self._db_now(conn))
            ):
                if (
                    conn.execute(
                        "SELECT 1 FROM opspilot_steps WHERE step_id=%s AND run_id=%s",
                        (step_id, lease.run_id),
                    ).fetchone()
                    is None
                ):
                    raise PersistenceError("UNKNOWN_IDENTITY")
                self._late_result(
                    conn,
                    lease.run_id,
                    f"{_LATE_RESULT_KEY_PREFIX}publish:{step_id}",
                    conclusion,
                    lease.control_generation,
                )
                return False
            final_step = conn.execute(
                "SELECT response,status,control_generation FROM opspilot_steps WHERE step_id=%s AND run_id=%s FOR UPDATE",
                (step_id, lease.run_id),
            ).fetchone()
            if (
                not final_step
                or final_step["control_generation"] != lease.control_generation
                or final_step["status"]
                not in {"response_committed", "tool_result_committed"}
                or final_step["response"] != conclusion
            ):
                raise PersistenceError("FINAL_STEP_REQUIRED")
            conn.execute(
                "UPDATE opspilot_incidents SET conclusion=%s WHERE incident_id=%s",
                (Jsonb(conclusion), lease.incident_id),
            )
            conn.execute(
                "UPDATE opspilot_runs SET state='completed',owner=NULL,lease_until=NULL WHERE run_id=%s",
                (lease.run_id,),
            )
            return True

    def rebuild(self, incident_id: UUID) -> dict[str, Any]:
        """重建断点。三条查询在同一个一致快照内，不会读出互相矛盾的行。"""
        with self.transaction(snapshot=True) as conn:
            row = conn.execute(
                "SELECT * FROM opspilot_incidents WHERE incident_id=%s", (incident_id,)
            ).fetchone()
            if not row:
                raise PersistenceError("UNKNOWN_IDENTITY")
            # 与写路径的栅栏同一条规则：人工决定的权威是事故代际，`run` 行上的
            # 同名列只是 claim()/control() 盖下的副本。这里绑成具名变量，是为了
            # 让「读的是哪一份」在读点上就可见，而不是靠 `row` 指向谁来推断。
            incident_generation = row["control_generation"]
            run = conn.execute(
                "SELECT * FROM opspilot_runs WHERE run_id=%s", (row["current_run_id"],)
            ).fetchone()
            if run is None or run["incident_id"] != incident_id:
                raise PersistenceError("INCONSISTENT_STATE")
            steps = conn.execute(
                "SELECT * FROM opspilot_steps WHERE run_id=%s ORDER BY sequence, step_id",
                (row["current_run_id"],),
            ).fetchall()
            for step in steps:
                # Late tool/step results are immutable history payloads, not
                # model responses. They may have any JSON shape and must not
                # make a later rebuild fail model-plan validation.
                if step["status"] == "late_result":
                    continue
                calls = _tool_plan(step["response"])
                if not isinstance(calls, list) or any(
                    not isinstance(call, dict) for call in calls
                ):
                    raise PersistenceError("INCONSISTENT_STATE")
                _completed_tool_ordinals(step["tool_results"], len(calls))
            pending_tools: list[dict[str, Any]] = []
            for step in steps:
                if step["control_generation"] != incident_generation or step[
                    "status"
                ] not in {"response_committed", "tool_result_committed"}:
                    continue
                calls = _tool_plan(step["response"])
                completed = _completed_tool_ordinals(step["tool_results"], len(calls))
                for ordinal, call in enumerate(calls):
                    if ordinal not in completed:
                        # No operation id here: its canonical format lives in
                        # opspilot.domain.tool_operation_id, and whether this
                        # module may depend on the domain layer is still an
                        # open decision (tests/test_architecture.py records it
                        # as a strict xfail). Reaching for the helper here
                        # would settle that decision in passing, so
                        # recovery.rebuild_plan stamps it instead -- one
                        # definition of the format executors and the evidence
                        # store deduplicate by, with the decision left open.
                        pending_tools.append(
                            {
                                "step_id": step["step_id"],
                                "ordinal": ordinal,
                                "tool_call": call,
                            }
                        )
            return {
                "incident_id": row["incident_id"],
                "state": row["state"],
                "control_generation": incident_generation,
                "run": run,
                "steps": steps,
                # 只列出当前代际、且仍是活步骤的待办工具调用。late_result 即使
                # 代际未变（只是租约过期）也只是历史；列进 pending_tools 会让新
                # 租约把迟到响应写回成可发布步骤。
                "pending_tools": pending_tools,
                "conclusion": row["conclusion"],
            }

    def recovery_metadata(self, incident_id: UUID) -> dict[str, Any]:
        """Read identity/version fields without decoding versioned step payloads."""
        with self.transaction(snapshot=True) as conn:
            row = conn.execute(
                "SELECT i.state AS incident_state,i.control_generation,r.run_id,r.state AS run_state,r.versions FROM opspilot_incidents i JOIN opspilot_runs r ON r.run_id=i.current_run_id WHERE i.incident_id=%s",
                (incident_id,),
            ).fetchone()
            if not row:
                raise PersistenceError("UNKNOWN_IDENTITY")
            return row
