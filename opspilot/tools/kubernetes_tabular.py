"""Column queries over cluster objects: ``kubernetes_tabular_query`` (A6, #558).

Probe skeleton: the tool is registered in the ``otel-demo-ext`` profile and
every call is refused with a fixed code until the evaluator lands.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from opspilot.tools import otel_demo
from opspilot.tools.executor import TransportError, TransportRequest, TransportResponse
from opspilot.tools.kubernetes_core import (
    DEFAULT_NAMESPACE,
    ERROR_CLASSES,
    KubernetesClient,
    refused,
)
from opspilot.tools.registry import (
    MAX_VIEW_TOKENS,
    ParameterSpec,
    ToolDescription,
    ToolRegistration,
)

__all__ = [
    "TABULAR_TOOL",
    "TOOL_NAMES",
    "TOOL_SCHEMAS",
    "TabularQueryTransport",
    "tabular_registrations",
]

TABULAR_TOOL = "kubernetes_tabular_query"
TOOL_NAMES: tuple[str, ...] = (TABULAR_TOOL,)
REQUEST_TIMEOUT_SECONDS = 30.0
MAX_RESULT_BYTES = 2 * 1024 * 1024

TOOL_SCHEMAS: tuple[Mapping[str, Any], ...] = (
    {
        "type": "function",
        "function": {
            "name": TABULAR_TOOL,
            "description": "List cluster objects of one kind as a table.",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string"},
                    "columns": {"type": "string"},
                    "namespace": {"type": "string"},
                    "label_selector": {"type": "string"},
                    "field_selector": {"type": "string"},
                    "filter_pattern": {"type": "string"},
                },
                "required": ["kind", "columns"],
            },
        },
    },
)


class TabularQueryTransport:
    """Serves ``kubernetes_tabular_query`` over the A2 client."""

    def __init__(
        self,
        *,
        client: KubernetesClient,
        application_namespace: str = DEFAULT_NAMESPACE,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._client = client
        self._application_namespace = application_namespace
        self._monotonic = monotonic

    def fetch(self, request: TransportRequest) -> TransportResponse:
        if request.tool != TABULAR_TOOL:
            raise TransportError("TOOL_NOT_SUPPORTED")
        return refused("A6_NOT_IMPLEMENTED")


def tabular_registrations(*, max_window_seconds: int) -> tuple[ToolRegistration, ...]:
    return (
        ToolRegistration(
            name=TABULAR_TOOL,
            version="v1",
            source=otel_demo.SOURCE,
            verb="list",
            may_contain_secrets=True,
            description=ToolDescription(
                returns="A table of cluster objects.",
                window_format="The authorized absolute window is appended as {window}.",
                values_format="The authorized target is appended as {values}.",
                limits="Not implemented yet.",
                cannot_prove="Objects are state at query time.",
            ),
            parameters={
                "kind": ParameterSpec("string", required=True),
                "columns": ParameterSpec("string", required=True),
                "namespace": ParameterSpec("string"),
                "label_selector": ParameterSpec("string"),
                "field_selector": ParameterSpec("string"),
                "filter_pattern": ParameterSpec("string"),
            },
            result_path=("rows",),
            request_timeout_seconds=REQUEST_TIMEOUT_SECONDS,
            max_result_bytes=MAX_RESULT_BYTES,
            max_view_tokens=MAX_VIEW_TOKENS,
            max_window_seconds=max_window_seconds,
            error_classes={**ERROR_CLASSES, "A6_NOT_IMPLEMENTED": "SOURCE_ERROR"},
        ),
    )


def _registrations() -> tuple[ToolRegistration, ...]:
    """The registration under the profiles' 24 h frame (inventory seam)."""
    return tabular_registrations(max_window_seconds=otel_demo.OBSERVATION_SECONDS)
