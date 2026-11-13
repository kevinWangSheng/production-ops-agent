"""独立合同测试：工具面拓展 A4（Jaeger trace 查询，issue #556）。

本文件从 A4 开工说明、决定表 r3（R1--R17、N3、A4-1--A4-12）和 C3
第 8 节编写。起点是 ``main``，A4 实现位于另一个 worktree；因此在本
worktree 上 collection 或断言失败都是预期的红灯。

接缝假设（实现者若采用不同内部结构，应保持以下公开行为）：

* ``select_profile`` 继续返回 ``ToolProfile``，扩展档位通过现有的
  ``face(clock)``、``versions()`` 和 ``executor_factory(store, sink, clock)``
  接缝构造；A4 的四个工具只从 ``otel-demo-ext`` 暴露。
* 替身传输沿用 A2/M1 的公开测试接缝：monkeypatch
  ``urllib.request.build_opener``，传输从该 opener 取得
  ``urllib.request.Request``，并把 Jaeger 的 GET 请求交给它；测试不构造
  A4 的传输类，也不依赖其私有函数名或构造参数。
* 执行请求仍使用 ``ToolRequest``、现有固定授权 ``Window``、M1 的
  ``FakeStore``/``RecordingSink``，结果仍通过通用 ``ToolOutcome.model_view``
  和记录 sink 观察。
* Jaeger 使用 OTel Demo 目标的匿名查询端口；四个新工具不把 Prometheus
  basic/bearer 或 Kubernetes bearer 放进请求头。
* 按 ID 查询和按标签搜索复用既有 trace 投影的可见 span 白名单；扩展
  投影标识写入保存的投影记录，值为 ``otel-demo-ext-traces-v1``。
* 404、400、5xx 的结果分类由注册表/传输映射到既有
  ``ToolOutcome`` 合同；``INVALID_TAGS`` 是 A4 新增的固定输入原因码。

所有凭据都是低熵合成占位值；本文件不启动容器、不访问真实 Jaeger。
"""

from __future__ import annotations

import io
import json
import os
import shutil
import urllib.error
import urllib.request
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

import opspilot.tools.otel_demo as otel_demo
from opspilot.tools import ToolRequest
from opspilot.tools.profiles import PROFILE_ENV, ToolProfile, select_profile
from tests.m1_tool_support import FakeClock, RecordingSink
from tests.test_m1_otel_demo_contract import (
    FakeStore,
    NOW,
    WINDOW as AUTHORIZED_WINDOW,
    _lease,
)

EXT_PROFILE = "otel-demo-ext"
BASE_TOOL_NAMES = (
    "metrics_range_query",
    "traces_search",
    "get_metric_names",
    "get_label_values",
    "read_evidence",
)
K8S_JQ_TOOL = "kubernetes_jq_query"
K8S_COUNT_TOOL = "kubernetes_count"
TRACE_SEARCH = "traces_search_by_tags"
TRACE_BY_ID = "traces_query_trace_by_id"
LIST_SERVICES = "traces_list_services"
LIST_OPERATIONS = "traces_list_operations"
NEW_TOOLS = {TRACE_SEARCH, TRACE_BY_ID, LIST_SERVICES, LIST_OPERATIONS}
EXT_EXISTING_TOOLS = set(BASE_TOOL_NAMES) | {K8S_JQ_TOOL, K8S_COUNT_TOOL}
OTEL_DEMO_SCHEMA_REVISION = "otel-demo-a3a95a0f888e"
EXT_TRACE_PROJECTION = "otel-demo-ext-traces-v1"
TRACE_ID = "a" * 32
OTEL_TOKEN = "otel-aaaaaaaa357"
K8S_TOKEN = "k8s-aaaaaaaa357"


@pytest.fixture(autouse=True)
def _synthetic_k8s_ca(tmp_path, monkeypatch):
    """The reused A2 executor seam needs a readable synthetic CA path."""
    ca_file = tmp_path / "synthetic-ca.pem"
    ca_file.write_text("synthetic ca placeholder\n")
    monkeypatch.setenv("A2_TEST_K8S_CA_FILE", str(ca_file))


class JaegerOpener:
    """Small urllib double matching exact paths or path prefixes."""

    def __init__(self, routes=(), *, statuses=()):
        self.routes = dict(routes)
        self.statuses = dict(statuses)
        self.requests: list[object] = []
        self.timeouts: list[float | None] = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        self.timeouts.append(timeout)
        url = _url(request)
        path = urlsplit(url).path
        for suffix, (status, body) in self.statuses.items():
            if path == suffix or path.endswith(suffix):
                raise urllib.error.HTTPError(
                    url, status, "synthetic status", {}, io.BytesIO(body)
                )
        value = self.routes.get(path)
        if value is None:
            for suffix, candidate in self.routes.items():
                if path == suffix or path.endswith(suffix) or path.startswith(suffix):
                    value = candidate
                    break
        if value is None:
            raise AssertionError(f"unexpected Jaeger request: {url}")
        if isinstance(value, BaseException):
            raise value
        return _reply(value)


def _reply(body: bytes, status: int = 200):
    class Reply:
        def __init__(self):
            self._stream = io.BytesIO(body)
            self.status = status

        def read(self, size: int = -1) -> bytes:
            return self._stream.read(size)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    return Reply()


def _url(request) -> str:
    return request.full_url if isinstance(request, urllib.request.Request) else str(request)


def _headers(request) -> dict[str, str]:
    if not isinstance(request, urllib.request.Request):
        return {}
    return {
        key.lower(): value
        for key, value in {**request.headers, **request.unredirected_hdrs}.items()
    }


def _epoch(moment) -> int:
    return int(moment.timestamp() * 1_000_000)


def _span(
    trace_id: str,
    span_id: str,
    start,
    *,
    service: str = "payments",
    operation: str = "charge",
    duration_us: int = 25_000,
    tags=(),
    process_id: str = "p1",
    extra: dict | None = None,
) -> dict:
    value = {
        "traceID": trace_id,
        "spanID": span_id,
        "operationName": operation,
        "startTime": _epoch(start),
        "duration": duration_us,
        "tags": list(tags),
        "logs": [],
        "references": [],
        "processID": process_id,
    }
    if extra:
        value.update(extra)
    return value


def _trace_payload(spans: list[dict], *, service: str = "payments", errors=None) -> bytes:
    return json.dumps(
        {
            "data": [
                {
                    "traceID": spans[0]["traceID"] if spans else TRACE_ID,
                    "spans": spans,
                    "processes": {
                        "p1": {
                            "serviceName": service,
                            "tags": [{"key": "service.version", "value": "v1"}],
                        }
                    },
                }
            ]
            if spans
            else [],
            "total": 1 if spans else 0,
            "errors": [] if errors is None else errors,
        },
        separators=(",", ":"),
    ).encode()


def _search_payload(count: int = 1, *, service: str = "payments", errors=None) -> bytes:
    start = AUTHORIZED_WINDOW.start + timedelta(seconds=10)
    traces = []
    for index in range(count):
        trace_id = f"{index + 1:032x}"
        traces.append(
            {
                "traceID": trace_id,
                "spans": [
                    _span(
                        trace_id,
                        f"{index + 1:016x}",
                        start + timedelta(seconds=index),
                        service=service,
                        tags=[
                            {"key": "http.status_code", "type": "int64", "value": 500},
                            {"key": "retry", "type": "bool", "value": True},
                        ],
                        extra={"secret_field": "must-not-be-visible"},
                    )
                ],
                "processes": {"p1": {"serviceName": service, "tags": []}},
            }
        )
    return json.dumps(
        {"data": traces, "total": count, "errors": [] if errors is None else errors},
        separators=(",", ":"),
    ).encode()


def _call(tool: str, params: dict, *, index: int = 0) -> ToolRequest:
    return ToolRequest(
        step_id="a4-step",
        tool_index=index,
        tool_name=tool,
        target_ref=otel_demo.TARGET_ID,
        params=params,
        window=AUTHORIZED_WINDOW.as_json(),
    )


def _input(profile: ToolProfile, lease, clock: FakeClock):
    return profile.face(clock).input_for(
        run_id=str(lease.run_id),
        question="Why is checkout degraded?",
        target_id=otel_demo.TARGET_ID,
        deadline=NOW + timedelta(minutes=10),
        model_requests=8,
    )


def _executor(monkeypatch, opener):
    if shutil.which("jq") is None:
        pytest.skip("local jq is required for the otel-demo-ext worker seam")
    env = {
        PROFILE_ENV: EXT_PROFILE,
        "OPSPILOT_K8S_API_URL": "https://127.0.0.1:16443",
        "OPSPILOT_K8S_TOKEN": K8S_TOKEN,
        "OPSPILOT_K8S_CA_FILE": os.environ["A2_TEST_K8S_CA_FILE"],
        "OPSPILOT_K8S_NAMESPACE": "otel-demo",
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://127.0.0.1:19090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://127.0.0.1:16686/jaeger/ui",
        "OPSPILOT_OTEL_TOKEN": OTEL_TOKEN,
    }
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: opener)
    profile = select_profile(env)
    assert isinstance(profile, ToolProfile)
    lease = _lease()
    store = FakeStore(lease, deadline=NOW + timedelta(minutes=10))
    clock = FakeClock(start=NOW)
    sink = RecordingSink()
    factory = profile.executor_factory(store, sink, clock)
    executor = factory(lease, _input(profile, lease, clock))
    return executor, sink, lease, clock, profile


def _schema_map(profile_name: str = EXT_PROFILE):
    profile = select_profile({PROFILE_ENV: profile_name})
    return {
        schema["function"]["name"]: schema["function"]
        for schema in profile.face(FakeClock(start=NOW)).tool_schemas
    }


def _content(outcome):
    view = _view(outcome)
    return view["content"]


def _view(outcome):
    assert outcome.model_view is not None
    return outcome.model_view


def _projection_from_sink(sink) -> dict:
    assert sink.records
    return json.loads(sink.records[-1].raw.decode())


def test_extension_surface_is_existing_ext_plus_four_and_otel_demo_is_pinned():
    base = select_profile({PROFILE_ENV: "otel-demo"})
    ext = select_profile({PROFILE_ENV: EXT_PROFILE})
    base_face = base.face(FakeClock(start=NOW))
    ext_face = ext.face(FakeClock(start=NOW))
    base_names = [schema["function"]["name"] for schema in base_face.tool_schemas]
    ext_names = [schema["function"]["name"] for schema in ext_face.tool_schemas]

    assert tuple(base_names) == tuple(otel_demo.TOOL_NAMES)
    assert set(ext_names) >= EXT_EXISTING_TOOLS | NEW_TOOLS
    assert ext_names[: len(base_names)] == base_names
    assert base.versions()["tool_schema_revision"] == OTEL_DEMO_SCHEMA_REVISION
    assert base.versions() == select_profile({PROFILE_ENV: "otel-demo"}).versions()
    assert ext.versions()["tool_schema_revision"].startswith("otel-demo-ext-")

    base_schemas = {
        item["function"]["name"]: item
        for item in base_face.tool_schemas
    }
    ext_schemas = {
        item["function"]["name"]: item
        for item in ext_face.tool_schemas
    }
    assert base_schemas["traces_search"] == next(
        item for item in otel_demo.TOOL_SCHEMAS
        if item["function"]["name"] == "traces_search"
    )
    assert ext_schemas["traces_search"] == base_schemas["traces_search"]


def test_a4_schemas_describe_parameters_and_red_lines():
    schemas = _schema_map()
    assert set(schemas[TRACE_SEARCH]["parameters"]["properties"]) == {
        "service", "tags", "operation", "min_duration", "max_duration",
        "limit", "start", "end",
    }
    assert schemas[TRACE_SEARCH]["parameters"]["required"] == ["service"]
    assert set(schemas[TRACE_BY_ID]["parameters"]["properties"]) == {
        "trace_id", "start", "end"
    }
    assert schemas[TRACE_BY_ID]["parameters"]["required"] == ["trace_id"]
    assert set(schemas[LIST_SERVICES]["parameters"]["properties"]) == set()
    assert set(schemas[LIST_OPERATIONS]["parameters"]["properties"]) == {
        "service", "span_kind"
    }
    assert schemas[LIST_OPERATIONS]["parameters"]["required"] == ["service"]

    search_text = schemas[TRACE_SEARCH]["description"].lower()
    assert "span" in search_text and "duration" in search_text
    assert "same" in search_text or "one span" in search_text or "individual" in search_text
    assert "service" in search_text and ("empty" in search_text or "check" in search_text)
    list_text = schemas[LIST_SERVICES]["description"].lower()
    assert ("startup" in list_text or "restart" in list_text) and (
        "cannot" in list_text or "not prove" in list_text
    )
    operations_text = schemas[LIST_OPERATIONS]["description"].lower()
    assert ("startup" in operations_text or "restart" in operations_text) and (
        "cannot" in operations_text or "not prove" in operations_text
    )


@pytest.mark.parametrize("service", ["", "x" * 201, "payments\ninternal", "payments\x00internal"])
def test_search_service_validation_is_before_request(monkeypatch, service):
    opener = JaegerOpener(routes={"/api/traces": _search_payload()})
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(TRACE_SEARCH, {"service": service}))
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    assert opener.requests == []


def test_search_accepts_non_enumerated_service_and_sends_default_limit_tags_and_duration(
    monkeypatch,
):
    service = "payments-custom"
    opener = JaegerOpener(routes={"/api/traces": _search_payload(service=service)})
    executor, sink, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(
        _call(
            TRACE_SEARCH,
            {
                "service": service,
                "tags": 'http.status_code="500" retry=true',
                "operation": "charge",
                "min_duration": "10ms",
                "max_duration": "2s",
                "start": AUTHORIZED_WINDOW.start.isoformat(),
                "end": AUTHORIZED_WINDOW.end.isoformat(),
            },
        )
    )
    assert outcome.status == "ok"
    request = opener.requests[0]
    query = parse_qs(urlsplit(_url(request)).query)
    assert query["service"] == [service]
    assert query["limit"] == ["20"]
    expected_tags = {"http.status_code": "500", "retry": "true"}
    parsed_tags = None
    if len(query.get("tags", ())) == 1:
        try:
            parsed_tags = json.loads(query["tags"][0])
        except (TypeError, ValueError):
            parsed_tags = None
    assert parsed_tags == expected_tags or set(query.get("tag", ())) == {
        "http.status_code:500",
        "retry:true",
    }
    assert query["operation"] == ["charge"]
    assert query["minDuration"] == ["10ms"]
    assert query["maxDuration"] == ["2s"]
    assert "start" in query and "end" in query
    assert _projection_from_sink(sink)["projection"] == EXT_TRACE_PROJECTION
    assert "secret_field" not in json.dumps(_content(outcome))


def test_search_invalid_logfmt_and_reversed_or_bad_durations_never_send(monkeypatch):
    opener = JaegerOpener(routes={"/api/traces": _search_payload()})
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    cases = [
        ({"service": "payments", "tags": 'http.status_code="500'}, "INVALID_TAGS"),
        ({"service": "payments", "min_duration": "5"}, "INVALID_PARAMS"),
        ({"service": "payments", "min_duration": "3s", "max_duration": "2s"}, "INVALID_PARAMS"),
    ]
    for params, reason in cases:
        outcome = executor.execute(_call(TRACE_SEARCH, params, index=len(opener.requests)))
        assert (outcome.status, outcome.reason) == ("error", reason)
    assert opener.requests == []


def test_search_default_limit_full_result_is_marked_incomplete_and_empty_mentions_service(
    monkeypatch,
):
    full = JaegerOpener(routes={"/api/traces": _search_payload(20)})
    executor, _, _, _, _ = _executor(monkeypatch, full)
    outcome = executor.execute(_call(TRACE_SEARCH, {"service": "payments"}))
    assert outcome.status == "ok"
    assert _view(outcome)["incomplete"] is True

    empty = JaegerOpener(routes={"/api/traces": _search_payload(0)})
    executor, _, _, _, _ = _executor(monkeypatch, empty)
    outcome = executor.execute(_call(TRACE_SEARCH, {"service": "payments"}))
    assert outcome.status == "no_data"
    text = json.dumps(_view(outcome)).lower()
    assert "service" in text and (
        "check" in text
        or "verify" in text
        or "traces_list_services" in text
        or "list services" in text
    )


def test_search_limit_keeps_the_newest_results_when_backend_returns_more_than_requested(
    monkeypatch,
):
    opener = JaegerOpener(routes={"/api/traces": _search_payload(3)})
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(
        _call(TRACE_SEARCH, {"service": "payments", "limit": 2})
    )
    assert outcome.status == "ok"
    content = _content(outcome)
    assert len(content) == 2
    assert {row["trace_id"] for row in content} == {
        "00000000000000000000000000000002",
        "00000000000000000000000000000003",
    }


def test_query_by_id_validates_hex_id_and_filters_out_of_window_spans_in_time_order(
    monkeypatch,
):
    before = AUTHORIZED_WINDOW.start - timedelta(seconds=5)
    first = AUTHORIZED_WINDOW.start + timedelta(seconds=4)
    second = AUTHORIZED_WINDOW.start + timedelta(seconds=2)
    spans = [
        _span(TRACE_ID, "0000000000000001", first),
        _span(TRACE_ID, "0000000000000002", before),
        _span(TRACE_ID, "0000000000000003", second),
    ]
    opener = JaegerOpener(routes={"/api/traces/" + TRACE_ID: _trace_payload(spans)})
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(TRACE_BY_ID, {"trace_id": TRACE_ID}))
    assert outcome.status == "ok"
    view = _view(outcome)
    assert view["window"] == AUTHORIZED_WINDOW.as_json()
    content = _content(outcome)
    assert len(content) == 2
    assert [row["start_us"] for row in content] == sorted(row["start_us"] for row in content)
    assert "1" in json.dumps(view) and ("drop" in json.dumps(view).lower() or "outside" in json.dumps(view).lower())
    assert urlsplit(_url(opener.requests[0])).path.endswith("/api/traces/" + TRACE_ID)


@pytest.mark.parametrize("trace_id", ["", "g" * 32, "a" * 33])
def test_query_by_id_rejects_non_hex_or_wrong_length_without_request(monkeypatch, trace_id):
    opener = JaegerOpener(routes={"/api/traces/" + TRACE_ID: _trace_payload([])})
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(TRACE_BY_ID, {"trace_id": trace_id}))
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    assert opener.requests == []


def test_query_by_id_404_is_successful_no_data_with_ring_limitation(monkeypatch):
    opener = JaegerOpener(
        statuses={"/api/traces/" + TRACE_ID: (404, b'{"errors":[]}')}
    )
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(TRACE_BY_ID, {"trace_id": TRACE_ID}))
    assert (outcome.status, outcome.reason) == ("no_data", "NO_DATA")
    text = json.dumps(_view(outcome)).lower()
    assert "5000" in text and ("cannot" in text or "not prove" in text)


def test_query_by_id_never_samples_and_keeps_projection_whitelist(monkeypatch):
    start = AUTHORIZED_WINDOW.start + timedelta(seconds=1)
    spans = [
        _span(
            TRACE_ID,
            f"{index:016x}",
            start + timedelta(seconds=index),
            extra={"unknown_span_field": "do-not-return"},
        )
        for index in range(1, 4)
    ]
    opener = JaegerOpener(routes={"/api/traces/" + TRACE_ID: _trace_payload(spans)})
    executor, sink, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(TRACE_BY_ID, {"trace_id": TRACE_ID}))
    assert len(_content(outcome)) == 3
    allowed = {
        "trace_id", "span_id", "service", "service_version", "operation",
        "operation_truncated", "start_us", "duration_us", "error_by_visible_tags",
        "status_tags", "status_state", "parent_references", "error_details",
        "raw_error_detail_field_count", "omitted_error_detail_field_count",
    }
    assert all(set(row) <= allowed for row in _content(outcome))
    assert "unknown_span_field" not in json.dumps(_content(outcome))
    assert _projection_from_sink(sink)["projection"] == EXT_TRACE_PROJECTION


def test_query_by_id_raw_read_limit_and_view_limit_refuse_whole_result(monkeypatch):
    huge = b"{" + b"x" * (4 * 1024 * 1024) + b"}"
    opener = JaegerOpener(routes={"/api/traces/" + TRACE_ID: huge})
    executor, sink, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(TRACE_BY_ID, {"trace_id": TRACE_ID}))
    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
    assert _view(outcome)["content"] is None
    assert sink.records == []

    spans = [
        _span(
            TRACE_ID,
            f"{index:016x}",
            AUTHORIZED_WINDOW.start + timedelta(seconds=1),
            operation="x" * 150,
            extra={"tags_blob": "y" * 160},
        )
        for index in range(1, 701)
    ]
    opener = JaegerOpener(routes={"/api/traces/" + TRACE_ID: _trace_payload(spans)})
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(TRACE_BY_ID, {"trace_id": TRACE_ID}))
    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
    assert _view(outcome)["content"] is None


def test_services_and_operations_have_no_time_parameters_and_leave_source_time_empty(
    monkeypatch,
):
    long_name = "operation-" + "z" * 180
    opener = JaegerOpener(
        routes={
            "/api/services": json.dumps(
                {"data": ["payments", "checkout"], "total": 2, "errors": []}
            ).encode(),
            "/api/services/payments/operations": json.dumps(
                {
                    "data": [{"name": long_name, "spanKind": "server"}],
                    "total": 1,
                    "errors": [],
                }
            ).encode(),
            "/api/operations": json.dumps(
                {
                    "data": [{"name": long_name, "spanKind": "server"}],
                    "total": 1,
                    "errors": [],
                }
            ).encode(),
        }
    )
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    services = executor.execute(_call(LIST_SERVICES, {}, index=0))
    operations = executor.execute(
        _call(LIST_OPERATIONS, {"service": "payments", "span_kind": "server"}, index=1)
    )
    assert services.status == "ok" and operations.status == "ok"
    for outcome in (services, operations):
        view = _view(outcome)
        assert view["source_start_at"] is None
        assert view["source_end_at"] is None
        assert view["data_as_of"] is None
    assert "payments" in _content(services)
    rows = _content(operations)
    names = [row.get("name", row) if isinstance(row, dict) else row for row in rows]
    assert len(names[0]) == 150
    operation_request = opener.requests[1]
    operation_parts = urlsplit(_url(operation_request))
    query = parse_qs(operation_parts.query)
    assert query["spanKind"] == ["server"]
    assert (
        query.get("service") == ["payments"]
        or operation_parts.path.endswith("/api/services/payments/operations")
    )
    assert "start" not in query and "end" not in query

    schemas = _schema_map()
    assert "start" not in schemas[LIST_SERVICES]["parameters"]["properties"]
    assert "end" not in schemas[LIST_SERVICES]["parameters"]["properties"]
    assert "start" not in schemas[LIST_OPERATIONS]["parameters"]["properties"]
    assert "end" not in schemas[LIST_OPERATIONS]["parameters"]["properties"]


def test_list_tools_reject_model_time_parameters_before_request(monkeypatch):
    opener = JaegerOpener(routes={"/api/services": b'{"data":[],"errors":[]}'})
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(
        _call(LIST_SERVICES, {"start": AUTHORIZED_WINDOW.start.isoformat()})
    )
    assert (outcome.status, outcome.reason) == ("denied", "PARAM_NOT_ALLOWED")
    assert opener.requests == []


@pytest.mark.parametrize(
    ("tool", "path", "params"),
    [
        (TRACE_SEARCH, "/api/traces", {"service": "payments"}),
        (TRACE_BY_ID, "/api/traces/" + TRACE_ID, {"trace_id": TRACE_ID}),
        (LIST_SERVICES, "/api/services", {}),
        (LIST_OPERATIONS, "/api/services/payments/operations", {"service": "payments"}),
    ],
)
@pytest.mark.parametrize("status,reason", [(400, "INVALID_PARAMS"), (500, "SOURCE_UNAVAILABLE")])
def test_a4_http_errors_use_fixed_classification(monkeypatch, tool, path, params, status, reason):
    statuses = {path: (status, b'{"errors":["bad"]}')}
    if tool == LIST_OPERATIONS:
        statuses["/api/operations"] = (status, b'{"errors":["bad"]}')
    opener = JaegerOpener(statuses=statuses)
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(tool, params))
    assert (outcome.status, outcome.reason) == ("error", reason)


def test_nonempty_jaeger_errors_mark_a_result_incomplete(monkeypatch):
    body = _search_payload(errors=[{"code": "partial", "message": "partial"}])
    opener = JaegerOpener(routes={"/api/traces": body})
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(TRACE_SEARCH, {"service": "payments"}))
    assert outcome.status == "ok"
    assert _view(outcome)["incomplete"] is True


def test_new_jaeger_tools_are_anonymous_and_do_not_forward_prom_or_k8s_credentials(
    monkeypatch,
):
    spans = [_span(TRACE_ID, "0000000000000001", AUTHORIZED_WINDOW.start)]
    opener = JaegerOpener(
        routes={
            "/api/traces": _search_payload(),
            "/api/traces/" + TRACE_ID: _trace_payload(spans),
            "/api/services": b'{"data":["payments"],"total":1,"errors":[]}',
            "/api/services/payments/operations": b'{"data":[],"total":0,"errors":[]}',
        }
    )
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    calls = [
        (TRACE_SEARCH, {"service": "payments"}),
        (TRACE_BY_ID, {"trace_id": TRACE_ID}),
        (LIST_SERVICES, {}),
        (LIST_OPERATIONS, {"service": "payments"}),
    ]
    for index, (tool, params) in enumerate(calls):
        executor.execute(_call(tool, params, index=index))
    assert len(opener.requests) == 4
    for request in opener.requests:
        headers = _headers(request)
        assert "authorization" not in headers
        assert OTEL_TOKEN not in json.dumps(headers)
        assert K8S_TOKEN not in json.dumps(headers)

