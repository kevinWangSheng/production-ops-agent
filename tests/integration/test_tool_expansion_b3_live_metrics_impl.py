"""Implementer real-cluster tests for the live usage tools (B3, #562, R8-25).

``kubectl_top_pods`` and ``kubectl_top_nodes`` through the public profile
path (``OPSPILOT_TOOL_PROFILE=otel-demo-ext`` with
``OPSPILOT_K8S_LIVE_METRICS=1``) against a real kind API server and
metrics-server v0.8.1, with the A1 read-only token only. No model.

Runs only in the CI job ``k8s-live-metrics`` (``OPSPILOT_K8S_LIVE=1``), which
creates the cluster, applies ``tests/fixtures/b3_live_metrics/workloads.yaml``
and exports ``B3_K8S_*``; ``kubectl`` there holds the engineer (admin)
kubeconfig the fixtures use. The last test scales metrics-server to zero and
deletes its APIService for the real 503 and 404, then restores both: it must
stay last in this file, and this file runs after the independent tests.
"""

from __future__ import annotations

import json
import os
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from opspilot.tools import ToolRequest
from opspilot.tools.kubernetes_core import KubernetesClient
from opspilot.tools.outcomes import Window
from opspilot.tools.profiles import PROFILE_ENV, select_profile
from tests.m1_tool_support import FakeClock, RecordingSink
from tests.test_m1_otel_demo_contract import TARGET_ID, FakeStore, _lease

pytestmark = pytest.mark.skipif(
    os.environ.get("OPSPILOT_K8S_LIVE") != "1",
    reason="the real cluster runs only in the CI k8s-live-metrics job",
)

API = os.environ.get("B3_K8S_API_URL", "")
CA = os.environ.get("B3_K8S_CA_FILE", "")
MANIFEST = os.environ.get(
    "B3_METRICS_SERVER_MANIFEST", "scripts/kind_lab/metrics-server-v0.8.1.yaml"
)
ALLOWED_PATHS = {
    "/apis/metrics.k8s.io/v1beta1/pods",
    "/apis/metrics.k8s.io/v1beta1/namespaces/ns-a/pods",
    "/apis/metrics.k8s.io/v1beta1/nodes",
    "/api/v1/nodes",
}


def _token(name: str) -> str:
    path = os.environ.get(name, "")
    return Path(path).read_text().strip() if path else ""


def _env(token: str) -> dict[str, str]:
    return {
        PROFILE_ENV: "otel-demo-ext",
        "OPSPILOT_K8S_API_URL": API,
        "OPSPILOT_K8S_TOKEN": token,
        "OPSPILOT_K8S_CA_FILE": CA,
        "OPSPILOT_K8S_NAMESPACE": "ns-a",
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://127.0.0.1:19090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://127.0.0.1:16686/jaeger/ui",
        "OPSPILOT_K8S_LIVE_METRICS": "1",
    }


@pytest.fixture
def sent(monkeypatch) -> Iterator[list[tuple[str, str]]]:
    """Every request the tool layer sends (method is always GET by
    construction; the path is checked against the fixed set)."""
    calls: list[tuple[str, str]] = []
    original = KubernetesClient.get

    def recording(self, url, **kwargs):
        calls.append(("GET", urlsplit(url).path))
        return original(self, url, **kwargs)

    monkeypatch.setattr(KubernetesClient, "get", recording)
    yield calls
    assert {path for _, path in calls} <= ALLOWED_PATHS, calls


def _call(tool: str, params: Mapping[str, object], *, token: str | None = None):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    clock = FakeClock(start=now)
    profile = select_profile(_env(token or _token("B3_K8S_TOKEN_FILE")))
    lease = _lease()
    deadline = now + timedelta(minutes=10)
    factory = profile.executor_factory(
        FakeStore(lease, deadline=deadline), RecordingSink(), clock
    )
    executor = factory(
        lease,
        profile.face(clock).input_for(
            run_id=str(lease.run_id),
            question="Which Pods use the most CPU right now?",
            target_id=TARGET_ID,
            deadline=deadline,
            model_requests=12,
        ),
    )
    outcome = executor.execute(
        ToolRequest(
            step_id="b3-live",
            tool_index=0,
            tool_name=tool,
            target_ref=TARGET_ID,
            params=dict(params),
            window=Window(now - timedelta(hours=1), now).as_json(),
        )
    )
    view = {
        **dict(outcome.model_view),
        "status": outcome.status,
        "reason": outcome.reason,
    }
    return view, now


def _kubectl(*args: str) -> str:
    return subprocess.run(
        ["kubectl", *args], check=True, capture_output=True, text=True, timeout=120
    ).stdout


def _raw(path: str) -> dict[str, Any]:
    return json.loads(_kubectl("get", "--raw", path))


# Independent of the module's parser: metrics-server reports CPU in n (or
# plain 0) and memory in Ki (or plain bytes).
def _nano(value: str) -> int:
    if value.endswith("n"):
        return int(value[:-1])
    if value.endswith("u"):
        return int(value[:-1]) * 1000
    if value.endswith("m"):
        return int(value[:-1]) * 1_000_000
    return int(value) * 1_000_000_000


def _bytes(value: str) -> int:
    for suffix, factor in (("Ki", 1024), ("Mi", 1024**2), ("Gi", 1024**3)):
        if value.endswith(suffix):
            return int(value[: -len(suffix)]) * factor
    return int(value)


def _milli_up(nano: int) -> int:
    return -(-nano // 1_000_000)


def test_namespace_rows_magnitudes_and_containers(sent):
    view, now = _call("kubectl_top_pods", {"namespace": "ns-a"})
    assert view["status"] == "ok", view
    rows = {row["pod"]: row for row in view["content"]}
    assert set(rows) == {"cpu-burner", "mem-holder", "two-containers"}
    assert {row["namespace"] for row in rows.values()} == {"ns-a"}
    assert 120 <= rows["cpu-burner"]["cpu_millicores"] <= 260
    assert rows["mem-holder"]["memory_bytes"] >= 64 * 2**20
    two = rows["two-containers"]
    assert [c["name"] for c in two["containers"]] == ["app", "sidecar"]
    assert two["memory_bytes"] == sum(c["memory_bytes"] for c in two["containers"])
    total = sum(c["cpu_millicores"] for c in two["containers"])
    assert total - len(two["containers"]) < two["cpu_millicores"] <= total
    # Current state (B3-U1=a): no source time, sampled_at and the note.
    assert view["source_start_at"] is None and view["data_as_of"] is None
    stamps = sorted(row["timestamp"] for row in rows.values())
    assert view["sampled_at"] == {"earliest": stamps[0], "latest": stamps[-1]}
    assert view["notes"][0].startswith(f"Sampled at {stamps[0]}")
    assert "the end of the authorized window" in view["notes"][0]
    assert view["citable_as_fact"] is True
    assert sent == [("GET", "/apis/metrics.k8s.io/v1beta1/namespaces/ns-a/pods")]


def test_whole_cluster_includes_other_namespaces(sent):
    # metrics-server leaves out a Pod until it has two samples of it (an idle
    # Pod can miss a scrape), so wait a few resolution periods for ns-b (#739).
    for _ in range(10):
        view, _ = _call("kubectl_top_pods", {})
        assert view["status"] == "ok", view
        namespaces = {row["namespace"] for row in view["content"]}
        if {"kube-system", "ns-a", "ns-b"} <= namespaces:
            break
        time.sleep(3)
    assert {"kube-system", "ns-a", "ns-b"} <= namespaces
    assert view["scope"] == {"namespace": None, "all_namespaces": True}


def test_rows_match_raw_metrics_at_the_same_timestamp(sent):
    matched = 0
    for _ in range(5):
        view, _ = _call("kubectl_top_pods", {"namespace": "ns-a"})
        raw = _raw("/apis/metrics.k8s.io/v1beta1/namespaces/ns-a/pods")
        by_pod = {item["metadata"]["name"]: item for item in raw["items"]}
        for row in view["content"]:
            item = by_pod.get(row["pod"])
            if item is None:
                continue
            stamp = datetime.fromisoformat(item["timestamp"]).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            if stamp != row["timestamp"]:
                continue
            nano = sum(_nano(c["usage"]["cpu"]) for c in item["containers"])
            memory = sum(_bytes(c["usage"]["memory"]) for c in item["containers"])
            assert row["cpu_millicores"] == _milli_up(nano), (row, item)
            assert row["memory_bytes"] == memory, (row, item)
            matched += 1
        if matched:
            break
        time.sleep(2)
    assert matched, "no row shared a timestamp with the raw read"


def test_nodes(sent):
    view, _ = _call("kubectl_top_nodes", {})
    assert view["status"] == "ok", view
    (row,) = view["content"]
    assert row["node"] == "b3-control-plane"
    assert row["cpu_millicores"] > 0 and row["memory_bytes"] > 0
    assert row["cpu_percent"] is not None and row["cpu_percent"] >= 0
    assert row["memory_percent"] is not None and 0 < row["memory_percent"] <= 100
    raw = _raw("/apis/metrics.k8s.io/v1beta1/nodes")["items"][0]
    stamp = datetime.fromisoformat(raw["timestamp"]).strftime("%Y-%m-%dT%H:%M:%SZ")
    if stamp == row["timestamp"]:
        assert row["memory_bytes"] == _bytes(raw["usage"]["memory"])
    assert [path for _, path in sent] == [
        "/apis/metrics.k8s.io/v1beta1/nodes",
        "/api/v1/nodes",
    ]


def test_forbidden_tokens():
    view, _ = _call(
        "kubectl_top_pods", {}, token=_token("B3_K8S_NO_METRICS_TOKEN_FILE")
    )
    assert (view["status"], view["reason"]) == ("error", "SOURCE_FORBIDDEN"), view
    view, _ = _call("kubectl_top_nodes", {}, token="not-a-valid-token-562")
    assert (view["status"], view["reason"]) == ("error", "SOURCE_FORBIDDEN"), view


def _direct(method: str, path: str) -> int:
    context = ssl.create_default_context(cafile=CA)
    request = urllib.request.Request(
        API + path,
        method=method,
        data=b"{}" if method in ("POST", "PUT") else None,
        headers={
            "Authorization": f"Bearer {_token('B3_K8S_TOKEN_FILE')}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, context=context, timeout=30) as reply:
            return int(reply.status)
    except urllib.error.HTTPError as error:
        return int(error.code)


def test_identity_refuses_writes_and_secrets():
    pods = "/apis/metrics.k8s.io/v1beta1/namespaces/ns-a/pods"
    assert _direct("POST", pods) in (403, 405)
    assert _direct("DELETE", pods + "/cpu-burner") in (403, 405)
    assert _direct("GET", "/api/v1/secrets") == 403


def _until(tool: str, predicate, seconds: float = 150) -> dict[str, Any]:
    deadline = time.monotonic() + seconds
    while True:
        view, _ = _call(tool, {})
        if predicate(view) or time.monotonic() > deadline:
            return view
        time.sleep(3)


def test_zz_unavailable_then_missing_metrics_api():
    """Last in the file: breaks metrics-server, then restores it."""
    try:
        _kubectl(
            "-n", "kube-system", "scale", "deployment/metrics-server", "--replicas=0"
        )
        view = _until("kubectl_top_pods", lambda v: v["reason"] == "SOURCE_UNAVAILABLE")
        assert view["reason"] == "SOURCE_UNAVAILABLE", view
        _kubectl("delete", "apiservice", "v1beta1.metrics.k8s.io")
        view = _until("kubectl_top_nodes", lambda v: v["reason"] == "SOURCE_ERROR")
        assert view["reason"] == "SOURCE_ERROR", view
        assert "metrics-server is not installed" in json.dumps(view)
    finally:
        _kubectl("apply", "-f", MANIFEST)
        _kubectl(
            "-n", "kube-system", "scale", "deployment/metrics-server", "--replicas=1"
        )
        _kubectl(
            "-n",
            "kube-system",
            "wait",
            "--for=condition=Available",
            "deployment/metrics-server",
            "--timeout=180s",
        )
    view = _until("kubectl_top_pods", lambda v: v["status"] == "ok")
    assert view["status"] == "ok", view
