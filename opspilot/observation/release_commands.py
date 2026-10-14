"""Controller release commands and the shared release/session lock protocol."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import UUID, uuid4, uuid5

import psycopg
from psycopg.types.json import Jsonb
from pydantic import AwareDatetime, ValidationError

from opspilot.domain.base import DTO, Count
from opspilot.domain.subjects import ReleaseStatus
from opspilot.observation.bindings import _TargetProfileOps
from opspilot.observation.release_policy import (
    ReleasePolicy,
    ReleaseText,
    canonical_json,
    load_release_policy_catalog,
    sha256,
    storable_text,
)
from opspilot.observer.health_profile import HealthProfile
from opspilot.persistence.base import Connection, PersistenceError

if TYPE_CHECKING:
    from opspilot.observation.release_types import ReleaseAnomalyEnding


class ReleaseCommandReceipt(DTO):
    observation_id: UUID
    audit_id: UUID
    sequence: Count
    action: Literal["authorize", "pause", "resume", "cancel"]
    outcome: Literal["applied", "rejected"]
    replayed: bool
    reason: ReleaseText | None
    session_id: UUID | None
    ending_id: UUID | None
    status: ReleaseStatus
    paused: bool
    control_generation: Count
    observation_generation: Count
    used_sample_count: Count
    deadline_at: AwareDatetime


def utc_values(value: Any) -> Any:
    """Normalize release-only PG timestamps before elapsed-time arithmetic."""
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            raise ValueError("INVALID_TIMESTAMP")
        return value.astimezone(timezone.utc)
    if isinstance(value, dict):
        return {key: utc_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [utc_values(item) for item in value]
    if isinstance(value, tuple):
        return tuple(utc_values(item) for item in value)
    return value


def release_scope(
    conn: Connection, target_id: UUID, *, lock: bool = False
) -> dict[str, Any]:
    suffix = " FOR SHARE" if lock else ""
    scope = conn.execute(
        "SELECT global_suspended,global_generation FROM opspilot_scope_controls WHERE scope_id=1"
        + suffix
    ).fetchone()
    target = conn.execute(
        "SELECT suspended,generation FROM opspilot_target_suspensions WHERE target_id=%s"
        + suffix,
        (target_id,),
    ).fetchone()
    if scope is None or target is None:
        raise PersistenceError("INCONSISTENT_STATE")
    return {
        **scope,
        "target_suspended": target["suspended"],
        "target_generation": target["generation"],
    }


def scope_blocked(scope: dict[str, Any], session: dict[str, Any]) -> bool:
    return bool(
        scope["global_suspended"]
        or scope["target_suspended"]
        or scope["global_generation"] != session["authorized_global_generation"]
        or scope["target_generation"] != session["authorized_target_generation"]
    )


def release_row(
    conn: Connection, observation_id: UUID, *, lock: bool = False
) -> dict[str, Any]:
    from opspilot.observation.releases import _RELEASE_COLUMNS

    row = conn.execute(
        "SELECT "
        + _RELEASE_COLUMNS
        + " FROM opspilot_release_observations WHERE observation_id=%s"
        + (" FOR UPDATE" if lock else ""),
        (observation_id,),
    ).fetchone()
    if row is None:
        raise PersistenceError("NOT_FOUND")
    return cast(dict[str, Any], utc_values(row))


def end_release_session(
    conn: Connection,
    release: dict[str, Any],
    session: dict[str, Any],
    *,
    reason: str,
    status_after: str | None = None,
    sample_id: UUID | None = None,
) -> UUID:
    ending_id = uuid5(
        release["observation_id"], "session-ending:" + str(session["session_id"])
    )
    existing = conn.execute(
        "SELECT ending_id FROM opspilot_observation_endings WHERE ending_id=%s",
        (ending_id,),
    ).fetchone()
    if existing:
        return ending_id
    state = (
        "completed"
        if reason in ("healthy_confirmed", "anomaly_detected")
        else "expired"
        if reason in ("deadline_expired", "max_samples_exhausted")
        else "revoked"
    )
    after = status_after or release["status"]
    transition = (
        "release_" + after
        if after in ("healthy", "anomalous", "unknown")
        and reason
        in (
            "healthy_confirmed",
            "anomaly_detected",
            "deadline_expired",
            "max_samples_exhausted",
        )
        else None
    )
    conn.execute(
        "UPDATE opspilot_observation_sessions SET state=%s,ended_reason=%s,active_sample_job_id=NULL,active_sample_sequence=NULL,active_sample_due_at=NULL,active_sample_owner=NULL,active_sample_lease_until=NULL,updated_at=clock_timestamp() WHERE session_id=%s",
        (state, reason, session["session_id"]),
    )
    conn.execute(
        "INSERT INTO opspilot_observation_endings(ending_id,session_id,release_observation_id,ended_reason,transition,sample_id,subject_state_before,subject_state_after,anomaly_key,job_id,job_sequence) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            ending_id,
            session["session_id"],
            release["observation_id"],
            reason,
            transition,
            sample_id,
            release["status"],
            after,
            "release-anomaly:" + str(release["observation_id"])
            if reason == "anomaly_detected"
            else None,
            session["active_sample_job_id"],
            session["active_sample_sequence"],
        ),
    )
    return ending_id


def audit_release(
    conn: Connection,
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    action: str,
    actor: str,
    expected: int,
    key: str,
    fingerprint: str,
    outcome: str,
    reason: str | None,
    session_id: UUID | None,
    ending_id: UUID | None,
    receipt: dict[str, Any],
) -> dict[str, Any]:
    sequence = conn.execute(
        "SELECT COALESCE(max(sequence),0)+1 AS sequence FROM opspilot_release_audit WHERE observation_id=%s",
        (before["observation_id"],),
    ).fetchone()
    assert sequence is not None
    result = {**receipt, "audit_id": str(uuid4()), "sequence": sequence["sequence"]}
    columns = [
        "audit_id",
        "observation_id",
        "sequence",
        "idempotency_key",
        "request_sha256",
        "action",
        "principal_kind",
        "actor",
        "outcome",
        "reason",
        "expected_control_generation",
        "control_generation_before",
        "control_generation_after",
        "observation_generation_before",
        "observation_generation_after",
        "status_before",
        "status_after",
        "paused_before",
        "paused_after",
        "session_id",
        "ending_id",
        "result",
    ]
    values = [
        UUID(result["audit_id"]),
        before["observation_id"],
        result["sequence"],
        key,
        fingerprint,
        action,
        "observer"
        if action == "deadline_expired"
        else "system"
        if action == "authorize"
        else "basic_auth",
        actor,
        outcome,
        reason,
        expected,
        before["control_generation"],
        after["control_generation"],
        before["observation_generation"],
        after["observation_generation"],
        before["status"],
        after["status"],
        before["paused"],
        after["paused"],
        session_id,
        ending_id,
        Jsonb(result),
    ]
    conn.execute(
        "INSERT INTO opspilot_release_audit("
        + ",".join(columns)
        + ") VALUES("
        + ",".join(["%s"] * len(values))
        + ")",
        values,
    )
    return result


class _ReleaseCommandOps(_TargetProfileOps):
    def authorize_release(
        self,
        observation_id: UUID,
        *,
        actor: str,
        expected_control_generation: int,
        idempotency_key: str,
        calibrated_policy_revision: str,
    ) -> ReleaseCommandReceipt:
        with self.transaction() as conn:
            return self.authorize_release_in(
                conn,
                observation_id,
                actor=actor,
                expected_control_generation=expected_control_generation,
                idempotency_key=idempotency_key,
                calibrated_policy_revision=calibrated_policy_revision,
            )

    def control_release(
        self,
        observation_id: UUID,
        *,
        action: Literal["pause", "resume", "cancel"],
        actor: str,
        expected_control_generation: int,
        idempotency_key: str,
        calibrated_policy_revision: str | None,
    ) -> ReleaseCommandReceipt:
        with self.transaction() as conn:
            return self.control_release_in(
                conn,
                observation_id,
                action=action,
                actor=actor,
                expected_control_generation=expected_control_generation,
                idempotency_key=idempotency_key,
                calibrated_policy_revision=calibrated_policy_revision,
            )

    def authorize_release_in(
        self,
        conn: Connection,
        observation_id: UUID,
        *,
        actor: str,
        expected_control_generation: int,
        idempotency_key: str,
        calibrated_policy_revision: str,
    ) -> ReleaseCommandReceipt:
        return self._release_command_in(
            conn,
            observation_id,
            action="authorize",
            actor=actor,
            expected=expected_control_generation,
            key=idempotency_key,
            revision=calibrated_policy_revision,
        )

    def control_release_in(
        self,
        conn: Connection,
        observation_id: UUID,
        *,
        action: Literal["pause", "resume", "cancel"],
        actor: str,
        expected_control_generation: int,
        idempotency_key: str,
        calibrated_policy_revision: str | None,
    ) -> ReleaseCommandReceipt:
        return self._release_command_in(
            conn,
            observation_id,
            action=action,
            actor=actor,
            expected=expected_control_generation,
            key=idempotency_key,
            revision=calibrated_policy_revision,
        )

    def _release_command_in(
        self,
        conn: Connection,
        observation_id: UUID,
        *,
        action: str,
        actor: str,
        expected: int,
        key: str,
        revision: str | None,
    ) -> ReleaseCommandReceipt:
        try:
            if (
                not isinstance(observation_id, UUID)
                or action not in ("authorize", "pause", "resume", "cancel")
                or type(expected) is not int
                or expected < 0
                or not isinstance(actor, str)
                or not actor
                or not isinstance(key, str)
                or not 1 <= len(key) <= 256
                or key.startswith("system:")
                or (
                    action in ("authorize", "resume")
                    and (not isinstance(revision, str) or not revision)
                )
                or (action in ("pause", "cancel") and revision is not None)
            ):
                raise PersistenceError("INVALID_INPUT")
            for value in (actor, key, revision):
                if value is not None:
                    storable_text(value)
        except ValueError:
            raise PersistenceError("INVALID_INPUT") from None
        try:
            return self._command_locked(
                conn, observation_id, action, actor, expected, key, revision
            )
        except psycopg.Error as exc:
            raise PersistenceError(self._error_code(exc)) from exc

    def _command_locked(
        self,
        conn: Connection,
        oid: UUID,
        action: str,
        actor: str,
        expected: int,
        key: str,
        revision: str | None,
    ) -> ReleaseCommandReceipt:
        initial = release_row(conn, oid)
        scope = release_scope(conn, initial["target_id"], lock=True)
        row = release_row(conn, oid, lock=True)
        fingerprint = sha256(
            canonical_json(
                {
                    "action": action,
                    "actor": actor,
                    "expected_control_generation": expected,
                    "calibrated_policy_revision": revision,
                }
            ).encode()
        )
        old = conn.execute(
            "SELECT request_sha256,result FROM opspilot_release_audit WHERE observation_id=%s AND idempotency_key=%s",
            (oid, key),
        ).fetchone()
        if old:
            if old["request_sha256"] != fingerprint:
                raise PersistenceError("IDEMPOTENCY_CONFLICT")
            return ReleaseCommandReceipt.model_validate_json(
                canonical_json({**old["result"], "replayed": True})
            )
        sessions = utc_values(
            conn.execute(
                "SELECT "
                + _release_session_columns()
                + " FROM opspilot_observation_sessions WHERE release_observation_id=%s AND state='authorized' ORDER BY session_id FOR UPDATE",
                (oid,),
            ).fetchall()
        )
        now = self._db_now(conn).astimezone(timezone.utc)
        reason = None
        policy = None
        if row["control_generation"] != expected:
            reason = "CONTROL_CONFLICT"
        elif row["status"] not in ("pending", "observing"):
            reason = "RELEASE_TERMINAL"
        elif action != "cancel" and now >= row["deadline_at"]:
            reason = "RELEASE_DEADLINE_EXPIRED"
        elif action == "authorize":
            if row["paused"]:
                reason = "RELEASE_PAUSED"
            elif sessions:
                reason = "OBSERVATION_ALREADY_AUTHORIZED"
            elif (
                row["observation_generation"] > 0
                or row["calibrated_policy_revision"] is not None
            ):
                reason = "REAUTHORIZATION_REQUIRED"
        elif action == "pause" and row["paused"]:
            reason = "ALREADY_PAUSED"
        elif (
            action == "resume"
            and not row["paused"]
            and sessions
            and not scope_blocked(scope, sessions[0])
        ):
            reason = "OBSERVATION_ALREADY_AUTHORIZED"
        if (
            reason is None
            and action == "resume"
            and not row["paused"]
            and row["calibrated_policy_revision"] is None
        ):
            reason = "REAUTHORIZATION_REQUIRED"
        if reason is None and action in ("authorize", "resume"):
            if scope["global_suspended"] or scope["target_suspended"]:
                reason = "SCOPE_SUSPENDED"
            else:
                reason, policy = self._checked_policy(conn, row, revision)
            if reason is None and row["used_sample_count"] >= row["max_samples"]:
                reason = "RELEASE_SAMPLE_BUDGET_EXHAUSTED"
        now = self._db_now(conn).astimezone(timezone.utc)
        if reason is None and action != "cancel" and now >= row["deadline_at"]:
            reason = "RELEASE_DEADLINE_EXPIRED"
        before = dict(row)
        sid = None
        ending = None
        if reason is None:
            for session in sessions:
                ending = end_release_session(
                    conn,
                    row,
                    session,
                    reason="cancelled" if action == "cancel" else "authority_revoked",
                    status_after="cancelled" if action == "cancel" else None,
                )
            row["observation_generation"] += 1
            if action != "authorize":
                row["control_generation"] += 1
            row["paused"] = action == "pause"
            if action == "cancel":
                row.update(status="cancelled", ended_reason="cancelled", ended_at=now)
            if action in ("authorize", "resume"):
                assert policy is not None
                if row["calibrated_policy_revision"] is None:
                    row.update(
                        calibrated_policy_revision=policy.revision,
                        calibrated_policy_content=canonical_json(
                            policy.content.model_dump(mode="json")
                        ),
                        calibrated_policy_sha256=policy.content_sha256,
                    )
                row["status"] = "observing"
                sid = uuid4()
                columns = "session_id,release_observation_id,purpose,target_id,target,subject_control_generation,observation_generation,authorized_by,health_profile_revision,authorized_global_generation,authorized_target_generation,deadline_at,max_samples,sample_interval_seconds,sustained_window_seconds,issued_sequence,active_sample_job_id,active_sample_sequence,active_sample_due_at,authorization_source,release_policy_revision,release_budget_used_at_authorization"
                values = [
                    sid,
                    oid,
                    "release_observation",
                    row["target_id"],
                    Jsonb(
                        {
                            k: v
                            for k, v in row["target_snapshot"].items()
                            if k != "workload"
                        }
                    ),
                    row["control_generation"],
                    row["observation_generation"],
                    actor,
                    row["health_profile_revision"],
                    scope["global_generation"],
                    scope["target_generation"],
                    row["deadline_at"],
                    row["max_samples"],
                    row["sample_interval_seconds"],
                    row["sustained_window_seconds"],
                    1,
                    uuid4(),
                    1,
                    now + timedelta(seconds=row["sample_interval_seconds"]),
                    None,
                    row["calibrated_policy_revision"],
                    row["used_sample_count"],
                ]
                conn.execute(
                    "INSERT INTO opspilot_observation_sessions("
                    + columns
                    + ") VALUES("
                    + ",".join(["%s"] * len(values))
                    + ")",
                    values,
                )
            fields = "status,paused,control_generation,observation_generation,ended_reason,ended_at,calibrated_policy_revision,calibrated_policy_content,calibrated_policy_sha256".split(
                ","
            )
            if action != "cancel":
                conn.execute(
                    "UPDATE opspilot_release_observations SET "
                    + ",".join(f"{f}=%s" for f in fields)
                    + ",updated_at=clock_timestamp() WHERE observation_id=%s",
                    [row[f] for f in fields] + [oid],
                )
        data = {
            "observation_id": str(oid),
            "action": action,
            "outcome": "applied" if reason is None else "rejected",
            "replayed": False,
            "reason": reason,
            "session_id": None if sid is None else str(sid),
            "ending_id": None if ending is None else str(ending),
            **{
                f: row[f]
                for f in (
                    "status",
                    "paused",
                    "control_generation",
                    "observation_generation",
                    "used_sample_count",
                )
            },
            "deadline_at": row["deadline_at"].isoformat(),
        }
        result = audit_release(
            conn,
            before,
            row,
            action=action,
            actor=actor,
            expected=expected,
            key=key,
            fingerprint=fingerprint,
            outcome=data["outcome"],
            reason=reason,
            session_id=sid,
            ending_id=ending,
            receipt=data,
        )
        if reason is None and action == "cancel":
            terminal_time = conn.execute(
                "SELECT recorded_at FROM opspilot_observation_endings WHERE ending_id=%s"
                if ending is not None
                else "SELECT recorded_at FROM opspilot_release_audit WHERE audit_id=%s",
                (ending if ending is not None else UUID(result["audit_id"]),),
            ).fetchone()
            assert terminal_time is not None
            conn.execute(
                "UPDATE opspilot_release_observations SET status=%s,paused=%s,control_generation=%s,observation_generation=%s,ended_reason=%s,ended_at=%s,updated_at=clock_timestamp() WHERE observation_id=%s",
                (
                    row["status"],
                    row["paused"],
                    row["control_generation"],
                    row["observation_generation"],
                    row["ended_reason"],
                    terminal_time["recorded_at"],
                    oid,
                ),
            )
        return ReleaseCommandReceipt.model_validate_json(canonical_json(result))

    def _checked_policy(
        self, conn: Connection, row: dict[str, Any], revision: str | None
    ) -> tuple[str | None, ReleasePolicy | None]:
        target = conn.execute(
            "SELECT integration_id,cluster_uid,namespace,resource_uid,workload FROM opspilot_targets WHERE target_id=%s",
            (row["target_id"],),
        ).fetchone()
        if target is None or any(v is None or v == "" for v in target.values()):
            return "TARGET_IDENTITY_MISSING", None
        if target != row["target_snapshot"]:
            return "TARGET_MISMATCH", None
        frozen = conn.execute(
            "SELECT content,content_sha256 FROM opspilot_health_profiles WHERE health_profile_revision=%s",
            (row["health_profile_revision"],),
        ).fetchone()
        if frozen is None:
            return "HEALTH_PROFILE_UNAVAILABLE", None
        try:
            profile = HealthProfile.model_validate_json(frozen["content"])
        except ValidationError:
            return "INVALID_RELEASE_POLICY", None
        if (
            profile.subject.service != target["workload"]
            or profile.subject.kubernetes_namespace != target["namespace"]
        ):
            return "HEALTH_PROFILE_TARGET_MISMATCH", None
        try:
            catalog = load_release_policy_catalog()
        except (OSError, ValueError):
            return "RELEASE_POLICY_CATALOG_INVALID", None
        policy = next((p for p in catalog.policies if p.revision == revision), None)
        if policy is None:
            return "RELEASE_POLICY_UNCALIBRATED", None
        content = policy.content
        if (
            content.target != target
            or content.health_profile_revision != row["health_profile_revision"]
            or content.health_profile_sha256 != frozen["content_sha256"]
            or sha256(frozen["content"].encode()) != frozen["content_sha256"]
            or profile.revision != row["health_profile_revision"]
            or content.min_tracking_seconds != row["min_tracking_seconds"]
            or any(
                getattr(profile.session, f) != row[f]
                for f in (
                    "deadline_seconds",
                    "max_samples",
                    "sample_interval_seconds",
                    "sustained_window_seconds",
                )
            )
            or (
                row["calibrated_policy_revision"] is not None
                and (
                    policy.revision != row["calibrated_policy_revision"]
                    or policy.content_sha256 != row["calibrated_policy_sha256"]
                )
            )
        ):
            return "RELEASE_POLICY_MISMATCH", None
        from opspilot.observer.release_evaluation import supported_profile

        if not supported_profile(profile):
            return "RELEASE_POLICY_UNSUPPORTED", None
        return None, policy

    def release_audit(
        self, observation_id: UUID, *, after: int = 0
    ) -> tuple[dict[str, Any], ...]:
        if not isinstance(observation_id, UUID) or type(after) is not int or after < 0:
            raise PersistenceError("INVALID_INPUT")
        with self.transaction(snapshot=True) as conn:
            return tuple(
                conn.execute(
                    "SELECT "
                    + _audit_columns()
                    + " FROM opspilot_release_audit WHERE observation_id=%s AND sequence>%s ORDER BY sequence",
                    (observation_id, after),
                ).fetchall()
            )

    def pending_anomaly_endings(
        self, *, limit: int = 20
    ) -> tuple[ReleaseAnomalyEnding, ...]:
        from opspilot.observation.release_types import (
            ReleaseAnomalyEnding,
            ReleaseEvaluation,
        )

        if type(limit) is not int or limit <= 0:
            raise PersistenceError("INVALID_INPUT")
        with self.transaction(snapshot=True) as conn:
            rows = conn.execute(
                "SELECT e.ending_id,e.session_id,e.release_observation_id AS observation_id,e.sample_id,e.job_id,e.job_sequence,s.subject_control_generation,s.observation_generation,s.release_policy_revision AS calibrated_policy_revision,m.release_evidence_sha256,m.release_evaluation,e.anomaly_key,e.recorded_at FROM opspilot_observation_endings e JOIN opspilot_release_observations r ON r.observation_id=e.release_observation_id JOIN opspilot_observation_sessions s ON s.session_id=e.session_id JOIN opspilot_observation_samples m ON m.sample_id=e.sample_id WHERE r.status='anomalous' AND r.incident_id IS NULL AND e.anomaly_key IS NOT NULL ORDER BY e.recorded_at,e.ending_id LIMIT %s",
                (limit,),
            ).fetchall()
        return tuple(
            ReleaseAnomalyEnding(
                **{
                    **r,
                    "result_sha256": sha256(
                        canonical_json(r["release_evaluation"]).encode()
                    ),
                    "release_evaluation": ReleaseEvaluation.model_validate_json(
                        canonical_json(r["release_evaluation"])
                    ),
                }
            )
            for r in rows
        )


def _release_session_columns() -> str:
    # Incident readers retain their original column lists and return shape.
    from opspilot.observation.store import _SESSION_VIEW_COLUMNS

    return (
        _SESSION_VIEW_COLUMNS
        + ",release_observation_id,subject_kind,subject_id,release_policy_revision,release_budget_used_at_authorization,tracking_since,tracking_until"
    )


def _audit_columns() -> str:
    return "audit_id,observation_id,sequence,idempotency_key,request_sha256,action,principal_kind,actor,outcome,reason,expected_control_generation,control_generation_before,control_generation_after,observation_generation_before,observation_generation_after,status_before,status_after,paused_before,paused_after,session_id,ending_id,result,recorded_at"
