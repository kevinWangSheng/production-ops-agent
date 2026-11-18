"""Independent contract tests for tool expansion B1 (#560): Elasticsearch 8 /
OpenSearch 2 log tools ``elasticsearch_search``, ``elasticsearch_mappings``
and ``elasticsearch_list_indices``.

Written from the B1 task record (docs/tasks/2026-11-16-tool-expansion-b1.md),
decision table r9 (B1-U1, B1-U2, B1-1 ... B1-15), the B/C batch test decisions
and C3 sections 8 and 12, without reading the implementation. Red on ``main``
until the implementation lands; collection must stay green.

Every test goes through the public path: build the ``otel-demo-ext`` profile
from environment variables, call a tool by name through the executor, and
assert on the returned view, the refusal code, or what a local
``http.server`` fake Elasticsearch received (B1-15). No container, no real
backend, no model.

Seams assumed beyond r9 (each also marked ``# ASSUMPTION:`` where used):

* ``select_profile(env)`` reads ``OPSPILOT_ES_URL`` from the mapping it is
  given (not from ``os.environ``) to decide the extension face (B1-U2); the
  worker reads the ES credentials from the same mapping when it builds the
  executor factory, like the A2 Kubernetes settings.
* The ES transport opens real sockets (no ``urllib.request.build_opener``
  monkeypatch); a loopback ``http://127.0.0.1:<port>`` passes the lab gate.
* ``OPSPILOT_ES_API_KEY`` holds the *encoded* key that Elasticsearch's create
  API key call returns and is sent verbatim as ``Authorization: ApiKey <v>``.
* The search view exposes the B1-8 projection either as ``content`` (a list
  of hits, with ``total``/``aggregations``/``timed_out``/``shards`` as view
  fields) or as a ``content`` object carrying those keys; the helpers accept
  both. ``notes`` and ``cannot_prove`` are view fields as in A3.
* Fixed detail codes (``QUERY_CONSTRUCT_NOT_ALLOWED``, ``INDEX_NOT_FOUND``)
  appear somewhere in the refusal view (notes or detail), as A3's
  ``POD_NOT_FOUND`` does.
* The hidden source-time aggregations (B1-7) are top-level ``min``/``max``
  (or ``stats``) aggregations on ``time_field`` in the ``_search`` body.
* ``columns`` is a comma-separated string; ``index_pattern`` rules for
  ``list_indices``'s ``pattern`` are the same as for ``index``.

All synthetic credentials and canaries are low-entropy placeholders.
"""

from __future__ import annotations

import base64
import fnmatch
import hashlib
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

from opspilot import tracing
from opspilot.tools import ToolRequest
from opspilot.tools.outcomes import Window
from opspilot.tools.profiles import PROFILE_ENV, ToolProfile, select_profile
from opspilot.tools.registry import canonical
from opspilot.tracing import check_lab_target
from tests.m1_tool_support import FakeClock, RecordingSink
from tests.test_m1_otel_demo_contract import NOW, TARGET_ID, FakeStore, _lease

EXT_PROFILE = "otel-demo-ext"
SEARCH = "elasticsearch_search"
MAPPINGS = "elasticsearch_mappings"
LIST = "elasticsearch_list_indices"
ES_TOOLS = {SEARCH, MAPPINGS, LIST}

ES_API_KEY = "es-ro-aaaaaaaa560"
ES_USERNAME = "es-reader"
ES_PASSWORD = "bbbbbbbb560"
PROM_USERNAME = "investigator"
PROM_PASSWORD = "cccccccc560"
K8S_TOKEN = "dddddddd560"
K8S_API = "https://127.0.0.1:16443"
CANARY = "eeeeeeee560"

# Pinned on main 09c1a68b (B1-U2 / R8-10): otel-demo, and otel-demo-ext with
# no OPSPILOT_ES_URL, must stay byte-identical when B1 lands.
BASE_TOOL_SCHEMA_REVISION = "otel-demo-a3a95a0f888e"
EXT_TOOL_SCHEMA_REVISION = "otel-demo-ext-c63f058afb18"
BASE_SURFACE_SHA256 = "672b8fea291ae6facf69f9b8f270ff05200c8093d1772e4b8b8a09b56031275e"
EXT_SURFACE_SHA256 = "a81a2c4075ebb722d29897dceb59da4697becf331adce1d35d178d322f88f63a"

ES_ENV_NAMES = (
    "OPSPILOT_ES_URL",
    "OPSPILOT_ES_API_KEY",
    "OPSPILOT_ES_USERNAME",
    "OPSPILOT_ES_PASSWORD",
    "OPSPILOT_ES_CA_FILE",
)
ES_SECRET_NAMES = tuple(name for name in ES_ENV_NAMES if name != "OPSPILOT_ES_URL")

# The call's authorized frame; the default query window is its last hour.
FRAME = Window(NOW - timedelta(hours=6), NOW)
DEFAULT_START = NOW - timedelta(hours=1)

LOG_PROPERTIES = {
    "@timestamp": {"type": "date"},
    "observed_at": {"type": "date"},
    "message": {"type": "text"},
    "service": {"type": "keyword"},
    "level": {"type": "keyword"},
}
INVENTORY_PROPERTIES = {
    "sku": {"type": "keyword"},
    "quantity": {"type": "long"},
    "name": {"type": "text"},
}


def _z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _instant(value: object) -> datetime:
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    assert isinstance(value, str), value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _hit(doc_id: str, when: datetime | None, **source: object) -> dict[str, Any]:
    body: dict[str, Any] = dict(source)
    if when is not None:
        body["@timestamp"] = _z(when)
    return {
        "_index": "logs-app-a" if when is not None else "inventory",
        "_id": doc_id,
        "_score": None,
        "_routing": "r1",
        "_ignored": ["message.keyword"],
        "_source": body,
        "sort": [int(when.timestamp() * 1000)] if when is not None else [],
    }


# -- local Elasticsearch fake (B1-15) -----------------------------------------


class FakeElasticsearch:
    """A loopback HTTP(S) server answering in Elasticsearch's JSON shapes and
    recording every request it receives."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.mappings: dict[str, dict[str, Any]] = {
            "logs-app-a": LOG_PROPERTIES,
            "logs-app-b": LOG_PROPERTIES,
            "inventory": INVENTORY_PROPERTIES,
        }
        self.hits: list[dict[str, Any]] = []
        self.total: int | None = None
        self.timed_out = False
        self.failed_shards = 0
        self.agg_results: dict[str, Any] = {}
        self.raw_search: bytes | None = None
        self.fail: tuple[int, dict[str, Any] | bytes] | None = None
        self.fail_on: str | None = None  # "_search", "_mapping", "_cat" or None=all
        self.search_delay = 0.0
        self.redirect = False
        self._server: ThreadingHTTPServer | None = None
        self.url = ""

    # request routing ------------------------------------------------------

    def _indices(self, target: str) -> list[str] | None:
        chosen: list[str] = []
        for segment in target.split(","):
            if segment.startswith("-"):
                chosen = [i for i in chosen if not fnmatch.fnmatch(i, segment[1:])]
            elif segment in ("_all", "*") or "*" in segment:
                pattern = "*" if segment == "_all" else segment
                chosen += [i for i in self.mappings if fnmatch.fnmatch(i, pattern)]
            elif segment in self.mappings:
                chosen.append(segment)
            else:
                return None
        return chosen

    def _not_found(self, target: str) -> tuple[int, bytes]:
        return 404, _json(
            {
                "error": {
                    "root_cause": [
                        {
                            "type": "index_not_found_exception",
                            "reason": f"no such index [{target}]",
                        }
                    ],
                    "type": "index_not_found_exception",
                    "reason": f"no such index [{target}]",
                    "index": target,
                },
                "status": 404,
            }
        )

    def answer(self, record: dict[str, Any]) -> tuple[int, dict[str, str], bytes]:
        path = record["path"]
        kind = (
            "_search"
            if path.endswith("/_search")
            else "_mapping"
            if path.endswith("/_mapping")
            else "_cat"
            if path.startswith("/_cat/")
            else "other"
        )
        if self.redirect:
            return 302, {"Location": "/elsewhere/_search"}, b""
        if self.fail is not None and self.fail_on in (None, kind):
            status, body = self.fail
            return status, {}, body if isinstance(body, bytes) else _json(body)
        if kind == "_cat":
            target = path[len("/_cat/indices") :].lstrip("/") or "*"
            names = self._indices(target)
            if names is None:
                return (*self._with_headers(self._not_found(target)),)
            columns = (record["query"].get("h") or [""])[0]
            rows = []
            for name in sorted(names):
                row = {
                    "health": "green",
                    "status": "open",
                    "index": name,
                    "uuid": "u-" + name,
                    "pri": "1",
                    "rep": "0",
                    "docs.count": "3",
                    "store.size": "10kb",
                }
                if columns:
                    row = {k: v for k, v in row.items() if k in columns.split(",")}
                rows.append(row)
            return 200, {}, _json(rows)
        if kind == "_mapping":
            target = path[1 : -len("/_mapping")]
            names = self._indices(target)
            if names is None:
                return (*self._with_headers(self._not_found(target)),)
            return (
                200,
                {},
                _json(
                    {n: {"mappings": {"properties": self.mappings[n]}} for n in names}
                ),
            )
        if kind == "_search":
            if self.search_delay:
                time.sleep(self.search_delay)
            target = path[1 : -len("/_search")]
            if self._indices(target) is None:
                return (*self._with_headers(self._not_found(target)),)
            if self.raw_search is not None:
                return 200, {}, self.raw_search
            body = json.loads(record["body"] or b"{}")
            size = body.get("size", 10)
            hits = self.hits[: size if isinstance(size, int) else 10]
            aggs: dict[str, Any] = {}
            for name, spec in (
                body.get("aggs") or body.get("aggregations") or {}
            ).items():
                aggs[name] = self._aggregate(spec, self.hits, name)
            return (
                200,
                {},
                _json(
                    {
                        "took": 3,
                        "timed_out": self.timed_out,
                        "_shards": {
                            "total": 2,
                            "successful": 2 - self.failed_shards,
                            "skipped": 0,
                            "failed": self.failed_shards,
                        },
                        "hits": {
                            "total": {
                                "value": len(self.hits)
                                if self.total is None
                                else self.total,
                                "relation": "eq",
                            },
                            "max_score": None,
                            "hits": hits,
                        },
                        **({"aggregations": aggs} if aggs else {}),
                    }
                ),
            )
        return 405, {}, _json({"error": "method or path not served by the fake"})

    @staticmethod
    def _with_headers(pair: tuple[int, bytes]) -> tuple[int, dict[str, str], bytes]:
        return pair[0], {}, pair[1]

    def _aggregate(self, spec: Mapping[str, Any], hits, name: str) -> Any:
        for kind in ("min", "max", "stats"):
            if kind in spec and isinstance(spec[kind], Mapping):
                field = spec[kind].get("field")
                stamps = [
                    _instant(h["_source"][field]) for h in hits if field in h["_source"]
                ]
                low = min(stamps) if stamps else None
                high = max(stamps) if stamps else None

                def ms(moment):
                    return None if moment is None else moment.timestamp() * 1000

                def text(moment):
                    return None if moment is None else _z(moment)

                if kind == "min":
                    return {"value": ms(low), "value_as_string": text(low)}
                if kind == "max":
                    return {"value": ms(high), "value_as_string": text(high)}
                return {
                    "count": len(stamps),
                    "min": ms(low),
                    "max": ms(high),
                    "avg": None,
                    "sum": 0,
                    "min_as_string": text(low),
                    "max_as_string": text(high),
                }
        return self.agg_results.get(name, {"buckets": []})

    # lifecycle ------------------------------------------------------------

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

    # what was received ------------------------------------------------------

    def searches(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["path"].endswith("/_search")]

    def search_body(self) -> dict[str, Any]:
        bodies = self.searches()
        assert len(bodies) == 1, [r["path"] for r in self.requests]
        return json.loads(bodies[0]["body"])


def _json(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


@pytest.fixture
def es() -> Iterator[FakeElasticsearch]:
    fake = FakeElasticsearch()
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture(autouse=True)
def _clean_environment(tmp_path, monkeypatch):
    # Tests pass their configuration as a mapping; nothing leaks in from the
    # developer's shell. A proxy that refuses connections proves the ES
    # transport goes direct (B1-3, like kubernetes_opener).
    for name in ES_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    ca_file = tmp_path / "synthetic-k8s-ca.pem"
    ca_file.write_text("synthetic ca placeholder\n")
    monkeypatch.setenv("B1_TEST_K8S_CA_FILE", str(ca_file))


def _env(es_url: str | None = None, **overrides: str) -> dict[str, str]:
    values = {
        PROFILE_ENV: EXT_PROFILE,
        "OPSPILOT_K8S_API_URL": K8S_API,
        "OPSPILOT_K8S_TOKEN": K8S_TOKEN,
        "OPSPILOT_K8S_CA_FILE": os.environ["B1_TEST_K8S_CA_FILE"],
        "OPSPILOT_K8S_NAMESPACE": "otel-demo",
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://127.0.0.1:19090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://127.0.0.1:16686/jaeger/ui",
        "OPSPILOT_OTEL_PROMETHEUS_USERNAME": PROM_USERNAME,
        "OPSPILOT_OTEL_PROMETHEUS_PASSWORD": PROM_PASSWORD,
    }
    if es_url is not None:
        values["OPSPILOT_ES_URL"] = es_url
        values["OPSPILOT_ES_API_KEY"] = ES_API_KEY
    values.update(overrides)
    return {k: v for k, v in values.items() if v != ""}


def _require_jq() -> None:
    if shutil.which("jq") is None or shutil.which("python3") is None:
        pytest.skip("the extension worker needs jq and python3 on PATH")


def _worker(env: Mapping[str, str], *, deadline_seconds: float = 600):
    """The executor a worker of ``env`` builds, for one fresh Run."""
    _require_jq()
    clock = FakeClock(start=NOW)
    profile = select_profile(env)
    assert isinstance(profile, ToolProfile)
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


def _executor(es: FakeElasticsearch, **env: str):
    executor, _ = _worker(_env(es.url, **env))
    return executor


def _call(tool: str, params: dict[str, object], *, index: int = 0) -> ToolRequest:
    return ToolRequest(
        step_id="b1-step",
        tool_index=index,
        tool_name=tool,
        target_ref=TARGET_ID,
        params=params,
        window=FRAME.as_json(),
    )


def _run(es: FakeElasticsearch, tool: str, params: dict[str, object], **env: str):
    return _executor(es, **env).execute(_call(tool, params))


def _view(outcome) -> Mapping[str, Any]:
    assert outcome.model_view is not None
    return outcome.model_view


def _text(outcome) -> str:
    return json.dumps(_view(outcome), ensure_ascii=False, sort_keys=True)


# ASSUMPTION: the B1-8 projection is either the content list of hits (with
# the other keys as view fields) or one content object carrying them.
def _result(view: Mapping[str, Any]) -> dict[str, Any]:
    content = view.get("content")
    if isinstance(content, Mapping):
        merged = dict(content)
    elif (
        isinstance(content, list)
        and len(content) == 1
        and isinstance(content[0], Mapping)
        and "hits" in content[0]
    ):
        merged = dict(content[0])
    else:
        merged = {"hits": content}
    for key in ("total", "aggregations", "timed_out", "shards", "took"):
        if key not in merged and key in view:
            merged[key] = view[key]
    return merged


def _hits(outcome) -> list[dict[str, Any]]:
    hits = _result(_view(outcome))["hits"]
    assert isinstance(hits, list), _view(outcome)
    return hits


def _time_ranges(query: object, field: str) -> list[Mapping[str, Any]]:
    """The tool's range clauses on ``field`` at the top level of the query:
    a ``bool`` whose ``filter``/``must`` the model's query cannot escape."""
    assert isinstance(query, Mapping) and set(query) == {"bool"}, query
    clauses: list[object] = []
    for key in ("filter", "must"):
        value = query["bool"].get(key)
        clauses += value if isinstance(value, list) else [value] if value else []
    return [
        clause["range"][field]
        for clause in clauses
        if isinstance(clause, Mapping)
        and isinstance(clause.get("range"), Mapping)
        and field in clause["range"]
    ]


def _clauses(query: Mapping[str, Any]) -> list[object]:
    out: list[object] = []
    for key in ("filter", "must"):
        value = query["bool"].get(key)
        out += value if isinstance(value, list) else [value] if value else []
    return out


def _bounds(clause: Mapping[str, Any]) -> tuple[datetime, datetime]:
    low = clause.get("gte", clause.get("gt"))
    high = clause.get("lte", clause.get("lt"))
    assert low is not None and high is not None, clause
    return _instant(low), _instant(high)


def _aggs(body: Mapping[str, Any]) -> Mapping[str, Any]:
    return body.get("aggs") or body.get("aggregations") or {}


def _schemas(env: Mapping[str, str]) -> dict[str, Mapping[str, Any]]:
    face = select_profile(env).face(FakeClock(start=NOW))
    return {s["function"]["name"]: s["function"] for s in face.tool_schemas}


def _surface_sha256(profile: ToolProfile) -> str:
    face = profile.face(FakeClock(start=NOW))
    blob = {
        "tool_schemas": [dict(item) for item in face.tool_schemas],
        "variant_id": face.variant_id,
        "evidence_context": dict(face.evidence_context("b1-run")),
        "versions": profile.versions(),
    }
    return hashlib.sha256(canonical(blob).encode()).hexdigest()


# -- ⑩ surface: otel-demo and unconfigured ext are byte-stable (B1-U2) --------


def test_surface_is_byte_stable_without_es_url_and_adds_three_tools_with_it():
    base = select_profile(
        {PROFILE_ENV: "otel-demo", "OPSPILOT_ES_URL": "http://127.0.0.1:9200"}
    )
    assert base.versions()["tool_schema_revision"] == BASE_TOOL_SCHEMA_REVISION
    assert _surface_sha256(base) == BASE_SURFACE_SHA256

    without = select_profile({PROFILE_ENV: EXT_PROFILE})
    assert without.versions()["tool_schema_revision"] == EXT_TOOL_SCHEMA_REVISION
    assert _surface_sha256(without) == EXT_SURFACE_SHA256
    # Credentials alone never change what the web records (B1-4, B1-U2).
    creds_only = select_profile(
        {PROFILE_ENV: EXT_PROFILE, "OPSPILOT_ES_API_KEY": ES_API_KEY}
    )
    assert _surface_sha256(creds_only) == EXT_SURFACE_SHA256

    web = select_profile(
        {PROFILE_ENV: EXT_PROFILE, "OPSPILOT_ES_URL": "http://127.0.0.1:9200"}
    )
    worker = select_profile(_env("http://127.0.0.1:9200"))
    old_names = [
        s["function"]["name"] for s in without.face(FakeClock(start=NOW)).tool_schemas
    ]
    new_names = [
        s["function"]["name"] for s in web.face(FakeClock(start=NOW)).tool_schemas
    ]
    assert set(new_names) == set(old_names) | ES_TOOLS
    assert [n for n in new_names if n not in ES_TOOLS] == old_names
    sources = web.face(FakeClock(start=NOW)).evidence_context("b1-run")["data_sources"]
    old_sources = without.face(FakeClock(start=NOW)).evidence_context("b1-run")[
        "data_sources"
    ]
    assert set(sources["available"]["elasticsearch"]) == ES_TOOLS
    assert {
        k: v for k, v in sources["available"].items() if k != "elasticsearch"
    } == old_sources["available"]
    assert web.versions()["tool_schema_revision"] != EXT_TOOL_SCHEMA_REVISION
    assert web.versions()["tool_schema_revision"].startswith("otel-demo-ext-")
    # The web (URL only) and the worker (URL and key) must agree (C3 §5).
    assert web.versions() == worker.versions()
    assert _surface_sha256(web) == _surface_sha256(worker)


def test_schemas_expose_exactly_the_decided_parameters():
    schemas = _schemas(
        {PROFILE_ENV: EXT_PROFILE, "OPSPILOT_ES_URL": "http://127.0.0.1:9200"}
    )
    search = schemas[SEARCH]["parameters"]
    assert set(search["properties"]) == {
        "index",
        "query",
        "aggregations",
        "sort",
        "source",
        "size",
        "from_offset",
        "profile",
        "start_time",
        "end_time",
        "time_field",
    }
    assert search["required"] == ["index"]
    for name in (
        "index",
        "query",
        "aggregations",
        "sort",
        "source",
        "start_time",
        "end_time",
        "time_field",
    ):
        assert search["properties"][name]["type"] == "string", name
    assert search["properties"]["size"]["type"] == "integer"
    assert search["properties"]["from_offset"]["type"] == "integer"
    assert search["properties"]["profile"]["type"] == "boolean"

    listing = schemas[LIST]["parameters"]
    assert set(listing["properties"]) == {
        "pattern",
        "sort",
        "columns",
        "health",
        "bytes",
        "expand_wildcards",
        "pri",
        "jq",
    }
    assert listing.get("required", []) == []
    mappings = schemas[MAPPINGS]["parameters"]
    assert set(mappings["properties"]) == {"index", "jq"}
    assert mappings["required"] == ["index"]


# -- ① only _search POSTs; paths and query strings are fixed -----------------


def test_every_request_uses_a_fixed_method_path_and_query_string(es):
    es.hits = [_hit("1", NOW - timedelta(minutes=5), message="ok", service="cart")]
    executor = _executor(es)
    outcomes = [
        executor.execute(
            _call(LIST, {"pattern": "logs-*", "columns": "index,docs.count"}, index=0)
        ),
        executor.execute(_call(MAPPINGS, {"index": "logs-app-a"}, index=1)),
        executor.execute(
            _call(
                SEARCH,
                {
                    "index": "logs-app-*,-logs-app-b",
                    "query": '{"match": {"message": "ok"}}',
                    "aggregations": '{"by_service": {"terms": {"field": "service"}}}',
                    "sort": '[{"@timestamp": "asc"}]',
                    "source": '["message", "service"]',
                    "size": 5,
                    "from_offset": 0,
                    "profile": False,
                },
                index=2,
            )
        ),
    ]
    assert [o.status for o in outcomes] == ["ok", "ok", "ok"], [
        _view(o) for o in outcomes
    ]
    # list_indices and mappings are current state (B1-7): no source time.
    for outcome in outcomes[:2]:
        view = _view(outcome)
        assert view["source_start_at"] is None and view["source_end_at"] is None
        assert view.get("cannot_prove")
    assert {"logs-app-a", "logs-app-b"} <= set(
        re.findall(r"logs-app-[ab]", _text(outcomes[0]))
    )
    assert "@timestamp" in _text(outcomes[1]) and "observed_at" in _text(outcomes[1])
    allowed_cat = {"format", "h", "s", "health", "bytes", "expand_wildcards", "pri"}
    posts = 0
    for record in es.requests:
        method, path = record["method"], record["path"]
        if method == "POST":
            posts += 1
            assert re.fullmatch(r"/[^/]+/_search", path), path
            assert record["raw_query"] == ""
            assert "json" in record["headers"].get("content-type", "")
        elif path.startswith("/_cat/"):
            assert method == "GET"
            assert re.fullmatch(r"/_cat/indices(/[^/]+)?", path), path
            assert record["query"]["format"] == ["json"]
            assert set(record["query"]) <= allowed_cat
            assert record["body"] == b""
        else:
            assert method == "GET", record
            assert re.fullmatch(r"/[^/]+/_mapping", path), path
            assert record["raw_query"] == ""
            assert record["body"] == b""
    assert posts == 1
    assert es.searches()[0]["path"] == "/logs-app-*,-logs-app-b/_search"
    body = es.search_body()
    assert set(body) <= {
        "query",
        "aggs",
        "aggregations",
        "sort",
        "_source",
        "size",
        "from",
        "profile",
        "timeout",
        "track_total_hits",
    }
    assert body["track_total_hits"] is True
    assert re.fullmatch(r"[0-9]+s", body["timeout"]), body["timeout"]
    assert 1 <= int(body["timeout"][:-1]) <= 30
    assert body["_source"] == ["message", "service"]
    assert body["size"] == 5 and body.get("from", 0) == 0


# -- ② index-name injection is refused before any request ---------------------

INJECTED_INDEX_NAMES = [
    "logs/../_bulk",
    "logs/_doc/1",
    "logs?pretty=true",
    "logs#frag",
    "remote:logs",
    "..",
    "logs..old",
    "logs app",
    "_security",
    "logs,_tasks",
    "LOGS",
    "logs%2F_bulk",
    "logs\n",
]


@pytest.mark.parametrize("name", INJECTED_INDEX_NAMES)
@pytest.mark.parametrize(
    ("tool", "param"), [(SEARCH, "index"), (MAPPINGS, "index"), (LIST, "pattern")]
)
def test_injected_index_names_never_reach_the_backend(es, tool, param, name):
    outcome = _run(es, tool, {param: name})
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS"), _view(
        outcome
    )
    assert es.requests == []


@pytest.mark.parametrize("name", ["_all", "logs-app-*,-logs-app-b", "logs-app-a"])
def test_valid_index_expressions_are_sent_url_encoded_in_the_fixed_path(es, name):
    outcome = _run(es, MAPPINGS, {"index": name})
    assert outcome.status == "ok", _view(outcome)
    assert [(r["method"], r["path"]) for r in es.requests] == [
        ("GET", f"/{name}/_mapping")
    ]
    assert re.fullmatch(r"/[A-Za-z0-9._%*,-]+/_mapping", es.requests[0]["raw_path"])


# -- ③ forbidden DSL constructs at any depth ----------------------------------

FORBIDDEN_DSL = [
    ("query", {"bool": {"filter": [{"script": {"script": {"source": "true"}}}]}}),
    (
        "query",
        {
            "function_score": {
                "query": {"match_all": {}},
                "functions": [{"script_score": {"script": "1"}}],
            }
        },
    ),
    ("query", {"wrapper": {"query": "eyJtYXRjaF9hbGwiOnt9fQ=="}}),
    (
        "query",
        {
            "bool": {
                "must": [{"match_all": {}}],
                "runtime_mappings": {"x": {"type": "long"}},
            }
        },
    ),
    ("query", {"bool": {"should": [{"bool": {"must": [{"pit": {"id": "x"}}]}}]}}),
    ("query", {"bool": {"must": [{"scroll": "1m"}]}}),
    ("query", {"bool": {"must": [{"stored_scripts": {"id": "x"}}]}}),
    (
        "aggregations",
        {
            "everything": {
                "global": {},
                "aggs": {"n": {"value_count": {"field": "_id"}}},
            }
        },
    ),
    (
        "aggregations",
        {
            "by_service": {
                "terms": {"field": "service"},
                "aggs": {"deep": {"global": {}}},
            }
        },
    ),
    (
        "aggregations",
        {
            "m": {
                "scripted_metric": {
                    "map_script": "state.x = 1",
                    "combine_script": "1",
                    "reduce_script": "1",
                }
            }
        },
    ),
    ("aggregations", {"t": {"top_hits": {"script_fields": {"x": {"script": "1"}}}}}),
    (
        "sort",
        [{"_script": {"type": "number", "script": {"source": "1"}, "order": "asc"}}],
    ),
    ("source", {"includes": ["message"], "runtime_mappings": {}}),
]
BAD_JSON = [
    ("query", "{not json"),
    ("query", json.dumps({"match": {"message": "x" * 8000}})),  # over 8000 chars
    ("aggregations", "[1, 2"),
    ("sort", json.dumps([{"service": "asc"}] * 700)),
]


@pytest.mark.parametrize(
    ("param", "raw", "code"),
    [(p, json.dumps(v), "QUERY_CONSTRUCT_NOT_ALLOWED") for p, v in FORBIDDEN_DSL]
    + [(p, raw, None) for p, raw in BAD_JSON],
)
def test_forbidden_or_malformed_dsl_is_refused_before_any_request(es, param, raw, code):
    outcome = _run(es, SEARCH, {"index": "logs-app-a", param: raw})
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS"), _view(
        outcome
    )
    if code is not None:
        # ASSUMPTION: the fixed code is visible in the refusal view (notes/detail).
        assert code in _text(outcome)
    assert es.requests == []


def test_forbidden_words_as_values_are_searchable(es):
    query = {"query_string": {"query": "script OR global OR wrapper"}}
    aggs = {"by_scope": {"terms": {"field": "global"}}}
    outcome = _run(
        es,
        SEARCH,
        {
            "index": "logs-app-a",
            "query": json.dumps(query),
            "aggregations": json.dumps(aggs),
        },
    )
    assert outcome.status in ("ok", "no_data"), _view(outcome)
    body = es.search_body()
    assert query in _clauses(body["query"])
    assert _aggs(body)["by_scope"] == aggs["by_scope"]


# -- ④ the incident-window filter (B1-U1, B1-7) -------------------------------


def test_time_filter_is_always_added_and_source_time_comes_from_hidden_aggregations(es):
    first, last = NOW - timedelta(minutes=40), NOW - timedelta(minutes=5)
    es.hits = [
        _hit("2", last, message="checkout failed", service="checkout", level="error"),
        _hit("1", first, message="checkout slow", service="checkout", level="warn"),
    ]
    es.agg_results = {"by_level": {"buckets": [{"key": "error", "doc_count": 1}]}}
    # The model's own range cannot widen the tool's filter.
    wide = {"range": {"@timestamp": {"gte": "1970-01-01T00:00:00Z"}}}
    model_query = {"bool": {"should": [wide], "minimum_should_match": 0}}
    outcome = _run(
        es,
        SEARCH,
        {
            "index": "logs-app-a",
            "query": json.dumps(model_query),
            "aggregations": '{"by_level": {"terms": {"field": "level"}}}',
        },
    )
    assert outcome.status == "ok", _view(outcome)
    body = es.search_body()
    ranges = _time_ranges(body["query"], "@timestamp")
    assert len(ranges) == 1
    assert _bounds(ranges[0]) == (DEFAULT_START, NOW)
    assert model_query in _clauses(body["query"])
    # No sort given: newest first on the time field, so truncation keeps the latest.
    sort = body["sort"]
    first_sort = sort[0] if isinstance(sort, list) else sort
    assert set(first_sort) == {"@timestamp"} and "desc" in json.dumps(
        first_sort["@timestamp"]
    )
    hidden = {name for name in _aggs(body) if name != "by_level"}
    assert hidden, "source time must be read from aggregations on the time field"
    view = _view(outcome)
    result = _result(view)
    assert set(result["aggregations"]) == {"by_level"}
    assert result["aggregations"]["by_level"]["buckets"][0]["key"] == "error"
    assert _instant(view["source_start_at"]) == first
    assert _instant(view["source_end_at"]) == last
    assert view["source"] == "otel-demo" and view["target_id"] == TARGET_ID


def test_explicit_window_and_alternative_time_field_are_honoured(es):
    start, end = NOW - timedelta(hours=4), NOW - timedelta(hours=2)
    outcome = _run(
        es,
        SEARCH,
        {
            "index": "logs-app-a",
            "start_time": _z(start),
            "end_time": _z(end),
            "time_field": "observed_at",
        },
    )
    assert outcome.status in ("ok", "no_data"), _view(outcome)
    body = es.search_body()
    ranges = _time_ranges(body["query"], "observed_at")
    assert len(ranges) == 1 and _bounds(ranges[0]) == (start, end)
    assert _time_ranges(body["query"], "@timestamp") == []


@pytest.mark.parametrize(
    "params",
    [
        {"time_field": "none"},
        {"time_field": "message"},  # mapped, but not a date
        {"time_field": "no_such_field"},
        {"time_filter": False},
        {"start_time": "yesterday", "end_time": _z(NOW)},
        {
            "start_time": _z(NOW - timedelta(hours=7)),
            "end_time": _z(NOW),
        },  # before the frame
        {"start_time": _z(NOW - timedelta(hours=1))},  # half a pair
    ],
)
def test_attempts_to_turn_off_or_escape_the_window_never_search(es, params):
    outcome = _run(es, SEARCH, {"index": "logs-app-a", **params})
    assert outcome.status in ("error", "denied"), _view(outcome)
    assert outcome.reason in (
        "INVALID_PARAMS",
        "PARAM_NOT_ALLOWED",
        "WINDOW_OUT_OF_SCOPE",
    )
    assert es.searches() == []


def test_index_without_a_time_field_is_unfiltered_and_says_it_cannot_prove_the_window(
    es,
):
    es.hits = [_hit("sku-1", None, sku="A-1", quantity=3, name="cart widget")]
    model_query = {"term": {"sku": "A-1"}}
    outcome = _run(es, SEARCH, {"index": "inventory", "query": json.dumps(model_query)})
    assert outcome.status == "ok", _view(outcome)
    body = es.search_body()
    assert "range" not in json.dumps(body.get("query"))
    assert not any(
        kind in spec
        for spec in _aggs(body).values()
        for kind in ("min", "max", "stats")
    )
    view = _view(outcome)
    assert view["source_start_at"] is None and view["source_end_at"] is None
    # ASSUMPTION: ``cannot_prove`` is a view field, as in A3.
    cannot_prove = str(view["cannot_prove"]).lower()
    assert "window" in cannot_prove
    assert _hits(outcome)[0]["_source"]["sku"] == "A-1"


# -- ⑤ size and from bounds -----------------------------------------------------


@pytest.mark.parametrize(
    ("params", "sent"),
    [
        ({}, (100, 0)),
        ({"size": 0}, (0, 0)),
        ({"size": 500}, (500, 0)),
        ({"from_offset": 9500, "size": 500}, (500, 9500)),
        ({"size": 501}, None),
        ({"size": -1}, None),
        ({"from_offset": -1}, None),
        ({"from_offset": 9501, "size": 500}, None),
        ({"from_offset": 9901}, None),  # default size 100
    ],
)
def test_size_and_from_bounds(es, params, sent):
    outcome = _run(es, SEARCH, {"index": "logs-app-a", **params})
    if sent is None:
        assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
        assert es.requests == []
        return
    assert outcome.status in ("ok", "no_data"), _view(outcome)
    body = es.search_body()
    assert (body["size"], body.get("from", 0)) == sent


# -- ⑥ size limits, truncation, redaction, incompleteness -----------------------


def test_raw_response_over_2_mib_is_refused_whole(es):
    es.raw_search = _json(
        {
            "took": 1,
            "timed_out": False,
            "_shards": {"total": 1, "successful": 1, "skipped": 0, "failed": 0},
            "hits": {
                "total": {"value": 1, "relation": "eq"},
                "hits": [
                    _hit(
                        "big",
                        NOW - timedelta(minutes=1),
                        message="y" * (2 * 1024 * 1024 + 1),
                    )
                ],
            },
        }
    )
    outcome = _run(es, SEARCH, {"index": "logs-app-a"})
    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
    assert _view(outcome)["content"] is None


def test_view_over_25k_tokens_is_refused_whole_with_a_narrowing_hint(es):
    words = " ".join(f"word{n} checkout latency spike" for n in range(60))
    es.hits = [
        _hit(str(n), NOW - timedelta(seconds=n + 1), message=words, service="checkout")
        for n in range(500)
    ]
    outcome = _run(es, SEARCH, {"index": "logs-app-a", "size": 500})
    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
    view = _view(outcome)
    assert view["content"] is None
    hint = _text(outcome).lower()
    assert "size" in hint or "source" in hint


def test_long_strings_are_cut_to_2000_chars_marked_and_secrets_redacted(es):
    es.hits = [
        _hit(
            "long",
            NOW - timedelta(minutes=2),
            message="x" * 5000,
            detail={"stack": "s" * 3000},
            audit=f"user login password={CANARY} accepted",
            service="checkout",
        )
    ]
    outcome = _run(es, SEARCH, {"index": "logs-app-a"})
    assert outcome.status == "ok", _view(outcome)
    text = _text(outcome)
    assert "x" * 1000 in text and "x" * 2001 not in text
    assert "s" * 1000 in text and "s" * 2001 not in text
    assert "trunc" in text.lower()
    assert CANARY not in text and "REDACTED" in text
    hit = _hits(outcome)[0]
    assert {"_index", "_id", "_source"} <= set(hit)
    assert not {"_score", "_routing", "_ignored"} & set(hit)


@pytest.mark.parametrize(
    ("setup", "word"),
    [
        ({"total": 50}, "total"),
        ({"timed_out": True}, "time"),
        ({"failed_shards": 1}, "shard"),
        ({}, None),  # a complete answer is not marked
    ],
)
def test_three_incompleteness_reasons_are_marked_and_explained(es, setup, word):
    es.hits = [
        _hit("a", NOW - timedelta(minutes=3), message="m", service="cart"),
        _hit("b", NOW - timedelta(minutes=2), message="m", service="cart"),
    ]
    for key, value in setup.items():
        setattr(es, key, value)
    outcome = _run(es, SEARCH, {"index": "logs-app-a", "size": 2})
    assert outcome.status == "ok", _view(outcome)
    view = _view(outcome)
    if word is None:
        assert view["incomplete"] is False
        return
    assert view["incomplete"] is True
    assert word in json.dumps(view.get("notes")).lower()


# -- ⑦ error classification (B1-12) -----------------------------------------------


def _es_error(status: int, kind: str, reason: str) -> dict[str, Any]:
    return {
        "error": {
            "root_cause": [{"type": kind, "reason": reason}],
            "type": kind,
            "reason": reason,
        },
        "status": status,
    }


@pytest.mark.parametrize(
    "kind", ["parsing_exception", "search_phase_execution_exception"]
)
def test_backend_400_is_invalid_params_echoing_redacted_type_and_reason(es, kind):
    es.fail = (400, _es_error(400, kind, f"unknown query [bogus] password={CANARY}"))
    es.fail_on = "_search"
    outcome = _run(es, SEARCH, {"index": "logs-app-a", "query": '{"bogus": {}}'})
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    text = _text(outcome)
    assert kind in text and "unknown query" in text
    assert CANARY not in text


@pytest.mark.parametrize(
    ("target", "expected", "code"),
    [
        ("missing-index", ("error", "INVALID_PARAMS"), "INDEX_NOT_FOUND"),  # 404
        ("nomatch-*", ("no_data", "NO_DATA"), None),  # wildcard: 200, nothing
    ],
)
@pytest.mark.parametrize(
    ("tool", "param"), [(SEARCH, "index"), (MAPPINGS, "index"), (LIST, "pattern")]
)
def test_missing_index_and_empty_wildcard_are_told_apart(
    es, tool, param, target, expected, code
):
    outcome = _run(es, tool, {param: target})
    assert (outcome.status, outcome.reason) == expected, _view(outcome)
    if code is not None:
        assert code in _text(outcome)


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (401, "SOURCE_FORBIDDEN"),
        (403, "SOURCE_FORBIDDEN"),
        (408, "SOURCE_UNAVAILABLE"),
        (429, "SOURCE_UNAVAILABLE"),
        (500, "SOURCE_UNAVAILABLE"),
        (503, "SOURCE_UNAVAILABLE"),
    ],
)
@pytest.mark.parametrize(
    ("tool", "params"),
    [
        (SEARCH, {"index": "logs-app-a"}),
        (MAPPINGS, {"index": "logs-app-a"}),
        (LIST, {}),
    ],
)
def test_backend_statuses_map_to_fixed_reasons(es, tool, params, status, reason):
    es.fail = (status, _es_error(status, "security_exception", "synthetic"))
    outcome = _run(es, tool, params)
    assert (outcome.status, outcome.reason) == ("error", reason), _view(outcome)


def test_a_search_past_the_call_deadline_is_a_timeout(es):
    es.search_delay = 4.0
    executor, _ = _worker(_env(es.url), deadline_seconds=2)
    started = time.monotonic()
    outcome = executor.execute(_call(SEARCH, {"index": "logs-app-a"}))
    assert outcome.status == "timeout", _view(outcome)
    assert time.monotonic() - started < 3.9
    assert int(es.search_body()["timeout"][:-1]) <= 2


def test_redirects_are_not_followed(es):
    es.redirect = True
    outcome = _run(es, LIST, {})
    assert outcome.status == "error"
    assert len(es.requests) == 1


# -- ⑧ the es-ro credential slot -----------------------------------------------


def _authorization(record: Mapping[str, Any]) -> str:
    return record["headers"].get("authorization", "")


def test_api_key_and_basic_auth_go_only_to_elasticsearch(es):
    prometheus = FakeElasticsearch()
    prometheus.start()
    try:
        executor, _ = _worker(_env(es.url, OPSPILOT_OTEL_PROMETHEUS_URL=prometheus.url))
        executor.execute(_call(LIST, {}, index=0))
        executor.execute(
            _call("get_metric_names", {"match": '{service_name="cart"}'}, index=1)
        )
    finally:
        prometheus.stop()
    # ASSUMPTION: the encoded key is sent verbatim after "ApiKey ".
    assert {_authorization(r) for r in es.requests} == {f"ApiKey {ES_API_KEY}"}
    assert prometheus.requests, "the Prometheus tool should have reached its own fake"
    for record in prometheus.requests:
        assert ES_API_KEY not in json.dumps(record["headers"])
    for record in es.requests:
        flat = json.dumps(record["headers"])
        assert K8S_TOKEN not in flat and PROM_PASSWORD not in flat

    es.requests.clear()
    basic = _env(
        es.url,
        OPSPILOT_ES_API_KEY="",
        OPSPILOT_ES_USERNAME=ES_USERNAME,
        OPSPILOT_ES_PASSWORD=ES_PASSWORD,
    )
    executor, _ = _worker(basic)
    executor.execute(_call(LIST, {}))
    expected = (
        "Basic " + base64.b64encode(f"{ES_USERNAME}:{ES_PASSWORD}".encode()).decode()
    )
    assert {_authorization(r) for r in es.requests} == {expected}


def test_credentials_never_appear_in_views_notes_or_trace(es):
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from tests.test_tracing import lab_env

    es.hits = [_hit("1", NOW - timedelta(minutes=5), message="ok", service="cart")]
    exporter = InMemorySpanExporter()
    tracer = tracing.configure(
        lab_env(
            OPSPILOT_TOOL_PROFILE=EXT_PROFILE,
            OPSPILOT_K8S_API_URL=K8S_API,
            OPSPILOT_ES_URL=es.url,
            OPSPILOT_OTEL_PROMETHEUS_URL="http://127.0.0.1:19090",
            OPSPILOT_OTEL_JAEGER_URL="http://127.0.0.1:16686",
        ),
        exporter=exporter,
    )
    try:
        views = []
        for env in (
            _env(es.url),
            _env(
                es.url,
                OPSPILOT_ES_API_KEY="",
                OPSPILOT_ES_USERNAME=ES_USERNAME,
                OPSPILOT_ES_PASSWORD=ES_PASSWORD,
            ),
        ):
            executor, sink = _worker(env)
            with tracer.run(incident_id="b1-incident", run_id="b1-run", versions={}):
                views.append(
                    executor.execute(_call(SEARCH, {"index": "logs-app-a"}, index=0))
                )
                es.fail = (
                    401,
                    _es_error(401, "security_exception", "missing authentication"),
                )
                views.append(executor.execute(_call(LIST, {}, index=1)))
                es.fail = None
            views.extend(sink.records)
        tracer.shutdown()
        spans = exporter.get_finished_spans()
    finally:
        tracing.reset()
    assert any("elasticsearch_search" in span.name for span in spans)
    exported = json.dumps([dict(span.attributes or {}) for span in spans], default=str)
    rendered = json.dumps(
        [getattr(v, "model_view", None) or getattr(v, "_view", None) for v in views],
        default=str,
    )
    for secret in (
        ES_API_KEY,
        ES_PASSWORD,
        base64.b64encode(f"{ES_USERNAME}:{ES_PASSWORD}".encode()).decode(),
    ):
        assert secret not in exported
        assert secret not in rendered


# -- ⑨ startup codes, and the web does not read ES credentials ------------------


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"OPSPILOT_ES_API_KEY": ""}, "ES_CONFIG_MISSING"),
        # Another backend's credential is never borrowed (B1-4).
        (
            {"OPSPILOT_ES_API_KEY": "", "OPSPILOT_OTEL_TOKEN": "ffffffff560"},
            "ES_CONFIG_MISSING",
        ),
        (
            {"OPSPILOT_ES_USERNAME": ES_USERNAME, "OPSPILOT_ES_PASSWORD": ES_PASSWORD},
            "ES_CONFIG_INVALID",
        ),
        ({"OPSPILOT_ES_URL": ""}, "ES_CONFIG_INVALID"),  # key without URL
        (
            {
                "OPSPILOT_ES_URL": "",
                "OPSPILOT_ES_API_KEY": "",
                "OPSPILOT_ES_PASSWORD": ES_PASSWORD,
            },
            "ES_CONFIG_INVALID",
        ),
        # Half a basic pair: r9 names both codes for "neither/both"; either is accepted.
        (
            {"OPSPILOT_ES_API_KEY": "", "OPSPILOT_ES_USERNAME": ES_USERNAME},
            "ES_CONFIG_(MISSING|INVALID)",
        ),
    ],
)
def test_worker_refuses_to_start_with_missing_or_conflicting_es_configuration(
    overrides, code
):
    _require_jq()
    env = _env("http://127.0.0.1:9200", **overrides)
    with pytest.raises(SystemExit, match=code):
        _worker(env)


def test_unconfigured_extension_worker_still_starts_without_es_tools(es):
    executor, _ = _worker(_env(None))
    outcome = executor.execute(_call(SEARCH, {"index": "logs-app-a"}))
    assert outcome.status in ("error", "denied")
    assert es.requests == []


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


def test_web_side_profile_reads_only_the_es_url():
    env = _TrackingEnv(
        _env(
            "http://127.0.0.1:9200",
            OPSPILOT_ES_USERNAME=ES_USERNAME,
            OPSPILOT_ES_PASSWORD=ES_PASSWORD,
            OPSPILOT_ES_CA_FILE="/nonexistent/ca.pem",
        )
    )
    profile = select_profile(env)
    face = profile.face(FakeClock(start=NOW))
    face.evidence_context("b1-run")
    profile.versions()
    assert {s["function"]["name"] for s in face.tool_schemas} >= ES_TOOLS
    assert not env.read & set(ES_SECRET_NAMES)
    assert not env.iterated


# -- ⑪ lab gate ----------------------------------------------------------------


def test_lab_gate_requires_a_lab_elasticsearch_host():
    env = {
        "OPSPILOT_TRACE": "lab",
        PROFILE_ENV: EXT_PROFILE,
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://127.0.0.1:19090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://127.0.0.1:16686",
        "OPSPILOT_K8S_API_URL": K8S_API,
        "LANGSMITH_PROJECT": "opspilot-lab-b1",
        "LANGSMITH_API_KEY": "lsv2-aaaaaaaa560",
        "LANGSMITH_ENDPOINT": "https://api.smith.langchain.com",
    }
    assert check_lab_target(env) == ()
    assert check_lab_target({**env, "OPSPILOT_ES_URL": "http://127.0.0.1:9200"}) == ()
    rejected = check_lab_target(
        {**env, "OPSPILOT_ES_URL": "https://logs.example.invalid:9200"}
    )
    assert "ELASTICSEARCH_NOT_LAB" in rejected
    assert "ELASTICSEARCH_NOT_LAB" in check_lab_target(
        {**env, "OPSPILOT_ES_URL": "http://reader@127.0.0.1:9200"}
    )


# -- HTTPS with OPSPILOT_ES_CA_FILE (B1-4, B1-18) ---------------------------------


def test_https_backend_is_verified_against_the_configured_ca(tmp_path):
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is needed to mint the synthetic CA")
    cert, key = tmp_path / "es-cert.pem", tmp_path / "es-key.pem"
    subprocess.run(
        [
            openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cert), "-days", "2",
            "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    fake = FakeElasticsearch()
    fake.start(tls=(cert, key))
    try:
        trusted, _ = _worker(_env(fake.url, OPSPILOT_ES_CA_FILE=str(cert)))
        outcome = trusted.execute(_call(LIST, {}))
        assert outcome.status == "ok", _view(outcome)
        assert {_authorization(r) for r in fake.requests} == {f"ApiKey {ES_API_KEY}"}
        fake.requests.clear()
        untrusted, _ = _worker(_env(fake.url))
        outcome = untrusted.execute(_call(LIST, {}))
        assert outcome.status == "error"
        assert fake.requests == []  # the handshake failed; nothing was read
    finally:
        fake.stop()


# -- deploy entrypoint passes OPSPILOT_ES_* to the worker -----------------------


def test_entrypoint_imports_es_settings_from_the_worker_env_file(tmp_path):
    entrypoint = Path(__file__).resolve().parents[1] / "deploy" / "docker-entrypoint.sh"
    worker_env = tmp_path / "worker_env"
    worker_env.write_text(
        "OPSPILOT_ES_URL=http://127.0.0.1:9200\n"
        f"OPSPILOT_ES_API_KEY={ES_API_KEY}\n"
        f"OPSPILOT_ES_USERNAME={ES_USERNAME}\n"
        f"OPSPILOT_ES_PASSWORD='{ES_PASSWORD}'\n"
        "OPSPILOT_ES_CA_FILE=/run/secrets/es-ca.pem\n"
        "OPSPILOT_ESCAPE=not-an-es-setting\n"
        "OPSPILOT_ES_$(touch pwned)=x\n"
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
    assert env["OPSPILOT_ES_URL"] == "http://127.0.0.1:9200"
    assert env["OPSPILOT_ES_API_KEY"] == ES_API_KEY
    assert env["OPSPILOT_ES_USERNAME"] == ES_USERNAME
    assert env["OPSPILOT_ES_PASSWORD"] == ES_PASSWORD
    assert env["OPSPILOT_ES_CA_FILE"] == "/run/secrets/es-ca.pem"
    assert "OPSPILOT_ESCAPE" not in env
    assert not (tmp_path / "pwned").exists()
