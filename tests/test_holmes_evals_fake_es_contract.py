"""#666 PR-B contract: the data-driven fake Elasticsearch (H-25).

Independent contract tests written from the task record's PR-B public
interface (``fake_es.py``) without reading the implementation. Collection
stays green before the module exists (lazy import).

Covered: ApiKey authentication (engineer key full rights; keys minted by
``POST /_security/api_key`` read-only, writes 403 ``security_exception``;
missing/unknown 401), the operations the upstream ``before_test`` scripts use
(index create, ``_bulk``, ``_doc``, ``_refresh``, ``_count``, ``DELETE``,
``_index_template``, ``_cluster/settings``), the product's read endpoints
(``_cat/indices``, ``_mapping``, ``_search`` with the listed query subset and
aggregations), 400 for unsupported constructs and unknown paths, and the
loopback-only server. An end-to-end check runs real upstream ``before_test``
scripts through ``setup.run_script`` against the served fake when the pinned
checkout is present (skipped in CI).

Assumptions beyond the interface (also marked ``# ASSUMPTION:``):

* ``handle`` receives header names as written by the client
  (``Authorization``); the tests use that exact spelling.
* Documents become visible to ``_search``/``_count`` at the latest after an
  explicit ``_refresh`` (the tests always refresh).
* ``_cat/indices?format=json`` values follow ES: strings; numbers are read
  with ``int()``.
* ``min``/``max`` over a date field return ``value`` in epoch milliseconds,
  as ES does.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from tests.holmes_evals_b_support import (
    ENGINEER,
    OTHER,
    Es,
    encoded,
    error_type,
    http,
    jbody,
    ndjson,
    new_store,
    no_proxy_env,  # noqa: F401 - fixture
    served,
)
from tests.holmes_evals_support import mod, upstream_root

pytestmark = pytest.mark.usefixtures("no_proxy_env")


@pytest.fixture
def es() -> Es:
    return Es(new_store())


def _ms(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000


def _readonly(es: Es) -> dict[str, str]:
    out = es.ok(
        "POST",
        "/_security/api_key",
        {
            "name": "opspilot-investigator",
            "expiration": "7d",
            "role_descriptors": {
                "opspilot_readonly": {
                    "cluster": ["monitor"],
                    "indices": [
                        {
                            "names": ["*"],
                            "privileges": ["read", "view_index_metadata", "monitor"],
                        }
                    ],
                }
            },
        },
    )
    assert isinstance(out.get("id"), str) and out["id"]
    assert isinstance(out.get("api_key"), str) and out["api_key"]
    assert isinstance(out.get("encoded"), str) and out["encoded"]
    return out


def _seed_logs(es: Es, index: str = "app-275-logs-x") -> None:
    es.create(
        index,
        {
            "@timestamp": {"type": "date"},
            "severity": {"type": "keyword"},
            "service": {"type": "keyword"},
            "message": {"type": "text"},
            "latency_ms": {"type": "long"},
        },
    )
    es.bulk(
        index,
        [
            {"@timestamp": "2026-05-14T12:00:01Z", "severity": "ERROR",
             "service": "checkout", "message": "DB connection refused 1",
             "latency_ms": 900},
            {"@timestamp": "2026-05-14T12:00:02Z", "severity": "ERROR",
             "service": "checkout", "message": "DB connection refused 2",
             "latency_ms": 700},
            {"@timestamp": "2026-05-14T12:01:01Z", "severity": "WARN",
             "service": "payment", "message": "Slow query 1",
             "latency_ms": 300},
            {"@timestamp": "2026-05-14T14:02:01+02:00", "severity": "INFO",
             "service": "payment", "message": "Health check ok",
             "latency_ms": 100},
            {"@timestamp": "2026-05-14T13:30:00Z", "severity": "INFO",
             "service": "frontend", "message": "Health check ok",
             "latency_ms": 50},
        ],
    )  # fmt: skip


def _ids(out: dict) -> list[str]:
    return [h["_id"] for h in out["hits"]["hits"]]


def _total(out: dict) -> int:
    total = out["hits"]["total"]
    return total["value"] if isinstance(total, dict) else int(total)


# -- authentication --------------------------------------------------------------


def test_missing_unknown_or_basic_credentials_get_401(es: Es) -> None:
    _seed_logs(es)
    assert es("GET", "/app-275-logs-x/_count", headers={})[0] == 401
    assert es("GET", "/app-275-logs-x/_count", key=OTHER)[0] == 401
    basic = {"Authorization": "Basic " + encoded("elastic", "changeme")}
    assert es("GET", "/app-275-logs-x/_count", headers=basic)[0] == 401
    # A write with a bad key changes nothing.
    assert es("DELETE", "/app-275-logs-x", key=OTHER)[0] == 401
    assert es.ok("GET", "/app-275-logs-x/_count")["count"] == 5


def test_minted_key_reads_but_every_write_is_403_security_exception(es: Es) -> None:
    _seed_logs(es)
    minted = _readonly(es)
    for ro in (minted["encoded"], encoded(minted["id"], minted["api_key"])):
        assert es("GET", "/app-275-logs-x/_count", key=ro)[0] == 200
        assert es("GET", "/_cat/indices", query={"format": "json"}, key=ro)[0] == 200
        assert es("GET", "/app-275-logs-x/_mapping", key=ro)[0] == 200
        status, _ = es(
            "POST", "/app-275-logs-x/_search", {"query": {"match_all": {}}}, key=ro
        )
        assert status == 200
        writes = [
            ("PUT", "/app-999-new", jbody({})),
            ("DELETE", "/app-275-logs-x", b""),
            ("POST", "/app-275-logs-x/_doc", jbody({"message": "x"})),
            ("POST", "/app-275-logs-x/_bulk",
             ndjson([{"index": {}}, {"message": "x"}])),
            ("POST", "/_bulk",
             ndjson([{"index": {"_index": "app-275-logs-x"}}, {"message": "x"}])),
            ("PUT", "/_cluster/settings",
             jbody({"persistent": {"cluster.max_shards_per_node": 10000}})),
            ("PUT", "/_index_template/t1",
             jbody({"index_patterns": ["app-9-*"], "template": {}})),
        ]  # fmt: skip
        for method, path, body in writes:
            status, payload = es(method, path, body, key=ro)
            assert status == 403, (method, path, status)
            assert error_type(payload) == "security_exception", payload
    assert es.ok("GET", "/app-275-logs-x/_count")["count"] == 5
    rows = es.ok("GET", "/_cat/indices", query={"format": "json", "h": "index"})
    assert [r["index"] for r in rows] == ["app-275-logs-x"]


# -- the upstream setup operations ----------------------------------------------


def test_cluster_settings_round_trip_like_es_test_utils(es: Es) -> None:
    got = es.ok(
        "GET",
        "/_cluster/settings",
        query={"include_defaults": "true", "flat_settings": "true"},
    )
    limit = (
        got.get("persistent", {}).get("cluster.max_shards_per_node")
        or got.get("transient", {}).get("cluster.max_shards_per_node")
        or got.get("defaults", {}).get("cluster.max_shards_per_node")
    )
    assert limit is not None
    put = es.ok(
        "PUT", "/_cluster/settings",
        {"persistent": {"cluster.max_shards_per_node": 10000}},
    )  # fmt: skip
    assert put.get("acknowledged") is True
    # es_test_utils.sh greps the raw body for '"acknowledged":true'.
    assert '"acknowledged":true' in json.dumps(put, separators=(",", ":"))
    again = es.ok(
        "GET",
        "/_cluster/settings",
        query={"include_defaults": "true", "flat_settings": "true"},
    )
    assert int(again["persistent"]["cluster.max_shards_per_node"]) == 10000


def test_create_index_acknowledged_and_mapping_returned(es: Es) -> None:
    out = es.ok(
        "PUT",
        "/app-275-logs-x",
        {
            "settings": {"number_of_shards": 1, "number_of_replicas": 0},
            "mappings": {"properties": {"@timestamp": {"type": "date"},
                                        "severity": {"type": "keyword"}}},
        },
        query={"wait_for_active_shards": "1"},
    )  # fmt: skip
    assert out.get("acknowledged") is True
    mapping = es.ok("GET", "/app-275-logs-x/_mapping")
    props = mapping["app-275-logs-x"]["mappings"]["properties"]
    assert props["@timestamp"]["type"] == "date"
    assert props["severity"]["type"] == "keyword"


def test_bulk_index_create_delete_with_index_in_action(es: Es) -> None:
    lines = [
        {"index": {"_index": "app-1-a", "_id": "1"}},
        {"message": "one"},
        {"create": {"_index": "app-1-a", "_id": "2"}},
        {"message": "two"},
        {"index": {"_index": "app-1-b"}},
        {"message": "three"},
        {"delete": {"_index": "app-1-a", "_id": "1"}},
    ]
    out = es.ok("POST", "/_bulk", ndjson(lines), query={"refresh": "wait_for"})
    assert out["errors"] is False
    assert len(out["items"]) == 4
    assert [next(iter(item)) for item in out["items"]] == [
        "index", "create", "index", "delete",
    ]  # fmt: skip
    for item in out["items"]:
        body = next(iter(item.values()))
        assert body["_index"] in {"app-1-a", "app-1-b"}
        assert isinstance(body["_id"], str) and body["_id"]
        assert isinstance(body["status"], int)
    es.ok("POST", "/_refresh")
    assert es.ok("GET", "/app-1-a/_count")["count"] == 1
    assert es.ok("GET", "/app-1-b/_count")["count"] == 1
    assert es.sources("app-1-a") == {"2": {"message": "two"}}


def test_bulk_on_index_path_and_doc_post(es: Es) -> None:
    es.create("app-2-x", {"message": {"type": "text"}})
    out = es.ok(
        "POST", "/app-2-x/_bulk",
        ndjson([{"index": {}}, {"message": "a"}, {"index": {}}, {"message": "b"}]),
    )  # fmt: skip
    assert out["errors"] is False and len(out["items"]) == 2
    doc = es.ok("POST", "/app-2-x/_doc", {"message": "c"})
    assert isinstance(doc.get("_id"), str) and doc["_id"]
    es.ok("POST", "/app-2-x/_refresh")
    assert es.ok("GET", "/app-2-x/_count")["count"] == 3
    assert (
        es.ok("POST", "/app-2-x/_forcemerge", query={"max_num_segments": "1"})
        is not None
    )


def test_count_with_query(es: Es) -> None:
    _seed_logs(es)
    out = es.ok(
        "POST", "/app-275-logs-x/_count", {"query": {"term": {"severity": "ERROR"}}}
    )
    assert out["count"] == 2


def test_delete_exact_and_wildcard(es: Es) -> None:
    for name in ("app-275-a", "app-275-b", "app-2750-c", "app-276-a"):
        es.create(name, {"m": {"type": "keyword"}})
    es.ok("DELETE", "/app-275-a")
    es.ok("DELETE", "/app-275-*")
    rows = es.ok("GET", "/_cat/indices", query={"format": "json", "h": "index"})
    assert sorted(r["index"] for r in rows) == ["app-2750-c", "app-276-a"]
    # Deleting a missing index is a 404 that upstream scripts tolerate with
    # ``|| true``; it must not 5xx.
    assert es("DELETE", "/app-275-a")[0] in (200, 404)


def test_index_template_applies_on_create_and_can_be_deleted(es: Es) -> None:
    es.ok(
        "PUT",
        "/_index_template/app-9-tpl",
        {
            "index_patterns": ["app-9-*"],
            "template": {"mappings": {"properties": {"code": {"type": "keyword"},
                                                     "ts": {"type": "date"}}}},
        },
    )  # fmt: skip
    es.bulk("app-9-x", [{"code": "abc", "ts": "2024-01-15T10:00:00Z", "note": "n"}])
    props = es.ok("GET", "/app-9-x/_mapping")["app-9-x"]["mappings"]["properties"]
    assert props["code"]["type"] == "keyword"
    assert props["ts"]["type"] == "date"
    es.ok("DELETE", "/_index_template/app-9-tpl")
    es.bulk("app-9-y", [{"code": "abc"}])
    props = es.ok("GET", "/app-9-y/_mapping")["app-9-y"]["mappings"]["properties"]
    assert props["code"]["type"] == "text"


def test_dynamic_mapping_types(es: Es) -> None:
    es.bulk(
        "app-3-dyn",
        [
            {
                "when": "2024-01-15T10:00:00Z",
                "day": "2024-01-15",
                "word": "hello",
                "count": 3,
                "ratio": 1.5,
                "flag": True,
                "obj": {"inner": "x", "at": "2025-01-01T00:00:00.123Z"},
            }
        ],
    )
    props = es.ok("GET", "/app-3-dyn/_mapping")["app-3-dyn"]["mappings"]["properties"]
    assert props["when"]["type"] == "date"
    assert props["day"]["type"] == "date"
    assert props["word"]["type"] == "text"
    assert props["word"]["fields"]["keyword"]["type"] == "keyword"
    assert props["count"]["type"] == "long"
    assert props["ratio"]["type"] == "float"
    assert props["flag"]["type"] == "boolean"
    inner = props["obj"]["properties"]
    assert inner["inner"]["type"] == "text"
    assert inner["at"]["type"] == "date"


def test_cat_indices_json_columns_sort_and_pattern(es: Es) -> None:
    es.bulk("app-4-small", [{"m": "x"}])
    es.bulk("app-4-large", [{"m": "y" * 2000} for _ in range(20)])
    es.bulk("app-5-other", [{"m": "z"}])
    rows = es.ok(
        "GET",
        "/_cat/indices/app-4-*",
        query={"format": "json", "h": "index,store.size,docs.count",
               "s": "store.size:desc", "bytes": "b"},
    )  # fmt: skip
    assert [r["index"] for r in rows] == ["app-4-large", "app-4-small"]
    assert all(set(r) == {"index", "store.size", "docs.count"} for r in rows)
    assert int(rows[0]["docs.count"]) == 20
    assert int(rows[0]["store.size"]) > int(rows[1]["store.size"]) > 0
    every = es.ok("GET", "/_cat/indices", query={"format": "json"})
    names = {r["index"] for r in every}
    assert {"app-4-small", "app-4-large", "app-5-other"} <= names
    assert all("health" in r and "docs.count" in r for r in every)


# -- search ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ({"match_all": {}}, 5),
        ({"term": {"severity": "ERROR"}}, 2),
        ({"terms": {"service": ["payment", "frontend"]}}, 3),
        ({"match": {"message": "health"}}, 2),
        ({"match_phrase": {"message": "connection refused"}}, 2),
        ({"exists": {"field": "latency_ms"}}, 5),
        ({"exists": {"field": "no_such_field"}}, 0),
        ({"prefix": {"service": "pay"}}, 2),
        ({"wildcard": {"service": "check*"}}, 2),
        ({"range": {"latency_ms": {"gte": 300, "lt": 900}}}, 2),
        ({"query_string": {"query": "severity:ERROR"}}, 2),
        ({"query_string": {"query": "severity:INFO AND service:payment"}}, 1),
        ({"query_string": {"query": "severity:WARN OR service:frontend"}}, 2),
        (
            {"bool": {"must": [{"term": {"severity": "INFO"}}],
                      "must_not": [{"term": {"service": "frontend"}}]}},
            1,
        ),
        (
            {"bool": {"filter": [{"range": {"latency_ms": {"gt": 100}}}],
                      "should": [{"term": {"service": "payment"}},
                                 {"term": {"severity": "ERROR"}}],
                      "minimum_should_match": 1}},
            3,
        ),
    ],
)  # fmt: skip
def test_search_query_subset(es: Es, query: dict, expected: int) -> None:
    _seed_logs(es)
    out = es.search("app-275-logs-x", {"query": query, "track_total_hits": True})
    assert _total(out) == expected
    assert len(out["hits"]["hits"]) == expected
    for hit in out["hits"]["hits"]:
        assert hit["_index"] == "app-275-logs-x"
        assert isinstance(hit["_source"], dict)


def test_range_on_date_field_compares_instants_not_strings(es: Es) -> None:
    _seed_logs(es)
    # 14:02:01+02:00 is 12:02:01Z: inside [12:01:30Z, 12:30Z) by time, but a
    # string comparison would place it after 12:30.
    out = es.search(
        "app-275-logs-x",
        {"query": {"range": {"@timestamp": {"gte": "2026-05-14T12:01:30Z",
                                            "lt": "2026-05-14T12:30:00Z"}}}},
    )  # fmt: skip
    assert [h["_source"]["message"] for h in out["hits"]["hits"]] == ["Health check ok"]
    assert out["hits"]["hits"][0]["_source"]["service"] == "payment"


def test_size_from_sort_and_source_filtering(es: Es) -> None:
    _seed_logs(es)
    out = es.search(
        "app-275-logs-x",
        {"query": {"match_all": {}}, "sort": [{"latency_ms": "desc"}],
         "size": 2, "from": 1, "_source": ["service", "latency_ms"],
         "timeout": "30s"},
    )  # fmt: skip
    assert [h["_source"] for h in out["hits"]["hits"]] == [
        {"service": "checkout", "latency_ms": 700},
        {"service": "payment", "latency_ms": 300},
    ]
    out = es.search(
        "app-275-logs-x",
        {"query": {"match_all": {}}, "sort": [{"@timestamp": {"order": "asc"}}],
         "size": 1, "_source": {"excludes": ["message", "latency_ms"]}},
    )  # fmt: skip
    first = out["hits"]["hits"][0]["_source"]
    assert first == {"@timestamp": "2026-05-14T12:00:01Z", "severity": "ERROR",
                     "service": "checkout"}  # fmt: skip
    out = es.search(
        "app-275-logs-x",
        {"query": {"match_all": {}}, "sort": ["_doc"], "size": 2,
         "_source": {"includes": ["message"]}},
    )  # fmt: skip
    assert all(set(h["_source"]) == {"message"} for h in out["hits"]["hits"])


def test_search_over_wildcard_pattern_and_get_method(es: Es) -> None:
    es.bulk("app-6-a", [{"m": "x"}])
    es.bulk("app-6-b", [{"m": "y"}, {"m": "z"}])
    es.bulk("app-7-c", [{"m": "w"}])
    out = es.ok("GET", "/app-6-*/_search", {"query": {"match_all": {}}})
    assert _total(out) == 3
    assert {h["_index"] for h in out["hits"]["hits"]} == {"app-6-a", "app-6-b"}


def test_aggregations(es: Es) -> None:
    _seed_logs(es)
    out = es.search(
        "app-275-logs-x",
        {
            "size": 0,
            "aggs": {
                "first": {"min": {"field": "@timestamp"}},
                "last": {"max": {"field": "@timestamp"}},
                "avg_lat": {"avg": {"field": "latency_ms"}},
                "sum_lat": {"sum": {"field": "latency_ms"}},
                "n": {"value_count": {"field": "latency_ms"}},
                "services": {"cardinality": {"field": "service"}},
                "by_sev": {
                    "terms": {"field": "severity", "size": 2},
                    "aggs": {"max_lat": {"max": {"field": "latency_ms"}}},
                },
                "per_hour": {
                    "date_histogram": {"field": "@timestamp", "fixed_interval": "1h"}
                },
            },
        },
    )
    aggs = out["aggregations"]
    assert aggs["first"]["value"] == _ms("2026-05-14T12:00:01Z")
    assert aggs["last"]["value"] == _ms("2026-05-14T13:30:00Z")
    assert aggs["avg_lat"]["value"] == pytest.approx(410.0)
    assert aggs["sum_lat"]["value"] == pytest.approx(2050.0)
    assert aggs["n"]["value"] == 5
    assert aggs["services"]["value"] == 3
    buckets = aggs["by_sev"]["buckets"]
    assert [(b["key"], b["doc_count"]) for b in buckets] == [("ERROR", 2), ("INFO", 2)]
    assert buckets[0]["max_lat"]["value"] == pytest.approx(900.0)
    hours = [(b["key"], b["doc_count"]) for b in aggs["per_hour"]["buckets"]]
    assert hours == [
        (_ms("2026-05-14T12:00:00Z"), 4),
        (_ms("2026-05-14T13:00:00Z"), 1),
    ]


def test_profile_block(es: Es) -> None:
    _seed_logs(es)
    out = es.search(
        "app-275-logs-x",
        {"profile": True, "query": {"bool": {"must": [{"match_all": {}}]}}},
    )
    shard = out["profile"]["shards"][0]
    top = shard["searches"][0]["query"][0]
    assert isinstance(top["type"], str) and top["type"]
    assert "bool" in top["type"].lower()


@pytest.mark.parametrize(
    "body",
    [
        {"query": {"geo_distance": {"distance": "1km", "loc": [0, 0]}}},
        {"query": {"script": {"script": "doc['x'].value > 1"}}},
        {"query": {"fuzzy": {"service": "checkot"}}},
        {"query": {"match_all": {}}, "aggs": {"p": {"percentiles": {"field": "latency_ms"}}}},
        {"query": {"match_all": {}}, "aggs": {"h": {"date_histogram": {"field": "@timestamp", "calendar_interval": "1M"}}}},
    ],
)  # fmt: skip
def test_unsupported_constructs_are_400_parsing_exception(es: Es, body: dict) -> None:
    _seed_logs(es)
    status, payload = es("POST", "/app-275-logs-x/_search", body)
    assert status == 400
    assert error_type(payload) == "parsing_exception"


def test_unknown_path_is_400_illegal_argument(es: Es) -> None:
    status, payload = es("GET", "/_snapshot/repo1")
    assert status == 400
    assert error_type(payload) == "illegal_argument_exception"


def test_errors_never_echo_credentials(es: Es) -> None:
    for status, payload in (
        es("GET", "/_cat/indices", key=OTHER),
        es("GET", "/_snapshot/repo1"),
    ):
        text = json.dumps(payload)
        assert ENGINEER not in text and OTHER not in text


# -- server --------------------------------------------------------------------------


def test_serve_refuses_non_loopback_host() -> None:
    store = new_store()
    with pytest.raises(Exception):  # noqa: B017 - any refusal
        server = mod("fake_es").serve(store, "0.0.0.0", 0)
        server.server_close()


def test_served_fake_answers_over_http_with_apikey() -> None:
    store = new_store()
    with served(store) as url:
        assert url.startswith("http://127.0.0.1:")
        assert http("PUT", f"{url}/app-8-x", body={})[0] == 200
        status, _ = http(
            "POST", f"{url}/app-8-x/_bulk",
            body=ndjson([{"index": {}}, {"m": "x"}]),
            headers={"Content-Type": "application/x-ndjson"},
        )  # fmt: skip
        assert status == 200
        assert http("POST", f"{url}/app-8-x/_refresh")[0] == 200
        status, payload = http("GET", f"{url}/app-8-x/_count")
        assert (status, payload["count"]) == (200, 1)
        assert http("GET", f"{url}/app-8-x/_count", key=None)[0] == 401


def test_served_wire_format_matches_upstream_greps() -> None:
    """Upstream scripts grep raw bodies: '"acknowledged":true',
    '"errors":true', '"count":[0-9]*' (es_test_utils.sh, before_test)."""
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def raw(
        method: str,
        url: str,
        body: bytes | None = None,
        ctype: str = "application/json",
    ) -> bytes:
        req = urllib.request.Request(
            url, data=body, method=method,
            headers={"Authorization": f"ApiKey {ENGINEER}", "Content-Type": ctype},
        )  # fmt: skip
        with opener.open(req, timeout=10) as response:
            return response.read()

    store = new_store()
    with served(store) as url:
        assert b'"acknowledged":true' in raw("PUT", f"{url}/app-8-y", b"{}")
        bulk = raw(
            "POST", f"{url}/app-8-y/_bulk",
            ndjson([{"index": {}}, {"m": "x"}, {"index": {}}, {"m": "y"}]),
            "application/x-ndjson",
        )  # fmt: skip
        assert b'"errors":false' in bulk
        raw("POST", f"{url}/app-8-y/_refresh")
        assert b'"count":2' in raw("GET", f"{url}/app-8-y/_count")
        settings = raw(
            "PUT", f"{url}/_cluster/settings",
            jbody({"persistent": {"cluster.max_shards_per_node": 10000}}),
        )  # fmt: skip
        assert b'"acknowledged":true' in settings


def test_cli_prints_url_first_line(tmp_path: Path) -> None:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    key_file = tmp_path / "engineer.key"
    key_file.write_text(ENGINEER + "\n")
    key_file.chmod(0o600)
    proc = subprocess.Popen(
        [sys.executable, "-m", "scripts.holmes_evals.fake_es",
         "--port", str(port), "--key-file", str(key_file)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        cwd=Path(__file__).resolve().parents[1],
    )  # fmt: skip
    try:
        assert proc.stdout is not None
        first = json.loads(proc.stdout.readline())
        assert first["url"] == f"http://127.0.0.1:{port}"
        status, payload = http("GET", f"{first['url']}/_cat/indices?format=json")
        assert status == 200 and payload == []
        assert (
            http("GET", f"{first['url']}/_cat/indices?format=json", key=OTHER)[0] == 401
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


# -- upstream before_test end to end (local only) -------------------------------------


#: Questions whose ``before_test`` is short and uses only the listed
#: operations (es_setup, PUT index, _bulk, _refresh, _count, DELETE).
_E2E = (
    ("275_es_field_name_correction", "app-275-logs-qe7p2k4d", 30),
    ("254_elasticsearch_dr_test_log_check", "app-254-prod-logs", None),
    ("278_es_schema_discovery", "app-278-events-n8c3v6q1", None),
)


@pytest.mark.parametrize(("qid", "index", "count"), _E2E)
def test_upstream_before_test_runs_against_served_fake(
    tmp_path: Path, qid: str, index: str, count: int | None
) -> None:
    root = upstream_root()
    if root is None:
        pytest.skip("pinned upstream checkout not present")
    question = mod("questions").load_question(root, qid)
    setup = mod("setup")
    store = new_store()
    with served(store) as url:
        env = setup.setup_env(url, ENGINEER, None)
        qdir = root / "tests/llm/fixtures/test_ask_holmes" / qid
        result = setup.run_script(
            question.before_test,
            operation="before_test",
            cwd=qdir,
            env=env,
            timeout_s=120,
            log_path=tmp_path / "before.log",
        )
        assert result.timed_out is False
        assert result.exit_code == 0, (tmp_path / "before.log").read_text()[-2000:]
        es = Es(store)
        got = es.ok("GET", f"/{index}/_count")["count"]
        assert got > 0
        if count is not None:
            assert got == count
        # The product's read-only identity can see it; cleanup removes it.
        ro = _readonly(es)["encoded"]
        assert es("GET", f"/{index}/_count", key=ro)[0] == 200
        after = setup.run_script(
            question.after_test,
            operation="after_test",
            cwd=qdir,
            env=env,
            timeout_s=120,
            log_path=tmp_path / "after.log",
        )
        assert after.exit_code == 0
        # Some upstream after_test scripts keep their indices on purpose
        # (reentrancy); the evaluation's own purge removes them (H-7).
        setup.purge_question_indices(mod("shift").EsAdmin(url, ENGINEER), qid)
        rows = es.ok("GET", "/_cat/indices", query={"format": "json", "h": "index"})
        assert index not in {r["index"] for r in rows}


def test_upstream_shard_limit_setup_needs_update_flag(tmp_path: Path) -> None:
    """es_setup only raises the shard limit with ES_UPDATE_SHARD_LIMIT=true,
    which ``setup_env`` sets (H-5)."""
    root = upstream_root()
    if root is None:
        pytest.skip("pinned upstream checkout not present")
    setup = mod("setup")
    store = new_store()
    with served(store) as url:
        env = setup.setup_env(url, ENGINEER, None)
        assert env["ES_UPDATE_SHARD_LIMIT"] == "true"
        result = setup.run_script(
            "source ../../shared/es_test_utils.sh\nes_setup\n",
            operation="before_test",
            cwd=root
            / "tests/llm/fixtures/test_ask_holmes/275_es_field_name_correction",
            env=env,
            timeout_s=60,
            log_path=tmp_path / "setup.log",
        )
        assert result.exit_code == 0, (tmp_path / "setup.log").read_text()[-2000:]
        got = Es(store).ok(
            "GET", "/_cluster/settings",
            query={"include_defaults": "true", "flat_settings": "true"},
        )  # fmt: skip
        assert int(got["persistent"]["cluster.max_shards_per_node"]) >= 10000
