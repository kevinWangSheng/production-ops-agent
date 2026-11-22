"""Implementer unit tests for the live usage tools (B3, #562).

The independent contract tests are the acceptance suite; these pin the
implementer's own reading of the B3 interface
(docs/tasks/2026-11-19-tool-expansion-b3.md) through the executor against a
local ``http.server`` fake of the API server. No cluster, no model.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

import pytest

from opspilot.tools import kubernetes_live_metrics as lm
from opspilot.tools import otel_demo_ext as ext
from opspilot.tools.kubernetes_core import KubernetesClient
from opspilot.tools.profiles import select_profile
from tests.m1_tool_support import (
    WINDOW_END,
    WINDOW_START,
    FakeClock,
    build,
    request,
    target,
)

TOKEN = "k8s-ro-aaaaaaaa562"


class FakeApiServer:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []
        self.answers: dict[str, tuple[int, bytes]] = {}

    def start(self) -> None:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:
                return None

            def _serve(self) -> None:
                fake.requests.append((self.command, self.path))
                path = urlsplit(self.path).path
                status, body = fake.answers.get(path, (404, b"not found"))
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _serve

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def api() -> Iterator[FakeApiServer]:
    server = FakeApiServer()
    server.start()
    yield server
    server.stop()


def _json(payload: object) -> bytes:
    return json.dumps(payload).encode()


def _pod(namespace, name, containers, stamp="2026-09-14T01:00:30Z"):
    return {
        "metadata": {"name": name, "namespace": namespace, "labels": {"a": "b"}},
        "timestamp": stamp,
        "window": "15.022s",
        "containers": [
            {"name": n, "usage": {"cpu": cpu, "memory": memory}}
            for n, cpu, memory in containers
        ],
    }


def _execute(api: FakeApiServer, tool: str, params: dict[str, object]):
    clock = FakeClock(start=WINDOW_END + timedelta(seconds=40))
    executor, _, sink, _ = build(
        registrations=list(lm.live_metrics_registrations(max_window_seconds=86400)),
        targets=[
            target(
                source="otel-demo",
                selector={"kubernetes_endpoint": api.url},
            )
        ],
        transport=lm.LiveMetricsTransport(
            client=KubernetesClient(credentials={"k8s-ro": TOKEN})
        ),
        clock=clock,
        scope_overrides={"tool_names": frozenset(lm.TOOL_NAMES)},
    )
    outcome = executor.execute(
        request(
            tool_name=tool,
            params=params,
            window={"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()},
        )
    )
    return outcome


def _view(outcome) -> dict[str, Any]:
    return {
        **dict(outcome.model_view),
        "status": outcome.status,
        "reason": outcome.reason,
    }


# -- quantities ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "millicores"),
    [
        ("123456789n", 124),
        ("1000000n", 1),
        ("1n", 1),
        ("0", 0),
        ("250m", 250),
        ("1", 1000),
        ("1.5", 1500),
        ("2500u", 3),
        ("1e-3", 1),
        ("1e3", 1_000_000),
    ],
)
def test_cpu_rounds_up_to_millicores(raw, millicores):
    assert lm.parse_cpu_millicores(raw) == millicores


@pytest.mark.parametrize(
    ("raw", "value"),
    [
        ("45678Ki", 45678 * 1024),
        ("64Mi", 64 * 2**20),
        ("1Gi", 2**30),
        ("1k", 1000),
        ("1M", 10**6),
        ("1G", 10**9),
        ("1E", 10**18),
        ("1E3", 1000),
        ("123", 123),
        ("100m", 1),
        ("1.5Ki", 1536),
    ],
)
def test_memory_bytes(raw, value):
    assert lm.parse_memory_bytes(raw) == value


@pytest.mark.parametrize("raw", ["", "abc", "1Kb", "1 Mi", "Mi", "1e", None, 5, "-1"])
def test_bad_quantities(raw):
    assert lm.parse_memory_bytes(raw) is None


def test_exact_quantity():
    assert lm.parse_quantity("1500u") == Decimal("0.0015")


# -- switch and profile -----------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "on"), [(None, False), ("", False), ("0", False), ("1", True)]
)
def test_switch_values(value, on):
    env = {} if value is None else {lm.ENV: value}
    assert lm.live_metrics_enabled(env) is on


@pytest.mark.parametrize("value", ["true", "yes", " 1", "2"])
def test_switch_rejects_other_values(value):
    with pytest.raises(SystemExit, match="K8S_LIVE_METRICS_INVALID"):
        lm.live_metrics_enabled({lm.ENV: value})


def test_existing_revisions_unchanged_and_live_set_distinct():
    revision = ext.tool_schema_revision
    unchanged = {
        (False, False, False): "otel-demo-ext-9b1547d77ecd",
        (True, False, False): "otel-demo-ext-1fce458c3579",
        (False, True, False): "otel-demo-ext-ab1feb76ec12",
        (True, True, False): "otel-demo-ext-ce9b52089d38",
        (False, False, True): "otel-demo-ext-1021ae4ff05b",
        (True, False, True): "otel-demo-ext-0d4e29be0c4c",
        (False, True, True): "otel-demo-ext-3bcc4c4444e7",
        (True, True, True): "otel-demo-ext-8233fd90ff6e",
    }
    live = set()
    for (es, loki, vl), value in unchanged.items():
        assert revision(elasticsearch=es, loki=loki, victorialogs=vl) == value
        live.add(
            revision(elasticsearch=es, loki=loki, victorialogs=vl, live_metrics=True)
        )
    assert len(live) == 8 and not live & set(unchanged.values())
    assert ext.LIVE_METRICS_TOOL_SCHEMA_REVISION == revision(live_metrics=True)
    assert ext.LIVE_METRICS_TOOL_NAMES[-2:] == lm.TOOL_NAMES


def test_face_appends_tools_last_and_lists_them_under_kubernetes():
    clock = FakeClock(start=WINDOW_END)
    face = ext.otel_demo_ext_face(clock, victorialogs=True, live_metrics=True)
    names = [s["function"]["name"] for s in face.tool_schemas]
    assert names[-2:] == ["kubectl_top_pods", "kubectl_top_nodes"]
    context = face.evidence_context("run-1")
    kubernetes = context["data_sources"]["available"]["kubernetes"]
    assert {"kubectl_top_pods", "kubectl_top_nodes"} <= set(kubernetes)
    assert kubernetes == sorted(kubernetes)
    off = ext.otel_demo_ext_face(clock).evidence_context("run-1")
    assert "kubectl_top_pods" not in off["data_sources"]["available"]["kubernetes"]


def test_web_profile_reads_switch(monkeypatch):
    env = {"OPSPILOT_TOOL_PROFILE": "otel-demo-ext", lm.ENV: "1"}
    versions = select_profile(env).versions()
    assert versions["tool_schema_revision"] == ext.LIVE_METRICS_TOOL_SCHEMA_REVISION
    with pytest.raises(SystemExit, match="K8S_LIVE_METRICS_INVALID"):
        select_profile({"OPSPILOT_TOOL_PROFILE": "otel-demo-ext", lm.ENV: "on"})
    # Other profiles never read it.
    select_profile({"OPSPILOT_TOOL_PROFILE": "otel-demo", lm.ENV: "on"})


# -- tool behaviour through the executor -------------------------------------------


def test_pods_namespace_rows_units_and_time(api):
    api.answers["/apis/metrics.k8s.io/v1beta1/namespaces/ns-a/pods"] = (
        200,
        _json(
            {
                "kind": "PodMetricsList",
                "items": [
                    _pod(
                        "ns-a",
                        "two",
                        [
                            ("side", "100500n", "1Mi"),
                            ("app", "200500n", "2Mi"),
                        ],
                        stamp="2026-09-14T01:00:45Z",
                    ),
                    _pod("ns-a", "one", [("main", "250m", "64Mi")]),
                ],
            }
        ),
    )
    outcome = _execute(api, "kubectl_top_pods", {"namespace": "ns-a"})
    view = _view(outcome)
    assert view["status"] == "ok", view
    rows = view["content"]
    assert [r["pod"] for r in rows] == ["one", "two"]
    two = rows[1]
    assert [c["name"] for c in two["containers"]] == ["app", "side"]
    assert two["cpu_millicores"] == 1  # ceil(0.301 m)
    assert sum(c["cpu_millicores"] for c in two["containers"]) == 2
    assert two["memory_bytes"] == 3 * 2**20
    assert rows[0]["cpu_millicores"] == 250 and rows[0]["memory_bytes"] == 64 * 2**20
    assert "labels" not in json.dumps(rows)
    assert view["sampled_at"] == {
        "earliest": "2026-09-14T01:00:30Z",
        "latest": "2026-09-14T01:00:45Z",
    }
    assert view["notes"][0].startswith(
        "Sampled at 2026-09-14T01:00:30Z–2026-09-14T01:00:45Z: current usage, "
        "30 s after the end"
    )
    assert view["scope"] == {"namespace": "ns-a", "all_namespaces": False}
    assert view.get("source_start_at") is None and view.get("data_as_of") is None
    assert api.requests == [
        ("GET", "/apis/metrics.k8s.io/v1beta1/namespaces/ns-a/pods")
    ]


def test_sample_before_window_end_says_before(api):
    api.answers["/apis/metrics.k8s.io/v1beta1/pods"] = (
        200,
        _json({"items": [_pod("a", "p", [("c", "1m", "1")], "2026-09-14T00:59:50Z")]}),
    )
    view = _view(_execute(api, "kubectl_top_pods", {}))
    assert "10 s before the end of the authorized window" in view["notes"][0]
    assert view["scope"]["all_namespaces"] is True


def test_invalid_namespace_sends_nothing(api):
    view = _view(_execute(api, "kubectl_top_pods", {"namespace": "Bad_NS"}))
    assert view["status"] != "ok"
    assert "INVALID_PARAMS" in json.dumps(view)
    assert api.requests == []


def test_nodes_percent_and_unsampled(api):
    api.answers["/apis/metrics.k8s.io/v1beta1/nodes"] = (
        200,
        _json(
            {
                "kind": "NodeMetricsList",
                "items": [
                    {
                        "metadata": {"name": "n1"},
                        "timestamp": "2026-09-14T01:00:30Z",
                        "window": "20.04s",
                        "usage": {"cpu": "1999999999n", "memory": "1Gi"},
                    },
                    {
                        "metadata": {"name": "n0"},
                        "timestamp": "2026-09-14T01:00:30Z",
                        "window": "20.04s",
                        "usage": {"cpu": "10m", "memory": "10Mi"},
                    },
                ],
            }
        ),
    )
    api.answers["/api/v1/nodes"] = (
        200,
        _json(
            {
                "items": [
                    {
                        "metadata": {"name": "n1"},
                        "status": {"allocatable": {"cpu": "3", "memory": "4Gi"}},
                    },
                    {"metadata": {"name": "n0"}, "status": {}},
                    {
                        "metadata": {"name": "n2"},
                        "status": {"allocatable": {"cpu": "1", "memory": "1Gi"}},
                    },
                ]
            }
        ),
    )
    view = _view(_execute(api, "kubectl_top_nodes", {}))
    assert view["status"] == "ok", view
    rows = view["content"]
    assert [r["node"] for r in rows] == ["n0", "n1"]
    n1 = rows[1]
    assert n1["cpu_millicores"] == 2000 and n1["cpu_percent"] == 66
    assert n1["memory_percent"] == 25
    assert rows[0]["cpu_percent"] is None and rows[0]["memory_percent"] is None
    notes = " ".join(view["notes"])
    assert "1 nodes have no usage sample" in notes
    assert "percentage is null: n0" in notes
    assert [r[0] for r in api.requests] == ["GET", "GET"]
    assert api.requests[1][1] == "/api/v1/nodes?limit=500"


@pytest.mark.parametrize(
    ("status", "klass"),
    [
        (404, "SOURCE_ERROR"),
        (403, "SOURCE_FORBIDDEN"),
        (401, "SOURCE_FORBIDDEN"),
        (429, "SOURCE_UNAVAILABLE"),
        (503, "SOURCE_UNAVAILABLE"),
        (400, "INVALID_PARAMS"),
    ],
)
def test_error_classes(api, status, klass):
    api.answers["/apis/metrics.k8s.io/v1beta1/pods"] = (status, b"x")
    view = _view(_execute(api, "kubectl_top_pods", {}))
    assert klass in json.dumps(view), view
    if status == 404:
        assert "metrics-server is not installed" in json.dumps(view)


def test_empty_is_no_data(api):
    api.answers["/apis/metrics.k8s.io/v1beta1/nodes"] = (200, _json({"items": []}))
    view = _view(_execute(api, "kubectl_top_nodes", {}))
    assert view["status"] == "no_data"
    assert len(api.requests) == 1


@pytest.mark.parametrize(
    "item",
    [
        _pod("a", "p", [("c", "lots", "1")]),
        {**_pod("a", "p", [("c", "1m", "1")]), "timestamp": "yesterday"},
        {**_pod("a", "p", [("c", "1m", "1")]), "timestamp": "2026-09-14T01:00:30"},
        {k: v for k, v in _pod("a", "p", [("c", "1m", "1")]).items() if k != "window"},
    ],
)
def test_malformed(api, item):
    api.answers["/apis/metrics.k8s.io/v1beta1/pods"] = (200, _json({"items": [item]}))
    view = _view(_execute(api, "kubectl_top_pods", {}))
    assert "MALFORMED_RESULT" in json.dumps(view), view


def test_current_state_is_citable_as_fact(api):
    """B3-U1=a: no source time, still citable."""
    api.answers["/apis/metrics.k8s.io/v1beta1/pods"] = (
        200,
        _json({"items": [_pod("a", "p", [("c", "1m", "1")])]}),
    )
    view = _view(_execute(api, "kubectl_top_pods", {}))
    assert view["citable_as_fact"] is True
    assert view["source_start_at"] is None and view["source_end_at"] is None


def test_read_budget_is_shared_by_both_node_reads(api, monkeypatch):
    monkeypatch.setattr(lm, "MAX_SOURCE_READ_BYTES", 400)
    metrics = _json(
        {
            "items": [
                {
                    "metadata": {"name": "n1"},
                    "timestamp": "2026-09-14T01:00:30Z",
                    "window": "20s",
                    "usage": {"cpu": "1m", "memory": "1Ki"},
                }
            ]
        }
    )
    assert len(metrics) < 400
    api.answers["/apis/metrics.k8s.io/v1beta1/nodes"] = (200, metrics)
    api.answers["/api/v1/nodes"] = (200, b'{"items": [], "pad": "' + b"x" * 300 + b'"}')
    view = _view(_execute(api, "kubectl_top_nodes", {}))
    assert "RESULT_TOO_LARGE" in json.dumps(view), view
