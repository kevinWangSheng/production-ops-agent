"""Release sampling persistence; incident storage keeps its original fold."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from opspilot.domain.subjects import ReleaseStatus, SubjectRef
from opspilot.observation.release_commands import (
    _audit_columns,
    _release_session_columns,
    audit_release,
    end_release_session,
    release_row,
    release_scope,
    scope_blocked,
    utc_values,
)
from opspilot.observation.release_policy import ReleasePolicy, canonical_json, sha256
from opspilot.observation.release_types import (
    ReleaseSampleEvidence,
    ReleaseSampleLease,
    ReleaseSampleReceipt,
)
from opspilot.observation.releases import ReleaseRecord
from opspilot.observer.health_profile import HealthProfile
from opspilot.observer.release_evaluation import (
    ReleaseEvaluationContext,
    advance_release_window,
    evaluate_release_sample,
    query_evidence_consistent,
)
from opspilot.persistence.base import Connection, PersistenceError, _StoreBase

if TYPE_CHECKING:
    from opspilot.observation.store import SampleLease


def context_in(
    conn: Connection, session: dict[str, Any], release: dict[str, Any]
) -> ReleaseEvaluationContext:
    frozen = conn.execute(
        "SELECT content FROM opspilot_health_profiles WHERE health_profile_revision=%s",
        (session["health_profile_revision"],),
    ).fetchone()
    if frozen is None:
        raise PersistenceError("HEALTH_PROFILE_UNAVAILABLE")
    revision = release["calibrated_policy_revision"]
    if revision is None or release["calibrated_policy_content"] is None:
        raise PersistenceError("RELEASE_POLICY_MISMATCH")
    policy = ReleasePolicy.model_validate_json(
        canonical_json(
            {
                "policy_id": revision.rsplit("@", 1)[0],
                "revision": revision,
                "content_sha256": release["calibrated_policy_sha256"],
                "content": json.loads(release["calibrated_policy_content"]),
            }
        )
    )
    return ReleaseEvaluationContext(
        release=ReleaseRecord.model_validate(release),
        session_id=session["session_id"],
        authorized_at=session["authorized_at"],
        policy=policy,
        health_profile=HealthProfile.model_validate_json(frozen["content"]),
    )


def _fingerprint_json(value: Any) -> Any:
    # PostgreSQL timestamptz preserves instants, not the caller's offset text.
    # Canonicalize typed timestamps before hashing both submissions and replay.
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {key: _fingerprint_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_fingerprint_json(item) for item in value]
    return value


def sample_fingerprint(
    lease: ReleaseSampleLease, evidence: ReleaseSampleEvidence
) -> str:
    data = evidence.model_dump(exclude={"queries"})
    data["queries"] = [
        {**q.model_dump(exclude={"raw"}), "raw": q.raw.hex()}
        for q in sorted(evidence.queries, key=lambda q: q.query_key)
    ]
    data["lease"] = lease.model_dump()
    return sha256(canonical_json(_fingerprint_json(data)).encode())


class _ReleaseSampleOps(_StoreBase):
    @contextmanager
    def _release_transaction(self, *, snapshot: bool = False) -> Iterator[Connection]:
        # Keep the incident error mapping unchanged; new release boundaries map
        # SQL guards/constraints to the explicit integrity refusal contract.
        try:
            with self.transaction(snapshot=snapshot) as conn:
                yield conn
        except PersistenceError as exc:
            if (
                isinstance(exc.__cause__, psycopg.errors.UniqueViolation)
                and exc.__cause__.diag.constraint_name
                == "opspilot_observation_samples_pkey"
            ):
                # Same-subject submissions serialize on the release lock;
                # a concurrent collision here belongs to another subject.
                raise PersistenceError("SAMPLE_IDENTITY_CONFLICT") from exc.__cause__
            if isinstance(
                exc.__cause__,
                (psycopg.IntegrityError, psycopg.errors.InsufficientPrivilege),
            ):
                raise PersistenceError("INTEGRITY_REFUSED") from exc.__cause__
            raise

    def _claim_release_due(
        self, owner: UUID, session_id: UUID, lease_seconds: int
    ) -> list[ReleaseSampleLease]:
        with self._release_transaction() as conn:
            now = self._db_now(conn).astimezone(timezone.utc)
            identity = conn.execute(
                "SELECT release_observation_id FROM opspilot_observation_sessions WHERE session_id=%s AND purpose='release_observation'",
                (session_id,),
            ).fetchone()
            if identity is None:
                return []
            # Two explicit locks preserve release -> session, both skip locked.
            subject = conn.execute(
                "SELECT observation_id FROM opspilot_release_observations WHERE observation_id=%s FOR UPDATE SKIP LOCKED",
                (identity["release_observation_id"],),
            ).fetchone()
            if subject is None:
                return []
            release = release_row(conn, subject["observation_id"])
            session = conn.execute(
                "SELECT "
                + _release_session_columns()
                + " FROM opspilot_observation_sessions WHERE session_id=%s AND state='authorized' AND authorized AND active_sample_job_id IS NOT NULL AND active_sample_due_at<=%s AND (active_sample_lease_until IS NULL OR active_sample_lease_until<=%s) AND deadline_at>%s FOR UPDATE SKIP LOCKED",
                (session_id, now, now, now),
            ).fetchone()
            session = utc_values(session)
            if session is None:
                return []
            scope = release_scope(conn, release["target_id"])
            if scope_blocked(scope, session):
                end_release_session(conn, release, session, reason="scope_suspended")
                return []
            if (
                release["paused"]
                or release["status"] != "observing"
                or release["control_generation"]
                != session["subject_control_generation"]
                or release["observation_generation"]
                != session["observation_generation"]
            ):
                end_release_session(conn, release, session, reason="binding_stale")
                return []
            epoch = session["active_sample_epoch"] + 1
            until = now + timedelta(seconds=lease_seconds)
            conn.execute(
                "UPDATE opspilot_observation_sessions SET active_sample_owner=%s,active_sample_epoch=%s,active_sample_lease_until=%s,updated_at=clock_timestamp() WHERE session_id=%s",
                (owner, epoch, until, session_id),
            )
            return [
                ReleaseSampleLease(
                    session_id=session_id,
                    observation_id=release["observation_id"],
                    purpose="release_observation",
                    subject=SubjectRef(
                        kind="release_observation", id=str(release["observation_id"])
                    ),
                    job_id=session["active_sample_job_id"],
                    sequence=session["active_sample_sequence"],
                    owner=owner,
                    epoch=epoch,
                    lease_until=until,
                    subject_control_generation=session["subject_control_generation"],
                    observation_generation=session["observation_generation"],
                    health_profile_revision=session["health_profile_revision"],
                    calibrated_policy_revision=session["release_policy_revision"],
                    deadline_at=session["deadline_at"],
                    sample_interval_seconds=session["sample_interval_seconds"],
                    adopted_window_end=session["adopted_window_end"],
                    global_suspension_generation=scope["global_generation"],
                    target_suspension_generation=scope["target_generation"],
                )
            ]

    def claim_due_samples(
        self, owner: UUID, *, limit: int = 20, lease_seconds: int = 120
    ) -> list[SampleLease | ReleaseSampleLease]:
        if (
            not isinstance(owner, UUID)
            or type(limit) is not int
            or limit <= 0
            or type(lease_seconds) is not int
            or lease_seconds <= 0
        ):
            raise PersistenceError("INVALID_INPUT")
        leases: list[SampleLease | ReleaseSampleLease] = []
        probe_budget = 8 * limit
        probed = 0
        now: datetime | None = None
        cursor: tuple[datetime, UUID] | None = None
        while len(leases) < limit and probed < probe_budget:
            page_limit = min(limit, probe_budget - probed)
            with self.transaction(snapshot=True) as conn:
                if now is None:
                    now = self._db_now(conn).astimezone(timezone.utc)
                query = "SELECT session_id,purpose,active_sample_due_at FROM opspilot_observation_sessions s JOIN opspilot_scope_controls g ON g.scope_id=1 LEFT JOIN opspilot_target_suspensions t ON t.target_id=s.target_id WHERE s.state='authorized' AND s.authorized AND s.active_sample_job_id IS NOT NULL AND s.active_sample_due_at<=%s AND (s.active_sample_lease_until IS NULL OR s.active_sample_lease_until<=%s) AND s.deadline_at>%s AND NOT g.global_suspended AND NOT COALESCE(t.suspended,false)"
                parameters: list[object] = [now, now, now]
                if cursor is not None:
                    query += " AND (active_sample_due_at,session_id)>(%s,%s)"
                    parameters.extend(cursor)
                query += " ORDER BY active_sample_due_at,session_id LIMIT %s"
                parameters.append(page_limit)
                candidates = conn.execute(query, parameters).fetchall()
            if not candidates:
                break
            cursor = (
                candidates[-1]["active_sample_due_at"],
                candidates[-1]["session_id"],
            )
            for row in candidates:
                if len(leases) >= limit:
                    break
                probed += 1
                if row["purpose"] == "release_observation":
                    leases.extend(
                        self._claim_release_due(owner, row["session_id"], lease_seconds)
                    )
                else:
                    # Existing incident transaction and lease shape are retained.
                    leases.extend(
                        self._claim_incident_due_samples(  # type: ignore[attr-defined]
                            owner,
                            limit=1,
                            lease_seconds=lease_seconds,
                            selected_session_id=row["session_id"],
                        )
                    )
            if len(candidates) < page_limit:
                break
        return leases

    def _release_lease_current(self, lease: ReleaseSampleLease) -> bool:
        with self._release_transaction(snapshot=True) as conn:
            try:
                release = release_row(conn, lease.observation_id)
            except PersistenceError:
                return False
            session = conn.execute(
                "SELECT "
                + _release_session_columns()
                + " FROM opspilot_observation_sessions WHERE session_id=%s",
                (lease.session_id,),
            ).fetchone()
            session = utc_values(session)
            scope = release_scope(conn, release["target_id"])
            now = self._db_now(conn).astimezone(timezone.utc)
        return bool(
            session
            and session["release_observation_id"] == lease.observation_id
            and session["authorized"]
            and session["state"] == "authorized"
            and session["active_sample_job_id"] == lease.job_id
            and session["active_sample_owner"] == lease.owner
            and session["active_sample_epoch"] == lease.epoch
            and session["active_sample_lease_until"] == lease.lease_until
            and now < lease.lease_until
            and now < release["deadline_at"]
            and release["status"] == "observing"
            and not release["paused"]
            and release["control_generation"]
            == lease.subject_control_generation
            == session["subject_control_generation"]
            and release["observation_generation"]
            == lease.observation_generation
            == session["observation_generation"]
            and session["release_policy_revision"] == lease.calibrated_policy_revision
            and session["health_profile_revision"] == lease.health_profile_revision
            and not scope_blocked(scope, session)
            and scope["global_generation"] == lease.global_suspension_generation
            and scope["target_generation"] == lease.target_suspension_generation
        )

    def release_context(self, session_id: UUID) -> ReleaseEvaluationContext:
        if not isinstance(session_id, UUID):
            raise PersistenceError("INVALID_INPUT")
        with self._release_transaction(snapshot=True) as conn:
            session = conn.execute(
                "SELECT "
                + _release_session_columns()
                + " FROM opspilot_observation_sessions WHERE session_id=%s",
                (session_id,),
            ).fetchone()
            session = utc_values(session)
            if session is None or session["purpose"] != "release_observation":
                raise PersistenceError("NOT_FOUND")
            return context_in(
                conn, session, release_row(conn, session["release_observation_id"])
            )

    def submit_release_sample(
        self, lease: ReleaseSampleLease, evidence: ReleaseSampleEvidence
    ) -> ReleaseSampleReceipt:
        if not isinstance(lease, ReleaseSampleLease) or not isinstance(
            evidence, ReleaseSampleEvidence
        ):
            raise PersistenceError("INVALID_INPUT")
        # Frozen DTO containers can still be mutable. Revalidate before entering SQL.
        lease = ReleaseSampleLease.model_validate(lease.model_dump())
        evidence = ReleaseSampleEvidence.model_validate(evidence.model_dump())
        fingerprint = sample_fingerprint(lease, evidence)
        with self._release_transaction() as conn:
            release = release_row(conn, lease.observation_id, lock=True)
            session = conn.execute(
                "SELECT "
                + _release_session_columns()
                + " FROM opspilot_observation_sessions WHERE session_id=%s FOR UPDATE",
                (lease.session_id,),
            ).fetchone()
            session = utc_values(session)
            if (
                session is None
                or session["release_observation_id"] != lease.observation_id
            ):
                raise PersistenceError("INVALID_INPUT")
            previous = conn.execute(
                "SELECT release_evidence_sha256,release_receipt FROM opspilot_observation_samples WHERE sample_id=%s",
                (evidence.sample_id,),
            ).fetchone()
            if previous:
                if previous["release_evidence_sha256"] != fingerprint:
                    raise PersistenceError("SAMPLE_IDENTITY_CONFLICT")
                saved = {
                    k: v
                    for k, v in previous["release_receipt"].items()
                    if k != "_lease"
                }
                return ReleaseSampleReceipt.model_validate_json(
                    canonical_json({**saved, "replayed": True})
                )
            now = self._db_now(conn).astimezone(timezone.utc)
            scope = release_scope(conn, release["target_id"])
            context = context_in(conn, session, release)
            evaluation = evaluate_release_sample(context, evidence, decision_at=now)
            consistent = query_evidence_consistent(context, evidence, now)
            valid = (
                session["active_sample_job_id"] == lease.job_id
                and session["active_sample_owner"] == lease.owner
                and session["active_sample_epoch"] == lease.epoch
                and session["active_sample_lease_until"] == lease.lease_until
                and now < lease.lease_until
            )
            stamps = (
                session["active_sample_sequence"] == lease.sequence
                and session["subject_control_generation"]
                == lease.subject_control_generation
                and session["observation_generation"] == lease.observation_generation
                and session["health_profile_revision"] == lease.health_profile_revision
                and session["release_policy_revision"]
                == lease.calibrated_policy_revision
                and session["deadline_at"] == lease.deadline_at
            )
            blocked = (
                scope_blocked(scope, session)
                or scope["global_generation"] != lease.global_suspension_generation
                or scope["target_generation"] != lease.target_suspension_generation
            )
            reason = "adopted"
            if release["control_generation"] != lease.subject_control_generation:
                reason = "control_generation_stale"
            elif release["observation_generation"] != lease.observation_generation:
                reason = "observation_generation_stale"
            elif release["status"] != "observing" or release["paused"]:
                reason = "subject_state_not_adoptable"
            elif blocked:
                reason = "suspended"
            elif now >= release["deadline_at"]:
                reason = "deadline_expired"
            elif session["state"] != "authorized" or not session["authorized"]:
                reason = "session_not_authorized"
            elif not valid:
                reason = "lease_revoked"
            elif not stamps:
                reason = "lease_stamp_mismatch"
            elif lease.sequence <= session["adopted_sequence"]:
                reason = "sequence_not_advancing"
            elif (
                session["adopted_window_end"] is not None
                and evidence.window.end < session["adopted_window_end"]
            ):
                reason = "window_regressed"
            elif not consistent:
                reason = "readings_inconsistent"
            accepted = reason == "adopted"
            status = None
            ending_reason = None
            ending_id = None
            transition = None
            used = release["used_sample_count"]
            tracking = healthy = 0.0
            due = None
            if accepted:
                used += 1
                marks, status, ending_reason = advance_release_window(
                    context,
                    session,
                    evaluation,
                    window_start=evidence.window.start,
                    window_end=evidence.window.end,
                    decision_at=now,
                    used_sample_count=used,
                )
                session.update(
                    marks,
                    adopted_count=session["adopted_count"] + 1,
                    adopted_sequence=lease.sequence,
                    adopted_window_end=evidence.window.end,
                )
                if marks["tracking_since"] is not None:
                    tracking = (
                        marks["tracking_until"] - marks["tracking_since"]
                    ).total_seconds()
                if marks["healthy_since"] is not None:
                    healthy = (
                        evidence.window.end - marks["healthy_since"]
                    ).total_seconds()
                conn.execute(
                    "UPDATE opspilot_observation_sessions SET tracking_since=%s,tracking_until=%s,healthy_since=%s,adopted_count=%s,adopted_sequence=%s,adopted_window_end=%s,updated_at=clock_timestamp() WHERE session_id=%s",
                    (
                        marks["tracking_since"],
                        marks["tracking_until"],
                        marks["healthy_since"],
                        session["adopted_count"],
                        lease.sequence,
                        evidence.window.end,
                        lease.session_id,
                    ),
                )
                if status is not None:
                    transition = "release_" + status
                    # Sample FK requires insertion before the ending, below.
                    from uuid import uuid5

                    ending_id = uuid5(
                        lease.observation_id, "session-ending:" + str(lease.session_id)
                    )
                    session["state"] = (
                        "completed" if status in ("healthy", "anomalous") else "expired"
                    )
                else:
                    due = now + timedelta(seconds=release["sample_interval_seconds"])
                    conn.execute(
                        "UPDATE opspilot_observation_sessions SET issued_sequence=issued_sequence+1,active_sample_job_id=%s,active_sample_sequence=issued_sequence+1,active_sample_due_at=%s,active_sample_owner=NULL,active_sample_lease_until=NULL,updated_at=clock_timestamp() WHERE session_id=%s",
                        (uuid4(), due, lease.session_id),
                    )
            elif session["state"] == "authorized" and reason in (
                "suspended",
                "control_generation_stale",
                "observation_generation_stale",
                "subject_state_not_adoptable",
            ):
                ending_id = end_release_session(
                    conn,
                    release,
                    session,
                    reason="scope_suspended"
                    if reason == "suspended"
                    else "binding_stale",
                )
                session["state"] = "revoked"
            elif valid and session["state"] == "authorized":
                due = now + timedelta(seconds=release["sample_interval_seconds"])
                conn.execute(
                    "UPDATE opspilot_observation_sessions SET active_sample_due_at=%s,active_sample_owner=NULL,active_sample_lease_until=NULL,updated_at=clock_timestamp() WHERE session_id=%s",
                    (due, lease.session_id),
                )
            receipt = ReleaseSampleReceipt(
                sample_id=evidence.sample_id,
                session_id=lease.session_id,
                observation_id=lease.observation_id,
                replayed=False,
                accepted=accepted,
                disposition="adopted" if accepted else "history_only",
                reason=reason,
                evaluation=evaluation,
                session_state=session["state"],
                release_status=cast(ReleaseStatus, status or release["status"]),
                used_sample_count=used,
                tracking_seconds=tracking,
                healthy_window_seconds=healthy,
                ending_id=ending_id,
                next_sample_due_at=due,
            )
            columns = "sample_id,session_id,job_id,sequence,epoch,window_start,window_end,outcome,required_signals_present,subject_control_generation,observation_generation,health_profile_revision,disposition,reason,confirms_health,health_basis,scope_suspended,global_generation,target_generation,within_deadline,lease_valid,lease_stamps_match,readings_consistent,transition,current_subject_state,current_subject_control_generation,current_subject_observation_generation,decision_at,release_evidence_sha256,release_evaluation,release_receipt"
            confirms = accepted and evaluation.verdict == "healthy"
            saved = {
                **receipt.model_dump(mode="json"),
                "_lease": lease.model_dump(mode="json"),
            }
            values = [
                evidence.sample_id,
                lease.session_id,
                lease.job_id,
                lease.sequence,
                lease.epoch,
                evidence.window.start,
                evidence.window.end,
                evaluation.outcome,
                evaluation.evidence_sufficient,
                lease.subject_control_generation,
                lease.observation_generation,
                lease.health_profile_revision,
                receipt.disposition,
                reason,
                confirms,
                "confirmed"
                if confirms
                else "not_adopted"
                if not accepted
                else "outcome_not_healthy",
                blocked,
                scope["global_generation"],
                scope["target_generation"],
                now < release["deadline_at"],
                valid,
                stamps,
                consistent,
                transition,
                release["status"],
                release["control_generation"],
                release["observation_generation"],
                now,
                fingerprint,
                Jsonb(evaluation.model_dump(mode="json")),
                Jsonb(saved),
            ]
            conn.execute(
                "INSERT INTO opspilot_observation_samples("
                + columns
                + ") VALUES("
                + ",".join(["%s"] * len(values))
                + ")",
                values,
            )
            for q in evidence.queries:
                data = q.model_dump()
                fields = ["sample_id", *data]
                conn.execute(
                    "INSERT INTO opspilot_release_query_results("
                    + ",".join(fields)
                    + ") VALUES("
                    + ",".join(["%s"] * len(fields))
                    + ")",
                    [evidence.sample_id, *data.values()],
                )
            if accepted:
                if status is not None:
                    end_release_session(
                        conn,
                        release,
                        session,
                        reason=ending_reason or "",
                        status_after=status,
                        sample_id=evidence.sample_id,
                    )
                conn.execute(
                    "UPDATE opspilot_release_observations SET used_sample_count=%s,status=%s,ended_reason=%s,ended_at=%s,updated_at=clock_timestamp() WHERE observation_id=%s",
                    (
                        used,
                        status or release["status"],
                        ending_reason,
                        now if status else None,
                        lease.observation_id,
                    ),
                )
            return receipt

    def sweep_expired_releases(self, *, limit: int = 20) -> list[UUID]:
        if type(limit) is not int or limit <= 0:
            raise PersistenceError("INVALID_INPUT")
        ended = []
        with self._release_transaction() as conn:
            now = self._db_now(conn).astimezone(timezone.utc)
            from opspilot.observation.releases import _RELEASE_COLUMNS

            rows = conn.execute(
                "SELECT "
                + _RELEASE_COLUMNS
                + " FROM opspilot_release_observations WHERE status IN ('pending','observing') AND deadline_at<=%s ORDER BY deadline_at,observation_id LIMIT %s FOR UPDATE SKIP LOCKED",
                (now, limit),
            ).fetchall()
            for row in rows:
                row = utc_values(row)
                now = self._db_now(conn).astimezone(timezone.utc)
                sessions = conn.execute(
                    "SELECT "
                    + _release_session_columns()
                    + " FROM opspilot_observation_sessions WHERE release_observation_id=%s AND state='authorized' ORDER BY session_id FOR UPDATE",
                    (row["observation_id"],),
                ).fetchall()
                ending = None
                for session in sessions:
                    ending = end_release_session(
                        conn,
                        row,
                        session,
                        reason="deadline_expired",
                        status_after="unknown",
                    )
                after = {
                    **row,
                    "status": "unknown",
                    "ended_reason": "deadline_expired",
                    "ended_at": now,
                }
                data = {
                    "observation_id": str(row["observation_id"]),
                    "action": "deadline_expired",
                    "outcome": "applied",
                    "replayed": False,
                    "reason": "deadline_expired",
                    "session_id": str(sessions[0]["session_id"]) if sessions else None,
                    "ending_id": str(ending) if ending else None,
                    **{
                        k: after[k]
                        for k in (
                            "status",
                            "paused",
                            "control_generation",
                            "observation_generation",
                            "used_sample_count",
                        )
                    },
                    "deadline_at": row["deadline_at"].isoformat(),
                }
                audit = audit_release(
                    conn,
                    row,
                    after,
                    action="deadline_expired",
                    actor="observer",
                    expected=row["control_generation"],
                    key="system:deadline",
                    fingerprint=sha256(b"system:deadline"),
                    outcome="applied",
                    reason="deadline_expired",
                    session_id=sessions[0]["session_id"] if sessions else None,
                    ending_id=ending,
                    receipt=data,
                )
                terminal_time = conn.execute(
                    "SELECT recorded_at FROM opspilot_observation_endings WHERE ending_id=%s"
                    if ending is not None
                    else "SELECT recorded_at FROM opspilot_release_audit WHERE audit_id=%s",
                    (ending if ending is not None else UUID(audit["audit_id"]),),
                ).fetchone()
                assert terminal_time is not None
                conn.execute(
                    "UPDATE opspilot_release_observations SET status='unknown',ended_reason='deadline_expired',ended_at=%s,updated_at=clock_timestamp() WHERE observation_id=%s",
                    (terminal_time["recorded_at"], row["observation_id"]),
                )
                ended.append(row["observation_id"])
        return ended

    def _release_history_in(
        self, conn: Connection, observation_id: UUID
    ) -> dict[str, Any]:
        from opspilot.observation.store import _ENDING_COLUMNS, _SAMPLE_COLUMNS

        release = release_row(conn, observation_id)
        profile = conn.execute(
            "SELECT health_profile_revision AS revision,content,content_sha256 FROM opspilot_health_profiles WHERE health_profile_revision=%s",
            (release["health_profile_revision"],),
        ).fetchone()
        sessions = conn.execute(
            "SELECT "
            + _release_session_columns()
            + " FROM opspilot_observation_sessions WHERE release_observation_id=%s ORDER BY observation_generation,session_id",
            (observation_id,),
        ).fetchall()
        samples = conn.execute(
            "SELECT "
            + _SAMPLE_COLUMNS
            + ",purpose,subject_kind,subject_id,current_subject_state,current_subject_control_generation,current_subject_observation_generation,decision_at,release_evidence_sha256,release_evaluation,release_receipt FROM opspilot_observation_samples WHERE subject_kind='release_observation' AND subject_id=%s ORDER BY decision_at,sample_id",
            (observation_id,),
        ).fetchall()
        for sample in samples:
            sample["queries"] = conn.execute(
                'SELECT sample_id,query_key,query,source,evaluated_at,sent_at,status,http_status,detail,body_complete,raw,raw_sha256 FROM opspilot_release_query_results WHERE sample_id=%s ORDER BY query_key COLLATE "C"',
                (sample["sample_id"],),
            ).fetchall()
        endings = conn.execute(
            "SELECT "
            + _ENDING_COLUMNS
            + ",release_observation_id,purpose,subject_kind,subject_id,subject_state_before,subject_state_after,anomaly_key FROM opspilot_observation_endings WHERE release_observation_id=%s ORDER BY recorded_at,ending_id",
            (observation_id,),
        ).fetchall()
        audit = conn.execute(
            "SELECT "
            + _audit_columns()
            + " FROM opspilot_release_audit WHERE observation_id=%s ORDER BY sequence",
            (observation_id,),
        ).fetchall()
        return cast(
            dict[str, Any],
            utc_values(
                {
                    "format_version": 1,
                    "release": ReleaseRecord.model_validate(release),
                    "health_profile": profile,
                    "sessions": sessions,
                    "samples": samples,
                    "endings": endings,
                    "audit": audit,
                }
            ),
        )

    def release_history(self, observation_id: UUID) -> Mapping[str, object]:
        if not isinstance(observation_id, UUID):
            raise PersistenceError("INVALID_INPUT")
        with self._release_transaction(snapshot=True) as conn:
            return self._release_history_in(conn, observation_id)
