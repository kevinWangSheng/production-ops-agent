"""Flash investigation loop: context, model turn, tool pairing, handoff.

Public surface is the loop, its request/outcome types, the model-client
seam, the L2 report contract, frozen per-Run ceilings, and the step-commit
adapters. Instruction text for L1 lives in ``opspilot.instructions``.
"""

from .client import DeepSeekClient
from .limits import (
    MAX_CONTEXT_TOKENS,
    MAX_HTTP_REQUEST_BYTES,
    MAX_HTTP_RESPONSE_BYTES,
    MAX_MODEL_REQUESTS_PER_RUN,
    MAX_OUTPUT_TOKENS,
    MAX_TOOL_OPERATIONS_PER_RUN,
    MAX_TOOL_SECONDS_PER_RUN,
    MODEL_REQUEST_TIMEOUT_SECONDS,
    RUN_WALL_SECONDS,
)
from .loop import (
    ACCEPTED_RESPONSE_MODEL,
    DISCIPLINE_VARIANT,
    InvestigationLoop,
    InvestigationRequest,
    LoopOutcome,
    ModelCall,
    ModelClient,
    ModelError,
    ModelReply,
)
from .messages import PairingError, pair_tool_results, validate_tool_calls
from .reports import (
    FINAL_REPORT_INSTRUCTION,
    REPORT_CONTRACT,
    REPORT_SCHEMA_VERSION,
    ReportV2,
    parse_report,
)
from .store import DurableStepStore, MemoryStepStore, StepCommitter, StepStoreError

__all__ = [
    "ACCEPTED_RESPONSE_MODEL",
    "DISCIPLINE_VARIANT",
    "FINAL_REPORT_INSTRUCTION",
    "MAX_CONTEXT_TOKENS",
    "MAX_HTTP_REQUEST_BYTES",
    "MAX_HTTP_RESPONSE_BYTES",
    "MAX_MODEL_REQUESTS_PER_RUN",
    "MAX_OUTPUT_TOKENS",
    "MAX_TOOL_OPERATIONS_PER_RUN",
    "MAX_TOOL_SECONDS_PER_RUN",
    "MODEL_REQUEST_TIMEOUT_SECONDS",
    "REPORT_CONTRACT",
    "REPORT_SCHEMA_VERSION",
    "RUN_WALL_SECONDS",
    "DeepSeekClient",
    "DurableStepStore",
    "InvestigationLoop",
    "InvestigationRequest",
    "LoopOutcome",
    "MemoryStepStore",
    "ModelCall",
    "ModelClient",
    "ModelError",
    "ModelReply",
    "PairingError",
    "ReportV2",
    "StepCommitter",
    "StepStoreError",
    "pair_tool_results",
    "parse_report",
    "validate_tool_calls",
]
