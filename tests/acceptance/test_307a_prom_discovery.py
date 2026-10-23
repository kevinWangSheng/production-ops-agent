"""Independent acceptance tests for #307 A Prometheus discovery tools.

These tests are written from ``docs/tasks/2026-10-22-307a-prom-discovery-contract.md``
and the public tool gateway interfaces.  The source is a real local HTTP
server reached through the production urllib transport; urllib is not mocked.
"""

from __future__ import annotations

import base64
import json
import threading
from datetime import timedelta
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from opspilot.investigation.loop import investigation_versions
from opspilot.redaction import REDACTED_CREDENTIAL
from opspilot.tools import PROJECTION_REVISION, ToolRequest, Window, otel_demo
from opspilot.tools.otel_demo import (
    METRICS_TOOL,
    TOOL_SCHEMA_REVISION,
    TOOL_SCHEMAS,
    TRACE_PROJECTION,
    TRACES_TOOL,
    OtelDemoConfig,
    otel_demo_executor_factory,
    otel_demo_registrations,
)
from opspilot.tools.registry import ToolRegistry, canonical, canonical_hash
from scripts.eval_live import template_scope
from tests.m1_tool_support import FakeClock, RecordingSink
from tests.test_m1_otel_demo_contract import (
    NOW,
    WINDOW,
    FakeStore,
    _input,
    _lease,
)

METRIC_NAMES_TOOL = "get_metric_names"
LABEL_VALUES_TOOL = "get_label_values"
# Keep collection useful against the pre-#307 baseline: the dedicated test
# below still fails loudly until the contractual constant exists.
DISCOVERY_LIMIT = getattr(otel_demo, "DISCOVERY_LIMIT", 100)
_BASIC_USER = "investigator"
_BASIC_PASSWORD = "investigator-pw"

# Baseline values read from the a4eea42 source tree.  The new discovery
# schemas must bump only the tool schema revision; the projection and prompt
# revisions are contracts that remain unchanged.
OLD_TOOL_SCHEMA_REVISION = "otel-demo-4d7168180749"  # a4eea42 import value
OLD_PROJECTION_REVISION = "m1-01-tool-view-v8"  # a4eea42 outcomes.py
OLD_PROMPT_REVISION = "prompt-replay-candidate-dd9018031487"  # a4eea42
# F3 PR-2 (#311 + #310) contract change: #310 adds L1a sections, so prompt_revision now differs from
# the a4eea42 value (inequality only); #311 bumps the projection to v10.
OLD_TRACES_DESCRIPTION_SHA256 = "559e4ed81e5c0f8624b5fd96f8434e164ce403d22545381253223bec893b55c3"  # sha256(TOOL_SCHEMAS traces description.encode()) at a4eea42


def _json_data(values: list[str], *, warnings: list[str] | None = None) -> bytes:
    payload: dict[str, object] = {
        "status": "success",
        "data": values,
    }
    if warnings is not None:
        payload["warnings"] = warnings
    return json.dumps(payload, separators=(",", ":")).encode()


class _PrometheusStub:
    """A local HTTP source whose requests and response can be inspected."""

    def __init__(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server API
                owner.requests.append(
                    {
                        "path": self.path,
                        "method": self.command,
                        "authorization": self.headers.get("Authorization"),
                    }
                )
                status, body, headers = owner.response
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                return

        self.requests: list[dict[str, object]] = []
        self.response: tuple[int, bytes, dict[str, str]] = (
            200,
            _json_data([]),
            {},
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@pytest.fixture
def prometheus_stub():
    stub = _PrometheusStub()
    try:
        yield stub
    finally:
        stub.close()


def _executor(stub: _PrometheusStub, *, token_counter=None):
    store = FakeStore(_lease(), deadline=NOW + timedelta(minutes=10))
    sink = RecordingSink()
    config = OtelDemoConfig(
        prometheus_url=stub.url,
        # The discovery calls never use Jaeger, but the target contract still
        # requires a valid endpoint for the second backend.
        jaeger_url=stub.url,
        prometheus_username=_BASIC_USER,
        prometheus_password=_BASIC_PASSWORD,
    )
    factory = otel_demo_executor_factory(
        store,
        evidence=sink,
        clock=FakeClock(start=NOW),
        config=config,
        token_counter=token_counter or (lambda text: len(text.encode("utf-8"))),
    )
    return factory(store.lease, _input(str(store.lease.run_id))), sink


def _call(tool: str, params: dict[str, object], *, index: int = 0, window=WINDOW):
    return ToolRequest(
        step_id="discovery-step",
        tool_index=index,
        tool_name=tool,
        target_ref="m0-otel-20260909",
        params=params,
        window=window.as_json(),
    )


def _basic(user: str = _BASIC_USER, password: str = _BASIC_PASSWORD) -> str:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return f"Basic {token}"


def _assert_evidence(outcome, sink, body: bytes):
    evidence = outcome.evidence
    assert evidence is not None
    assert evidence.raw == body
    assert evidence.raw_sha256 == sha256(body).hexdigest()
    assert evidence.view == outcome.model_view
    assert evidence.view_sha256 == canonical_hash(outcome.model_view)
    assert sink.records == [evidence]
    return evidence


def _assert_discovery_request(
    request: dict[str, object], *, path: str, match: str, window: Window
):
    parsed = urlsplit(str(request["path"]))
    query = parse_qs(parsed.query)
    assert request["method"] == "GET"
    assert parsed.path == path
    assert query["match[]"] == [match]
    assert query["limit"] == [str(DISCOVERY_LIMIT)]
    assert float(query["start"][0]) == pytest.approx(window.start.timestamp())
    assert float(query["end"][0]) == pytest.approx(window.end.timestamp())
    assert request["authorization"] == _basic()


# Contract item 1: both discovery adapters issue bounded GET requests through
# the real urllib transport, including the selected window and Basic header.
def test_metric_names_request_shape_and_basic_auth(prometheus_stub):
    names = ["http_server_requests_total", "rpc_client_duration_seconds_count"]
    prometheus_stub.response = (200, _json_data(names), {})
    executor, sink = _executor(prometheus_stub)
    selected = Window(
        WINDOW.start + timedelta(seconds=30), WINDOW.end - timedelta(seconds=30)
    )
    outcome = executor.execute(
        _call(
            METRIC_NAMES_TOOL,
            {
                "match": "http_.*",
                "start": selected.start.isoformat(),
                "end": selected.end.isoformat(),
            },
            window=WINDOW,
        )
    )

    assert (outcome.status, outcome.reason) == ("ok", None)
    _assert_discovery_request(
        prometheus_stub.requests[0],
        path="/api/v1/label/__name__/values",
        match="http_.*",
        window=selected,
    )
    assert outcome.model_view["content"] == names
    assert outcome.model_view["window"] == selected.as_json()
    _assert_evidence(outcome, sink, _json_data(names))


def test_label_values_request_shape_and_optional_match(prometheus_stub):
    values = ["checkout", "frontend"]
    body = _json_data(values)
    prometheus_stub.response = (200, body, {})
    executor, sink = _executor(prometheus_stub)
    selected = Window(WINDOW.start + timedelta(seconds=60), WINDOW.end)
    outcome = executor.execute(
        _call(
            LABEL_VALUES_TOOL,
            {
                "label": "service_name",
                "match": "traces_.*",
                "start": selected.start.isoformat(),
                "end": selected.end.isoformat(),
            },
            index=1,
        )
    )

    assert (outcome.status, outcome.reason) == ("ok", None)
    _assert_discovery_request(
        prometheus_stub.requests[0],
        path="/api/v1/label/service_name/values",
        match="traces_.*",
        window=selected,
    )
    assert outcome.model_view["content"] == values
    _assert_evidence(outcome, sink, body)


# Contract item 2: model input errors are rejected before urllib is called.
@pytest.mark.parametrize(
    "tool,params",
    [
        (METRIC_NAMES_TOOL, {}),
        (METRIC_NAMES_TOOL, {"match": ""}),
        (METRIC_NAMES_TOOL, {"match": "x" * 2001}),
        (LABEL_VALUES_TOOL, {"label": "service_name", "match": ""}),
        (LABEL_VALUES_TOOL, {"label": "a/b"}),
        (LABEL_VALUES_TOOL, {"label": ".."}),
        (LABEL_VALUES_TOOL, {"label": "foo-bar"}),
        (LABEL_VALUES_TOOL, {"label": "password"}),
    ],
)
def test_invalid_discovery_parameters_are_refused_before_send(
    prometheus_stub, tool, params
):
    executor, sink = _executor(prometheus_stub)
    outcome = executor.execute(_call(tool, params))
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    assert prometheus_stub.requests == []
    assert sink.records == []


def test_discovery_window_outside_frame_is_refused_before_send(prometheus_stub):
    executor, sink = _executor(prometheus_stub)
    outside = {
        "start": (WINDOW.start - timedelta(seconds=1)).isoformat(),
        "end": WINDOW.start.isoformat(),
    }
    outcome = executor.execute(_call(METRIC_NAMES_TOOL, {"match": "up", **outside}))
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    assert prometheus_stub.requests == []
    assert sink.records == []


# Contract item 3: warnings mark an incomplete, bounded result without
# truncating the returned list; a clean response remains complete.
def test_warning_marks_incomplete_without_truncating_rows(prometheus_stub):
    names = [f"metric_{i:03d}" for i in range(DISCOVERY_LIMIT)]
    body = _json_data(names, warnings=["results truncated due to limit"])
    prometheus_stub.response = (200, body, {})
    executor, sink = _executor(prometheus_stub)
    outcome = executor.execute(_call(METRIC_NAMES_TOOL, {"match": ".+"}))

    assert (outcome.status, outcome.reason) == ("ok", None)
    view = outcome.model_view
    assert view["content"] == names
    assert view["result_count"] == DISCOVERY_LIMIT
    assert view["incomplete"] is True
    assert view["incomplete_reason"]
    assert view["truncated"] is False
    assert view["limit"] == DISCOVERY_LIMIT
    evidence = _assert_evidence(outcome, sink, body)
    assert evidence.incomplete is True and evidence.truncated is False


def test_no_warning_is_complete(prometheus_stub):
    names = ["up", "process_cpu_seconds_total"]
    body = _json_data(names)
    prometheus_stub.response = (200, body, {})
    executor, sink = _executor(prometheus_stub)
    outcome = executor.execute(_call(METRIC_NAMES_TOOL, {"match": "up|process_.*"}))

    assert (outcome.status, outcome.reason) == ("ok", None)
    assert outcome.model_view["incomplete"] is False
    assert outcome.model_view["incomplete_reason"] is None
    assert outcome.model_view["truncated"] is False
    assert outcome.model_view["limit"] == DISCOVERY_LIMIT
    _assert_evidence(outcome, sink, body)


# Contract item 4: source classifications, no-data, and both result limits.
@pytest.mark.parametrize(
    "tool,params",
    [
        (METRIC_NAMES_TOOL, {"match": "up"}),
        (LABEL_VALUES_TOOL, {"label": "service_name"}),
    ],
)
def test_empty_data_is_no_data_with_evidence(prometheus_stub, tool, params):
    body = _json_data([])
    prometheus_stub.response = (200, body, {})
    executor, sink = _executor(prometheus_stub)
    outcome = executor.execute(_call(tool, params))

    assert (outcome.status, outcome.reason) == ("no_data", "NO_DATA")
    assert outcome.model_view["content"] == []
    _assert_evidence(outcome, sink, body)


@pytest.mark.parametrize(
    "status,expected_status,expected_reason",
    [
        (400, "error", "INVALID_PARAMS"),
        (422, "error", "INVALID_PARAMS"),
        (500, "error", "SOURCE_UNAVAILABLE"),
        (502, "error", "SOURCE_UNAVAILABLE"),
        (504, "error", "SOURCE_UNAVAILABLE"),
    ],
)
def test_http_error_classification(
    status, expected_status, expected_reason, prometheus_stub
):
    prometheus_stub.response = (
        status,
        json.dumps({"status": "error", "error": "synthetic"}).encode(),
        {},
    )
    executor, sink = _executor(prometheus_stub)
    outcome = executor.execute(_call(METRIC_NAMES_TOOL, {"match": "up"}))
    assert (outcome.status, outcome.reason) == (expected_status, expected_reason)
    assert len(prometheus_stub.requests) == 1
    assert sink.records == []


def test_503_timeout_is_tool_timeout_after_source_contact(prometheus_stub):
    prometheus_stub.response = (
        503,
        json.dumps({"status": "error", "errorType": "timeout"}).encode(),
        {},
    )
    executor, sink = _executor(prometheus_stub)
    outcome = executor.execute(_call(LABEL_VALUES_TOOL, {"label": "service_name"}))
    assert (outcome.status, outcome.reason) == ("timeout", "TOOL_TIMEOUT")
    assert outcome.source_contact == "confirmed"
    assert len(prometheus_stub.requests) == 1
    assert sink.records == []


def test_result_byte_limit_returns_no_content(prometheus_stub):
    # The registration cap is 1 MiB.  This is valid JSON but exceeds that cap.
    body = _json_data(["x" * 20_000 for _ in range(60)])
    assert len(body) > 1_048_576
    prometheus_stub.response = (200, body, {})
    executor, sink = _executor(prometheus_stub)
    outcome = executor.execute(_call(METRIC_NAMES_TOOL, {"match": ".+"}))
    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
    assert outcome.model_view.get("content") is None
    assert len(prometheus_stub.requests) == 1
    assert outcome.evidence is None and sink.records == []


def test_result_token_limit_returns_no_content(prometheus_stub):
    # Keep the wire body below 1 MiB but make the deterministic token count
    # exceed the registered 25,000-token view cap.
    body = _json_data(["metric_" + "x" * 300 for _ in range(100)])
    assert len(body) < 1_048_576
    prometheus_stub.response = (200, body, {})
    executor, sink = _executor(prometheus_stub)
    outcome = executor.execute(_call(METRIC_NAMES_TOOL, {"match": ".+"}))
    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
    assert outcome.model_view.get("content") is None
    assert len(prometheus_stub.requests) == 1
    assert outcome.evidence is None and sink.records == []


# Contract item 5: source bytes and model view both pass through redaction.
@pytest.mark.parametrize(
    "tool,params",
    [
        (METRIC_NAMES_TOOL, {"match": ".+"}),
        (LABEL_VALUES_TOOL, {"label": "service_name"}),
    ],
)
def test_discovery_values_are_redacted_in_raw_evidence_and_view(
    prometheus_stub, tool, params
):
    body = _json_data(["password=xyz"])
    prometheus_stub.response = (200, body, {})
    executor, sink = _executor(prometheus_stub)
    outcome = executor.execute(_call(tool, params))

    assert (outcome.status, outcome.reason) == ("ok", None)
    evidence = outcome.evidence
    assert evidence is not None
    assert b"password=xyz" not in evidence.raw
    assert REDACTED_CREDENTIAL.encode() in evidence.raw
    assert "password=xyz" not in canonical(outcome.model_view)
    assert REDACTED_CREDENTIAL in canonical(outcome.model_view)
    assert sink.records == [evidence]


# Contract item 6: revision relationships and the unchanged trace face.
def test_revisions_bump_only_the_tool_schema_and_trace_description_stays_equal():
    registrations = otel_demo_registrations()
    registry_revision = ToolRegistry(registrations.tools).revision
    digest = canonical_hash(
        {
            "tool_schemas": [dict(item) for item in TOOL_SCHEMAS],
            "registry": registry_revision,
            "trace_projection": TRACE_PROJECTION,
            "projection_revision": PROJECTION_REVISION,
        }
    )
    assert TOOL_SCHEMA_REVISION == f"otel-demo-{digest[:12]}"
    assert TOOL_SCHEMA_REVISION != OLD_TOOL_SCHEMA_REVISION
    # #308 PR2 bumps the projection once (contract R10 / acceptance 7).
    assert PROJECTION_REVISION != OLD_PROJECTION_REVISION
    assert PROJECTION_REVISION == "m1-01-tool-view-v10"
    assert investigation_versions()["prompt_revision"] != OLD_PROMPT_REVISION
    traces_description = next(
        schema["function"]["description"]
        for schema in TOOL_SCHEMAS
        if schema["function"]["name"] == TRACES_TOOL
    )
    assert (
        sha256(traces_description.encode()).hexdigest() == OLD_TRACES_DESCRIPTION_SHA256
    )


# Contract item 7: the executor scope and the live driver's template scope
# expose all four tools.
def test_factory_and_template_scope_contain_all_four_tools(prometheus_stub):
    executor, _ = _executor(prometheus_stub)
    expected = frozenset(
        {METRICS_TOOL, TRACES_TOOL, METRIC_NAMES_TOOL, LABEL_VALUES_TOOL}
    )
    # #308 PR2 adds read_evidence; the four #307 A tools stay.
    assert expected < executor.scope.tool_names
    assert executor.scope.tool_names == expected | {"read_evidence"}
    expected = executor.scope.tool_names
    assert template_scope().tool_names == expected


def test_discovery_registration_contract_is_public_and_bounded():
    assert getattr(otel_demo, "DISCOVERY_LIMIT", None) == 100
    registrations = {
        registration.name: registration
        for registration in otel_demo_registrations().tools
    }
    assert {METRIC_NAMES_TOOL, LABEL_VALUES_TOOL} <= registrations.keys()
    for name in (METRIC_NAMES_TOOL, LABEL_VALUES_TOOL):
        registration = registrations[name]
        assert registration.source == "otel-demo"
        assert registration.version == "v1"
        assert registration.verb == "list"
        assert registration.result_path == ("data",)
        assert registration.max_result_bytes == 1_048_576
        assert registration.max_view_tokens == 25_000
        assert registration.incomplete_marker == "warnings"
        assert registration.may_contain_secrets is False
        assert "100" in registration.description.limits
        description_text = " ".join(
            getattr(registration.description, field)
            for field in (
                "returns",
                "window_format",
                "values_format",
                "limits",
                "cannot_prove",
            )
        )
        assert "://" not in description_text
