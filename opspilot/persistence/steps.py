"""Step commits: model steps, tool checkpoints, rounds and final publish."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from opspilot.persistence.base import Connection, Lease, PersistenceError, _StoreBase

# 迟到历史占用 (run_id, logical_key) 唯一键。业务步骤不得使用此外缀，否则会
# 把迟到结果挤掉或把 late_result 行当成已提交步骤返回。
_LATE_RESULT_KEY_PREFIX = "late_result:"


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


class _StepOps(_StoreBase):
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
        sequence = _StoreBase._require_row(
            conn.execute(
                "SELECT COALESCE(MAX(sequence), -1) + 1 AS next_sequence FROM opspilot_steps WHERE run_id=%s",
                (run_id,),
            )
        )["next_sequence"]
        conn.execute(
            "INSERT INTO opspilot_steps(step_id,run_id,sequence,logical_key,status,response,control_generation,observed_at) VALUES(%s,%s,%s,%s,'late_result',%s,%s,clock_timestamp()) ON CONFLICT (run_id, logical_key) DO NOTHING",
            (uuid4(), run_id, sequence, logical_key, Jsonb(payload), generation),
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
