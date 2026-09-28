"""Frozen M1-01 per-Run resource ceilings.

Source: ``docs/testing/first-investigation-v4-2026-09-10.md``, user-approved
2026-09-13 (ROADMAP B2), superseded 2026-09-28 by the user decision recorded
in ``docs/tasks/2026-09-28-m1-01-loop-limits.md``: the per-Run budget
scaffolding is dropped and the loop is aligned with upstream HolmesGPT — one
anti-loop ceiling (the model-request count, default 100), no separate tool
call-count or cumulative tool-time ceiling. Raising a ceiling still needs a
new freeze, not a code change alone. Tool-side ceilings already live on the
gateway; this module holds the investigation-loop ceilings and re-exports the
tool ones so a caller can read one table.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

from opspilot.tools.executor import MAX_OPERATIONS_PER_RUN, MAX_TOOL_SECONDS_PER_RUN
from opspilot.tools.registry import MAX_REQUEST_TIMEOUT_SECONDS, MAX_RESULT_BYTES

# Per-Run model HTTP requests, including the reserved final-report request.
# 100, aligned with upstream HolmesGPT's default ``max_steps``
# (``holmes/config.py:112``) -- the loop's single anti-loop ceiling as of the
# 2026-09-28 user decision (docs/tasks/2026-09-28-m1-01-loop-limits.md, L1).
MAX_MODEL_REQUESTS_PER_RUN = 100

# Completion budget handed to the provider. 65_536 matches DeepSeek's
# thinking-mode default max output
# (https://api-docs.deepseek.com/api/create-chat-completion/), superseding
# the 2026-09-13 freeze of 16_384 (2026-09-28 user decision, L3). L3a (same
# decision, contract supplement) leaves this untouched -- only the context
# ceiling below changed.
MAX_OUTPUT_TOKENS = 65_536

# The full DeepSeek Flash context window (docs/tasks/2026-09-28-m1-01-loop-
# limits.md L3a): upstream HolmesGPT budgets input as
# "context window - reserved output" (``holmes/core/llm.py:787``), so this
# ceiling now tracks the provider's real window instead of the earlier
# 131_072 sketch L3 alone left in place.
#
# Sourced 2026-09-28 from https://api-docs.deepseek.com/quick_start/pricing/
# (checked live; the page carries no revision date): the "Models & Pricing"
# table's only two rows, deepseek-flash and deepseek-v4-pro, both list
# "CONTEXT LENGTH: 1M" and "MAXIMUM (output): 384K" -- neither model or an
# unabbreviated deepseek-v4.1-flash/deepseek-v4-flash row exists on the page,
# but ``ACCEPTED_RESPONSE_MODEL`` (opspilot/investigation/loop.py) is
# "deepseek-flash" and this is the only pricing row for it.
#
# The page gives context length only as the abbreviated "1M", never an exact
# digit count. L3a's original pass read DeepSeek's own K/M abbreviations as
# base-1024 (confirmed for "384K" == 393_216 by the companion API reference's
# exact digit count) and applied that same convention to "1M", landing on
# 1_048_576. The lead's 2026-09-28 batch-B supplement (B6,
# docs/tasks/2026-09-28-m1-01-upstream-alignment-b.md) revisits that specific
# extrapolation: unlike "384K", "1M" is never independently confirmed to an
# exact digit anywhere in DeepSeek's public docs, so assuming the same
# base-1024 reading for it is unverified. The conservative reading -- treat
# "1M" as the round decimal 1_000_000 absent an exact digit count -- is used
# here instead; it under-reads the true window if DeepSeek's "1M" is in fact
# 1_048_576, which only ever makes this ceiling stricter than the provider's
# real limit, never looser.
MAX_CONTEXT_TOKENS = 1_000_000

# Sized to a full-context request, not the earlier 512 KiB sketch (2026-09-28
# user decision, L3a): "1M" tokens contain roughly a factor-of-~2.4 more
# request bytes than the old 131_072-token context assumed. Calculated from
# this module's own estimator ratio (``_TOKENS_PER_BYTE = 0.25`` in
# opspilot/investigation/context.py, i.e. 4 bytes/token -- not a separately
# guessed ratio) applied to the input budget (context - output =
# 1_048_576 - 65_536 = 983_040 tokens): 983_040 * 4 == 3_932_160 raw content
# bytes, measured via ``serialized_request()`` on a single-message
# ``ModelCall`` sized to exactly that many estimated tokens at 3_932_333
# bytes once wrapped in the Chat Completions JSON envelope (173 bytes of
# per-request overhead beyond the raw content). A real Run's request has
# many messages -- system prompt, question, evidence context, a growing
# tool-call history -- whose per-message JSON structure adds more overhead
# than one padded blob does, so this ceiling must clear that measured floor
# with real margin, not sit right at it: 8 MiB (8_388_608 bytes), a little
# over double the measured single-message floor.
#
# Re-checked 2026-09-28 (B6): the input budget shrank to context - output =
# 1_000_000 - 65_536 = 934_464 tokens (smaller than the 983_040 this ceiling
# was measured against), so the measured floor above is itself now an
# over-estimate and this 8 MiB ceiling clears the real floor with more margin
# than before, not less. No value change needed.
MAX_HTTP_REQUEST_BYTES = 8 * 1024 * 1024
MAX_HTTP_RESPONSE_BYTES = 2 * 1024 * 1024

MODEL_REQUEST_TIMEOUT_SECONDS = 360.0
# A stuck-run backstop only (ADR-0005's timeout sweep), not a routine budget:
# 7200, up from the 2026-09-13 freeze of 1800 (2026-09-28 user decision, L4).
RUN_WALL_SECONDS = 7200.0


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
