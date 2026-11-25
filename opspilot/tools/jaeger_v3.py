"""Jaeger v3 query API support of the ``otel-demo-ext`` trace tools (#859).

Jaeger 2.21 removed the v1 search endpoints (``/api/services``,
``/api/operations``, ``/api/traces?service=``); a Jaeger at or past it
answers only ``/api/v3/*``. This module lets the extended profile's five
trace tools (``traces_search`` and the four A4 tools of
:mod:`opspilot.tools.jaeger_query`) read such a Jaeger (decision table
``docs/tasks/2026-11-25-859-jaeger-v3.md``, D1-D11):

* ``JaegerApiProbe`` (D1): per worker process and ``traces_endpoint``, the
  first trace call GETs ``/api/services``; a 200 v1 envelope is v1. A 404
  (or a 200 that is not the envelope: the UI's HTML fallback) is followed by
  ``/api/v3/services``, and a 200 whose body is ``{"services": [...]}`` is
  v3. Only a decided answer is cached; anything else leaves the tool on its
  v1 path, which returns the source's own error as before.
* ``traces_data`` (D5): an OTLP ``TracesData`` (``{"result": ...}``) is
  normalized to the v1 ``{"data": [trace, ...]}`` shape, following Jaeger's
  own OTLP-to-model translation, so ``otel_demo.project_traces`` projects it
  unchanged (record shape, caps and cuts stay the same).
* ``ExtTracesTransport`` (D3): ``traces_search`` of the extended profile. In
  v1 mode it delegates to the profile's ``OtelDemoTransport`` byte for byte;
  in v3 mode it sends the same query to ``/api/v3/traces`` through the same
  ``_get`` (the same bearer header, D4).

Only GETs to the same ``traces_endpoint``; this module opens no socket of
its own. ``otel_demo`` stays unchanged, so the ``otel-demo`` profile is v1
only (D2).
"""

from __future__ import annotations

import base64
import binascii
import json
import threading
import urllib.parse
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

from opspilot.redaction import has_duplicate_keys
from opspilot.tools import otel_demo
from opspilot.tools.executor import (
    TransportError,
    TransportRequest,
    TransportResponse,
    TransportResultTooLarge,
)
from opspilot.tools.registry import canonical

__all__ = [
    "V1",
    "V3",
    "V3_NOTE",
    "ExtTracesTransport",
    "JaegerApiProbe",
    "rfc3339",
    "traces_data",
    "v3_error_text",
]

V1 = "v1"
V3 = "v3"

#: D9: replaces the v1 notes that speak of an in-memory store and restarts.
V3_NOTE = (
    "This Jaeger answers only its v3 query API; this tool does not know its "
    "storage or retention, so what the tool description says about how this "
    "Jaeger stores and keeps traces may not apply. Not found or not listed "
    "does not prove it does not exist."
)

#: OTLP ``Status.code`` (integer or enum name, D5).
_STATUS_OK = frozenset({1, "1", "STATUS_CODE_OK"})
_STATUS_ERROR = frozenset({2, "2", "STATUS_CODE_ERROR"})


class JaegerApiProbe:
    """D1: which query API a Jaeger answers, decided once per endpoint.

    Lives in the worker process only (never persisted); a worker restart
    probes again. Concurrent first calls may each probe; harmless.
    """

    def __init__(self) -> None:
        self._modes: dict[str, str] = {}
        self._lock = threading.Lock()

    def mode(self, base: str, get: Callable[[str], Any]) -> str | None:
        """``V1``, ``V3`` or ``None`` (undecided: use the v1 path).

        ``get(url)`` is the calling transport's GET (``_Fetched``); its
        exceptions (5xx, timeout, over-large) propagate as the call's own.
        """
        with self._lock:
            known = self._modes.get(base)
        if known is not None:
            return known
        decided: str | None = None
        first = get(f"{base}/api/services")
        if first.status == 200 and _v1_body(first.body):
            decided = V1
        elif first.status in (200, 404):
            # A 200 that is not the v1 envelope is the UI's HTML fallback
            # (Jaeger 2.23 answers an unknown non-API path with index.html).
            second = get(f"{base}/api/v3/services")
            if second.status == 200 and _services_body(second.body):
                decided = V3
        if decided is not None:
            with self._lock:
                self._modes[base] = decided
        return decided


def _v1_body(body: bytes) -> bool:
    """A v1 ``/api/services`` envelope: an object with ``data`` (a list, or
    null for an empty v1.53 store)."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return False
    return (
        isinstance(payload, Mapping)
        and (payload.get("data") is None or isinstance(payload.get("data"), list))
        and "data" in payload
    )


def _services_body(body: bytes) -> bool:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return False
    return isinstance(payload, Mapping) and isinstance(payload.get("services"), list)


def rfc3339(moment: datetime) -> str:
    """D7: an RFC 3339 instant in UTC as Go's RFC3339Nano writes it: the
    fraction (microseconds here) without trailing zeros, none when zero."""
    utc = moment.astimezone(timezone.utc)
    fraction = f"{utc.microsecond:06d}".rstrip("0")
    head = utc.strftime("%Y-%m-%dT%H:%M:%S")
    return f"{head}.{fraction}Z" if fraction else f"{head}Z"


def v3_error_text(body: bytes) -> str | None:
    """D8: ``error.message`` of a v3 error body, or ``None``."""
    try:
        text = body.decode("utf-8")
        payload = json.loads(text)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(payload, Mapping) or has_duplicate_keys(text):
        return None
    error = payload.get("error")
    message = error.get("message") if isinstance(error, Mapping) else None
    if not isinstance(message, str) or not message:
        return None
    try:
        message.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return message


def v3_source_error(status: int, body: bytes) -> TransportResponse:
    """D8: a non-200, non-404 v3 answer (5xx already raised by ``_get``).
    Only a 400 hands its ``error.message`` on; the executor redacts and cuts
    it."""
    return TransportResponse(
        body=body,
        source_status=str(status),
        http_status=status,
        source_error_text=v3_error_text(body) if status == 400 else None,
    )


# -- OTLP to the v1 trace shape (D5) --------------------------------------------


def _hex_id(value: object, size: int) -> str | None:
    """A trace (``size`` 16) or span (8) id as lower-case hex: OTLP/JSON
    writes hex; base64 (the protobuf JSON mapping) is accepted too."""
    if not isinstance(value, str) or not value:
        return None
    lowered = value.lower()
    if len(lowered) == 2 * size and all(c in "0123456789abcdef" for c in lowered):
        return lowered
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(raw) != size:
        return None
    return raw.hex()


def _nanos(value: object) -> int | None:
    """OTLP/JSON writes 64-bit integers as decimal strings; numbers too."""
    if type(value) is int:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _any_value(value: object) -> object:
    """An OTLP ``AnyValue`` as the plain value a v1 tag carries."""
    if not isinstance(value, Mapping):
        return None
    if "stringValue" in value:
        return value["stringValue"]
    if "boolValue" in value:
        return value["boolValue"]
    if "intValue" in value:
        number = value["intValue"]
        if type(number) is int:
            return number
        if isinstance(number, str):
            try:
                return int(number)
            except ValueError:
                return number
        return number
    if "doubleValue" in value:
        return value["doubleValue"]
    if "bytesValue" in value:
        return value["bytesValue"]
    if "arrayValue" in value:
        items = value["arrayValue"]
        values = items.get("values") if isinstance(items, Mapping) else None
        return json.dumps(
            [_any_value(item) for item in values or []], ensure_ascii=False
        )
    if "kvlistValue" in value:
        items = value["kvlistValue"]
        values = items.get("values") if isinstance(items, Mapping) else None
        return json.dumps(
            {k: v for k, v in _attributes(values)}, ensure_ascii=False, default=str
        )
    return None


def _attributes(items: object) -> list[tuple[str, object]]:
    pairs: list[tuple[str, object]] = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, Mapping) and isinstance(item.get("key"), str):
            pairs.append((item["key"], _any_value(item.get("value"))))
    return pairs


def _span(
    span: Mapping[str, Any], trace_id: str, process_id: str
) -> dict[str, Any] | None:
    span_id = _hex_id(span.get("spanId"), 8)
    if span_id is None:
        return None
    tags = [{"key": k, "value": v} for k, v in _attributes(span.get("attributes"))]
    status = span.get("status")
    if isinstance(status, Mapping):
        code = status.get("code")
        if code in _STATUS_ERROR:
            tags.append({"key": "otel.status_code", "value": "ERROR"})
            tags.append({"key": "error", "value": True})
        elif code in _STATUS_OK:
            tags.append({"key": "otel.status_code", "value": "OK"})
        message = status.get("message")
        if isinstance(message, str) and message:
            tags.append({"key": "otel.status_description", "value": message})
    logs: list[dict[str, Any]] = []
    for event in span.get("events") or []:
        if not isinstance(event, Mapping):
            continue
        stamp = _nanos(event.get("timeUnixNano"))
        fields: list[dict[str, Any]] = []
        if isinstance(event.get("name"), str) and event["name"]:
            fields.append({"key": "event", "value": event["name"]})
        fields += [
            {"key": k, "value": v} for k, v in _attributes(event.get("attributes"))
        ]
        logs.append(
            {
                "timestamp": stamp // 1000 if stamp is not None else None,
                "fields": fields,
            }
        )
    references: list[dict[str, Any]] = []
    parent = _hex_id(span.get("parentSpanId"), 8)
    if parent is not None:
        references.append(
            {"refType": "CHILD_OF", "traceID": trace_id, "spanID": parent}
        )
    start = _nanos(span.get("startTimeUnixNano"))
    end = _nanos(span.get("endTimeUnixNano"))
    record: dict[str, Any] = {
        "traceID": trace_id,
        "spanID": span_id,
        "operationName": span.get("name") if isinstance(span.get("name"), str) else "",
        "references": references,
        "tags": tags,
        "logs": logs,
        "processID": process_id,
    }
    if start is not None:
        record["startTime"] = start // 1000
        if end is not None and end >= start:
            record["duration"] = (end - start) // 1000
    return record


def traces_data(result: object) -> dict[str, Any] | None:
    """A v3 ``{"result": TracesData}`` body as the v1 ``{"data": [...]}``
    shape, regrouped by trace id in first-seen order; ``None`` when it is not
    a TracesData envelope (MALFORMED_RESULT, never no_data)."""
    if not isinstance(result, Mapping):
        return None
    data = result.get("result")
    if not isinstance(data, Mapping):
        return None
    resource_spans = data.get("resourceSpans", [])
    if not isinstance(resource_spans, list):
        return None
    traces: dict[str, dict[str, Any]] = {}
    for index, block in enumerate(resource_spans):
        if not isinstance(block, Mapping):
            continue
        resource = block.get("resource")
        attrs = dict(
            _attributes(resource.get("attributes"))
            if isinstance(resource, Mapping)
            else []
        )
        process: dict[str, Any] = {
            "serviceName": str(attrs.get("service.name", "unknown")),
            "tags": (
                [{"key": "service.version", "value": attrs["service.version"]}]
                if "service.version" in attrs
                else []
            ),
        }
        process_id = f"p{index + 1}"
        for scope in block.get("scopeSpans") or []:
            if not isinstance(scope, Mapping):
                continue
            for span in scope.get("spans") or []:
                if not isinstance(span, Mapping):
                    continue
                trace_id = _hex_id(span.get("traceId"), 16)
                if trace_id is None:
                    continue
                converted = _span(span, trace_id, process_id)
                if converted is None:
                    continue
                trace = traces.setdefault(
                    trace_id, {"traceID": trace_id, "spans": [], "processes": {}}
                )
                trace["spans"].append(converted)
                trace["processes"][process_id] = process
    return {"data": list(traces.values())}


# -- traces_search of the extended profile (D3) --------------------------------


class ExtTracesTransport:
    """``traces_search`` in ``otel-demo-ext``: v1 delegates to ``sources``
    (output bytes unchanged), v3 reads ``/api/v3/traces`` through the same
    ``sources._get`` (the same bearer header, D4) and projects with
    ``otel_demo.project_traces``. The fixed ``SERVICES`` check stays."""

    def __init__(self, sources: otel_demo.OtelDemoTransport, probe: JaegerApiProbe):
        self._sources = sources
        self._probe = probe

    def fetch(self, request: TransportRequest) -> TransportResponse:
        if request.tool != otel_demo.TRACES_TOOL:
            raise TransportError("TOOL_NOT_SUPPORTED")
        if request.credential_ref not in self._sources._credentials:
            raise TransportError("CREDENTIAL_REF_UNKNOWN")
        service = request.params.get("service")
        if not isinstance(service, str) or service not in otel_demo.SERVICES:
            return self._sources.fetch(request)
        limit = request.params.get("limit", otel_demo.DEFAULT_TRACE_LIMIT)
        if type(limit) is not int or limit < 1:
            return self._sources.fetch(request)
        window, refusal = otel_demo._query_window(request)
        if refusal is not None or window is None:
            return self._sources.fetch(request)
        base = request.selector.get("traces_endpoint", "")
        if not base:
            raise TransportError("TRACES_ENDPOINT_MISSING")

        def get(url: str) -> Any:
            return self._sources._get(
                url, request, max_bytes=otel_demo.TRACE_SOURCE_READ_BYTES
            )

        if self._probe.mode(base, get) != V3:
            return self._sources.fetch(request)
        url = f"{base}/api/v3/traces?" + urllib.parse.urlencode(
            [
                ("query.serviceName", service),
                ("query.startTimeMin", rfc3339(window.start)),
                ("query.startTimeMax", rfc3339(window.end)),
                ("query.searchDepth", str(limit)),
            ]
        )
        fetched = get(url)
        if fetched.status == 404:
            payload: dict[str, Any] = {"data": []}
        elif fetched.status != 200:
            return v3_source_error(fetched.status, fetched.body)
        else:
            try:
                parsed = json.loads(fetched.body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, RecursionError):
                return TransportResponse(body=fetched.body[: request.max_result_bytes])
            converted = traces_data(parsed)
            if converted is None:
                return TransportResponse(body=b"null")
            payload = converted
        traces = payload["data"]
        if len(traces) > limit:
            # D7: a backend's searchDepth may be inexact; keep the newest.
            traces = sorted(traces, key=_trace_start, reverse=True)[:limit]
        record, start_at, end_at = otel_demo.project_traces(
            {"data": traces},
            service=service,
            limit=limit,
            window=window,
            source_sha256=sha256(fetched.body).hexdigest(),
            source_bytes=len(fetched.body),
        )
        record["query"]["jaeger_api"] = V3
        body = canonical(record).encode("utf-8")
        if len(body) > request.max_result_bytes:
            raise TransportResultTooLarge(str(request.max_result_bytes))
        return TransportResponse(
            body=body,
            data_as_of=end_at,
            source_start_at=start_at,
            source_end_at=end_at,
            query_window=window,
            row_unit="spans",
            backend_rows_returned=record["backend_returned_span_count"],
            view_fields={
                "traces_requested": limit,
                "backend_traces_returned": record["backend_returned_trace_count"],
                "incomplete_reason": otel_demo.TRACES_INCOMPLETE_REASON
                if record["incomplete"]
                else None,
                "span_groups": otel_demo._span_groups(record["data"]["sampled_spans"]),
                "span_groups_note": (
                    "span_groups summarizes the spans shown in content; "
                    "spans the adapter sampled out are not counted in any "
                    "group."
                ),
            },
        )


def _trace_start(trace: object) -> int:
    if not isinstance(trace, Mapping):
        return -1
    starts = [
        span["startTime"]
        for span in trace.get("spans") or []
        if isinstance(span, Mapping) and type(span.get("startTime")) is int
    ]
    return min(starts) if starts else -1
