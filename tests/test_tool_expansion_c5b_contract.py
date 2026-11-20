"""Independent contract tests for tool expansion C5b (#569): the five
VictoriaLogs tools ``victorialogs_query``, ``victorialogs_streams``,
``victorialogs_field_names``, ``victorialogs_field_values`` and
``victorialogs_hits``.

Written from the C5 task record (docs/tasks/2026-11-18-tool-expansion-c5.md,
section "行为（公开接口，C5b，2026-11-19）"), decision table r10 (C5-0 ...
C5-21) and R8-25, without reading the implementation. Red until the
implementation lands; collection must stay green (new product names are
imported inside the tests that need them).

Every test goes through the public path: build the ``otel-demo-ext`` profile
from an environment mapping, call a tool through the executor, and assert on
the returned view, the refusal code, or what a local ``http.server`` fake
VictoriaLogs received. No container, no real backend, no model.

Seams assumed beyond the interface (each also marked ``# ASSUMPTION:``):

* ``select_profile(env)`` reads ``OPSPILOT_VICTORIALOGS_URL`` from the
  mapping it is given; the worker reads the VictoriaLogs credentials from the
  same mapping (as C5a does for ``OPSPILOT_LOKI_*``).
* ``OPSPILOT_VICTORIALOGS_BEARER_TOKEN`` is sent as ``Authorization: Bearer
  <v>`` and the user name and password as RFC 7617 ``Basic``.
* Pre-request refusals surface as ``INVALID_PARAMS`` with the code at the
  start of the first ``notes`` entry (interface, as C5a).
* The view's ``content`` is the list of ``results`` rows, and the executor's
  ``backend_<row_unit>_returned`` field names the row unit (C5a view shape).
* ``scripts/loki_readonly_gateway.py render`` takes ``--backend
  victorialogs``.

All synthetic credentials and canaries are low-entropy placeholders.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
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

ROOT = Path(__file__).resolve().parents[1]
EXT_PROFILE = "otel-demo-ext"

QUERY = "victorialogs_query"
STREAMS = "victorialogs_streams"
FIELD_NAMES = "victorialogs_field_names"
FIELD_VALUES = "victorialogs_field_values"
HITS = "victorialogs_hits"
TOOLS = (QUERY, STREAMS, FIELD_NAMES, FIELD_VALUES, HITS)
PATHS = {
    QUERY: "/select/logsql/query",
    STREAMS: "/select/logsql/streams",
    FIELD_NAMES: "/select/logsql/field_names",
    FIELD_VALUES: "/select/logsql/field_values",
    HITS: "/select/logsql/hits",
}
# The fixed query-string keys of each tool (interface table).
KEYS = {
    QUERY: {"query", "start", "end", "limit"},
    STREAMS: {"query", "start", "end", "limit"},
    FIELD_NAMES: {"query", "start", "end"},
    FIELD_VALUES: {"query", "field", "start", "end", "limit"},
    HITS: {"query", "start", "end", "step"},
}
# The smallest valid call of each tool.
MINIMAL: dict[str, dict[str, object]] = {
    QUERY: {"query": "error"},
    STREAMS: {},
    FIELD_NAMES: {},
    FIELD_VALUES: {"field": "level"},
    HITS: {"query": "error"},
}
LIMITED = (QUERY, STREAMS, FIELD_VALUES)
WITH_QUERY_REQUIRED = (QUERY, HITS)
ROW_UNIT = {
    QUERY: "entries",
    STREAMS: "streams",
    FIELD_NAMES: "fields",
    FIELD_VALUES: "values",
    HITS: "series",
}

VL_BEARER = "vlogs-ro-aaaaaaaa569"
VL_USERNAME = "vlogs-reader"
VL_PASSWORD = "bbbbbbbb569"
LOKI_BEARER = "loki-ro-cccccccc569"
ES_API_KEY = "es-ro-dddddddd569"
PROM_USERNAME = "investigator"
PROM_PASSWORD = "eeeeeeee569"
K8S_TOKEN = "ffffffff569"
K8S_API = "https://127.0.0.1:16443"
CANARY = "gggggggg569"
RUN_ID = "c5b-run"

# Pinned on main a18e61e9 (before any C5b code), computed with
# ``_surface_sha256`` below (RUN_ID ``c5b-run``): these four extension faces and otel-demo must
# stay byte-identical when C5b lands (C5-14).
BASE_TOOL_SCHEMA_REVISION = "otel-demo-a3a95a0f888e"
BASE_SURFACE_SHA256 = "4ed3fdd7d08057402d6cd4166d9d7023958f82d5217aa8c9ce8a222d7f00504a"
PINNED = {
    # (elasticsearch, loki): (revision, surface sha256)
    (False, False): (
        "otel-demo-ext-c63f058afb18",
        "e0631d7c5defdd243edb0d7c0275643e66f66740706e561f571266720f6ea07b",
    ),
    (True, False): (
        "otel-demo-ext-4f7faf728706",
        "e19b0177f3aea02bf87eb92b07b64b793bdcd24cf938ef12c0b347501f63f076",
    ),
    (False, True): (
        "otel-demo-ext-dd942a2f1292",
        "789e777e0e8b1a8ed91549e655a0751ba4a2056b803116eae103e13721300e09",
    ),
    (True, True): (
        "otel-demo-ext-f11fc2ce4d18",
        "489ed77610aa700b2acea37407f07c90cf5357937ec432e38da56b8bdd227ecd",
    ),
}
# ``render`` output of the Loki gateway on main a18e61e9 (default backend).
GATEWAY_CI_ARGS = (
    "render", "--server", "8080=loki3:3100", "--server", "8081=loki29:3100",
    "--deny-port", "8082", "--bind", "0.0.0.0",
    "--access-log", "/var/log/opspilot/access.log",
)  # fmt: skip
GATEWAY_CI_SHA256 = "36b781162fd753c22052646e0042f0ecfed2d568da090b297ed934567f1b6ff5"
GATEWAY_ONE_SHA256 = "a14518204e62a652ff492305a741d1e02752efe294a9529342a0727113cac32b"

VL_ENV_NAMES = (
    "OPSPILOT_VICTORIALOGS_URL",
    "OPSPILOT_VICTORIALOGS_USERNAME",
    "OPSPILOT_VICTORIALOGS_PASSWORD",
    "OPSPILOT_VICTORIALOGS_BEARER_TOKEN",
    "OPSPILOT_VICTORIALOGS_CA_FILE",
    "OPSPILOT_VICTORIALOGS_ACCOUNT_ID",
    "OPSPILOT_VICTORIALOGS_PROJECT_ID",
)
VL_CREDENTIAL_NAMES = (
    "OPSPILOT_VICTORIALOGS_USERNAME",
    "OPSPILOT_VICTORIALOGS_PASSWORD",
    "OPSPILOT_VICTORIALOGS_BEARER_TOKEN",
    "OPSPILOT_VICTORIALOGS_CA_FILE",
)
OTHER_BACKEND_ENV = (
    "OPSPILOT_ES_URL",
    "OPSPILOT_ES_API_KEY",
    "OPSPILOT_LOKI_URL",
    "OPSPILOT_LOKI_BEARER_TOKEN",
    "OPSPILOT_LOKI_USERNAME",
    "OPSPILOT_LOKI_PASSWORD",
    "OPSPILOT_LOKI_CA_FILE",
    "OPSPILOT_LOKI_TENANT_ID",
)

# The call's authorized frame; the default query window is its last hour.
FRAME = Window(NOW - timedelta(hours=6), NOW)
DEFAULT_START = NOW - timedelta(hours=1)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_Z_RFC3339 = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?Z")
_ANY_RFC3339 = re.compile(
    r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)"
)


# -- time helpers ----------------------------------------------------------------


def _z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _ns(moment: datetime, extra_ns: int = 0) -> int:
    return (moment - _EPOCH) // timedelta(microseconds=1) * 1000 + extra_ns


def _rfc3339_ns(text: str) -> int | None:
    match = _ANY_RFC3339.fullmatch(text)
    if not match:
        return None
    zone = "+00:00" if match.group(3) == "Z" else match.group(3)
    whole = datetime.fromisoformat(match.group(1) + zone)
    return _ns(whole) + int((match.group(2) or "").ljust(9, "0"))


def _vl_time(moment: datetime, extra_ns: int = 0) -> str:
    """A ``_time`` value as VictoriaLogs writes it (RFC3339, nanoseconds)."""
    ns = _ns(moment, extra_ns)
    whole = datetime.fromtimestamp(ns // 10**9, timezone.utc)
    return whole.strftime("%Y-%m-%dT%H:%M:%S") + f".{ns % 10**9:09d}Z"


def _instant(value: object) -> datetime:
    assert isinstance(value, str), value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _close(ns_a: int, ns_b: int, tolerance_ns: int = 1000) -> bool:
    return abs(ns_a - ns_b) <= tolerance_ns


# -- local VictoriaLogs fake -------------------------------------------------------


def _json(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


class FakeVictoriaLogs:
    """A loopback HTTP(S) server answering the five ``/select/logsql/*``
    endpoints in VictoriaLogs' shapes and recording every request.

    ``/query`` honours ``start``/``end`` and ``limit`` (the newest ``limit``
    entries, deliberately returned oldest first so the tool's own ordering is
    what the test sees); the metadata endpoints return their canned rows, cut
    to ``limit`` in the canned order."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.entries: list[dict[str, Any]] = []
        self.streams: list[tuple[str, int]] = []
        self.field_names: list[tuple[str, int]] = []
        self.field_values: list[tuple[str, int]] = []
        self.hits: list[dict[str, Any]] = []
        self.raw: bytes | None = None
        self.fail: tuple[int, str, bytes] | None = None
        self.delay = 0.0
        self.redirect = False
        self.prefix = ""
        self._server: ThreadingHTTPServer | None = None
        self.url = ""

    def add(self, when: datetime, msg: str, extra_ns: int = 0, **fields: str) -> None:
        entry = {
            "_time": _vl_time(when, extra_ns),
            "_stream": fields.pop("_stream", '{service="checkout"}'),
            "_msg": msg,
        }
        entry.update(fields)
        self.entries.append(entry)

    def answer(self, record: dict[str, Any]) -> tuple[int, dict[str, str], bytes]:
        if self.delay:
            time.sleep(self.delay)
        if self.redirect:
            return 302, {"Location": "/elsewhere"}, b""
        if self.fail is not None:
            status, content_type, body = self.fail
            return status, {"Content-Type": content_type}, body
        paths = {self.prefix + path: tool for tool, path in PATHS.items()}
        tool = paths.get(record["path"])
        if record["method"] != "GET" or tool is None:
            return 404, {"Content-Type": "text/plain"}, b"unsupported path\n"
        if self.raw is not None:
            return 200, {}, self.raw
        query = {k: v[-1] for k, v in record["query"].items()}
        limit = int(query["limit"]) if query.get("limit", "").isdigit() else None
        if tool == QUERY:
            start = _rfc3339_ns(query.get("start", ""))
            end = _rfc3339_ns(query.get("end", ""))
            if start is None or end is None:
                return 400, {"Content-Type": "text/plain"}, b"cannot parse start/end\n"
            chosen = [
                e
                for e in self.entries
                if start <= (_rfc3339_ns(e["_time"]) or 0) <= end
            ]
            chosen.sort(key=lambda e: _rfc3339_ns(e["_time"]) or 0, reverse=True)
            if limit is not None:
                chosen = chosen[:limit]
            chosen.reverse()
            body = b"".join(_json(e) + b"\n" for e in chosen)
            return 200, {"Content-Type": "application/stream+json"}, body
        if tool == HITS:
            return 200, {}, _json({"hits": self.hits})
        canned = {
            STREAMS: self.streams,
            FIELD_NAMES: self.field_names,
            FIELD_VALUES: self.field_values,
        }[tool]
        rows = canned if limit is None else canned[:limit]
        return 200, {}, _json({"values": [{"value": v, "hits": h} for v, h in rows]})

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


@pytest.fixture
def vl() -> Iterator[FakeVictoriaLogs]:
    fake = FakeVictoriaLogs()
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture(autouse=True)
def _clean_environment(tmp_path, monkeypatch):
    # Configuration comes from the mapping each test passes; a proxy that
    # refuses connections proves the transport goes direct (C5-3).
    for name in VL_ENV_NAMES + OTHER_BACKEND_ENV:
        monkeypatch.delenv(name, raising=False)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    ca_file = tmp_path / "synthetic-k8s-ca.pem"
    ca_file.write_text("synthetic ca placeholder\n")
    monkeypatch.setenv("C5B_TEST_K8S_CA_FILE", str(ca_file))


def _base_env() -> dict[str, str]:
    return {
        PROFILE_ENV: EXT_PROFILE,
        "OPSPILOT_K8S_API_URL": K8S_API,
        "OPSPILOT_K8S_TOKEN": K8S_TOKEN,
        "OPSPILOT_K8S_CA_FILE": os.environ["C5B_TEST_K8S_CA_FILE"],
        "OPSPILOT_K8S_NAMESPACE": "otel-demo",
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://127.0.0.1:19090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://127.0.0.1:16686/jaeger/ui",
        "OPSPILOT_OTEL_PROMETHEUS_USERNAME": PROM_USERNAME,
        "OPSPILOT_OTEL_PROMETHEUS_PASSWORD": PROM_PASSWORD,
    }


def _env(vl_url: str | None = None, **overrides: str) -> dict[str, str]:
    """A worker environment; with a VictoriaLogs URL, the bearer credential."""
    values = _base_env()
    if vl_url is not None:
        values["OPSPILOT_VICTORIALOGS_URL"] = vl_url
        values["OPSPILOT_VICTORIALOGS_BEARER_TOKEN"] = VL_BEARER
    values.update(overrides)
    return {k: v for k, v in values.items() if v != ""}


def _basic_env(vl_url: str, **overrides: str) -> dict[str, str]:
    return _env(
        vl_url,
        OPSPILOT_VICTORIALOGS_BEARER_TOKEN="",
        OPSPILOT_VICTORIALOGS_USERNAME=VL_USERNAME,
        OPSPILOT_VICTORIALOGS_PASSWORD=VL_PASSWORD,
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
    if env.get("OPSPILOT_VICTORIALOGS_URL"):
        # Positive control: refusal tests below are vacuous without the tools.
        names = {s["function"]["name"] for s in profile.face(clock).tool_schemas}
        assert set(TOOLS) <= names, names
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
    params: dict[str, object], tool: str = QUERY, *, index: int = 0
) -> ToolRequest:
    return ToolRequest(
        step_id="c5b-step",
        tool_index=index,
        tool_name=tool,
        target_ref=TARGET_ID,
        params=params,
        window=FRAME.as_json(),
    )


def _run(
    vl: FakeVictoriaLogs, params: dict[str, object], tool: str = QUERY, **env: str
):
    executor, _ = _worker(_env(vl.url, **env))
    return executor.execute(_call(params, tool))


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


ES_URL = "http://127.0.0.1:9200"
LOKI_URL = "http://127.0.0.1:3100"
VL_URL = "http://127.0.0.1:9428"


def _web(
    *, es: bool = False, loki: bool = False, vlogs: bool = False
) -> dict[str, str]:
    env = {PROFILE_ENV: EXT_PROFILE}
    if es:
        env["OPSPILOT_ES_URL"] = ES_URL
    if loki:
        env["OPSPILOT_LOKI_URL"] = LOKI_URL
    if vlogs:
        env["OPSPILOT_VICTORIALOGS_URL"] = VL_URL
    return env


# -- ⑪ visibility: byte-stable surfaces, the tools only with the URL ---------------


def test_otel_demo_profile_ignores_victorialogs_settings():
    base = select_profile(
        {
            PROFILE_ENV: "otel-demo",
            "OPSPILOT_VICTORIALOGS_URL": VL_URL,
            "OPSPILOT_VICTORIALOGS_ACCOUNT_ID": "7",
        }
    )
    assert base.versions()["tool_schema_revision"] == BASE_TOOL_SCHEMA_REVISION
    assert _surface_sha256(base) == BASE_SURFACE_SHA256


@pytest.mark.parametrize(("es", "loki"), sorted(PINNED))
def test_existing_extension_faces_are_byte_identical_without_victorialogs(es, loki):
    revision, sha = PINNED[(es, loki)]
    profile = select_profile(_web(es=es, loki=loki))
    assert profile.versions()["tool_schema_revision"] == revision
    assert _surface_sha256(profile) == sha
    # VictoriaLogs settings without the address never change what the web
    # records (B1-U2: the web looks at the address only).
    for extra in (
        {"OPSPILOT_VICTORIALOGS_BEARER_TOKEN": VL_BEARER},
        {
            "OPSPILOT_VICTORIALOGS_USERNAME": VL_USERNAME,
            "OPSPILOT_VICTORIALOGS_PASSWORD": VL_PASSWORD,
        },
        {
            "OPSPILOT_VICTORIALOGS_ACCOUNT_ID": "7",
            "OPSPILOT_VICTORIALOGS_PROJECT_ID": "1",
        },
    ):
        with_extra = select_profile({**_web(es=es, loki=loki), **extra})
        assert _surface_sha256(with_extra) == sha, extra
    names = _tool_names(_web(es=es, loki=loki))
    assert not set(TOOLS) & set(names)
    assert "victorialogs" not in _available(_web(es=es, loki=loki))


def test_existing_revision_function_values_are_unchanged():
    from opspilot.tools.otel_demo_ext import tool_schema_revision

    for (es, loki), (revision, _) in PINNED.items():
        assert tool_schema_revision(elasticsearch=es, loki=loki) == revision
        assert (
            tool_schema_revision(elasticsearch=es, loki=loki, victorialogs=False)
            == revision
        )


@pytest.mark.parametrize(("es", "loki"), sorted(PINNED))
def test_victorialogs_url_appends_exactly_the_five_tools_and_one_source(es, loki):
    without = _web(es=es, loki=loki)
    web = _web(es=es, loki=loki, vlogs=True)
    old_names = _tool_names(without)
    new_names = _tool_names(web)
    assert new_names[: len(old_names)] == old_names
    assert sorted(new_names[len(old_names) :]) == sorted(TOOLS)
    available = _available(web)
    assert available["victorialogs"] == sorted(TOOLS)
    assert {k: v for k, v in available.items() if k != "victorialogs"} == _available(
        without
    )
    revision = select_profile(web).versions()["tool_schema_revision"]
    assert revision.startswith("otel-demo-ext-")
    assert revision not in {value[0] for value in PINNED.values()}


def test_all_eight_backend_sets_have_distinct_revisions_in_a_fixed_order():
    from opspilot.tools.otel_demo_ext import tool_schema_revision

    revisions = {}
    for es in (False, True):
        for loki in (False, True):
            for vlogs in (False, True):
                env = _web(es=es, loki=loki, vlogs=vlogs)
                revision = select_profile(env).versions()["tool_schema_revision"]
                assert revision == tool_schema_revision(
                    elasticsearch=es, loki=loki, victorialogs=vlogs
                )
                revisions[(es, loki, vlogs)] = revision
    assert len(set(revisions.values())) == 8
    # Backend order is fixed: ES, Loki, VictoriaLogs; mapping order is moot.
    full = _web(es=True, loki=True, vlogs=True)
    reordered = {
        "OPSPILOT_VICTORIALOGS_URL": VL_URL,
        "OPSPILOT_LOKI_URL": LOKI_URL,
        PROFILE_ENV: EXT_PROFILE,
        "OPSPILOT_ES_URL": ES_URL,
    }
    assert _surface_sha256(select_profile(reordered)) == _surface_sha256(
        select_profile(full)
    )
    names = _tool_names(full)
    assert sorted(names[-5:]) == sorted(TOOLS)
    assert names[:-5] == _tool_names(_web(es=True, loki=True))


def test_new_constants_name_the_victorialogs_only_face():
    from opspilot.tools.otel_demo_ext import (
        VICTORIALOGS_TOOL_NAMES,
        VICTORIALOGS_TOOL_SCHEMA_REVISION,
        tool_schema_revision,
    )

    assert sorted(VICTORIALOGS_TOOL_NAMES) == sorted(TOOLS)
    assert len(VICTORIALOGS_TOOL_NAMES) == 5
    assert VICTORIALOGS_TOOL_SCHEMA_REVISION == tool_schema_revision(victorialogs=True)
    assert (
        select_profile(_web(vlogs=True)).versions()["tool_schema_revision"]
        == VICTORIALOGS_TOOL_SCHEMA_REVISION
    )


def test_web_and_worker_agree_on_the_victorialogs_face():
    web = _web(vlogs=True)
    worker = _env(
        VL_URL,
        OPSPILOT_VICTORIALOGS_ACCOUNT_ID="7",
        OPSPILOT_VICTORIALOGS_PROJECT_ID="3",
    )
    assert select_profile(web).versions() == select_profile(worker).versions()
    assert _surface_sha256(select_profile(web)) == _surface_sha256(
        select_profile(worker)
    )


def test_schemas_expose_exactly_the_upstream_parameters_and_no_tenant():
    schemas = {
        s["function"]["name"]: s["function"]
        for s in _face(_web(vlogs=True)).tool_schemas
    }
    expected = {
        QUERY: ({"query", "start", "end", "limit"}, ["query"]),
        STREAMS: ({"query", "start", "end", "limit"}, []),
        FIELD_NAMES: ({"query", "start", "end"}, []),
        FIELD_VALUES: ({"field", "query", "start", "end", "limit"}, ["field"]),
        HITS: ({"query", "start", "end", "step", "field"}, ["query"]),
    }
    for tool, (properties, required) in expected.items():
        params = schemas[tool]["parameters"]
        assert set(params["properties"]) == properties, tool
        assert sorted(params.get("required") or []) == required, tool
        for name in properties - {"limit"}:
            assert params["properties"][name]["type"] == "string", (tool, name)
        if "limit" in properties:
            assert params["properties"]["limit"]["type"] == "integer", tool


@pytest.mark.parametrize("tool", TOOLS)
def test_unconfigured_extension_worker_has_no_victorialogs_tools(vl, tool):
    executor, _ = _worker(_env(None))
    outcome = executor.execute(_call(dict(MINIMAL[tool]), tool))
    assert outcome.status in ("error", "denied")
    assert vl.requests == []


# -- ① only GET, fixed paths, fixed query-string keys ------------------------------


@pytest.mark.parametrize("tool", TOOLS)
def test_each_tool_sends_one_get_of_its_fixed_path_with_its_fixed_keys(vl, tool):
    outcome = _run(vl, dict(MINIMAL[tool]), tool)
    assert outcome.status in ("ok", "no_data"), _view(outcome)
    record = vl.only()
    assert record["method"] == "GET", record
    assert record["path"] == PATHS[tool], record
    assert record["body"] == b""
    assert set(record["query"]) == KEYS[tool], record["query"]
    sent = vl.sent()
    for key in ("start", "end"):
        assert _Z_RFC3339.fullmatch(sent[key]), sent
    assert _close(_rfc3339_ns(sent["start"]) or 0, _ns(DEFAULT_START))
    assert _close(_rfc3339_ns(sent["end"]) or 0, _ns(NOW))
    if tool in WITH_QUERY_REQUIRED:
        assert sent["query"] == MINIMAL[tool]["query"]
    else:
        assert sent["query"] == "*"  # the upstream default
    if tool in LIMITED:
        assert sent["limit"] == "101"  # default 100, plus one to detect more
    if tool == FIELD_VALUES:
        assert sent["field"] == "level"
    if tool == HITS:
        assert sent["step"] == "5m"


def test_hits_sends_field_only_when_given(vl):
    executor, _ = _worker(_env(vl.url))
    executor.execute(_call({"query": "error", "step": "1m"}, HITS, index=0))
    executor.execute(
        _call({"query": "error", "step": "10m", "field": "level"}, HITS, index=1)
    )
    first, second = vl.requests
    assert set(first["query"]) == KEYS[HITS]
    assert first["query"]["step"] == ["1m"]
    assert set(second["query"]) == KEYS[HITS] | {"field"}
    assert second["query"]["field"] == ["level"]
    assert second["query"]["step"] == ["10m"]


def test_query_text_is_sent_as_given_and_cannot_inject_keys(vl):
    executor, _ = _worker(_env(vl.url))
    queries = [
        'error "x&limit=5000&start=0&end=now"',
        '{service="checkout"} level:error | stats count() rows',
        "_time:5m error",
        "* | delete password | rename a as b",
    ]
    for index, query in enumerate(queries):
        outcome = executor.execute(_call({"query": query}, QUERY, index=index))
        assert outcome.status in ("ok", "no_data"), _view(outcome)
    assert [r["query"]["query"] for r in vl.requests] == [[q] for q in queries]
    for record in vl.requests:
        assert set(record["query"]) == KEYS[QUERY]
        assert record["query"]["limit"] == ["101"]


@pytest.mark.parametrize(
    "extra",
    [
        {"AccountID": "1"},
        {"ProjectID": "1"},
        {"account_id": "1"},
        {"tenant": "other"},
        {"method": "POST"},
        {"path": "/insert/jsonline"},
        {"direction": "forward"},
    ],
)
@pytest.mark.parametrize("tool", TOOLS)
def test_parameters_outside_the_schema_never_reach_victorialogs(vl, tool, extra):
    outcome = _run(vl, {**MINIMAL[tool], **extra}, tool)
    assert outcome.status in ("error", "denied"), _view(outcome)
    assert vl.requests == []


def test_a_base_path_prefix_in_the_url_is_kept(vl):
    vl.prefix = "/gw/vlogs-prefix"
    vl.add(NOW - timedelta(minutes=5), "ok")
    executor, _ = _worker(_env(vl.url + vl.prefix))
    outcome = executor.execute(_call({"query": "*"}))
    assert outcome.status == "ok", _view(outcome)
    record = vl.only()
    assert (record["method"], record["path"]) == ("GET", vl.prefix + PATHS[QUERY])
    assert [row["_msg"] for row in _rows(outcome)] == ["ok"]


def test_redirects_are_not_followed(vl):
    vl.redirect = True
    outcome = _run(vl, {"query": "*"})
    assert outcome.status == "error", _view(outcome)
    assert len(vl.requests) == 1


# -- ④ start/end: both or neither, RFC3339 with a timezone, inside the frame -------


@pytest.mark.parametrize("tool", TOOLS)
def test_explicit_window_is_sent_in_z_form(vl, tool):
    start, end = NOW - timedelta(hours=5), NOW - timedelta(hours=1, minutes=30)
    params = {
        **MINIMAL[tool],
        "start": start.astimezone(timezone(timedelta(hours=-8))).isoformat(),
        "end": end.astimezone(timezone(timedelta(hours=5, minutes=30))).isoformat(),
    }
    outcome = _run(vl, params, tool)
    assert outcome.status in ("ok", "no_data"), _view(outcome)
    sent = vl.sent()
    assert _Z_RFC3339.fullmatch(sent["start"]) and _Z_RFC3339.fullmatch(sent["end"])
    assert _close(_rfc3339_ns(sent["start"]) or 0, _ns(start))
    assert _close(_rfc3339_ns(sent["end"]) or 0, _ns(end))


@pytest.mark.parametrize(
    "window",
    [
        {"start": _z(NOW - timedelta(hours=1))},  # half a pair
        {"end": _z(NOW)},
        {"start": "2026-09-26T13:00:00", "end": _z(NOW)},  # no timezone
        {"start": "-3600", "end": "0"},  # upstream relative seconds
        {"start": "now-1h", "end": "now"},
        {"start": "1h", "end": "now"},
        {
            "start": str(int((NOW - timedelta(hours=1)).timestamp())),
            "end": str(int(NOW.timestamp())),
        },
        {"start": _z(NOW), "end": _z(NOW - timedelta(hours=1))},  # reversed
    ],
)
@pytest.mark.parametrize("tool", TOOLS)
def test_malformed_windows_are_refused_without_a_request(vl, tool, window):
    outcome = _run(vl, {**MINIMAL[tool], **window}, tool)
    assert outcome.status in ("error", "denied"), _view(outcome)
    assert vl.requests == []


@pytest.mark.parametrize(
    "window",
    [
        (NOW - timedelta(hours=7), NOW - timedelta(hours=5)),  # starts before
        (NOW - timedelta(hours=1), NOW + timedelta(minutes=10)),  # ends after
        (NOW - timedelta(days=400), NOW - timedelta(days=399)),
    ],
)
@pytest.mark.parametrize("tool", TOOLS)
def test_windows_outside_the_frame_are_refused_and_name_the_frame(vl, tool, window):
    params = {**MINIMAL[tool], "start": _z(window[0]), "end": _z(window[1])}
    outcome = _run(vl, params, tool)
    _refused_with(outcome, "QUERY_OUT_OF_WINDOW")
    text = _text(outcome)
    assert FRAME.start.isoformat() in text and FRAME.end.isoformat() in text
    assert vl.requests == []


# -- ⑤ limit, step, field ------------------------------------------------------------


@pytest.mark.parametrize(("limit", "sent"), [(1, "2"), (100, "101"), (500, "501")])
@pytest.mark.parametrize("tool", LIMITED)
def test_limit_bounds_are_sent_plus_one(vl, tool, limit, sent):
    outcome = _run(vl, {**MINIMAL[tool], "limit": limit}, tool)
    assert outcome.status in ("ok", "no_data"), _view(outcome)
    assert vl.sent()["limit"] == sent


@pytest.mark.parametrize("limit", [0, -1, 501, 5000])
@pytest.mark.parametrize("tool", LIMITED)
def test_limit_outside_1_to_500_is_refused(vl, tool, limit):
    outcome = _run(vl, {**MINIMAL[tool], "limit": limit}, tool)
    _refused_with(outcome, "INVALID_LIMIT")
    assert vl.requests == []


@pytest.mark.parametrize("limit", ["100", 10.5, True])
@pytest.mark.parametrize("tool", LIMITED)
def test_limit_of_the_wrong_type_is_refused(vl, tool, limit):
    outcome = _run(vl, {**MINIMAL[tool], "limit": limit}, tool)
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS"), _view(
        outcome
    )
    assert vl.requests == []


@pytest.mark.parametrize("tool", (FIELD_NAMES, HITS))
def test_tools_without_limit_refuse_a_limit(vl, tool):
    outcome = _run(vl, {**MINIMAL[tool], "limit": 10}, tool)
    assert outcome.status in ("error", "denied"), _view(outcome)
    assert vl.requests == []


@pytest.mark.parametrize("step", ["3s", "90s", "15m", "1h", "24h", "999999s"])
def test_valid_steps_are_sent_unchanged(vl, step):
    # The default hour: 1200 buckets of 3 s at most.
    outcome = _run(vl, {"query": "*", "step": step}, HITS)
    assert outcome.status in ("ok", "no_data"), _view(outcome)
    assert vl.sent()["step"] == step


@pytest.mark.parametrize(
    "step",
    [
        "", "5", "0s", "0m", "1000000s", "-5m", "+5m", "5d", "5w", "5ms", "1h30m",
        "1.5m", " 5m", "5m ", "5M", "five", "5m&field=x",
    ],
)  # fmt: skip
def test_malformed_steps_are_refused(vl, step):
    outcome = _run(vl, {"query": "*", "step": step}, HITS)
    _refused_with(outcome, "INVALID_STEP")
    assert vl.requests == []


@pytest.mark.parametrize("step", [5, 5.0, True])
def test_step_of_the_wrong_type_is_refused(vl, step):
    outcome = _run(vl, {"query": "*", "step": step}, HITS)
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS"), _view(
        outcome
    )
    assert vl.requests == []


def test_more_than_1440_buckets_is_refused_and_1440_is_sent(vl):
    executor, _ = _worker(_env(vl.url))
    # The default hour in 1 s or 2 s buckets: 3600 or 1800 buckets.
    for index, step in enumerate(("1s", "2s")):
        outcome = executor.execute(
            _call({"query": "*", "step": step}, HITS, index=index)
        )
        _refused_with(outcome, "INVALID_STEP")
    assert vl.requests == []
    # A 4 h window whose ends are multiples of 10 s (NOW is): exactly 1440
    # buckets of 10 s, however they are counted, is allowed ...
    assert int(NOW.timestamp()) % 10 == 0
    start, end = NOW - timedelta(hours=5), NOW - timedelta(hours=1)
    window = {"start": _z(start), "end": _z(end)}
    ok = executor.execute(_call({"query": "*", "step": "10s", **window}, HITS, index=2))
    assert ok.status in ("ok", "no_data"), _view(ok)
    assert vl.sent()["step"] == "10s"
    # ... one more bucket is not, nor 9 s buckets (1600).
    wider = {"start": _z(start - timedelta(seconds=10)), "end": _z(end)}
    for index, (step, span) in enumerate((("10s", wider), ("9s", window)), start=3):
        outcome = executor.execute(
            _call({"query": "*", "step": step, **span}, HITS, index=index)
        )
        _refused_with(outcome, "INVALID_STEP")
    assert len(vl.requests) == 1


@pytest.mark.parametrize(
    "field",
    ["level", "_stream", "_msg", "kubernetes.pod_name", "k8s:ns-1", "a" * 200, "0"],
)
@pytest.mark.parametrize("tool", (FIELD_VALUES, HITS))
def test_valid_field_names_are_sent(vl, tool, field):
    params = {"query": "*", "field": field} if tool == HITS else {"field": field}
    outcome = _run(vl, params, tool)
    assert outcome.status in ("ok", "no_data"), _view(outcome)
    assert vl.sent()["field"] == field


@pytest.mark.parametrize(
    "field",
    [
        "", "a" * 201, "level name", "level|x", "level,x", 'le"vel', "le'vel",
        "level)", "le\nvel", "lev\tel", "*", "level&limit=9", "é", "lvl/x",
    ],
)  # fmt: skip
@pytest.mark.parametrize("tool", (FIELD_VALUES, HITS))
def test_malformed_field_names_are_refused(vl, tool, field):
    params = {"query": "*", "field": field} if tool == HITS else {"field": field}
    outcome = _run(vl, params, tool)
    _refused_with(outcome, "INVALID_FIELD")
    assert vl.requests == []


def test_field_values_requires_field(vl):
    outcome = _run(vl, {"query": "*"}, FIELD_VALUES)
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS"), _view(
        outcome
    )
    assert vl.requests == []


# -- ③ query text: INVALID_QUERY and the LogsQL fail-closed scan (C5-7) -------------


@pytest.mark.parametrize("tool", WITH_QUERY_REQUIRED)
@pytest.mark.parametrize("query", ["", "   "])
def test_an_empty_required_query_is_refused(vl, tool, query):
    outcome = _run(vl, {"query": query}, tool)
    _refused_with(outcome, "INVALID_QUERY")
    assert vl.requests == []


@pytest.mark.parametrize("tool", WITH_QUERY_REQUIRED)
def test_a_missing_required_query_is_refused(vl, tool):
    outcome = _run(vl, {}, tool)
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS"), _view(
        outcome
    )
    assert vl.requests == []


@pytest.mark.parametrize(
    "query",
    [
        "x" * 4001,
        "error\nlevel:warn",
        "error\r",
        "error\tlevel:warn",
        'error "\x00"',
        "service:a\x1bb",
        "error\u2028x",
        # Unclosed string literals.
        '"error',
        "`error",
        'error "a\\"',  # the closing quote is escaped
        '{service="checkout} error',
    ],
)
@pytest.mark.parametrize("tool", TOOLS)
def test_overlong_unprintable_or_unclosed_queries_are_refused(vl, tool, query):
    params = {**MINIMAL[tool], "query": query}
    outcome = _run(vl, params, tool)
    _refused_with(outcome, "INVALID_QUERY")
    assert vl.requests == []


def test_a_query_of_exactly_4000_chars_is_sent(vl):
    prefix = 'error "'
    query = prefix + "y" * (4000 - len(prefix) - 1) + '"'
    assert len(query) == 4000
    outcome = _run(vl, {"query": query})
    assert outcome.status in ("ok", "no_data"), _view(outcome)
    assert vl.sent()["query"] == query


REFUSED_CONSTRUCTS = [
    # Whole words outside literals, any case.
    "* | join by (service) (level:error)",
    "* | JOIN by (service) (level:error)",
    "* | Join by (service) (level:error)",
    "options(ignore_global_time_filter=true) error",
    "OPTIONS(ignore_global_time_filter=true) error",
    "Options (ignore_global_time_filter=true) error",
    "* | union (service:other)",
    "* | UNION (service:other)",
    "error | stream_context before 10 after 10",
    "error | STREAM_CONTEXT after 5",
    # Subquery arguments: a '|' or ':' inside in(...), contains_any(...),
    # contains_all(...) outside literals.
    "trace_id:in(service:payment | fields trace_id)",
    "trace_id:in(* | keep trace_id)",
    "trace_id:IN(* | keep trace_id)",
    "trace_id:in (* | keep trace_id)",
    "trace_id:in(_time:1d)",
    "trace_id:in(a, (b | c))",
    "_msg:contains_any(* | fields word)",
    "_msg:CONTAINS_ANY(service:x)",
    "_msg:contains_all(* | fields word)",
    "_msg:contains_all(level:error)",
    'trace_id:in("a", * | fields trace_id)',
    # Single quotes outside literals (C5-7, as Loki).
    "'error'",
    "service:'checkout'",
    "\"a\" 'join'",
    "error' join '",
    # Bypass attempts: the word sits outside a literal after all.
    '"x" join',
    '"x"join',
    '"a\\"b" | join by (x) (y)',  # the escaped quote stays inside the literal
    '"a\\\\" join',  # an escaped backslash closes the literal
    "`a\\` join",  # backticks have no escapes: the literal is `a\`
    "`x``y` | union (z)",
    "error # join",  # '#' is not a comment: the whole string is scanned
    "error #| union (z)",
    "# options(ignore_global_time_filter=true)",
    "error |join by (x) (y)",
    "error |union(z)",
    "error\u00a0| join by (x) (y)",
    "(error) | stream_context(before 3)",
]


@pytest.mark.parametrize("query", REFUSED_CONSTRUCTS)
def test_logsql_constructs_outside_literals_are_refused(vl, query):
    outcome = _run(vl, {"query": query})
    # A non-breaking space may already be refused as unprintable.
    if "\u00a0" in query and "INVALID_QUERY" in _text(outcome):
        assert vl.requests == []
        return
    _refused_with(outcome, "QUERY_CONSTRUCT_NOT_ALLOWED")
    assert vl.requests == []


@pytest.mark.parametrize(
    "query",
    [
        "* | join by (service) (level:error)",
        "options(ignore_global_time_filter=true) *",
        "* | union (service:other)",
        "trace_id:in(* | keep trace_id)",
        "'x'",
    ],
)
@pytest.mark.parametrize("tool", TOOLS)
def test_every_tool_scans_its_query(vl, tool, query):
    outcome = _run(vl, {**MINIMAL[tool], "query": query}, tool)
    _refused_with(outcome, "QUERY_CONSTRUCT_NOT_ALLOWED")
    assert vl.requests == []


@pytest.mark.parametrize(
    "query",
    [
        # The words inside string literals.
        '"join"',
        '_msg:"user rejoined via join link"',
        '"options(ignore_global_time_filter=true)"',
        "`union`",
        '"stream_context"',
        '"x | union (y)"',
        '"trace_id:in(* | keep trace_id)"',
        "`it's`",
        '"it\'s"',
        '"a\\"join\\""',  # escaped quotes inside one literal
        "`a\\` error",  # backslash inside backticks, then a plain word
        # Not whole words.
        "joined",
        "rejoin",
        "reunion",
        "unions",
        "optionsx",
        "stream_contexts",
        # in/contains_* without a subquery.
        "error in checkout",
        "service:in(checkout, payment)",
        'service:in("a|b", "c:d")',
        "_msg:contains_any(timeout, refused)",
        "_msg:contains_all(db, timeout)",
        # Allowed by C5-7.
        "_time:5m error",
        "_time:[2026-09-26T13:00:00Z, 2026-09-26T14:00:00Z] error",
        "* | delete password",
        "* | rename a as b",
        "* | stats by (level) count() rows",
        "error #tag",
        "level:error | fields _time, _msg",
    ],
)
def test_allowed_queries_are_sent_unchanged(vl, query):
    outcome = _run(vl, {"query": query})
    assert outcome.status in ("ok", "no_data"), _view(outcome)
    assert vl.sent()["query"] == query


# -- ⑥ projection, ordering, truncation, size limits --------------------------------


def test_query_rows_are_whole_objects_newest_first(vl):
    first = NOW - timedelta(minutes=40)
    middle = NOW - timedelta(minutes=20)
    last = NOW - timedelta(minutes=5)
    vl.add(first, "slow request", 123, level="warn", pod="checkout-1")
    vl.add(last, "retry exhausted", 7, level="error", pod="checkout-1")
    vl.add(middle, "payment failed", 456789, level="error", pod="checkout-2")
    outcome = _run(vl, {"query": "*"})
    assert outcome.status == "ok", _view(outcome)
    rows = _rows(outcome)
    assert [row["_msg"] for row in rows] == [
        "retry exhausted",
        "payment failed",
        "slow request",
    ]
    assert rows[1]["pod"] == "checkout-2" and rows[1]["level"] == "error"
    assert rows[0]["_stream"] == '{service="checkout"}'
    assert [_rfc3339_ns(row["_time"]) for row in rows] == [
        _ns(last, 7),
        _ns(middle, 456789),
        _ns(first, 123),
    ]
    view = _view(outcome)
    # Source time from the rows' _time (C5-9).
    assert _close(_ns(_instant(view["source_start_at"])), _ns(first, 123))
    assert _close(_ns(_instant(view["source_end_at"])), _ns(last, 7))
    assert view["source"] == "otel-demo" and view["target_id"] == TARGET_ID
    assert view["incomplete"] is False
    assert f"backend_{ROW_UNIT[QUERY]}_returned" in view, view


def test_rows_without_time_follow_in_their_original_order(vl):
    lines = [
        {"level": "error", "rows": "7"},
        {"_time": _vl_time(NOW - timedelta(minutes=30)), "_msg": "older"},
        {"level": "warn", "rows": "3"},
        {"_time": _vl_time(NOW - timedelta(minutes=10)), "_msg": "newer"},
        {"level": "info", "rows": "1"},
    ]
    vl.raw = b"".join(_json(line) + b"\n" for line in lines)
    outcome = _run(vl, {"query": "* | stats by (level) count() rows"})
    assert outcome.status == "ok", _view(outcome)
    rows = _rows(outcome)
    assert [row.get("_msg") for row in rows[:2]] == ["newer", "older"]
    assert [row.get("level") for row in rows[2:]] == ["error", "warn", "info"]


def test_query_source_time_is_clamped_into_the_window_or_is_the_window(vl):
    window = {
        "start": _z(NOW - timedelta(hours=3)),
        "end": _z(NOW - timedelta(hours=2)),
    }
    vl.raw = b"".join(
        _json(row) + b"\n"
        for row in (
            {"_time": _vl_time(NOW - timedelta(hours=4)), "_msg": "before"},
            {"_time": _vl_time(NOW - timedelta(hours=2, minutes=30)), "_msg": "in"},
            {"_time": _vl_time(NOW - timedelta(hours=1)), "_msg": "after"},
        )
    )
    outcome = _run(vl, {"query": "*", **window})
    assert outcome.status == "ok", _view(outcome)
    view = _view(outcome)
    assert _instant(view["source_start_at"]) == NOW - timedelta(hours=3)
    assert _instant(view["source_end_at"]) == NOW - timedelta(hours=2)

    vl.raw = _json({"level": "error", "rows": "4"}) + b"\n"
    vl.requests.clear()
    outcome = _run(vl, {"query": "* | stats by (level) count() rows", **window})
    assert outcome.status == "ok", _view(outcome)
    view = _view(outcome)
    assert _instant(view["source_start_at"]) == NOW - timedelta(hours=3)
    assert _instant(view["source_end_at"]) == NOW - timedelta(hours=2)


def test_query_at_limit_plus_one_is_incomplete_and_keeps_the_newest(vl):
    for minute in range(1, 11):
        vl.add(NOW - timedelta(minutes=minute), f"line-{minute}")
    outcome = _run(vl, {"query": "*", "limit": 3})
    assert outcome.status == "ok", _view(outcome)
    assert vl.sent()["limit"] == "4"
    assert [row["_msg"] for row in _rows(outcome)] == ["line-1", "line-2", "line-3"]
    view = _view(outcome)
    assert view["incomplete"] is True
    assert "limit" in str(view["incomplete_reason"]).lower()


def test_exactly_limit_rows_are_complete(vl):
    # Unlike Loki, the extra row tells the tool whether more exist.
    for minute in range(1, 4):
        vl.add(NOW - timedelta(minutes=minute), f"line-{minute}")
    outcome = _run(vl, {"query": "*", "limit": 3})
    assert outcome.status == "ok", _view(outcome)
    assert len(_rows(outcome)) == 3
    assert _view(outcome)["incomplete"] is False


def test_streams_are_sorted_by_hits_then_stream(vl):
    vl.streams = [
        ('{service="a"}', 5),
        ('{service="c"}', 9),
        ('{service="b"}', 9),
        ('{service="d"}', 1),
    ]
    outcome = _run(vl, {}, STREAMS)
    assert outcome.status == "ok", _view(outcome)
    assert _rows(outcome) == [
        {"stream": '{service="b"}', "hits": 9},
        {"stream": '{service="c"}', "hits": 9},
        {"stream": '{service="a"}', "hits": 5},
        {"stream": '{service="d"}', "hits": 1},
    ]
    view = _view(outcome)
    assert view["incomplete"] is False
    assert f"backend_{ROW_UNIT[STREAMS]}_returned" in view, view


def test_streams_over_the_limit_keep_the_first_limit_after_sorting(vl):
    vl.streams = [
        ('{service="a"}', 2),
        ('{service="b"}', 8),
        ('{service="c"}', 5),
        ('{service="d"}', 9),
        ('{service="e"}', 100),  # beyond limit+1: never returned by the fake
    ]
    outcome = _run(vl, {"limit": 3}, STREAMS)
    assert outcome.status == "ok", _view(outcome)
    assert vl.sent()["limit"] == "4"
    assert [row["stream"] for row in _rows(outcome)] == [
        '{service="d"}',
        '{service="b"}',
        '{service="c"}',
    ]
    view = _view(outcome)
    assert view["incomplete"] is True
    assert "limit" in str(view["incomplete_reason"]).lower()


def test_field_names_rows_are_field_and_hits_sorted(vl):
    vl.field_names = [("level", 40), ("_msg", 90), ("_time", 90), ("pod", 3)]
    outcome = _run(vl, {"query": "service:checkout"}, FIELD_NAMES)
    assert outcome.status == "ok", _view(outcome)
    assert _rows(outcome) == [
        {"field": "_msg", "hits": 90},
        {"field": "_time", "hits": 90},
        {"field": "level", "hits": 40},
        {"field": "pod", "hits": 3},
    ]
    assert _view(outcome)["incomplete"] is False


def test_field_values_rows_carry_the_field_and_respect_limit(vl):
    vl.field_values = [("warn", 4), ("error", 12), ("info", 30)]
    outcome = _run(vl, {"field": "level"}, FIELD_VALUES)
    assert outcome.status == "ok", _view(outcome)
    assert _rows(outcome) == [
        {"value": "info", "hits": 30},
        {"value": "error", "hits": 12},
        {"value": "warn", "hits": 4},
    ]
    view = _view(outcome)
    assert view["field"] == "level"
    assert view["incomplete"] is False

    vl.requests.clear()
    outcome = _run(vl, {"field": "level", "limit": 2}, FIELD_VALUES)
    assert outcome.status == "ok", _view(outcome)
    assert vl.sent()["limit"] == "3"
    assert len(_rows(outcome)) == 2
    assert _view(outcome)["incomplete"] is True


def test_hits_series_keep_their_shape_and_drop_zero_totals(vl):
    base = int((NOW - timedelta(minutes=60)).timestamp()) // 300 * 300
    stamps = [
        datetime.fromtimestamp(base + 300 * n, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
        for n in range(3)
    ]
    vl.hits = [
        {
            "fields": {"level": "error"},
            "timestamps": stamps,
            "values": [1, 0, 4],
            "total": 5,
        },
        {"fields": {"level": "debug"}, "timestamps": [], "values": [], "total": 0},
        {
            "fields": {"level": "warn"},
            "timestamps": stamps[:1],
            "values": [2],
            "total": 2,
        },
    ]
    outcome = _run(vl, {"query": "*", "field": "level"}, HITS)
    assert outcome.status == "ok", _view(outcome)
    rows = _rows(outcome)
    assert sorted(rows, key=lambda r: r["fields"]["level"]) == [
        {
            "fields": {"level": "error"},
            "timestamps": stamps,
            "values": [1, 0, 4],
            "total": 5,
        },
        {
            "fields": {"level": "warn"},
            "timestamps": stamps[:1],
            "values": [2],
            "total": 2,
        },
    ]
    view = _view(outcome)
    assert f"backend_{ROW_UNIT[HITS]}_returned" in view, view
    # The step-aligned first bucket may start before the window (notes say so).
    notes = " ".join(str(note) for note in view.get("notes") or []).lower()
    assert "step" in notes or "bucket" in notes, view


@pytest.mark.parametrize("tool", (STREAMS, FIELD_NAMES, FIELD_VALUES, HITS))
def test_metadata_source_time_is_the_query_window(vl, tool):
    vl.streams = [('{service="a"}', 3)]
    vl.field_names = [("level", 3)]
    vl.field_values = [("error", 3)]
    vl.hits = [
        {
            "fields": {},
            "timestamps": [_z(NOW - timedelta(hours=4, minutes=1))],
            "values": [3],
            "total": 3,
        }
    ]
    start, end = NOW - timedelta(hours=4), NOW - timedelta(hours=3)
    params = {**MINIMAL[tool], "start": _z(start), "end": _z(end)}
    outcome = _run(vl, params, tool)
    assert outcome.status == "ok", _view(outcome)
    view = _view(outcome)
    assert _instant(view["source_start_at"]) == start
    assert _instant(view["source_end_at"]) == end
    assert view["window"] == {"start": start.isoformat(), "end": end.isoformat()}
    assert view.get("cannot_prove"), view


def test_long_values_are_cut_to_2000_chars_and_listed(vl):
    vl.add(NOW - timedelta(minutes=2), "x" * 5000, build="b" * 3000, level="error")
    vl.add(NOW - timedelta(minutes=1), "short", level="info")
    outcome = _run(vl, {"query": "*"})
    assert outcome.status == "ok", _view(outcome)
    rows = _rows(outcome)
    long_row = next(row for row in rows if row["_msg"].startswith("x"))
    assert len(long_row["_msg"]) == 2000 and len(long_row["build"]) == 2000
    assert sorted(long_row["_truncated_fields"]) == ["_msg", "build"]
    short_row = next(row for row in rows if row["_msg"] == "short")
    assert not short_row.get("_truncated_fields")
    text = _text(outcome)
    assert "x" * 2001 not in text and "b" * 2001 not in text


def test_long_metadata_values_are_cut(vl):
    vl.field_values = [("v" * 3000, 2), ("short", 1)]
    outcome = _run(vl, {"field": "path"}, FIELD_VALUES)
    assert outcome.status == "ok", _view(outcome)
    text = _text(outcome)
    assert "v" * 2000 in text and "v" * 2001 not in text


def test_secrets_in_logs_and_values_are_redacted(vl):
    vl.add(NOW - timedelta(minutes=1), f"user login password={CANARY} accepted")
    outcome = _run(vl, {"query": "*"})
    assert outcome.status == "ok", _view(outcome)
    text = _text(outcome)
    assert CANARY not in text and "REDACTED" in text

    vl.field_values = [(f"password={CANARY}", 1)]
    outcome = _run(vl, {"field": "_msg"}, FIELD_VALUES)
    assert outcome.status == "ok", _view(outcome)
    text = _text(outcome)
    assert CANARY not in text and "REDACTED" in text


@pytest.mark.parametrize("tool", (QUERY, FIELD_VALUES, HITS))
def test_raw_response_over_2_mib_is_refused_whole(vl, tool):
    big = "y" * (2 * 1024 * 1024 + 1)
    if tool == QUERY:
        vl.raw = _json({"_time": _vl_time(NOW - timedelta(minutes=1)), "_msg": big})
    elif tool == FIELD_VALUES:
        vl.raw = _json({"values": [{"value": big, "hits": 1}]})
    else:
        vl.raw = _json(
            {
                "hits": [
                    {"fields": {"x": big}, "timestamps": [], "values": [], "total": 1}
                ]
            }
        )
    outcome = _run(vl, dict(MINIMAL[tool]), tool)
    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE"), _view(
        outcome
    )
    assert _view(outcome)["content"] is None


def test_view_over_25k_tokens_is_refused_whole_with_a_narrowing_hint(vl):
    words = " ".join(f"word{n} checkout latency spike" for n in range(60))
    assert len(words) < 2000
    for n in range(500):
        vl.add(NOW - timedelta(seconds=n + 1), words)
    outcome = _run(vl, {"query": "*", "limit": 500})
    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
    view = _view(outcome)
    assert view["content"] is None
    hint = json.dumps({k: v for k, v in view.items() if k != "query"}).lower()
    assert "limit" in hint


def test_empty_results_are_no_data(vl):
    executor, _ = _worker(_env(vl.url))
    for index, tool in enumerate(TOOLS):
        outcome = executor.execute(_call(dict(MINIMAL[tool]), tool, index=index))
        assert (outcome.status, outcome.reason) == ("no_data", "NO_DATA"), (
            tool,
            _view(outcome),
        )
    # Hits whose every series totals 0 leave no row (interpretation: 0 rows
    # after dropping the zero series is no_data).
    vl.hits = [{"fields": {}, "timestamps": [], "values": [], "total": 0}]
    outcome = executor.execute(_call({"query": "*"}, HITS, index=9))
    assert (outcome.status, outcome.reason) == ("no_data", "NO_DATA"), _view(outcome)


# -- ⑦ error classification (C5-11) ---------------------------------------------------


def test_400_is_invalid_params_with_truncated_redacted_text(vl):
    message = (
        f"cannot parse query [error |]: missing pipe after '|'; password={CANARY} "
    ) + "z" * 20000
    vl.fail = (400, "text/plain; charset=utf-8", message.encode())
    outcome = _run(vl, {"query": "error | bogus"})
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS"), _view(
        outcome
    )
    text = _text(outcome)
    assert "cannot parse query" in text
    assert CANARY not in text
    assert "z" * 5000 not in text
    assert len(vl.requests) == 1


GATEWAY_403 = (
    b"<html>\r\n<head><title>403 Forbidden</title></head>\r\n<body>\r\n"
    b"<center><h1>403 Forbidden</h1></center>\r\n<hr><center>nginx</center>\r\n"
    b"</body>\r\n</html>\r\n"
)


@pytest.mark.parametrize(
    ("status", "content_type", "body", "reason"),
    [
        (401, "text/plain", b"unauthorized marker-hhhh569\n", "SOURCE_FORBIDDEN"),
        (403, "text/plain", b"forbidden marker-hhhh569\n", "SOURCE_FORBIDDEN"),
        (403, "text/html", GATEWAY_403, "SOURCE_FORBIDDEN"),
        (404, "text/plain", b"missing marker-hhhh569\n", "SOURCE_ERROR"),
        (408, "text/plain", b"timeout\n", "SOURCE_UNAVAILABLE"),
        (429, "text/plain", b"too many requests\n", "SOURCE_UNAVAILABLE"),
        (500, "text/plain", b"internal error\n", "SOURCE_UNAVAILABLE"),
        (502, "text/html", b"<html>502 Bad Gateway</html>", "SOURCE_UNAVAILABLE"),
        (503, "text/plain", b"service unavailable\n", "SOURCE_UNAVAILABLE"),
    ],
)
@pytest.mark.parametrize("tool", (QUERY, STREAMS, HITS))
def test_backend_statuses_map_to_fixed_reasons_with_one_request(
    vl, tool, status, content_type, body, reason
):
    vl.fail = (status, content_type, body)
    outcome = _run(vl, dict(MINIMAL[tool]), tool)
    assert (outcome.status, outcome.reason) == ("error", reason), _view(outcome)
    assert len(vl.requests) == 1  # one request per call, no retry
    if status in (401, 403, 404):
        # Only the status, not the body (C5-11).
        assert "marker-hhhh569" not in _text(outcome)


def test_an_unreachable_backend_is_source_unavailable(vl):
    url = vl.url
    vl.stop()
    executor, _ = _worker(_env(url))
    outcome = executor.execute(_call({"query": "*"}))
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_UNAVAILABLE"), _view(
        outcome
    )


def test_a_query_past_the_call_deadline_is_a_timeout(vl):
    vl.delay = 4.0
    executor, _ = _worker(_env(vl.url), deadline_seconds=2)
    started = time.monotonic()
    outcome = executor.execute(_call({"query": "*"}))
    assert outcome.status == "timeout", _view(outcome)
    assert time.monotonic() - started < 3.9


def test_a_malformed_body_is_an_error_not_data(vl):
    vl.raw = b"<html>not json</html>"
    outcome = _run(vl, {}, STREAMS)
    assert outcome.status == "error", _view(outcome)


# -- ⑧ credentials: the vlogs-ro slot, no fallback, never shown ---------------------


def _authorization(record: Mapping[str, Any]) -> str:
    return record["headers"].get("authorization", "")


def _basic(username: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()


def test_bearer_and_basic_credentials_go_only_to_victorialogs(vl):
    loki = FakeVictoriaLogs()  # stands in for Loki: records what it receives
    loki.start()
    try:
        executor, _ = _worker(
            _env(
                vl.url,
                OPSPILOT_LOKI_URL=loki.url,
                OPSPILOT_LOKI_BEARER_TOKEN=LOKI_BEARER,
            )
        )
        executor.execute(_call({"query": "*"}, QUERY, index=0))
        executor.execute(_call({}, STREAMS, index=1))
        executor.execute(
            _call({"query": '{app="checkout"}'}, "grafana_loki_query", index=2)
        )
    finally:
        loki.stop()
    assert {_authorization(r) for r in vl.requests} == {f"Bearer {VL_BEARER}"}
    assert loki.requests, "the Loki tool should have reached its own fake"
    assert {_authorization(r) for r in loki.requests} == {f"Bearer {LOKI_BEARER}"}
    for record in vl.requests:
        flat = json.dumps(record["headers"])
        assert LOKI_BEARER not in flat and K8S_TOKEN not in flat
        assert PROM_PASSWORD not in flat

    vl.requests.clear()
    executor, _ = _worker(_basic_env(vl.url))
    executor.execute(_call({"query": "*"}))
    assert {_authorization(r) for r in vl.requests} == {
        _basic(VL_USERNAME, VL_PASSWORD)
    }


def test_no_fallback_to_another_slot_without_a_victorialogs_credential(vl):
    env = _env(
        vl.url,
        OPSPILOT_VICTORIALOGS_BEARER_TOKEN="",
        OPSPILOT_LOKI_URL="http://127.0.0.1:9",
        OPSPILOT_LOKI_BEARER_TOKEN=LOKI_BEARER,
        OPSPILOT_ES_URL="http://127.0.0.1:9",
        OPSPILOT_ES_API_KEY=ES_API_KEY,
    )
    with pytest.raises(SystemExit, match="VICTORIALOGS_CONFIG_MISSING"):
        _worker(env)
    assert vl.requests == []


def test_tenant_headers_come_only_from_configuration(vl):
    executor, _ = _worker(
        _env(
            vl.url,
            OPSPILOT_VICTORIALOGS_ACCOUNT_ID="42",
            OPSPILOT_VICTORIALOGS_PROJECT_ID="7",
        )
    )
    for index, tool in enumerate(TOOLS):
        executor.execute(_call(dict(MINIMAL[tool]), tool, index=index))
    assert len(vl.requests) == 5
    for record in vl.requests:
        assert record["headers"].get("accountid") == "42", record["headers"]
        assert record["headers"].get("projectid") == "7", record["headers"]

    vl.requests.clear()
    executor, _ = _worker(_env(vl.url, OPSPILOT_VICTORIALOGS_ACCOUNT_ID="4294967295"))
    executor.execute(_call({"query": "*"}))
    headers = vl.only()["headers"]
    assert headers.get("accountid") == "4294967295"
    assert "projectid" not in headers

    vl.requests.clear()
    executor, _ = _worker(_env(vl.url))
    executor.execute(_call({"query": "*"}))
    headers = vl.only()["headers"]
    assert "accountid" not in headers and "projectid" not in headers


def test_credentials_never_appear_in_views_notes_or_trace(vl):
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from tests.test_tracing import lab_env

    vl.add(NOW - timedelta(minutes=5), "ok")
    exporter = InMemorySpanExporter()
    tracer = tracing.configure(
        lab_env(
            OPSPILOT_TOOL_PROFILE=EXT_PROFILE,
            OPSPILOT_K8S_API_URL=K8S_API,
            OPSPILOT_VICTORIALOGS_URL=vl.url,
            OPSPILOT_OTEL_PROMETHEUS_URL="http://127.0.0.1:19090",
            OPSPILOT_OTEL_JAEGER_URL="http://127.0.0.1:16686",
        ),
        exporter=exporter,
    )
    try:
        views = []
        for env in (_env(vl.url), _basic_env(vl.url)):
            executor, sink = _worker(env)
            with tracer.run(incident_id="c5b-incident", run_id=RUN_ID, versions={}):
                views.append(executor.execute(_call({"query": "*"}, QUERY, index=0)))
                vl.fail = (401, "text/plain", b"unauthorized\n")
                views.append(executor.execute(_call({}, STREAMS, index=1)))
                vl.fail = (400, "text/plain", b"cannot parse query: unexpected token\n")
                views.append(executor.execute(_call({"query": "a |"}, QUERY, index=2)))
                vl.fail = None
                views.append(
                    executor.execute(_call({"query": "* | join (x)"}, QUERY, index=3))
                )
            views.extend(sink.records)
        tracer.shutdown()
        spans = exporter.get_finished_spans()
    finally:
        tracing.reset()
    assert any("victorialogs" in span.name for span in spans)
    exported = json.dumps([dict(span.attributes or {}) for span in spans], default=str)
    rendered = json.dumps(
        [getattr(v, "model_view", None) or getattr(v, "_view", None) for v in views],
        default=str,
    )
    raw = b"".join(getattr(v, "raw", b"") or b"" for v in views)
    for secret in (
        VL_BEARER,
        VL_PASSWORD,
        base64.b64encode(f"{VL_USERNAME}:{VL_PASSWORD}".encode()).decode(),
    ):
        assert secret not in exported
        assert secret not in rendered
        assert secret.encode() not in raw


# -- ⑩ startup codes, and the web does not read the credentials ----------------------


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        # A credential is required with the address.
        ({"OPSPILOT_VICTORIALOGS_BEARER_TOKEN": ""}, "VICTORIALOGS_CONFIG_MISSING"),
        # Basic and bearer are exclusive.
        (
            {
                "OPSPILOT_VICTORIALOGS_USERNAME": VL_USERNAME,
                "OPSPILOT_VICTORIALOGS_PASSWORD": VL_PASSWORD,
            },
            "VICTORIALOGS_CONFIG_INVALID",
        ),
        (
            {"OPSPILOT_VICTORIALOGS_USERNAME": VL_USERNAME},
            "VICTORIALOGS_CONFIG_INVALID",
        ),
        # Half a basic pair is INVALID (interface: "两种凭据都配或半对").
        (
            {
                "OPSPILOT_VICTORIALOGS_BEARER_TOKEN": "",
                "OPSPILOT_VICTORIALOGS_USERNAME": VL_USERNAME,
            },
            "VICTORIALOGS_CONFIG_INVALID",
        ),
        (
            {
                "OPSPILOT_VICTORIALOGS_BEARER_TOKEN": "",
                "OPSPILOT_VICTORIALOGS_PASSWORD": VL_PASSWORD,
            },
            "VICTORIALOGS_CONFIG_INVALID",
        ),
        # The address: no userinfo, query string or fragment; http(s) only.
        (
            {"OPSPILOT_VICTORIALOGS_URL": "http://reader:iiii569@127.0.0.1:9428"},
            "VICTORIALOGS_CONFIG_INVALID",
        ),
        (
            {"OPSPILOT_VICTORIALOGS_URL": "http://reader@127.0.0.1:9428"},
            "VICTORIALOGS_CONFIG_INVALID",
        ),
        (
            {"OPSPILOT_VICTORIALOGS_URL": "http://127.0.0.1:9428?x=1"},
            "VICTORIALOGS_CONFIG_INVALID",
        ),
        (
            {"OPSPILOT_VICTORIALOGS_URL": "http://127.0.0.1:9428#top"},
            "VICTORIALOGS_CONFIG_INVALID",
        ),
        (
            {"OPSPILOT_VICTORIALOGS_URL": "ftp://127.0.0.1:9428"},
            "VICTORIALOGS_CONFIG_INVALID",
        ),
        (
            {"OPSPILOT_VICTORIALOGS_URL": "127.0.0.1:9428"},
            "VICTORIALOGS_CONFIG_INVALID",
        ),
        # A CA file only with https.
        (
            {"OPSPILOT_VICTORIALOGS_CA_FILE": "/run/secrets/vl-ca.pem"},
            "VICTORIALOGS_CONFIG_INVALID",
        ),
        # Tenant IDs: decimal unsigned 32-bit.
        ({"OPSPILOT_VICTORIALOGS_ACCOUNT_ID": "-1"}, "VICTORIALOGS_CONFIG_INVALID"),
        (
            {"OPSPILOT_VICTORIALOGS_ACCOUNT_ID": "4294967296"},
            "VICTORIALOGS_CONFIG_INVALID",
        ),
        ({"OPSPILOT_VICTORIALOGS_ACCOUNT_ID": "0x10"}, "VICTORIALOGS_CONFIG_INVALID"),
        ({"OPSPILOT_VICTORIALOGS_ACCOUNT_ID": "1.5"}, "VICTORIALOGS_CONFIG_INVALID"),
        ({"OPSPILOT_VICTORIALOGS_ACCOUNT_ID": "team-a"}, "VICTORIALOGS_CONFIG_INVALID"),
        ({"OPSPILOT_VICTORIALOGS_ACCOUNT_ID": " 7"}, "VICTORIALOGS_CONFIG_INVALID"),
        ({"OPSPILOT_VICTORIALOGS_ACCOUNT_ID": "1|2"}, "VICTORIALOGS_CONFIG_INVALID"),
        ({"OPSPILOT_VICTORIALOGS_PROJECT_ID": "-3"}, "VICTORIALOGS_CONFIG_INVALID"),
        (
            {"OPSPILOT_VICTORIALOGS_PROJECT_ID": "99999999999"},
            "VICTORIALOGS_CONFIG_INVALID",
        ),
        ({"OPSPILOT_VICTORIALOGS_PROJECT_ID": "p1"}, "VICTORIALOGS_CONFIG_INVALID"),
    ],
)
def test_worker_refuses_conflicting_victorialogs_configuration(overrides, code):
    _require_jq()
    env = _env("http://127.0.0.1:9428", **overrides)
    with pytest.raises(SystemExit, match=code):
        _worker(env)


@pytest.mark.parametrize(
    "extra",
    [
        {"OPSPILOT_VICTORIALOGS_BEARER_TOKEN": VL_BEARER},
        {"OPSPILOT_VICTORIALOGS_USERNAME": VL_USERNAME},
        {"OPSPILOT_VICTORIALOGS_PASSWORD": VL_PASSWORD},
        {"OPSPILOT_VICTORIALOGS_CA_FILE": "/run/secrets/vl-ca.pem"},
        {"OPSPILOT_VICTORIALOGS_ACCOUNT_ID": "7"},
        {"OPSPILOT_VICTORIALOGS_PROJECT_ID": "7"},
    ],
)
def test_settings_without_the_address_are_invalid_for_the_worker(extra):
    _require_jq()
    with pytest.raises(SystemExit, match="VICTORIALOGS_CONFIG_INVALID"):
        _worker(_env(None, **extra))


def test_tenant_id_bounds_are_accepted(vl):
    for value in ("0", "4294967295"):
        executor, _ = _worker(
            _env(
                vl.url,
                OPSPILOT_VICTORIALOGS_ACCOUNT_ID=value,
                OPSPILOT_VICTORIALOGS_PROJECT_ID=value,
            )
        )
        outcome = executor.execute(_call({"query": "*"}))
        assert outcome.status in ("ok", "no_data"), _view(outcome)


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


def test_web_side_profile_reads_no_victorialogs_credential():
    env = _TrackingEnv(
        _basic_env(
            "https://127.0.0.1:9428",
            OPSPILOT_VICTORIALOGS_CA_FILE="/nonexistent/vl-ca.pem",
            OPSPILOT_VICTORIALOGS_ACCOUNT_ID="7",
        )
    )
    profile = select_profile(env)
    face = profile.face(FakeClock(start=NOW))
    face.evidence_context(RUN_ID)
    profile.versions()
    assert set(TOOLS) <= {s["function"]["name"] for s in face.tool_schemas}
    assert not env.read & set(VL_CREDENTIAL_NAMES)
    assert not env.iterated


# -- ⑫ lab gate -------------------------------------------------------------------


def test_lab_gate_requires_a_lab_victorialogs_host():
    env = {
        "OPSPILOT_TRACE": "lab",
        PROFILE_ENV: EXT_PROFILE,
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://127.0.0.1:19090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://127.0.0.1:16686",
        "OPSPILOT_K8S_API_URL": K8S_API,
        "LANGSMITH_PROJECT": "opspilot-lab-c5b",
        "LANGSMITH_API_KEY": "lsv2-aaaaaaaa569",
        "LANGSMITH_ENDPOINT": "https://api.smith.langchain.com",
    }
    assert check_lab_target(env) == ()
    for lab_url in (
        "http://127.0.0.1:9428",
        "http://localhost:9428",
        "http://victorialogs.monitoring.svc:9428",
        "http://vlogs.logging.svc.cluster.local:9428/prefix",
    ):
        assert check_lab_target({**env, "OPSPILOT_VICTORIALOGS_URL": lab_url}) == (), (
            lab_url
        )
    for remote in (
        "https://logs.example.invalid:9428",
        "http://reader@127.0.0.1:9428",
        "http://127.attacker.example:9428",
    ):
        failures = check_lab_target({**env, "OPSPILOT_VICTORIALOGS_URL": remote})
        assert "VICTORIALOGS_NOT_LAB" in failures, remote
    # Other backends never raise this code.
    others = check_lab_target(
        {
            **env,
            "OPSPILOT_LOKI_URL": "https://logs.example.invalid:3100",
            "OPSPILOT_ES_URL": "https://logs.example.invalid:9200",
        }
    )
    assert "VICTORIALOGS_NOT_LAB" not in others


# -- HTTPS with OPSPILOT_VICTORIALOGS_CA_FILE -------------------------------------


def test_https_is_verified_against_the_configured_ca(tmp_path):
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is needed to mint the synthetic CA")
    cert, key = tmp_path / "vl-cert.pem", tmp_path / "vl-key.pem"
    subprocess.run(
        [
            openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cert), "-days", "2",
            "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    fake = FakeVictoriaLogs()
    fake.start(tls=(cert, key))
    try:
        trusted, _ = _worker(_env(fake.url, OPSPILOT_VICTORIALOGS_CA_FILE=str(cert)))
        outcome = trusted.execute(_call({"query": "*"}))
        assert outcome.status in ("ok", "no_data"), _view(outcome)
        assert {_authorization(r) for r in fake.requests} == {f"Bearer {VL_BEARER}"}
        fake.requests.clear()
        untrusted, _ = _worker(_env(fake.url))
        outcome = untrusted.execute(_call({"query": "*"}))
        assert outcome.status == "error"
        assert fake.requests == []
    finally:
        fake.stop()


# -- deploy entrypoint passes OPSPILOT_VICTORIALOGS_* to the worker -------------------


def test_entrypoint_imports_victorialogs_settings_from_the_worker_env_file(tmp_path):
    entrypoint = ROOT / "deploy" / "docker-entrypoint.sh"
    worker_env = tmp_path / "worker_env"
    worker_env.write_text(
        "OPSPILOT_VICTORIALOGS_URL=http://127.0.0.1:9428\n"
        f"OPSPILOT_VICTORIALOGS_BEARER_TOKEN={VL_BEARER}\n"
        f"OPSPILOT_VICTORIALOGS_USERNAME={VL_USERNAME}\n"
        f"OPSPILOT_VICTORIALOGS_PASSWORD='{VL_PASSWORD}'\n"
        "OPSPILOT_VICTORIALOGS_CA_FILE=/run/secrets/vl-ca.pem\n"
        "OPSPILOT_VICTORIALOGS_ACCOUNT_ID=42\n"
        "OPSPILOT_VICTORIALOGS_PROJECT_ID=7\n"
        "OPSPILOT_VICTORIALOGSX=not-a-setting\n"
        "OPSPILOT_VICTORIALOGS_$(touch pwned)=x\n"
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
    assert env["OPSPILOT_VICTORIALOGS_URL"] == "http://127.0.0.1:9428"
    assert env["OPSPILOT_VICTORIALOGS_BEARER_TOKEN"] == VL_BEARER
    assert env["OPSPILOT_VICTORIALOGS_USERNAME"] == VL_USERNAME
    assert env["OPSPILOT_VICTORIALOGS_PASSWORD"] == VL_PASSWORD
    assert env["OPSPILOT_VICTORIALOGS_CA_FILE"] == "/run/secrets/vl-ca.pem"
    assert env["OPSPILOT_VICTORIALOGS_ACCOUNT_ID"] == "42"
    assert env["OPSPILOT_VICTORIALOGS_PROJECT_ID"] == "7"
    assert "OPSPILOT_VICTORIALOGSX" not in env
    assert not (tmp_path / "pwned").exists()


# -- the read-only gateway's VictoriaLogs backend (C5-U1 second layer) ---------------


def _gateway(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-I",
            str(ROOT / "scripts" / "loki_readonly_gateway.py"),
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_gateway_default_backend_renders_byte_identical_loki_config():
    for args, sha in (
        (GATEWAY_CI_ARGS, GATEWAY_CI_SHA256),
        (("render", "--server", "8080=loki3:3100"), GATEWAY_ONE_SHA256),
    ):
        result = _gateway(*args)
        assert result.returncode == 0, result.stderr
        assert hashlib.sha256(result.stdout.encode()).hexdigest() == sha, args
        explicit = _gateway(args[0], "--backend", "loki", *args[1:])
        assert explicit.returncode == 0, explicit.stderr
        assert explicit.stdout == result.stdout


def test_gateway_victorialogs_backend_allows_only_the_five_read_paths():
    # ASSUMPTION: --backend is an option of render (and of start/switch).
    result = _gateway(
        "render", "--backend", "victorialogs", "--server", "8090=vlogs:9428",
        "--deny-port", "8091",
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    config = result.stdout
    for path in PATHS.values():
        assert path in config, path
    assert "/loki/api/v1/query_range" not in config
    for write_path in ("/insert", "/internal", "/delete", "/select/logsql/tail"):
        assert f"location = {write_path}" not in config
        assert f"location {write_path}" not in config
    assert 'proxy_set_header Authorization ""' in config
    assert "proxy_pass_request_body off" in config
