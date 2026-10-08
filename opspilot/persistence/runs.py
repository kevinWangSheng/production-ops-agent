"""Runs: creation, claim and lease lifecycle, parking and deadline sweep."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from opspilot.persistence.base import Connection, Lease, PersistenceError, _StoreBase


class _RunOps(_StoreBase):
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
        payload: dict[str, Any] | None = None,
    ) -> int:
        """Continue a cancelled incident with a fresh Run and control generation.

        ``payload`` goes to the audit row as in ``control()`` (the caller's
        request key, #128); the Run takes nothing from it.
        """
        if type(expected_generation) is not int or expected_generation < 0:
            raise PersistenceError("INVALID_INPUT")
        if payload is not None and not isinstance(payload, dict):
            raise PersistenceError("INVALID_INPUT")
        with self.transaction() as conn:
            scope = self._lock_scope(conn, incident_id)
            row = conn.execute(
                "SELECT state,mode,control_generation,current_run_id FROM opspilot_incidents WHERE incident_id=%s FOR UPDATE",
                (incident_id,),
            ).fetchone()
            if not row:
                raise PersistenceError("UNKNOWN_IDENTITY")
            if row["mode"] == "human_owned":
                # A new Run is automatic investigation; human ownership has no
                # way back to automatic in the domain model (C3 §10, #121).
                raise PersistenceError("ILLEGAL_TRANSITION")
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
            # A new Run never inherits an observation authorization (C3 §10
            # "更换 Run 不默认继承旧观察授权"): revoke before the lifecycle
            # goes back to open, in this transaction.
            from opspilot.observation.revocation import revoke_authorized_sessions

            revoke_authorized_sessions(conn, incident_id)
            conn.execute(
                "UPDATE opspilot_incidents SET state=%s,lifecycle='open',control_generation=%s,current_run_id=%s,conclusion=NULL WHERE incident_id=%s",
                (next_state, nxt, run_id, incident_id),
            )
            conn.execute(
                "INSERT INTO opspilot_controls(audit_id,incident_id,action,expected_generation,resulting_generation,actor,payload) VALUES(%s,%s,'new_run',%s,%s,%s,%s)",
                (
                    uuid4(),
                    incident_id,
                    nxt - 1,
                    nxt,
                    actor,
                    Jsonb(payload) if payload is not None else None,
                ),
            )
            return nxt

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
                "SELECT i.control_generation AS incident_generation,i.state AS incident_state,i.mode AS incident_mode,r.state AS run_state,r.epoch,r.lease_until,r.deadline,r.versions,sc.global_suspended,sc.global_generation,COALESCE(ts.suspended,false) AS target_suspended,COALESCE(ts.generation,0) AS target_generation FROM opspilot_incidents i JOIN opspilot_runs r ON r.incident_id=i.incident_id JOIN opspilot_scope_controls sc ON sc.scope_id=1 LEFT JOIN opspilot_target_suspensions ts ON ts.target_id=i.target_id WHERE i.incident_id=%s AND r.run_id=%s FOR UPDATE OF i,r",
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
            # human_owned: the Agent does not investigate (C3 §10, #121).
            if row["incident_mode"] == "human_owned":
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
    def _park(
        conn: Connection, run_id: UUID, *, from_states: tuple[str, ...] = ("running",)
    ) -> bool:
        """The one ``-> waiting_human`` write (``hand_off`` and the sweep).

        ``hand_off`` parks a leased, hence ``running``, Run; the sweep also
        parks an overdue ``queued`` one (never claimed before its wall).
        """
        parked = conn.execute(
            "UPDATE opspilot_runs SET state='waiting_human',owner=NULL,lease_until=NULL WHERE run_id=%s AND state=ANY(%s) RETURNING run_id",
            (run_id, list(from_states)),
        ).fetchone()
        return parked is not None

    def sweep_expired_runs_with_generations(
        self, *, incident_id: UUID | None = None, limit: int = 100
    ) -> tuple[tuple[UUID, UUID, int], ...]:
        """Park every ``running`` or ``queued`` Run whose ``deadline`` has passed
        (ADR-0005 §2).

        A Run past its deadline can never settle itself: the deadline fences
        every worker write and every claim, so without this sweep a
        ``running`` row stays ``running`` for ever and a ``queued`` row that
        no worker reached before its wall (worker down, backlog) stays
        ``queued`` for ever with no event (bot review, PR #52). Each overdue
        Run is parked exactly as ``hand_off`` parks a loop handoff
        (``_park``): ``waiting_human``, lease released, incident open,
        conclusion untouched; the reason (``DEADLINE_EXCEEDED``) is the
        caller's to announce. A human then continues with follow_up/correct
        (a renewed Run, #47) or cancel + new_run.

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
                "SELECT incident_id,run_id FROM opspilot_runs WHERE state IN ('queued','running') AND deadline<=clock_timestamp() AND (%s::uuid IS NULL OR incident_id=%s) ORDER BY deadline LIMIT %s",
                (incident_id, incident_id, limit),
            ).fetchall()
        parked: list[tuple[UUID, UUID, int]] = []
        for candidate in candidates:
            with self.transaction() as conn:
                conn.execute(
                    "SELECT 1 FROM opspilot_incidents WHERE incident_id=%s FOR UPDATE",
                    (candidate["incident_id"],),
                )
                row = conn.execute(
                    "SELECT state,deadline,(SELECT control_generation FROM opspilot_incidents WHERE incident_id=%s) AS control_generation FROM opspilot_runs WHERE run_id=%s FOR UPDATE",
                    (candidate["incident_id"], candidate["run_id"]),
                ).fetchone()
                if (
                    row is None
                    or row["state"] not in ("queued", "running")
                    or row["deadline"] > self._db_now(conn)
                ):
                    continue
                if self._park(
                    conn, candidate["run_id"], from_states=("queued", "running")
                ):
                    parked.append(
                        (
                            candidate["incident_id"],
                            candidate["run_id"],
                            int(row["control_generation"]),
                        )
                    )
        return tuple(parked)

    def sweep_expired_runs(
        self, *, incident_id: UUID | None = None, limit: int = 100
    ) -> tuple[tuple[UUID, UUID], ...]:
        return tuple(
            (subject_id, run_id)
            for subject_id, run_id, _ in self.sweep_expired_runs_with_generations(
                incident_id=incident_id, limit=limit
            )
        )

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
