"""Reconstruct a worker attempt from committed PostgreSQL business rows."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import UUID

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
    data = deepcopy(dict(snapshot))
    run = data["run"]
    return RecoveryPlan(
        data["incident_id"],
        run["run_id"],
        run["state"],
        int(data["control_generation"]),
        run,
        tuple(data.get("steps", ())),
        tuple(data.get("pending_tools", ())),
        data.get("conclusion"),
    )


def recover(store: DurableStore, incident_id: UUID) -> RecoveryPlan:
    return rebuild_plan(store.rebuild(incident_id))
