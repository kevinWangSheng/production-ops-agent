"""The product-mode trace outbox, write side (#243 PR-1, #427; C3 section 11).

Every model step, tool result and postmortem generation attempt adds one row
to ``opspilot_trace_outbox`` in the transaction that commits it; Run state
changes add theirs through the 0024 trigger. Nothing here sends anything:
delivery is PR-2. The worker's outbox thread (``OutboxMaintainer``) only
deletes rows 7 days old (Q16, OB-R8).

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

import logging
import os
import re
import threading
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from opspilot import __version__
from opspilot.persistence.base import Connection, PersistenceError, _StoreBase

__all__ = [
    "KINDS",
    "PAYLOAD_KEYS",
    "PURGE_INTERVAL_SECONDS",
    "RETENTION",
    "THREAD_NAME",
    "OutboxMaintainer",
    "OutboxStore",
    "build_payload",
    "image_fields",
    "insert_entry",
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


def _number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


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


class OutboxStore(_StoreBase):
    """The worker's outbox maintenance transactions."""

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
    """The worker's outbox thread (r3-L1): in PR-1 it only runs the 7-day
    cleanup, export off; PR-2 adds delivery to this same thread. A storage
    refusal is logged by code and retried at the next interval."""

    def __init__(
        self,
        store: OutboxStore,
        *,
        stop: threading.Event,
        interval: float = PURGE_INTERVAL_SECONDS,
    ) -> None:
        self.store = store
        self.stop = stop
        self.interval = interval

    def run_once(self) -> int | None:
        try:
            deleted = self.store.purge(export_enabled=False)
        except PersistenceError as exc:
            _log.warning("outbox cleanup refused: %s", exc)
            return None
        if deleted:
            _log.info("outbox cleanup deleted=%d", deleted)
        return deleted

    def run(self) -> None:
        while not self.stop.is_set():
            self.run_once()
            if self.stop.wait(self.interval):
                return
