"""Jaeger trace tools of the extended profile (tool expansion A4, #556).

Four read-only tools over the lab Jaeger's query HTTP API (v1.53.0,
all-in-one with in-memory storage), registered only in ``otel-demo-ext``
(decision table r3, A4-1 to A4-12; N3 for what that Jaeger can do):

* ``traces_search_by_tags`` -- ``/api/traces`` for one service, filtered by
  span tags (a logfmt string the model writes, parsed here and refused with
  ``INVALID_TAGS`` before anything is sent), an operation name and a
  minimum/maximum duration (Go duration syntax). Jaeger requires every
  condition to hold on one and the same span, and the durations compare a
  single span's duration, not the whole trace's. The result goes through
  ``otel_demo.project_traces`` unchanged; only the query record and the
  projection id (``otel-demo-ext-traces-v1``) differ (A4-6).
* ``traces_query_trace_by_id`` -- ``/api/traces/{id}``: every span of one
  trace, ordered by start time, nothing sampled out (A4-8). Jaeger takes no
  time parameter here, so the spans not wholly inside the query window (the
  whole authorized window when ``start``/``end`` are omitted, A4-7) are
  dropped and counted. A 404 is an empty, successful view that says the
  in-memory store keeps only the most recent traces (A4-9).
* ``traces_list_services`` / ``traces_list_operations`` -- ``/api/services``
  and ``/api/operations``: what this Jaeger has seen since it started. No
  time range, so no source time is reported (A4-10).

Service names are no longer the fixed ``otel_demo.SERVICES`` set (A4-3):
non-empty, at most 200 characters, no control characters.

Network: ``JaegerQueryTransport`` is ``otel_demo.OtelDemoTransport``'s GET
seam (no proxy, no redirects, bounded read, request deadline) with this
module's own dispatch; this module opens no socket and starts no process.
``otel_demo`` is imported and reused, never changed, so the ``otel-demo``
profile's bytes stay pinned.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any

from opspilot.redaction import has_duplicate_keys, redact_credentials
from opspilot.tools import otel_demo
from opspilot.tools.executor import (
    TransportError,
    TransportRequest,
    TransportResponse,
    TransportResultTooLarge,
)
from opspilot.tools.outcomes import Window
from opspilot.tools.registry import (
    MAX_RESULT_BYTES as REGISTRY_MAX_RESULT_BYTES,
)
from opspilot.tools.registry import (
    MAX_VIEW_TOKENS,
    ParameterSpec,
    ToolDescription,
    ToolRegistration,
    canonical,
)

__all__ = [
    "DEFAULT_TRACE_LIMIT",
    "JAEGER_TOOLS",
    "LIST_OPERATIONS_TOOL",
    "LIST_SERVICES_TOOL",
    "ROW_UNITS",
    "SEARCH_BY_TAGS_TOOL",
    "TOOL_NAMES",
    "TOOL_SCHEMAS",
    "TRACE_BY_ID_TOOL",
    "TRACE_PROJECTION",
    "JaegerQueryTransport",
    "jaeger_registrations",
    "parse_duration",
    "parse_tags",
]

SEARCH_BY_TAGS_TOOL = "traces_search_by_tags"
TRACE_BY_ID_TOOL = "traces_query_trace_by_id"
LIST_SERVICES_TOOL = "traces_list_services"
LIST_OPERATIONS_TOOL = "traces_list_operations"
TOOL_NAMES: tuple[str, ...] = (
    SEARCH_BY_TAGS_TOOL,
    TRACE_BY_ID_TOOL,
    LIST_SERVICES_TOOL,
    LIST_OPERATIONS_TOOL,
)
JAEGER_TOOLS: frozenset[str] = frozenset(TOOL_NAMES)
#: A4-6: the search's projection id; the otel-demo one is not reused.
TRACE_PROJECTION = "otel-demo-ext-traces-v1"
#: What one result row is, per tool (the profile's over-limit summary).
ROW_UNITS: Mapping[str, str] = {
    SEARCH_BY_TAGS_TOOL: "spans",
    TRACE_BY_ID_TOOL: "spans",
    LIST_SERVICES_TOOL: "services",
    LIST_OPERATIONS_TOOL: "operations",
}

#: A4-5: same default as ``traces_search`` (B1), no upper bound.
DEFAULT_TRACE_LIMIT = otel_demo.DEFAULT_TRACE_LIMIT
#: A4-3: the longest service (and operation) name accepted.
MAX_NAME_CHARS = 200
#: The longest ``tags`` string accepted (same bound as a PromQL expression).
MAX_TAGS_CHARS = otel_demo.MAX_PROMQL_CHARS
MAX_DURATION_CHARS = 64
#: A4-10: an operation name in a listing is redacted and cut to this.
MAX_OPERATION_CHARS = otel_demo.MAX_OPERATION_CHARS
#: A4-8: the raw read cap, shared with ``traces_search``.
SOURCE_READ_BYTES = otel_demo.TRACE_SOURCE_READ_BYTES
REQUEST_TIMEOUT_SECONDS = 30.0
MAX_RESULT_BYTES = 1024 * 1024
#: A4-8: a whole trace's projected record may be kept as evidence (for
#: read_evidence, when the sink can) up to the registry's 2 MiB ceiling, so the view token limit
#: is what usually refuses an over-large trace; a record past 2 MiB is
#: RESULT_TOO_LARGE with nothing kept.
BY_ID_MAX_RESULT_BYTES = REGISTRY_MAX_RESULT_BYTES
#: Jaeger's span kinds (``/api/operations?spanKind=``).
SPAN_KINDS: tuple[str, ...] = ("client", "consumer", "internal", "producer", "server")

_TRACE_ID = re.compile(r"[0-9a-fA-F]{1,32}")
_TAG_KEY = re.compile(r"[^\s=\"\\]+")
_DURATION_PART = re.compile(r"([0-9]+(?:\.[0-9]*)?|\.[0-9]+)(ns|us|µs|μs|ms|s|m|h)")
_DURATION_NS = {
    "ns": Decimal(1),
    "us": Decimal(1_000),
    "µs": Decimal(1_000),
    "μs": Decimal(1_000),
    "ms": Decimal(1_000_000),
    "s": Decimal(1_000_000_000),
    "m": Decimal(60_000_000_000),
    "h": Decimal(3_600_000_000_000),
}

_RING_NOTE = (
    "This Jaeger keeps traces in memory, only the most recent 5000; a trace "
    "that is not found may have been evicted or never collected, so not "
    "found does not prove it does not exist."
)
_EMPTY_SEARCH_NOTE = (
    "No trace matched. Check the service name first (traces_list_services "
    "lists the names this Jaeger has seen), then the operation and tags: "
    "tag values are compared as exact strings."
)
_LIST_NOTE = (
    "Lists what this Jaeger has seen since it last started, with no time "
    "range: a name missing here does not prove it does not exist, and a "
    "Jaeger restart empties the list."
)
_ERRORS_REASON = (
    "Jaeger reported errors alongside this result, so part of it may be missing."
)

# -- validation ---------------------------------------------------------------


def _has_control(text: str) -> bool:
    return any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F for ch in text)


def _name_ok(value: object) -> bool:
    """A4-3: non-empty, at most 200 characters, no control characters."""
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= MAX_NAME_CHARS
        and not _has_control(value)
    )


def parse_tags(text: object) -> dict[str, str] | None:
    """A4-2: ``key="value" key2=value2`` (logfmt) to a tag map, or ``None``.

    A key is a run of characters other than whitespace, ``=``, ``"`` and
    ``\\``; ``=`` is required; a value is a double-quoted string (``\\"`` and
    ``\\\\`` escape) or a non-empty run of non-whitespace without quotes.
    Pairs are separated by whitespace. A repeated key, a control character or
    more than ``MAX_TAGS_CHARS`` characters is refused. A blank string is no
    tags at all.
    """
    if not isinstance(text, str) or len(text) > MAX_TAGS_CHARS:
        return None
    if _has_control(text):
        return None
    tags: dict[str, str] = {}
    index, size = 0, len(text)
    while True:
        while index < size and text[index] == " ":
            index += 1
        if index == size:
            return tags
        key_match = _TAG_KEY.match(text, index)
        if key_match is None:
            return None
        key = key_match.group(0)
        index = key_match.end()
        if index == size or text[index] != "=":
            return None
        index += 1
        if index < size and text[index] == '"':
            index += 1
            value_chars: list[str] = []
            while True:
                if index == size:
                    return None
                char = text[index]
                if char == "\\":
                    if index + 1 == size or text[index + 1] not in '"\\':
                        return None
                    value_chars.append(text[index + 1])
                    index += 2
                    continue
                if char == '"':
                    index += 1
                    break
                value_chars.append(char)
                index += 1
            value = "".join(value_chars)
        else:
            start = index
            while index < size and text[index] != " ":
                if text[index] in '"\\':
                    return None
                index += 1
            value = text[start:index]
            if not value:
                return None
        if index < size and text[index] != " ":
            return None
        if key in tags:
            return None
        tags[key] = value


def parse_duration(text: object) -> int | None:
    """A4-4: a positive Go duration (``300ms``, ``1.5s``, ``1h30m``) in
    whole nanoseconds, or ``None``."""
    if not isinstance(text, str) or not text or len(text) > MAX_DURATION_CHARS:
        return None
    total = Decimal(0)
    index = 0
    while index < len(text):
        match = _DURATION_PART.match(text, index)
        if match is None:
            return None
        try:
            total += Decimal(match.group(1)) * _DURATION_NS[match.group(2)]
        except InvalidOperation:
            return None
        index = match.end()
    nanos = int(total)
    return nanos if nanos > 0 else None


def _refused(code: str, *, frame: Window | None = None) -> TransportResponse:
    return otel_demo._refused(code, frame=frame)


def _request_window(
    request: TransportRequest, *, omitted: Window | None
) -> tuple[Window | None, TransportResponse | None]:
    """``start``/``end`` as ``traces_search`` checks them; ``omitted`` (when
    given) replaces the default used when both are left out."""
    params = request.params
    if (
        omitted is not None
        and params.get("start") is None
        and params.get("end") is None
    ):
        return omitted, None
    return otel_demo._query_window(request)


# -- source errors -------------------------------------------------------------


def _jaeger_error_text(body: bytes) -> str | None:
    """The ``errors[].msg`` strings of a Jaeger error envelope, or ``None``."""
    try:
        text = body.decode("utf-8")
        payload = json.loads(text)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(payload, Mapping) or has_duplicate_keys(text):
        return None
    errors = payload.get("errors")
    if not isinstance(errors, list):
        return None
    messages = [
        item["msg"]
        for item in errors
        if isinstance(item, Mapping) and isinstance(item.get("msg"), str)
    ]
    if not messages:
        return None
    joined = "; ".join(messages)
    try:
        joined.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return joined


def _source_error(status: int, body: bytes) -> TransportResponse:
    """A4-11: a non-200 answer (5xx already raised as unavailable). Only a
    400 hands its Jaeger error text on; the executor redacts and cuts it."""
    return TransportResponse(
        body=body,
        source_status=str(status),
        http_status=status,
        source_error_text=_jaeger_error_text(body) if status == 400 else None,
    )


def _errors_count(payload: object) -> int:
    errors = payload.get("errors") if isinstance(payload, Mapping) else None
    return len(errors) if isinstance(errors, list) else 0


def _envelope_ok(payload: object) -> bool:
    """A 200 answer is a Jaeger envelope: an object whose ``data`` is a list.
    Anything else (a bare list, ``data`` missing, null, an object or a
    string) is not an empty result but an unreadable one."""
    return isinstance(payload, Mapping) and isinstance(payload.get("data"), list)


#: Handed to the executor for a 200 that is not a Jaeger envelope: no result
#: path resolves in it, so the outcome is error/MALFORMED_RESULT, never
#: no_data (the general contract, tests/test_m1_tool_outcomes.py).
_MALFORMED = TransportResponse(body=b"null")


def _incomplete_reason(*reasons: str | None) -> str | None:
    kept = [reason for reason in reasons if reason]
    return " ".join(kept) if kept else None


# -- transport -----------------------------------------------------------------


def _trace_start(trace: object) -> int:
    if not isinstance(trace, Mapping):
        return -1
    starts = [
        span["startTime"]
        for span in trace.get("spans") or []
        if isinstance(span, Mapping) and type(span.get("startTime")) is int
    ]
    return min(starts) if starts else -1


def _utc_us(micros: int) -> datetime:
    return datetime.fromtimestamp(micros / 1_000_000, tz=timezone.utc)


class JaegerQueryTransport(otel_demo.OtelDemoTransport):
    """Serves the four tools of this module over the ``traces_endpoint`` of
    the target selector; any other tool is ``TOOL_NOT_SUPPORTED``.

    The GET itself is the parent's (``_get``): no proxy, no redirect, the
    request deadline and a bounded read. Anonymous: the lab Jaeger is reached
    on its anonymous query port (A4 brief), so whatever token a slot holds is
    dropped here and no ``Authorization`` header is ever sent -- neither the
    Prometheus basic auth (``_get`` sends it only for Prometheus tools) nor a
    bearer token. A slot must still be registered (``CREDENTIAL_REF_UNKNOWN``).
    """

    def __init__(
        self,
        *,
        credentials: Mapping[str, str | None],
        # ``urllib.request.OpenerDirector | None``, typed loosely so this
        # module imports no network library itself.
        opener: Any = None,
    ) -> None:
        super().__init__(credentials=dict.fromkeys(credentials), opener=opener)

    def fetch(self, request: TransportRequest) -> TransportResponse:
        if request.credential_ref not in self._credentials:
            raise TransportError("CREDENTIAL_REF_UNKNOWN")
        if request.tool == SEARCH_BY_TAGS_TOOL:
            return self._search(request)
        if request.tool == TRACE_BY_ID_TOOL:
            return self._by_id(request)
        if request.tool == LIST_SERVICES_TOOL:
            return self._services(request)
        if request.tool == LIST_OPERATIONS_TOOL:
            return self._operations(request)
        raise TransportError("TOOL_NOT_SUPPORTED")

    @staticmethod
    def _base(request: TransportRequest) -> str:
        base = request.selector.get("traces_endpoint", "")
        if not base:
            raise TransportError("TRACES_ENDPOINT_MISSING")
        return base

    def _read_json(
        self, url: str, request: TransportRequest
    ) -> tuple[Any, bytes, TransportResponse | None]:
        """GET ``url``; ``(payload, body, None)`` on a parsed 200, otherwise
        ``(None, body, response)`` with the response to return as is."""
        fetched = self._get(url, request, max_bytes=SOURCE_READ_BYTES)
        if fetched.status != 200:
            return None, fetched.body, _source_error(fetched.status, fetched.body)
        try:
            payload = json.loads(fetched.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            # The executor classifies these bytes as MALFORMED_RESULT.
            return (
                None,
                fetched.body,
                TransportResponse(body=fetched.body[: request.max_result_bytes]),
            )
        if not _envelope_ok(payload):
            return None, fetched.body, _MALFORMED
        return payload, fetched.body, None

    @staticmethod
    def _encoded(record: Mapping[str, Any], request: TransportRequest) -> bytes:
        body = canonical(record).encode("utf-8")
        if len(body) > request.max_result_bytes:
            raise TransportResultTooLarge(str(request.max_result_bytes))
        return body

    # -- traces_search_by_tags ----------------------------------------------

    def _search(self, request: TransportRequest) -> TransportResponse:
        params = request.params
        service = params.get("service")
        if not _name_ok(service):
            return _refused("INVALID_SERVICE")
        assert isinstance(service, str)
        tags: dict[str, str] = {}
        if params.get("tags") is not None:
            parsed = parse_tags(params.get("tags"))
            if parsed is None:
                return _refused("INVALID_TAGS")
            tags = parsed
        operation = params.get("operation")
        if operation is not None and not _name_ok(operation):
            return _refused("INVALID_OPERATION")
        durations: dict[str, int] = {}
        for name in ("min_duration", "max_duration"):
            if params.get(name) is not None:
                nanos = parse_duration(params.get(name))
                if nanos is None:
                    return _refused("INVALID_DURATION")
                durations[name] = nanos
        if (
            len(durations) == 2
            and durations["max_duration"] < durations["min_duration"]
        ):
            return _refused("INVALID_DURATION")
        limit = params.get("limit", DEFAULT_TRACE_LIMIT)
        if type(limit) is not int or limit < 1:
            return _refused("INVALID_LIMIT")
        window, refusal = _request_window(request, omitted=None)
        if refusal is not None:
            return refusal
        assert window is not None
        query: list[tuple[str, str]] = [("service", service)]
        if isinstance(operation, str):
            query.append(("operation", operation))
        if tags:
            query.append(("tags", json.dumps(tags, separators=(",", ":"))))
        # Sent as written: Jaeger parses Go durations itself, and every
        # string ``parse_duration`` accepts is one Go's parser accepts.
        for name, wire in (
            ("min_duration", "minDuration"),
            ("max_duration", "maxDuration"),
        ):
            if name in durations:
                query.append((wire, str(params[name])))
        query += [
            ("start", str(int(window.start.timestamp() * 1_000_000))),
            ("end", str(int(window.end.timestamp() * 1_000_000))),
            ("limit", str(limit)),
        ]
        url = f"{self._base(request)}/api/traces?" + urllib.parse.urlencode(query)
        payload, raw, early = self._read_json(url, request)
        if early is not None:
            return early
        traces = payload.get("data") if isinstance(payload, Mapping) else None
        traces = traces if isinstance(traces, list) else []
        backend_traces = len(traces)
        if backend_traces > limit:
            # A4-5: past ``limit``, the newest traces (latest first span).
            traces = sorted(traces, key=_trace_start, reverse=True)[:limit]
        errors = _errors_count(payload)
        record, start_at, end_at = otel_demo.project_traces(
            {"data": traces},
            service=service,
            limit=limit,
            window=window,
            source_sha256=sha256(raw).hexdigest(),
            source_bytes=len(raw),
        )
        record["projection"] = TRACE_PROJECTION
        record["query"] = {
            "service": service,
            "tags": tags,
            "operation": operation,
            "min_duration": params.get("min_duration"),
            "max_duration": params.get("max_duration"),
            "limit": limit,
            "window": window.as_json(),
        }
        record["backend_traces_dropped_over_limit"] = backend_traces - len(traces)
        full = bool(record["incomplete"])
        record["backend_errors"] = errors
        record["incomplete"] = full or errors > 0
        notes = [] if traces else [_EMPTY_SEARCH_NOTE]
        return TransportResponse(
            body=self._encoded(record, request),
            data_as_of=end_at,
            source_start_at=start_at,
            source_end_at=end_at,
            query_window=window,
            row_unit="spans",
            backend_rows_returned=record["backend_returned_span_count"],
            view_fields={
                "traces_requested": limit,
                "backend_traces_returned": backend_traces,
                "incomplete_reason": _incomplete_reason(
                    otel_demo.TRACES_INCOMPLETE_REASON if full else None,
                    _ERRORS_REASON if errors else None,
                ),
                "span_groups": otel_demo._span_groups(record["data"]["sampled_spans"]),
                "span_groups_note": (
                    "span_groups summarizes the spans shown in content; "
                    "spans the adapter sampled out are not counted in any "
                    "group."
                ),
                "notes": notes,
            },
        )

    # -- traces_query_trace_by_id -------------------------------------------

    def _by_id(self, request: TransportRequest) -> TransportResponse:
        trace_id = request.params.get("trace_id")
        if not isinstance(trace_id, str) or not _TRACE_ID.fullmatch(trace_id):
            return _refused("INVALID_TRACE_ID")
        trace_id = trace_id.lower()
        window, refusal = _request_window(request, omitted=request.window)
        if refusal is not None:
            return refusal
        assert window is not None
        url = f"{self._base(request)}/api/traces/{urllib.parse.quote(trace_id)}"
        fetched = self._get(url, request, max_bytes=SOURCE_READ_BYTES)
        found = fetched.status == 200
        if fetched.status == 404:
            payload: Any = {"data": []}
        elif not found:
            return _source_error(fetched.status, fetched.body)
        else:
            try:
                payload = json.loads(fetched.body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, RecursionError):
                return TransportResponse(body=fetched.body[: request.max_result_bytes])
            if not _envelope_ok(payload):
                return _MALFORMED
        traces = payload.get("data") if isinstance(payload, Mapping) else None
        traces = (
            [t for t in traces if isinstance(t, Mapping)]
            if isinstance(traces, list)
            else []
        )
        lo = int(window.start.timestamp() * 1_000_000)
        hi = int(window.end.timestamp() * 1_000_000)
        total = 0
        kept_traces: list[dict[str, Any]] = []
        for trace in traces:
            spans = [s for s in trace.get("spans") or [] if isinstance(s, Mapping)]
            total += len(spans)
            inside = [
                span
                for span in spans
                if type(span.get("startTime")) is int
                and type(span.get("duration")) is int
                and lo <= span["startTime"]
                and span["startTime"] + span["duration"] <= hi
            ]
            kept_traces.append({**trace, "spans": inside})
        dropped = total - sum(len(t["spans"]) for t in kept_traces)
        record, _, _ = otel_demo.project_traces(
            {"data": kept_traces},
            service="",
            limit=1,
            window=window,
            source_sha256=sha256(fetched.body).hexdigest(),
            source_bytes=len(fetched.body),
        )
        spans = record["data"]["sampled_spans"]
        # A4-8: every span, by start time; nothing sampled out.
        spans.sort(
            key=lambda s: (int(s["start_us"]), str(s["trace_id"]), str(s["span_id"]))
        )
        errors = _errors_count(payload) if found else 0
        record["projection"] = TRACE_PROJECTION
        record["query"] = {
            "trace_id": trace_id,
            "window": window.as_json(),
            "window_is_authorized_window": window == request.window,
        }
        record["trace_found"] = found and bool(traces)
        record["backend_returned_span_count"] = total
        record["spans_outside_window"] = dropped
        record["omitted_span_count"] = dropped
        record["limits"] = {
            "detail_max_fields_per_span": otel_demo.MAX_DETAIL_FIELDS_PER_SPAN,
            "detail_max_utf8_bytes_per_field": otel_demo.MAX_DETAIL_BYTES_PER_FIELD,
        }
        record["selection_policy"] = (
            "Every span of the trace that lies wholly inside the query window, "
            "ordered by start time; spans outside it are dropped and counted, "
            "nothing else is sampled out."
        )
        record["backend_errors"] = errors
        record["incomplete"] = errors > 0
        start_at = end_at = None
        if spans:
            start_at = _utc_us(min(s["start_us"] for s in spans))
            end_at = _utc_us(max(s["start_us"] + s["duration_us"] for s in spans))
        notes: list[str] = []
        if not record["trace_found"]:
            notes.append(_RING_NOTE)
        if dropped:
            notes.append(
                f"{dropped} span(s) of this trace lie outside the query window "
                "(or carry no start time or duration) and are not shown."
            )
        return TransportResponse(
            body=self._encoded(record, request),
            data_as_of=end_at,
            source_start_at=start_at,
            source_end_at=end_at,
            query_window=window,
            row_unit="spans",
            backend_rows_returned=total,
            view_fields={
                "trace_found": record["trace_found"],
                "spans_outside_window": dropped,
                "incomplete_reason": _ERRORS_REASON if errors else None,
                "span_groups": otel_demo._span_groups(spans),
                "notes": notes,
            },
        )

    # -- traces_list_services / traces_list_operations -----------------------

    def _services(self, request: TransportRequest) -> TransportResponse:
        url = f"{self._base(request)}/api/services"
        payload, raw, early = self._read_json(url, request)
        if early is not None:
            return early
        data = payload.get("data") if isinstance(payload, Mapping) else None
        names = sorted(
            {
                redact_credentials(item)[:MAX_NAME_CHARS]
                for item in (data if isinstance(data, list) else [])
                if isinstance(item, str)
            }
        )
        return self._listing(
            request,
            raw=raw,
            payload=payload,
            key="services",
            rows=names,
            query={},
        )

    def _operations(self, request: TransportRequest) -> TransportResponse:
        service = request.params.get("service")
        if not _name_ok(service):
            return _refused("INVALID_SERVICE")
        assert isinstance(service, str)
        span_kind = request.params.get("span_kind")
        if span_kind is not None and span_kind not in SPAN_KINDS:
            return _refused("INVALID_SPAN_KIND")
        query = [("service", service)]
        if isinstance(span_kind, str):
            query.append(("spanKind", span_kind))
        url = f"{self._base(request)}/api/operations?" + urllib.parse.urlencode(query)
        payload, raw, early = self._read_json(url, request)
        if early is not None:
            return early
        data = payload.get("data") if isinstance(payload, Mapping) else None
        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str | None]] = set()
        for item in data if isinstance(data, list) else []:
            if isinstance(item, Mapping) and isinstance(item.get("name"), str):
                name, kind = item["name"], item.get("spanKind")
            elif isinstance(item, str):
                name, kind = item, None
            else:
                continue
            redacted = redact_credentials(name)
            kind_text = kind if isinstance(kind, str) and kind in SPAN_KINDS else None
            entry = (redacted[:MAX_OPERATION_CHARS], kind_text)
            if entry in seen:
                continue
            seen.add(entry)
            rows.append(
                {
                    "name": entry[0],
                    "name_truncated": len(redacted) > MAX_OPERATION_CHARS,
                    "span_kind": kind_text,
                }
            )
        rows.sort(key=lambda row: (row["name"], row["span_kind"] or ""))
        return self._listing(
            request,
            raw=raw,
            payload=payload,
            key="operations",
            rows=rows,
            query={"service": service, "span_kind": span_kind},
        )

    def _listing(
        self,
        request: TransportRequest,
        *,
        raw: bytes,
        payload: object,
        key: str,
        rows: Sequence[object],
        query: Mapping[str, object],
    ) -> TransportResponse:
        errors = _errors_count(payload)
        record = {
            "source": "jaeger",
            "query": dict(query),
            "source_response_sha256": sha256(raw).hexdigest(),
            "source_response_bytes": len(raw),
            "backend_errors": errors,
            "incomplete": errors > 0,
            key: list(rows),
        }
        # A4-10: no time range, so no source time and no query window.
        return TransportResponse(
            body=self._encoded(record, request),
            row_unit=key,
            backend_rows_returned=len(rows),
            view_fields={
                "incomplete_reason": _ERRORS_REASON if errors else None,
                "notes": [_LIST_NOTE],
            },
        )


# -- model-visible face ---------------------------------------------------------

_WINDOW_PARAMS_TEXT = (
    "Give both start and end, or neither. Must lie inside the authorized "
    "window; a query window outside it is refused and the refusal states the "
    "authorized window."
)
_SOURCE_ERRORS_TEXT = (
    "Jaeger HTTP 400 is INVALID_PARAMS and carries Jaeger's own error text "
    "(untrusted, credential-redacted, cut short); 5xx is SOURCE_UNAVAILABLE. "
    "When Jaeger reports errors next to a result, the result is marked "
    "incomplete."
)
_TOO_LARGE_TEXT = (
    "A result whose whole view exceeds 25,000 tokens is refused with "
    "RESULT_TOO_LARGE and nothing is shown. Only when that refusal carries an "
    "evidence_id (stored is not false) can the result be read back with "
    "read_evidence; otherwise narrow the query."
)
_SPAN_ROW_TEXT = (
    "Each span row has trace_id, span_id, service, operation, start_us, "
    "duration_us, status tags with status_state recorded or not_recorded "
    "(not_recorded is Unset in OTel terms, not OK), parent references and up "
    "to 4 clipped error detail fields; other tags are not shown."
)

TOOL_SCHEMAS: tuple[Mapping[str, Any], ...] = (
    {
        "type": "function",
        "function": {
            "name": SEARCH_BY_TAGS_TOOL,
            "description": (
                "Search the registered target's Jaeger for traces of one "
                "service, filtered by span tags, operation and span duration, "
                "inside a query window you may choose within the authorized "
                "window. Every condition must hold on the same single span of "
                "that service: tags, operation, min_duration and max_duration "
                "are all compared against one span, and the durations are "
                "that span's duration, not the whole trace's. Tag values are "
                "compared as exact strings. Returns a projected sample of the "
                "spans of the matching traces (requested service first, then "
                "error status, then longest), the same projection as "
                "traces_search. " + _SPAN_ROW_TEXT + " At most limit traces "
                "(default 20, no upper bound); when Jaeger returns as many as "
                "asked, incomplete is true and more may exist, and the newest "
                "traces are the ones shown. Service names are not a fixed "
                "list; an empty result may mean a wrong service name, so check "
                "it with traces_list_services. A malformed tags string is "
                "refused with INVALID_TAGS before anything is sent. "
                + _TOO_LARGE_TEXT
                + " A biased sample, not a failure rate or the complete trace "
                "graph; an empty result is not proof that no such span exists."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {
                        "type": "string",
                        "description": (
                            "Service name as Jaeger reports it (see "
                            "traces_list_services); required."
                        ),
                    },
                    "tags": {
                        "type": "string",
                        "description": (
                            "Span tags in logfmt, for example: "
                            'http.response.status_code="500" error=true. '
                            "Quote a value that has spaces; every pair must "
                            "match on the same span."
                        ),
                    },
                    "operation": {
                        "type": "string",
                        "description": "Exact span operation name of that service.",
                    },
                    "min_duration": {
                        "type": "string",
                        "description": (
                            "Minimum duration of the matching span, Go "
                            "duration syntax such as 300ms, 1.5s or 2m."
                        ),
                    },
                    "max_duration": {
                        "type": "string",
                        "description": (
                            "Maximum duration of the matching span, Go "
                            "duration syntax; not less than min_duration."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            "How many traces to ask Jaeger for, at least 1; "
                            "omitted means 20."
                        ),
                    },
                    "start": {
                        "type": "string",
                        "description": (
                            "Absolute ISO-8601 start of the query window. "
                            + _WINDOW_PARAMS_TEXT
                            + " Omitted with end also omitted, the query "
                            "window is the time policy's default_query_window "
                            "when it states one, otherwise the 1 hour ending "
                            "at the authorized window's end."
                        ),
                    },
                    "end": {
                        "type": "string",
                        "description": (
                            "Absolute ISO-8601 end of the query window. "
                            + _WINDOW_PARAMS_TEXT
                        ),
                    },
                },
                "required": ["service"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": TRACE_BY_ID_TOOL,
            "description": (
                "Fetch one whole trace by its trace ID from the registered "
                "target's Jaeger: every span, ordered by start time, nothing "
                "sampled out. " + _SPAN_ROW_TEXT + " Spans not wholly inside "
                "the query window are dropped and counted in "
                "spans_outside_window; omitting start and end uses the whole "
                "authorized window. When Jaeger has no such trace the result "
                "is empty with trace_found false: this Jaeger keeps only the "
                "most recent 5000 traces in memory, so not found does not "
                "prove the trace does not exist. " + _TOO_LARGE_TEXT
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "trace_id": {
                        "type": "string",
                        "description": "Trace ID, 1 to 32 hexadecimal characters.",
                    },
                    "start": {
                        "type": "string",
                        "description": (
                            "Absolute ISO-8601 start of the query window. "
                            + _WINDOW_PARAMS_TEXT
                            + " Omitted with end also omitted, the whole "
                            "authorized window."
                        ),
                    },
                    "end": {
                        "type": "string",
                        "description": (
                            "Absolute ISO-8601 end of the query window. "
                            + _WINDOW_PARAMS_TEXT
                        ),
                    },
                },
                "required": ["trace_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": LIST_SERVICES_TOOL,
            "description": (
                "List the service names the registered target's Jaeger has "
                "seen since it last started. Use it to find the exact service "
                "name for traces_search_by_tags or traces_list_operations. "
                "The list has no time range: a service missing from it does "
                "not prove the service does not exist or made no calls, and a Jaeger restart empties it."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": LIST_OPERATIONS_TOOL,
            "description": (
                "List the span operation names the registered target's Jaeger "
                "has seen for one service since it last started, each with its "
                "span kind; names are credential-redacted and cut to 150 "
                "characters. The list has no time range: an operation missing "
                "from it does not prove it does not exist, and a Jaeger restart "
                "empties it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {
                        "type": "string",
                        "description": (
                            "Service name as Jaeger reports it (see "
                            "traces_list_services); required."
                        ),
                    },
                    "span_kind": {
                        "type": "string",
                        "description": (
                            "Only operations of this span kind: one of "
                            "server, client, producer, consumer, internal."
                        ),
                    },
                },
                "required": ["service"],
            },
        },
    },
)


def _window_parameters(omitted: str) -> dict[str, ParameterSpec]:
    return {
        "start": ParameterSpec(
            "string",
            description=(
                "Absolute ISO-8601 start of the query window; give both start "
                f"and end or neither ({omitted})."
            ),
        ),
        "end": ParameterSpec(
            "string",
            description=(
                "Absolute ISO-8601 end of the query window; give both start "
                "and end or neither."
            ),
        ),
    }


_SERVICE_PARAMETER = ParameterSpec(
    "string",
    required=True,
    description="Service name as Jaeger reports it; not a fixed list.",
)

_SEARCH_WINDOW_FORMAT = (
    "The authorized absolute query window, appended as {window}. start/end "
    "pick a query window inside it; omitted, the query window is the time "
    "policy's default_query_window when it states one, otherwise the 1 hour "
    "ending at the authorized window's end; a chosen window outside the "
    "authorized window is refused."
)
_BY_ID_WINDOW_FORMAT = (
    "The authorized absolute query window, appended as {window}. Jaeger takes "
    "no time range for a trace ID, so the spans not wholly inside the query "
    "window (start/end, or the whole authorized window when both are "
    "omitted) are dropped and counted; a chosen window outside the "
    "authorized window is refused."
)
_LIST_WINDOW_FORMAT = (
    "The authorized absolute window is appended as {window}, but this list "
    "has no time range: it is what Jaeger has seen since it last started, "
    "and no source time is reported."
)
_NAME_VALUES_FORMAT = (
    "The authorized target is appended as {values}. service is any name "
    "Jaeger reports (traces_list_services): non-empty, at most 200 "
    "characters, no control characters; it is not the fixed traces_search "
    "list."
)
_ERROR_CLASSES = {
    "400": "INVALID_PARAMS",
    "404": "INVALID_PARAMS",
    "503": "SOURCE_UNAVAILABLE",
    "INVALID_SERVICE": "INVALID_PARAMS",
    "INVALID_TAGS": "INVALID_TAGS",
    "INVALID_OPERATION": "INVALID_PARAMS",
    "INVALID_DURATION": "INVALID_PARAMS",
    "INVALID_LIMIT": "INVALID_PARAMS",
    "INVALID_TRACE_ID": "INVALID_PARAMS",
    "INVALID_SPAN_KIND": "INVALID_PARAMS",
    "QUERY_OUT_OF_WINDOW": "INVALID_PARAMS",
}


def jaeger_registrations(*, max_window_seconds: int) -> tuple[ToolRegistration, ...]:
    """The four registrations (``source`` is the lab target's, A2-1)."""
    common: dict[str, Any] = {
        "version": "v1",
        "source": otel_demo.SOURCE,
        "verb": "query",
        "may_contain_secrets": False,
        "request_timeout_seconds": REQUEST_TIMEOUT_SECONDS,
        "max_result_bytes": MAX_RESULT_BYTES,
        "max_view_tokens": MAX_VIEW_TOKENS,
        "max_window_seconds": max_window_seconds,
        "error_classes": dict(_ERROR_CLASSES),
        "incomplete_marker": "incomplete",
    }
    search = ToolRegistration(
        name=SEARCH_BY_TAGS_TOOL,
        description=ToolDescription(
            returns=(
                "A projected observation record (otel-demo-ext-traces-v1) "
                "over the Jaeger trace search for one service filtered by "
                "span tags, operation and span duration: sampled spans under "
                "data.sampled_spans, backend trace/span counts per service, "
                "the query as parsed, and the SHA-256 and byte count of the "
                "wire response; the view adds traces_requested, "
                "backend_traces_returned, backend_spans_returned, "
                "spans_shown, spans_omitted, span_groups and "
                "incomplete_reason."
            ),
            window_format=_SEARCH_WINDOW_FORMAT,
            values_format=_NAME_VALUES_FORMAT
            + ' tags is a logfmt string (key="value" key2=value2); a '
            "malformed one is INVALID_TAGS before anything is sent. "
            "min_duration/max_duration are Go durations, max not below min.",
            limits=(
                "limit defaults to 20 with no upper bound; when Jaeger "
                "returns as many traces as asked the result is incomplete and "
                "the newest traces are kept. The whole view is limited to the "
                "registered max_view_tokens (25,000 DeepSeek tokens): a "
                "larger result is refused with RESULT_TOO_LARGE, never "
                "truncated. " + _SOURCE_ERRORS_TEXT
            ),
            cannot_prove=(
                "All conditions match one single span and the durations are "
                "that span's, not the trace's. A biased sample, not the "
                "complete trace graph or a failure rate; an empty result is "
                "not proof that no such span exists (check the service name "
                "with traces_list_services; tag values compare as exact "
                "strings)."
            ),
        ),
        parameters={
            "service": _SERVICE_PARAMETER,
            "tags": ParameterSpec(
                "string", description='Span tags in logfmt: key="value" key2=value2.'
            ),
            "operation": ParameterSpec(
                "string", description="Exact span operation name."
            ),
            "min_duration": ParameterSpec(
                "string", description="Minimum span duration, Go duration syntax."
            ),
            "max_duration": ParameterSpec(
                "string", description="Maximum span duration, Go duration syntax."
            ),
            "limit": ParameterSpec(
                "integer", description="How many traces to ask Jaeger for, at least 1."
            ),
            **_window_parameters("omitted: the default query window"),
        },
        result_path=("data", "sampled_spans"),
        **common,
    )
    by_id = ToolRegistration(
        name=TRACE_BY_ID_TOOL,
        description=ToolDescription(
            returns=(
                "A projected observation record (otel-demo-ext-traces-v1) of "
                "one whole Jaeger trace: every span inside the query window "
                "under data.sampled_spans, ordered by start time, with "
                "trace_found, spans_outside_window and the SHA-256 and byte "
                "count of the wire response; the view adds "
                "backend_spans_returned, spans_shown, spans_omitted and "
                "span_groups."
            ),
            window_format=_BY_ID_WINDOW_FORMAT,
            values_format=(
                "The authorized target is appended as {values}. trace_id is 1 "
                "to 32 hexadecimal characters; anything else is "
                "INVALID_PARAMS before anything is sent."
            ),
            limits=(
                "Nothing is sampled out. More than 4 MiB read from Jaeger, or "
                "a view over the registered max_view_tokens (25,000 DeepSeek "
                "tokens), refuses the whole trace with RESULT_TOO_LARGE. Only a "
                "refusal that carries an evidence_id can be read back with "
                "read_evidence; a raw read over 4 MiB or a projected record "
                "over 2 MiB keeps nothing, so narrow start/end. HTTP 404 "
                "is an empty result with trace_found false. " + _SOURCE_ERRORS_TEXT
            ),
            cannot_prove=(
                "Not found does not prove the trace does not exist: this "
                "Jaeger keeps only the most recent 5000 traces in memory. "
                "Spans outside the query window are not shown and are "
                "unknown, not absent."
            ),
        ),
        parameters={
            "trace_id": ParameterSpec(
                "string",
                required=True,
                description="Trace ID, 1 to 32 hexadecimal characters.",
            ),
            **_window_parameters("omitted: the whole authorized window"),
        },
        result_path=("data", "sampled_spans"),
        **{**common, "max_result_bytes": BY_ID_MAX_RESULT_BYTES},
    )
    services = ToolRegistration(
        name=LIST_SERVICES_TOOL,
        description=ToolDescription(
            returns=(
                "The service names this Jaeger has seen since it last "
                "started, sorted and credential-redacted, under services."
            ),
            window_format=_LIST_WINDOW_FORMAT,
            values_format="The authorized target is appended as {values}.",
            limits=(
                "Every name Jaeger returns, each cut to 200 characters. "
                + _SOURCE_ERRORS_TEXT
            ),
            cannot_prove=(
                "Only what Jaeger has seen since it last started, with no time "
                "range: a missing service is not proof it does not exist or "
                "made no calls, and a Jaeger restart empties the list."
            ),
        ),
        parameters={},
        result_path=("services",),
        **common,
    )
    operations = ToolRegistration(
        name=LIST_OPERATIONS_TOOL,
        description=ToolDescription(
            returns=(
                "The span operation names this Jaeger has seen for one "
                "service since it last started, each with name, "
                "name_truncated and span_kind, under operations."
            ),
            window_format=_LIST_WINDOW_FORMAT,
            values_format=_NAME_VALUES_FORMAT
            + " span_kind, when given, is one of server, client, producer, "
            "consumer, internal.",
            limits=(
                "Every operation Jaeger returns; names are credential-redacted "
                "and cut to 150 characters. " + _SOURCE_ERRORS_TEXT
            ),
            cannot_prove=(
                "Only what Jaeger has seen since it last started, with no time "
                "range: a missing operation is not proof it does not exist, "
                "and a Jaeger restart empties the list."
            ),
        ),
        parameters={
            "service": _SERVICE_PARAMETER,
            "span_kind": ParameterSpec(
                "string",
                description="One of server, client, producer, consumer, internal.",
            ),
        },
        result_path=("operations",),
        **common,
    )
    return search, by_id, services, operations
