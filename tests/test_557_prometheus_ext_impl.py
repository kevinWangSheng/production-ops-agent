"""Implementer unit tests for tool expansion A5 (#557): the Prometheus instant
query, series and label-name tools of the ``otel-demo-ext`` profile.

Driven through the profile's executor with the urllib opener double of the A2
contract tests (``urllib.request.build_opener`` patched), so the requests the
transport builds and the views the executor returns are both observed.
"""

from __future__ import annotations

import base64
import json
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from opspilot.tools import otel_demo, otel_demo_ext, prometheus_ext
from opspilot.tools.executor import TransportResponse
from opspilot.tools.profiles import PROFILE_ENV, select_profile
from tests.m1_tool_support import FakeClock
from tests.test_m1_otel_demo_contract import NOW
from tests.test_tool_expansion_a2_contract import (
    AUTHORIZED_WINDOW,
    BASE_TOOL_SCHEMA_REVISION,
    KubeOpener,
    _call,
    _executor,
    _headers,
    _synthetic_k8s_ca,  # noqa: F401  (autouse fixture: the A2 CA file)
    _url,
    _view,
)

INSTANT = prometheus_ext.INSTANT_QUERY_TOOL
SERIES = prometheus_ext.SERIES_TOOL
LABELS = prometheus_ext.LABELS_TOOL
PROM_USER = "aaaaaaaa357"
PROM_PASSWORD = "bbbbbbbb357"
FRAME = AUTHORIZED_WINDOW


def _ts(moment) -> float:
    return moment.timestamp()


def _vector(at, *, warnings=None) -> bytes:
    body: dict = {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": {"service_name": "checkout"}, "value": [_ts(at), "1.5"]}
            ],
        },
    }
    if warnings:
        body["warnings"] = warnings
    return json.dumps(body).encode()


def _list_body(data, *, warnings=None) -> bytes:
    body: dict = {"status": "success", "data": data}
    if warnings:
        body["warnings"] = warnings
    return json.dumps(body).encode()


def _query(opener, index=0) -> dict[str, list[str]]:
    return parse_qs(urlsplit(_url(opener.requests[index])).query)


def _path(opener, index=0) -> str:
    return urlsplit(_url(opener.requests[index])).path


def _basic_env() -> dict[str, str]:
    return {
        "OPSPILOT_OTEL_PROMETHEUS_USERNAME": PROM_USER,
        "OPSPILOT_OTEL_PROMETHEUS_PASSWORD": PROM_PASSWORD,
    }


# -- profile surface -----------------------------------------------------------


def test_new_tools_join_only_the_extension_and_otel_demo_bytes_stay():
    assert not prometheus_ext.PROMETHEUS_EXT_TOOLS & otel_demo.PROMETHEUS_TOOLS
    assert not set(prometheus_ext.TOOL_NAMES) & set(otel_demo.TOOL_NAMES)
    assert otel_demo.TOOL_SCHEMA_REVISION == BASE_TOOL_SCHEMA_REVISION
    ext = select_profile({PROFILE_ENV: "otel-demo-ext"})
    names = [s["function"]["name"] for s in ext.face(FakeClock(start=NOW)).tool_schemas]
    assert set(prometheus_ext.TOOL_NAMES) <= set(names)
    assert set(prometheus_ext.TOOL_NAMES) <= set(otel_demo_ext.TOOL_NAMES)
    context = ext.face(FakeClock(start=NOW)).evidence_context("run-1")
    prom = context["data_sources"]["available"]["prometheus"]
    assert set(prometheus_ext.TOOL_NAMES) <= set(prom)
    assert set(otel_demo.PROMETHEUS_TOOLS) <= set(prom)


def test_schemas_have_the_upstream_parameter_names_and_no_timeout():
    schemas = {
        s["function"]["name"]: s["function"] for s in prometheus_ext.TOOL_SCHEMAS
    }
    assert set(schemas[INSTANT]["parameters"]["properties"]) == {"query", "time"}
    assert schemas[INSTANT]["parameters"]["required"] == ["query"]
    assert set(schemas[SERIES]["parameters"]["properties"]) == {"match", "start", "end"}
    assert schemas[SERIES]["parameters"]["required"] == ["match"]
    assert set(schemas[LABELS]["parameters"]["properties"]) == {"match", "start", "end"}
    assert schemas[LABELS]["parameters"]["required"] == []
    registrations = {r.name: r for r in prometheus_ext.prometheus_ext_registrations()}
    for name in prometheus_ext.TOOL_NAMES:
        assert registrations[name].request_timeout_seconds == 30.0
        assert registrations[name].source == otel_demo.SOURCE
        assert set(registrations[name].parameters) == set(
            schemas[name]["parameters"]["properties"]
        )


# -- instant query -----------------------------------------------------------------


def test_instant_time_omitted_is_the_frame_end_with_basic_auth(monkeypatch):
    opener = KubeOpener([_vector(FRAME.end)])
    executor, *_ = _executor(monkeypatch, opener, env=_basic_env())
    outcome = executor.execute(_call(INSTANT, {"query": "up"}))
    assert (outcome.status, outcome.reason) == ("ok", None), outcome.model_view
    assert _path(opener).endswith("/api/v1/query")
    query = _query(opener)
    assert float(query["time"][0]) == pytest.approx(_ts(FRAME.end))
    assert "limit" not in query
    assert query["timeout"] == ["30s"]
    pair = base64.b64encode(f"{PROM_USER}:{PROM_PASSWORD}".encode()).decode()
    assert _headers(opener.requests[0])["authorization"] == f"Basic {pair}"
    view = _view(outcome)
    assert view["evaluation_time"] == FRAME.end.isoformat()
    assert view["lookback_seconds"] is None
    assert view["incomplete"] is False


def test_instant_given_time_and_lookback_inside_the_frame(monkeypatch):
    at = FRAME.end - timedelta(seconds=60)
    opener = KubeOpener([_vector(at)])
    executor, *_ = _executor(monkeypatch, opener)
    outcome = executor.execute(
        _call(INSTANT, {"query": "rate(up[1m])", "time": at.isoformat()})
    )
    assert outcome.status == "ok", outcome.model_view
    assert float(_query(opener)["time"][0]) == pytest.approx(_ts(at))
    view = _view(outcome)
    assert view["lookback_seconds"] == 60
    assert view["lookback_start_at"] == (at - timedelta(seconds=60)).isoformat()


@pytest.mark.parametrize(
    "params",
    [
        {"query": "up", "time": (FRAME.end + timedelta(seconds=1)).isoformat()},
        {"query": "up", "time": (FRAME.start - timedelta(seconds=1)).isoformat()},
        {"query": "up", "time": "2026-09-14T00:00:00"},  # naive
        {"query": "up", "time": "1757808000"},  # relative/unix text
        {"query": "up offset 5m"},
        {"query": "up @ 1757808000"},
        {"query": "rate(up[1m])", "time": FRAME.start.isoformat()},
        {"query": ""},
    ],
)
def test_instant_refusals_send_nothing(monkeypatch, params):
    opener = KubeOpener([])
    executor, *_ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(INSTANT, params))
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    assert opener.requests == []


def test_instant_503_timeout_is_a_confirmed_timeout_and_400_echoes(monkeypatch):
    import io
    import urllib.error

    def http(status, body):
        return urllib.error.HTTPError("u", status, "x", {}, io.BytesIO(body))

    class Opener(KubeOpener):
        def open(self, request, timeout=None):
            self.requests.append(request)
            raise self.responses.pop(0)

    timeout_body = b'{"status":"error","errorType":"timeout","error":"slow"}'
    bad_body = b'{"status":"error","errorType":"bad_data","error":"parse error"}'
    opener = Opener([http(503, timeout_body), http(400, bad_body)])
    executor, *_ = _executor(monkeypatch, opener)
    first = executor.execute(_call(INSTANT, {"query": "up"}, index=0))
    assert first.status == "timeout"
    second = executor.execute(_call(INSTANT, {"query": "sum("}, index=1))
    assert (second.status, second.reason) == ("error", "INVALID_PARAMS")
    assert "parse error" in json.dumps(second.model_view)


def test_instant_overflow_summary_is_the_range_query_summary():
    rows = [{"metric": {"pod": f"p{i}"}, "value": [1.0, "1"]} for i in range(7)]
    summary = prometheus_ext.overflow_summary(INSTANT, rows, {"query": "up"})
    assert summary["row_unit"] == "series"
    assert summary["series_count"] == 7
    assert "topk(5, up)" in summary["suggestion"]


# -- series and labels ---------------------------------------------------------


def test_series_default_window_fixed_limit_and_complete(monkeypatch):
    opener = KubeOpener([_list_body([{"__name__": "up", "job": "a"}])])
    executor, *_ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(SERIES, {"match": '{job="a"}'}))
    assert outcome.status == "ok", outcome.model_view
    assert _path(opener).endswith("/api/v1/series")
    query = _query(opener)
    assert query["match[]"] == ['{job="a"}']
    assert query["limit"] == ["100"]
    end = float(query["end"][0])
    start = float(query["start"][0])
    assert end == pytest.approx(_ts(FRAME.end))
    assert end - start == pytest.approx(min(3600, FRAME.seconds))
    view = _view(outcome)
    assert view["incomplete"] is False and view["incomplete_reason"] is None
    assert view["limit"] == 100


def test_series_exactly_100_is_incomplete_and_raw_bytes_unchanged(monkeypatch):
    body = _list_body([{"__name__": "up", "i": str(i)} for i in range(100)])
    opener = KubeOpener([body])
    executor, sink, *_ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(SERIES, {"match": "up"}))
    view = _view(outcome)
    assert view["incomplete"] is True
    assert view["incomplete_reason"] == prometheus_ext.LIST_INCOMPLETE_REASON
    assert view["truncated"] is False
    assert sink.records[-1].raw == body
    assert sink.records[-1].incomplete is True


def test_labels_warnings_are_incomplete_and_match_is_optional(monkeypatch):
    opener = KubeOpener(
        [_list_body(["__name__", "job"], warnings=["results truncated due to limit"])]
    )
    executor, *_ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(LABELS, {}))
    assert outcome.status == "ok", outcome.model_view
    assert _path(opener).endswith("/api/v1/labels")
    assert "match[]" not in _query(opener)
    assert _view(outcome)["incomplete"] is True


@pytest.mark.parametrize(
    ("tool", "params"),
    [
        (SERIES, {}),
        (SERIES, {"match": "  "}),
        (LABELS, {"match": ""}),
        (SERIES, {"match": "up", "start": FRAME.start.isoformat()}),
        (
            LABELS,
            {
                "start": (FRAME.start - timedelta(hours=1)).isoformat(),
                "end": FRAME.end.isoformat(),
            },
        ),
    ],
)
def test_list_refusals_send_nothing(monkeypatch, tool, params):
    opener = KubeOpener([])
    executor, *_ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(tool, params))
    assert outcome.status in ("error", "denied"), outcome.model_view
    assert opener.requests == []


def test_an_adapter_incomplete_flag_that_is_not_bool_is_malformed(monkeypatch):
    assert TransportResponse(body=b"{}").incomplete is False

    def bad(self, request):
        return TransportResponse(body=_list_body(["job"]), incomplete="yes")

    monkeypatch.setattr(prometheus_ext.PrometheusExtTransport, "_list", bad)
    opener = KubeOpener([])
    executor, *_ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(LABELS, {}))
    assert (outcome.status, outcome.reason) == ("error", "MALFORMED_RESULT")
