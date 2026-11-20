"""Independent contract tests for tool expansion C4 (#568): ArgoCD read-only
tools -- ``argocd_app_list``, ``argocd_app_get``, ``argocd_app_history`` and
``argocd_app_resources``.

Written from the C4 task record (docs/tasks/2026-11-20-tool-expansion-c4.md,
the public interface), decision table R8-19 and r11 (B4C4-U1=a) and the
B4/C4 pre-review (C4-1 ... C4-11), without reading the implementation. Red
until the implementation lands; collection stays green.

The tools are exercised through the public path: build ``otel-demo-ext`` from
an env mapping, call the tools through the worker's executor, and assert on
the view, evidence, logs, spans and what a loopback HTTPS fake API server
received. No container, no cluster, no model.

Baseline note: B4 (Helm, #563, PR #752) is not on main yet. The interface puts
the four tools after B4's three Helm tools and before ``fetch_skill`` /
``TodoWrite``, and appends their names to B4's ``change_history`` key. So
nothing here pins a revision or a tools-array hash: the switch-off surface is
compared with the same profile built without the switch, and the switch-on
surface with the switch-off one plus exactly the documented delta. Where Helm
is on, its tools are whatever the switch-off surface already has.

Seams assumed beyond the interface (each also marked ``# ASSUMPTION:``):

* An invalid switch value ends the start with ``SystemExit`` whose text
  contains ``ARGOCD_INVALID``, as every other ``_STARTUP_CODES`` entry.
* ``otel_demo_ext._registrations(argocd=True)`` and
  ``otel_demo_ext.tool_schema_revision(argocd=True)`` take ``argocd`` as a
  keyword (the interface names ``tool_schema_revision(..., argocd=False)``).
* A pre-request refusal surfaces as the tool's error class with
  ``source_contact: none``; rows are the view's ``content``; the
  ``sync``/``health`` objects of a get row carry ``status`` keys.
* Row caps (list 300, resources 500) are the interface's estimates; the
  record says they may be lowered after measuring tokens per row. If that
  happens it is a contract change and these numbers move with it.

Synthetic credentials and canaries are low-entropy placeholders.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from opspilot.tools import ToolRequest
from opspilot.tools.outcomes import Window
from opspilot.tools.profiles import PROFILE_ENV, ToolProfile, select_profile
from opspilot.tools.registry import canonical
from tests.m1_tool_support import FakeClock, RecordingSink
from tests.test_m1_otel_demo_contract import NOW, TARGET_ID, FakeStore, _lease

REPO = Path(__file__).resolve().parents[1]
EXT_PROFILE = "otel-demo-ext"
SWITCH = "OPSPILOT_ARGOCD"
HELM_SWITCH = "OPSPILOT_HELM_RELEASES"
LIST = "argocd_app_list"
GET = "argocd_app_get"
HISTORY = "argocd_app_history"
RESOURCES = "argocd_app_resources"
#: The interface table's order.
ARGO_TOOLS = (LIST, GET, HISTORY, RESOURCES)
NAMED_TOOLS = (GET, HISTORY, RESOURCES)

LIST_ROW_FIELDS = {
    "name",
    "namespace",
    "project",
    "sources",
    "destination",
    "sync_status",
    "health_status",
    "operation_phase",
    "reconciled_at",
}
GET_ROW_FIELDS = {
    "name",
    "namespace",
    "project",
    "destination",
    "automated",
    "sources",
    "sync",
    "health",
    "conditions",
    "operation_state",
    "reconciled_at",
    "images",
}
GET_ROW_CORE = {"name", "namespace", "project", "sources", "sync", "health"}
SOURCE_FIELDS = {"repo_url", "path", "chart", "target_revision", "ref", "name"}
HISTORY_ROW_FIELDS = {
    "id",
    "revision",
    "revisions",
    "deployed_at",
    "deploy_started_at",
    "initiated_by",
    "sources",
}
RESOURCE_ROW_FIELDS = {
    "group",
    "version",
    "kind",
    "namespace",
    "name",
    "status",
    "health",
    "hook",
    "requires_pruning",
    "sync_wave",
}
OP_STATE_FIELDS = {
    "phase",
    "message",
    "started_at",
    "finished_at",
    "retry_count",
    "sync_result",
}
SYNC_RESULT_RESOURCE_FIELDS = {
    "group",
    "kind",
    "namespace",
    "name",
    "status",
    "message",
    "hook_phase",
    "sync_phase",
}

K8S_TOKEN = "k8s-ro-aaaaaaaa568"
PROM_PASSWORD = "bbbbbbbb568"
RUN_ID = "c4-run"
MIB = 1024 * 1024
GROUP_PATH = "/apis/argoproj.io/v1alpha1"

FRAME = Window(NOW - timedelta(hours=6), NOW)
FORBIDDEN_NOTE = (
    "This cluster's read-only identity has no permission to read ArgoCD Applications."
)
CRD_MISSING_NOTE = (
    "ARGOCD_CRD_MISSING: this cluster has no ArgoCD Application CRD "
    "(applications.argoproj.io)."
)
_FIRST_NOTE = re.compile(
    r"ArgoCD Application state as stored now \(queried at (\S+)\); history "
    r"entries outside the authorized window are listed too\."
)
_RFC3339_Z = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")


def _json(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


def _z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ======================================================================================
# 1. Synthetic Applications
# ======================================================================================


def source(name: str, **extra: Any) -> dict[str, Any]:
    body = {
        "repoURL": f"https://git.example.com/org/{name}.git",
        "path": f"apps/{name}",
        "targetRevision": "main",
    }
    body.update(extra)
    return body


def history_entry(index: int, deployed: datetime, **extra: Any) -> dict[str, Any]:
    entry = {
        "id": index,
        "revision": f"{index:040x}",
        "deployedAt": _z(deployed),
        "deployStartedAt": _z(deployed - timedelta(seconds=30)),
        "initiatedBy": {"automated": True} if index % 2 else {"username": "admin"},
        "source": source("hist"),
    }
    entry.update(extra)
    return entry


def resource_entry(
    name: str,
    *,
    group: str = "apps",
    kind: str = "Deployment",
    namespace: str = "c4-demo",
    status: str = "Synced",
    health: str | None = "Healthy",
    **extra: Any,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "group": group,
        "version": "v1",
        "kind": kind,
        "namespace": namespace,
        "name": name,
        "status": status,
    }
    if health is not None:
        entry["health"] = {"status": health}
    entry.update(extra)
    return entry


def app(
    name: str,
    namespace: str = "argocd",
    *,
    sync: str = "Synced",
    health: str = "Healthy",
    history: list[dict[str, Any]] | None = None,
    resources: list[dict[str, Any]] | None = None,
    spec_source: dict[str, Any] | None = None,
    spec_sources: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "project": "default",
        "destination": {
            "server": "https://kubernetes.default.svc",
            "namespace": "c4-demo",
        },
    }
    if spec_sources is not None:
        spec["sources"] = spec_sources
    else:
        spec["source"] = spec_source or source(name)
    status: dict[str, Any] = {
        "sync": {
            "status": sync,
            "revision": "c" * 40,
            "comparedTo": {"destination": dict(spec["destination"])},
        },
        "health": {"status": health},
        "reconciledAt": "2026-11-20T09:00:00Z",
        "summary": {"images": ["ghcr.io/example/app:1.0"]},
    }
    if history is not None:
        status["history"] = history
    if resources is not None:
        status["resources"] = resources
    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "uid": f"uid-{name}",
            "resourceVersion": "568",
            "generation": 1,
            "creationTimestamp": "2026-10-01T00:00:00Z",
        },
        "spec": spec,
        "status": status,
    }


def status_body(
    code: int, reason: str, message: str, details: Mapping[str, Any] | None = None
) -> bytes:
    body: dict[str, Any] = {
        "kind": "Status",
        "apiVersion": "v1",
        "metadata": {},
        "status": "Failure",
        "message": message,
        "reason": reason,
        "code": code,
    }
    if details is not None:
        body["details"] = dict(details)
    return _json(body)


# -- the fake API server ---------------------------------------------------------------

_APP_PATH = re.compile(
    r"/apis/argoproj\.io/v1alpha1/(?:namespaces/([^/]+)/)?applications(?:/([^/]+))?"
)


class FakeApiServer:
    """A loopback HTTPS kube-apiserver serving ``applications.argoproj.io``
    the way it does with the CRD installed (``limit`` and ``continue``,
    named NotFound with ``details.name``), recording every request."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.apps: list[dict[str, Any]] = []
        #: ``"status"``: 404 Status without details; ``"text"``: plain 404.
        self.crd_missing: str | None = None
        #: request ordinal -> (status, body) / raw 200 body.
        self.fail_at: dict[int, tuple[int, bytes]] = {}
        self.raw_at: dict[int, bytes] = {}
        self.delay = 0.0
        self._server: ThreadingHTTPServer | None = None
        self.url = ""
        self.ca: Path | None = None
        self._lock = threading.Lock()

    def _list(self, namespace: str | None, query: Mapping[str, str]) -> bytes:
        # Insertion order, not sorted: the tools sort.
        items = [
            a
            for a in self.apps
            if namespace is None or a["metadata"]["namespace"] == namespace
        ]
        limit = int(query.get("limit") or 0) or max(len(items), 1)
        token = query.get("continue") or ""
        offset = int(token[2:-2]) if token else 0
        page = items[offset : offset + limit]
        following = offset + limit
        metadata: dict[str, Any] = {"resourceVersion": "568"}
        if following < len(items):
            metadata["continue"] = f"c+{following}/="
            metadata["remainingItemCount"] = len(items) - following
        return _json(
            {
                "kind": "ApplicationList",
                "apiVersion": "argoproj.io/v1alpha1",
                "metadata": metadata,
                "items": page,
            }
        )

    def _crd_missing(self) -> tuple[int, bytes]:
        if self.crd_missing == "text":
            return 404, b"404 page not found\n"
        return 404, status_body(
            404, "NotFound", "the server could not find the requested resource", {}
        )

    def answer(self, index: int, record: dict[str, Any]) -> tuple[int, bytes]:
        if self.delay:
            time.sleep(self.delay)
        if record["method"] != "GET":
            return 405, status_body(405, "MethodNotAllowed", "not allowed")
        if index in self.fail_at:
            return self.fail_at[index]
        if index in self.raw_at:
            return 200, self.raw_at[index]
        query = {k: v[-1] for k, v in record["query"].items()}
        match = _APP_PATH.fullmatch(record["path"])
        if match:
            if self.crd_missing:
                return self._crd_missing()
            namespace, name = match[1], match[2]
            if name is None:
                return 200, self._list(namespace, query)
            if namespace is None:
                return self._crd_missing()
            for item in self.apps:
                meta = item["metadata"]
                if (meta["namespace"], meta["name"]) == (namespace, name):
                    return 200, _json(item)
            return 404, status_body(
                404,
                "NotFound",
                f'applications.argoproj.io "{name}" not found',
                {"name": name, "group": "argoproj.io", "kind": "applications"},
            )
        if record["path"].startswith(("/api/v1/", "/apis/")):
            return 200, _json(
                {"kind": "List", "apiVersion": "v1", "metadata": {}, "items": []}
            )
        return 404, status_body(404, "NotFound", "the server could not find it")

    def start(self, tls: tuple[Path, Path]) -> None:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                return None

            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                parts = urlsplit(self.path)
                record = {
                    "method": self.command,
                    "raw_path": parts.path,
                    "path": unquote(parts.path),
                    "raw_query": parts.query,
                    "query": parse_qs(parts.query, keep_blank_values=True),
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": body,
                }
                with fake._lock:
                    index = len(fake.requests)
                    fake.requests.append(record)
                status, payload = fake.answer(index, record)
                try:
                    self.send_response(status)
                    content_type = (
                        "text/plain"
                        if payload.startswith(b"404 page")
                        else "application/json"
                    )
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except OSError:
                    pass

            do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_PATCH = _serve

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        server.block_on_close = False
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(tls[0]), str(tls[1]))
        server.socket = context.wrap_socket(server.socket, server_side=True)
        self._server = server
        threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        ).start()
        self.url = f"https://127.0.0.1:{server.server_address[1]}"
        self.ca = tls[0]

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


@pytest.fixture(scope="module")
def tls_pair(tmp_path_factory) -> tuple[Path, Path]:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is needed to mint the synthetic API server CA")
    folder = tmp_path_factory.mktemp("c4-tls")
    cert, key = folder / "apiserver.pem", folder / "apiserver-key.pem"
    subprocess.run(
        [
            openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cert), "-days", "2",
            "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    return cert, key


@pytest.fixture
def api(tls_pair) -> Iterator[FakeApiServer]:
    fake = FakeApiServer()
    fake.start(tls_pair)
    yield fake
    fake.stop()


OTHER_SWITCHES = (
    SWITCH,
    HELM_SWITCH,
    "OPSPILOT_ES_URL",
    "OPSPILOT_LOKI_URL",
    "OPSPILOT_VICTORIALOGS_URL",
    "OPSPILOT_K8S_LIVE_METRICS",
    "OPSPILOT_KNOWLEDGE_TOOL",
    "OPSPILOT_TODO_WRITE",
    "OPSPILOT_SQL_URL",
    "OPSPILOT_KAFKA_BOOTSTRAP_SERVERS",
    "OPSPILOT_TRACE",
)


@pytest.fixture(autouse=True)
def _clean_environment(tmp_path, monkeypatch):
    for name in OTHER_SWITCHES:
        monkeypatch.delenv(name, raising=False)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    placeholder = tmp_path / "synthetic-ca-placeholder.pem"
    placeholder.write_text("synthetic ca placeholder\n")
    monkeypatch.setenv("C4_TEST_PLACEHOLDER_CA", str(placeholder))


def _env(api: FakeApiServer | None = None, **overrides: str | None) -> dict[str, str]:
    """A worker environment with the switch on, pointed at ``api``."""
    values: dict[str, str | None] = {
        PROFILE_ENV: EXT_PROFILE,
        SWITCH: "1",
        "OPSPILOT_K8S_API_URL": api.url
        if api is not None
        else "https://127.0.0.1:16443",
        "OPSPILOT_K8S_TOKEN": K8S_TOKEN,
        "OPSPILOT_K8S_CA_FILE": str(api.ca)
        if api is not None and api.ca
        else os.environ["C4_TEST_PLACEHOLDER_CA"],
        "OPSPILOT_K8S_NAMESPACE": "otel-demo",
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://127.0.0.1:19090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://127.0.0.1:16686/jaeger/ui",
        "OPSPILOT_OTEL_PROMETHEUS_USERNAME": "investigator",
        "OPSPILOT_OTEL_PROMETHEUS_PASSWORD": PROM_PASSWORD,
    }
    values.update(overrides)
    return {k: v for k, v in values.items() if v is not None}


def _require_tools() -> None:
    if shutil.which("jq") is None or shutil.which("python3") is None:
        pytest.skip("the extension worker needs jq and python3 on PATH")


def _input(profile: ToolProfile, lease, clock: FakeClock, deadline: datetime):
    return profile.face(clock).input_for(
        run_id=str(lease.run_id),
        question="What is the sync status of c4-healthy?",
        target_id=TARGET_ID,
        deadline=deadline,
        model_requests=12,
    )


class Worker:
    def __init__(self, env: Mapping[str, str], *, deadline_seconds: float = 600):
        _require_tools()
        clock = FakeClock(start=NOW)
        profile = select_profile(env)
        assert isinstance(profile, ToolProfile)
        if env.get(SWITCH) == "1":
            # Positive control: refusal tests are vacuous without the tools.
            names = {s["function"]["name"] for s in profile.face(clock).tool_schemas}
            assert set(ARGO_TOOLS) <= names
        lease = _lease()
        deadline = NOW + timedelta(seconds=deadline_seconds)
        self.store = FakeStore(lease, deadline=deadline)
        self.sink = RecordingSink()
        factory = profile.executor_factory(self.store, self.sink, clock)
        self.executor = factory(lease, _input(profile, lease, clock, deadline))
        self._index = 0

    def call(self, tool: str, params: Mapping[str, object] | None = None):
        request = ToolRequest(
            step_id="c4-step",
            tool_index=self._index,
            tool_name=tool,
            target_ref=TARGET_ID,
            params=dict(params or {}),
            window=FRAME.as_json(),
        )
        self._index += 1
        return self.executor.execute(request)


def _run(
    api: FakeApiServer, tool: str, params: Mapping[str, object] | None = None, **env
):
    return Worker(_env(api, **env)).call(tool, params)


def _view(outcome) -> Mapping[str, Any]:
    assert outcome.model_view is not None
    return outcome.model_view


def _text(outcome) -> str:
    return json.dumps(_view(outcome), ensure_ascii=False, sort_keys=True)


def _rows(outcome) -> list[Mapping[str, Any]]:
    assert outcome.status == "ok", _view(outcome)
    content = _view(outcome)["content"]
    assert isinstance(content, list), _view(outcome)
    return content


def _row(outcome) -> Mapping[str, Any]:
    rows = _rows(outcome)
    assert len(rows) == 1, rows
    return rows[0]


def _notes(outcome) -> list[str]:
    notes = _view(outcome).get("notes") or []
    assert isinstance(notes, list), _view(outcome)
    return [str(n) for n in notes]


def _failed(outcome, reason: str) -> None:
    assert (outcome.status, outcome.reason) == ("error", reason), _view(outcome)
    assert _view(outcome).get("content") is None, _view(outcome)


def _named(name: str, namespace: str | None = None) -> dict[str, str]:
    params = {"app_name": name}
    if namespace is not None:
        params["namespace"] = namespace
    return params


def _basic_apps() -> list[dict[str, Any]]:
    return [
        app(
            "c4-healthy",
            history=[
                history_entry(1, NOW - timedelta(days=2)),
                history_entry(2, NOW - timedelta(hours=1)),
            ],
            resources=[resource_entry("web"), resource_entry("api")],
        ),  # fmt: skip
        app("c4-elsewhere", "c4-other", history=[], resources=[]),
    ]


# ======================================================================================
# 2. Requests the tools send (only ``applications``, fixed shapes, GET)
# ======================================================================================


def _assert_plain_get(record: Mapping[str, Any]) -> None:
    assert record["method"] == "GET", record
    assert record["body"] == b""
    assert record["headers"].get("authorization") == f"Bearer {K8S_TOKEN}"
    assert record["headers"].get("accept") == "application/json"
    assert PROM_PASSWORD not in json.dumps(record["headers"])


def _assert_only_application_paths(api: FakeApiServer) -> None:
    for record in api.requests:
        assert _APP_PATH.fullmatch(record["path"]), record["path"]
        assert record["raw_path"] == record["path"], record["raw_path"]
        lowered = record["raw_path"].lower() + "?" + record["raw_query"].lower()
        for other in ("applicationsets", "appprojects", "secrets", "watch"):
            assert other not in lowered, record


@pytest.mark.parametrize("tool", NAMED_TOOLS)
@pytest.mark.parametrize(
    ("namespace", "path_namespace"),
    [(None, "argocd"), ("argocd", "argocd"), ("c4-other", "c4-other")],
)
def test_named_tools_send_one_get_to_the_named_application_path(
    api, tool, namespace, path_namespace
):
    api.apps = [
        app("c4-healthy", path_namespace, history=[history_entry(1, NOW)],
            resources=[resource_entry("web")]),
    ]  # fmt: skip
    outcome = _run(api, tool, _named("c4-healthy", namespace))
    assert outcome.status == "ok", _view(outcome)
    assert len(api.requests) == 1
    record = api.requests[0]
    assert record["raw_path"] == (
        f"{GROUP_PATH}/namespaces/{path_namespace}/applications/c4-healthy"
    )
    assert record["raw_query"] == ""
    _assert_plain_get(record)


@pytest.mark.parametrize(
    ("params", "path"),
    [
        ({}, f"{GROUP_PATH}/applications"),
        ({"namespace": "argocd"}, f"{GROUP_PATH}/namespaces/argocd/applications"),
    ],
)
def test_list_pages_with_limit_100_and_the_servers_continue_token(api, params, path):
    api.apps = [_small_app(f"a-{n:03d}", "argocd") for n in range(210)]
    outcome = _run(api, LIST, params)
    assert len(_rows(outcome)) == 210
    assert [r["raw_path"] for r in api.requests] == [path] * 3
    assert [r["query"] for r in api.requests] == [
        {"limit": ["100"]},
        {"limit": ["100"], "continue": ["c+100/="]},
        {"limit": ["100"], "continue": ["c+200/="]},
    ]
    for record in api.requests:
        _assert_plain_get(record)


def test_list_without_namespace_spans_the_cluster_and_with_one_only_that_one(api):
    api.apps = [
        app("c4-zeta", "argocd"),
        app("c4-elsewhere", "c4-other"),
        app("c4-alpha", "argocd"),
    ]
    rows = _rows(_run(api, LIST))
    assert [(r["namespace"], r["name"]) for r in rows] == [
        ("argocd", "c4-alpha"),
        ("argocd", "c4-zeta"),
        ("c4-other", "c4-elsewhere"),
    ]
    rows = _rows(_run(api, LIST, {"namespace": "c4-other"}))
    assert [r["name"] for r in rows] == ["c4-elsewhere"]


INVALID_NAMESPACES = ["Argocd", "-argocd", "argocd-", "argo_cd", "n" * 64,
                      "argocd/../kube-system", "argo cd", "argocd%2Fx",
                      "", 7]  # fmt: skip
INVALID_APP_NAMES = [
    "C4-app",
    "c4_app",
    "-c4",
    "c4-",
    "a..b",
    ".c4",
    "c4.",
    "a" * 254,
    "c4/../../applicationsets",
    "c4/status",
    "c4?watch=true",
    "c4%2Fx",
    "c4 app",
    "c4!",
    "",
    7,
    None,
]


@pytest.mark.parametrize("namespace", INVALID_NAMESPACES)
@pytest.mark.parametrize("tool", ARGO_TOOLS)
def test_a_namespace_that_is_not_a_dns_label_is_refused_before_any_request(
    api, tool, namespace
):
    params: dict[str, object] = {"namespace": namespace}
    if tool != LIST:
        params["app_name"] = "c4-healthy"
    outcome = _run(api, tool, params)
    assert outcome.status in ("error", "denied"), _view(outcome)
    if outcome.status == "error":
        assert outcome.reason == "INVALID_PARAMS", _view(outcome)
        assert _view(outcome).get("source_contact") == "none"
        if isinstance(namespace, str):
            assert "INVALID_NAMESPACE" in _text(outcome), _view(outcome)
    assert api.requests == []


def test_a_namespace_named_like_another_resource_stays_a_namespace(api):
    _run(api, LIST, {"namespace": "applicationsets"})
    _run(api, GET, _named("c4", "appprojects"))
    assert [r["raw_path"] for r in api.requests] == [
        f"{GROUP_PATH}/namespaces/applicationsets/applications",
        f"{GROUP_PATH}/namespaces/appprojects/applications/c4",
    ]


@pytest.mark.parametrize("app_name", INVALID_APP_NAMES)
@pytest.mark.parametrize("tool", NAMED_TOOLS)
def test_an_app_name_outside_dns_1123_subdomain_is_refused_before_any_request(
    api, tool, app_name
):
    outcome = _run(api, tool, {"app_name": app_name, "namespace": "argocd"})
    assert outcome.status in ("error", "denied"), _view(outcome)
    if outcome.status == "error":
        assert outcome.reason == "INVALID_PARAMS", _view(outcome)
        assert _view(outcome).get("source_contact") == "none"
        if isinstance(app_name, str) and app_name:
            assert "INVALID_APP_NAME" in _text(outcome), _view(outcome)
    assert api.requests == []


#: Every dot-separated part kept within 63 characters (see the reply's
#: ambiguity note on per-label length); 253 characters in all.
_LONG_NAME = ".".join(["a" * 63] * 3 + ["b" * 61])


@pytest.mark.parametrize("app_name", ["a", "0c4", "c4.app-1", "a1.b2.c3", _LONG_NAME])
def test_app_names_within_the_rule_are_sent_in_the_path(api, app_name):
    assert len(_LONG_NAME) == 253
    api.apps = [app(app_name)]
    row = _row(_run(api, GET, _named(app_name)))
    assert row["name"] == app_name
    assert api.requests[0]["raw_path"].endswith(f"/applications/{app_name}")


@pytest.mark.parametrize(
    ("tool", "params"),
    [
        (GET, {}),
        (GET, {"namespace": "argocd"}),
        (HISTORY, {}),
        (RESOURCES, {"namespace": "argocd"}),
        (GET, {"app_name": "c4", "kind": "applicationsets"}),
        (GET, {"app_name": "c4", "resource": "appprojects"}),
        (GET, {"app_name": "c4", "path": "/apis/argoproj.io/v1alpha1/appprojects"}),
        (HISTORY, {"app_name": "c4", "max": 3}),
        (RESOURCES, {"app_name": "c4", "output": "manifests"}),
        (LIST, {"label_selector": "team=x"}),
        (LIST, {"project": "default"}),
        (LIST, {"app_name": "c4"}),
        (LIST, {"start": "2026-11-20T00:00:00Z"}),
    ],
)
def test_missing_or_unknown_parameters_never_reach_the_cluster(api, tool, params):
    outcome = _run(api, tool, params)
    assert outcome.status in ("error", "denied"), _view(outcome)
    assert api.requests == []


def _schemas() -> dict[str, Mapping[str, Any]]:
    return {
        s["function"]["name"]: s["function"]
        for s in _face({PROFILE_ENV: EXT_PROFILE, SWITCH: "1"}).tool_schemas
    }


def test_schemas_expose_only_the_documented_parameters():
    schemas = _schemas()
    listing = schemas[LIST]["parameters"]
    assert set(listing.get("properties") or {}) == {"namespace"}
    assert not listing.get("required")
    for name in NAMED_TOOLS:
        params = schemas[name]["parameters"]
        assert set(params.get("properties") or {}) == {"app_name", "namespace"}
        assert set(params.get("required") or ()) == {"app_name"}
    # C4-1: the seven upstream tools left out are not offered.
    assert not {
        "argocd_app_diff",
        "argocd_app_manifests",
        "argocd_app_manifest_source_revision",
        "argocd_repo_list",
        "argocd_cluster_list",
        "argocd_proj_list",
        "argocd_proj_get",
    } & set(schemas)


def test_the_resources_description_says_out_of_sync_has_no_field_diff():
    text = json.dumps(_schemas()[RESOURCES], ensure_ascii=False).lower()
    assert "outofsync" in text
    assert "kubernetes" in text  # look at the live object with the k8s tools


# ======================================================================================
# 3. Rows: projection, order, time
# ======================================================================================


def test_list_rows_are_the_nine_summary_fields(api):
    item = app("c4-healthy")
    item["status"]["operationState"] = {"phase": "Succeeded"}
    api.apps = [item]
    row = _row(_run(api, LIST))
    assert set(row) == LIST_ROW_FIELDS
    assert row["name"] == "c4-healthy"
    assert row["namespace"] == "argocd"
    assert row["project"] == "default"
    assert row["sync_status"] == "Synced"
    assert row["health_status"] == "Healthy"
    assert row["operation_phase"] == "Succeeded"
    assert row["reconciled_at"] == "2026-11-20T09:00:00Z"
    assert [set(s) for s in row["sources"]] == [SOURCE_FIELDS]
    assert row["destination"]["server"] == "https://kubernetes.default.svc"
    assert row["destination"]["namespace"] == "c4-demo"
    assert set(row["destination"]) <= {"server", "name", "namespace"}


def _degraded() -> dict[str, Any]:
    item = app(
        "c4-degraded",
        sync="OutOfSync",
        health="Degraded",
        resources=[resource_entry("web", status="OutOfSync", health="Degraded")],
    )
    item["spec"]["syncPolicy"] = {"automated": {"prune": True, "selfHeal": False}}
    item["status"]["health"]["message"] = (
        "Deployment web exceeded its progress deadline"
    )
    item["status"]["conditions"] = [
        {
            "type": "SyncError",
            "message": "one or more objects failed to apply",
            "lastTransitionTime": "2026-11-19T08:00:00Z",
        }
    ]
    item["status"]["sync"]["revisions"] = ["d" * 40, "e" * 40]
    item["status"]["operationState"] = {
        "phase": "Failed",
        "message": "one or more synchronization tasks completed unsuccessfully",
        "startedAt": "2026-11-19T07:59:00Z",
        "finishedAt": "2026-11-19T08:00:00Z",
        "retryCount": 2,
        "syncResult": {
            "revision": "c" * 40,
            "resources": [
                {
                    "group": "apps",
                    "version": "v1",
                    "kind": "Deployment",
                    "namespace": "c4-demo",
                    "name": "web",
                    "status": "SyncFailed",
                    "message": "admission webhook denied the request",
                    "hookPhase": "Failed",
                    "syncPhase": "Sync",
                }
            ],
        },
    }
    item["status"]["summary"]["images"] = ["ghcr.io/example/web:2.0", "ghcr.io/x:1"]
    return item


def test_get_is_one_whitelisted_row_with_status_operation_and_conditions(api):
    api.apps = [_degraded()]
    row = _row(_run(api, GET, _named("c4-degraded")))
    assert set(row) <= GET_ROW_FIELDS
    assert GET_ROW_CORE <= set(row)
    assert row["name"] == "c4-degraded" and row["namespace"] == "argocd"
    assert row["project"] == "default"
    assert row["automated"] == {"prune": True, "self_heal": False}
    assert row["sync"]["status"] == "OutOfSync"
    assert row["sync"].get("revision") == "c" * 40
    assert row["sync"].get("revisions") == ["d" * 40, "e" * 40]
    assert row["health"] == {
        "status": "Degraded",
        "message": "Deployment web exceeded its progress deadline",
    }
    assert row["conditions"] == [
        {
            "type": "SyncError",
            "message": "one or more objects failed to apply",
            "last_transition_time": "2026-11-19T08:00:00Z",
        }
    ]
    op = row["operation_state"]
    assert set(op) <= OP_STATE_FIELDS
    assert op["phase"] == "Failed"
    assert op["message"] == "one or more synchronization tasks completed unsuccessfully"
    assert op["started_at"] == "2026-11-19T07:59:00Z"
    assert op["finished_at"] == "2026-11-19T08:00:00Z"
    assert op["retry_count"] == 2
    assert op["sync_result"].get("revision") == "c" * 40
    [result] = op["sync_result"]["resources"]
    assert set(result) <= SYNC_RESULT_RESOURCE_FIELDS
    assert result == {
        "group": "apps",
        "kind": "Deployment",
        "namespace": "c4-demo",
        "name": "web",
        "status": "SyncFailed",
        "message": "admission webhook denied the request",
        "hook_phase": "Failed",
        "sync_phase": "Sync",
    }
    assert row["reconciled_at"] == "2026-11-20T09:00:00Z"
    assert row["images"] == ["ghcr.io/example/web:2.0", "ghcr.io/x:1"]


def test_automated_is_null_without_an_automated_policy(api):
    item = app("c4-manual")
    item["spec"]["syncPolicy"] = {"syncOptions": ["CreateNamespace=true"]}
    api.apps = [item]
    assert _row(_run(api, GET, _named("c4-manual")))["automated"] is None


def test_a_single_source_and_multiple_sources_are_both_a_list_of_six_fields(api):
    multi = [
        source("chart", chart="web", repoURL="https://charts.example.com",
               targetRevision="1.2.3", path=None),
        source("values", ref="values"),
        source("extra", name="extra-src"),
    ]  # fmt: skip
    for entry in multi:
        if entry.get("path") is None:
            entry.pop("path")
    api.apps = [app("c4-single"), app("c4-multi", spec_sources=multi)]
    single = _row(_run(api, GET, _named("c4-single")))["sources"]
    assert single == [
        {
            "repo_url": "https://git.example.com/org/c4-single.git",
            "path": "apps/c4-single",
            "chart": None,
            "target_revision": "main",
            "ref": None,
            "name": None,
        }
    ]
    sources = _row(_run(api, GET, _named("c4-multi")))["sources"]
    assert [set(s) for s in sources] == [SOURCE_FIELDS] * 3
    assert [s["repo_url"] for s in sources] == [
        "https://charts.example.com",
        "https://git.example.com/org/values.git",
        "https://git.example.com/org/extra.git",
    ]
    assert sources[0]["chart"] == "web"
    assert sources[0]["target_revision"] == "1.2.3"
    assert sources[1]["ref"] == "values"
    assert sources[2]["name"] == "extra-src"


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("https://git.example.com/org/repo.git", "https://git.example.com/org/repo.git"),
        ("https://user:tok@git.example.com/org/repo.git",
         "https://git.example.com/org/repo.git"),
        ("https://user@git.example.com/org/repo", "https://git.example.com/org/repo"),
        ("https://git.example.com/org/repo?private_token=x&a=b",
         "https://git.example.com/org/repo"),
        ("https://git.example.com/org/repo#frag", "https://git.example.com/org/repo"),
        ("https://u:p@git.example.com:8443/org/repo.git?x=1#y",
         "https://git.example.com:8443/org/repo.git"),
    ],
)  # fmt: skip
def test_repo_url_loses_userinfo_query_and_fragment(api, raw, clean):
    api.apps = [app("c4-repo", spec_source=source("c4-repo", repoURL=raw))]
    for tool in (LIST, GET):
        params = {} if tool == LIST else _named("c4-repo")
        [entry] = _rows(_run(api, tool, params))[0]["sources"]
        assert entry["repo_url"] == clean, tool


def test_an_unparsable_repo_url_is_null(api):
    api.apps = [
        app("c4-repo", spec_source=source("c4-repo", repoURL="https://[u:p@bad"))
    ]
    outcome = _run(api, GET, _named("c4-repo"))
    [entry] = _row(outcome)["sources"]
    assert entry["repo_url"] is None
    assert "u:p" not in _text(outcome)


def test_history_is_every_entry_in_ascending_id_order(api):
    entries = [history_entry(i, NOW - timedelta(days=13 - i)) for i in range(1, 13)]
    random.Random(568).shuffle(entries)
    entries[0]["revisions"] = ["f" * 40, "0" * 40]
    entries[0].pop("revision")
    api.apps = [app("c4-history", history=entries)]
    rows = _rows(_run(api, HISTORY, _named("c4-history")))
    assert [r["id"] for r in rows] == list(range(1, 13))
    for row in rows:
        assert set(row) <= HISTORY_ROW_FIELDS
        assert {"id", "deployed_at"} <= set(row)
        assert _RFC3339_Z.fullmatch(row["deployed_at"]), row
        assert [set(s) for s in row["sources"]] == [SOURCE_FIELDS]
        assert set(row["initiated_by"]) <= {"username", "automated"}
    by_id = {r["id"]: r for r in rows}
    original = {e["id"]: e for e in entries}
    for index, row in by_id.items():
        source_entry = original[index]
        assert row["deployed_at"] == source_entry["deployedAt"]
        assert row["deploy_started_at"] == source_entry["deployStartedAt"]
        if "revision" in source_entry:
            assert row["revision"] == source_entry["revision"]
        else:
            assert row["revisions"] == source_entry["revisions"]
    assert by_id[2]["initiated_by"].get("username") == "admin"
    assert by_id[3]["initiated_by"].get("automated") is True


def test_history_sources_are_cleaned_to_six_fields(api):
    entry = history_entry(1, NOW)
    entry["source"] = source(
        "hist", repoURL="https://u:p@git.example.com/org/h.git?t=1"
    )
    second = history_entry(2, NOW)
    second.pop("source")
    second["sources"] = [source("one"), source("two", ref="vals")]
    api.apps = [app("c4-h", history=[entry, second])]
    rows = _rows(_run(api, HISTORY, _named("c4-h")))
    assert rows[0]["sources"] == [
        {
            "repo_url": "https://git.example.com/org/h.git",
            "path": "apps/hist",
            "chart": None,
            "target_revision": "main",
            "ref": None,
            "name": None,
        }
    ]
    assert [s["repo_url"] for s in rows[1]["sources"]] == [
        "https://git.example.com/org/one.git",
        "https://git.example.com/org/two.git",
    ]


def test_resources_rows_are_the_ten_fields_sorted_by_group_kind_namespace_name(api):
    entries = [
        resource_entry("web", status="OutOfSync", health="Degraded",
                       hook=False, requiresPruning=False, syncWave=1),
        resource_entry("cfg", group="", kind="ConfigMap", health=None),
        resource_entry("api"),
        resource_entry("api", namespace="a-first"),
        resource_entry("svc", group="", kind="Service"),
        resource_entry("db", kind="StatefulSet"),
        resource_entry("job", group="batch", kind="Job", hook=True,
                       requiresPruning=True, syncWave=-1),
    ]  # fmt: skip
    entries[0]["health"]["message"] = "progress deadline exceeded"
    api.apps = [app("c4-res", resources=entries)]
    rows = _rows(_run(api, RESOURCES, _named("c4-res")))
    assert all(set(r) == RESOURCE_ROW_FIELDS for r in rows), rows
    assert [(r["group"], r["kind"], r["namespace"], r["name"]) for r in rows] == [
        ("", "ConfigMap", "c4-demo", "cfg"),
        ("", "Service", "c4-demo", "svc"),
        ("apps", "Deployment", "a-first", "api"),
        ("apps", "Deployment", "c4-demo", "api"),
        ("apps", "Deployment", "c4-demo", "web"),
        ("apps", "StatefulSet", "c4-demo", "db"),
        ("batch", "Job", "c4-demo", "job"),
    ]
    web = rows[4]
    assert web["version"] == "v1"
    assert web["status"] == "OutOfSync"
    assert web["health"] == {
        "status": "Degraded",
        "message": "progress deadline exceeded",
    }
    assert web["requires_pruning"] is False
    assert web["sync_wave"] == 1
    job = rows[6]
    assert job["hook"] is True and job["requires_pruning"] is True
    assert job["sync_wave"] == -1


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-11-20T09:00:00Z", "2026-11-20T09:00:00Z"),
        ("2026-11-20T11:00:00.987654+02:00", "2026-11-20T09:00:00Z"),
        ("2026-11-20T04:00:00-05:00", "2026-11-20T09:00:00Z"),
        ("not-a-time", None),
        ("", None),
    ],
)
def test_times_are_utc_rfc3339_to_the_second_or_null(api, value, expected):
    item = app("c4-time", history=[history_entry(1, NOW)])
    item["status"]["reconciledAt"] = value
    item["status"]["history"][0]["deployedAt"] = value
    item["status"]["conditions"] = [
        {"type": "ComparisonError", "message": "m", "lastTransitionTime": value}
    ]
    api.apps = [item]
    row = _row(_run(api, GET, _named("c4-time")))
    assert row["reconciled_at"] == expected
    assert row["conditions"][0]["last_transition_time"] == expected
    assert _rows(_run(api, HISTORY, _named("c4-time")))[0]["deployed_at"] == expected
    assert _row(_run(api, LIST))["reconciled_at"] == expected


def test_a_missing_time_is_null(api):
    item = app("c4-time")
    item["status"].pop("reconciledAt")
    api.apps = [item]
    assert _row(_run(api, GET, _named("c4-time")))["reconciled_at"] is None


def test_messages_are_cut_to_2000_characters_and_redacted(api):
    from opspilot.redaction import redact_credentials

    leaking = "helm template failed: password=abcdabcd568 rejected"
    item = app("c4-msg")
    item["status"]["conditions"] = [
        {"type": "ComparisonError", "message": "x" * 5000},
        {"type": "SyncError", "message": leaking},
    ]
    item["status"]["health"]["message"] = "y" * 5000
    item["status"]["operationState"] = {"phase": "Failed", "message": "z" * 5000}
    api.apps = [item]
    outcome = _run(api, GET, _named("c4-msg"))
    row = _row(outcome)
    assert row["conditions"][0]["message"] == "x" * 2000
    assert row["health"]["message"] == "y" * 2000
    assert row["operation_state"]["message"] == "z" * 2000
    assert row["conditions"][1]["message"] == redact_credentials(leaking)
    assert "abcdabcd568" not in _text(outcome)


# -- time: current-state class (B4C4-U1=a, C4-7) ----------------------------------------


@pytest.mark.parametrize("tool", ARGO_TOOLS)
def test_source_times_are_empty_and_history_is_not_cut_to_the_frame(api, tool):
    entries = [
        history_entry(1, NOW - timedelta(days=40)),  # long before the frame
        history_entry(2, NOW - timedelta(hours=7)),  # just before it
        history_entry(3, NOW - timedelta(hours=1)),  # inside
        history_entry(4, NOW + timedelta(hours=2)),  # after it
    ]
    api.apps = [app("c4-history", history=entries, resources=[resource_entry("w")])]
    params = {} if tool == LIST else _named("c4-history")
    outcome = Worker(_env(api)).call(tool, params)
    view = _view(outcome)
    assert outcome.status == "ok", view
    assert view["source_start_at"] is None
    assert view["source_end_at"] is None
    assert view["data_as_of"] is None
    record = outcome.evidence
    assert record.source_start_at is None and record.source_end_at is None
    assert record.data_as_of is None
    if tool == HISTORY:
        assert [r["id"] for r in _rows(outcome)] == [1, 2, 3, 4]
    notes = _notes(outcome)
    assert notes, view
    match = _FIRST_NOTE.fullmatch(notes[0])
    assert match, notes
    queried = datetime.fromisoformat(match[1].replace("Z", "+00:00"))
    assert queried.tzinfo is not None
    cannot = str(view.get("cannot_prove") or "").lower()
    assert "reconcil" in cannot, cannot  # the controller's last reconcile
    assert "outofsync" in cannot, cannot  # no field-level diff here
    assert "window" in cannot, cannot  # outside entries listed; no proof of absence


# ======================================================================================
# 4. Nothing outside the whitelist ever leaves the tool
# ======================================================================================

CANARIES = {
    # the sixteen of the interface's c4-canary
    "helm-values": "CANARY-C4-VALUES",
    "helm-values-object": "CANARY-C4-VALUESOBJ",
    "helm-parameter": "CANARY-C4-PARAM",
    "helm-file-parameter": "CANARY-C4-FILEPARAM",
    "plugin-env": "CANARY-C4-PLUGINENV",
    "plugin-parameter": "CANARY-C4-PLUGINPARAM",
    "kustomize-patch": "CANARY-C4-KPATCH",
    "jsonnet-ext-var": "CANARY-C4-JSONNET",
    "annotation": "CANARY-C4-ANNOTATION",
    "label": "CANARY-C4-LABEL",
    "operation": "CANARY-C4-OPERATION",
    "history-source": "CANARY-C4-HISTSRC",
    "operation-state-operation": "CANARY-C4-OPSTATE",
    "compared-to-source": "CANARY-C4-COMPARED",
    "repo-userinfo": "CANARY-C4-REPOUSER",
    "repo-query": "CANARY-C4-REPOQUERY",
    # further places the whitelist also excludes
    "helm-value-files": "CANARY-C4-VALUEFILES",
    "helm-release-name": "CANARY-C4-RELEASENAME",
    "jsonnet-tla": "CANARY-C4-TLA",
    "kustomize-common-label": "CANARY-C4-KLABEL",
    "directory-include": "CANARY-C4-DIRINCLUDE",
    "repo-password": "CANARY-C4-REPOPASS",
    "repo-fragment": "CANARY-C4-REPOFRAG",
    "last-applied": "CANARY-C4-LASTAPPLIED",
    "managed-fields": "CANARY-C4-MANAGED",
    "spec-info": "CANARY-C4-INFO",
    "ignore-differences": "CANARY-C4-IGNOREDIFF",
    "destination-extra": "CANARY-C4-DESTEXTRA",
    "spec-extra": "CANARY-C4-SPECEXTRA",
    "status-extra": "CANARY-C4-STATUSEXTRA",
    "top-extra": "CANARY-C4-TOPEXTRA",
    "operation-sources": "CANARY-C4-OPSOURCES",
    "history-sources": "CANARY-C4-HISTSOURCES",
    "history-repo-userinfo": "CANARY-C4-HISTREPOUSER",
    "history-extra": "CANARY-C4-HISTEXTRA",
    "sync-result-source": "CANARY-C4-SYNCRESULTSRC",
    "compared-to-sources": "CANARY-C4-COMPAREDSRCS",
    "external-urls": "CANARY-C4-EXTURL",
    "resource-extra": "CANARY-C4-RESEXTRA",
    "multi-source-helm": "CANARY-C4-MULTIHELM",
    "multi-source-repo-userinfo": "CANARY-C4-MULTIREPOUSER",
}


def _secret_source(tag: str = "") -> dict[str, Any]:
    """An ``ApplicationSource`` with a canary in every withheld member."""
    c = CANARIES
    return {
        "repoURL": (
            f"https://{c['repo-userinfo']}:{c['repo-password']}@git.example.com/"
            f"org/secret.git?token={c['repo-query']}#{c['repo-fragment']}"
        ),
        "path": "apps/secret",
        "targetRevision": "main",
        "helm": {
            "values": f"image:\n  tag: {c['helm-values']}{tag}\n",
            "valuesObject": {"db": {"password": c["helm-values-object"]}},
            "parameters": [{"name": "p", "value": c["helm-parameter"]}],
            "fileParameters": [{"name": "f", "path": c["helm-file-parameter"]}],
            "valueFiles": [f"values-{c['helm-value-files']}.yaml"],
            "releaseName": c["helm-release-name"],
        },
        "kustomize": {
            "patches": [{"patch": f"- op: add\n  value: {c['kustomize-patch']}"}],
            "commonLabels": {"team": c["kustomize-common-label"]},
        },
        "plugin": {
            "name": "vault",
            "env": [{"name": "TOKEN", "value": c["plugin-env"]}],
            "parameters": [{"name": "p", "string": c["plugin-parameter"]}],
        },
        "directory": {
            "include": c["directory-include"],
            "jsonnet": {
                "extVars": [{"name": "v", "value": c["jsonnet-ext-var"]}],
                "tlas": [{"name": "t", "value": c["jsonnet-tla"]}],
            },
        },
    }


def _with_helm_values(value: str) -> dict[str, Any]:
    return {"repoURL": "https://git.example.com/org/x.git",
            "helm": {"values": f"k: {value}"}}  # fmt: skip


def _canary_app() -> dict[str, Any]:
    c = CANARIES
    item = app(
        "c4-canary",
        spec_source=_secret_source(),
        history=[history_entry(1, NOW - timedelta(days=3)),
                 history_entry(2, NOW - timedelta(hours=1))],
        resources=[resource_entry("web", extra=c["resource-extra"])],
    )  # fmt: skip
    meta = item["metadata"]
    meta["annotations"] = {
        "note": c["annotation"],
        "kubectl.kubernetes.io/last-applied-configuration": json.dumps(
            {"spec": {"source": {"helm": {"values": c["last-applied"]}}}}
        ),
    }
    meta["labels"] = {"team": c["label"]}
    meta["managedFields"] = [
        {"manager": "kubectl", "fieldsV1": {f"f:{c['managed-fields']}": {}}}
    ]
    spec = item["spec"]
    spec["info"] = [{"name": "owner", "value": c["spec-info"]}]
    spec["ignoreDifferences"] = [
        {"kind": "Secret", "jsonPointers": [c["ignore-differences"]]}
    ]
    spec["destination"]["extra"] = c["destination-extra"]
    spec["extraField"] = c["spec-extra"]
    spec["syncPolicy"] = {"automated": {"prune": False, "selfHeal": True}}
    item["operation"] = {
        "sync": {
            "source": _with_helm_values(c["operation"]),
            "sources": [_with_helm_values(c["operation-sources"])],
        },
        "initiatedBy": {"username": "admin"},
    }
    status = item["status"]
    status["history"][0]["source"] = _with_helm_values(c["history-source"])
    status["history"][0]["source"]["repoURL"] = (
        f"https://{c['history-repo-userinfo']}@git.example.com/org/h.git"
    )
    status["history"][0]["extra"] = c["history-extra"]
    status["history"][1].pop("source")
    status["history"][1]["sources"] = [_with_helm_values(c["history-sources"])]
    status["operationState"] = {
        "operation": {
            "sync": {"source": _with_helm_values(c["operation-state-operation"])}
        },
        "phase": "Succeeded",
        "message": "successfully synced",
        "startedAt": "2026-11-20T08:00:00Z",
        "finishedAt": "2026-11-20T08:01:00Z",
        "syncResult": {
            "revision": "c" * 40,
            "source": _with_helm_values(c["sync-result-source"]),
            "resources": [],
        },
    }
    status["sync"]["comparedTo"]["source"] = _with_helm_values(c["compared-to-source"])
    status["sync"]["comparedTo"]["sources"] = [
        _with_helm_values(c["compared-to-sources"])
    ]
    status["summary"]["externalURLs"] = [
        f"https://shop.example.com/{c['external-urls']}"
    ]
    status["extraField"] = c["status-extra"]
    item["extra"] = c["top-extra"]
    return item


def _canary_apps() -> list[dict[str, Any]]:
    c = CANARIES
    multi = app(
        "c4-canary-multi",
        spec_sources=[
            source("plain"),
            {**_with_helm_values(c["multi-source-helm"]),
             "repoURL": f"https://{c['multi-source-repo-userinfo']}@git.example.com/o/m"},
        ],
    )  # fmt: skip
    return [_canary_app(), multi]


def _collect_spans(run: Callable[[], list[Any]], api: FakeApiServer):
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from opspilot import tracing
    from tests.test_tracing import lab_env

    exporter = InMemorySpanExporter()
    tracer = tracing.configure(
        lab_env(
            OPSPILOT_TOOL_PROFILE=EXT_PROFILE,
            OPSPILOT_K8S_API_URL=api.url,
            OPSPILOT_OTEL_PROMETHEUS_URL="http://127.0.0.1:19090",
            OPSPILOT_OTEL_JAEGER_URL="http://127.0.0.1:16686",
            **{SWITCH: "1"},
        ),
        exporter=exporter,
    )
    try:
        with tracer.run(incident_id="c4-incident", run_id=RUN_ID, versions={}):
            results = run()
        tracer.shutdown()
        spans = exporter.get_finished_spans()
    finally:
        tracing.reset()
    exported = json.dumps(
        [
            {
                "name": span.name,
                "attributes": dict(span.attributes or {}),
                "events": [
                    {"name": e.name, "attributes": dict(e.attributes or {})}
                    for e in span.events
                ],
            }
            for span in spans
        ],
        default=str,
    )
    return results, spans, exported


def _leaks(outputs: Mapping[str, str]) -> list[tuple[str, str]]:
    return [
        (where, place)
        for where, text in outputs.items()
        for place, value in CANARIES.items()
        if value in text
    ]


def test_every_canary_is_planted_in_what_the_server_returns():
    # Positive control: a missing canary would make the leak test vacuous.
    planted = json.dumps(_canary_apps())
    assert [p for p, v in CANARIES.items() if v not in planted] == []
    assert len(set(CANARIES.values())) == len(CANARIES)


def test_no_withheld_field_reaches_any_output(api, caplog):
    caplog.set_level(logging.DEBUG)
    api.apps = _canary_apps()
    worker = Worker(_env(api))

    def run():
        return [
            worker.call(LIST, {}),
            worker.call(LIST, {"namespace": "argocd"}),
            worker.call(GET, _named("c4-canary")),
            worker.call(GET, _named("c4-canary-multi", "argocd")),
            worker.call(HISTORY, _named("c4-canary")),
            worker.call(RESOURCES, _named("c4-canary")),
        ]

    outcomes, spans, exported = _collect_spans(run, api)
    assert any(GET in span.name or GET in json.dumps(dict(span.attributes or {}), default=str)
               for span in spans), [s.name for s in spans]  # fmt: skip
    for outcome in outcomes:
        assert outcome.status == "ok", _view(outcome)
    get_row = _rows(outcomes[2])[0]
    assert set(get_row) <= GET_ROW_FIELDS
    assert get_row["sources"][0]["repo_url"] == "https://git.example.com/org/secret.git"
    assert get_row["automated"] == {"prune": False, "self_heal": True}
    assert [r["id"] for r in _rows(outcomes[4])] == [1, 2]
    assert _rows(outcomes[4])[0]["sources"][0]["repo_url"] == (
        "https://git.example.com/org/h.git"
    )
    assert set(_rows(outcomes[5])[0]) == RESOURCE_ROW_FIELDS
    outputs = {
        "views": json.dumps([_view(o) for o in outcomes], default=str),
        "evidence-raw": "\n".join(
            o.evidence.raw.decode("utf-8", "replace") for o in outcomes
        ),
        "evidence-view": json.dumps(
            [dict(o.evidence.view) for o in outcomes], default=str
        ),
        "sink": repr(worker.sink.records),
        "ledger": repr(worker.store.charges),
        "spans": exported,
        "logs": caplog.text + repr([r.args for r in caplog.records]),
    }
    assert _leaks(outputs) == []
    for where, text in outputs.items():
        assert "last-applied-configuration" not in text, where
        assert "managedFields" not in text, where
        assert "valuesObject" not in text, where


def test_an_oversized_view_saves_no_withheld_field_either(api, caplog):
    # 500 resources with dense 2000-character health messages: the
    # projection is ~1 MB (under 2 MiB), the view far over 25k tokens, so the
    # whole call is RESULT_TOO_LARGE and the executor keeps the body as raw.
    caplog.set_level(logging.DEBUG)
    item = _canary_app()
    resources = []
    for n in range(500):
        dense = hashlib.sha256(str(n).encode()).hexdigest() * 32
        entry = resource_entry(f"r-{n:03d}")
        entry["health"]["message"] = dense[:2000]
        resources.append(entry)
    item["status"]["resources"] = resources
    api.apps = [item]
    worker = Worker(_env(api))
    outcome = worker.call(RESOURCES, _named("c4-canary"))
    _failed(outcome, "RESULT_TOO_LARGE")
    assert "c4-canary" in _text(outcome) or "app_name" in _text(outcome)
    outputs = {
        "view": _text(outcome),
        "evidence-raw": outcome.evidence.raw.decode("utf-8", "replace")
        if outcome.evidence is not None
        else "",
        "sink": repr(worker.sink.records),
        "sink-raw": repr([getattr(r, "raw", b"") for r in worker.sink.records]),
        "logs": caplog.text,
    }
    assert _leaks(outputs) == []


def test_an_error_answer_echoes_no_withheld_field(api, caplog):
    caplog.set_level(logging.DEBUG)
    api.raw_at[0] = _json([CANARIES["helm-values"], {"spec": _secret_source()}])
    worker = Worker(_env(api))
    outcome = worker.call(GET, _named("c4-canary"))
    _failed(outcome, "MALFORMED_RESULT")
    outputs = {
        "view": _text(outcome),
        "sink": repr(worker.sink.records),
        "logs": caplog.text,
    }
    assert _leaks(outputs) == []


# ======================================================================================
# 5. Caps, budgets and error classes
# ======================================================================================


def _small_app(name: str, namespace: str = "n") -> dict[str, Any]:
    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"project": "p", "source": {"repoURL": "https://g.x/r"},
                 "destination": {"namespace": "d"}},
        "status": {"sync": {"status": "Synced"}, "health": {"status": "Healthy"}},
    }  # fmt: skip


def test_list_keeps_the_first_300_after_sorting_and_marks_incomplete(api):
    # NOTE: 300 is the interface's estimate (see the module docstring).
    names = [(f"ns-{n % 3}", f"a-{n:03d}") for n in range(350)]
    random.Random(568).shuffle(names)
    api.apps = [_small_app(name, ns) for ns, name in names]
    outcome = _run(api, LIST)
    rows = _rows(outcome)
    assert [(r["namespace"], r["name"]) for r in rows] == sorted(names)[:300]
    assert _view(outcome)["incomplete"] is True


def test_list_at_the_cap_is_complete(api):
    api.apps = [_small_app(f"a-{n:03d}") for n in range(300)]
    outcome = _run(api, LIST)
    assert len(_rows(outcome)) == 300
    assert _view(outcome)["incomplete"] is False


def test_resources_keep_the_first_500_after_sorting_and_mark_incomplete(api):
    # NOTE: 500 is the interface's estimate (see the module docstring).
    entries = [
        {"group": "", "version": "v1", "kind": "ConfigMap", "namespace": "d",
         "name": f"c{n:03d}", "status": "Synced"}
        for n in range(520)
    ]  # fmt: skip
    random.Random(568).shuffle(entries)
    item = _small_app("c4-res", "argocd")
    item["status"]["resources"] = entries
    api.apps = [item]
    outcome = _run(api, RESOURCES, _named("c4-res"))
    rows = _rows(outcome)
    assert [r["name"] for r in rows] == [f"c{n:03d}" for n in range(500)]
    assert _view(outcome)["incomplete"] is True


def test_history_has_no_row_cap(api):
    entries = [history_entry(i, NOW - timedelta(minutes=i)) for i in range(1, 41)]
    api.apps = [app("c4-long", history=entries)]
    outcome = _run(api, HISTORY, _named("c4-long"))
    assert [r["id"] for r in _rows(outcome)] == list(range(1, 41))
    assert _view(outcome)["incomplete"] is False


def _padded(name: str, size: int, *, small: bool = False) -> dict[str, Any]:
    item = _small_app(name, "argocd") if small else app(name)
    item["spec"]["source"]["helm"] = {"values": "A" * size}  # read, never projected
    return item


def test_a_named_object_over_4_mib_is_too_large_and_one_under_is_read(api):
    api.apps = [_padded("c4-big", int(3.5 * MIB))]
    assert _row(_run(api, GET, _named("c4-big")))["name"] == "c4-big"
    api.apps = [_padded("c4-big", int(4.5 * MIB))]
    _failed(_run(api, GET, _named("c4-big")), "RESULT_TOO_LARGE")


def test_list_pages_share_one_4_mib_budget(api):
    # Three pages of ~1.5 MiB: the third crosses 4 MiB in all.
    api.apps = [_padded(f"a-{n:03d}", 15_000, small=True) for n in range(300)]
    outcome = _run(api, LIST)
    _failed(outcome, "RESULT_TOO_LARGE")
    assert len(api.requests) <= 3
    api.requests.clear()
    api.apps = api.apps[:200]  # ~3 MiB in two pages
    assert len(_rows(_run(api, LIST))) == 200


def test_at_most_50_list_pages_are_read(api):
    api.apps = [_small_app(f"a-{n:04d}") for n in range(5050)]  # 51 pages
    outcome = _run(api, LIST)
    assert len(api.requests) <= 50
    # The interface does not say whether hitting the page cap is
    # RESULT_TOO_LARGE or an incomplete answer; it is never a complete one.
    if outcome.status == "ok":
        assert _view(outcome)["incomplete"] is True
    else:
        assert outcome.reason == "RESULT_TOO_LARGE", _view(outcome)


def test_a_read_past_the_call_deadline_is_a_timeout(api):
    api.apps = [app("c4-slow")]
    api.delay = 4.0
    worker = Worker(_env(api), deadline_seconds=2)
    started = time.monotonic()
    outcome = worker.call(GET, _named("c4-slow"))
    assert (outcome.status, outcome.reason) == ("timeout", "TOOL_TIMEOUT"), _view(
        outcome
    )
    assert time.monotonic() - started < 3.9


def test_registrations_are_bounded_reads_that_may_contain_secrets():
    from opspilot.tools import otel_demo_ext

    # ASSUMPTION: the private registry seam takes ``argocd`` as a keyword.
    regs = {r.name: r for r in otel_demo_ext._registrations(argocd=True)}
    assert set(ARGO_TOOLS) <= set(regs)
    assert not set(ARGO_TOOLS) & {r.name for r in otel_demo_ext._registrations()}
    for name in ARGO_TOOLS:
        reg = regs[name]
        assert reg.read_only is True, name
        assert reg.verb == ("list" if name == LIST else "get"), name
        assert reg.source == "otel-demo", name
        assert reg.result_path == ("results",), name
        assert reg.may_contain_secrets is True, name
        assert reg.max_result_bytes == 2 * MIB, name
        assert reg.max_view_tokens == 25_000, name
        assert 0 < reg.request_timeout_seconds <= 30, name


def _fail(api: FakeApiServer, index: int, status: int, reason: str) -> None:
    api.fail_at[index] = (status, status_body(status, reason, f"synthetic {reason}"))


STATUS_CASES = [
    (400, "BadRequest", "INVALID_PARAMS"),
    (422, "Invalid", "INVALID_PARAMS"),
    (401, "Unauthorized", "SOURCE_FORBIDDEN"),
    (403, "Forbidden", "SOURCE_FORBIDDEN"),
    (410, "Expired", "SOURCE_ERROR"),
    (429, "TooManyRequests", "SOURCE_UNAVAILABLE"),
    (500, "InternalError", "SOURCE_UNAVAILABLE"),
    (503, "ServiceUnavailable", "SOURCE_UNAVAILABLE"),
]


def _params(tool: str) -> dict[str, str]:
    return {} if tool == LIST else _named("c4-healthy")


@pytest.mark.parametrize(("status", "k8s_reason", "reason"), STATUS_CASES)
@pytest.mark.parametrize("tool", ARGO_TOOLS)
def test_statuses_map_to_the_argocd_error_table_with_one_request(
    api, tool, status, k8s_reason, reason
):
    api.apps = _basic_apps()
    _fail(api, 0, status, k8s_reason)
    outcome = _run(api, tool, _params(tool))
    _failed(outcome, reason)
    assert len(api.requests) == 1  # no retry
    if status in (401, 403):
        assert FORBIDDEN_NOTE in _text(outcome), _view(outcome)


@pytest.mark.parametrize("tool", NAMED_TOOLS)
@pytest.mark.parametrize("namespace", [None, "c4-other"])
def test_a_named_404_naming_the_app_is_no_data_app_not_found(api, tool, namespace):
    api.apps = _basic_apps()
    outcome = _run(api, tool, _named("c4-missing", namespace))
    assert (outcome.status, outcome.reason) == ("no_data", "NO_DATA"), _view(outcome)
    expected = f"APP_NOT_FOUND: {namespace or 'argocd'}/c4-missing"
    assert any(expected in note for note in _notes(outcome)), _notes(outcome)
    assert CRD_MISSING_NOTE not in _text(outcome)
    assert len(api.requests) == 1


@pytest.mark.parametrize("form", ["status", "text"])
@pytest.mark.parametrize("tool", ARGO_TOOLS)
def test_a_404_without_the_apps_name_is_source_error_crd_missing(api, tool, form):
    api.apps = _basic_apps()
    api.crd_missing = form
    outcome = _run(api, tool, _params(tool))
    _failed(outcome, "SOURCE_ERROR")
    assert CRD_MISSING_NOTE in _text(outcome), _view(outcome)
    assert "APP_NOT_FOUND" not in _text(outcome)
    assert len(api.requests) == 1


@pytest.mark.parametrize("tool", NAMED_TOOLS)
def test_a_named_404_for_another_name_is_crd_missing_not_no_data(api, tool):
    api.fail_at[0] = (
        404,
        status_body(404, "NotFound", "not found",
                    {"name": "someone-else", "group": "argoproj.io", "kind": "applications"}),
    )  # fmt: skip
    outcome = _run(api, tool, _named("c4-healthy"))
    _failed(outcome, "SOURCE_ERROR")
    assert CRD_MISSING_NOTE in _text(outcome), _view(outcome)


def test_an_expired_continue_fails_the_whole_list_without_partial_rows(api):
    api.apps = [_small_app(f"a-{n:03d}") for n in range(210)]
    _fail(api, 1, 410, "Expired")
    outcome = _run(api, LIST)
    _failed(outcome, "SOURCE_ERROR")
    assert len(api.requests) == 2


MALFORMED = {
    "list-array": (LIST, b"[]"),
    "list-string": (LIST, b'"x"'),
    "list-not-json": (LIST, b"{not json"),
    "list-items-not-list": (LIST, _json({"kind": "ApplicationList", "items": {}})),
    "get-array": (GET, b"[]"),
    "get-not-json": (GET, b"<html>"),
    "history-number": (HISTORY, b"7"),
    "resources-string": (RESOURCES, b'"x"'),
}


@pytest.mark.parametrize("case", sorted(MALFORMED))
def test_an_answer_that_is_not_an_object_is_malformed(api, case):
    tool, body = MALFORMED[case]
    api.raw_at[0] = body
    _failed(_run(api, tool, _params(tool)), "MALFORMED_RESULT")


def test_an_unreachable_api_server_is_source_unavailable(api):
    env = _env(api)
    api.stop()
    _failed(Worker(env).call(LIST), "SOURCE_UNAVAILABLE")


def test_an_untrusted_certificate_reads_nothing(api):
    api.apps = _basic_apps()
    env = _env(api, OPSPILOT_K8S_CA_FILE=os.environ["C4_TEST_PLACEHOLDER_CA"])
    outcome = Worker(env).call(LIST)
    assert outcome.status == "error", _view(outcome)
    assert api.requests == []


def test_an_empty_list_is_no_data(api):
    api.apps = _basic_apps()
    outcome = _run(api, LIST, {"namespace": "c4-none"})
    assert (outcome.status, outcome.reason) == ("no_data", "NO_DATA"), _view(outcome)


@pytest.mark.parametrize("tool", (HISTORY, RESOURCES))
@pytest.mark.parametrize("shape", ["empty", "absent", "no-status"])
def test_an_app_without_history_or_resources_is_no_data_naming_it(api, tool, shape):
    item = app("c4-quiet", history=[], resources=[])
    if shape == "absent":
        item["status"].pop("history")
        item["status"].pop("resources")
    elif shape == "no-status":
        item.pop("status")
    api.apps = [item]
    outcome = _run(api, tool, _named("c4-quiet"))
    assert (outcome.status, outcome.reason) == ("no_data", "NO_DATA"), _view(outcome)
    assert "c4-quiet" in " ".join(_notes(outcome)), _notes(outcome)
    assert "APP_NOT_FOUND" not in _text(outcome)


def test_every_request_of_every_tool_is_an_applications_path(api):
    api.apps = [*_basic_apps(), *_canary_apps()]
    worker = Worker(_env(api))
    for tool, params in (
        (LIST, {}),
        (LIST, {"namespace": "argocd"}),
        (LIST, {"namespace": "c4-other"}),
        (GET, _named("c4-healthy")),
        (GET, _named("c4-elsewhere", "c4-other")),
        (HISTORY, _named("c4-canary")),
        (RESOURCES, _named("c4-healthy")),
        (GET, _named("c4-missing")),
    ):
        worker.call(tool, params)
    assert len(api.requests) == 8
    _assert_only_application_paths(api)


# ======================================================================================
# 6. Not in the generic resource table
# ======================================================================================


@pytest.mark.parametrize("tool", ["kubernetes_jq_query", "kubernetes_count"])
@pytest.mark.parametrize(
    "kind", ["applications", "Application", "applications.argoproj.io"]
)
def test_object_tools_still_cannot_read_applications(api, tool, kind):
    outcome = _run(api, tool, {"kind": kind, "jq_expr": ".items[]"})
    assert outcome.status == "error", _view(outcome)
    assert api.requests == []


def test_the_generic_resource_table_has_no_argoproj_resource():
    from opspilot.tools import kubernetes_core

    table = json.dumps(kubernetes_core.resource_table(), default=str)
    assert "argoproj" not in table
    assert "application" not in table.lower()


# ======================================================================================
# 7. The switch, revisions and the source list
# ======================================================================================


def _face(env: Mapping[str, str]):
    return select_profile(env).face(FakeClock(start=NOW))


def _tool_names(env: Mapping[str, str]) -> list[str]:
    return [s["function"]["name"] for s in _face(env).tool_schemas]


def _surface(env: Mapping[str, str]) -> dict[str, Any]:
    profile = select_profile(env)
    face = profile.face(FakeClock(start=NOW))
    return {
        "tool_schemas": [dict(item) for item in face.tool_schemas],
        "variant_id": face.variant_id,
        "evidence_context": json.loads(canonical(dict(face.evidence_context(RUN_ID)))),
        "versions": profile.versions(),
    }


FLAGS = {
    "es": ("OPSPILOT_ES_URL", "http://127.0.0.1:9200"),
    "loki": ("OPSPILOT_LOKI_URL", "http://127.0.0.1:3100"),
    "vlogs": ("OPSPILOT_VICTORIALOGS_URL", "http://127.0.0.1:9428"),
    "live": ("OPSPILOT_K8S_LIVE_METRICS", "1"),
    "helm": (HELM_SWITCH, "1"),
    "knowledge": ("OPSPILOT_KNOWLEDGE_TOOL", "1"),
    "todo": ("OPSPILOT_TODO_WRITE", "1"),
}
COMBOS = [
    (),
    ("es",),
    ("loki", "vlogs"),
    ("live",),
    ("helm",),
    ("knowledge",),
    ("todo",),
    ("helm", "knowledge", "todo"),
    ("es", "loki", "vlogs", "live", "helm", "knowledge", "todo"),
]


def _tail_tools() -> set[str]:
    """Tools that stay after ArgoCD's in the array: ``fetch_skill`` and
    ``TodoWrite`` (the interface), and whatever main already puts after them
    (C6a SQL, C3 Kafka)."""
    from opspilot.tools import kafka_admin, sql_query

    return {"fetch_skill", "TodoWrite", *sql_query.TOOL_NAMES, *kafka_admin.TOOL_NAMES}


def _combo_env(combo: tuple[str, ...], switch: str | None) -> dict[str, str]:
    env = {PROFILE_ENV: EXT_PROFILE}
    for flag in combo:
        name, value = FLAGS[flag]
        env[name] = value
    if switch is not None:
        env[SWITCH] = switch
    return env


@pytest.mark.parametrize("combo", COMBOS, ids=lambda c: "+".join(c) or "none")
def test_switch_off_spellings_leave_every_surface_byte_identical(combo):
    reference = canonical(_surface(_combo_env(combo, None)))
    for off in ("", "0"):
        assert canonical(_surface(_combo_env(combo, off))) == reference, off
    assert not set(ARGO_TOOLS) & set(_tool_names(_combo_env(combo, None)))


@pytest.mark.parametrize("combo", COMBOS, ids=lambda c: "+".join(c) or "none")
def test_switch_on_adds_exactly_the_four_tools_and_the_change_history_names(combo):
    off_env, on_env = _combo_env(combo, None), _combo_env(combo, "1")
    off, on = _surface(off_env), _surface(on_env)
    off_names = [s["function"]["name"] for s in off["tool_schemas"]]
    on_names = [s["function"]["name"] for s in on["tool_schemas"]]
    tail_set = _tail_tools()
    split = len(off_names)
    while split and off_names[split - 1] in tail_set:
        split -= 1
    head, tail = off_names[:split], off_names[split:]
    assert on_names == head + list(ARGO_TOOLS) + tail
    helm_names = sorted(n for n in off_names if n.startswith("helm_"))
    if helm_names:
        # After B4's Helm tools.
        assert on_names.index(LIST) > max(on_names.index(n) for n in helm_names)
    # Every other tool schema is byte for byte the same.
    kept = [s for s in on["tool_schemas"] if s["function"]["name"] not in ARGO_TOOLS]
    assert canonical(kept) == canonical(off["tool_schemas"])
    assert on["variant_id"] == off["variant_id"]
    # Versions: only the tool schema revision moves.
    assert set(on["versions"]) == set(off["versions"])
    for key, value in off["versions"].items():
        if key == "tool_schema_revision":
            assert on["versions"][key] != value
        else:
            assert on["versions"][key] == value, key
    # data_sources: the four names join change_history (sorted with Helm's).
    off_sources = off["evidence_context"]["data_sources"]
    on_sources = on["evidence_context"]["data_sources"]
    off_available = off_sources["available"]
    on_available = on_sources["available"]
    assert set(on_available) == set(off_available) | {"change_history"}
    assert on_available["change_history"] == sorted(
        [*off_available.get("change_history", []), *ARGO_TOOLS]
    )
    if helm_names:
        assert set(helm_names) <= set(off_available.get("change_history", []))
    else:
        assert on_available["change_history"] == sorted(ARGO_TOOLS)
    for key in off_available:
        if key != "change_history":
            assert on_available[key] == off_available[key], key
    assert "change_history" not in on_sources["not_integrated"]
    assert on_sources["not_integrated"] == [
        n for n in off_sources["not_integrated"] if n != "change_history"
    ]
    assert on_sources["instruction"] == off_sources["instruction"]
    rest_on = {k: v for k, v in on["evidence_context"].items() if k != "data_sources"}
    rest_off = {k: v for k, v in off["evidence_context"].items() if k != "data_sources"}
    assert rest_on == rest_off


def test_each_backend_set_has_its_own_argocd_revision():
    from opspilot.tools import otel_demo_ext

    revisions = set()
    for combo in COMBOS:
        for switch in (None, "1"):
            revisions.add(
                select_profile(_combo_env(combo, switch)).versions()[
                    "tool_schema_revision"
                ]
            )
    helm_merged = hasattr(otel_demo_ext, "HELM_TOOL_NAMES")
    distinct_combos = (
        len(COMBOS)
        if helm_merged
        else len({tuple(f for f in c if f != "helm") for c in COMBOS})
    )
    assert len(revisions) == 2 * distinct_combos
    on = select_profile({PROFILE_ENV: EXT_PROFILE, SWITCH: "1"}).versions()
    # ASSUMPTION: ``argocd`` is a keyword of tool_schema_revision.
    assert (
        otel_demo_ext.ARGOCD_TOOL_SCHEMA_REVISION
        == on["tool_schema_revision"]
        == otel_demo_ext.tool_schema_revision(argocd=True)
    )
    assert (
        otel_demo_ext.tool_schema_revision(argocd=False)
        == otel_demo_ext.TOOL_SCHEMA_REVISION
    )
    assert set(ARGO_TOOLS) <= set(otel_demo_ext.ARGOCD_TOOL_NAMES)


@pytest.mark.parametrize("value", ["1", "garbage", "true"])
def test_the_switch_is_read_only_in_the_extension_profile(value):
    for profile in ("otel-demo", None):
        base_env = {PROFILE_ENV: profile} if profile else {}
        reference = canonical(_surface(base_env))
        assert canonical(_surface({**base_env, SWITCH: value})) == reference


INVALID_SWITCH = ["true", "yes", "on", "2", "01", " 1", "1 ", "TRUE", "-1", "1.0",
                  "off", "false"]  # fmt: skip


def _web_start(env: Mapping[str, str]) -> None:
    profile = select_profile(env)
    profile.versions()
    profile.face(FakeClock(start=NOW)).tool_schemas  # noqa: B018


def _worker_start(env: Mapping[str, str]) -> None:
    profile = select_profile(env)
    clock = FakeClock(start=NOW)
    lease = _lease()
    deadline = NOW + timedelta(minutes=10)
    factory = profile.executor_factory(
        FakeStore(lease, deadline=deadline), RecordingSink(), clock
    )
    factory(lease, _input(profile, lease, clock, deadline))


@pytest.mark.parametrize("value", INVALID_SWITCH)
def test_an_invalid_switch_value_fails_the_web_start(value):
    # ASSUMPTION: the same SystemExit the other startup codes end with.
    with pytest.raises(SystemExit, match="ARGOCD_INVALID"):
        _web_start({PROFILE_ENV: EXT_PROFILE, SWITCH: value})


@pytest.mark.parametrize("value", ["true", "on", " 1"])
def test_an_invalid_switch_value_fails_the_worker_start(value):
    _require_tools()
    with pytest.raises(SystemExit, match="ARGOCD_INVALID"):
        _worker_start(_env(None, **{SWITCH: value}))


def test_the_startup_code_is_registered():
    from opspilot.tools import profiles

    assert "ARGOCD_INVALID" in profiles._STARTUP_CODES


@pytest.mark.parametrize(
    "url", ["https://k8s.example.invalid:6443", "https://10.0.0.1:6443"]
)
def test_there_is_no_lab_only_gate_for_argocd(url):
    # C4-11: the switch adds no lab gate (the tracer's own KUBERNETES_NOT_LAB
    # rule is unchanged and not exercised here).
    _require_tools()
    _worker_start(_env(None, OPSPILOT_K8S_API_URL=url))


def test_web_on_and_worker_off_record_different_versions():
    on = select_profile({PROFILE_ENV: EXT_PROFILE, SWITCH: "1"}).versions()
    off = select_profile({PROFILE_ENV: EXT_PROFILE}).versions()
    assert on != off
    assert select_profile(_env(None)).versions() == on


@pytest.mark.parametrize("off", [None, "", "0"])
def test_with_the_switch_off_the_tools_do_not_exist(api, off):
    api.apps = _basic_apps()
    worker = Worker(_env(api, **{SWITCH: off}))
    for tool in ARGO_TOOLS:
        outcome = worker.call(tool, _params(tool))
        assert outcome.status in ("error", "denied"), _view(outcome)
    assert api.requests == []


def test_entrypoint_passes_the_switch_to_the_worker(tmp_path):
    entrypoint = REPO / "deploy" / "docker-entrypoint.sh"
    worker_env = tmp_path / "worker_env"
    worker_env.write_text(f"{SWITCH}=1\n")
    result = subprocess.run(
        ["sh", str(entrypoint), "env"],
        env={"PATH": "/usr/bin:/bin", "OPSPILOT_ENV_FILE": str(worker_env)},
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    env = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    assert env[SWITCH] == "1"


# -- identity layer and CI fixtures (C4-4, R8-25) ---------------------------------------

#: Pinned on origin/main 23a034c6: the base manifest does not change (C4-4).
BASE_RBAC_SHA256 = "ddd4299d538ecfeb48ed272bd2f7d6306c54778f8c188ae09366a4b4370fdea0"


def test_the_base_identity_is_unchanged_and_the_argocd_overlay_reads_applications():
    import yaml

    base = REPO / "scripts" / "kind_lab" / "opspilot-readonly-rbac.yaml"
    assert hashlib.sha256(base.read_bytes()).hexdigest() == BASE_RBAC_SHA256
    overlay = REPO / "scripts" / "kind_lab" / "opspilot-readonly-rbac-argocd.yaml"
    documents = [d for d in yaml.safe_load_all(overlay.read_text()) if d]
    kinds = {d["kind"]: d for d in documents}
    assert set(kinds) == {"ClusterRole", "ClusterRoleBinding"}
    assert len(documents) == 2
    role, binding = kinds["ClusterRole"], kinds["ClusterRoleBinding"]
    assert role["metadata"]["name"] == "opspilot-investigator-argocd"
    assert role["rules"] == [
        {"apiGroups": ["argoproj.io"], "resources": ["applications"],
         "verbs": ["get", "list"]}
    ]  # fmt: skip
    assert binding["metadata"]["name"] == "opspilot-investigator-argocd"
    assert binding["roleRef"]["kind"] == "ClusterRole"
    assert binding["roleRef"]["name"] == "opspilot-investigator-argocd"
    assert binding["subjects"] == [
        {
            "kind": "ServiceAccount",
            "name": "opspilot-investigator",
            "namespace": "opspilot-readonly",
        }
    ]


INTERFACE_CANARIES = {
    "CANARY-C4-VALUES", "CANARY-C4-VALUESOBJ", "CANARY-C4-PARAM",
    "CANARY-C4-FILEPARAM", "CANARY-C4-PLUGINENV", "CANARY-C4-PLUGINPARAM",
    "CANARY-C4-KPATCH", "CANARY-C4-JSONNET", "CANARY-C4-ANNOTATION",
    "CANARY-C4-LABEL", "CANARY-C4-OPERATION", "CANARY-C4-HISTSRC",
    "CANARY-C4-OPSTATE", "CANARY-C4-COMPARED", "CANARY-C4-REPOUSER",
    "CANARY-C4-REPOQUERY",
}  # fmt: skip


def test_the_canary_list_names_every_planted_canary():
    assert INTERFACE_CANARIES <= set(CANARIES.values())
    folder = REPO / "tests" / "fixtures" / "c4_argocd"
    listed = {
        line.strip()
        for line in (folder / "canaries.txt").read_text().splitlines()
        if line.strip()
    }
    assert INTERFACE_CANARIES <= listed
    seed = (folder / "apps.yaml").read_text()
    assert [c for c in sorted(listed) if c not in seed] == []


def test_the_authz_probe_has_the_argocd_overlay_mode():
    probe = REPO / "scripts" / "k8s_authz_probe.py"
    result = subprocess.run(
        [sys.executable, str(probe), "--help"],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO,
    )
    assert result.returncode == 0, result.stderr
    assert "--expect-argocd-overlay" in result.stdout
