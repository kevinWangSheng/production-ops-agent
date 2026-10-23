"""Implementer's unit tests for #307 A (Prometheus discovery tools, contract r1).

The independent acceptance tests live elsewhere; these cover the
implementation through the real profile factory, real ``urllib`` and a local
HTTP stub (no fake opener), plus the static face/revision checks.
"""

from __future__ import annotations

import base64
import json
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from opspilot.tools import fixture as fixture_profile
from opspilot.tools.evidence_read import EVIDENCE_TOOL
from opspilot.tools.otel_demo import (
    DISCOVERY_LIMIT,
    LABEL_VALUES_TOOL,
    METRIC_NAMES_TOOL,
    METRICS_TOOL,
    PROMETHEUS_TOOLS,
    TOOL_NAMES,
    TOOL_SCHEMA_REVISION,
    TOOL_SCHEMAS,
    TRACES_TOOL,
    OtelDemoConfig,
    _registrations,
    _tool_schema_revision,
    otel_demo_executor_factory,
    otel_demo_registrations,
)
from opspilot.tools.outcomes import PROJECTION_REVISION, Window
from opspilot.tools.registry import ToolRegistry
from tests.m1_tool_support import FakeClock, RecordingSink
from tests.test_m1_otel_demo_contract import (
    JAEGER_URL,
    NOW,
    WINDOW,
    FakeStore,
    _call,
    _input,
    _lease,
)

USER, PASSWORD = "investigator", "investigator-pw"
BASIC = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()


def _names(count: int, *, warnings: bool = False) -> bytes:
    payload: dict = {"status": "success", "data": [f"m_{i}" for i in range(count)]}
    if warnings:
        payload["warnings"] = ["results truncated due to limit"]
    return json.dumps(payload).encode()


@pytest.fixture
def stub():
    state: dict = {"status": 200, "body": _names(3), "seen": []}

    class Handler(BaseHTTPRequestHandler):
        def _answer(self):
            state["seen"].append(
                (self.command, self.path, self.headers.get("Authorization"))
            )
            body = state["body"]
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = _answer  # noqa: N815 - http.server API
        do_POST = _answer  # noqa: N815

        def log_message(self, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()


def _executor(url: str):
    store = FakeStore(_lease(), deadline=NOW + timedelta(minutes=10))
    config = OtelDemoConfig(
        prometheus_url=url,
        jaeger_url=JAEGER_URL,
        prometheus_username=USER,
        prometheus_password=PASSWORD,
    )
    sink = RecordingSink()
    factory = otel_demo_executor_factory(
        store, evidence=sink, clock=FakeClock(start=NOW), config=config
    )
    return factory(store.lease, _input(str(store.lease.run_id))), sink


def _default_window() -> Window:
    return Window(max(WINDOW.start, WINDOW.end - timedelta(hours=1)), WINDOW.end)


# -- request shape --------------------------------------------------------------


def test_metric_names_request_shape(stub):
    url, state = stub
    executor, _ = _executor(url)
    outcome = executor.execute(_call(METRIC_NAMES_TOOL, {"match": '{job="x"}'}))
    assert (outcome.status, outcome.reason) == ("ok", None), outcome.model_view
    ((method, path, auth),) = state["seen"]
    parts = urlsplit(path)
    query = parse_qs(parts.query)
    window = _default_window()
    assert method == "GET" and auth == BASIC
    assert parts.path == "/api/v1/label/__name__/values"
    assert query["match[]"] == ['{job="x"}']
    assert query["limit"] == [str(DISCOVERY_LIMIT)] == ["100"]
    assert float(query["start"][0]) == window.start.timestamp()
    assert float(query["end"][0]) == window.end.timestamp()
    view = outcome.model_view
    assert view["content"] == ["m_0", "m_1", "m_2"]
    assert view["result_count"] == 3 and view["incomplete"] is False
    assert view["incomplete_reason"] is None and view["limit"] == 100
    assert view["window"] == window.as_json()
    assert view["truncated"] is False
    assert outcome.evidence is not None and outcome.evidence.raw == state["body"]


def test_label_values_request_without_match_and_with_explicit_window(stub):
    url, state = stub
    executor, _ = _executor(url)
    start = WINDOW.start + timedelta(seconds=10)
    end = WINDOW.end - timedelta(seconds=10)
    outcome = executor.execute(
        _call(
            LABEL_VALUES_TOOL,
            {
                "label": "service_name",
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
        )
    )
    assert outcome.status == "ok", outcome.model_view
    ((method, path, auth),) = state["seen"]
    parts = urlsplit(path)
    query = parse_qs(parts.query)
    assert method == "GET" and auth == BASIC
    assert parts.path == "/api/v1/label/service_name/values"
    assert "match[]" not in query and query["limit"] == ["100"]
    assert float(query["start"][0]) == start.timestamp()
    assert float(query["end"][0]) == end.timestamp()


def test_label_values_accepts_dunder_name(stub):
    url, state = stub
    executor, _ = _executor(url)
    outcome = executor.execute(
        _call(LABEL_VALUES_TOOL, {"label": "__name__", "match": "{a='b'}"})
    )
    assert outcome.status == "ok"
    assert urlsplit(state["seen"][0][1]).path == "/api/v1/label/__name__/values"


# -- refusals before sending ---------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "params"),
    [
        (METRIC_NAMES_TOOL, {}),
        (METRIC_NAMES_TOOL, {"match": ""}),
        (METRIC_NAMES_TOOL, {"match": "   "}),
        (METRIC_NAMES_TOOL, {"match": "{" + "a" * 2001 + "}"}),
        (LABEL_VALUES_TOOL, {}),
        (LABEL_VALUES_TOOL, {"label": "a/b"}),
        (LABEL_VALUES_TOOL, {"label": ".."}),
        (LABEL_VALUES_TOOL, {"label": "foo-bar"}),
        (LABEL_VALUES_TOOL, {"label": "1abc"}),
        (LABEL_VALUES_TOOL, {"label": "password"}),
        (LABEL_VALUES_TOOL, {"label": "api_key"}),
        (LABEL_VALUES_TOOL, {"label": "job", "match": ""}),
        (
            METRIC_NAMES_TOOL,
            {
                "match": "{a='b'}",
                "start": (WINDOW.start - timedelta(hours=2)).isoformat(),
                "end": (WINDOW.start - timedelta(hours=1)).isoformat(),
            },
        ),
    ],
)
def test_refused_before_sending(stub, tool, params):
    url, state = stub
    executor, sink = _executor(url)
    outcome = executor.execute(_call(tool, params))
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    assert outcome.source_contact == "none"
    assert state["seen"] == [] and sink.records == []


def test_transport_codes_for_label_refusals():
    from opspilot.tools.otel_demo import OtelDemoTransport
    from tests.test_m1_otel_demo_contract import _transport_request

    transport = OtelDemoTransport(credentials={"otel-demo-ro": None})
    for params, code in (
        ({"label": "a/b"}, "INVALID_LABEL"),
        ({"label": "session_token"}, "CREDENTIAL_LABEL_REFUSED"),
        ({"label": "job", "match": ""}, "MATCH_INVALID"),
    ):
        response = transport.fetch(_transport_request(LABEL_VALUES_TOOL, params))
        assert (response.source_status, response.sent) == (code, False)
    response = transport.fetch(_transport_request(METRIC_NAMES_TOOL, {}))
    assert (response.source_status, response.sent) == ("MATCH_INVALID", False)


# -- incomplete, errors, redaction ----------------------------------------------------


def test_limit_with_warning_is_incomplete_not_truncated(stub):
    url, state = stub
    state["body"] = _names(100, warnings=True)
    executor, _ = _executor(url)
    outcome = executor.execute(_call(METRIC_NAMES_TOOL, {"match": "{a='b'}"}))
    view = outcome.model_view
    assert outcome.status == "ok"
    assert view["result_count"] == 100 and len(view["content"]) == 100
    assert view["incomplete"] is True and view["truncated"] is False
    assert isinstance(view["incomplete_reason"], str) and view["incomplete_reason"]
    assert outcome.evidence is not None and outcome.evidence.incomplete is True


def test_empty_data_is_no_data(stub):
    url, state = stub
    state["body"] = _names(0)
    executor, _ = _executor(url)
    outcome = executor.execute(_call(LABEL_VALUES_TOOL, {"label": "job"}))
    assert (outcome.status, outcome.reason) == ("no_data", "NO_DATA")


@pytest.mark.parametrize(
    ("status", "body", "expected", "contact"),
    [
        (
            400,
            b'{"status":"error","errorType":"bad_data"}',
            ("error", "INVALID_PARAMS"),
            "confirmed",
        ),
        (422, b'{"status":"error"}', ("error", "INVALID_PARAMS"), "confirmed"),
        (401, b"Unauthorized", ("error", "SOURCE_ERROR"), "confirmed"),
        (403, b"Forbidden", ("error", "SOURCE_ERROR"), "confirmed"),
        (
            503,
            b'{"status":"error","errorType":"timeout","error":"query timed out"}',
            ("timeout", "TOOL_TIMEOUT"),
            "confirmed",
        ),
        (
            503,
            b'{"status":"error","errorType":"unavailable"}',
            ("error", "SOURCE_UNAVAILABLE"),
            "possible",
        ),
        (500, b"boom", ("error", "SOURCE_UNAVAILABLE"), "possible"),
    ],
)
def test_http_status_classification(stub, status, body, expected, contact):
    url, state = stub
    state["status"], state["body"] = status, body
    executor, sink = _executor(url)
    outcome = executor.execute(_call(LABEL_VALUES_TOOL, {"label": "job"}))
    assert (outcome.status, outcome.reason) == expected
    assert outcome.source_contact == contact
    assert sink.records == [] and outcome.model_view["content"] is None


def test_metrics_range_query_503_timeout_unchanged(stub):
    url, state = stub
    state["status"] = 503
    state["body"] = b'{"status":"error","errorType":"timeout"}'
    executor, _ = _executor(url)
    outcome = executor.execute(_call(METRICS_TOOL, {"expr": "up"}))
    assert (outcome.status, outcome.reason) == ("timeout", "TOOL_TIMEOUT")
    assert state["seen"][0][2] == BASIC


def test_oversized_raw_body_is_result_too_large(stub):
    url, state = stub
    state["body"] = json.dumps(
        {"status": "success", "data": ["x" * 1024] * 1100}
    ).encode()
    executor, sink = _executor(url)
    outcome = executor.execute(_call(METRIC_NAMES_TOOL, {"match": "{a='b'}"}))
    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
    assert outcome.model_view["content"] is None and sink.records == []


def test_over_token_view_is_result_too_large(stub):
    url, state = stub
    # ~100 values of ~1.5 KB each: under 1 MiB raw, well over 25k tokens.
    state["body"] = json.dumps(
        {"status": "success", "data": [f"v{i}-" + "ab " * 500 for i in range(100)]}
    ).encode()
    executor, _ = _executor(url)
    outcome = executor.execute(_call(LABEL_VALUES_TOOL, {"label": "job"}))
    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
    assert outcome.model_view["content"] is None


def test_credential_values_are_redacted_in_raw_and_view(stub):
    url, state = stub
    state["body"] = json.dumps(
        {"status": "success", "data": ["ok", "password=xyz"]}
    ).encode()
    executor, _ = _executor(url)
    outcome = executor.execute(_call(LABEL_VALUES_TOOL, {"label": "job"}))
    assert outcome.status == "ok"
    assert outcome.evidence is not None
    assert b"xyz" not in outcome.evidence.raw
    assert "xyz" not in json.dumps(outcome.model_view)


# -- face, registration, revision, scope ---------------------------------------------


def test_registrations_and_schemas_cover_four_tools():
    names = {reg.name for reg in _registrations()}
    assert (
        names
        == set(TOOL_NAMES)
        == {
            METRICS_TOOL,
            TRACES_TOOL,
            METRIC_NAMES_TOOL,
            LABEL_VALUES_TOOL,
            # #308 PR2 adds the evidence read-back tool to the profile.
            EVIDENCE_TOOL,
        }
    )
    assert {s["function"]["name"] for s in TOOL_SCHEMAS} == names
    assert PROMETHEUS_TOOLS == {METRICS_TOOL, METRIC_NAMES_TOOL, LABEL_VALUES_TOOL}
    for reg in _registrations():
        if reg.name not in (METRIC_NAMES_TOOL, LABEL_VALUES_TOOL):
            continue
        assert (reg.version, reg.verb, reg.source) == ("v1", "list", "otel-demo")
        assert reg.result_path == ("data",)
        assert reg.max_result_bytes == 1024 * 1024
        assert reg.max_view_tokens == 25_000
        assert reg.incomplete_marker == "warnings"
        assert reg.may_contain_secrets is False
        for code in (
            "QUERY_OUT_OF_WINDOW",
            "MATCH_INVALID",
            "INVALID_LABEL",
            "CREDENTIAL_LABEL_REFUSED",
            "400",
            "422",
        ):
            assert reg.error_classes[code] == "INVALID_PARAMS"
        assert reg.error_classes["503"] == "SOURCE_UNAVAILABLE"
        assert "100" in reg.description.limits
        assert "token" in reg.description.limits.lower()
    schemas = {s["function"]["name"]: s["function"] for s in TOOL_SCHEMAS}
    assert schemas[METRIC_NAMES_TOOL]["parameters"]["required"] == ["match"]
    assert schemas[LABEL_VALUES_TOOL]["parameters"]["required"] == ["label"]
    for name in (METRIC_NAMES_TOOL, LABEL_VALUES_TOOL):
        text = json.dumps(schemas[name])
        assert "://" not in text and "/api/" not in text
        assert "100" in schemas[name]["description"]
        assert "limit" not in schemas[name]["parameters"]["properties"]


def test_metrics_description_carries_the_discovery_sentence():
    metrics = next(s for s in TOOL_SCHEMAS if s["function"]["name"] == METRICS_TOOL)
    text = metrics["function"]["description"]
    assert "get_metric_names" in text and "get_label_values" in text
    assert "http_server_request_duration_seconds_count." in text


def test_revision_is_recomputable_and_changed():
    assert TOOL_SCHEMA_REVISION == _tool_schema_revision()
    assert TOOL_SCHEMA_REVISION != "otel-demo-4d7168180749"  # pre-#307 A value
    assert TOOL_SCHEMA_REVISION != fixture_profile.TOOL_SCHEMA_REVISION
    assert PROJECTION_REVISION  # unchanged module constant, not part of this PR
    fixture_names = {fixture_profile._registration().name}
    assert METRIC_NAMES_TOOL not in fixture_names
    assert LABEL_VALUES_TOOL not in fixture_names


def test_factory_scope_and_eval_template_scope_carry_four_tools(stub):
    url, _ = stub
    executor, _ = _executor(url)
    assert executor.scope.tool_names == frozenset(TOOL_NAMES)
    from scripts import eval_live

    dto = otel_demo_registrations()
    assert ToolRegistry(dto.tools).revision == executor.scope.tool_registry_revision
    template = eval_live.template_scope(OtelDemoConfig())
    assert template.tool_names == frozenset(TOOL_NAMES)
