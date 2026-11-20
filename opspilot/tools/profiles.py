"""Tool profile selection for the workbench and the worker.

A *profile* is the tool half of the product composition: the model-visible
face the workbench records into every Run's input, the ``versions`` both
processes must agree on (C3 §5: a Run recorded under one face is blocked on
claim by a worker running another), and the executor factory the worker
drives. Selection is by ``OPSPILOT_TOOL_PROFILE``:

* ``fixture`` (default): the canned Prometheus view, no network -- what CI,
  the tests and the local demo use;
* ``otel-demo``: real read-only Prometheus and Jaeger queries against the
  pinned OTel Demo lab (``opspilot.tools.otel_demo``), configured by
  ``OPSPILOT_OTEL_PROMETHEUS_URL``, ``OPSPILOT_OTEL_JAEGER_URL`` and the
  investigation side's own credential: ``OPSPILOT_OTEL_PROMETHEUS_USERNAME``
  / ``OPSPILOT_OTEL_PROMETHEUS_PASSWORD`` (basic auth, the lab) or
  ``OPSPILOT_OTEL_TOKEN`` (bearer). The Observer's ``OPSPILOT_OBSERVER_*``
  variables are never read here (C3 §3, D3).
* ``otel-demo-ext``: every ``otel-demo`` tool unchanged plus the read-only
  cluster object queries (``opspilot.tools.otel_demo_ext``, tool expansion
  A2). Its face and versions need no cluster setting, so the web starts
  without one; only the worker reads ``OPSPILOT_K8S_API_URL``,
  ``OPSPILOT_K8S_TOKEN``, ``OPSPILOT_K8S_CA_FILE`` and
  ``OPSPILOT_K8S_NAMESPACE``, when it builds the executor, and refuses to
  start without them or without ``jq``. With ``OPSPILOT_ES_URL`` set, the
  three Elasticsearch/OpenSearch tools join it (B1-U2): both processes read
  that address to pick the face and versions, and only the worker reads the
  ``es-ro`` credential (``OPSPILOT_ES_API_KEY``, or ``OPSPILOT_ES_USERNAME``
  and ``OPSPILOT_ES_PASSWORD``; ``OPSPILOT_ES_CA_FILE`` for HTTPS). With
  ``OPSPILOT_LOKI_URL`` set, ``grafana_loki_query`` joins it the same way
  (C5-14): both processes read the address, only the worker reads the
  ``loki-ro`` credential (``OPSPILOT_LOKI_USERNAME`` and
  ``OPSPILOT_LOKI_PASSWORD``, or ``OPSPILOT_LOKI_BEARER_TOKEN``;
  ``OPSPILOT_LOKI_CA_FILE`` for HTTPS) and the tenant
  ``OPSPILOT_LOKI_TENANT_ID``. ``OPSPILOT_VICTORIALOGS_URL`` adds the five
  ``victorialogs_*`` tools likewise (C5b): only the worker reads the
  ``vlogs-ro`` credential (``OPSPILOT_VICTORIALOGS_USERNAME`` and
  ``_PASSWORD``, or ``_BEARER_TOKEN``; ``_CA_FILE``) and the tenant
  (``_ACCOUNT_ID``, ``_PROJECT_ID``). ``OPSPILOT_K8S_LIVE_METRICS=1`` adds
  ``kubectl_top_pods`` and ``kubectl_top_nodes`` (B3-1) over the existing
  cluster connection; any value but ``1``, ``0`` or empty is a startup error
  (``K8S_LIVE_METRICS_INVALID``) in both processes.
  ``OPSPILOT_KNOWLEDGE_TOOL=1`` adds ``fetch_skill`` and, in the web, the
  approved-runbook list to new Runs (C2b); the same values rule
  (``KNOWLEDGE_TOOL_INVALID``). ``OPSPILOT_SQL_URL`` adds the three
  ``database_sql_*`` tools (C6a): both processes read the address's scheme
  for the dialect, only the worker reads the ``sql-ro`` credential
  (``OPSPILOT_SQL_USERNAME``, ``OPSPILOT_SQL_PASSWORD``; ``OPSPILOT_SQL_TLS``,
  ``OPSPILOT_SQL_CA_FILE``) and refuses an address that is one of
  OpsPilot's own ``OPSPILOT_*DSN`` databases (``SQL_TARGETS_OPSPILOT_DB``).
  ``OPSPILOT_KAFKA_BOOTSTRAP_SERVERS`` adds the eight Kafka tools (C3): the
  web reads the address only, the worker the ``kafka-ro`` settings
  (``OPSPILOT_KAFKA_SECURITY_PROTOCOL``, ``_SASL_MECHANISM``, ``_USERNAME``,
  ``_PASSWORD``, ``_CA_FILE``; ``KAFKA_CONFIG_MISSING``/``_INVALID``).
  ``OPSPILOT_MONGODB_URL`` adds the six MongoDB tools (C6c): the web reads
  the address only, the worker the ``mongodb-ro`` settings
  (``OPSPILOT_MONGODB_DATABASE``, ``_USERNAME``, ``_PASSWORD``,
  ``_AUTH_SOURCE``, ``_TLS``, ``_CA_FILE``; ``MONGODB_CONFIG_MISSING``/
  ``_INVALID``, ``MONGODB_TARGETS_OPSPILOT_DB``).

Both processes must be started with the same value (and, for
``otel-demo-ext``, with ``OPSPILOT_ES_URL``, ``OPSPILOT_LOKI_URL`` and
``OPSPILOT_VICTORIALOGS_URL``, ``OPSPILOT_SQL_URL`` and
``OPSPILOT_KAFKA_BOOTSTRAP_SERVERS`` each set (to the same value) in both or
in neither, and
``OPSPILOT_K8S_LIVE_METRICS`` and ``OPSPILOT_KNOWLEDGE_TOOL`` the same in
both).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from opspilot.investigation.inputs import ToolFace
from opspilot.investigation.runner import ExecutorFactory
from opspilot.investigation.todo import todo_write_enabled
from opspilot.persistence import DurableStore
from opspilot.tools.elasticsearch_logs import (
    ElasticsearchConfig,
    elasticsearch_enabled,
)
from opspilot.tools.executor import Clock, EvidenceSink
from opspilot.tools.fixture import (
    fixture_executor_factory,
    fixture_face,
    fixture_versions,
)
from opspilot.tools.kafka_admin import KafkaConfig, kafka_enabled
from opspilot.tools.knowledge_fetch import knowledge_tool_enabled
from opspilot.tools.kubernetes_core import KubernetesConfig
from opspilot.tools.kubernetes_live_metrics import live_metrics_enabled
from opspilot.tools.loki_logs import LokiConfig, loki_enabled
from opspilot.tools.mongodb_query import MongoConfig, mongodb_enabled
from opspilot.tools.otel_demo import (
    OtelDemoConfig,
    otel_demo_executor_factory,
    otel_demo_face,
    otel_demo_versions,
)
from opspilot.tools.otel_demo_ext import (
    otel_demo_ext_executor_factory,
    otel_demo_ext_face,
    otel_demo_ext_versions,
)
from opspilot.tools.registry import ToolContractError
from opspilot.tools.sql_query import SqlConfig, sql_dialect
from opspilot.tools.victorialogs_logs import (
    VictoriaLogsConfig,
    victorialogs_enabled,
)

__all__ = ["PROFILE_ENV", "PROFILE_NAMES", "ToolProfile", "select_profile"]

PROFILE_ENV = "OPSPILOT_TOOL_PROFILE"
PROFILE_NAMES = ("fixture", "otel-demo", "otel-demo-ext")


@dataclass(frozen=True)
class ToolProfile:
    name: str
    face: Callable[[Clock], ToolFace]
    versions: Callable[[], dict[str, str]]
    executor_factory: Callable[[DurableStore, EvidenceSink, Clock], ExecutorFactory]
    #: C2b: whether new Runs carry the approved-runbook list (input v7);
    #: only ``otel-demo-ext`` with ``OPSPILOT_KNOWLEDGE_TOOL=1``.
    runbook_catalog: bool = False


def _fixture() -> ToolProfile:
    return ToolProfile(
        name="fixture",
        face=lambda clock: fixture_face(),
        versions=fixture_versions,
        executor_factory=lambda store, evidence, clock: fixture_executor_factory(
            store, evidence=evidence, clock=clock
        ),
    )


def _otel_demo(env: Mapping[str, str]) -> ToolProfile:
    config = OtelDemoConfig.from_env(env)
    return ToolProfile(
        name="otel-demo",
        face=otel_demo_face,
        versions=otel_demo_versions,
        executor_factory=lambda store, evidence, clock: otel_demo_executor_factory(
            store, evidence=evidence, clock=clock, config=config
        ),
    )


_STARTUP_CODES = frozenset(
    {
        "K8S_CONFIG_MISSING",
        "K8S_CONFIG_INVALID",
        "JQ_NOT_FOUND",
        "PYTHON_NOT_FOUND",
        "ES_CONFIG_MISSING",
        "ES_CONFIG_INVALID",
        "LOKI_CONFIG_MISSING",
        "LOKI_CONFIG_INVALID",
        "VICTORIALOGS_CONFIG_MISSING",
        "VICTORIALOGS_CONFIG_INVALID",
        "SQL_CONFIG_MISSING",
        "SQL_CONFIG_INVALID",
        "SQL_TARGETS_OPSPILOT_DB",
        "MONGODB_CONFIG_MISSING",
        "MONGODB_CONFIG_INVALID",
        "MONGODB_TARGETS_OPSPILOT_DB",
        "KAFKA_CONFIG_MISSING",
        "KAFKA_CONFIG_INVALID",
    }
)


def _otel_demo_ext(env: Mapping[str, str]) -> ToolProfile:
    config = OtelDemoConfig.from_env(env)
    # A2-3: no OPSPILOT_K8S_* key is read, copied or iterated here -- the web
    # selects this profile too. Only the worker, when it builds the executor
    # factory, reads them from the same mapping (the process environment).
    # B1-U2: the Elasticsearch address alone decides the face and versions;
    # the web never reads an ``OPSPILOT_ES_*`` credential.
    elasticsearch = elasticsearch_enabled(env)
    # C5-14: likewise the Loki address; never an ``OPSPILOT_LOKI_*``
    # credential or tenant in the web.
    loki = loki_enabled(env)
    victorialogs = victorialogs_enabled(env)
    # B3-1: the switch only; it adds no credential or address.
    live_metrics = live_metrics_enabled(env)
    # C2b: the switch only; the worker reads knowledge with its own runtime
    # role, the web adds the runbook list to new Runs.
    knowledge = knowledge_tool_enabled(env)
    # C1 (#565): the switch only; the loop answers ``TodoWrite`` from the Run
    # input's tools, so the worker reads it only to record the same versions.
    todo = todo_write_enabled(env)
    # C6-13: the dialect from the address's scheme only; never an
    # ``OPSPILOT_SQL_*`` credential in the web.
    sql = sql_dialect(env)
    # C3-12: the bootstrap address only; never an ``OPSPILOT_KAFKA_*``
    # credential in the web.
    kafka = kafka_enabled(env)
    # C6c: the address only; never an ``OPSPILOT_MONGODB_*`` credential.
    mongodb = mongodb_enabled(env)

    def executor_factory(
        store: DurableStore, evidence: EvidenceSink, clock: Clock
    ) -> ExecutorFactory:
        # The cluster setting and jq are startup preconditions of this
        # worker: their fixed codes end the process like any other
        # misconfiguration (``K8S_CONFIG_MISSING``, ``K8S_CONFIG_INVALID``,
        # ``JQ_NOT_FOUND``; ``PYTHON_NOT_FOUND`` for the Pod log filters;
        # ``ES_CONFIG_MISSING`` / ``ES_CONFIG_INVALID`` for B1-4;
        # ``LOKI_CONFIG_MISSING`` / ``LOKI_CONFIG_INVALID`` and
        # ``VICTORIALOGS_CONFIG_MISSING`` / ``VICTORIALOGS_CONFIG_INVALID``
        # for C5-4; ``SQL_CONFIG_*`` and ``SQL_TARGETS_OPSPILOT_DB`` for
        # C6-7/C6-8; ``KAFKA_CONFIG_*`` for C3-4).
        try:
            return otel_demo_ext_executor_factory(
                store,
                evidence=evidence,
                clock=clock,
                config=config,
                kubernetes=KubernetesConfig.from_env(env),
                elasticsearch=ElasticsearchConfig.from_env(env),
                loki=LokiConfig.from_env(env),
                victorialogs=VictoriaLogsConfig.from_env(env),
                live_metrics=live_metrics,
                knowledge=knowledge,
                todo=todo,
                sql=SqlConfig.from_env(env),
                kafka=KafkaConfig.from_env(env),
                mongodb=MongoConfig.from_env(env),
            )
        except ToolContractError as error:
            code = str(error)
            if code in _STARTUP_CODES:
                raise SystemExit(code) from None
            raise

    return ToolProfile(
        name="otel-demo-ext",
        face=lambda clock: otel_demo_ext_face(
            clock,
            elasticsearch=elasticsearch,
            loki=loki,
            victorialogs=victorialogs,
            live_metrics=live_metrics,
            knowledge=knowledge,
            todo=todo,
            sql=sql,
            kafka=kafka,
            mongodb=mongodb,
        ),
        versions=lambda: otel_demo_ext_versions(
            elasticsearch=elasticsearch,
            loki=loki,
            victorialogs=victorialogs,
            live_metrics=live_metrics,
            knowledge=knowledge,
            todo=todo,
            sql=sql,
            kafka=kafka,
            mongodb=mongodb,
        ),
        runbook_catalog=knowledge,
        executor_factory=executor_factory,
    )


def select_profile(env: Mapping[str, str]) -> ToolProfile:
    """The profile ``env`` names; an unknown name is a startup error."""
    name = env.get(PROFILE_ENV) or "fixture"
    if name == "fixture":
        return _fixture()
    if name == "otel-demo":
        return _otel_demo(env)
    if name == "otel-demo-ext":
        return _otel_demo_ext(env)
    raise SystemExit(f"{PROFILE_ENV} must be one of {', '.join(PROFILE_NAMES)}")
