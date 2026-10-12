"""Append to the per-subject event log inside a caller's transaction.

The log itself (``opspilot.web.events``) is a projection for readers; this is
its one write statement, shared so a business transaction outside the web
package can commit an event together with the fact it announces.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from opspilot.persistence.base import Connection, PersistenceError


def check_event(kind: str, payload: Mapping[str, Any]) -> None:
    if not isinstance(kind, str) or not kind or len(kind) > 64:
        raise ValueError("INVALID_INPUT")
    if not isinstance(payload, Mapping):
        raise ValueError("INVALID_INPUT")


def append_once_in(
    conn: Connection,
    subject_id: UUID,
    kind: str,
    payload: Mapping[str, Any],
    *,
    key: Mapping[str, Any] | None = None,
) -> int:
    """``DurableEventLog.append_once`` inside the caller's transaction: the
    event commits or rolls back with the business write that caused it
    (#184 C9: an automatic observation handed to a human is never recorded
    without its timeline event). Lives below ``opspilot.web`` so the
    observation store can write it without importing the web package."""
    check_event(kind, payload)
    # Same per-subject lock as append(): the existence check and the
    # insert are one critical section, so two announcers of the same
    # fact cannot both pass the check. ``@>`` is jsonb containment,
    # so an empty key matches any payload of the kind.
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s::text, 0))",
        (str(subject_id),),
    )
    wanted = {} if key is None else dict(key)
    if wanted.get("reconciled") is False:
        legacy = dict(wanted)
        legacy.pop("reconciled")
        retained = conn.execute(
            """SELECT MIN(sequence) AS sequence
               FROM opspilot_subject_events
               WHERE subject_id=%s AND kind=%s
                 AND (payload @> %s OR (payload @> %s AND NOT (payload ? 'reconciled')))""",
            (subject_id, kind, Jsonb(wanted), Jsonb(legacy)),
        ).fetchone()
    else:
        retained = conn.execute(
            "SELECT MIN(sequence) AS sequence FROM opspilot_subject_events WHERE subject_id=%s AND kind=%s AND payload @> %s",
            (subject_id, kind, Jsonb(wanted)),
        ).fetchone()
    if retained is not None and retained["sequence"] is not None:
        return int(retained["sequence"])
    row = conn.execute(
        "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM opspilot_subject_events WHERE subject_id=%s",
        (subject_id,),
    ).fetchone()
    if row is None:
        raise PersistenceError("INCONSISTENT_STATE")
    sequence = int(row["next_sequence"])
    conn.execute(
        "INSERT INTO opspilot_subject_events(subject_id,sequence,kind,payload) VALUES(%s,%s,%s,%s)",
        (subject_id, sequence, kind, Jsonb(dict(payload))),
    )
    return sequence
