"""Jaeger trace tools of the extended profile (tool expansion A4, #556).

Probe skeleton: the four tool names are registered in ``otel-demo-ext``; the
transport answers every call with one fixed error until the implementation
lands.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from opspilot.tools import otel_demo
from opspilot.tools.executor import TransportRequest, TransportResponse
from opspilot.tools.registry import (
    MAX_VIEW_TOKENS,
    ParameterSpec,
    ToolDescription,
    ToolRegistration,
    canonical,
)

__all__ = [
    "JAEGER_TOOLS",
    "LIST_OPERATIONS_TOOL",
    "LIST_SERVICES_TOOL",
    "SEARCH_BY_TAGS_TOOL",
    "TOOL_NAMES",
    "TOOL_SCHEMAS",
    "TRACE_BY_ID_TOOL",
    "TRACE_PROJECTION",
    "JaegerQueryTransport",
    "jaeger_registrations",
]

SEARCH_BY_TAGS_TOOL = "traces_search_by_tags"
TRACE_BY_ID_TOOL = "traces_query_trace_by_id"
LIST_SERVICES_TOOL = "traces_list_services"
LIST_OPERATIONS_TOOL = "traces_list_operations"
TOOL_NAMES: tuple[str, ...] = (
    SEARCH_BY_TAGS_TOOL,
    TRACE_BY_ID_TOOL,
    LIST_SERVICES_TOOL,
    LIST_OPERATIONS_TOOL,
)
JAEGER_TOOLS: frozenset[str] = frozenset(TOOL_NAMES)
TRACE_PROJECTION = "otel-demo-ext-traces-v1"


def _schema(name: str, properties: Mapping[str, Any], required: list[str]) -> Any:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Not implemented yet.",
            "parameters": {
                "type": "object",
                "properties": dict(properties),
                "required": required,
            },
        },
    }


TOOL_SCHEMAS: tuple[Mapping[str, Any], ...] = (
    _schema(SEARCH_BY_TAGS_TOOL, {"service": {"type": "string"}}, ["service"]),
    _schema(TRACE_BY_ID_TOOL, {"trace_id": {"type": "string"}}, ["trace_id"]),
    _schema(LIST_SERVICES_TOOL, {}, []),
    _schema(LIST_OPERATIONS_TOOL, {"service": {"type": "string"}}, ["service"]),
)


def jaeger_registrations(*, max_window_seconds: int) -> tuple[ToolRegistration, ...]:
    def registration(
        name: str, parameters: dict[str, ParameterSpec]
    ) -> ToolRegistration:
        return ToolRegistration(
            name=name,
            version="v1",
            source=otel_demo.SOURCE,
            verb="query",
            description=ToolDescription(
                returns="Not implemented yet.",
                window_format="The authorized window, appended as {window}.",
                values_format="The authorized target, appended as {values}.",
                limits="Not implemented yet.",
                cannot_prove="Not implemented yet.",
            ),
            may_contain_secrets=False,
            parameters=parameters,
            result_path=("data",),
            request_timeout_seconds=30.0,
            max_result_bytes=1024 * 1024,
            max_view_tokens=MAX_VIEW_TOKENS,
            max_window_seconds=max_window_seconds,
            error_classes={"NOT_IMPLEMENTED": "SOURCE_ERROR"},
        )

    service = {"service": ParameterSpec("string", required=True)}
    return (
        registration(SEARCH_BY_TAGS_TOOL, dict(service)),
        registration(
            TRACE_BY_ID_TOOL, {"trace_id": ParameterSpec("string", required=True)}
        ),
        registration(LIST_SERVICES_TOOL, {}),
        registration(LIST_OPERATIONS_TOOL, dict(service)),
    )


class JaegerQueryTransport(otel_demo.OtelDemoTransport):
    """Probe skeleton: every call is one fixed error, nothing is sent."""

    def fetch(self, request: TransportRequest) -> TransportResponse:
        return TransportResponse(
            body=canonical({"error": "NOT_IMPLEMENTED"}).encode(),
            source_status="NOT_IMPLEMENTED",
            sent=False,
        )
