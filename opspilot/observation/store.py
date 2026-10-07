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
from collections.abc import Sequence
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
    "lease_valid,transition,submitted_at"
)
_ENDING_COLUMNS = (
    "ending_id,session_id,incident_id,ended_reason,transition,sample_id,recorded_at"
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
        if self.status != "ok" and self.value is not None:
            raise ValueError("VALUE_WITHOUT_OK_STATUS")
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
        return (
            self.stored == self.replayed
            and not self.raw_mismatches
            and not self.signal_mismatches
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

    @property
    def session_consistent(self) -> bool:
        if self.replayed_session_state == self.stored_session_state:
            return True
        return (
            self.replayed_session_state == "authorized"
            and self.stored_session_state
            in {
                "expired",
                "revoked",
            }
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
    if lease_valid:
        decision = evaluate_sample(
            session,
            sample,
            subject_state=subject_state,
            within_deadline=within_deadline,
            suspension_blocks=suspension_blocks,
        )
    else:
        decision = SampleAcceptance(
            accepted=False, disposition="history_only", reason="lease_revoked"
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
        target: Target,
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

        ``health_profile`` is the canonical JSON text of the profile the
        revision names; it is stored once per revision so a replay can read
        the coverage queries and thresholds the samples were judged by.

        Storage primitive only: the human "handling registered" action with
        its ``expected_version``, idempotency key and audit row is step 3,
        which calls :meth:`authorize_session_in` inside its own transaction.
        """
        with self.transaction() as conn:
            return self.authorize_session_in(
                conn,
                incident_id,
                target=target,
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
        target: Target,
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
        """
        if (
            not isinstance(incident_id, UUID)
            or not isinstance(target, Target)
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
        elif health_profile is not None:
            raise PersistenceError("INVALID_INPUT")
        incident = self._lock_incident(conn, incident_id)
        if incident["target_id"] is None:
            raise PersistenceError("UNKNOWN_TARGET")
        registered = self._require_row(
            conn.execute(
                "SELECT resource_uid FROM opspilot_targets WHERE target_id=%s",
                (incident["target_id"],),
            )
        )
        if registered["resource_uid"] != target.resource_uid:
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

    def revoke_sessions(self, incident_id: UUID) -> list[UUID]:
        with self.transaction() as conn:
            self._lock_incident(conn, incident_id)
            return self.revoke_sessions_in(conn, incident_id)

    def revoke_sessions_in(self, conn: Connection, incident_id: UUID) -> list[UUID]:
        """Withdraw every authorized session of an incident (caller holds the
        incident lock). The lifecycle is the caller's decision, not this one's."""
        if not isinstance(incident_id, UUID):
            raise PersistenceError("INVALID_INPUT")
        rows = conn.execute(
            "SELECT session_id FROM opspilot_observation_sessions WHERE incident_id=%s AND state='authorized' ORDER BY session_id FOR UPDATE",
            (incident_id,),
        ).fetchall()
        revoked: list[UUID] = []
        for row in rows:
            self._end_session(
                conn,
                row["session_id"],
                incident_id,
                ended_reason="authority_revoked",
                transition=None,
            )
            revoked.append(cast(UUID, row["session_id"]))
        return revoked

    def _end_session(
        self,
        conn: Connection,
        session_id: UUID,
        incident_id: UUID,
        *,
        ended_reason: EndedReason,
        transition: Transition | None,
        sample_id: UUID | None = None,
        watermark: dict[str, Any] | None = None,
    ) -> None:
        """Close a session: state by the session state machine, job slot
        cleared, and the ending recorded (the row the lifecycle evidence
        trigger looks for when the Observer changes the incident)."""
        trigger = {
            "recovery_confirmed": "observation_completed",
            "deadline_expired": "deadline_expired",
            "max_samples_exhausted": "deadline_expired",
            "authority_revoked": "authority_revoked",
            "binding_stale": "authority_revoked",
            "scope_suspended": "authority_revoked",
        }[ended_reason]
        state = OBSERVATION_SESSION.fire("authorized", trigger)
        marks = watermark or {}
        conn.execute(
            "UPDATE opspilot_observation_sessions SET state=%s,ended_reason=%s,adopted_sequence=COALESCE(%s,adopted_sequence),adopted_window_end=COALESCE(%s,adopted_window_end),adopted_count=COALESCE(%s,adopted_count),healthy_since=CASE WHEN %s THEN %s ELSE healthy_since END,active_sample_job_id=NULL,active_sample_sequence=NULL,active_sample_due_at=NULL,active_sample_owner=NULL,active_sample_lease_until=NULL,updated_at=clock_timestamp() WHERE session_id=%s",
            (
                state,
                ended_reason,
                marks.get("adopted_sequence"),
                marks.get("adopted_window_end"),
                marks.get("adopted_count"),
                watermark is not None,
                marks.get("healthy_since"),
                session_id,
            ),
        )
        conn.execute(
            "INSERT INTO opspilot_observation_endings(ending_id,session_id,incident_id,ended_reason,transition,sample_id) VALUES(%s,%s,%s,%s,%s,%s)",
            (uuid4(), session_id, incident_id, ended_reason, transition, sample_id),
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
            rows = conn.execute(
                "SELECT s.session_id,s.incident_id,s.active_sample_job_id,s.active_sample_sequence,s.active_sample_epoch,s.subject_control_generation,s.observation_generation,s.health_profile_revision,s.deadline_at,s.sample_interval_seconds,s.adopted_window_end,s.authorized_global_generation,s.authorized_target_generation FROM opspilot_observation_sessions s WHERE s.state='authorized' AND s.authorized AND s.active_sample_job_id IS NOT NULL AND s.active_sample_due_at<=%s AND (s.active_sample_lease_until IS NULL OR s.active_sample_lease_until<=%s) AND s.deadline_at>%s AND NOT (SELECT global_suspended FROM opspilot_scope_controls WHERE scope_id=1) AND NOT COALESCE((SELECT suspended FROM opspilot_target_suspensions t WHERE t.target_id=s.target_id),false) ORDER BY s.active_sample_due_at,s.session_id LIMIT %s FOR UPDATE OF s SKIP LOCKED",
                (now, now, now, limit),
            ).fetchall()
            for row in rows:
                # Read, not locked: the incident row lock in submit_sample()
                # is what orders a sample against a suspension (the
                # suspension paths lock every affected incident row), and
                # a lock here would need UPDATE on the control tables.
                scope = self._lock_scope(conn, row["incident_id"], lock=False)
                blocked, generations = _scope_blocks(scope, row)
                if blocked:
                    # Ending needs the incident row (the ending row's FK and
                    # the lock order every other ending path keeps: incident
                    # first, then session). This transaction already holds
                    # the session row, so it must not wait for the incident:
                    # take it only if free, else leave the session for the
                    # next claim or sweep.
                    if (
                        conn.execute(
                            "SELECT incident_id FROM opspilot_incidents WHERE incident_id=%s FOR UPDATE SKIP LOCKED",
                            (row["incident_id"],),
                        ).fetchone()
                        is None
                    ):
                        continue
                    self._end_session(
                        conn,
                        row["session_id"],
                        row["incident_id"],
                        ended_reason="scope_suspended",
                        transition=None,
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
            if lease_valid and (
                sample.sequence != int(row["active_sample_sequence"])
                or sample.subject_control_generation != lease.subject_control_generation
                or sample.observation_generation != lease.observation_generation
                or sample.health_profile_revision != lease.health_profile_revision
            ):
                # Sequence, generations and profile revision came with the
                # lease; different stamps are an Observer bug, not an
                # observation, and must not end the session as stale.
                raise PersistenceError("INVALID_INPUT")
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
                "INSERT INTO opspilot_observation_samples(sample_id,session_id,job_id,sequence,epoch,window_start,window_end,outcome,required_signals_present,subject_control_generation,observation_generation,health_profile_revision,disposition,reason,confirms_health,health_basis,subject_lifecycle,incident_control_generation,incident_observation_generation,scope_suspended,global_generation,target_generation,within_deadline,lease_valid,transition) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
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
                    transition=(
                        verdict.transition
                        if lifecycle != incident["lifecycle"]
                        else None
                    ),
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
                    transition="observation_ended_unconfirmed" if changed else None,
                )
                if changed:
                    conn.execute(
                        "UPDATE opspilot_incidents SET lifecycle=%s WHERE incident_id=%s",
                        (lifecycle, candidate["incident_id"]),
                    )
                expired.append(cast(UUID, candidate["session_id"]))
        return expired

    # --- reading back

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
        # No profile revision: nothing to check (such a session never confirms
        # health). A revision whose stored content cannot yield the required
        # signals fails every sample of the replay.
        required: tuple[str, ...] | None = None
        unreadable = False
        if history["health_profile"] is not None:
            try:
                required = required_signals(str(history["health_profile"]["content"]))
            except PersistenceError:
                unreadable = True
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
            verdict = _judge(
                session,
                sample,
                subject_state=str(stored["subject_lifecycle"]),
                within_deadline=bool(stored["within_deadline"]),
                suspension_blocks=bool(stored["scope_suspended"]),
                lease_valid=bool(stored["lease_valid"]),
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
            raw_mismatches = tuple(
                str(reading["signal_name"])
                for reading in stored["readings"]
                if reading["raw"] is not None
                and hashlib.sha256(bytes(reading["raw"])).hexdigest()
                != reading["raw_sha256"]
            )
            ok_signals = {
                str(reading["signal_name"])
                for reading in stored["readings"]
                if reading["status"] == "ok"
            }
            signal_mismatches: tuple[str, ...] = ()
            if unreadable:
                signal_mismatches = ("health_profile_unreadable",)
            elif required is not None:
                missing = tuple(name for name in required if name not in ok_signals)
                # Structural check only (F6 step 5's threshold replay is #87):
                # a healthy sample must have an ok reading for every required
                # signal, and the stored flag must say what the rows say.
                if (stored["outcome"] == "healthy" and missing) or bool(
                    stored["required_signals_present"]
                ) != (not missing):
                    signal_mismatches = missing or ("required_signals_present",)
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
        return ReplayReport(
            session_id=session_id,
            samples=tuple(replayed),
            replayed_session_state=state,
            stored_session_state=str(row["state"]),
            expected_lifecycle=expected_lifecycle,
            recorded_lifecycle=recorded_lifecycle,
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
