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
    global_suspension_generation: int = 0
    target_suspension_generation: int = 0


# 预留结算的两种去向：列名由结算结果决定，不由调用方拼 SQL。
_SETTLEMENTS: dict[str, str] = {"spent": "budget_spent", "unknown": "budget_unknown"}


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
        with self.transaction() as conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS opspilot_incidents (
              incident_id uuid PRIMARY KEY, intake_key text UNIQUE NOT NULL,
              state text NOT NULL, lifecycle text NOT NULL DEFAULT 'open', control_generation integer NOT NULL DEFAULT 0,
              current_run_id uuid, conclusion jsonb, created_at timestamptz NOT NULL DEFAULT clock_timestamp()
            );
            ALTER TABLE opspilot_incidents ADD COLUMN IF NOT EXISTS lifecycle text NOT NULL DEFAULT 'open';
            CREATE TABLE IF NOT EXISTS opspilot_targets (
              target_id uuid PRIMARY KEY, resource_uid text UNIQUE NOT NULL,
              created_at timestamptz NOT NULL DEFAULT clock_timestamp()
            );
            ALTER TABLE opspilot_incidents ADD COLUMN IF NOT EXISTS target_id uuid REFERENCES opspilot_targets;
            CREATE INDEX IF NOT EXISTS opspilot_incidents_target_id_idx ON opspilot_incidents(target_id);
            CREATE TABLE IF NOT EXISTS opspilot_scope_controls (
              scope_id smallint PRIMARY KEY CHECK (scope_id = 1),
              global_suspended boolean NOT NULL DEFAULT false,
              global_generation integer NOT NULL DEFAULT 0
            );
            INSERT INTO opspilot_scope_controls(scope_id) VALUES(1) ON CONFLICT DO NOTHING;
            CREATE TABLE IF NOT EXISTS opspilot_target_suspensions (
              target_id uuid PRIMARY KEY REFERENCES opspilot_targets,
              suspended boolean NOT NULL DEFAULT false,
              generation integer NOT NULL DEFAULT 0,
              updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
            );
            INSERT INTO opspilot_target_suspensions(target_id) SELECT target_id FROM opspilot_targets ON CONFLICT DO NOTHING;
            CREATE TABLE IF NOT EXISTS opspilot_suspension_audit (
              audit_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, target_id uuid,
              suspended boolean NOT NULL, generation integer NOT NULL, actor text NOT NULL,
              created_at timestamptz NOT NULL DEFAULT clock_timestamp()
            );
            CREATE TABLE IF NOT EXISTS opspilot_runs (
              run_id uuid PRIMARY KEY, incident_id uuid NOT NULL REFERENCES opspilot_incidents,
              state text NOT NULL, epoch integer NOT NULL DEFAULT 0, owner uuid,
              lease_until timestamptz, control_generation integer NOT NULL,
              budget_limit bigint NOT NULL, budget_reserved bigint NOT NULL DEFAULT 0,
              budget_spent bigint NOT NULL DEFAULT 0, budget_unknown bigint NOT NULL DEFAULT 0,
              deadline timestamptz NOT NULL, versions jsonb NOT NULL, input_watermark integer NOT NULL DEFAULT 0
            );
            ALTER TABLE opspilot_runs ADD COLUMN IF NOT EXISTS input_watermark integer NOT NULL DEFAULT 0;
            ALTER TABLE opspilot_suspension_audit ADD COLUMN IF NOT EXISTS actor text NOT NULL DEFAULT 'unknown';
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
            ALTER TABLE opspilot_controls ADD COLUMN IF NOT EXISTS payload jsonb;
            CREATE TABLE IF NOT EXISTS opspilot_inputs (
              input_id uuid PRIMARY KEY, incident_id uuid NOT NULL REFERENCES opspilot_incidents,
              sequence integer NOT NULL, kind text NOT NULL, content jsonb NOT NULL,
              actor text, control_generation integer NOT NULL,
              received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
              UNIQUE(incident_id, sequence)
            );
            CREATE INDEX IF NOT EXISTS opspilot_inputs_incident_id_idx ON opspilot_inputs(incident_id, sequence);
            CREATE TABLE IF NOT EXISTS opspilot_input_rounds (
              run_id uuid NOT NULL REFERENCES opspilot_runs, logical_key text NOT NULL,
              control_generation integer NOT NULL, input_watermark integer NOT NULL,
              committed boolean NOT NULL DEFAULT false,
              PRIMARY KEY(run_id,logical_key)
            );
            CREATE TABLE IF NOT EXISTS opspilot_budget_reservations (
              reservation_id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES opspilot_runs,
              amount bigint NOT NULL, state text NOT NULL DEFAULT 'reserved', UNIQUE(run_id, reservation_id)
            );
            -- 工具次数/秒数是每 Run 的冻结上限（C3 第 13 节：重启不能重置预算）。
            -- 累计值落在 run 行；**每一次真实派发**记一行，主键是 dispatch_id，
            -- 同一次派发重复结算只更新秒数，不重复计次。键不是 operation_id：
            -- 后者由步骤 ID 和工具序号稳定生成（C3 第 7 节），而 C3 第 13 节要求
            -- 「重试计入次数和费用」、第 4 节明确不承诺外部查询 exactly-once，
            -- 所以同一 operation 的第二次真实读取必须再计一次，不能被去重掉。
            ALTER TABLE opspilot_runs ADD COLUMN IF NOT EXISTS tool_operations_used integer NOT NULL DEFAULT 0;
            ALTER TABLE opspilot_runs ADD COLUMN IF NOT EXISTS tool_seconds_used double precision NOT NULL DEFAULT 0;
            CREATE TABLE IF NOT EXISTS opspilot_tool_charges (
              dispatch_id uuid PRIMARY KEY,
              run_id uuid NOT NULL REFERENCES opspilot_runs, epoch integer NOT NULL,
              operation_id text NOT NULL,
              -- 预留的授权秒数（C3 第 13 节：预算原子预留和结算）。seconds 为 NULL
              -- 表示尚未结算，此时该次派发按 reserved 占用预算——「未知费用保持占用」。
              reserved double precision NOT NULL DEFAULT 0,
              seconds double precision
            );
            CREATE INDEX IF NOT EXISTS opspilot_tool_charges_run_epoch_idx ON opspilot_tool_charges(run_id, epoch);
            -- 输入快照（C3 第 5 节：重建输入所需的实际内容或固定持久引用）。新 worker
            -- 只有这一列和步骤行可读，没有它就无法重建 system/user 消息与工具面。
            ALTER TABLE opspilot_runs ADD COLUMN IF NOT EXISTS input jsonb;
            -- 模型请求的活跃时间与次数同一套预留/结算（C3 第 13 节「租约状态不明时
            -- 保守计量」）：预留时按超时上界占用 reserved_seconds，结算时写实测 seconds；
            -- 请求中崩溃的预留永远没有 seconds，读取时按上界计入。
            ALTER TABLE opspilot_budget_reservations ADD COLUMN IF NOT EXISTS reserved_seconds double precision NOT NULL DEFAULT 0;
            ALTER TABLE opspilot_budget_reservations ADD COLUMN IF NOT EXISTS seconds double precision;
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
        input: dict[str, Any] | None = None,
        target_id: UUID | None = None,
    ) -> None:
        """Create incident/run atomically. Return only after commit.

        ``input`` is the Run's input snapshot (question, authorization facts,
        evidence context, tool face, limits). It is what a later worker
        rebuilds the model context from; ``None`` keeps callers that never
        run the investigation loop (M0 harness, control tests) unchanged.
        """
        with self.transaction() as conn:
            scope = self._lock_scope(conn, None)
            if (
                target_id is not None
                and not conn.execute(
                    "SELECT 1 FROM opspilot_targets WHERE target_id=%s", (target_id,)
                ).fetchone()
            ):
                raise PersistenceError("UNKNOWN_TARGET")
            if target_id is not None:
                target_scope = self._require_row(
                    conn.execute(
                        "SELECT suspended FROM opspilot_target_suspensions WHERE target_id=%s FOR SHARE",
                        (target_id,),
                    )
                )
                scope["target_suspended"] = target_scope["suspended"]
            row = conn.execute(
                "SELECT incident_id,current_run_id,target_id FROM opspilot_incidents WHERE intake_key=%s",
                (intake_key,),
            ).fetchone()
            if row:
                if (
                    row["incident_id"] != incident_id
                    or row["current_run_id"] != run_id
                    or row["target_id"] != target_id
                ):
                    raise PersistenceError("IDENTITY_CONFLICT")
                return
            inserted = conn.execute(
                # 不限定冲突目标：同一身份并发重投会同时撞上 intake_key 唯一索引
                # 和 incident_id 主键，只声明前者会让后者漏成 UniqueViolation。
                "INSERT INTO opspilot_incidents(incident_id,intake_key,state,lifecycle,current_run_id,target_id) VALUES(%s,%s,'queued','open',%s,%s) ON CONFLICT DO NOTHING RETURNING incident_id",
                (incident_id, intake_key, run_id, target_id),
            ).fetchone()
            existing = conn.execute(
                "SELECT incident_id,current_run_id,target_id FROM opspilot_incidents WHERE intake_key=%s",
                (intake_key,),
            ).fetchone()
            if existing is None:
                # 插入被主键冲突吞掉：这个 incident_id 已经绑定到别的 intake_key。
                # 存储本身一致，是调用方给了冲突的身份。
                raise PersistenceError("IDENTITY_CONFLICT")
            if (
                existing["incident_id"] != incident_id
                or existing["current_run_id"] != run_id
                or existing["target_id"] != target_id
            ):
                raise PersistenceError("IDENTITY_CONFLICT")
            if inserted is None:
                return
            conn.execute(
                "INSERT INTO opspilot_runs(run_id,incident_id,state,control_generation,budget_limit,deadline,versions,input) VALUES(%s,%s,'queued',0,%s,%s,%s,%s)",
                (
                    run_id,
                    incident_id,
                    budget_limit,
                    deadline,
                    Jsonb(versions),
                    None if input is None else Jsonb(input),
                ),
            )
            if scope["global_suspended"] or scope["target_suspended"]:
                conn.execute(
                    "UPDATE opspilot_incidents SET state='paused' WHERE incident_id=%s",
                    (incident_id,),
                )
                conn.execute(
                    "UPDATE opspilot_runs SET state='paused' WHERE run_id=%s", (run_id,)
                )

    def register_target(
        self, resource_uid: str, *, target_id: UUID | None = None
    ) -> UUID:
        """Register an immutable target identity before it can be suspended."""
        if not isinstance(resource_uid, str) or not resource_uid:
            raise PersistenceError("INVALID_INPUT")
        identity = target_id or uuid4()
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT target_id,resource_uid FROM opspilot_targets WHERE resource_uid=%s OR target_id=%s",
                (resource_uid, identity),
            ).fetchone()
            if existing:
                if existing["resource_uid"] != resource_uid or (
                    target_id is not None and existing["target_id"] != identity
                ):
                    raise PersistenceError("IDENTITY_CONFLICT")
                return cast(UUID, existing["target_id"])
            conn.execute(
                "INSERT INTO opspilot_targets(target_id,resource_uid) VALUES(%s,%s)",
                (identity, resource_uid),
            )
            conn.execute(
                "INSERT INTO opspilot_target_suspensions(target_id) VALUES(%s)",
                (identity,),
            )
        return identity

    def set_global_suspension(
        self, suspended: bool, *, expected_generation: int, actor: str
    ) -> int:
        """Atomically change the global gate; suspension invalidates active leases."""
        if type(suspended) is not bool:
            raise PersistenceError("INVALID_INPUT")
        with self.transaction() as conn:
            row = self._require_row(
                conn.execute(
                    "SELECT global_suspended,global_generation FROM opspilot_scope_controls WHERE scope_id=1 FOR UPDATE"
                )
            )
            current = int(row["global_generation"])
            if (
                type(expected_generation) is not int
                or expected_generation < 0
                or expected_generation != current
            ):
                raise PersistenceError("CONTROL_CONFLICT")
            if suspended == row["global_suspended"]:
                # Same-value write (e.g. a retried release after a lost ack,
                # resubmitted against a refreshed generation): still record
                # the decision, but do not bump the fence generation. Every
                # active lease's captured global_suspension_generation is
                # compared for equality in _lease_revoked(), so an
                # unconditional bump here would revoke leases that were
                # never actually affected by any suspension state change.
                conn.execute(
                    "INSERT INTO opspilot_suspension_audit(target_id,suspended,generation,actor) VALUES(NULL,%s,%s,%s)",
                    (suspended, current, actor),
                )
                return current
            nxt = current + 1
            conn.execute(
                "UPDATE opspilot_scope_controls SET global_suspended=%s,global_generation=%s WHERE scope_id=1",
                (suspended, nxt),
            )
            if suspended:
                conn.execute(
                    "SELECT incident_id FROM opspilot_incidents ORDER BY incident_id FOR UPDATE"
                )
                conn.execute(
                    "UPDATE opspilot_incidents i SET state='paused' WHERE conclusion IS NULL AND state NOT IN ('completed','cancelled') AND EXISTS (SELECT 1 FROM opspilot_runs r WHERE r.run_id=i.current_run_id AND r.state IN ('queued','running','waiting_human','paused'))"
                )
                conn.execute(
                    "UPDATE opspilot_runs SET owner=NULL,lease_until=NULL,state='paused' WHERE state IN ('queued','running','waiting_human')"
                )
            conn.execute(
                "INSERT INTO opspilot_suspension_audit(target_id,suspended,generation,actor) VALUES(NULL,%s,%s,%s)",
                (suspended, nxt, actor),
            )
            return nxt

    # Short names are useful to callers that model the domain operation directly.
    suspend_global = set_global_suspension

    def set_target_suspension(
        self,
        target_id: UUID,
        suspended: bool,
        *,
        expected_generation: int,
        actor: str,
    ) -> int:
        """Change one registered immutable target's gate and invalidate its leases."""
        if not isinstance(target_id, UUID) or type(suspended) is not bool:
            raise PersistenceError("INVALID_INPUT")
        with self.transaction() as conn:
            self._lock_scope(conn)
            if not conn.execute(
                "SELECT 1 FROM opspilot_targets WHERE target_id=%s", (target_id,)
            ).fetchone():
                raise PersistenceError("UNKNOWN_TARGET")
            conn.execute(
                "INSERT INTO opspilot_target_suspensions(target_id,suspended,generation) VALUES(%s,false,0) ON CONFLICT DO NOTHING",
                (target_id,),
            )
            row = self._require_row(
                conn.execute(
                    "SELECT suspended,generation FROM opspilot_target_suspensions WHERE target_id=%s FOR UPDATE",
                    (target_id,),
                )
            )
            current = int(row["generation"])
            if (
                type(expected_generation) is not int
                or expected_generation < 0
                or expected_generation != current
            ):
                raise PersistenceError("CONTROL_CONFLICT")
            if suspended == row["suspended"]:
                # Same-value write: see set_global_suspension for why this
                # must not bump the fence generation (it would revoke every
                # lease on this target's incidents for no actual state
                # change).
                conn.execute(
                    "INSERT INTO opspilot_suspension_audit(target_id,suspended,generation,actor) VALUES(%s,%s,%s,%s)",
                    (target_id, suspended, current, actor),
                )
                return current
            nxt = current + 1
            conn.execute(
                "UPDATE opspilot_target_suspensions SET suspended=%s,generation=%s,updated_at=clock_timestamp() WHERE target_id=%s",
                (suspended, nxt, target_id),
            )
            if suspended:
                conn.execute(
                    "SELECT incident_id FROM opspilot_incidents WHERE target_id=%s ORDER BY incident_id FOR UPDATE",
                    (target_id,),
                )
                conn.execute(
                    "UPDATE opspilot_incidents i SET state='paused' WHERE target_id=%s AND conclusion IS NULL AND state NOT IN ('completed','cancelled') AND EXISTS (SELECT 1 FROM opspilot_runs r WHERE r.run_id=i.current_run_id AND r.state IN ('queued','running','waiting_human','paused'))",
                    (target_id,),
                )
                conn.execute(
                    "UPDATE opspilot_runs SET owner=NULL,lease_until=NULL,state='paused' WHERE state IN ('queued','running','waiting_human') AND incident_id IN (SELECT incident_id FROM opspilot_incidents WHERE target_id=%s)",
                    (target_id,),
                )
            conn.execute(
                "INSERT INTO opspilot_suspension_audit(target_id,suspended,generation,actor) VALUES(%s,%s,%s,%s)",
                (target_id, suspended, nxt, actor),
            )
            return nxt

    suspend_target = set_target_suspension

    def _lock_scope(
        self, conn: Connection, incident_id: UUID | None = None
    ) -> dict[str, Any]:
        scope = self._require_row(
            conn.execute(
                "SELECT global_suspended,global_generation FROM opspilot_scope_controls WHERE scope_id=1 FOR SHARE"
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
                    "SELECT suspended,generation FROM opspilot_target_suspensions WHERE target_id=%s FOR SHARE",
                    (target["target_id"],),
                ).fetchone()
                if row is None:
                    raise PersistenceError("INCONSISTENT_STATE")
                scope.update(
                    target_suspended=row["suspended"],
                    target_generation=row["generation"],
                )
        return scope

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
        input: dict[str, Any] | None = None,
    ) -> int:
        """Continue a cancelled incident with a fresh Run and control generation."""
        if type(expected_generation) is not int or expected_generation < 0:
            raise PersistenceError("INVALID_INPUT")
        with self.transaction() as conn:
            scope = self._lock_scope(conn, incident_id)
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
                    and row["state"] in {"queued", "paused"}
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
            next_state = (
                "paused"
                if scope["global_suspended"] or scope["target_suspended"]
                else "queued"
            )
            conn.execute(
                "INSERT INTO opspilot_runs(run_id,incident_id,state,control_generation,budget_limit,deadline,versions,input) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    run_id,
                    incident_id,
                    next_state,
                    nxt,
                    budget_limit,
                    deadline,
                    Jsonb(versions),
                    None if input is None else Jsonb(input),
                ),
            )
            conn.execute(
                "UPDATE opspilot_incidents SET state=%s,lifecycle='open',control_generation=%s,current_run_id=%s,conclusion=NULL WHERE incident_id=%s",
                (next_state, nxt, run_id, incident_id),
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
        """登记迟到结果。logical_key 必须落在保留前缀下，重放不得另写一行。

        键里带上租约 epoch：同一逻辑轮次可能被多个先后被围栏的尝试各自
        回复一次，每个物理回复（不同 response id / usage / 内容）都是应保留
        的历史；只有同一尝试对同一身份的重放才会撞键而被 DO NOTHING 吞掉
        （机器人审查发现，PR #29）。
        """
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
            self._lock_scope(conn, incident_id)
            row = conn.execute(
                "SELECT i.control_generation AS incident_generation,i.state AS incident_state,r.state AS run_state,r.epoch,r.lease_until,r.deadline,r.versions,sc.global_suspended,sc.global_generation,COALESCE(ts.suspended,false) AS target_suspended,COALESCE(ts.generation,0) AS target_generation FROM opspilot_incidents i JOIN opspilot_runs r ON r.incident_id=i.incident_id JOIN opspilot_scope_controls sc ON sc.scope_id=1 LEFT JOIN opspilot_target_suspensions ts ON ts.target_id=i.target_id WHERE i.incident_id=%s AND r.run_id=%s FOR UPDATE OF i,r",
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
            if row["global_suspended"] or row["target_suspended"]:
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
                    incident_id,
                    run_id,
                    owner,
                    epoch,
                    int(row["incident_generation"]),
                    int(row["global_generation"]),
                    int(row["target_generation"]),
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

    def reserve_budget(
        self,
        lease: Lease,
        reservation_id: UUID,
        amount: int,
        *,
        seconds: float = 0.0,
    ) -> None:
        """Reserve ``amount`` request slots and ``seconds`` of active time.

        ``seconds`` is the upper bound the request may take (its timeout).
        Until settled it counts in full against the Run's active time, so an
        attempt killed mid-request never under-reports what it may have used.
        """
        if amount <= 0:
            raise PersistenceError("INVALID_INPUT")
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not seconds >= 0
            or seconds == float("inf")
        ):
            raise PersistenceError("INVALID_INPUT")
        with self.transaction() as conn:
            self._lock_scope(conn, lease.incident_id)
            # 先锁 incident 再锁 run：全模块统一这个顺序，避免与 control()/
            # publish() 交叉形成 ABBA 死锁（control 只拿到 incident_id，
            # 结构上必须先读 incident，因此以它为规范顺序）。
            conn.execute(
                "SELECT 1 FROM opspilot_incidents WHERE incident_id=%s FOR UPDATE",
                (lease.incident_id,),
            )
            row = conn.execute(
                "SELECT r.owner,r.epoch,r.lease_until,r.deadline,r.budget_limit,r.budget_reserved,r.budget_spent,r.budget_unknown,i.control_generation AS incident_generation,sc.global_suspended,sc.global_generation,COALESCE(ts.suspended,false) AS target_suspended,COALESCE(ts.generation,0) AS target_generation FROM opspilot_runs r JOIN opspilot_incidents i ON i.incident_id=r.incident_id JOIN opspilot_scope_controls sc ON sc.scope_id=1 LEFT JOIN opspilot_target_suspensions ts ON ts.target_id=i.target_id WHERE r.run_id=%s FOR UPDATE OF i,r",
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
                "INSERT INTO opspilot_budget_reservations(reservation_id,run_id,amount,reserved_seconds) VALUES(%s,%s,%s,%s)",
                (reservation_id, lease.run_id, amount, float(seconds)),
            )
            conn.execute(
                "UPDATE opspilot_runs SET budget_reserved=budget_reserved+%s WHERE run_id=%s",
                (amount, lease.run_id),
            )

    def settle_budget(
        self,
        lease: Lease,
        reservation_id: UUID,
        outcome: str,
        *,
        seconds: float | None = None,
    ) -> None:
        """Settle one reservation after the physical request finished (C3 §13).

        ``seconds`` is the measured active time of the request. ``None``
        keeps the reserved upper bound (the right value for ``unknown``).

        ``spent``: the provider answered, the request is real usage.
        ``unknown``: the outcome is not known (timeout, transport failure,
        rejected request); the reserved amount stays occupied under
        ``budget_unknown`` and is never released. Settling the same
        reservation twice with the same outcome is a no-op; a different
        outcome is an identity conflict. Fenced by the lease like every
        other write path, so a revoked attempt leaves its reservation as
        ``reserved`` -- still counted against the limit.
        """
        if outcome not in _SETTLEMENTS:
            raise PersistenceError("INVALID_INPUT")
        if seconds is not None and (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not seconds >= 0
            or seconds == float("inf")
        ):
            raise PersistenceError("INVALID_INPUT")
        with self.transaction() as conn:
            # 先锁 incident 再锁 run，与其余写路径同一顺序。
            conn.execute(
                "SELECT 1 FROM opspilot_incidents WHERE incident_id=%s FOR UPDATE",
                (lease.incident_id,),
            )
            row = conn.execute(
                "SELECT r.owner,r.epoch,r.lease_until,r.deadline,i.control_generation FROM opspilot_runs r JOIN opspilot_incidents i ON i.incident_id=r.incident_id WHERE r.run_id=%s FOR UPDATE",
                (lease.run_id,),
            ).fetchone()
            if (
                not row
                or row["owner"] != lease.owner
                or row["epoch"] != lease.epoch
                or row["control_generation"] != lease.control_generation
                or row["lease_until"] is None
                or row["lease_until"] <= self._db_now(conn)
                or row["deadline"] <= self._db_now(conn)
            ):
                raise PersistenceError("CONTROL_DENIED")
            reservation = conn.execute(
                "SELECT amount,run_id,state FROM opspilot_budget_reservations WHERE reservation_id=%s FOR UPDATE",
                (reservation_id,),
            ).fetchone()
            if not reservation or reservation["run_id"] != lease.run_id:
                raise PersistenceError("UNKNOWN_IDENTITY")
            if reservation["state"] != "reserved":
                if reservation["state"] == outcome:
                    return
                raise PersistenceError("IDENTITY_CONFLICT")
            conn.execute(
                "UPDATE opspilot_budget_reservations SET state=%s,seconds=COALESCE(%s,reserved_seconds) WHERE reservation_id=%s",
                (outcome, None if seconds is None else float(seconds), reservation_id),
            )
            column = _SETTLEMENTS[outcome]
            conn.execute(
                f"UPDATE opspilot_runs SET budget_reserved=budget_reserved-%s,{column}={column}+%s WHERE run_id=%s",
                (reservation["amount"], reservation["amount"], lease.run_id),
            )

    def charge_tool(
        self,
        lease: Lease,
        operation_id: str,
        seconds: float,
        *,
        max_operations: int,
        max_tool_seconds: float,
        dispatch_id: UUID,
    ) -> None:
        """Charge one tool dispatch to the Run's durable tool budget.

        The first charge for ``dispatch_id`` counts one operation; later
        charges with the same ``dispatch_id`` only raise its recorded seconds,
        so an executor may charge once before its read goes out and once after
        it with the measured wall time.

        The first charge for a ``dispatch_id`` is a **reservation**: its
        ``seconds`` is the longest this dispatch is authorized to take, and the
        whole of it is held against the Run's time ceiling until the dispatch
        settles (C3 section 13: 预算在 PostgreSQL 原子预留和结算，未知费用保持
        占用). Both ceilings gate that one atomic UPDATE -- a new dispatch is
        refused unless the Run still has an operation left *and* room for the
        full reservation. Settling is never refused: it releases the
        reservation and records what the read actually cost, which may be less
        than reserved and, for a transport that overran its bound, more. The
        ceiling governs what may be started; the record tells the truth about
        what was spent.

        ``dispatch_id`` is the key, not ``operation_id``, and it is required
        rather than derived here: ``operation_id`` is stable by construction
        (technical plan section 7 -- step id plus tool ordinal), so keying on
        it silently collapsed *two real reads* of the same tool call into one
        charge whenever a duplicate delivery or a same-epoch retry occurred --
        the Run was charged one operation and ``max(seconds)`` instead of the
        sum, and could exceed both frozen ceilings with real queries (bot
        review finding). Section 13 settles the semantics -- "重试计入次数和
        费用" -- and section 4 explicitly declines to promise exactly-once
        external queries, so the second dispatch is charged, not refused. A
        new attempt that re-dispatches an uncommitted operation likewise
        issues a real second query and is charged again; the per-Run total
        only ever grows. Fenced by the lease like every other write path.

        ``max_operations`` is required, not defaulted: this module does not
        import the frozen ceiling from ``opspilot.tools.executor`` (that
        would invert the existing one-way dependency, ``opspilot.tools``
        already imports from here), so every caller must supply the same
        value the executor is enforcing. Counting a *new* operation is
        refused, atomically under the same row lock as everything else in
        this transaction, once the Run is already at the cap -- two
        executors racing from the same stale ``operations_used`` snapshot
        serialize on this lock, and only the one that arrives first may
        still increment (bot review finding: the increment used to be
        unconditional once past application-level checks, so both could
        succeed and the durable count could exceed the frozen cap).
        Settling an *already-counted* dispatch's seconds is never subject
        to this check -- it does not add a new operation.
        """
        if not isinstance(operation_id, str) or not operation_id:
            raise PersistenceError("INVALID_INPUT")
        if (
            type(seconds) not in (int, float)
            or seconds != seconds
            or seconds in (float("inf"), float("-inf"))
            or seconds < 0
        ):
            raise PersistenceError("INVALID_INPUT")
        if type(max_operations) is not int or max_operations <= 0:
            raise PersistenceError("INVALID_INPUT")
        if (
            type(max_tool_seconds) not in (int, float)
            or max_tool_seconds != max_tool_seconds
            or max_tool_seconds in (float("inf"), float("-inf"))
            or max_tool_seconds <= 0
        ):
            raise PersistenceError("INVALID_INPUT")
        if not isinstance(dispatch_id, UUID):
            raise PersistenceError("INVALID_INPUT")
        with self.transaction() as conn:
            self._lock_scope(conn, lease.incident_id)
            # 先锁 incident 再锁 run，与其余写路径同一顺序。
            conn.execute(
                "SELECT 1 FROM opspilot_incidents WHERE incident_id=%s FOR UPDATE",
                (lease.incident_id,),
            )
            row = conn.execute(
                "SELECT r.owner,r.epoch,r.lease_until,r.deadline,r.tool_operations_used,r.tool_seconds_used,i.control_generation AS incident_generation,sc.global_suspended,sc.global_generation,COALESCE(ts.suspended,false) AS target_suspended,COALESCE(ts.generation,0) AS target_generation FROM opspilot_runs r JOIN opspilot_incidents i ON i.incident_id=r.incident_id JOIN opspilot_scope_controls sc ON sc.scope_id=1 LEFT JOIN opspilot_target_suspensions ts ON ts.target_id=i.target_id WHERE r.run_id=%s AND i.incident_id=%s FOR UPDATE OF i,r",
                (lease.run_id, lease.incident_id),
            ).fetchone()
            # 事故与 Run 两个身份都要绑定：只按 run_id 查会让「事故 A + 事故 B 的 Run」
            # 这种 Lease 锁住 A 却改 B 的业务记录，同时绕开上面刚建立的 incident→run
            # 锁序（bot review 发现）。renew_lease()/lease_current() 本就是这个写法。
            if not row:
                raise PersistenceError("CONTROL_DENIED")
            existing = conn.execute(
                "SELECT reserved,seconds FROM opspilot_tool_charges WHERE dispatch_id=%s AND run_id=%s AND epoch=%s AND operation_id=%s FOR UPDATE",
                (dispatch_id, lease.run_id, lease.epoch, operation_id),
            ).fetchone()
            # 与其余写路径共用 `_lease_revoked`：租约栅栏在本文件只有一份实现。
            # 唯一的例外是**结算一个本租约已经预留过的派发**：栅栏若连它也拒，
            # 预留的整段超时就永远占着 tool_seconds_used，恢复后的尝试继承这个
            # 虚高总量，可能因为根本没花掉的秒数而 TIME_BUDGET_EXHAUSTED
            # （bot review 发现）。结算只是把已知的实际耗时写回本租约自己建立的
            # 那一行：它不新建预留、不采纳任何结果，人工决定仍由执行器取回后的
            # control 复读上报并按 history-only 登记。
            if existing is None and self._lease_revoked(row, lease, self._db_now(conn)):
                raise PersistenceError("CONTROL_DENIED")
            if existing is None:
                # 首次出现该 dispatch_id 即预留：`seconds` 是本次派发被授权的最长
                # 时间，整段先占住预算，结算时再核减为实际耗时。此前这里记的是
                # 调用方传来的 0.0，于是两个执行器都能通过「已用 < 上限」这道门、
                # 各自派发，再各自无条件结算，把 Run 推过冻结上限（bot review 发现：
                # 上一轮只判已用量，并没有真的关掉这个竞态）。
                conn.execute(
                    "INSERT INTO opspilot_tool_charges(dispatch_id,run_id,epoch,operation_id,reserved,seconds) VALUES(%s,%s,%s,%s,%s,NULL)",
                    (
                        dispatch_id,
                        lease.run_id,
                        lease.epoch,
                        operation_id,
                        float(seconds),
                    ),
                )
                cursor = conn.execute(
                    "UPDATE opspilot_runs SET tool_operations_used=tool_operations_used+1,tool_seconds_used=tool_seconds_used+%s WHERE run_id=%s AND tool_operations_used<%s AND tool_seconds_used+%s<=%s",
                    (
                        float(seconds),
                        lease.run_id,
                        max_operations,
                        float(seconds),
                        float(max_tool_seconds),
                    ),
                )
                if cursor.rowcount == 0:
                    # The row is already locked (``FOR UPDATE`` above), so this
                    # is not a lost-update race with another writer -- a cap
                    # was already reached when we got here. Raising rolls back
                    # the INSERT above too, so no orphaned charge row survives
                    # for an operation that was never actually counted.
                    #
                    # 两个上限都在这一条 UPDATE 里判定。秒数此前只在进程内把关：
                    # 两个执行器各自读到 239 秒，就会各自按「还剩 1 秒」派发并成功结算，
                    # 把 Run 留在 241 秒，而两次观察都被采纳（bot review 发现）。
                    # 行锁下读到的计数用来区分是哪一个上限触发，报告与进程内同一个
                    # 原因码。
                    raise PersistenceError(
                        "OPERATION_BUDGET_EXHAUSTED"
                        if int(row["tool_operations_used"]) >= max_operations
                        else "TIME_BUDGET_EXHAUSTED"
                    )
                return
            if existing["seconds"] is None:
                # 第一次结算：释放预留，按实际耗时记账。差值可以为负——预留本就是
                # 上界，这正是 C3「原子预留和结算」里的结算那一半。
                settled = float(seconds)
                delta = settled - float(existing["reserved"])
            else:
                # 重复结算只允许向上修正，不允许把已记录的实际耗时改小。
                settled = max(float(existing["seconds"]), float(seconds))
                delta = settled - float(existing["seconds"])
            conn.execute(
                "UPDATE opspilot_tool_charges SET seconds=%s WHERE dispatch_id=%s",
                (settled, dispatch_id),
            )
            if delta == 0.0:
                return
            conn.execute(
                "UPDATE opspilot_runs SET tool_seconds_used=tool_seconds_used+%s WHERE run_id=%s",
                (delta, lease.run_id),
            )

    def run_usage(self, run_id: UUID) -> dict[str, Any]:
        """Durable per-Run usage a new attempt continues from (C3 §13).

        Model request slots are the three budget columns; model active seconds
        sum every reservation, settled ones at their measured value and
        unsettled ones at their reserved upper bound. Tool usage comes from the
        tool ledger columns. Read in one snapshot; never fenced, never written.
        """
        with self.transaction(snapshot=True) as conn:
            row = conn.execute(
                "SELECT r.budget_limit,r.budget_reserved,r.budget_spent,r.budget_unknown,r.tool_operations_used,r.tool_seconds_used,"
                "(SELECT COALESCE(SUM(COALESCE(b.seconds,b.reserved_seconds)),0) FROM opspilot_budget_reservations b WHERE b.run_id=r.run_id) AS model_seconds_used "
                "FROM opspilot_runs r WHERE r.run_id=%s",
                (run_id,),
            ).fetchone()
            if not row:
                raise PersistenceError("UNKNOWN_IDENTITY")
            return {
                "budget_limit": int(row["budget_limit"]),
                "model_requests_used": int(
                    row["budget_reserved"] + row["budget_spent"] + row["budget_unknown"]
                ),
                "model_seconds_used": float(row["model_seconds_used"]),
                "tool_operations_used": int(row["tool_operations_used"]),
                "tool_seconds_used": float(row["tool_seconds_used"]),
            }

    def block(self, lease: Lease) -> None:
        """Move this attempt's Run to ``blocked`` (C3 §7 incompatible/malformed state).

        Fenced like every other write: a lease that human control already
        superseded cannot block the Run it no longer holds. ``claim()`` never
        silently resumes a blocked Run; a human or an explicit migration does.
        """
        with self.transaction() as conn:
            self._lock_scope(conn, lease.incident_id)
            conn.execute(
                "SELECT 1 FROM opspilot_incidents WHERE incident_id=%s FOR UPDATE",
                (lease.incident_id,),
            )
            row = conn.execute(
                "SELECT r.owner,r.epoch,r.lease_until,r.deadline,i.control_generation AS incident_generation,sc.global_suspended,sc.global_generation,COALESCE(ts.suspended,false) AS target_suspended,COALESCE(ts.generation,0) AS target_generation FROM opspilot_runs r JOIN opspilot_incidents i ON i.incident_id=r.incident_id JOIN opspilot_scope_controls sc ON sc.scope_id=1 LEFT JOIN opspilot_target_suspensions ts ON ts.target_id=i.target_id WHERE r.run_id=%s FOR UPDATE OF i,r",
                (lease.run_id,),
            ).fetchone()
            if not row or self._lease_revoked(row, lease, self._db_now(conn)):
                raise PersistenceError("CONTROL_DENIED")
            conn.execute(
                "UPDATE opspilot_runs SET state='blocked',owner=NULL,lease_until=NULL WHERE run_id=%s AND state='running'",
                (lease.run_id,),
            )

    def hand_off(self, lease: Lease) -> None:
        """Park this attempt's Run for a human (ADR-0005: a handoff is not published).

        ``running -> waiting_human`` (``RUN_EXECUTION`` ``awaiting_human_input``):
        the lease is released, the incident stays open and its conclusion
        untouched. ``claim()`` never resumes a waiting Run on its own;
        ``control()`` re-queues it on follow_up/correct, or cancels it so a
        ``new_run`` can follow. Fenced like ``block()``: a lease human control
        already superseded cannot park a Run it no longer holds.
        """
        with self.transaction() as conn:
            self._lock_scope(conn, lease.incident_id)
            conn.execute(
                "SELECT 1 FROM opspilot_incidents WHERE incident_id=%s FOR UPDATE",
                (lease.incident_id,),
            )
            row = conn.execute(
                "SELECT r.owner,r.epoch,r.lease_until,r.deadline,r.state AS run_state,i.control_generation AS incident_generation,sc.global_suspended,sc.global_generation,COALESCE(ts.suspended,false) AS target_suspended,COALESCE(ts.generation,0) AS target_generation FROM opspilot_runs r JOIN opspilot_incidents i ON i.incident_id=r.incident_id JOIN opspilot_scope_controls sc ON sc.scope_id=1 LEFT JOIN opspilot_target_suspensions ts ON ts.target_id=i.target_id WHERE r.run_id=%s FOR UPDATE OF i,r",
                (lease.run_id,),
            ).fetchone()
            if (
                not row
                or row["run_state"] != "running"
                or self._lease_revoked(row, lease, self._db_now(conn))
            ):
                raise PersistenceError("CONTROL_DENIED")
            self._park(conn, lease.run_id)

    @staticmethod
    def _park(conn: Connection, run_id: UUID) -> bool:
        """The one ``running -> waiting_human`` write (``hand_off`` and the sweep)."""
        parked = conn.execute(
            "UPDATE opspilot_runs SET state='waiting_human',owner=NULL,lease_until=NULL WHERE run_id=%s AND state='running' RETURNING run_id",
            (run_id,),
        ).fetchone()
        return parked is not None

    def sweep_expired_runs(
        self, *, incident_id: UUID | None = None, limit: int = 100
    ) -> tuple[tuple[UUID, UUID], ...]:
        """Park every ``running`` Run whose ``deadline`` has passed (ADR-0005 §2).

        A Run past its deadline can never settle itself: the deadline fences
        every worker write, so without this sweep the row stays ``running``
        for ever. Each overdue Run is parked exactly as ``hand_off`` parks a
        loop handoff (``_park``): ``waiting_human``, lease released, incident
        open, conclusion untouched; the reason (``DEADLINE_EXCEEDED``) is the
        caller's to announce.

        No lease is involved, so the guard is the state and the deadline,
        re-checked under row locks in the transaction that writes: a human
        decision that landed first (cancelled, paused, re-queued, or a newer
        Run) has already moved the row off ``running`` and is left alone.
        Candidates are read by the database clock; each is then parked in its
        own transaction, locking the incident before the Run in the same order
        as every other write path, so two sweeps (or a sweep and a worker)
        never deadlock and only one of them parks a given Run. Returns the
        ``(incident_id, run_id)`` pairs this call parked; ``incident_id``
        narrows the scan to one incident (the per-poll / per-page-load use).
        """
        with self.transaction(snapshot=True) as conn:
            candidates = conn.execute(
                "SELECT incident_id,run_id FROM opspilot_runs WHERE state='running' AND deadline<=clock_timestamp() AND (%s::uuid IS NULL OR incident_id=%s) ORDER BY deadline LIMIT %s",
                (incident_id, incident_id, limit),
            ).fetchall()
        parked: list[tuple[UUID, UUID]] = []
        for candidate in candidates:
            with self.transaction() as conn:
                conn.execute(
                    "SELECT 1 FROM opspilot_incidents WHERE incident_id=%s FOR UPDATE",
                    (candidate["incident_id"],),
                )
                row = conn.execute(
                    "SELECT state,deadline FROM opspilot_runs WHERE run_id=%s FOR UPDATE",
                    (candidate["run_id"],),
                ).fetchone()
                if (
                    row is None
                    or row["state"] != "running"
                    or row["deadline"] > self._db_now(conn)
                ):
                    continue
                if self._park(conn, candidate["run_id"]):
                    parked.append((candidate["incident_id"], candidate["run_id"]))
        return tuple(parked)

    def lease_current(self, lease: Lease) -> bool:
        """Read the authoritative owner/epoch/generation/expiry fence."""
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT r.owner,r.epoch,r.lease_until,r.deadline,i.control_generation AS incident_generation,sc.global_suspended,sc.global_generation,COALESCE(ts.suspended,false) AS target_suspended,COALESCE(ts.generation,0) AS target_generation FROM opspilot_runs r JOIN opspilot_incidents i ON i.incident_id=r.incident_id JOIN opspilot_scope_controls sc ON sc.scope_id=1 LEFT JOIN opspilot_target_suspensions ts ON ts.target_id=i.target_id WHERE r.run_id=%s AND i.incident_id=%s",
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
            self._lock_scope(conn, lease.incident_id)
            # 先锁 incident 再锁 run：全模块统一这个顺序，避免与 control()/
            # publish() 交叉形成 ABBA 死锁（control 只拿到 incident_id，
            # 结构上必须先读 incident，因此以它为规范顺序）。
            conn.execute(
                "SELECT 1 FROM opspilot_incidents WHERE incident_id=%s FOR UPDATE",
                (lease.incident_id,),
            )
            row = conn.execute(
                "SELECT r.owner,r.epoch,r.lease_until,r.deadline,i.control_generation AS incident_generation,sc.global_suspended,sc.global_generation,COALESCE(ts.suspended,false) AS target_suspended,COALESCE(ts.generation,0) AS target_generation FROM opspilot_runs r JOIN opspilot_incidents i ON i.incident_id=r.incident_id JOIN opspilot_scope_controls sc ON sc.scope_id=1 LEFT JOIN opspilot_target_suspensions ts ON ts.target_id=i.target_id WHERE r.run_id=%s FOR UPDATE OF i,r",
                (lease.run_id,),
            ).fetchone()
            if not row or self._lease_revoked(row, lease, self._db_now(conn)):
                self._late_result(
                    conn,
                    lease.run_id,
                    f"{_LATE_RESULT_KEY_PREFIX}step:{logical_key}:e{lease.epoch}",
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
            frozen = conn.execute(
                "UPDATE opspilot_input_rounds SET committed=true WHERE run_id=%s AND logical_key=%s AND control_generation=%s RETURNING input_watermark",
                (lease.run_id, logical_key, lease.control_generation),
            ).fetchone()
            if frozen:
                conn.execute(
                    "UPDATE opspilot_runs SET input_watermark=GREATEST(input_watermark,%s) WHERE run_id=%s",
                    (frozen["input_watermark"], lease.run_id),
                )
            return step_id

    def commit_tool(
        self, lease: Lease, step_id: UUID, ordinal: int, result: dict[str, Any]
    ) -> None:
        """Commit one tool result idempotently; late or expired leases are rejected."""
        with self.transaction() as conn:
            self._lock_scope(conn, lease.incident_id)
            # 先锁 incident 再锁 run：全模块统一这个顺序，避免与 control()/
            # publish() 交叉形成 ABBA 死锁（control 只拿到 incident_id，
            # 结构上必须先读 incident，因此以它为规范顺序）。
            conn.execute(
                "SELECT 1 FROM opspilot_incidents WHERE incident_id=%s FOR UPDATE",
                (lease.incident_id,),
            )
            row = conn.execute(
                "SELECT r.owner,r.epoch,r.lease_until,r.deadline,i.control_generation AS incident_generation,s.control_generation AS step_generation,s.status AS step_status,s.response,s.tool_results,sc.global_suspended,sc.global_generation,COALESCE(ts.suspended,false) AS target_suspended,COALESCE(ts.generation,0) AS target_generation FROM opspilot_runs r JOIN opspilot_incidents i ON i.incident_id=r.incident_id JOIN opspilot_steps s ON s.run_id=r.run_id JOIN opspilot_scope_controls sc ON sc.scope_id=1 LEFT JOIN opspilot_target_suspensions ts ON ts.target_id=i.target_id WHERE r.run_id=%s AND s.step_id=%s FOR UPDATE OF i,r,s",
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
                    f"{_LATE_RESULT_KEY_PREFIX}tool:{step_id}:{ordinal}:e{lease.epoch}",
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
        self,
        incident_id: UUID,
        expected_generation: int,
        action: str,
        actor: str,
        payload: dict[str, Any] | None = None,
        *,
        renew_run_id: UUID | None = None,
        renew_deadline: datetime | None = None,
        renew_input: dict[str, Any] | None = None,
    ) -> int:
        """Apply one human decision under ``expected_generation``.

        ``renew_run_id`` / ``renew_deadline`` (user decision 2026-09-25): a
        follow_up or correct on a Run whose ``deadline`` has passed (parked by
        the sweep as ``DEADLINE_EXCEEDED``, or still ``running``/``queued``
        while overdue -- every claim and write is fenced either way) cannot
        continue that Run. Mirroring upstream, where a message after TIMEOUT
        is a new request, the note is recorded and a fresh Run starts under
        the same generation step, exactly as ``new_run`` builds one: the old
        Run is cancelled, the new row keeps its budget limit and versions,
        takes ``renew_deadline`` and ``renew_input`` (the successor's own
        input snapshot, ``None`` like ``new_run``'s default: an input bound
        to the old Run's id cannot be reused as is), and the incident points
        at it. The new Run reads every prior human input (``begin_round``
        freezes the incident's inputs, not the Run's). Without a renewal the
        note is refused (``ILLEGAL_TRANSITION``) rather than re-queueing a
        row nothing can ever claim. A Run that is not overdue is re-queued as
        before and the renewal arguments are unused.
        """
        # payload 只接受 JSON 对象：列表/标量虽是合法 JSON，落库后模型侧投影
        # （非 Mapping 一律丢弃）会把操作者的文字静默吞掉，而 control() 却已报告
        # 成功（codex review，PR #31）。在任何写入之前拒绝，代际不推进，不留审计行；
        # 与 append_input() 对事件内容的同一条规则一致。
        if payload is not None and not isinstance(payload, dict):
            raise PersistenceError("INVALID_INPUT")
        with self.transaction() as conn:
            scope = self._lock_scope(conn, incident_id)
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
                    "SELECT state AS run_state,deadline,budget_limit,versions FROM opspilot_runs WHERE run_id=%s AND incident_id=%s FOR UPDATE",
                    (row["current_run_id"], incident_id),
                ).fetchone()
                if row["current_run_id"] is not None
                else None
            )
            if not run:
                raise PersistenceError("INCONSISTENT_STATE")
            # 超时的 Run 换新 Run：条件与清扫同源（数据库时钟、行锁下判定），
            # 因此与清扫并发时无论谁先到，结果都是「旧 Run 关闭、新 Run 排队」。
            renew = (
                action in {"follow_up", "correct"}
                and run["run_state"] in _CONTROL_OPEN_RUN_STATES
                and run["deadline"] <= self._db_now(conn)
            )
            if (
                row["state"] in {"cancelled", "completed"}
                or row["conclusion"] is not None
            ):
                raise PersistenceError("ILLEGAL_TRANSITION")
            # 暂停态只接受 resume 与 cancel，与 opspilot/domain/runs.py 的
            # RUN_EXECUTION（paused -> human_resume / human_cancel）一致。
            # 追问与纠正不得静默解除人工暂停：带 payload 的 follow_up/correct
            # 只是被记录（下方 keep_paused 保持暂停），不带 payload 的一律拒绝；
            # 对已暂停事故再次 pause 无论是否带 payload 都是非法迁移。
            if (
                row["state"] == "paused"
                and action in {"pause", "follow_up", "correct"}
                and (action == "pause" or payload is None)
            ):
                raise PersistenceError("ILLEGAL_TRANSITION")
            # 按放行名单判定而不是点名 blocked：原写法只挡住当时想到的那一个
            # 状态，`failed`/`budget_exhausted` 这类终态一旦开始被写入就会静默
            # 变成「允许」。cancel 不受限，它是人工控制的兜底出口。
            if action != "cancel" and run["run_state"] not in _CONTROL_OPEN_RUN_STATES:
                raise PersistenceError("ILLEGAL_TRANSITION")
            if renew:
                if renew_run_id is None or renew_deadline is None:
                    raise PersistenceError("ILLEGAL_TRANSITION")
                if conn.execute(
                    "SELECT 1 FROM opspilot_runs WHERE run_id=%s", (renew_run_id,)
                ).fetchone():
                    raise PersistenceError("IDENTITY_CONFLICT")
            keep_paused = action != "cancel" and (
                scope["global_suspended"]
                or scope["target_suspended"]
                or (row["state"] == "paused" and action in {"follow_up", "correct"})
            )
            nxt = expected_generation + 1
            state = (
                "cancelled"
                if action == "cancel"
                else ("paused" if action == "pause" else "running")
            )
            if keep_paused:
                state = "paused"
            conn.execute(
                "UPDATE opspilot_incidents SET control_generation=%s,state=%s WHERE incident_id=%s",
                (nxt, state, incident_id),
            )
            if action == "cancel":
                conn.execute(
                    "UPDATE opspilot_runs SET state='cancelled',owner=NULL,lease_until=NULL,control_generation=%s WHERE incident_id=%s AND state IN ('queued','paused','running','waiting_human','blocked')",
                    (nxt, incident_id),
                )
            elif renew:
                # 与 new_run 同一套写入：旧 Run 关闭、新行沿用预算上限与版本、
                # 事故指向新 Run；只是代际推进一步而不是两步，审计行仍是这条追问。
                # 必须排在 keep_paused 分支之前：暂停的事故 / 挂起的范围下追问同样
                # 不能把过期 Run 留在原地，新 Run 以 paused 开出（独立审查 P1-1）。
                conn.execute(
                    "UPDATE opspilot_runs SET state='cancelled',owner=NULL,lease_until=NULL,control_generation=%s WHERE incident_id=%s AND state IN ('queued','paused','running','waiting_human','blocked')",
                    (nxt, incident_id),
                )
                conn.execute(
                    "INSERT INTO opspilot_runs(run_id,incident_id,state,control_generation,budget_limit,deadline,versions,input) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        renew_run_id,
                        incident_id,
                        "paused" if keep_paused else "queued",
                        nxt,
                        run["budget_limit"],
                        renew_deadline,
                        Jsonb(run["versions"]),
                        None if renew_input is None else Jsonb(renew_input),
                    ),
                )
                conn.execute(
                    "UPDATE opspilot_incidents SET current_run_id=%s,lifecycle='open',conclusion=NULL WHERE incident_id=%s",
                    (renew_run_id, incident_id),
                )
            elif action == "pause" and not keep_paused:
                conn.execute(
                    "UPDATE opspilot_runs SET state='paused',owner=NULL,lease_until=NULL,control_generation=%s WHERE incident_id=%s AND state IN ('running','waiting_human')",
                    (nxt, incident_id),
                )
            elif keep_paused:
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
                "INSERT INTO opspilot_controls(audit_id,incident_id,action,expected_generation,resulting_generation,actor,payload) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (
                    uuid4(),
                    incident_id,
                    action,
                    expected_generation,
                    nxt,
                    actor,
                    Jsonb(payload) if payload is not None else None,
                ),
            )
            if action in {"follow_up", "correct"}:
                content = payload if payload is not None else {}
                seq = self._require_row(
                    conn.execute(
                        "SELECT COALESCE(MAX(sequence),0)+1 AS next_sequence FROM opspilot_inputs WHERE incident_id=%s",
                        (incident_id,),
                    )
                )["next_sequence"]
                conn.execute(
                    "INSERT INTO opspilot_inputs(input_id,incident_id,sequence,kind,content,actor,control_generation) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                    (uuid4(), incident_id, seq, action, Jsonb(content), actor, nxt),
                )
            return nxt

    def append_input(
        self,
        incident_id: UUID,
        input_id: UUID,
        content: dict[str, Any],
        *,
        actor: str = "event",
    ) -> int:
        """Receive an event even while paused; it never advances analyzed state."""
        if not isinstance(content, dict):
            raise PersistenceError("INVALID_INPUT")
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT control_generation FROM opspilot_incidents WHERE incident_id=%s FOR UPDATE",
                (incident_id,),
            ).fetchone()
            if row is None:
                raise PersistenceError("UNKNOWN_IDENTITY")
            existing = conn.execute(
                "SELECT * FROM opspilot_inputs WHERE input_id=%s", (input_id,)
            ).fetchone()
            if existing:
                if (
                    existing["incident_id"] != incident_id
                    or existing["content"] != content
                    or existing["actor"] != actor
                ):
                    raise PersistenceError("IDENTITY_CONFLICT")
                return int(existing["sequence"])
            seq = self._require_row(
                conn.execute(
                    "SELECT COALESCE(MAX(sequence),0)+1 AS sequence FROM opspilot_inputs WHERE incident_id=%s",
                    (incident_id,),
                )
            )["sequence"]
            conn.execute(
                "INSERT INTO opspilot_inputs(input_id,incident_id,sequence,kind,content,actor,control_generation) VALUES(%s,%s,%s,'event',%s,%s,%s)",
                (
                    input_id,
                    incident_id,
                    seq,
                    Jsonb(content),
                    actor,
                    row["control_generation"],
                ),
            )
            return int(seq)

    def begin_round(self, lease: Lease, logical_key: str) -> dict[str, Any]:
        """Freeze input references before dispatch; retry reads the same boundary."""
        if not logical_key or logical_key.startswith(_LATE_RESULT_KEY_PREFIX):
            raise PersistenceError("INVALID_INPUT")
        with self.transaction() as conn:
            scope = self._lock_scope(conn, lease.incident_id)
            conn.execute(
                "SELECT 1 FROM opspilot_incidents WHERE incident_id=%s FOR UPDATE",
                (lease.incident_id,),
            )
            row = conn.execute(
                "SELECT r.owner,r.epoch,r.lease_until,r.deadline,i.control_generation AS incident_generation FROM opspilot_runs r JOIN opspilot_incidents i ON i.incident_id=r.incident_id WHERE r.run_id=%s AND i.incident_id=%s FOR UPDATE OF r",
                (lease.run_id, lease.incident_id),
            ).fetchone()
            if not row or self._lease_revoked(
                {**row, **scope}, lease, self._db_now(conn)
            ):
                raise PersistenceError("CONTROL_DENIED")
            frozen = conn.execute(
                "SELECT * FROM opspilot_input_rounds WHERE run_id=%s AND logical_key=%s",
                (lease.run_id, logical_key),
            ).fetchone()
            if frozen and frozen["control_generation"] != lease.control_generation:
                # The step key (``segment:round-N``) is stable across
                # control generations: the transcript rebuild parses it, so
                # it cannot carry the generation. A boundary frozen under a
                # superseded generation whose round never committed belongs
                # to a dead attempt (e.g. a provider retry that aborted); the
                # human decision that moved the generation on is exactly what
                # this round must now see, so it is re-frozen at the current
                # watermark. A *committed* round under another generation is
                # a real conflict and stays refused.
                if frozen["committed"]:
                    raise PersistenceError("CONTROL_DENIED")
                frozen = None
            if frozen is None:
                watermark = self._require_row(
                    conn.execute(
                        "SELECT COALESCE(MAX(sequence),0) AS watermark FROM opspilot_inputs WHERE incident_id=%s",
                        (lease.incident_id,),
                    )
                )["watermark"]
                frozen = self._require_row(
                    conn.execute(
                        "INSERT INTO opspilot_input_rounds(run_id,logical_key,control_generation,input_watermark) VALUES(%s,%s,%s,%s) ON CONFLICT (run_id,logical_key) DO UPDATE SET control_generation=EXCLUDED.control_generation,input_watermark=EXCLUDED.input_watermark,committed=false RETURNING *",
                        (
                            lease.run_id,
                            logical_key,
                            lease.control_generation,
                            watermark,
                        ),
                    )
                )
            inputs = conn.execute(
                "SELECT sequence,kind,content FROM opspilot_inputs WHERE incident_id=%s AND sequence<=%s ORDER BY sequence",
                (lease.incident_id, frozen["input_watermark"]),
            ).fetchall()
            return {**frozen, "inputs": inputs}

    def read_inputs(
        self, incident_id: UUID, *, after_sequence: int = 0
    ) -> list[dict[str, Any]]:
        with self.transaction(snapshot=True) as conn:
            return list(
                conn.execute(
                    "SELECT * FROM opspilot_inputs WHERE incident_id=%s AND sequence>%s ORDER BY sequence",
                    (incident_id, after_sequence),
                ).fetchall()
            )

    def publish(
        self, lease: Lease, conclusion: dict[str, Any], *, step_id: UUID
    ) -> bool:
        with self.transaction() as conn:
            self._lock_scope(conn, lease.incident_id)
            row = conn.execute(
                "SELECT i.current_run_id,i.conclusion,i.state AS incident_state,i.control_generation AS incident_generation,r.owner,r.epoch,r.lease_until,r.deadline,r.state AS run_state,sc.global_suspended,sc.global_generation,COALESCE(ts.suspended,false) AS target_suspended,COALESCE(ts.generation,0) AS target_generation FROM opspilot_incidents i JOIN opspilot_runs r ON r.run_id=i.current_run_id JOIN opspilot_scope_controls sc ON sc.scope_id=1 LEFT JOIN opspilot_target_suspensions ts ON ts.target_id=i.target_id WHERE i.incident_id=%s FOR UPDATE OF i,r",
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
                    f"{_LATE_RESULT_KEY_PREFIX}publish:{step_id}:e{lease.epoch}",
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
            inputs = conn.execute(
                "SELECT * FROM opspilot_inputs WHERE incident_id=%s ORDER BY sequence",
                (incident_id,),
            ).fetchall()
            rounds = conn.execute(
                "SELECT * FROM opspilot_input_rounds WHERE run_id=%s ORDER BY logical_key",
                (run["run_id"],),
            ).fetchall()
            return {
                "inputs": inputs,
                "input_rounds": rounds,
                "pending_inputs": [
                    item for item in inputs if item["sequence"] > run["input_watermark"]
                ],
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
