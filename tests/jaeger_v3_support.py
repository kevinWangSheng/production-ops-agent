"""A faked Jaeger for the #859 contract tests: v1 query API or v3 only.

Written by the independent test author from the #859 pre-review (decisions
D1-D11) and the Jaeger v2.23.0 api_v3 HTTP gateway source, without the
implementation. The shipped ``otel-demo-ext`` profile is built through its
public factory exactly as ``tests/safety/gateway_support.py`` does; only
``urllib.request.build_opener`` is replaced, by :class:`JaegerDouble`.

* ``mode="v1"``: a Jaeger 1.x query service. ``/api/services``,
  ``/api/operations``, ``/api/traces`` and ``/api/traces/{id}`` answer in the
  v1 envelope (``{"data": ...}``); ``/api/v3/*`` answers too, as Jaeger 1.x
  does, so a tool that needlessly switches to v3 is observable.
* ``mode="v3"``: a Jaeger >= 2.21 (pinned 2.23.0 in ITBench). The removed v1
  routes (``/api/services``, ``/api/operations``, the ``/api/traces`` search)
  are a plain-text 404; ``GET /api/traces/{id}`` is kept (v1 envelope); the
  api_v3 routes answer ``{"services": [...]}``, ``{"operations": [...]}``
  and ``{"result": <OTLP TracesData>}``, with every trace merged into one
  ``resourceSpans`` list (http_gateway.go returnTraces). No trace found is
  HTTP 404 ``{"error":{"httpCode":404,"message":"No traces found"}}`` for
  the search and for a trace ID alike.
* ``mode="down"``: every request is a 503 (probe failures, D1).

OTLP/JSON encoding (pdata's marshaller, per the OTLP/JSON spec): ids are
lowercase hex strings, ``*UnixNano`` are decimal strings, enums
(``kind``, ``status.code``) are integers. The pre-review marks the exact
wire encoding unconfirmed; these fixtures use the spec's encoding.

Every value below is synthetic; the Jaeger base URL differs per test so a
probe cache keyed by endpoint cannot leak between tests.
"""

from __future__ import annotations

import io
import json
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest

from opspilot.persistence import Lease
from opspilot.tools.otel_demo import TARGET_ID
from opspilot.tools.profiles import select_profile
from tests.m1_tool_support import FakeClock, RecordingSink
from tests.safety.gateway_support import NOW, WINDOW, Gateway, _Store, ext_env

# -- the synthetic traces -------------------------------------------------------

#: 2026-11-18T11:30:00Z, inside the gateway's default query window
#: (11:00-11:59, the hour ending at the authorized frame's end).
T0_NS = int(datetime(2026, 11, 18, 11, 30, tzinfo=UTC).timestamp()) * 1_000_000_000
T0_US = T0_NS // 1_000
MS = 1_000_000  # ns

TRACE_A = "5a5a0000000000000000000000000a01"
TRACE_B = "5a5a0000000000000000000000000b01"
SPAN_A1 = "5a5a00000000a001"  # checkout PlaceOrder, root, status unset
SPAN_A2 = "5a5a00000000a002"  # payment Charge, child of A1, status ERROR
SPAN_B1 = "5a5a00000000b001"  # checkout PlaceOrder, root, status OK
UNKNOWN_TRACE = "5a5a00000000000000000000000ffff1"

SERVICE_VERSIONS = {"checkout": "1.4.0", "payment": "2.0.1"}
STATUS_MESSAGE = "card declined"
EXCEPTION_TYPE = "ChargeError"
EXCEPTION_MESSAGE = "charge failed: gateway timeout"

#: OTLP span kinds (SPAN_KIND_SERVER = 2, SPAN_KIND_CLIENT = 3).
_KIND = {"server": 2, "client": 3}
#: OTLP status codes (STATUS_CODE_OK = 1, STATUS_CODE_ERROR = 2).
_STATUS = {"ok": 1, "error": 2}


@dataclass(frozen=True)
class SpanSpec:
    """One span, rendered both as OTLP (v3) and as a Jaeger v1 span."""

    trace_id: str
    span_id: str
    service: str
    name: str
    kind: str
    start_ns: int
    end_ns: int
    parent: str | None = None
    status: str | None = None  # None (unset), "ok", "error"
    status_message: str | None = None
    attributes: tuple[tuple[str, str], ...] = ()
    #: (time_ns, name, ((key, value), ...))
    events: tuple[tuple[int, str, tuple[tuple[str, str], ...]], ...] = ()


def default_spans(
    *,
    status_message: str = STATUS_MESSAGE,
    exception_message: str = EXCEPTION_MESSAGE,
    exception_stacktrace: str | None = None,
    extra_attribute: tuple[str, str] | None = None,
) -> list[SpanSpec]:
    """Trace A (checkout -> payment, payment errors) and trace B (checkout
    only, status OK)."""
    exception = [
        ("exception.type", EXCEPTION_TYPE),
        ("exception.message", exception_message),
    ]
    if exception_stacktrace is not None:
        exception.append(("exception.stacktrace", exception_stacktrace))
    payment_attributes = [("rpc.system", "grpc")]
    if extra_attribute is not None:
        payment_attributes.append(extra_attribute)
    return [
        SpanSpec(
            TRACE_A,
            SPAN_A1,
            "checkout",
            "PlaceOrder",
            "server",
            T0_NS,
            T0_NS + 50 * MS,
            attributes=(("rpc.system", "grpc"),),
        ),
        SpanSpec(
            TRACE_A,
            SPAN_A2,
            "payment",
            "Charge",
            "server",
            T0_NS + 10 * MS,
            T0_NS + 30 * MS,
            parent=SPAN_A1,
            status="error",
            status_message=status_message,
            attributes=tuple(payment_attributes),
            events=((T0_NS + 20 * MS, "exception", tuple(exception)),),
        ),
        SpanSpec(
            TRACE_B,
            SPAN_B1,
            "checkout",
            "PlaceOrder",
            "server",
            T0_NS + 5_000 * MS,
            T0_NS + 5_020 * MS,
            status="ok",
            attributes=(("rpc.system", "grpc"),),
        ),
    ]


def _otlp_attrs(pairs) -> list[dict[str, Any]]:
    return [{"key": k, "value": {"stringValue": v}} for k, v in pairs]


def otlp_traces(spans: list[SpanSpec]) -> bytes:
    """``{"result": TracesData}`` as the v2.23 gateway sends it: one
    resourceSpans entry per service, spans of different traces mixed."""
    by_service: dict[str, list[SpanSpec]] = {}
    for span in spans:
        by_service.setdefault(span.service, []).append(span)
    resource_spans = []
    for service, members in by_service.items():
        otlp_spans = []
        for s in members:
            item: dict[str, Any] = {
                "traceId": s.trace_id,
                "spanId": s.span_id,
                "name": s.name,
                "kind": _KIND[s.kind],
                "startTimeUnixNano": str(s.start_ns),
                "endTimeUnixNano": str(s.end_ns),
                "attributes": _otlp_attrs(s.attributes),
                "status": {},
            }
            if s.parent is not None:
                item["parentSpanId"] = s.parent
            if s.status is not None:
                item["status"] = {"code": _STATUS[s.status]}
                if s.status_message is not None:
                    item["status"]["message"] = s.status_message
            if s.events:
                item["events"] = [
                    {
                        "timeUnixNano": str(t),
                        "name": name,
                        "attributes": _otlp_attrs(attrs),
                    }
                    for t, name, attrs in s.events
                ]
            otlp_spans.append(item)
        resource_spans.append(
            {
                "resource": {
                    "attributes": _otlp_attrs(
                        [
                            ("service.name", service),
                            ("service.version", SERVICE_VERSIONS.get(service, "0.0.0")),
                        ]
                    )
                },
                "scopeSpans": [{"scope": {"name": "synthetic"}, "spans": otlp_spans}],
            }
        )
    return json.dumps({"result": {"resourceSpans": resource_spans}}).encode()


def v1_traces(spans: list[SpanSpec]) -> bytes:
    """The same spans as a Jaeger v1 ``/api/traces`` envelope."""
    traces: dict[str, dict[str, Any]] = {}
    for s in spans:
        trace = traces.setdefault(
            s.trace_id,
            {"traceID": s.trace_id, "spans": [], "processes": {}, "warnings": None},
        )
        pid = next(
            (
                p
                for p, proc in trace["processes"].items()
                if proc["serviceName"] == s.service
            ),
            None,
        )
        if pid is None:
            pid = f"p{len(trace['processes']) + 1}"
            trace["processes"][pid] = {
                "serviceName": s.service,
                "tags": [
                    {
                        "key": "service.version",
                        "type": "string",
                        "value": SERVICE_VERSIONS.get(s.service, "0.0.0"),
                    }
                ],
            }
        tags = [{"key": k, "type": "string", "value": v} for k, v in s.attributes]
        tags.append({"key": "span.kind", "type": "string", "value": s.kind})
        if s.status == "error":
            tags += [
                {"key": "otel.status_code", "type": "string", "value": "ERROR"},
                {"key": "error", "type": "bool", "value": True},
            ]
        elif s.status == "ok":
            tags.append({"key": "otel.status_code", "type": "string", "value": "OK"})
        if s.status_message is not None:
            tags.append(
                {
                    "key": "otel.status_description",
                    "type": "string",
                    "value": s.status_message,
                }
            )
        trace["spans"].append(
            {
                "traceID": s.trace_id,
                "spanID": s.span_id,
                "operationName": s.name,
                "references": (
                    [{"refType": "CHILD_OF", "traceID": s.trace_id, "spanID": s.parent}]
                    if s.parent
                    else []
                ),
                "startTime": s.start_ns // 1_000,
                "duration": (s.end_ns - s.start_ns) // 1_000,
                "tags": tags,
                "logs": [
                    {
                        "timestamp": t // 1_000,
                        "fields": [
                            {"key": "event", "type": "string", "value": name},
                            *(
                                {"key": k, "type": "string", "value": v}
                                for k, v in attrs
                            ),
                        ],
                    }
                    for t, name, attrs in s.events
                ],
                "processID": pid,
                "warnings": None,
            }
        )
    return json.dumps(
        {
            "data": list(traces.values()),
            "total": 0,
            "limit": 0,
            "offset": 0,
            "errors": None,
        }
    ).encode()


SERVICES = ["payment", "checkout"]
OPERATIONS = [
    {"name": "PlaceOrder", "spanKind": "server"},
    {"name": "GetCart", "spanKind": "client"},
]

_V3_NOT_FOUND = json.dumps(
    {"error": {"httpCode": 404, "message": "No traces found"}}
).encode()
_V1_NOT_FOUND = json.dumps(
    {
        "data": None,
        "total": 0,
        "limit": 0,
        "offset": 0,
        "errors": [{"code": 404, "msg": "trace not found"}],
    }
).encode()
_GO_404 = b"404 page not found\n"


# -- the fake Jaeger -------------------------------------------------------------


@dataclass(frozen=True)
class Sent:
    url: str
    path: str  # below the configured base path, e.g. "/api/v3/services"
    query: dict[str, list[str]]
    authorization: str | None


class _Reply:
    def __init__(self, body: bytes):
        self._stream = io.BytesIO(body)
        self.status = 200
        self.headers: dict[str, str] = {"Content-Type": "application/json"}

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def getcode(self) -> int:
        return 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@dataclass
class JaegerDouble:
    mode: str  # "v1" | "v3" | "down"
    spans: list[SpanSpec] = field(default_factory=default_spans)
    #: ``False``: the search finds nothing (v1: empty data, v3: 404).
    search_hits: bool = True
    requests: list[Sent] = field(default_factory=list)

    def _trace(self, trace_id: str) -> list[SpanSpec]:
        return [s for s in self.spans if s.trace_id == trace_id.lower()]

    def open(self, request, timeout=None):
        url = (
            request.full_url
            if isinstance(request, urllib.request.Request)
            else str(request)
        )
        auth = (
            request.get_header("Authorization")
            if isinstance(request, urllib.request.Request)
            else None
        )
        parts = urlsplit(url)
        index = parts.path.find("/api/")
        path = parts.path[index:] if index >= 0 else parts.path
        self.requests.append(Sent(url, path, parse_qs(parts.query), auth))
        status, body = self._answer(path, parse_qs(parts.query))
        if status != 200:
            raise urllib.error.HTTPError(url, status, "synthetic", {}, io.BytesIO(body))
        return _Reply(body)

    def _answer(self, path: str, query: dict[str, list[str]]) -> tuple[int, bytes]:
        if self.mode == "down":
            return 503, b"upstream unavailable\n"
        v1_routes = self.mode == "v1"
        if path == "/api/services":
            if not v1_routes:
                return 404, _GO_404
            return 200, json.dumps(
                {
                    "data": list(SERVICES),
                    "total": len(SERVICES),
                    "limit": 0,
                    "offset": 0,
                    "errors": None,
                }
            ).encode()
        if path == "/api/operations":
            if not v1_routes:
                return 404, _GO_404
            return 200, json.dumps(
                {
                    "data": list(OPERATIONS),
                    "total": len(OPERATIONS),
                    "limit": 0,
                    "offset": 0,
                    "errors": None,
                }
            ).encode()
        if path == "/api/traces":
            if not v1_routes:
                return 404, _GO_404
            return 200, v1_traces(self.spans if self.search_hits else [])
        if path.startswith("/api/traces/"):  # kept in v2.23 too
            found = self._trace(path.rsplit("/", 1)[1])
            return (200, v1_traces(found)) if found else (404, _V1_NOT_FOUND)
        if path == "/api/v3/services":
            return 200, json.dumps({"services": list(SERVICES)}).encode()
        if path == "/api/v3/operations":
            return 200, json.dumps({"operations": list(OPERATIONS)}).encode()
        if path == "/api/v3/traces":
            service = (
                query.get("query.serviceName")
                or query.get("query.service_name")
                or [""]
            )[0]
            low = (
                query.get("query.startTimeMin")
                or query.get("query.start_time_min")
                or [""]
            )[0]
            high = (
                query.get("query.startTimeMax")
                or query.get("query.start_time_max")
                or [""]
            )[0]
            if not service or parse_rfc3339(low) is None or parse_rfc3339(high) is None:
                return 400, json.dumps(
                    {"error": {"httpCode": 400, "message": "malformed parameter"}}
                ).encode()
            if not self.search_hits:
                return 404, _V3_NOT_FOUND
            return 200, otlp_traces(self.spans)
        if path.startswith("/api/v3/traces/"):
            found = self._trace(path.rsplit("/", 1)[1])
            return (200, otlp_traces(found)) if found else (404, _V3_NOT_FOUND)
        return 404, _GO_404

    def on(self, path: str) -> list[Sent]:
        return [sent for sent in self.requests if sent.path == path]


def parse_rfc3339(text: str) -> datetime | None:
    """RFC3339Nano to an aware datetime (fraction cut to microseconds)."""
    if not text:
        return None
    value = text.replace("Z", "+00:00")
    if "." in value:
        head, rest = value.split(".", 1)
        digits = "".join(ch for ch in rest if ch.isdigit())
        zone = rest[len(digits) :]
        value = f"{head}.{digits[:6].ljust(6, '0')}{zone}"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


_PORTS = iter(range(26000, 27000))


def ext_gateway(monkeypatch, tmp_path, double: JaegerDouble) -> Gateway:
    """The shipped ``otel-demo-ext`` gateway over ``double``, with a Jaeger
    base URL of its own (``/jaeger/ui`` base path, as configured in ITBench)."""
    if shutil.which("jq") is None:
        pytest.skip("jq is a startup precondition of the otel-demo-ext worker")
    ca_file = tmp_path / "synthetic-ca.pem"
    ca_file.write_text("synthetic ca placeholder\n")
    env = {
        **ext_env(str(ca_file)),
        "OPSPILOT_OTEL_JAEGER_URL": f"http://127.0.0.1:{next(_PORTS)}/jaeger/ui",
    }
    profile = select_profile(env)
    lease = Lease(
        incident_id=uuid4(),
        run_id=uuid4(),
        owner=uuid4(),
        epoch=1,
        control_generation=3,
        global_suspension_generation=1,
        target_suspension_generation=2,
    )
    clock = FakeClock(start=NOW)
    evidence = RecordingSink()
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: double)
    store = _Store(lease)
    factory = profile.executor_factory(store, evidence, clock)
    run_input = profile.face(clock).input_for(
        run_id=str(lease.run_id),
        question="Why is checkout degraded?",
        target_id=TARGET_ID,
        deadline=NOW + timedelta(minutes=10),
        model_requests=8,
    )
    return Gateway(factory(lease, run_input), double, evidence, store)  # type: ignore[arg-type]


def record_of(outcome) -> dict[str, Any]:
    """The projected record the transport produced (the evidence bytes)."""
    assert outcome.evidence is not None, outcome.model_view
    return json.loads(outcome.evidence.raw)


__all__ = [
    "EXCEPTION_MESSAGE",
    "EXCEPTION_TYPE",
    "OPERATIONS",
    "SERVICES",
    "SPAN_A1",
    "SPAN_A2",
    "SPAN_B1",
    "STATUS_MESSAGE",
    "T0_US",
    "TRACE_A",
    "TRACE_B",
    "UNKNOWN_TRACE",
    "WINDOW",
    "JaegerDouble",
    "default_spans",
    "ext_gateway",
    "parse_rfc3339",
    "record_of",
]
