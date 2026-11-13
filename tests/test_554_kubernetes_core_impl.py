"""Implementer's unit tests for the A2 cluster object queries (#554).

Hermetic: a fake ``urllib`` opener stands in for the API server (installed
the way the OTel Demo tests do, at ``urllib.request.build_opener``); jq is
the real binary on PATH. The independent contract tests live elsewhere.
"""

from __future__ import annotations

import io
import json
import shutil
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest

from opspilot.investigation.inputs import ToolFace
from opspilot.investigation.loop import DISCIPLINE_VARIANT
from opspilot.persistence import Lease
from opspilot.tools import kubernetes_core as k8s
from opspilot.tools import otel_demo, otel_demo_ext
from opspilot.tools.evidence_read import RoutedTransport
from opspilot.tools.executor import (
    ReadOnlyToolExecutor,
    ToolRequest,
    TransportError,
    TransportRequest,
    TransportResponse,
    TransportResultTooLarge,
    TransportTimeout,
)
from opspilot.tools.outcomes import DENIED_REASONS, Window
from opspilot.tools.profiles import select_profile
from opspilot.tools.registry import ToolContractError
from opspilot.tracing import check_lab_target
from tests.m1_tool_support import FakeClock, RecordingSink

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not on PATH")

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
WINDOW = Window(NOW - timedelta(hours=24), NOW - timedelta(minutes=1))
API = "https://127.0.0.1:6443"
K8S_TOKEN = "aaaaaaaa357"
OTEL_TOKEN = "bbbbbbbb357"


class _Reply:
    def __init__(self, body: bytes, status: int = 200):
        self._buffer = io.BytesIO(body)
        self.status = status

    def read(self, n: int = -1) -> bytes:
        return self._buffer.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeApi:
    """Serves list pages by path; ``pages`` maps a path to a list of bodies
    (one per page, in order) or an int HTTP status."""

    def __init__(self, pages=None):
        self.pages = {
            key: list(value) if isinstance(value, list) else value
            for key, value in (pages or {}).items()
        }
        self.requests: list[urllib.request.Request] = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        parts = urlsplit(request.full_url)
        served = self.pages.get(parts.path)
        if served is None:
            raise AssertionError(f"unexpected request: {request.full_url}")
        if isinstance(served, int):
            raise urllib.error.HTTPError(
                request.full_url, served, "error", {}, io.BytesIO(b'{"kind":"Status"}')
            )
        return _Reply(served.pop(0))

    def headers(self, index: int = 0) -> dict[str, str]:
        request = self.requests[index]
        merged = {**request.headers, **request.unredirected_hdrs}
        return {key.lower(): value for key, value in merged.items()}

    def query(self, index: int = 0) -> dict[str, list[str]]:
        return parse_qs(urlsplit(self.requests[index].full_url).query)


def _list(kind: str, items: list[dict], *, token: str | None = None, api="v1") -> bytes:
    return json.dumps(
        {
            "kind": f"{kind}List",
            "apiVersion": api,
            "metadata": {"continue": token} if token else {},
            "items": items,
        }
    ).encode()


def _pod(name: str, phase: str = "Running", namespace: str = "otel-demo") -> dict:
    return {
        "metadata": {
            "name": name,
            "namespace": namespace,
            "managedFields": [{"manager": "kubelet"}],
        },
        "status": {"phase": phase},
    }


def _transport(api: FakeApi, *, credentials=None) -> k8s.KubernetesTransport:
    return k8s.KubernetesTransport(
        credentials={k8s.CREDENTIAL_REF: K8S_TOKEN}
        if credentials is None
        else credentials,
        jq_path=shutil.which("jq") or "jq",
        opener=api,  # type: ignore[arg-type]
    )


def _request(tool: str, params: dict, *, timeout: float = 10.0) -> TransportRequest:
    return TransportRequest(
        operation_id="step-1-t0",
        source=otel_demo.SOURCE,
        verb="list",
        endpoint="http://127.0.0.1:19090",
        selector={k8s.ENDPOINT_SELECTOR: API},
        params=params,
        window=WINDOW,
        timeout_seconds=timeout,
        max_result_bytes=k8s.MAX_JQ_OUTPUT_BYTES,
        credential_ref=otel_demo.CREDENTIAL_REF,
        tool=tool,
    )


# -- refusals before anything is sent ------------------------------------------


@pytest.mark.parametrize(
    "kind", ["secrets", "Secret", "SECRETS", "secret", "secrets.v1"]
)
def test_secrets_are_refused_before_any_request(kind):
    api = FakeApi()
    response = _transport(api).fetch(
        _request(k8s.JQ_QUERY_TOOL, {"kind": kind, "jq_expr": ".items"})
    )
    assert response.sent is False
    assert response.source_status == "RESOURCE_NOT_ALLOWED"
    assert api.requests == []
    assert "RESOURCE_NOT_ALLOWED" in DENIED_REASONS


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"kind": "Pods", "jq_expr": "."}, "UNKNOWN_KIND"),
        ({"kind": "widgets", "jq_expr": "."}, "UNKNOWN_KIND"),
        ({"kind": "pods", "jq_expr": ".", "namespace": "Bad_NS"}, "INVALID_NAMESPACE"),
        ({"kind": "pods", "jq_expr": 'import "x" as y; .'}, "INVALID_JQ"),
        ({"kind": "pods", "jq_expr": 'include "x"; .'}, "INVALID_JQ"),
        ({"kind": "pods", "jq_expr": "." * 2001}, "INVALID_JQ"),
        ({"kind": "pods", "jq_expr": ".items[] |"}, "JQ_COMPILE_ERROR"),
        (
            {"kind": "pods", "jq_expr": ".", "label_selector": "a\nb"},
            "INVALID_SELECTOR",
        ),
    ],
)
def test_bad_input_is_refused_before_any_request(params, code):
    api = FakeApi()
    response = _transport(api).fetch(_request(k8s.JQ_QUERY_TOOL, params))
    assert (response.sent, response.source_status) == (False, code)
    assert api.requests == []
    assert k8s.ERROR_CLASSES[code] in {"INVALID_PARAMS", "RESOURCE_NOT_ALLOWED"}


def test_missing_k8s_slot_is_a_credential_error_and_sends_nothing():
    api = FakeApi({"/api/v1/pods": [_list("Pod", [])]})
    transport = _transport(api, credentials={otel_demo.CREDENTIAL_REF: OTEL_TOKEN})
    with pytest.raises(TransportError, match="CREDENTIAL_REF_UNKNOWN"):
        transport.fetch(_request(k8s.JQ_QUERY_TOOL, {"kind": "pods", "jq_expr": "."}))
    assert api.requests == []


# -- reads --------------------------------------------------------------------


def test_pages_are_read_to_the_end_merged_and_jq_runs_once():
    api = FakeApi(
        {
            "/api/v1/namespaces/otel-demo/pods": [
                _list("Pod", [_pod("a"), _pod("b", "Pending")], token="next-1"),
                _list("Pod", [_pod("c", "Failed")]),
            ]
        }
    )
    response = _transport(api).fetch(
        _request(
            k8s.JQ_QUERY_TOOL,
            {
                "kind": "pods",
                "namespace": "otel-demo",
                "label_selector": "app=checkout",
                "jq_expr": '.items[] | select(.status.phase != "Running") '
                "| {name: .metadata.name, kind, mf: .metadata.managedFields}",
            },
        )
    )
    assert response.source_status is None
    assert json.loads(response.body) == {
        "results": [
            {"name": "b", "kind": "Pod", "mf": None},
            {"name": "c", "kind": "Pod", "mf": None},
        ]
    }
    assert api.query(0) == {"limit": ["500"], "labelSelector": ["app=checkout"]}
    assert api.query(1)["continue"] == ["next-1"]
    assert api.headers(0)["authorization"] == f"Bearer {K8S_TOKEN}"
    assert all(r.get_method() == "GET" for r in api.requests)
    fields = response.view_fields or {}
    assert fields["total_items_processed"] == 3
    assert fields["pages_read"] == 2
    assert fields["scope"]["all_namespaces"] is False
    assert (response.source_start_at, response.source_end_at) == (None, None)


def test_namespace_omitted_reads_the_whole_cluster_and_cluster_kinds_ignore_it():
    api = FakeApi(
        {
            "/api/v1/pods": [_list("Pod", [_pod("a")])],
            "/api/v1/nodes": [_list("Node", [{"metadata": {"name": "n1"}}])],
        }
    )
    transport = _transport(api)
    all_pods = transport.fetch(
        _request(k8s.JQ_QUERY_TOOL, {"kind": "pods", "jq_expr": ".items | length"})
    )
    assert json.loads(all_pods.body) == {"results": [1]}
    assert (all_pods.view_fields or {})["scope"]["all_namespaces"] is True
    nodes = transport.fetch(
        _request(
            k8s.JQ_QUERY_TOOL,
            {
                "kind": "nodes",
                "namespace": "otel-demo",
                "jq_expr": ".items[].metadata.name",
            },
        )
    )
    assert json.loads(nodes.body) == {"results": ["n1"]}
    assert "cluster-scoped" in (nodes.view_fields or {})["notes"][0]


def test_count_returns_total_and_a_clipped_preview():
    pods = [_pod(f"pod-{i:02d}-" + "x" * 300) for i in range(12)]
    api = FakeApi({"/api/v1/pods": [_list("Pod", pods)]})
    response = _transport(api).fetch(
        _request(k8s.COUNT_TOOL, {"kind": "pods", "jq_expr": ".items[].metadata.name"})
    )
    body = json.loads(response.body)
    assert body["count"] == 12
    assert len(body["preview"]) == 10
    assert all(len(line) == 200 for line in body["preview"])
    assert (response.row_unit, response.backend_rows_returned) == ("matches", 12)


def test_events_outside_the_window_or_untimed_are_dropped_before_jq():
    inside = (WINDOW.end - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    early = (WINDOW.start - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    events = [
        {"metadata": {"name": "e1"}, "lastTimestamp": inside, "reason": "BackOff"},
        {"metadata": {"name": "e2"}, "lastTimestamp": early, "reason": "Old"},
        {"metadata": {"name": "e3"}, "lastTimestamp": None, "eventTime": None},
        {"metadata": {"name": "e4"}, "lastTimestamp": None, "eventTime": inside},
    ]
    api = FakeApi({"/api/v1/events": [_list("Event", events)]})
    response = _transport(api).fetch(
        _request(
            k8s.JQ_QUERY_TOOL, {"kind": "events", "jq_expr": "[.items[].metadata.name]"}
        )
    )
    assert json.loads(response.body) == {"results": [["e1", "e4"]]}
    notes = " ".join((response.view_fields or {})["notes"])
    assert "1 events outside" in notes and "1 events without" in notes
    assert response.source_start_at == response.source_end_at
    assert response.source_start_at is not None
    assert WINDOW.start <= response.source_start_at <= WINDOW.end


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (403, "SOURCE_FORBIDDEN"),
        (401, "SOURCE_FORBIDDEN"),
        (404, "INVALID_PARAMS"),
        (410, "SOURCE_ERROR"),
        (429, "SOURCE_UNAVAILABLE"),
    ],
)
def test_http_errors_carry_the_status_for_classification(status, reason):
    api = FakeApi({"/api/v1/pods": status})
    response = _transport(api).fetch(
        _request(k8s.JQ_QUERY_TOOL, {"kind": "pods", "jq_expr": "."})
    )
    assert (response.source_status, response.http_status) == (str(status), status)
    assert k8s.ERROR_CLASSES[str(status)] == reason


def test_more_than_4_mib_read_refuses_the_whole_call():
    big = [_pod("p" + "x" * 1000) for _ in range(2500)]
    pages = [
        _list("Pod", big[:1250], token="t"),
        _list("Pod", big[1250:], token="t2"),
        _list("Pod", big),
    ]
    api = FakeApi({"/api/v1/pods": pages})
    with pytest.raises(TransportResultTooLarge):
        _transport(api).fetch(
            _request(k8s.JQ_QUERY_TOOL, {"kind": "pods", "jq_expr": "."})
        )


def test_jq_output_past_2_mib_is_too_large():
    api = FakeApi({"/api/v1/pods": [_list("Pod", [_pod("a")])]})
    with pytest.raises(TransportResultTooLarge):
        _transport(api).fetch(
            _request(k8s.JQ_QUERY_TOOL, {"kind": "pods", "jq_expr": "range(500000)"})
        )


def test_jq_timeout_is_a_confirmed_contact_timeout():
    api = FakeApi({"/api/v1/pods": [_list("Pod", [_pod("a")])]})
    with pytest.raises(TransportTimeout) as raised:
        _transport(api).fetch(
            _request(
                k8s.JQ_QUERY_TOOL,
                {"kind": "pods", "jq_expr": "[.items[] | until(false; .)]"},
                timeout=1.0,
            )
        )
    assert raised.value.source_contact == "confirmed"


def test_jq_runs_with_an_empty_environment():
    api = FakeApi({"/api/v1/pods": [_list("Pod", [])]})
    response = _transport(api).fetch(
        _request(
            k8s.JQ_QUERY_TOOL, {"kind": "pods", "jq_expr": "[env, $ENV] | map(length)"}
        )
    )
    assert json.loads(response.body) == {"results": [[0, 0]]}


def test_jq_runtime_error_is_reported_after_the_read():
    api = FakeApi({"/api/v1/pods": [_list("Pod", [_pod("a")])]})
    response = _transport(api).fetch(
        _request(
            k8s.JQ_QUERY_TOOL,
            {"kind": "pods", "jq_expr": ".items[] | .metadata.name + 1"},
        )
    )
    assert (response.sent, response.source_status) == (True, "JQ_RUNTIME_ERROR")
    assert k8s.ERROR_CLASSES["JQ_RUNTIME_ERROR"] == "INVALID_PARAMS"


# -- routing, profile and configuration ----------------------------------------


class _Marker:
    def __init__(self, name):
        self.name = name
        self.seen: list[str] = []

    def fetch(self, request):
        self.seen.append(request.tool)
        return TransportResponse(body=b"{}")


def test_routed_transport_dispatches_on_exact_names():
    evidence, sources, cluster = _Marker("e"), _Marker("s"), _Marker("k")
    routed = RoutedTransport(
        evidence=evidence, sources=sources, routes={k8s.JQ_QUERY_TOOL: cluster}
    )
    for tool in (
        k8s.JQ_QUERY_TOOL,
        "kubernetes_other",
        "read_evidence",
        "fetch_pod_logs",
    ):
        routed.fetch(_request(tool, {}))
    assert cluster.seen == [k8s.JQ_QUERY_TOOL]
    assert sources.seen == ["kubernetes_other", "fetch_pod_logs"]
    assert evidence.seen == ["read_evidence"]


def test_web_builds_face_and_versions_without_cluster_settings():
    profile = select_profile({"OPSPILOT_TOOL_PROFILE": "otel-demo-ext"})
    assert profile.name == "otel-demo-ext"
    revision = profile.versions()["tool_schema_revision"]
    assert revision.startswith("otel-demo-ext-")
    assert revision != otel_demo.TOOL_SCHEMA_REVISION
    names = [
        s["function"]["name"] for s in profile.face(FakeClock(start=NOW)).tool_schemas
    ]
    assert names[-2:] == [k8s.JQ_QUERY_TOOL, k8s.COUNT_TOOL]
    assert not set(k8s.TOOL_NAMES) & set(otel_demo.TOOL_NAMES)
    assert not set(k8s.TOOL_NAMES) & otel_demo.PROMETHEUS_TOOLS
    context = profile.face(FakeClock(start=NOW)).evidence_context("run-1")
    assert context["data_sources"]["available"]["kubernetes"] == sorted(k8s.TOOL_NAMES)
    assert "kubernetes_state" not in context["data_sources"]["not_integrated"]


def test_worker_without_cluster_settings_fails_with_a_fixed_code():
    profile = select_profile({"OPSPILOT_TOOL_PROFILE": "otel-demo-ext"})
    with pytest.raises(ToolContractError, match="K8S_CONFIG_MISSING"):
        profile.executor_factory(object(), RecordingSink(), FakeClock(start=NOW))  # type: ignore[arg-type]


def test_worker_without_jq_fails_with_a_fixed_code(monkeypatch, tmp_path):
    ca = tmp_path / "ca.crt"
    ca.write_text("not a certificate")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    profile = select_profile(
        {
            "OPSPILOT_TOOL_PROFILE": "otel-demo-ext",
            "OPSPILOT_K8S_API_URL": API,
            "OPSPILOT_K8S_TOKEN": K8S_TOKEN,
            "OPSPILOT_K8S_CA_FILE": str(ca),
        }
    )
    with pytest.raises(ToolContractError, match="JQ_UNAVAILABLE"):
        profile.executor_factory(object(), RecordingSink(), FakeClock(start=NOW))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "env",
    [
        {"OPSPILOT_K8S_API_URL": "http://127.0.0.1:6443"},
        {"OPSPILOT_K8S_NAMESPACE": "Bad_NS"},
        {"OPSPILOT_K8S_CA_FILE": "/nonexistent/ca.crt"},
    ],
)
def test_malformed_cluster_settings_are_refused(tmp_path, env):
    ca = tmp_path / "ca.crt"
    ca.write_text("x")
    base = {
        "OPSPILOT_K8S_API_URL": API,
        "OPSPILOT_K8S_TOKEN": K8S_TOKEN,
        "OPSPILOT_K8S_CA_FILE": str(ca),
    }
    with pytest.raises(ToolContractError, match="K8S_CONFIG_INVALID"):
        k8s.KubernetesConfig.from_env({**base, **env})


def test_lab_gate_requires_a_lab_cluster_address():
    env = {
        "OPSPILOT_TRACE": "lab",
        "OPSPILOT_TOOL_PROFILE": "otel-demo-ext",
        "LANGSMITH_PROJECT": "opspilot-lab-a2",
        "LANGSMITH_API_KEY": "cccccccc357",
    }
    assert "KUBERNETES_NOT_LAB" in check_lab_target(env)
    assert "KUBERNETES_NOT_LAB" in check_lab_target(
        {**env, "OPSPILOT_K8S_API_URL": "https://cluster.example.com"}
    )
    assert check_lab_target({**env, "OPSPILOT_K8S_API_URL": API}) == ()


# -- through the executor --------------------------------------------------------


class _Store:
    def __init__(self, lease: Lease):
        self.lease = lease

    def rebuild(self, incident_id):
        return {
            "run": {
                "run_id": self.lease.run_id,
                "deadline": NOW + timedelta(minutes=10),
                "tool_operations_used": 0,
                "tool_seconds_used": 0.0,
            }
        }

    def charge_tool(self, *args, **kwargs):
        return None

    def control_state(self, incident_id):
        return {
            "incident_generation": self.lease.control_generation,
            "incident_state": "running",
            "global_suspended": False,
            "global_generation": self.lease.global_suspension_generation,
            "target_suspended": False,
            "target_generation": self.lease.target_suspension_generation,
        }


def _executor(monkeypatch, tmp_path, api: FakeApi) -> ReadOnlyToolExecutor:
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: api)
    ca = tmp_path / "ca.crt"
    ca.write_text("x")
    lease = Lease(
        incident_id=uuid4(),
        run_id=uuid4(),
        owner=uuid4(),
        epoch=1,
        control_generation=1,
        global_suspension_generation=1,
        target_suspension_generation=1,
    )
    factory = otel_demo_ext.otel_demo_ext_executor_factory(
        _Store(lease),  # type: ignore[arg-type]
        evidence=RecordingSink(),
        clock=FakeClock(start=NOW),
        config=otel_demo.OtelDemoConfig(token=OTEL_TOKEN),
        kubernetes=k8s.KubernetesConfig(api_url=API, token=K8S_TOKEN, ca_file=str(ca)),
    )

    def context(run_id: str) -> dict:
        return {
            "type": "opspilot-evidence-context-v4",
            "run_id": run_id,
            "time_policies": [
                {
                    "id": "policy-window-1",
                    "mode": "historical_window",
                    "all_authorized_targets": True,
                    "window": WINDOW.as_json(),
                    "reference_rule": "response_received_at",
                }
            ],
        }

    face = ToolFace(
        tool_schemas=otel_demo_ext.TOOL_SCHEMAS,
        variant_id=DISCIPLINE_VARIANT,
        evidence_context=context,
    )
    run_input = face.input_for(
        run_id=str(lease.run_id),
        question="Why is checkout crashing?",
        target_id=otel_demo.TARGET_ID,
        deadline=NOW + timedelta(minutes=10),
        model_requests=2,
    )
    return factory(lease, run_input)


def _call(tool: str, params: dict) -> ToolRequest:
    return ToolRequest(
        step_id="step-1",
        tool_index=0,
        tool_name=tool,
        target_ref=otel_demo.TARGET_ID,
        params=params,
        window=WINDOW.as_json(),
    )


def test_executor_adopts_a_query_and_denies_secrets_and_403(monkeypatch, tmp_path):
    api = FakeApi({"/api/v1/pods": [_list("Pod", [_pod("a", "CrashLoopBackOff")])]})
    executor = _executor(monkeypatch, tmp_path, api)
    ok = executor.execute(
        _call(k8s.JQ_QUERY_TOOL, {"kind": "pods", "jq_expr": ".items[].status.phase"})
    )
    assert (ok.status, ok.model_view["content"]) == ("ok", ["CrashLoopBackOff"])
    assert ok.model_view["source_start_at"] is None
    assert api.headers(0)["authorization"] == f"Bearer {K8S_TOKEN}"
    secret = executor.execute(
        _call(k8s.COUNT_TOOL, {"kind": "Secret", "jq_expr": ".items[]"})
    )
    assert (secret.status, secret.reason) == ("denied", "RESOURCE_NOT_ALLOWED")
    api.pages["/api/v1/pods"] = 403
    forbidden = executor.execute(
        _call(k8s.JQ_QUERY_TOOL, {"kind": "pods", "jq_expr": "."})
    )
    assert (forbidden.status, forbidden.reason) == ("denied", "SOURCE_FORBIDDEN")
    assert len(api.requests) == 2
