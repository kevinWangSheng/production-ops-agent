"""The extended OTel Demo tool profile ``otel-demo-ext`` (tool expansion A2-2).

Every ``otel-demo`` tool, byte for byte (``opspilot.tools.otel_demo`` is not
changed), plus the cluster object queries of
:mod:`opspilot.tools.kubernetes_core` and the Prometheus instant query, series
and label-name tools of :mod:`opspilot.tools.prometheus_ext` (A5) with its
rules, scrape-target and metric-metadata tools (B2). Later tool-expansion
items (column queries, more trace and Prometheus tools) join this profile,
never ``otel-demo``.

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
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta, timezone
from typing import Any, cast

from opspilot.investigation.context import InvestigationInput
from opspilot.investigation.inputs import ToolFace
from opspilot.investigation.loop import DISCIPLINE_VARIANT, investigation_versions
from opspilot.investigation.reports import DATA_SOURCES_INSTRUCTION
from opspilot.investigation.runner import ExecutorFactory
from opspilot.persistence import DurableStore, Lease
from opspilot.tools import (
    jaeger_query,
    kubernetes_core,
    kubernetes_logs,
    otel_demo,
    prometheus_ext,
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
    "PROFILE_NAME",
    "TOOL_NAMES",
    "TOOL_SCHEMAS",
    "TOOL_SCHEMA_REVISION",
    "otel_demo_ext_executor_factory",
    "otel_demo_ext_face",
    "otel_demo_ext_registrations",
    "otel_demo_ext_versions",
]

PROFILE_NAME = "otel-demo-ext"

#: ``otel-demo``'s tools array unchanged, then the new tools.
TOOL_SCHEMAS: tuple[Mapping[str, Any], ...] = (
    *otel_demo.TOOL_SCHEMAS,
    *jaeger_query.TOOL_SCHEMAS,
    *kubernetes_core.TOOL_SCHEMAS,
    *prometheus_ext.TOOL_SCHEMAS,
    *kubernetes_logs.TOOL_SCHEMAS,
)

#: What this profile can and cannot query (#311 U5); ``kubernetes_state``
#: moves from not integrated to available (A2-2), and ``pod_logs`` with it
#: (A3-11).
_NOT_INTEGRATED: tuple[str, ...] = tuple(
    name
    for name in otel_demo.NOT_INTEGRATED
    if name not in {"kubernetes_state", "pod_logs"}
)


def _data_sources() -> dict[str, Any]:
    return {
        "available": {
            "prometheus": sorted(
                otel_demo.PROMETHEUS_TOOLS | prometheus_ext.PROMETHEUS_EXT_TOOLS
            ),
            "jaeger": [otel_demo.TRACES_TOOL, *jaeger_query.TOOL_NAMES],
            "kubernetes": sorted(
                kubernetes_core.K8S_TOOLS | set(kubernetes_logs.TOOL_NAMES)
            ),
        },
        "not_integrated": list(_NOT_INTEGRATED),
        "instruction": DATA_SOURCES_INSTRUCTION,
    }


def _registrations() -> tuple[ToolRegistration, ...]:
    return (
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
    )


def _tool_schema_revision() -> str:
    """C3 §5: a content hash of this profile's template bytes, with the
    static resource table (A2-5: a new resource is a new version)."""
    digest = canonical_hash(
        {
            "profile": PROFILE_NAME,
            "tool_schemas": [dict(item) for item in TOOL_SCHEMAS],
            "registry": ToolRegistry(_registrations()).revision,
            "trace_projection": otel_demo.TRACE_PROJECTION,
            "projection_revision": PROJECTION_REVISION,
            "data_sources": _data_sources(),
            "kubernetes_resources": kubernetes_core.resource_table(),
        }
    )
    return f"{PROFILE_NAME}-{digest[:12]}"


TOOL_SCHEMA_REVISION = _tool_schema_revision()
TOOL_NAMES: tuple[str, ...] = tuple(item.name for item in _registrations())


def otel_demo_ext_versions() -> dict[str, str]:
    return {**investigation_versions(), "tool_schema_revision": TOOL_SCHEMA_REVISION}


def otel_demo_ext_face(clock: Clock) -> ToolFace:
    """The ``otel-demo`` face with this profile's tools and source list; the
    window is fixed at submission the same way."""

    def evidence_context(run_id: str) -> Mapping[str, Any]:
        end = clock.now().astimezone(timezone.utc).replace(microsecond=0)
        window = Window(end - timedelta(seconds=otel_demo.OBSERVATION_SECONDS), end)
        context = dict(otel_demo._evidence_context(run_id, window))
        context["data_sources"] = _data_sources()
        return context

    return ToolFace(
        tool_schemas=TOOL_SCHEMAS,
        variant_id=DISCIPLINE_VARIANT,
        evidence_context=evidence_context,
    )


def _target(
    config: otel_demo.OtelDemoConfig, kubernetes: kubernetes_core.KubernetesConfig
) -> RegisteredTarget:
    base = otel_demo._target(config)
    return RegisteredTarget(
        target_id=base.target_id,
        source=base.source,
        endpoint=base.endpoint,
        credential_ref=base.credential_ref,
        selector={
            **base.selector,
            kubernetes_core.ENDPOINT_SELECTOR: kubernetes.api_url,
            kubernetes_core.NAMESPACE_SELECTOR: kubernetes.namespace,
        },
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
) -> OtelDemoExtRegistrations:
    """The same registrations ``otel_demo_ext_executor_factory`` registers."""
    return OtelDemoExtRegistrations(
        tools=_registrations(), targets=(_target(config, kubernetes),)
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
    if tool in jaeger_query.ROW_UNITS:
        return {"row_unit": jaeger_query.ROW_UNITS[tool]}
    return otel_demo.overflow_summary(tool, rows, query)


def otel_demo_ext_executor_factory(
    store: DurableStore,
    *,
    evidence: EvidenceSink,
    clock: Clock,
    config: otel_demo.OtelDemoConfig | None = None,
    kubernetes: kubernetes_core.KubernetesConfig,
    jq_path: str | None = None,
    token_counter: TokenCounter | None = None,
) -> ExecutorFactory:
    """An ``ExecutorFactory`` over this profile; built once per worker slot.

    ``jq_path`` defaults to ``jq`` on PATH: a worker without it does not start
    (``JQ_UNAVAILABLE``, A2-8), nor without a tokenizer, nor
    without ``python3`` on PATH for the Pod log filters' regex child (A3-7,
    ``PYTHON_NOT_FOUND``; the child resolves it again per call).
    """
    config = config or otel_demo.OtelDemoConfig()
    jq = jq_path or kubernetes_core.require_jq()
    kubernetes_core.require_python()  # startup precondition (A3-7)
    counter = token_counter if token_counter is not None else default_counter()
    registrations = otel_demo_ext_registrations(config, kubernetes)
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
                routes={
                    **dict.fromkeys(prometheus_ext.TOOL_NAMES, prometheus),
                    **dict.fromkeys(jaeger_query.TOOL_NAMES, traces),
                    # A3: served by the cluster transport, routed by name.
                    **dict.fromkeys(kubernetes_logs.TOOL_NAMES, cluster),
                },
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
