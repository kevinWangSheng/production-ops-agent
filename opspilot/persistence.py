"""PostgreSQL business-state authority for the first durable M1 slice."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


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
    def _db_now(conn):
        return conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]

    @contextmanager
    def transaction(self):
        try:
            with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
                conn.execute("SET LOCAL statement_timeout='5000ms'")
                conn.execute("SET LOCAL lock_timeout='4000ms'")
                yield conn
        except psycopg.Error as exc:
            raise PersistenceError("STORAGE_UNAVAILABLE") from exc

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
                "INSERT INTO opspilot_incidents(incident_id,intake_key,state,lifecycle,current_run_id) VALUES(%s,%s,'queued','open',%s) ON CONFLICT (intake_key) DO NOTHING RETURNING incident_id",
                (incident_id, intake_key, run_id),
            ).fetchone()
            existing = conn.execute(
                "SELECT incident_id,current_run_id FROM opspilot_incidents WHERE intake_key=%s",
                (intake_key,),
            ).fetchone()
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
                "SELECT i.control_generation,i.state AS incident_state,r.* FROM opspilot_incidents i JOIN opspilot_runs r ON r.incident_id=i.incident_id WHERE i.incident_id=%s AND r.run_id=%s FOR UPDATE",
                (incident_id, run_id),
            ).fetchone()
            if not row:
                raise PersistenceError("UNKNOWN_IDENTITY")
            active_lease = (
                row["state"] == "running"
                and row["lease_until"] is not None
                and row["lease_until"] > self._db_now(conn)
            )
            if active_lease:
                raise PersistenceError("LEASE_ACTIVE")
            if row["versions"] != versions:
                conn.execute(
                    "UPDATE opspilot_runs SET state='blocked' WHERE run_id=%s",
                    (run_id,),
                )
                incompatible = True
            elif row["incident_state"] in {"completed", "cancelled"}:
                raise PersistenceError("CONTROL_DENIED")
            elif row["state"] not in ("queued", "running"):
                raise PersistenceError("CONTROL_DENIED")
            elif (
                row["state"] == "running"
                and row["lease_until"] is not None
                and row["lease_until"] > self._db_now(conn)
            ):
                raise PersistenceError("LEASE_ACTIVE")
            else:
                epoch = int(row["epoch"]) + 1
                conn.execute(
                    "UPDATE opspilot_runs SET state='running',owner=%s,epoch=%s,control_generation=%s,lease_until=clock_timestamp()+make_interval(secs=>%s) WHERE run_id=%s",
                    (owner, epoch, row["control_generation"], lease_seconds, run_id),
                )
                lease = Lease(
                    incident_id, run_id, owner, epoch, int(row["control_generation"])
                )
        if incompatible:
            raise PersistenceError("INCOMPATIBLE_STATE")
        assert lease is not None
        return lease

    def reserve_budget(self, lease: Lease, reservation_id: UUID, amount: int) -> None:
        if amount <= 0:
            raise PersistenceError("INVALID_INPUT")
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT r.*,i.control_generation FROM opspilot_runs r JOIN opspilot_incidents i ON i.incident_id=r.incident_id WHERE r.run_id=%s FOR UPDATE",
                (lease.run_id,),
            ).fetchone()
            if (
                not row
                or row["owner"] != lease.owner
                or row["epoch"] != lease.epoch
                or row["control_generation"] != lease.control_generation
                or (
                    row["lease_until"] is not None
                    and row["lease_until"] <= self._db_now(conn)
                )
                or row["deadline"] <= self._db_now(conn)
            ):
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
            row = conn.execute(
                "SELECT r.*,i.control_generation FROM opspilot_runs r JOIN opspilot_incidents i ON i.incident_id=r.incident_id WHERE r.run_id=%s FOR UPDATE",
                (lease.run_id,),
            ).fetchone()
            if (
                not row
                or row["owner"] != lease.owner
                or row["epoch"] != lease.epoch
                or row["control_generation"] != lease.control_generation
                or (
                    row["lease_until"] is not None
                    and row["lease_until"] <= self._db_now(conn)
                )
                or row["deadline"] <= self._db_now(conn)
            ):
                raise PersistenceError("CONTROL_DENIED")
            existing = conn.execute(
                "SELECT step_id FROM opspilot_steps WHERE run_id=%s AND logical_key=%s",
                (lease.run_id, logical_key),
            ).fetchone()
            if existing:
                return existing["step_id"]
            step_id = uuid4()
            sequence = conn.execute(
                "SELECT COALESCE(MAX(sequence), -1) + 1 AS next_sequence FROM opspilot_steps WHERE run_id=%s",
                (lease.run_id,),
            ).fetchone()["next_sequence"]
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
            row = conn.execute(
                "SELECT r.*,i.control_generation,s.tool_results FROM opspilot_runs r JOIN opspilot_incidents i ON i.incident_id=r.incident_id JOIN opspilot_steps s ON s.run_id=r.run_id WHERE r.run_id=%s AND s.step_id=%s FOR UPDATE",
                (lease.run_id, step_id),
            ).fetchone()
            if (
                not row
                or row["owner"] != lease.owner
                or row["epoch"] != lease.epoch
                or row["control_generation"] != lease.control_generation
                or (
                    row["lease_until"] is not None
                    and row["lease_until"] <= self._db_now(conn)
                )
                or row["deadline"] <= self._db_now(conn)
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
                "SELECT control_generation,state,conclusion FROM opspilot_incidents WHERE incident_id=%s FOR UPDATE",
                (incident_id,),
            ).fetchone()
            if not row or row["control_generation"] != expected_generation:
                raise PersistenceError("CONTROL_CONFLICT")
            if action not in {"cancel", "pause", "resume", "follow_up", "correct"}:
                raise PersistenceError("INVALID_INPUT")
            if action in {
                "cancel",
                "pause",
                "resume",
                "follow_up",
                "correct",
            } and (
                row.get("state")
                in {
                    "cancelled",
                    "completed",
                }
                or row.get("conclusion") is not None
            ):
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
                    "UPDATE opspilot_runs SET state='cancelled',owner=NULL,lease_until=NULL,control_generation=%s WHERE incident_id=%s AND state IN ('queued','running')",
                    (nxt, incident_id),
                )
            elif action == "pause":
                conn.execute(
                    "UPDATE opspilot_runs SET state='queued',owner=NULL,lease_until=NULL,control_generation=%s WHERE incident_id=%s AND state='running'",
                    (nxt, incident_id),
                )
            elif action == "resume":
                conn.execute(
                    "UPDATE opspilot_runs SET state='queued',owner=NULL,lease_until=NULL,control_generation=%s WHERE incident_id=%s AND state IN ('queued','paused','running')",
                    (nxt, incident_id),
                )
            elif action in {"follow_up", "correct"}:
                conn.execute(
                    "UPDATE opspilot_runs SET state='queued',owner=NULL,lease_until=NULL,control_generation=%s WHERE incident_id=%s AND state='running'",
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
                "SELECT i.*,r.owner,r.epoch,r.lease_until,r.deadline,r.state AS run_state,r.control_generation AS run_generation FROM opspilot_incidents i JOIN opspilot_runs r ON r.run_id=i.current_run_id WHERE i.incident_id=%s FOR UPDATE",
                (lease.incident_id,),
            ).fetchone()
            if (
                not row
                or row["current_run_id"] != lease.run_id
                or row["owner"] != lease.owner
                or row["epoch"] != lease.epoch
                or row["control_generation"] != lease.control_generation
                or row["run_state"] != "running"
                or row["state"] in {"completed", "cancelled"}
                or row["lease_until"] is None
                or row["lease_until"] <= self._db_now(conn)
                or row["deadline"] <= self._db_now(conn)
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
                "SELECT response,status FROM opspilot_steps WHERE step_id=%s AND run_id=%s FOR UPDATE",
                (step_id, lease.run_id),
            ).fetchone()
            if (
                not final_step
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
        with self.transaction() as conn:
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
                "pending_tools": [
                    {"step_id": step["step_id"], "ordinal": ordinal}
                    for step in steps
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
