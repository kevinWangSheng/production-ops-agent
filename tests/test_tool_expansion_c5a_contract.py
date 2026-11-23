"""Independent contract tests for tool expansion C5a (#569): the Loki log
search tool ``grafana_loki_query``.

Written from the C5 task record (docs/tasks/2026-11-18-tool-expansion-c5.md),
decision table r10 (C5-U1, C5-0 ... C5-15) with B1-U1/B1-U2 from r9, and the
C5 pre-review (Loki parts of C5-0 ... C5-15), without reading the
implementation. Red until the implementation lands; collection must stay
green.

Every test goes through the public path: build the ``otel-demo-ext`` profile
from an environment mapping, call ``grafana_loki_query`` through the
executor, and assert on the returned view, the refusal code, or what a local
``http.server`` fake Loki received. No container, no real backend, no model.

Seams assumed beyond r10 (each also marked ``# ASSUMPTION:`` where used):

* ``select_profile(env)`` reads ``OPSPILOT_LOKI_URL`` from the mapping it is
  given to decide the extension face (B1-U2, C5-14); the worker reads the
  Loki credentials from the same mapping, as B1 does for ``OPSPILOT_ES_*``.
* ``OPSPILOT_LOKI_BEARER_TOKEN`` is sent as ``Authorization: Bearer <v>``
  and the user name and password as RFC 7617 ``Basic``.
* Pre-request refusal codes (``QUERY_OUT_OF_WINDOW``, ``INVALID_QUERY``,
  ``INVALID_LIMIT``) surface as ``INVALID_PARAMS`` with the code at the start
  of the first ``notes`` entry, as every existing transport refusal does.
* The stream view's ``content`` is the list of ``{timestamp, labels, line}``
  rows and the matrix view's ``content`` the list of ``{metric, values}``
  rows (C5-10, executor ``content`` convention).
* The Loki ``start``/``end`` query parameters may be RFC3339 or Unix epoch
  (nanoseconds or fractional seconds); the fake accepts all three, as Loki
  does.

All synthetic credentials and canaries are low-entropy placeholders.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
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

from opspilot import tracing
from opspilot.tools import ToolRequest
from opspilot.tools.outcomes import Window
from opspilot.tools.profiles import PROFILE_ENV, ToolProfile, select_profile
from opspilot.tools.registry import canonical
from opspilot.tracing import check_lab_target
from tests.m1_tool_support import FakeClock, RecordingSink
from tests.test_m1_otel_demo_contract import NOW, TARGET_ID, FakeStore, _lease

EXT_PROFILE = "otel-demo-ext"
LOKI = "grafana_loki_query"
QUERY_PATH = "/loki/api/v1/query_range"

LOKI_BEARER = "loki-ro-aaaaaaaa569"
LOKI_USERNAME = "loki-reader"
LOKI_PASSWORD = "bbbbbbbb569"
ES_API_KEY = "es-ro-cccccccc569"
PROM_USERNAME = "investigator"
PROM_PASSWORD = "dddddddd569"
K8S_TOKEN = "eeeeeeee569"
K8S_API = "https://127.0.0.1:16443"
CANARY = "ffffffff569"
TENANT = "team-checkout"
RUN_ID = "c5a-run"

# Pinned on main 6110b966 (B1-U2 / C5-14), computed with ``_surface_sha256``
# below against main before any C5 code existed: otel-demo, otel-demo-ext
# with no log backend, and otel-demo-ext with only OPSPILOT_ES_URL must stay
# byte-identical when C5a lands.
BASE_TOOL_SCHEMA_REVISION = "otel-demo-4c0e69ef2e8b"
EXT_TOOL_SCHEMA_REVISION = "otel-demo-ext-d44b47f8618a"
ES_TOOL_SCHEMA_REVISION = "otel-demo-ext-dfa67abdb45d"
BASE_SURFACE_SHA256 = "ec62fdabb8153c6555e3749f42b0f9d926c8cacb5c7c648810956442bb3efbb8"
EXT_SURFACE_SHA256 = "d24716ddc9b6558a48a5926f91d61cdc928b8ed3ac496cd538c4ccf79df78453"
ES_SURFACE_SHA256 = "73b9be4f4d9922d5f64c18719fd8fb9ea7b0c34088fc1c398257d23476d97738"

LOKI_ENV_NAMES = (
    "OPSPILOT_LOKI_URL",
    "OPSPILOT_LOKI_USERNAME",
    "OPSPILOT_LOKI_PASSWORD",
    "OPSPILOT_LOKI_BEARER_TOKEN",
    "OPSPILOT_LOKI_CA_FILE",
    "OPSPILOT_LOKI_TENANT_ID",
)
LOKI_CREDENTIAL_NAMES = (
    "OPSPILOT_LOKI_USERNAME",
    "OPSPILOT_LOKI_PASSWORD",
    "OPSPILOT_LOKI_BEARER_TOKEN",
    "OPSPILOT_LOKI_CA_FILE",
)
ALLOWED_QUERY_KEYS = {"query", "start", "end", "limit", "direction"}

# The call's authorized frame; the default query window is its last hour.
FRAME = Window(NOW - timedelta(hours=6), NOW)
DEFAULT_START = NOW - timedelta(hours=1)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


# -- time helpers ----------------------------------------------------------------


def _z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _ns(moment: datetime, extra_ns: int = 0) -> int:
    return (moment - _EPOCH) // timedelta(microseconds=1) * 1000 + extra_ns


_RFC3339 = re.compile(
    r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)"
)


def _rfc3339_ns(text: str) -> int:
    """Nanoseconds since the epoch of an RFC3339 timestamp (up to 9 digits)."""
    match = _RFC3339.fullmatch(text)
    assert match, f"not RFC3339 with a timezone: {text!r}"
    zone = "+00:00" if match.group(3) == "Z" else match.group(3)
    whole = datetime.fromisoformat(match.group(1) + zone)
    fraction = (match.group(2) or "").ljust(9, "0")
    return _ns(whole) + int(fraction)


def _loki_time_ns(text: str) -> int:
    """A Loki ``start``/``end`` value: RFC3339, Unix ns, or Unix seconds."""
    if re.fullmatch(r"\d+", text):
        value = int(text)
        return value if value > 10**14 else value * 10**9
    if re.fullmatch(r"\d+\.\d+", text):
        whole, fraction = text.split(".")
        return int(whole) * 10**9 + int(fraction.ljust(9, "0")[:9])
    return _rfc3339_ns(text)


def _instant(value: object) -> datetime:
    assert isinstance(value, str), value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _close(ns_a: int, ns_b: int, tolerance_ns: int = 1000) -> bool:
    return abs(ns_a - ns_b) <= tolerance_ns


# -- local Loki fake -------------------------------------------------------------


class FakeLoki:
    """A loopback HTTP(S) server answering ``query_range`` in Loki's JSON
    shapes, honouring ``start``/``end``/``limit``/``direction`` as Loki does
    (``direction`` defaults to ``backward``), and recording every request."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.entries: list[tuple[dict[str, str], int, str]] = []  # labels, ns, line
        self.matrix: list[dict[str, Any]] | None = None
        self.raw: bytes | None = None
        self.fail: tuple[int, str, bytes] | None = None  # status, type, body
        self.delay = 0.0
        self.redirect = False
        self.prefix = ""  # a gateway / Grafana proxy base path
        self._server: ThreadingHTTPServer | None = None
        self.url = ""

    def add(self, labels: Mapping[str, str], when_ns: int, line: str) -> None:
        self.entries.append((dict(labels), when_ns, line))

    def answer(self, record: dict[str, Any]) -> tuple[int, dict[str, str], bytes]:
        if self.delay:
            time.sleep(self.delay)
        if self.redirect:
            return 302, {"Location": "/elsewhere"}, b""
        if self.fail is not None:
            status, content_type, body = self.fail
            return status, {"Content-Type": content_type}, body
        if record["method"] != "GET" or record["path"] != self.prefix + QUERY_PATH:
            return 404, {"Content-Type": "text/plain"}, b"404 page not found\n"
        if self.raw is not None:
            return 200, {}, self.raw
        query = {k: v[-1] for k, v in record["query"].items()}
        if self.matrix is not None:
            payload = {"resultType": "matrix", "result": self.matrix}
            return 200, {}, _json({"status": "success", "data": payload})
        start = _loki_time_ns(query["start"])
        end = _loki_time_ns(query["end"])
        limit = int(query.get("limit", "100"))
        backward = query.get("direction", "backward") == "backward"
        chosen = [e for e in self.entries if start <= e[1] <= end]
        chosen.sort(key=lambda e: e[1], reverse=backward)
        chosen = chosen[:limit]
        streams: dict[str, dict[str, Any]] = {}
        for labels, when, line in chosen:
            key = json.dumps(labels, sort_keys=True)
            stream = streams.setdefault(key, {"stream": labels, "values": []})
            stream["values"].append([str(when), line])
        payload = {
            "resultType": "streams",
            "result": list(streams.values()),
            "stats": {"summary": {"totalEntriesReturned": len(chosen)}},
        }
        return 200, {}, _json({"status": "success", "data": payload})

    def start(self, tls: tuple[Path, Path] | None = None) -> None:
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
        scheme = "http"
        if tls is not None:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(str(tls[0]), str(tls[1]))
            server.socket = context.wrap_socket(server.socket, server_side=True)
            scheme = "https"
        self._server = server
        threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        ).start()
        self.url = f"{scheme}://127.0.0.1:{server.server_address[1]}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    def only(self) -> dict[str, Any]:
        assert len(self.requests) == 1, [r["path"] for r in self.requests]
        return self.requests[0]

    def sent(self) -> dict[str, str]:
        """The single request's query parameters (one value each)."""
        record = self.only()
        for key, values in record["query"].items():
            assert len(values) == 1, (key, values)
        return {k: v[0] for k, v in record["query"].items()}


def _json(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


@pytest.fixture
def loki() -> Iterator[FakeLoki]:
    fake = FakeLoki()
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture(autouse=True)
def _clean_environment(tmp_path, monkeypatch):
    # Tests pass their configuration as a mapping; nothing leaks in from the
    # developer's shell. A proxy that refuses connections proves the Loki
    # transport goes direct (C5-3).
    for name in LOKI_ENV_NAMES + ("OPSPILOT_ES_URL", "OPSPILOT_ES_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    ca_file = tmp_path / "synthetic-k8s-ca.pem"
    ca_file.write_text("synthetic ca placeholder\n")
    monkeypatch.setenv("C5A_TEST_K8S_CA_FILE", str(ca_file))


def _base_env() -> dict[str, str]:
    return {
        PROFILE_ENV: EXT_PROFILE,
        "OPSPILOT_K8S_API_URL": K8S_API,
        "OPSPILOT_K8S_TOKEN": K8S_TOKEN,
        "OPSPILOT_K8S_CA_FILE": os.environ["C5A_TEST_K8S_CA_FILE"],
        "OPSPILOT_K8S_NAMESPACE": "otel-demo",
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://127.0.0.1:19090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://127.0.0.1:16686/jaeger/ui",
        "OPSPILOT_OTEL_PROMETHEUS_USERNAME": PROM_USERNAME,
        "OPSPILOT_OTEL_PROMETHEUS_PASSWORD": PROM_PASSWORD,
    }


def _env(loki_url: str | None = None, **overrides: str) -> dict[str, str]:
    """A worker environment; with a Loki URL, the bearer credential too."""
    values = _base_env()
    if loki_url is not None:
        values["OPSPILOT_LOKI_URL"] = loki_url
        values["OPSPILOT_LOKI_BEARER_TOKEN"] = LOKI_BEARER
    values.update(overrides)
    return {k: v for k, v in values.items() if v != ""}


def _basic_env(loki_url: str, **overrides: str) -> dict[str, str]:
    return _env(
        loki_url,
        OPSPILOT_LOKI_BEARER_TOKEN="",
        OPSPILOT_LOKI_USERNAME=LOKI_USERNAME,
        OPSPILOT_LOKI_PASSWORD=LOKI_PASSWORD,
        **overrides,
    )


def _require_jq() -> None:
    if shutil.which("jq") is None or shutil.which("python3") is None:
        pytest.skip("the extension worker needs jq and python3 on PATH")


def _worker(env: Mapping[str, str], *, deadline_seconds: float = 600):
    """The executor a worker of ``env`` builds, for one fresh Run."""
    _require_jq()
    clock = FakeClock(start=NOW)
    profile = select_profile(env)
    assert isinstance(profile, ToolProfile)
    if env.get("OPSPILOT_LOKI_URL"):
        # Positive control: refusal tests below are vacuous without the tool.
        assert LOKI in {s["function"]["name"] for s in profile.face(clock).tool_schemas}
    lease = _lease()
    deadline = NOW + timedelta(seconds=deadline_seconds)
    store = FakeStore(lease, deadline=deadline)
    sink = RecordingSink()
    factory = profile.executor_factory(store, sink, clock)
    model_input = profile.face(clock).input_for(
        run_id=str(lease.run_id),
        question="Why is checkout degraded?",
        target_id=TARGET_ID,
        deadline=deadline,
        model_requests=12,
    )
    return factory(lease, model_input), sink


def _call(
    params: dict[str, object], *, index: int = 0, tool: str = LOKI
) -> ToolRequest:
    return ToolRequest(
        step_id="c5a-step",
        tool_index=index,
        tool_name=tool,
        target_ref=TARGET_ID,
        params=params,
        window=FRAME.as_json(),
    )


def _run(loki: FakeLoki, params: dict[str, object], **env: str):
    executor, _ = _worker(_env(loki.url, **env))
    return executor.execute(_call(params))


def _view(outcome) -> Mapping[str, Any]:
    assert outcome.model_view is not None
    return outcome.model_view


def _text(outcome) -> str:
    return json.dumps(_view(outcome), ensure_ascii=False, sort_keys=True)


def _rows(outcome) -> list[Mapping[str, Any]]:
    content = _view(outcome)["content"]
    assert isinstance(content, list), _view(outcome)
    return content


def _refused_with(outcome, code: str) -> None:
    """A pre-request refusal: INVALID_PARAMS, ``code`` first in notes."""
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS"), _view(
        outcome
    )
    notes = _view(outcome).get("notes") or []
    assert notes and str(notes[0]).startswith(code), _view(outcome)


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


# -- ⑧ visibility: byte-stable surfaces, the tool only with OPSPILOT_LOKI_URL ----


ES_URL = "http://127.0.0.1:9200"
LOKI_URL = "http://127.0.0.1:3100"


def test_otel_demo_profile_ignores_loki_settings():
    base = select_profile(
        {
            PROFILE_ENV: "otel-demo",
            "OPSPILOT_LOKI_URL": LOKI_URL,
            "OPSPILOT_LOKI_TENANT_ID": TENANT,
        }
    )
    assert base.versions()["tool_schema_revision"] == BASE_TOOL_SCHEMA_REVISION
    assert _surface_sha256(base) == BASE_SURFACE_SHA256


def test_extension_without_any_log_backend_is_byte_identical_to_main():
    without = select_profile({PROFILE_ENV: EXT_PROFILE})
    assert without.versions()["tool_schema_revision"] == EXT_TOOL_SCHEMA_REVISION
    assert _surface_sha256(without) == EXT_SURFACE_SHA256
    # Loki credentials or a tenant alone never change what the web records.
    for extra in (
        {"OPSPILOT_LOKI_BEARER_TOKEN": LOKI_BEARER},
        {
            "OPSPILOT_LOKI_USERNAME": LOKI_USERNAME,
            "OPSPILOT_LOKI_PASSWORD": LOKI_PASSWORD,
        },
        {"OPSPILOT_LOKI_TENANT_ID": TENANT},
    ):
        profile = select_profile({PROFILE_ENV: EXT_PROFILE, **extra})
        assert _surface_sha256(profile) == EXT_SURFACE_SHA256, extra
    assert LOKI not in _tool_names({PROFILE_ENV: EXT_PROFILE})
    assert "loki" not in _available({PROFILE_ENV: EXT_PROFILE})


def test_extension_with_only_elasticsearch_is_byte_identical_to_main():
    es_only = select_profile({PROFILE_ENV: EXT_PROFILE, "OPSPILOT_ES_URL": ES_URL})
    assert es_only.versions()["tool_schema_revision"] == ES_TOOL_SCHEMA_REVISION
    assert _surface_sha256(es_only) == ES_SURFACE_SHA256
    with_loki_credential = select_profile(
        {
            PROFILE_ENV: EXT_PROFILE,
            "OPSPILOT_ES_URL": ES_URL,
            "OPSPILOT_LOKI_BEARER_TOKEN": LOKI_BEARER,
        }
    )
    assert _surface_sha256(with_loki_credential) == ES_SURFACE_SHA256


def test_loki_url_adds_exactly_the_loki_tool_source_and_a_new_revision():
    without = {PROFILE_ENV: EXT_PROFILE}
    web = {PROFILE_ENV: EXT_PROFILE, "OPSPILOT_LOKI_URL": LOKI_URL}
    old_names = _tool_names(without)
    new_names = _tool_names(web)
    assert set(new_names) == set(old_names) | {LOKI}
    assert [n for n in new_names if n != LOKI] == old_names
    available = _available(web)
    assert set(available["loki"]) == {LOKI}
    assert {k: v for k, v in available.items() if k != "loki"} == _available(without)
    revision = select_profile(web).versions()["tool_schema_revision"]
    assert revision.startswith("otel-demo-ext-")
    assert revision not in (EXT_TOOL_SCHEMA_REVISION, ES_TOOL_SCHEMA_REVISION)
    # The web (URL only) and the worker (URL and credential) agree (C3 §5).
    worker = select_profile(_env(LOKI_URL, OPSPILOT_LOKI_TENANT_ID=TENANT))
    assert select_profile(web).versions() == worker.versions()
    assert _surface_sha256(select_profile(web)) == _surface_sha256(worker)


def test_loki_and_elasticsearch_together_are_computed_from_the_backend_set():
    es_only = {PROFILE_ENV: EXT_PROFILE, "OPSPILOT_ES_URL": ES_URL}
    loki_only = {PROFILE_ENV: EXT_PROFILE, "OPSPILOT_LOKI_URL": LOKI_URL}
    both = {**es_only, "OPSPILOT_LOKI_URL": LOKI_URL}
    names = _tool_names(both)
    assert [n for n in names if n != LOKI] == _tool_names(es_only)
    assert LOKI in names
    available = _available(both)
    assert set(available["loki"]) == {LOKI}
    assert {k: v for k, v in available.items() if k != "loki"} == _available(es_only)
    revisions = {
        select_profile(env).versions()["tool_schema_revision"]
        for env in ({PROFILE_ENV: EXT_PROFILE}, es_only, loki_only, both)
    }
    assert len(revisions) == 4
    # The backend order is fixed: the mapping's key order changes nothing.
    reordered = {"OPSPILOT_LOKI_URL": LOKI_URL, PROFILE_ENV: EXT_PROFILE}
    reordered["OPSPILOT_ES_URL"] = ES_URL
    assert _surface_sha256(select_profile(reordered)) == _surface_sha256(
        select_profile(both)
    )


def test_schema_exposes_exactly_the_upstream_parameters_and_no_tenant():
    schemas = {
        s["function"]["name"]: s["function"]
        for s in _face(
            {PROFILE_ENV: EXT_PROFILE, "OPSPILOT_LOKI_URL": LOKI_URL}
        ).tool_schemas
    }
    params = schemas[LOKI]["parameters"]
    assert set(params["properties"]) == {"query", "start", "end", "limit"}
    assert params["required"] == ["query"]
    for name in ("query", "start", "end"):
        assert params["properties"][name]["type"] == "string", name
    assert params["properties"]["limit"]["type"] == "integer"


def test_unconfigured_extension_worker_has_no_loki_tool(loki):
    executor, _ = _worker(_env(None))
    outcome = executor.execute(_call({"query": '{app="checkout"}'}))
    assert outcome.status in ("error", "denied")
    assert loki.requests == []


# -- ① only GET, a fixed path and a fixed query-string key set ---------------------


def test_every_request_is_a_get_of_the_fixed_path_with_fixed_keys(loki):
    loki.add({"app": "checkout"}, _ns(NOW - timedelta(minutes=5)), "ok")
    executor, _ = _worker(_env(loki.url))
    queries = [
        {"query": '{app="checkout"} |= "error"'},
        {"query": '{app="checkout"}', "limit": 7},
        {
            "query": '{app="checkout"}',
            "start": _z(NOW - timedelta(hours=3)),
            "end": _z(NOW - timedelta(hours=2)),
        },
        {"query": 'sum by (app) (count_over_time({app="checkout"}[5m]))'},
        # Query-string injection inside the LogQL text stays inside ``query``.
        {"query": '{app="checkout"} |= "x&limit=5000&start=0&direction=forward"'},
    ]
    for index, params in enumerate(queries):
        outcome = executor.execute(_call(params, index=index))
        assert outcome.status in ("ok", "no_data"), _view(outcome)
    assert len(loki.requests) == len(queries)
    for record, params in zip(loki.requests, queries):
        assert record["method"] == "GET", record
        assert record["path"] == QUERY_PATH, record
        assert record["body"] == b""
        keys = set(record["query"])
        # Decision B (r10 addendum): a metric query also carries step.
        metric = "count_over_time" in params["query"]
        allowed = ALLOWED_QUERY_KEYS | {"step"} if metric else ALLOWED_QUERY_KEYS
        assert {"query", "start", "end", "limit"} <= keys <= allowed, keys
        assert ("step" in keys) == metric, keys
        for key, values in record["query"].items():
            assert len(values) == 1, (key, values)
        if "direction" in keys:
            assert record["query"]["direction"] == ["backward"]
        # The LogQL is sent as given; the window travels in start/end only.
        assert record["query"]["query"] == [params["query"]]
    assert record["query"]["limit"] == ["100"]
    assert loki.requests[1]["query"]["limit"] == ["7"]


@pytest.mark.parametrize(
    "extra",
    [
        {"direction": "forward"},
        {"step": "1s"},
        {"tenant": "other"},
        {"X-Scope-OrgID": "other"},
        {"path": "/loki/api/v1/push"},
        {"method": "POST"},
    ],
)
def test_parameters_outside_the_schema_never_reach_loki(loki, extra):
    outcome = _run(loki, {"query": '{app="checkout"}', **extra})
    assert outcome.status in ("error", "denied"), _view(outcome)
    assert loki.requests == []


def test_a_base_path_prefix_in_the_url_is_kept(loki):
    # Lead decision (r10 addendum, gap 2): the address may carry a path
    # prefix (gateway or Grafana datasource proxy); the fixed path follows it.
    loki.prefix = "/gw/loki-prefix"
    loki.add({"app": "checkout"}, _ns(NOW - timedelta(minutes=5)), "ok")
    executor, _ = _worker(_env(loki.url + loki.prefix))
    outcome = executor.execute(_call({"query": '{app="checkout"}'}))
    assert outcome.status == "ok", _view(outcome)
    record = loki.only()
    assert (record["method"], record["path"]) == ("GET", loki.prefix + QUERY_PATH)
    assert [row["line"] for row in _rows(outcome)] == ["ok"]


def test_redirects_are_not_followed(loki):
    loki.redirect = True
    outcome = _run(loki, {"query": '{app="checkout"}'})
    assert outcome.status == "error", _view(outcome)
    assert len(loki.requests) == 1


# -- ② LogQL time rules: offset, range selectors, the pushed evaluation start ------


@pytest.mark.parametrize(
    "query",
    [
        'count_over_time({app="checkout"}[5m] offset 1h)',
        'count_over_time({app="checkout"}[5m] OFFSET 1h)',
        'sum(rate({app="checkout"}[1m] Offset 10m))',
        # Longer than the default one-hour query window.
        'count_over_time({app="checkout"}[2h])',
        'count_over_time({app="checkout"}[61m])',
        'count_over_time({app="checkout"}[1d])',
        'sum(count_over_time({app="checkout"}[5m])) / sum(count_over_time({app="checkout"}[3h]))',
        # Durations that do not parse.
        'count_over_time({app="checkout"}[5x])',
        'count_over_time({app="checkout"}[$__interval])',
        'count_over_time({app="checkout"}[])',
        'count_over_time({app="checkout"}[m5])',
        'count_over_time({app="checkout"}[-5m])',
        'count_over_time({app="checkout"}[5m)',
    ],
)
def test_offset_long_or_unparseable_ranges_are_refused_before_any_request(loki, query):
    outcome = _run(loki, {"query": query})
    _refused_with(outcome, "QUERY_OUT_OF_WINDOW")
    assert loki.requests == []


def test_a_range_longer_than_an_explicit_window_is_refused(loki):
    start, end = NOW - timedelta(hours=3), NOW - timedelta(hours=2, minutes=30)
    outcome = _run(
        loki,
        {
            "query": 'count_over_time({app="checkout"}[45m])',
            "start": _z(start),
            "end": _z(end),
        },
    )
    _refused_with(outcome, "QUERY_OUT_OF_WINDOW")
    assert loki.requests == []


@pytest.mark.parametrize(
    "query",
    [
        '{app="checkout"} |~ "status=[0-9]{3}"',
        '{app="checkout"} |~ `took [0-9]+ms`',
        '{pod=~"checkout-[a-z0-9]+-[0-9]+"}',
        '{app="checkout"} |= "[5h]"',
        '{app="checkout"} |= "offset"',
        '{app="checkout"} |= "a\\"offset 5m\\""',
        '{app="offset-service"} != "OFFSET 1h"',
        '{app="checkout"} | line_format "{{.msg}} [2h]"',
    ],
)
def test_brackets_and_offset_inside_string_literals_are_sent_unchanged(loki, query):
    outcome = _run(loki, {"query": query})
    assert outcome.status in ("ok", "no_data"), _view(outcome)
    sent = loki.sent()
    assert sent["query"] == query
    # No range selector outside strings: the evaluation starts at the window.
    assert "step" not in sent
    assert _close(_loki_time_ns(sent["start"]), _ns(DEFAULT_START))
    assert _close(_loki_time_ns(sent["end"]), _ns(NOW))


def _assert_metric_alignment(
    sent: Mapping[str, str], start: datetime, end: datetime, lookback: int
) -> int:
    """Decision B (r10 addendum): ``step=<n>s`` with n = max(floor(range/250), 1);
    the lookback-pushed start rounded up and the end rounded down to epoch
    multiples of n. Returns n."""
    match = re.fullmatch(r"([0-9]+)s", sent.get("step", ""))
    assert match, sent
    n = int(match.group(1))
    # Lead answer (r10 addendum): the evaluation range, before rounding.
    evaluation = (end - start).total_seconds() - lookback
    assert n == max(math.floor(evaluation / 250), 1), (n, sent)
    step_ns = n * 10**9
    sent_start = _loki_time_ns(sent["start"])
    sent_end = _loki_time_ns(sent["end"])
    pushed = _ns(start + timedelta(seconds=lookback))
    assert sent_start % step_ns == 0 and sent_end % step_ns == 0, sent
    # Rounded up (the smallest multiple at or after the pushed start) and
    # rounded down (the largest multiple at or before the end).
    assert pushed <= sent_start < pushed + step_ns, sent
    assert _ns(end) - step_ns < sent_end <= _ns(end), sent
    return n


@pytest.mark.parametrize(
    ("query", "window", "push"),
    [
        ('count_over_time({app="checkout"}[5m])', None, timedelta(minutes=5)),
        (
            'sum(count_over_time({app="checkout"}[5m])) / sum(count_over_time({app="checkout"}[15m]))',
            None,
            timedelta(minutes=15),
        ),
        (
            'sum by (pod) (rate({app="checkout"} |= "error" [90s]))',
            None,
            timedelta(seconds=90),
        ),
        (
            'count_over_time({app="checkout"}[30m15s])',
            None,
            timedelta(minutes=30, seconds=15),
        ),
        (
            'rate({app="checkout"}[10m])',
            (NOW - timedelta(hours=3), NOW - timedelta(hours=2)),
            timedelta(minutes=10),
        ),
        (
            'rate({app="checkout"}[10m])',
            (
                NOW - timedelta(hours=3, seconds=7, milliseconds=300),
                NOW - timedelta(hours=1, seconds=11, milliseconds=600),
            ),
            timedelta(minutes=10),
        ),
        # Decision A: '#' starts a comment; its [30d] is not a range.
        ('count_over_time({job="x"}[5m]) # [30d]', None, timedelta(minutes=5)),
    ],
)
def test_metric_queries_start_evaluation_after_the_longest_range(
    loki, query, window, push
):
    loki.matrix = [
        {
            "metric": {"app": "checkout"},
            "values": [[(NOW - timedelta(minutes=1)).timestamp(), "3"]],
        }
    ]
    params: dict[str, object] = {"query": query}
    start, end = (DEFAULT_START, NOW) if window is None else window
    if window is not None:
        params.update(start=start.isoformat(), end=end.isoformat())
    outcome = _run(loki, params)
    assert outcome.status == "ok", _view(outcome)
    sent = loki.sent()
    assert sent["query"] == query
    lookback = int(push.total_seconds())
    _assert_metric_alignment(sent, start, end, lookback)
    view = _view(outcome)
    # C5-8: a matrix result reports its lookback like PromQL does.
    assert view["lookback_seconds"] == lookback
    rounded = _loki_time_ns(sent["start"])
    reported = _ns(_instant(view["lookback_start_at"]))
    # Lead answer: the earliest instant any point reads, the rounded start
    # minus the lookback.
    assert reported == rounded - lookback * 10**9, view
    assert reported >= _ns(start)
    row = _rows(outcome)[0]
    assert set(row) >= {"metric", "values"}
    assert row["metric"] == {"app": "checkout"}


def test_a_metric_query_whose_rounded_start_passes_its_end_is_refused(loki):
    # Window [T+0.3s, T+600.9s] with [10m]: evaluation range 0.6 s, so n = 1;
    # the pushed start T+600.3s rounds up to T+601s, past the end rounded
    # down to T+600s.
    base = NOW - timedelta(hours=2)
    start = base + timedelta(milliseconds=300)
    end = base + timedelta(seconds=600, milliseconds=900)
    outcome = _run(
        loki,
        {
            "query": 'count_over_time({app="checkout"}[10m])',
            "start": start.isoformat(),
            "end": end.isoformat(),
        },
    )
    _refused_with(outcome, "QUERY_OUT_OF_WINDOW")
    assert loki.requests == []


def test_a_range_equal_to_the_window_is_one_aligned_point(loki):
    # Evaluation range 0 s gives n = 1; NOW is a whole second, so the pushed
    # start and the end are the same aligned instant.
    loki.matrix = [{"metric": {"app": "checkout"}, "values": []}]
    outcome = _run(loki, {"query": 'count_over_time({app="checkout"}[1h])'})
    assert outcome.status in ("ok", "no_data"), _view(outcome)
    sent = loki.sent()
    assert _assert_metric_alignment(sent, DEFAULT_START, NOW, 3600) == 1
    assert _loki_time_ns(sent["start"]) == _loki_time_ns(sent["end"]) == _ns(NOW)


@pytest.mark.parametrize(
    "query",
    [
        # Decision A: Loki's lexer skips /* */ and // comments and reads 'x'
        # as a char literal outside strings; the tool refuses all three.
        'sum(count_over_time({job="x"} /* " */ [1h] /* " */))',
        'sum(count_over_time({job="x"}[1m] /* " */ offset 29m /* " */))',
        "sum by ('\"') (count_over_time({job=`x`}[1h])) #\"",
        'count_over_time({job="x"}[5m]) // [30d]',
        '{job="x"} // comment',
        "{job='x'}",
    ],
)
def test_comments_and_char_literals_outside_strings_are_refused(loki, query):
    outcome = _run(loki, {"query": query})
    _refused_with(outcome, "QUERY_CONSTRUCT_NOT_ALLOWED")
    assert loki.requests == []


@pytest.mark.parametrize(
    "query",
    [
        '{job="x"} |= "a // b"',
        '{job="x"} |= "a /* b */"',
        '{job="x"} |= `a /* b */ // c`',
        '{job="x"} |= "it\'s"',
    ],
)
def test_comment_and_quote_characters_inside_string_literals_are_sent(loki, query):
    outcome = _run(loki, {"query": query})
    assert outcome.status in ("ok", "no_data"), _view(outcome)
    sent = loki.sent()
    assert sent["query"] == query
    assert "step" not in sent
    assert _close(_loki_time_ns(sent["start"]), _ns(DEFAULT_START))


# -- ③ start/end: both or neither, RFC3339 with a timezone, inside the frame -------


def test_default_window_is_the_last_hour_of_the_frame(loki):
    outcome = _run(loki, {"query": '{app="checkout"}'})
    assert outcome.status in ("ok", "no_data"), _view(outcome)
    sent = loki.sent()
    assert _close(_loki_time_ns(sent["start"]), _ns(DEFAULT_START))
    assert _close(_loki_time_ns(sent["end"]), _ns(NOW))


def test_explicit_window_inside_the_frame_is_sent(loki):
    start, end = NOW - timedelta(hours=5), NOW - timedelta(hours=1, minutes=30)
    outcome = _run(
        loki,
        {
            "query": '{app="checkout"}',
            "start": start.astimezone(timezone(timedelta(hours=-8))).isoformat(),
            "end": _z(end),
        },
    )
    assert outcome.status in ("ok", "no_data"), _view(outcome)
    sent = loki.sent()
    assert _close(_loki_time_ns(sent["start"]), _ns(start))
    assert _close(_loki_time_ns(sent["end"]), _ns(end))


@pytest.mark.parametrize(
    "window",
    [
        {"start": _z(NOW - timedelta(hours=1))},  # half a pair
        {"end": _z(NOW)},
        {"start": "2026-09-26T13:00:00", "end": _z(NOW)},  # no timezone
        {"start": _z(NOW - timedelta(hours=1)), "end": "2026-09-26T14:00:00"},
        {"start": "-3600", "end": "0"},  # upstream relative seconds
        {"start": "now-1h", "end": "now"},
        {
            "start": str(int((NOW - timedelta(hours=1)).timestamp())),
            "end": str(int(NOW.timestamp())),
        },  # Unix seconds
        {"start": "yesterday", "end": _z(NOW)},
        {"start": _z(NOW), "end": _z(NOW - timedelta(hours=1))},  # reversed
    ],
)
def test_malformed_windows_are_refused_without_a_request(loki, window):
    outcome = _run(loki, {"query": '{app="checkout"}', **window})
    assert outcome.status in ("error", "denied"), _view(outcome)
    assert loki.requests == []


@pytest.mark.parametrize(
    "window",
    [
        (NOW - timedelta(hours=7), NOW - timedelta(hours=5)),  # starts before
        (NOW - timedelta(hours=1), NOW + timedelta(minutes=10)),  # ends after
        (NOW - timedelta(days=400), NOW - timedelta(days=399)),  # far outside
    ],
)
def test_windows_outside_the_frame_are_refused_and_name_the_frame(loki, window):
    outcome = _run(
        loki,
        {"query": '{app="checkout"}', "start": _z(window[0]), "end": _z(window[1])},
    )
    assert outcome.status in ("error", "denied"), _view(outcome)
    assert outcome.reason in ("INVALID_PARAMS", "WINDOW_OUT_OF_SCOPE")
    if outcome.reason == "INVALID_PARAMS":
        assert str(_view(outcome)["notes"][0]).startswith("QUERY_OUT_OF_WINDOW")
    # The refusal states the frame so the model can retry inside it.
    text = _text(outcome)
    assert FRAME.start.isoformat() in text and FRAME.end.isoformat() in text
    assert loki.requests == []


# -- ④ limit ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("limit", "sent"),
    [(None, "100"), (1, "1"), (100, "100"), (500, "500")],
)
def test_limit_default_and_bounds_are_sent(loki, limit, sent):
    params: dict[str, object] = {"query": '{app="checkout"}'}
    if limit is not None:
        params["limit"] = limit
    outcome = _run(loki, params)
    assert outcome.status in ("ok", "no_data"), _view(outcome)
    assert loki.sent()["limit"] == sent


@pytest.mark.parametrize("limit", [0, -1, 501, 5000])
def test_limit_outside_1_to_500_is_refused_before_any_request(loki, limit):
    outcome = _run(loki, {"query": '{app="checkout"}', "limit": limit})
    _refused_with(outcome, "INVALID_LIMIT")
    assert loki.requests == []


@pytest.mark.parametrize("limit", ["100", 10.5, True])
def test_limit_of_the_wrong_type_is_refused(loki, limit):
    outcome = _run(loki, {"query": '{app="checkout"}', "limit": limit})
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS"), _view(
        outcome
    )
    assert loki.requests == []


@pytest.mark.parametrize(
    "query",
    ["", "   ", "x" * 4001, '{app="checkout"} |= "\x00"', '{app="a\x1bb"}'],
)
def test_empty_overlong_or_unprintable_queries_are_refused(loki, query):
    outcome = _run(loki, {"query": query})
    _refused_with(outcome, "INVALID_QUERY")
    assert loki.requests == []


def test_a_query_of_exactly_4000_chars_is_sent(loki):
    prefix = '{app="checkout"} |= "'
    query = prefix + "y" * (4000 - len(prefix) - 1) + '"'
    assert len(query) == 4000
    outcome = _run(loki, {"query": query})
    assert outcome.status in ("ok", "no_data"), _view(outcome)
    assert loki.sent()["query"] == query


# -- ⑤ projection, size limits, truncation, incompleteness -------------------------


def test_stream_rows_are_newest_first_with_nanosecond_timestamps(loki):
    first = _ns(NOW - timedelta(minutes=40), 123)
    middle = _ns(NOW - timedelta(minutes=20), 456789)
    last = _ns(NOW - timedelta(minutes=5), 987654321 % 1000)
    loki.add({"app": "checkout", "pod": "checkout-1"}, first, "slow request")
    loki.add({"app": "checkout", "pod": "checkout-2"}, middle, "payment failed")
    loki.add({"app": "checkout", "pod": "checkout-1"}, last, "retry exhausted")
    outcome = _run(loki, {"query": '{app="checkout"}'})
    assert outcome.status == "ok", _view(outcome)
    rows = _rows(outcome)
    assert [row["line"] for row in rows] == [
        "retry exhausted",
        "payment failed",
        "slow request",
    ]
    assert [_rfc3339_ns(row["timestamp"]) for row in rows] == [last, middle, first]
    assert rows[1]["labels"] == {"app": "checkout", "pod": "checkout-2"}
    assert set(rows[0]) >= {"timestamp", "labels", "line"}
    view = _view(outcome)
    # Source time comes from the data (C5-8).
    assert _close(_ns(_instant(view["source_start_at"])), first)
    assert _close(_ns(_instant(view["source_end_at"])), last)
    assert view["source"] == "otel-demo" and view["target_id"] == TARGET_ID
    assert view["incomplete"] is False


def test_reaching_the_limit_marks_incomplete_and_keeps_the_newest(loki):
    for minute in range(1, 11):
        loki.add(
            {"app": "checkout"}, _ns(NOW - timedelta(minutes=minute)), f"line-{minute}"
        )
    outcome = _run(loki, {"query": '{app="checkout"}', "limit": 3})
    assert outcome.status == "ok", _view(outcome)
    assert [row["line"] for row in _rows(outcome)] == ["line-1", "line-2", "line-3"]
    view = _view(outcome)
    assert view["incomplete"] is True
    assert "limit" in json.dumps(view.get("notes")).lower()


def test_fewer_lines_than_the_limit_are_complete(loki):
    loki.add({"app": "checkout"}, _ns(NOW - timedelta(minutes=2)), "one")
    loki.add({"app": "checkout"}, _ns(NOW - timedelta(minutes=1)), "two")
    outcome = _run(loki, {"query": '{app="checkout"}', "limit": 3})
    assert outcome.status == "ok", _view(outcome)
    assert _view(outcome)["incomplete"] is False


def test_raw_response_over_2_mib_is_refused_whole(loki):
    big = "y" * (2 * 1024 * 1024 + 1)
    loki.raw = _json(
        {
            "status": "success",
            "data": {
                "resultType": "streams",
                "result": [
                    {
                        "stream": {"app": "checkout"},
                        "values": [[str(_ns(NOW - timedelta(minutes=1))), big]],
                    }
                ],
            },
        }
    )
    outcome = _run(loki, {"query": '{app="checkout"}'})
    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
    assert _view(outcome)["content"] is None


def test_view_over_25k_tokens_is_refused_whole_with_a_narrowing_hint(loki):
    words = " ".join(f"word{n} checkout latency spike" for n in range(60))
    assert len(words) < 2000
    for n in range(500):
        loki.add({"app": "checkout"}, _ns(NOW - timedelta(seconds=n + 1)), words)
    outcome = _run(loki, {"query": '{app="checkout"}', "limit": 500})
    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
    view = _view(outcome)
    assert view["content"] is None
    # The narrowing hint, not the echoed query, names limit.
    hint = json.dumps({k: v for k, v in view.items() if k != "query"}).lower()
    assert "limit" in hint


def test_long_values_are_cut_to_2000_chars_marked_and_secrets_redacted(loki):
    loki.add(
        {"app": "checkout", "build": "b" * 3000},
        _ns(NOW - timedelta(minutes=2)),
        "x" * 5000,
    )
    loki.add(
        {"app": "checkout"},
        _ns(NOW - timedelta(minutes=1)),
        f"user login password={CANARY} accepted",
    )
    outcome = _run(loki, {"query": '{app="checkout"}'})
    assert outcome.status == "ok", _view(outcome)
    text = _text(outcome)
    assert "x" * 1000 in text and "x" * 2001 not in text
    assert "b" * 1000 in text and "b" * 2001 not in text
    assert "trunc" in text.lower()
    assert CANARY not in text and "REDACTED" in text


def test_empty_results_are_no_data(loki):
    outcome = _run(loki, {"query": '{app="nothing-here"}'})
    assert (outcome.status, outcome.reason) == ("no_data", "NO_DATA"), _view(outcome)
    loki.raw = _json(
        {
            "status": "success",
            "data": {
                "resultType": "streams",
                "result": [{"stream": {"app": "checkout"}, "values": []}],
            },
        }
    )
    outcome = _run(loki, {"query": '{app="checkout"}'})
    assert (outcome.status, outcome.reason) == ("no_data", "NO_DATA"), _view(outcome)


# -- ⑥ error classification (C5-11) ----------------------------------------------------


def test_loki_400_is_invalid_params_with_truncated_redacted_text(loki):
    message = (
        "parse error at line 1, col 18: syntax error: unexpected IDENTIFIER "
        f"password={CANARY} "
    ) + "z" * 20000
    loki.fail = (400, "text/plain; charset=utf-8", message.encode())
    outcome = _run(loki, {"query": '{app="checkout"} |= bogus'})
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS"), _view(
        outcome
    )
    text = _text(outcome)
    assert "parse error" in text
    assert CANARY not in text
    assert "z" * 5000 not in text
    assert len(loki.requests) == 1


def test_loki_limit_exceeded_400_is_echoed_as_invalid_params(loki):
    body = (
        b"max entries limit per query exceeded, limit > max_entries_limit (5000 > 1000)"
    )
    loki.fail = (400, "text/plain; charset=utf-8", body)
    outcome = _run(loki, {"query": '{app="checkout"}', "limit": 500})
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    assert "max entries limit" in _text(outcome)


GATEWAY_403 = (
    b"<html>\r\n<head><title>403 Forbidden</title></head>\r\n<body>\r\n"
    b"<center><h1>403 Forbidden</h1></center>\r\n<hr><center>nginx</center>\r\n"
    b"</body>\r\n</html>\r\n"
)


@pytest.mark.parametrize(
    ("status", "content_type", "body", "reason"),
    [
        (401, "text/plain", b"no org id\n", "SOURCE_FORBIDDEN"),
        (
            401,
            "text/html",
            b"<html>401 Authorization Required</html>",
            "SOURCE_FORBIDDEN",
        ),
        (403, "text/plain", b"forbidden\n", "SOURCE_FORBIDDEN"),
        (403, "text/html", GATEWAY_403, "SOURCE_FORBIDDEN"),  # read-only gateway
        (404, "text/plain", b"404 page not found\n", "SOURCE_ERROR"),
        (408, "text/plain", b"timeout\n", "SOURCE_UNAVAILABLE"),
        (429, "text/plain", b"too many outstanding requests\n", "SOURCE_UNAVAILABLE"),
        (500, "text/plain", b"internal error\n", "SOURCE_UNAVAILABLE"),
        (502, "text/html", b"<html>502 Bad Gateway</html>", "SOURCE_UNAVAILABLE"),
        (503, "text/plain", b"Ingester not ready\n", "SOURCE_UNAVAILABLE"),
    ],
)
def test_backend_statuses_map_to_fixed_reasons_with_one_request(
    loki, status, content_type, body, reason
):
    loki.fail = (status, content_type, body)
    outcome = _run(loki, {"query": '{app="checkout"}'})
    assert (outcome.status, outcome.reason) == ("error", reason), _view(outcome)
    assert len(loki.requests) == 1  # one request per call, no retry


def test_an_unreachable_loki_is_source_unavailable(loki):
    url = loki.url
    loki.stop()
    executor, _ = _worker(_env(url))
    outcome = executor.execute(_call({"query": '{app="checkout"}'}))
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_UNAVAILABLE"), _view(
        outcome
    )


def test_a_query_past_the_call_deadline_is_a_timeout(loki):
    loki.delay = 4.0
    executor, _ = _worker(_env(loki.url), deadline_seconds=2)
    started = time.monotonic()
    outcome = executor.execute(_call({"query": '{app="checkout"}'}))
    assert outcome.status == "timeout", _view(outcome)
    assert time.monotonic() - started < 3.9


# -- ⑦ the loki-ro credential slot and the tenant header ------------------------------


def _authorization(record: Mapping[str, Any]) -> str:
    return record["headers"].get("authorization", "")


def _basic(username: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()


def test_bearer_and_basic_credentials_go_only_to_loki(loki):
    prometheus = FakeLoki()
    prometheus.start()
    try:
        executor, _ = _worker(
            _env(loki.url, OPSPILOT_OTEL_PROMETHEUS_URL=prometheus.url)
        )
        executor.execute(_call({"query": '{app="checkout"}'}, index=0))
        executor.execute(
            _call({"match": '{service_name="cart"}'}, index=1, tool="get_metric_names")
        )
    finally:
        prometheus.stop()
    assert {_authorization(r) for r in loki.requests} == {f"Bearer {LOKI_BEARER}"}
    assert prometheus.requests, "the Prometheus tool should have reached its own fake"
    for record in prometheus.requests:
        assert LOKI_BEARER not in json.dumps(record["headers"])
    for record in loki.requests:
        flat = json.dumps(record["headers"])
        assert K8S_TOKEN not in flat and PROM_PASSWORD not in flat

    loki.requests.clear()
    executor, _ = _worker(_basic_env(loki.url))
    executor.execute(_call({"query": '{app="checkout"}'}))
    assert {_authorization(r) for r in loki.requests} == {
        _basic(LOKI_USERNAME, LOKI_PASSWORD)
    }


def test_no_fallback_to_another_slot_when_loki_has_no_credential(loki):
    # Lead decision (r10 addendum, gap 1): a Loki credential is required once
    # OPSPILOT_LOKI_URL is set; another slot's credential is never borrowed.
    env = _env(
        loki.url,
        OPSPILOT_LOKI_BEARER_TOKEN="",
        OPSPILOT_ES_URL="http://127.0.0.1:9",
        OPSPILOT_ES_API_KEY=ES_API_KEY,
        OPSPILOT_OTEL_TOKEN="gggggggg569",
    )
    with pytest.raises(SystemExit, match="LOKI_CONFIG_MISSING"):
        _worker(env)
    assert loki.requests == []


def test_tenant_header_comes_only_from_configuration(loki):
    executor, _ = _worker(_env(loki.url, OPSPILOT_LOKI_TENANT_ID=TENANT))
    executor.execute(_call({"query": '{app="checkout"}'}, index=0))
    assert [r["headers"].get("x-scope-orgid") for r in loki.requests] == [TENANT]

    loki.requests.clear()
    executor, _ = _worker(_env(loki.url))
    executor.execute(_call({"query": '{app="checkout"}'}))
    assert [r["headers"].get("x-scope-orgid") for r in loki.requests] == [None]


def test_credentials_never_appear_in_views_notes_or_trace(loki):
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from tests.test_tracing import lab_env

    loki.add({"app": "checkout"}, _ns(NOW - timedelta(minutes=5)), "ok")
    exporter = InMemorySpanExporter()
    tracer = tracing.configure(
        lab_env(
            OPSPILOT_TOOL_PROFILE=EXT_PROFILE,
            OPSPILOT_K8S_API_URL=K8S_API,
            OPSPILOT_LOKI_URL=loki.url,
            OPSPILOT_OTEL_PROMETHEUS_URL="http://127.0.0.1:19090",
            OPSPILOT_OTEL_JAEGER_URL="http://127.0.0.1:16686",
        ),
        exporter=exporter,
    )
    try:
        views = []
        for env in (_env(loki.url), _basic_env(loki.url)):
            executor, sink = _worker(env)
            with tracer.run(incident_id="c5a-incident", run_id=RUN_ID, versions={}):
                views.append(
                    executor.execute(_call({"query": '{app="checkout"}'}, index=0))
                )
                loki.fail = (401, "text/plain", b"unauthorized\n")
                views.append(executor.execute(_call({"query": '{app="x"}'}, index=1)))
                loki.fail = (400, "text/plain", b"parse error: unexpected IDENTIFIER\n")
                views.append(
                    executor.execute(_call({"query": '{app="x"} |= y'}, index=2))
                )
                loki.fail = None
            views.extend(sink.records)
        tracer.shutdown()
        spans = exporter.get_finished_spans()
    finally:
        tracing.reset()
    assert any(LOKI in span.name for span in spans)
    exported = json.dumps([dict(span.attributes or {}) for span in spans], default=str)
    rendered = json.dumps(
        [getattr(v, "model_view", None) or getattr(v, "_view", None) for v in views],
        default=str,
    )
    for secret in (
        LOKI_BEARER,
        LOKI_PASSWORD,
        base64.b64encode(f"{LOKI_USERNAME}:{LOKI_PASSWORD}".encode()).decode(),
    ):
        assert secret not in exported
        assert secret not in rendered


# -- startup codes, and the web does not read Loki credentials (⑩) -----------------


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        # A credential is required with the address (lead decision, gap 1).
        ({"OPSPILOT_LOKI_BEARER_TOKEN": ""}, "LOKI_CONFIG_MISSING"),
        # Basic and bearer are exclusive (C5-4).
        (
            {
                "OPSPILOT_LOKI_USERNAME": LOKI_USERNAME,
                "OPSPILOT_LOKI_PASSWORD": LOKI_PASSWORD,
            },
            "LOKI_CONFIG_INVALID",
        ),
        # A credential without the address is never silently ignored.
        ({"OPSPILOT_LOKI_URL": ""}, "LOKI_CONFIG_INVALID"),
        (
            {
                "OPSPILOT_LOKI_URL": "",
                "OPSPILOT_LOKI_BEARER_TOKEN": "",
                "OPSPILOT_LOKI_PASSWORD": LOKI_PASSWORD,
            },
            "LOKI_CONFIG_INVALID",
        ),
        # Half a basic pair.
        (
            {"OPSPILOT_LOKI_BEARER_TOKEN": "", "OPSPILOT_LOKI_USERNAME": LOKI_USERNAME},
            "LOKI_CONFIG_(MISSING|INVALID)",
        ),
        (
            {"OPSPILOT_LOKI_BEARER_TOKEN": "", "OPSPILOT_LOKI_PASSWORD": LOKI_PASSWORD},
            "LOKI_CONFIG_(MISSING|INVALID)",
        ),
        # The address carries no userinfo or query string (C5-4).
        (
            {"OPSPILOT_LOKI_URL": "http://reader:hhhhhhhh569@127.0.0.1:3100"},
            "LOKI_CONFIG_INVALID",
        ),
        ({"OPSPILOT_LOKI_URL": "http://127.0.0.1:3100?orgId=1"}, "LOKI_CONFIG_INVALID"),
        # Cross-tenant federation is refused at configuration (C5-0).
        ({"OPSPILOT_LOKI_TENANT_ID": "team-a|team-b"}, "LOKI_CONFIG_INVALID"),
    ],
)
def test_worker_refuses_to_start_with_conflicting_loki_configuration(overrides, code):
    _require_jq()
    env = _env("http://127.0.0.1:3100", **overrides)
    with pytest.raises(SystemExit, match=code):
        _worker(env)


class _TrackingEnv(Mapping[str, str]):
    def __init__(self, values: Mapping[str, str]):
        self._values = dict(values)
        self.read: set[str] = set()
        self.iterated = False

    def __getitem__(self, key: str) -> str:
        self.read.add(key)
        return self._values[key]

    def __iter__(self):
        self.iterated = True
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


def test_web_side_profile_reads_no_loki_credential():
    env = _TrackingEnv(
        _basic_env(
            "http://127.0.0.1:3100",
            OPSPILOT_LOKI_CA_FILE="/nonexistent/loki-ca.pem",
            OPSPILOT_LOKI_TENANT_ID=TENANT,
        )
    )
    profile = select_profile(env)
    face = profile.face(FakeClock(start=NOW))
    face.evidence_context(RUN_ID)
    profile.versions()
    assert LOKI in {s["function"]["name"] for s in face.tool_schemas}
    assert not env.read & set(LOKI_CREDENTIAL_NAMES)
    assert not env.iterated


# -- ⑨ lab gate ------------------------------------------------------------------


def test_lab_gate_requires_a_lab_loki_host():
    env = {
        "OPSPILOT_TRACE": "lab",
        PROFILE_ENV: EXT_PROFILE,
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://127.0.0.1:19090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://127.0.0.1:16686",
        "OPSPILOT_K8S_API_URL": K8S_API,
        "LANGSMITH_PROJECT": "opspilot-lab-c5a",
        "LANGSMITH_API_KEY": "lsv2-aaaaaaaa569",
        "LANGSMITH_ENDPOINT": "https://api.smith.langchain.com",
    }
    assert check_lab_target(env) == ()
    for lab_url in (
        "http://127.0.0.1:3100",
        "http://localhost:3100",
        "http://loki.monitoring.svc:3100",
        "http://loki-gateway.loki.svc.cluster.local",
    ):
        assert check_lab_target({**env, "OPSPILOT_LOKI_URL": lab_url}) == (), lab_url
    for remote in (
        "https://logs.example.invalid:3100",
        "http://reader@127.0.0.1:3100",
        "http://127.attacker.example:3100",
    ):
        assert "LOKI_NOT_LAB" in check_lab_target({**env, "OPSPILOT_LOKI_URL": remote})
    assert "LOKI_NOT_LAB" not in check_lab_target(
        {**env, "OPSPILOT_ES_URL": "https://logs.example.invalid:9200"}
    )


# -- HTTPS with OPSPILOT_LOKI_CA_FILE (C5-4) --------------------------------------------


def test_https_loki_is_verified_against_the_configured_ca(tmp_path):
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is needed to mint the synthetic CA")
    cert, key = tmp_path / "loki-cert.pem", tmp_path / "loki-key.pem"
    subprocess.run(
        [
            openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cert), "-days", "2",
            "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    fake = FakeLoki()
    fake.start(tls=(cert, key))
    try:
        trusted, _ = _worker(_env(fake.url, OPSPILOT_LOKI_CA_FILE=str(cert)))
        outcome = trusted.execute(_call({"query": '{app="checkout"}'}))
        assert outcome.status in ("ok", "no_data"), _view(outcome)
        assert {_authorization(r) for r in fake.requests} == {f"Bearer {LOKI_BEARER}"}
        fake.requests.clear()
        untrusted, _ = _worker(_env(fake.url))
        outcome = untrusted.execute(_call({"query": '{app="checkout"}'}))
        assert outcome.status == "error"
        assert fake.requests == []  # the handshake failed; nothing was read
    finally:
        fake.stop()


# -- deploy entrypoint passes OPSPILOT_LOKI_* to the worker ---------------------------


def test_entrypoint_imports_loki_settings_from_the_worker_env_file(tmp_path):
    entrypoint = Path(__file__).resolve().parents[1] / "deploy" / "docker-entrypoint.sh"
    worker_env = tmp_path / "worker_env"
    worker_env.write_text(
        "OPSPILOT_LOKI_URL=http://127.0.0.1:3100\n"
        f"OPSPILOT_LOKI_BEARER_TOKEN={LOKI_BEARER}\n"
        f"OPSPILOT_LOKI_USERNAME={LOKI_USERNAME}\n"
        f"OPSPILOT_LOKI_PASSWORD='{LOKI_PASSWORD}'\n"
        "OPSPILOT_LOKI_CA_FILE=/run/secrets/loki-ca.pem\n"
        f"OPSPILOT_LOKI_TENANT_ID={TENANT}\n"
        "OPSPILOT_LOKIX=not-a-loki-setting\n"
        "OPSPILOT_LOKI_$(touch pwned)=x\n"
    )
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
    assert env["OPSPILOT_LOKI_URL"] == "http://127.0.0.1:3100"
    assert env["OPSPILOT_LOKI_BEARER_TOKEN"] == LOKI_BEARER
    assert env["OPSPILOT_LOKI_USERNAME"] == LOKI_USERNAME
    assert env["OPSPILOT_LOKI_PASSWORD"] == LOKI_PASSWORD
    assert env["OPSPILOT_LOKI_CA_FILE"] == "/run/secrets/loki-ca.pem"
    assert env["OPSPILOT_LOKI_TENANT_ID"] == TENANT
    assert "OPSPILOT_LOKIX" not in env
    assert not (tmp_path / "pwned").exists()
