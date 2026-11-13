"""Implementer unit tests for the A4 Jaeger trace tools (#556).

Jaeger responses are synthetic, shaped like the v1.53.0 query HTTP API
(``{"data": ..., "total": ..., "limit": ..., "offset": ..., "errors": ...}``).
The executor is built through ``select_profile`` with the injected
``urllib.request.build_opener`` seam the A2 contract tests use.
"""

from __future__ import annotations

import ast
import io
import json
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from opspilot.tools import TransportRequest, otel_demo
from opspilot.tools import jaeger_query as jq
from opspilot.tools.otel_demo import CREDENTIAL_REF, TARGET_ID
from opspilot.tools.outcomes import Window
from opspilot.tools.profiles import PROFILE_ENV, select_profile
from tests.m1_tool_support import FakeClock
from tests.test_m1_otel_demo_contract import NOW, WINDOW
from tests.test_tool_expansion_a2_contract import (  # noqa: F401  (autouse CA)
    _call,
    _executor,
    _synthetic_k8s_ca,
)

EXT = "otel-demo-ext"
END_US = int(WINDOW.end.timestamp() * 1_000_000)
START_US = int(WINDOW.start.timestamp() * 1_000_000)


class _Reply:
    def __init__(self, body: bytes):
        self._stream = io.BytesIO(body)
        self.status = 200

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class JaegerOpener:
    """Ordered replies: bytes (200) or ``(status, body)`` (an HTTP error)."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.urls: list[str] = []
        self.headers: list[dict[str, str]] = []

    def open(self, request, timeout=None):
        url = request.full_url
        self.urls.append(url)
        self.headers.append(
            {
                k.lower(): v
                for k, v in {**request.headers, **request.unredirected_hdrs}.items()
                if k.lower() == "authorization"
            }
        )
        assert request.get_method() == "GET"
        value = self.replies.pop(0)
        if isinstance(value, tuple):
            status, body = value
            raise urllib.error.HTTPError(url, status, "x", {}, io.BytesIO(body))
        return _Reply(value)


def _envelope(data, errors=None) -> bytes:
    return json.dumps(
        {"data": data, "total": 0, "limit": 0, "offset": 0, "errors": errors}
    ).encode()


def _span(span_id, start, duration=1000, *, trace="abc123", op="op", tags=()):
    return {
        "traceID": trace,
        "spanID": span_id,
        "operationName": op,
        "references": [],
        "startTime": start,
        "duration": duration,
        "tags": [{"key": k, "type": "string", "value": v} for k, v in tags],
        "logs": [],
        "processID": "p1",
    }


def _trace(trace_id, spans, service="checkout"):
    return {
        "traceID": trace_id,
        "spans": spans,
        "processes": {"p1": {"serviceName": service, "tags": []}},
    }


def _ext(monkeypatch, *replies):
    opener = JaegerOpener(*replies)
    executor, sink, *_ = _executor(monkeypatch, opener)
    return executor, opener


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


# -- face --------------------------------------------------------------------


def test_extension_face_adds_the_four_tools_and_leaves_otel_demo_pinned():
    ext = select_profile({PROFILE_ENV: EXT})
    names = [s["function"]["name"] for s in ext.face(FakeClock(start=NOW)).tool_schemas]
    for name in jq.TOOL_NAMES:
        assert name in names
    assert otel_demo.TOOL_SCHEMA_REVISION == "otel-demo-a3a95a0f888e"
    assert not set(jq.TOOL_NAMES) & set(otel_demo.TOOL_NAMES)
    context = ext.face(FakeClock(start=NOW)).evidence_context("run-1")
    assert context["data_sources"]["available"]["jaeger"] == [
        otel_demo.TRACES_TOOL,
        *jq.TOOL_NAMES,
    ]
    registrations = {r.name: r for r in jq.jaeger_registrations(max_window_seconds=60)}
    for schema in jq.TOOL_SCHEMAS:
        function = schema["function"]
        assert set(function["parameters"]["properties"]) == set(
            registrations[function["name"]].parameters
        )


# -- parsing ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "parsed"),
    [
        ('http.status_code="500"', {"http.status_code": "500"}),
        ("error=true  k2=v2", {"error": "true", "k2": "v2"}),
        ('msg="a \\"b\\" c\\\\"', {"msg": 'a "b" c\\'}),
        ('empty=""', {"empty": ""}),
        ("   ", {}),
    ],
)
def test_parse_tags_accepts_logfmt(text, parsed):
    assert jq.parse_tags(text) == parsed


@pytest.mark.parametrize(
    "text",
    ["key", "=v", "k=", 'k="open', "k=a k=b", 'k="v"x', "k=a\tb", "k=a\nb", 'k=a"b'],
)
def test_parse_tags_refuses_malformed(text):
    assert jq.parse_tags(text) is None


@pytest.mark.parametrize(
    ("text", "nanos"),
    [("300ms", 300_000_000), ("1.5s", 1_500_000_000), ("1h30m", 5_400_000_000_000)],
)
def test_parse_duration(text, nanos):
    assert jq.parse_duration(text) == nanos


@pytest.mark.parametrize("text", ["", "5", "0s", "-1s", "1d", "s", "1.s5"])
def test_parse_duration_refuses(text):
    assert jq.parse_duration(text) is None


# -- traces_search_by_tags ------------------------------------------------------


def test_search_sends_every_filter_and_records_the_query(monkeypatch):
    body = _envelope(
        [_trace("abc123", [_span("s1", END_US - 2_000_000, tags=[("k", "v")])])]
    )
    executor, opener = _ext(monkeypatch, body)
    outcome = executor.execute(
        _call(
            jq.SEARCH_BY_TAGS_TOOL,
            {
                "service": "my-new-service",
                "tags": 'http.response.status_code="500" error=true',
                "operation": "POST /x",
                "min_duration": "100ms",
                "max_duration": "2s",
                "limit": 5,
            },
        )
    )
    assert (outcome.status, outcome.reason) == ("ok", None), outcome.model_view
    url = opener.urls[0]
    assert urlsplit(url).path.endswith("/api/traces")
    query = _query(url)
    assert query["service"] == ["my-new-service"]
    assert json.loads(query["tags"][0]) == {
        "http.response.status_code": "500",
        "error": "true",
    }
    assert query["operation"] == ["POST /x"]
    assert query["minDuration"] == ["100ms"]
    assert query["maxDuration"] == ["2s"]
    assert query["limit"] == ["5"]
    view = outcome.model_view
    assert view["incomplete"] is False
    assert view["backend_spans_returned"] == 1
    assert [row["span_id"] for row in view["content"]] == ["s1"]


def test_search_full_result_keeps_newest_and_is_incomplete(monkeypatch):
    traces = [
        _trace(
            f"abc{i}", [_span(f"s{i}", END_US - (10 - i) * 1_000_000, trace=f"abc{i}")]
        )
        for i in range(3)
    ]
    executor, _ = _ext(monkeypatch, _envelope(traces))
    outcome = executor.execute(
        _call(jq.SEARCH_BY_TAGS_TOOL, {"service": "checkout", "limit": 2})
    )
    view = outcome.model_view
    assert outcome.status == "ok", view
    assert view["incomplete"] is True
    assert view["backend_traces_returned"] == 3
    assert {row["trace_id"] for row in view["content"]} == {"abc1", "abc2"}


def test_search_errors_in_body_mark_incomplete_and_empty_hints_service(monkeypatch):
    body = _envelope([], errors=[{"code": 500, "msg": "partial"}])
    executor, _ = _ext(monkeypatch, body)
    outcome = executor.execute(_call(jq.SEARCH_BY_TAGS_TOOL, {"service": "checkout"}))
    view = outcome.model_view
    # An empty view is the executor's no_data, as for traces_search.
    assert (outcome.status, outcome.reason) == ("no_data", "NO_DATA"), view
    assert view["incomplete"] is True
    assert "traces_list_services" in " ".join(view["notes"])


@pytest.mark.parametrize(
    "params",
    [
        {"service": ""},
        {"service": "a" * 201},
        {"service": "bad\x00name"},
        {"service": "checkout", "min_duration": "2s", "max_duration": "1s"},
        {"service": "checkout", "min_duration": "soon"},
        {"service": "checkout", "limit": 0},
    ],
)
def test_search_refusals_send_nothing(monkeypatch, params):
    executor, opener = _ext(monkeypatch)
    outcome = executor.execute(_call(jq.SEARCH_BY_TAGS_TOOL, params))
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    assert opener.urls == []


def test_invalid_tags_is_its_own_outcome_reason(monkeypatch):
    executor, opener = _ext(monkeypatch)
    outcome = executor.execute(
        _call(jq.SEARCH_BY_TAGS_TOOL, {"service": "checkout", "tags": "broken"})
    )
    assert (outcome.status, outcome.reason) == ("error", "INVALID_TAGS")
    assert opener.urls == []


def test_configured_otel_token_is_not_sent_to_jaeger():
    opener = JaegerOpener(_envelope(["cart"]))
    transport = jq.JaegerQueryTransport(
        credentials={CREDENTIAL_REF: "otel-aaaaaaaa357"}, opener=opener
    )
    transport.fetch(_transport_request(jq.LIST_SERVICES_TOOL, {}, WINDOW))
    assert opener.headers == [{}]


def test_invalid_tags_reason_code():
    transport = jq.JaegerQueryTransport(credentials={CREDENTIAL_REF: None})
    response = transport.fetch(
        TransportRequest(
            operation_id="a4",
            source="otel-demo",
            verb="query",
            endpoint="http://127.0.0.1:19090",
            selector={"traces_endpoint": "http://127.0.0.1:16686"},
            params={"service": "checkout", "tags": "k="},
            window=WINDOW,
            timeout_seconds=1.0,
            max_result_bytes=1024,
            credential_ref=CREDENTIAL_REF,
            tool=jq.SEARCH_BY_TAGS_TOOL,
            target_id=TARGET_ID,
        )
    )
    assert response.source_status == "INVALID_TAGS" and response.sent is False


# -- traces_query_trace_by_id ----------------------------------------------------


def test_by_id_returns_every_span_in_start_order_and_drops_outside(monkeypatch):
    spans = [
        _span("late", END_US - 1_000_000),
        _span("early", END_US - 5_000_000),
        _span("before", START_US - 10_000_000),
    ]
    executor, opener = _ext(monkeypatch, _envelope([_trace("abc123", spans)]))
    outcome = executor.execute(_call(jq.TRACE_BY_ID_TOOL, {"trace_id": "ABC123"}))
    view = outcome.model_view
    assert outcome.status == "ok", view
    assert urlsplit(opener.urls[0]).path.endswith("/api/traces/abc123")
    assert urlsplit(opener.urls[0]).query == ""
    assert [row["span_id"] for row in view["content"]] == ["early", "late"]
    assert view["spans_outside_window"] == 1
    assert view["backend_spans_returned"] == 3 and view["spans_omitted"] == 1
    assert view["trace_found"] is True
    assert view["window"] == WINDOW.as_json()


def test_by_id_404_is_an_empty_success_with_the_ring_note(monkeypatch):
    body = _envelope(None, errors=[{"code": 404, "msg": "trace not found"}])
    executor, _ = _ext(monkeypatch, (404, body))
    outcome = executor.execute(_call(jq.TRACE_BY_ID_TOOL, {"trace_id": "abc123"}))
    view = outcome.model_view
    assert (outcome.status, outcome.reason) == ("no_data", "NO_DATA"), view
    assert view["content"] == [] and view["trace_found"] is False
    assert "5000" in " ".join(view["notes"])
    assert view["source_start_at"] is None


def test_by_id_400_echoes_jaeger_error_text(monkeypatch):
    body = _envelope(None, errors=[{"code": 400, "msg": "bad trace id aaaaaaaa357"}])
    executor, _ = _ext(monkeypatch, (400, body))
    outcome = executor.execute(_call(jq.TRACE_BY_ID_TOOL, {"trace_id": "abc123"}))
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    assert "bad trace id" in outcome.model_view["source_error"]["error"]


@pytest.mark.parametrize("trace_id", ["", "xyz", "a" * 33, "abc-123"])
def test_by_id_refuses_a_non_hex_id(monkeypatch, trace_id):
    executor, opener = _ext(monkeypatch)
    outcome = executor.execute(_call(jq.TRACE_BY_ID_TOOL, {"trace_id": trace_id}))
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    assert opener.urls == []


def test_jaeger_5xx_is_source_unavailable(monkeypatch):
    executor, _ = _ext(monkeypatch, (503, b"{}"))
    outcome = executor.execute(_call(jq.TRACE_BY_ID_TOOL, {"trace_id": "abc123"}))
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_UNAVAILABLE")


# -- listings ----------------------------------------------------------------


def test_list_services_has_no_source_time(monkeypatch):
    executor, opener = _ext(monkeypatch, _envelope(["frontend", "cart", "cart"]))
    outcome = executor.execute(_call(jq.LIST_SERVICES_TOOL, {}))
    view = outcome.model_view
    assert outcome.status == "ok", view
    assert urlsplit(opener.urls[0]).path.endswith("/api/services")
    assert view["content"] == ["cart", "frontend"]
    assert view["source_start_at"] is None and view["source_end_at"] is None
    assert "since it last started" in " ".join(view["notes"])


def test_list_operations_redacts_and_cuts_names(monkeypatch):
    data = [
        {"name": "x" * 200, "spanKind": "server"},
        {"name": "GET /a", "spanKind": "client"},
    ]
    executor, opener = _ext(monkeypatch, _envelope(data))
    outcome = executor.execute(
        _call(jq.LIST_OPERATIONS_TOOL, {"service": "checkout", "span_kind": "server"})
    )
    view = outcome.model_view
    assert outcome.status == "ok", view
    query = _query(opener.urls[0])
    assert query == {"service": ["checkout"], "spanKind": ["server"]}
    rows = {row["name"]: row for row in view["content"]}
    assert rows["GET /a"]["span_kind"] == "client"
    long = rows["x" * 150]
    assert long["name_truncated"] is True


def test_list_operations_refuses_unknown_span_kind(monkeypatch):
    executor, opener = _ext(monkeypatch)
    outcome = executor.execute(
        _call(jq.LIST_OPERATIONS_TOOL, {"service": "checkout", "span_kind": "weird"})
    )
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    assert opener.urls == []


# -- code surface ------------------------------------------------------------


def test_module_opens_no_socket_and_starts_no_process():
    path = Path(jq.__file__)
    tree = ast.parse(path.read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not imported & {"socket", "http.client", "subprocess", "urllib.request"}


def _transport_request(tool, params, window):
    return TransportRequest(
        operation_id="a4",
        source="otel-demo",
        verb="query",
        endpoint="http://127.0.0.1:19090",
        selector={"traces_endpoint": "http://127.0.0.1:16686"},
        params=params,
        window=window,
        timeout_seconds=5.0,
        max_result_bytes=1024 * 1024,
        credential_ref=CREDENTIAL_REF,
        tool=tool,
        target_id=TARGET_ID,
    )


def test_by_id_without_start_end_uses_the_whole_authorized_window():
    frame = Window(WINDOW.end - timedelta(hours=3), WINDOW.end)
    old = int((WINDOW.end - timedelta(hours=2)).timestamp() * 1_000_000)
    opener = JaegerOpener(_envelope([_trace("abc123", [_span("old", old)])]))
    transport = jq.JaegerQueryTransport(
        credentials={CREDENTIAL_REF: None}, opener=opener
    )
    response = transport.fetch(
        _transport_request(jq.TRACE_BY_ID_TOOL, {"trace_id": "abc123"}, frame)
    )
    record = json.loads(response.body)
    assert [s["span_id"] for s in record["data"]["sampled_spans"]] == ["old"]
    assert response.query_window == frame


def test_by_id_over_view_limit_is_refused_whole_and_kept(monkeypatch):
    spans = [
        _span(f"s{i:05d}", END_US - 200_000_000 + i * 10, op=f"operation-{i}")
        for i in range(3000)
    ]
    executor, _ = _ext(monkeypatch, _envelope([_trace("abc123", spans)]))
    outcome = executor.execute(_call(jq.TRACE_BY_ID_TOOL, {"trace_id": "abc123"}))
    view = outcome.model_view
    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE"), view
    # Reached the view-token refusal (not the byte ceiling), which is the
    # executor path that keeps the result for read_evidence when the sink can
    # (#308); this test sink cannot count spills, so nothing is kept here.
    assert view["view_tokens"] > view["max_view_tokens"]
    assert view["adopted"] is False
