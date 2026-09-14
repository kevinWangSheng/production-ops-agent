"""`ObservationSession` (C3 section 4, adoption rules from C3 section 10).

A session binds its purpose, subject, immutable target, the subject control
generation, its own observation generation, the health-rule revision and an
explicit authorization. Adoption is a decision, not an exception: an invalid
identity, authorization or watermark keeps the sample as history instead of
discarding it.

Adoption and health are separate questions. A legal ``no_data``/``stale``/error
sample is adoptable as an unknown observation, but it never confirms health and
never extends the healthy window, and a target with no bound health-rule
revision can still be investigated while never confirming recovery.
"""

from typing import Literal

from pydantic import AwareDatetime, model_validator

from .base import DTO, Count, DomainError, Positive, StateMachine, Text
from .evidence import QueryWindow
from .intake import Target
from .subjects import (
    ACTIVE_INCIDENT_LIFECYCLES,
    ACTIVE_RELEASE_STATUSES,
    SubjectRef,
)

ObservationPurpose = Literal["incident_recovery", "release_observation"]
ObservationSessionState = Literal["authorized", "completed", "revoked", "expired"]
SampleOutcome = Literal["healthy", "degraded", "no_data", "stale", "timeout", "failed"]

OBSERVATION_SESSION = StateMachine(
    "observation_session",
    {
        "authorized": {
            "observation_completed": "completed",
            "authority_revoked": "revoked",
            "deadline_expired": "expired",
        },
        "completed": {},
        "revoked": {},
        "expired": {},
    },
)

_ACTIVE_SUBJECT_STATES: dict[str, frozenset[str]] = {
    "incident_recovery": ACTIVE_INCIDENT_LIFECYCLES,
    "release_observation": ACTIVE_RELEASE_STATUSES,
}

_SUBJECT_KIND_FOR_PURPOSE: dict[str, str] = {
    "incident_recovery": "incident",
    "release_observation": "release_observation",
}


class ObservationSession(DTO):
    """One explicitly authorized period of independent health observation."""

    session_id: Text
    purpose: ObservationPurpose
    subject: SubjectRef
    target: Target
    subject_control_generation: Count
    observation_generation: Count
    authorized: bool
    state: ObservationSessionState = "authorized"
    health_profile_revision: Text | None = None
    adopted_sequence: Count = 0
    adopted_window_end: AwareDatetime | None = None
    active_sample_job_id: Text | None = None

    @model_validator(mode="after")
    def subject_matches_purpose(self) -> "ObservationSession":
        """An empty incident may not stand in for a release subject."""
        expected = _SUBJECT_KIND_FOR_PURPOSE[self.purpose]
        if self.subject.kind != expected:
            raise ValueError("SUBJECT_KIND_MISMATCH")
        return self


class HealthSample(DTO):
    """One logical sample submitted by the Observer for a session."""

    sample_id: Text
    session_id: Text
    sequence: Positive
    window: QueryWindow
    outcome: SampleOutcome
    subject_control_generation: Count
    observation_generation: Count
    health_profile_revision: Text | None = None
    required_signals_present: bool = False


class SampleAcceptance(DTO):
    """Whether a sample is adopted or kept as history only."""

    accepted: bool
    disposition: Literal["adopted", "history_only"]
    reason: Literal[
        "adopted",
        "session_not_authorized",
        "session_mismatch",
        "control_generation_stale",
        "observation_generation_stale",
        "health_profile_revision_mismatch",
        "sequence_not_advancing",
        "window_regressed",
        "subject_state_not_adoptable",
        "deadline_expired",
        "suspended",
    ]


def advance_session(session: ObservationSession, trigger: str) -> ObservationSession:
    """Fire one session trigger, or raise ``ILLEGAL_TRANSITION``."""
    if not isinstance(session, ObservationSession):
        raise DomainError("INVALID_INPUT", "observation session is required")
    return session.model_copy(
        update={"state": OBSERVATION_SESSION.fire(session.state, trigger)}
    )


def may_schedule_sample(session: ObservationSession) -> bool:
    """At most one active sampling job per session."""
    if not isinstance(session, ObservationSession):
        raise DomainError("INVALID_INPUT", "observation session is required")
    return (
        session.state == "authorized"
        and session.authorized
        and session.active_sample_job_id is None
    )


def evaluate_sample(
    session: ObservationSession,
    sample: HealthSample,
    *,
    subject_state: str,
    within_deadline: bool = True,
    suspension_blocks: bool = False,
) -> SampleAcceptance:
    """Decide whether one sample may be adopted by its session."""
    if not isinstance(session, ObservationSession) or not isinstance(
        sample, HealthSample
    ):
        raise DomainError("INVALID_INPUT", "session and sample are required")
    if type(within_deadline) is not bool or type(suspension_blocks) is not bool:
        raise DomainError("INVALID_INPUT", "deadline and suspension must be bools")
    allowed = _ACTIVE_SUBJECT_STATES[session.purpose]
    if subject_state not in allowed:
        return _history_only("subject_state_not_adoptable")
    if suspension_blocks:
        return _history_only("suspended")
    if not within_deadline:
        return _history_only("deadline_expired")
    if session.state != "authorized" or not session.authorized:
        return _history_only("session_not_authorized")
    if sample.session_id != session.session_id:
        return _history_only("session_mismatch")
    if sample.subject_control_generation != session.subject_control_generation:
        return _history_only("control_generation_stale")
    if sample.observation_generation != session.observation_generation:
        return _history_only("observation_generation_stale")
    if sample.health_profile_revision != session.health_profile_revision:
        return _history_only("health_profile_revision_mismatch")
    if sample.sequence <= session.adopted_sequence:
        return _history_only("sequence_not_advancing")
    if (
        session.adopted_window_end is not None
        and sample.window.end < session.adopted_window_end
    ):
        return _history_only("window_regressed")
    return SampleAcceptance(accepted=True, disposition="adopted", reason="adopted")


def adopt_sample(
    session: ObservationSession, sample: HealthSample
) -> ObservationSession:
    """Move the adopted watermark forward after an accepted sample."""
    if not isinstance(session, ObservationSession) or not isinstance(
        sample, HealthSample
    ):
        raise DomainError("INVALID_INPUT", "session and sample are required")
    if sample.sequence <= session.adopted_sequence:
        raise DomainError("STALE_RESULT", "sample sequence does not advance")
    return session.model_copy(
        update={
            "adopted_sequence": sample.sequence,
            "adopted_window_end": sample.window.end,
            "active_sample_job_id": None,
        }
    )


def confirms_health(session: ObservationSession, sample: HealthSample) -> bool:
    """Health needs a bound health-rule revision and the required signals.

    Without a health profile the subject can still be investigated, but recovery
    is never independently confirmed.
    """
    if not isinstance(session, ObservationSession) or not isinstance(
        sample, HealthSample
    ):
        raise DomainError("INVALID_INPUT", "session and sample are required")
    return (
        session.health_profile_revision is not None
        and sample.outcome == "healthy"
        and sample.required_signals_present
    )


def extends_healthy_window(session: ObservationSession, sample: HealthSample) -> bool:
    """A missing, stale or failed query never extends the healthy window."""
    return confirms_health(session, sample)


def _history_only(reason: str) -> SampleAcceptance:
    return SampleAcceptance(accepted=False, disposition="history_only", reason=reason)
