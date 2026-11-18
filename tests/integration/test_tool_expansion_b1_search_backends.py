"""Real-backend integration tests for tool expansion B1 (#560), R8-25 and
r9 B1-16 ... B1-21: the three Elasticsearch tools against a real
Elasticsearch 8 (security on, HTTP) and a real OpenSearch 2 (security plugin
off), through the public tool path. No model is called.

Runs only in the CI job ``search-backends`` (``OPSPILOT_SEARCH_BACKENDS=1``),
whose service containers listen on ``B1_ES_URL`` / ``B1_OPENSEARCH_URL``;
skipped everywhere else (the local machine has no memory for them, B1-16).

The fixtures, not the product, hold the engineer credential: the ``elastic``
account seeds synthetic data with ``_bulk?refresh=wait_for`` (B1-20) and
creates the read-only API key of B1-5; the tools only ever see that key.
Authentication and permission assertions run against Elasticsearch only;
OpenSearch is checked for the three endpoints returning and projecting the
same way (B1-19). The ``ASSUMPTION`` notes of the contract test file apply
here unchanged (view shape, ``cannot_prove``/``notes``, fixed codes, jq over
the raw response body, comma-separated ``columns``).
"""

from __future__ import annotations

import base64
import itertools
import json
import os
import shutil
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from opspilot.tools import ToolRequest
from opspilot.tools.outcomes import Window
from opspilot.tools.profiles import PROFILE_ENV, ToolProfile, select_profile
from tests.m1_tool_support import FakeClock, RecordingSink
from tests.test_m1_otel_demo_contract import TARGET_ID, FakeStore, _lease
from tests.test_tool_expansion_b1_contract import (
    LIST,
    MAPPINGS,
    SEARCH,
    _hits,
    _instant,
    _result,
    _text,
    _view,
    _z,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("OPSPILOT_SEARCH_BACKENDS") != "1",
    reason="real Elasticsearch/OpenSearch run only in the CI search-backends job",
)

ES_URL = os.environ.get("B1_ES_URL", "http://127.0.0.1:59200")
OPENSEARCH_URL = os.environ.get("B1_OPENSEARCH_URL", "http://127.0.0.1:59201")
# Low-entropy CI-only test password of the service container (B1-18).
ELASTIC_PASSWORD = os.environ.get("B1_ELASTIC_PASSWORD", "changeme560")
CANARY = "aaaaaaaa560"

NOW = datetime.now(timezone.utc).replace(microsecond=0)
FRAME = Window(NOW - timedelta(hours=6), NOW)
DEFAULT_START = NOW - timedelta(hours=1)

LOG_MAPPING = {
    "properties": {
        "@timestamp": {"type": "date"},
        "message": {"type": "text"},
        "service": {"type": "keyword"},
        "level": {"type": "keyword"},
    }
}
LOGS_A = {
    "a1": (NOW - timedelta(minutes=50), "checkout", "error", "payment declined"),
    "a2": (NOW - timedelta(minutes=30), "checkout", "warn", "payment slow"),
    "a3": (NOW - timedelta(minutes=10), "cart", "info", "cart updated"),
    "a-long": (NOW - timedelta(minutes=20), "cart", "info", "L" * 5000),
    "a-secret": (
        NOW - timedelta(minutes=15),
        "auth",
        "info",
        f"user login password={CANARY} accepted",
    ),
    "a-old": (NOW - timedelta(hours=3), "checkout", "error", "old outage"),
    "a-ancient": (NOW - timedelta(days=2), "checkout", "error", "ancient outage"),
    "a-future": (NOW + timedelta(hours=1), "checkout", "error", "future event"),
}
IN_DEFAULT_WINDOW = {"a1", "a2", "a3", "a-long", "a-secret"}
LOGS_B = {
    "b1": (NOW - timedelta(minutes=25), "payments", "error", "card network down"),
    "b-future": (NOW + timedelta(hours=2), "payments", "info", "future event"),
}
INVENTORY = {
    "i1": {"sku": "A-1", "quantity": 3, "name": "cart widget"},
    "i2": {"sku": "B-2", "quantity": 0, "name": "checkout gadget"},
    "i3": {"sku": "C-3", "quantity": 9, "name": "payment gizmo"},
}
BULK_DOCS = 600
WIDE_FIELDS = 5000


# -- raw HTTP for fixtures (engineer side, never the product) ----------------

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http(
    method: str,
    url: str,
    body: object = None,
    *,
    auth: str | None = None,
    ndjson: bool = False,
) -> tuple[int, Any]:
    data = None
    headers = {}
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        headers["Content-Type"] = (
            "application/x-ndjson" if ndjson else "application/json"
        )
    if auth is not None:
        headers["Authorization"] = auth
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with _OPENER.open(request, timeout=60) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        raw = error.read()
        status = error.code
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, raw.decode(errors="replace")


def _basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


@dataclass(frozen=True)
class Backend:
    name: str
    url: str
    admin: str | None  # Authorization header of the engineer account

    def call(self, method: str, path: str, body: object = None, **kw):
        return _http(method, self.url + path, body, auth=self.admin, **kw)


ELASTIC = Backend("elasticsearch", ES_URL, _basic("elastic", ELASTIC_PASSWORD))
OPENSEARCH = Backend("opensearch", OPENSEARCH_URL, None)


def _wait_healthy(backend: Backend) -> None:
    deadline = time.monotonic() + 180
    while True:
        try:
            status, body = backend.call(
                "GET", "/_cluster/health?wait_for_status=yellow&timeout=5s"
            )
            if status == 200 and body.get("status") in ("yellow", "green"):
                return
        except OSError:
            pass
        if time.monotonic() > deadline:
            raise AssertionError(
                f"{backend.name} at {backend.url} never became healthy"
            )
        time.sleep(2)


def _bulk(backend: Backend, index: str, docs: Mapping[str, Mapping[str, Any]]) -> None:
    lines = []
    for doc_id, source in docs.items():
        lines.append(json.dumps({"index": {"_index": index, "_id": doc_id}}))
        lines.append(json.dumps(source))
    status, body = backend.call(
        "POST",
        "/_bulk?refresh=wait_for",
        ("\n".join(lines) + "\n").encode(),
        ndjson=True,
    )
    assert status == 200 and body["errors"] is False, body


def _log_docs(rows) -> dict[str, dict[str, Any]]:
    return {
        doc_id: {
            "@timestamp": _z(when),
            "service": service,
            "level": level,
            "message": message,
        }
        for doc_id, (when, service, level, message) in rows.items()
    }


def _seed(backend: Backend) -> None:
    _wait_healthy(backend)
    for index in ("logs-app-a", "logs-app-b", "inventory", "logs-bulk", "wide-mapping"):
        backend.call("DELETE", f"/{index}")
    settings = {"number_of_shards": 1, "number_of_replicas": 0}
    for index in ("logs-app-a", "logs-app-b", "logs-bulk"):
        status, body = backend.call(
            "PUT", f"/{index}", {"settings": settings, "mappings": LOG_MAPPING}
        )
        assert status == 200, body
    status, body = backend.call(
        "PUT",
        "/inventory",
        {
            "settings": settings,
            "mappings": {
                "properties": {
                    "sku": {"type": "keyword"},
                    "quantity": {"type": "long"},
                    "name": {"type": "text"},
                }
            },
        },
    )
    assert status == 200, body
    status, body = backend.call(
        "PUT",
        "/wide-mapping",
        {
            "settings": {
                **settings,
                "index.mapping.total_fields.limit": WIDE_FIELDS + 1000,
            },
            "mappings": {
                "properties": {
                    f"field_{n:05d}": {"type": "keyword"} for n in range(WIDE_FIELDS)
                }
            },
        },
    )
    assert status == 200, body
    _bulk(backend, "logs-app-a", _log_docs(LOGS_A))
    _bulk(backend, "logs-app-b", _log_docs(LOGS_B))
    _bulk(backend, "inventory", INVENTORY)
    _bulk(
        backend,
        "logs-bulk",
        {
            f"bulk-{n:03d}": {
                "@timestamp": _z(
                    NOW - timedelta(minutes=59) + timedelta(seconds=5 * n)
                ),
                "service": "bulk",
                "level": "info",
                "message": f"bulk line {n} " + "m" * 4500,
            }
            for n in range(BULK_DOCS)
        },
    )


@pytest.fixture(scope="module")
def elastic() -> Iterator[Backend]:
    _seed(ELASTIC)
    yield ELASTIC


@pytest.fixture(scope="module")
def opensearch() -> Iterator[Backend]:
    _seed(OPENSEARCH)
    yield OPENSEARCH


def _api_key(backend: Backend, name: str, indices: list[str]) -> str:
    """B1-5's read-only key, created by the engineer account."""
    status, body = backend.call(
        "POST",
        "/_security/api_key",
        {
            "name": name,
            "role_descriptors": {
                "b1_read_only": {
                    "cluster": ["monitor"],
                    "indices": [
                        {
                            "names": indices,
                            "privileges": ["read", "view_index_metadata", "monitor"],
                        }
                    ],
                }
            },
        },
    )
    assert status == 200, body
    return body["encoded"]


@pytest.fixture(scope="module")
def read_only_key(elastic) -> str:
    return _api_key(elastic, "b1-read-only", ["*"])


# -- the product path ----------------------------------------------------------


@pytest.fixture(scope="module")
def k8s_ca(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("b1-k8s") / "synthetic-ca.pem"
    path.write_text("synthetic ca placeholder\n")
    return str(path)


def _env(url: str, k8s_ca: str, **es: str) -> dict[str, str]:
    return {
        PROFILE_ENV: "otel-demo-ext",
        "OPSPILOT_K8S_API_URL": "https://127.0.0.1:16443",
        "OPSPILOT_K8S_TOKEN": "dddddddd560",
        "OPSPILOT_K8S_CA_FILE": k8s_ca,
        "OPSPILOT_K8S_NAMESPACE": "otel-demo",
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://127.0.0.1:19090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://127.0.0.1:16686/jaeger/ui",
        "OPSPILOT_ES_URL": url,
        **es,
    }


def _executor(env: Mapping[str, str]):
    if shutil.which("jq") is None or shutil.which("python3") is None:
        pytest.skip("the extension worker needs jq and python3 on PATH")
    clock = FakeClock(start=NOW)
    profile = select_profile(env)
    assert isinstance(profile, ToolProfile)
    lease = _lease()
    deadline = NOW + timedelta(minutes=10)
    factory = profile.executor_factory(
        FakeStore(lease, deadline=deadline), RecordingSink(), clock
    )
    return factory(
        lease,
        profile.face(clock).input_for(
            run_id=str(lease.run_id),
            question="Why is checkout degraded?",
            target_id=TARGET_ID,
            deadline=deadline,
            model_requests=12,
        ),
    )


@pytest.fixture
def es_tools(elastic, read_only_key, k8s_ca):
    return _executor(_env(elastic.url, k8s_ca, OPSPILOT_ES_API_KEY=read_only_key))


@pytest.fixture
def os_tools(opensearch, k8s_ca):
    # Security plugin off: the basic pair is configured (the worker requires
    # one credential) and ignored by the backend (B1-19).
    return _executor(
        _env(
            opensearch.url,
            k8s_ca,
            OPSPILOT_ES_USERNAME="reader",
            OPSPILOT_ES_PASSWORD="changeme560",
        )
    )


@pytest.fixture(params=["elasticsearch", "opensearch"])
def tools(request):
    return request.getfixturevalue(
        "es_tools" if request.param == "elasticsearch" else "os_tools"
    )


_INDEX = itertools.count()


def _call(tool: str, params: dict[str, object]) -> ToolRequest:
    return ToolRequest(
        step_id="b1-live",
        tool_index=next(_INDEX),
        tool_name=tool,
        target_ref=TARGET_ID,
        params=params,
        window=FRAME.as_json(),
    )


def _ids(outcome) -> set[str]:
    return {hit["_id"] for hit in _hits(outcome)}


# -- both backends: the three endpoints return and project the same way -------


def test_three_tools_return_real_data_and_the_default_window(tools):
    listing = tools.execute(
        _call(LIST, {"pattern": "logs-app-*", "columns": "index,docs.count"})
    )
    assert listing.status == "ok", _view(listing)
    assert "logs-app-a" in _text(listing) and "logs-app-b" in _text(listing)
    assert _view(listing)["source_start_at"] is None

    mappings = tools.execute(_call(MAPPINGS, {"index": "logs-app-a"}))
    assert mappings.status == "ok", _view(mappings)
    assert "@timestamp" in _text(mappings) and "keyword" in _text(mappings)

    search = tools.execute(_call(SEARCH, {"index": "logs-app-a"}))
    assert search.status == "ok", _view(search)
    assert _ids(search) == IN_DEFAULT_WINDOW
    view = _view(search)
    assert _instant(view["source_start_at"]) == LOGS_A["a1"][0]
    assert _instant(view["source_end_at"]) == LOGS_A["a3"][0]
    times = [_instant(hit["_source"]["@timestamp"]) for hit in _hits(search)]
    assert times == sorted(times, reverse=True)
    assert _result(view)["total"]["value"] == len(IN_DEFAULT_WINDOW)


def test_explicit_window_and_wildcard_across_indices(tools):
    outcome = tools.execute(
        _call(
            SEARCH,
            {
                "index": "logs-app-*",
                "start_time": _z(NOW - timedelta(hours=4)),
                "end_time": _z(NOW),
            },
        )
    )
    assert outcome.status == "ok", _view(outcome)
    assert _ids(outcome) == IN_DEFAULT_WINDOW | {"a-old", "b1"}
    assert _instant(_view(outcome)["source_start_at"]) == LOGS_A["a-old"][0]


def test_the_time_filter_cannot_be_bypassed(tools):
    wide = {"range": {"@timestamp": {"gte": "1970-01-01T00:00:00Z"}}}
    widened = tools.execute(
        _call(
            SEARCH,
            {
                "index": "logs-app-a",
                "query": json.dumps({"bool": {"should": [wide, {"match_all": {}}]}}),
            },
        )
    )
    assert widened.status == "ok"
    assert _ids(widened) == IN_DEFAULT_WINDOW
    future_only = {"range": {"@timestamp": {"gte": _z(NOW + timedelta(minutes=30))}}}
    future = tools.execute(
        _call(SEARCH, {"index": "logs-app-a", "query": json.dumps(future_only)})
    )
    assert (future.status, future.reason) == ("no_data", "NO_DATA")
    everything = tools.execute(
        _call(
            SEARCH,
            {
                "index": "logs-app-a",
                "aggregations": json.dumps({"all": {"global": {}}}),
            },
        )
    )
    assert (everything.status, everything.reason) == ("error", "INVALID_PARAMS")


def test_index_without_time_field_is_unfiltered_with_unknown_source_time(tools):
    outcome = tools.execute(_call(SEARCH, {"index": "inventory"}))
    assert outcome.status == "ok", _view(outcome)
    assert _ids(outcome) == set(INVENTORY)
    view = _view(outcome)
    assert view["source_start_at"] is None and view["source_end_at"] is None
    assert "window" in str(view["cannot_prove"]).lower()


# -- Elasticsearch only: limits, projection, errors, read-only layers ----------


def test_size_truncation_marks_total_incomplete_and_keeps_the_newest(es_tools):
    outcome = es_tools.execute(
        _call(SEARCH, {"index": "logs-bulk", "source": json.dumps(["service"])})
    )
    assert outcome.status == "ok", _view(outcome)
    hits = _hits(outcome)
    assert len(hits) == 100
    view = _view(outcome)
    assert _result(view)["total"]["value"] == BULK_DOCS
    assert view["incomplete"] is True
    assert "total" in json.dumps(view.get("notes")).lower()
    newest = NOW - timedelta(minutes=59) + timedelta(seconds=5 * (BULK_DOCS - 1))
    assert _instant(view["source_end_at"]) == newest
    sorts = [hit["sort"][0] for hit in hits]
    assert sorts == sorted(sorts, reverse=True)
    assert _instant(sorts[0]) == newest


def test_oversized_results_are_refused_whole_and_jq_narrows_a_wide_mapping(es_tools):
    big = es_tools.execute(_call(SEARCH, {"index": "logs-bulk", "size": 500}))
    assert (big.status, big.reason) == ("error", "RESULT_TOO_LARGE")
    assert _view(big)["content"] is None

    wide = es_tools.execute(_call(MAPPINGS, {"index": "wide-mapping"}))
    assert (wide.status, wide.reason) == ("error", "RESULT_TOO_LARGE")
    narrowed = es_tools.execute(
        _call(
            MAPPINGS,
            {"index": "wide-mapping", "jq": ".[].mappings.properties | length"},
        )
    )
    assert narrowed.status == "ok", _view(narrowed)
    assert str(WIDE_FIELDS) in json.dumps(_view(narrowed)["content"])


def test_long_lines_are_cut_and_credentials_in_logs_are_redacted(es_tools):
    outcome = es_tools.execute(_call(SEARCH, {"index": "logs-app-a"}))
    assert outcome.status == "ok"
    text = _text(outcome)
    assert "L" * 1000 in text and "L" * 2001 not in text
    assert "trunc" in text.lower()
    assert CANARY not in text and "REDACTED" in text


def test_aggregations_are_real_and_hidden_time_aggregations_are_removed(es_tools):
    outcome = es_tools.execute(
        _call(
            SEARCH,
            {
                "index": "logs-app-a",
                "size": 0,
                "aggregations": json.dumps(
                    {"by_service": {"terms": {"field": "service"}}}
                ),
            },
        )
    )
    assert outcome.status in ("ok", "no_data"), _view(outcome)
    aggregations = _result(_view(outcome))["aggregations"]
    assert set(aggregations) == {"by_service"}
    counts = {b["key"]: b["doc_count"] for b in aggregations["by_service"]["buckets"]}
    assert counts == {"checkout": 2, "cart": 2, "auth": 1}


def test_error_classes_come_from_real_responses(
    elastic, read_only_key, k8s_ca, es_tools
):
    key_header = f"ApiKey {read_only_key}"
    # The scenarios really produce these statuses (guards the setup itself).
    assert (
        _http("GET", f"{elastic.url}/missing-index/_mapping", auth=key_header)[0] == 404
    )
    assert (
        _http(
            "POST",
            f"{elastic.url}/logs-app-a/_search",
            {"query": {"no_such_query": {}}},
            auth=key_header,
        )[0]
        == 400
    )
    missing = es_tools.execute(_call(SEARCH, {"index": "missing-index"}))
    assert (missing.status, missing.reason) == ("error", "INVALID_PARAMS")
    assert "INDEX_NOT_FOUND" in _text(missing)
    bad_dsl = es_tools.execute(
        _call(
            SEARCH, {"index": "logs-app-a", "query": json.dumps({"no_such_query": {}})}
        )
    )
    assert (bad_dsl.status, bad_dsl.reason) == ("error", "INVALID_PARAMS")
    assert "parsing_exception" in _text(bad_dsl)
    nothing = es_tools.execute(_call(SEARCH, {"index": "nomatch-*"}))
    assert (nothing.status, nothing.reason) == ("no_data", "NO_DATA")

    wrong_key = base64.b64encode(b"bogus-id:bogus-key").decode()
    assert (
        _http(
            "GET", f"{elastic.url}/_cat/indices?format=json", auth=f"ApiKey {wrong_key}"
        )[0]
        == 401
    )
    unauthenticated = _executor(
        _env(elastic.url, k8s_ca, OPSPILOT_ES_API_KEY=wrong_key)
    )
    outcome = unauthenticated.execute(_call(LIST, {}))
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_FORBIDDEN")

    narrow_key = _api_key(elastic, "b1-only-app-a", ["logs-app-a"])
    assert (
        _http("GET", f"{elastic.url}/inventory/_mapping", auth=f"ApiKey {narrow_key}")[
            0
        ]
        == 403
    )
    narrow = _executor(_env(elastic.url, k8s_ca, OPSPILOT_ES_API_KEY=narrow_key))
    outcome = narrow.execute(_call(SEARCH, {"index": "inventory"}))
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_FORBIDDEN")


def _state(elastic: Backend) -> tuple[int, int, int]:
    """Documents and searches served on ``logs-app-a``, and the index count;
    read without running a search, so reading it moves none of them."""
    status, stats = elastic.call("GET", "/logs-app-a/_stats/docs,search")
    assert status == 200, stats
    status, indices = elastic.call(
        "GET", "/_cat/indices?format=json&expand_wildcards=all"
    )
    assert status == 200, indices
    total = stats["_all"]["total"]
    return total["docs"]["count"], len(indices), total["search"]["query_total"]


def test_read_only_holds_at_both_layers(elastic, read_only_key, es_tools):
    before = _state(elastic)
    for param, value in [
        ("query", {"script": {"script": {"source": "ctx._source.level = 'x'"}}}),
        ("aggregations", {"all": {"global": {}}}),
        (
            "query",
            {
                "bool": {
                    "must": [{"match_all": {}}],
                    "runtime_mappings": {"x": {"type": "long"}},
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
    ]:
        outcome = es_tools.execute(
            _call(SEARCH, {"index": "logs-app-a", param: json.dumps(value)})
        )
        assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    # Nothing reached the backend: documents, indices and search count unchanged.
    assert _state(elastic) == before

    key_header = f"ApiKey {read_only_key}"
    writes = [
        ("PUT", "/logs-app-a/_doc/injected", {"message": "x"}, False),
        ("DELETE", "/logs-app-a", None, False),
        (
            "POST",
            "/_bulk",
            b'{"index":{"_index":"logs-app-a","_id":"z"}}\n{"message":"x"}\n',
            True,
        ),
        ("PUT", "/b1-new-index", {}, False),
    ]
    for method, path, body, ndjson in writes:
        status, answer = _http(
            method, elastic.url + path, body, auth=key_header, ndjson=ndjson
        )
        if path == "/_bulk" and status == 200:
            # _bulk answers 200 with per-item 403s.
            assert all(item["index"]["status"] == 403 for item in answer["items"]), (
                answer
            )
        else:
            assert status == 403, (method, path, status, answer)
    count, indices, _ = _state(elastic)
    assert (count, indices) == before[:2]
