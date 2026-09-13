"""Frozen read-only replay tool face shared by the upstream and candidate arms.

Standard library only so the same module runs inside a mount-free container.
Tools return only views frozen in the packet; nothing is queried live.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

TOOL_NAMES = ("otel_services", "otel_metrics", "otel_logs", "otel_traces")
MAX_TOOL_CALLS = 20
PACKET_VERSION = "m0-r07-replay-packet-v1"


def canonical_hash(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


def load_packet(path: Path) -> dict:
    packet = json.loads(Path(path).read_text())
    if packet.get("packet_version") != PACKET_VERSION:
        raise ValueError("unsupported packet version")
    for key in ("packet_id", "question", "scope", "views"):
        if key not in packet:
            raise ValueError(f"packet missing {key}")
    scope = packet["scope"]
    if not isinstance(scope.get("services"), list) or not scope["services"]:
        raise ValueError("packet scope services required")
    window = scope.get("window", {})
    if not (
        isinstance(window.get("start"), (int, float))
        and isinstance(window.get("end"), (int, float))
    ):
        raise ValueError("packet window required")
    for view in packet["views"]:
        if view.get("tool") not in TOOL_NAMES:
            raise ValueError("packet view tool invalid")
        if canonical_hash(
            {k: view[k] for k in ("evidence_id", "tool", "query", "data")}
        ) != view.get("view_sha256"):
            raise ValueError(f"packet view hash mismatch: {view.get('evidence_id')}")
    return packet


def available_queries(packet: dict) -> dict:
    result = {"otel_metrics": [], "otel_logs": [], "otel_traces": []}
    for view in packet["views"]:
        if view["tool"] == "otel_metrics":
            result["otel_metrics"].append(view["query"]["query"])
        elif view["tool"] in ("otel_logs", "otel_traces"):
            result[view["tool"]].append(view["query"]["service"])
    return result


def tool_definitions(packet: dict) -> list[dict]:
    """Name, description and parameter schema; identical for both arms."""
    queries = available_queries(packet)
    scope = packet["scope"]
    window = f"{scope['window']['start']}..{scope['window']['end']}"
    return [
        {
            "name": "otel_services",
            "description": (
                "List the authorized services and instance identity of the fixed read-only "
                f"OpenTelemetry Demo instance {scope['integration_id']}. Listing does not prove health."
            ),
            "parameters": {},
        },
        {
            "name": "otel_metrics",
            "description": (
                "Return a frozen Prometheus instant-query result evaluated at the window end for the fixed window "
                f"{window}. Only these exact PromQL strings are available in this read-only replay set: "
                + json.dumps(queries["otel_metrics"])
                + ". Any other query returns an error."
            ),
            "parameters": {
                "query": {
                    "type": "string",
                    "description": "Exact PromQL string from the available list.",
                }
            },
        },
        {
            "name": "otel_logs",
            "description": (
                f"Return up to 20 frozen log rows for one service within the fixed window {window}. "
                "Available services: " + json.dumps(queries["otel_logs"]) + "."
            ),
            "parameters": {
                "service": {
                    "type": "string",
                    "description": "Exact service name from the available list.",
                }
            },
        },
        {
            "name": "otel_traces",
            "description": (
                f"Return a frozen trace summary (trace ids and per-service span/error counts) for one service within the fixed window {window}. "
                "Available services: " + json.dumps(queries["otel_traces"]) + "."
            ),
            "parameters": {
                "service": {
                    "type": "string",
                    "description": "Exact service name from the available list.",
                }
            },
        },
    ]


def openai_tool_schemas(packet: dict) -> list[dict]:
    schemas = []
    for tool in tool_definitions(packet):
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": {
                        "type": "object",
                        "properties": tool["parameters"],
                        "required": list(tool["parameters"]),
                    },
                },
            }
        )
    return schemas


class ReplayState:
    def __init__(self, packet: dict):
        self.packet = packet
        self.calls: list[dict] = []

    def dispatch(self, name: str, params) -> tuple[bool, dict]:
        """Return (ok, view). Views are the frozen packet views; errors carry no data."""
        if not isinstance(params, dict):
            params = {}
        record = {"ordinal": len(self.calls) + 1, "tool": name, "params": params}
        self.calls.append(record)
        if len(self.calls) > MAX_TOOL_CALLS:
            record["error"] = "query budget exhausted"
            return False, {"error": record["error"]}
        if name not in TOOL_NAMES:
            record["error"] = "tool interface scope denied"
            return False, {"error": record["error"]}
        scope = self.packet["scope"]
        if name == "otel_services":
            view = {
                "evidence_id": f"{self.packet['packet_id']}-services",
                "tool": name,
                "data": {
                    "integration_id": scope["integration_id"],
                    "services": scope["services"],
                    "window": scope["window"],
                },
            }
            record["evidence_id"] = view["evidence_id"]
            return True, view
        allowed = {
            "otel_metrics": {"query"},
            "otel_logs": {"service"},
            "otel_traces": {"service"},
        }[name]
        if set(params) - allowed or not allowed <= set(params):
            record["error"] = "tool arguments denied"
            return False, {
                "error": record["error"],
                "expected_parameters": sorted(allowed),
            }
        for view in self.packet["views"]:
            if view["tool"] == name and view["query"] == {
                k: params[k] for k in allowed
            }:
                record["evidence_id"] = view["evidence_id"]
                return True, {
                    k: view[k] for k in ("evidence_id", "tool", "query", "data")
                }
        record["error"] = "query not in frozen replay set"
        return False, {
            "error": record["error"],
            "available": available_queries(self.packet)[name],
        }
