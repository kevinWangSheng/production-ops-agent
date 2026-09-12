"""Bounded M0 PG reconstruction seam. No HTTP client, scheduler or credentials loader.

Private response/input columns are protocol state, never diagnostic output.
Only ``summary`` is an export surface. Callers must not print rebuild output.
"""

import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import get_args
from uuid import UUID, uuid4, uuid5

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .budget import PostgresBudget
from .contracts import BudgetError, RequestIdentity, RunContext
from .outcomes_v3 import ControlAction, ControlEvent
from .protocol import ProtocolError, continuation


def _validated_tool_calls(assistant):
    """Fail closed on malformed provider containers before iterating calls."""
    if not isinstance(assistant, dict):
        raise ProtocolError("ASSISTANT_INVALID")
    calls = assistant.get("tool_calls", [])
    if not isinstance(calls, list) or any(
        not isinstance(call, dict)
        or not isinstance(call.get("id"), str)
        or not call["id"]
        for call in calls
    ):
        raise ProtocolError("TOOL_CALLS_INVALID")
    return calls


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


@dataclass(frozen=True)
class Fence:
    subject: UUID
    run: RunContext
    generation: int
    owner: UUID
    epoch: int


class StepStore:
    def __init__(self, ledger: PostgresBudget):
        self.ledger = ledger

    def install(self):
        with self.ledger._transaction() as conn:
            conn.execute(Path(__file__).with_suffix(".sql").read_text())

    @staticmethod
    def _lock(conn, subject, *, session=False):
        # One cross-process lock shared by control COMMIT and physical initiation.
        fn = "pg_advisory_lock" if session else "pg_advisory_xact_lock"
        conn.execute(f"SELECT {fn}(hashtextextended(%s,0))", (str(subject),))

    @staticmethod
    def _validate_versions(versions):
        if (
            not isinstance(versions, dict)
            or not versions
            or any(
                not isinstance(key, str)
                or not key
                or not isinstance(value, str)
                or not value
                for key, value in versions.items()
            )
        ):
            raise BudgetError("INVALID_INPUT")

    def accept(self, run, key, initial, versions, *, target=None):
        """``target`` is the optional per-target pause scope key, never a credential."""
        self._validate_versions(versions)
        if not key or (
            target is not None and (not isinstance(target, str) or not target)
        ):
            raise BudgetError("INVALID_INPUT")
        with self.ledger._transaction() as conn:
            exp = self.ledger._experiment(conn, run.experiment_id)
            prior = conn.execute(
                "SELECT * FROM m0_v3_subject WHERE experiment_id=%s AND intake_key=%s",
                (run.experiment_id, key),
            ).fetchone()
            if prior is not None:
                if (
                    prior["intake_hash"] != digest(initial)
                    or prior["versions"] != versions
                ):
                    raise BudgetError("IDENTITY_CONFLICT")
                return prior["id"]
            if run.deadline > exp["deadline"]:
                raise BudgetError("IDENTITY_CONFLICT")
            conn.execute(
                "INSERT INTO m0_runs VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (run.run_id, run.experiment_id, run.provider, run.deadline),
            )
            self.ledger._run(conn, run)
            row = conn.execute(
                "INSERT INTO m0_v3_subject(id,experiment_id,intake_key,intake_hash,input,current_run,versions,target_key) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(experiment_id,intake_key) DO NOTHING RETURNING id",
                (
                    uuid4(),
                    run.experiment_id,
                    key,
                    digest(initial),
                    Jsonb(initial),
                    run.run_id,
                    Jsonb(versions),
                    target,
                ),
            ).fetchone()
            if row:
                result = row["id"]
            else:
                prior = conn.execute(
                    "SELECT * FROM m0_v3_subject WHERE experiment_id=%s AND intake_key=%s",
                    (run.experiment_id, key),
                ).fetchone()
                if (
                    prior["intake_hash"] != digest(initial)
                    or prior["versions"] != versions
                ):
                    raise BudgetError("IDENTITY_CONFLICT")
                result = prior["id"]
            conn.execute(
                "INSERT INTO m0_v3_run_input(run_id,subject,input,versions) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (run.run_id, result, Jsonb(initial), Jsonb(versions)),
            )
        return result

    def new_run(self, subject, expected_generation, run, business_input, versions):
        """Explicit fresh Run; caller supplies permitted business facts only.

        Never copies old protocol columns. Old run input/steps/budget remain.
        """
        self._validate_versions(versions)
        paused = None
        with self.ledger._transaction() as conn:
            self._lock(conn, subject)
            row = conn.execute(
                "SELECT * FROM m0_v3_subject WHERE id=%s FOR UPDATE", (subject,)
            ).fetchone()
            if (
                row is None
                or row["generation"] != expected_generation
                or row["experiment_id"] != run.experiment_id
                or row["current_run"] == run.run_id
            ):
                raise BudgetError("CONTROL_CONFLICT")
            if self._pause_active(conn, row["target_key"]):
                paused = row["generation"]
        if paused is not None:
            # Resume never implicitly continues; a new Run is only admitted
            # once no covering pause is active.
            self._deny(subject, "PAUSED", paused)
        with self.ledger._transaction() as conn:
            self._lock(conn, subject)
            row = conn.execute(
                "SELECT * FROM m0_v3_subject WHERE id=%s FOR UPDATE", (subject,)
            ).fetchone()
            if (
                row is None
                or row["generation"] != expected_generation
                or row["experiment_id"] != run.experiment_id
                or row["current_run"] == run.run_id
                or self._pause_active(conn, row["target_key"])
            ):
                raise BudgetError("CONTROL_CONFLICT")
            exp = self.ledger._experiment(conn, run.experiment_id)
            if run.deadline > exp["deadline"]:
                raise BudgetError("IDENTITY_CONFLICT")
            if conn.execute(
                "SELECT id FROM m0_runs WHERE id=%s", (run.run_id,)
            ).fetchone():
                raise BudgetError("IDENTITY_CONFLICT")
            conn.execute(
                "INSERT INTO m0_runs VALUES(%s,%s,%s,%s)",
                (run.run_id, run.experiment_id, run.provider, run.deadline),
            )
            conn.execute(
                "INSERT INTO m0_v3_run_input VALUES(%s,%s,%s,%s)",
                (run.run_id, subject, Jsonb(business_input), Jsonb(versions)),
            )
            conn.execute(
                "UPDATE m0_v3_subject SET current_run=%s,versions=%s,generation=generation+1,state='running',owner=NULL,lease_until=NULL,final=NULL WHERE id=%s",
                (run.run_id, Jsonb(versions), subject),
            )
            self._audit(conn, subject, "new_run", True, expected_generation + 1)
        return expected_generation + 1

    def claim(self, subject, run, owner, versions, lease_seconds=30):
        self._validate_versions(versions)
        if not isinstance(owner, UUID) or not 0 < lease_seconds <= 300:
            raise BudgetError("INVALID_INPUT")
        incompatible = False
        paused = None
        with self.ledger._transaction() as conn:
            self._lock(conn, subject)
            row = conn.execute(
                "SELECT *,clock_timestamp() AS now FROM m0_v3_subject WHERE id=%s FOR UPDATE",
                (subject,),
            ).fetchone()
            if (
                row is None
                or row["current_run"] != run.run_id
                or row["experiment_id"] != run.experiment_id
            ):
                raise BudgetError("IDENTITY_CONFLICT")
            self.ledger._run(conn, run)
            if row["state"] != "running" or row["now"] >= run.deadline:
                raise BudgetError("CONTROL_DENIED")
            if row["owner"] is not None and row["lease_until"] > row["now"]:
                raise BudgetError("LEASE_ACTIVE")
            if self._pause_active(conn, row["target_key"]):
                paused = row["generation"]
            elif row["versions"] != versions:
                conn.execute(
                    "UPDATE m0_v3_subject SET state='blocked' WHERE id=%s", (subject,)
                )
                self._audit(
                    conn, subject, "INCOMPATIBLE_STATE", False, row["generation"]
                )
                incompatible = True
            else:
                epoch = row["epoch"] + 1
                conn.execute(
                    "UPDATE m0_v3_subject SET owner=%s,epoch=%s,lease_until=%s WHERE id=%s",
                    (
                        owner,
                        epoch,
                        min(
                            run.deadline, row["now"] + timedelta(seconds=lease_seconds)
                        ),
                        subject,
                    ),
                )
                fence = Fence(subject, run, row["generation"], owner, epoch)
        if paused is not None:
            self._deny(subject, "PAUSED", paused)
        if incompatible:
            raise BudgetError("INCOMPATIBLE_STATE")
        return fence

    @staticmethod
    def _pause_active(conn, target_key):
        """True when a global pause or a pause for this subject's target is active."""
        return (
            conn.execute(
                "SELECT 1 FROM m0_v3_pause WHERE active AND (scope='global' OR (scope='target' AND target_key=%s))",
                (target_key or "",),
            ).fetchone()
            is not None
        )

    def _deny(self, subject, event, generation):
        """Persist an audited denial outside the rolled-back transaction, then raise."""
        with self.ledger._transaction() as conn:
            self._audit(conn, subject, event, False, generation)
        raise BudgetError(event)

    def _pause_denied(self, conn, fence):
        """Audit a PAUSED denial on a session-locked autocommit-capable connection."""
        row = conn.execute(
            "SELECT target_key,generation FROM m0_v3_subject WHERE id=%s",
            (fence.subject,),
        ).fetchone()
        if row is None or not self._pause_active(conn, row["target_key"]):
            # End the implicit read transaction so autocommit can be toggled later.
            conn.rollback()
            return False
        self._audit(conn, fence.subject, "PAUSED", False, row["generation"])
        conn.commit()
        conn.execute(
            "SELECT pg_advisory_unlock(hashtextextended(%s,0))", (str(fence.subject),)
        )
        conn.commit()
        return True

    def _scope_control(self, scope, target, action, reason):
        if scope not in {"global", "target"}:
            raise BudgetError("INVALID_INPUT")
        if scope == "target":
            if not isinstance(target, str) or not target:
                raise BudgetError("INVALID_INPUT")
        elif target is not None:
            raise BudgetError("INVALID_INPUT")
        if reason is not None and (not isinstance(reason, str) or len(reason) > 200):
            raise BudgetError("INVALID_INPUT")
        key = target or ""
        with self.ledger._transaction() as conn:
            # Serialize scope control against every subject-level control lock.
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('m0-pause',0))")
            conn.execute(
                "INSERT INTO m0_v3_pause(scope,target_key) VALUES(%s,%s) ON CONFLICT DO NOTHING",
                (scope, key),
            )
            state = conn.execute(
                "SELECT * FROM m0_v3_pause WHERE scope=%s AND target_key=%s FOR UPDATE",
                (scope, key),
            ).fetchone()
            if state["active"] == (action == "pause"):
                raise BudgetError("CONTROL_CONFLICT")
            version = state["version"] + 1
            conn.execute(
                "UPDATE m0_v3_pause SET active=%s,version=%s,updated_at=clock_timestamp() WHERE scope=%s AND target_key=%s",
                (action == "pause", version, scope, key),
            )
            conn.execute(
                "INSERT INTO m0_v3_pause_event(scope,target_key,action,version,reason) VALUES(%s,%s,%s,%s,%s)",
                (scope, key, action, version, reason),
            )
            covered = conn.execute(
                "SELECT id FROM m0_v3_subject WHERE state=%s AND (%s='global' OR target_key=%s) ORDER BY id",
                ("running" if action == "pause" else "paused", scope, key),
            ).fetchall()
            affected = []
            for item in covered:
                self._lock(conn, item["id"])
                if action == "pause":
                    # Old Runs stop being claimable; their steps, evidence, ledger
                    # reservations and deadlines remain untouched history.
                    row = conn.execute(
                        "UPDATE m0_v3_subject SET generation=generation+1,state='paused',owner=NULL,lease_until=NULL WHERE id=%s AND state='running' RETURNING generation",
                        (item["id"],),
                    ).fetchone()
                    if row is None:
                        continue
                    conn.execute(
                        "INSERT INTO m0_v3_control VALUES(%s,%s,'pause',%s)",
                        (item["id"], row["generation"], Jsonb({"scope": scope})),
                    )
                    self._audit(conn, item["id"], "pause", True, row["generation"])
                    affected.append(str(item["id"]))
                else:
                    row = conn.execute(
                        "SELECT generation FROM m0_v3_subject WHERE id=%s AND state='paused'",
                        (item["id"],),
                    ).fetchone()
                    if row is None:
                        continue
                    # No state or generation change: resume acknowledges only.
                    self._audit(conn, item["id"], "resume", True, row["generation"])
                    affected.append(str(item["id"]))
        return {"scope": scope, "version": version, "subjects": affected}

    def pause(self, scope, *, target=None, reason=None):
        """Global or per-target pause. New queries/dispatches/claims are denied.

        Running subjects under the scope move to ``paused`` with a new generation;
        nothing already committed is deleted or re-executed.
        """
        return self._scope_control(scope, target, "pause", reason)

    def resume(self, scope, *, target=None):
        """Lift a pause. Paused subjects stay paused until an explicit ``new_run``.

        Deadlines and ledger budgets are never reset by resume.
        """
        return self._scope_control(scope, target, "resume", None)

    def pause_snapshot(self):
        """Safe export: scope states and events only, no subject payloads."""
        with self.ledger._transaction() as conn:
            states = conn.execute(
                "SELECT scope,target_key,active,version FROM m0_v3_pause ORDER BY scope,target_key"
            ).fetchall()
            events = conn.execute(
                "SELECT scope,target_key,action,version,created_at AS at FROM m0_v3_pause_event ORDER BY sequence"
            ).fetchall()
        return {
            "states": states,
            "events": [
                {**event, "at": event["at"].astimezone(timezone.utc).isoformat()}
                for event in events
            ],
        }

    def authorize_observer(
        self, subject, run, *, window_start, window_end, query_limit
    ):
        """Independent observer authorization: own Run, experiment ledger and window.

        It never becomes an investigation fence and cannot continue a paused,
        cancelled or blocked investigation Run.
        """
        if (
            not isinstance(run, RunContext)
            or type(query_limit) is not int
            or query_limit <= 0
            or not all(
                isinstance(v, datetime) and v.tzinfo is not None
                for v in (window_start, window_end)
            )
            or window_end <= window_start
        ):
            raise BudgetError("INVALID_INPUT")
        observer = uuid4()
        with self.ledger._transaction() as conn:
            self._lock(conn, subject)
            row = conn.execute(
                "SELECT * FROM m0_v3_subject WHERE id=%s FOR UPDATE", (subject,)
            ).fetchone()
            if row is None:
                raise BudgetError("UNKNOWN_IDENTITY")
            if row["experiment_id"] == run.experiment_id:
                # Budget/query window must not be borrowed from the investigation.
                raise BudgetError("AUTHORIZATION_SCOPE_CONFLICT")
            exp = self.ledger._experiment(conn, run.experiment_id)
            if run.deadline > exp["deadline"]:
                raise BudgetError("IDENTITY_CONFLICT")
            if conn.execute(
                "SELECT id FROM m0_runs WHERE id=%s", (run.run_id,)
            ).fetchone():
                raise BudgetError("IDENTITY_CONFLICT")
            conn.execute(
                "INSERT INTO m0_runs VALUES(%s,%s,%s,%s)",
                (run.run_id, run.experiment_id, run.provider, run.deadline),
            )
            conn.execute(
                "INSERT INTO m0_v3_observer VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (
                    observer,
                    subject,
                    run.run_id,
                    run.experiment_id,
                    window_start,
                    window_end,
                    query_limit,
                ),
            )
            self._audit(conn, subject, "observer_authorized", True, row["generation"])
        return observer

    def observe(self, subject, run, attempt_id, starter, *, now=None):
        """One bounded read-only observer query under its own authorization.

        Denied while any covering pause is active, outside the window, or past
        the observer's own query limit. Never touches investigation state.
        """
        if not isinstance(run, RunContext) or not isinstance(attempt_id, UUID):
            raise BudgetError("INVALID_INPUT")
        if now is not None and (not isinstance(now, datetime) or now.tzinfo is None):
            raise BudgetError("INVALID_INPUT")
        denial = None
        with self.ledger._transaction() as conn:
            self._lock(conn, subject)
            row = conn.execute(
                "SELECT o.*,s.target_key,s.generation AS subject_generation,clock_timestamp() AS now "
                "FROM m0_v3_observer o JOIN m0_v3_subject s ON s.id=o.subject WHERE o.subject=%s AND o.run_id=%s FOR UPDATE OF o",
                (subject, run.run_id),
            ).fetchone()
            if row is None:
                raise BudgetError("UNKNOWN_IDENTITY")
            self.ledger._run(conn, run)
            moment = now or row["now"]
            if self._pause_active(conn, row["target_key"]):
                denial = "PAUSED"
            elif not row["window_start"] <= moment < row["window_end"]:
                denial = "OBSERVATION_WINDOW_CLOSED"
            elif moment >= run.deadline:
                denial = "DEADLINE_EXCEEDED"
            else:
                count = conn.execute(
                    "SELECT count(*) AS n FROM m0_v3_observer_attempt WHERE observer=%s",
                    (row["id"],),
                ).fetchone()["n"]
                if count >= row["query_limit"]:
                    denial = "QUERY_LIMIT"
            if denial is None:
                inserted = conn.execute(
                    "INSERT INTO m0_v3_observer_attempt(id,observer) VALUES(%s,%s) ON CONFLICT DO NOTHING RETURNING id",
                    (attempt_id, row["id"]),
                ).fetchone()
                if inserted is None:
                    raise BudgetError("REQUEST_ALREADY_RESERVED")
                self._audit(
                    conn, subject, "observer_query", True, row["subject_generation"]
                )
            generation = row["subject_generation"]
        if denial is not None:
            self._deny(subject, denial, generation)
        return starter()

    def record_observation(self, subject, observation, profile, *, captured_at):
        """Persistent HealthProfile observation stream with ordering rules.

        Older-than-last-accepted captures are rejected (``OUT_OF_ORDER``); a
        profile revision mismatch is stored as ``unknown``. Every sample is kept.
        """
        if (
            not isinstance(captured_at, datetime)
            or captured_at.tzinfo is None
            or not hasattr(observation, "signals")
            or not hasattr(profile, "revision")
        ):
            raise BudgetError("INVALID_INPUT")
        evidence_ids = [signal.evidence_id for signal in observation.signals]
        verdicts = {signal.verdict for signal in observation.signals}
        with self.ledger._transaction() as conn:
            self._lock(conn, subject)
            row = conn.execute(
                "SELECT generation FROM m0_v3_subject WHERE id=%s", (subject,)
            ).fetchone()
            if row is None:
                raise BudgetError("UNKNOWN_IDENTITY")
            last = conn.execute(
                "SELECT max(captured_at) AS last FROM m0_v3_observation_stream WHERE subject=%s AND accepted",
                (subject,),
            ).fetchone()["last"]
            if last is not None and captured_at < last:
                accepted, code, verdict = False, "OUT_OF_ORDER", "unknown"
            elif last is not None and captured_at == last:
                accepted, code, verdict = False, "DUPLICATE_CAPTURE", "unknown"
            elif observation.profile_revision != profile.revision:
                accepted, code, verdict = False, "PROFILE_REVISION_MISMATCH", "unknown"
            elif not set(profile.required_signals) <= {
                s.name for s in observation.signals
            }:
                accepted, code, verdict = False, "SIGNAL_MISSING", "unknown"
            else:
                accepted, code = True, "ACCEPTED"
                verdict = (
                    "degraded"
                    if "degraded" in verdicts
                    else "unknown"
                    if "unknown" in verdicts
                    else "healthy"
                )
            sequence = conn.execute(
                "INSERT INTO m0_v3_observation_stream(subject,profile_revision,observation_revision,captured_at,evidence_ids,verdict,accepted,code) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING sequence",
                (
                    subject,
                    profile.revision,
                    observation.profile_revision,
                    captured_at,
                    Jsonb(evidence_ids),
                    verdict,
                    accepted,
                    code,
                ),
            ).fetchone()["sequence"]
            self._audit(conn, subject, "observation", accepted, row["generation"])
        return {
            "sequence": sequence,
            "accepted": accepted,
            "code": code,
            "verdict": verdict,
        }

    def observation_stream(self, subject):
        """Safe export of the persisted stream: no signal content, ids only."""
        with self.ledger._transaction() as conn:
            rows = conn.execute(
                "SELECT sequence,profile_revision,observation_revision,captured_at,evidence_ids,verdict,accepted,code FROM m0_v3_observation_stream WHERE subject=%s ORDER BY sequence",
                (subject,),
            ).fetchall()
        return [
            {
                **row,
                "captured_at": row["captured_at"].astimezone(timezone.utc).isoformat(),
            }
            for row in rows
        ]

    def record_stream_interruption(
        self, fence, request_id, *, partial_sha256, partial_bytes
    ):
        """Audit an interrupted streamed model request; partial bytes never become a step.

        The reservation stays occupied (unknown); a retry needs a new request id.
        """
        if (
            not isinstance(request_id, UUID)
            or type(partial_bytes) is not int
            or partial_bytes < 0
            or (partial_sha256 is not None and not isinstance(partial_sha256, str))
        ):
            raise BudgetError("INVALID_INPUT")
        with self.ledger._transaction() as conn:
            self._lock(conn, fence.subject)
            row = conn.execute(
                "SELECT d.step,s.response FROM m0_v3_dispatch d JOIN m0_v3_step s ON s.id=d.step WHERE d.request=%s AND d.subject=%s AND s.run_id=%s",
                (request_id, fence.subject, fence.run.run_id),
            ).fetchone()
            if row is None:
                raise BudgetError("UNKNOWN_MODEL_ATTEMPT")
            if row["response"] is not None:
                raise BudgetError("STEP_ALREADY_COMMITTED")
            conn.execute(
                "INSERT INTO m0_v3_stream_interruption(request,partial_sha256,partial_bytes) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                (request_id, partial_sha256, partial_bytes),
            )
            self.ledger.change_in_transaction(
                conn, RequestIdentity(fence.run, request_id), "unknown"
            )
            self._audit(
                conn, fence.subject, "stream_interrupted", False, fence.generation
            )
        return row["step"]

    @staticmethod
    def _audit(conn, subject, event, accepted, generation):
        conn.execute(
            "INSERT INTO m0_v3_audit(subject,event,accepted,generation) VALUES(%s,%s,%s,%s)",
            (subject, event, accepted, generation),
        )

    @staticmethod
    def _valid(conn, fence):
        return (
            conn.execute(
                "SELECT id FROM m0_v3_subject WHERE id=%s AND current_run=%s AND experiment_id=%s "
                "AND generation=%s AND owner=%s AND epoch=%s AND lease_until>clock_timestamp() "
                "AND state='running' AND %s>clock_timestamp() FOR UPDATE",
                (
                    fence.subject,
                    fence.run.run_id,
                    fence.run.experiment_id,
                    fence.generation,
                    fence.owner,
                    fence.epoch,
                    fence.run.deadline,
                ),
            ).fetchone()
            is not None
        )

    def control(self, subject, expected_generation, action, *, payload=None):
        if action not in {"cancel", "correct"}:
            raise BudgetError("INVALID_INPUT")
        with self.ledger._transaction() as conn:
            self._lock(conn, subject)
            row = conn.execute(
                "UPDATE m0_v3_subject SET generation=generation+1,state=%s,final=NULL WHERE id=%s AND generation=%s RETURNING generation",
                (
                    "cancelled" if action == "cancel" else "waiting_human",
                    subject,
                    expected_generation,
                ),
            ).fetchone()
            if row is None:
                raise BudgetError("CONTROL_CONFLICT")
            conn.execute(
                "INSERT INTO m0_v3_control VALUES(%s,%s,%s,%s)",
                (subject, row["generation"], action, Jsonb(payload or {})),
            )
            self._audit(conn, subject, action, True, row["generation"])
        return row["generation"]

    def dispatch(
        self,
        fence,
        segment,
        round_number,
        snapshot,
        request_id,
        reserved,
        max_requests,
        starter,
        *,
        before_lock=None,
        _prepare_only=False,
    ):
        """Atomically prepare snapshot/reservation, then initiate under control lock.

        ``starter`` MUST initiate the physical operation before returning a handle,
        and MUST NOT wait for its response. It must use the parent's shared round
        HTTP guard. No coroutine or deferred unscheduled work is a valid starter.
        An uncertain initiation consumes this request; retry uses a new identity.
        max_requests is per Run. Experiment fees/deadline remain in the PG
        ledger; the caller's shared authorization guard owns total HTTP limits.
        before_lock is a deterministic test barrier, outside authority checks.
        """
        if (
            type(round_number) is not int
            or round_number < 0
            or not segment
            or type(max_requests) is not int
            or max_requests <= 0
            or type(_prepare_only) is not bool
        ):
            raise BudgetError("INVALID_INPUT")
        step = uuid5(fence.run.run_id, f"{segment}:{round_number}")
        request = RequestIdentity(fence.run, request_id)
        if before_lock:
            before_lock()
        # Session lock outlives preparation COMMIT, so cancellation cannot commit
        # between the last authority check and actual starter initiation.
        try:
            with psycopg.connect(
                self.ledger._dsn, connect_timeout=3, row_factory=dict_row
            ) as conn:
                conn.execute("SET statement_timeout='5000ms'")
                self._lock(conn, fence.subject, session=True)
                conn.commit()
                if self._pause_denied(conn, fence):
                    raise BudgetError("PAUSED")
                with conn.transaction():
                    if not self._valid(conn, fence):
                        raise BudgetError("CONTROL_DENIED")
                    self.ledger._experiment(conn, fence.run.experiment_id)
                    count = conn.execute(
                        "SELECT count(*) AS n FROM m0_v3_dispatch d JOIN m0_v3_step s ON s.id=d.step WHERE s.run_id=%s AND d.kind='model'",
                        (fence.run.run_id,),
                    ).fetchone()["n"]
                    if count >= max_requests:
                        raise BudgetError("REQUEST_LIMIT")
                    conn.execute(
                        "INSERT INTO m0_v3_step(id,subject,run_id,segment,round,input,input_hash) VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        (
                            step,
                            fence.subject,
                            fence.run.run_id,
                            segment,
                            round_number,
                            Jsonb(snapshot),
                            digest(snapshot),
                        ),
                    )
                    stored = conn.execute(
                        "SELECT * FROM m0_v3_step WHERE id=%s", (step,)
                    ).fetchone()
                    if stored["subject"] != fence.subject or stored[
                        "input_hash"
                    ] != digest(snapshot):
                        raise BudgetError("IDENTITY_CONFLICT")
                    if stored["response"] is not None:
                        raise BudgetError("STEP_ALREADY_COMMITTED")
                    reservation = self.ledger.change_in_transaction(
                        conn, request, "reserve", reserved
                    )
                    if not reservation.created:
                        raise BudgetError("REQUEST_ALREADY_RESERVED")
                    conn.execute(
                        "INSERT INTO m0_v3_dispatch(request,subject,step,kind) VALUES(%s,%s,%s,'model')",
                        (request_id, fence.subject, step),
                    )
                    conn.execute(
                        "INSERT INTO m0_v3_model_execution VALUES(%s,%s,%s,%s)",
                        (request_id, fence.generation, fence.owner, fence.epoch),
                    )
                    if _prepare_only:
                        conn.execute(
                            "INSERT INTO m0_v3_send_grant(request) VALUES(%s)",
                            (request_id,),
                        )
                # The durable request now exists. A crash here leaves reserved;
                # neither this API nor recovery reuses its send permission.
                conn.autocommit = True
                if not self._valid(conn, fence):
                    raise BudgetError("CONTROL_DENIED")
                handle = None if _prepare_only else starter()
                conn.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s,0))",
                    (str(fence.subject),),
                )
                conn.commit()
                return step, handle
        except psycopg.Error:
            raise BudgetError("STORAGE_UNAVAILABLE") from None

    def dispatch_tool(
        self,
        fence,
        step,
        ordinal,
        attempt_id,
        max_queries,
        starter,
        *,
        before_lock=None,
    ):
        """Per-Run query limit; gateway owns authorization and source budgets."""
        if type(max_queries) is not int or max_queries <= 0:
            raise BudgetError("INVALID_INPUT")
        if before_lock:
            before_lock()
        try:
            with psycopg.connect(
                self.ledger._dsn, connect_timeout=3, row_factory=dict_row
            ) as conn:
                conn.execute("SET statement_timeout='5000ms'")
                self._lock(conn, fence.subject, session=True)
                conn.commit()
                if self._pause_denied(conn, fence):
                    raise BudgetError("PAUSED")
                with conn.transaction():
                    if not self._valid(conn, fence):
                        raise BudgetError("CONTROL_DENIED")
                    self.ledger._experiment(conn, fence.run.experiment_id)
                    row = conn.execute(
                        "SELECT o.result FROM m0_v3_operation o JOIN m0_v3_step s ON s.id=o.step WHERE o.step=%s AND o.ordinal=%s AND s.subject=%s AND s.run_id=%s",
                        (step, ordinal, fence.subject, fence.run.run_id),
                    ).fetchone()
                    if row is None or row["result"] is not None:
                        raise BudgetError("OPERATION_NOT_PENDING")
                    count = conn.execute(
                        "SELECT count(*) AS n FROM m0_v3_tool_attempt a JOIN m0_v3_step s ON s.id=a.step WHERE s.run_id=%s",
                        (fence.run.run_id,),
                    ).fetchone()["n"]
                    if count >= max_queries:
                        raise BudgetError("QUERY_LIMIT")
                    inserted = conn.execute(
                        "INSERT INTO m0_v3_tool_attempt(id,step,ordinal) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id",
                        (attempt_id, step, ordinal),
                    ).fetchone()
                    if inserted is None:
                        raise BudgetError("REQUEST_ALREADY_RESERVED")
                    conn.execute(
                        "INSERT INTO m0_v3_tool_execution VALUES(%s,%s,%s,%s)",
                        (attempt_id, fence.generation, fence.owner, fence.epoch),
                    )
                conn.autocommit = True
                if not self._valid(conn, fence):
                    raise BudgetError("CONTROL_DENIED")
                handle = starter()
                conn.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s,0))",
                    (str(fence.subject),),
                )
                conn.commit()
                return handle
        except psycopg.Error:
            raise BudgetError("STORAGE_UNAVAILABLE") from None

    def prepare_request(
        self, fence, segment, round_number, snapshot, request_id, reserved, max_requests
    ):
        """Reserve only. A separate transport must claim send_guard before HTTP.

        The no-op callback is internal preparation, never claimed as a send.
        If preparation crashes, reserved budget stays occupied; no automatic send.
        """
        step, _ = self.dispatch(
            fence,
            segment,
            round_number,
            snapshot,
            request_id,
            reserved,
            max_requests,
            lambda: None,
            _prepare_only=True,
        )
        return step

    @contextmanager
    def send_guard(self, fence, request_id, *, input_hash):
        """Transport-owned initiation lock, safe when its supervisor disappears.

        Yield (release, check) callbacks: check immediately before headers/body;
        release only after actual body transmission.
        The one-use grant is committed before network I/O; lost/unknown sends
        never receive a second grant. No provider call occurs in a transaction.
        """
        try:
            with psycopg.connect(
                self.ledger._dsn, connect_timeout=3, row_factory=dict_row
            ) as conn:
                conn.execute("SET statement_timeout='5000ms'")
                self._lock(conn, fence.subject, session=True)
                conn.commit()
                if self._pause_denied(conn, fence):
                    raise BudgetError("PAUSED")
                with conn.transaction():
                    if not self._valid(conn, fence):
                        raise BudgetError("CONTROL_DENIED")
                    row = conn.execute(
                        "SELECT d.step FROM m0_v3_dispatch d JOIN m0_v3_model_execution e ON e.request=d.request WHERE d.request=%s AND d.subject=%s AND e.generation=%s AND e.owner=%s AND e.epoch=%s AND d.request=(SELECT request FROM m0_v3_dispatch WHERE step=d.step ORDER BY created_at DESC,request DESC LIMIT 1)",
                        (
                            request_id,
                            fence.subject,
                            fence.generation,
                            fence.owner,
                            fence.epoch,
                        ),
                    ).fetchone()
                    if row is None:
                        raise BudgetError("UNKNOWN_MODEL_ATTEMPT")
                    snapshot = conn.execute(
                        "SELECT input_hash FROM m0_v3_step WHERE id=%s", (row["step"],)
                    ).fetchone()
                    if snapshot["input_hash"] != input_hash:
                        raise BudgetError("DELIVERY_INPUT_MISMATCH")
                    grant = conn.execute(
                        "UPDATE m0_v3_send_grant SET claimed=true WHERE request=%s AND NOT claimed RETURNING request",
                        (request_id,),
                    ).fetchone()
                    if grant is None:
                        raise BudgetError("SEND_GRANT_CONSUMED")
                conn.autocommit = True
                if not self._valid(conn, fence):
                    raise BudgetError("CONTROL_DENIED")
                released = False

                def release():
                    nonlocal released
                    if not released:
                        conn.execute(
                            "SELECT pg_advisory_unlock(hashtextextended(%s,0))",
                            (str(fence.subject),),
                        )
                        released = True

                def check():
                    if released or not self._valid(conn, fence):
                        raise BudgetError("CONTROL_DENIED")

                try:
                    yield release, check
                finally:
                    release()
        except psycopg.Error:
            raise BudgetError("STORAGE_UNAVAILABLE") from None

    def commit_response(self, fence, step, assistant, *, request_id):
        # Validate protocol without exposing it; synthetic result placeholders
        # validate the tool plan before any actual operation may run.
        calls = _validated_tool_calls(assistant)
        if calls:
            placeholders = [
                {"role": "tool", "tool_call_id": c.get("id"), "content": "pending"}
                for c in calls
            ]
            clean = continuation(
                assistant,
                placeholders,
                provider=fence.run.provider,
                run_id=str(fence.run.run_id),
                expected_run_id=str(fence.run.run_id),
            )[0]
        else:
            if (
                assistant.get("role") != "assistant"
                or not isinstance(assistant.get("content"), str)
                or not assistant["content"]
            ):
                raise ProtocolError("FINAL_INVALID")
            clean = {
                k: assistant[k]
                for k in ("role", "content", "reasoning_content")
                if k in assistant
            }
            clean["tool_calls"] = []
        accepted = False
        with self.ledger._transaction() as conn:
            self._lock(conn, fence.subject)
            if self._valid(conn, fence):
                attempt = conn.execute(
                    "SELECT d.request FROM m0_v3_dispatch d JOIN m0_v3_model_execution e ON e.request=d.request WHERE d.request=%s AND d.subject=%s AND d.step=%s AND e.generation=%s AND e.owner=%s AND e.epoch=%s AND d.request=(SELECT request FROM m0_v3_dispatch WHERE step=d.step ORDER BY created_at DESC,request DESC LIMIT 1)",
                    (
                        request_id,
                        fence.subject,
                        step,
                        fence.generation,
                        fence.owner,
                        fence.epoch,
                    ),
                ).fetchone()
                if attempt is None:
                    raise BudgetError("UNKNOWN_MODEL_ATTEMPT")
                grant = conn.execute(
                    "SELECT claimed FROM m0_v3_send_grant WHERE request=%s",
                    (request_id,),
                ).fetchone()
                if grant is not None and not grant["claimed"]:
                    raise BudgetError("MODEL_REQUEST_NOT_INITIATED")
                row = conn.execute(
                    "SELECT * FROM m0_v3_step WHERE id=%s AND subject=%s AND run_id=%s FOR UPDATE",
                    (step, fence.subject, fence.run.run_id),
                ).fetchone()
                if row is None:
                    raise BudgetError("UNKNOWN_IDENTITY")
                if row["response_hash"] not in (None, digest(clean)):
                    raise BudgetError("RESPONSE_CONFLICT")
                conn.execute(
                    "UPDATE m0_v3_step SET response=%s,response_hash=%s WHERE id=%s",
                    (Jsonb(clean), digest(clean), step),
                )
                for ordinal, call in enumerate(clean["tool_calls"]):
                    conn.execute(
                        "INSERT INTO m0_v3_operation(step,ordinal,call) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                        (step, ordinal, Jsonb(call)),
                    )
                accepted = True
            self._audit(
                conn, fence.subject, "model_response", accepted, fence.generation
            )
        return accepted

    def commit_tool(
        self, fence, step, ordinal, result, *, attempt_id, status="success"
    ):
        if status not in {"success", "failed", "cancelled", "unknown"}:
            raise BudgetError("INVALID_INPUT")
        if (
            not isinstance(result, dict)
            or set(result) != {"role", "tool_call_id", "content"}
            or result["role"] != "tool"
            or not isinstance(result["content"], str)
        ):
            raise ProtocolError("TOOL_PAIRING_INVALID")
        accepted = False
        with self.ledger._transaction() as conn:
            self._lock(conn, fence.subject)
            if self._valid(conn, fence):
                attempted = conn.execute(
                    "SELECT a.id FROM m0_v3_tool_attempt a JOIN m0_v3_tool_execution e ON e.attempt=a.id WHERE a.id=%s AND a.step=%s AND a.ordinal=%s AND e.generation=%s AND e.owner=%s AND e.epoch=%s AND a.id=(SELECT id FROM m0_v3_tool_attempt WHERE step=a.step AND ordinal=a.ordinal ORDER BY created_at DESC,id DESC LIMIT 1)",
                    (
                        attempt_id,
                        step,
                        ordinal,
                        fence.generation,
                        fence.owner,
                        fence.epoch,
                    ),
                ).fetchone()
                if attempted is None:
                    raise BudgetError("UNKNOWN_TOOL_ATTEMPT")
                row = conn.execute(
                    "SELECT o.* FROM m0_v3_operation o JOIN m0_v3_step s ON s.id=o.step WHERE o.step=%s AND o.ordinal=%s AND s.subject=%s AND s.run_id=%s FOR UPDATE",
                    (step, ordinal, fence.subject, fence.run.run_id),
                ).fetchone()
                if row is None or row["call"]["id"] != result["tool_call_id"]:
                    raise ProtocolError("TOOL_PAIRING_INVALID")
                if row["result"] is not None and (
                    row["result"] != result or row["status"] != status
                ):
                    raise BudgetError("RESULT_CONFLICT")
                conn.execute(
                    "UPDATE m0_v3_operation SET result=%s,status=%s,captured_at=COALESCE(captured_at,clock_timestamp()) WHERE step=%s AND ordinal=%s",
                    (Jsonb(result), status, step, ordinal),
                )
                accepted = True
            self._audit(conn, fence.subject, "tool_result", accepted, fence.generation)
        return accepted

    def rebuild(self, fence, step):
        """Restricted protocol return. Feed only same-provider/Run; never export."""
        with self.ledger._transaction() as conn:
            self._lock(conn, fence.subject)
            if not self._valid(conn, fence):
                raise BudgetError("CONTROL_DENIED")
            row = conn.execute(
                "SELECT * FROM m0_v3_step WHERE id=%s AND subject=%s AND run_id=%s",
                (step, fence.subject, fence.run.run_id),
            ).fetchone()
            if row is None:
                raise BudgetError("UNKNOWN_IDENTITY")
            if row["input_hash"] != digest(row["input"]):
                raise BudgetError("STATE_HASH_MISMATCH")
            operations = conn.execute(
                "SELECT * FROM m0_v3_operation WHERE step=%s ORDER BY ordinal", (step,)
            ).fetchall()
            response = row["response"]
            if response is None:
                return {
                    "status": "retry_model",
                    "input": row["input"],
                    "messages": [],
                    "pending": [],
                }
            if digest(response) != row["response_hash"]:
                raise BudgetError("STATE_HASH_MISMATCH")
            pending = [
                {"ordinal": o["ordinal"], "call": o["call"]}
                for o in operations
                if o["result"] is None
            ]
            if pending:
                return {
                    "status": "pending_tools",
                    "input": row["input"],
                    "messages": [],
                    "pending": pending,
                }
            messages = (
                continuation(
                    response,
                    [o["result"] for o in operations],
                    provider=fence.run.provider,
                    run_id=str(fence.run.run_id),
                    expected_run_id=str(fence.run.run_id),
                )
                if operations
                else [response]
            )
            return {
                "status": "ready",
                "input": row["input"],
                "messages": messages,
                "pending": [],
            }

    def publish(self, fence, candidate, *, step):
        """Publish a committed JSON final candidate after external contract checks.

        This method enforces provenance/fencing, not causal report correctness.
        """
        with self.ledger._transaction() as conn:
            self._lock(conn, fence.subject)
            accepted = self._valid(conn, fence)
            if accepted:
                row = conn.execute(
                    "SELECT response FROM m0_v3_step WHERE id=%s AND subject=%s AND run_id=%s",
                    (step, fence.subject, fence.run.run_id),
                ).fetchone()
                if row is None:
                    raise BudgetError("UNCOMMITTED_CANDIDATE")
                try:
                    response = row["response"]
                    valid = (
                        isinstance(response, dict)
                        and not response.get("tool_calls")
                        and json.loads(response["content"]) == candidate
                    )
                except (KeyError, TypeError, ValueError):
                    valid = False
                if not valid:
                    raise BudgetError("UNCOMMITTED_CANDIDATE")
                conn.execute(
                    "INSERT INTO m0_v3_report(run_id,candidate,generation) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                    (fence.run.run_id, Jsonb(candidate), fence.generation),
                )
                conn.execute(
                    "UPDATE m0_v3_subject SET final=%s,state='completed' WHERE id=%s",
                    (Jsonb(candidate), fence.subject),
                )
            else:
                # Lost publication ACK may retry exactly the same already
                # accepted report, but cannot regain authority after new control.
                accepted = (
                    conn.execute(
                        "SELECT s.id FROM m0_v3_subject s JOIN m0_v3_report r ON r.run_id=s.current_run WHERE s.id=%s AND s.current_run=%s AND s.generation=%s AND s.owner=%s AND s.epoch=%s AND s.state='completed' AND r.candidate=%s",
                        (
                            fence.subject,
                            fence.run.run_id,
                            fence.generation,
                            fence.owner,
                            fence.epoch,
                            Jsonb(candidate),
                        ),
                    ).fetchone()
                    is not None
                )
            self._audit(conn, fence.subject, "publish", accepted, fence.generation)
        return accepted

    def control_snapshot(self, subject):
        """Atomic public control watermark and accepted transitions only.

        m0_v3_control holds human payloads, not the complete generation history:
        new_run is recorded in the same committed audit as cancel/correct.
        Never expose payloads, model responses, evidence or private protocol.
        """
        with self.ledger._transaction() as conn:
            self._lock(conn, subject)
            row = conn.execute(
                "SELECT current_run,generation FROM m0_v3_subject WHERE id=%s",
                (subject,),
            ).fetchone()
            if row is None:
                raise BudgetError("UNKNOWN_IDENTITY")
            events = conn.execute(
                "SELECT generation,event AS action,created_at AS at FROM m0_v3_audit WHERE subject=%s AND accepted AND event=ANY(%s) ORDER BY sequence",
                (subject, list(get_args(ControlAction))),
            ).fetchall()
        return {
            "current_run": str(row["current_run"]),
            "final_generation": row["generation"],
            "controls": [
                ControlEvent.model_validate(event).model_dump(mode="json")
                for event in events
            ],
        }

    def summary(self, subject):
        """Safe metadata only: no private payload, input, tool content or report."""
        with self.ledger._transaction() as conn:
            row = conn.execute(
                "SELECT state,generation,epoch,(final IS NOT NULL) AS published FROM m0_v3_subject WHERE id=%s",
                (subject,),
            ).fetchone()
            audits = conn.execute(
                "SELECT event,accepted,generation FROM m0_v3_audit WHERE subject=%s ORDER BY sequence",
                (subject,),
            ).fetchall()
        return {"subject": str(subject), "state": row, "audit": audits}
