"""Independent contract tests for tool expansion A5 (#557).

These tests are written against the A5 task record, decision table r3 and C3
section 8.  The implementation is intentionally absent from this worktree;
the collection/run is expected to be red on ``main`` until A5 is implemented.

接缝假设（实现必须按公开行为满足；若实现接缝不同，应在不放宽合同的前提下
由实现者适配）：

* ``select_profile`` exposes ``otel-demo-ext`` through the existing A2
  ``ToolProfile.face/versions/executor_factory`` seam.  The extension keeps
  the five shipped ``otel-demo`` tool names and revision unchanged, then adds
  the three A5 names.
* The extension executor uses the existing ``urllib.request.build_opener``
  injection seam used by the A2 contract tests.  Prometheus requests from all
  three A5 tools use the existing OTel Prometheus basic-auth configuration;
  Kubernetes configuration is supplied only to let the extension factory
  start, and no Kubernetes request is expected in these tests.
* Calls go through the existing ``ToolRequest``/``ToolOutcome`` executor seam.
  The adapter emits the Prometheus v3 HTTP paths and query parameters described
  by A5; the test does not assume a private adapter function or class name.
* A5 response rows are the v3 ``/api/v1/query`` vector result, ``/series``
  ``data`` list, and ``/labels`` ``data`` list.  ``warnings`` is the source
  completeness marker, while a full 100-row result is also incomplete.
* Instant-query overflow uses the existing OTel overflow-summary seam: the
  refusal exposes series/point counts, label cardinality and the existing
  Prometheus ``topk`` suggestion text without returning partial rows.
* Alert defaults are supplied with the existing public ``AlertAnchor`` input
  seam; the default query window's endpoint is the anchored frame endpoint,
  while a normal Run defaults to the one-hour window ending at the chosen
  authorized frame endpoint.

All synthetic credentials are deliberately low-entropy placeholders.
"""

from __future__ import annotations

import base64
import io
import json
import os
import urllib.error
import urllib.request
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from opspilot.investigation.inputs import AlertAnchor
from opspilot.tools import ToolRequest, Window
from opspilot.tools import otel_demo
from opspilot.tools.profiles import PROFILE_ENV, ToolProfile, select_profile
from tests.m1_tool_support import FakeClock, RecordingSink
from tests.test_m1_otel_demo_contract import (
    FakeStore,
    NOW,
    WINDOW as FIXTURE_WINDOW,
    _lease,
)


EXT_PROFILE = "otel-demo-ext"
TARGET_ID = "m0-otel-20260909"
PROM_URL = "http://127.0.0.1:19090"
JAEGER_URL = "http://127.0.0.1:16686/jaeger/ui"
PROM_USERNAME = "investigator"
PROM_PASSWORD = "aaaaaaaa357"
K8S_TOKEN = "bbbbbbbb357"
K8S_API = "https://127.0.0.1:16443"
K8S_NAMESPACE = "otel-demo"

INSTANT_TOOL = "execute_prometheus_instant_query"
SERIES_TOOL = "get_series"
ALL_LABELS_TOOL = "get_all_labels"
A5_TOOLS = {INSTANT_TOOL, SERIES_TOOL, ALL_LABELS_TOOL}

BASE_TOOL_NAMES = (
    "metrics_range_query",
    "traces_search",
    "get_metric_names",
    "get_label_values",
    "read_evidence",
)
BASE_TOOL_SCHEMA_REVISION = "otel-demo-a3a95a0f888e"

BASIC_AUTH = "Basic " + base64.b64encode(
    f"{PROM_USERNAME}:{PROM_PASSWORD}".encode()
).decode("ascii")


@pytest.fixture(autouse=True)
def _synthetic_k8s_ca(tmp_path, monkeypatch):
    ca_file = tmp_path / "synthetic-ca.pem"
    ca_file.write_text("synthetic ca placeholder\n")
    monkeypatch.setenv("A5_TEST_K8S_CA_FILE", str(ca_file))


class _Reply:
    def __init__(self, body: bytes, status: int = 200):
        self._stream = io.BytesIO(body)
        self.status = status

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class PrometheusOpener:
    """Small urllib double recording every request and timeout."""

    def __init__(self, body: bytes, *, status: int = 200):
        self.body = body
        self.status = status
        self.requests: list[object] = []
        self.timeouts: list[float | None] = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        self.timeouts.append(timeout)
        url = _url(request)
        if self.status >= 400:
            raise urllib.error.HTTPError(
                url,
                self.status,
                "synthetic status",
                {},
                io.BytesIO(self.body),
            )
        return _Reply(self.body, self.status)


class RefusingSpillSink(RecordingSink):
    """Lets the executor build an over-limit summary without keeping rows."""

    def register_spilled(self, record, *, max_records, max_bytes):
        return False


def _url(request) -> str:
    return request.full_url if isinstance(request, urllib.request.Request) else str(request)


def _headers(request) -> dict[str, str]:
    if not isinstance(request, urllib.request.Request):
        return {}
    return {
        key.lower(): value
        for key, value in {**request.headers, **request.unredirected_hdrs}.items()
    }


def _body(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode()


def _instant_body(rows=(), *, warnings=None) -> bytes:
    payload = {
        "status": "success",
        "data": {"resultType": "vector", "result": list(rows)},
    }
    if warnings is not None:
        payload["warnings"] = warnings
    return _body(payload)


def _series_body(rows=(), *, warnings=None) -> bytes:
    payload = {"status": "success", "data": list(rows)}
    if warnings is not None:
        payload["warnings"] = warnings
    return _body(payload)


def _labels_body(labels=(), *, warnings=None) -> bytes:
    payload = {"status": "success", "data": list(labels)}
    if warnings is not None:
        payload["warnings"] = warnings
    return _body(payload)


def _env(**overrides) -> dict[str, str]:
    values = {
        PROFILE_ENV: EXT_PROFILE,
        "OPSPILOT_K8S_API_URL": K8S_API,
        "OPSPILOT_K8S_TOKEN": K8S_TOKEN,
        "OPSPILOT_K8S_CA_FILE": os.environ["A5_TEST_K8S_CA_FILE"],
        "OPSPILOT_K8S_NAMESPACE": K8S_NAMESPACE,
        "OPSPILOT_OTEL_PROMETHEUS_URL": PROM_URL,
        "OPSPILOT_OTEL_JAEGER_URL": JAEGER_URL,
        "OPSPILOT_OTEL_PROMETHEUS_USERNAME": PROM_USERNAME,
        "OPSPILOT_OTEL_PROMETHEUS_PASSWORD": PROM_PASSWORD,
    }
    values.update(overrides)
    return values


def _input(profile: ToolProfile, lease, clock: FakeClock, *, alert=None):
    return profile.face(clock).input_for(
        run_id=str(lease.run_id),
        question="Why is checkout degraded?",
        target_id=TARGET_ID,
        deadline=clock.now() + timedelta(minutes=10),
        model_requests=12,
        alert=alert,
    )


def _executor(
    monkeypatch, opener: PrometheusOpener, *, alert=None, clock=None, sink=None
):
    clock = clock or FakeClock(start=NOW)
    profile = select_profile(_env())
    assert isinstance(profile, ToolProfile)
    lease = _lease()
    store = FakeStore(lease, deadline=clock.now() + timedelta(minutes=10))
    sink = sink or RecordingSink()
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: opener)
    factory = profile.executor_factory(store, sink, clock)
    executor = factory(lease, _input(profile, lease, clock, alert=alert))
    return executor, sink, lease, clock


def _call(tool: str, params: dict, *, window: Window = FIXTURE_WINDOW) -> ToolRequest:
    return ToolRequest(
        step_id="a5-step",
        tool_index=0,
        tool_name=tool,
        target_ref=TARGET_ID,
        params=params,
        window=window.as_json(),
    )


def _query(request) -> dict[str, list[str]]:
    return parse_qs(urlsplit(_url(request)).query)


def test_extension_surface_adds_only_a5_and_otel_demo_is_byte_stable():
    base = select_profile({PROFILE_ENV: "otel-demo"})
    ext = select_profile({PROFILE_ENV: EXT_PROFILE})
    base_names = [
        schema["function"]["name"]
        for schema in base.face(FakeClock(start=NOW)).tool_schemas
    ]
    ext_names = [
        schema["function"]["name"]
        for schema in ext.face(FakeClock(start=NOW)).tool_schemas
    ]
    assert tuple(base_names) == BASE_TOOL_NAMES
    assert ext_names[: len(base_names)] == base_names
    assert set(ext_names) >= set(base_names) | A5_TOOLS
    assert base.versions()["tool_schema_revision"] == BASE_TOOL_SCHEMA_REVISION
    assert base.versions() == select_profile({PROFILE_ENV: "otel-demo"}).versions()
    assert A5_TOOLS.isdisjoint(otel_demo.PROMETHEUS_TOOLS)


def test_a5_registration_module_is_present():
    # The task record fixes the module home but leaves private registration
    # helper names open; do not couple this test to an unlisted helper name.
    from opspilot.tools import prometheus_ext

    assert prometheus_ext.__name__ == "opspilot.tools.prometheus_ext"


def test_a5_schemas_have_only_the_decided_parameters():
    profile = select_profile({PROFILE_ENV: EXT_PROFILE})
    schemas = {
        schema["function"]["name"]: schema["function"]
        for schema in profile.face(FakeClock(start=NOW)).tool_schemas
    }
    assert set(schemas[INSTANT_TOOL]["parameters"]["properties"]) == {"query", "time"}
    assert schemas[INSTANT_TOOL]["parameters"]["required"] == ["query"]
    for tool in (SERIES_TOOL, ALL_LABELS_TOOL):
        assert set(schemas[tool]["parameters"]["properties"]) == {
            "match",
            "start",
            "end",
        }
        required = schemas[tool]["parameters"].get("required", [])
        assert required == (["match"] if tool == SERIES_TOOL else [])
        assert "limit" not in schemas[tool]["parameters"]["properties"]


def test_instant_query_uses_explicit_time_at_normal_default_window_end(monkeypatch):
    wide = Window(FIXTURE_WINDOW.end - timedelta(hours=2), FIXTURE_WINDOW.end)
    opener = PrometheusOpener(_instant_body())
    executor, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(INSTANT_TOOL, {"query": "up"}, window=wide))
    assert outcome.status == "no_data", outcome.model_view
    query = _query(opener.requests[0])
    assert float(query["time"][0]) == pytest.approx(wide.end.timestamp())
    assert opener.timeouts == [30.0]
    assert query["query"] == ["up"]


def test_instant_query_alert_default_uses_alert_frame_end_not_current_time(monkeypatch):
    alert_end = FIXTURE_WINDOW.end - timedelta(minutes=10)
    alert = AlertAnchor(
        received_at=alert_end,
        starts_at=alert_end - timedelta(minutes=30),
        original="2026-09-13T23:30:00Z",
    )
    opener = PrometheusOpener(_instant_body())
    executor, _, _, clock = _executor(monkeypatch, opener, alert=alert)
    frame = executor.scope.window
    outcome = executor.execute(_call(INSTANT_TOOL, {"query": "up"}, window=frame))
    assert outcome.status == "no_data", outcome.model_view
    query = _query(opener.requests[0])
    assert float(query["time"][0]) == pytest.approx(frame.end.timestamp())
    assert frame.end < clock.now()


def test_instant_query_sends_a_supplied_time_inside_the_authorized_frame(monkeypatch):
    supplied = FIXTURE_WINDOW.start + timedelta(seconds=30)
    opener = PrometheusOpener(_instant_body())
    executor, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(
        _call(INSTANT_TOOL, {"query": "up", "time": supplied.isoformat()})
    )
    assert outcome.status == "no_data", outcome.model_view
    assert float(_query(opener.requests[0])["time"][0]) == pytest.approx(
        supplied.timestamp()
    )


@pytest.mark.parametrize(
    "bad_time",
    [
        (FIXTURE_WINDOW.start - timedelta(seconds=1)).isoformat(),
        (FIXTURE_WINDOW.end + timedelta(seconds=1)).isoformat(),
    ],
)
def test_instant_time_outside_authorized_frame_is_refused_before_request(
    monkeypatch, bad_time
):
    opener = PrometheusOpener(_instant_body())
    executor, sink, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(INSTANT_TOOL, {"query": "up", "time": bad_time}))
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    assert not opener.requests and not sink.records


@pytest.mark.parametrize("query", ["up[10m]", "up offset 1h", "up @ 10"])
def test_instant_query_rejects_lookback_before_frame_offset_and_at(
    monkeypatch, query
):
    opener = PrometheusOpener(_instant_body())
    executor, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(INSTANT_TOOL, {"query": query}))
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    assert not opener.requests


def test_instant_over_view_limit_is_refused_with_summary_and_topk(monkeypatch):
    rows = [
        {
            "metric": {"job": f"checkout-{index}", "instance": "a" * 1800},
            "value": [FIXTURE_WINDOW.end.timestamp(), "1"],
        }
        for index in range(100)
    ]
    opener = PrometheusOpener(_instant_body(rows))
    sink = RefusingSpillSink()
    executor, sink, _, _ = _executor(monkeypatch, opener, sink=sink)
    outcome = executor.execute(_call(INSTANT_TOOL, {"query": "up"}))
    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
    view = outcome.model_view
    assert view["content"] is None and not outcome.adopted
    assert sink.records == []
    assert view["series_count"] == 100
    assert view["total_points"] == 100
    assert view["label_cardinality"]
    assert "topk(" in view["suggestion"]


def test_instant_503_timeout_is_a_tool_timeout(monkeypatch):
    opener = PrometheusOpener(
        _body({"status": "error", "errorType": "timeout", "error": "timed out"}),
        status=503,
    )
    executor, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(INSTANT_TOOL, {"query": "up"}))
    assert (outcome.status, outcome.reason) == ("timeout", "TOOL_TIMEOUT")


@pytest.mark.parametrize(
    "tool,params",
    [
        (SERIES_TOOL, {}),
        (SERIES_TOOL, {"match": ""}),
        (SERIES_TOOL, {"match": "   "}),
        (SERIES_TOOL, {"match": "x" * 2001}),
        (ALL_LABELS_TOOL, {"match": ""}),
        (ALL_LABELS_TOOL, {"match": "   "}),
        (ALL_LABELS_TOOL, {"match": "x" * 2001}),
    ],
)
def test_series_and_labels_reject_missing_or_invalid_match_before_request(
    monkeypatch, tool, params
):
    opener = PrometheusOpener(_series_body())
    executor, sink, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(tool, params))
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    assert not opener.requests and not sink.records


def test_get_series_uses_required_match_limit_100_and_default_one_hour_window(
    monkeypatch,
):
    wide = Window(FIXTURE_WINDOW.end - timedelta(hours=2), FIXTURE_WINDOW.end)
    opener = PrometheusOpener(_series_body([{"__name__": "up", "job": "checkout"}]))
    executor, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(
        _call(SERIES_TOOL, {"match": '{job="checkout"}'}, window=wide)
    )
    assert outcome.status == "ok", outcome.model_view
    query = _query(opener.requests[0])
    assert query["match[]"] == ['{job="checkout"}']
    assert query["limit"] == ["100"]
    assert float(query["end"][0]) == pytest.approx(wide.end.timestamp())
    assert float(query["start"][0]) == pytest.approx(
        (wide.end - timedelta(hours=1)).timestamp()
    )


@pytest.mark.parametrize(
    "body",
    [
        _series_body([{"__name__": f"m_{index}"} for index in range(100)]),
        _series_body([{"__name__": "up"}], warnings=["results truncated due to limit"]),
    ],
)
def test_get_series_marks_full_limit_or_warnings_incomplete(monkeypatch, body):
    opener = PrometheusOpener(body)
    executor, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(SERIES_TOOL, {"match": "{__name__=~\"m_.*\"}"}))
    assert outcome.status == "ok", outcome.model_view
    assert outcome.model_view["incomplete"] is True


def test_get_all_labels_match_is_optional_and_uses_limit_and_default_window(monkeypatch):
    wide = Window(FIXTURE_WINDOW.end - timedelta(hours=2), FIXTURE_WINDOW.end)
    opener = PrometheusOpener(_labels_body(["job", "instance"]))
    executor, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(ALL_LABELS_TOOL, {}, window=wide))
    assert outcome.status == "ok", outcome.model_view
    query = _query(opener.requests[0])
    assert "match[]" not in query
    assert query["limit"] == ["100"]
    assert float(query["start"][0]) == pytest.approx(
        (wide.end - timedelta(hours=1)).timestamp()
    )
    assert float(query["end"][0]) == pytest.approx(wide.end.timestamp())


@pytest.mark.parametrize(
    "body",
    [
        _labels_body([f"label_{index}" for index in range(100)]),
        _labels_body(["job"], warnings=["results truncated due to limit"]),
    ],
)
def test_get_all_labels_marks_full_limit_or_warnings_incomplete(monkeypatch, body):
    opener = PrometheusOpener(body)
    executor, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(ALL_LABELS_TOOL, {}))
    assert outcome.status == "ok", outcome.model_view
    assert outcome.model_view["incomplete"] is True


def test_all_a5_prometheus_tools_use_basic_auth_and_no_kubernetes_or_jaeger_credential(
    monkeypatch,
):
    for tool, params, body in (
        (INSTANT_TOOL, {"query": "up"}, _instant_body()),
        (SERIES_TOOL, {"match": "{job=\"checkout\"}"}, _series_body()),
        (ALL_LABELS_TOOL, {}, _labels_body()),
    ):
        opener = PrometheusOpener(body)
        executor, _, _, _ = _executor(monkeypatch, opener)
        executor.execute(_call(tool, params))
        request = opener.requests[0]
        headers = _headers(request)
        assert headers["authorization"] == BASIC_AUTH
        assert "Bearer" not in headers["authorization"]
        assert K8S_TOKEN not in _url(request)
        assert K8S_TOKEN not in json.dumps(headers)
        assert JAEGER_URL not in _url(request)


@pytest.mark.parametrize("tool,params", [
    (INSTANT_TOOL, {"query": "up"}),
    (SERIES_TOOL, {"match": "{job=\"checkout\"}"}),
    (ALL_LABELS_TOOL, {}),
])
@pytest.mark.parametrize(
    "status,body,expected",
    [
        (400, _body({"status": "error", "errorType": "bad_data", "error": "bad"}), "INVALID_PARAMS"),
        (422, _body({"status": "error", "errorType": "bad_data", "error": "bad"}), "INVALID_PARAMS"),
        (401, b"Unauthorized", "SOURCE_ERROR"),
        (403, b"Forbidden", "SOURCE_ERROR"),
        (500, b"server error", "SOURCE_UNAVAILABLE"),
        (503, _body({"status": "error", "errorType": "unavailable"}), "SOURCE_UNAVAILABLE"),
    ],
)
def test_a5_http_error_classification_matches_discovery(monkeypatch, tool, params, status, body, expected):
    opener = PrometheusOpener(body, status=status)
    executor, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(tool, params))
    assert (outcome.status, outcome.reason) == ("error", expected)


@pytest.mark.parametrize("tool,params", [
    (INSTANT_TOOL, {"query": "up"}),
    (SERIES_TOOL, {"match": "{job=\"checkout\"}"}),
    (ALL_LABELS_TOOL, {}),
])
def test_a5_503_timeout_is_classified_as_tool_timeout(monkeypatch, tool, params):
    opener = PrometheusOpener(
        _body({"status": "error", "errorType": "timeout", "error": "timed out"}),
        status=503,
    )
    executor, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(tool, params))
    assert (outcome.status, outcome.reason) == ("timeout", "TOOL_TIMEOUT")
