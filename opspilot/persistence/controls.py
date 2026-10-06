"""Human controls: suspensions, control actions, inputs and control state."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from opspilot.persistence.base import PersistenceError, _StoreBase

# 非 cancel 的人工动作只对这些 run 状态开放；其余一律拒绝（fail closed）。
_CONTROL_OPEN_RUN_STATES = frozenset({"queued", "running", "paused", "waiting_human"})


class _ControlOps(_StoreBase):
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

    def control_state(self, incident_id: UUID) -> dict[str, Any]:
        """控制状态的只读快照，供工具网关的 ``ControlAuthority`` 使用。

        返回事故代际、事故状态，以及全局/目标两层挂起的标志与代际——与四条
        写路径的 ``_lease_revoked`` 比较的是同一批列。它只是执行器派发前后的
        快路径检查；权威栅栏仍是 ``charge_tool``/``commit_tool`` 在行锁内的比较。
        """
        if not isinstance(incident_id, UUID):
            raise PersistenceError("INVALID_INPUT")
        with self.transaction(snapshot=True) as conn:
            row = conn.execute(
                "SELECT control_generation,state FROM opspilot_incidents WHERE incident_id=%s",
                (incident_id,),
            ).fetchone()
            if row is None:
                raise PersistenceError("UNKNOWN_IDENTITY")
            scope = self._lock_scope(conn, incident_id, lock=False)
        return {
            "incident_generation": int(row["control_generation"]),
            "incident_state": row["state"],
            "global_suspended": bool(scope["global_suspended"]),
            "global_generation": int(scope["global_generation"]),
            "target_suspended": bool(scope["target_suspended"]),
            "target_generation": int(scope["target_generation"]),
        }

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
            overdue = run["run_state"] in _CONTROL_OPEN_RUN_STATES and run[
                "deadline"
            ] <= self._db_now(conn)
            renew = action in {"follow_up", "correct"} and overdue
            # resume 不带新输入，过期的 Run 没有可以恢复进去的东西：重排队只会
            # 留下一行谁都领不到、每次轮询都被拒绝的 queued。与 #47 对无续开
            # 参数的追问同一处置：拒绝；出路是追问/纠正（续开）或 cancel + new_run。
            if action == "resume" and overdue:
                raise PersistenceError("ILLEGAL_TRANSITION")
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
