"""Building the Run input snapshot the runner rebuilds from (C3 §5).

``InvestigationRunner`` refuses a Run whose row carries no input
(``INPUT_MISSING``), so every path that creates a Run -- the workbench's
intake, ``new_run``, the renewal a note on a timed-out Run starts (#47), and
the bounded live scripts -- must record one, built the same way. This
module is that one way. It knows nothing about the tool source: the
``ToolFace`` is the model-visible half of a tool profile (schemas, evidence
context, discipline variant) and is supplied by the composition root.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from opspilot.investigation.context import (
    ContextError,
    InvestigationInput,
    continuation_context,
)
from opspilot.investigation.limits import M1_FROZEN_LIMITS, RunLimits

__all__ = ["ToolFace", "continuation_input"]


@dataclass(frozen=True)
class ToolFace:
    """What the model sees of a tool profile, independent of any Run.

    ``evidence_context`` builds the v4 evidence context a Run cites, keyed to
    that Run's own id (the loop's projection discards a context bound to a
    different Run).
    """

    tool_schemas: tuple[Mapping[str, Any], ...]
    variant_id: str
    evidence_context: Callable[[str], Mapping[str, Any] | None]

    def input_for(
        self,
        *,
        run_id: str,
        question: str,
        target_id: str,
        deadline: datetime,
        model_requests: int,
        limits: RunLimits = M1_FROZEN_LIMITS,
    ) -> InvestigationInput:
        """A fresh Run's input over ``question`` against one authorized target.

        ``bound_target_id`` and ``scope_facts`` record the intake's
        authorization facts for audit and continuation; the tool gateway
        re-issues authorization on every attempt from its own scope.
        """
        return InvestigationInput(
            question=question,
            model_requests=model_requests,
            limits=limits,
            tool_schemas=tuple(dict(item) for item in self.tool_schemas),
            evidence_context=self.evidence_context(run_id),
            variant_id=self.variant_id,
            bound_target_id=target_id,
            scope_facts={
                "target_ids": [target_id],
                "deadline": deadline.isoformat(),
            },
        )


def continuation_input(
    snapshot: Mapping[str, Any],
    *,
    new_run_id: str,
    deadline: datetime,
    authorized_targets: frozenset[str],
) -> InvestigationInput:
    """The successor Run's input: the C3 continuation of ``snapshot``'s Run.

    Same shape the bounded live runs use: the previous input with the
    carried evidence re-bound to the new Run id, the deterministic handoff
    note appended to the question, and the successor's own deadline in its
    scope facts (a renewed Run must not keep refusing on the old wall).
    Raises ``ContextError`` (``INPUT_MISSING`` when the previous Run has no
    snapshot) exactly as ``continuation_context`` does.
    """
    run = snapshot.get("run")
    if not isinstance(run, Mapping) or run.get("input") is None:
        raise ContextError("INPUT_MISSING")
    previous = InvestigationInput.from_json(run["input"])
    cont = continuation_context(
        snapshot, new_run_id=new_run_id, authorized_targets=authorized_targets
    )
    return replace(
        previous,
        question=f"{previous.question}\n\n{cont.handoff_note}",
        evidence_context=cont.evidence_context,
        scope_facts={**previous.scope_facts, "deadline": deadline.isoformat()},
    )
