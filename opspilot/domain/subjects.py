"""`Incident` and `ReleaseObservation` (C3 section 4, "事故与发布观察分开建模").

The two subjects are modelled separately and share no lifecycle. A release
observation carries its own identity, control version, status machine and
result ownership, and exists without any incident; only an abnormal result
creates or links one. A human close is not an independent recovery
confirmation, so no human trigger reaches ``resolved``.
"""

from datetime import timedelta
from typing import Literal

from pydantic import AwareDatetime, model_validator

from .base import DTO, Count, DomainError, Positive, StateMachine, Text
from .control import ControlState
from .intake import Target

IncidentLifecycle = Literal["open", "observing_recovery", "resolved", "closed"]
ReleaseStatus = Literal[
    "pending", "observing", "healthy", "anomalous", "unknown", "cancelled"
]

INCIDENT_LIFECYCLE = StateMachine(
    "incident",
    {
        "open": {
            "start_recovery_observation": "observing_recovery",
            "human_close": "closed",
        },
        "observing_recovery": {
            # Only an independent healthy observation reaches resolved.
            "recovery_confirmed": "resolved",
            "observation_ended_unconfirmed": "open",
            "human_close": "closed",
        },
        "resolved": {"human_close": "closed", "human_reopen": "open"},
        "closed": {"human_reopen": "open"},
    },
)

RELEASE_OBSERVATION = StateMachine(
    "release_observation",
    {
        "pending": {
            "observation_started": "observing",
            "deadline_expired": "unknown",
            "human_cancel": "cancelled",
        },
        "observing": {
            "healthy_window_satisfied": "healthy",
            "anomaly_detected": "anomalous",
            "evidence_unavailable": "unknown",
            "deadline_expired": "unknown",
            "human_cancel": "cancelled",
        },
        "healthy": {},
        "anomalous": {},
        "unknown": {},
        "cancelled": {},
    },
)

ACTIVE_INCIDENT_LIFECYCLES = frozenset({"open", "observing_recovery"})
ACTIVE_RELEASE_STATUSES = frozenset({"pending", "observing"})


class SubjectRef(DTO):
    """Explicit binding of a record to the subject that owns it."""

    kind: Literal["incident", "release_observation"]
    id: Text


class Incident(DTO):
    """Incident lifecycle, human control version and current investigation Run."""

    incident_id: Text
    target: Target
    opened_at: AwareDatetime
    lifecycle: IncidentLifecycle = "open"
    control: ControlState = ControlState()
    current_run_id: Text | None = None
    related_incident_id: Text | None = None
    observation_generation: Count = 0

    @property
    def ref(self) -> SubjectRef:
        return SubjectRef(kind="incident", id=self.incident_id)


class ReleaseIdentity(DTO):
    """Source release id, target and immutable released revision."""

    source: Text
    release_id: Text
    target: Target
    release_revision: Text


class ReleaseObservationPolicy(DTO):
    """Fixed release start, minimum tracking span, cadence, window and deadline."""

    released_at: AwareDatetime
    min_tracking_seconds: Positive
    sample_interval_seconds: Positive
    healthy_window_seconds: Positive
    max_samples: Positive
    deadline: AwareDatetime

    @model_validator(mode="after")
    def windows_fit_the_deadline(self) -> "ReleaseObservationPolicy":
        if self.deadline <= self.released_at:
            raise ValueError("INVALID_DEADLINE")
        if self.earliest_healthy_at > self.deadline:
            raise ValueError("REQUIRED_WINDOW_EXCEEDS_DEADLINE")
        return self

    @property
    def earliest_healthy_at(self) -> AwareDatetime:
        """No healthy completion before the minimum tracking span has elapsed."""
        span = max(self.min_tracking_seconds, self.healthy_window_seconds)
        return self.released_at + timedelta(seconds=span)


class ReleaseObservation(DTO):
    """Release identity, target, observation status and any linked incident.

    ``incident_id`` stays ``None`` for a normal release: this record does not
    require an incident to exist.
    """

    observation_id: Text
    identity: ReleaseIdentity
    policy: ReleaseObservationPolicy
    status: ReleaseStatus = "pending"
    control: ControlState = ControlState()
    current_run_id: Text | None = None
    incident_id: Text | None = None
    supersedes_observation_id: Text | None = None
    observation_generation: Count = 0

    @property
    def ref(self) -> SubjectRef:
        return SubjectRef(kind="release_observation", id=self.observation_id)


def advance_incident(incident: Incident, trigger: str) -> Incident:
    """Fire one incident lifecycle trigger, or raise ``ILLEGAL_TRANSITION``."""
    if not isinstance(incident, Incident):
        raise DomainError("INVALID_INPUT", "incident is required")
    return incident.model_copy(
        update={"lifecycle": INCIDENT_LIFECYCLE.fire(incident.lifecycle, trigger)}
    )


def disposition_for_new_anomaly(
    incident: Incident,
) -> Literal["attach_event", "new_related_incident"]:
    """A new anomaly on a closed incident defaults to a new, related incident.

    Reopening the closed incident instead stays an explicit human control action.
    """
    if not isinstance(incident, Incident):
        raise DomainError("INVALID_INPUT", "incident is required")
    return "new_related_incident" if incident.lifecycle == "closed" else "attach_event"


def advance_release(
    observation: ReleaseObservation,
    trigger: str,
    *,
    now: AwareDatetime | None = None,
    incident_id: str | None = None,
) -> ReleaseObservation:
    """Fire one release observation trigger, or raise ``ILLEGAL_TRANSITION``.

    ``healthy_window_satisfied`` additionally requires the minimum tracking span
    to have elapsed, so a healthy sample taken earlier leaves the record
    observing. ``anomaly_detected`` requires the incident it creates or links.
    """
    if not isinstance(observation, ReleaseObservation):
        raise DomainError("INVALID_INPUT", "release observation is required")
    status = RELEASE_OBSERVATION.fire(observation.status, trigger)
    update: dict[str, object] = {"status": status}
    if trigger == "healthy_window_satisfied":
        if now is None or now < observation.policy.earliest_healthy_at:
            raise DomainError(
                "ILLEGAL_TRANSITION", "minimum tracking span not yet covered"
            )
        if now > observation.policy.deadline:
            raise DomainError("ILLEGAL_TRANSITION", "deadline already expired")
    if trigger == "anomaly_detected":
        linked = incident_id or observation.incident_id
        if not isinstance(linked, str) or not linked:
            raise DomainError(
                "INVALID_INPUT", "an abnormal release links or creates an incident"
            )
        update["incident_id"] = linked
    return observation.model_copy(update=update)


def successor_observation(
    previous: ReleaseObservation,
    *,
    observation_id: str,
    identity: ReleaseIdentity,
    policy: ReleaseObservationPolicy,
) -> ReleaseObservation:
    """Observing again after a terminal status creates a new, linked record."""
    if not isinstance(previous, ReleaseObservation):
        raise DomainError("INVALID_INPUT", "previous observation is required")
    if not RELEASE_OBSERVATION.terminal(previous.status):
        raise DomainError(
            "ILLEGAL_TRANSITION", f"{previous.status} is not a terminal status"
        )
    return ReleaseObservation(
        observation_id=observation_id,
        identity=identity,
        policy=policy,
        supersedes_observation_id=previous.observation_id,
    )
