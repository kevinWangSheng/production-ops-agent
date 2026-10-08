"""External F6 seam: committed recovery records -> ``RecoveryOutcome`` (#140).

The acceptance entry for recovery observation (AGENTS.md: ``IncidentScenario
-> IncidentOutcome``). Like ``opspilot.acceptance`` for the investigation
Run, this module has no transport, no model and no SQL of its own: it
projects what the product committed -- the incident row, every observation
session with its samples, readings, raw bundles and ending records, the
human control audit -- and what the offline replay (``opspilot.observer
.replay``) recomputes from those rows. The verdict, the reasons and the
healthy window are the replay's; nothing here judges telemetry again, and a
record the replay cannot reproduce comes out as ``unknown`` with
``STORED_OBSERVATION_INTEGRITY_MISMATCH`` among its reasons.

Field sources (``RecoveryOutcome``):

* ``incident_lifecycle`` / ``incident_mode``: the committed incident row.
* ``target``: the session's immutable target binding (``opspilot_observation
  _sessions.target``), never telemetry content (PRODUCT-CONSTRAINTS).
* ``recovery_verdict`` / ``recovery_confirmed`` / ``latest_sample_verdict`` /
  ``healthy_window_seconds``: ``SessionReplay`` of the latest session.
* ``recovery_reasons``: the replay's integrity codes plus the per-signal
  verdicts it recomputed for the latest adopted sample (``INSUFFICIENT_
  TRAFFIC``, ``REQUIRED_TELEMETRY_MISSING``, ``MISSING_SIGNAL:<name>``,
  ``STALE_TELEMETRY:<name>``, ``DEGRADED_SIGNAL:<name>``), ``CONTINUED_
  DEGRADATION`` when the verdict is degraded and ``OBSERVATION_UNCONFIRMED``
  when an ended session confirmed nothing.
* ``used_sample_count``: the session row's ``adopted_count`` (the replay
  checks it against the fold).
* ``observation_ended`` / ``observation_ended_reason``: the session row's
  state and ``ended_reason``.
* ``human_interaction`` / ``handoff_reasons``: ``handoff`` when the session
  ended by deadline or budget (C3 section 10: bounded continuation, then
  handoff), with the ending code and the recovery reasons.
* ``recovery_samples``: every sample row with its reading rows (value,
  status, point count, query, source, window, the bundle's sha256 and the
  sha256 of the response body inside it) and the replay's per-signal
  verdict; ``evidence_id`` is ``<sample_id>:<signal_name>``.
* ``recovery_profile_revision`` / ``recovery_profile_content``: the frozen
  profile row behind the session's revision; ``recovery_handled_at``: the
  session's ``authorized_at``.
* ``actions``: the audit of what the product did, in record order -- the
  control rows (``register_remediation`` -> ``record_handling`` +
  ``advance_incident_lifecycle``; ``takeover`` -> ``human_takeover``; any
  other control -> ``human_control:<action>``), then per session each
  sample's reading rows that issued queries (``read_only_query`` per
  reading, the profile sentinel issues none), ``persist_observation`` per
  sample, and per ending record ``advance_incident_lifecycle`` when the
  lifecycle moved and ``human_handoff`` for a deadline or budget ending.
* ``permissions``: derived from the privileges the Observer login actually
  holds (``ObservationStore.table_privileges``), see
  ``permissions_from_grants``; ``human_control`` when a control row exists.
* ``model_requests``: empty -- the recovery path has no model client
  (``opspilot.observer`` imports none, checked by ``tests/test_m1_observer
  .py``); the acceptance harness counts calls on its own stub.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from opspilot.observer.health_profile import HealthProfile
from opspilot.observer.replay import (
    ReplayedReading,
    SessionReplay,
    replay_history,
)
from opspilot.observer.sampler import PROFILE_SENTINEL

__all__ = [
    "RecoveryOutcome",
    "RecoveryRecords",
    "RecoverySample",
    "RecoverySignal",
    "permissions_from_grants",
    "recovery_outcome",
]

#: The writes the Observer role needs for its own records (migration 0003):
#: anything beyond these is reported as a wider permission.
OWN_RECORD_WRITES: Mapping[str, frozenset[str]] = {
    "opspilot_observation_sessions": frozenset({"UPDATE"}),
    "opspilot_observation_samples": frozenset({"INSERT"}),
    "opspilot_observation_signal_readings": frozenset({"INSERT"}),
    "opspilot_observation_endings": frozenset({"INSERT"}),
    "opspilot_incidents": frozenset({"UPDATE"}),
}
#: The records the verdict is read from; a login that cannot read them is
#: not a read-only observer of them.
RECORDS_READ: frozenset[str] = frozenset(
    {
        "opspilot_incidents",
        "opspilot_observation_sessions",
        "opspilot_observation_samples",
        "opspilot_observation_signal_readings",
        "opspilot_observation_endings",
        "opspilot_health_profiles",
    }
)
_WRITES = frozenset({"INSERT", "UPDATE"})
_DELETES = frozenset({"DELETE", "TRUNCATE"})
_HANDOFF_ENDINGS = frozenset({"deadline_expired", "max_samples_exhausted"})
_SESSION_CONTRACT_FIELDS = (
    "session_id",
    "purpose",
    "subject_control_generation",
    "observation_generation",
    "state",
    "health_profile_revision",
    "adopted_sequence",
    "adopted_window_end",
    "active_sample_job_id",
)


@dataclass(frozen=True)
class RecoveryRecords:
    """What the product committed, as read by the caller in one snapshot
    (``ObservationStore.incident_records``) plus the privileges measured on
    the Observer's own connection (``ObservationStore.table_privileges``)."""

    incident: Mapping[str, Any]
    sessions: tuple[Mapping[str, Any], ...]
    controls: tuple[Mapping[str, Any], ...]
    grants: Mapping[str, frozenset[str]]


@dataclass(frozen=True)
class RecoverySignal:
    signal_name: str
    status: str
    value: float | None
    sample_count: int | None
    query: str
    source: str
    window_start: datetime
    window_end: datetime
    evaluated_at: datetime
    evidence_id: str
    raw_sha256: str | None
    body_sha256: str | None
    verdict: str | None
    reason: str | None


@dataclass(frozen=True)
class RecoverySample:
    subject_id: str
    sample_id: str
    session_id: str
    sequence: int
    target: Mapping[str, Any]
    subject_control_generation: int
    observation_generation: int
    health_profile_revision: str | None
    window_start: datetime
    window_end: datetime
    disposition: str
    reason: str
    outcome: str
    required_signals_present: bool
    confirms_health: bool
    health_basis: str
    transition: str | None
    signals: Mapping[str, RecoverySignal]


@dataclass(frozen=True)
class RecoveryOutcome:
    """Only externally inspectable evidence, decisions, actions and state of
    one incident's recovery observation."""

    scenario_id: str
    subject_id: str
    incident_lifecycle: str
    incident_mode: str | None
    target: Mapping[str, Any] | None
    recovery_confirmed: bool
    recovery_verdict: str
    recovery_reasons: tuple[str, ...]
    latest_sample_verdict: str | None
    healthy_window_seconds: int
    used_sample_count: int
    observation_ended: bool
    observation_ended_reason: str | None
    human_interaction: str | None
    handoff_reasons: tuple[str, ...]
    recovery_samples: tuple[RecoverySample, ...]
    recovery_profile_revision: str | None
    recovery_profile_content: str | None
    recovery_handled_at: datetime | None
    observation_sessions: tuple[Mapping[str, Any], ...]
    observation_authorization: Mapping[str, Any] | None
    sample_jobs: tuple[Mapping[str, Any], ...]
    handling_audit: tuple[Mapping[str, Any], ...]
    actions: tuple[str, ...]
    permissions: tuple[str, ...]
    replay: SessionReplay | None
    model_requests: tuple[Any, ...] = field(default=())


def permissions_from_grants(
    grants: Mapping[str, frozenset[str] | set[str]], *, human_control: bool
) -> tuple[str, ...]:
    """The permission vocabulary derived from measured table privileges.

    ``read_only`` when the login can read every record it judges by;
    ``unreadable:<table>`` otherwise. ``human_control`` when a human control
    decision was recorded on the incident. Any write beyond the Observer's
    own records is reported truthfully: ``record_rewrite:<table>`` for a
    further write on an observation table, ``investigation_write:<table>``
    for a write on any other product table, ``record_delete:<table>`` for a
    delete or truncate anywhere. Never a constant: an empty measurement is
    refused.
    """
    if not grants:
        raise ValueError("GRANTS_REQUIRED")
    flags: list[str] = []
    for table in sorted(grants):
        privileges = frozenset(grants[table])
        if table in RECORDS_READ and "SELECT" not in privileges:
            flags.append(f"unreadable:{table}")
        if privileges & _DELETES:
            flags.append(f"record_delete:{table}")
        extra = privileges & _WRITES
        if table in OWN_RECORD_WRITES:
            extra = extra - OWN_RECORD_WRITES[table]
            if extra:
                flags.append(f"record_rewrite:{table}")
        elif extra:
            flags.append(f"investigation_write:{table}")
    permissions: list[str] = []
    if not any(flag.startswith("unreadable:") for flag in flags):
        permissions.append("read_only")
    if human_control:
        permissions.append("human_control")
    permissions.extend(dict.fromkeys(flags))
    return tuple(permissions)


def _profile(history: Mapping[str, Any]) -> HealthProfile | None:
    row = history.get("health_profile")
    if row is None:
        return None
    try:
        return HealthProfile.model_validate(json.loads(str(row["content"])))
    except (ValueError, ValidationError, KeyError):
        return None


def _body_sha256(row: Mapping[str, Any], verified: bool | None) -> str | None:
    if not verified:
        return None
    try:
        part = json.loads(bytes(row["raw"]))["query"]
        digest = part.get("body_sha256")
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    return str(digest) if isinstance(digest, str) else None


def _signals(
    stored: Mapping[str, Any],
    replayed: Sequence[ReplayedReading],
) -> dict[str, RecoverySignal]:
    by_name = {item.signal_name: item for item in replayed}
    signals: dict[str, RecoverySignal] = {}
    for row in stored.get("readings") or ():
        name = str(row["signal_name"])
        item = by_name.get(name)
        value = row.get("value")
        count = row.get("sample_count")
        signals[name] = RecoverySignal(
            signal_name=name,
            status=str(row["status"]),
            value=None if value is None else float(value),
            sample_count=None if count is None else int(count),
            query=str(row["query"]),
            source=str(row["source"]),
            window_start=row["window_start"],
            window_end=row["window_end"],
            evaluated_at=row["window_end"],
            evidence_id=f"{stored['sample_id']}:{name}",
            raw_sha256=row.get("raw_sha256"),
            body_sha256=_body_sha256(row, None if item is None else item.raw_verified),
            verdict=None if item is None else item.verdict,
            reason=None if item is None else item.reason,
        )
    return signals


def _samples(
    subject_id: str, history: Mapping[str, Any], replay: SessionReplay
) -> list[RecoverySample]:
    session = history["session"]
    target = dict(session.get("target") or {})
    samples: list[RecoverySample] = []
    for stored, basis in zip(history["samples"], replay.samples, strict=True):
        samples.append(
            RecoverySample(
                subject_id=subject_id,
                sample_id=str(stored["sample_id"]),
                session_id=str(session["session_id"]),
                sequence=int(stored["sequence"]),
                target=target,
                subject_control_generation=int(stored["subject_control_generation"]),
                observation_generation=int(stored["observation_generation"]),
                health_profile_revision=stored.get("health_profile_revision"),
                window_start=stored["window_start"],
                window_end=stored["window_end"],
                disposition=str(stored["disposition"]),
                reason=str(stored["reason"]),
                outcome=str(stored["outcome"]),
                required_signals_present=bool(stored["required_signals_present"]),
                confirms_health=bool(stored["confirms_health"]),
                health_basis=str(stored["health_basis"]),
                transition=stored.get("transition"),
                signals=_signals(stored, basis.readings),
            )
        )
    return samples


def _signal_reasons(
    history: Mapping[str, Any], replay: SessionReplay
) -> tuple[list[str], str | None]:
    """Reasons from the per-signal verdicts the replay recomputed for the
    latest adopted sample, and that sample's recompute status."""
    profile = _profile(history)
    latest = None
    for basis in replay.samples:
        if basis.decision.replayed[0] == "adopted":
            latest = basis
    if latest is None:
        return [], None
    reasons: list[str] = []
    if latest.recompute_skipped in (
        "HEALTH_PROFILE_UNAVAILABLE",
        "HEALTH_PROFILE_INVALID",
    ):
        reasons.extend(["REQUIRED_TELEMETRY_MISSING", latest.recompute_skipped])
        return reasons, latest.recompute_skipped
    required = (
        {signal.name for signal in profile.signals if signal.required}
        if profile is not None
        else set()
    )
    seen = {reading.signal_name for reading in latest.readings}
    missing: list[str] = []
    for name in sorted(required - seen - {PROFILE_SENTINEL}):
        missing.append(name)
    for reading in latest.readings:
        verdict = reading.verdict
        if reading.signal_name not in required or verdict is None:
            continue
        if verdict == "below_traffic_gate":
            reasons.append("INSUFFICIENT_TRAFFIC")
        elif verdict == "degraded":
            reasons.append(f"DEGRADED_SIGNAL:{reading.signal_name}")
        elif verdict == "stale":
            missing.append(reading.signal_name)
            reasons.append(f"STALE_TELEMETRY:{reading.signal_name}")
        elif verdict in ("missing", "no_data", "timeout", "failed", "query_mismatch"):
            missing.append(reading.signal_name)
        elif verdict == "insufficient_samples":
            missing.append(reading.signal_name)
    if missing:
        reasons.append("REQUIRED_TELEMETRY_MISSING")
        reasons.extend(f"MISSING_SIGNAL:{name}" for name in dict.fromkeys(missing))
    return reasons, latest.recompute_skipped


def _actions(
    controls: Sequence[Mapping[str, Any]], sessions: Sequence[Mapping[str, Any]]
) -> tuple[str, ...]:
    actions: list[str] = []
    for row in controls:
        action = str(row.get("action"))
        if action == "register_remediation":
            actions.extend(("record_handling", "advance_incident_lifecycle"))
        elif action == "takeover":
            actions.append("human_takeover")
        else:
            actions.append(f"human_control:{action}")
    for history in sessions:
        for stored in history["samples"]:
            for row in stored.get("readings") or ():
                if str(row.get("signal_name")) != PROFILE_SENTINEL:
                    actions.append("read_only_query")
            actions.append("persist_observation")
        for ending in history.get("endings") or ():
            if ending.get("lifecycle_before") != ending.get("lifecycle_after"):
                actions.append("advance_incident_lifecycle")
            if ending.get("ended_reason") in _HANDOFF_ENDINGS:
                actions.append("human_handoff")
    return tuple(actions)


def _session_projection(subject_id: str, row: Mapping[str, Any]) -> dict[str, Any]:
    projected: dict[str, Any] = {
        name: (str(row[name]) if name == "session_id" else row.get(name))
        for name in _SESSION_CONTRACT_FIELDS
    }
    projected["active_sample_job_id"] = (
        None
        if row.get("active_sample_job_id") is None
        else str(row["active_sample_job_id"])
    )
    projected["subject"] = {"kind": "incident", "id": subject_id}
    projected["target"] = dict(row.get("target") or {})
    projected["authorized"] = (
        bool(row.get("authorized")) and row.get("state") == "authorized"
    )
    return projected


def _sample_jobs(sessions: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    jobs: set[tuple[str, str, int]] = set()
    for history in sessions:
        row = history["session"]
        for stored in history["samples"]:
            jobs.add(
                (str(stored["job_id"]), str(row["session_id"]), int(stored["sequence"]))
            )
        if row.get("active_sample_job_id") is not None:
            jobs.add(
                (
                    str(row["active_sample_job_id"]),
                    str(row["session_id"]),
                    int(row["active_sample_sequence"]),
                )
            )
    return tuple(
        {"job_id": job_id, "session_id": session_id, "sequence": sequence}
        for job_id, session_id, sequence in sorted(jobs)
    )


def recovery_outcome(scenario: Any, records: RecoveryRecords) -> RecoveryOutcome:
    """Project one incident's committed recovery records for ``scenario``.

    ``scenario.subject_id`` names the incident the caller read the records
    for; every sample and session in the outcome is bound to it. The latest
    session (by creation order) carries the verdict; earlier sessions are
    history and appear in ``observation_sessions`` and ``recovery_samples``.
    """
    subject_id = str(scenario.subject_id)
    incident = records.incident
    sessions = tuple(records.sessions)
    controls = tuple(dict(row) for row in records.controls)
    latest = sessions[-1] if sessions else None
    replay = None if latest is None else replay_history(latest)
    session_row = None if latest is None else latest["session"]

    samples: list[RecoverySample] = []
    for history in sessions:
        this_replay = replay if history is latest else replay_history(history)
        assert this_replay is not None
        samples.extend(_samples(subject_id, history, this_replay))

    reasons: list[str] = []
    ended = session_row is not None and str(session_row.get("state")) != "authorized"
    ended_reason = None if session_row is None else session_row.get("ended_reason")
    if replay is not None:
        reasons.extend(replay.integrity)
        signal_reasons, _ = _signal_reasons(latest or {}, replay)
        reasons.extend(signal_reasons)
        if replay.recovery_verdict == "degraded":
            reasons.append("CONTINUED_DEGRADATION")
        if ended and not replay.recovery_confirmed:
            reasons.append("OBSERVATION_UNCONFIRMED")
        if (
            session_row is not None
            and session_row.get("health_profile_revision") is None
        ):
            reasons.append("NO_HEALTH_PROFILE")
    recovery_reasons = tuple(dict.fromkeys(reasons))

    handoff = ended and ended_reason in _HANDOFF_ENDINGS
    handoff_reasons: tuple[str, ...] = ()
    if handoff:
        handoff_reasons = tuple(
            dict.fromkeys((str(ended_reason).upper(), *recovery_reasons))
        )

    profile_row = None if latest is None else latest.get("health_profile")
    return RecoveryOutcome(
        scenario_id=str(scenario.scenario_id),
        subject_id=subject_id,
        incident_lifecycle=str(incident["lifecycle"]),
        incident_mode=None if incident.get("mode") is None else str(incident["mode"]),
        target=None if session_row is None else dict(session_row.get("target") or {}),
        recovery_confirmed=replay is not None and replay.recovery_confirmed,
        recovery_verdict="unknown" if replay is None else replay.recovery_verdict,
        recovery_reasons=recovery_reasons,
        latest_sample_verdict=None if replay is None else replay.latest_sample_verdict,
        healthy_window_seconds=0 if replay is None else replay.healthy_window_seconds,
        used_sample_count=0
        if session_row is None
        else int(session_row.get("adopted_count", 0)),
        observation_ended=ended,
        observation_ended_reason=None if ended_reason is None else str(ended_reason),
        human_interaction="handoff" if handoff else None,
        handoff_reasons=handoff_reasons,
        recovery_samples=tuple(samples),
        recovery_profile_revision=(
            None if session_row is None else session_row.get("health_profile_revision")
        ),
        recovery_profile_content=None
        if profile_row is None
        else str(profile_row["content"]),
        recovery_handled_at=None
        if session_row is None
        else session_row.get("authorized_at"),
        observation_sessions=tuple(
            _session_projection(subject_id, history["session"]) for history in sessions
        ),
        observation_authorization=(
            None
            if session_row is None
            else {
                **_session_projection(subject_id, session_row),
                "subject_id": subject_id,
            }
        ),
        sample_jobs=_sample_jobs(sessions),
        handling_audit=controls,
        actions=_actions(controls, sessions),
        permissions=permissions_from_grants(
            records.grants, human_control=bool(controls)
        ),
        replay=replay,
    )
