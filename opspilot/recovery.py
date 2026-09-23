"""Reconstruct a worker attempt from committed PostgreSQL business rows."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping
from uuid import UUID

from .domain import tool_operation_id
from .persistence import DurableStore


@dataclass(frozen=True)
class RecoveryPlan:
    incident_id: UUID
    run_id: UUID
    state: str
    control_generation: int
    run: Mapping[str, Any]
    steps: tuple[Mapping[str, Any], ...]
    pending_tools: tuple[Mapping[str, Any], ...]
    conclusion: Mapping[str, Any] | None

    @property
    def candidate(self) -> bool:
        return self.state in {"queued", "running"} and self.conclusion is None


def rebuild_plan(snapshot: Mapping[str, Any]) -> RecoveryPlan:
    data = _freeze(dict(snapshot))
    run = data["run"]
    return RecoveryPlan(
        data["incident_id"],
        run["run_id"],
        run["state"],
        int(data["control_generation"]),
        run,
        tuple(data.get("steps", ())),
        tuple(_identified(item) for item in data.get("pending_tools", ())),
        data.get("conclusion"),
    )


def _identified(pending: Mapping[str, Any]) -> Mapping[str, Any]:
    """Stamp the canonical operation id on a recovered call.

    A restart replays a call that may already have reached the outside world,
    so the id a gateway or the evidence store deduplicates by has to be the
    same one the original dispatch used. ``tool_operation_id`` is that single
    definition; deriving a second format here would make a replay look like a
    new operation and split its evidence history.
    """
    return MappingProxyType(
        {
            **pending,
            "operation_id": tool_operation_id(
                str(pending["step_id"]), int(pending["ordinal"])
            ),
        }
    )


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


def recover(store: DurableStore, incident_id: UUID) -> RecoveryPlan:
    return rebuild_plan(store.rebuild(incident_id))
