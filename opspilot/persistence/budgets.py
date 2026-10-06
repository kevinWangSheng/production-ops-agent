"""Budgets: reservations, settlement, tool charges and usage."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from opspilot.persistence.base import Lease, PersistenceError, _StoreBase

# 预留结算的两种去向：列名由结算结果决定，不由调用方拼 SQL。
_SETTLEMENTS: dict[str, str] = {"spent": "budget_spent", "unknown": "budget_unknown"}


class _BudgetOps(_StoreBase):
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
        whole of it is added to the Run's cumulative tool time before the
        dispatch settles (C3 section 13: 预算在 PostgreSQL 原子预留和结算，未知
        费用保持占用). Settling releases the reservation and records what the
        read actually cost, which may be less than reserved and, for a
        transport that overran its bound, more.

        ``dispatch_id`` is the key, not ``operation_id``, and it is required
        rather than derived here: ``operation_id`` is stable by construction
        (technical plan section 7 -- step id plus tool ordinal), so keying on
        it silently collapsed *two real reads* of the same tool call into one
        charge whenever a duplicate delivery or a same-epoch retry occurred --
        the Run was charged one operation and ``max(seconds)`` instead of the
        sum (bot review finding). Section 13 settles the semantics -- "重试
        计入次数和费用" -- and section 4 explicitly declines to promise
        exactly-once external queries, so the second dispatch is charged, not
        refused. A new attempt that re-dispatches an uncommitted operation
        likewise issues a real second query and is charged again; the per-Run
        total only ever grows. Fenced by the lease like every other write path.

        M1-01 (2026-09-28 user decision,
        docs/tasks/2026-09-28-m1-01-loop-limits.md, L2): counting a *new*
        operation is never refused on ``max_operations``/``max_tool_seconds``
        any more -- the loop's single anti-loop ceiling is the model-request
        count (L1), not a separate per-Run tool-call-count or cumulative
        tool-time ceiling. ``max_operations``/``max_tool_seconds`` stay
        **required** parameters (this module still does not import from
        ``opspilot.tools.executor``, so every caller supplies its own values)
        purely so the input-validation shape below is unchanged for existing
        callers; they no longer gate the UPDATE below and a caller may pass
        any positive value. ``tool_operations_used``/``tool_seconds_used``
        keep accumulating without limit -- accounting and audit, not a cap.
        The row lock (``FOR UPDATE`` above) still serializes concurrent
        charges against the same Run, so the accounting itself stays race-free.
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
            # 虚高总量——记账仍然保留（M1-01，2026-09-28 用户决定，L2 之后不再
            # 是上限判定的输入，但仍是审计事实），一笔从未真正花掉的耗时永远赖在
            # 账上依旧是账目错误（bot review 发现，过时表述已按 L2 更新）。结算
            # 只是把已知的实际耗时写回本租约自己建立的那一行：它不新建预留、不
            # 采纳任何结果，人工决定仍由执行器取回后的 control 复读上报并按
            # history-only 登记。
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
                # M1-01 (2026-09-28 user decision, L2): no ``max_operations``/
                # ``max_tool_seconds`` condition in the WHERE clause any more --
                # the row is already locked (``FOR UPDATE`` above) and confirmed
                # to exist, so this UPDATE always matches it; it only counts and
                # accumulates, never refuses.
                conn.execute(
                    "UPDATE opspilot_runs SET tool_operations_used=tool_operations_used+1,tool_seconds_used=tool_seconds_used+%s WHERE run_id=%s",
                    (float(seconds), lease.run_id),
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
