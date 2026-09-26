"""Contract tests for the OTel Demo read-only tool profile (M1-01).

Written from the C3 §8 tool-gateway contract, ``PRODUCT-CONSTRAINTS.md`` and
the public interface of ``opspilot.tools.otel_demo`` / ``opspilot.tools
.profiles`` -- not from the implementation. Hermetic: no network, no
PostgreSQL. Real backend responses recorded under
``tests/fixtures/otel_demo/`` are served by a fake ``urllib`` opener that
records the URL, method and headers it was asked for.
"""

from __future__ import annotations

import inspect
import io
import json
import socket
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest

from opspilot.investigation.context import ContextError
from opspilot.investigation.inputs import ToolFace
from opspilot.investigation.loop import investigation_versions
from opspilot.persistence import Lease, PersistenceError
from opspilot.tools import (
    ERROR_REASONS,
    ControlUnavailable,
    ReadOnlyToolExecutor,
    ToolContractError,
    ToolRequest,
    TransportError,
    TransportRequest,
    TransportResponse,
    TransportResultTooLarge,
    TransportTimeout,
    TransportUnavailable,
    Window,
)
from opspilot.tools import fixture as fixture_profile
from opspilot.tools.otel_demo import (
    CREDENTIAL_REF,
    METRICS_TOOL,
    OBSERVATION_SECONDS,
    SERVICES,
    SOURCE,
    TARGET_ID,
    TOOL_SCHEMA_REVISION,
    TOOL_SCHEMAS,
    TRACE_PROJECTION,
    TRACES_TOOL,
    DurableControl,
    OtelDemoConfig,
    OtelDemoTransport,
    otel_demo_executor_factory,
    otel_demo_face,
    otel_demo_versions,
    project_traces,
    promql_problem,
    scope_window,
)
from opspilot.tools.profiles import PROFILE_ENV, ToolProfile, select_profile
from opspilot.tools.registry import canonical, redact_credentials
from opspilot.worker_main import build_loop
from tests.m1_tool_support import TRANSPORT_ONLY_MARKER, FakeClock, RecordingSink

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
        max_operations,
        max_tool_seconds,
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


def _input(run_id: str, *, face_clock=None):
    """A Run input whose ``policy-window-1`` is exactly the recorded window."""
    face = otel_demo_face(face_clock or FakeClock(start=WINDOW_END))
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


def test_profile_constants_match_the_contract():
    assert TARGET_ID == "m0-otel-20260909"
    assert SOURCE == "otel-demo"
    assert METRICS_TOOL == "metrics_range_query"
    assert TRACES_TOOL == "traces_search"
    assert CREDENTIAL_REF == "otel-demo-ro"
    assert OBSERVATION_SECONDS == 300
    assert TRACE_PROJECTION == "otel-demo-traces-v1"
    assert isinstance(SERVICES, tuple) and len(SERVICES) == 17
    assert set(SERVICES) == {
        "accounting",
        "ad",
        "cart",
        "checkout",
        "currency",
        "email",
        "fraud-detection",
        "frontend",
        "frontend-proxy",
        "image-provider",
        "kafka",
        "load-generator",
        "payment",
        "product-catalog",
        "quote",
        "recommendation",
        "shipping",
    }
    assert isinstance(TOOL_SCHEMA_REVISION, str)
    assert TOOL_SCHEMA_REVISION.startswith("otel-demo-")
    assert TOOL_SCHEMA_REVISION != fixture_profile.TOOL_SCHEMA_REVISION


def test_tool_schemas_are_two_function_schemas_named_as_registered():
    assert isinstance(TOOL_SCHEMAS, tuple) and len(TOOL_SCHEMAS) == 2
    by_name = {schema["function"]["name"]: schema for schema in TOOL_SCHEMAS}
    assert set(by_name) == {METRICS_TOOL, TRACES_TOOL}
    for schema in TOOL_SCHEMAS:
        assert schema["type"] == "function"
        parameters = schema["function"]["parameters"]
        assert parameters["type"] == "object"
        assert set(parameters["required"]) <= set(parameters["properties"])
    metrics = by_name[METRICS_TOOL]["function"]["parameters"]
    assert "expr" in metrics["required"]
    assert set(metrics["properties"]) == {"expr", "step_seconds"}
    traces = by_name[TRACES_TOOL]["function"]["parameters"]
    assert "service" in traces["required"]
    assert set(traces["properties"]) == {"service", "limit"}


def _model_visible_texts():
    for schema in TOOL_SCHEMAS:
        function = schema["function"]
        yield function["name"], function["description"]
        for name, spec in function["parameters"]["properties"].items():
            yield f"{function['name']}.{name}", spec["description"]


def test_model_visible_face_carries_no_endpoint_or_credential_material():
    """C3 §8 保密: no credential, auth material, endpoint or credential
    handle in the text the model sees; the registry's own prose rule."""
    for where, text in _model_visible_texts():
        assert text.strip(), where
        assert "://" not in text, where
        assert redact_credentials(text) == text, where
        lowered = text.lower()
        for word in ("bearer", "authorization", CREDENTIAL_REF, "password", "secret"):
            assert word not in lowered, (where, word)
        for host in ("127.0.0.1", "19090", "16686", "localhost"):
            assert host not in text, (where, host)


def test_model_visible_face_inlines_the_service_enumeration():
    """C3 §8: a finite enumeration is inlined and says other values error."""
    traces = next(
        s["function"]["description"]
        for s in TOOL_SCHEMAS
        if s["function"]["name"] == TRACES_TOOL
    )
    for service in SERVICES:
        assert f'"{service}"' in traces, service
    assert "error" in traces.lower()


def test_model_visible_face_states_limits_and_what_it_cannot_prove():
    """The five C3 §8 facets are registered as a ``ToolDescription`` (the
    registry raises otherwise; see the factory test) and the text the model
    receives states the cap/truncation semantics and the misreading to
    refuse for both tools."""
    for name, text in (
        (s["function"]["name"], s["function"]["description"]) for s in TOOL_SCHEMAS
    ):
        lowered = text.lower()
        assert "truncated" in lowered, name
        assert "not" in lowered, name  # a cannot_prove clause
    metrics = dict(_model_visible_texts())[METRICS_TOOL].lower()
    assert "unknown" in metrics and "zero" in metrics
    traces = dict(_model_visible_texts())[TRACES_TOOL].lower()
    assert "sample" in traces


# -- face, versions, scope window ---------------------------------------------


def test_face_evidence_context_is_the_300s_window_ending_at_the_clock():
    now = datetime(2026, 9, 26, 14, 3, 10, tzinfo=timezone.utc)
    face = otel_demo_face(FakeClock(start=now))
    assert isinstance(face, ToolFace)
    assert tuple(s["function"]["name"] for s in face.tool_schemas) == (
        METRICS_TOOL,
        TRACES_TOOL,
    )
    context = face.evidence_context("run-1")
    assert context == {
        "type": "opspilot-evidence-context-v4",
        "run_id": "run-1",
        "time_policies": [
            {
                "id": "policy-window-1",
                "mode": "historical_window",
                "all_authorized_targets": True,
                "window": {
                    "start": (now - timedelta(seconds=300)).isoformat(),
                    "end": now.isoformat(),
                },
                "reference_rule": "response_received_at",
            }
        ],
    }


def test_face_window_uses_utc_whole_seconds():
    now = datetime(2026, 9, 26, 16, 3, 10, 654321, tzinfo=timezone(timedelta(hours=2)))
    context = otel_demo_face(FakeClock(start=now)).evidence_context("run-2")
    window = context["time_policies"][0]["window"]
    start, end = (datetime.fromisoformat(window[k]) for k in ("start", "end"))
    assert end.utcoffset() == timedelta(0) and start.utcoffset() == timedelta(0)
    assert end.microsecond == 0 and start.microsecond == 0
    assert abs((end - now).total_seconds()) < 1
    assert end - start == timedelta(seconds=OBSERVATION_SECONDS)


def test_versions_are_the_loop_versions_plus_this_face_revision():
    versions = otel_demo_versions()
    assert versions == {
        **investigation_versions(),
        "tool_schema_revision": TOOL_SCHEMA_REVISION,
    }
    assert otel_demo_versions() == versions
    assert all(isinstance(v, str) and v for v in versions.values())
    assert versions != fixture_profile.fixture_versions()


def test_scope_window_is_the_policy_window_of_the_input():
    input = _input("run-3")
    assert scope_window(input) == WINDOW


def test_scope_window_fails_closed_without_a_policy_window():
    input = _input("run-4")
    from dataclasses import replace

    for context in (
        None,
        {"type": "opspilot-evidence-context-v4", "run_id": "run-4"},
        {
            "type": "opspilot-evidence-context-v4",
            "run_id": "run-4",
            "time_policies": [{"id": "other", "window": WINDOW.as_json()}],
        },
        {
            "type": "opspilot-evidence-context-v4",
            "run_id": "run-4",
            "time_policies": [
                {"id": "policy-window-1", "window": {"start": "soon", "end": "later"}}
            ],
        },
    ):
        with pytest.raises(ContextError) as caught:
            scope_window(replace(input, evidence_context=context))
        assert caught.value.code == "SCOPE_WINDOW_MISSING", context


# -- configuration ---------------------------------------------------------------


def test_config_defaults_and_env():
    config = OtelDemoConfig()
    assert config == OtelDemoConfig(
        prometheus_url="http://127.0.0.1:19090",
        jaeger_url="http://127.0.0.1:16686/jaeger/ui",
        token=None,
    )
    with pytest.raises((AttributeError, TypeError)):
        config.prometheus_url = "http://elsewhere"  # type: ignore[misc]
    assert OtelDemoConfig.from_env({}) == config
    loaded = OtelDemoConfig.from_env(
        {
            "OPSPILOT_OTEL_PROMETHEUS_URL": "http://prom.lab:9090",
            "OPSPILOT_OTEL_JAEGER_URL": "http://jaeger.lab:16686/jaeger/ui",
            "OPSPILOT_OTEL_TOKEN": TRANSPORT_ONLY_MARKER,
            "UNRELATED": "x",
        }
    )
    assert loaded == OtelDemoConfig(
        prometheus_url="http://prom.lab:9090",
        jaeger_url="http://jaeger.lab:16686/jaeger/ui",
        token=TRANSPORT_ONLY_MARKER,
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://user:pw@127.0.0.1:19090",
        "http://127.0.0.1:19090/?token=x",
        "http://127.0.0.1:19090/#frag",
    ],
)
@pytest.mark.parametrize("field", ["prometheus_url", "jaeger_url"])
def test_config_refuses_credential_bearing_urls(url, field):
    with pytest.raises(ToolContractError):
        OtelDemoConfig(**{field: url})


# -- promql guard -------------------------------------------------------------


@pytest.mark.parametrize(
    "expr",
    [
        "",
        "x" * 2001,
        "sum(rate(m[5m] @ 1790431390))",
        BAD_EXPR,
        "rate(m[5m] OFFSET 5m)",
        "rate(m[1h])",
        "rate(m[301s])",
        "rate(m[10m])",
        "rate(m[5x])",
        "rate(m[5])",
        "rate(m[1d])",
        "rate(m[1.5m])",
    ],
)
def test_promql_problem_refuses_reads_outside_the_window(expr):
    assert promql_problem(expr, 300) == "QUERY_OUT_OF_WINDOW"


@pytest.mark.parametrize(
    "expr",
    [
        GOOD_EXPR,
        "rate(m[300s])",
        "rate(m[5m])",
        "sum by(service_name) (rate(m[2m])) / sum by(service_name) (rate(n[2m]))",
        'app_payment_transactions_total{service_name="payment"}',
        "x" * 2000,
    ],
)
def test_promql_problem_accepts_in_window_expressions(expr):
    assert promql_problem(expr, 300) is None


def test_promql_problem_range_bound_is_the_window_length():
    assert promql_problem("rate(m[10m])", 600) is None
    assert promql_problem("rate(m[11m])", 600) == "QUERY_OUT_OF_WINDOW"


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


def test_project_traces_records_the_recorded_checkout_search():
    record, start, end = _projected(CHECKOUT_TRACES, service="checkout", limit=2)
    raw = json.loads(CHECKOUT_TRACES)
    total_spans = sum(len(t["spans"]) for t in raw["data"])
    assert record["source"] == "jaeger"
    assert record["projection"] == TRACE_PROJECTION
    assert record["query"] == {
        "service": "checkout",
        "limit": 2,
        "window": WINDOW.as_json(),
    }
    assert record["source_response_sha256"] == sha256(CHECKOUT_TRACES).hexdigest()
    assert record["source_response_bytes"] == len(CHECKOUT_TRACES)
    assert record["backend_returned_trace_count"] == 2
    assert record["backend_returned_span_count"] == total_spans
    assert sorted(record["backend_trace_ids"]) == sorted(
        t["traceID"] for t in raw["data"]
    )
    counts = record["backend_counts_by_service"]
    expected = _spans_by_service(CHECKOUT_TRACES)
    assert {name: c["span_count"] for name, c in counts.items()} == expected
    for entry in counts.values():
        assert set(entry) >= {
            "span_count",
            "error_spans_by_listed_status_tags",
            "max_duration_us",
        }
        assert (
            entry["max_duration_us"] >= 0
            and entry["error_spans_by_listed_status_tags"] >= 0
        )
    assert record["spans_outside_window"] == 0
    assert record["sampled_span_count"] == 20 == len(record["data"]["sampled_spans"])
    assert record["omitted_span_count"] == total_spans - 20
    assert record["incomplete"] is True  # 2 traces returned for limit 2
    # The requested service's spans come first; there are more than 20 of them.
    assert expected["checkout"] > 20
    assert all(s["service"] == "checkout" for s in record["data"]["sampled_spans"])
    for span in record["data"]["sampled_spans"]:
        assert set(span) >= {
            "trace_id",
            "span_id",
            "service",
            "operation",
            "start_us",
            "duration_us",
            "error_by_visible_tags",
            "status_tags",
            "parent_references",
            "error_details",
        }
        assert span["trace_id"] in record["backend_trace_ids"]
        assert isinstance(span["error_by_visible_tags"], bool)
        assert len(span["error_details"]) <= 4
        for reference in span["parent_references"]:
            assert isinstance(reference["parent_is_visible"], bool)
    # Time bounds: earliest start / latest end across every returned span.
    all_spans = [s for t in raw["data"] for s in t["spans"]]
    earliest = min(s["startTime"] for s in all_spans)
    latest = max(s["startTime"] + s["duration"] for s in all_spans)
    assert start == datetime.fromtimestamp(earliest / 1e6, timezone.utc)
    assert end == datetime.fromtimestamp(latest / 1e6, timezone.utc)
    assert WINDOW.start <= start <= end <= WINDOW.end


def test_project_traces_is_complete_when_fewer_traces_than_the_limit_came_back():
    record, _, _ = _projected(CHECKOUT_TRACES, service="checkout", limit=3)
    assert record["incomplete"] is False
    assert record["query"]["limit"] == 3


def test_project_traces_payment_search_puts_payment_spans_first():
    record, _, _ = _projected(PAYMENT_TRACES, service="payment", limit=2)
    counts = _spans_by_service(PAYMENT_TRACES)
    sampled = record["data"]["sampled_spans"]
    assert len(sampled) == 20
    leading = [s["service"] for s in sampled[: counts["payment"]]]
    assert leading == ["payment"] * counts["payment"]
    rest = sampled[counts["payment"] :]
    assert all(s["service"] != "payment" for s in rest)
    # No error span in this recording: the remaining order is longest first.
    assert all(not s["error_by_visible_tags"] for s in rest)
    durations = [s["duration_us"] for s in rest]
    assert durations == sorted(durations, reverse=True)
    assert record["source_response_sha256"] == sha256(PAYMENT_TRACES).hexdigest()


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


def test_project_traces_orders_requested_service_then_errors_then_longest():
    payload = _synthetic_payload(WINDOW)
    body = canonical(payload).encode()
    record, start, end = project_traces(
        payload,
        service="payment",
        limit=10,
        window=WINDOW,
        source_sha256=sha256(body).hexdigest(),
        source_bytes=len(body),
    )
    sampled = record["data"]["sampled_spans"]
    assert [s["operation"] for s in sampled] == [
        "charge",
        "erroring",
        "long-checkout",
        "short",
        "early",
    ]
    assert record["backend_returned_trace_count"] == 1
    assert record["backend_returned_span_count"] == 5
    assert record["sampled_span_count"] == 5 and record["omitted_span_count"] == 0
    assert record["incomplete"] is False
    assert record["spans_outside_window"] == 1
    erroring = sampled[1]
    assert erroring["error_by_visible_tags"] is True
    assert (
        record["backend_counts_by_service"]["frontend"][
            "error_spans_by_listed_status_tags"
        ]
        == 1
    )
    assert record["backend_counts_by_service"]["checkout"]["max_duration_us"] == 900_000
    assert 1 <= len(erroring["error_details"]) <= 4
    for detail in erroring["error_details"]:
        assert isinstance(detail["value_truncated"], bool)
        assert len(str(detail["value"]).encode("utf-8")) <= 600
        if len(str(detail["value"]).encode("utf-8")) == 600:
            assert detail["value_truncated"] is True
    assert any(d["value_truncated"] for d in erroring["error_details"])
    # Parent visibility is decided against the returned spans.
    assert sampled[0]["parent_references"][0]["parent_is_visible"] is True
    assert sampled[3]["parent_references"][0]["parent_is_visible"] is False
    # Bounds are clipped to the window: the early span starts before it.
    assert start == WINDOW.start
    assert end == datetime.fromtimestamp(
        (int(WINDOW.start.timestamp() * 1e6) + 10_000_000 + 900_000) / 1e6, timezone.utc
    )


def test_project_traces_without_spans_reports_no_bounds():
    body = b'{"data":[],"total":0,"limit":0,"offset":0,"errors":null}'
    record, start, end = _projected(body, service="checkout", limit=2)
    assert (start, end) == (None, None)
    assert record["backend_returned_trace_count"] == 0
    assert record["backend_returned_span_count"] == 0
    assert record["data"]["sampled_spans"] == []
    assert record["incomplete"] is False


# -- transport: routing, refusals, error classification ---------------------------


def test_transport_metrics_request_and_response():
    opener = FakeOpener(by_query={GOOD_EXPR: CHECKOUT_BODY})
    response = _transport(opener).fetch(
        _transport_request(METRICS_TOOL, {"expr": GOOD_EXPR})
    )
    assert isinstance(response, TransportResponse)
    assert response.body == CHECKOUT_BODY
    assert response.source_status is None
    assert response.data_as_of == CHECKOUT_LAST
    assert response.source_start_at == CHECKOUT_FIRST
    assert response.source_end_at == CHECKOUT_LAST
    (request,) = opener.requests
    parts = urlsplit(_url(request))
    assert f"{parts.scheme}://{parts.netloc}" == PROMETHEUS_URL
    assert parts.path == "/api/v1/query_range"
    query = parse_qs(parts.query)
    assert query["query"] == [GOOD_EXPR]
    # Epoch seconds of the request window (Prometheus accepts "1790431090"
    # and "1790431090.000" alike; the instant is what the contract fixes).
    assert float(query["start"][0]) == WINDOW.start.timestamp()
    assert float(query["end"][0]) == WINDOW.end.timestamp()
    assert query["step"] == ["30"]
    assert "timeout" in query
    assert _method(request) == "GET"
    assert opener.timeouts == [30.0]
    assert "authorization" not in _headers(request)


def test_transport_metrics_honours_step_seconds():
    opener = FakeOpener(by_query={GOOD_EXPR: CHECKOUT_BODY})
    _transport(opener).fetch(
        _transport_request(METRICS_TOOL, {"expr": GOOD_EXPR, "step_seconds": 60})
    )
    assert parse_qs(urlsplit(opener.urls()[0]).query)["step"] == ["60"]


@pytest.mark.parametrize("step", [14, 0, -1, 301, "30", 30.0, True])
def test_transport_refuses_a_bad_step_without_sending(step):
    opener = FakeOpener(by_query={GOOD_EXPR: CHECKOUT_BODY})
    response = _transport(opener).fetch(
        _transport_request(METRICS_TOOL, {"expr": GOOD_EXPR, "step_seconds": step})
    )
    assert response.source_status == "INVALID_STEP"
    assert not opener.called


@pytest.mark.parametrize("step", [15, 300])
def test_transport_step_bounds_are_inclusive(step):
    opener = FakeOpener(by_query={GOOD_EXPR: CHECKOUT_BODY})
    response = _transport(opener).fetch(
        _transport_request(METRICS_TOOL, {"expr": GOOD_EXPR, "step_seconds": step})
    )
    assert response.source_status is None and opener.called


def test_transport_never_sends_a_query_the_guard_refuses():
    opener = FakeOpener(routes={"/api/v1/query_range": CHECKOUT_BODY})
    response = _transport(opener).fetch(
        _transport_request(METRICS_TOOL, {"expr": BAD_EXPR})
    )
    assert response.source_status == "QUERY_OUT_OF_WINDOW"
    assert not opener.called


def test_transport_empty_result_has_no_bounds():
    opener = FakeOpener(by_query={GOOD_EXPR: EMPTY_BODY})
    response = _transport(opener).fetch(
        _transport_request(METRICS_TOOL, {"expr": GOOD_EXPR})
    )
    assert response.body == EMPTY_BODY and response.source_status is None
    assert response.source_start_at is None and response.source_end_at is None
    assert response.data_as_of is None


@pytest.mark.parametrize("status", [400, 422])
def test_transport_client_errors_come_back_as_source_status(status):
    opener = FakeOpener(routes={"/api/v1/query_range": status})
    response = _transport(opener).fetch(
        _transport_request(METRICS_TOOL, {"expr": "sum("})
    )
    assert response.source_status == str(status)
    assert response.body == BAD_REQUEST_BODY
    assert response.data_as_of is None
    assert response.source_start_at is None and response.source_end_at is None


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_transport_server_errors_are_unavailable(status):
    opener = FakeOpener(routes={"/api/v1/query_range": status})
    with pytest.raises(TransportUnavailable):
        _transport(opener).fetch(_transport_request(METRICS_TOOL, {"expr": GOOD_EXPR}))


def test_transport_connection_failure_is_unavailable():
    opener = FakeOpener(
        routes={"/api/v1/query_range": urllib.error.URLError(ConnectionRefusedError())}
    )
    with pytest.raises(TransportUnavailable):
        _transport(opener).fetch(_transport_request(METRICS_TOOL, {"expr": GOOD_EXPR}))


@pytest.mark.parametrize(
    "error",
    [
        socket.timeout("timed out"),
        TimeoutError(),
        urllib.error.URLError(socket.timeout()),
    ],
)
def test_transport_timeouts_are_timeouts(error):
    opener = FakeOpener(routes={"/api/v1/query_range": error})
    with pytest.raises(TransportTimeout):
        _transport(opener).fetch(_transport_request(METRICS_TOOL, {"expr": GOOD_EXPR}))


def test_transport_oversized_body_is_too_large():
    opener = FakeOpener(by_query={GOOD_EXPR: CHECKOUT_BODY})
    with pytest.raises(TransportResultTooLarge):
        _transport(opener).fetch(
            _transport_request(METRICS_TOOL, {"expr": GOOD_EXPR}, max_result_bytes=100)
        )


def test_transport_traces_request_and_projected_body():
    opener = FakeOpener(routes={"/api/traces": CHECKOUT_TRACES})
    response = _transport(opener).fetch(
        _transport_request(TRACES_TOOL, {"service": "checkout", "limit": 2})
    )
    (request,) = opener.requests
    parts = urlsplit(_url(request))
    assert _url(request).startswith(JAEGER_URL + "/api/traces")
    assert parts.path.endswith("/api/traces")
    query = parse_qs(parts.query)
    assert query["service"] == ["checkout"]
    assert query["limit"] == ["2"]
    assert query["start"] == [str(int(WINDOW.start.timestamp()) * 1_000_000)]
    assert query["end"] == [str(int(WINDOW.end.timestamp()) * 1_000_000)]
    assert _method(request) == "GET"
    expected, start, end = _projected(CHECKOUT_TRACES, service="checkout", limit=2)
    assert response.body != CHECKOUT_TRACES
    assert response.body == canonical(expected).encode("utf-8")
    assert json.loads(response.body) == expected
    assert response.source_status is None
    assert (response.source_start_at, response.source_end_at) == (start, end)
    assert response.data_as_of == end


def test_transport_traces_limit_defaults_to_ten():
    opener = FakeOpener(routes={"/api/traces": CHECKOUT_TRACES})
    response = _transport(opener).fetch(
        _transport_request(TRACES_TOOL, {"service": "checkout"})
    )
    assert parse_qs(urlsplit(opener.urls()[0]).query)["limit"] == ["10"]
    assert json.loads(response.body)["query"]["limit"] == 10
    assert json.loads(response.body)["incomplete"] is False


@pytest.mark.parametrize("limit", [0, 21, -1, "2", 2.0, True])
def test_transport_traces_refuses_a_bad_limit_without_sending(limit):
    opener = FakeOpener(routes={"/api/traces": CHECKOUT_TRACES})
    response = _transport(opener).fetch(
        _transport_request(TRACES_TOOL, {"service": "checkout", "limit": limit})
    )
    assert response.source_status == "INVALID_LIMIT"
    assert not opener.called


@pytest.mark.parametrize(
    "service", ["flagd", "Checkout", "checkout ", "", "checkout;x"]
)
def test_transport_traces_refuses_an_unlisted_service_without_sending(service):
    opener = FakeOpener(routes={"/api/traces": CHECKOUT_TRACES})
    response = _transport(opener).fetch(
        _transport_request(TRACES_TOOL, {"service": service, "limit": 2})
    )
    assert response.source_status == "SERVICE_NOT_AVAILABLE"
    assert not opener.called


def test_transport_traces_server_error_is_unavailable():
    opener = FakeOpener(routes={"/api/traces": 503})
    with pytest.raises(TransportUnavailable):
        _transport(opener).fetch(
            _transport_request(TRACES_TOOL, {"service": "checkout"})
        )


def test_transport_refuses_an_unknown_tool_or_credential_ref():
    opener = FakeOpener(
        routes={"/api/v1/query_range": CHECKOUT_BODY, "/api/traces": CHECKOUT_TRACES}
    )
    transport = _transport(opener)
    with pytest.raises(TransportError):
        transport.fetch(_transport_request("logs_search", {"expr": GOOD_EXPR}))
    with pytest.raises(TransportError):
        transport.fetch(_transport_request("", {"expr": GOOD_EXPR}))
    with pytest.raises(TransportError):
        transport.fetch(
            _transport_request(
                METRICS_TOOL, {"expr": GOOD_EXPR}, credential_ref="prom-ro-other"
            )
        )
    assert not opener.called


def test_transport_sends_the_credential_only_as_a_bearer_header():
    opener = FakeOpener(
        by_query={GOOD_EXPR: CHECKOUT_BODY}, routes={"/api/traces": CHECKOUT_TRACES}
    )
    transport = _transport(opener, token=TRANSPORT_ONLY_MARKER)
    transport.fetch(_transport_request(METRICS_TOOL, {"expr": GOOD_EXPR}))
    transport.fetch(_transport_request(TRACES_TOOL, {"service": "checkout"}))
    for request in opener.requests:
        assert _headers(request)["authorization"] == f"Bearer {TRANSPORT_ONLY_MARKER}"
        assert TRANSPORT_ONLY_MARKER not in _url(request)
    # A None credential means no header at all; an empty string too.
    for token in (None, ""):
        bare = FakeOpener(by_query={GOOD_EXPR: CHECKOUT_BODY})
        _transport(bare, token=token).fetch(
            _transport_request(METRICS_TOOL, {"expr": GOOD_EXPR})
        )
        assert "authorization" not in _headers(bare.requests[0])


def test_transport_requests_are_read_only_by_construction():
    """PRODUCT-CONSTRAINTS: only reads. The request type refuses anything
    else, and every request the transport issues is a GET."""
    with pytest.raises(ToolContractError):
        _transport_request(METRICS_TOOL, {"expr": GOOD_EXPR}, read_only=False)
    with pytest.raises(ToolContractError):
        _transport_request(METRICS_TOOL, {"expr": GOOD_EXPR}, verb="delete")
    opener = FakeOpener(
        by_query={GOOD_EXPR: CHECKOUT_BODY}, routes={"/api/traces": CHECKOUT_TRACES}
    )
    transport = _transport(opener)
    transport.fetch(_transport_request(METRICS_TOOL, {"expr": GOOD_EXPR}))
    transport.fetch(_transport_request(TRACES_TOOL, {"service": "checkout"}))
    assert [_method(r) for r in opener.requests] == ["GET", "GET"]


def test_default_opener_follows_no_redirects_and_uses_no_proxy(monkeypatch):
    """Without an injected opener the transport builds one that neither
    follows redirects nor consults proxy settings (PRODUCT-CONSTRAINTS: no
    data exit beyond the registered endpoint)."""
    built: list[tuple] = []
    opener = FakeOpener(by_query={GOOD_EXPR: CHECKOUT_BODY})

    def build_opener(*handlers):
        built.append(handlers)
        return opener

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    transport = OtelDemoTransport(credentials={CREDENTIAL_REF: None})
    transport.fetch(_transport_request(METRICS_TOOL, {"expr": GOOD_EXPR}))
    assert opener.called and built, "the default opener was not built via build_opener"
    handlers = [h for call in built for h in call]

    def is_a(handler, kind) -> bool:
        return isinstance(handler, kind) or (
            inspect.isclass(handler) and issubclass(handler, kind)
        )

    proxies = [h for h in handlers if is_a(h, urllib.request.ProxyHandler)]
    assert proxies and all(getattr(h, "proxies", {}) == {} for h in proxies)
    assert any(is_a(h, urllib.request.HTTPRedirectHandler) for h in handlers)


# -- DurableControl ----------------------------------------------------------------


def test_durable_control_maps_the_store_state_onto_a_snapshot():
    lease = _lease()
    store = FakeStore(lease, deadline=NOW)
    executor, _, _, _ = _executor(pytest.MonkeyPatch(), FakeOpener(), store=store)
    snapshot = DurableControl(store).snapshot(executor.scope)
    assert snapshot.control_generation == lease.control_generation
    assert snapshot.global_suspension_generation == lease.global_suspension_generation
    assert snapshot.target_suspension_generation == lease.target_suspension_generation
    assert snapshot.suspended is False
    for flag in ("global_suspended", "target_suspended"):
        store.control = {**store.control, flag: True}
        assert DurableControl(store).snapshot(executor.scope).suspended is True
        store.control = {**store.control, flag: False}
    store.control = {**store.control, "incident_generation": 9, "global_generation": 4}
    moved = DurableControl(store).snapshot(executor.scope)
    assert moved.control_generation == 9 and moved.global_suspension_generation == 4


def test_durable_control_store_failure_is_control_unavailable():
    lease = _lease()
    store = FakeStore(lease, deadline=NOW)
    executor, _, _, _ = _executor(pytest.MonkeyPatch(), FakeOpener(), store=store)
    # The store's own failure vocabulary (a lost connection surfaces as
    # ``PersistenceError("STORAGE_UNAVAILABLE")`` out of ``transaction()``).
    for code in ("STORAGE_UNAVAILABLE", "LOCK_TIMEOUT", "UNKNOWN_IDENTITY"):
        store.control = PersistenceError(code)
        with pytest.raises(ControlUnavailable):
            DurableControl(store).snapshot(executor.scope)


# -- the executor built by the profile factory ---------------------------------------


def test_factory_scope_is_issued_from_the_lease_the_run_row_and_the_input(monkeypatch):
    executor, _, lease, store = _executor(monkeypatch, FakeOpener())
    scope = executor.scope
    assert scope.target_ids == frozenset({TARGET_ID})
    assert scope.tool_names == frozenset({METRICS_TOOL, TRACES_TOOL})
    assert scope.window == WINDOW == scope_window(_input(str(lease.run_id)))
    assert scope.control_generation == lease.control_generation
    assert scope.global_suspension_generation == lease.global_suspension_generation
    assert scope.target_suspension_generation == lease.target_suspension_generation
    assert scope.deadline == store.deadline
    assert scope.subject_kind == "incident" and scope.subject_id == str(
        lease.incident_id
    )
    assert scope.run_id == str(lease.run_id)


def test_metrics_call_through_the_executor_is_ok_with_exact_raw_bytes(monkeypatch):
    opener = FakeOpener(by_query={GOOD_EXPR: CHECKOUT_BODY})
    executor, sink, _, store = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(METRICS_TOOL, {"expr": GOOD_EXPR}))
    assert (outcome.status, outcome.reason) == ("ok", None), outcome.model_view
    assert outcome.source_contact == "confirmed" and outcome.adopted
    evidence = outcome.evidence
    assert evidence is not None and evidence.raw == CHECKOUT_BODY
    assert evidence.raw_sha256 == sha256(CHECKOUT_BODY).hexdigest()
    assert evidence.data_as_of == CHECKOUT_LAST
    assert (
        WINDOW.start <= evidence.source_start_at <= evidence.source_end_at <= WINDOW.end
    )
    assert (
        evidence.source_start_at == CHECKOUT_FIRST
        and evidence.source_end_at == CHECKOUT_LAST
    )
    view = outcome.model_view
    assert view["tool"] == METRICS_TOOL and view["source"] == SOURCE
    assert view["target_id"] == TARGET_ID
    assert view["content"] == json.loads(CHECKOUT_BODY)["data"]["result"]
    assert view["result_count"] == 1 and view["returned_count"] == 1
    assert view["truncated"] is False and view["incomplete"] is False
    assert view["data_as_of"] == CHECKOUT_LAST.isoformat()
    assert view["query"] == {"expr": GOOD_EXPR}
    assert view["window"] == WINDOW.as_json()
    assert [r.evidence_id for r in sink.records] == [evidence.evidence_id]
    assert outcome.operation.tool == METRICS_TOOL
    assert outcome.operation.source == SOURCE
    assert outcome.operation.credential_ref == CREDENTIAL_REF
    assert outcome.operation.sent is True
    assert len(store.charges) == 2  # reservation + settlement
    (request,) = opener.requests
    assert (
        _method(request) == "GET"
        and urlsplit(_url(request)).path == "/api/v1/query_range"
    )


def test_metrics_call_with_an_empty_result_is_no_data(monkeypatch):
    opener = FakeOpener(by_query={GOOD_EXPR: EMPTY_BODY})
    executor, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(METRICS_TOOL, {"expr": GOOD_EXPR}))
    assert (outcome.status, outcome.reason) == ("no_data", "NO_DATA")
    assert outcome.evidence is not None and outcome.evidence.raw == EMPTY_BODY
    assert outcome.model_view["content"] == []
    assert outcome.model_view["data_as_of"] is None


def test_metrics_400_is_invalid_params_and_the_error_body_is_not_evidence(monkeypatch):
    opener = FakeOpener(routes={"/api/v1/query_range": 400})
    executor, sink, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(METRICS_TOOL, {"expr": "sum(rate(m[5m])"}))
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    assert outcome.source_contact == "confirmed"
    assert outcome.evidence is None and sink.records == []
    assert outcome.model_view["content"] is None
    assert "unclosed left parenthesis" not in json.dumps(outcome.model_view)


def test_metrics_503_is_source_unavailable(monkeypatch):
    opener = FakeOpener(routes={"/api/v1/query_range": 503})
    executor, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(METRICS_TOOL, {"expr": GOOD_EXPR}))
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_UNAVAILABLE")
    assert outcome.source_contact == "possible"


def test_metrics_offset_query_is_refused_before_any_request(monkeypatch):
    opener = FakeOpener(routes={"/api/v1/query_range": CHECKOUT_BODY})
    executor, sink, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(METRICS_TOOL, {"expr": BAD_EXPR}))
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    assert not opener.called
    assert sink.records == [] and outcome.evidence is None


def test_metrics_bad_step_is_refused_before_any_request(monkeypatch):
    opener = FakeOpener(routes={"/api/v1/query_range": CHECKOUT_BODY})
    executor, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(
        _call(METRICS_TOOL, {"expr": GOOD_EXPR, "step_seconds": 5})
    )
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    assert not opener.called


def test_traces_call_through_the_executor_returns_sampled_spans(monkeypatch):
    opener = FakeOpener(routes={"/api/traces": CHECKOUT_TRACES})
    executor, sink, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(TRACES_TOOL, {"service": "checkout", "limit": 2}))
    assert (outcome.status, outcome.reason) == ("ok", None), outcome.model_view
    evidence = outcome.evidence
    assert evidence is not None
    record = json.loads(evidence.raw)
    expected, start, end = _projected(CHECKOUT_TRACES, service="checkout", limit=2)
    assert record == expected
    assert record["source_response_sha256"] == sha256(CHECKOUT_TRACES).hexdigest()
    assert record["projection"] == TRACE_PROJECTION
    assert evidence.source_start_at == start and evidence.source_end_at == end
    assert WINDOW.start <= start <= end <= WINDOW.end
    view = outcome.model_view
    assert view["tool"] == TRACES_TOOL and view["source"] == SOURCE
    rows = view["content"]
    assert 0 < len(rows) <= 20
    assert rows == expected["data"]["sampled_spans"][: len(rows)]
    assert all(row["service"] == "checkout" for row in rows)
    assert view["result_count"] == 20
    assert view["incomplete"] is True  # limit 2, 2 traces returned
    assert view["query"] == {"service": "checkout", "limit": 2}
    assert [r.evidence_id for r in sink.records] == [evidence.evidence_id]
    (request,) = opener.requests
    assert _method(request) == "GET" and urlsplit(_url(request)).path.endswith(
        "/api/traces"
    )
    assert _url(request).startswith(JAEGER_URL)


def test_traces_unknown_service_is_an_error_before_any_request(monkeypatch):
    opener = FakeOpener(routes={"/api/traces": CHECKOUT_TRACES})
    executor, sink, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(TRACES_TOOL, {"service": "flagd", "limit": 2}))
    assert outcome.status == "error"
    assert outcome.reason in ERROR_REASONS
    assert not opener.called
    assert sink.records == [] and outcome.evidence is None


def test_traces_bad_limit_is_an_error_before_any_request(monkeypatch):
    opener = FakeOpener(routes={"/api/traces": CHECKOUT_TRACES})
    executor, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(TRACES_TOOL, {"service": "checkout", "limit": 21}))
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    assert not opener.called


def test_an_undeclared_parameter_is_denied_before_any_request(monkeypatch):
    opener = FakeOpener(routes={"/api/v1/query_range": CHECKOUT_BODY})
    executor, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(
        _call(METRICS_TOOL, {"expr": GOOD_EXPR, "endpoint": "http://evil.invalid"})
    )
    assert (outcome.status, outcome.reason) == ("denied", "PARAM_NOT_ALLOWED")
    assert not opener.called


def test_a_window_outside_the_scope_is_denied_before_any_request(monkeypatch):
    opener = FakeOpener(routes={"/api/v1/query_range": CHECKOUT_BODY})
    executor, _, _, _ = _executor(monkeypatch, opener)
    wider = Window(WINDOW.start - timedelta(seconds=1), WINDOW.end)
    outcome = executor.execute(_call(METRICS_TOOL, {"expr": GOOD_EXPR}, window=wider))
    assert (outcome.status, outcome.reason) == ("denied", "WINDOW_OUT_OF_SCOPE")
    assert not opener.called


def test_the_credential_never_reaches_an_outcome(monkeypatch):
    opener = FakeOpener(
        by_query={GOOD_EXPR: CHECKOUT_BODY, BAD_EXPR: CHECKOUT_BODY},
        routes={"/api/traces": CHECKOUT_TRACES, "/api/v1/query_range": 400},
    )
    executor, sink, _, _ = _executor(monkeypatch, opener, token=TRANSPORT_ONLY_MARKER)
    outcomes = [
        executor.execute(_call(METRICS_TOOL, {"expr": GOOD_EXPR}, index=0)),
        executor.execute(
            _call(TRACES_TOOL, {"service": "checkout", "limit": 2}, index=1)
        ),
        executor.execute(_call(METRICS_TOOL, {"expr": "sum("}, index=2)),
        executor.execute(_call(METRICS_TOOL, {"expr": BAD_EXPR}, index=3)),
    ]
    assert [o.status for o in outcomes] == ["ok", "ok", "error", "error"]
    # The header went out on every real request ...
    assert len(opener.requests) == 3
    for request in opener.requests:
        assert _headers(request)["authorization"] == f"Bearer {TRANSPORT_ONLY_MARKER}"
    # ... and appears nowhere the model, the page or the audit trail can see.
    for outcome in outcomes:
        surfaces = [outcome.model_view, outcome.operation.audit_json(), repr(outcome)]
        if outcome.evidence is not None:
            surfaces += [outcome.evidence.view, outcome.evidence.raw]
        for text in _strings(surfaces):
            assert TRANSPORT_ONLY_MARKER not in text
    for record in sink.records:
        for text in _strings([record.view, record.raw]):
            assert TRANSPORT_ONLY_MARKER not in text


def test_every_request_the_executor_issues_is_a_get(monkeypatch):
    opener = FakeOpener(
        by_query={GOOD_EXPR: CHECKOUT_BODY}, routes={"/api/traces": PAYMENT_TRACES}
    )
    executor, _, _, _ = _executor(monkeypatch, opener)
    executor.execute(_call(METRICS_TOOL, {"expr": GOOD_EXPR}, index=0))
    executor.execute(_call(TRACES_TOOL, {"service": "payment"}, index=1))
    assert [_method(r) for r in opener.requests] == ["GET", "GET"]
    hosts = {urlsplit(u).netloc for u in opener.urls()}
    assert hosts == {urlsplit(PROMETHEUS_URL).netloc, urlsplit(JAEGER_URL).netloc}


def test_executor_control_check_fires_on_the_durable_control_state(monkeypatch):
    """The profile's control authority is the store's control state, so the
    executor's own SUSPENDED / CONTROL_GENERATION_CHANGED checks fire (unlike
    the fixture profile's echo control)."""
    opener = FakeOpener(by_query={GOOD_EXPR: CHECKOUT_BODY})
    executor, _, _, store = _executor(monkeypatch, opener)
    store.control = {**store.control, "global_suspended": True, "global_generation": 5}
    outcome = executor.execute(_call(METRICS_TOOL, {"expr": GOOD_EXPR}, index=0))
    assert (outcome.status, outcome.reason) == ("denied", "SUSPENDED")
    assert not opener.called and outcome.operation.sent is False
    store.control = {**store.control, "global_suspended": False}
    outcome = executor.execute(_call(METRICS_TOOL, {"expr": GOOD_EXPR}, index=1))
    assert (outcome.status, outcome.reason) == ("denied", "CONTROL_GENERATION_CHANGED")
    assert not opener.called
    store.control = RuntimeError("database unreachable")
    outcome = executor.execute(_call(METRICS_TOOL, {"expr": GOOD_EXPR}, index=2))
    assert (outcome.status, outcome.reason) == ("denied", "CONTROL_UNAVAILABLE")
    assert not opener.called


# -- profile selection ----------------------------------------------------------------


def test_profile_env_and_default_fixture_profile():
    assert PROFILE_ENV == "OPSPILOT_TOOL_PROFILE"
    profile = select_profile({})
    assert isinstance(profile, ToolProfile) and profile.name == "fixture"
    clock = FakeClock(start=NOW)
    face = profile.face(clock)
    reference = fixture_profile.fixture_face()
    assert isinstance(face, ToolFace)
    assert face.tool_schemas == reference.tool_schemas
    assert face.variant_id == reference.variant_id
    assert face.evidence_context("r") == reference.evidence_context("r")
    assert profile.versions() == fixture_profile.fixture_versions()
    assert select_profile({"UNRELATED": "otel-demo"}).name == "fixture"


def test_otel_demo_profile_is_selected_from_the_environment():
    profile = select_profile({PROFILE_ENV: "otel-demo"})
    assert profile.name == "otel-demo"
    assert profile.versions() == otel_demo_versions()
    clock = FakeClock(start=WINDOW_END)
    face = profile.face(clock)
    assert face.tool_schemas == otel_demo_face(clock).tool_schemas
    assert face.evidence_context("r") == otel_demo_face(clock).evidence_context("r")
    assert profile.versions()["tool_schema_revision"] == TOOL_SCHEMA_REVISION
    assert profile.versions() != select_profile({}).versions()


@pytest.mark.parametrize("value", ["otel", "OTEL-DEMO", "fixture ", "prod"])
def test_an_unknown_profile_name_exits(value):
    with pytest.raises(SystemExit):
        select_profile({PROFILE_ENV: value})


def test_profile_executor_factory_builds_the_otel_executor(monkeypatch):
    profile = select_profile({PROFILE_ENV: "otel-demo"})
    lease = _lease()
    store = FakeStore(lease, deadline=NOW + timedelta(minutes=10))
    opener = FakeOpener(by_query={GOOD_EXPR: CHECKOUT_BODY})
    monkeypatch.setattr(urllib.request, "build_opener", lambda *h: opener)
    factory = profile.executor_factory(store, RecordingSink(), FakeClock(start=NOW))
    executor = factory(lease, _input(str(lease.run_id)))
    assert isinstance(executor, ReadOnlyToolExecutor)
    assert executor.scope.tool_names == frozenset({METRICS_TOOL, TRACES_TOOL})
    assert executor.scope.target_ids == frozenset({TARGET_ID})


def test_worker_loop_composition_accepts_a_profile():
    parameters = inspect.signature(build_loop).parameters
    assert "profile" in parameters
    assert parameters["profile"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["profile"].default is None
