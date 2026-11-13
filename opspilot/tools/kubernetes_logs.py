"""Pod logs: ``fetch_pod_logs`` (tool expansion A3, #555) -- probe skeleton."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from opspilot.tools.executor import (
    TransportError,
    TransportRequest,
    TransportResponse,
)
from opspilot.tools.kubernetes_core import ERROR_CLASSES, refused
from opspilot.tools.otel_demo import SOURCE
from opspilot.tools.registry import (
    MAX_VIEW_TOKENS,
    ParameterSpec,
    ToolDescription,
    ToolRegistration,
)

__all__ = [
    "POD_LOGS_TOOL",
    "TOOL_NAMES",
    "TOOL_SCHEMAS",
    "PodLogsTransport",
    "pod_logs_registrations",
]

POD_LOGS_TOOL = "fetch_pod_logs"
TOOL_NAMES: tuple[str, ...] = (POD_LOGS_TOOL,)
REQUEST_TIMEOUT_SECONDS = 30.0
MAX_RESULT_BYTES = 2 * 1024 * 1024

TOOL_SCHEMAS: tuple[Mapping[str, Any], ...] = (
    {
        "type": "function",
        "function": {
            "name": POD_LOGS_TOOL,
            "description": "Fetch the logs of every container of one pod.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pod_name": {"type": "string"},
                    "namespace": {"type": "string"},
                    "start_time": {"type": "string"},
                    "end_time": {"type": "string"},
                    "limit": {"type": "integer"},
                    "filter": {"type": "string"},
                    "exclude_filter": {"type": "string"},
                },
                "required": ["pod_name", "namespace"],
            },
        },
    },
)


class PodLogsTransport:
    """Probe skeleton: refuses every call before anything is sent."""

    def fetch(self, request: TransportRequest) -> TransportResponse:
        if request.tool != POD_LOGS_TOOL:
            raise TransportError("TOOL_NOT_SUPPORTED")
        return refused("NOT_IMPLEMENTED")


def pod_logs_registrations(*, max_window_seconds: int) -> tuple[ToolRegistration, ...]:
    return (
        ToolRegistration(
            name=POD_LOGS_TOOL,
            version="v1",
            source=SOURCE,
            verb="logs",
            may_contain_secrets=True,
            description=ToolDescription(
                returns="Pod log lines.",
                window_format="The authorized window is appended as {window}.",
                values_format="The authorized target is appended as {values}.",
                limits="Probe skeleton.",
                cannot_prove="Probe skeleton.",
            ),
            parameters={
                "pod_name": ParameterSpec("string", required=True),
                "namespace": ParameterSpec("string", required=True),
                "start_time": ParameterSpec("string"),
                "end_time": ParameterSpec("string"),
                "limit": ParameterSpec("integer"),
                "filter": ParameterSpec("string"),
                "exclude_filter": ParameterSpec("string"),
            },
            result_path=("lines",),
            request_timeout_seconds=REQUEST_TIMEOUT_SECONDS,
            max_result_bytes=MAX_RESULT_BYTES,
            max_view_tokens=MAX_VIEW_TOKENS,
            max_window_seconds=max_window_seconds,
            error_classes={**ERROR_CLASSES, "NOT_IMPLEMENTED": "INVALID_PARAMS"},
        ),
    )
