"""PostgreSQL business-state authority for the first durable M1 slice."""

from __future__ import annotations

from collections.abc import Iterator
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


class PersistenceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Lease:
    incident_id: UUID
    run_id: UUID
    owner: UUID
    epoch: int
    control_generation: int


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
              UNIQUE(run_id, logical_key)
            );
            ALTER TABLE opspilot_steps ADD COLUMN IF NOT EXISTS sequence integer NOT NULL DEFAULT 0;
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
            if row["versions"] != versions:
                conn.execute(
                    "UPDATE opspilot_runs SET state='blocked' WHERE run_id=%s",
                    (run_id,),
                )
                incompatible = True
            elif row["incident_state"] in {"completed", "cancelled", "paused"}:
                raise PersistenceError("CONTROL_DENIED")
            elif row["run_state"] not in ("queued", "running"):
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
                    "UPDATE opspilot_runs SET state='running',owner=%s,epoch=%s,control_generation=%s,lease_until=clock_timestamp()+make_interval(secs=>%s) WHERE run_id=%s",
                    (owner, epoch, row["incident_generation"], lease_seconds, run_id),
                )
                lease = Lease(
                    incident_id, run_id, owner, epoch, int(row["incident_generation"])
                )
        if incompatible:
            raise PersistenceError("INCOMPATIBLE_STATE")
        assert lease is not None
        return lease

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

    def commit_step(
        self, lease: Lease, logical_key: str, response: dict[str, Any]
    ) -> UUID:
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
                "SELECT r.owner,r.epoch,r.lease_until,r.deadline,i.control_generation AS incident_generation,s.control_generation AS step_generation,s.tool_results FROM opspilot_runs r JOIN opspilot_incidents i ON i.incident_id=r.incident_id JOIN opspilot_steps s ON s.run_id=r.run_id WHERE r.run_id=%s AND s.step_id=%s FOR UPDATE",
                (lease.run_id, step_id),
            ).fetchone()
            if (
                not row
                or row["step_generation"] != lease.control_generation
                or self._lease_revoked(row, lease, self._db_now(conn))
            ):
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
            row = conn.execute(
                "SELECT i.control_generation,i.state,i.conclusion,r.state AS run_state FROM opspilot_incidents i JOIN opspilot_runs r ON r.run_id=i.current_run_id WHERE i.incident_id=%s FOR UPDATE",
                (incident_id,),
            ).fetchone()
            if not row or row["control_generation"] != expected_generation:
                raise PersistenceError("CONTROL_CONFLICT")
            if action not in {"cancel", "pause", "resume", "follow_up", "correct"}:
                raise PersistenceError("INVALID_INPUT")
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
            if row["run_state"] == "blocked" and action != "cancel":
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
                conn.execute(
                    "INSERT INTO opspilot_steps(step_id,run_id,logical_key,status,response,control_generation) VALUES(%s,%s,%s,'late_result',%s,%s) ON CONFLICT DO NOTHING",
                    (
                        uuid4(),
                        lease.run_id,
                        f"late:{uuid4()}",
                        Jsonb(conclusion),
                        lease.control_generation,
                    ),
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
            run = conn.execute(
                "SELECT * FROM opspilot_runs WHERE run_id=%s", (row["current_run_id"],)
            ).fetchone()
            steps = conn.execute(
                "SELECT * FROM opspilot_steps WHERE run_id=%s ORDER BY sequence, step_id",
                (row["current_run_id"],),
            ).fetchall()
            return {
                "incident_id": row["incident_id"],
                "state": row["state"],
                "control_generation": row["control_generation"],
                "run": run,
                "steps": steps,
                # 只列出当前代际的待办工具调用。旧代际的步骤仍留在 steps 里作为
                # 记录，但人工决定之后它们已经不该再被执行；照旧列出会让调用方
                # 把过期证据重新提交成「当前已提交的证据」，而 commit_tool 在
                # 写入处拒绝它们——断点会因此永远重建出做不完的待办。
                "pending_tools": [
                    {"step_id": step["step_id"], "ordinal": ordinal}
                    for step in steps
                    if step["control_generation"] == row["control_generation"]
                    for ordinal in range(
                        len((step["response"] or {}).get("tool_calls", []))
                    )
                    if ordinal
                    not in {
                        item.get("ordinal") for item in (step["tool_results"] or [])
                    }
                ],
                "conclusion": row["conclusion"],
            }
