"""Frozen M1-01 per-Run resource ceilings.

Source: ``docs/testing/first-investigation-v4-2026-09-10.md``, user-approved
2026-09-13 (ROADMAP B2). Raising a ceiling needs a new freeze, not a code
change alone. Tool-side ceilings already live on the gateway; this module
holds the investigation-loop ceilings and re-exports the tool ones so a
caller can read one table.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

from opspilot.tools.executor import MAX_OPERATIONS_PER_RUN, MAX_TOOL_SECONDS_PER_RUN
from opspilot.tools.registry import MAX_REQUEST_TIMEOUT_SECONDS, MAX_RESULT_BYTES

# Per-Run model HTTP requests, including the reserved final-report request.
MAX_MODEL_REQUESTS_PER_RUN = 4

# Completion budget handed to the provider. Distinct from the 131072 context
# window; the 2026-09-13 freeze is 16_384, not the earlier 32_768 sketch.
MAX_OUTPUT_TOKENS = 16_384
MAX_CONTEXT_TOKENS = 131_072

MAX_HTTP_REQUEST_BYTES = 512 * 1024
MAX_HTTP_RESPONSE_BYTES = 2 * 1024 * 1024

MODEL_REQUEST_TIMEOUT_SECONDS = 360.0
RUN_WALL_SECONDS = 1800.0


@dataclass(frozen=True)
class RunLimits:
    """Per-Run resource ceilings the loop enforces; the M1 freeze is one instance.

    The loop takes these as a parameter so a deterministic test can drive more
    logical rounds than the frozen value allows. The product boundary (the
    runner that admits a Run) must refuse any instance that is not
    ``within(M1_FROZEN_LIMITS)``; raising the freeze remains a user decision.
    Tool ceilings stay on the tool gateway scope and are not repeated here.
    """

    model_requests: int = MAX_MODEL_REQUESTS_PER_RUN
    output_tokens: int = MAX_OUTPUT_TOKENS
    context_tokens: int = MAX_CONTEXT_TOKENS
    request_bytes: int = MAX_HTTP_REQUEST_BYTES
    model_request_timeout_seconds: float = MODEL_REQUEST_TIMEOUT_SECONDS
    active_seconds: float = RUN_WALL_SECONDS

    def __post_init__(self) -> None:
        for name in (
            "model_requests",
            "output_tokens",
            "context_tokens",
            "request_bytes",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError("INVALID_LIMITS")
        for name in ("model_request_timeout_seconds", "active_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("INVALID_LIMITS")
            if not value > 0 or value != value or value in (float("inf"),):
                raise ValueError("INVALID_LIMITS")

    def within(self, ceiling: "RunLimits") -> bool:
        """True when no dimension exceeds ``ceiling``."""
        return all(
            getattr(self, item.name) <= getattr(ceiling, item.name)
            for item in fields(self)
        )

    def as_json(self) -> dict[str, Any]:
        return {item.name: getattr(self, item.name) for item in fields(self)}

    @classmethod
    def from_json(cls, value: object) -> "RunLimits":
        if not isinstance(value, dict):
            raise ValueError("INVALID_LIMITS")
        names = {item.name for item in fields(cls)}
        if set(value) != names:
            raise ValueError("INVALID_LIMITS")
        return cls(**{name: value[name] for name in names})


M1_FROZEN_LIMITS = RunLimits()

# Re-exported so tests and callers do not have to remember two modules.
MAX_TOOL_OPERATIONS_PER_RUN = MAX_OPERATIONS_PER_RUN
MAX_TOOL_TIMEOUT_SECONDS = MAX_REQUEST_TIMEOUT_SECONDS
MAX_TOOL_RESULT_BYTES = MAX_RESULT_BYTES

__all__ = [
    "M1_FROZEN_LIMITS",
    "RunLimits",
    "MAX_CONTEXT_TOKENS",
    "MAX_HTTP_REQUEST_BYTES",
    "MAX_HTTP_RESPONSE_BYTES",
    "MAX_MODEL_REQUESTS_PER_RUN",
    "MAX_OUTPUT_TOKENS",
    "MAX_TOOL_OPERATIONS_PER_RUN",
    "MAX_TOOL_RESULT_BYTES",
    "MAX_TOOL_SECONDS_PER_RUN",
    "MAX_TOOL_TIMEOUT_SECONDS",
    "MODEL_REQUEST_TIMEOUT_SECONDS",
    "RUN_WALL_SECONDS",
]
