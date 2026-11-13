"""Implementer unit tests for tool expansion B2 (#561): Prometheus rules,
scrape targets and metric metadata in ``otel-demo-ext``, and the lab proxy's
three new GET paths.

Driven through the profile's executor with the urllib opener double of the A2
contract tests, the same seam as the A5 implementer tests.
"""

from __future__ import annotations

import base64
import io
import json
import urllib.error
from urllib.parse import parse_qs, urlsplit

import pytest

from opspilot.tools import otel_demo, otel_demo_ext, prometheus_ext
from opspilot.tools.profiles import PROFILE_ENV, select_profile
from scripts import f7_authz_probe as probe
from scripts import kind_lab as lab
from tests.m1_tool_support import FakeClock
from tests.test_m1_otel_demo_contract import NOW
from tests.test_tool_expansion_a2_contract import (
    BASE_TOOL_SCHEMA_REVISION,
    KubeOpener,
    _call,
    _executor,
    _headers,
    _synthetic_k8s_ca,  # noqa: F401  (autouse fixture: the A2 CA file)
    _url,
    _view,
)

RULES = prometheus_ext.RULES_TOOL
TARGET = prometheus_ext.TARGET_TOOL
METADATA = prometheus_ext.METADATA_TOOL
B2 = (RULES, TARGET, METADATA)
PROM_USER = "aaaaaaaa357"
PROM_PASSWORD = "bbbbbbbb357"


def _envelope(data, *, warnings=None) -> bytes:
    body: dict = {"status": "success", "data": data}
    if warnings:
        body["warnings"] = warnings
    return json.dumps(body).encode()


def _query(opener, index=0) -> dict[str, list[str]]:
    return parse_qs(urlsplit(_url(opener.requests[index])).query)


def _path(opener, index=0) -> str:
    return urlsplit(_url(opener.requests[index])).path


def _groups() -> dict:
    return {
        "groups": [
            {
                "name": "checkout",
                "file": "/etc/prometheus/rules.yml",
                "rules": [
                    {
                        "name": "CheckoutErrors",
                        "type": "alerting",
                        "query": "rate(errors[5m]) > 1",
                        "state": "firing",
                        "alerts": [
                            {
                                "labels": {"alertname": "CheckoutErrors"},
                                "state": "firing",
                            }
                        ],
                    }
                ],
            }
        ]
    }


def _targets() -> dict:
    return {
        "activeTargets": [
            {
                "labels": {"job": "otel-collector", "instance": "c:8888"},
                "discoveredLabels": {"__meta_big": "x" * 50},
                "scrapePool": "otel-collector",
                "health": "up",
                "lastError": "",
            },
            {"labels": {"job": "other", "instance": "o:1"}, "health": "down"},
        ],
        "droppedTargets": [],
    }


# -- profile surface -----------------------------------------------------------


def test_b2_tools_join_only_the_extension():
    assert not set(B2) & set(otel_demo.TOOL_NAMES)
    assert not set(B2) & otel_demo.PROMETHEUS_TOOLS
    assert otel_demo.TOOL_SCHEMA_REVISION == BASE_TOOL_SCHEMA_REVISION
    assert set(B2) <= set(otel_demo_ext.TOOL_NAMES)
    assert set(B2) <= prometheus_ext.PROMETHEUS_EXT_TOOLS
    ext = select_profile({PROFILE_ENV: "otel-demo-ext"})
    face = ext.face(FakeClock(start=NOW))
    names = {s["function"]["name"] for s in face.tool_schemas}
    assert set(B2) <= names
    available = face.evidence_context("run-1")["data_sources"]["available"]
    assert set(B2) <= set(available["prometheus"])


def test_b2_schemas_match_registrations_and_state_the_query_moment():
    schemas = {
        s["function"]["name"]: s["function"] for s in prometheus_ext.TOOL_SCHEMAS
    }
    registrations = {r.name: r for r in prometheus_ext.prometheus_ext_registrations()}
    assert set(schemas[RULES]["parameters"]["properties"]) == {
        "type",
        "rule_name",
        "rule_group",
        "file",
        "match",
    }
    assert schemas[RULES]["parameters"]["required"] == []
    assert set(schemas[TARGET]["parameters"]["properties"]) == {"target_name"}
    assert schemas[TARGET]["parameters"]["required"] == ["target_name"]
    assert set(schemas[METADATA]["parameters"]["properties"]) == {"metric"}
    for name in B2:
        assert "moment of the query" in schemas[name]["description"]
        assert "jq" not in schemas[name]["parameters"]["properties"]
        registration = registrations[name]
        assert registration.request_timeout_seconds == 30.0
        assert registration.source == otel_demo.SOURCE
        assert set(registration.parameters) == set(
            schemas[name]["parameters"]["properties"]
        )


# -- list_prometheus_rules -----------------------------------------------------------


def test_rules_forward_filters_with_basic_auth_and_keep_exact_bytes(monkeypatch):
    body = _envelope(_groups())
    opener = KubeOpener([body])
    env = {
        "OPSPILOT_OTEL_PROMETHEUS_USERNAME": PROM_USER,
        "OPSPILOT_OTEL_PROMETHEUS_PASSWORD": PROM_PASSWORD,
    }
    executor, sink, *_ = _executor(monkeypatch, opener, env=env)
    params = {
        "type": "alert",
        "rule_name": "CheckoutErrors",
        "rule_group": "checkout",
        "file": "/etc/prometheus/rules.yml",
        "match": '{severity="critical"}',
    }
    outcome = executor.execute(_call(RULES, params))
    assert outcome.status == "ok", outcome.model_view
    assert _path(opener).endswith("/api/v1/rules")
    assert _query(opener) == {
        "type": ["alert"],
        "rule_name[]": ["CheckoutErrors"],
        "rule_group[]": ["checkout"],
        "file[]": ["/etc/prometheus/rules.yml"],
        "match[]": ['{severity="critical"}'],
    }
    pair = base64.b64encode(f"{PROM_USER}:{PROM_PASSWORD}".encode()).decode()
    assert _headers(opener.requests[0])["authorization"] == f"Basic {pair}"
    view = _view(outcome)
    assert view["content"][0]["rules"][0]["alerts"][0]["state"] == "firing"
    assert view["source_start_at"] is None and view["data_as_of"] is None
    assert view["incomplete"] is False and view["incomplete_reason"] is None
    assert view["notes"] == [prometheus_ext.STATE_NOTE]
    assert sink.records[-1].raw == body


def test_rules_without_filters_send_no_query(monkeypatch):
    opener = KubeOpener([_envelope({"groups": []})])
    executor, *_ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(RULES, {}))
    assert outcome.status == "no_data", outcome.model_view
    assert urlsplit(_url(opener.requests[0])).query == ""
    assert _view(outcome)["content"] == []


@pytest.mark.parametrize(
    "data_warnings",
    [({**_groups(), "groupNextToken": "abc"}, None), (_groups(), ["partial"])],
)
def test_rules_next_page_token_or_warnings_are_incomplete(monkeypatch, data_warnings):
    data, warnings = data_warnings
    opener = KubeOpener([_envelope(data, warnings=warnings)])
    executor, *_ = _executor(monkeypatch, opener)
    view = _view(executor.execute(_call(RULES, {})))
    assert view["incomplete"] is True
    assert view["incomplete_reason"] == prometheus_ext.RULES_INCOMPLETE_REASON


@pytest.mark.parametrize(
    "params",
    [
        {"type": "alerting"},
        {"type": "ALERT"},
        {"rule_name": ""},
        {"rule_group": "  "},
        {"file": "a\nb"},
        {"match": ""},
    ],
)
def test_rules_refusals_send_nothing(monkeypatch, params):
    opener = KubeOpener([])
    executor, *_ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(RULES, params))
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    assert opener.requests == []


def test_rules_400_echoes_and_5xx_is_unavailable(monkeypatch):
    class Opener(KubeOpener):
        def open(self, request, timeout=None):
            self.requests.append(request)
            raise self.responses.pop(0)

    def http(status, body):
        return urllib.error.HTTPError("u", status, "x", {}, io.BytesIO(body))

    bad = b'{"status":"error","errorType":"bad_data","error":"bad matcher"}'
    opener = Opener([http(400, bad), http(502, b"")])
    executor, *_ = _executor(monkeypatch, opener)
    first = executor.execute(_call(RULES, {"match": "{"}, index=0))
    assert (first.status, first.reason) == ("error", "INVALID_PARAMS")
    assert "bad matcher" in json.dumps(first.model_view)
    second = executor.execute(_call(RULES, {}, index=1))
    assert (second.status, second.reason) == ("error", "SOURCE_UNAVAILABLE")


# -- get_prometheus_target -----------------------------------------------------------


def test_target_asks_active_filters_job_and_drops_discovered_labels(monkeypatch):
    body = _envelope(_targets())
    opener = KubeOpener([body])
    executor, sink, *_ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(TARGET, {"target_name": "otel-collector"}))
    assert outcome.status == "ok", outcome.model_view
    assert _path(opener).endswith("/api/v1/targets")
    assert _query(opener) == {"state": ["active"]}
    view = _view(outcome)
    assert [t["labels"]["instance"] for t in view["content"]] == ["c:8888"]
    assert all("discoveredLabels" not in t for t in view["content"])
    assert view["jobs_seen"] == ["otel-collector", "other"]
    assert view["active_targets_total"] == 2
    assert view["source_start_at"] is None
    record = json.loads(sink.records[-1].raw)
    assert record["source_response_bytes"] == len(body)
    assert "discoveredLabels" not in sink.records[-1].raw.decode()


def test_target_with_no_matching_job_is_no_data_with_jobs_seen(monkeypatch):
    opener = KubeOpener([_envelope(_targets())])
    executor, *_ = _executor(monkeypatch, opener)
    view = _view(executor.execute(_call(TARGET, {"target_name": "checkout"})))
    assert view["status"] == "no_data" and view["content"] == []
    assert "otel-collector" in view["jobs_seen"]


@pytest.mark.parametrize("name", ["", "  ", "a\tb", "x" * 201])
def test_target_name_refusals_send_nothing(monkeypatch, name):
    opener = KubeOpener([])
    executor, *_ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(TARGET, {"target_name": name}))
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    assert opener.requests == []


@pytest.mark.parametrize(
    "body",
    [
        b'{"status":"success","data":{"activeTargets":{}}}',
        b'{"status":"error","data":{"activeTargets":[]}}',
        b'{"status":"success","status":"success","data":{"activeTargets":[]}}',
        b"not json",
    ],
)
def test_target_malformed_envelope_is_malformed(monkeypatch, body):
    opener = KubeOpener([body])
    executor, *_ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(TARGET, {"target_name": "a"}))
    assert (outcome.status, outcome.reason) == ("error", "MALFORMED_RESULT")


# -- get_metric_metadata -----------------------------------------------------------


def test_metadata_fixed_limit_metric_filter_and_rows(monkeypatch):
    data = {
        "http_requests_total": [
            {"type": "counter", "help": "Requests.", "unit": ""},
            {"type": "counter", "help": "Requests (other target).", "unit": ""},
        ]
    }
    opener = KubeOpener([_envelope(data)])
    executor, *_ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(METADATA, {"metric": "http_requests_total"}))
    assert outcome.status == "ok", outcome.model_view
    assert _path(opener).endswith("/api/v1/metadata")
    assert _query(opener) == {"limit": ["100"], "metric": ["http_requests_total"]}
    view = _view(outcome)
    assert view["content"] == [
        {
            "metric": "http_requests_total",
            "type": "counter",
            "help": "Requests.",
            "unit": "",
        },
        {
            "metric": "http_requests_total",
            "type": "counter",
            "help": "Requests (other target).",
            "unit": "",
        },
    ]
    assert view["incomplete"] is False and view["limit"] == 100


def test_metadata_100_metrics_or_warnings_are_incomplete(monkeypatch):
    full = {f"m_{i}": [{"type": "gauge", "help": "", "unit": ""}] for i in range(100)}
    few = {"m": [{"type": "gauge", "help": "", "unit": ""}]}
    opener = KubeOpener([_envelope(full), _envelope(few, warnings=["w"])])
    executor, *_ = _executor(monkeypatch, opener)
    first = _view(executor.execute(_call(METADATA, {}, index=0)))
    assert "metric" not in _query(opener)
    assert first["incomplete"] is True
    assert first["incomplete_reason"] == prometheus_ext.METADATA_INCOMPLETE_REASON
    second = _view(executor.execute(_call(METADATA, {}, index=1)))
    assert second["incomplete"] is True


@pytest.mark.parametrize("metric", ["", "1abc", "a-b", 'up{job="x"}', "a" * 201])
def test_metadata_metric_refusals_send_nothing(monkeypatch, metric):
    opener = KubeOpener([])
    executor, *_ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(METADATA, {"metric": metric}))
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    assert opener.requests == []


# -- lab proxy and authorization probe ---------------------------------------------

B2_PATHS = ("/api/v1/rules", "/api/v1/targets", "/api/v1/metadata")
STILL_DENIED = ("/api/v1/alerts", "/api/v1/targets/metadata", "/api/v1/status/config")


def test_proxy_opens_exactly_the_three_get_paths():
    accounts = {
        name: "aaaaaaaa357" for name in ("collector", "proxy", *lab.ROLE_ENV_VARS)
    }
    config = lab.proxy_nginx_config(accounts)
    for path in B2_PATHS:
        assert path in lab.READ_PATHS
        assert f"location = {path} {{ try_files /__absent__ @read; }}" in config
    for path in STILL_DENIED:
        assert f"location = {path} " not in config
    # The form-POST exception stays query/query_range only.
    assert "holmes:POST:/api/v1/query(_range)?" in config
    assert "rules" not in config.split("map $content_length", 1)[0].split("map ", 1)[1]


def test_probe_expects_200_for_the_new_gets_and_403_for_the_rest():
    for account in probe.PROBE_ACCOUNTS:
        assert account != "holmes"
    for path in B2_PATHS:
        assert ("GET", path) in probe.READ_REQUESTS
        assert ("POST", path, "form", 403) in probe.HOLMES_ROWS
        assert ("GET", path, None, 200) in probe.HOLMES_ROWS
    for path in STILL_DENIED:
        assert ("GET", path) in probe.DENIED_REQUESTS
        assert ("GET", path, None, 403) in probe.HOLMES_ROWS
    assert probe._query("/api/v1/targets") == "?state=active"


def test_b2_tools_declare_may_contain_secrets_and_a5_tools_do_not():
    registrations = {r.name: r for r in prometheus_ext.prometheus_ext_registrations()}
    for name in B2:
        assert registrations[name].may_contain_secrets is True
    for name in (
        prometheus_ext.INSTANT_QUERY_TOOL,
        prometheus_ext.SERIES_TOOL,
        prometheus_ext.LABELS_TOOL,
    ):
        assert registrations[name].may_contain_secrets is False


def _direct_fetch(tool: str, params: dict, body: bytes):
    """The transport's own response, before the executor ORs in the marker."""
    from opspilot.tools.executor import TransportRequest
    from tests.test_tool_expansion_a2_contract import AUTHORIZED_WINDOW

    class _Base:
        _credentials = {"ref": None}

        def _get(self, url, request, *, max_bytes):
            return otel_demo._Fetched(200, body)

    transport = prometheus_ext.PrometheusExtTransport(_Base())  # type: ignore[arg-type]
    return transport.fetch(
        TransportRequest(
            operation_id="op",
            source=otel_demo.SOURCE,
            verb="list",
            endpoint="http://127.0.0.1:19090",
            selector={},
            params=params,
            window=AUTHORIZED_WINDOW,
            timeout_seconds=30.0,
            max_result_bytes=1024 * 1024,
            credential_ref="ref",
            tool=tool,
        )
    )


@pytest.mark.parametrize(
    ("tool", "body"),
    [
        (RULES, _envelope(_groups(), warnings=["partial"])),
        (
            METADATA,
            _envelope(
                {"m": [{"type": "gauge", "help": "", "unit": ""}]}, warnings=["w"]
            ),
        ),
    ],
)
def test_warnings_below_the_limit_set_the_transport_incomplete_flag(tool, body):
    response = _direct_fetch(tool, {}, body)
    assert response.incomplete is True
    assert response.view_fields is not None
    assert response.view_fields["incomplete_reason"]


def test_a_lone_surrogate_in_a_projected_record_is_an_error_not_a_crash(monkeypatch):
    # JSON text with an escaped lone surrogate: json.loads accepts it, UTF-8
    # encoding of the projected record does not.
    body = b'{"status":"success","data":{"m":[{"type":"gauge","help":"\\ud800","unit":""}]}}'
    assert "\\ud800" in body.decode()
    opener = KubeOpener([body])
    executor, *_ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(METADATA, {}))
    # executor.py converges an exception from fetch() into SOURCE_ERROR.
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_ERROR")
