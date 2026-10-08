"""Durable observation sessions and health samples (C3 section 10, issue #84).

The store is the first persistence module built directly on the domain state
machines: adoption is decided by ``evaluate_sample`` / ``adopt_sample`` /
``confirms_health`` from ``opspilot.domain.observation`` and the lifecycle
moves through ``INCIDENT_LIFECYCLE`` / ``OBSERVATION_SESSION``; nothing here
re-states those rules. ``opspilot/persistence`` keeps its "built on domain?"
question open (``tests/test_architecture.py`` records it as an xfail); this
package does not reopen it.

Role split (C3 section 3, decision D3): the Observer process holds
``claim_due_samples`` / ``submit_sample`` / ``sweep_expired_sessions`` under
the ``opspilot_observer`` role that migration 0003 grants; authorizing or
revoking a session is Controller work (step 3) and runs under the owner
connection. The class is the same; the DSN decides what the database allows.

Every submission is kept: an adopted sample moves the watermark, a rejected
one stays ``history_only`` with its reason and never changes the incident.
Adoption, watermark, lifecycle transition and the next sampling job commit in
one transaction, under the incident row lock first and the session row lock
second (the lock order step 3's revoke path must keep).

The adopted sample's decision is stored together with the conditions it was
taken under (lifecycle, scope suspension, deadline, lease, incident control
and observation generations), so ``replay_session`` recomputes every
decision from the stored rows alone -- it never re-queries telemetry
(F6 step 5).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from pydantic import AwareDatetime, model_validator

from opspilot.domain.base import DTO, Count, DomainError, Text
from opspilot.domain.evidence import QueryWindow
from opspilot.domain.intake import Target
from opspilot.domain.observation import (
    OBSERVATION_SESSION,
    HealthSample,
    ObservationSession,
    SampleAcceptance,
    adopt_sample,
    confirms_health,
    evaluate_sample,
)
from opspilot.domain.subjects import INCIDENT_LIFECYCLE, SubjectRef
from opspilot.observation.revocation import end_session, revoke_authorized_sessions
from opspilot.persistence.base import Connection, PersistenceError, _StoreBase

# How long one claimed sampling job stays leased to its owner. A Prometheus
# query is bounded by the data-source timeout (C3 section 13: 20 s); the lease
# only has to outlive one attempt, a crashed Observer's job is reclaimable
# after this and keeps its logical sequence (C3 section 10).
OBSERVER_LEASE_SECONDS = 120
# A sample window may end this much after the database clock. The Observer
# ends its query window at its own "now"; a small clock difference between
# it and PostgreSQL must not reject every sample, a window reaching into the
# future (which no telemetry can have covered) must. 30 s is well above any
# NTP-kept skew and well below the shortest sampling interval in use.
WINDOW_FUTURE_SKEW_SECONDS = 30

# Rows are read by name through the dict_row connection; the column lists are
# spelled out so no read depends on physical column order
# (tests/test_sql_column_order_independence.py).
_SESSION_COLUMNS = (
    "session_id,incident_id,purpose,target_id,target,subject_control_generation,"
    "observation_generation,authorized,authorized_by,authorized_at,state,ended_reason,"
    "authorized_global_generation,authorized_target_generation,"
    "health_profile_revision,deadline_at,max_samples,sample_interval_seconds,"
    "sustained_window_seconds,adopted_sequence,adopted_window_end,adopted_count,"
    "healthy_since,issued_sequence,active_sample_job_id,active_sample_sequence,"
    "active_sample_due_at,active_sample_owner,active_sample_epoch,"
    "active_sample_lease_until,created_at,updated_at"
)
_SAMPLE_COLUMNS = (
    "sample_id,session_id,job_id,sequence,epoch,window_start,window_end,outcome,"
    "required_signals_present,subject_control_generation,observation_generation,"
    "health_profile_revision,disposition,reason,confirms_health,health_basis,"
    "subject_lifecycle,incident_control_generation,incident_observation_generation,"
    "scope_suspended,global_generation,target_generation,within_deadline,"
    "lease_valid,lease_stamps_match,readings_consistent,transition,submitted_at"
)
_ENDING_COLUMNS = (
    "ending_id,session_id,incident_id,ended_reason,transition,sample_id,"
    "lifecycle_before,lifecycle_after,recorded_at"
)
_READING_COLUMNS = (
    "sample_id,signal_name,status,value,sample_count,query,window_start,"
    "window_end,source,raw_sha256,raw"
)
# One signal's raw result, kept for hash verification on replay; the same
# bound migration 0003 enforces (M0 response limit, 128 KiB).
READING_RAW_LIMIT = 131072

Disposition = Literal["adopted", "history_only"]
Transition = Literal["recovery_confirmed", "observation_ended_unconfirmed"]
EndedReason = Literal[
    "recovery_confirmed",
    "deadline_expired",
    "max_samples_exhausted",
    "authority_revoked",
    "binding_stale",
    "scope_suspended",
]
HealthBasis = Literal[
    "confirmed",
    "not_adopted",
    "no_health_profile",
    "outcome_not_healthy",
    "required_signals_missing",
    "window_before_authorization",
]
# A sample rejected for one of these proves the session's bindings are
# stale for good; the session ends instead of retrying until its deadline.
_STALE_BINDINGS = frozenset(
    {
        "control_generation_stale",
        "observation_generation_stale",
        "subject_state_not_adoptable",
    }
)
ReadingStatus = Literal["ok", "no_data", "stale", "timeout", "failed"]


def profile_revision(profile_id: str, content: str) -> str:
    """``<profile_id>@<sha256(content)[:12]>`` (interface contract item 1)."""
    return f"{profile_id}@{hashlib.sha256(content.encode()).hexdigest()[:12]}"


class SignalReading(DTO):
    """One signal's reading inside a sample (interface contract item 3).

    The field names and types are the contract shared with the HealthProfile
    step; the store does not interpret ``signal_name`` or judge ``value``.
    """

    signal_name: Text
    status: ReadingStatus
    value: float | None = None
    sample_count: Count | None = None
    query: Text
    window_start: AwareDatetime
    window_end: AwareDatetime
    source: Text
    raw_sha256: Text | None = None
    # The raw result as returned by the source, when the Observer keeps it;
    # its sha256 must be ``raw_sha256`` (filled in when only ``raw`` is given).
    raw: bytes | None = None

    @model_validator(mode="after")
    def consistent(self) -> SignalReading:
        if (self.status == "ok") != (self.value is not None):
            raise ValueError("VALUE_STATUS_MISMATCH")
        if self.window_end < self.window_start:
            raise ValueError("INVALID_WINDOW")
        if self.raw is not None:
            if len(self.raw) > READING_RAW_LIMIT:
                raise ValueError("RAW_TOO_LARGE")
            digest = hashlib.sha256(self.raw).hexdigest()
            if self.raw_sha256 is None:
                object.__setattr__(self, "raw_sha256", digest)
            elif self.raw_sha256 != digest:
                raise ValueError("RAW_SHA256_MISMATCH")
        return self


@dataclass(frozen=True)
class SampleLease:
    """The credential a claimed sampling job hands the Observer."""

    session_id: UUID
    incident_id: UUID
    job_id: UUID
    sequence: int
    owner: UUID
    epoch: int
    lease_until: datetime
    subject_control_generation: int
    observation_generation: int
    health_profile_revision: str | None
    deadline_at: datetime
    sample_interval_seconds: int
    adopted_window_end: datetime | None
    # Control scope versions at claim time: a suspension in between moves
    # them and the submission is history only (C3 section 4).
    global_suspension_generation: int = 0
    target_suspension_generation: int = 0


@dataclass(frozen=True)
class SampleReceipt:
    """What one submission did, as committed."""

    sample_id: UUID
    accepted: bool
    disposition: Disposition
    reason: str
    confirms_health: bool
    health_basis: HealthBasis
    session_state: str
    incident_lifecycle: str
    transition: Transition | None
    next_sample_due_at: datetime | None


@dataclass(frozen=True)
class ReplayedSample:
    sample_id: UUID
    sequence: int
    # (disposition, reason, confirms_health, health_basis, transition)
    stored: tuple[str, str, bool, str, str | None]
    replayed: tuple[str, str, bool, str, str | None]
    # signal names whose stored raw bytes no longer hash to raw_sha256
    raw_mismatches: tuple[str, ...] = ()
    # required signals (per the stored profile) without an ok reading row on
    # a healthy sample, or "required_signals_present" when that flag
    # contradicts the reading rows
    signal_mismatches: tuple[str, ...] = ()

    @property
    def matches(self) -> bool:
        # A sample the store itself filed as readings_inconsistent carries
        # its structural problem as the stored verdict; the list then
        # explains it rather than contradicting it.
        return (
            self.stored == self.replayed
            and not self.raw_mismatches
            and (
                not self.signal_mismatches or self.stored[1] == "readings_inconsistent"
            )
        )


@dataclass(frozen=True)
class ReplayReport:
    session_id: UUID
    samples: tuple[ReplayedSample, ...]
    # session state the fold ends in vs. the stored one (a sweep or a revoke
    # ends a session without a sample; those endings are accepted as such)
    replayed_session_state: str
    stored_session_state: str
    # the lifecycle the replayed verdicts imply (None: no claim) vs. the one
    # the last stored sample recorded (its lifecycle after its transition).
    # The incident's *current* lifecycle is not compared: a later session,
    # a human close or reopen must not make an old session look wrong.
    expected_lifecycle: str | None
    recorded_lifecycle: str | None
    # how the session says it ended, and the last ending record
    # (ended_reason, transition, sample_id, lifecycle_before, lifecycle_after);
    # None when there is none
    stored_ended_reason: str | None = None
    recorded_ending: tuple[str, str | None, UUID | None, str, str] | None = None
    # the sample whose replayed verdict ended the session, if any
    ending_sample_id: UUID | None = None

    @property
    def ending_consistent(self) -> bool:
        """The stored ending record agrees with the session's end.

        A session still authorized has no ending record. An ended one has a
        record whose reason is the session's, whose reason implies the stored
        state, whose transition is exactly what the reason fires (a
        confirmation carries ``recovery_confirmed`` and points at the
        confirming sample; a deadline or budget ending carries
        ``observation_ended_unconfirmed``; a revocation carries nothing),
        whose recorded lifecycle after equals the lifecycle before advanced
        by that transition (unchanged when the incident could not take it,
        e.g. already ``open``), and -- when a replayed verdict ended the
        session -- which points at exactly that sample.
        """
        if self.stored_session_state == "authorized":
            return self.stored_ended_reason is None and self.recorded_ending is None
        if self.recorded_ending is None or self.stored_ended_reason is None:
            return False
        reason, transition, sample_id, before, after = self.recorded_ending
        if reason != self.stored_ended_reason:
            return False
        implied_state = {
            "recovery_confirmed": "completed",
            "deadline_expired": "expired",
            "max_samples_exhausted": "expired",
            "authority_revoked": "revoked",
            "binding_stale": "revoked",
            "scope_suspended": "revoked",
        }.get(reason)
        if implied_state != self.stored_session_state:
            return False
        if reason == "recovery_confirmed":
            fits = transition == "recovery_confirmed" and sample_id is not None
        elif reason in {"deadline_expired", "max_samples_exhausted"}:
            fits = transition == "observation_ended_unconfirmed"
        else:
            fits = transition is None
        if transition is None:
            fits = fits and after == before
        elif transition in INCIDENT_LIFECYCLE.triggers(before):
            fits = fits and after == INCIDENT_LIFECYCLE.fire(before, transition)
        else:
            # A confirmation that the lifecycle could not take is never
            # written (the store rolls back); an unconfirmed ending on an
            # incident already open leaves it as it is.
            fits = fits and transition != "recovery_confirmed" and after == before
        if self.ending_sample_id is not None:
            fits = fits and sample_id == self.ending_sample_id
        return fits

    @property
    def session_consistent(self) -> bool:
        """The fold ends where the session says it ended, or the session was
        ended without a sample (sweep, revoke, scope change, stale binding)
        and the ending record says so."""
        if not self.ending_consistent:
            return False
        if self.replayed_session_state == self.stored_session_state:
            return True
        return (
            self.replayed_session_state == "authorized"
            and self.ending_sample_id is None
            and self.stored_session_state in {"expired", "revoked"}
        )

    @property
    def lifecycle_consistent(self) -> bool:
        return (
            self.expected_lifecycle is None
            or self.expected_lifecycle == self.recorded_lifecycle
        )

    @property
    def consistent(self) -> bool:
        return (
            all(item.matches for item in self.samples)
            and self.session_consistent
            and self.lifecycle_consistent
        )


@dataclass(frozen=True)
class _Verdict:
    """The outcome of judging one sample against the session's fold state."""

    decision: SampleAcceptance
    healthy: bool
    health_basis: HealthBasis
    transition: Transition | None
    session_state: str
    ended_reason: EndedReason | None
    adopted_sequence: int
    adopted_window_end: datetime | None
    adopted_count: int
    healthy_since: datetime | None


def _health_basis(
    session: ObservationSession,
    sample: HealthSample,
    *,
    accepted: bool,
    authorized_at: datetime,
) -> HealthBasis:
    """Why ``confirms_health`` holds or not, with the one store-side rule on
    top of the domain's: data from before the authorization (the human
    handling) is adopted as an observation but never counts as healthy."""
    if not accepted:
        return "not_adopted"
    if session.health_profile_revision is None:
        return "no_health_profile"
    if sample.outcome != "healthy":
        return "outcome_not_healthy"
    if not sample.required_signals_present:
        return "required_signals_missing"
    if sample.window.start < authorized_at:
        return "window_before_authorization"
    return "confirmed"


def _judge(
    session: ObservationSession,
    sample: HealthSample,
    *,
    subject_state: str,
    within_deadline: bool,
    suspension_blocks: bool,
    lease_valid: bool,
    stamps_match: bool,
    readings_consistent: bool,
    authorized_at: datetime,
    adopted_count: int,
    healthy_since: datetime | None,
    max_samples: int,
    sustained_window_seconds: int,
) -> _Verdict:
    """The one decision procedure ``submit_sample`` and ``replay_session`` share.

    The lease is checked first (it is the store's own check, C3 section 10
    "owner、epoch 和 lease"); everything else is ``evaluate_sample``. A
    rejected sample changes nothing, except that one rejected for the deadline
    ends the session (interface contract item 6) and one rejected for a stale
    binding ends it as ``binding_stale``.

    The healthy streak is the span of consecutive adopted healthy samples,
    measured from the first one's ``window.start`` to the latest ``window.end``.
    It restarts when a sample is adopted after a gap (its window starts after
    the previous adopted window ended: unobserved time never counts) and it
    is cleared by any adopted sample that does not confirm health. A
    suspension cannot reach the streak: any change of the control scope
    since the authorization ends the session (``scope_suspended``), a lifted
    suspension never resumes an old authorization (C3 section 4).
    """
    if not lease_valid:
        decision = SampleAcceptance(
            accepted=False, disposition="history_only", reason="lease_revoked"
        )
    elif not stamps_match:
        decision = SampleAcceptance(
            accepted=False, disposition="history_only", reason="lease_stamp_mismatch"
        )
    elif not readings_consistent:
        decision = SampleAcceptance(
            accepted=False, disposition="history_only", reason="readings_inconsistent"
        )
    else:
        decision = evaluate_sample(
            session,
            sample,
            subject_state=subject_state,
            within_deadline=within_deadline,
            suspension_blocks=suspension_blocks,
        )
    basis = _health_basis(
        session,
        sample,
        accepted=decision.accepted,
        authorized_at=authorized_at,
    )
    healthy = (
        decision.accepted and confirms_health(session, sample) and basis == "confirmed"
    )
    assert healthy == (basis == "confirmed")
    transition: Transition | None = None
    ended: EndedReason | None = None
    state: str = session.state
    adopted_sequence = session.adopted_sequence
    adopted_window_end = session.adopted_window_end
    if decision.accepted:
        advanced = adopt_sample(session, sample)
        adopted_sequence = advanced.adopted_sequence
        adopted_window_end = advanced.adopted_window_end
        adopted_count += 1
        gap = (
            session.adopted_window_end is not None
            and sample.window.start > session.adopted_window_end
        )
        if not healthy:
            healthy_since = None
        elif healthy_since is None or gap:
            healthy_since = sample.window.start
        if (
            healthy
            and healthy_since is not None
            and (sample.window.end - healthy_since).total_seconds()
            >= sustained_window_seconds
        ):
            transition = "recovery_confirmed"
            state = OBSERVATION_SESSION.fire(state, "observation_completed")
            ended = "recovery_confirmed"
        elif adopted_count >= max_samples:
            transition = "observation_ended_unconfirmed"
            state = OBSERVATION_SESSION.fire(state, "deadline_expired")
            ended = "max_samples_exhausted"
    elif decision.reason == "deadline_expired" and state == "authorized":
        transition = "observation_ended_unconfirmed"
        state = OBSERVATION_SESSION.fire(state, "deadline_expired")
        ended = "deadline_expired"
    elif decision.reason in _STALE_BINDINGS and state == "authorized":
        state = OBSERVATION_SESSION.fire(state, "authority_revoked")
        ended = "binding_stale"
    elif decision.reason == "suspended" and state == "authorized":
        state = OBSERVATION_SESSION.fire(state, "authority_revoked")
        ended = "scope_suspended"
    return _Verdict(
        decision=decision,
        healthy=healthy,
        health_basis=basis,
        transition=transition,
        session_state=state,
        ended_reason=ended,
        adopted_sequence=adopted_sequence,
        adopted_window_end=adopted_window_end,
        adopted_count=adopted_count,
        healthy_since=healthy_since,
    )


def _parse_required(
    profile: dict[str, Any] | None,
) -> tuple[tuple[str, ...] | None, bool]:
    """``(required names, unreadable)`` for a stored profile row; ``(None,
    False)`` when the session has no profile revision.

    Fail closed on integrity as well as on shape: the stored content must
    still hash to ``content_sha256`` and that hash must still name the
    revision, or the profile is treated as unreadable.
    """
    if profile is None:
        return None, False
    content = str(profile["content"])
    digest = hashlib.sha256(content.encode()).hexdigest()
    revision = str(profile["health_profile_revision"])
    profile_id, _, suffix = revision.rpartition("@")
    if (
        digest != profile["content_sha256"]
        or suffix != digest[:12]
        or revision != profile_revision(profile_id, content)
    ):
        return None, True
    try:
        return required_signals(content), False
    except PersistenceError:
        return None, True


# A reading's window must be the sample's window: Prometheus range queries
# snap their bounds to the step, so a small difference is normal, a window
# of its own (older data standing in for this sample's) is not. 5 % of the
# sample window is the tolerance the HealthProfile step uses as well.
WINDOW_TOLERANCE_FRACTION = 0.05


def _covers(
    status: object,
    value: object,
    sample_count: object,
    *,
    reading_window: tuple[datetime, datetime],
    sample_window: tuple[datetime, datetime],
) -> bool:
    """A reading covers its signal only as an ok result with a value and at
    least one underlying sample (``coverage_query``), taken over this
    sample's window; an ok with nothing behind it, or data from another
    window, is no coverage (C3 section 10: minimum samples are a necessary
    condition of healthy, and a result must belong to the current logical
    sample)."""
    tolerance = (sample_window[1] - sample_window[0]) * WINDOW_TOLERANCE_FRACTION
    return (
        status == "ok"
        and value is not None
        and isinstance(sample_count, int)
        and sample_count > 0
        and abs(reading_window[0] - sample_window[0]) <= tolerance
        and abs(reading_window[1] - sample_window[1]) <= tolerance
    )


def _missing_required_signals(
    required: tuple[str, ...] | None,
    *,
    unreadable: bool,
    outcome: str,
    required_signals_present: bool,
    ok_signals: set[str],
) -> tuple[str, ...]:
    """What the reading rows fail to support: the required signals without an
    ok reading when the sample claims health, ``required_signals_present``
    when that flag contradicts the rows, ``health_profile_unreadable`` when
    the profile yields no required-signal list (fail closed). Empty when the
    rows support the sample, or when the session has no profile at all (such
    a session never confirms health). Structural only: F6 step 5's threshold
    replay is #87."""
    if unreadable:
        return ("health_profile_unreadable",)
    if required is None:
        return ()
    missing = tuple(name for name in required if name not in ok_signals)
    if (outcome == "healthy" and missing) or required_signals_present != (not missing):
        return missing or ("required_signals_present",)
    return ()


def _scope_blocks(
    scope: dict[str, Any], row: dict[str, Any]
) -> tuple[bool, tuple[int, int]]:
    """Suspended now, or suspended-and-released since the authorization (the
    generations moved): either way the session is over (C3 section 4)."""
    generations = (int(scope["global_generation"]), int(scope["target_generation"]))
    blocked = (
        bool(scope["global_suspended"])
        or bool(scope["target_suspended"])
        or generations
        != (
            int(row["authorized_global_generation"]),
            int(row["authorized_target_generation"]),
        )
    )
    return blocked, generations


def required_signals(content: str) -> tuple[str, ...]:
    """Required signal names from a stored profile's canonical content.

    The HealthProfile step owns the format: ``signals`` is a list of objects
    with ``name`` and a ``required`` flag that defaults to true (the
    canonical dump writes it explicitly). Fail closed: unparsable content,
    no ``signals`` list, a malformed entry or an empty required list raises
    ``PersistenceError("HEALTH_PROFILE_UNREADABLE")``; a replay then reports
    every sample as inconsistent instead of silently skipping the check.
    """
    try:
        parsed = json.loads(content)
    except ValueError as exc:
        raise PersistenceError("HEALTH_PROFILE_UNREADABLE") from exc
    signals = parsed.get("signals") if isinstance(parsed, dict) else None
    if not isinstance(signals, list):
        raise PersistenceError("HEALTH_PROFILE_UNREADABLE")
    names: list[str] = []
    for item in signals:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("name"), str)
            or not item["name"]
            or type(item.get("required", True)) is not bool
        ):
            raise PersistenceError("HEALTH_PROFILE_UNREADABLE")
        if item.get("required", True):
            names.append(item["name"])
    if not names:
        raise PersistenceError("HEALTH_PROFILE_UNREADABLE")
    return tuple(names)


# The identity columns of ``opspilot_targets`` (migration 0004), in the order
# ``_complete_identity`` and ``_registered_target`` read them.
_IDENTITY_FIELDS = ("integration_id", "cluster_uid", "namespace", "resource_uid")
# Completed together with the identity: the workload the profile observes.
_BINDING_FIELDS = ("workload",)


def _profile_subject(content: str) -> tuple[str, str]:
    """``(kubernetes_namespace, service)`` of a stored profile's ``subject``
    (the HealthProfile format, step 1); fail closed when absent or empty."""
    try:
        parsed = json.loads(content)
    except ValueError as exc:
        raise PersistenceError("HEALTH_PROFILE_UNREADABLE") from exc
    subject = parsed.get("subject") if isinstance(parsed, dict) else None
    if not isinstance(subject, dict):
        raise PersistenceError("HEALTH_PROFILE_UNREADABLE")
    namespace, service = subject.get("kubernetes_namespace"), subject.get("service")
    if not all(isinstance(value, str) and value for value in (namespace, service)):
        raise PersistenceError("HEALTH_PROFILE_UNREADABLE")
    return str(namespace), str(service)


def _identity(target: Target) -> tuple[str, str, str, str]:
    """The four identity fields (user decision 2026-10-07): ``revision`` is a
    snapshot, not identity."""
    return (
        target.integration_id,
        target.cluster_uid,
        target.namespace,
        target.resource_uid,
    )


def _domain_session(
    row: dict[str, Any],
    *,
    control_generation: int,
    observation_generation: int,
    state: str,
    adopted_sequence: int,
    adopted_window_end: datetime | None,
    active_job: UUID | None,
) -> ObservationSession:
    """The domain view of a session row, bound to the *current* incident
    generations so a sample stamped with older ones is judged stale."""
    return ObservationSession(
        session_id=str(row["session_id"]),
        purpose=row["purpose"],
        subject=SubjectRef(kind="incident", id=str(row["incident_id"])),
        target=Target(**row["target"]),
        subject_control_generation=control_generation,
        observation_generation=observation_generation,
        authorized=True,
        state=cast(Any, state),
        health_profile_revision=row["health_profile_revision"],
        adopted_sequence=adopted_sequence,
        adopted_window_end=adopted_window_end,
        active_sample_job_id=None if active_job is None else str(active_job),
    )


class ObservationStore(_StoreBase):
    """Observation sessions, sampling jobs and samples on PostgreSQL."""

    # --- authorization (Controller side; step 3 wraps this in the human action)

    def authorize_session(
        self,
        incident_id: UUID,
        *,
        revision: str,
        actor: str,
        deadline_at: datetime,
        max_samples: int,
        sample_interval_seconds: int,
        sustained_window_seconds: int,
        health_profile_revision: str | None = None,
        health_profile: str | None = None,
        session_id: UUID | None = None,
        first_sample_due_at: datetime | None = None,
    ) -> UUID:
        """Create one authorized session and its first sampling job.

        The session's ``Target`` is the identity the incident was accepted
        against (``opspilot_targets``, migration 0004), never a caller's;
        ``revision`` is the one field that is not identity -- the target
        revision after the handling, snapshotted for the audit (user decision
        2026-10-07). ``health_profile`` is the canonical JSON text of the
        profile the revision names; it is stored once per revision so a
        replay can read the coverage queries and thresholds the samples were
        judged by.

        Storage primitive only: the human "register remediation" action with
        its ``expected_version``, idempotency key and audit row is
        :meth:`register_remediation`, which calls
        :meth:`authorize_session_in` inside its own transaction.
        """
        with self.transaction() as conn:
            return self.authorize_session_in(
                conn,
                incident_id,
                revision=revision,
                actor=actor,
                deadline_at=deadline_at,
                max_samples=max_samples,
                sample_interval_seconds=sample_interval_seconds,
                sustained_window_seconds=sustained_window_seconds,
                health_profile_revision=health_profile_revision,
                health_profile=health_profile,
                session_id=session_id,
                first_sample_due_at=first_sample_due_at,
            )

    def authorize_session_in(
        self,
        conn: Connection,
        incident_id: UUID,
        *,
        revision: str,
        actor: str,
        deadline_at: datetime,
        max_samples: int,
        sample_interval_seconds: int,
        sustained_window_seconds: int,
        health_profile_revision: str | None = None,
        health_profile: str | None = None,
        session_id: UUID | None = None,
        first_sample_due_at: datetime | None = None,
    ) -> UUID:
        """Same as :meth:`authorize_session`, inside the caller's transaction.

        Locks the incident row, moves ``open`` to ``observing_recovery``
        (an incident already observing keeps its lifecycle), increments the
        incident's observation generation and binds the session to it. Only
        one authorized session may exist per incident.

        The identity (integration, cluster, namespace, resource uid) is read
        from the registry row the incident points at and must equal the
        identity every earlier session on that target was authorized with;
        a difference is ``TARGET_MISMATCH`` and nothing is written. A changed
        ``revision`` is not a difference (a redeployed target can be observed
        again).
        """
        if (
            not isinstance(incident_id, UUID)
            or not isinstance(revision, str)
            or not revision
            or not isinstance(actor, str)
            or not actor
            or not isinstance(deadline_at, datetime)
            or deadline_at.tzinfo is None
            or type(max_samples) is not int
            or max_samples <= 0
            or type(sample_interval_seconds) is not int
            or sample_interval_seconds <= 0
            or type(sustained_window_seconds) is not int
            or sustained_window_seconds <= 0
            or (
                health_profile_revision is not None
                and (
                    not isinstance(health_profile_revision, str)
                    or not health_profile_revision
                )
            )
        ):
            raise PersistenceError("INVALID_INPUT")
        if health_profile_revision is not None:
            self._store_profile(conn, health_profile_revision, health_profile)
            assert health_profile is not None
            subject: tuple[str, str] | None = _profile_subject(health_profile)
        elif health_profile is not None:
            raise PersistenceError("INVALID_INPUT")
        else:
            subject = None
        incident = self._lock_incident(conn, incident_id)
        # A paused incident takes no new observation authorization: human
        # control outranks it (C3 section 4 "暂停期间 ... 不发起自动查询"),
        # and registering a remediation must not lift a pause by the side;
        # resume is its own explicit decision (independent review of PR
        # #120, P1). Read apart from ``_lock_incident``: that helper also
        # serves the Observer role, which is not granted ``state``.
        control_state = self._require_row(
            conn.execute(
                "SELECT state FROM opspilot_incidents WHERE incident_id=%s",
                (incident_id,),
            )
        )["state"]
        if control_state == "paused":
            raise PersistenceError("ILLEGAL_TRANSITION")
        if incident["target_id"] is None:
            raise PersistenceError("UNKNOWN_TARGET")
        target = self._registered_target(conn, incident["target_id"], revision)
        if subject is not None:
            # The profile observes one workload in one namespace (its
            # ``subject``); it may authorize only the registered target that
            # is that workload -- checkout's health must never confirm a
            # payment incident (PR #120 bot review P1, second recheck).
            # Compared under the incident lock from the registry row, so the
            # storage primitive is bound as well as the workbench action.
            workload = self._require_row(
                conn.execute(
                    "SELECT workload FROM opspilot_targets WHERE target_id=%s",
                    (incident["target_id"],),
                )
            )["workload"]
            if subject != (target.namespace, workload):
                raise PersistenceError("HEALTH_PROFILE_TARGET_MISMATCH")
        # Every session on this target recorded the identity it was
        # authorized with; the registry row must still say the same (a
        # rebound or edited registry row is not this target any more, C3
        # section 4: rebinding is its own transaction that revokes first).
        expected_identity = _identity(target)
        for earlier in conn.execute(
            "SELECT target FROM opspilot_observation_sessions WHERE target_id=%s",
            (incident["target_id"],),
        ).fetchall():
            if _identity(Target(**earlier["target"])) != expected_identity:
                raise PersistenceError("TARGET_MISMATCH")
        if conn.execute(
            "SELECT 1 FROM opspilot_observation_sessions WHERE incident_id=%s AND state='authorized'",
            (incident_id,),
        ).fetchone():
            raise PersistenceError("OBSERVATION_ALREADY_AUTHORIZED")
        lifecycle = str(incident["lifecycle"])
        if lifecycle != "observing_recovery":
            lifecycle = self._fire_incident(lifecycle, "start_recovery_observation")
        now = self._db_now(conn)
        if deadline_at <= now:
            raise PersistenceError("INVALID_INPUT")
        scope = self._lock_scope(conn, incident_id, lock=False)
        generation = int(incident["observation_generation"]) + 1
        conn.execute(
            "UPDATE opspilot_incidents SET lifecycle=%s,observation_generation=%s WHERE incident_id=%s",
            (lifecycle, generation, incident_id),
        )
        identity = session_id or uuid4()
        due = first_sample_due_at or now + timedelta(seconds=sample_interval_seconds)
        conn.execute(
            "INSERT INTO opspilot_observation_sessions(session_id,incident_id,purpose,target_id,target,subject_control_generation,observation_generation,authorized_by,health_profile_revision,authorized_global_generation,authorized_target_generation,deadline_at,max_samples,sample_interval_seconds,sustained_window_seconds,issued_sequence,active_sample_job_id,active_sample_sequence,active_sample_due_at) VALUES(%s,%s,'incident_recovery',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s,1,%s)",
            (
                identity,
                incident_id,
                incident["target_id"],
                Jsonb(target.model_dump()),
                int(incident["control_generation"]),
                generation,
                actor,
                health_profile_revision,
                int(scope["global_generation"]),
                int(scope["target_generation"]),
                deadline_at,
                max_samples,
                sample_interval_seconds,
                sustained_window_seconds,
                uuid4(),
                due,
            ),
        )
        return identity

    def _store_profile(
        self, conn: Connection, revision: str, content: str | None
    ) -> None:
        """Keep the profile content behind a revision, once per revision.

        The revision must be ``<profile_id>@<sha256(content)[:12]>``; a
        revision already stored must carry the same content hash (a 12-hex
        prefix is not unique by construction, the full hash is checked).
        """
        if not isinstance(content, str) or not content:
            raise PersistenceError("INVALID_INPUT")
        profile_id, separator, suffix = revision.rpartition("@")
        digest = hashlib.sha256(content.encode()).hexdigest()
        if not separator or not profile_id or suffix != digest[:12]:
            raise PersistenceError("HEALTH_PROFILE_REVISION_MISMATCH")
        existing = conn.execute(
            "SELECT content_sha256 FROM opspilot_health_profiles WHERE health_profile_revision=%s",
            (revision,),
        ).fetchone()
        if existing is not None:
            if existing["content_sha256"] != digest:
                raise PersistenceError("HEALTH_PROFILE_REVISION_MISMATCH")
            return
        conn.execute(
            "INSERT INTO opspilot_health_profiles(health_profile_revision,profile_id,content_sha256,content) VALUES(%s,%s,%s,%s) ON CONFLICT (health_profile_revision) DO NOTHING",
            (revision, profile_id, digest, content),
        )

    def health_profile(self, revision: str) -> dict[str, Any]:
        """The stored canonical content of one profile revision."""
        if not isinstance(revision, str) or not revision:
            raise PersistenceError("INVALID_INPUT")
        with self.transaction(snapshot=True) as conn:
            row = conn.execute(
                "SELECT health_profile_revision,profile_id,content_sha256,content,created_at FROM opspilot_health_profiles WHERE health_profile_revision=%s",
                (revision,),
            ).fetchone()
        if row is None:
            raise PersistenceError("UNKNOWN_IDENTITY")
        return row

    def register_remediation(
        self,
        incident_id: UUID,
        *,
        expected_generation: int,
        actor: str,
        revision: str,
        deadline_at: datetime,
        max_samples: int,
        sample_interval_seconds: int,
        sustained_window_seconds: int,
        health_profile_revision: str | None = None,
        health_profile: str | None = None,
        session_id: UUID | None = None,
        identity: Mapping[str, str] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        """The human action "the incident was handled outside the system":
        one transaction that increments ``control_generation`` under
        ``expected_generation`` (``CONTROL_CONFLICT`` otherwise, like every
        other human decision), withdraws an earlier authorization, authorizes
        a new observation session bound to the new generation, moves the
        incident to ``observing_recovery`` and writes the ``opspilot_controls``
        audit row (action ``register_remediation``).

        ``identity`` (``integration_id``, ``cluster_uid``, ``namespace``,
        ``resource_uid`` from the configured registry, never the operator)
        completes the registry row the first time a remediation is
        registered on the target; a row already complete must agree with it
        (``TARGET_MISMATCH``), a row still bare without an identity to
        complete it refuses the registration (``TARGET_IDENTITY_MISSING``),
        both before anything is written (user decision 2026-10-07: intake
        does not need the identity, authorization does). The session
        parameters are the HealthProfile's, handed over by the caller that
        loaded it; the idempotency key is the workbench's (its
        ledger and audit reconciliation, as for the other actions). Refused
        while the global or target scope is suspended (``SCOPE_SUSPENDED``):
        a suspension outranks a separately granted observation authorization
        (C3 section 4), so the decision is not recorded as granted. The
        investigation Run is left alone (D3: observation is independent of
        the investigation); the generation step fences a lease an older
        attempt still holds, as any human decision does. Returns the new
        generation.
        """
        if (
            type(expected_generation) is not int
            or expected_generation < 0
            or (payload is not None and not isinstance(payload, dict))
        ):
            raise PersistenceError("INVALID_INPUT")
        with self.transaction() as conn:
            # Same order as control(): scope FOR SHARE, then the incident row.
            scope = self._lock_scope(conn, incident_id)
            incident = self._lock_incident(conn, incident_id)
            if int(incident["control_generation"]) != expected_generation:
                raise PersistenceError("CONTROL_CONFLICT")
            if scope["global_suspended"] or scope["target_suspended"]:
                raise PersistenceError("SCOPE_SUSPENDED")
            if incident["target_id"] is None:
                raise PersistenceError("UNKNOWN_TARGET")
            self._complete_identity(conn, incident["target_id"], identity)
            nxt = expected_generation + 1
            # Generation first: the session is bound to the generation this
            # decision creates, and the old authorization ends under it.
            conn.execute(
                "UPDATE opspilot_incidents SET control_generation=%s WHERE incident_id=%s",
                (nxt, incident_id),
            )
            revoked = revoke_authorized_sessions(conn, incident_id)
            session = self.authorize_session_in(
                conn,
                incident_id,
                revision=revision,
                actor=actor,
                deadline_at=deadline_at,
                max_samples=max_samples,
                sample_interval_seconds=sample_interval_seconds,
                sustained_window_seconds=sustained_window_seconds,
                health_profile_revision=health_profile_revision,
                health_profile=health_profile,
                session_id=session_id,
            )
            audit = dict(payload or {})
            audit.update(
                revision=revision,
                session_id=str(session),
                health_profile_revision=health_profile_revision,
                revoked_sessions=[str(item) for item in revoked],
            )
            conn.execute(
                "INSERT INTO opspilot_controls(audit_id,incident_id,action,expected_generation,resulting_generation,actor,payload) VALUES(%s,%s,'register_remediation',%s,%s,%s,%s)",
                (uuid4(), incident_id, expected_generation, nxt, actor, Jsonb(audit)),
            )
            return nxt

    def revoke_sessions(self, incident_id: UUID) -> list[UUID]:
        with self.transaction() as conn:
            self._lock_incident(conn, incident_id)
            return self.revoke_sessions_in(conn, incident_id)

    def revoke_sessions_in(self, conn: Connection, incident_id: UUID) -> list[UUID]:
        """Withdraw every authorized session of an incident (caller holds the
        incident lock). The lifecycle is the caller's decision, not this one's.
        The human-control paths call the same function directly
        (``opspilot.observation.revocation``)."""
        return revoke_authorized_sessions(conn, incident_id)

    @staticmethod
    def _end_session(
        conn: Connection,
        session_id: UUID,
        incident_id: UUID,
        *,
        ended_reason: EndedReason,
        transition: Transition | None,
        lifecycle_before: str,
        lifecycle_after: str,
        sample_id: UUID | None = None,
        watermark: dict[str, Any] | None = None,
    ) -> None:
        """Close a session: state by the session state machine, job slot
        cleared, and the ending recorded (the row the lifecycle evidence
        trigger looks for when the Observer changes the incident)."""
        end_session(
            conn,
            session_id,
            incident_id,
            ended_reason=ended_reason,
            transition=transition,
            lifecycle_before=lifecycle_before,
            lifecycle_after=lifecycle_after,
            sample_id=sample_id,
            watermark=watermark,
        )

    def _complete_identity(
        self, conn: Connection, target_id: UUID, identity: Mapping[str, str] | None
    ) -> None:
        """Write the identity columns of a bare registry row once; a complete
        row must agree with ``identity`` when one is given. The caller holds
        the incident row lock; the registry row is locked here."""
        fields = _IDENTITY_FIELDS + _BINDING_FIELDS
        if identity is not None and (
            set(identity) != set(fields)
            or any(
                not isinstance(identity[field], str) or not identity[field]
                for field in fields
            )
        ):
            raise PersistenceError("INVALID_INPUT")
        row = self._require_row(
            conn.execute(
                "SELECT integration_id,cluster_uid,namespace,resource_uid,workload FROM opspilot_targets WHERE target_id=%s FOR UPDATE",
                (target_id,),
            )
        )
        if identity is not None and identity["resource_uid"] != row["resource_uid"]:
            raise PersistenceError("TARGET_MISMATCH")
        complete = all(
            row[field] is not None for field in fields if field != "resource_uid"
        )
        if complete:
            if identity is not None and any(
                row[field] != identity[field] for field in fields
            ):
                raise PersistenceError("TARGET_MISMATCH")
            return
        if identity is None:
            raise PersistenceError("TARGET_IDENTITY_MISSING")
        conn.execute(
            "UPDATE opspilot_targets SET integration_id=%s,cluster_uid=%s,namespace=%s,workload=%s WHERE target_id=%s AND integration_id IS NULL AND cluster_uid IS NULL AND namespace IS NULL AND workload IS NULL",
            (
                identity["integration_id"],
                identity["cluster_uid"],
                identity["namespace"],
                identity["workload"],
                target_id,
            ),
        )

    def _registered_target(
        self, conn: Connection, target_id: UUID, revision: str
    ) -> Target:
        """The immutable identity the registry holds for ``target_id`` plus
        the revision snapshot of this authorization; a row without the
        identity cannot be observed (``TARGET_IDENTITY_MISSING``)."""
        row = self._require_row(
            conn.execute(
                "SELECT integration_id,cluster_uid,namespace,resource_uid FROM opspilot_targets WHERE target_id=%s",
                (target_id,),
            )
        )
        if any(row[field] is None for field in _IDENTITY_FIELDS):
            raise PersistenceError("TARGET_IDENTITY_MISSING")
        return Target(
            integration_id=row["integration_id"],
            cluster_uid=row["cluster_uid"],
            namespace=row["namespace"],
            resource_uid=row["resource_uid"],
            revision=revision,
        )

    # --- sampling (Observer side)

    def claim_due_samples(
        self,
        owner: UUID,
        *,
        limit: int = 20,
        lease_seconds: int = OBSERVER_LEASE_SECONDS,
    ) -> list[SampleLease]:
        """Lease due sampling jobs.

        A session whose scope is suspended right now is not leased (filtered
        in the query, so it does not use up ``limit``); one whose scope
        generation moved since its authorization is ended as
        ``scope_suspended`` instead of being leased: a lifted suspension never
        resumes an old authorization (C3 section 4). Re-claiming a job whose
        lease expired bumps the epoch and keeps the logical sequence (C3
        section 10). Sessions past their deadline are left to
        :meth:`sweep_expired_sessions`.
        """
        if (
            not isinstance(owner, UUID)
            or type(limit) is not int
            or limit <= 0
            or type(lease_seconds) is not int
            or lease_seconds <= 0
        ):
            raise PersistenceError("INVALID_INPUT")
        leases: list[SampleLease] = []
        with self.transaction() as conn:
            now = self._db_now(conn)
            # A lease is issued only under the incident row lock, which
            # serializes the claim against the suspension paths (they lock
            # every affected incident row FOR UPDATE, C3 section 4): a
            # suspension that committed first is seen in the scope read
            # below, one that comes later waits for this commit and then
            # invalidates the lease at submit. Ending a session needs the
            # same lock (the ending row's FK, and the lock order every other
            # ending path keeps: incident first, then session). Both rows are
            # locked by the one statement with SKIP LOCKED, so a session whose
            # incident another writer holds is skipped *before* LIMIT counts
            # it (issue #86: a separate per-row SKIP LOCKED probe let locked
            # candidates use up the limit) and the claim never waits.
            rows = conn.execute(
                "SELECT s.session_id,s.incident_id,s.active_sample_job_id,s.active_sample_sequence,s.active_sample_epoch,s.subject_control_generation,s.observation_generation,s.health_profile_revision,s.deadline_at,s.sample_interval_seconds,s.adopted_window_end,s.authorized_global_generation,s.authorized_target_generation,i.lifecycle FROM opspilot_observation_sessions s JOIN opspilot_incidents i ON i.incident_id=s.incident_id WHERE s.state='authorized' AND s.authorized AND s.active_sample_job_id IS NOT NULL AND s.active_sample_due_at<=%s AND (s.active_sample_lease_until IS NULL OR s.active_sample_lease_until<=%s) AND s.deadline_at>%s AND NOT (SELECT global_suspended FROM opspilot_scope_controls WHERE scope_id=1) AND NOT COALESCE((SELECT suspended FROM opspilot_target_suspensions t WHERE t.target_id=s.target_id),false) ORDER BY s.active_sample_due_at,s.session_id LIMIT %s FOR UPDATE OF s SKIP LOCKED FOR NO KEY UPDATE OF i SKIP LOCKED",
                (now, now, now, limit),
            ).fetchall()
            for row in rows:
                scope = self._lock_scope(conn, row["incident_id"], lock=False)
                blocked, generations = _scope_blocks(scope, row)
                if blocked:
                    lifecycle = str(row["lifecycle"])
                    self._end_session(
                        conn,
                        row["session_id"],
                        row["incident_id"],
                        ended_reason="scope_suspended",
                        transition=None,
                        lifecycle_before=lifecycle,
                        lifecycle_after=lifecycle,
                    )
                    continue
                epoch = int(row["active_sample_epoch"]) + 1
                until = now + timedelta(seconds=lease_seconds)
                conn.execute(
                    "UPDATE opspilot_observation_sessions SET active_sample_owner=%s,active_sample_epoch=%s,active_sample_lease_until=%s,updated_at=clock_timestamp() WHERE session_id=%s",
                    (owner, epoch, until, row["session_id"]),
                )
                leases.append(
                    SampleLease(
                        session_id=row["session_id"],
                        incident_id=row["incident_id"],
                        job_id=row["active_sample_job_id"],
                        sequence=int(row["active_sample_sequence"]),
                        owner=owner,
                        epoch=epoch,
                        lease_until=until,
                        subject_control_generation=int(
                            row["subject_control_generation"]
                        ),
                        observation_generation=int(row["observation_generation"]),
                        health_profile_revision=row["health_profile_revision"],
                        deadline_at=row["deadline_at"],
                        sample_interval_seconds=int(row["sample_interval_seconds"]),
                        adopted_window_end=row["adopted_window_end"],
                        global_suspension_generation=generations[0],
                        target_suspension_generation=generations[1],
                    )
                )
        return leases

    def lease_scope_current(self, lease: SampleLease) -> bool:
        """Is the authorization the lease was issued under still in force?

        Read-only snapshot, no lock: the Observer asks this before *every*
        Prometheus request (C3 section 4: task claim, gateway request and
        result adoption all check the current global/target control version,
        and human control takes precedence; issue #86, codex round 5 P1).
        False when any of these moved since the claim: the global or target
        scope is suspended now or its generation changed; the session is no
        longer ``authorized``; the active job is no longer this lease (a
        pause / takeover / cancel revokes the session and clears the lease);
        the incident's control generation or observation generation differs
        from the session's binding. The Observer then issues no further
        request and submits what it has, which the store files as
        ``suspended`` / ``lease_revoked`` / stale-binding history. A request
        already in flight cannot be recalled (bounded cancellation).
        """
        if not isinstance(lease, SampleLease):
            raise PersistenceError("INVALID_INPUT")
        with self.transaction(snapshot=True) as conn:
            scope = self._lock_scope(conn, lease.incident_id, lock=False)
            session = conn.execute(
                "SELECT state,active_sample_job_id,active_sample_owner,active_sample_epoch,subject_control_generation,observation_generation FROM opspilot_observation_sessions WHERE session_id=%s",
                (lease.session_id,),
            ).fetchone()
            incident = conn.execute(
                "SELECT control_generation,observation_generation FROM opspilot_incidents WHERE incident_id=%s",
                (lease.incident_id,),
            ).fetchone()
        if session is None or incident is None:
            return False
        return (
            not bool(scope["global_suspended"])
            and not bool(scope["target_suspended"])
            and int(scope["global_generation"]) == lease.global_suspension_generation
            and int(scope["target_generation"]) == lease.target_suspension_generation
            and session["state"] == "authorized"
            and session["active_sample_job_id"] == lease.job_id
            and session["active_sample_owner"] == lease.owner
            and int(session["active_sample_epoch"]) == lease.epoch
            and int(incident["control_generation"]) == lease.subject_control_generation
            and int(incident["observation_generation"]) == lease.observation_generation
            and int(session["subject_control_generation"])
            == lease.subject_control_generation
            and int(session["observation_generation"]) == lease.observation_generation
        )

    def current_time(self) -> datetime:
        """The database clock, the only clock product code reads (ruff TID251)."""
        with self.transaction(snapshot=True) as conn:
            return self._db_now(conn)

    def submit_sample(
        self,
        lease: SampleLease,
        sample: HealthSample,
        readings: Sequence[SignalReading],
    ) -> SampleReceipt:
        """Adopt or file one sample; everything commits in one transaction.

        Order inside the transaction: incident row lock, session row lock,
        control scope read, lease check, ``evaluate_sample``, watermark,
        session state, incident lifecycle, next job (or none), sample row,
        reading rows. A rejected sample with a valid lease releases the lease
        and postpones the same job (same sequence) by one interval; a sample
        that proves the bindings stale ends the session instead.
        """
        if not isinstance(lease, SampleLease) or not isinstance(sample, HealthSample):
            raise PersistenceError("INVALID_INPUT")
        rows = tuple(readings)
        if any(not isinstance(item, SignalReading) for item in rows):
            raise PersistenceError("INVALID_INPUT")
        if len({item.signal_name for item in rows}) != len(rows):
            raise PersistenceError("INVALID_INPUT")
        if sample.session_id != str(lease.session_id):
            raise PersistenceError("INVALID_INPUT")
        with self.transaction() as conn:
            incident = self._lock_incident(conn, lease.incident_id)
            row = self._lock_session(conn, lease.session_id)
            if row["incident_id"] != lease.incident_id:
                raise PersistenceError("INCONSISTENT_STATE")
            now = self._db_now(conn)
            scope = self._lock_scope(conn, lease.incident_id, lock=False)
            suspended, generations = _scope_blocks(scope, row)
            lease_valid = (
                row["active_sample_job_id"] == lease.job_id
                and row["active_sample_owner"] == lease.owner
                and int(row["active_sample_epoch"]) == lease.epoch
                and row["active_sample_lease_until"] is not None
                and row["active_sample_lease_until"] > now
            )
            # Sequence, generations and profile revision came with the lease;
            # other stamps are an invalid identity: filed as history
            # (``lease_stamp_mismatch``), never judged against the session
            # (so never ``binding_stale``), the job retried later.
            stamps_match = not lease_valid or (
                sample.sequence == int(row["active_sample_sequence"])
                and sample.subject_control_generation
                == lease.subject_control_generation
                and sample.observation_generation == lease.observation_generation
                and sample.health_profile_revision == lease.health_profile_revision
            )
            required, unreadable = self._required_signals_of(
                conn, row["health_profile_revision"]
            )
            readings_consistent = not _missing_required_signals(
                required,
                unreadable=unreadable,
                outcome=sample.outcome,
                required_signals_present=sample.required_signals_present,
                ok_signals={
                    item.signal_name
                    for item in rows
                    if _covers(
                        item.status,
                        item.value,
                        item.sample_count,
                        reading_window=(item.window_start, item.window_end),
                        sample_window=(sample.window.start, sample.window.end),
                    )
                },
            )
            if sample.window.end > now + timedelta(seconds=WINDOW_FUTURE_SKEW_SECONDS):
                # No telemetry covers a window that has not happened yet.
                raise PersistenceError("INVALID_INPUT")
            lifecycle = str(incident["lifecycle"])
            control_generation = int(incident["control_generation"])
            observation_generation = int(incident["observation_generation"])
            session = _domain_session(
                row,
                control_generation=control_generation,
                observation_generation=observation_generation,
                state=str(row["state"]),
                adopted_sequence=int(row["adopted_sequence"]),
                adopted_window_end=row["adopted_window_end"],
                active_job=row["active_sample_job_id"],
            )
            within = now < row["deadline_at"]
            verdict = _judge(
                session,
                sample,
                subject_state=lifecycle,
                within_deadline=within,
                suspension_blocks=suspended,
                lease_valid=lease_valid,
                stamps_match=stamps_match,
                readings_consistent=readings_consistent,
                authorized_at=row["authorized_at"],
                adopted_count=int(row["adopted_count"]),
                healthy_since=row["healthy_since"],
                max_samples=int(row["max_samples"]),
                sustained_window_seconds=int(row["sustained_window_seconds"]),
            )
            next_due: datetime | None = None
            sample_id = uuid4()
            if verdict.ended_reason is not None:
                if verdict.transition is not None:
                    lifecycle = self._transition(lifecycle, verdict.transition)
                if lifecycle != incident["lifecycle"]:
                    conn.execute(
                        "UPDATE opspilot_incidents SET lifecycle=%s WHERE incident_id=%s",
                        (lifecycle, lease.incident_id),
                    )
            elif verdict.decision.accepted:
                next_due = now + timedelta(seconds=int(row["sample_interval_seconds"]))
                conn.execute(
                    "UPDATE opspilot_observation_sessions SET adopted_sequence=%s,adopted_window_end=%s,adopted_count=%s,healthy_since=%s,issued_sequence=issued_sequence+1,active_sample_job_id=%s,active_sample_sequence=issued_sequence+1,active_sample_due_at=%s,active_sample_owner=NULL,active_sample_epoch=0,active_sample_lease_until=NULL,updated_at=clock_timestamp() WHERE session_id=%s",
                    (
                        verdict.adopted_sequence,
                        verdict.adopted_window_end,
                        verdict.adopted_count,
                        verdict.healthy_since,
                        uuid4(),
                        next_due,
                        lease.session_id,
                    ),
                )
            elif lease_valid:
                # Released and postponed: the same job retries one interval
                # later, not immediately and not forever.
                next_due = now + timedelta(seconds=int(row["sample_interval_seconds"]))
                conn.execute(
                    "UPDATE opspilot_observation_sessions SET active_sample_due_at=%s,active_sample_owner=NULL,active_sample_lease_until=NULL,updated_at=clock_timestamp() WHERE session_id=%s",
                    (next_due, lease.session_id),
                )
            conn.execute(
                "INSERT INTO opspilot_observation_samples(sample_id,session_id,job_id,sequence,epoch,window_start,window_end,outcome,required_signals_present,subject_control_generation,observation_generation,health_profile_revision,disposition,reason,confirms_health,health_basis,subject_lifecycle,incident_control_generation,incident_observation_generation,scope_suspended,global_generation,target_generation,within_deadline,lease_valid,lease_stamps_match,readings_consistent,transition) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    sample_id,
                    lease.session_id,
                    lease.job_id,
                    sample.sequence,
                    lease.epoch,
                    sample.window.start,
                    sample.window.end,
                    sample.outcome,
                    sample.required_signals_present,
                    sample.subject_control_generation,
                    sample.observation_generation,
                    sample.health_profile_revision,
                    verdict.decision.disposition,
                    verdict.decision.reason,
                    verdict.healthy,
                    verdict.health_basis,
                    incident["lifecycle"],
                    control_generation,
                    observation_generation,
                    suspended,
                    generations[0],
                    generations[1],
                    within,
                    lease_valid,
                    stamps_match,
                    readings_consistent,
                    verdict.transition,
                ),
            )
            with conn.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO opspilot_observation_signal_readings(sample_id,signal_name,status,value,sample_count,query,window_start,window_end,source,raw_sha256,raw) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    [
                        (
                            sample_id,
                            item.signal_name,
                            item.status,
                            item.value,
                            item.sample_count,
                            item.query,
                            item.window_start,
                            item.window_end,
                            item.source,
                            item.raw_sha256,
                            item.raw,
                        )
                        for item in rows
                    ],
                )
            if verdict.ended_reason is not None:
                # After the sample row: the ending points at it, and the
                # evidence trigger checks both at commit.
                self._end_session(
                    conn,
                    lease.session_id,
                    lease.incident_id,
                    ended_reason=verdict.ended_reason,
                    transition=verdict.transition,
                    lifecycle_before=str(incident["lifecycle"]),
                    lifecycle_after=lifecycle,
                    sample_id=sample_id if verdict.decision.accepted else None,
                    watermark={
                        "adopted_sequence": verdict.adopted_sequence,
                        "adopted_window_end": verdict.adopted_window_end,
                        "adopted_count": verdict.adopted_count,
                        "healthy_since": verdict.healthy_since,
                    },
                )
            return SampleReceipt(
                sample_id=sample_id,
                accepted=verdict.decision.accepted,
                disposition=verdict.decision.disposition,
                reason=verdict.decision.reason,
                confirms_health=verdict.healthy,
                health_basis=verdict.health_basis,
                session_state=verdict.session_state,
                incident_lifecycle=lifecycle,
                transition=verdict.transition,
                next_sample_due_at=next_due,
            )

    def sweep_expired_sessions(self, *, limit: int = 20) -> list[UUID]:
        """End authorized sessions whose deadline passed without confirmation:
        the session expires and the incident goes back to ``open`` (interface
        contract item 6). One transaction per session, incident lock first."""
        if type(limit) is not int or limit <= 0:
            raise PersistenceError("INVALID_INPUT")
        with self.transaction(snapshot=True) as conn:
            candidates = conn.execute(
                "SELECT session_id,incident_id FROM opspilot_observation_sessions WHERE state='authorized' AND deadline_at<=clock_timestamp() ORDER BY deadline_at,session_id LIMIT %s",
                (limit,),
            ).fetchall()
        expired: list[UUID] = []
        for candidate in candidates:
            with self.transaction() as conn:
                incident = self._lock_incident(conn, candidate["incident_id"])
                row = self._lock_session(conn, candidate["session_id"])
                if row["state"] != "authorized" or row["deadline_at"] > self._db_now(
                    conn
                ):
                    continue
                lifecycle = self._transition(
                    str(incident["lifecycle"]), "observation_ended_unconfirmed"
                )
                changed = lifecycle != incident["lifecycle"]
                self._end_session(
                    conn,
                    candidate["session_id"],
                    candidate["incident_id"],
                    ended_reason="deadline_expired",
                    transition="observation_ended_unconfirmed",
                    lifecycle_before=str(incident["lifecycle"]),
                    lifecycle_after=lifecycle,
                )
                if changed:
                    conn.execute(
                        "UPDATE opspilot_incidents SET lifecycle=%s WHERE incident_id=%s",
                        (lifecycle, candidate["incident_id"]),
                    )
                expired.append(cast(UUID, candidate["session_id"]))
        return expired

    # --- reading back

    def incident_sessions(self, incident_id: UUID) -> list[dict[str, Any]]:
        """Every session of an incident, oldest first (the workbench's view)."""
        if not isinstance(incident_id, UUID):
            raise PersistenceError("INVALID_INPUT")
        with self.transaction(snapshot=True) as conn:
            return list(
                conn.execute(
                    f"SELECT {_SESSION_COLUMNS} FROM opspilot_observation_sessions WHERE incident_id=%s ORDER BY created_at,session_id",
                    (incident_id,),
                ).fetchall()
            )

    def session(self, session_id: UUID) -> dict[str, Any]:
        with self.transaction(snapshot=True) as conn:
            row = conn.execute(
                f"SELECT {_SESSION_COLUMNS} FROM opspilot_observation_sessions WHERE session_id=%s",
                (session_id,),
            ).fetchone()
        if row is None:
            raise PersistenceError("UNKNOWN_IDENTITY")
        return row

    def session_history(self, session_id: UUID) -> dict[str, Any]:
        """The session row and every sample with its readings, one snapshot."""
        if not isinstance(session_id, UUID):
            raise PersistenceError("INVALID_INPUT")
        with self.transaction(snapshot=True) as conn:
            row = conn.execute(
                f"SELECT {_SESSION_COLUMNS} FROM opspilot_observation_sessions WHERE session_id=%s",
                (session_id,),
            ).fetchone()
            if row is None:
                raise PersistenceError("UNKNOWN_IDENTITY")
            incident = self._require_row(
                conn.execute(
                    "SELECT lifecycle FROM opspilot_incidents WHERE incident_id=%s",
                    (row["incident_id"],),
                )
            )
            profile = (
                conn.execute(
                    "SELECT health_profile_revision,profile_id,content_sha256,content,created_at FROM opspilot_health_profiles WHERE health_profile_revision=%s",
                    (row["health_profile_revision"],),
                ).fetchone()
                if row["health_profile_revision"] is not None
                else None
            )
            samples = conn.execute(
                f"SELECT {_SAMPLE_COLUMNS} FROM opspilot_observation_samples WHERE session_id=%s ORDER BY submitted_at,sample_id",
                (session_id,),
            ).fetchall()
            endings = conn.execute(
                f"SELECT {_ENDING_COLUMNS} FROM opspilot_observation_endings WHERE session_id=%s ORDER BY recorded_at,ending_id",
                (session_id,),
            ).fetchall()
            readings = conn.execute(
                f"SELECT {_READING_COLUMNS} FROM opspilot_observation_signal_readings WHERE sample_id IN (SELECT sample_id FROM opspilot_observation_samples WHERE session_id=%s) ORDER BY sample_id,signal_name",
                (session_id,),
            ).fetchall()
        by_sample: dict[UUID, list[dict[str, Any]]] = {}
        for reading in readings:
            by_sample.setdefault(reading["sample_id"], []).append(reading)
        for sample in samples:
            sample["readings"] = by_sample.get(sample["sample_id"], [])
        return {
            "session": row,
            "incident_lifecycle": str(incident["lifecycle"]),
            "health_profile": profile,
            "samples": samples,
            "endings": endings,
        }

    def replay_session(self, session_id: UUID) -> ReplayReport:
        """Recompute every stored decision from the stored rows alone.

        Reads nothing but the session, its samples and readings; the
        conditions each decision was taken under travel with the sample row.
        Stored raw payloads are re-hashed against ``raw_sha256``; the fold's
        final session state and the lifecycle the verdicts imply are compared
        with what is stored. A mismatch means the stored basis no longer
        reproduces the stored verdict.
        """
        history = self.session_history(session_id)
        row = history["session"]
        state = "authorized"
        adopted_sequence, adopted_window_end = 0, None
        adopted_count, healthy_since = 0, None
        expected_lifecycle: str | None = None
        ending_sample_id: UUID | None = None
        # No profile revision: nothing to check (such a session never confirms
        # health). A revision whose stored content cannot yield the required
        # signals fails every sample of the replay.
        required, unreadable = _parse_required(history["health_profile"])
        replayed: list[ReplayedSample] = []
        for stored in history["samples"]:
            session = _domain_session(
                row,
                control_generation=int(stored["incident_control_generation"]),
                observation_generation=int(stored["incident_observation_generation"]),
                state=state,
                adopted_sequence=adopted_sequence,
                adopted_window_end=adopted_window_end,
                active_job=stored["job_id"],
            )
            sample = HealthSample(
                sample_id=str(stored["sample_id"]),
                session_id=str(session_id),
                sequence=int(stored["sequence"]),
                window=QueryWindow(
                    start=stored["window_start"], end=stored["window_end"]
                ),
                outcome=stored["outcome"],
                subject_control_generation=int(stored["subject_control_generation"]),
                observation_generation=int(stored["observation_generation"]),
                health_profile_revision=stored["health_profile_revision"],
                required_signals_present=bool(stored["required_signals_present"]),
            )
            ok_signals = {
                str(reading["signal_name"])
                for reading in stored["readings"]
                if _covers(
                    reading["status"],
                    reading["value"],
                    reading["sample_count"],
                    reading_window=(reading["window_start"], reading["window_end"]),
                    sample_window=(stored["window_start"], stored["window_end"]),
                )
            }
            # Recomputed from the stored rows: a deleted reading changes the
            # replayed decision (readings_inconsistent) as well as this list.
            signal_mismatches = _missing_required_signals(
                required,
                unreadable=unreadable,
                outcome=str(stored["outcome"]),
                required_signals_present=bool(stored["required_signals_present"]),
                ok_signals=ok_signals,
            )
            verdict = _judge(
                session,
                sample,
                subject_state=str(stored["subject_lifecycle"]),
                within_deadline=bool(stored["within_deadline"]),
                suspension_blocks=bool(stored["scope_suspended"]),
                lease_valid=bool(stored["lease_valid"]),
                stamps_match=bool(stored["lease_stamps_match"]),
                readings_consistent=not signal_mismatches,
                authorized_at=row["authorized_at"],
                adopted_count=adopted_count,
                healthy_since=healthy_since,
                max_samples=int(row["max_samples"]),
                sustained_window_seconds=int(row["sustained_window_seconds"]),
            )
            state = verdict.session_state
            adopted_sequence = verdict.adopted_sequence
            adopted_window_end = verdict.adopted_window_end
            adopted_count = verdict.adopted_count
            healthy_since = verdict.healthy_since
            if verdict.transition == "recovery_confirmed":
                expected_lifecycle = "resolved"
            elif verdict.transition == "observation_ended_unconfirmed":
                expected_lifecycle = "open"
            if (
                verdict.ended_reason is not None
                and verdict.decision.accepted
                and ending_sample_id is None
            ):
                # The adopted sample whose verdict ended the session; a
                # rejected sample ending it (deadline, stale binding, scope)
                # leaves the record pointing at no sample.
                ending_sample_id = cast(UUID, stored["sample_id"])
            raw_mismatches = tuple(
                str(reading["signal_name"])
                for reading in stored["readings"]
                if reading["raw"] is not None
                and hashlib.sha256(bytes(reading["raw"])).hexdigest()
                != reading["raw_sha256"]
            )
            replayed.append(
                ReplayedSample(
                    sample_id=stored["sample_id"],
                    sequence=int(stored["sequence"]),
                    signal_mismatches=signal_mismatches,
                    stored=(
                        str(stored["disposition"]),
                        str(stored["reason"]),
                        bool(stored["confirms_health"]),
                        str(stored["health_basis"]),
                        stored["transition"],
                    ),
                    replayed=(
                        verdict.decision.disposition,
                        verdict.decision.reason,
                        verdict.healthy,
                        verdict.health_basis,
                        verdict.transition,
                    ),
                    raw_mismatches=raw_mismatches,
                )
            )
        # The record to compare with: the sample that ended the session (a
        # late duplicate filed as history afterwards says nothing new), else
        # the last sample.
        recorded_lifecycle: str | None = None
        ending = [item for item in history["samples"] if item["transition"] is not None]
        if ending or history["samples"]:
            last = ending[-1] if ending else history["samples"][-1]
            recorded_lifecycle = str(last["subject_lifecycle"])
            trigger = last["transition"]
            if trigger is not None and trigger in INCIDENT_LIFECYCLE.triggers(
                recorded_lifecycle
            ):
                recorded_lifecycle = INCIDENT_LIFECYCLE.fire(
                    recorded_lifecycle, trigger
                )
        endings = history["endings"]
        recorded_ending = None
        if endings:
            last_ending = endings[-1]
            recorded_ending = (
                str(last_ending["ended_reason"]),
                last_ending["transition"],
                last_ending["sample_id"],
                str(last_ending["lifecycle_before"]),
                str(last_ending["lifecycle_after"]),
            )
        return ReplayReport(
            session_id=session_id,
            samples=tuple(replayed),
            replayed_session_state=state,
            stored_session_state=str(row["state"]),
            expected_lifecycle=expected_lifecycle,
            recorded_lifecycle=recorded_lifecycle,
            stored_ended_reason=row["ended_reason"],
            recorded_ending=recorded_ending,
            ending_sample_id=ending_sample_id,
        )

    # --- helpers

    def _lock_incident(self, conn: Connection, incident_id: UUID) -> dict[str, Any]:
        if not isinstance(incident_id, UUID):
            raise PersistenceError("INVALID_INPUT")
        row = conn.execute(
            "SELECT incident_id,lifecycle,control_generation,observation_generation,target_id FROM opspilot_incidents WHERE incident_id=%s FOR UPDATE",
            (incident_id,),
        ).fetchone()
        if row is None:
            raise PersistenceError("UNKNOWN_IDENTITY")
        return row

    def _required_signals_of(
        self, conn: Connection, revision: str | None
    ) -> tuple[tuple[str, ...] | None, bool]:
        if revision is None:
            return None, False
        profile = conn.execute(
            "SELECT health_profile_revision,content_sha256,content FROM opspilot_health_profiles WHERE health_profile_revision=%s",
            (revision,),
        ).fetchone()
        return _parse_required(profile)

    def _lock_session(self, conn: Connection, session_id: UUID) -> dict[str, Any]:
        if not isinstance(session_id, UUID):
            raise PersistenceError("INVALID_INPUT")
        row = conn.execute(
            f"SELECT {_SESSION_COLUMNS} FROM opspilot_observation_sessions WHERE session_id=%s FOR UPDATE",
            (session_id,),
        ).fetchone()
        if row is None:
            raise PersistenceError("UNKNOWN_IDENTITY")
        return row

    @staticmethod
    def _fire_incident(lifecycle: str, trigger: str) -> str:
        try:
            return INCIDENT_LIFECYCLE.fire(lifecycle, trigger)
        except DomainError as exc:
            raise PersistenceError("ILLEGAL_TRANSITION") from exc

    @classmethod
    def _transition(cls, lifecycle: str, trigger: Transition) -> str:
        """Apply the trigger a session ending fires on its incident.

        ``recovery_confirmed`` must be legal or nothing is written: an
        authorized session on an incident that is not observing (a human
        renewal moved it back to ``open`` without revoking the session; step 3
        closes that gap in one transaction) cannot be confirmed recovered.
        ``observation_ended_unconfirmed`` asserts nothing, so a lifecycle that
        cannot take it (already ``open``) is left as it is and only the
        session ends.
        """
        if trigger == "recovery_confirmed":
            return cls._fire_incident(lifecycle, trigger)
        if trigger in INCIDENT_LIFECYCLE.triggers(lifecycle):
            return INCIDENT_LIFECYCLE.fire(lifecycle, trigger)
        return lifecycle
