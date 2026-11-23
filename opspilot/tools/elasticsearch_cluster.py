"""Elasticsearch 8 / OpenSearch 2 cluster tools (#808, ruling B4-U7, amends
R8-12).

The five tools of upstream HolmesGPT's ``elasticsearch/cluster`` toolset
(``holmes/plugins/toolsets/elasticsearch/elasticsearch.py`` at ``a0009195``,
``:345-425`` and ``:555-772``), same names and parameters:

* ``elasticsearch_cat`` -- ``GET /_cat/{endpoint}[/{index}]?format=json``
  for upstream's fifteen endpoints only; ``index`` goes into the path for
  shards, indices, segments, recovery and aliases (as upstream), ``columns``
  and ``sort`` become ``h`` and ``s``, ``health`` applies to indices.
* ``elasticsearch_cluster_health`` -- ``GET /_cluster/health[/{index}]``
  with ``level`` cluster, indices or shards.
* ``elasticsearch_index_stats`` -- ``GET /{index}/_stats[/{metrics}]``.
* ``elasticsearch_allocation_explain`` -- ``GET /_cluster/allocation/explain``;
  with ``index`` and ``shard`` the shard is named in a JSON body
  ``{index, shard, primary}`` as upstream does (the one GET with a body the
  client allows, on that path only; Elasticsearch and OpenSearch both read
  it), without them the backend explains the first unassigned shard.
* ``elasticsearch_nodes_stats`` -- ``GET /_nodes/{node_id}/stats[/{metrics}]``.

They ride the B1 gate unchanged: served only by ``otel-demo-ext`` when
``OPSPILOT_ES_URL`` is set, over the same ``es-ro`` credential slot and
address selector, listed under ``data_sources.available["elasticsearch"]``.
Read-only by construction: GET only, fixed paths; every model value is
checked before anything is sent (index names by the B1-3 rule, ``endpoint``,
``level``, ``metrics`` and ``health`` against upstream's listed values,
``node_id`` ``_all``, ``_local`` or ``[A-Za-z0-9._-]``). Answers are the
cluster's state when the query ran and carry no source time.
"""

from __future__ import annotations

import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from opspilot.tools import otel_demo
from opspilot.tools.elasticsearch_logs import (
    _ERROR_CLASSES,
    _INDEX_RULE,
    _TOO_LARGE_TEXT,
    ENDPOINT_SELECTOR,
    HEALTH_VALUES,
    READ_LIMIT_BYTES,
    REQUEST_TIMEOUT_SECONDS,
    ElasticsearchClient,
    _decode,
    _monotonic,
    _refused,
    _source_error,
    check_index,
    index_path,
)
from opspilot.tools.executor import (
    TransportError,
    TransportRequest,
    TransportResponse,
    TransportResultTooLarge,
)
from opspilot.tools.registry import (
    MAX_VIEW_TOKENS,
    ParameterSpec,
    ToolDescription,
    ToolRegistration,
    canonical,
)

__all__ = [
    "ALLOCATION_EXPLAIN_TOOL",
    "CAT_ENDPOINTS",
    "CAT_TOOL",
    "CLUSTER_HEALTH_TOOL",
    "INDEX_STATS_TOOL",
    "NODES_STATS_TOOL",
    "TOOL_NAMES",
    "TOOL_SCHEMAS",
    "ElasticsearchClusterTransport",
    "cluster_registrations",
    "overflow_summary",
]

CAT_TOOL = "elasticsearch_cat"
CLUSTER_HEALTH_TOOL = "elasticsearch_cluster_health"
INDEX_STATS_TOOL = "elasticsearch_index_stats"
ALLOCATION_EXPLAIN_TOOL = "elasticsearch_allocation_explain"
NODES_STATS_TOOL = "elasticsearch_nodes_stats"
#: Upstream's order (``elasticsearch.py:924-930``).
TOOL_NAMES: tuple[str, ...] = (
    CAT_TOOL,
    CLUSTER_HEALTH_TOOL,
    INDEX_STATS_TOOL,
    ALLOCATION_EXPLAIN_TOOL,
    NODES_STATS_TOOL,
)

#: Upstream's ``endpoint`` values (``elasticsearch.py:358-362``).
CAT_ENDPOINTS: tuple[str, ...] = (
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
#: Endpoints whose path takes the index (``elasticsearch.py:403-410``).
CAT_INDEX_ENDPOINTS: tuple[str, ...] = (
    "shards",
    "indices",
    "segments",
    "recovery",
    "aliases",
)
LEVEL_VALUES: tuple[str, ...] = ("cluster", "indices", "shards")
#: ``elasticsearch.py:651-655``.
INDEX_METRICS: tuple[str, ...] = (
    "_all",
    "docs",
    "store",
    "indexing",
    "search",
    "get",
    "merge",
    "refresh",
    "flush",
    "warmer",
    "query_cache",
    "fielddata",
    "completion",
    "segments",
    "translog",
    "recovery",
)
#: ``elasticsearch.py:751-754``.
NODE_METRICS: tuple[str, ...] = (
    "_all",
    "breaker",
    "fs",
    "http",
    "indices",
    "jvm",
    "os",
    "process",
    "thread_pool",
    "transport",
    "discovery",
)
#: Upstream calls it ``endpoint``, a key the gateway reserves (C3 §8,
#: ``registry.RESERVED_PARAMETERS``); same values under another key.
CAT_API_PARAM = "api"
DEFAULT_NODE_ID = "_all"
MAX_COLUMNS_CHARS = 1000
MAX_NODE_ID_CHARS = 200
EXPLAIN_PATH = "/_cluster/allocation/explain"

_COLUMN_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._,:*-"
)
_NODE_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)


# -- input checks ----------------------------------------------------------------


def _given(value: object) -> bool:
    return value is not None and value != ""


def _csv_in(value: object, allowed: Sequence[str]) -> bool:
    if not isinstance(value, str) or not value:
        return False
    return all(part in allowed for part in value.split(","))


def _columns_ok(value: object) -> bool:
    """``h``/``s`` of a _cat call: column names (``docs.count``), commas and
    ``:asc``/``:desc``; no character that could end the value in a URL."""
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_COLUMNS_CHARS
        and set(value) <= _COLUMN_CHARS
    )


def _node_id_ok(value: object) -> bool:
    if value in ("_all", "_local"):
        return True
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_NODE_ID_CHARS
        and set(value) <= _NODE_ID_CHARS
        and not value.startswith("_")
        and ".." not in value
    )


def _single_index(value: object) -> bool:
    """One concrete index name (allocation explain names one shard)."""
    return (
        check_index(value)
        and isinstance(value, str)
        and value != "_all"
        and not value.startswith("-")
        and "*" not in value
        and "," not in value
    )


def _one_of(name: str, allowed: Sequence[str], *, many: bool = False) -> str:
    return (
        f"{name} must be "
        + ("a comma-separated list of " if many else "one of ")
        + ", ".join(allowed)
        + "."
    )


# -- transport -------------------------------------------------------------------


class ElasticsearchClusterTransport:
    """Serves the five cluster tools over one ``ElasticsearchClient``
    (``es-ro``, the B1 slot). Every refusal of the model's input is decided
    before anything is sent."""

    def __init__(
        self,
        *,
        credentials: Mapping[str, str | None],
        opener: urllib.request.OpenerDirector | None = None,
        ca_file: str | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._client = ElasticsearchClient(
            credentials=credentials, opener=opener, ca_file=ca_file
        )
        self._monotonic = monotonic or _monotonic

    def fetch(self, request: TransportRequest) -> TransportResponse:
        if request.tool not in TOOL_NAMES:
            raise TransportError("TOOL_NOT_SUPPORTED")
        planned = _plan(request.tool, request.params)
        if isinstance(planned, TransportResponse):
            return planned
        path, query, body, index, notes = planned
        deadline = self._monotonic() + request.timeout_seconds
        self._client.authorization()  # a missing slot fails before sending
        endpoint = request.selector.get(ENDPOINT_SELECTOR, "")
        if not endpoint:
            raise TransportError("ES_ENDPOINT_MISSING")
        fetched = self._client.get(
            endpoint.rstrip("/"),
            path,
            query,
            body,
            deadline=deadline,
            max_bytes=READ_LIMIT_BYTES,
        )
        if fetched.status != 200:
            return _source_error(fetched, index or "")
        payload = _decode(fetched.body)
        if request.tool == CAT_TOOL:
            if not isinstance(payload, list) or not all(
                isinstance(item, dict) for item in payload
            ):
                return TransportResponse(body=b"")
            rows: list[Any] = payload
            unit = "rows"
        else:
            if not isinstance(payload, dict):
                return TransportResponse(body=b"")
            rows = [payload]
            unit = "results"
        out = canonical({"results": rows}).encode("utf-8")
        if len(out) > request.max_result_bytes:
            raise TransportResultTooLarge(str(request.max_result_bytes))
        return TransportResponse(
            body=out,
            row_unit=unit,
            backend_rows_returned=len(rows),
            view_fields={"notes": [*notes, _STATE_NOTE], "cannot_prove": _CANNOT_PROVE},
        )


_Plan = tuple[str, list[tuple[str, str]], dict[str, Any] | None, str | None, list[str]]


def _plan(tool: str, params: Mapping[str, Any]) -> _Plan | TransportResponse:
    """The fixed path, query and body of one call, or the refusal."""
    if tool == CAT_TOOL:
        return _plan_cat(params)
    if tool == CLUSTER_HEALTH_TOOL:
        index = params.get("index")
        if _given(index) and not check_index(index):
            return _refused("INVALID_INDEX", _INDEX_RULE)
        level = params.get("level")
        if _given(level) and level not in LEVEL_VALUES:
            return _refused("INVALID_LEVEL", _one_of("level", LEVEL_VALUES))
        path = "/_cluster/health"
        if _given(index):
            path += "/" + index_path(str(index))
        query = [("level", str(level))] if _given(level) else []
        return path, query, None, str(index) if _given(index) else None, []
    if tool == INDEX_STATS_TOOL:
        index = params.get("index")
        if not check_index(index):
            return _refused("INVALID_INDEX", _INDEX_RULE)
        assert isinstance(index, str)
        metrics = params.get("metrics")
        if _given(metrics) and not _csv_in(metrics, INDEX_METRICS):
            return _refused(
                "INVALID_METRICS", _one_of("metrics", INDEX_METRICS, many=True)
            )
        path = f"/{index_path(index)}/_stats"
        if _given(metrics):
            path += f"/{metrics}"
        return path, [], None, index, []
    if tool == ALLOCATION_EXPLAIN_TOOL:
        return _plan_explain(params)
    node_id = params.get("node_id")
    if not _given(node_id):
        node_id = DEFAULT_NODE_ID
    if not _node_id_ok(node_id):
        return _refused(
            "INVALID_NODE_ID",
            "node_id must be _all, _local, or one node id or name of letters, "
            "digits and . _ - (not starting with _, no ..).",
        )
    metrics = params.get("metrics")
    if _given(metrics) and not _csv_in(metrics, NODE_METRICS):
        return _refused("INVALID_METRICS", _one_of("metrics", NODE_METRICS, many=True))
    path = f"/_nodes/{node_id}/stats"
    if _given(metrics):
        path += f"/{metrics}"
    return path, [], None, None, []


def _plan_cat(params: Mapping[str, Any]) -> _Plan | TransportResponse:
    endpoint = params.get(CAT_API_PARAM)
    if endpoint not in CAT_ENDPOINTS:
        return _refused("INVALID_ENDPOINT", _one_of(CAT_API_PARAM, CAT_ENDPOINTS))
    assert isinstance(endpoint, str)
    index = params.get("index")
    if _given(index) and not check_index(index):
        return _refused("INVALID_INDEX", _INDEX_RULE)
    columns = params.get("columns")
    if _given(columns) and not _columns_ok(columns):
        return _refused(
            "INVALID_COLUMNS",
            "columns must be comma-separated column names of letters, digits "
            "and . _ * - (for example index,shard,prirep,state).",
        )
    sort = params.get("sort")
    if _given(sort) and not _columns_ok(sort):
        return _refused(
            "INVALID_SORT",
            "sort must be comma-separated column names, each optionally "
            ":asc or :desc (for example docs.count:desc,index).",
        )
    health = params.get("health")
    if _given(health) and health not in HEALTH_VALUES:
        return _refused("INVALID_HEALTH", _one_of("health", HEALTH_VALUES))
    notes: list[str] = []
    path = f"/_cat/{endpoint}"
    if _given(index):
        if endpoint in CAT_INDEX_ENDPOINTS:
            path += "/" + index_path(str(index))
        else:
            notes.append(
                f"index was ignored: the {endpoint} endpoint is not filtered by "
                "index (only " + ", ".join(CAT_INDEX_ENDPOINTS) + " are)."
            )
    query: list[tuple[str, str]] = [("format", "json")]
    if _given(columns):
        query.append(("h", str(columns)))
    if _given(sort):
        query.append(("s", str(sort)))
    if _given(health):
        if endpoint == "indices":
            query.append(("health", str(health)))
        else:
            notes.append("health was ignored: it filters the indices endpoint only.")
    chosen = str(index) if _given(index) and endpoint in CAT_INDEX_ENDPOINTS else None
    return path, query, None, chosen, notes


def _plan_explain(params: Mapping[str, Any]) -> _Plan | TransportResponse:
    index = params.get("index")
    shard = params.get("shard")
    primary = params.get("primary")
    if _given(index) and not _single_index(index):
        return _refused(
            "INVALID_INDEX",
            "index must be one existing index name (no wildcard, comma or "
            "_all); " + _INDEX_RULE,
        )
    if shard is not None and (
        type(shard) is not int or shard < 0  # bool is not a shard number
    ):
        return _refused("INVALID_SHARD", "shard must be an integer 0 or more.")
    if primary is not None and type(primary) is not bool:
        return _refused("INVALID_PRIMARY", "primary must be true or false.")
    named = _given(index), shard is not None
    if named == (False, False):
        if primary is not None:
            return _refused(
                "INVALID_SHARD",
                "primary needs index and shard; omit all three to explain the "
                "first unassigned shard.",
            )
        return EXPLAIN_PATH, [], None, None, []
    if named != (True, True):
        return _refused(
            "INVALID_SHARD",
            "give index and shard together, or neither to explain the first "
            "unassigned shard.",
        )
    body = {
        "index": index,
        "shard": shard,
        "primary": True if primary is None else primary,
    }
    return EXPLAIN_PATH, [], body, index, []


def overflow_summary(
    tool: str, rows: Sequence[object], query: Mapping[str, object]
) -> dict[str, object]:
    """The over-limit refusal summary (#308 U5) with what to narrow."""
    suggestion = {
        CAT_TOOL: "Filter with index, choose fewer columns.",
        CLUSTER_HEALTH_TOOL: "Use a lower level or one index.",
        INDEX_STATS_TOOL: "Narrow the index pattern or choose fewer metrics.",
        ALLOCATION_EXPLAIN_TOOL: "Name one index and shard.",
        NODES_STATS_TOOL: "Choose fewer metrics or one node_id.",
    }[tool]
    return {
        "row_unit": "rows" if tool == CAT_TOOL else "results",
        "suggestion": suggestion,
    }


# -- model-visible face and registrations ---------------------------------------

_STATE_NOTE = (
    "This tool has no time range: it shows the cluster's state at the moment "
    "of the query, which can be after the authorized window."
)
_CANNOT_PROVE = (
    "This is the state at the moment of the query only: it cannot prove what "
    "the cluster health, shard allocation, or node and index statistics were "
    "during the incident window. Statistics counters are cumulative since a "
    "node started or an index was created, not counts inside the window."
)
_ERRORS_TEXT = (
    "A missing index is INVALID_PARAMS with INDEX_NOT_FOUND; a request the "
    "backend rejects is INVALID_PARAMS with its error type and reason; values "
    "outside the listed ones are INVALID_PARAMS before sending; HTTP 401/403 "
    "are SOURCE_FORBIDDEN; 408, 429 and 5xx are SOURCE_UNAVAILABLE; a call "
    "past its 30 s deadline is a timeout."
)
_INDEX_TEXT = (
    "Index name or pattern, wildcards allowed (for example logs-*); "
    "comma-separated for several; a leading - excludes a pattern."
)
_CURRENT = " There is no time range: this is the cluster when the query ran. "

_CAT_DESCRIPTION = (
    "Query Elasticsearch/OpenSearch _cat APIs for cluster information "
    "(indices, shards, nodes, health, allocation, recovery, segments, "
    "aliases and more). Always use index when querying shards, segments or "
    "recovery. content is one object per row with the chosen columns (values "
    "are strings)." + _CURRENT + _TOO_LARGE_TEXT + " " + _ERRORS_TEXT
)
_HEALTH_DESCRIPTION = (
    "Get cluster health: status (green/yellow/red), node count, shard counts "
    "(active, relocating, initializing, unassigned) and pending tasks, "
    "optionally per index or per shard."
    + _CURRENT
    + _TOO_LARGE_TEXT
    + " "
    + _ERRORS_TEXT
)
_INDEX_STATS_DESCRIPTION = (
    "Get statistics for indices: document count, store size, indexing, "
    "search, merge and other counters (cumulative, not per time range)."
    + _CURRENT
    + _TOO_LARGE_TEXT
    + " "
    + _ERRORS_TEXT
)
_EXPLAIN_DESCRIPTION = (
    "Explain why a shard is unassigned or where it is allocated and why. "
    "Call without parameters to explain the first unassigned shard (an error "
    "when there is none), or give index and shard to explain a specific "
    "shard." + _CURRENT + _TOO_LARGE_TEXT + " " + _ERRORS_TEXT
)
_NODES_DESCRIPTION = (
    "Get statistics for cluster nodes: JVM, OS, process, thread pool, "
    "filesystem, breakers, transport and HTTP."
    + _CURRENT
    + _TOO_LARGE_TEXT
    + " "
    + _ERRORS_TEXT
)


def _schema(
    name: str, description: str, properties: dict[str, Any], required: list[str]
) -> Mapping[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


TOOL_SCHEMAS: tuple[Mapping[str, Any], ...] = (
    _schema(
        CAT_TOOL,
        _CAT_DESCRIPTION,
        {
            CAT_API_PARAM: {
                "type": "string",
                "description": "The _cat API to query, one of: "
                + ", ".join(CAT_ENDPOINTS)
                + ".",
            },
            "index": {
                "type": "string",
                "description": (
                    "Optional index name or pattern (for example logs-*); "
                    "filters shards, indices, segments, recovery and aliases, "
                    "ignored for the other endpoints. Use it for shards, "
                    "segments and recovery to avoid every index's rows."
                ),
            },
            "columns": {
                "type": "string",
                "description": (
                    "Optional comma-separated columns to return (for example "
                    "index,shard,prirep,state,docs); run without columns first "
                    "to see the available ones."
                ),
            },
            "sort": {
                "type": "string",
                "description": (
                    "Optional comma-separated columns to sort by, each with "
                    "optional :asc or :desc (for example docs:desc,index)."
                ),
            },
            "health": {
                "type": "string",
                "description": (
                    "Optional index health filter for the indices endpoint: "
                    "green, yellow or red."
                ),
            },
        },
        [CAT_API_PARAM],
    ),
    _schema(
        CLUSTER_HEALTH_TOOL,
        _HEALTH_DESCRIPTION,
        {
            "index": {
                "type": "string",
                "description": "Optional index name or pattern to get health for.",
            },
            "level": {
                "type": "string",
                "description": (
                    "Optional level of detail: cluster (default), indices or "
                    "shards; higher levels return more data."
                ),
            },
        },
        [],
    ),
    _schema(
        INDEX_STATS_TOOL,
        _INDEX_STATS_DESCRIPTION,
        {
            "index": {
                "type": "string",
                "description": _INDEX_TEXT + " Use _all for all indices.",
            },
            "metrics": {
                "type": "string",
                "description": "Optional comma-separated metrics from: "
                + ", ".join(INDEX_METRICS)
                + ".",
            },
        },
        ["index"],
    ),
    _schema(
        ALLOCATION_EXPLAIN_TOOL,
        _EXPLAIN_DESCRIPTION,
        {
            "index": {
                "type": "string",
                "description": (
                    "Optional index name of the shard to explain (one name, "
                    "no wildcard); give it with shard."
                ),
            },
            "shard": {
                "type": "integer",
                "description": "Optional shard number (0-based); give it with index.",
            },
            "primary": {
                "type": "boolean",
                "description": (
                    "Optional: true for the primary shard, false for a replica "
                    "(default true)."
                ),
            },
        },
        [],
    ),
    _schema(
        NODES_STATS_TOOL,
        _NODES_DESCRIPTION,
        {
            "node_id": {
                "type": "string",
                "description": (
                    "Optional node id or name; _local for the node answering, "
                    "_all (default) for all nodes."
                ),
            },
            "metrics": {
                "type": "string",
                "description": "Optional comma-separated metrics from: "
                + ", ".join(NODE_METRICS)
                + ".",
            },
        },
        [],
    ),
)

_CLUSTER_ERROR_CLASSES: dict[str, str] = {
    **_ERROR_CLASSES,
    "INVALID_ENDPOINT": "INVALID_PARAMS",
    "INVALID_LEVEL": "INVALID_PARAMS",
    "INVALID_METRICS": "INVALID_PARAMS",
    "INVALID_NODE_ID": "INVALID_PARAMS",
    "INVALID_SHARD": "INVALID_PARAMS",
    "INVALID_PRIMARY": "INVALID_PARAMS",
}

_LIMITS = (
    "One call reads at most 2 MiB from the backend and returns at most 2 MiB "
    "(RESULT_TOO_LARGE otherwise, whole call); the view is limited to 25,000 "
    "tokens and refused whole above it, never truncated. 30 s per call. " + _ERRORS_TEXT
)
_STATE_WINDOW = (
    "The authorized absolute window is appended as {window}. There is no "
    "time range: the answer is the cluster's state when the query ran and "
    "carries no source time."
)


def cluster_registrations(*, max_window_seconds: int) -> tuple[ToolRegistration, ...]:
    """The five registrations, like B1-9's state tools: ``source`` the lab
    target's, ``verb="get"``, ``may_contain_secrets=True``."""
    common: dict[str, Any] = {
        "version": "v1",
        "verb": "get",
        "source": otel_demo.SOURCE,
        "may_contain_secrets": True,
        "request_timeout_seconds": REQUEST_TIMEOUT_SECONDS,
        "max_result_bytes": READ_LIMIT_BYTES,
        "max_view_tokens": MAX_VIEW_TOKENS,
        "max_window_seconds": max_window_seconds,
        "error_classes": dict(_CLUSTER_ERROR_CLASSES),
        "result_path": ("results",),
    }

    def description(returns: str, values: str) -> ToolDescription:
        return ToolDescription(
            returns=returns,
            window_format=_STATE_WINDOW,
            values_format="The authorized target is appended as {values}. " + values,
            limits=_LIMITS,
            cannot_prove=_CANNOT_PROVE,
        )

    return (
        ToolRegistration(
            name=CAT_TOOL,
            description=description(
                "The rows of one _cat endpoint as JSON objects with the chosen "
                "columns (string values) under results.",
                "endpoint: " + ", ".join(CAT_ENDPOINTS) + "; index follows the "
                "index name rule; columns and sort are column names; health: "
                "green, yellow, red; other values are INVALID_PARAMS.",
            ),
            parameters={
                CAT_API_PARAM: ParameterSpec(
                    "string", required=True, description="The _cat API."
                ),
                "index": ParameterSpec("string", description="Index filter."),
                "columns": ParameterSpec("string", description="Columns to return."),
                "sort": ParameterSpec("string", description="Sort columns."),
                "health": ParameterSpec("string", description="Health filter."),
            },
            **common,
        ),
        ToolRegistration(
            name=CLUSTER_HEALTH_TOOL,
            description=description(
                "The cluster health answer (status, nodes, shard counts, "
                "pending tasks; per index or shard at higher levels) as one "
                "object under results.",
                "index follows the index name rule; level: cluster, indices, shards.",
            ),
            parameters={
                "index": ParameterSpec("string", description="Index or pattern."),
                "level": ParameterSpec("string", description="Level of detail."),
            },
            **common,
        ),
        ToolRegistration(
            name=INDEX_STATS_TOOL,
            description=description(
                "The index statistics answer (_shards, _all and per index "
                "primaries and total) as one object under results.",
                "index follows the index name rule; metrics from: "
                + ", ".join(INDEX_METRICS)
                + ".",
            ),
            parameters={
                "index": ParameterSpec(
                    "string", required=True, description="Index or pattern."
                ),
                "metrics": ParameterSpec("string", description="Metrics."),
            },
            **common,
        ),
        ToolRegistration(
            name=ALLOCATION_EXPLAIN_TOOL,
            description=description(
                "The allocation explanation of one shard (current state, "
                "whether and why it can be allocated, per-node decisions) as "
                "one object under results.",
                "index is one index name; shard an integer 0 or more; index "
                "and shard together or neither.",
            ),
            parameters={
                "index": ParameterSpec("string", description="Index name."),
                "shard": ParameterSpec("integer", description="Shard number."),
                "primary": ParameterSpec("boolean", description="Primary shard."),
            },
            **common,
        ),
        ToolRegistration(
            name=NODES_STATS_TOOL,
            description=description(
                "The node statistics answer (_nodes, cluster_name, nodes by "
                "id) as one object under results.",
                "node_id: _all, _local or one node id or name; metrics from: "
                + ", ".join(NODE_METRICS)
                + ".",
            ),
            parameters={
                "node_id": ParameterSpec("string", description="Node id or name."),
                "metrics": ParameterSpec("string", description="Metrics."),
            },
            **common,
        ),
    )
