"""The product-mode trace outbox, write side (#243 PR-1, #427; C3 section 11).

Every model step, tool result and postmortem generation attempt adds one row
to ``opspilot_trace_outbox`` in the transaction that commits it; Run state
changes add theirs through the 0024 trigger. Nothing here sends anything.
The worker's outbox thread (``OutboxMaintainer``) deletes rows 7 days old
(Q16, OB-R8) and, when ``OPSPILOT_TRACE=outbox`` passes its preconditions,
runs ``opspilot.outbox_delivery.OutboxDelivery`` (#428), which claims,
settles and trims rows through ``OutboxStore`` below.

The payload is never a copy of a stored record. ``build_payload`` takes
named fields and keeps only the Q14 allowlist (``PAYLOAD_KEYS``): ids,
versions, numbers and fixed codes. Each value is checked against the shape
its key admits, and anything else becomes ``UNCLASSIFIED`` (codes, names)
or is left out (ids, numbers, times), so no prompt, reply, tool view,
query, target or service name, report text, ``reasoning_content`` or
credential can reach a row. String values are bounded (64 characters), so
a payload always fits the table's 4096-byte CHECK and an outbox INSERT
cannot fail the business transaction it rides in (OB-R6).
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from psycopg.types.json import Jsonb

from opspilot import __version__
from opspilot.persistence.base import Connection, PersistenceError, _StoreBase

if TYPE_CHECKING:
    from opspilot.health import OutboxStats
    from opspilot.outbox_delivery import OutboxDelivery

__all__ = [
    "KINDS",
    "PAYLOAD_KEYS",
    "PURGE_INTERVAL_SECONDS",
    "RETENTION",
    "THREAD_NAME",
    "ClaimedRow",
    "OutboxMaintainer",
    "OutboxStore",
    "build_payload",
    "image_fields",
    "insert_entry",
    "key_part",
    "purge_expired",
]

_log = logging.getLogger(__name__)

#: Q14 allowlist (contract r3, OB-Q1): the only keys a payload may hold.
PAYLOAD_KEYS: frozenset[str] = frozenset(
    {
        "incident_id",
        "run_id",
        "step_id",
        "attempt_id",
        "image_version",
        "image_revision",
        "prompt_revision",
        "tool_schema_revision",
        "context_policy_revision",
        "model",
        "span_kind",
        "started_at",
        "ended_at",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "reasoning_tokens",
        "finish_reason",
        "run_state",
        "handoff_reason",
        "tool_status_class",
        "tool_reason",
        "tool_name",
        "source_contact",
        "budget_seconds",
    }
)

KINDS: frozenset[str] = frozenset(
    {"run_state", "model_step", "tool_result", "generation"}
)
RETENTION = timedelta(days=7)
THREAD_NAME = "opspilot-outbox"
PURGE_INTERVAL_SECONDS = 600.0

UNCLASSIFIED = "UNCLASSIFIED"
# Fixed upper-case codes (tracing.py ``_CODE``, contract r3 code shape).
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
# Provider finish reasons are lower case (stop / length / tool_calls).
_FINISH = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
#: A version, revision or model name; the 0024 trigger holds a frozen copy.
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# A registry tool name.
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
# Closed vocabularies: opspilot_runs.state (0002 CHECK), ToolStatus and
# SourceContact (opspilot.tools.outcomes).
_RUN_STATES = frozenset(
    {
        "queued",
        "running",
        "waiting_human",
        "paused",
        "blocked",
        "completed",
        "failed",
        "cancelled",
        "budget_exhausted",
    }
)
_TOOL_STATUSES = frozenset({"ok", "no_data", "error", "timeout", "denied"})
_SOURCE_CONTACTS = frozenset({"none", "possible", "confirmed"})

_IDS = ("incident_id", "run_id", "step_id", "attempt_id")
_IDENTIFIERS = (
    "image_version",
    "image_revision",
    "prompt_revision",
    "tool_schema_revision",
    "context_policy_revision",
    "model",
)
_TIMES = ("started_at", "ended_at")
_NUMBERS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "reasoning_tokens",
    "budget_seconds",
)
_CODES = ("handoff_reason", "tool_reason")
_SPAN_KINDS = KINDS


def _closed(value: object, allowed: frozenset[str]) -> str:
    return value if isinstance(value, str) and value in allowed else UNCLASSIFIED


def _shaped(value: object, shape: re.Pattern[str]) -> str:
    return value if isinstance(value, str) and shape.fullmatch(value) else UNCLASSIFIED


def _id(value: object) -> str | None:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str):
        try:
            return str(UUID(value))
        except ValueError:
            return None
    return None


def _time(value: object) -> str | None:
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(UTC).isoformat()
    return None


# Largest count kept: keeps every number to a few JSON digits, so a huge
# provider integer cannot push the payload past the 4096-byte CHECK.
_MAX_NUMBER = 2**53


def _number(value: object) -> int | float | None:
    """A finite number of bounded size, else ``None`` (left out). NaN and
    infinities would serialize to tokens PostgreSQL jsonb rejects and roll
    back the business transaction (OB-R6); a count that is not finite
    carries nothing worth a fixed code, so it is omitted like a wrong type."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if abs(value) > _MAX_NUMBER:
        return None
    return value


_KEY_PART = re.compile(r"^[A-Za-z0-9_.#-]{1,64}$")
_MAX_KEY_BYTES = 256  # the 0024 CHECK on idempotency_key


def key_part(value: str) -> str:
    """A provider-supplied key component (a tool call id) as the key holds
    it: unchanged when short and plain, else ``sha256-`` and 32 hex digits
    of its hash. Deterministic, so a repeat of the same subject still
    collides; never longer than 64 characters, so no provider id can fail
    the key CHECK and with it the business transaction (OB-R6)."""
    if _KEY_PART.fullmatch(value):
        return value
    return (
        "sha256-"
        + hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()[:32]
    )


def build_payload(fields: Mapping[str, Any]) -> dict[str, Any]:
    """The Q14 payload of one outbox row, from named fields only.

    Keys outside ``PAYLOAD_KEYS`` and ``None`` values are dropped. ids must
    be UUIDs, times timezone-aware datetimes, ``*_tokens`` and
    ``budget_seconds`` numbers (kept as they are); a value of the wrong type
    is dropped. Codes (``handoff_reason``, ``tool_reason``) must match
    ``^[A-Z][A-Z0-9_]{1,63}$``, ``finish_reason`` the provider's lower-case
    shape, versions / revisions / the model name ``IDENTIFIER``, the tool
    name a registry name; ``run_state``, ``tool_status_class``,
    ``source_contact`` and ``span_kind`` their closed vocabularies. Anything
    else among those becomes ``UNCLASSIFIED``. Pure: no I/O, no clock.
    """
    payload: dict[str, Any] = {}
    for key, value in fields.items():
        if key not in PAYLOAD_KEYS or value is None:
            continue
        kept: Any
        if key in _IDS:
            kept = _id(value)
        elif key in _TIMES:
            kept = _time(value)
        elif key in _NUMBERS:
            kept = _number(value)
        elif key in _IDENTIFIERS:
            kept = _shaped(value, IDENTIFIER)
        elif key in _CODES:
            kept = _shaped(value, _CODE)
        elif key == "finish_reason":
            kept = _shaped(value, _FINISH)
        elif key == "tool_name":
            # a non-registry name is left out, not classified (seam r2)
            kept = (
                value
                if isinstance(value, str) and _TOOL_NAME.fullmatch(value)
                else None
            )
        elif key == "run_state":
            kept = _closed(value, _RUN_STATES)
        elif key == "tool_status_class":
            kept = _closed(value, _TOOL_STATUSES)
        elif key == "source_contact":
            kept = _closed(value, _SOURCE_CONTACTS)
        elif key == "span_kind":
            kept = _closed(value, _SPAN_KINDS)
        else:  # pragma: no cover - every PAYLOAD_KEYS entry is handled above
            kept = None
        if kept is not None:
            payload[key] = kept
    return payload


def image_fields() -> dict[str, str]:
    """This process's image version and revision (``opspilot_build_info``)."""
    return {
        "image_version": __version__,
        "image_revision": os.environ.get("OPSPILOT_REVISION") or "unknown",
    }


def _revisions(versions: object) -> dict[str, Any]:
    if not isinstance(versions, Mapping):
        return {}
    return {
        key: versions.get(key)
        for key in (
            "prompt_revision",
            "tool_schema_revision",
            "context_policy_revision",
        )
    }


def insert_entry(
    conn: Connection,
    *,
    kind: str,
    idempotency_key: str,
    incident_id: UUID,
    run_id: UUID | None,
    fields: Mapping[str, Any],
    versions: object = None,
) -> None:
    """Add one outbox row inside the caller's transaction; a repeat of the
    same ``idempotency_key`` adds nothing (OB-R3). ``fields`` pass through
    ``build_payload`` together with this process's image fields and the
    revisions of ``versions`` (a Run's ``versions``)."""
    if kind not in KINDS:
        raise ValueError(f"unknown outbox kind {kind!r}")
    if len(idempotency_key.encode("utf-8", "surrogatepass")) > _MAX_KEY_BYTES:
        # every caller builds keys from UUIDs, integers and ``key_part``;
        # this only keeps a future caller from failing the business commit
        idempotency_key = (
            f"{kind}:{key_part(idempotency_key)}"  # ":" never passes plain
        )
    payload = build_payload(
        {**image_fields(), **_revisions(versions), "span_kind": kind, **fields}
    )
    conn.execute(
        "INSERT INTO opspilot_trace_outbox(idempotency_key,incident_id,run_id,kind,payload) "
        "VALUES(%s,%s,%s,%s,%s) ON CONFLICT (idempotency_key) DO NOTHING",
        (idempotency_key, incident_id, run_id, kind, Jsonb(payload)),
    )


def purge_expired(
    conn: Connection,
    *,
    now: datetime,
    retention: timedelta = RETENTION,
    export_enabled: bool = False,
) -> int:
    """Delete final rows (``delivered``/``dropped``) whose ``finalized_at``
    is at least ``retention`` old (r3-L4) and, while export is off, ``pending``
    rows whose ``created_at`` is (OB-R8). Returns the number deleted.
    Database only, no network."""
    cutoff = now - retention
    cursor = conn.execute(
        "DELETE FROM opspilot_trace_outbox WHERE "
        "(state IN ('delivered','dropped') AND finalized_at <= %s) "
        "OR (%s AND state = 'pending' AND created_at <= %s)",
        (cutoff, not export_enabled, cutoff),
    )
    return int(cursor.rowcount or 0)


@dataclass(frozen=True)
class ClaimedRow:
    """One pending row claimed for delivery. ``attempts`` already counts
    this attempt; ``run_started_at`` is the earliest ``created_at`` of the
    row's Run still in the table (the Run root span's start)."""

    entry_id: int
    idempotency_key: str
    incident_id: UUID
    run_id: UUID | None
    kind: str
    payload: Any
    attempts: int
    created_at: datetime
    run_started_at: datetime | None


class OutboxStore(_StoreBase):
    """The worker's outbox maintenance and delivery transactions. Every
    ``now`` defaults to the database clock (the clock ``created_at`` is
    written with); a test passes its own."""

    def _now(self, conn: Connection, now: datetime | None) -> datetime:
        return self._db_now(conn) if now is None else now

    def count_pending(self) -> int:
        with self.transaction() as conn:
            row = self._require_row(
                conn.execute(
                    "SELECT count(*) AS n FROM opspilot_trace_outbox WHERE state = 'pending'"
                )
            )
            return int(row["n"])

    def trim_backlog(self, *, limit: int, now: datetime | None = None) -> int:
        """Mark the oldest pending rows beyond ``limit`` ``dropped(BACKLOG)``
        (OB-R5); returns how many. A row another worker holds is skipped."""
        with self.transaction() as conn:
            row = self._require_row(
                conn.execute(
                    "SELECT count(*) AS n FROM opspilot_trace_outbox WHERE state = 'pending'"
                )
            )
            excess = int(row["n"]) - limit
            if excess <= 0:
                return 0
            cursor = conn.execute(
                "UPDATE opspilot_trace_outbox t SET state = 'dropped', "
                "drop_reason = 'BACKLOG', finalized_at = %s "
                "FROM (SELECT entry_id FROM opspilot_trace_outbox WHERE state = 'pending' "
                "ORDER BY created_at, entry_id LIMIT %s FOR UPDATE SKIP LOCKED) old "
                "WHERE t.entry_id = old.entry_id",
                (self._now(conn, now), excess),
            )
            return int(cursor.rowcount or 0)

    def claim(
        self, *, limit: int, lease: timedelta, now: datetime | None = None
    ) -> list[ClaimedRow]:
        """Claim up to ``limit`` due pending rows (``FOR UPDATE SKIP LOCKED``,
        OB-R4): in the same transaction count the attempt and push
        ``next_attempt_at`` ``lease`` ahead, so no other worker claims them
        while this one is sending, and a crash retries them after the lease."""
        with self.transaction() as conn:
            at = self._now(conn, now)
            rows = conn.execute(
                "SELECT entry_id, idempotency_key, incident_id, run_id, kind, payload, "
                "created_at FROM opspilot_trace_outbox WHERE state = 'pending' "
                "AND (next_attempt_at IS NULL OR next_attempt_at <= %s) "
                "ORDER BY entry_id LIMIT %s FOR UPDATE SKIP LOCKED",
                (at, limit),
            ).fetchall()
            if not rows:
                return []
            ids = [row["entry_id"] for row in rows]
            # attempts as counted by this claim (the UPDATE's RETURNING row)
            attempts = {
                updated["entry_id"]: int(updated["attempts"])
                for updated in conn.execute(
                    "UPDATE opspilot_trace_outbox SET attempts = attempts + 1, "
                    "next_attempt_at = %s WHERE entry_id = ANY(%s) "
                    "RETURNING entry_id, attempts",
                    (at + lease, ids),
                ).fetchall()
            }
            run_ids = sorted({row["run_id"] for row in rows if row["run_id"]}, key=str)
            starts: dict[UUID, datetime] = {}
            if run_ids:
                starts = {
                    row["run_id"]: row["started"]
                    for row in conn.execute(
                        "SELECT run_id, min(created_at) AS started FROM opspilot_trace_outbox "
                        "WHERE run_id = ANY(%s) GROUP BY run_id",
                        (run_ids,),
                    ).fetchall()
                }
            return [
                ClaimedRow(
                    entry_id=row["entry_id"],
                    idempotency_key=row["idempotency_key"],
                    incident_id=row["incident_id"],
                    run_id=row["run_id"],
                    kind=row["kind"],
                    payload=row["payload"],
                    attempts=attempts[row["entry_id"]],
                    created_at=row["created_at"],
                    run_started_at=starts.get(row["run_id"]) if row["run_id"] else None,
                )
                for row in rows
            ]

    def mark_delivered(
        self, entry_ids: Sequence[int], *, now: datetime | None = None
    ) -> int:
        if not entry_ids:
            return 0
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE opspilot_trace_outbox SET state = 'delivered', finalized_at = %s "
                "WHERE entry_id = ANY(%s) AND state = 'pending'",
                (self._now(conn, now), list(entry_ids)),
            )
            return int(cursor.rowcount or 0)

    def mark_dropped(
        self, entry_ids: Sequence[int], reason: str, *, now: datetime | None = None
    ) -> int:
        if not entry_ids:
            return 0
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE opspilot_trace_outbox SET state = 'dropped', drop_reason = %s, "
                "finalized_at = %s WHERE entry_id = ANY(%s) AND state = 'pending'",
                (reason, self._now(conn, now), list(entry_ids)),
            )
            return int(cursor.rowcount or 0)

    def schedule_retry(
        self, retries: Mapping[int, timedelta], *, now: datetime | None = None
    ) -> int:
        """Set each row's ``next_attempt_at`` to ``now`` plus its delay."""
        if not retries:
            return 0
        with self.transaction() as conn:
            at = self._now(conn, now)
            ids = list(retries)
            cursor = conn.execute(
                "UPDATE opspilot_trace_outbox t SET next_attempt_at = r.due "
                "FROM unnest(%s::bigint[], %s::timestamptz[]) AS r(entry_id, due) "
                "WHERE t.entry_id = r.entry_id AND t.state = 'pending'",
                (ids, [at + retries[i] for i in ids]),
            )
            return int(cursor.rowcount or 0)

    def purge(
        self, *, retention: timedelta = RETENTION, export_enabled: bool = False
    ) -> int:
        """``purge_expired`` against the database clock (the clock
        ``created_at`` is written with)."""
        with self.transaction() as conn:
            return purge_expired(
                conn,
                now=self._db_now(conn),
                retention=retention,
                export_enabled=export_enabled,
            )


class OutboxMaintainer:
    """The worker's outbox thread (r3-L1). Always the 7-day cleanup; with
    ``delivery`` (``OPSPILOT_TRACE=outbox`` and its preconditions met, #428)
    also delivery every ``delivery_interval`` seconds, back to back while a
    full batch was claimed. Without it -- off, lab, or fail-closed -- pending
    rows are deleted 7 days after ``created_at`` (r3-L5). A storage refusal
    is logged by code and retried at the next interval; any other error is
    logged by type only and retried too, because a dead outbox thread stops
    the whole worker (``worker_main._run``), which would reach the Runs
    (OB-R6)."""

    def __init__(
        self,
        store: OutboxStore,
        *,
        stop: threading.Event,
        interval: float = PURGE_INTERVAL_SECONDS,
        delivery: OutboxDelivery | None = None,
        stats: OutboxStats | None = None,
        delivery_interval: float | None = None,
    ) -> None:
        self.store = store
        self.stop = stop
        self.interval = interval
        self.delivery = delivery
        self.stats = stats
        if delivery_interval is None:
            from opspilot.outbox_delivery import DELIVERY_INTERVAL_SECONDS

            delivery_interval = DELIVERY_INTERVAL_SECONDS
        self.delivery_interval = delivery_interval

    def run_once(self) -> int | None:
        """One cleanup pass; the number of rows deleted, ``None`` if refused."""
        try:
            deleted = self.store.purge(export_enabled=self.delivery is not None)
        except PersistenceError as exc:
            _log.warning("outbox cleanup refused: %s", exc)
            return None
        except Exception as exc:  # noqa: BLE001 - never end the thread (OB-R6)
            _log.error("outbox cleanup failed error=%s", type(exc).__name__)
            return None
        if deleted:
            _log.info("outbox cleanup deleted=%d", deleted)
        return deleted

    def refresh_pending(self) -> None:
        """The pending count for ``/metrics`` while nothing is delivered."""
        if self.stats is None:
            return
        try:
            self.stats.set_pending(self.store.count_pending())
        except PersistenceError as exc:
            _log.warning("outbox count refused: %s", exc)
        except Exception as exc:  # noqa: BLE001 - never end the thread (OB-R6)
            _log.error("outbox count failed error=%s", type(exc).__name__)

    def deliver_once(self) -> bool:
        """One delivery cycle; ``True`` when a full batch was claimed (more
        may be waiting)."""
        if self.delivery is None:
            return False
        try:
            result = self.delivery.deliver_once()
        except PersistenceError as exc:
            _log.warning("outbox delivery refused: %s", exc)
            return False
        except Exception as exc:  # noqa: BLE001 - never end the thread (OB-R6)
            # a dead outbox thread stops the whole worker (``_run``); the
            # rows stay pending and the next cycle retries. Type only.
            _log.error("outbox delivery failed error=%s", type(exc).__name__)
            return False
        return result.claimed >= self.delivery.batch_size

    def run(self) -> None:
        next_purge = 0.0
        while not self.stop.is_set():
            if time.monotonic() >= next_purge:
                self.run_once()
                if self.delivery is None:
                    self.refresh_pending()
                next_purge = time.monotonic() + self.interval
            if self.delivery is None:
                wait = max(0.0, next_purge - time.monotonic())
            else:
                wait = 0.0 if self.deliver_once() else self.delivery_interval
            if self.stop.wait(min(wait, max(0.0, next_purge - time.monotonic()))):
                return
