"""Strict release sampling records shared by storage and offline evaluation."""

from datetime import timezone
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, AwareDatetime, Field, model_validator

from opspilot.domain.base import DTO, Count, Positive
from opspilot.domain.evidence import QueryWindow
from opspilot.domain.observation import ObservationSessionState, SampleOutcome
from opspilot.domain.subjects import ReleaseStatus, SubjectRef
from opspilot.observation.release_policy import (
    FormatVersion,
    ReleaseText,
    Sha256,
    sha256,
    storable_text,
)
from opspilot.observation.releases import Sha256Digest

UtcDatetime = Annotated[
    AwareDatetime, AfterValidator(lambda value: value.astimezone(timezone.utc))
]


class ReleaseSampleLease(DTO):
    session_id: UUID
    observation_id: UUID
    purpose: Literal["release_observation"]
    subject: SubjectRef
    job_id: UUID
    sequence: Positive
    owner: UUID
    epoch: Count
    lease_until: UtcDatetime
    subject_control_generation: Count
    observation_generation: Count
    health_profile_revision: ReleaseText
    calibrated_policy_revision: ReleaseText
    deadline_at: UtcDatetime
    sample_interval_seconds: Positive
    adopted_window_end: UtcDatetime | None
    global_suspension_generation: Count
    target_suspension_generation: Count

    @model_validator(mode="after")
    def bound_subject(self) -> "ReleaseSampleLease":
        if self.subject.kind != "release_observation" or self.subject.id != str(
            self.observation_id
        ):
            raise ValueError("SUBJECT_BINDING_MISMATCH")
        return self


class ReleaseQueryResult(DTO):
    query_key: ReleaseText
    query: ReleaseText
    source: Literal["prometheus"]
    evaluated_at: UtcDatetime
    sent_at: UtcDatetime | None
    status: Literal["ok", "no_data", "timeout", "failed"]
    http_status: Count | None
    detail: Annotated[str, AfterValidator(storable_text)]
    body_complete: bool
    raw: bytes = Field(max_length=30720)
    raw_sha256: Sha256

    @model_validator(mode="after")
    def valid_raw(self) -> "ReleaseQueryResult":
        if sha256(self.raw) != self.raw_sha256:
            raise ValueError("RAW_SHA256_MISMATCH")
        return self


class ReleaseSampleEvidence(DTO):
    format_version: FormatVersion
    sample_id: UUID
    window: Annotated[
        QueryWindow,
        AfterValidator(
            lambda value: QueryWindow(
                start=value.start.astimezone(timezone.utc),
                end=value.end.astimezone(timezone.utc),
            )
        ),
    ]
    queries: tuple[ReleaseQueryResult, ...] = Field(max_length=64)

    @model_validator(mode="after")
    def unique_queries(self) -> "ReleaseSampleEvidence":
        if len({q.query_key for q in self.queries}) != len(self.queries):
            raise ValueError("DUPLICATE_QUERY_KEY")
        return self


class ReleaseInstance(DTO):
    pod_uid: UUID
    pod_name: ReleaseText
    replicaset_name: ReleaseText
    images: dict[ReleaseText, Sha256Digest]


class ReleaseSignalVerdict(DTO):
    scope: Literal["context", "after"]
    signal_name: ReleaseText
    verdict: Literal[
        "healthy",
        "degraded",
        "missing",
        "stale",
        "timeout",
        "failed",
        "insufficient_samples",
        "insufficient_traffic",
    ]
    value: float | None = Field(allow_inf_nan=False)
    sample_count: Count | None
    latest_sample_at: UtcDatetime | None
    query_keys: tuple[ReleaseText, ...]


class ReleaseEvaluation(DTO):
    verdict: Literal["healthy", "degraded", "unknown"]
    outcome: SampleOutcome
    evidence_sufficient: bool
    reason_codes: tuple[ReleaseText, ...]
    instances: tuple[ReleaseInstance, ...]
    signals: tuple[ReleaseSignalVerdict, ...]


class ReleaseSampleReceipt(DTO):
    sample_id: UUID
    session_id: UUID
    observation_id: UUID
    replayed: bool
    accepted: bool
    disposition: Literal["adopted", "history_only"]
    reason: ReleaseText
    evaluation: ReleaseEvaluation
    session_state: ObservationSessionState
    release_status: ReleaseStatus
    used_sample_count: Count
    tracking_seconds: float = Field(ge=0)
    healthy_window_seconds: float = Field(ge=0)
    ending_id: UUID | None
    next_sample_due_at: UtcDatetime | None


class ReleaseAnomalyEnding(DTO):
    ending_id: UUID
    session_id: UUID
    observation_id: UUID
    sample_id: UUID
    job_id: UUID
    job_sequence: Positive
    subject_control_generation: Count
    observation_generation: Count
    calibrated_policy_revision: ReleaseText
    release_evidence_sha256: Sha256
    release_evaluation: ReleaseEvaluation
    anomaly_key: ReleaseText
    result_sha256: Sha256
    recorded_at: UtcDatetime
