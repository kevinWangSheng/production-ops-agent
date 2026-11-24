"""The extended OTel Demo tool profile ``otel-demo-ext`` (tool expansion A2-2).

Every ``otel-demo`` tool, byte for byte (``opspilot.tools.otel_demo`` is not
changed), plus the cluster object queries of
:mod:`opspilot.tools.kubernetes_core` and the Prometheus instant query, series
and label-name tools of :mod:`opspilot.tools.prometheus_ext` (A5) with its
rules, scrape-target and metric-metadata tools (B2). Later tool-expansion
items join this profile, never ``otel-demo``.

Where configuration is read (A2-3): the face and the versions are built from
template text only, so the web -- which selects the profile to record a
Run's tools and versions -- needs no ``OPSPILOT_K8S_*`` variable. The worker
reads them when it builds the executor factory
(``otel_demo_ext_executor_factory``) and refuses to start without them
(``K8S_CONFIG_MISSING`` / ``K8S_CONFIG_INVALID``) or without ``jq``
(``JQ_UNAVAILABLE``).

One target, three backends (A2-1): the Kubernetes tools are registered under
the existing lab target with ``source="otel-demo"``; the API server address
travels in the target selector (``kubernetes_endpoint``) and the token in the
Kubernetes transport's own credential slot ``k8s-ro``. Tools are routed to
their transport by exact name (``RoutedTransport``); the Jaeger trace tools
of :mod:`opspilot.tools.jaeger_query` (A4) go to its ``JaegerQueryTransport``
over the same ``traces_endpoint`` and credential slot as ``traces_search``;
``fetch_pod_logs`` (A3, :mod:`opspilot.tools.kubernetes_logs`) goes by name
to the same ``KubernetesTransport`` (Kubernetes address, ``k8s-ro`` slot).

Configured backends (B1-U2, #560): the three Elasticsearch/OpenSearch tools
of :mod:`opspilot.tools.elasticsearch_logs`, then the five cluster tools of
:mod:`opspilot.tools.elasticsearch_cluster` (#808), join this profile only when
``OPSPILOT_ES_URL`` is set -- then ``data_sources.available`` lists
``elasticsearch`` and ``tool_schema_revision`` is ``ES_TOOL_SCHEMA_REVISION``.
Without it every name, face, version and registration here is byte for byte
what it was before B1. The web passes only whether the address is set; the
worker also passes the ``es-ro`` connection, routed by tool name to its own
``ElasticsearchTransport`` (address in the selector
``elasticsearch_endpoint``).

Configured-backend set (C5-14, #569): the same rule for Loki -- the
``grafana_loki_query`` tool of :mod:`opspilot.tools.loki_logs` joins only when
``OPSPILOT_LOKI_URL`` is set, listed under ``loki`` in
``data_sources.available``. The tools array, source list and registry are
computed from the set of configured backends in a fixed order
(Elasticsearch, then Loki): the empty set gives ``TOOL_SCHEMA_REVISION`` and
the Elasticsearch-only set ``ES_TOOL_SCHEMA_REVISION``, byte for byte what
they were before C5. The worker routes the tool to its own ``LokiTransport``
(``loki-ro`` slot, address in the selector ``loki_endpoint``).

C5b (#569): the five ``victorialogs_*`` tools of
:mod:`opspilot.tools.victorialogs_logs` join the same way when
``OPSPILOT_VICTORIALOGS_URL`` is set, after Loki in the fixed order and under
``victorialogs`` in ``data_sources.available``; every set without
VictoriaLogs is byte for byte what it was before C5b. The worker routes them
to their own ``VictoriaLogsTransport`` (``vlogs-ro`` slot, address in the
selector ``victorialogs_endpoint``).

B3 (#562): ``kubectl_top_pods`` and ``kubectl_top_nodes`` of
:mod:`opspilot.tools.kubernetes_live_metrics` join last in the fixed order
when ``OPSPILOT_K8S_LIVE_METRICS=1`` (B3-1), listed under ``kubernetes`` in
``data_sources.available``; every set without the switch is byte for byte
what it was before B3. The worker routes them to the same
``KubernetesTransport`` (``k8s-ro`` slot, ``kubernetes_endpoint``).

C6a (#570): the three ``database_sql_*`` tools of
:mod:`opspilot.tools.sql_query` join last when ``OPSPILOT_SQL_URL`` is set,
listed under ``sql`` in ``data_sources.available``; the dialect
(``postgresql`` or ``mysql``) is in the query tool's description, so each
dialect has its own revision, and every set without an SQL address is byte
for byte what it was before C6a. The worker routes them to their own
``SqlTransport`` (``sql-ro`` slot, address in the selector ``sql_endpoint``).

C3 (#567): the eight Kafka tools of :mod:`opspilot.tools.kafka_admin` join
last, after the SQL tools, when ``OPSPILOT_KAFKA_BOOTSTRAP_SERVERS`` is set,
listed under ``kafka`` in ``data_sources.available``; every set without the
address is byte for byte what it was before C3. The worker routes them to
their own ``KafkaTransport`` (``kafka-ro`` slot, address in the selector
``kafka_bootstrap``).

C4 (#568): the four ``argocd_app_*`` tools of :mod:`opspilot.tools.argocd_apps`
join right after the Helm tools when ``OPSPILOT_ARGOCD=1``, listed under
``change_history`` with them; every set without the switch is byte for byte
what it was before C4. The worker routes them to the same
``KubernetesTransport`` (``k8s-ro`` slot).

C6c (#570): the six ``mongodb_*`` tools of :mod:`opspilot.tools.mongodb_query`
join last when ``OPSPILOT_MONGODB_URL`` is set, listed under ``mongodb`` in
``data_sources.available``; every set without the address is byte for byte
what it was before C6c. The worker routes them to their own ``MongoTransport``
(``mongodb-ro`` slot, address in the selector ``mongodb_endpoint``).

#773: the six ``redis_*`` tools of :mod:`opspilot.tools.redis_inspect` join
last, after the MongoDB tools, when ``OPSPILOT_REDIS_URL`` is set, listed under
``redis`` in ``data_sources.available``; every set without the address is byte
for byte what it was before #773. The worker routes them to their own
``RedisTransport`` (``redis-ro`` slot, address in the selector
``redis_endpoint``).

B4 (#563): ``helm_list``, ``helm_history`` and ``helm_status`` of
:mod:`opspilot.tools.helm_releases` join when ``OPSPILOT_HELM_RELEASES=1``,
after the live usage tools and before ``fetch_skill`` and ``TodoWrite`` (the
SQL and Kafka tools stay after those two), listed under ``change_history``
in ``data_sources.available``, which then leaves ``not_integrated``; every
set without the switch is byte for byte what it was before B4. The worker
routes them to the same ``KubernetesTransport`` (``k8s-ro`` slot), built
with the Helm switch so the Secret gateway passes Helm release pages.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import timedelta, timezone
from typing import Any, cast

from opspilot.instructions.discipline import todo_variant
from opspilot.investigation import todo as todo_write
from opspilot.investigation.context import InvestigationInput, catalog_max_age
from opspilot.investigation.inputs import ToolFace
from opspilot.investigation.loop import investigation_versions
from opspilot.investigation.reports import DATA_SOURCES_INSTRUCTION
from opspilot.investigation.runner import ExecutorFactory
from opspilot.persistence import DurableStore, Lease
from opspilot.tools import (
    argocd_apps,
    elasticsearch_cluster,
    elasticsearch_logs,
    helm_releases,
    jaeger_query,
    kafka_admin,
    knowledge_fetch,
    kubernetes_core,
    kubernetes_live_metrics,
    kubernetes_logs,
    kubernetes_tabular,
    loki_logs,
    mongodb_query,
    otel_demo,
    prometheus_ext,
    redis_inspect,
    sql_query,
    victorialogs_logs,
)
from opspilot.tools.evidence_read import (
    EvidenceReader,
    EvidenceReadTransport,
    RoutedTransport,
)
from opspilot.tools.executor import (
    Clock,
    EvidenceSink,
    QueryScope,
    ReadOnlyToolExecutor,
)
from opspilot.tools.ledger import DurableToolLedger
from opspilot.tools.outcomes import PROJECTION_REVISION, Window
from opspilot.tools.registry import (
    RegisteredTarget,
    TargetRegistry,
    ToolContractError,
    ToolRegistration,
    ToolRegistry,
    canonical_hash,
)
from opspilot.tools.tokens import TokenCounter, default_counter
from opspilot.tracing import _is_lab_host

__all__ = [
    "ES_TOOL_NAMES",
    "ES_TOOL_SCHEMAS",
    "ES_TOOL_SCHEMA_REVISION",
    "ARGOCD_TOOL_NAMES",
    "ARGOCD_TOOL_SCHEMA_REVISION",
    "HELM_TOOL_NAMES",
    "HELM_TOOL_SCHEMA_REVISION",
    "LIVE_METRICS_TOOL_NAMES",
    "LIVE_METRICS_TOOL_SCHEMA_REVISION",
    "LOKI_TOOL_NAMES",
    "LOKI_TOOL_SCHEMA_REVISION",
    "PROFILE_NAME",
    "TOOL_NAMES",
    "TOOL_SCHEMAS",
    "KAFKA_TOOL_NAMES",
    "KAFKA_TOOL_SCHEMA_REVISION",
    "MONGODB_TOOL_NAMES",
    "MONGODB_TOOL_SCHEMA_REVISION",
    "REDIS_TOOL_NAMES",
    "REDIS_TOOL_SCHEMA_REVISION",
    "SQL_CLICKHOUSE_TOOL_SCHEMA_REVISION",
    "SQL_MSSQL_TOOL_SCHEMA_REVISION",
    "SQL_MYSQL_TOOL_SCHEMA_REVISION",
    "SQL_POSTGRESQL_TOOL_SCHEMA_REVISION",
    "SQL_TOOL_NAMES",
    "VICTORIALOGS_TOOL_NAMES",
    "VICTORIALOGS_TOOL_SCHEMA_REVISION",
    "TOOL_SCHEMA_REVISION",
    "otel_demo_ext_executor_factory",
    "otel_demo_ext_face",
    "otel_demo_ext_registrations",
    "otel_demo_ext_versions",
    "tool_schema_revision",
]

PROFILE_NAME = "otel-demo-ext"

#: A7 (#559, r7 A7-1/A7-6): this profile's L1a variant -- ``replay-candidate``
#: plus the Kubernetes investigation guidance. ``otel-demo`` keeps
#: ``replay-candidate``; the revision follows the content hash.
DISCIPLINE_VARIANT = "replay-candidate-k8s"
#: #722 (r10 H-U1): the same plus the multi-cluster paragraph, used when the
#: operator set the local cluster name (``OPSPILOT_CLUSTER_NAME``). Only
#: whether a name is set picks it; the name itself is an L1b value recorded
#: in the Run input and never reaches ``prompt_revision``.
CLUSTER_DISCIPLINE_VARIANT = "replay-candidate-k8s-cluster"


def _variant(todo: bool, cluster_name: str | None = None) -> str:
    """#722: the cluster variant when a name is set; C1: then the ``-todo``
    suffix when the task list is on."""
    base = CLUSTER_DISCIPLINE_VARIANT if cluster_name else DISCIPLINE_VARIANT
    return todo_variant(base) if todo else base


#: ``otel-demo``'s tools array unchanged, then the new tools.
TOOL_SCHEMAS: tuple[Mapping[str, Any], ...] = (
    *otel_demo.TOOL_SCHEMAS,
    *jaeger_query.TOOL_SCHEMAS,
    *kubernetes_core.TOOL_SCHEMAS,
    *prometheus_ext.TOOL_SCHEMAS,
    *kubernetes_logs.TOOL_SCHEMAS,
    *kubernetes_tabular.TOOL_SCHEMAS,
)
#: B1-U2: the tools array with ``OPSPILOT_ES_URL`` set -- the same array, then
#: the three Elasticsearch tools and the five cluster tools (#808).
ES_TOOL_SCHEMAS: tuple[Mapping[str, Any], ...] = (
    *TOOL_SCHEMAS,
    *elasticsearch_logs.TOOL_SCHEMAS,
    *elasticsearch_cluster.TOOL_SCHEMAS,
)


def _tool_schemas(
    elasticsearch: bool = False,
    loki: bool = False,
    victorialogs: bool = False,
    live_metrics: bool = False,
    knowledge: bool = False,
    todo: bool = False,
    sql: str | None = None,
    kafka: bool = False,
    mongodb: bool = False,
    redis: bool = False,
    helm: bool = False,
    argocd: bool = False,
) -> tuple[Mapping[str, Any], ...]:
    """C5-14: the base array, then each configured backend's tools in the
    fixed order Elasticsearch, Loki, VictoriaLogs, live usage (B3-1), the
    knowledge tool (C2b) and the task list (C1)."""
    schemas = ES_TOOL_SCHEMAS if elasticsearch else TOOL_SCHEMAS
    if loki:
        schemas = (*schemas, *loki_logs.TOOL_SCHEMAS)
    if victorialogs:
        schemas = (*schemas, *victorialogs_logs.TOOL_SCHEMAS)
    if live_metrics:
        schemas = (*schemas, *kubernetes_live_metrics.TOOL_SCHEMAS)
    if helm:
        # B4-2 (#563): after the backends that come before the knowledge
        # tool and the task list; the SQL and Kafka tools, added after
        # those two, stay after them so their revisions do not move.
        schemas = (*schemas, *helm_releases.TOOL_SCHEMAS)
    if argocd:
        # C4 (#568): right after the Helm tools, same reason.
        schemas = (*schemas, *argocd_apps.TOOL_SCHEMAS)
    if knowledge:
        # C2b (#566): last in the fixed order
        schemas = (*schemas, *knowledge_fetch.TOOL_SCHEMAS)
    if todo:
        # C1 (#565): last; answered by the loop, never registered here.
        schemas = (*schemas, todo_write.TODO_WRITE_SCHEMA)
    if sql is not None:
        # C6-13 (#570): after every earlier item; the dialect is in the
        # query tool's description.
        schemas = (*schemas, *sql_query.tool_schemas(sql))
    if kafka:
        # C3-12 (#567): after every earlier item.
        schemas = (*schemas, *kafka_admin.TOOL_SCHEMAS)
    if mongodb:
        # C6c (#570): after every earlier item.
        schemas = (*schemas, *mongodb_query.TOOL_SCHEMAS)
    if redis:
        # #773: after every earlier item.
        schemas = (*schemas, *redis_inspect.TOOL_SCHEMAS)
    return schemas


#: What this profile can and cannot query (#311 U5); ``kubernetes_state``
#: moves from not integrated to available (A2-2), and ``pod_logs`` with it
#: (A3-11).
_NOT_INTEGRATED: tuple[str, ...] = tuple(
    name
    for name in otel_demo.NOT_INTEGRATED
    if name not in {"kubernetes_state", "pod_logs"}
)


def _data_sources(
    elasticsearch: bool = False,
    loki: bool = False,
    victorialogs: bool = False,
    live_metrics: bool = False,
    knowledge: bool = False,
    sql: str | None = None,
    kafka: bool = False,
    mongodb: bool = False,
    redis: bool = False,
    helm: bool = False,
    argocd: bool = False,
) -> dict[str, Any]:
    available: dict[str, Any] = {
        "prometheus": sorted(
            otel_demo.PROMETHEUS_TOOLS | prometheus_ext.PROMETHEUS_EXT_TOOLS
        ),
        "jaeger": [otel_demo.TRACES_TOOL, *jaeger_query.TOOL_NAMES],
        "kubernetes": sorted(
            kubernetes_core.K8S_TOOLS
            | set(kubernetes_logs.TOOL_NAMES)
            | set(kubernetes_tabular.TOOL_NAMES)
            | (set(kubernetes_live_metrics.TOOL_NAMES) if live_metrics else set())
        ),
    }
    if elasticsearch:
        available["elasticsearch"] = sorted(
            (*elasticsearch_logs.TOOL_NAMES, *elasticsearch_cluster.TOOL_NAMES)
        )
    if loki:
        available["loki"] = sorted(loki_logs.TOOL_NAMES)
    if victorialogs:
        available["victorialogs"] = sorted(victorialogs_logs.TOOL_NAMES)
    if helm or argocd:
        # B4-14: change history moves from not integrated to available; C4
        # adds the ArgoCD tools under the same key.
        available["change_history"] = sorted(
            (set(helm_releases.TOOL_NAMES) if helm else set())
            | (set(argocd_apps.TOOL_NAMES) if argocd else set())
        )
    if knowledge:
        available["knowledge"] = list(knowledge_fetch.TOOL_NAMES)
    if sql is not None:
        available["sql"] = sorted(sql_query.TOOL_NAMES)
    if kafka:
        available["kafka"] = sorted(kafka_admin.TOOL_NAMES)
    if mongodb:
        available["mongodb"] = sorted(mongodb_query.TOOL_NAMES)
    if redis:
        available["redis"] = sorted(redis_inspect.TOOL_NAMES)
    return {
        "available": available,
        "not_integrated": [
            name
            for name in _NOT_INTEGRATED
            if not ((helm or argocd) and name == "change_history")
        ],
        "instruction": DATA_SOURCES_INSTRUCTION,
    }


def _registrations(
    elasticsearch: bool = False,
    loki: bool = False,
    victorialogs: bool = False,
    live_metrics: bool = False,
    knowledge: bool = False,
    sql: str | None = None,
    kafka: bool = False,
    mongodb: bool = False,
    redis: bool = False,
    helm: bool = False,
    argocd: bool = False,
    window_seconds: int = otel_demo.OBSERVATION_SECONDS,
) -> tuple[ToolRegistration, ...]:
    base = _registered(
        elasticsearch,
        loki,
        victorialogs,
        live_metrics,
        knowledge,
        sql=sql,
        kafka=kafka,
        mongodb=mongodb,
        redis=redis,
        helm=helm,
        argocd=argocd,
    )
    if window_seconds == otel_demo.OBSERVATION_SECONDS:
        return base
    # #779 B4-2: the loop sends the Run's whole frame as every call's
    # top-level window, so each tool's ceiling is raised to the deployment's
    # longest frame; a shorter Run is still bounded by its own frame.
    return tuple(
        replace(item, max_window_seconds=window_seconds)
        if item.max_window_seconds == otel_demo.OBSERVATION_SECONDS
        else item
        for item in base
    )


def _registered(
    elasticsearch: bool = False,
    loki: bool = False,
    victorialogs: bool = False,
    live_metrics: bool = False,
    knowledge: bool = False,
    sql: str | None = None,
    kafka: bool = False,
    mongodb: bool = False,
    redis: bool = False,
    helm: bool = False,
    argocd: bool = False,
) -> tuple[ToolRegistration, ...]:
    base = (
        *otel_demo._registrations(),
        *jaeger_query.jaeger_registrations(
            max_window_seconds=otel_demo.OBSERVATION_SECONDS
        ),
        *kubernetes_core.kubernetes_registrations(
            max_window_seconds=otel_demo.OBSERVATION_SECONDS
        ),
        *prometheus_ext.prometheus_ext_registrations(),
        *kubernetes_logs.pod_logs_registrations(
            max_window_seconds=otel_demo.OBSERVATION_SECONDS
        ),
        *kubernetes_tabular.tabular_registrations(
            max_window_seconds=otel_demo.OBSERVATION_SECONDS
        ),
    )
    if elasticsearch:
        base = (
            *base,
            *elasticsearch_logs.elasticsearch_registrations(
                max_window_seconds=otel_demo.OBSERVATION_SECONDS
            ),
            *elasticsearch_cluster.cluster_registrations(
                max_window_seconds=otel_demo.OBSERVATION_SECONDS
            ),
        )
    if loki:
        base = (
            *base,
            *loki_logs.loki_registrations(
                max_window_seconds=otel_demo.OBSERVATION_SECONDS
            ),
        )
    if victorialogs:
        base = (
            *base,
            *victorialogs_logs.victorialogs_registrations(
                max_window_seconds=otel_demo.OBSERVATION_SECONDS
            ),
        )
    if live_metrics:
        base = (
            *base,
            *kubernetes_live_metrics.live_metrics_registrations(
                max_window_seconds=otel_demo.OBSERVATION_SECONDS
            ),
        )
    if helm:
        base = (
            *base,
            *helm_releases.helm_registrations(
                max_window_seconds=otel_demo.OBSERVATION_SECONDS
            ),
        )
    if argocd:
        base = (
            *base,
            *argocd_apps.argocd_registrations(
                max_window_seconds=otel_demo.OBSERVATION_SECONDS
            ),
        )
    if knowledge:
        base = (
            *base,
            knowledge_fetch.fetch_skill_registration(
                max_window_seconds=otel_demo.OBSERVATION_SECONDS
            ),
        )
    if sql is not None:
        base = (
            *base,
            *sql_query.sql_registrations(
                sql, max_window_seconds=otel_demo.OBSERVATION_SECONDS
            ),
        )
    if kafka:
        base = (
            *base,
            *kafka_admin.kafka_registrations(
                max_window_seconds=otel_demo.OBSERVATION_SECONDS
            ),
        )
    if mongodb:
        base = (
            *base,
            *mongodb_query.mongodb_registrations(
                max_window_seconds=otel_demo.OBSERVATION_SECONDS
            ),
        )
    if redis:
        base = (
            *base,
            *redis_inspect.redis_registrations(
                max_window_seconds=otel_demo.OBSERVATION_SECONDS
            ),
        )
    return base


def tool_schema_revision(
    *,
    elasticsearch: bool = False,
    loki: bool = False,
    victorialogs: bool = False,
    live_metrics: bool = False,
    knowledge: bool = False,
    todo: bool = False,
    sql: str | None = None,
    kafka: bool = False,
    mongodb: bool = False,
    redis: bool = False,
    helm: bool = False,
    argocd: bool = False,
    window_seconds: int = otel_demo.OBSERVATION_SECONDS,
) -> str:
    """C3 §5: a content hash of this profile's template bytes, with the
    static resource table (A2-5: a new resource is a new version). With a
    backend configured (B1-U2, C5-14) the tools, registry and source list
    differ, so the hash does; the inputs of the empty and the
    Elasticsearch-only set are unchanged."""
    digest = canonical_hash(
        {
            "profile": PROFILE_NAME,
            "tool_schemas": [
                dict(item)
                for item in _tool_schemas(
                    elasticsearch,
                    loki,
                    victorialogs,
                    live_metrics,
                    knowledge,
                    todo,
                    sql=sql,
                    kafka=kafka,
                    mongodb=mongodb,
                    redis=redis,
                    helm=helm,
                    argocd=argocd,
                )
            ],
            "registry": ToolRegistry(
                _registrations(
                    elasticsearch,
                    loki,
                    victorialogs,
                    live_metrics,
                    knowledge,
                    sql=sql,
                    kafka=kafka,
                    mongodb=mongodb,
                    redis=redis,
                    helm=helm,
                    argocd=argocd,
                    window_seconds=window_seconds,
                )
            ).revision,
            "trace_projection": otel_demo.TRACE_PROJECTION,
            "projection_revision": PROJECTION_REVISION,
            "data_sources": _data_sources(
                elasticsearch,
                loki,
                victorialogs,
                live_metrics,
                knowledge,
                sql=sql,
                kafka=kafka,
                mongodb=mongodb,
                redis=redis,
                helm=helm,
                argocd=argocd,
            ),
            "kubernetes_resources": kubernetes_core.resource_table(),
        }
    )
    return f"{PROFILE_NAME}-{digest[:12]}"


TOOL_SCHEMA_REVISION = tool_schema_revision()
TOOL_NAMES: tuple[str, ...] = tuple(item.name for item in _registrations())
#: B1-U2: the same with ``OPSPILOT_ES_URL`` set.
ES_TOOL_SCHEMA_REVISION = tool_schema_revision(elasticsearch=True)
ES_TOOL_NAMES: tuple[str, ...] = tuple(
    item.name for item in _registrations(elasticsearch=True)
)
#: C5-14: the same with only ``OPSPILOT_LOKI_URL`` set.
LOKI_TOOL_SCHEMA_REVISION = tool_schema_revision(loki=True)
LOKI_TOOL_NAMES: tuple[str, ...] = tuple(
    item.name for item in _registrations(loki=True)
)
#: C5-14: the same with only ``OPSPILOT_VICTORIALOGS_URL`` set.
VICTORIALOGS_TOOL_SCHEMA_REVISION = tool_schema_revision(victorialogs=True)
VICTORIALOGS_TOOL_NAMES: tuple[str, ...] = tuple(
    item.name for item in _registrations(victorialogs=True)
)
#: B3-1: the same with only ``OPSPILOT_K8S_LIVE_METRICS=1``.
LIVE_METRICS_TOOL_SCHEMA_REVISION = tool_schema_revision(live_metrics=True)
LIVE_METRICS_TOOL_NAMES: tuple[str, ...] = tuple(
    item.name for item in _registrations(live_metrics=True)
)
#: C2b: the same with only ``OPSPILOT_KNOWLEDGE_TOOL=1``.
KNOWLEDGE_TOOL_SCHEMA_REVISION = tool_schema_revision(knowledge=True)
KNOWLEDGE_TOOL_NAMES: tuple[str, ...] = tuple(
    item.name for item in _registrations(knowledge=True)
)
#: C1: the same with only ``OPSPILOT_TODO_WRITE=1`` (no registration changes).
TODO_TOOL_SCHEMA_REVISION = tool_schema_revision(todo=True)
#: C6-13: the same with only ``OPSPILOT_SQL_URL`` set, per dialect.
SQL_POSTGRESQL_TOOL_SCHEMA_REVISION = tool_schema_revision(sql="postgresql")
SQL_MYSQL_TOOL_SCHEMA_REVISION = tool_schema_revision(sql="mysql")
SQL_CLICKHOUSE_TOOL_SCHEMA_REVISION = tool_schema_revision(sql="clickhouse")
SQL_MSSQL_TOOL_SCHEMA_REVISION = tool_schema_revision(sql="mssql")
SQL_TOOL_NAMES: tuple[str, ...] = tuple(
    item.name for item in _registrations(sql="postgresql")
)
#: C3-12: the same with only ``OPSPILOT_KAFKA_BOOTSTRAP_SERVERS`` set.
KAFKA_TOOL_SCHEMA_REVISION = tool_schema_revision(kafka=True)
#: C6c: the same with only ``OPSPILOT_MONGODB_URL`` set.
MONGODB_TOOL_SCHEMA_REVISION = tool_schema_revision(mongodb=True)
MONGODB_TOOL_NAMES: tuple[str, ...] = tuple(
    item.name for item in _registrations(mongodb=True)
)
#: #773: the same with only ``OPSPILOT_REDIS_URL`` set.
REDIS_TOOL_SCHEMA_REVISION = tool_schema_revision(redis=True)
REDIS_TOOL_NAMES: tuple[str, ...] = tuple(
    item.name for item in _registrations(redis=True)
)
KAFKA_TOOL_NAMES: tuple[str, ...] = tuple(
    item.name for item in _registrations(kafka=True)
)
#: B4-2: the same with only ``OPSPILOT_HELM_RELEASES=1``.
HELM_TOOL_SCHEMA_REVISION = tool_schema_revision(helm=True)
HELM_TOOL_NAMES: tuple[str, ...] = tuple(
    item.name for item in _registrations(helm=True)
)
#: C4: the same with only ``OPSPILOT_ARGOCD=1``.
ARGOCD_TOOL_SCHEMA_REVISION = tool_schema_revision(argocd=True)
ARGOCD_TOOL_NAMES: tuple[str, ...] = tuple(
    item.name for item in _registrations(argocd=True)
)


def otel_demo_ext_versions(
    *,
    elasticsearch: bool = False,
    loki: bool = False,
    victorialogs: bool = False,
    live_metrics: bool = False,
    knowledge: bool = False,
    todo: bool = False,
    sql: str | None = None,
    kafka: bool = False,
    mongodb: bool = False,
    redis: bool = False,
    helm: bool = False,
    argocd: bool = False,
    cluster_name: str | None = None,
    window_seconds: int = otel_demo.OBSERVATION_SECONDS,
) -> dict[str, str]:
    """``elasticsearch`` / ``loki`` / ``victorialogs``: whether
    ``OPSPILOT_ES_URL`` / ``OPSPILOT_LOKI_URL`` /
    ``OPSPILOT_VICTORIALOGS_URL`` is set (B1-U2, C5-14); ``live_metrics``:
    whether ``OPSPILOT_K8S_LIVE_METRICS=1`` (B3-1); ``knowledge``: whether
    ``OPSPILOT_KNOWLEDGE_TOOL=1`` (C2b), which also adds the runbook list's
    boundary wording to ``prompt_revision``; ``todo``: whether
    ``OPSPILOT_TODO_WRITE=1`` (C1), which adds ``TodoWrite`` and the
    ``-todo`` variant; ``cluster_name``: the operator's local cluster name
    (#722), of which only whether it is set matters here."""
    return {
        **investigation_versions(
            _variant(todo, cluster_name), knowledge_tool=knowledge
        ),
        "tool_schema_revision": tool_schema_revision(
            elasticsearch=elasticsearch,
            loki=loki,
            victorialogs=victorialogs,
            live_metrics=live_metrics,
            knowledge=knowledge,
            todo=todo,
            sql=sql,
            kafka=kafka,
            mongodb=mongodb,
            redis=redis,
            helm=helm,
            argocd=argocd,
            window_seconds=window_seconds,
        ),
    }


def otel_demo_ext_face(
    clock: Clock,
    *,
    elasticsearch: bool = False,
    loki: bool = False,
    victorialogs: bool = False,
    live_metrics: bool = False,
    knowledge: bool = False,
    todo: bool = False,
    sql: str | None = None,
    kafka: bool = False,
    mongodb: bool = False,
    redis: bool = False,
    helm: bool = False,
    argocd: bool = False,
    cluster_name: str | None = None,
    window_seconds: int = otel_demo.OBSERVATION_SECONDS,
) -> ToolFace:
    """The ``otel-demo`` face with this profile's tools and source list; the
    window is fixed at submission the same way. ``elasticsearch`` / ``loki``:
    whether ``OPSPILOT_ES_URL`` / ``OPSPILOT_LOKI_URL`` is set (B1-U2,
    C5-14). ``cluster_name`` (#722): the local cluster name every fresh Run
    records and the multi-cluster paragraph names; ``None`` (or empty) leaves
    the face as it was."""

    def evidence_context(run_id: str) -> Mapping[str, Any]:
        end = clock.now().astimezone(timezone.utc).replace(microsecond=0)
        window = Window(end - timedelta(seconds=otel_demo.OBSERVATION_SECONDS), end)
        context = dict(otel_demo._evidence_context(run_id, window))
        context["data_sources"] = _data_sources(
            elasticsearch,
            loki,
            victorialogs,
            live_metrics,
            knowledge,
            sql=sql,
            kafka=kafka,
            mongodb=mongodb,
            redis=redis,
            helm=helm,
            argocd=argocd,
        )
        return context

    return ToolFace(
        tool_schemas=_tool_schemas(
            elasticsearch,
            loki,
            victorialogs,
            live_metrics,
            knowledge,
            todo,
            sql=sql,
            kafka=kafka,
            mongodb=mongodb,
            redis=redis,
            helm=helm,
            argocd=argocd,
        ),
        variant_id=_variant(todo, cluster_name),
        evidence_context=evidence_context,
        cluster_name=cluster_name or None,
        max_lookback_seconds=window_seconds,
    )


def _target(
    config: otel_demo.OtelDemoConfig,
    kubernetes: kubernetes_core.KubernetesConfig,
    elasticsearch: elasticsearch_logs.ElasticsearchConfig | None = None,
    loki: loki_logs.LokiConfig | None = None,
    victorialogs: victorialogs_logs.VictoriaLogsConfig | None = None,
    sql: sql_query.SqlConfig | None = None,
    kafka: kafka_admin.KafkaConfig | None = None,
    mongodb: mongodb_query.MongoConfig | None = None,
    redis: redis_inspect.RedisConfig | None = None,
) -> RegisteredTarget:
    base = otel_demo._target(config)
    selector = {
        **base.selector,
        kubernetes_core.ENDPOINT_SELECTOR: kubernetes.api_url,
        kubernetes_core.NAMESPACE_SELECTOR: kubernetes.namespace,
    }
    if elasticsearch is not None:
        selector[elasticsearch_logs.ENDPOINT_SELECTOR] = elasticsearch.url
    if loki is not None:
        selector[loki_logs.ENDPOINT_SELECTOR] = loki.url
    if victorialogs is not None:
        selector[victorialogs_logs.ENDPOINT_SELECTOR] = victorialogs.url
    if sql is not None:
        selector[sql_query.ENDPOINT_SELECTOR] = sql.endpoint
    if kafka is not None:
        selector[kafka_admin.ENDPOINT_SELECTOR] = kafka.bootstrap_servers
    if mongodb is not None:
        selector[mongodb_query.ENDPOINT_SELECTOR] = mongodb.endpoint
    if redis is not None:
        selector[redis_inspect.ENDPOINT_SELECTOR] = redis.endpoint
    return RegisteredTarget(
        target_id=base.target_id,
        source=base.source,
        endpoint=base.endpoint,
        credential_ref=base.credential_ref,
        selector=selector,
        display_name=base.display_name,
    )


@dataclass(frozen=True)
class OtelDemoExtRegistrations:
    """The registration DTOs an executor of this profile is built from."""

    tools: tuple[ToolRegistration, ...]
    targets: tuple[RegisteredTarget, ...]


def otel_demo_ext_registrations(
    config: otel_demo.OtelDemoConfig,
    kubernetes: kubernetes_core.KubernetesConfig,
    elasticsearch: elasticsearch_logs.ElasticsearchConfig | None = None,
    loki: loki_logs.LokiConfig | None = None,
    victorialogs: victorialogs_logs.VictoriaLogsConfig | None = None,
    live_metrics: bool = False,
    knowledge: bool = False,
    sql: sql_query.SqlConfig | None = None,
    kafka: kafka_admin.KafkaConfig | None = None,
    mongodb: mongodb_query.MongoConfig | None = None,
    redis: redis_inspect.RedisConfig | None = None,
    helm: bool = False,
    argocd: bool = False,
    window_seconds: int = otel_demo.OBSERVATION_SECONDS,
) -> OtelDemoExtRegistrations:
    """The same registrations ``otel_demo_ext_executor_factory`` registers."""
    return OtelDemoExtRegistrations(
        tools=_registrations(
            elasticsearch is not None,
            loki is not None,
            victorialogs is not None,
            live_metrics,
            knowledge,
            sql=None if sql is None else sql.dialect,
            kafka=kafka is not None,
            mongodb=mongodb is not None,
            redis=redis is not None,
            helm=helm,
            argocd=argocd,
            window_seconds=window_seconds,
        ),
        targets=(
            _target(
                config,
                kubernetes,
                elasticsearch,
                loki,
                victorialogs,
                sql,
                kafka,
                mongodb,
                redis,
            ),
        ),
    )


def _overflow_summary(
    tool: str, rows: Sequence[object], query: Mapping[str, object]
) -> dict[str, object]:
    if tool == kubernetes_core.JQ_QUERY_TOOL:
        return {"row_unit": "results"}
    if tool == kubernetes_core.COUNT_TOOL:
        return {"row_unit": "matches"}
    if tool in prometheus_ext.PROMETHEUS_EXT_TOOLS:
        return prometheus_ext.overflow_summary(tool, rows, query)
    if tool == kubernetes_logs.POD_LOGS_TOOL:
        return {"row_unit": "lines"}
    if tool == kubernetes_tabular.TABULAR_TOOL:
        return {"row_unit": "rows"}
    if tool in jaeger_query.ROW_UNITS:
        return {"row_unit": jaeger_query.ROW_UNITS[tool]}
    if tool in elasticsearch_logs.TOOL_NAMES:
        return elasticsearch_logs.overflow_summary(tool, rows, query)
    if tool in elasticsearch_cluster.TOOL_NAMES:
        return elasticsearch_cluster.overflow_summary(tool, rows, query)
    if tool in loki_logs.TOOL_NAMES:
        return loki_logs.overflow_summary(tool, rows, query)
    if tool in victorialogs_logs.TOOL_NAMES:
        return victorialogs_logs.overflow_summary(tool, rows, query)
    if tool == kubernetes_live_metrics.PODS_TOOL:
        return {"row_unit": "pods"}
    if tool == kubernetes_live_metrics.NODES_TOOL:
        return {"row_unit": "nodes"}
    if tool == helm_releases.HISTORY_TOOL:
        return {"row_unit": "revisions"}
    if tool in argocd_apps.TOOL_NAMES:
        return argocd_apps.overflow_summary(tool, rows, query)
    if tool in helm_releases.TOOL_NAMES:
        return {"row_unit": "releases"}
    if tool in sql_query.TOOL_NAMES:
        return sql_query.overflow_summary(tool, rows, query)
    if tool in kafka_admin.TOOL_NAMES:
        return kafka_admin.overflow_summary(tool, rows, query)
    if tool in mongodb_query.TOOL_NAMES:
        return mongodb_query.overflow_summary(tool, rows, query)
    if tool in redis_inspect.TOOL_NAMES:
        return redis_inspect.overflow_summary(tool, rows, query)
    return otel_demo.overflow_summary(tool, rows, query)


def otel_demo_ext_executor_factory(
    store: DurableStore,
    *,
    evidence: EvidenceSink,
    clock: Clock,
    config: otel_demo.OtelDemoConfig | None = None,
    kubernetes: kubernetes_core.KubernetesConfig,
    elasticsearch: elasticsearch_logs.ElasticsearchConfig | None = None,
    loki: loki_logs.LokiConfig | None = None,
    victorialogs: victorialogs_logs.VictoriaLogsConfig | None = None,
    live_metrics: bool = False,
    knowledge: bool = False,
    todo: bool = False,
    sql: sql_query.SqlConfig | None = None,
    kafka: kafka_admin.KafkaConfig | None = None,
    mongodb: mongodb_query.MongoConfig | None = None,
    redis: redis_inspect.RedisConfig | None = None,
    helm: bool = False,
    argocd: bool = False,
    jq_path: str | None = None,
    token_counter: TokenCounter | None = None,
    window_seconds: int = otel_demo.OBSERVATION_SECONDS,
) -> ExecutorFactory:
    """An ``ExecutorFactory`` over this profile; built once per worker slot.

    ``jq_path`` defaults to ``jq`` on PATH: a worker without it does not start
    (``JQ_UNAVAILABLE``, A2-8), nor without a tokenizer, nor
    without ``python3`` on PATH for the Pod log filters' regex child (A3-7,
    ``PYTHON_NOT_FOUND``; the child resolves it again per call).

    ``elasticsearch`` (B1-U2): the ``es-ro`` connection when
    ``OPSPILOT_ES_URL`` is set; the three tools are registered and routed
    only then.

    ``loki`` (C5-14): the ``loki-ro`` connection when ``OPSPILOT_LOKI_URL``
    is set; ``grafana_loki_query`` is registered and routed only then.

    ``victorialogs`` (C5b): the ``vlogs-ro`` connection when
    ``OPSPILOT_VICTORIALOGS_URL`` is set; the five tools are registered and
    routed only then.

    ``live_metrics`` (B3-1): ``OPSPILOT_K8S_LIVE_METRICS=1``; the two live
    usage tools are registered and routed to the cluster transport only then.

    ``knowledge`` (C2b): ``OPSPILOT_KNOWLEDGE_TOOL=1``; ``fetch_skill`` is
    registered, and each Run's executor routes it to a ``KnowledgeTransport``
    bound to that Run's own target and catalog threshold (the registered
    target is the fixed lab target, never the Run's ``resource_uid``).

    The Helm switch (B4): ``OPSPILOT_HELM_RELEASES=1``; the three Helm tools are
    registered and routed to the cluster transport, whose client then lets
    Helm release pages through the Secret gateway. With a cluster address
    that is not a lab host the worker does not start (``HELM_NOT_LAB``).

    ``todo`` (C1): ``OPSPILOT_TODO_WRITE=1``. Accepted for symmetry with the
    face and versions and deliberately unused: ``TodoWrite`` is not a gateway
    tool, so nothing is registered or routed; the loop answers it from the
    Run input's tools.

    ``window_seconds`` (#779): the deployment's ``OPSPILOT_MAX_LOOKBACK``,
    every registered window ceiling (B4-2); each Run stays bounded by the
    frame its own input recorded.
    """
    del todo
    if helm and not _is_lab_host(kubernetes.api_url):
        # B4-3: with Helm on, the identity can read every Secret's content;
        # only a synthetic (lab) cluster may be read this way.
        raise ToolContractError(helm_releases.NOT_LAB_CODE)
    config = config or otel_demo.OtelDemoConfig()
    jq = jq_path or kubernetes_core.require_jq()
    kubernetes_core.require_python()  # startup precondition (A3-7)
    counter = token_counter if token_counter is not None else default_counter()
    registrations = otel_demo_ext_registrations(
        config,
        kubernetes,
        elasticsearch,
        loki,
        victorialogs,
        live_metrics,
        knowledge,
        sql=sql,
        kafka=kafka,
        mongodb=mongodb,
        redis=redis,
        helm=helm,
        argocd=argocd,
        window_seconds=window_seconds,
    )
    # C2b: the worker runtime role, read-only snapshot reads only.
    knowledge_reader = (
        knowledge_fetch.KnowledgeReader(
            store.dsn, pool=getattr(store, "pool_config", None)
        )
        if knowledge
        else None
    )
    tools = ToolRegistry(registrations.tools)
    targets = TargetRegistry(list(registrations.targets))
    pair = config.prometheus_basic_auth
    sources = otel_demo.OtelDemoTransport(
        credentials={otel_demo.CREDENTIAL_REF: config.token},
        basic_auth={} if pair is None else {otel_demo.CREDENTIAL_REF: pair},
        # A5-7: this profile's Prometheus tools get the same basic auth, 503
        # timeout and error echo; ``otel_demo.PROMETHEUS_TOOLS`` is unchanged.
        prometheus_tools=otel_demo.PROMETHEUS_TOOLS
        | prometheus_ext.PROMETHEUS_EXT_TOOLS,
    )
    prometheus = prometheus_ext.PrometheusExtTransport(sources)
    # A4: anonymous, like the lab Jaeger's query port; the slot is named only.
    traces = jaeger_query.JaegerQueryTransport(
        credentials={otel_demo.CREDENTIAL_REF: None},
    )
    cluster = kubernetes_core.KubernetesTransport(
        credentials={kubernetes_core.CREDENTIAL_REF: kubernetes.token},
        jq_path=jq,
        ca_file=kubernetes.ca_file,
        application_namespace=kubernetes.namespace,
        # B4-5: only then may the gateway pass a Helm release page.
        helm_releases=helm,
        # B4-11: the "queried at" note reads the executor's clock.
        now=clock.now,
    )
    routes: dict[str, Any] = {
        **dict.fromkeys(prometheus_ext.TOOL_NAMES, prometheus),
        **dict.fromkeys(jaeger_query.TOOL_NAMES, traces),
        # A3: served by the cluster transport, routed by name.
        **dict.fromkeys(kubernetes_logs.TOOL_NAMES, cluster),
        # A6: likewise.
        **dict.fromkeys(kubernetes_tabular.TOOL_NAMES, cluster),
    }
    if live_metrics:
        # B3-9: the same cluster transport, ``k8s-ro`` slot.
        routes.update(dict.fromkeys(kubernetes_live_metrics.TOOL_NAMES, cluster))
    if helm:
        # B4-15: the same cluster transport and ``k8s-ro`` slot.
        routes.update(dict.fromkeys(helm_releases.TOOL_NAMES, cluster))
    if argocd:
        # C4-10: the same cluster transport and ``k8s-ro`` slot.
        routes.update(dict.fromkeys(argocd_apps.TOOL_NAMES, cluster))
    if elasticsearch is not None:
        # B1-4: its own slot, never another backend's credential.
        search = elasticsearch_logs.ElasticsearchTransport(
            credentials={
                elasticsearch_logs.CREDENTIAL_REF: elasticsearch.authorization
            },
            jq_path=jq,
            ca_file=elasticsearch.ca_file,
        )
        routes.update(dict.fromkeys(elasticsearch_logs.TOOL_NAMES, search))
        # #808: the cluster tools over the same address and ``es-ro`` slot.
        cluster_state = elasticsearch_cluster.ElasticsearchClusterTransport(
            credentials={
                elasticsearch_logs.CREDENTIAL_REF: elasticsearch.authorization
            },
            ca_file=elasticsearch.ca_file,
        )
        routes.update(dict.fromkeys(elasticsearch_cluster.TOOL_NAMES, cluster_state))
    if loki is not None:
        # C5-4: its own slot and configured tenant, never another backend's.
        logs = loki_logs.LokiTransport(
            credentials={loki_logs.CREDENTIAL_REF: loki.authorization},
            tenant_id=loki.tenant_id,
            ca_file=loki.ca_file,
        )
        routes.update(dict.fromkeys(loki_logs.TOOL_NAMES, logs))
    if victorialogs is not None:
        # C5-4: its own slot and configured tenant, never another backend's.
        vlogs = victorialogs_logs.VictoriaLogsTransport(
            credentials={victorialogs_logs.CREDENTIAL_REF: victorialogs.authorization},
            tenant_headers=victorialogs.tenant_headers,
            ca_file=victorialogs.ca_file,
        )
        routes.update(dict.fromkeys(victorialogs_logs.TOOL_NAMES, vlogs))
    if sql is not None:
        # C6-7/C6-8: its own ``sql-ro`` slot; never a DSN of OpsPilot's own.
        database = sql_query.SqlTransport(sql)
        routes.update(dict.fromkeys(sql_query.TOOL_NAMES, database))
    if kafka is not None:
        # C3-4/C3-9: its own ``kafka-ro`` slot, one short client per call.
        brokers = kafka_admin.KafkaTransport(kafka)
        routes.update(dict.fromkeys(kafka_admin.TOOL_NAMES, brokers))
    if mongodb is not None:
        # C6c: its own ``mongodb-ro`` slot, one short client per call.
        documents = mongodb_query.MongoTransport(mongodb)
        routes.update(dict.fromkeys(mongodb_query.TOOL_NAMES, documents))
    if redis is not None:
        # #773: its own ``redis-ro`` slot, one short connection per call.
        keyspace = redis_inspect.RedisTransport(redis)
        routes.update(dict.fromkeys(redis_inspect.TOOL_NAMES, keyspace))
    control = otel_demo.DurableControl(store)
    reader = cast(
        "EvidenceReader | None",
        evidence if callable(getattr(evidence, "get", None)) else None,
    )
    result_paths = {
        item.name: item.result_path
        for item in registrations.tools
        if item.source == otel_demo.SOURCE
    }
    citable = {
        item.name: item.citable_as_fact
        for item in registrations.tools
        if item.source == otel_demo.SOURCE
    }

    def factory(lease: Lease, input: InvestigationInput) -> ReadOnlyToolExecutor:
        run = store.rebuild(lease.incident_id)["run"]
        run_routes = routes
        if knowledge_reader is not None:
            run_routes = {
                **routes,
                knowledge_fetch.TOOL_NAME: knowledge_fetch.KnowledgeTransport(
                    reader=knowledge_reader,
                    resource_uid=input.bound_target_id,
                    max_age_seconds=catalog_max_age(input.scope_facts),
                ),
            }
        scope = QueryScope(
            scope_id=f"{PROFILE_NAME}:{lease.run_id}:{lease.epoch}",
            subject_kind="incident",
            subject_id=str(lease.incident_id),
            run_id=str(lease.run_id),
            control_generation=lease.control_generation,
            global_suspension_generation=lease.global_suspension_generation,
            target_suspension_generation=lease.target_suspension_generation,
            registry_revision=targets.revision,
            tool_registry_revision=tools.revision,
            target_ids=frozenset({otel_demo.TARGET_ID}),
            tool_names=frozenset(item.name for item in registrations.tools),
            window=otel_demo.scope_window(input),
            default_query_window=otel_demo.scope_default_query_window(input),
            deadline=run["deadline"],
        )
        return ReadOnlyToolExecutor(
            scope=scope,
            tools=tools,
            targets=targets,
            transport=RoutedTransport(
                evidence=EvidenceReadTransport(
                    store=reader,
                    run_id=scope.run_id,
                    target_ids=scope.target_ids,
                    result_paths=result_paths,
                    citable=citable,
                ),
                sources=sources,
                kubernetes=cluster,
                routes=run_routes,
            ),
            evidence=evidence,
            control=control,
            clock=clock,
            ledger=DurableToolLedger(store, lease),
            token_counter=counter,
            overflow_summary=_overflow_summary,
        )

    return factory
