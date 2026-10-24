"""Controller release commands and the shared release/session lock protocol."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
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
    from opspilot.observation.release_types import (
        ReleaseAnomalyEnding,
        ReleaseLinkDisposition,
        ReleaseLinkReceipt,
    )
    from opspilot.observation.releases import ReleaseRecord

#: Equal to ``opspilot.web.service._INTAKE_NAMESPACE`` (F11 R1): the ids a
#: consumption creates derive from the anomaly key like the intake ids do
#: from an idempotency key, so every later control treats the incident like
#: any other and a retry finds the same rows.
INTAKE_NAMESPACE = UUID("0f4c9d3e-2b7a-4a6e-9c1d-5e8f7a6b3c21")
#: F2 U-B1-2: a new release-anomaly incident is a warning (no severity).
RELEASE_INCIDENT_PRIORITY = 1
#: The actor of the event an anomaly appends to an open incident (R4).
RELEASE_EVENT_ACTOR = "release_observation"
#: R7: the handoff reason when the release's target is not resolvable.
RELEASE_TARGET_UNRESOLVED = "RELEASE_TARGET_UNRESOLVED"
_TARGET_FIELDS = (
    "integration_id",
    "cluster_uid",
    "namespace",
    "resource_uid",
    "workload",
)
_LINK_COLUMNS = "observation_id,ending_id,anomaly_key,disposition,incident_id,related_incident_id,run_id,input_id,handoff_reason,comparison_id,consumed_at"


def release_intake_ids(anomaly_key: str) -> tuple[str, UUID, UUID]:
    """R1: the intake key, incident id and first Run id an anomaly opens."""
    intake_key = "intake:" + anomaly_key
    return (
        intake_key,
        uuid5(INTAKE_NAMESPACE, intake_key),
        uuid5(INTAKE_NAMESPACE, intake_key + ":run:0"),
    )


def release_event_id(anomaly_key: str, incident_id: UUID) -> UUID:
    """R1: the id of the one event an anomaly appends to ``incident_id``."""
    return uuid5(INTAKE_NAMESPACE, f"{anomaly_key}:event:{incident_id}")


def comparison_wait_seconds() -> int:
    """How long after its anomaly ending a consumption waits for the
    before/after comparison (P4-01): the Observer's three attempt leases plus
    a minute. Past it the anomaly is consumed with the comparison stated
    ``unavailable``, so a missing Observer, or a suspended scope the Observer
    does not compare in, never keeps the anomaly from an incident."""
    from opspilot.observation.release_comparison import (
        COMPARISON_LEASE_SECONDS,
        COMPARISON_MAX_ATTEMPTS,
    )

    return COMPARISON_MAX_ATTEMPTS * COMPARISON_LEASE_SECONDS + 60


@dataclass(frozen=True)
class ReleaseConsumption:
    """What ``consume_anomaly_in`` decided for one anomaly ending, handed to
    the caller's ``opening`` before anything is written: the disposition
    and the ids it writes under, the release, its ending and the stored
    comparison (``None`` when the wait for one ran out), and
    ``consumed_at``, the transaction's database time the new Run's frame
    ends at (R3)."""

    disposition: ReleaseLinkDisposition
    release: ReleaseRecord
    ending: ReleaseAnomalyEnding
    comparison: Mapping[str, Any] | None
    consumed_at: datetime
    intake_key: str
    incident_id: UUID
    run_id: UUID | None
    related_incident_id: UUID | None
    input_id: UUID | None
    handoff_reason: str | None


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

    # -- anomaly consumption (F11 PR-4b) --------------------------------

    def consume_anomaly(
        self,
        ending: ReleaseAnomalyEnding,
        *,
        opening: Callable[[ReleaseConsumption], Mapping[str, Any]],
    ) -> ReleaseLinkReceipt | None:
        """``consume_anomaly_in`` in its own transaction."""
        with self.transaction() as conn:
            return self.consume_anomaly_in(conn, ending, opening=opening)

    def consume_anomaly_in(
        self,
        conn: Connection,
        ending: ReleaseAnomalyEnding,
        *,
        opening: Callable[[ReleaseConsumption], Mapping[str, Any]],
    ) -> ReleaseLinkReceipt | None:
        """Consume one anomaly ending (P4-02, P4-03): open or correlate its
        incident and link it to the release, all in the caller's
        transaction, or nothing.

        The disposition follows the shared open-incident rule (F2 PR-C C3,
        ``choose_correlation``) over every incident on the release's target:
        a non-closed one takes the anomaly as one event (``attached``, no
        Run, nothing else of the incident changes); when all are closed a
        new incident relates to the latest (``created_related``); with none
        a new incident stands alone (``created``). A target whose registry
        row is incomplete or differs from the release's snapshot is not
        correlated: the anomaly is handed to a human (``handoff_created``,
        R7). ``opening`` is called once with the decision and returns what
        only the caller can build -- for a new Run its ``deadline``,
        ``budget_limit``, ``versions``, ``input`` and intake ``ledger`` rows,
        for an event its ``content``.

        Returns ``None`` while the ending still waits for its comparison
        (``comparison_wait_seconds``); the receipt of the committed link
        otherwise, ``replayed`` when an earlier consumption made it.

        Locks (R2 as aligned with PR-C r3): a transaction advisory lock on
        the observation, then the scope and the target suspension row (FOR
        SHARE), then the candidate incidents (FOR UPDATE, id order), then
        the release row; the same scope -> incidents -> release order the
        suspension and release command paths take.
        """
        from opspilot.observation.release_types import ReleaseAnomalyEnding

        if not isinstance(ending, ReleaseAnomalyEnding) or not callable(opening):
            raise PersistenceError("INVALID_INPUT")
        try:
            return self._consume_locked(conn, ending, opening)
        except psycopg.Error as exc:
            raise PersistenceError(self._error_code(exc)) from exc

    def _consume_locked(
        self,
        conn: Connection,
        ending: ReleaseAnomalyEnding,
        opening: Callable[[ReleaseConsumption], Mapping[str, Any]],
    ) -> ReleaseLinkReceipt | None:
        from opspilot.observation.releases import ReleaseRecord
        from opspilot.persistence.alerts import _ledger
        from opspilot.persistence.controls import append_input_in
        from opspilot.persistence.correlation import (
            Correlation,
            CorrelationCandidate,
            choose_correlation,
        )
        from opspilot.persistence.incidents import _check_priority, _IncidentOps

        oid = ending.observation_id
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            ("release-link:" + str(oid),),
        )
        link = self._link_in(conn, oid)
        if link is not None:
            if (
                link["ending_id"] != ending.ending_id
                or link["anomaly_key"] != ending.anomaly_key
            ):
                raise PersistenceError("IDENTITY_CONFLICT")
            return _link_receipt(link, replayed=True)
        stored = self._anomaly_ending_in(conn, oid)
        if stored is None or stored != ending:
            raise PersistenceError("IDENTITY_CONFLICT")
        release = release_row(conn, oid)
        if release["status"] != "anomalous" or release["incident_id"] is not None:
            raise PersistenceError("INCONSISTENT_STATE")
        comparison = conn.execute(
            "SELECT comparison_id,comparison FROM opspilot_release_comparisons WHERE ending_id=%s",
            (ending.ending_id,),
        ).fetchone()
        if comparison is None and self._db_now(conn) < ending.recorded_at + timedelta(
            seconds=comparison_wait_seconds()
        ):
            return None
        target = conn.execute(
            "SELECT "
            + ",".join(_TARGET_FIELDS)
            + " FROM opspilot_targets WHERE target_id=%s",
            (release["target_id"],),
        ).fetchone()
        resolved = (
            target is not None
            and all(isinstance(v, str) and v for v in target.values())
            and dict(target) == release["target_snapshot"]
        )
        self._lock_scope(conn, None)
        correlation = Correlation(attach_to=None, related_to=None)
        if resolved:
            self._require_row(
                conn.execute(
                    "SELECT suspended FROM opspilot_target_suspensions WHERE target_id=%s FOR SHARE",
                    (release["target_id"],),
                )
            )
            rows = conn.execute(
                "SELECT incident_id,lifecycle,created_at FROM opspilot_incidents WHERE target_id=%s ORDER BY incident_id FOR UPDATE",
                (release["target_id"],),
            ).fetchall()
            correlation = choose_correlation(
                [
                    CorrelationCandidate(
                        row["incident_id"], row["lifecycle"], row["created_at"]
                    )
                    for row in rows
                ]
            )
        release = release_row(conn, oid, lock=True)
        if release["status"] != "anomalous" or release["incident_id"] is not None:
            raise PersistenceError("INCONSISTENT_STATE")
        consumed_at = self._require_row(
            conn.execute("SELECT transaction_timestamp() AS at")
        )["at"]
        intake_key, new_incident, new_run = release_intake_ids(ending.anomaly_key)
        disposition: ReleaseLinkDisposition
        incident_id = new_incident
        run_id: UUID | None = new_run
        input_id: UUID | None = None
        handoff_reason: str | None = None
        if not resolved:
            disposition, run_id = "handoff_created", None
            handoff_reason = RELEASE_TARGET_UNRESOLVED
        elif correlation.attach_to is not None:
            disposition, run_id = "attached", None
            incident_id = correlation.attach_to
            input_id = release_event_id(ending.anomaly_key, incident_id)
        elif correlation.related_to is not None:
            disposition = "created_related"
        else:
            disposition = "created"
        plan = ReleaseConsumption(
            disposition=disposition,
            release=ReleaseRecord.model_validate(release),
            ending=ending,
            comparison=None if comparison is None else comparison["comparison"],
            consumed_at=consumed_at,
            intake_key=intake_key,
            incident_id=incident_id,
            run_id=run_id,
            related_incident_id=correlation.related_to,
            input_id=input_id,
            handoff_reason=handoff_reason,
        )
        spec = opening(plan)
        if disposition == "attached":
            assert input_id is not None
            append_input_in(
                conn,
                incident_id,
                input_id,
                dict(spec["content"]),
                actor=RELEASE_EVENT_ACTOR,
            )
        elif disposition == "handoff_created":
            # R7, like an alert handed to a human at intake (E5): no Run, no
            # target binding, ``waiting_human`` and outside the claim listing.
            _check_priority(RELEASE_INCIDENT_PRIORITY)
            inserted = conn.execute(
                "INSERT INTO opspilot_incidents(incident_id,intake_key,state,lifecycle,current_run_id,target_id,priority) VALUES(%s,%s,'waiting_human','open',NULL,NULL,%s) ON CONFLICT DO NOTHING RETURNING incident_id",
                (incident_id, intake_key, RELEASE_INCIDENT_PRIORITY),
            ).fetchone()
            if inserted is None:
                raise PersistenceError("IDENTITY_CONFLICT")
        else:
            assert run_id is not None
            _IncidentOps(self.dsn, pool=self.pool_config)._accept_in(
                conn,
                incident_id,
                run_id,
                intake_key,
                deadline=spec["deadline"],
                budget_limit=spec["budget_limit"],
                versions=dict(spec["versions"]),
                input=spec.get("input"),
                target_id=release["target_id"],
                priority=RELEASE_INCIDENT_PRIORITY,
                related_incident_id=correlation.related_to,
            )
            _ledger(conn, spec.get("ledger") or ())
        conn.execute(
            "INSERT INTO opspilot_release_incident_links(observation_id,ending_id,anomaly_key,disposition,incident_id,related_incident_id,run_id,input_id,handoff_reason,comparison_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                oid,
                ending.ending_id,
                ending.anomaly_key,
                disposition,
                incident_id,
                correlation.related_to,
                run_id,
                input_id,
                handoff_reason,
                None if comparison is None else comparison["comparison_id"],
            ),
        )
        conn.execute(
            "UPDATE opspilot_release_observations SET incident_id=%s WHERE observation_id=%s",
            (incident_id, oid),
        )
        committed = self._link_in(conn, oid)
        assert committed is not None
        return _link_receipt(committed, replayed=False)

    @staticmethod
    def _link_in(conn: Connection, observation_id: UUID) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT "
            + _LINK_COLUMNS
            + " FROM opspilot_release_incident_links WHERE observation_id=%s",
            (observation_id,),
        ).fetchone()
        return None if row is None else dict(row)

    @staticmethod
    def _anomaly_ending_in(
        conn: Connection, observation_id: UUID
    ) -> ReleaseAnomalyEnding | None:
        """The release's unique anomaly ending, as ``pending_anomaly_endings``
        reads it (``None`` without one)."""
        from opspilot.observation.release_types import (
            ReleaseAnomalyEnding,
            ReleaseEvaluation,
        )

        rows = conn.execute(
            "SELECT e.ending_id,e.session_id,e.release_observation_id AS observation_id,e.sample_id,e.job_id,e.job_sequence,s.subject_control_generation,s.observation_generation,s.release_policy_revision AS calibrated_policy_revision,m.release_evidence_sha256,m.release_evaluation,e.anomaly_key,e.recorded_at FROM opspilot_observation_endings e JOIN opspilot_observation_sessions s ON s.session_id=e.session_id JOIN opspilot_observation_samples m ON m.sample_id=e.sample_id WHERE e.release_observation_id=%s AND e.anomaly_key IS NOT NULL",
            (observation_id,),
        ).fetchall()
        if len(rows) != 1:
            return None
        r = rows[0]
        return ReleaseAnomalyEnding(
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

    def release_link(self, observation_id: UUID) -> ReleaseLinkReceipt | None:
        """The committed consumption of ``observation_id``'s anomaly, if any."""
        if not isinstance(observation_id, UUID):
            raise PersistenceError("INVALID_INPUT")
        with self.transaction(snapshot=True) as conn:
            row = self._link_in(conn, observation_id)
        return None if row is None else _link_receipt(row, replayed=False)

    def release_incident_records(self, incident_id: UUID) -> dict[str, Any] | None:
        """What a rebuild of the release incident ``incident_id`` reads (R6),
        one snapshot: the link that created it (``created`` or
        ``created_related``), the release, its anomaly ending and the
        comparison the link names. ``None`` for an incident no anomaly
        created; a part that cannot be read is ``None`` in the result."""
        if not isinstance(incident_id, UUID):
            raise PersistenceError("INVALID_INPUT")
        from opspilot.observation.releases import ReleaseRecord

        with self.transaction(snapshot=True) as conn:
            link = conn.execute(
                "SELECT "
                + _LINK_COLUMNS
                + " FROM opspilot_release_incident_links WHERE incident_id=%s AND disposition IN ('created','created_related')",
                (incident_id,),
            ).fetchall()
            if not link:
                return None
            if len(link) != 1:
                return {
                    "link": None,
                    "release": None,
                    "ending": None,
                    "comparison": None,
                }
            row = dict(link[0])
            release = conn.execute(
                "SELECT "
                + ",".join(ReleaseRecord.model_fields)
                + " FROM opspilot_release_observations WHERE observation_id=%s",
                (row["observation_id"],),
            ).fetchone()
            ending = self._anomaly_ending_in(conn, row["observation_id"])
            comparison = (
                None
                if row["comparison_id"] is None
                else conn.execute(
                    "SELECT comparison FROM opspilot_release_comparisons WHERE comparison_id=%s",
                    (row["comparison_id"],),
                ).fetchone()
            )
        return {
            "link": utc_values(row),
            "release": None
            if release is None
            else ReleaseRecord.model_validate(utc_values(release)),
            "ending": ending,
            "comparison": None if comparison is None else comparison["comparison"],
        }


def _link_receipt(row: Mapping[str, Any], *, replayed: bool) -> ReleaseLinkReceipt:
    from opspilot.observation.release_types import ReleaseLinkReceipt

    return ReleaseLinkReceipt(**dict(utc_values(dict(row))), replayed=replayed)


def _release_session_columns() -> str:
    # Incident readers retain their original column lists and return shape.
    from opspilot.observation.store import _SESSION_VIEW_COLUMNS

    return (
        _SESSION_VIEW_COLUMNS
        + ",release_observation_id,subject_kind,subject_id,release_policy_revision,release_budget_used_at_authorization,tracking_since,tracking_until"
    )


def _audit_columns() -> str:
    return "audit_id,observation_id,sequence,idempotency_key,request_sha256,action,principal_kind,actor,outcome,reason,expected_control_generation,control_generation_before,control_generation_after,observation_generation_before,observation_generation_after,status_before,status_after,paused_before,paused_after,session_id,ending_id,result,recorded_at"
