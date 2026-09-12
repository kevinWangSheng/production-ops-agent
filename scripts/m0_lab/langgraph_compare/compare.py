"""Bounded offline loop comparison; never calls a model or tool endpoint."""

from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass

TOOLS = ("otel_services", "otel_traces", "otel_metrics")


@dataclass
class State:
    steps: list[str]
    persistence_points: int
    cancelled: bool


def run_existing(cancel_after: int = 2) -> State:
    state = State([], 0, False)
    for index, tool in enumerate(TOOLS, start=1):
        state.steps.append(tool)
        state.persistence_points += 1  # existing loop commits after each tool
        if index == cancel_after:
            state.cancelled = True
            break
    return state


def run_langgraph_minimal(cancel_after: int = 2) -> State:
    """Reserved seam; never claim a comparison without a real graph."""
    if importlib.util.find_spec("langgraph") is None:
        raise RuntimeError("LANGGRAPH_EXTRA_UNAVAILABLE")
    # Do not substitute the existing loop for a LangGraph implementation.  A
    # future extra install must first add a real graph candidate and its own
    # versioned state/persistence assertions.
    raise RuntimeError("LANGGRAPH_COMPARISON_NOT_IMPLEMENTED")


def compare() -> dict:
    existing = run_existing()
    result = {
        "scenario": {
            "tool_sequence": list(TOOLS),
            "cancel_after": 2,
            "model_http": 0,
            "tool_http": 0,
        },
        "existing_loop": existing.__dict__,
        "langgraph": None,
        "status": "blocked_dependency",
        "reason": "LANGGRAPH_EXTRA_UNAVAILABLE",
        "decision": "推迟",
    }
    try:
        candidate = run_langgraph_minimal()
    except RuntimeError as exc:
        result["reason"] = str(exc)
    else:
        result["langgraph"] = candidate.__dict__
        result["status"] = "compared"
        result["decision"] = (
            "采用"
            if candidate.persistence_points < existing.persistence_points
            else "推迟"
        )
    return result


if __name__ == "__main__":
    print(json.dumps(compare(), ensure_ascii=False, indent=2, sort_keys=True))
