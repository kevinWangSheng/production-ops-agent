"""Frozen release events and transactional Controller authorization/control."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb
from pydantic import AfterValidator, AwareDatetime, Field, ValidationError

from opspilot.domain.base import DTO, Count, Positive, Text
from opspilot.observation.bindings import _TargetProfileOps
from opspilot.observation.release_commands import (
    ReleaseCommandReceipt as ReleaseCommandReceipt,
)
from opspilot.observation.release_commands import (
    _ReleaseCommandOps,
)
from opspilot.observation.release_policy import ReleaseText, Sha256
from opspilot.observer.health_profile import HealthProfile
from opspilot.persistence.base import Connection, PersistenceError
from opspilot.redaction import redact_credentials


def _storable_release_text(value: str) -> str:
    """Reject input that PostgreSQL text/JSONB or UTF-8 cannot represent."""
    if "\x00" in value:
        raise ValueError("RELEASE_TEXT_NOT_STORABLE")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("RELEASE_TEXT_NOT_STORABLE") from None
    return value


_ReleaseText = Annotated[Text, AfterValidator(_storable_release_text)]
Sha256Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class ReleaseRevision(DTO):
    images: dict[_ReleaseText, Sha256Digest] = Field(min_length=1)
    pod_template_digest: Sha256Digest


class ReleaseAuxiliary(DTO):
    deployment_uid: _ReleaseText | None = None
    before_replicaset_uid: _ReleaseText | None = None
    after_replicaset_uid: _ReleaseText | None = None
    before_pod_template_hash: _ReleaseText | None = None
    after_pod_template_hash: _ReleaseText | None = None
    deployment_generation: Count | None = None


class ReleaseEvent(DTO):
    format_version: Literal[1]
    source: _ReleaseText
    source_release_id: _ReleaseText
    released_at: AwareDatetime
    before: ReleaseRevision
    after: ReleaseRevision
    auxiliary: ReleaseAuxiliary = Field(default_factory=ReleaseAuxiliary)


class ReleaseReceipt(DTO):
    outcome: Literal["created", "replayed", "rejected"]
    observation_id: UUID | None
    event_id: int
    status: str | None
    reason: str | None


class ReleaseRecord(DTO):
    observation_id: UUID
    format_version: Literal[1]
    source: Text
    source_release_id: Text
    target_id: UUID
    target_snapshot: dict[str, str]
    before_revision: ReleaseRevision
    after_revision: ReleaseRevision
    auxiliary: ReleaseAuxiliary
    semantic_sha256: Text
    health_profile_revision: Text
    timing_policy_version: Literal[1]
    released_at: AwareDatetime
    received_at: AwareDatetime
    deadline_at: AwareDatetime
    deadline_seconds: Positive
    min_tracking_seconds: Positive
    sample_interval_seconds: Positive
    sustained_window_seconds: Positive
    max_samples: Positive
    status: Literal[
        "pending", "observing", "healthy", "anomalous", "unknown", "cancelled"
    ]
    control_generation: Count
    observation_generation: Count
    paused: bool
    ended_reason: str | None
    ended_at: AwareDatetime | None
    incident_id: UUID | None
    supersedes_observation_id: UUID | None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    used_sample_count: Count
    calibrated_policy_revision: ReleaseText | None
    calibrated_policy_content: ReleaseText | None
    calibrated_policy_sha256: Sha256 | None


class ReleaseEventRecord(DTO):
    event_id: int
    observation_id: UUID | None
    sequence: Positive | None
    kind: Literal["release_delivery"]
    outcome: Literal["created", "replayed", "rejected"]
    reason: str | None
    source: Text
    source_release_id: Text
    actor: Text
    received_at: AwareDatetime
    raw_sha256: Text
    semantic_sha256: Text
    audit_text: str
    truncated: bool


_RELEASE_COLUMNS = ",".join(ReleaseRecord.model_fields)
_EVENT_COLUMNS = ",".join(ReleaseEventRecord.model_fields)
_BUSINESS_REFUSALS = frozenset(
    {
        "TARGET_MISMATCH",
        "TARGET_IDENTITY_MISSING",
        "HEALTH_PROFILE_UNAVAILABLE",
        "HEALTH_PROFILE_TARGET_MISMATCH",
    }
)


def _semantic_sha256(
    event: ReleaseEvent, target_id: UUID, target_snapshot: Mapping[str, Any] | None
) -> str:
    # UTC normalization makes equivalent offsets replay the same instant.
    content = {
        "target_id": str(target_id),
        "target_snapshot": None if target_snapshot is None else dict(target_snapshot),
        "before": event.before.model_dump(mode="json"),
        "after": event.after.model_dump(mode="json"),
        "released_at": event.released_at.astimezone(timezone.utc).isoformat(),
        "auxiliary": event.auxiliary.model_dump(mode="json"),
    }
    canonical = json.dumps(
        content, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class _ExistingRelease(Exception):
    """Roll back tentative bindings when another writer won the unique key."""

    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row


class ReleaseStore(_ReleaseCommandOps):
    """Register frozen releases and apply audited Controller commands."""

    @staticmethod
    def _error_code(exc: psycopg.Error) -> str:
        if isinstance(
            exc, (psycopg.IntegrityError, psycopg.errors.InsufficientPrivilege)
        ):
            return "INTEGRITY_REFUSED"
        return _TargetProfileOps._error_code(exc)

    def record_release(
        self,
        *,
        event: ReleaseEvent,
        target_id: UUID,
        identity: Mapping[str, str] | None,
        health_profile_revision: str,
        health_profile: str,
        min_tracking_seconds: int,
        actor: str,
        raw_body: bytes,
    ) -> ReleaseReceipt:
        with self.transaction() as conn:
            return self.record_release_in(
                conn,
                event=event,
                target_id=target_id,
                identity=identity,
                health_profile_revision=health_profile_revision,
                health_profile=health_profile,
                min_tracking_seconds=min_tracking_seconds,
                actor=actor,
                raw_body=raw_body,
            )

    def record_release_in(
        self,
        conn: Connection,
        *,
        event: ReleaseEvent,
        target_id: UUID,
        identity: Mapping[str, str] | None,
        health_profile_revision: str,
        health_profile: str,
        min_tracking_seconds: int,
        actor: str,
        raw_body: bytes,
    ) -> ReleaseReceipt:
        if (
            not isinstance(event, ReleaseEvent)
            or not isinstance(target_id, UUID)
            or not isinstance(actor, str)
            or not actor
            or not isinstance(raw_body, bytes)
        ):
            raise PersistenceError("INVALID_INPUT")
        # Revalidate mutable containers inside the frozen DTO before any writes.
        event = ReleaseEvent.model_validate(event.model_dump())
        try:
            return self._record_in(
                conn,
                event,
                target_id,
                identity,
                health_profile_revision,
                health_profile,
                min_tracking_seconds,
                actor,
                raw_body,
            )
        except psycopg.Error as exc:
            # The caller owns the transaction and must roll it back on failure.
            raise PersistenceError(self._error_code(exc)) from exc

    def _record_in(
        self,
        conn: Connection,
        event: ReleaseEvent,
        target_id: UUID,
        identity: Mapping[str, str] | None,
        health_profile_revision: str,
        health_profile: str,
        min_tracking_seconds: int,
        actor: str,
        raw_body: bytes,
    ) -> ReleaseReceipt:
        now = self._db_now(conn)
        row = self._find_in(conn, event.source, event.source_release_id)
        # The registry is authoritative; an omitted configuration identity is
        # the same target as its equivalent dictionary, not a semantic change.
        target_snapshot = conn.execute(
            "SELECT integration_id,cluster_uid,namespace,resource_uid,workload "
            "FROM opspilot_targets WHERE target_id=%s",
            (target_id,),
        ).fetchone()
        semantic = _semantic_sha256(event, target_id, target_snapshot)
        outcome: Literal["created", "replayed", "rejected"] = "rejected"
        reason = None
        if row is None:
            try:
                # Business refusal rolls back registry/profile writes, while the
                # enclosing transaction can still commit its one audit receipt.
                with conn.transaction():
                    if event.released_at > now:
                        reason = "RELEASE_TIME_IN_FUTURE"
                    else:
                        self._complete_identity(conn, target_id, identity)
                        target = self._registered_target(conn, target_id, None)
                        workload = self._require_row(
                            conn.execute(
                                "SELECT workload FROM opspilot_targets WHERE target_id=%s",
                                (target_id,),
                            )
                        )["workload"]
                        target_snapshot = {
                            **target.model_dump(exclude={"revision"}),
                            "workload": workload,
                        }
                        semantic = _semantic_sha256(event, target_id, target_snapshot)
                        if health_profile is None or health_profile == "":
                            raise PersistenceError("HEALTH_PROFILE_UNAVAILABLE")
                        if (
                            not isinstance(health_profile, str)
                            or not isinstance(health_profile_revision, str)
                            or not health_profile_revision
                        ):
                            raise PersistenceError("INVALID_RELEASE_POLICY")
                        try:
                            profile = HealthProfile.model_validate_json(health_profile)
                        except ValidationError as exc:
                            raise PersistenceError("INVALID_RELEASE_POLICY") from exc
                        if (
                            profile.subject.kubernetes_namespace,
                            profile.subject.service,
                        ) != (
                            target.namespace,
                            workload,
                        ):
                            raise PersistenceError("HEALTH_PROFILE_TARGET_MISMATCH")
                        policy = profile.session
                        if (
                            type(min_tracking_seconds) is not int
                            or min_tracking_seconds <= 0
                            or max(
                                min_tracking_seconds, policy.sustained_window_seconds
                            )
                            >= policy.deadline_seconds
                        ):
                            raise PersistenceError("INVALID_RELEASE_POLICY")
                        try:
                            self._store_profile(
                                conn, health_profile_revision, health_profile
                            )
                        except PersistenceError as exc:
                            if str(exc) not in {
                                "HEALTH_PROFILE_REVISION_MISMATCH",
                                "INVALID_INPUT",
                            }:
                                raise
                            raise PersistenceError("INVALID_RELEASE_POLICY") from exc
                        deadline = event.released_at + timedelta(
                            seconds=policy.deadline_seconds
                        )
                        for attempt in range(2):
                            # Identity/profile locks may have consumed the remaining
                            # window; reception is sampled again at insertion.
                            now = self._db_now(conn)
                            late = now >= deadline
                            try:
                                with conn.transaction():
                                    row = conn.execute(
                                        "INSERT INTO opspilot_release_observations("
                                        "observation_id,format_version,source,source_release_id,target_id,"
                                        "target_snapshot,before_revision,after_revision,auxiliary,semantic_sha256,"
                                        "health_profile_revision,timing_policy_version,released_at,deadline_at,"
                                        "deadline_seconds,min_tracking_seconds,sample_interval_seconds,"
                                        "sustained_window_seconds,max_samples,status,ended_reason,ended_at) "
                                        "VALUES(%s,1,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                                        "ON CONFLICT (source,source_release_id) WHERE supersedes_observation_id IS NULL "
                                        "DO NOTHING RETURNING " + _RELEASE_COLUMNS,
                                        (
                                            uuid4(),
                                            event.source,
                                            event.source_release_id,
                                            target_id,
                                            Jsonb(target_snapshot),
                                            Jsonb(event.before.model_dump()),
                                            Jsonb(event.after.model_dump()),
                                            Jsonb(event.auxiliary.model_dump()),
                                            semantic,
                                            health_profile_revision,
                                            event.released_at,
                                            deadline,
                                            policy.deadline_seconds,
                                            min_tracking_seconds,
                                            policy.sample_interval_seconds,
                                            policy.sustained_window_seconds,
                                            policy.max_samples,
                                            "unknown" if late else "pending",
                                            "late_delivery" if late else None,
                                            now if late else None,
                                        ),
                                    ).fetchone()
                            except psycopg.errors.InsufficientPrivilege as exc:
                                # A deadline crossed between the clock read and
                                # the INSERT guard. Roll back only that attempt,
                                # then initialize late using the new DB instant.
                                if (
                                    attempt != 0
                                    or late
                                    or exc.diag.message_primary
                                    != "release initialization refused"
                                    or self._db_now(conn) < deadline
                                ):
                                    raise
                            else:
                                break
                        if row is None:
                            existing = self._find_in(
                                conn, event.source, event.source_release_id
                            )
                            assert existing is not None
                            raise _ExistingRelease(existing)
                        if row is not None:
                            outcome = "created"
                            reason = row["ended_reason"]
                            now = row["received_at"]
            except _ExistingRelease as exc:
                row = exc.row
            except PersistenceError as exc:
                if str(exc) not in _BUSINESS_REFUSALS | {"INVALID_RELEASE_POLICY"}:
                    raise
                reason = str(exc)
            if row is None:
                row = self._find_in(conn, event.source, event.source_release_id)
            if row is None and reason is None:
                # ON CONFLICT waited for the first writer. READ COMMITTED now
                # sees its committed row; uniqueness decides who created it.
                row = self._find_in(conn, event.source, event.source_release_id)
                assert row is not None
        if row is not None and outcome != "created":
            if row["semantic_sha256"] == semantic:
                # Normalizing the fingerprint does not exempt a supplied
                # identity from the existing registry comparison rule.
                try:
                    with conn.transaction():
                        self._complete_identity(conn, target_id, identity)
                except PersistenceError as exc:
                    if str(exc) not in _BUSINESS_REFUSALS:
                        raise
                    outcome, reason = "rejected", str(exc)
                else:
                    outcome, reason = "replayed", None
            else:
                outcome, reason = "rejected", "RELEASE_IDENTITY_CONFLICT"
        return self._audit_in(
            conn, event, row, outcome, reason, actor, raw_body, semantic, now
        )

    def _audit_in(
        self,
        conn: Connection,
        event: ReleaseEvent,
        row: dict[str, Any] | None,
        outcome: Literal["created", "replayed", "rejected"],
        reason: str | None,
        actor: str,
        raw_body: bytes,
        semantic: str,
        now: datetime,
    ) -> ReleaseReceipt:
        observation_id = None if row is None else row["observation_id"]
        sequence = None
        if observation_id is not None:
            # No UPDATE grant on any release column: use a per-subject xact
            # lock for sequence allocation rather than granting FOR UPDATE.
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ("release-audit:" + str(observation_id),),
            )
            sequence = self._require_row(
                conn.execute(
                    "SELECT COALESCE(max(sequence),0)+1 AS sequence FROM opspilot_release_events "
                    "WHERE observation_id=%s",
                    (observation_id,),
                )
            )["sequence"]
        # PostgreSQL text cannot hold NUL; raw_sha256 still covers the exact bytes.
        decoded = raw_body.decode("utf-8", errors="replace").replace("\x00", "\ufffd")
        encoded = redact_credentials(decoded).encode("utf-8")
        truncated = len(encoded) > 16384
        audit_text = encoded[:16384].decode("utf-8", errors="ignore")
        audit = self._require_row(
            conn.execute(
                "INSERT INTO opspilot_release_events(observation_id,sequence,kind,outcome,reason,"
                "source,source_release_id,actor,received_at,raw_sha256,semantic_sha256,audit_text,truncated) "
                "VALUES(%s,%s,'release_delivery',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING event_id",
                (
                    observation_id,
                    sequence,
                    outcome,
                    reason,
                    event.source,
                    event.source_release_id,
                    actor,
                    now,
                    hashlib.sha256(raw_body).hexdigest(),
                    semantic,
                    audit_text,
                    truncated,
                ),
            )
        )
        return ReleaseReceipt(
            outcome=outcome,
            observation_id=observation_id,
            event_id=audit["event_id"],
            status=None if row is None else row["status"],
            reason=reason,
        )

    @staticmethod
    def _find_in(
        conn: Connection, source: str, source_release_id: str
    ) -> dict[str, Any] | None:
        return conn.execute(
            "SELECT " + _RELEASE_COLUMNS + " FROM opspilot_release_observations "
            "WHERE source=%s AND source_release_id=%s AND supersedes_observation_id IS NULL",
            (source, source_release_id),
        ).fetchone()

    def get_release(self, observation_id: UUID) -> ReleaseRecord:
        with self.transaction(snapshot=True) as conn:
            row = conn.execute(
                "SELECT "
                + _RELEASE_COLUMNS
                + " FROM opspilot_release_observations WHERE observation_id=%s",
                (observation_id,),
            ).fetchone()
        if row is None:
            raise PersistenceError("NOT_FOUND")
        return ReleaseRecord.model_validate(row)

    def find_release(self, source: str, source_release_id: str) -> ReleaseRecord | None:
        with self.transaction(snapshot=True) as conn:
            row = self._find_in(conn, source, source_release_id)
        return None if row is None else ReleaseRecord.model_validate(row)

    def release_events(
        self, observation_id: UUID, *, after: int = 0
    ) -> tuple[ReleaseEventRecord, ...]:
        if type(after) is not int or after < 0:
            raise PersistenceError("INVALID_INPUT")
        with self.transaction(snapshot=True) as conn:
            rows = conn.execute(
                "SELECT "
                + _EVENT_COLUMNS
                + " FROM opspilot_release_events WHERE observation_id=%s "
                "AND sequence>%s ORDER BY sequence",
                (observation_id, after),
            ).fetchall()
        return tuple(ReleaseEventRecord.model_validate(row) for row in rows)
