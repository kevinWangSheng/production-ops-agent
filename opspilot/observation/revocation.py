"""Ending observation sessions inside a caller's transaction (C3 section 10).

"暂停、接管、取消当前调查 ... 时，在同一事务中增加相关版本并撤销旧观察任务":
the human-control paths in ``opspilot.persistence`` (``control()``,
``new_run()``) and the Controller-side authorization in
``opspilot.observation.store`` all end sessions through these two
functions, so one definition writes the session state and the append-only
ending record the lifecycle evidence trigger of migration 0003 reads.

This module depends on ``opspilot.persistence.base`` and the domain state
machine only; ``opspilot.observation.store`` builds on it, and
``opspilot.persistence.controls`` imports it without a cycle.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID, uuid4

from opspilot.domain.observation import OBSERVATION_SESSION
from opspilot.persistence.base import Connection, PersistenceError, _StoreBase

# How each ending reason moves the session state machine.
_SESSION_TRIGGER = {
    "recovery_confirmed": "observation_completed",
    "deadline_expired": "deadline_expired",
    "max_samples_exhausted": "deadline_expired",
    "authority_revoked": "authority_revoked",
    "binding_stale": "authority_revoked",
    "scope_suspended": "authority_revoked",
}


def end_session(
    conn: Connection,
    session_id: UUID,
    incident_id: UUID,
    *,
    ended_reason: str,
    transition: str | None,
    lifecycle_before: str,
    lifecycle_after: str,
    sample_id: UUID | None = None,
    watermark: dict[str, Any] | None = None,
) -> None:
    """Close a session: state by the session state machine, job slot cleared,
    and the ending recorded (the row the lifecycle evidence trigger looks for
    when the Observer changes the incident). The caller holds the incident
    row lock and the session row lock, in that order."""
    state = OBSERVATION_SESSION.fire("authorized", _SESSION_TRIGGER[ended_reason])
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
        "INSERT INTO opspilot_observation_endings(ending_id,session_id,incident_id,ended_reason,transition,sample_id,lifecycle_before,lifecycle_after) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            uuid4(),
            session_id,
            incident_id,
            ended_reason,
            transition,
            sample_id,
            lifecycle_before,
            lifecycle_after,
        ),
    )


def revoke_authorized_sessions(conn: Connection, incident_id: UUID) -> list[UUID]:
    """Withdraw every authorized session of an incident (``authority_revoked``).

    The caller holds the incident row lock (incident first, then sessions:
    the lock order every ending path keeps). The lifecycle is the caller's
    decision, not this one's; the ending record carries it unchanged.
    """
    if not isinstance(incident_id, UUID):
        raise PersistenceError("INVALID_INPUT")
    rows = conn.execute(
        "SELECT session_id FROM opspilot_observation_sessions WHERE incident_id=%s AND state='authorized' ORDER BY session_id FOR UPDATE",
        (incident_id,),
    ).fetchall()
    if not rows:
        return []
    lifecycle = str(
        _StoreBase._require_row(
            conn.execute(
                "SELECT lifecycle FROM opspilot_incidents WHERE incident_id=%s",
                (incident_id,),
            )
        )["lifecycle"]
    )
    revoked: list[UUID] = []
    for row in rows:
        end_session(
            conn,
            row["session_id"],
            incident_id,
            ended_reason="authority_revoked",
            transition=None,
            lifecycle_before=lifecycle,
            lifecycle_after=lifecycle,
        )
        revoked.append(cast(UUID, row["session_id"]))
    return revoked
