"""Bounded offline loop comparison; never calls a model or tool endpoint."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
from dataclasses import dataclass
from typing import TypedDict

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
    """Run a real minimal LangGraph graph when the isolated extra is present."""
    if importlib.util.find_spec("langgraph") is None:
        raise RuntimeError("LANGGRAPH_EXTRA_UNAVAILABLE")

    from langgraph.graph import END, START, StateGraph

    class GraphState(TypedDict):
        steps: list[str]
        persistence_points: int
        cancelled: bool

    graph = StateGraph(GraphState)
    for index, tool in enumerate(TOOLS):

        def visit(state, tool=tool, index=index):
            return {
                "steps": [*state["steps"], tool],
                "persistence_points": state["persistence_points"] + 1,
                "cancelled": index + 1 == cancel_after,
            }

        graph.add_node(tool, visit)
    graph.add_edge(START, TOOLS[0])
    for index in range(cancel_after - 1):
        graph.add_edge(TOOLS[index], TOOLS[index + 1])
    graph.add_edge(TOOLS[cancel_after - 1], END)
    result = graph.compile().invoke(
        {"steps": [], "persistence_points": 0, "cancelled": False}
    )
    result.pop("__interrupt__", None)
    return State(
        steps=result["steps"],
        persistence_points=result["persistence_points"],
        cancelled=result["cancelled"],
    )


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
        result["reason"] = None
        result["langgraph_version"] = importlib.metadata.version("langgraph")
        result["decision"] = (
            "采用"
            if candidate.persistence_points < existing.persistence_points
            else "推迟"
        )
    return result


if __name__ == "__main__":
    print(json.dumps(compare(), ensure_ascii=False, indent=2, sort_keys=True))
