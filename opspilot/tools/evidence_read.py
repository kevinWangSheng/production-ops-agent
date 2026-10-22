"""``read_evidence``: row slices of an over-limit result this Run kept (#308 PR2).

A result whose whole view exceeds its registration's ``max_view_tokens`` is
refused, never truncated, and its redacted raw body is kept as un-adopted
evidence (PR1). This tool reads that record back in exact row ranges
(contract r1, U4/U6): ``read_evidence(evidence_id, offset, limit)`` returns
rows ``[offset, offset + limit)`` under the parent tool's ``result_path``
(metrics: series, traces: spans) as new, adopted evidence that carries the
parent's source and time fields and a ``slice`` header, so it may be cited
for the rows it shows.

It goes through ``ReadOnlyToolExecutor`` like every other tool (R3): control,
pause, fence, charging and registration are the executor's. The transport
here reads only the gateway's own evidence store (``get``), never a data
source, and serves only a record this Run kept as over-limit, taken from the
requested target while that target is still authorized; anything else is
``EVIDENCE_NOT_READABLE`` before anything is read. There is no JSONPath, grep
or other expression: an offset and a row count are the whole interface
(U6), and a slice whose view does not fit is refused whole like any result.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from hashlib import sha256
from typing import Any, Protocol

from .executor import (
    EVIDENCE_SOURCE,
    ReadOnlyTransport,
    TransportRequest,
    TransportResponse,
    TransportUnavailable,
)
from .outcomes import Window
from .registry import (
    MAX_VIEW_TOKENS,
    ParameterSpec,
    ToolDescription,
    ToolRegistration,
    canonical,
)

__all__ = [
    "EVIDENCE_TOOL",
    "EVIDENCE_TOOL_SCHEMA",
    "EvidenceReadTransport",
    "EvidenceReader",
    "RoutedTransport",
    "evidence_registration",
]

EVIDENCE_TOOL = "read_evidence"
# An evidence_id is ``<operation_id>:<dispatch uuid>``; anything longer is not
# one this gateway issued.
_MAX_EVIDENCE_ID_CHARS = 512

_CANNOT_PROVE = (
    "A slice proves only the rows it shows: rows of the kept result outside "
    "[offset, offset + returned) are unknown from this view, and a slice is "
    "not the whole result, so no total, maximum, minimum or absence may be "
    "read from it. The kept result itself (the over-limit refusal's "
    "evidence_id) is not citable; cite the slice. A single row larger than "
    "the view limit cannot be read this way; narrow the original query."
)
_LIMITS = (
    "offset (at least 0, default 0) and limit (at least 1) count rows under "
    "the parent tool's result path: series for metrics_range_query, spans "
    "for traces_search, names or values for the discovery tools. An offset "
    "at or past total_rows returns no_data. The whole slice view is limited "
    "to the registered max_view_tokens (25,000 DeepSeek tokens): a larger "
    "slice is refused with RESULT_TOO_LARGE and no rows, never cut; read "
    "fewer rows. Only an evidence_id from an over-limit refusal of this Run "
    "can be read; any other is denied with EVIDENCE_NOT_READABLE."
)

EVIDENCE_TOOL_SCHEMA: Mapping[str, Any] = {
    "type": "function",
    "function": {
        "name": EVIDENCE_TOOL,
        "description": (
            "Read a range of rows from a result this investigation kept "
            "because its whole view exceeded 25,000 tokens. Such a "
            "RESULT_TOO_LARGE refusal carries an evidence_id, adopted false, "
            "total_rows, row_unit and suggested_limit; that evidence_id is "
            "not citable itself. "
            "read_evidence returns rows offset to offset + limit - 1 of that "
            "result as they were captured (series for metrics_range_query, "
            "spans for traces_search, names or values for the discovery "
            "tools), with the parent's window, observed_at, data_as_of and "
            "source_start_at/source_end_at, parent_tool and a slice header "
            "(parent_evidence_id, parent_raw_sha256, offset, returned, "
            "total_rows). truncated is true when the slice omits rows of the "
            "result. The slice has its own evidence_id and may be cited for "
            "the rows it shows only; rows outside the slice are unknown, so "
            "no total, maximum, minimum or absence follows from one slice. "
            "A slice whose view exceeds 25,000 tokens is refused with "
            "RESULT_TOO_LARGE; read fewer rows. A single row that does not "
            "fit cannot be read; narrow the original query instead. Only an "
            "evidence_id from an over-limit refusal of this investigation is "
            "readable; any other is denied with EVIDENCE_NOT_READABLE."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "evidence_id": {
                    "type": "string",
                    "description": (
                        "The evidence_id of a RESULT_TOO_LARGE refusal that "
                        "says the result was kept."
                    ),
                },
                "offset": {
                    "type": "integer",
                    "description": "First row to return, from 0; omitted means 0.",
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "How many rows to return, at least 1; the refusal's "
                        "suggested_limit is about how many fit one read."
                    ),
                },
            },
            "required": ["evidence_id", "limit"],
        },
    },
}


def evidence_registration(*, max_window_seconds: int) -> ToolRegistration:
    """The ``read_evidence`` registration for a profile whose frame is
    ``max_window_seconds`` long (the loop always sends the whole frame)."""
    return ToolRegistration(
        name=EVIDENCE_TOOL,
        version="v1",
        source=EVIDENCE_SOURCE,
        verb="get",
        description=ToolDescription(
            returns=(
                "Rows of one over-limit result this Run kept as un-adopted "
                "evidence, read back from the gateway's evidence store: the "
                "rows offset to offset + limit - 1 under the parent tool's "
                "result path, with parent_tool and a slice header; source, "
                "window and time fields are the parent's."
            ),
            window_format=(
                "The parent result's query window, which lies inside the "
                "authorized window appended as {window}; a slice never "
                "reads outside it."
            ),
            values_format=(
                "The authorized target for this Run is appended as {values}; "
                "only a result kept from that target is readable."
            ),
            limits=_LIMITS,
            cannot_prove=_CANNOT_PROVE,
        ),
        may_contain_secrets=False,
        parameters={
            "evidence_id": ParameterSpec(
                "string",
                required=True,
                description="evidence_id of a kept over-limit result.",
            ),
            "offset": ParameterSpec(
                "integer", description="First row to return, from 0."
            ),
            "limit": ParameterSpec(
                "integer", required=True, description="How many rows to return."
            ),
        },
        result_path=("rows",),
        request_timeout_seconds=10.0,
        # The slice re-encodes at most the parent's rows, and every parent is
        # bounded by its own ``max_result_bytes`` (1 MiB here).
        max_result_bytes=2 * 1024 * 1024,
        max_view_tokens=MAX_VIEW_TOKENS,
        max_window_seconds=max_window_seconds,
        error_classes={
            "EVIDENCE_NOT_READABLE": "EVIDENCE_NOT_READABLE",
            "INVALID_OFFSET": "INVALID_PARAMS",
            "INVALID_LIMIT": "INVALID_PARAMS",
            "EVIDENCE_CORRUPT": "SOURCE_ERROR",
        },
    )


class EvidenceReader(Protocol):
    """The one read this transport makes (``DurableEvidenceStore.get``)."""

    def get(self, evidence_id: str) -> Any: ...


def _kept_over_limit(stored: Any) -> bool:
    """The PR1 kept-record predicate (``opspilot.web.evidence._spilled``)."""
    view = getattr(stored, "view", None)
    return (
        getattr(stored, "adopted", None) is False
        and getattr(stored, "status", None) == "error"
        and isinstance(view, Mapping)
        and view.get("reason") == "RESULT_TOO_LARGE"
    )


def _moment(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None or moment.utcoffset() is None:
        return None
    return moment


def _refused(code: str) -> TransportResponse:
    """Decided before anything is read: never dispatched, no contact."""
    return TransportResponse(
        body=canonical({"error": code}).encode(), source_status=code, sent=False
    )


class EvidenceReadTransport:
    """Serves ``read_evidence`` for one Run from the gateway's evidence store.

    ``run_id`` and ``target_ids`` are the scope's (the Controller's), never
    the model's; ``result_paths`` maps each readable parent tool to its
    registration's ``result_path``. ``store`` may be ``None`` (a sink that
    cannot read back): every read is then not readable.
    """

    def __init__(
        self,
        *,
        store: EvidenceReader | None,
        run_id: str,
        target_ids: frozenset[str],
        result_paths: Mapping[str, tuple[str, ...]],
    ) -> None:
        self._store = store
        self._run_id = run_id
        self._target_ids = frozenset(target_ids)
        self._result_paths = dict(result_paths)

    def fetch(self, request: TransportRequest) -> TransportResponse:
        params = request.params
        offset = params.get("offset", 0)
        limit = params.get("limit")
        if type(offset) is not int or offset < 0:
            return _refused("INVALID_OFFSET")
        if type(limit) is not int or limit < 1:
            return _refused("INVALID_LIMIT")
        evidence_id = params.get("evidence_id")
        if (
            self._store is None
            or not isinstance(evidence_id, str)
            or not evidence_id
            or len(evidence_id) > _MAX_EVIDENCE_ID_CHARS
        ):
            return _refused("EVIDENCE_NOT_READABLE")
        try:
            stored = self._store.get(evidence_id)
        except Exception:
            # Storage error text stays inside the store.
            raise TransportUnavailable("EVIDENCE_STORE_UNAVAILABLE") from None
        if stored is None or not _kept_over_limit(stored):
            return _refused("EVIDENCE_NOT_READABLE")
        view: Mapping[str, Any] = stored.view
        target = view.get("target_id")
        tool = view.get("tool")
        if (
            getattr(stored, "run_id", None) != self._run_id
            or target != request.target_id
            or target not in self._target_ids
            or not isinstance(tool, str)
            or tool not in self._result_paths
        ):
            # Another Run's record, a record from another target or one whose
            # target is no longer authorized, or a tool this profile does not
            # serve: refused before any row is read.
            return _refused("EVIDENCE_NOT_READABLE")
        raw = getattr(stored, "raw", None)
        if not isinstance(raw, bytes) or sha256(raw).hexdigest() != getattr(
            stored, "raw_sha256", None
        ):
            return TransportResponse(body=b"{}", source_status="EVIDENCE_CORRUPT")
        rows = _rows(raw, self._result_paths[tool])
        window = Window.parse(view.get("window"))
        observed = _moment(view.get("observed_at"))
        source = view.get("source")
        if rows is None or window is None or observed is None or not source:
            return TransportResponse(body=b"{}", source_status="EVIDENCE_CORRUPT")
        part = rows[offset : offset + limit]
        omitted = rows[:offset] + rows[offset + len(part) :]
        body = canonical(
            {
                "parent_evidence_id": evidence_id,
                "parent_raw_sha256": stored.raw_sha256,
                "offset": offset,
                "limit": limit,
                "rows": part,
            }
        ).encode("utf-8")
        start, end = view.get("source_start_at"), view.get("source_end_at")
        return TransportResponse(
            body=body,
            data_as_of=_moment(view.get("data_as_of")),
            source_start_at=_moment(start) if start is not None else None,
            source_end_at=_moment(end) if end is not None else None,
            query_window=window,
            view_fields={
                "parent_tool": tool,
                "slice": {
                    "parent_evidence_id": evidence_id,
                    "parent_raw_sha256": stored.raw_sha256,
                    "offset": offset,
                    "returned": len(part),
                    "total_rows": len(rows),
                },
            },
            carried={
                "source": source,
                "observed_at": observed,
                "dispatch_started_at": _moment(view.get("dispatch_started_at")),
                "incomplete": view.get("incomplete") is True,
                "omitted_rows": len(omitted),
                "omitted_bytes": sum(
                    len(canonical(row).encode("utf-8")) for row in omitted
                ),
            },
        )


def _rows(raw: bytes, path: Sequence[str]) -> list[object] | None:
    try:
        cursor: object = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    for step in path:
        if not isinstance(cursor, Mapping) or step not in cursor:
            return None
        cursor = cursor[step]
    return list(cursor) if isinstance(cursor, list) else None


class RoutedTransport:
    """``read_evidence`` to the evidence transport, every other tool to the
    profile's own transport (the request's gateway-resolved ``tool``)."""

    def __init__(
        self, *, evidence: ReadOnlyTransport, sources: ReadOnlyTransport
    ) -> None:
        self._evidence = evidence
        self._sources = sources

    def fetch(self, request: TransportRequest) -> TransportResponse:
        if request.tool == EVIDENCE_TOOL:
            return self._evidence.fetch(request)
        return self._sources.fetch(request)
