"""Frozen M1-01 per-Run resource ceilings.

Source: ``docs/testing/first-investigation-v4-2026-09-10.md``, user-approved
2026-09-13 (ROADMAP B2). Raising a ceiling needs a new freeze, not a code
change alone. Tool-side ceilings already live on the gateway; this module
holds the investigation-loop ceilings and re-exports the tool ones so a
caller can read one table.
"""

from __future__ import annotations

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

# Re-exported so tests and callers do not have to remember two modules.
MAX_TOOL_OPERATIONS_PER_RUN = MAX_OPERATIONS_PER_RUN
MAX_TOOL_TIMEOUT_SECONDS = MAX_REQUEST_TIMEOUT_SECONDS
MAX_TOOL_RESULT_BYTES = MAX_RESULT_BYTES

__all__ = [
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
