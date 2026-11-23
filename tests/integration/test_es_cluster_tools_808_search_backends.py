"""Real-backend integration test for #808 (B4-U7, R8-25 style): the five
upstream ``elasticsearch/cluster`` tools against the CI ``search-backends``
service containers (Elasticsearch 8 with security on, OpenSearch 2 with the
security plugin off), through the public tool path. No model.

Runs only where ``OPSPILOT_SEARCH_BACKENDS=1`` (the CI job); skipped
elsewhere. Reuses the B1 fixtures: the engineer account seeds the synthetic
indices and creates the read-only API key (``cluster: monitor``); the tools
only ever see that key. One extra index with a replica on a single node
gives a real unassigned shard, a yellow cluster and something for
``elasticsearch_allocation_explain`` to explain.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from tests.integration.test_tool_expansion_b1_search_backends import (
    LOGS_A,
    Backend,
    _call,
    _view,
    elastic,  # noqa: F401  (module fixture)
    es_tools,  # noqa: F401
    k8s_ca,  # noqa: F401
    opensearch,  # noqa: F401
    os_tools,  # noqa: F401
    pytestmark,  # noqa: F401  (the OPSPILOT_SEARCH_BACKENDS gate)
    read_only_key,  # noqa: F401
)

CAT = "elasticsearch_cat"
HEALTH = "elasticsearch_cluster_health"
INDEX_STATS = "elasticsearch_index_stats"
EXPLAIN = "elasticsearch_allocation_explain"
NODES = "elasticsearch_nodes_stats"

REPLICA_INDEX = "cluster-808-replica"


def _with_replica(backend: Backend) -> None:
    backend.call("DELETE", f"/{REPLICA_INDEX}")
    status, body = backend.call(
        "PUT",
        f"/{REPLICA_INDEX}",
        {"settings": {"number_of_shards": 1, "number_of_replicas": 1}},
    )
    assert status == 200, body
    status, body = backend.call(
        "GET", f"/_cluster/health/{REPLICA_INDEX}?wait_for_status=yellow&timeout=30s"
    )
    assert status == 200 and body["status"] == "yellow", body


@pytest.fixture(params=["elasticsearch", "opensearch"])
def cluster(request) -> Iterator[Any]:
    backend: Backend = request.getfixturevalue(request.param)
    _with_replica(backend)
    tools = request.getfixturevalue(
        "es_tools" if request.param == "elasticsearch" else "os_tools"
    )
    yield tools
    backend.call("DELETE", f"/{REPLICA_INDEX}")


def _values(value: object, key: str) -> list[object]:
    """Every value under ``key`` anywhere in the view, looking inside
    JSON-encoded strings too (the projection shape is the implementer's)."""
    found: list[object] = []
    if isinstance(value, str) and value[:1] in "{[":
        try:
            value = json.loads(value)
        except ValueError:
            return found
    if isinstance(value, dict):
        for name, inner in value.items():
            if name == key:
                found.append(inner)
            found += _values(inner, key)
    elif isinstance(value, list):
        for inner in value:
            found += _values(inner, key)
    return found


def _ok(tools, tool: str, params: dict[str, object]):
    outcome = tools.execute(_call(tool, params))
    assert outcome.status == "ok", _view(outcome)
    view = _view(outcome)
    assert view.get("source_start_at") is None
    return view


def test_cluster_health_reports_the_unassigned_replica(cluster):
    view = _ok(cluster, HEALTH, {})
    assert "yellow" in _values(view, "status")
    assert any(n >= 1 for n in _values(view, "unassigned_shards"))
    assert any(n >= 1 for n in _values(view, "number_of_nodes"))

    by_index = _ok(cluster, HEALTH, {"index": "logs-app-a", "level": "indices"})
    assert "green" in _values(by_index, "status")
    assert "logs-app-a" in json.dumps(by_index)


def test_node_stats_return_jvm_figures(cluster):
    view = _ok(cluster, NODES, {"metrics": "jvm"})
    heap = _values(view, "heap_used_percent")
    assert heap and all(isinstance(n, int) and 0 <= n <= 100 for n in heap)
    assert _ok(cluster, NODES, {"node_id": "_local"})


def test_index_stats_count_the_seeded_documents(cluster):
    view = _ok(cluster, INDEX_STATS, {"index": "logs-app-a", "metrics": "docs"})
    assert len(LOGS_A) in _values(view, "count")

    missing = cluster.execute(_call(INDEX_STATS, {"index": "no-such-index-808"}))
    assert (missing.status, missing.reason) == ("error", "INVALID_PARAMS")
    assert "INDEX_NOT_FOUND" in json.dumps(_view(missing))


def test_allocation_explain_explains_real_shards(cluster):
    replica = _ok(
        cluster, EXPLAIN, {"index": REPLICA_INDEX, "shard": 0, "primary": False}
    )
    assert "unassigned" in _values(replica, "current_state")
    assert "no" in _values(replica, "can_allocate")

    primary = _ok(
        cluster, EXPLAIN, {"index": "logs-app-a", "shard": 0, "primary": True}
    )
    assert "started" in _values(primary, "current_state")

    first = _ok(cluster, EXPLAIN, {})
    assert "unassigned" in _values(first, "current_state")


def test_cat_shards_and_health(cluster):
    shards = _ok(
        cluster,
        CAT,
        {
            "api": "shards",
            "index": REPLICA_INDEX,
            "columns": "index,shard,prirep,state",
        },
    )
    states = _values(shards, "state")
    assert "STARTED" in states and "UNASSIGNED" in states
    assert set(_values(shards, "prirep")) == {"p", "r"}

    health = _ok(cluster, CAT, {"api": "health"})
    assert "yellow" in _values(health, "status")
