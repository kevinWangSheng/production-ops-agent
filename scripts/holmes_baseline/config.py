"""HolmesGPT ``a0009195`` configuration for the F14 baseline (U3, U4, U6).

Only ``prometheus/metrics`` (through the lab proxy's ``holmes`` account) and
one GET-only ``http`` toolset on the Jaeger query API are enabled. ``holmes
ask`` enables every builtin toolset it can (``enable_all_toolsets_possible``),
so every other builtin of the pinned image is named here and disabled.
Model and budgets match OpsPilot through configuration and environment only;
the upstream code is not modified.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

UPSTREAM_COMMIT = "a0009195468ebc55a7dff7d602b61feabe3cda1b"
# LiteLLM's DeepSeek provider, the model OpsPilot requests
# (``opspilot.investigation.loop.ACCEPTED_RESPONSE_MODEL``) at api.deepseek.com.
MODEL = "deepseek/deepseek-flash"
MODEL_HOST = "api.deepseek.com"
# OpsPilot's single anti-loop ceiling (``limits.MAX_MODEL_REQUESTS_PER_RUN``)
# equals Holmes' default ``max_steps``. Holmes does not count compaction or
# retries as steps; OpsPilot counts every model request (disclosed, U6).
MAX_STEPS = 100
# ``limits.RUN_WALL_SECONDS``; Holmes has no Run wall clock, the harness kills
# the container at this deadline (R11: budget_exhausted).
RUN_WALL_SECONDS = 7200
# ``opspilot.tools.registry`` per-tool timeout.
TOOL_TIMEOUT_SECONDS = 30
# Environment the container gets for the model (holmes/common/env_vars.py,
# holmes/core/llm.py at a0009195). At this commit MAX_OUTPUT_TOKEN_RESERVATION
# is read but never used; the request's max_tokens is OVERRIDE_MAX_OUTPUT_TOKEN.
MODEL_ENV = {
    "THINKING": json.dumps({"type": "enabled"}),
    "REASONING_EFFORT": "high",
    "OVERRIDE_MAX_OUTPUT_TOKEN": "65536",  # limits.MAX_OUTPUT_TOKENS
    "OVERRIDE_MAX_CONTENT_SIZE": "1000000",  # limits.MAX_CONTEXT_TOKENS
    "LLM_REQUEST_TIMEOUT": "360",  # limits.MODEL_REQUEST_TIMEOUT_SECONDS
    # OpsPilot sends no temperature (loop.serialized_request); Holmes' default
    # is 1e-8 unless this is "none"
    "TEMPERATURE": "none",
    "ENABLE_TELEMETRY": "false",
}
# Every builtin toolset name ``load_builtin_toolsets()`` returns in the pinned
# image (``python -m scripts.holmes_baseline.run toolsets`` on image
# sha256:3f2995d1a448..., 48 names; recorded in pin.json).
BUILTIN_TOOLSETS: tuple[str, ...] = (
    "MongoDBAtlas",
    "aks/core",
    "aks/node-health",
    "argocd/core",
    "bash",
    "cilium/core",
    "confluence",
    "connectivity_check",
    "coralogix",
    "core_investigation",
    "crossplane/core",
    "database/sql",
    "datadog/general",
    "datadog/logs",
    "datadog/metrics",
    "datadog/traces",
    "docker/core",
    "elasticsearch/cluster",
    "elasticsearch/data",
    "grafana/dashboards",
    "grafana/loki",
    "grafana/tempo",
    "helm/core",
    "hubble/observability",
    "inspektor-gadget/node",
    "inspektor-gadget/tcpdump",
    "internet",
    "kafka/admin",
    "kubernetes/core",
    "kubernetes/kube-prometheus-stack",
    "kubernetes/live-metrics",
    "kubernetes/logs",
    "kubevela/core",
    "newrelic",
    "notion",
    "opensearch/query_assist",
    "openshift/core",
    "openshift/live-metrics",
    "openshift/logs",
    "openshift/security",
    "prometheus/metrics",
    "prometheus/multi-tenant-metrics",
    "rabbitmq/core",
    "robusta",
    "servicenow/tables",
    "skills",
    "slab",
    "victorialogs",
)
ENABLED = frozenset({"prometheus/metrics"})
JAEGER_TOOLSET = "jaeger"


def _jaeger(jaeger_url: str) -> dict[str, Any]:
    parts = urlsplit(jaeger_url)
    base = parts.path.rstrip("/")
    return {
        "type": "http",
        "enabled": True,
        "config": {
            "endpoints": [
                {
                    "hosts": [parts.netloc],
                    "paths": [f"{base}/api/*"],
                    "methods": ["GET"],
                }
            ],
            "timeout_seconds": TOOL_TIMEOUT_SECONDS,
        },
        # where the API lives; the same facts OpsPilot's trace tool is built on
        "llm_instructions": (
            "Jaeger query API (read-only, GET only). "
            f"Base URL: {jaeger_url.rstrip('/')}. "
            "GET <base>/api/services lists services; "
            "GET <base>/api/traces?service=<name>&start=<unix microseconds>"
            "&end=<unix microseconds>&limit=<n>&tags=<url-encoded JSON> "
            "searches traces; GET <base>/api/traces/<traceID> fetches one trace."
        ),
    }


def holmes_config(prometheus_url: str, jaeger_url: str) -> dict[str, Any]:
    """The ``--config`` file content. The proxy credential is an env
    placeholder Holmes resolves at load time, never a value in the file."""
    toolsets: dict[str, dict[str, Any]] = {
        name: {"enabled": False} for name in BUILTIN_TOOLSETS if name not in ENABLED
    }
    toolsets["prometheus/metrics"] = {
        "enabled": True,
        "config": {
            "prometheus_url": prometheus_url,
            "additional_headers": {
                "Authorization": "{{ env.HOLMES_PROMETHEUS_AUTHORIZATION }}"
            },
            "query_timeout_seconds_default": TOOL_TIMEOUT_SECONDS,
            "query_timeout_seconds_hard_max": TOOL_TIMEOUT_SECONDS,
        },
    }
    toolsets[JAEGER_TOOLSET] = _jaeger(jaeger_url)
    return {"model": MODEL, "max_steps": MAX_STEPS, "toolsets": toolsets}
