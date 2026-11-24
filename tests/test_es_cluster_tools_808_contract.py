"""Independent contract tests for #808 (ruling B4-U7, amends R8-12): the five
read-only tools of upstream HolmesGPT's ``elasticsearch/cluster`` toolset.

Upstream source (names, parameters, paths): HolmesGPT commit ``a0009195``
(the pin of ``third_party/holmesgpt/PROVENANCE``),
``holmes/plugins/toolsets/elasticsearch/elasticsearch.py`` --
``ElasticsearchCat`` (``:345-425``), ``ElasticsearchClusterHealth``
(``:555-591``), ``ElasticsearchIndexStats`` (``:637-675``),
``ElasticsearchAllocationExplain`` (``:682-725``),
``ElasticsearchNodesStats`` (``:735-772``), toolset ``:909-930``.

Written without reading any #808 implementation. Same public path as the B1
contract tests (``tests/test_tool_expansion_b1_contract.py``): build the
``otel-demo-ext`` profile from an env mapping, call a tool by name through the
executor, assert on the view, the refusal and what a loopback fake
Elasticsearch received. No container, no model.

Assumptions (each also marked ``# ASSUMPTION:`` where used):

* The five tools ride the B1 gate unchanged: present exactly when the
  extended profile has ``OPSPILOT_ES_URL``, over the same ``es-ro``
  credential, under ``data_sources.available["elasticsearch"]``.
* Index-valued parameters follow the B1-3 index rule (``check_index``);
  ``api`` (upstream ``endpoint``), ``level``, ``metrics`` and ``health`` accept only the values
  upstream's descriptions list; ``node_id`` is ``_all``, ``_local`` or a
  node id/name of ``[A-Za-z0-9._-]``; anything else is ``INVALID_PARAMS``
  decided before any request.
* ``elasticsearch_allocation_explain`` may carry ``index``/``shard``/
  ``primary`` in a GET body (as upstream) or as query parameters (ES 8); the
  tests accept either, but the method is GET.
* Like the other state tools (``elasticsearch_mappings``), the answers carry
  no source time (``source_start_at``/``source_end_at`` are ``None``).
* Error classes are B1-12's (401/403 ``SOURCE_FORBIDDEN``; 429/5xx
  ``SOURCE_UNAVAILABLE``; 404 index_not_found ``INVALID_PARAMS`` with
  ``INDEX_NOT_FOUND``).

Projection hashes are not pinned here (the implementer moves the pins).
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

import pytest

from opspilot.tools.profiles import PROFILE_ENV, select_profile
from opspilot.tools.registry import RESERVED_PARAMETERS
from tests.m1_tool_support import FakeClock
from tests.test_m1_otel_demo_contract import NOW
from tests.test_tool_expansion_b1_contract import (
    CANARY,
    ES_API_KEY,
    ES_TOOLS,
    EXT_PROFILE,
    INJECTED_INDEX_NAMES,
    FakeElasticsearch,
    _call,
    _clean_environment,  # noqa: F401  (autouse: env and proxy isolation)
    _env,
    _es_error,
    _json,
    _run,
    _schemas,
    _text,
    _view,
    _worker,
)

CAT = "elasticsearch_cat"
HEALTH = "elasticsearch_cluster_health"
INDEX_STATS = "elasticsearch_index_stats"
EXPLAIN = "elasticsearch_allocation_explain"
NODES = "elasticsearch_nodes_stats"
CLUSTER_TOOLS = {CAT, HEALTH, INDEX_STATS, EXPLAIN, NODES}

# Upstream elasticsearch.py:358-395 (description of ``endpoint``).
CAT_ENDPOINTS = (
    "indices",
    "shards",
    "nodes",
    "health",
    "allocation",
    "recovery",
    "segments",
    "aliases",
    "pending_tasks",
    "thread_pool",
    "plugins",
    "nodeattrs",
    "repositories",
    "snapshots",
    "tasks",
)


class FakeCluster(FakeElasticsearch):
    """The B1 fake plus the cluster read paths, in Elasticsearch's shapes."""

    def __init__(self) -> None:
        super().__init__()
        self.cluster_delay = 0.0
        self.explanation = "the shard is unassigned"

    def answer(self, record: dict[str, Any]) -> tuple[int, dict[str, str], bytes]:
        path = record["path"]
        cluster = (
            path.startswith("/_cluster/")
            or path.startswith("/_nodes")
            or "/_stats" in path
            or (path.startswith("/_cat/") and not path.startswith("/_cat/indices"))
        )
        if not cluster:
            return super().answer(record)
        if self.cluster_delay:
            time.sleep(self.cluster_delay)
        if self.fail is not None:
            status, body = self.fail
            return status, {}, body if isinstance(body, bytes) else _json(body)
        if path.startswith("/_cluster/health"):
            return (
                200,
                {},
                _json(
                    {
                        "cluster_name": "lab",
                        "status": "yellow",
                        "number_of_nodes": 1,
                        "active_primary_shards": 3,
                        "active_shards": 3,
                        "unassigned_shards": 1,
                    }
                ),
            )
        if path == "/_cluster/allocation/explain":
            return (
                200,
                {},
                _json(
                    {
                        "index": "logs-app-a",
                        "shard": 0,
                        "primary": False,
                        "current_state": "unassigned",
                        "can_allocate": "no",
                        "allocate_explanation": (
                            f"{self.explanation}; operator note password={CANARY}"
                        ),
                    }
                ),
            )
        if path.startswith("/_nodes"):
            return (
                200,
                {},
                _json(
                    {
                        "_nodes": {"total": 1, "successful": 1, "failed": 0},
                        "cluster_name": "lab",
                        "nodes": {
                            "n1": {
                                "name": "es-node-1",
                                "jvm": {"mem": {"heap_used_percent": 61}},
                            }
                        },
                    }
                ),
            )
        if "/_stats" in path:
            target = path[1 : path.index("/_stats")]
            if self._indices(target) is None:
                return (*self._with_headers(self._not_found(target)),)
            return (
                200,
                {},
                _json(
                    {
                        "_shards": {"total": 1, "successful": 1, "failed": 0},
                        "_all": {"primaries": {"docs": {"count": 3}}},
                        "indices": {
                            "logs-app-a": {"primaries": {"docs": {"count": 3}}}
                        },
                    }
                ),
            )
        endpoint = path[len("/_cat/") :].split("/")[0]
        return 200, {}, _json([{"endpoint": endpoint, "state": "STARTED"}])


@pytest.fixture
def es() -> Iterator[FakeCluster]:
    fake = FakeCluster()
    fake.start()
    yield fake
    fake.stop()


def _web(url: str | None = "http://127.0.0.1:9200", profile: str = EXT_PROFILE):
    env = {PROFILE_ENV: profile}
    if url is not None:
        env["OPSPILOT_ES_URL"] = url
    return select_profile(env)


def _names(profile) -> list[str]:
    return [
        s["function"]["name"] for s in profile.face(FakeClock(start=NOW)).tool_schemas
    ]


# -- registration: extended profile with the ES source only -------------------


def test_cluster_tools_join_only_the_configured_extended_profile():
    with_es = _web()
    names = _names(with_es)
    assert CLUSTER_TOOLS <= set(names)
    assert len(names) == len(set(names))
    assert not CLUSTER_TOOLS & set(_names(_web(None)))
    assert not CLUSTER_TOOLS & set(_names(_web(profile="otel-demo")))
    # Credentials alone never add them (B1-U2).
    creds_only = select_profile(
        {PROFILE_ENV: EXT_PROFILE, "OPSPILOT_ES_API_KEY": ES_API_KEY}
    )
    assert not CLUSTER_TOOLS & set(_names(creds_only))
    # ASSUMPTION: same data source entry as the B1 tools.
    sources = with_es.face(FakeClock(start=NOW)).evidence_context("808-run")[
        "data_sources"
    ]
    assert set(sources["available"]["elasticsearch"]) == ES_TOOLS | CLUSTER_TOOLS
    # The web (URL only) and the worker (URL and key) agree (C3 §5).
    worker = select_profile(_env("http://127.0.0.1:9200"))
    assert with_es.versions() == worker.versions()
    assert (
        with_es.versions()["tool_schema_revision"]
        != _web(None).versions()["tool_schema_revision"]
    )


def test_schemas_match_upstream_parameters():
    schemas = _schemas(
        {PROFILE_ENV: EXT_PROFILE, "OPSPILOT_ES_URL": "http://127.0.0.1:9200"}
    )
    expected = {
        CAT: (
            {
                "api": "string",
                "index": "string",
                "columns": "string",
                "sort": "string",
                "health": "string",
            },
            ["api"],
        ),
        HEALTH: ({"index": "string", "level": "string"}, []),
        INDEX_STATS: ({"index": "string", "metrics": "string"}, ["index"]),
        EXPLAIN: (
            {"index": "string", "shard": "integer", "primary": "boolean"},
            [],
        ),
        NODES: ({"node_id": "string", "metrics": "string"}, []),
    }
    for tool, (properties, required) in expected.items():
        parameters = schemas[tool]["parameters"]
        assert {
            name: spec["type"] for name, spec in parameters["properties"].items()
        } == properties, tool
        assert sorted(parameters.get("required", [])) == required, tool
    # C3 §8: no registered tool exposes a reserved parameter key (upstream's
    # elasticsearch_cat ``endpoint`` is renamed ``api``).
    for tool, schema in schemas.items():
        assert not RESERVED_PARAMETERS & set(schema["parameters"]["properties"]), tool


# -- read-only: one GET to a fixed path per call ------------------------------

FIXED_PATHS = [
    (HEALTH, {}, {"/_cluster/health"}, {}),
    (
        HEALTH,
        {"index": "logs-app-a", "level": "shards"},
        {"/_cluster/health/logs-app-a"},
        {"level": ["shards"]},
    ),
    (NODES, {}, {"/_nodes/stats", "/_nodes/_all/stats"}, {}),
    (
        NODES,
        {"node_id": "_local", "metrics": "jvm,os"},
        {"/_nodes/_local/stats/jvm,os"},
        {},
    ),
    (INDEX_STATS, {"index": "logs-app-a"}, {"/logs-app-a/_stats"}, {}),
    (
        INDEX_STATS,
        {"index": "logs-app-*", "metrics": "docs,store"},
        {"/logs-app-*/_stats/docs,store"},
        {},
    ),
    (EXPLAIN, {}, {"/_cluster/allocation/explain"}, {}),
    (CAT, {"api": "health"}, {"/_cat/health"}, {"format": ["json"]}),
    (
        CAT,
        {
            "api": "shards",
            "index": "logs-app-a",
            "columns": "index,shard,prirep,state",
            "sort": "index",
        },
        {"/_cat/shards/logs-app-a"},
        {"format": ["json"], "h": ["index,shard,prirep,state"], "s": ["index"]},
    ),
    (
        CAT,
        {"api": "indices", "index": "logs-*", "health": "yellow"},
        {"/_cat/indices/logs-*"},
        {"format": ["json"], "health": ["yellow"]},
    ),
]


@pytest.mark.parametrize(("tool", "params", "paths", "query"), FIXED_PATHS)
def test_each_call_is_one_get_to_its_fixed_path(es, tool, params, paths, query):
    outcome = _run(es, tool, params)
    assert outcome.status == "ok", _view(outcome)
    assert len(es.requests) == 1, [(r["method"], r["path"]) for r in es.requests]
    record = es.requests[0]
    assert record["method"] == "GET"
    assert record["path"] in paths
    for key, value in query.items():
        assert record["query"].get(key) == value, record["query"]
    # The es-ro slot, and nothing else, authenticates the call (B1-4).
    assert record["headers"].get("authorization") == f"ApiKey {ES_API_KEY}"


@pytest.mark.parametrize("endpoint", CAT_ENDPOINTS)
def test_every_upstream_cat_endpoint_is_a_get_of_its_own_path(es, endpoint):
    outcome = _run(es, CAT, {"api": endpoint})
    assert outcome.status == "ok", _view(outcome)
    assert [(r["method"], r["path"]) for r in es.requests] == [
        ("GET", f"/_cat/{endpoint}")
    ]


def test_allocation_explain_names_the_requested_shard(es):
    outcome = _run(es, EXPLAIN, {"index": "logs-app-a", "shard": 0, "primary": False})
    assert outcome.status == "ok", _view(outcome)
    [record] = es.requests
    assert (record["method"], record["path"]) == ("GET", "/_cluster/allocation/explain")
    # ASSUMPTION: body (upstream) or query parameters (ES 8) both acceptable.
    if record["body"]:
        assert json.loads(record["body"]) == {
            "index": "logs-app-a",
            "shard": 0,
            "primary": False,
        }
    else:
        assert record["query"].get("index") == ["logs-app-a"]
        assert record["query"].get("shard") == ["0"]
        assert record["query"].get("primary") == ["false"]


def test_cat_free_text_cannot_add_query_parameters(es):
    outcome = _run(
        es,
        CAT,
        {
            "api": "shards",
            "index": "logs-app-a",
            "columns": "index&format=yaml&pretty=true",
            "sort": "index#x",
        },
    )
    if outcome.status == "error":
        # Refusing the value outright is fine: nothing was sent.
        assert outcome.reason == "INVALID_PARAMS"
        assert es.requests == []
        return
    [record] = es.requests
    assert record["method"] == "GET" and record["path"] == "/_cat/shards/logs-app-a"
    assert record["query"]["format"] == ["json"]
    assert set(record["query"]) <= {"format", "h", "s", "health"}


# -- parameters: no path or URL injection, closed value sets ------------------

INDEX_PARAMS = [
    (HEALTH, {}, "index"),
    (INDEX_STATS, {}, "index"),
    (CAT, {"api": "shards"}, "index"),
    (EXPLAIN, {"shard": 0, "primary": True}, "index"),
]


@pytest.mark.parametrize("name", INJECTED_INDEX_NAMES)
@pytest.mark.parametrize(("tool", "base", "param"), INDEX_PARAMS)
def test_injected_index_names_never_reach_the_backend(es, tool, base, param, name):
    # ASSUMPTION: the B1-3 index rule applies to every index-valued parameter.
    outcome = _run(es, tool, {**base, param: name})
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS"), _view(
        outcome
    )
    assert es.requests == []


BAD_VALUES = [
    (CAT, {"api": "indices/../../_security"}),
    (CAT, {"api": "../_bulk"}),
    (CAT, {"api": "tasks?detailed=true"}),
    (CAT, {"api": "INDICES"}),
    (CAT, {"api": "master"}),  # a real _cat API, not in upstream's list
    (CAT, {"api": ""}),
    (CAT, {}),
    (CAT, {"api": "indices", "health": "purple"}),
    (HEALTH, {"level": "everything"}),
    (HEALTH, {"level": "shards&wait_for_status=green"}),
    (INDEX_STATS, {}),
    (INDEX_STATS, {"index": "logs-app-a", "metrics": "docs/../../_bulk"}),
    (INDEX_STATS, {"index": "logs-app-a", "metrics": "docs?pretty"}),
    (INDEX_STATS, {"index": "logs-app-a", "metrics": "not_a_metric"}),
    (NODES, {"node_id": "../_security"}),
    (NODES, {"node_id": "_all/../../_bulk"}),
    (NODES, {"node_id": "n1?pretty"}),
    (NODES, {"node_id": "n1#x"}),
    (NODES, {"node_id": "n 1"}),
    (NODES, {"node_id": "%2e%2e"}),
    (NODES, {"metrics": "jvm/../../_shutdown"}),
    (NODES, {"metrics": "not_a_metric"}),
    (EXPLAIN, {"index": "logs-app-a", "shard": -1}),
    (EXPLAIN, {"index": "logs-app-a", "shard": "0"}),
    (EXPLAIN, {"index": "logs-app-a", "shard": 0, "primary": "yes"}),
]


@pytest.mark.parametrize(("tool", "params"), BAD_VALUES)
def test_values_outside_the_upstream_sets_are_refused_before_sending(es, tool, params):
    # ASSUMPTION: closed value sets from upstream's parameter descriptions.
    outcome = _run(es, tool, params)
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS"), _view(
        outcome
    )
    assert es.requests == []


@pytest.mark.parametrize(
    ("tool", "params"),
    [
        (NODES, {"node_id": "_all"}),
        (NODES, {"node_id": "_local", "metrics": "_all"}),
        (NODES, {"node_id": "Xy3_node-1.a", "metrics": "breaker,fs,thread_pool"}),
        (INDEX_STATS, {"index": "_all"}),
        (INDEX_STATS, {"index": "logs-app-*,-logs-app-b", "metrics": "_all"}),
        (HEALTH, {"level": "cluster"}),
        (HEALTH, {"level": "indices"}),
        (CAT, {"api": "indices", "health": "red"}),
    ],
)
def test_upstream_values_are_accepted(es, tool, params):
    outcome = _run(es, tool, params)
    assert outcome.status == "ok", _view(outcome)
    assert [r["method"] for r in es.requests] == ["GET"]


# -- view: redaction, current-state, errors, timeout --------------------------

OK_CALLS = [
    (CAT, {"api": "health"}, "STARTED"),
    (HEALTH, {}, "yellow"),
    (INDEX_STATS, {"index": "logs-app-a"}, "logs-app-a"),
    (EXPLAIN, {}, "unassigned"),
    (NODES, {}, "heap_used_percent"),
]


@pytest.mark.parametrize(("tool", "params", "marker"), OK_CALLS)
def test_answers_are_current_state_without_source_time(es, tool, params, marker):
    outcome = _run(es, tool, params)
    assert outcome.status == "ok", _view(outcome)
    view = _view(outcome)
    assert marker in _text(outcome)
    # ASSUMPTION: no source time, like the other ES state tools.
    assert view.get("source_start_at") is None
    assert view.get("source_end_at") is None


def test_secrets_in_answers_are_redacted(es):
    outcome = _run(es, EXPLAIN, {})
    assert outcome.status == "ok", _view(outcome)
    text = _text(outcome)
    assert "the shard is unassigned" in text
    assert CANARY not in text and "REDACTED" in text


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (401, "SOURCE_FORBIDDEN"),
        (403, "SOURCE_FORBIDDEN"),
        (429, "SOURCE_UNAVAILABLE"),
        (503, "SOURCE_UNAVAILABLE"),
    ],
)
@pytest.mark.parametrize(("tool", "params", "_marker"), OK_CALLS)
def test_backend_statuses_map_to_fixed_reasons(
    es, tool, params, _marker, status, reason
):
    es.fail = (status, _es_error(status, "security_exception", "synthetic"))
    outcome = _run(es, tool, params)
    assert (outcome.status, outcome.reason) == ("error", reason), _view(outcome)


def test_missing_index_stats_is_index_not_found(es):
    outcome = _run(es, INDEX_STATS, {"index": "no-such-index"})
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS"), _view(
        outcome
    )
    assert "INDEX_NOT_FOUND" in _text(outcome)


def test_a_call_past_the_deadline_is_a_timeout(es):
    es.cluster_delay = 4.0
    executor, _ = _worker(_env(es.url), deadline_seconds=2)
    started = time.monotonic()
    outcome = executor.execute(_call(HEALTH, {}))
    assert outcome.status == "timeout", _view(outcome)
    assert time.monotonic() - started < 3.9


def test_unconfigured_extension_worker_has_no_cluster_tools(es):
    executor, _ = _worker(_env(None))
    for index, tool in enumerate(sorted(CLUSTER_TOOLS)):
        outcome = executor.execute(
            _call(tool, {"api": "health", "index": "logs-app-a"}, index=index)
        )
        assert outcome.status in ("error", "denied"), tool
    assert es.requests == []
