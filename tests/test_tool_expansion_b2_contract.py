"""Independent contract tests for tool-surface expansion B2 (#561).

The B2 implementation is deliberately absent from this worktree.  The tests
are written from the B2 task record, decision table r3, C3 section 8 and the
Prometheus v3.5 HTTP response shapes; collection or execution is expected to
be red on ``main`` until the implementation worktree lands.

接缝假设（实现必须按公开行为满足；若实现接缝不同，应在不放宽合同的前提下
由实现者适配）：

* ``select_profile`` exposes ``otel-demo-ext`` through the existing
  ``ToolProfile.face/versions/executor_factory`` seam.  The extension keeps
  the existing ``otel-demo`` face and revision, and adds the three B2 tool
  names without requiring a particular registration helper name.
* The extension executor accepts the same ``urllib.request.build_opener``
  injection used by the A5 contract tests.  The injected opener receives one
  GET request per operation; no live Prometheus, Kubernetes or Jaeger service
  is contacted.
* Calls use the existing ``ToolRequest``/``ToolOutcome`` executor seam.  The
  adapter consumes the Prometheus v3 ``rules.groups``, ``targets.activeTargets``
  and ``metadata`` response shapes and exposes the returned records through
  the outcome's model view.  The test does not assume a private adapter class,
  method or registration function.
* B2 has no time-window parameters.  The shared executor still records its
  authorized frame, while the Prometheus source timestamps for these current
  state endpoints remain unknown (``None``).
* Prometheus HTTP authentication and error mapping are the same as A5: the
  configured basic-auth pair is used; Kubernetes and Jaeger credentials are
  never sent; 400/422 are ``INVALID_PARAMS``, 401/403 are ``SOURCE_ERROR``,
  other 5xx are ``SOURCE_UNAVAILABLE``, and a 503 JSON ``errorType=timeout``
  is ``TOOL_TIMEOUT``.

All synthetic credentials and payload canaries use low-entropy placeholders.
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

from opspilot.tools import ToolRequest, otel_demo
from opspilot.tools.profiles import PROFILE_ENV, ToolProfile, select_profile
from tests.m1_tool_support import FakeClock, RecordingSink
from tests.test_m1_otel_demo_contract import NOW, FakeStore, _lease
from tests.test_m1_otel_demo_contract import WINDOW as FIXTURE_WINDOW

EXT_PROFILE = "otel-demo-ext"
TARGET_ID = "m0-otel-20260909"
PROM_URL = "http://127.0.0.1:19090"
JAEGER_URL = "http://127.0.0.1:16686/jaeger/ui"
PROM_USERNAME = "investigator"
PROM_PASSWORD = "aaaaaaaa357"
K8S_TOKEN = "bbbbbbbb357"
K8S_API = "https://127.0.0.1:16443"
K8S_NAMESPACE = "otel-demo"

RULES_TOOL = "list_prometheus_rules"
TARGET_TOOL = "get_prometheus_target"
METADATA_TOOL = "get_metric_metadata"
B2_TOOLS = {RULES_TOOL, TARGET_TOOL, METADATA_TOOL}

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
    monkeypatch.setenv("B2_TEST_K8S_CA_FILE", str(ca_file))


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
    """urllib double recording each request and timeout."""

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


def _url(request) -> str:
    return (
        request.full_url
        if isinstance(request, urllib.request.Request)
        else str(request)
    )


def _headers(request) -> dict[str, str]:
    if not isinstance(request, urllib.request.Request):
        return {}
    return {
        key.lower(): value
        for key, value in {**request.headers, **request.unredirected_hdrs}.items()
    }


def _body(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode()


def _rules_body(groups=()) -> bytes:
    return _body({"status": "success", "data": {"groups": list(groups)}})


def _targets_body(targets=()) -> bytes:
    return _body(
        {
            "status": "success",
            "data": {"activeTargets": list(targets), "droppedTargets": []},
        }
    )


def _metadata_body(metadata: dict[str, list[dict[str, str]]]) -> bytes:
    return _body({"status": "success", "data": metadata})


def _env(**overrides) -> dict[str, str]:
    values = {
        PROFILE_ENV: EXT_PROFILE,
        "OPSPILOT_K8S_API_URL": K8S_API,
        "OPSPILOT_K8S_TOKEN": K8S_TOKEN,
        "OPSPILOT_K8S_CA_FILE": os.environ["B2_TEST_K8S_CA_FILE"],
        "OPSPILOT_K8S_NAMESPACE": K8S_NAMESPACE,
        "OPSPILOT_OTEL_PROMETHEUS_URL": PROM_URL,
        "OPSPILOT_OTEL_JAEGER_URL": JAEGER_URL,
        "OPSPILOT_OTEL_PROMETHEUS_USERNAME": PROM_USERNAME,
        "OPSPILOT_OTEL_PROMETHEUS_PASSWORD": PROM_PASSWORD,
    }
    values.update(overrides)
    return values


def _input(profile: ToolProfile, lease, clock: FakeClock):
    return profile.face(clock).input_for(
        run_id=str(lease.run_id),
        question="Why is checkout degraded?",
        target_id=TARGET_ID,
        deadline=clock.now() + timedelta(minutes=10),
        model_requests=12,
    )


def _executor(monkeypatch, opener: PrometheusOpener, *, sink=None):
    clock = FakeClock(start=NOW)
    profile = select_profile(_env())
    assert isinstance(profile, ToolProfile)
    lease = _lease()
    store = FakeStore(lease, deadline=clock.now() + timedelta(minutes=10))
    sink = sink or RecordingSink()
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: opener)
    factory = profile.executor_factory(store, sink, clock)
    executor = factory(lease, _input(profile, lease, clock))
    return executor, sink


def _call(tool: str, params: dict) -> ToolRequest:
    return ToolRequest(
        step_id="b2-step",
        tool_index=0,
        tool_name=tool,
        target_ref=TARGET_ID,
        params=params,
        window=FIXTURE_WINDOW.as_json(),
    )


def _query(request) -> dict[str, list[str]]:
    return parse_qs(urlsplit(_url(request)).query)


def _schemas() -> dict[str, dict]:
    profile = select_profile({PROFILE_ENV: EXT_PROFILE})
    return {
        schema["function"]["name"]: schema["function"]
        for schema in profile.face(FakeClock(start=NOW)).tool_schemas
    }


def _schema_text(tool: str) -> str:
    return _schemas()[tool]["description"].lower()


def _assert_current_state_description(tool: str) -> None:
    text = _schema_text(tool)
    # C3 §8: the model-visible cannot_prove text must distinguish a snapshot
    # taken now from proof about the historical incident window.
    assert ("query" in text or "current" in text or "instant" in text) and (
        "prove" in text or "histor" in text or "incident" in text
    )
    assert any(marker in text for marker in ("histor", "incident", "before", "past"))


def test_extension_surface_adds_b2_and_otel_demo_is_byte_stable():
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
    assert set(ext_names) >= set(base_names) | B2_TOOLS
    assert base.versions()["tool_schema_revision"] == BASE_TOOL_SCHEMA_REVISION
    assert base.versions() == select_profile({PROFILE_ENV: "otel-demo"}).versions()
    assert B2_TOOLS.isdisjoint(otel_demo.PROMETHEUS_TOOLS)


def test_b2_schemas_have_decided_parameters_and_no_time_window():
    schemas = _schemas()
    rules = schemas[RULES_TOOL]["parameters"]
    assert set(rules["properties"]) == {
        "type",
        "rule_name",
        "rule_group",
        "file",
        "match",
    }
    assert rules.get("required", []) == []
    assert rules["properties"]["type"]["type"] == "string"
    for name in ("rule_name", "rule_group", "file", "match"):
        assert rules["properties"][name]["type"] == "string"

    target = schemas[TARGET_TOOL]["parameters"]
    assert set(target["properties"]) == {"target_name"}
    assert target["required"] == ["target_name"]

    metadata = schemas[METADATA_TOOL]["parameters"]
    assert set(metadata["properties"]) == {"metric"}
    assert metadata.get("required", []) == []
    assert metadata["properties"]["metric"]["type"] == "string"

    for tool in B2_TOOLS:
        assert not {"time", "start", "end"} & set(
            schemas[tool]["parameters"]["properties"]
        )
        _assert_current_state_description(tool)


def test_list_rules_forwards_filters_verbatim_and_preserves_active_alerts(monkeypatch):
    active_alert = {
        "labels": {"alertname": "CheckoutDown", "job": "checkout"},
        "annotations": {"summary": "checkout is down"},
        "state": "firing",
        "value": "1",
        "activeAt": "2026-09-14T00:20:00Z",
    }
    groups = [
        {
            "name": "checkout.rules",
            "file": "/etc/prometheus/rules.yml",
            "rules": [
                {
                    "state": "firing",
                    "name": "CheckoutDown",
                    "query": 'up{job="checkout"} == 0',
                    "duration": "5m",
                    "labels": {"severity": "critical"},
                    "annotations": {"summary": "checkout is down"},
                    "alerts": [active_alert],
                    "health": "ok",
                    "type": "alerting",
                    "lastError": "",
                    "evaluationTime": 0.002,
                    "lastEvaluation": "2026-09-14T00:20:00Z",
                }
            ],
        }
    ]
    opener = PrometheusOpener(_rules_body(groups))
    executor, _ = _executor(monkeypatch, opener)
    params = {
        "type": "alert",
        "rule_name": "CheckoutDown",
        "rule_group": "checkout.rules",
        "file": "/etc/prometheus/rules.yml",
        "match": '{job="checkout"}',
    }
    outcome = executor.execute(_call(RULES_TOOL, params))
    assert outcome.status == "ok", outcome.model_view
    assert outcome.model_view["content"] == groups
    assert outcome.model_view["content"][0]["rules"][0]["alerts"] == [active_alert]
    assert outcome.model_view["source_start_at"] is None
    assert outcome.model_view["source_end_at"] is None
    query = _query(opener.requests[0])
    assert query == {
        "type": ["alert"],
        "rule_name[]": ["CheckoutDown"],
        "rule_group[]": ["checkout.rules"],
        "file[]": ["/etc/prometheus/rules.yml"],
        "match[]": ['{job="checkout"}'],
    }
    assert opener.timeouts == [30.0]


@pytest.mark.parametrize("bad_type", ["other", "alerting"])
def test_list_rules_rejects_unknown_type_before_request(monkeypatch, bad_type):
    opener = PrometheusOpener(_rules_body())
    executor, sink = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(RULES_TOOL, {"type": bad_type}))
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    assert opener.requests == []
    assert sink.records == []


def test_list_rules_accepts_record_type_and_forwards_it(monkeypatch):
    opener = PrometheusOpener(_rules_body([]))
    executor, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(RULES_TOOL, {"type": "record"}))
    assert outcome.status == "no_data", outcome.model_view
    assert _query(opener.requests[0]) == {"type": ["record"]}


def test_get_prometheus_target_queries_active_and_filters_labels_job_without_discovered_labels(
    monkeypatch,
):
    checkout = {
        "discoveredLabels": {"__address__": "10.0.0.1:9090", "job": "checkout"},
        "labels": {"job": "checkout", "instance": "checkout:8080"},
        "scrapePool": "checkout",
        "scrapeUrl": "http://checkout:8080/metrics",
        "globalUrl": "http://checkout:8080/metrics",
        "lastError": "",
        "lastScrape": "2026-09-14T00:20:00Z",
        "lastScrapeDuration": 0.012,
        "health": "up",
        "scrapeInterval": "15s",
        "scrapeTimeout": "10s",
    }
    other = {
        **checkout,
        "labels": {"job": "payments", "instance": "payments:8080"},
    }
    opener = PrometheusOpener(_targets_body([checkout, other]))
    executor, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(TARGET_TOOL, {"target_name": "checkout"}))
    assert outcome.status == "ok", outcome.model_view
    content = outcome.model_view["content"]
    assert len(content) == 1
    assert content[0]["labels"]["job"] == "checkout"
    assert "discoveredLabels" not in content[0]
    assert outcome.model_view["source_start_at"] is None
    assert outcome.model_view["source_end_at"] is None
    assert _query(opener.requests[0]) == {"state": ["active"]}
    assert opener.timeouts == [30.0]


def test_get_metric_metadata_uses_fixed_limit_and_marks_100_rows_incomplete(
    monkeypatch,
):
    metadata = {
        f"checkout_metric_{index}": [
            {"type": "gauge", "help": f"synthetic help {index}", "unit": ""}
        ]
        for index in range(100)
    }
    opener = PrometheusOpener(_metadata_body(metadata))
    executor, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(METADATA_TOOL, {}))
    assert outcome.status == "ok", outcome.model_view
    assert outcome.model_view["incomplete"] is True
    assert outcome.model_view["source_start_at"] is None
    assert outcome.model_view["source_end_at"] is None
    assert _query(opener.requests[0]) == {"limit": ["100"]}
    assert opener.timeouts == [30.0]


def test_get_metric_metadata_forwards_metric_without_time_parameters(monkeypatch):
    metadata = {
        "checkout_requests_total": [
            {"type": "counter", "help": "checkout requests", "unit": ""}
        ]
    }
    opener = PrometheusOpener(_metadata_body(metadata))
    executor, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(
        _call(METADATA_TOOL, {"metric": "checkout_requests_total"})
    )
    assert outcome.status == "ok", outcome.model_view
    rendered = json.dumps(outcome.model_view["content"], sort_keys=True)
    assert "checkout_requests_total" in rendered
    assert "checkout requests" in rendered
    assert _query(opener.requests[0]) == {
        "limit": ["100"],
        "metric": ["checkout_requests_total"],
    }
    assert all(
        key not in _query(opener.requests[0]) for key in ("time", "start", "end")
    )


def test_get_metric_metadata_rejects_invalid_metric_before_request(monkeypatch):
    opener = PrometheusOpener(_metadata_body({}))
    executor, sink = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(METADATA_TOOL, {"metric": "bad metric"}))
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    assert opener.requests == []
    assert sink.records == []


@pytest.mark.parametrize(
    "tool,params,body",
    [
        (RULES_TOOL, {}, _rules_body()),
        (TARGET_TOOL, {"target_name": "checkout"}, _targets_body()),
        (METADATA_TOOL, {}, _metadata_body({})),
    ],
)
def test_all_b2_tools_use_prometheus_basic_auth_without_other_source_credentials(
    monkeypatch, tool, params, body
):
    opener = PrometheusOpener(body)
    executor, _ = _executor(monkeypatch, opener)
    executor.execute(_call(tool, params))
    request = opener.requests[0]
    headers = _headers(request)
    assert headers["authorization"] == BASIC_AUTH
    assert "Bearer" not in headers["authorization"]
    assert K8S_TOKEN not in _url(request)
    assert K8S_TOKEN not in json.dumps(headers)
    assert JAEGER_URL not in _url(request)


@pytest.mark.parametrize(
    "tool,params,body",
    [
        (RULES_TOOL, {}, _rules_body()),
        (TARGET_TOOL, {"target_name": "checkout"}, _targets_body()),
        (METADATA_TOOL, {}, _metadata_body({})),
    ],
)
@pytest.mark.parametrize(
    "status,body_override,expected",
    [
        (
            400,
            _body({"status": "error", "errorType": "bad_data", "error": "bad"}),
            "INVALID_PARAMS",
        ),
        (
            422,
            _body({"status": "error", "errorType": "bad_data", "error": "bad"}),
            "INVALID_PARAMS",
        ),
        (401, b"Unauthorized", "SOURCE_ERROR"),
        (403, b"Forbidden", "SOURCE_ERROR"),
        (500, b"server error", "SOURCE_UNAVAILABLE"),
        (
            503,
            _body({"status": "error", "errorType": "unavailable"}),
            "SOURCE_UNAVAILABLE",
        ),
    ],
)
def test_b2_http_error_classification_matches_a5(
    monkeypatch, tool, params, body, status, body_override, expected
):
    opener = PrometheusOpener(body_override, status=status)
    executor, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(tool, params))
    assert (outcome.status, outcome.reason) == ("error", expected)


@pytest.mark.parametrize(
    "tool,params,body",
    [
        (RULES_TOOL, {}, _rules_body()),
        (TARGET_TOOL, {"target_name": "checkout"}, _targets_body()),
        (METADATA_TOOL, {}, _metadata_body({})),
    ],
)
def test_b2_503_timeout_is_classified_as_tool_timeout(monkeypatch, tool, params, body):
    opener = PrometheusOpener(
        _body({"status": "error", "errorType": "timeout", "error": "timed out"}),
        status=503,
    )
    executor, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(tool, params))
    assert (outcome.status, outcome.reason) == ("timeout", "TOOL_TIMEOUT")
