"""Prometheus instant query, series and label-name discovery (tool expansion A5).

Probe skeleton: the three tools are registered in the ``otel-demo-ext``
profile; the transport refuses every call until the implementation lands.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from opspilot.tools import otel_demo
from opspilot.tools.executor import TransportError, TransportRequest, TransportResponse
from opspilot.tools.registry import ParameterSpec, ToolDescription, ToolRegistration

__all__ = [
    "INSTANT_QUERY_TOOL",
    "LABELS_TOOL",
    "PROMETHEUS_EXT_TOOLS",
    "SERIES_TOOL",
    "TOOL_NAMES",
    "TOOL_SCHEMAS",
    "PrometheusExtTransport",
    "prometheus_ext_registrations",
]

INSTANT_QUERY_TOOL = "execute_prometheus_instant_query"
SERIES_TOOL = "get_series"
LABELS_TOOL = "get_all_labels"
TOOL_NAMES: tuple[str, ...] = (INSTANT_QUERY_TOOL, SERIES_TOOL, LABELS_TOOL)
PROMETHEUS_EXT_TOOLS: frozenset[str] = frozenset(TOOL_NAMES)


def _schema(
    name: str, properties: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "probe skeleton",
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


_STRING = {"type": "string"}
TOOL_SCHEMAS: tuple[Mapping[str, Any], ...] = (
    _schema(INSTANT_QUERY_TOOL, {"query": _STRING, "time": _STRING}, ["query"]),
    _schema(
        SERIES_TOOL, {"match": _STRING, "start": _STRING, "end": _STRING}, ["match"]
    ),
    _schema(LABELS_TOOL, {"match": _STRING, "start": _STRING, "end": _STRING}, []),
)


def prometheus_ext_registrations() -> tuple[ToolRegistration, ...]:
    description = ToolDescription(
        returns="probe",
        window_format="{window}",
        values_format="{values}",
        limits="probe",
        cannot_prove="probe",
    )
    common: dict[str, Any] = {
        "version": "v1",
        "source": otel_demo.SOURCE,
        "may_contain_secrets": False,
        "description": description,
        "request_timeout_seconds": 30.0,
        "max_result_bytes": 1024 * 1024,
        "max_view_tokens": otel_demo.MAX_VIEW_TOKENS,
        "max_window_seconds": otel_demo.OBSERVATION_SECONDS,
        "incomplete_marker": "warnings",
        "error_classes": {"400": "INVALID_PARAMS", "503": "SOURCE_UNAVAILABLE"},
    }
    window = {"start": ParameterSpec("string"), "end": ParameterSpec("string")}
    return (
        ToolRegistration(
            name=INSTANT_QUERY_TOOL,
            verb="query",
            parameters={
                "query": ParameterSpec("string", required=True),
                "time": ParameterSpec("string"),
            },
            result_path=("data", "result"),
            **common,
        ),
        ToolRegistration(
            name=SERIES_TOOL,
            verb="list",
            parameters={"match": ParameterSpec("string", required=True), **window},
            result_path=("data",),
            **common,
        ),
        ToolRegistration(
            name=LABELS_TOOL,
            verb="list",
            parameters={"match": ParameterSpec("string"), **window},
            result_path=("data",),
            **common,
        ),
    )


class PrometheusExtTransport:
    def __init__(self, base: otel_demo.OtelDemoTransport) -> None:
        self._base = base

    def fetch(self, request: TransportRequest) -> TransportResponse:
        raise TransportError("TOOL_NOT_IMPLEMENTED")
