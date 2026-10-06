"""Incidents: intake, targets, claimable listing and recovery rebuild."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from opspilot.persistence.base import PersistenceError, _StoreBase
from opspilot.persistence.steps import _completed_tool_ordinals, _tool_plan


class _IncidentOps(_StoreBase):
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

    def claimable_incidents(self, *, limit: int = 20) -> tuple[UUID, ...]:
        """Incidents whose current Run a worker may try to claim right now.

        A read-only hint for the polling worker, never an authority:
        ``claim()`` re-checks everything under row locks, so a stale row here
        costs one refused claim and nothing else. Listed: an open incident
        whose current Run is ``queued``, or ``running`` with a lapsed lease
        (a killed worker's), and not yet overdue; parked and blocked rows are
        a human's, and an overdue row (``running`` or ``queued``) is the
        sweep's (ADR-0005 decision 2), which every worker poll runs first.
        Oldest deadline first, like the sweep, so a worker that falls behind
        serves the Run that will time out soonest.
        """
        if type(limit) is not int or limit < 1:
            raise PersistenceError("INVALID_INPUT")
        with self.transaction(snapshot=True) as conn:
            rows = conn.execute(
                "SELECT i.incident_id FROM opspilot_incidents i JOIN opspilot_runs r ON r.run_id=i.current_run_id "
                "WHERE i.state NOT IN ('paused','cancelled','completed') AND i.conclusion IS NULL "
                "AND r.state IN ('queued','running') AND r.deadline>clock_timestamp() "
                "AND (r.state='queued' OR r.lease_until IS NULL OR r.lease_until<=clock_timestamp()) "
                "ORDER BY r.deadline, i.incident_id LIMIT %s",
                (limit,),
            ).fetchall()
        return tuple(row["incident_id"] for row in rows)

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
