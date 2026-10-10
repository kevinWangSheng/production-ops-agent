"""External M1-04 seam: committed alert intake records -> ``AlertIntakeOutcome``.

The acceptance entry for the Alertmanager webhook intake (AGENTS.md:
``IncidentScenario -> IncidentOutcome``; contract r3 I8). Like
``opspilot.acceptance_recovery`` it projects what the product committed
and decides nothing again: the incident row, its alert identity (how the
target was resolved, or why it was handed to a human), the Run the intake
opened, and every delivery recorded for the incident in receipt order.
The workbench page renders the same projection (I9).

Field sources (``AlertIntakeOutcome``):

* ``incident_id``: ``scenario.subject_id``, which must be the incident the
  records were read for; a record filed under another incident is refused
  (``SUBJECT_MISMATCH``), never relabelled.
* ``target_id`` / ``affected_service`` / ``handoff_reason``: the identity
  row (``opspilot_alert_identities``) written with the incident.
* ``run_id``: the Run the intake opened (``None`` for a handoff-only
  incident); ``handoff`` holds exactly when there is none.
* ``lifecycle`` / ``run_ids`` / ``observation_session_ids``: the incident
  row, its Runs (by deadline: creation time plus the Run wall) and its
  observation sessions (by ``created_at``). ``run_input``: the committed
  input snapshot of the intake Run, verbatim.
* ``deliveries``: ``opspilot_alert_deliveries`` of the incident by
  ``delivery_id`` (receipt order); ``alert_deliveries_for_identity`` and
  ``alert_delivery_count`` read the table regardless of incident (I11). A resolved notification that found no
  incident (``resolved_recorded``) belongs to no incident and so is in no
  incident's records.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from psycopg.rows import dict_row

__all__ = [
    "AlertDelivery",
    "AlertIntakeOutcome",
    "AlertIntakeRecords",
    "alert_deliveries_for_identity",
    "alert_delivery_count",
    "alert_intake_outcome",
    "alert_intake_records",
]

_DELIVERY_COLUMNS = (
    "delivery_id,delivery_key,fingerprint,starts_at,starts_at_raw,status,outcome,"
    "incident_id,actor,received_at,raw_sha256,annotations_sha256,"
    "annotation_revision,alert_json,truncated"
)


@dataclass(frozen=True)
class AlertIntakeRecords:
    """What the product committed for one incident, read in one snapshot."""

    incident: Mapping[str, Any]
    identity: Mapping[str, Any] | None
    run: Mapping[str, Any] | None
    deliveries: tuple[Mapping[str, Any], ...]
    #: Every Run of the incident (``run_id``, ``incident_id``) in creation order.
    runs: tuple[Mapping[str, Any], ...] = ()
    #: Every observation session (``session_id``, ``incident_id``), oldest first.
    sessions: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class AlertDelivery:
    fingerprint: str
    #: UTC seconds, ``YYYY-MM-DDTHH:MM:SSZ``.
    starts_at: str
    status: str
    outcome: str
    actor: str
    raw_sha256: str
    #: Which annotation revision of the identity this delivery carried, from 1.
    annotation_revision: int
    truncated: bool
    #: Database receipt time, UTC ISO-8601 (``...Z``, microseconds kept).
    received_at: str
    #: ``startsAt`` exactly as the payload carried it.
    original_starts_at: str
    #: The committed redacted, bounded audit text, verbatim (``truncated``
    #: marks a cut at 16 KiB; whole JSON otherwise).
    audit_json: str


@dataclass(frozen=True)
class AlertIntakeOutcome:
    scenario_id: str
    incident_id: str
    target_id: str | None
    run_id: str | None
    handoff: bool
    handoff_reason: str | None
    affected_service: tuple[str, str] | None
    deliveries: tuple[AlertDelivery, ...]
    lifecycle: str
    run_ids: tuple[str, ...]
    observation_session_ids: tuple[str, ...]
    #: The committed input snapshot of the Run the intake opened, verbatim;
    #: ``None`` for a handoff-only incident (or a Run recorded without one).
    run_input: Mapping[str, Any] | None


@contextmanager
def _cursor(conn: Any) -> Iterator[Any]:
    # A psycopg connection, or a store that opens its own snapshot.
    transaction = getattr(conn, "transaction", None)
    if callable(transaction) and not hasattr(conn, "cursor"):
        with transaction(snapshot=True) as opened:
            with opened.cursor(row_factory=dict_row) as cursor:
                yield cursor
        return
    with conn.cursor(row_factory=dict_row) as cursor:
        yield cursor


def alert_intake_records(conn: Any, incident_id: UUID | str) -> AlertIntakeRecords:
    """Read one incident's committed alert intake records.

    ``conn`` is a psycopg connection (the caller owns its transaction; use
    a REPEATABLE READ one for a consistent snapshot) or a ``DurableStore``.
    An unknown incident is ``ValueError("UNKNOWN_INCIDENT")``.
    """
    subject = UUID(str(incident_id))
    with _cursor(conn) as cur:
        incident = cur.execute(
            "SELECT incident_id,intake_key,state,lifecycle,mode,current_run_id,target_id,created_at FROM opspilot_incidents WHERE incident_id=%s",
            (subject,),
        ).fetchone()
        if incident is None:
            raise ValueError("UNKNOWN_INCIDENT")
        identity = cur.execute(
            "SELECT delivery_key,fingerprint,starts_at,starts_at_raw,incident_id,run_id,target_id,namespace,workload,handoff_reason,created_at FROM opspilot_alert_identities WHERE incident_id=%s",
            (subject,),
        ).fetchone()
        run = None
        if identity is not None and identity["run_id"] is not None:
            run = cur.execute(
                "SELECT run_id,incident_id,state,input FROM opspilot_runs WHERE run_id=%s",
                (identity["run_id"],),
            ).fetchone()
        deliveries = cur.execute(
            f"SELECT {_DELIVERY_COLUMNS} FROM opspilot_alert_deliveries WHERE incident_id=%s ORDER BY delivery_id",
            (subject,),
        ).fetchall()
        # Runs carry no creation time; each one's deadline is its creation
        # (database clock) plus the Run wall, so it orders them.
        runs = cur.execute(
            "SELECT run_id,incident_id FROM opspilot_runs WHERE incident_id=%s ORDER BY deadline,run_id",
            (subject,),
        ).fetchall()
        sessions = cur.execute(
            "SELECT session_id,incident_id FROM opspilot_observation_sessions WHERE incident_id=%s ORDER BY created_at,session_id",
            (subject,),
        ).fetchall()
    return AlertIntakeRecords(
        incident=incident,
        identity=identity,
        run=run,
        deliveries=tuple(deliveries),
        runs=tuple(runs),
        sessions=tuple(sessions),
    )


def alert_deliveries_for_identity(
    conn: Any, fingerprint: str, starts_at: str
) -> tuple[AlertDelivery, ...]:
    """Every committed delivery of one identity in receipt order, whichever
    incident (or none) it is filed under (I11). ``starts_at`` is the I3
    normalized form ``YYYY-MM-DDTHH:MM:SSZ``."""
    moment = datetime.fromisoformat(starts_at)
    with _cursor(conn) as cur:
        rows = cur.execute(
            f"SELECT {_DELIVERY_COLUMNS} FROM opspilot_alert_deliveries WHERE fingerprint=%s AND starts_at=%s ORDER BY delivery_id",
            (fingerprint, moment),
        ).fetchall()
    return tuple(_delivery(row) for row in rows)


def alert_delivery_count(conn: Any) -> int:
    """How many delivery records are committed in total (I11)."""
    with _cursor(conn) as cur:
        row = cur.execute(
            "SELECT count(*) AS n FROM opspilot_alert_deliveries"
        ).fetchone()
    return int(row["n"])


def _delivery(row: Mapping[str, Any]) -> AlertDelivery:
    return AlertDelivery(
        fingerprint=row["fingerprint"],
        starts_at=utc_seconds(row["starts_at"]),
        status=row["status"],
        outcome=row["outcome"],
        actor=row["actor"],
        raw_sha256=row["raw_sha256"],
        annotation_revision=int(row["annotation_revision"]),
        truncated=bool(row["truncated"]),
        received_at=row["received_at"]
        .astimezone(UTC)
        .isoformat()
        .replace("+00:00", "Z"),
        original_starts_at=row["starts_at_raw"],
        audit_json=row["alert_json"],
    )


def utc_seconds(moment: datetime) -> str:
    return (
        moment.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )


def alert_intake_outcome(
    scenario: Any, records: AlertIntakeRecords
) -> AlertIntakeOutcome:
    """Project the records; ``SUBJECT_MISMATCH`` on any foreign record."""
    subject = str(records.incident["incident_id"])
    if str(scenario.subject_id) != subject:
        raise ValueError("SUBJECT_MISMATCH")
    identity = records.identity
    if identity is None:
        raise ValueError("NOT_ALERT_INTAKE")
    if str(identity["incident_id"]) != subject:
        raise ValueError("SUBJECT_MISMATCH")
    run = records.run
    if identity["run_id"] is not None and (
        run is None
        or str(run["run_id"]) != str(identity["run_id"])
        or str(run["incident_id"]) != subject
    ):
        raise ValueError("SUBJECT_MISMATCH")
    deliveries = []
    for row in records.deliveries:
        if str(row["incident_id"]) != subject:
            raise ValueError("SUBJECT_MISMATCH")
        deliveries.append(_delivery(row))
    for row in (*records.runs, *records.sessions):
        if str(row["incident_id"]) != subject:
            raise ValueError("SUBJECT_MISMATCH")
    namespace, workload = identity["namespace"], identity["workload"]
    return AlertIntakeOutcome(
        scenario_id=scenario.scenario_id,
        incident_id=subject,
        target_id=identity["target_id"],
        run_id=None if identity["run_id"] is None else str(identity["run_id"]),
        handoff=identity["run_id"] is None,
        handoff_reason=identity["handoff_reason"],
        affected_service=(
            None if namespace is None or workload is None else (namespace, workload)
        ),
        deliveries=tuple(deliveries),
        lifecycle=str(records.incident["lifecycle"]),
        run_ids=tuple(str(row["run_id"]) for row in records.runs),
        observation_session_ids=tuple(
            str(row["session_id"]) for row in records.sessions
        ),
        run_input=None if run is None or run["input"] is None else run["input"],
    )
