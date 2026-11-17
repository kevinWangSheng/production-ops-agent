"""Human controls: suspensions, control actions, inputs and control state."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid4, uuid5

from psycopg.types.json import Jsonb

from opspilot.persistence.base import Connection, PersistenceError, _StoreBase

# 非 cancel 的人工动作只对这些 run 状态开放；其余一律拒绝（fail closed）。
_CONTROL_OPEN_RUN_STATES = frozenset({"queued", "running", "paused", "waiting_human"})


class _ControlOps(_StoreBase):
    def suspension_state(self) -> dict[str, Any]:
        """Read gates in one snapshot, without reading the suspension audit."""
        with self.transaction(snapshot=True) as conn:
            scope = self._lock_scope(conn, lock=False)
            rows = conn.execute(
                "SELECT t.resource_uid,COALESCE(s.suspended,false) AS suspended,"
                "COALESCE(s.generation,0) AS generation FROM opspilot_targets t "
                "LEFT JOIN opspilot_target_suspensions s USING (target_id)"
            ).fetchall()
        return {
            "global": {
                "suspended": bool(scope["global_suspended"]),
                "generation": int(scope["global_generation"]),
            },
            "targets": {
                row["resource_uid"]: {
                    "suspended": bool(row["suspended"]),
                    "generation": int(row["generation"]),
                }
                for row in rows
            },
        }

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
                "SELECT observation_id FROM opspilot_release_observations ORDER BY observation_id FOR UPDATE"
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
        target_id: UUID | None,
        suspended: bool,
        *,
        expected_generation: int,
        actor: str,
        resource_uid: str | None = None,
    ) -> int:
        """Change one registered immutable target's gate and invalidate its leases.

        With ``resource_uid`` instead of ``target_id`` (the operator entry),
        the target is registered in the same transaction, so a refused
        request (e.g. ``CONTROL_CONFLICT``) leaves no target row behind.
        """
        if type(suspended) is not bool or (
            not isinstance(target_id, UUID)
            if resource_uid is None
            else target_id is not None
            or not isinstance(resource_uid, str)
            or not resource_uid
        ):
            raise PersistenceError("INVALID_INPUT")
        with self.transaction() as conn:
            self._lock_scope(conn)
            if resource_uid is not None:
                found = conn.execute(
                    "SELECT target_id FROM opspilot_targets WHERE resource_uid=%s",
                    (resource_uid,),
                ).fetchone()
                if found is None:
                    target_id = uuid4()
                    conn.execute(
                        "INSERT INTO opspilot_targets(target_id,resource_uid) VALUES(%s,%s)",
                        (target_id, resource_uid),
                    )
                else:
                    target_id = found["target_id"]
            elif not conn.execute(
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
                "SELECT observation_id FROM opspilot_release_observations WHERE target_id=%s ORDER BY observation_id FOR UPDATE",
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

    def control_times(self, incident_id: UUID) -> list[dict[str, Any]]:
        """Read the database timestamps of committed human decisions."""
        with self.transaction(snapshot=True) as conn:
            return list(
                conn.execute(
                    "SELECT resulting_generation,created_at FROM opspilot_controls WHERE incident_id=%s ORDER BY resulting_generation",
                    (incident_id,),
                ).fetchall()
            )

    def control(
        self,
        incident_id: UUID,
        expected_generation: int,
        action: str,
        actor: str,
        payload: dict[str, Any] | None = None,
    ) -> int:
        """Apply one human decision under ``expected_generation``.

        A human message to the investigation is not a decision: it is an
        input (``append_message``, workbench contract B1) and never passes
        here; the follow_up/correct actions that used to re-queue or renew a
        Run on a note are gone (contract O2). A published conclusion freezes
        pause/resume/cancel only while no continuation Run is active
        (contract A11).

        Close stops unfinished Runs and observation before closing the
        lifecycle; with a published conclusion the incident keeps it and is
        not marked cancelled (A11 r5). Reopen only returns resolved/closed
        incidents to open; it starts no work.
        """
        # payload 只接受 JSON 对象：列表/标量虽是合法 JSON，落库后模型侧投影
        # （非 Mapping 一律丢弃）会把操作者的文字静默吞掉，而 control() 却已报告
        # 成功（codex review，PR #31）。在任何写入之前拒绝，代际不推进，不留审计行；
        # 与 append_input() 对事件内容的同一条规则一致。
        if payload is not None and not isinstance(payload, dict):
            raise PersistenceError("INVALID_INPUT")
        with self.transaction() as conn:
            # #245 U2: a rebind names its new target by its uid in
            # ``payload["to_target"]`` (the caller admits only registry keys,
            # as intake does); the store records ``from_target`` itself from
            # the locked row. Registry rows are immutable, so the lookup needs
            # no lock; an existing new target's suspension row is locked with
            # the current one's (G10). A uid with no row yet is registered
            # only once every refusal below has passed, in this transaction,
            # so a refused rebind writes nothing (#378 review).
            rebind_to = None
            to_uid = None
            if action == "rebind_target":
                to_uid = None if payload is None else payload.get("to_target")
                if not isinstance(to_uid, str) or not to_uid:
                    raise PersistenceError("INVALID_INPUT")
                if payload is not None and "from_target" in payload:
                    raise PersistenceError("INVALID_INPUT")
                registered = conn.execute(
                    "SELECT target_id FROM opspilot_targets WHERE resource_uid=%s",
                    (to_uid,),
                ).fetchone()
                if registered is not None:
                    rebind_to = registered["target_id"]
            # #245 U3/U4: a merge names its survivor in
            # ``payload["into_incident_id"]``. The survivor's target row is
            # locked with the source's (G10; a mismatch is refused below),
            # then both incident rows in ``incident_id`` order.
            survivor_id = None
            extra = [] if rebind_to is None else [rebind_to]
            if action == "merge_into":
                try:
                    survivor_id = UUID(str((payload or {})["into_incident_id"]))
                except (KeyError, ValueError):
                    raise PersistenceError("INVALID_INPUT") from None
                survivor_target = conn.execute(
                    "SELECT target_id FROM opspilot_incidents WHERE incident_id=%s",
                    (survivor_id,),
                ).fetchone()
                if survivor_target and survivor_target["target_id"] is not None:
                    extra.append(survivor_target["target_id"])
            scope = self._lock_scope(conn, incident_id, extra_target_ids=extra)
            if survivor_id is not None:
                conn.execute(
                    "SELECT incident_id FROM opspilot_incidents WHERE incident_id=ANY(%s) ORDER BY incident_id FOR UPDATE",
                    ([incident_id, survivor_id],),
                )
            # 分两步读，而不是一条 JOIN：JOIN 取不到行时无法区分「事故不存在」
            # 与「事故存在但 run 行缺失」，两者都会落到 CONTROL_CONFLICT，
            # 而该码的约定处置是「重读代际后重试」——两种情况重试都不会成功。
            # 顺序仍是先锁 incident 再锁 run，与三条写路径一致。
            row = conn.execute(
                "SELECT control_generation,state,mode,lifecycle,conclusion,current_run_id,target_id,merged_into_incident_id FROM opspilot_incidents WHERE incident_id=%s FOR UPDATE",
                (incident_id,),
            ).fetchone()
            if not row:
                raise PersistenceError("UNKNOWN_IDENTITY")
            if row["control_generation"] != expected_generation:
                raise PersistenceError("CONTROL_CONFLICT")
            if action not in {
                "cancel",
                "pause",
                "resume",
                "takeover",
                "close",
                "reopen",
                "rebind_target",
                "merge_into",
            }:
                raise PersistenceError("INVALID_INPUT")
            # #245 G4: a merged incident is final; the pointer stays one way.
            if row["merged_into_incident_id"] is not None and action in {
                "reopen",
                "rebind_target",
                "merge_into",
            }:
                raise PersistenceError("ILLEGAL_TRANSITION")
            human_owned = row["mode"] == "human_owned"
            if action in {"close", "reopen"}:
                allowed = (
                    {"open", "observing_recovery", "resolved"}
                    if action == "close"
                    else {"resolved", "closed"}
                )
                if row["lifecycle"] not in allowed:
                    raise PersistenceError("ILLEGAL_TRANSITION")
                nxt = expected_generation + 1
                conn.execute(
                    "UPDATE opspilot_incidents SET control_generation=%s WHERE incident_id=%s",
                    (nxt, incident_id),
                )
                state = row["state"]
                if action == "close":
                    # 结束记录保留当前生命周期；撤销必须先于写入 closed。
                    from opspilot.observation.revocation import (
                        revoke_authorized_sessions,
                    )

                    revoke_authorized_sessions(conn, incident_id)
                    cancelled = conn.execute(
                        "UPDATE opspilot_runs SET state='cancelled',owner=NULL,lease_until=NULL,control_generation=%s WHERE incident_id=%s AND state IN ('queued','paused','running','waiting_human','blocked') RETURNING run_id",
                        (nxt, incident_id),
                    ).fetchall()
                    if row["conclusion"] is None and any(
                        r["run_id"] == row["current_run_id"] for r in cancelled
                    ):
                        state = "cancelled"
                    elif row["conclusion"] is not None and state == "paused":
                        # A11 r5: an incident with a published conclusion
                        # keeps it and is not cancelled by closing (a
                        # continuation Run is only stopped); a pause it
                        # leaves behind is lifted, as cancel does.
                        state = "running"
                conn.execute(
                    "UPDATE opspilot_incidents SET lifecycle=%s,state=%s WHERE incident_id=%s",
                    ("closed" if action == "close" else "open", state, incident_id),
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
                from opspilot.knowledge.store import mark_stale_in

                mark_stale_in(
                    conn,
                    incident_id,
                    reason="control_generation_changed",
                    source=f"control.{action}",
                )
                return nxt
            if action == "rebind_target":
                assert to_uid is not None and payload is not None
                # #245 U2/G2：只在 open / observing_recovery 上改目标；
                # resolved/closed 要先 reopen，与 F12 r2 同向。没有绑定目标的
                # 事故（handoff）无可改。同一事务里代际 +1、撤销旧目标上的授权
                # 观察（结束记录按当前生命周期写，必须先于写回 open；
                # observing_recovery -> open 即领域边 observation_ended_unconfirmed）、
                # 取消全部未终态 Run（含 waiting_human，与 close 一致）、换目标、
                # 写审计。不开 Run：之后的追问 / new_run 按新目标拿 fresh 输入（G7）。
                if row["lifecycle"] in {"resolved", "closed"}:
                    raise PersistenceError("REOPEN_REQUIRED")
                if row["lifecycle"] not in {"open", "observing_recovery"}:
                    raise PersistenceError("ILLEGAL_TRANSITION")
                if row["target_id"] is None:
                    raise PersistenceError("ILLEGAL_TRANSITION")
                if row["target_id"] == rebind_to:
                    raise PersistenceError("TARGET_UNCHANGED")
                if rebind_to is None:
                    rebind_to = _register_target_in(conn, to_uid)
                from_uid = self._require_row(
                    conn.execute(
                        "SELECT resource_uid FROM opspilot_targets WHERE target_id=%s",
                        (row["target_id"],),
                    )
                )["resource_uid"]
                nxt = expected_generation + 1
                conn.execute(
                    "UPDATE opspilot_incidents SET control_generation=%s WHERE incident_id=%s",
                    (nxt, incident_id),
                )
                from opspilot.observation.revocation import revoke_authorized_sessions

                revoke_authorized_sessions(conn, incident_id)
                cancelled = conn.execute(
                    "UPDATE opspilot_runs SET state='cancelled',owner=NULL,lease_until=NULL,control_generation=%s WHERE incident_id=%s AND state IN ('queued','paused','running','waiting_human','blocked') RETURNING run_id",
                    (nxt, incident_id),
                ).fetchall()
                state = row["state"]
                if any(r["run_id"] == row["current_run_id"] for r in cancelled):
                    state = "cancelled"
                conn.execute(
                    "UPDATE opspilot_incidents SET lifecycle='open',state=%s,target_id=%s WHERE incident_id=%s",
                    (state, rebind_to, incident_id),
                )
                conn.execute(
                    "INSERT INTO opspilot_controls(audit_id,incident_id,action,expected_generation,resulting_generation,actor,payload) VALUES(%s,%s,'rebind_target',%s,%s,%s,%s)",
                    (
                        uuid4(),
                        incident_id,
                        expected_generation,
                        nxt,
                        actor,
                        Jsonb({**payload, "from_target": from_uid}),
                    ),
                )
                from opspilot.knowledge.store import mark_stale_in

                mark_stale_in(
                    conn,
                    incident_id,
                    reason="control_generation_changed",
                    source="control.rebind_target",
                )
                return nxt
            if action == "merge_into":
                assert survivor_id is not None and payload is not None
                return self._merge_into(
                    conn,
                    incident_id,
                    row,
                    survivor_id,
                    expected_generation,
                    actor,
                    payload,
                )
            if action == "takeover":
                # C3 §10 接管：mode -> human_owned，同一事务递增代际、撤销观察
                # 授权、停下自动调查：当前 Run 交给人（waiting_human，收回租约），
                # 之后 claim/new_run/续开都按 mode 拒绝。结论已发布或 Run 已取消
                # 的事故同样可接管——接管要收回的是观察授权与自动化，不以 Run 状态
                # 为前提；事故的控制镜像 state 不改。已是 human_owned 时再次接管
                # 与领域 apply_control 一致：总是清掉观察授权（撤销 human_owned 下
                # 单独登记的新会话）、推进代际、写审计，mode 与 Run 不动（机器人
                # 审查 PR #123）。回到 automatic 的边见 #124。
                nxt = expected_generation + 1
                conn.execute(
                    "UPDATE opspilot_incidents SET control_generation=%s,mode='human_owned' WHERE incident_id=%s",
                    (nxt, incident_id),
                )
                from opspilot.observation.revocation import revoke_authorized_sessions

                revoke_authorized_sessions(conn, incident_id)
                if not human_owned:
                    conn.execute(
                        "UPDATE opspilot_runs SET state='waiting_human',owner=NULL,lease_until=NULL,control_generation=%s WHERE incident_id=%s AND state IN ('queued','running','paused','blocked')",
                        (nxt, incident_id),
                    )
                conn.execute(
                    "INSERT INTO opspilot_controls(audit_id,incident_id,action,expected_generation,resulting_generation,actor,payload) VALUES(%s,%s,'takeover',%s,%s,%s,%s)",
                    (
                        uuid4(),
                        incident_id,
                        expected_generation,
                        nxt,
                        actor,
                        Jsonb(payload) if payload is not None else None,
                    ),
                )
                # M1-03 D18: the control generation moved.
                from opspilot.knowledge.store import mark_stale_in

                mark_stale_in(
                    conn,
                    incident_id,
                    reason="control_generation_changed",
                    source="control.takeover",
                )
                return nxt
            # 连 incident_id 一起查：状态判定读的是这一行，而下面的状态推进按
            # incident_id 作用于本 incident 真正的 run。两者指向不同的行时，一个
            # 外来的 running run 会把 blocked run 的保护顶开——实测 incident 停在
            # paused 而它自己的 run 仍是 blocked。只按 run_id 查会让守卫读到不属于
            # 这个 incident 的状态，因此先判为不一致，不推进任何状态。
            run = (
                conn.execute(
                    "SELECT state AS run_state,deadline FROM opspilot_runs WHERE run_id=%s AND incident_id=%s FOR UPDATE",
                    (row["current_run_id"], incident_id),
                ).fetchone()
                if row["current_run_id"] is not None
                else None
            )
            if not run:
                raise PersistenceError("INCONSISTENT_STATE")
            overdue = run["run_state"] in _CONTROL_OPEN_RUN_STATES and run[
                "deadline"
            ] <= self._db_now(conn)
            # Workbench contract A11 (r4): a published conclusion freezes the
            # controls only once no Run is active. A continuation Run (one a
            # message started beside the conclusion, contract A1) takes
            # pause/resume/cancel like any Run; cancel stops only that Run and
            # keeps the conclusion, lifecycle and observation.
            continuation = row["conclusion"] is not None and run["run_state"] in {
                "queued",
                "running",
                "paused",
            }
            concluded = row["state"] in {"cancelled", "completed"} or (
                row["conclusion"] is not None and not continuation
            )
            # resume 不带新输入，过期的 Run 没有可以恢复进去的东西：重排队只会
            # 留下一行谁都领不到、每次轮询都被拒绝的 queued。出路是 cancel +
            # new_run（Re-investigate）。human_owned 下 resume 不排队任何 Run，
            # 所以不受此限。
            if action == "resume" and overdue and not human_owned:
                raise PersistenceError("ILLEGAL_TRANSITION")
            if concluded:
                raise PersistenceError("ILLEGAL_TRANSITION")
            # 暂停态只接受 resume 与 cancel，与 opspilot/domain/runs.py 的
            # RUN_EXECUTION（paused -> human_resume / human_cancel）一致；对已暂停
            # 事故再次 pause 是非法迁移。
            if row["state"] == "paused" and action == "pause":
                raise PersistenceError("ILLEGAL_TRANSITION")
            # 按放行名单判定而不是点名 blocked：原写法只挡住当时想到的那一个
            # 状态，`failed`/`budget_exhausted` 这类终态一旦开始被写入就会静默
            # 变成「允许」。cancel 不受限，它是人工控制的兜底出口。
            if action != "cancel" and run["run_state"] not in _CONTROL_OPEN_RUN_STATES:
                raise PersistenceError("ILLEGAL_TRANSITION")
            keep_paused = action != "cancel" and (
                scope["global_suspended"] or scope["target_suspended"]
            )
            nxt = expected_generation + 1
            state = (
                "cancelled"
                if action == "cancel"
                else ("paused" if action == "pause" else "running")
            )
            if keep_paused:
                state = "paused"
            if action == "cancel" and continuation:
                # A11: the incident keeps its conclusion; its control mirror
                # leaves a pause behind but never says cancelled.
                state = "running" if row["state"] == "paused" else row["state"]
            # human_owned 下的 resume 只解除主体暂停：镜像走上面的默认值
            # （running），但 keep_paused 仍优先——全局/目标挂起期间 resume 不能
            # 把镜像写成 running（C3 §4 范围暂停优先于主体 resume；复验 PR #123）。
            conn.execute(
                "UPDATE opspilot_incidents SET control_generation=%s,state=%s WHERE incident_id=%s",
                (nxt, state, incident_id),
            )
            # C3 §10：暂停、取消在同一事务里撤销观察授权。其余动作不显式撤销：
            # 代际一变，会话绑定的控制代际过期，下一次采样按 binding_stale 结束
            # 会话。A11：取消续接 Run 不动观察。
            if action == "pause" or (action == "cancel" and not continuation):
                from opspilot.observation.revocation import revoke_authorized_sessions

                revoke_authorized_sessions(conn, incident_id)
            if action == "cancel":
                conn.execute(
                    "UPDATE opspilot_runs SET state='cancelled',owner=NULL,lease_until=NULL,control_generation=%s WHERE incident_id=%s AND state IN ('queued','paused','running','waiting_human','blocked')",
                    (nxt, incident_id),
                )
            elif human_owned and action in {"pause", "resume"}:
                # Under human ownership the Run is the human's (waiting_human):
                # pausing and resuming change the incident's control mirror,
                # never the Run.
                pass
            elif action == "pause" or keep_paused:
                conn.execute(
                    "UPDATE opspilot_runs SET state='paused',owner=NULL,lease_until=NULL,control_generation=%s WHERE incident_id=%s AND state IN ('running','waiting_human')",
                    (nxt, incident_id),
                )
            elif action == "resume":
                conn.execute(
                    "UPDATE opspilot_runs SET state='queued',owner=NULL,lease_until=NULL,control_generation=%s WHERE incident_id=%s AND state IN ('queued','paused','running','waiting_human')",
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
            # M1-03 D18: the control generation moved; drafts behind it are
            # stale in this transaction.
            from opspilot.knowledge.store import mark_stale_in

            mark_stale_in(
                conn,
                incident_id,
                reason="control_generation_changed",
                source=f"control.{action}",
            )
            return nxt

    def _merge_into(
        self,
        conn: Connection,
        incident_id: UUID,
        row: dict[str, Any],
        survivor_id: UUID,
        expected_generation: int,
        actor: str,
        payload: dict[str, Any],
    ) -> int:
        """#245 U3/U4/G3-G5 under the locks ``control`` took.

        The source (``incident_id``) is decided like a close: generation +1,
        observation revoked (the ending keeps the current lifecycle, written
        before ``closed``), every unfinished Run cancelled, lifecycle
        ``closed`` with ``merged_into_incident_id``, audit, ``mark_stale_in``.
        The survivor only takes one event input from a fixed template (no
        conclusion text): its generation, Runs and observation stay as they
        are (C3 section 10, the #343 attach precedent); ``begin_round`` reads
        the event at its next safe boundary.
        """
        survivor = conn.execute(
            "SELECT lifecycle,current_run_id,target_id,merged_into_incident_id FROM opspilot_incidents WHERE incident_id=%s",
            (survivor_id,),
        ).fetchone()
        if survivor is None:
            raise PersistenceError("UNKNOWN_IDENTITY")
        if survivor_id == incident_id:
            raise PersistenceError("ILLEGAL_TRANSITION")
        # An incident handed to a human at intake has no Run and no target.
        if row["current_run_id"] is None or survivor["current_run_id"] is None:
            raise PersistenceError("HANDOFF_ONLY")
        if (
            survivor["lifecycle"] == "closed"
            or survivor["merged_into_incident_id"] is not None
        ):
            raise PersistenceError("ILLEGAL_TRANSITION")
        if survivor["target_id"] != row["target_id"]:
            raise PersistenceError("TARGET_MISMATCH")
        content = _merge_event(conn, incident_id, row)
        nxt = expected_generation + 1
        conn.execute(
            "UPDATE opspilot_incidents SET control_generation=%s WHERE incident_id=%s",
            (nxt, incident_id),
        )
        from opspilot.observation.revocation import revoke_authorized_sessions

        revoke_authorized_sessions(conn, incident_id)
        cancelled = conn.execute(
            "UPDATE opspilot_runs SET state='cancelled',owner=NULL,lease_until=NULL,control_generation=%s WHERE incident_id=%s AND state IN ('queued','paused','running','waiting_human','blocked') RETURNING run_id",
            (nxt, incident_id),
        ).fetchall()
        state = row["state"]
        if any(r["run_id"] == row["current_run_id"] for r in cancelled):
            state = "cancelled"
        conn.execute(
            "UPDATE opspilot_incidents SET lifecycle='closed',state=%s,merged_into_incident_id=%s WHERE incident_id=%s",
            (state, survivor_id, incident_id),
        )
        conn.execute(
            "INSERT INTO opspilot_controls(audit_id,incident_id,action,expected_generation,resulting_generation,actor,payload) VALUES(%s,%s,'merge_into',%s,%s,%s,%s)",
            (
                uuid4(),
                incident_id,
                expected_generation,
                nxt,
                actor,
                Jsonb({**payload, "into_incident_id": str(survivor_id)}),
            ),
        )
        from opspilot.knowledge.store import mark_stale_in

        mark_stale_in(
            conn,
            incident_id,
            reason="control_generation_changed",
            source="control.merge_into",
        )
        # The survivor's event (its own mark_stale_in, input_added).
        append_input_in(
            conn,
            survivor_id,
            uuid5(_MERGE_NAMESPACE, f"merge:{incident_id}:{survivor_id}"),
            content,
            actor=actor,
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
            return append_input_in(conn, incident_id, input_id, content, actor=actor)

    def append_message(
        self,
        incident_id: UUID,
        input_id: UUID,
        content: dict[str, Any],
        *,
        actor: str,
    ) -> tuple[int, bool]:
        """Receive one human message for the investigation (workbench
        contract B1, D1-D10): a ``message`` input, never a control decision.

        The generation, the Runs, the lease, the lifecycle, the conclusion and
        the observation stay as they are. Admission is decided under the
        incident lock (r3: separate from the relay's predicate): an incident
        without a Run (handed to a human at intake) is ``HANDOFF_ONLY``, a
        merged one ``MERGED``; under human ownership the note is only
        recorded; an automatic incident that was cancelled, or whose
        investigation ended with neither a published conclusion nor any
        validated report (failed or timed out, D7b), is
        ``INVESTIGATION_NOT_AVAILABLE``. Idempotent on ``input_id``:
        returns ``(sequence, replayed)``.
        """
        if not isinstance(content, dict):
            raise PersistenceError("INVALID_INPUT")
        with self.transaction() as conn:
            scope = self._lock_scope(conn, incident_id)
            row = conn.execute(
                "SELECT mode,state,conclusion IS NOT NULL AS concluded,current_run_id,merged_into_incident_id FROM opspilot_incidents WHERE incident_id=%s FOR UPDATE",
                (incident_id,),
            ).fetchone()
            if row is None:
                raise PersistenceError("UNKNOWN_IDENTITY")
            existing = conn.execute(
                "SELECT incident_id,sequence FROM opspilot_inputs WHERE input_id=%s",
                (input_id,),
            ).fetchone()
            if existing is not None and existing["incident_id"] == incident_id:
                # A retried request replays whatever happened since; the
                # content check is append_input_in's.
                return (
                    append_input_in(
                        conn,
                        incident_id,
                        input_id,
                        content,
                        actor=actor,
                        kind="message",
                    ),
                    True,
                )
            if row["current_run_id"] is None:
                raise PersistenceError("HANDOFF_ONLY")
            if row["merged_into_incident_id"] is not None:
                raise PersistenceError("MERGED")
            if row["mode"] != "human_owned":
                if row["state"] == "cancelled":
                    raise PersistenceError("INVESTIGATION_NOT_AVAILABLE")
                run = self._require_row(
                    conn.execute(
                        "SELECT state FROM opspilot_runs WHERE run_id=%s",
                        (row["current_run_id"],),
                    )
                )
                active = (
                    run["state"] in {"queued", "running", "paused"}
                    or row["state"] == "paused"
                    or scope["global_suspended"]
                    or scope["target_suspended"]
                )
                if not (
                    active
                    or row["concluded"]
                    or _validated_report_in(conn, incident_id)
                ):
                    raise PersistenceError("INVESTIGATION_NOT_AVAILABLE")
            return (
                append_input_in(
                    conn, incident_id, input_id, content, actor=actor, kind="message"
                ),
                False,
            )

    def relay_candidates(self, *, limit: int = 20) -> tuple[UUID, ...]:
        """Incidents that may need a continuation Run (workbench contract
        R1/R2): automatic, not cancelled/paused/merged, with a ``message``
        no Run has read and no active current Run. A hint for the relay
        pass; ``relay_continuation`` re-checks everything under the lock."""
        if type(limit) is not int or limit < 1:
            raise PersistenceError("INVALID_INPUT")
        with self.transaction(snapshot=True) as conn:
            rows = conn.execute(
                "SELECT i.incident_id FROM opspilot_incidents i JOIN opspilot_runs r ON r.run_id=i.current_run_id "
                "WHERE i.mode='automatic' AND i.state NOT IN ('cancelled','paused') AND i.merged_into_incident_id IS NULL "
                "AND r.state NOT IN ('queued','running') "
                "AND EXISTS (SELECT 1 FROM opspilot_inputs m WHERE m.incident_id=i.incident_id AND m.kind='message' "
                "AND m.sequence>(SELECT COALESCE(max(x.input_watermark),0) FROM opspilot_runs x WHERE x.incident_id=i.incident_id)) "
                "ORDER BY i.priority,i.incident_id LIMIT %s",
                (limit,),
            ).fetchall()
        return tuple(row["incident_id"] for row in rows)

    def relay_basis(self, incident_id: UUID) -> dict[str, Any] | None:
        """What a continuation Run of ``incident_id`` would start from now
        (contract R1/B4), or ``None`` when the relay predicate does not hold:
        ``{"current_run_id", "max_unread", "floor", "mode"}``. A snapshot
        read; ``relay_continuation`` re-derives it under the lock."""
        with self.transaction(snapshot=True) as conn:
            scope = self._lock_scope(conn, incident_id, lock=False)
            return _relay_basis_in(conn, incident_id, scope, lock=False)

    def relay_continuation(
        self,
        incident_id: UUID,
        run_id: UUID,
        *,
        basis: dict[str, Any],
        deadline: datetime,
        budget_limit: int,
        versions: dict[str, str],
        input: dict[str, Any] | None,
    ) -> bool:
        """Open the continuation Run (workbench contract A1, A10, R3, R4).

        One transaction, scope lock then incident lock: an existing
        ``run_id`` is a replay (``False``, nothing written); the relay
        predicate is re-checked and must still give ``basis`` (else
        ``RENEW_INPUT_STALE``, the caller rebuilds the input; ``False`` when
        it no longer holds at all). Then the incident's unfinished inactive
        Runs (waiting_human, blocked, a leftover paused) are cancelled, the
        queued Run is inserted at the incident's current generation and the
        incident points at it. No control audit row, no generation step, no
        observation revoke, no lifecycle change, the conclusion kept.
        """
        with self.transaction() as conn:
            scope = self._lock_scope(conn, incident_id)
            if conn.execute(
                "SELECT 1 FROM opspilot_runs WHERE run_id=%s", (run_id,)
            ).fetchone():
                return False
            now = _relay_basis_in(conn, incident_id, scope, lock=True)
            if now is None:
                return False
            if now != basis:
                raise PersistenceError("RENEW_INPUT_STALE")
            generation = self._require_row(
                conn.execute(
                    "SELECT control_generation FROM opspilot_incidents WHERE incident_id=%s",
                    (incident_id,),
                )
            )["control_generation"]
            conn.execute(
                "UPDATE opspilot_runs SET state='cancelled',owner=NULL,lease_until=NULL WHERE incident_id=%s AND state IN ('waiting_human','blocked','paused')",
                (incident_id,),
            )
            conn.execute(
                "INSERT INTO opspilot_runs(run_id,incident_id,state,control_generation,budget_limit,deadline,versions,input) VALUES(%s,%s,'queued',%s,%s,%s,%s,%s)",
                (
                    run_id,
                    incident_id,
                    generation,
                    budget_limit,
                    deadline,
                    Jsonb(versions),
                    None if input is None else Jsonb(input),
                ),
            )
            conn.execute(
                "UPDATE opspilot_incidents SET current_run_id=%s WHERE incident_id=%s",
                (run_id, incident_id),
            )
            from opspilot.knowledge.store import mark_stale_in

            mark_stale_in(conn, incident_id, reason="run_added", source="relay")
            return True

    def has_validated_report(self, incident_id: UUID) -> bool:
        """Contract D7a: whether any Run of the incident ended on a validated
        report (published, or handed off as self-declared incomplete)."""
        with self.transaction(snapshot=True) as conn:
            return _validated_report_in(conn, incident_id)

    def conversation_floor(self, incident_id: UUID) -> int:
        """Contract B4/B7: the highest input sequence any Run of the incident
        has committed a step over (its read watermark)."""
        with self.transaction(snapshot=True) as conn:
            return _floor_in(conn, incident_id)

    def prior_replies(self, incident_id: UUID) -> list[dict[str, Any]]:
        """Contract B8: every validated reply the incident's Runs committed
        (tool steps' ``replies`` and conclusion steps' ``conclusion.replies``),
        by the message answered, then the step's database time."""
        with self.transaction(snapshot=True) as conn:
            rows = conn.execute(
                "SELECT s.response FROM opspilot_steps s JOIN opspilot_runs r ON r.run_id=s.run_id "
                "WHERE r.incident_id=%s AND s.status<>'late_result' "
                "ORDER BY s.observed_at,s.sequence,s.step_id",
                (incident_id,),
            ).fetchall()
        found: list[dict[str, Any]] = []
        for row in rows:
            response = row["response"]
            if not isinstance(response, dict):
                continue
            items = (
                (response.get("conclusion") or {}).get("replies")
                if response.get("kind") == "conclusion"
                and isinstance(response.get("conclusion"), dict)
                else response.get("replies")
            )
            if isinstance(items, list):
                found.extend(item for item in items if isinstance(item, dict))
        return sorted(
            found,
            key=lambda item: (
                item["to_input_seq"] if type(item.get("to_input_seq")) is int else 0
            ),
        )

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


_MERGE_NAMESPACE = UUID("2b4f6d55-8f0e-4c1a-9d35-245a7e0e5e01")
#: Bounds of the merge event's lists: episodes and release links named.
_MERGE_EVENT_ITEMS = 16


def _merge_event(
    conn: Connection, incident_id: UUID, row: dict[str, Any]
) -> dict[str, Any]:
    """The fixed-template event a survivor takes for a merged source (#245
    U3): the source's id, its alert fingerprints with ``startsAt`` (opening
    identity first, then attached episodes), the release observations that
    opened or joined it, its lifecycle at the merge and whether it had a
    conclusion -- never the conclusion's text. Only ``text`` and
    ``channel`` reach the model (``INPUT_CONTENT_FIELDS``); the rest is for
    people reading the input row."""
    alerts = [
        {"fingerprint": r["fingerprint"], "starts_at": r["starts_at_raw"]}
        for r in conn.execute(
            "SELECT fingerprint,starts_at_raw FROM opspilot_alert_identities WHERE incident_id=%s AND handoff_reason IS NULL",
            (incident_id,),
        ).fetchall()
    ]
    for r in conn.execute(
        "SELECT fingerprint,starts_at_raw,min(delivery_id) AS first FROM opspilot_alert_deliveries WHERE incident_id=%s AND outcome='attached' GROUP BY fingerprint,starts_at_raw ORDER BY first LIMIT %s",
        (incident_id, _MERGE_EVENT_ITEMS),
    ).fetchall():
        item = {"fingerprint": r["fingerprint"], "starts_at": r["starts_at_raw"]}
        if item not in alerts:
            alerts.append(item)
    alerts = alerts[:_MERGE_EVENT_ITEMS]
    releases = [
        str(r["observation_id"])
        for r in conn.execute(
            "SELECT observation_id FROM opspilot_release_incident_links WHERE incident_id=%s ORDER BY consumed_at,observation_id LIMIT %s",
            (incident_id, _MERGE_EVENT_ITEMS),
        ).fetchall()
    ]
    concluded = row["conclusion"] is not None
    alert_text = (
        ", ".join(f"{a['fingerprint']} (startsAt {a['starts_at']})" for a in alerts)
        or "none"
    )
    text = (
        f"Incident {incident_id} was merged into this incident by a human "
        f"decision (same target). Its alerts: {alert_text}. Its release "
        f"observations: {', '.join(releases) or 'none'}. Its lifecycle at the "
        f"merge: {row['lifecycle']}; "
        + ("it had a published conclusion" if concluded else "it had no conclusion")
        + ". Its records stay on that incident."
    )
    return {
        "channel": "merge",
        "text": text,
        "source_incident_id": str(incident_id),
        "source_alerts": alerts,
        "source_release_observations": releases,
        "source_lifecycle": row["lifecycle"],
        "source_concluded": concluded,
    }


def _floor_in(conn: Connection, incident_id: UUID) -> int:
    return int(
        _StoreBase._require_row(
            conn.execute(
                "SELECT COALESCE(max(input_watermark),0) AS floor FROM opspilot_runs WHERE incident_id=%s",
                (incident_id,),
            )
        )["floor"]
    )


def _validated_report_in(conn: Connection, incident_id: UUID) -> bool:
    """Whether any Run of the incident ended on a validated report (its
    conclusion step's ``execution`` is ``completed``: published, or handed
    off only as self-declared incomplete -- contract D7a, R1 r2)."""
    return (
        conn.execute(
            "SELECT 1 FROM opspilot_steps s JOIN opspilot_runs r ON r.run_id=s.run_id "
            "WHERE r.incident_id=%s AND s.status<>'late_result' AND s.response->>'kind'='conclusion' "
            "AND s.response->'conclusion'->>'execution'='completed' LIMIT 1",
            (incident_id,),
        ).fetchone()
        is not None
    )


def _relay_basis_in(
    conn: Connection, incident_id: UUID, scope: dict[str, Any], *, lock: bool
) -> dict[str, Any] | None:
    """The relay predicate (workbench contract R1) and, when it holds, the
    continuation's basis. ``scope`` is the caller's scope row (locked when
    ``lock``); the incident row is locked here when ``lock``."""
    row = conn.execute(
        "SELECT mode,state,lifecycle,conclusion IS NOT NULL AS concluded,current_run_id,merged_into_incident_id FROM opspilot_incidents WHERE incident_id=%s"
        + (" FOR UPDATE" if lock else ""),
        (incident_id,),
    ).fetchone()
    if row is None:
        raise PersistenceError("UNKNOWN_IDENTITY")
    if (
        row["mode"] != "automatic"
        or row["state"] in {"cancelled", "paused"}
        or row["merged_into_incident_id"] is not None
        or row["current_run_id"] is None
        or scope["global_suspended"]
        or scope["target_suspended"]
    ):
        return None
    run = conn.execute(
        "SELECT state FROM opspilot_runs WHERE run_id=%s AND incident_id=%s",
        (row["current_run_id"], incident_id),
    ).fetchone()
    # A paused Run of an incident that is neither paused nor suspended is a
    # leftover (a suspension released without re-queueing it, A10).
    if run is None or run["state"] in {"queued", "running"}:
        return None
    floor = _floor_in(conn, incident_id)
    unread = conn.execute(
        "SELECT max(sequence) AS seq FROM opspilot_inputs WHERE incident_id=%s AND kind='message' AND sequence>%s",
        (incident_id, floor),
    ).fetchone()
    if unread is None or unread["seq"] is None:
        return None
    # Human control first (PRODUCT-CONSTRAINTS): after a human cancel or
    # close, only a message admitted after that decision opens a Run; the Run
    # it opens still reads every unread message above the floor.
    if (
        conn.execute(
            "SELECT 1 FROM opspilot_inputs m WHERE m.incident_id=%s AND m.kind='message' AND m.sequence>%s "
            "AND m.received_at>COALESCE((SELECT max(c.created_at) FROM opspilot_controls c "
            "WHERE c.incident_id=%s AND c.action IN ('cancel','close')),'-infinity'::timestamptz) LIMIT 1",
            (incident_id, floor, incident_id),
        ).fetchone()
        is None
    ):
        return None
    if not row["concluded"] and not _validated_report_in(conn, incident_id):
        return None
    return {
        "current_run_id": str(row["current_run_id"]),
        "max_unread": int(unread["seq"]),
        "floor": floor,
        "mode": "reply_only"
        if row["lifecycle"] in {"resolved", "closed"}
        else "report_optional",
    }


def _register_target_in(conn: Connection, resource_uid: str) -> UUID:
    """Register ``resource_uid`` in the caller's transaction (the rows
    ``register_target`` writes); a concurrent first registration of the same
    uid is waited for and reused."""
    inserted = conn.execute(
        "INSERT INTO opspilot_targets(target_id,resource_uid) VALUES(%s,%s) ON CONFLICT (resource_uid) DO NOTHING RETURNING target_id",
        (uuid4(), resource_uid),
    ).fetchone()
    if inserted is not None:
        conn.execute(
            "INSERT INTO opspilot_target_suspensions(target_id) VALUES(%s)",
            (inserted["target_id"],),
        )
        return cast(UUID, inserted["target_id"])
    return cast(
        UUID,
        _StoreBase._require_row(
            conn.execute(
                "SELECT target_id FROM opspilot_targets WHERE resource_uid=%s",
                (resource_uid,),
            )
        )["target_id"],
    )


def append_input_in(
    conn: Connection,
    incident_id: UUID,
    input_id: UUID,
    content: dict[str, Any],
    *,
    actor: str,
    kind: str = "event",
) -> int:
    """``append_input`` inside the caller's transaction (the alert intake
    appends a re-fired episode with its delivery record, F2 PR-C; F11 PR-4
    reuses it). Locks the incident row, so a caller that also holds the scope
    or target suspension locks must take them first (R-C2). Idempotent on
    ``input_id``; a different incident, content or actor under the same id is
    ``IDENTITY_CONFLICT``. ``kind`` is ``event`` unless a human message
    (``message``, workbench contract B1) is recorded. Returns the input's
    sequence."""
    if not isinstance(content, dict):
        raise PersistenceError("INVALID_INPUT")
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
            or existing["kind"] != kind
        ):
            raise PersistenceError("IDENTITY_CONFLICT")
        return int(existing["sequence"])
    seq = _StoreBase._require_row(
        conn.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 AS sequence FROM opspilot_inputs WHERE incident_id=%s",
            (incident_id,),
        )
    )["sequence"]
    conn.execute(
        "INSERT INTO opspilot_inputs(input_id,incident_id,sequence,kind,content,actor,control_generation) VALUES(%s,%s,%s,%s,%s,%s,%s)",
        (
            input_id,
            incident_id,
            seq,
            kind,
            Jsonb(content),
            actor,
            row["control_generation"],
        ),
    )
    # M1-03 D18: a new input moves the input watermark.
    from opspilot.knowledge.store import mark_stale_in

    mark_stale_in(conn, incident_id, reason="input_added", source="append_input")
    return int(seq)
