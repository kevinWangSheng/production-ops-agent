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
from datetime import datetime, timedelta, timezone
from typing import Any

from opspilot.investigation.context import (
    ALERT_STARTS_AT,
    ContextError,
    InvestigationInput,
    continuation_context,
)
from opspilot.investigation.limits import M1_FROZEN_LIMITS, RunLimits

__all__ = [
    "ALERT_ANCHOR_RULE",
    "ALERT_FRAME_SECONDS",
    "AlertAnchor",
    "ToolFace",
    "continuation_input",
]

#: F1: an alert Run's authorized frame is the fixed 24 h ending when the
#: alert was received -- the same length and place as a submitted Run's.
ALERT_FRAME_SECONDS = 24 * 3600
#: How far before the anchor the default query window starts (J3), the
#: upstream one-hour default moved from the frame's end to the anchor.
ALERT_DEFAULT_LOOKBACK_SECONDS = 3600
#: Names what fixed ``anchor`` in an alert Run's time policy (J3).
ALERT_ANCHOR_RULE = "alert_starts_at"


def _utc_seconds(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(microsecond=0)


@dataclass(frozen=True)
class AlertAnchor:
    """The alert facts an alert Run's frame and anchor derive from (J1-J2).

    ``received_at`` is the database instant the alert was first received
    (the incident's earliest delivery), ``starts_at`` its normalized
    ``startsAt`` and ``original`` the ``startsAt`` text as Alertmanager sent
    it. Everything else is computed, so a fresh rebuild (J5) from the same
    three facts yields the same frame and anchor byte for byte.
    """

    received_at: datetime
    starts_at: datetime
    original: str

    def __post_init__(self) -> None:
        for moment in (self.received_at, self.starts_at):
            if not isinstance(moment, datetime) or moment.utcoffset() is None:
                raise ContextError("INPUT_INVALID")
        if not isinstance(self.original, str):
            raise ContextError("INPUT_INVALID")

    @property
    def frame(self) -> tuple[datetime, datetime]:
        """J1: ``[received_at - 24h, received_at]``, whole UTC seconds."""
        end = _utc_seconds(self.received_at)
        return end - timedelta(seconds=ALERT_FRAME_SECONDS), end

    @property
    def anchor(self) -> tuple[datetime, str | None]:
        """J2: ``startsAt`` clamped into the frame, and which edge clamped it."""
        start, end = self.frame
        moment = _utc_seconds(self.starts_at)
        if moment > end:
            return end, "future"
        if moment < start:
            return start, "before_frame"
        return moment, None

    @property
    def default_query_window(self) -> tuple[datetime, datetime]:
        """J3: from an hour before the anchor (not before the frame) to the
        frame's end."""
        start, end = self.frame
        anchor, _ = self.anchor
        return (
            max(anchor - timedelta(seconds=ALERT_DEFAULT_LOOKBACK_SECONDS), start),
            end,
        )

    def scope_fact(self) -> dict[str, Any]:
        anchor, adjusted = self.anchor
        return {
            "anchor": _iso(anchor),
            "original": self.original,
            "adjusted": adjusted,
        }

    def anchored(self, context: Mapping[str, Any] | None) -> dict[str, Any]:
        """``context`` with its first time policy moved onto this frame (J3).

        The face's own policy (id, mode, targets, reference rule) is kept;
        only its window is replaced and the anchor and default query window
        added. A face with no time policy cannot carry an anchor.
        """
        if not isinstance(context, Mapping):
            raise ContextError("INPUT_INVALID")
        policies = context.get("time_policies")
        if not isinstance(policies, list) or not policies:
            raise ContextError("INPUT_INVALID")
        first = policies[0]
        if not isinstance(first, Mapping):
            raise ContextError("INPUT_INVALID")
        start, end = self.frame
        default_start, default_end = self.default_query_window
        anchor, _ = self.anchor
        policy = {
            **first,
            "window": {"start": _iso(start), "end": _iso(end)},
            "anchor": _iso(anchor),
            "anchor_rule": ALERT_ANCHOR_RULE,
            "default_query_window": {
                "start": _iso(default_start),
                "end": _iso(default_end),
            },
        }
        return {**context, "time_policies": [policy, *policies[1:]]}


def _iso(moment: datetime) -> str:
    # The form ``Window.as_json`` writes, so the transport parses it back.
    return moment.isoformat()


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
        alert: AlertAnchor | None = None,
    ) -> InvestigationInput:
        """A fresh Run's input over ``question`` against one authorized target.

        ``bound_target_id`` and ``scope_facts`` record the intake's
        authorization facts for audit and continuation; the tool gateway
        re-issues authorization on every attempt from its own scope.
        ``alert`` (an Alertmanager Run, M1-04 J1-J3) fixes the frame at the
        alert's receipt instead of the face's clock and anchors the default
        query window at its ``startsAt``; the input is then v3.
        """
        context = self.evidence_context(run_id)
        scope_facts: dict[str, Any] = {
            "target_ids": [target_id],
            "deadline": deadline.isoformat(),
        }
        if alert is not None:
            context = alert.anchored(context)
            scope_facts[ALERT_STARTS_AT] = alert.scope_fact()
        return InvestigationInput(
            question=question,
            model_requests=model_requests,
            limits=limits,
            tool_schemas=tuple(dict(item) for item in self.tool_schemas),
            evidence_context=context,
            variant_id=self.variant_id,
            bound_target_id=target_id,
            scope_facts=scope_facts,
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
    scope facts (a renewed Run must not keep refusing on the old wall). An
    alert Run's frame, anchor and default query window carry over unchanged
    (F5, J5): the time policies and ``alert_starts_at`` are the previous
    Run's, never re-taken at renewal.
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
