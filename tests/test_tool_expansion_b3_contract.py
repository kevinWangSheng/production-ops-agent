"""Independent contract tests for tool expansion B3 (#562): the live usage
tools ``kubectl_top_pods`` and ``kubectl_top_nodes``.

Written from the B3 task record (docs/tasks/2026-11-19-tool-expansion-b3.md,
"行为（公开接口）"), the decision table (R8-21, R8-25; user ruling B3-U1=a:
current-state class, empty source times, a sampling instant per row, a fixed
``cannot_prove``) and the B3 pre-review, without reading the implementation.
The interface overrides pre-review B3-11 item 4: a Pod's total is the exact
container sum rounded up once (as kubectl does), so its memory equals the
container sum while its CPU may be up to (containers - 1) millicores below
the sum of the rounded container values. Red until the implementation
lands; collection must stay green.

Every test goes through the public path: build the ``otel-demo-ext``
profile from an environment mapping, call the tools through the executor
and assert on the returned view, the refusal code, the evidence record, or
what a local ``http.server`` fake API server (HTTPS, a synthetic CA minted
with ``openssl``) received. No container, no real cluster, no model.

Seams assumed beyond the interface (each also marked ``# ASSUMPTION:``):

* ``select_profile(env)`` reads ``OPSPILOT_K8S_LIVE_METRICS`` from the
  mapping it is given (web and worker alike, as B1/C5 read their URLs). An
  invalid value ends the start of either process with ``SystemExit`` whose
  text contains ``K8S_LIVE_METRICS_INVALID``, at ``select_profile`` or while
  building the face, versions or worker executor -- the same exit the other
  startup codes use.
* ``otel_demo_ext._registrations(..., live_metrics=True)`` lists the two
  registrations, mirroring ``tool_schema_revision(..., live_metrics=)``.
* A pre-request ``INVALID_NAMESPACE`` refusal surfaces as ``INVALID_PARAMS``
  with the code visible in the view (as every existing transport refusal).
* The tool rows are the view's ``content`` (``result_path=("results",)``),
  and the evidence body parses to ``{"results": <the same rows>, ...}``.

All synthetic credentials and canaries are low-entropy placeholders.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import ssl
import subprocess
import threading
import time
from collections.abc import Iterator, Mapping
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
from opspilot.tracing import check_lab_target
from tests.m1_tool_support import FakeClock, RecordingSink
from tests.test_m1_otel_demo_contract import NOW, TARGET_ID, FakeStore, _lease

EXT_PROFILE = "otel-demo-ext"
SWITCH = "OPSPILOT_K8S_LIVE_METRICS"
PODS = "kubectl_top_pods"
NODES = "kubectl_top_nodes"
LIVE_TOOLS = (PODS, NODES)

METRICS_PODS = "/apis/metrics.k8s.io/v1beta1/pods"
METRICS_NODES = "/apis/metrics.k8s.io/v1beta1/nodes"
CORE_NODES = "/api/v1/nodes"
_NS_PODS = re.compile(r"/apis/metrics\.k8s\.io/v1beta1/namespaces/([^/]+)/pods")

K8S_TOKEN = "k8s-ro-aaaaaaaa562"
PROM_USERNAME = "investigator"
PROM_PASSWORD = "bbbbbbbb562"
CANARY = "cccccccc562"
RUN_ID = "b3-run"

MIB = 1024 * 1024

# Pinned on origin/main a9fe55df + interface commit 7f0308e4 (before any B3
# code), computed with ``_surface_sha256`` below (RUN_ID ``b3-run``): with
# the switch off, otel-demo and all eight extension backend sets must stay
# byte-identical when B3 lands (B3-1).
BASE_TOOL_SCHEMA_REVISION = "otel-demo-3b70fe7c4382"
BASE_SURFACE_SHA256 = "1d0f667ff3612e55a5cb502f6dd4c017092a104bef4adddb69222e2689e8e49b"
PINNED = {
    # (elasticsearch, loki, victorialogs): (revision, surface sha256)
    (False, False, False): (
        "otel-demo-ext-bdb3eda167eb",
        "d5fe5afa660061ce0290d623632ffceda757d2f6e0db7d0c1b570118ccac2fcf",
    ),
    (False, False, True): (
        "otel-demo-ext-9529fceac18e",
        "d4b5b05d6861a8dca487270e31c4b34ff27368be5f603cb5a52909a7b2ed7ed6",
    ),
    (False, True, False): (
        "otel-demo-ext-4a12e3486bb2",
        "a6f4423e62a319c7f65e8710cf4541662f89739cf5e57e2f50d9cedbdf0fbdf7",
    ),
    (False, True, True): (
        "otel-demo-ext-c7bd7d38e50e",
        "3f7014063fb670c2678136e707ed24e541d305e8ddeb1945525bce121e147efa",
    ),
    (True, False, False): (
        "otel-demo-ext-1998f0b7318d",
        "d3464e061ef2b930850af7f9ce309421f55339e384ba7f0b0b981dcb35e0d73b",
    ),
    (True, False, True): (
        "otel-demo-ext-3ff51c96a8ad",
        "ca0539cbb8726065baa82dba575394c52fc88ecc6f3bf84dfd2b5de70c588849",
    ),
    (True, True, False): (
        "otel-demo-ext-663d72c25b30",
        "3914acbe0660dc97a002cb999bc589f13b5482db0667f318dc9eada4f7dc7544",
    ),
    (True, True, True): (
        "otel-demo-ext-1b15ecc5444b",
        "c18485daa6bef2602c10fa08ac3ebbaf8d7ee7a33270b6e0b6b98f82c8a48d43",
    ),
}
BACKEND_URLS = {
    "OPSPILOT_ES_URL": "http://127.0.0.1:9200",
    "OPSPILOT_LOKI_URL": "http://127.0.0.1:3100",
    "OPSPILOT_VICTORIALOGS_URL": "http://127.0.0.1:9428",
}
OTHER_BACKEND_ENV = (
    "OPSPILOT_ES_URL",
    "OPSPILOT_ES_API_KEY",
    "OPSPILOT_LOKI_URL",
    "OPSPILOT_LOKI_BEARER_TOKEN",
    "OPSPILOT_VICTORIALOGS_URL",
    "OPSPILOT_VICTORIALOGS_BEARER_TOKEN",
)

# The call's authorized frame; its end is the "end of the authorized window".
FRAME = Window(NOW - timedelta(hours=6), NOW)
WINDOW_TEXT = "15.022s"


def _z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _instant(value: object) -> datetime:
    assert isinstance(value, str), value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


SAMPLE = NOW + timedelta(seconds=37)
SAMPLE_TS = _z(SAMPLE)
_RFC3339_Z = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")


# -- metrics.k8s.io bodies --------------------------------------------------------


def _json(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


def pod_item(
    namespace: str,
    name: str,
    containers: list[tuple[str, str, str]],
    *,
    timestamp: object = SAMPLE_TS,
    window: object = WINDOW_TEXT,
    labels: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "metadata": {
            "name": name,
            "namespace": namespace,
            "creationTimestamp": SAMPLE_TS,
            "labels": dict(labels or {"app": name}),
        },
        "timestamp": timestamp,
        "window": window,
        "containers": [
            {"name": c, "usage": {"cpu": cpu, "memory": memory}}
            for c, cpu, memory in containers
        ],
    }
    if timestamp is None:
        del item["timestamp"]
    if window is None:
        del item["window"]
    return item


def node_metric(
    name: str,
    cpu: str,
    memory: str,
    *,
    timestamp: object = SAMPLE_TS,
    window: object = WINDOW_TEXT,
) -> dict[str, Any]:
    return {
        "metadata": {"name": name, "creationTimestamp": SAMPLE_TS, "labels": {}},
        "timestamp": timestamp,
        "window": window,
        "usage": {"cpu": cpu, "memory": memory},
    }


def node_object(
    name: str, allocatable: Mapping[str, str] | None, *, padding: str = ""
) -> dict[str, Any]:
    status: dict[str, Any] = {"capacity": {"cpu": "64", "memory": "256Gi"}}
    if allocatable is not None:
        status["allocatable"] = {**allocatable, "pods": "110"}
    metadata: dict[str, Any] = {"name": name, "labels": {"kind": "synthetic"}}
    if padding:
        metadata["annotations"] = {"padding": padding}
    return {"metadata": metadata, "spec": {}, "status": status}


def pod_metrics_list(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "kind": "PodMetricsList",
        "apiVersion": "metrics.k8s.io/v1beta1",
        "metadata": {},
        "items": items,
    }


def node_metrics_list(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "kind": "NodeMetricsList",
        "apiVersion": "metrics.k8s.io/v1beta1",
        "metadata": {},
        "items": items,
    }


def status_body(code: int, reason: str, message: str) -> bytes:
    return _json(
        {
            "kind": "Status",
            "apiVersion": "v1",
            "metadata": {},
            "status": "Failure",
            "message": message,
            "reason": reason,
            "code": code,
        }
    )


# -- local API server fake ----------------------------------------------------------


class FakeApiServer:
    """A loopback HTTPS server answering the three read paths the way the
    API server and metrics-server do, and recording every request."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.pod_items: list[dict[str, Any]] = []
        self.node_metrics: list[dict[str, Any]] = []
        self.nodes: list[dict[str, Any]] = []
        #: The server's own page size for /api/v1/nodes (forces ``continue``).
        self.node_page_size: int | None = None
        #: path -> raw 200 body; path -> (status, content type, body).
        self.raw: dict[str, bytes] = {}
        self.fail: dict[str, tuple[int, str, bytes]] = {}
        self.delay = 0.0
        self.redirect = False
        self._server: ThreadingHTTPServer | None = None
        self.url = ""
        self.ca: Path | None = None

    def answer(self, record: dict[str, Any]) -> tuple[int, dict[str, str], bytes]:
        if self.delay:
            time.sleep(self.delay)
        path = record["path"]
        if self.redirect:
            return 302, {"Location": "/elsewhere"}, b""
        if record["method"] != "GET":
            return 405, {}, status_body(405, "MethodNotAllowed", "not allowed")
        if path in self.fail:
            status, content_type, body = self.fail[path]
            return status, {"Content-Type": content_type}, body
        if path in self.raw:
            return 200, {}, self.raw[path]
        if path == METRICS_PODS:
            return 200, {}, _json(pod_metrics_list(self.pod_items))
        match = _NS_PODS.fullmatch(path)
        if match:
            chosen = [
                i for i in self.pod_items if i["metadata"]["namespace"] == match[1]
            ]
            return 200, {}, _json(pod_metrics_list(chosen))
        if path == METRICS_NODES:
            return 200, {}, _json(node_metrics_list(self.node_metrics))
        if path == CORE_NODES:
            query = {k: v[-1] for k, v in record["query"].items()}
            size = int(query.get("limit") or len(self.nodes) or 1)
            if self.node_page_size is not None:
                size = min(size, self.node_page_size)
            offset = int(query.get("continue", "tok0")[3:])
            page = self.nodes[offset : offset + size]
            following = offset + size
            token = f"tok{following}" if following < len(self.nodes) else ""
            body = {
                "kind": "NodeList",
                "apiVersion": "v1",
                "metadata": {"resourceVersion": "562", "continue": token},
                "items": page,
            }
            return 200, {}, _json(body)
        return 404, {}, status_body(404, "NotFound", "the server could not find it")

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
                    "path": unquote(parts.path),
                    "raw_query": parts.query,
                    "query": parse_qs(parts.query, keep_blank_values=True),
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": body,
                }
                fake.requests.append(record)
                status, headers, payload = fake.answer(record)
                try:
                    self.send_response(status)
                    if "Content-Type" not in headers:
                        self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    for key, value in headers.items():
                        self.send_header(key, value)
                    self.end_headers()
                    self.wfile.write(payload)
                except OSError:
                    pass  # the client gave up (deadline tests)

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

    def paths(self) -> list[str]:
        return [r["path"] for r in self.requests]


@pytest.fixture(scope="module")
def tls_pair(tmp_path_factory) -> tuple[Path, Path]:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is needed to mint the synthetic API server CA")
    folder = tmp_path_factory.mktemp("b3-tls")
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


@pytest.fixture(autouse=True)
def _clean_environment(tmp_path, monkeypatch):
    # Configuration travels in a mapping; nothing leaks in from the shell. A
    # proxy that refuses connections proves the cluster transport goes direct.
    for name in (SWITCH, *OTHER_BACKEND_ENV):
        monkeypatch.delenv(name, raising=False)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    placeholder = tmp_path / "synthetic-ca-placeholder.pem"
    placeholder.write_text("synthetic ca placeholder\n")
    monkeypatch.setenv("B3_TEST_PLACEHOLDER_CA", str(placeholder))


def _env(
    api: FakeApiServer | None = None, *, ca: Path | None = None, **overrides: str
) -> dict[str, str]:
    """A worker environment with the switch on, pointed at ``api``."""
    values = {
        PROFILE_ENV: EXT_PROFILE,
        SWITCH: "1",
        "OPSPILOT_K8S_API_URL": api.url
        if api is not None
        else "https://127.0.0.1:16443",
        "OPSPILOT_K8S_TOKEN": K8S_TOKEN,
        "OPSPILOT_K8S_CA_FILE": str(ca) if ca else os.environ["B3_TEST_PLACEHOLDER_CA"],
        "OPSPILOT_K8S_NAMESPACE": "otel-demo",
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://127.0.0.1:19090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://127.0.0.1:16686/jaeger/ui",
        "OPSPILOT_OTEL_PROMETHEUS_USERNAME": PROM_USERNAME,
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
        question="Why is checkout degraded?",
        target_id=TARGET_ID,
        deadline=deadline,
        model_requests=12,
    )


def _worker(env: Mapping[str, str], *, deadline_seconds: float = 600):
    """The executor a worker of ``env`` builds, for one fresh Run."""
    _require_tools()
    clock = FakeClock(start=NOW)
    profile = select_profile(env)
    assert isinstance(profile, ToolProfile)
    if env.get(SWITCH) == "1":
        # Positive control: refusal tests are vacuous without the tools.
        names = {s["function"]["name"] for s in profile.face(clock).tool_schemas}
        assert set(LIVE_TOOLS) <= names
    lease = _lease()
    deadline = NOW + timedelta(seconds=deadline_seconds)
    store = FakeStore(lease, deadline=deadline)
    sink = RecordingSink()
    factory = profile.executor_factory(store, sink, clock)
    return factory(lease, _input(profile, lease, clock, deadline)), sink


def _call(tool: str, params: dict[str, object] | None = None, *, index: int = 0):
    return ToolRequest(
        step_id="b3-step",
        tool_index=index,
        tool_name=tool,
        target_ref=TARGET_ID,
        params=dict(params or {}),
        window=FRAME.as_json(),
    )


def _run(api: FakeApiServer, tool: str, params: dict[str, object] | None = None, **env):
    executor, _ = _worker(_env(api, ca=_ca(api), **env))
    return executor.execute(_call(tool, params))


def _ca(api: FakeApiServer) -> Path:
    """The minted certificate every fake API server is trusted through."""
    assert api.ca is not None
    return api.ca


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


def _notes(outcome) -> list[str]:
    notes = _view(outcome).get("notes") or []
    assert isinstance(notes, list), _view(outcome)
    return [str(n) for n in notes]


def _failed(outcome, reason: str) -> None:
    assert (outcome.status, outcome.reason) == ("error", reason), _view(outcome)
    assert _view(outcome).get("content") is None, _view(outcome)


def _face(env: Mapping[str, str]):
    return select_profile(env).face(FakeClock(start=NOW))


def _tool_names(env: Mapping[str, str]) -> list[str]:
    return [s["function"]["name"] for s in _face(env).tool_schemas]


def _available(env: Mapping[str, str]) -> Mapping[str, Any]:
    return _face(env).evidence_context(RUN_ID)["data_sources"]["available"]


def _surface_sha256(profile: ToolProfile) -> str:
    face = profile.face(FakeClock(start=NOW))
    blob = {
        "tool_schemas": [dict(item) for item in face.tool_schemas],
        "variant_id": face.variant_id,
        "evidence_context": dict(face.evidence_context(RUN_ID)),
        "versions": profile.versions(),
    }
    return hashlib.sha256(canonical(blob).encode()).hexdigest()


def _backend_env(es: bool, loki: bool, vlogs: bool) -> dict[str, str]:
    env = {PROFILE_ENV: EXT_PROFILE}
    for flag, name in zip(
        (es, loki, vlogs),
        ("OPSPILOT_ES_URL", "OPSPILOT_LOKI_URL", "OPSPILOT_VICTORIALOGS_URL"),
    ):
        if flag:
            env[name] = BACKEND_URLS[name]
    return env


# -- the switch: 1 on; unset, empty, 0 off; anything else fails both starts ------------


@pytest.mark.parametrize("value", [None, "", "0"])
def test_switch_off_values_leave_the_extension_byte_identical(value):
    env = {PROFILE_ENV: EXT_PROFILE}
    if value is not None:
        env[SWITCH] = value
    revision, surface = PINNED[(False, False, False)]
    profile = select_profile(env)
    assert profile.versions()["tool_schema_revision"] == revision
    assert _surface_sha256(profile) == surface
    assert not set(LIVE_TOOLS) & set(_tool_names(env))


def test_switch_value_1_adds_both_tools():
    names = _tool_names({PROFILE_ENV: EXT_PROFILE, SWITCH: "1"})
    assert set(LIVE_TOOLS) <= set(names)


INVALID_SWITCH = ["true", "yes", "on", "2", "01", " 1", "1 ", "TRUE", "-1", "1.0"]


def _web_start(env: Mapping[str, str]) -> None:
    profile = select_profile(env)
    profile.versions()
    profile.face(FakeClock(start=NOW)).tool_schemas  # noqa: B018


@pytest.mark.parametrize("value", INVALID_SWITCH)
def test_an_invalid_switch_value_fails_the_web_start(value):
    # ASSUMPTION: the same SystemExit the other startup codes end with.
    with pytest.raises(SystemExit, match="K8S_LIVE_METRICS_INVALID"):
        _web_start({PROFILE_ENV: EXT_PROFILE, SWITCH: value})


@pytest.mark.parametrize("value", ["true", "2", " 1"])
def test_an_invalid_switch_value_fails_the_worker_start(value):
    _require_tools()
    with pytest.raises(SystemExit, match="K8S_LIVE_METRICS_INVALID"):
        profile = select_profile(_env(None, **{SWITCH: value}))
        clock = FakeClock(start=NOW)
        lease = _lease()
        deadline = NOW + timedelta(minutes=10)
        factory = profile.executor_factory(
            FakeStore(lease, deadline=deadline), RecordingSink(), clock
        )
        factory(lease, _input(profile, lease, clock, deadline))


@pytest.mark.parametrize("value", ["1", "true", "garbage"])
def test_the_switch_is_read_only_in_the_extension_profile(value):
    base = select_profile({PROFILE_ENV: "otel-demo", SWITCH: value})
    assert base.versions()["tool_schema_revision"] == BASE_TOOL_SCHEMA_REVISION
    assert _surface_sha256(base) == BASE_SURFACE_SHA256
    fixture = select_profile({SWITCH: value})
    fixture.versions()
    assert not set(LIVE_TOOLS) & {
        s["function"]["name"] for s in fixture.face(FakeClock(start=NOW)).tool_schemas
    }


# -- revisions and the tool surface ------------------------------------------------------


@pytest.mark.parametrize("backends", sorted(PINNED))
def test_switch_off_keeps_all_eight_backend_revisions(backends):
    from opspilot.tools import otel_demo_ext

    revision, surface = PINNED[backends]
    es, loki, vlogs = backends
    for off in (None, "0", ""):
        env = _backend_env(*backends)
        if off is not None:
            env[SWITCH] = off
        profile = select_profile(env)
        assert profile.versions()["tool_schema_revision"] == revision, off
        assert _surface_sha256(profile) == surface, off
    assert (
        otel_demo_ext.tool_schema_revision(
            elasticsearch=es, loki=loki, victorialogs=vlogs, live_metrics=False
        )
        == revision
    )


@pytest.mark.parametrize("backends", sorted(PINNED))
def test_switch_on_appends_the_two_tools_and_lists_them_under_kubernetes(backends):
    off = _backend_env(*backends)
    on = {**off, SWITCH: "1"}
    old_names, new_names = _tool_names(off), _tool_names(on)
    assert new_names[: len(old_names)] == old_names
    assert set(new_names[len(old_names) :]) == set(LIVE_TOOLS)
    assert len(new_names) == len(old_names) + 2
    old_available, new_available = _available(off), _available(on)
    assert set(new_available) == set(old_available)  # no new source key
    assert set(new_available["kubernetes"]) == set(old_available["kubernetes"]) | set(
        LIVE_TOOLS
    )
    for key in old_available:
        if key != "kubernetes":
            assert new_available[key] == old_available[key], key
    revision = select_profile(on).versions()["tool_schema_revision"]
    assert revision.startswith("otel-demo-ext-")
    assert revision != PINNED[backends][0]


def test_every_backend_set_with_and_without_the_switch_has_its_own_revision():
    from opspilot.tools import otel_demo_ext

    revisions = set()
    for backends in PINNED:
        for switch in ("0", "1"):
            env = {**_backend_env(*backends), SWITCH: switch}
            revision = select_profile(env).versions()["tool_schema_revision"]
            es, loki, vlogs = backends
            assert revision == otel_demo_ext.tool_schema_revision(
                elasticsearch=es,
                loki=loki,
                victorialogs=vlogs,
                live_metrics=switch == "1",
            )
            revisions.add(revision)
    assert len(revisions) == 16
    # The mapping's key order changes nothing (fixed backend order).
    forward = {**_backend_env(True, True, True), SWITCH: "1"}
    backward = dict(reversed(list(forward.items())))
    assert _surface_sha256(select_profile(forward)) == _surface_sha256(
        select_profile(backward)
    )


def test_switch_only_constants():
    from opspilot.tools import otel_demo_ext

    on = select_profile({PROFILE_ENV: EXT_PROFILE, SWITCH: "1"})
    assert (
        otel_demo_ext.LIVE_METRICS_TOOL_SCHEMA_REVISION
        == on.versions()["tool_schema_revision"]
        == otel_demo_ext.tool_schema_revision(live_metrics=True)
    )
    names = tuple(otel_demo_ext.LIVE_METRICS_TOOL_NAMES)
    base = tuple(otel_demo_ext.TOOL_NAMES)
    assert names[: len(base)] == base
    assert set(names[len(base) :]) == set(LIVE_TOOLS)
    assert otel_demo_ext.TOOL_SCHEMA_REVISION == PINNED[(False, False, False)][0]


def test_web_on_and_worker_off_record_different_versions():
    # A Run recorded by a web with the switch is blocked on claim by a worker
    # without it (C3 §5, as for B1/C5): the versions differ.
    on = select_profile({PROFILE_ENV: EXT_PROFILE, SWITCH: "1"}).versions()
    off = select_profile({PROFILE_ENV: EXT_PROFILE}).versions()
    assert on != off
    # The web (switch only) and the worker (switch and cluster) agree.
    worker = select_profile(_env(None)).versions()
    assert worker == on


def _schemas() -> dict[str, Mapping[str, Any]]:
    return {
        s["function"]["name"]: s["function"]
        for s in _face({PROFILE_ENV: EXT_PROFILE, SWITCH: "1"}).tool_schemas
    }


def test_schemas_expose_only_an_optional_namespace_and_no_time_parameters():
    schemas = _schemas()
    pods = schemas[PODS]["parameters"]
    assert set(pods.get("properties") or {}) == {"namespace"}
    assert pods["properties"]["namespace"]["type"] == "string"
    assert not pods.get("required")
    nodes = schemas[NODES]["parameters"]
    assert not (nodes.get("properties") or {})
    assert not nodes.get("required")


def test_descriptions_say_snapshot_units_and_where_to_look_instead():
    schemas = _schemas()
    for name in LIVE_TOOLS:
        text = json.dumps(schemas[name], ensure_ascii=False).lower()
        assert "cpu" in text and "memory" in text, name
        assert "millicore" in text and "byte" in text, name
        assert "prometheus" in text, name
        assert "kubernetes_tabular_query" in text, name
        assert "metrics-server" in text, name
        assert "source_error" in text, name
    # The narrowing hint for oversized results names the parameter.
    assert "namespace" in json.dumps(schemas[PODS]["description"]).lower()


def _registrations() -> dict[str, Any]:
    from opspilot.tools import otel_demo_ext

    # ASSUMPTION: the private registry seam mirrors tool_schema_revision.
    regs = otel_demo_ext._registrations(live_metrics=True)
    found = {r.name: r for r in regs if r.name in LIVE_TOOLS}
    assert set(found) == set(LIVE_TOOLS)
    assert not {r.name for r in otel_demo_ext._registrations()} & set(LIVE_TOOLS)
    return found


def test_registrations_are_bounded_cluster_reads_that_may_contain_secrets():
    for name, reg in _registrations().items():
        assert reg.read_only is True, name
        assert reg.verb == "list", name
        assert reg.source == "otel-demo", name
        assert reg.result_path == ("results",), name
        assert reg.may_contain_secrets is True, name
        assert reg.max_result_bytes == 2 * MIB, name
        assert reg.max_view_tokens == 25_000, name
        assert 0 < reg.request_timeout_seconds <= 30, name


def test_the_lab_gate_still_bounds_the_api_address():
    env = {
        PROFILE_ENV: EXT_PROFILE,
        SWITCH: "1",
        "OPSPILOT_K8S_API_URL": "https://k8s.example.invalid:6443",
    }
    assert "KUBERNETES_NOT_LAB" in check_lab_target(env)
    lab = {**env, "OPSPILOT_K8S_API_URL": "https://127.0.0.1:16443"}
    assert "KUBERNETES_NOT_LAB" not in check_lab_target(lab)


def test_entrypoint_passes_the_switch_to_the_worker(tmp_path):
    entrypoint = Path(__file__).resolve().parents[1] / "deploy" / "docker-entrypoint.sh"
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


def test_authz_probe_expects_200_for_both_metrics_lists():
    path = Path(__file__).resolve().parents[1] / "scripts" / "k8s_authz_probe.py"
    spec = importlib.util.spec_from_file_location("b3_authz_probe", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    found = {"pod": "p", "container": "c", "node": "n"}
    table = module.rows("otel-demo", found)
    allowed = {(r["method"], r["path"]) for r in table if r["expected"] == 200}
    assert ("GET", METRICS_PODS) in allowed
    assert ("GET", METRICS_NODES) in allowed


# -- requests: only GET, fixed paths ----------------------------------------------------


def _one_pod(api: FakeApiServer, namespace: str = "ns-a", name: str = "web-0") -> None:
    api.pod_items.append(pod_item(namespace, name, [("app", "250m", "64Mi")]))


def _assert_plain_get(record: Mapping[str, Any]) -> None:
    assert record["method"] == "GET", record
    assert record["body"] == b""
    assert record["headers"].get("authorization") == f"Bearer {K8S_TOKEN}"
    flat = json.dumps(record["headers"])
    assert PROM_PASSWORD not in flat


def test_pods_without_namespace_is_one_plain_get_of_the_cluster_path(api):
    _one_pod(api)
    _one_pod(api, "ns-b", "worker-0")
    outcome = _run(api, PODS)
    assert {r["namespace"] for r in _rows(outcome)} == {"ns-a", "ns-b"}
    assert len(api.requests) == 1
    record = api.requests[0]
    _assert_plain_get(record)
    assert record["path"] == METRICS_PODS
    assert record["raw_query"] == ""
    scope = _view(outcome)["scope"]
    assert scope["namespace"] is None and scope["all_namespaces"] is True


def test_pods_with_namespace_reads_only_that_namespace(api):
    _one_pod(api)
    _one_pod(api, "ns-b", "worker-0")
    outcome = _run(api, PODS, {"namespace": "ns-a"})
    assert [r["namespace"] for r in _rows(outcome)] == ["ns-a"]
    assert len(api.requests) == 1
    record = api.requests[0]
    _assert_plain_get(record)
    assert record["path"] == "/apis/metrics.k8s.io/v1beta1/namespaces/ns-a/pods"
    assert record["raw_query"] == ""
    scope = _view(outcome)["scope"]
    assert scope["namespace"] == "ns-a" and scope["all_namespaces"] is False


@pytest.mark.parametrize(
    "namespace",
    ["Ns-A", "-ns", "ns-", "ns_a", "a" * 64, "ns/../kube-system", "ns a", "ns%2Fx", 7],
)
def test_a_namespace_that_is_not_a_dns_label_is_refused_before_any_request(
    api, namespace
):
    outcome = _run(api, PODS, {"namespace": namespace})
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS"), _view(
        outcome
    )
    assert _view(outcome).get("source_contact") == "none"
    assert api.requests == []


@pytest.mark.parametrize(
    ("tool", "params"),
    [
        (PODS, {"start": "2026-11-18T10:00:00Z"}),
        (PODS, {"end": "2026-11-18T11:00:00Z"}),
        (PODS, {"label_selector": "app=web"}),
        (PODS, {"path": "/api/v1/secrets"}),
        (NODES, {"namespace": "ns-a"}),
        (NODES, {"start": "2026-11-18T10:00:00Z"}),
    ],
)
def test_parameters_outside_the_schema_never_reach_the_cluster(api, tool, params):
    outcome = _run(api, tool, params)
    assert outcome.status in ("error", "denied"), _view(outcome)
    assert api.requests == []


def _nodes_fixture(api: FakeApiServer, count: int = 5) -> None:
    for n in range(count):
        name = f"node-{n}"
        api.node_metrics.append(node_metric(name, "1000m", "1Gi"))
        api.nodes.append(node_object(name, {"cpu": "4", "memory": "8Gi"}))


def test_nodes_reads_metrics_then_pages_of_500_nodes_with_plain_gets(api):
    _nodes_fixture(api, 5)
    api.node_page_size = 2  # the server pages; the tool follows ``continue``
    outcome = _run(api, NODES)
    rows = _rows(outcome)
    assert [r["node"] for r in rows] == [f"node-{n}" for n in range(5)]
    assert all(r["cpu_percent"] == 25 for r in rows)  # every page was merged
    assert api.paths() == [METRICS_NODES, CORE_NODES, CORE_NODES, CORE_NODES]
    for record in api.requests:
        _assert_plain_get(record)
    assert api.requests[0]["raw_query"] == ""
    continues = []
    for record in api.requests[1:]:
        assert set(record["query"]) <= {"limit", "continue"}, record["query"]
        assert record["query"]["limit"] == ["500"]
        continues.append(record["query"].get("continue", [None])[0])
    assert continues == [None, "tok2", "tok4"]


def test_nodes_list_is_read_only_after_metrics_succeed(api):
    api.fail[METRICS_NODES] = (
        404,
        "application/json",
        status_body(
            404, "NotFound", "the server could not find the requested resource"
        ),
    )
    _nodes_fixture(api, 1)
    outcome = _run(api, NODES)
    _failed(outcome, "SOURCE_ERROR")
    assert api.paths() == [METRICS_NODES]


def test_redirects_are_not_followed(api):
    api.redirect = True
    outcome = _run(api, PODS)
    assert outcome.status == "error", _view(outcome)
    assert len(api.requests) == 1


# -- projection, order and units ------------------------------------------------------------


def test_pod_rows_project_the_documented_fields_sorted(api):
    api.pod_items = [
        pod_item("ns-b", "api-1", [("main", "10m", "1Mi")]),
        pod_item("ns-a", "zeta", [("z", "1m", "1Ki"), ("a", "2m", "2Ki")]),
        pod_item("ns-a", "alpha", [("only", "3m", "3Ki")]),
    ]
    rows = _rows(_run(api, PODS))
    assert [(r["namespace"], r["pod"]) for r in rows] == [
        ("ns-a", "alpha"),
        ("ns-a", "zeta"),
        ("ns-b", "api-1"),
    ]
    for row in rows:
        assert set(row) == {
            "namespace",
            "pod",
            "cpu_millicores",
            "memory_bytes",
            "timestamp",
            "window",
            "containers",
        }, row
        for container in row["containers"]:
            assert set(container) == {"name", "cpu_millicores", "memory_bytes"}
        assert row["timestamp"] == SAMPLE_TS
        assert row["window"] == WINDOW_TEXT
    assert [c["name"] for c in rows[1]["containers"]] == ["a", "z"]
    assert rows[1]["containers"] == [
        {"name": "a", "cpu_millicores": 2, "memory_bytes": 2048},
        {"name": "z", "cpu_millicores": 1, "memory_bytes": 1024},
    ]
    assert (rows[1]["cpu_millicores"], rows[1]["memory_bytes"]) == (3, 3072)


def test_pod_view_counts_its_rows(api):
    api.pod_items = [pod_item("ns-a", f"p{n}", [("c", "1m", "1Ki")]) for n in range(3)]
    outcome = _run(api, PODS)
    assert len(_rows(outcome)) == 3
    assert _view(outcome)["backend_pods_returned"] == 3


CPU_TABLE = [
    ("250m", 250),
    ("0", 0),
    ("0m", 0),
    ("1", 1000),
    ("2", 2000),
    ("0.5", 500),
    ("1.0001", 1001),
    ("123456789n", 124),
    ("1n", 1),
    ("1000000n", 1),
    ("1000001n", 2),
    ("1500u", 2),
    ("1000u", 1),
    ("1e-3", 1),
    ("2.5e-1", 250),
    ("1e3", 1_000_000),
    ("1k", 1_000_000),
    ("1Ki", 1_024_000),
]


@pytest.mark.parametrize(("quantity", "millicores"), CPU_TABLE)
def test_cpu_quantities_are_millicores_rounded_up(api, quantity, millicores):
    api.pod_items = [pod_item("ns-a", "p", [("c", quantity, "1Ki")])]
    (row,) = _rows(_run(api, PODS))
    assert row["containers"][0]["cpu_millicores"] == millicores
    assert row["cpu_millicores"] == millicores
    assert type(row["cpu_millicores"]) is int


MEMORY_TABLE = [
    ("45678Ki", 46_774_272),
    ("1Ki", 1024),
    ("1Mi", 1_048_576),
    ("1Gi", 1_073_741_824),
    ("1Ti", 1_099_511_627_776),
    ("1Pi", 1_125_899_906_842_624),
    ("1Ei", 1_152_921_504_606_846_976),
    ("1k", 1000),
    ("1M", 1_000_000),
    ("1G", 1_000_000_000),
    ("1T", 10**12),
    ("1P", 10**15),
    ("1E", 10**18),
    ("123456789", 123_456_789),
    ("0", 0),
    ("1.5Ki", 1536),
    ("1.5", 2),
    ("100m", 1),
    ("1500m", 2),
    ("12345n", 1),
    ("7u", 1),
    ("1e3", 1000),
    ("1.5e3", 1500),
    ("5e-1", 1),
]


@pytest.mark.parametrize(("quantity", "size"), MEMORY_TABLE)
def test_memory_quantities_are_bytes_rounded_up(api, quantity, size):
    api.pod_items = [pod_item("ns-a", "p", [("c", "1m", quantity)])]
    (row,) = _rows(_run(api, PODS))
    assert row["containers"][0]["memory_bytes"] == size
    assert row["memory_bytes"] == size
    assert type(row["memory_bytes"]) is int


def test_pod_totals_round_the_exact_container_sum_up_once(api):
    api.pod_items = [
        pod_item(
            "ns-a",
            "three-nano",
            [("a", "1n", "1Ki"), ("b", "1n", "100"), ("c", "1n", "2Mi")],
        ),
        pod_item(
            "ns-a",
            "thirds",
            [("a", "333334u", "1"), ("b", "333334u", "1"), ("c", "333334u", "1")],
        ),
        pod_item("ns-a", "half-bytes", [("a", "1m", "500m"), ("b", "1m", "500m")]),
    ]
    rows = {r["pod"]: r for r in _rows(_run(api, PODS))}
    nano = rows["three-nano"]
    assert [c["cpu_millicores"] for c in nano["containers"]] == [1, 1, 1]
    assert nano["cpu_millicores"] == 1  # ceil(3n), not 1 + 1 + 1
    assert nano["memory_bytes"] == 1024 + 100 + 2 * 1_048_576
    assert nano["memory_bytes"] == sum(c["memory_bytes"] for c in nano["containers"])
    thirds = rows["thirds"]
    assert [c["cpu_millicores"] for c in thirds["containers"]] == [334, 334, 334]
    assert thirds["cpu_millicores"] == 1001  # ceil(1000.002)
    assert thirds["memory_bytes"] == 3
    for row in rows.values():
        summed = sum(c["cpu_millicores"] for c in row["containers"])
        assert 0 <= summed - row["cpu_millicores"] < len(row["containers"])
    assert [c["memory_bytes"] for c in rows["half-bytes"]["containers"]] == [1, 1]
    assert rows["half-bytes"]["memory_bytes"] == 1  # ceil(0.5 + 0.5)


MALFORMED_QUANTITIES = [
    "abc",
    "",
    "1.2.3",
    "Ki",
    "1 Ki",
    "1KI",
    "1mi",
    "0x10",
    "1Kib",
    "--1",
    "1e",
    "1ee3",
    "12Q",
]


@pytest.mark.parametrize("bad", MALFORMED_QUANTITIES)
@pytest.mark.parametrize("field", ["cpu", "memory"])
def test_an_unparseable_quantity_fails_the_whole_call(api, bad, field):
    good = pod_item("ns-a", "fine", [("c", "1m", "1Ki")])
    usage = {"cpu": "1m", "memory": "1Ki", field: bad}
    broken = pod_item("ns-a", "broken", [("c", usage["cpu"], usage["memory"])])
    api.pod_items = [good, broken]
    _failed(_run(api, PODS), "MALFORMED_RESULT")


def _with(item: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {**item, **changes}


_GOOD_POD = pod_item("ns-a", "p", [("c", "1m", "1Ki")])

MALFORMED_POD_BODIES = {
    "not json": b"<html>metrics</html>",
    "a json array": _json([_GOOD_POD]),
    "items not a list": _json({"kind": "PodMetricsList", "items": {"p": 1}}),
    "item not an object": _json(pod_metrics_list(["p"])),  # type: ignore[list-item]
    "no metadata name": _json(
        pod_metrics_list([_with(_GOOD_POD, metadata={"namespace": "ns-a"})])
    ),
    "containers not a list": _json(
        pod_metrics_list([_with(_GOOD_POD, containers={"c": {}})])
    ),
    "container without usage": _json(
        pod_metrics_list([_with(_GOOD_POD, containers=[{"name": "c"}])])
    ),
    "usage without cpu": _json(
        pod_metrics_list(
            [_with(_GOOD_POD, containers=[{"name": "c", "usage": {"memory": "1Ki"}}])]
        )
    ),
    "usage without memory": _json(
        pod_metrics_list(
            [_with(_GOOD_POD, containers=[{"name": "c", "usage": {"cpu": "1m"}}])]
        )
    ),
    "quantity not a string": _json(
        pod_metrics_list(
            [
                _with(
                    _GOOD_POD,
                    containers=[{"name": "c", "usage": {"cpu": None, "memory": "1Ki"}}],
                )
            ]
        )
    ),
    "no timestamp": _json(
        pod_metrics_list([pod_item("ns-a", "p", [("c", "1m", "1Ki")], timestamp=None)])
    ),
    "bad timestamp": _json(
        pod_metrics_list(
            [pod_item("ns-a", "p", [("c", "1m", "1Ki")], timestamp="yesterday")]
        )
    ),
    "numeric timestamp": _json(
        pod_metrics_list([pod_item("ns-a", "p", [("c", "1m", "1Ki")], timestamp=5)])
    ),
    "no window": _json(
        pod_metrics_list([pod_item("ns-a", "p", [("c", "1m", "1Ki")], window=None)])
    ),
    "window not a string": _json(
        pod_metrics_list([pod_item("ns-a", "p", [("c", "1m", "1Ki")], window=15)])
    ),
}


@pytest.mark.parametrize(
    "body", MALFORMED_POD_BODIES.values(), ids=list(MALFORMED_POD_BODIES)
)
def test_a_body_that_is_not_a_pod_metrics_list_is_malformed(api, body):
    api.raw[METRICS_PODS] = body
    _failed(_run(api, PODS), "MALFORMED_RESULT")


@pytest.mark.parametrize(
    ("given", "normalized"),
    [
        ("2026-11-18T12:00:37Z", "2026-11-18T12:00:37Z"),
        ("2026-11-18T04:00:37-08:00", "2026-11-18T12:00:37Z"),
        ("2026-11-18T12:00:37+00:00", "2026-11-18T12:00:37Z"),
        ("2026-11-18T12:00:37.250Z", "2026-11-18T12:00:37Z"),
    ],
)
def test_timestamps_are_rfc3339_z_to_the_second_and_window_is_verbatim(
    api, given, normalized
):
    api.pod_items = [
        pod_item("ns-a", "p", [("c", "1m", "1Ki")], timestamp=given, window="9.5s")
    ]
    (row,) = _rows(_run(api, PODS))
    assert row["timestamp"] == normalized
    assert row["window"] == "9.5s"


def test_node_rows_percentages_round_down_and_may_be_zero_or_over_100(api):
    api.node_metrics = [
        node_metric("n-mid", "1500m", "3Gi"),
        node_metric("n-busy", "5", "20Gi"),
        node_metric("n-idle", "1m", "1Ki"),
        node_metric("n-odd", "1950m", "1Gi"),
    ]
    api.nodes = [
        node_object("n-busy", {"cpu": "4", "memory": "16Gi"}),
        node_object("n-idle", {"cpu": "8", "memory": "16Gi"}),
        node_object("n-mid", {"cpu": "4", "memory": "16Gi"}),
        node_object("n-odd", {"cpu": "3900m", "memory": "3Gi"}),
    ]
    outcome = _run(api, NODES)
    rows = _rows(outcome)
    assert [r["node"] for r in rows] == ["n-busy", "n-idle", "n-mid", "n-odd"]
    for row in rows:
        assert set(row) == {
            "node",
            "cpu_millicores",
            "cpu_percent",
            "memory_bytes",
            "memory_percent",
            "timestamp",
            "window",
        }, row
        assert row["timestamp"] == SAMPLE_TS and row["window"] == WINDOW_TEXT
    by = {r["node"]: r for r in rows}
    assert (by["n-mid"]["cpu_millicores"], by["n-mid"]["memory_bytes"]) == (
        1500,
        3 * 1_073_741_824,
    )
    assert (by["n-mid"]["cpu_percent"], by["n-mid"]["memory_percent"]) == (37, 18)
    assert (by["n-busy"]["cpu_percent"], by["n-busy"]["memory_percent"]) == (125, 125)
    assert (by["n-idle"]["cpu_percent"], by["n-idle"]["memory_percent"]) == (0, 0)
    assert (by["n-odd"]["cpu_percent"], by["n-odd"]["memory_percent"]) == (50, 33)
    for row in rows:
        assert type(row["cpu_percent"]) is int and type(row["memory_percent"]) is int
    assert _view(outcome)["backend_nodes_returned"] == 4


def test_missing_or_zero_allocatable_gives_null_percentages_with_a_note(api):
    api.node_metrics = [
        node_metric("ghost", "100m", "1Gi"),
        node_metric("no-cpu", "100m", "1Gi"),
        node_metric("zero", "100m", "1Gi"),
        node_metric("no-alloc", "100m", "1Gi"),
    ]
    api.nodes = [
        node_object("no-cpu", {"memory": "4Gi"}),
        node_object("zero", {"cpu": "0", "memory": "0"}),
        node_object("no-alloc", None),
    ]
    outcome = _run(api, NODES)
    by = {r["node"]: r for r in _rows(outcome)}
    assert (by["ghost"]["cpu_percent"], by["ghost"]["memory_percent"]) == (None, None)
    assert by["no-cpu"]["cpu_percent"] is None
    assert by["no-cpu"]["memory_percent"] == 25
    assert (by["zero"]["cpu_percent"], by["zero"]["memory_percent"]) == (None, None)
    assert (by["no-alloc"]["cpu_percent"], by["no-alloc"]["memory_percent"]) == (
        None,
        None,
    )
    for row in by.values():
        assert row["cpu_millicores"] == 100 and row["memory_bytes"] == 1_073_741_824
    notes = " ".join(_notes(outcome)[1:])
    for name in ("ghost", "no-cpu", "zero", "no-alloc"):
        assert name in notes, (name, notes)


def test_nodes_without_usage_samples_are_counted_in_notes(api):
    api.node_metrics = [node_metric("sampled", "100m", "1Gi")]
    api.nodes = [
        node_object("sampled", {"cpu": "1", "memory": "4Gi"}),
        node_object("fresh-1", {"cpu": "1", "memory": "4Gi"}),
        node_object("fresh-2", {"cpu": "1", "memory": "4Gi"}),
    ]
    outcome = _run(api, NODES)
    assert [r["node"] for r in _rows(outcome)] == ["sampled"]
    later = _notes(outcome)[1:]
    assert any(re.search(r"\b2\b", n) and "node" in n.lower() for n in later), later


@pytest.mark.parametrize("bad", ["bogus", "1KI", ""])
def test_an_unparseable_allocatable_fails_the_whole_call(api, bad):
    api.node_metrics = [node_metric("n", "100m", "1Gi")]
    api.nodes = [node_object("n", {"cpu": bad, "memory": "4Gi"})]
    _failed(_run(api, NODES), "MALFORMED_RESULT")


MALFORMED_NODE_BODIES = {
    "not json": b"metrics unavailable",
    "items not a list": _json({"kind": "NodeMetricsList", "items": "n"}),
    "no usage": _json(
        node_metrics_list(
            [{"metadata": {"name": "n"}, "timestamp": SAMPLE_TS, "window": "1s"}]
        )
    ),
    "bad cpu": _json(node_metrics_list([node_metric("n", "lots", "1Gi")])),
    "bad timestamp": _json(
        node_metrics_list([node_metric("n", "1m", "1Gi", timestamp="now")])
    ),
    "no window": _json(node_metrics_list([node_metric("n", "1m", "1Gi", window=None)])),
}


@pytest.mark.parametrize(
    "body", MALFORMED_NODE_BODIES.values(), ids=list(MALFORMED_NODE_BODIES)
)
def test_a_body_that_is_not_a_node_metrics_list_is_malformed(api, body):
    api.raw[METRICS_NODES] = body
    api.nodes = [node_object("n", {"cpu": "1", "memory": "4Gi"})]
    _failed(_run(api, NODES), "MALFORMED_RESULT")


def test_raw_quantity_strings_and_labels_stay_out_of_view_and_evidence(api):
    api.pod_items = [
        pod_item(
            "ns-a",
            "p",
            [("c", "123456789n", "45678Ki")],
            labels={"team": CANARY},
        )
    ]
    executor, sink = _worker(_env(api, ca=_ca(api)))
    outcome = executor.execute(_call(PODS))
    rows = _rows(outcome)
    assert rows[0]["cpu_millicores"] == 124 and rows[0]["memory_bytes"] == 46_774_272
    raw = outcome.evidence.raw.decode()
    for text in (_text(outcome), raw, repr(sink.records)):
        assert "123456789n" not in text
        assert "45678Ki" not in text
        assert CANARY not in text
        assert "labels" not in text
    # The evidence body is the projection the view shows.
    assert json.loads(raw)["results"] == rows


def test_empty_results_are_no_data(api):
    outcome = _run(api, PODS)
    assert (outcome.status, outcome.reason) == ("no_data", "NO_DATA"), _view(outcome)
    outcome = _run(api, PODS, {"namespace": "nothing-here"})
    assert (outcome.status, outcome.reason) == ("no_data", "NO_DATA"), _view(outcome)
    api.nodes = [node_object("n", {"cpu": "1", "memory": "4Gi"})]
    outcome = _run(api, NODES)
    assert (outcome.status, outcome.reason) == ("no_data", "NO_DATA"), _view(outcome)


# -- time: current-state class (B3-U1=a) ---------------------------------------------------

_FIRST_NOTE = re.compile(
    r"Sampled at (\S+)–(\S+): current usage, (\d+) s (after|before) the end of "
    r"the authorized window \((.+)\)\."
)


def _first_note(outcome) -> re.Match[str]:
    notes = _notes(outcome)
    assert notes, _view(outcome)
    match = _FIRST_NOTE.fullmatch(notes[0])
    assert match, notes[0]
    return match


@pytest.mark.parametrize("tool", LIVE_TOOLS)
def test_source_times_are_empty_and_rows_carry_their_sampling_instant(api, tool):
    early, late = _z(NOW + timedelta(seconds=30)), _z(NOW + timedelta(seconds=45))
    api.pod_items = [
        pod_item("ns-a", "a", [("c", "1m", "1Ki")], timestamp=late),
        pod_item("ns-a", "b", [("c", "1m", "1Ki")], timestamp=early),
    ]
    api.node_metrics = [
        node_metric("n1", "1m", "1Ki", timestamp=early),
        node_metric("n2", "1m", "1Ki", timestamp=late),
    ]
    api.nodes = [node_object(n, {"cpu": "1", "memory": "1Gi"}) for n in ("n1", "n2")]
    executor, sink = _worker(_env(api, ca=_ca(api)))
    outcome = executor.execute(_call(tool))
    view = _view(outcome)
    assert outcome.status == "ok", view
    assert view["source_start_at"] is None
    assert view["source_end_at"] is None
    assert view["data_as_of"] is None
    record = outcome.evidence
    assert record.source_start_at is None and record.source_end_at is None
    assert record.data_as_of is None
    assert view["sampled_at"] == {"earliest": early, "latest": late}
    match = _first_note(outcome)
    assert (match[1], match[2]) == (early, late)
    end = _instant(view["window"]["end"])
    assert _instant(match[5]) == end
    assert match[4] == "after"
    # Which sample N is measured from is not pinned: earliest or latest.
    assert int(match[3]) in {
        int((_instant(early) - end).total_seconds()),
        int((_instant(late) - end).total_seconds()),
    }


@pytest.mark.parametrize(
    ("offset", "word", "seconds"), [(37, "after", 37), (-12, "before", 12)]
)
def test_first_note_says_how_far_the_sample_is_from_the_window_end(
    api, offset, word, seconds
):
    stamp = _z(NOW + timedelta(seconds=offset))
    api.pod_items = [pod_item("ns-a", "p", [("c", "1m", "1Ki")], timestamp=stamp)]
    outcome = _run(api, PODS)
    assert _instant(_view(outcome)["window"]["end"]) == NOW  # the frame's end
    match = _first_note(outcome)
    assert (match[1], match[2]) == (stamp, stamp)
    assert (int(match[3]), match[4]) == (seconds, word)
    assert _view(outcome)["sampled_at"] == {"earliest": stamp, "latest": stamp}


def test_cannot_prove_is_one_fixed_text_per_tool(api):
    api.pod_items = [pod_item("ns-a", "p", [("c", "1m", "1Ki")])]
    first = _view(_run(api, PODS))["cannot_prove"]
    api.pod_items = [
        pod_item(
            "ns-b",
            "q",
            [("c", "900m", "3Gi")],
            timestamp=_z(NOW - timedelta(seconds=5)),
        )
    ]
    second = _view(_run(api, PODS))["cannot_prove"]
    assert isinstance(first, str) and first == second
    api.node_metrics = [node_metric("n", "1m", "1Ki")]
    api.nodes = [node_object("n", {"cpu": "1", "memory": "1Gi"})]
    node_text = _view(_run(api, NODES))["cannot_prove"]
    for text in (first, node_text):
        lower = text.lower()
        assert "prometheus" in lower, text  # incident-window changes
        assert "window" in lower, text  # not the usage in the incident window
        assert "oom" in lower, text  # high usage is not throttling or OOM
        assert "kubernetes_tabular_query" in text, text  # requests and limits
    assert "pod" in first.lower()  # a missing Pod is not proof it is not running


def test_report_validation_accepts_a_fact_citing_live_usage(api):
    from opspilot.investigation.context import delivered_view
    from opspilot.investigation.reports import ReportV2, unsupported_citations

    api.pod_items = [pod_item("ns-a", "p", [("c", "900m", "3Gi")])]
    env = _env(api, ca=_ca(api))
    executor, _ = _worker(env)
    outcome = executor.execute(_call(PODS))
    assert outcome.status == "ok", _view(outcome)
    context = select_profile(env).face(FakeClock(start=NOW)).evidence_context(RUN_ID)
    policy_ids = [p["id"] for p in context["time_policies"]]
    view = delivered_view(
        dict(outcome.evidence.view),
        evidence_context=context,
        authorized_targets=frozenset({TARGET_ID}),
    )
    assert view is not None and view.citable_as_fact is True
    bound = sorted(view.time_scope_refs & set(policy_ids))
    assert bound, (view.time_scope_refs, policy_ids)
    report = ReportV2.model_validate(
        {
            "schema_version": "m0-report-v2",
            "assessment_status": "completed",
            "conclusion": "supported",
            "summary": "checkout pod uses 900 millicores now",
            "claims": [
                {
                    "kind": "fact",
                    "text": "pod p in ns-a uses 900 millicores at sampling time",
                    "evidence_ids": [view.evidence_id],
                    "target_refs": [TARGET_ID],
                    "time_scope_ref": bound[0],
                }
            ],
            "gaps": [],
        }
    )
    assert not unsupported_citations(
        report,
        views=[view],
        authorized_targets=frozenset({TARGET_ID}),
        time_policy_ids=policy_ids,
    )


# -- error classes (B3-8) ---------------------------------------------------------------------

STATUS_CASES = [
    (400, "BadRequest", "INVALID_PARAMS"),
    (401, "Unauthorized", "SOURCE_FORBIDDEN"),
    (403, "Forbidden", "SOURCE_FORBIDDEN"),
    (404, "NotFound", "SOURCE_ERROR"),
    (429, "TooManyRequests", "SOURCE_UNAVAILABLE"),
    (500, "InternalError", "SOURCE_UNAVAILABLE"),
    (503, "ServiceUnavailable", "SOURCE_UNAVAILABLE"),
]


def _fail(api: FakeApiServer, path: str, status: int, reason: str) -> None:
    message = {
        503: "the server is currently unable to handle the request "
        "(get pods.metrics.k8s.io)",
        404: "the server could not find the requested resource",
    }.get(status, f"synthetic {reason}")
    api.fail[path] = (status, "application/json", status_body(status, reason, message))


def _says_no_metrics_api(outcome) -> bool:
    # The API server's own 404 text names neither; only the tool's note does.
    text = _text(outcome).lower()
    return "metrics-server" in text or "metrics.k8s.io" in text


@pytest.mark.parametrize(("status", "k8s_reason", "reason"), STATUS_CASES)
def test_pod_statuses_map_to_fixed_reasons_with_one_request(
    api, status, k8s_reason, reason
):
    _fail(api, METRICS_PODS, status, k8s_reason)
    outcome = _run(api, PODS)
    _failed(outcome, reason)
    assert len(api.requests) == 1  # no retry
    if status == 404:
        assert _says_no_metrics_api(outcome), _view(outcome)


@pytest.mark.parametrize(("status", "k8s_reason", "reason"), STATUS_CASES)
def test_node_metrics_statuses_map_to_the_same_reasons(api, status, k8s_reason, reason):
    _fail(api, METRICS_NODES, status, k8s_reason)
    api.nodes = [node_object("n", {"cpu": "1", "memory": "1Gi"})]
    outcome = _run(api, NODES)
    _failed(outcome, reason)
    assert api.paths() == [METRICS_NODES]
    if status == 404:
        assert _says_no_metrics_api(outcome), _view(outcome)


@pytest.mark.parametrize(
    ("status", "k8s_reason", "reason"),
    [
        (403, "Forbidden", "SOURCE_FORBIDDEN"),
        (503, "ServiceUnavailable", "SOURCE_UNAVAILABLE"),
        (400, "BadRequest", "INVALID_PARAMS"),
    ],
)
def test_a_failed_nodes_list_fails_the_whole_node_call(api, status, k8s_reason, reason):
    api.node_metrics = [node_metric("n", "1m", "1Ki")]
    _fail(api, CORE_NODES, status, k8s_reason)
    _failed(_run(api, NODES), reason)
    assert api.paths() == [METRICS_NODES, CORE_NODES]


def test_an_unreachable_api_server_is_source_unavailable(api):
    ca = _ca(api)
    api.stop()
    executor, _ = _worker(_env(api, ca=ca))
    _failed(executor.execute(_call(PODS)), "SOURCE_UNAVAILABLE")


def test_a_read_past_the_call_deadline_is_a_timeout(api):
    _one_pod(api)
    api.delay = 4.0
    executor, _ = _worker(_env(api, ca=_ca(api)), deadline_seconds=2)
    started = time.monotonic()
    outcome = executor.execute(_call(PODS))
    assert (outcome.status, outcome.reason) == ("timeout", "TOOL_TIMEOUT"), _view(
        outcome
    )
    assert time.monotonic() - started < 3.9


def test_an_untrusted_certificate_reads_nothing(api):
    _one_pod(api)
    executor, _ = _worker(_env(api))  # the placeholder CA, not the minted one
    outcome = executor.execute(_call(PODS))
    assert outcome.status == "error", _view(outcome)
    assert api.requests == []


# -- size limits (whole refusal, never a partial result) -------------------------------------


def test_a_pod_metrics_read_over_4_mib_is_refused_whole(api):
    api.pod_items = [
        pod_item("ns-a", "p", [("c", "1m", "1Ki")], labels={"pad": "x" * (4 * MIB)})
    ]
    _failed(_run(api, PODS), "RESULT_TOO_LARGE")


def test_the_node_tool_shares_one_4_mib_read_budget(api):
    pad = "y" * int(2.5 * MIB)
    api.node_metrics = [node_metric("n", "1m", "1Ki")]
    api.raw[METRICS_NODES] = _json(
        {**node_metrics_list(api.node_metrics), "metadata": {"pad": pad}}
    )
    api.nodes = [node_object("n", {"cpu": "1", "memory": "1Gi"})]
    # Control: the padded metrics read alone fits.
    assert _run(api, NODES).status == "ok"
    api.requests.clear()
    api.nodes = [node_object("n", {"cpu": "1", "memory": "1Gi"}, padding=pad)]
    _failed(_run(api, NODES), "RESULT_TOO_LARGE")


def _container_name(pod: int, index: int) -> str:
    # Hex text: dense in tokens, unlike a run of one letter.
    digest = hashlib.sha256(f"{pod}-{index}".encode()).hexdigest()
    return f"c{index:03d}-{digest[:58]}"


def _many_pods(count: int, containers: int) -> list[dict[str, Any]]:
    return [
        pod_item(
            "ns-a",
            f"pod-{n:05d}",
            [(_container_name(n, j), "1m", "1Ki") for j in range(containers)],
            labels={},
        )
        for n in range(count)
    ]


def test_a_projection_over_2_mib_is_refused_whole(api):
    # ~110 projected bytes per container, raw below the 4 MiB read cap.
    api.pod_items = _many_pods(200, 110)
    body = _json(pod_metrics_list(api.pod_items))
    assert 2 * MIB < len(body) < 4 * MIB
    _failed(_run(api, PODS), "RESULT_TOO_LARGE")


def test_a_view_over_25k_tokens_is_refused_whole(api):
    api.pod_items = _many_pods(60, 20)  # well under 2 MiB projected
    assert len(_json(pod_metrics_list(api.pod_items))) < MIB
    _failed(_run(api, PODS), "RESULT_TOO_LARGE")
    api.pod_items = _many_pods(3, 2)  # control: a small answer is shown
    assert len(_rows(_run(api, PODS))) == 3
