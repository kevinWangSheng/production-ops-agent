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
of :mod:`opspilot.tools.elasticsearch_logs` join this profile only when
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
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta, timezone
from typing import Any, cast

from opspilot.investigation.context import InvestigationInput
from opspilot.investigation.inputs import ToolFace
from opspilot.investigation.loop import investigation_versions
from opspilot.investigation.reports import DATA_SOURCES_INSTRUCTION
from opspilot.investigation.runner import ExecutorFactory
from opspilot.persistence import DurableStore, Lease
from opspilot.tools import (
    elasticsearch_logs,
    jaeger_query,
    kubernetes_core,
    kubernetes_logs,
    kubernetes_tabular,
    loki_logs,
    otel_demo,
    prometheus_ext,
    victorialogs_logs,
)
from opspilot.tools.evidence_read import (
    EvidenceReader,
    EvidenceReadTransport,
    RoutedTransport,
)
from opspilot.tools.executor import (
    MAX_OPERATIONS_PER_RUN,
    MAX_TOOL_SECONDS_PER_RUN,
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
    ToolRegistration,
    ToolRegistry,
    canonical_hash,
)
from opspilot.tools.tokens import TokenCounter, default_counter

__all__ = [
    "ES_TOOL_NAMES",
    "ES_TOOL_SCHEMAS",
    "ES_TOOL_SCHEMA_REVISION",
    "LOKI_TOOL_NAMES",
    "LOKI_TOOL_SCHEMA_REVISION",
    "PROFILE_NAME",
    "TOOL_NAMES",
    "TOOL_SCHEMAS",
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
#: the three Elasticsearch tools.
ES_TOOL_SCHEMAS: tuple[Mapping[str, Any], ...] = (
    *TOOL_SCHEMAS,
    *elasticsearch_logs.TOOL_SCHEMAS,
)


def _tool_schemas(
    elasticsearch: bool = False, loki: bool = False, victorialogs: bool = False
) -> tuple[Mapping[str, Any], ...]:
    """C5-14: the base array, then each configured backend's tools in the
    fixed order Elasticsearch, Loki, VictoriaLogs."""
    schemas = ES_TOOL_SCHEMAS if elasticsearch else TOOL_SCHEMAS
    if loki:
        schemas = (*schemas, *loki_logs.TOOL_SCHEMAS)
    if victorialogs:
        schemas = (*schemas, *victorialogs_logs.TOOL_SCHEMAS)
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
    elasticsearch: bool = False, loki: bool = False, victorialogs: bool = False
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
        ),
    }
    if elasticsearch:
        available["elasticsearch"] = sorted(elasticsearch_logs.TOOL_NAMES)
    if loki:
        available["loki"] = sorted(loki_logs.TOOL_NAMES)
    if victorialogs:
        available["victorialogs"] = sorted(victorialogs_logs.TOOL_NAMES)
    return {
        "available": available,
        "not_integrated": list(_NOT_INTEGRATED),
        "instruction": DATA_SOURCES_INSTRUCTION,
    }


def _registrations(
    elasticsearch: bool = False, loki: bool = False, victorialogs: bool = False
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
    return base


def tool_schema_revision(
    *, elasticsearch: bool = False, loki: bool = False, victorialogs: bool = False
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
                dict(item) for item in _tool_schemas(elasticsearch, loki, victorialogs)
            ],
            "registry": ToolRegistry(
                _registrations(elasticsearch, loki, victorialogs)
            ).revision,
            "trace_projection": otel_demo.TRACE_PROJECTION,
            "projection_revision": PROJECTION_REVISION,
            "data_sources": _data_sources(elasticsearch, loki, victorialogs),
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


def otel_demo_ext_versions(
    *, elasticsearch: bool = False, loki: bool = False, victorialogs: bool = False
) -> dict[str, str]:
    """``elasticsearch`` / ``loki`` / ``victorialogs``: whether
    ``OPSPILOT_ES_URL`` / ``OPSPILOT_LOKI_URL`` /
    ``OPSPILOT_VICTORIALOGS_URL`` is set (B1-U2, C5-14)."""
    return {
        **investigation_versions(DISCIPLINE_VARIANT),
        "tool_schema_revision": tool_schema_revision(
            elasticsearch=elasticsearch, loki=loki, victorialogs=victorialogs
        ),
    }


def otel_demo_ext_face(
    clock: Clock,
    *,
    elasticsearch: bool = False,
    loki: bool = False,
    victorialogs: bool = False,
) -> ToolFace:
    """The ``otel-demo`` face with this profile's tools and source list; the
    window is fixed at submission the same way. ``elasticsearch`` / ``loki``:
    whether ``OPSPILOT_ES_URL`` / ``OPSPILOT_LOKI_URL`` is set (B1-U2,
    C5-14)."""

    def evidence_context(run_id: str) -> Mapping[str, Any]:
        end = clock.now().astimezone(timezone.utc).replace(microsecond=0)
        window = Window(end - timedelta(seconds=otel_demo.OBSERVATION_SECONDS), end)
        context = dict(otel_demo._evidence_context(run_id, window))
        context["data_sources"] = _data_sources(elasticsearch, loki, victorialogs)
        return context

    return ToolFace(
        tool_schemas=_tool_schemas(elasticsearch, loki, victorialogs),
        variant_id=DISCIPLINE_VARIANT,
        evidence_context=evidence_context,
    )


def _target(
    config: otel_demo.OtelDemoConfig,
    kubernetes: kubernetes_core.KubernetesConfig,
    elasticsearch: elasticsearch_logs.ElasticsearchConfig | None = None,
    loki: loki_logs.LokiConfig | None = None,
    victorialogs: victorialogs_logs.VictoriaLogsConfig | None = None,
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
) -> OtelDemoExtRegistrations:
    """The same registrations ``otel_demo_ext_executor_factory`` registers."""
    return OtelDemoExtRegistrations(
        tools=_registrations(
            elasticsearch is not None, loki is not None, victorialogs is not None
        ),
        targets=(_target(config, kubernetes, elasticsearch, loki, victorialogs),),
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
    if tool in loki_logs.TOOL_NAMES:
        return loki_logs.overflow_summary(tool, rows, query)
    if tool in victorialogs_logs.TOOL_NAMES:
        return victorialogs_logs.overflow_summary(tool, rows, query)
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
    jq_path: str | None = None,
    token_counter: TokenCounter | None = None,
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
    """
    config = config or otel_demo.OtelDemoConfig()
    jq = jq_path or kubernetes_core.require_jq()
    kubernetes_core.require_python()  # startup precondition (A3-7)
    counter = token_counter if token_counter is not None else default_counter()
    registrations = otel_demo_ext_registrations(
        config, kubernetes, elasticsearch, loki, victorialogs
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
    )
    routes: dict[str, Any] = {
        **dict.fromkeys(prometheus_ext.TOOL_NAMES, prometheus),
        **dict.fromkeys(jaeger_query.TOOL_NAMES, traces),
        # A3: served by the cluster transport, routed by name.
        **dict.fromkeys(kubernetes_logs.TOOL_NAMES, cluster),
        # A6: likewise.
        **dict.fromkeys(kubernetes_tabular.TOOL_NAMES, cluster),
    }
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

    def factory(lease: Lease, input: InvestigationInput) -> ReadOnlyToolExecutor:
        run = store.rebuild(lease.incident_id)["run"]
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
                ),
                sources=sources,
                kubernetes=cluster,
                routes=routes,
            ),
            evidence=evidence,
            control=control,
            clock=clock,
            ledger=DurableToolLedger(
                store,
                lease,
                max_operations=MAX_OPERATIONS_PER_RUN,
                max_tool_seconds=MAX_TOOL_SECONDS_PER_RUN,
            ),
            token_counter=counter,
            overflow_summary=_overflow_summary,
        )

    return factory
