"""Independent contract tests for tool-surface expansion A2 (#554).

These tests were written from the A2 task record, the r1/r2 contract table and
C3 sections 3, 8 and 12.  They intentionally import the new module before it
exists on ``main``: this worktree is the independent contract-test starting
point, so a red collection run is expected until the implementation worktree
lands.

接缝假设（实现必须按公开行为满足；若实现接缝不同，应在不放宽合同的前提下
由实现者适配）：

* ``select_profile`` exposes ``otel-demo-ext`` through the existing
  ``ToolProfile.face/versions/executor_factory`` seam; the extension keeps the
  OTel target identity and the base face's input shape.
* The worker-side profile factory validates the four Kubernetes environment
  variables lazily (web-side profile selection and face/version construction
  do not read them), and reports ``K8S_CONFIG_MISSING`` or ``JQ_NOT_FOUND`` as
  ``SystemExit`` startup codes.  Each test supplies a readable CA file under
  its own ``tmp_path``; no system CA path is assumed.
* ``opspilot.tools.kubernetes_core`` exposes the analogous public
  ``KubernetesTransport`` seam used by ``otel_demo.OtelDemoTransport``; an
  unknown ``k8s-ro`` slot raises ``CREDENTIAL_REF_UNKNOWN`` before a request
  is sent.  Its registration inventory follows the existing OTel private
  ``_registrations()`` test seam so the read-only acceptance test can inspect
  every shipped registration.
* The extension reuses the existing ``ToolRequest``/``ToolOutcome`` executor
  seam and the injected ``urllib.request.build_opener`` fake used by the
  OTel-demo contract tests.  A Kubernetes List response is an official
  ``*List`` JSON object; jq is evaluated once over the merged list stream and
  the resulting adapter object is exposed as ``model_view['content']``.
* The explicit route constructor extends ``RoutedTransport`` with a
  ``kubernetes=`` transport.  Dispatch is by exact registered tool name; the
  test deliberately uses a future ``fetch_pod_logs`` name to catch prefix
  routing.
* The lab-gate failure code for a non-lab Kubernetes API is
  ``KUBERNETES_NOT_LAB``.  The two new source reason codes are the task-record
  names ``RESOURCE_NOT_ALLOWED`` and ``SOURCE_FORBIDDEN``.

All synthetic credentials use low-entropy placeholders.  No test starts a
container or contacts a real Kubernetes API.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import stat
import urllib.error
import urllib.request
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

import opspilot.tools.kubernetes_core as kubernetes_core
from opspilot.tools import (
    ToolRequest,
    TransportError,
    TransportRequest,
    TransportResponse,
)
from opspilot.tools.evidence_read import RoutedTransport
from opspilot.tools.otel_demo import (
    CREDENTIAL_REF,
    METRICS_TOOL,
    TARGET_ID,
    TRACES_TOOL,
)
from opspilot.tools.profiles import PROFILE_ENV, ToolProfile, select_profile
from opspilot.tracing import check_lab_target
from tests.m1_tool_support import FakeClock, RecordingSink
from tests.test_m1_otel_demo_contract import (
    CHECKOUT_BODY,
    CHECKOUT_TRACES,
    NOW,
    FakeStore,
    _lease,
)
from tests.test_m1_otel_demo_contract import (
    WINDOW as AUTHORIZED_WINDOW,
)

EXT_PROFILE = "otel-demo-ext"
K8S_TOKEN = "k8s-aaaaaaaa357"
OTEL_TOKEN = "otel-aaaaaaaa357"
K8S_API = "https://127.0.0.1:16443"
K8S_NAMESPACE = "otel-demo"
K8S_JQ_TOOL = "kubernetes_jq_query"
K8S_COUNT_TOOL = "kubernetes_count"
BASE_TOOL_SCHEMA_REVISION = "otel-demo-a3a95a0f888e"
BASE_TOOL_NAMES = (
    "metrics_range_query",
    "traces_search",
    "get_metric_names",
    "get_label_values",
    "read_evidence",
)


@pytest.fixture(autouse=True)
def _synthetic_k8s_ca(tmp_path, monkeypatch):
    ca_file = tmp_path / "synthetic-ca.pem"
    ca_file.write_text("synthetic ca placeholder\n")
    monkeypatch.setenv("A2_TEST_K8S_CA_FILE", str(ca_file))


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


class KubeOpener:
    """urllib opener double with ordered responses and request capture."""

    def __init__(self, responses=(), *, by_path=None):
        self.responses = list(responses)
        self.by_path = dict(by_path or {})
        self.requests: list[object] = []
        self.timeouts: list[float | None] = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        self.timeouts.append(timeout)
        url = (
            request.full_url
            if isinstance(request, urllib.request.Request)
            else str(request)
        )
        path = urlsplit(url).path
        if path in self.by_path:
            value = self.by_path[path]
        elif self.responses:
            value = self.responses.pop(0)
        else:
            raise AssertionError(f"unexpected Kubernetes request: {url}")
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, int):
            raise urllib.error.HTTPError(
                url, value, "synthetic status", {}, io.BytesIO(b"{}")
            )
        return _reply(value)


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


def _list(items, *, continue_token: str | None = None, kind: str = "PodList") -> bytes:
    return json.dumps(
        {
            "apiVersion": "v1",
            "kind": kind,
            "metadata": {
                "resourceVersion": "aaaaaaaa357",
                "continue": continue_token or "",
            },
            "items": items,
        },
        separators=(",", ":"),
    ).encode()


def _pod(
    name: str,
    phase: str = "Running",
    *,
    namespace: str = K8S_NAMESPACE,
    blob: str | None = None,
):
    metadata = {
        "name": name,
        "namespace": namespace,
        "labels": {"app": "checkout"},
        "creationTimestamp": "2026-09-14T00:10:00Z",
        "managedFields": [{"manager": "synthetic", "operation": "Apply"}],
    }
    value = {"metadata": metadata, "status": {"phase": phase}}
    if blob is not None:
        value["blob"] = blob
    return value


def _event(name: str, when: str, *, reason: str = "BackOff"):
    return {
        "metadata": {
            "name": name,
            "namespace": K8S_NAMESPACE,
            "creationTimestamp": when,
            "managedFields": [{"manager": "synthetic"}],
        },
        "reason": reason,
        "eventTime": when,
        "lastTimestamp": when,
        "type": "Warning",
    }


def _env(**overrides) -> dict[str, str]:
    values = {
        PROFILE_ENV: EXT_PROFILE,
        "OPSPILOT_K8S_API_URL": K8S_API,
        "OPSPILOT_K8S_TOKEN": K8S_TOKEN,
        "OPSPILOT_K8S_CA_FILE": os.environ["A2_TEST_K8S_CA_FILE"],
        "OPSPILOT_K8S_NAMESPACE": K8S_NAMESPACE,
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://127.0.0.1:19090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://127.0.0.1:16686/jaeger/ui",
        "OPSPILOT_OTEL_TOKEN": OTEL_TOKEN,
    }
    values.update(overrides)
    return values


def _require_jq() -> None:
    if shutil.which("jq") is None:
        pytest.skip("local jq is required for Kubernetes execution tests")


def _input(profile: ToolProfile, lease, clock: FakeClock):
    return profile.face(clock).input_for(
        run_id=str(lease.run_id),
        question="Why is checkout degraded?",
        target_id=TARGET_ID,
        deadline=NOW + timedelta(minutes=10),
        model_requests=8,
    )


def _executor(monkeypatch, opener, *, env=None):
    _require_jq()
    env = _env(**(env or {}))
    profile = select_profile(env)
    assert isinstance(profile, ToolProfile)
    lease = _lease()
    store = FakeStore(lease, deadline=NOW + timedelta(minutes=10))
    clock = FakeClock(start=NOW)
    sink = RecordingSink()
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: opener)
    factory = profile.executor_factory(store, sink, clock)
    executor = factory(lease, _input(profile, lease, clock))
    return executor, sink, lease, clock, profile


def _call(tool: str, params: dict, *, index: int = 0) -> ToolRequest:
    return ToolRequest(
        step_id="a2-step",
        tool_index=index,
        tool_name=tool,
        target_ref=TARGET_ID,
        params=params,
        window=AUTHORIZED_WINDOW.as_json(),
    )


def _query(kind="pods", expr=".items[] | .metadata.name", **kwargs) -> dict:
    return {"kind": kind, "jq_expr": expr, **kwargs}


def _view(outcome):
    assert outcome.model_view is not None
    return outcome.model_view


def test_web_selects_extension_without_any_kubernetes_variables(monkeypatch):
    for name in (
        "OPSPILOT_K8S_API_URL",
        "OPSPILOT_K8S_TOKEN",
        "OPSPILOT_K8S_CA_FILE",
        "OPSPILOT_K8S_NAMESPACE",
    ):
        monkeypatch.delenv(name, raising=False)
    profile = select_profile({PROFILE_ENV: EXT_PROFILE})
    assert profile.name == EXT_PROFILE
    face = profile.face(FakeClock(start=NOW))
    assert {schema["function"]["name"] for schema in face.tool_schemas} >= {
        K8S_JQ_TOOL,
        K8S_COUNT_TOOL,
    }
    assert profile.versions()["tool_schema_revision"].startswith("otel-demo-ext-")


@pytest.mark.parametrize(
    "missing",
    [
        "OPSPILOT_K8S_API_URL",
        "OPSPILOT_K8S_TOKEN",
        "OPSPILOT_K8S_CA_FILE",
    ],
)
def test_worker_missing_kubernetes_configuration_has_a_fixed_startup_code(missing):
    env = _env()
    env.pop(missing)
    profile = select_profile(env)
    lease = _lease()
    store = FakeStore(lease, deadline=NOW + timedelta(minutes=10))
    clock = FakeClock(start=NOW)
    with pytest.raises(SystemExit, match="K8S_CONFIG_MISSING"):
        factory = profile.executor_factory(store, RecordingSink(), clock)
        factory(lease, _input(profile, lease, clock))


def test_worker_missing_jq_has_a_fixed_startup_code(monkeypatch):
    monkeypatch.setenv("PATH", "")
    profile = select_profile(_env())
    lease = _lease()
    store = FakeStore(lease, deadline=NOW + timedelta(minutes=10))
    clock = FakeClock(start=NOW)
    with pytest.raises(SystemExit, match="JQ_NOT_FOUND"):
        factory = profile.executor_factory(store, RecordingSink(), clock)
        factory(lease, _input(profile, lease, clock))


def test_extension_face_is_base_face_plus_two_tools_and_base_is_byte_stable():
    base = select_profile({PROFILE_ENV: "otel-demo"})
    ext = select_profile({PROFILE_ENV: EXT_PROFILE})
    base_face = base.face(FakeClock(start=NOW))
    ext_face = ext.face(FakeClock(start=NOW))
    base_names = [schema["function"]["name"] for schema in base_face.tool_schemas]
    ext_names = [schema["function"]["name"] for schema in ext_face.tool_schemas]
    assert tuple(base_names) == BASE_TOOL_NAMES
    assert ext_names[: len(base_names)] == base_names
    assert set(ext_names) == set(base_names) | {K8S_JQ_TOOL, K8S_COUNT_TOOL}
    assert base.versions()["tool_schema_revision"] == BASE_TOOL_SCHEMA_REVISION
    assert base.versions() == select_profile({PROFILE_ENV: "otel-demo"}).versions()
    assert ext.versions()["tool_schema_revision"].startswith("otel-demo-ext-")


@pytest.mark.parametrize("tool", [K8S_JQ_TOOL, K8S_COUNT_TOOL])
def test_kubernetes_schemas_have_the_upstream_parameter_surface(tool):
    profile = select_profile({PROFILE_ENV: EXT_PROFILE})
    schemas = {
        schema["function"]["name"]: schema
        for schema in profile.face(FakeClock()).tool_schemas
    }
    schema = schemas[tool]["function"]
    assert set(schema["parameters"]["properties"]) == {
        "kind",
        "jq_expr",
        "namespace",
        "label_selector",
        "field_selector",
    }
    assert schema["parameters"]["required"] == ["kind", "jq_expr"]
    text = schema["description"].lower()
    assert "cannot" in text and "result_too_large" in text


def test_kubernetes_registrations_declare_that_cluster_objects_may_contain_secrets():
    registrations = kubernetes_core._registrations()
    assert {item.name for item in registrations} == {K8S_JQ_TOOL, K8S_COUNT_TOOL}
    assert all(item.may_contain_secrets is True for item in registrations)


def test_pod_status_query_uses_list_shape_strips_managed_fields_and_records_current_state(
    monkeypatch,
):
    opener = KubeOpener(
        [_list([_pod("checkout-1", "Running"), _pod("checkout-2", "Pending")])]
    )
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(
        _call(
            K8S_JQ_TOOL,
            _query(
                expr=".items[] | {name: .metadata.name, phase: .status.phase, managed: .metadata.managedFields}",
                namespace=K8S_NAMESPACE,
                label_selector="app=checkout",
                field_selector="status.phase!=Succeeded",
            ),
        )
    )
    assert (outcome.status, outcome.reason) == ("ok", None), outcome.model_view
    view = _view(outcome)
    assert view["content"] == [
        {"name": "checkout-1", "phase": "Running", "managed": None},
        {"name": "checkout-2", "phase": "Pending", "managed": None},
    ]
    assert view["source_start_at"] is None and view["source_end_at"] is None
    assert "cannot" in str(view.get("cannot_prove", view)).lower()
    request = opener.requests[0]
    assert request.get_method() == "GET"
    parsed = urlsplit(_url(request))
    assert parsed.path.endswith("/api/v1/namespaces/otel-demo/pods")
    query = parse_qs(parsed.query)
    assert query["limit"] == ["500"]
    assert query["labelSelector"] == ["app=checkout"]
    assert query["fieldSelector"] == ["status.phase!=Succeeded"]


def test_namespace_omitted_queries_all_namespaces_and_cluster_kind_ignores_namespace(
    monkeypatch,
):
    all_ns = KubeOpener([_list([_pod("a"), _pod("b", namespace="other")])])
    executor, _, _, _, _ = _executor(monkeypatch, all_ns)
    outcome = executor.execute(
        _call(K8S_JQ_TOOL, _query(expr=".items[] | .metadata.name"))
    )
    assert outcome.status == "ok"
    assert urlsplit(_url(all_ns.requests[0])).path.endswith("/api/v1/pods")

    nodes = KubeOpener(
        [
            _list(
                [{"metadata": {"name": "node-a"}, "status": {"conditions": []}}],
                kind="NodeList",
            )
        ]
    )
    executor, _, _, _, _ = _executor(monkeypatch, nodes)
    outcome = executor.execute(
        _call(
            K8S_JQ_TOOL,
            _query(
                kind="nodes", expr=".items[] | .metadata.name", namespace=K8S_NAMESPACE
            ),
        )
    )
    assert outcome.status == "ok"
    assert urlsplit(_url(nodes.requests[0])).path.endswith("/api/v1/nodes")
    assert "ignored" in json.dumps(_view(outcome)["notes"]).lower()


def test_event_query_keeps_only_authorized_events_and_notes_discarded_count(
    monkeypatch,
):
    inside = (AUTHORIZED_WINDOW.start + timedelta(minutes=1)).isoformat()
    outside = (AUTHORIZED_WINDOW.end + timedelta(minutes=1)).isoformat()
    opener = KubeOpener(
        [
            _list(
                [_event("in-window", inside), _event("out-window", outside)],
                kind="EventList",
            )
        ]
    )
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(
        _call(
            K8S_JQ_TOOL,
            _query(kind="events", expr=".items[] | .reason", namespace=K8S_NAMESPACE),
        )
    )
    assert outcome.status == "ok"
    view = _view(outcome)
    assert view["content"] == ["BackOff"]
    notes = json.dumps(view["notes"]).lower()
    assert "1" in notes and ("discard" in notes or "drop" in notes)


def test_namespace_is_a_dns_label_and_bad_or_unknown_kinds_never_send_a_request(
    monkeypatch,
):
    opener = KubeOpener([_list([])])
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    for params in (
        _query(namespace="Bad_Namespace"),
        _query(kind="widgets"),
    ):
        outcome = executor.execute(_call(K8S_JQ_TOOL, params))
        assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    assert opener.requests == []


@pytest.mark.parametrize("kind", ["secrets", "Secrets", "secret", "Secret"])
def test_secret_variants_are_resource_denied_without_a_cluster_request(
    monkeypatch, kind
):
    opener = KubeOpener([])
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(K8S_JQ_TOOL, _query(kind=kind)))
    assert (outcome.status, outcome.reason) == ("error", "RESOURCE_NOT_ALLOWED")
    assert opener.requests == []


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (401, "SOURCE_FORBIDDEN"),
        (403, "SOURCE_FORBIDDEN"),
        (400, "INVALID_PARAMS"),
        (422, "INVALID_PARAMS"),
        (404, "INVALID_PARAMS"),
        (410, "SOURCE_ERROR"),
        (429, "SOURCE_UNAVAILABLE"),
        (500, "SOURCE_UNAVAILABLE"),
        (503, "SOURCE_UNAVAILABLE"),
    ],
)
def test_kubernetes_http_statuses_map_to_fixed_outcome_reasons(
    monkeypatch, status, reason
):
    opener = KubeOpener([status])
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(K8S_JQ_TOOL, _query()))
    assert (outcome.status, outcome.reason) == ("error", reason)


def test_kubernetes_transport_does_not_follow_redirects(monkeypatch):
    opener = KubeOpener([302, _list([_pod("must-not-be-read")])])
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(K8S_JQ_TOOL, _query()))
    assert outcome.status == "error" and outcome.reason == "SOURCE_ERROR"
    assert len(opener.requests) == 1


def test_pagination_is_500_per_page_and_jq_runs_once_after_all_pages(
    monkeypatch, tmp_path
):
    _require_jq()
    real_jq = shutil.which("jq")
    assert real_jq is not None
    counter = tmp_path / "jq.calls"
    wrapper = tmp_path / "jq"
    wrapper.write_text(f'#!/bin/sh\necho x >> {counter}\nexec {real_jq} "$@"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")
    opener = KubeOpener(
        [
            _list([_pod("p1")], continue_token="next-page"),
            _list([_pod("p2", "Pending")]),
        ]
    )
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    counter.write_text("")
    outcome = executor.execute(
        _call(K8S_COUNT_TOOL, _query(expr=".items[] | .status.phase"))
    )
    assert outcome.status == "ok"
    view = _view(outcome)
    assert view["matches_found"] == 2
    assert len(opener.requests) == 2
    for request in opener.requests:
        assert parse_qs(urlsplit(_url(request)).query)["limit"] == ["500"]
    assert len(counter.read_text().splitlines()) == 1


def test_count_returns_total_and_only_ten_previews_each_at_most_200_chars(monkeypatch):
    items = [_pod("n" + str(i) + "x" * 300) for i in range(12)]
    opener = KubeOpener([_list(items)])
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(
        _call(K8S_COUNT_TOOL, _query(expr=".items[] | .metadata.name"))
    )
    assert outcome.status == "ok"
    view = _view(outcome)
    assert view["matches_found"] == 12
    preview = view["content"]
    assert len(preview) == 10
    assert all(len(str(item)) <= 200 for item in preview)


def test_raw_api_bytes_over_4_mib_are_refused_as_a_whole(monkeypatch):
    huge = _list([_pod("huge", blob="x" * (4 * 1024 * 1024))])
    opener = KubeOpener([huge])
    executor, sink, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(
        _call(K8S_JQ_TOOL, _query(expr=".items[] | .metadata.name"))
    )
    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
    assert _view(outcome)["content"] is None
    assert sink.records == []


def test_jq_output_over_2_mib_is_refused_as_a_whole(monkeypatch):
    opener = KubeOpener(
        [_list([_pod("large-output", blob="x" * (2 * 1024 * 1024 + 1))])]
    )
    executor, sink, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(K8S_JQ_TOOL, _query(expr=".items[] | .blob")))
    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
    assert _view(outcome)["content"] is None
    assert sink.records == []


@pytest.mark.parametrize(
    "expr",
    [
        'import "x"; .',
        'include "x"; .',
        "." * 2001,
        "[",
    ],
)
def test_jq_import_include_overlong_and_compile_errors_are_invalid_params(
    monkeypatch, expr
):
    opener = KubeOpener([_list([_pod("never-read")])])
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(K8S_JQ_TOOL, _query(expr=expr)))
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    if "import" in expr or "include" in expr or len(expr) > 2000:
        assert opener.requests == []


def test_jq_subprocess_has_an_empty_environment(monkeypatch):
    opener = KubeOpener([_list([_pod("env-probe")])])
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    outcome = executor.execute(_call(K8S_JQ_TOOL, _query(expr="$ENV")))
    assert outcome.status == "ok"
    assert _view(outcome)["content"] == [{}]


def test_kubernetes_and_otel_credentials_are_separate_and_named_by_slot(monkeypatch):
    _require_jq()

    class Router(KubeOpener):
        def open(self, request, timeout=None):
            url = _url(request)
            path = urlsplit(url).path
            self.requests.append(request)
            self.timeouts.append(timeout)
            if "/api/v1/namespaces/otel-demo/pods" in path:
                return _reply(_list([_pod("credential-check")]))
            if path.endswith("/api/v1/query_range"):
                return _reply(CHECKOUT_BODY)
            if path.endswith("/api/traces"):
                return _reply(CHECKOUT_TRACES)
            raise AssertionError(url)

    opener = Router()
    executor, _, _, _, _ = _executor(monkeypatch, opener)
    k8s = executor.execute(_call(K8S_JQ_TOOL, _query(namespace=K8S_NAMESPACE)))
    prom = executor.execute(
        _call(
            METRICS_TOOL,
            {
                "expr": "sum by(service_name) (increase(traces_span_metrics_calls_total[5m]))"
            },
            index=1,
        )
    )
    traces = executor.execute(
        _call(TRACES_TOOL, {"service": "checkout", "limit": 1}, index=2)
    )
    assert {
        k8s.operation.credential_ref,
        prom.operation.credential_ref,
        traces.operation.credential_ref,
    } == {CREDENTIAL_REF}
    for request in opener.requests:
        headers = _headers(request)
        if "/api/v1/namespaces/otel-demo/pods" in _url(request):
            assert headers.get("authorization") == f"Bearer {K8S_TOKEN}"
            assert OTEL_TOKEN not in json.dumps(headers)
        else:
            assert K8S_TOKEN not in json.dumps(headers)


def test_missing_k8s_ro_credential_is_not_replaced_by_an_otel_credential():
    transport = kubernetes_core.KubernetesTransport(
        credentials={"otel-demo-ro": OTEL_TOKEN}
    )
    request = TransportRequest(
        operation_id="a2-t0",
        source="otel-demo",
        verb="query",
        endpoint=K8S_API,
        selector={},
        params=_query(),
        window=AUTHORIZED_WINDOW,
        timeout_seconds=1.0,
        max_result_bytes=4 * 1024 * 1024,
        credential_ref="k8s-ro",
        tool=K8S_JQ_TOOL,
        target_id=TARGET_ID,
    )
    with pytest.raises(TransportError, match="CREDENTIAL_REF_UNKNOWN"):
        transport.fetch(request)


def test_explicit_route_does_not_send_future_non_kubernetes_tool_to_kubernetes():
    class Recording:
        def __init__(self, body=b"{}"):
            self.requests = []
            self.body = body

        def fetch(self, request):
            self.requests.append(request)
            return TransportResponse(body=self.body)

    evidence = Recording()
    sources = Recording()
    kubernetes = Recording()
    route = RoutedTransport(evidence=evidence, sources=sources, kubernetes=kubernetes)
    common = dict(
        operation_id="a2-route",
        source="otel-demo",
        verb="query",
        endpoint=K8S_API,
        selector={},
        params={},
        window=AUTHORIZED_WINDOW,
        timeout_seconds=1.0,
        max_result_bytes=1024,
        credential_ref="k8s-ro",
        target_id=TARGET_ID,
    )
    route.fetch(TransportRequest(**common, tool="fetch_pod_logs"))
    route.fetch(TransportRequest(**common, tool=K8S_JQ_TOOL))
    assert len(sources.requests) == 1
    assert len(kubernetes.requests) == 1
    assert sources.requests[0].tool == "fetch_pod_logs"


def test_lab_gate_accepts_extension_only_for_a_lab_kubernetes_host():
    env = {
        "OPSPILOT_TRACE": "lab",
        PROFILE_ENV: EXT_PROFILE,
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://127.0.0.1:19090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://127.0.0.1:16686",
        "LANGSMITH_PROJECT": "opspilot-lab-a2",
        "LANGSMITH_API_KEY": "lsv2-aaaaaaaa357",
        "LANGSMITH_ENDPOINT": "https://api.smith.langchain.com",
        "OPSPILOT_K8S_API_URL": K8S_API,
    }
    assert check_lab_target(env) == ()
    rejected = check_lab_target(
        {**env, "OPSPILOT_K8S_API_URL": "https://kube.external.invalid:6443"}
    )
    assert "KUBERNETES_NOT_LAB" in rejected
