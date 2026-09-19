"""Persistent per-subject event sequence behind the SSE stream (C3 section 9).

Every progress or control fact the workbench shows is appended here with a
monotonically increasing ``sequence`` per subject. The browser resumes from
its last sequence; a cursor older than the retained floor gets a reload
instruction instead of a silently incomplete stream.

The log is a projection for readers. It never carries authority: the
business rows in ``DurableStore`` decide state, generation and conclusion.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from psycopg.types.json import Jsonb

from opspilot.persistence import DurableStore, PersistenceError

MAX_EVENT_PAYLOAD_BYTES = 64 * 1024


class CursorTooOld(Exception):
    """The requested cursor precedes the retained floor; reload the snapshot."""


@dataclass(frozen=True)
class SubjectEvent:
    subject_id: UUID
    sequence: int
    kind: str
    payload: Mapping[str, Any]
    recorded_at: datetime | None


class EventLog(Protocol):
    def append(
        self, subject_id: UUID, kind: str, payload: Mapping[str, Any]
    ) -> int: ...

    def append_once(
        self, subject_id: UUID, kind: str, payload: Mapping[str, Any]
    ) -> int:
        """Append unless an event of ``kind`` is already retained for the subject.

        The check and the insert happen under the same per-subject append
        lock, so concurrent callers cannot both append. Returns the retained
        or the new sequence. Retention may prune the earlier event, so callers
        that need exactly-once across pruning also keep a ledger marker.
        """
        ...

    def read_after(
        self, subject_id: UUID, cursor: int, *, limit: int = 100
    ) -> tuple[SubjectEvent, ...]: ...

    def latest(self, subject_id: UUID) -> int: ...

    def floor(self, subject_id: UUID) -> int:
        """Oldest retained sequence, or 1 when nothing was ever pruned."""
        ...


def _check(kind: str, payload: Mapping[str, Any]) -> None:
    if not isinstance(kind, str) or not kind or len(kind) > 64:
        raise ValueError("INVALID_INPUT")
    if not isinstance(payload, Mapping):
        raise ValueError("INVALID_INPUT")


def _check_cursor(cursor: int) -> None:
    if type(cursor) is not int or cursor < 0:
        raise ValueError("INVALID_INPUT")


class MemoryEventLog:
    """Process-local log with the same cursor semantics as the durable one."""

    def __init__(self) -> None:
        self._events: dict[UUID, list[SubjectEvent]] = {}
        self._floor: dict[UUID, int] = {}

    def append(self, subject_id: UUID, kind: str, payload: Mapping[str, Any]) -> int:
        _check(kind, payload)
        events = self._events.setdefault(subject_id, [])
        sequence = (events[-1].sequence if events else 0) + 1
        events.append(SubjectEvent(subject_id, sequence, kind, dict(payload), None))
        return sequence

    def append_once(
        self, subject_id: UUID, kind: str, payload: Mapping[str, Any]
    ) -> int:
        _check(kind, payload)
        for event in self._events.get(subject_id, ()):
            if event.kind == kind:
                return event.sequence
        return self.append(subject_id, kind, payload)

    def prune_before(self, subject_id: UUID, sequence: int) -> None:
        """Drop retained events older than ``sequence`` (tests and demos)."""
        events = self._events.get(subject_id, [])
        self._events[subject_id] = [e for e in events if e.sequence >= sequence]
        self._floor[subject_id] = max(self._floor.get(subject_id, 1), sequence)

    def read_after(
        self, subject_id: UUID, cursor: int, *, limit: int = 100
    ) -> tuple[SubjectEvent, ...]:
        _check_cursor(cursor)
        if cursor < self._floor.get(subject_id, 1) - 1:
            raise CursorTooOld()
        events = self._events.get(subject_id, [])
        return tuple(e for e in events if e.sequence > cursor)[:limit]

    def latest(self, subject_id: UUID) -> int:
        events = self._events.get(subject_id, [])
        newest = events[-1].sequence if events else 0
        return max(newest, self._floor.get(subject_id, 1) - 1)

    def floor(self, subject_id: UUID) -> int:
        return self._floor.get(subject_id, 1)


class DurableEventLog:
    """PostgreSQL-backed log. One table, keyed by (subject_id, sequence)."""

    def __init__(self, store: DurableStore) -> None:
        self._store = store

    def install(self) -> None:
        with self._store.transaction() as conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS opspilot_subject_events (
              subject_id uuid NOT NULL, sequence bigint NOT NULL, kind text NOT NULL,
              payload jsonb NOT NULL, recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
              PRIMARY KEY (subject_id, sequence)
            );
            """)

    def append(self, subject_id: UUID, kind: str, payload: Mapping[str, Any]) -> int:
        _check(kind, payload)
        with self._store.transaction() as conn:
            # Serialize appenders per subject without touching the business
            # rows: the incident row lock belongs to the write paths that
            # decide state, and this projection must never queue behind them.
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s::text, 0))",
                (str(subject_id),),
            )
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

    def append_once(
        self, subject_id: UUID, kind: str, payload: Mapping[str, Any]
    ) -> int:
        _check(kind, payload)
        with self._store.transaction() as conn:
            # Same per-subject lock as append(): the existence check and the
            # insert are one critical section, so two announcers of the same
            # fact cannot both pass the check.
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s::text, 0))",
                (str(subject_id),),
            )
            retained = conn.execute(
                "SELECT MIN(sequence) AS sequence FROM opspilot_subject_events WHERE subject_id=%s AND kind=%s",
                (subject_id, kind),
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

    def read_after(
        self, subject_id: UUID, cursor: int, *, limit: int = 100
    ) -> tuple[SubjectEvent, ...]:
        _check_cursor(cursor)
        with self._store.transaction(snapshot=True) as conn:
            floor = conn.execute(
                "SELECT MIN(sequence) AS floor FROM opspilot_subject_events WHERE subject_id=%s",
                (subject_id,),
            ).fetchone()
            oldest = floor["floor"] if floor and floor["floor"] is not None else None
            if oldest is not None and cursor < int(oldest) - 1:
                raise CursorTooOld()
            rows = conn.execute(
                "SELECT subject_id,sequence,kind,payload,recorded_at FROM opspilot_subject_events WHERE subject_id=%s AND sequence>%s ORDER BY sequence LIMIT %s",
                (subject_id, cursor, limit),
            ).fetchall()
        return tuple(
            SubjectEvent(
                row["subject_id"],
                int(row["sequence"]),
                row["kind"],
                row["payload"],
                row["recorded_at"],
            )
            for row in rows
        )

    def latest(self, subject_id: UUID) -> int:
        with self._store.transaction(snapshot=True) as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS latest FROM opspilot_subject_events WHERE subject_id=%s",
                (subject_id,),
            ).fetchone()
        return int(row["latest"]) if row else 0

    def floor(self, subject_id: UUID) -> int:
        with self._store.transaction(snapshot=True) as conn:
            row = conn.execute(
                "SELECT MIN(sequence) AS floor FROM opspilot_subject_events WHERE subject_id=%s",
                (subject_id,),
            ).fetchone()
        if row is None or row["floor"] is None:
            return 1
        return int(row["floor"])
