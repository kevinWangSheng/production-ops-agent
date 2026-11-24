"""OTel Demo profile test doubles and recorded fixtures (M1-01).

The contract tests that lived here were removed in the 2026-11-18 test-suite
slim (``docs/tasks/2026-11-18-test-suite-slim.md``); the gateway boundaries
are in ``tests/safety``. The module keeps its name because
``scripts/f7_hostile_live.py`` imports these doubles from it.


Written from the C3 §8 tool-gateway contract, ``PRODUCT-CONSTRAINTS.md`` and
the public interface of ``opspilot.tools.otel_demo`` / ``opspilot.tools
.profiles`` -- not from the implementation. Hermetic: no network, no
PostgreSQL. Real backend responses recorded under
``tests/fixtures/otel_demo/`` are served by a fake ``urllib`` opener that
records the URL, method and headers it was asked for.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from opspilot.investigation.inputs import ToolFace
from opspilot.investigation.loop import DISCIPLINE_VARIANT
from opspilot.persistence import Lease
from opspilot.tools import (
    ReadOnlyToolExecutor,
    ToolRequest,
    TransportRequest,
    Window,
)
from opspilot.tools.otel_demo import (
    CREDENTIAL_REF,
    SOURCE,
    TARGET_ID,
    TOOL_SCHEMAS,
    OtelDemoConfig,
    OtelDemoTransport,
    otel_demo_executor_factory,
    project_traces,
)
from tests.m1_tool_support import FakeClock, RecordingSink

FIXTURES = Path(__file__).parent / "fixtures" / "otel_demo"
_WINDOW_LINE = (FIXTURES / "window.txt").read_text().split()
WINDOW_START = datetime.fromtimestamp(int(_WINDOW_LINE[1]), timezone.utc)
WINDOW_END = datetime.fromtimestamp(int(_WINDOW_LINE[2]), timezone.utc)
WINDOW = Window(WINDOW_START, WINDOW_END)
# The executor refuses source timestamps later than the fetch finish, so the
# trusted clock sits after the recorded window.
NOW = WINDOW_END + timedelta(minutes=2)

PROMETHEUS_URL = "http://127.0.0.1:19090"
JAEGER_URL = "http://127.0.0.1:16686/jaeger/ui"
GOOD_EXPR = "sum by(service_name) (increase(traces_span_metrics_calls_total[5m]))"
BAD_EXPR = "sum(rate(traces_span_metrics_calls_total[5m] offset 1h))"

CHECKOUT_BODY = (FIXTURES / "prometheus-range-checkout.json").read_bytes()
EMPTY_BODY = (FIXTURES / "prometheus-range-empty.json").read_bytes()
BAD_REQUEST_BODY = (FIXTURES / "prometheus-range-400.json").read_bytes()
CHECKOUT_TRACES = (FIXTURES / "jaeger-traces-checkout-limit2.json").read_bytes()
PAYMENT_TRACES = (FIXTURES / "jaeger-traces-payment-limit2.json").read_bytes()


def _sample_bounds(body: bytes) -> tuple[datetime, datetime]:
    stamps = [
        int(point[0])
        for series in json.loads(body)["data"]["result"]
        for point in series["values"]
    ]
    return (
        datetime.fromtimestamp(min(stamps), timezone.utc),
        datetime.fromtimestamp(max(stamps), timezone.utc),
    )


CHECKOUT_FIRST, CHECKOUT_LAST = _sample_bounds(CHECKOUT_BODY)


# -- doubles -----------------------------------------------------------------


class _Reply:
    """What ``opener.open`` hands back: a context manager over a response."""

    def __init__(self, body: bytes, status: int = 200):
        self._buffer = io.BytesIO(body)
        self.status = status

    def read(self, n: int = -1) -> bytes:
        return self._buffer.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Serves recorded bytes by route; records every request it was asked for.

    ``routes`` maps a URL path suffix to a body, an ``int`` HTTP error
    status (served as ``HTTPError`` with ``BAD_REQUEST_BODY``), or an
    exception instance to raise. ``by_query`` narrows a Prometheus route by
    the ``query`` parameter.
    """

    def __init__(self, routes=None, by_query=None):
        self.routes = dict(routes or {})
        self.by_query = dict(by_query or {})
        self.requests = []
        self.timeouts = []

    @property
    def called(self) -> bool:
        return bool(self.requests)

    def urls(self) -> list[str]:
        return [_url(r) for r in self.requests]

    def open(self, request, timeout=None):
        self.requests.append(request)
        self.timeouts.append(timeout)
        url = _url(request)
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        served = None
        expr = query.get("query", [None])[0]
        if expr is not None and expr in self.by_query:
            served = self.by_query[expr]
        else:
            for suffix, value in self.routes.items():
                if parts.path.endswith(suffix):
                    served = value
                    break
        if served is None:
            raise AssertionError(f"unexpected request: {url}")
        if isinstance(served, BaseException):
            raise served
        if isinstance(served, int):
            raise urllib.error.HTTPError(
                url, served, "error", {}, io.BytesIO(BAD_REQUEST_BODY)
            )
        return _Reply(served)


def _url(request) -> str:
    return request.full_url if isinstance(request, urllib.request.Request) else request


def _method(request) -> str:
    if isinstance(request, urllib.request.Request):
        return request.get_method()
    return "GET"


def _headers(request) -> dict[str, str]:
    if not isinstance(request, urllib.request.Request):
        return {}
    merged = {**request.headers, **request.unredirected_hdrs}
    return {key.lower(): value for key, value in merged.items()}


class FakeStore:
    """The three ``DurableStore`` reads/writes the factory's wiring touches."""

    def __init__(self, lease: Lease, *, deadline: datetime, control=None):
        self.lease = lease
        self.deadline = deadline
        self.control = control or {
            "incident_generation": lease.control_generation,
            "incident_state": "running",
            "global_suspended": False,
            "global_generation": lease.global_suspension_generation,
            "target_suspended": False,
            "target_generation": lease.target_suspension_generation,
        }
        self.charges = []
        self.control_reads = 0

    def rebuild(self, incident_id):
        assert incident_id == self.lease.incident_id
        return {
            "run": {
                "run_id": self.lease.run_id,
                "deadline": self.deadline,
                "tool_operations_used": 0,
                "tool_seconds_used": 0.0,
            }
        }

    def charge_tool(
        self,
        lease,
        operation_id,
        seconds,
        *,
        dispatch_id,
    ):
        self.charges.append((operation_id, seconds, dispatch_id))

    def control_state(self, incident_id):
        assert incident_id == self.lease.incident_id
        self.control_reads += 1
        if isinstance(self.control, Exception):
            raise self.control
        return dict(self.control)


def _lease() -> Lease:
    return Lease(
        incident_id=uuid4(),
        run_id=uuid4(),
        owner=uuid4(),
        epoch=1,
        control_generation=3,
        global_suspension_generation=1,
        target_suspension_generation=2,
    )


def _fixed_window_evidence_context(run_id: str) -> dict:
    """Same shape as ``otel_demo_face``'s own evidence context, but with an
    explicit, fixed ``window`` instead of one computed from the clock.

    B2 (docs/tasks/2026-09-28-m1-01-upstream-alignment-b.md) moved the
    runner's default authorization frame from 300 s to 24 h
    (``OBSERVATION_SECONDS``), so ``otel_demo_face``'s own default no longer
    matches this file's recorded fixture responses, which were captured for
    exactly ``WINDOW``. Built directly rather than through ``otel_demo_face``
    so every fixture-replay test below stays pinned to ``WINDOW`` regardless
    of that frame default -- the same reason
    ``tests/test_m1_upstream_alignment_b_contract.py`` declares its own
    ``_evidence_context`` helper instead of using ``otel_demo_face``.
    """
    return {
        "type": "opspilot-evidence-context-v4",
        "run_id": run_id,
        "time_policies": [
            {
                "id": "policy-window-1",
                "mode": "historical_window",
                "all_authorized_targets": True,
                "window": WINDOW.as_json(),
                "reference_rule": "response_received_at",
            }
        ],
    }


def _input(run_id: str):
    """A Run input whose ``policy-window-1`` is exactly the recorded window."""
    face = ToolFace(
        tool_schemas=TOOL_SCHEMAS,
        variant_id=DISCIPLINE_VARIANT,
        evidence_context=_fixed_window_evidence_context,
    )
    return face.input_for(
        run_id=run_id,
        question="Why is checkout erroring?",
        target_id=TARGET_ID,
        deadline=NOW + timedelta(minutes=10),
        model_requests=2,
    )


def _executor(monkeypatch, opener: FakeOpener, *, token=None, store=None, clock=None):
    """A real executor from the profile factory with the fake opener installed.

    The factory does not take an opener, so the default one is replaced at
    ``urllib.request.build_opener``; ``opener.build_calls`` keeps what the
    profile asked the builder for.
    """
    store = store or FakeStore(_lease(), deadline=NOW + timedelta(minutes=10))
    lease = store.lease
    clock = clock or FakeClock(start=NOW)
    sink = RecordingSink()
    build_calls: list[tuple] = []

    def build_opener(*handlers):
        build_calls.append(handlers)
        return opener

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    config = OtelDemoConfig(
        prometheus_url=PROMETHEUS_URL, jaeger_url=JAEGER_URL, token=token
    )
    factory = otel_demo_executor_factory(
        store, evidence=sink, clock=clock, config=config
    )
    executor = factory(lease, _input(str(lease.run_id)))
    assert isinstance(executor, ReadOnlyToolExecutor)
    opener.build_calls = build_calls  # type: ignore[attr-defined]
    return executor, sink, lease, store


def _call(tool: str, params: dict, *, index: int = 0, window: Window = WINDOW):
    return ToolRequest(
        step_id="step-1",
        tool_index=index,
        tool_name=tool,
        target_ref=TARGET_ID,
        params=params,
        window=window.as_json(),
    )


def _transport_request(tool: str, params: dict, *, window: Window = WINDOW, **over):
    fields = {
        "operation_id": "step-1-t0",
        "source": SOURCE,
        "verb": "query",
        "endpoint": PROMETHEUS_URL,
        "selector": {
            "opspilot.integration.id": TARGET_ID,
            "traces_endpoint": JAEGER_URL,
        },
        "params": params,
        "window": window,
        "timeout_seconds": 30.0,
        "max_result_bytes": 1_048_576,
        "credential_ref": CREDENTIAL_REF,
        "tool": tool,
    }
    fields.update(over)
    return TransportRequest(**fields)


def _transport(opener: FakeOpener, token=None) -> OtelDemoTransport:
    return OtelDemoTransport(credentials={CREDENTIAL_REF: token}, opener=opener)


def _strings(value, out=None) -> list[str]:
    """Every string reachable inside a nested outcome/view/record."""
    out = [] if out is None else out
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, bytes):
        out.append(value.decode("utf-8", "replace"))
    elif isinstance(value, dict):
        for key, item in value.items():
            _strings(key, out)
            _strings(item, out)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _strings(item, out)
    return out


# -- constants and the model-visible face (C3 §8) -----------------------------


def _model_visible_texts():
    for schema in TOOL_SCHEMAS:
        function = schema["function"]
        yield function["name"], function["description"]
        for name, spec in function["parameters"]["properties"].items():
            yield f"{function['name']}.{name}", spec["description"]


# -- face, versions, scope window ---------------------------------------------


# -- configuration ---------------------------------------------------------------


# -- promql guard -------------------------------------------------------------


# -- project_traces --------------------------------------------------------------


def _projected(body: bytes, *, service: str, limit: int, window: Window = WINDOW):
    return project_traces(
        json.loads(body),
        service=service,
        limit=limit,
        window=window,
        source_sha256=sha256(body).hexdigest(),
        source_bytes=len(body),
    )


def _spans_by_service(body: bytes) -> dict[str, int]:
    counts: dict[str, int] = {}
    for trace in json.loads(body)["data"]:
        for span in trace["spans"]:
            name = trace["processes"][span["processID"]]["serviceName"]
            counts[name] = counts.get(name, 0) + 1
    return counts


def _synthetic_payload(window: Window):
    base = int(window.start.timestamp() * 1e6) + 10_000_000
    long_value = "x" * 2000
    spans = [
        # (service, operation, start_us, duration_us, tags)
        ("checkout", "long-checkout", base, 900_000, []),
        ("payment", "charge", base + 100, 5_000, []),
        (
            "frontend",
            "erroring",
            base + 200,
            1_000,
            [
                {"key": "otel.status_code", "type": "string", "value": "ERROR"},
                {"key": "error", "type": "bool", "value": True},
                {
                    "key": "otel.status_description",
                    "type": "string",
                    "value": long_value,
                },
                {"key": "exception.message", "type": "string", "value": long_value},
                {"key": "exception.type", "type": "string", "value": "Boom"},
                {"key": "exception.stacktrace", "type": "string", "value": long_value},
                {"key": "http.response.status_code", "type": "int64", "value": 500},
                {"key": "rpc.grpc.status_code", "type": "int64", "value": 13},
            ],
        ),
        ("frontend", "short", base + 300, 2_000, []),
        # Starts before the window: outside.
        ("cart", "early", int(window.start.timestamp() * 1e6) - 5_000_000, 1_000, []),
    ]
    processes = {
        f"p{i}": {"serviceName": s[0], "tags": []} for i, s in enumerate(spans)
    }
    trace_spans = []
    for i, (service, operation, start_us, duration, tags) in enumerate(spans):
        trace_spans.append(
            {
                "traceID": "t1",
                "spanID": f"s{i}",
                "operationName": operation,
                "references": []
                if i == 0
                else [{"refType": "CHILD_OF", "traceID": "t1", "spanID": "s0"}],
                "startTime": start_us,
                "duration": duration,
                "tags": tags,
                "logs": [],
                "processID": f"p{i}",
                "warnings": None,
            }
        )
    # A second-trace parent that is not among the returned spans.
    trace_spans[3]["references"] = [
        {"refType": "CHILD_OF", "traceID": "t1", "spanID": "missing"}
    ]
    return {
        "data": [{"traceID": "t1", "spans": trace_spans, "processes": processes}],
        "total": 0,
        "limit": 0,
        "offset": 0,
        "errors": None,
    }


# -- transport: routing, refusals, error classification ---------------------------


# -- DurableControl ----------------------------------------------------------------


# -- the executor built by the profile factory ---------------------------------------


# -- profile selection ----------------------------------------------------------------
