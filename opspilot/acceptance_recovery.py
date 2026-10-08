"""External F6 seam: committed recovery records -> ``RecoveryOutcome`` (#140).

The acceptance entry for recovery observation (AGENTS.md: ``IncidentScenario
-> IncidentOutcome``). Like ``opspilot.acceptance`` for the investigation
Run, this module has no transport, no model and no SQL of its own: it
projects what the product committed -- the incident row, every observation
session with its samples, readings, raw bundles and ending records, the
human control audit -- and what the offline replay (``opspilot.observer
.replay``) recomputes from those rows. The verdict, the reasons, the
handoff and the healthy window are the replay's (one logic for the online
verdict and the replay); nothing here judges telemetry again or assembles
reasons of its own, and a record the replay cannot reproduce comes out as
``unknown`` with ``STORED_OBSERVATION_INTEGRITY_MISMATCH`` among its reasons.

Field sources (``RecoveryOutcome``):

* ``subject_id``: ``scenario.subject_id``, which must be the incident id
  the records were read for; a record of another incident -- or a sample,
  reading, ending record or control row filed under another session or
  incident than the layer above it -- is refused (``SUBJECT_MISMATCH``),
  never relabelled.
* ``recorded_lifecycle`` / ``incident_mode``: the committed incident row.
  ``incident_lifecycle`` is the lifecycle the replay can vouch for: the
  recorded one when the stored basis reproduces, ``unverified`` when the
  replay reports an integrity mismatch (a recorded ``resolved`` the rows no
  longer support is not repeated; F6 step 5 scenario contract).
* ``target``: the session's immutable target binding (``opspilot_observation
  _sessions.target``), never telemetry content (PRODUCT-CONSTRAINTS).
* ``recovery_verdict`` / ``recovery_confirmed`` / ``latest_sample_verdict`` /
  ``healthy_window_seconds`` / ``recovery_reasons`` / ``observation_ended``
  / ``observation_ended_reason`` / ``human_interaction`` /
  ``handoff_reasons``: ``SessionReplay`` of the latest session, copied
  (``human_interaction`` is ``"handoff"`` when the replay's ``handoff``
  holds, else ``None``).
* ``used_sample_count``: the session row's ``adopted_count`` (the replay
  checks it against the fold).
* ``recovery_samples``: every sample row with its reading rows (value,
  status, point count, query, source, window, the bundle's sha256, the
  sha256 of the response body inside it, and the instants the bundle
  recorded: ``evaluated_at`` the queries were evaluated at, ``sample_time``
  the verdict judged freshness against, ``observed_at`` the newest raw
  sample behind the signal as the freshness query answered -- ``None``
  when the bundle does not verify or the answer carried none) and the
  replay's per-signal verdict; ``evidence_id`` is ``<sample_id>:<signal_name>``.
* ``recovery_profile_revision`` / ``recovery_profile_content``: the frozen
  profile row behind the session's revision; ``recovery_handled_at``: the
  session's ``authorized_at``.
* ``actions``: the audit of what the product did, merged in event time
  (control rows by ``created_at``, a sample's queries by the bundle's
  ``evaluated_at`` and its persistence by ``submitted_at``, ending
  records by ``recorded_at``; a row without a timestamp keeps its record
  position). Control rows: ``register_remediation`` -> ``record_handling``,
  plus ``advance_incident_lifecycle`` only when the lifecycle did move --
  the k-th registration authorizes the k-th session; the incident was
  ``open`` before the first one, and before a later one it was whatever the
  previous session's last ending record left it at (``lifecycle_after``);
  without such a record the move is unknown and not claimed. ``takeover``
  -> ``human_takeover``; any other control -> ``human_control:<action>``.
  Per sample: ``read_only_query`` once per instant query a reading's bundle
  shows as actually sent (a part recorded with detail ``LEASE_BUDGET`` was
  never sent; the profile sentinel sends nothing; a bundle that does not
  verify counts nothing), then ``persist_observation``. Per ending record:
  ``advance_incident_lifecycle`` when its lifecycle moved and
  ``human_handoff`` for a deadline or budget ending.
* ``permissions``: derived from the privileges the Observer login actually
  holds (``ObservationStore.table_privileges``), see
  ``permissions_from_grants``; ``human_control`` when a control row exists.
* ``model_requests``: empty -- the recovery path has no model client
  (``opspilot.observer`` imports none, checked by ``tests/test_m1_observer
  .py``); the acceptance harness counts calls on its own stub.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from opspilot.observation.store import RECORD_COLUMNS
from opspilot.observer.prometheus import _single_value, epoch_to_datetime
from opspilot.observer.replay import (
    ReplayedReading,
    SessionReplay,
    replay_history,
)
from opspilot.observer.sampler import PROFILE_SENTINEL

__all__ = [
    "OBSERVER_GRANTS",
    "RecoveryOutcome",
    "RecoveryRecords",
    "RecoverySample",
    "RecoverySignal",
    "permissions_from_grants",
    "recovery_outcome",
]

Columns = tuple[str, ...]
Grant = str | Columns  # "*" for the whole relation, else the granted columns

#: Exactly what migration 0003 grants the Observer role, table by table and
#: column by column (``tests/test_f6_recovery_outcome.py`` checks this
#: against the migration source). Anything measured beyond it is reported.
OBSERVER_GRANTS: Mapping[str, Mapping[str, Grant]] = {
    "alembic_version": {"SELECT": "*"},
    "opspilot_targets": {"SELECT": "*"},
    "opspilot_scope_controls": {"SELECT": "*"},
    "opspilot_target_suspensions": {"SELECT": "*"},
    "opspilot_incidents": {
        "SELECT": (
            "incident_id",
            "lifecycle",
            "control_generation",
            "observation_generation",
            "target_id",
        ),
        "UPDATE": ("lifecycle",),
    },
    "opspilot_health_profiles": {"SELECT": "*"},
    "opspilot_observation_sessions": {
        "SELECT": "*",
        "UPDATE": (
            "state",
            "ended_reason",
            "adopted_sequence",
            "adopted_window_end",
            "adopted_count",
            "healthy_since",
            "issued_sequence",
            "active_sample_job_id",
            "active_sample_sequence",
            "active_sample_due_at",
            "active_sample_owner",
            "active_sample_epoch",
            "active_sample_lease_until",
            "updated_at",
        ),
    },
    "opspilot_observation_signal_readings": {"SELECT": "*", "INSERT": "*"},
    "opspilot_observation_samples": {
        "SELECT": "*",
        "INSERT": (
            "sample_id",
            "session_id",
            "job_id",
            "sequence",
            "epoch",
            "window_start",
            "window_end",
            "outcome",
            "required_signals_present",
            "subject_control_generation",
            "observation_generation",
            "health_profile_revision",
            "disposition",
            "reason",
            "confirms_health",
            "health_basis",
            "subject_lifecycle",
            "incident_control_generation",
            "incident_observation_generation",
            "scope_suspended",
            "global_generation",
            "target_generation",
            "within_deadline",
            "lease_valid",
            "lease_stamps_match",
            "readings_consistent",
            "transition",
        ),
    },
    "opspilot_observation_endings": {
        "SELECT": "*",
        "INSERT": (
            "ending_id",
            "session_id",
            "incident_id",
            "ended_reason",
            "transition",
            "sample_id",
            "lifecycle_before",
            "lifecycle_after",
        ),
    },
}
_WRITES = ("INSERT", "UPDATE")
_DELETES = ("DELETE", "TRUNCATE")
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
    grants: Mapping[str, Any]


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
    evaluated_at: datetime | None
    sample_time: datetime | None
    observed_at: datetime | None
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
    recorded_lifecycle: str
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


def _extra(measured: Grant | None, allowed: Grant | None) -> Columns | str | None:
    """What ``measured`` grants beyond ``allowed``: ``"*"``, the extra
    columns, or ``None`` when nothing."""
    if measured is None:
        return None
    if allowed == "*":
        return None
    if measured == "*":
        return "*"
    extra = tuple(c for c in measured if allowed is None or c not in allowed)
    return extra or None


def _flag(prefix: str, name: str, extra: Columns | str) -> str:
    if extra == "*":
        return f"{prefix}:{name}"
    return f"{prefix}:{name}({','.join(extra)})"


def _missing_columns(measured: Grant | None, required: Columns) -> Columns:
    if measured == "*":
        return ()
    held = () if measured is None else tuple(measured)
    return tuple(column for column in required if column not in held)


def permissions_from_grants(
    grants: Mapping[str, Any], *, human_control: bool
) -> tuple[str, ...]:
    """The permission vocabulary derived from measured privileges
    (``ObservationStore.table_privileges``) against the exact grants of
    migration 0003 (``OBSERVER_GRANTS``).

    A relation is the product's only in the measurement's ``product_schema``
    (where the connection resolves ``opspilot_incidents``); a same-named
    relation anywhere else is foreign. ``read_only`` when the login can
    SELECT every column the replay reads of every record table
    (``unreadable:<table>(missing columns)`` otherwise); ``human_control``
    when a human control decision was recorded on the incident. Every
    capability beyond the Observer's own-record grants is reported,
    column-exact: ``record_rewrite:<table>(cols)`` for a further
    INSERT/UPDATE on a table the Observer may write,
    ``investigation_write:<table>(cols)`` for one on any other product
    table, ``record_delete:<table>`` for DELETE or TRUNCATE on a product
    table, ``foreign_write:<schema.relation>(cols)`` for a write on any
    relation outside the product (tables, views, materialized views and
    foreign tables alike), ``sequence_write:<schema.name>``,
    ``schema_create:<schema>``, ``database_create`` / ``database_temp`` and
    ``security_definer_execute:<function>``. Never a constant: an empty
    measurement, or one without a product schema, is refused.
    """
    tables = grants.get("tables") if isinstance(grants, Mapping) else None
    product_schema = (
        grants.get("product_schema") if isinstance(grants, Mapping) else None
    )
    if not tables or not isinstance(product_schema, str):
        raise ValueError("GRANTS_REQUIRED")
    flags: list[str] = []
    for qualified in sorted(tables):
        measured = tables[qualified]
        schema, _, name = qualified.rpartition(".")
        product = schema == product_schema and (
            name.startswith("opspilot_") or name == "alembic_version"
        )
        allowed = OBSERVER_GRANTS.get(name, {}) if product else {}
        if product and name in RECORD_COLUMNS:
            missing = _missing_columns(measured.get("SELECT"), RECORD_COLUMNS[name])
            if missing:
                flags.append(_flag("unreadable", name, missing))
        for kind in _WRITES:
            extra = _extra(measured.get(kind), allowed.get(kind))
            if extra is None:
                continue
            if not product:
                flags.append(_flag("foreign_write", qualified, extra))
            elif name in OBSERVER_GRANTS and any(k in allowed for k in _WRITES):
                flags.append(_flag("record_rewrite", name, extra))
            else:
                flags.append(_flag("investigation_write", name, extra))
        for kind in _DELETES:
            if kind in measured:
                flags.append(
                    f"record_delete:{name}" if product else f"foreign_write:{qualified}"
                )
    for qualified, held in sorted((grants.get("sequences") or {}).items()):
        if "UPDATE" in held or "USAGE" in held:
            flags.append(f"sequence_write:{qualified}")
    for schema, held in sorted((grants.get("schemas") or {}).items()):
        if "CREATE" in held:
            flags.append(f"schema_create:{schema}")
    database = grants.get("database") or ()
    if "CREATE" in database:
        flags.append("database_create")
    if "TEMP" in database:
        flags.append("database_temp")
    for function in sorted(grants.get("functions") or ()):
        flags.append(f"security_definer_execute:{function}")
    permissions: list[str] = []
    if not any(flag.startswith("unreadable:") for flag in flags):
        permissions.append("read_only")
    if human_control:
        permissions.append("human_control")
    permissions.extend(dict.fromkeys(flags))
    return tuple(permissions)


def _bundle(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """The reading's raw bundle, only when it still hashes to its row."""
    raw = row.get("raw")
    if raw is None or hashlib.sha256(bytes(raw)).hexdigest() != row.get("raw_sha256"):
        return None
    try:
        parsed = json.loads(bytes(raw))
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _instant(bundle: Mapping[str, Any] | None, key: str) -> datetime | None:
    if bundle is None or not isinstance(bundle.get(key), str):
        return None
    try:
        at = datetime.fromisoformat(str(bundle[key]))
    except ValueError:
        return None
    # fail closed: an instant without an offset cannot be ordered against
    # the aware record times, so it is no instant at all
    return at if at.tzinfo is not None else None


def _observed_at(bundle: Mapping[str, Any] | None) -> datetime | None:
    """The newest raw sample time behind the signal, as the bundle's
    freshness answer (exact response bytes) says; ``None`` when there is no
    usable answer."""
    if bundle is None:
        return None
    part = bundle.get("freshness")
    if not isinstance(part, Mapping) or not part.get("body_complete", True):
        return None
    try:
        body = base64.b64decode(str(part.get("body_b64", "")), validate=True)
    except (ValueError, TypeError):
        return None
    if hashlib.sha256(body).hexdigest() != part.get("body_sha256"):
        return None
    status, value, _ = _single_value(body)
    if status != "ok" or value is None:
        return None
    try:
        return epoch_to_datetime(value)
    except (OverflowError, OSError, ValueError):
        return None


def _body_sha256(bundle: Mapping[str, Any] | None) -> str | None:
    if bundle is None:
        return None
    part = bundle.get("query")
    digest = part.get("body_sha256") if isinstance(part, Mapping) else None
    return digest if isinstance(digest, str) else None


def _queries_sent(bundle: Mapping[str, Any] | None) -> int:
    """How many of the reading's three instant queries were actually sent:
    a part the sampler filled in locally because the lease budget was spent
    carries detail ``LEASE_BUDGET`` and was never a request."""
    if bundle is None:
        return 0
    sent = 0
    for kind in ("query", "coverage", "freshness"):
        part = bundle.get(kind)
        if isinstance(part, Mapping) and part.get("detail") != "LEASE_BUDGET":
            sent += 1
    return sent


def _signals(
    stored: Mapping[str, Any], replayed: Sequence[ReplayedReading]
) -> dict[str, RecoverySignal]:
    by_name = {item.signal_name: item for item in replayed}
    signals: dict[str, RecoverySignal] = {}
    for row in stored.get("readings") or ():
        name = str(row["signal_name"])
        item = by_name.get(name)
        bundle = _bundle(row)
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
            evaluated_at=_instant(bundle, "evaluated_at"),
            sample_time=_instant(bundle, "sample_time"),
            observed_at=_observed_at(bundle),
            evidence_id=f"{stored['sample_id']}:{name}",
            raw_sha256=row.get("raw_sha256"),
            body_sha256=_body_sha256(bundle),
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


def _lifecycle_before_registration(
    index: int, sessions: Sequence[Mapping[str, Any]]
) -> str | None:
    """The incident lifecycle before the ``index``-th registration, from the
    records: ``open`` before the first session; otherwise what the previous
    session's last ending record left it at, or unknown."""
    if index == 0:
        return "open"
    if index > len(sessions):
        return None
    endings = sessions[index - 1].get("endings") or ()
    if not endings:
        return None
    after = endings[-1].get("lifecycle_after")
    return None if after is None else str(after)


def _actions(
    controls: Sequence[Mapping[str, Any]], sessions: Sequence[Mapping[str, Any]]
) -> tuple[str, ...]:
    """The product's actions merged in event time (see the module doc)."""
    events: list[tuple[datetime | None, int, list[str]]] = []
    order = 0
    registrations = 0
    for row in controls:
        action = str(row.get("action"))
        done: list[str] = []
        if action == "register_remediation":
            done.append("record_handling")
            before = _lifecycle_before_registration(registrations, sessions)
            registrations += 1
            if before is not None and before != "observing_recovery":
                done.append("advance_incident_lifecycle")
        elif action == "takeover":
            done.append("human_takeover")
        else:
            done.append(f"human_control:{action}")
        events.append((row.get("created_at"), order, done))
        order += 1
    for history in sessions:
        for stored in history["samples"]:
            queries: list[str] = []
            evaluated: list[datetime] = []
            for row in stored.get("readings") or ():
                if str(row.get("signal_name")) == PROFILE_SENTINEL:
                    continue
                bundle = _bundle(row)
                queries.extend(["read_only_query"] * _queries_sent(bundle))
                at = _instant(bundle, "evaluated_at")
                if at is not None:
                    evaluated.append(at)
            if queries:
                # the queries ran when the bundle says; ``submitted_at`` is
                # only when the sample was persisted (a takeover can fall
                # between the two)
                events.append(
                    (min(evaluated, default=stored.get("submitted_at")), order, queries)
                )
                order += 1
            events.append((stored.get("submitted_at"), order, ["persist_observation"]))
            order += 1
        for ending in history.get("endings") or ():
            done = []
            if ending.get("lifecycle_before") != ending.get("lifecycle_after"):
                done.append("advance_incident_lifecycle")
            if ending.get("ended_reason") in _HANDOFF_ENDINGS:
                done.append("human_handoff")
            events.append((ending.get("recorded_at"), order, done))
            order += 1
    # event time first; a row without one keeps its position relative to
    # the previous timestamped row (stable sort on the record position)
    last: datetime | None = None
    keyed: list[tuple[datetime | None, int, list[str]]] = []
    for at, position, done in events:
        if at is not None:
            last = at
        keyed.append((last, position, done))
    keyed.sort(key=lambda item: (item[0] is not None, item[0] or datetime.min, item[1]))
    return tuple(action for _, _, done in keyed for action in done)


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


def _require_owned(subject_id: str, history: Mapping[str, Any]) -> None:
    """Every layer of one session history names the incident / session it
    hangs under; a sample or ending filed under another one is refused
    (``SUBJECT_MISMATCH``), never relabelled with the requested subject."""
    session = history["session"]
    session_id = str(session.get("session_id"))
    if str(session.get("incident_id")) != subject_id:
        raise ValueError("SUBJECT_MISMATCH")
    for stored in history["samples"]:
        if str(stored.get("session_id")) != session_id:
            raise ValueError("SUBJECT_MISMATCH")
        for row in stored.get("readings") or ():
            if str(row.get("sample_id")) != str(stored.get("sample_id")):
                raise ValueError("SUBJECT_MISMATCH")
    own_samples = {str(stored.get("sample_id")) for stored in history["samples"]}
    for ending in history.get("endings") or ():
        if (
            str(ending.get("session_id")) != session_id
            or str(ending.get("incident_id")) != subject_id
            or (
                ending.get("sample_id") is not None
                and str(ending["sample_id"]) not in own_samples
            )
        ):
            raise ValueError("SUBJECT_MISMATCH")


def recovery_outcome(scenario: Any, records: RecoveryRecords) -> RecoveryOutcome:
    """Project one incident's committed recovery records for ``scenario``.

    ``scenario.subject_id`` is the incident id; the incident row and every
    session must carry it (``SUBJECT_MISMATCH`` otherwise). The latest
    session (by creation order) carries the verdict; earlier sessions are
    history and appear in ``observation_sessions`` and ``recovery_samples``.
    """
    subject_id = str(scenario.subject_id)
    incident = records.incident
    if str(incident.get("incident_id")) != subject_id:
        raise ValueError("SUBJECT_MISMATCH")
    sessions = tuple(records.sessions)
    for history in sessions:
        _require_owned(subject_id, history)
    controls = tuple(dict(row) for row in records.controls)
    for row in controls:
        if str(row.get("incident_id")) != subject_id:
            raise ValueError("SUBJECT_MISMATCH")
    latest = sessions[-1] if sessions else None
    replay = None if latest is None else replay_history(latest)
    session_row = None if latest is None else latest["session"]

    samples: list[RecoverySample] = []
    for history in sessions:
        this_replay = replay if history is latest else replay_history(history)
        assert this_replay is not None
        samples.extend(_samples(subject_id, history, this_replay))

    recorded_lifecycle = str(incident["lifecycle"])
    vouched = replay is None or replay.consistent
    profile_row = None if latest is None else latest.get("health_profile")
    return RecoveryOutcome(
        scenario_id=str(scenario.scenario_id),
        subject_id=subject_id,
        incident_lifecycle=recorded_lifecycle if vouched else "unverified",
        recorded_lifecycle=recorded_lifecycle,
        incident_mode=None if incident.get("mode") is None else str(incident["mode"]),
        target=None if session_row is None else dict(session_row.get("target") or {}),
        recovery_confirmed=replay is not None and replay.recovery_confirmed,
        recovery_verdict="unknown" if replay is None else replay.recovery_verdict,
        recovery_reasons=() if replay is None else replay.recovery_reasons,
        latest_sample_verdict=None if replay is None else replay.latest_sample_verdict,
        healthy_window_seconds=0 if replay is None else replay.healthy_window_seconds,
        used_sample_count=(
            0 if session_row is None else int(session_row.get("adopted_count", 0))
        ),
        observation_ended=replay is not None and replay.observation_ended,
        observation_ended_reason=None if replay is None else replay.ended_reason,
        human_interaction="handoff" if replay is not None and replay.handoff else None,
        handoff_reasons=() if replay is None else replay.handoff_reasons,
        recovery_samples=tuple(samples),
        recovery_profile_revision=(
            None if session_row is None else session_row.get("health_profile_revision")
        ),
        recovery_profile_content=(
            None if profile_row is None else str(profile_row["content"])
        ),
        recovery_handled_at=(
            None if session_row is None else session_row.get("authorized_at")
        ),
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
