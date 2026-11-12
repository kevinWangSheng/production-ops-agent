"""PROBE ONLY (A2 full probe, not for merge): placeholder Kubernetes tools."""

from __future__ import annotations

from collections.abc import Mapping

from opspilot.tools.registry import ParameterSpec, ToolDescription, ToolRegistration

SOURCE = "kubernetes-api"
JQ_TOOL = "kubernetes_jq_query"
COUNT_TOOL = "kubernetes_count"
API_URL_ENV = "OPSPILOT_K8S_API_URL"


def api_url(env: Mapping[str, str]) -> str | None:
    return env.get("OPSPILOT_K8S_API_URL")


def _registrations() -> tuple[ToolRegistration, ...]:
    return tuple(
        ToolRegistration(
            name=name,
            version="v1",
            source=SOURCE,
            verb="list",
            parameters={
                "kind": ParameterSpec("string", required=True),
                "jq_expr": ParameterSpec("string", required=True),
                "namespace": ParameterSpec("string"),
            },
            result_path=("items",),
            request_timeout_seconds=30.0,
            max_result_bytes=2 * 1024 * 1024,
            max_view_tokens=25_000,
            max_window_seconds=3600,
            description=ToolDescription(
                returns="placeholder",
                window_format="query time {window}",
                values_format="{values}",
                limits="placeholder",
                cannot_prove="state inside the incident window",
            ),
            may_contain_secrets=True,
            error_classes={
                "403": "SOURCE_FORBIDDEN",
                "SECRET_KIND": "RESOURCE_NOT_ALLOWED",
            },
        )
        for name in (JQ_TOOL, COUNT_TOOL)
    )
