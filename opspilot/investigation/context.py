"""Durable transcript: input snapshot, step keys and rebuild from business rows.

Technical plan section 7: PostgreSQL business records are the only
cross-process recovery basis. The committed ``opspilot_steps`` rows *are*
the conversation transcript; this module turns them back into the ordered
``system/user/assistant/tool`` messages the next model call needs, without a
second message store and without a graph checkpoint.

Section 5: recovery rebuilds committed steps first and admits new input only
at an explicit boundary; a rebuilt transcript never leaves a dangling tool
pairing; revoked evidence never re-enters a model input.

Everything here is a pure function of a ``DurableStore.rebuild`` snapshot
(``MemoryStepStore.snapshot`` renders the same shape), so it is testable
without PostgreSQL.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from opspilot.instructions.discipline import render
from opspilot.investigation.limits import RunLimits
from opspilot.investigation.messages import (
    PairingError,
    assistant_message,
    pair_tool_results,
    validate_tool_calls,
)
from opspilot.investigation.reports import (
    REPORT_CONTRACT,
    DeliveredView,
    context_target_catalog,
    delivered_from_context,
    eligible_time_policies,
    evidence_context_projection,
)
from opspilot.tools.registry import canonical

INITIAL_SEGMENT = "ctx0"
CONCLUSION_KIND = "conclusion"
INPUT_VERSION = "opspilot-investigation-input-v1"


class ContextError(Exception):
    """Fixed-code transcript failure. Recovery must fail closed on it."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class InvestigationInput:
    """The Run's input snapshot (C3 §5): what a later worker rebuilds from.

    Authorization facts (targets, window, deadline) are *recorded* here for
    audit but re-issued by the Controller on every attempt through the tool
    executor's ``QueryScope``; the snapshot never grants them.
    """

    question: str
    model_requests: int
    limits: RunLimits
    tool_schemas: tuple[Mapping[str, Any], ...]
    evidence_context: Mapping[str, Any] | None
    variant_id: str
    bound_target_id: str | None = None
    scope_facts: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.question, str) or not self.question:
            raise ContextError("INPUT_INVALID")
        if type(self.model_requests) is not int or self.model_requests < 1:
            raise ContextError("INPUT_INVALID")
        if not isinstance(self.limits, RunLimits):
            raise ContextError("INPUT_INVALID")
        if self.model_requests > self.limits.model_requests:
            raise ContextError("INPUT_INVALID")
        if not isinstance(self.tool_schemas, tuple) or any(
            not isinstance(item, Mapping) for item in self.tool_schemas
        ):
            raise ContextError("INPUT_INVALID")
        if self.evidence_context is not None and not isinstance(
            self.evidence_context, Mapping
        ):
            raise ContextError("INPUT_INVALID")
        if not isinstance(self.variant_id, str) or not self.variant_id:
            raise ContextError("INPUT_INVALID")
        if self.bound_target_id is not None and (
            not isinstance(self.bound_target_id, str) or not self.bound_target_id
        ):
            raise ContextError("INPUT_INVALID")
        if not isinstance(self.scope_facts, Mapping):
            raise ContextError("INPUT_INVALID")

    @property
    def tool_face_sha256(self) -> str:
        return hashlib.sha256(
            canonical([dict(item) for item in self.tool_schemas]).encode("utf-8")
        ).hexdigest()

    def as_json(self) -> dict[str, Any]:
        return {
            "version": INPUT_VERSION,
            "question": self.question,
            "model_requests": self.model_requests,
            "limits": self.limits.as_json(),
            "tool_schemas": [dict(item) for item in self.tool_schemas],
            "tool_face_sha256": self.tool_face_sha256,
            "evidence_context": None
            if self.evidence_context is None
            else dict(self.evidence_context),
            "variant_id": self.variant_id,
            "bound_target_id": self.bound_target_id,
            "scope_facts": dict(self.scope_facts),
        }

    @classmethod
    def from_json(cls, value: object) -> InvestigationInput:
        if not isinstance(value, Mapping) or value.get("version") != INPUT_VERSION:
            raise ContextError("INPUT_INVALID")
        schemas = value.get("tool_schemas")
        if not isinstance(schemas, list):
            raise ContextError("INPUT_INVALID")
        try:
            limits = RunLimits.from_json(value.get("limits"))
        except (ValueError, TypeError):
            raise ContextError("INPUT_INVALID") from None
        try:
            parsed = cls(
                question=value.get("question"),  # type: ignore[arg-type]
                model_requests=value.get("model_requests"),  # type: ignore[arg-type]
                limits=limits,
                tool_schemas=tuple(schemas),
                evidence_context=value.get("evidence_context"),
                variant_id=value.get("variant_id"),  # type: ignore[arg-type]
                bound_target_id=value.get("bound_target_id"),
                scope_facts=value.get("scope_facts") or {},
            )
        except ContextError:
            raise
        if value.get("tool_face_sha256") != parsed.tool_face_sha256:
            # The recorded face and the recorded schemas disagree: the row was
            # edited or partially written. Refuse rather than pick one.
            raise ContextError("INPUT_INVALID")
        return parsed


def step_key(segment: str, round_no: int) -> str:
    """C3 §7 stable step key ``run_id + context_segment + logical_round``.

    ``run_id`` is the row's own column; the key carries the other two.
    """
    if (
        not isinstance(segment, str)
        or not segment
        or ":" in segment
        or type(round_no) is not int
        or round_no < 1
    ):
        raise ContextError("INVALID_INPUT")
    return f"{segment}:round-{round_no}"


def parse_step_key(key: object) -> tuple[str, int] | None:
    """Inverse of ``step_key``; the pre-segment form ``round-N`` is ``ctx0``."""
    if not isinstance(key, str):
        return None
    segment, sep, rest = key.rpartition(":")
    if not sep:
        segment, rest = INITIAL_SEGMENT, key
    if not segment or ":" in segment or not rest.startswith("round-"):
        return None
    digits = rest[len("round-") :]
    if not digits.isdigit() or digits != str(int(digits)) or int(digits) < 1:
        return None
    return segment, int(digits)


def initial_messages(
    input: InvestigationInput, *, evidence_context: Mapping[str, Any] | None
) -> tuple[list[dict[str, Any]], str]:
    """The fixed prefix every attempt of the Run sends: system, question, context.

    ``evidence_context`` is the already projected context (the caller
    projects once, with the Run identity, before anything reads it).
    """
    system = render(
        input.variant_id,
        model_requests=input.model_requests,
        report_contract=REPORT_CONTRACT,
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": input.question},
    ]
    if evidence_context is not None:
        messages.append({"role": "user", "content": canonical(evidence_context)})
    return messages, system


def delivered_view(
    view: Mapping[str, Any],
    *,
    evidence_context: Mapping[str, Any] | None,
    authorized_targets: frozenset[str],
) -> DeliveredView | None:
    """The citation a committed, adopted tool view entitles the report to.

    Computed from the persisted view alone, so a rebuilt attempt derives the
    same ``DeliveredView`` the original attempt did.
    """
    evidence_id = view.get("evidence_id")
    if (
        view.get("adopted") is not True
        or not isinstance(evidence_id, str)
        or not evidence_id
    ):
        return None
    target = view.get("target_id")
    registry = target if isinstance(target, str) else None
    catalog = (
        context_target_catalog(evidence_context, authorized_targets=authorized_targets)
        or {}
    )
    aliases = frozenset(key for key, mapped in catalog.items() if mapped == registry)
    target_ids = (frozenset({registry}) if registry else frozenset()) | aliases
    status = view.get("status")
    return DeliveredView(
        evidence_id=evidence_id,
        target_ids=target_ids,
        status=status if isinstance(status, str) else "unknown",
        time_scope_refs=eligible_time_policies(
            evidence_context.get("time_policies")
            if isinstance(evidence_context, Mapping)
            else None,
            source=view.get("source"),
            tool=view.get("tool"),
            target_ids=target_ids,
            window=view.get("window"),
            freshness_seconds=view.get("freshness_seconds"),
            source_start_at=view.get("source_start_at"),
            source_end_at=view.get("source_end_at"),
            dispatch_started_at=view.get("dispatch_started_at"),
            response_received_at=view.get("observed_at"),
        ),
    )


def revoked_view(view: Mapping[str, Any]) -> dict[str, Any]:
    """What the model sees in place of evidence whose target is no longer authorized.

    The pairing survives (the ``tool`` message is still there); the content
    does not (C3 §7: revoked restricted evidence never re-enters a model
    input). The persisted row keeps the full view.
    """
    evidence_id = view.get("evidence_id")
    return {
        "evidence_id": evidence_id if isinstance(evidence_id, str) else None,
        "status": "revoked",
        "adopted": False,
        "reason": "TARGET_NOT_AUTHORIZED",
    }


@dataclass
class Transcript:
    """Ordered messages and citation state rebuilt from committed rows."""

    run_id: str
    input: InvestigationInput
    evidence_context: Mapping[str, Any] | None
    messages: list[dict[str, Any]]
    delivered: list[DeliveredView]
    evidence_ids: list[str]
    next_round: int
    segment: str
    live_steps: int
    dropped_groups: tuple[UUID, ...] = ()
    pending_publish: tuple[UUID, dict[str, Any]] | None = None


def _ordered_results(
    step: Mapping[str, Any], call_count: int
) -> list[Mapping[str, Any]] | None:
    """Tool results by ordinal, or ``None`` when some ordinal is still missing."""
    raw = step.get("tool_results")
    if not isinstance(raw, list):
        raise ContextError("INCONSISTENT_STATE")
    by_ordinal: dict[int, Mapping[str, Any]] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            raise ContextError("INCONSISTENT_STATE")
        ordinal = item.get("ordinal")
        result = item.get("result")
        if (
            type(ordinal) is not int
            or ordinal < 0
            or ordinal >= call_count
            or ordinal in by_ordinal
            or not isinstance(result, Mapping)
        ):
            raise ContextError("INCONSISTENT_STATE")
        by_ordinal[ordinal] = result
    if len(by_ordinal) != call_count:
        return None
    return [by_ordinal[index] for index in range(call_count)]


def rebuild_transcript(
    snapshot: Mapping[str, Any],
    *,
    run_id: str,
    authorized_targets: frozenset[str],
    input: InvestigationInput | None = None,
) -> Transcript:
    """Rebuild the next model call's messages from a ``rebuild()`` snapshot.

    Rules (C3 §5/§7):

    - only this Run's rows, in ``sequence`` order; ``late_result`` rows and
      terminal ``conclusion`` rows never enter the transcript;
    - every ``assistant.tool_calls`` group is complete, or it belongs to a
      superseded control generation and is dropped whole (recorded in
      ``dropped_groups``); a current-generation incomplete group means the
      caller has not replayed pending tools yet -- refuse;
    - a view whose target is outside the current authorization is replaced by
      a revoked stub and earns no citation;
    - anything malformed or from an unknown step kind fails closed.
    """
    run = snapshot.get("run")
    if not isinstance(run, Mapping) or str(run.get("run_id")) != run_id:
        raise ContextError("RUN_MISMATCH")
    if input is None:
        recorded = run.get("input")
        if recorded is None:
            raise ContextError("INPUT_MISSING")
        input = InvestigationInput.from_json(recorded)
    generation = snapshot.get("control_generation")
    if type(generation) is not int:
        raise ContextError("INCONSISTENT_STATE")
    context = evidence_context_projection(input.evidence_context, run_id=run_id)
    messages, _system = initial_messages(input, evidence_context=context)
    delivered = delivered_from_context(context, run_id=run_id)
    evidence_ids = [view.evidence_id for view in delivered]
    steps = snapshot.get("steps")
    if not isinstance(steps, Sequence):
        raise ContextError("INCONSISTENT_STATE")
    ordered = sorted(
        (step for step in steps if isinstance(step, Mapping)),
        key=lambda step: (int(step.get("sequence", 0)), str(step.get("step_id"))),
    )
    if len(ordered) != len(steps):
        raise ContextError("INCONSISTENT_STATE")
    max_round = 0
    segment = INITIAL_SEGMENT
    live = 0
    dropped: list[UUID] = []
    pending_publish: tuple[UUID, dict[str, Any]] | None = None
    for step in ordered:
        if step.get("status") == "late_result":
            continue
        if str(step.get("run_id")) != run_id:
            raise ContextError("INCONSISTENT_STATE")
        response = step.get("response")
        if not isinstance(response, Mapping):
            raise ContextError("INCONSISTENT_STATE")
        kind = response.get("kind")
        step_id = step.get("step_id")
        if not isinstance(step_id, UUID):
            raise ContextError("INCONSISTENT_STATE")
        if kind == CONCLUSION_KIND:
            if snapshot.get("conclusion") is None:
                pending_publish = (step_id, dict(response))
            continue
        if kind is not None:
            raise ContextError("INCOMPATIBLE_STATE")
        parsed = parse_step_key(step.get("logical_key"))
        if parsed is None:
            raise ContextError("INCOMPATIBLE_STATE")
        assistant = response.get("assistant")
        if not isinstance(assistant, Mapping):
            raise ContextError("INCONSISTENT_STATE")
        try:
            calls = validate_tool_calls(assistant)
        except PairingError:
            raise ContextError("INCONSISTENT_STATE") from None
        max_round = max(max_round, parsed[1])
        segment = parsed[0]
        live += 1
        content = assistant.get("content")
        reasoning = assistant.get("reasoning_content")
        if content is not None and not isinstance(content, str):
            raise ContextError("INCONSISTENT_STATE")
        if reasoning is not None and not isinstance(reasoning, str):
            raise ContextError("INCONSISTENT_STATE")
        if not calls:
            messages.append(
                assistant_message(
                    content=content, reasoning_content=reasoning, tool_calls=()
                )
            )
            continue
        results = _ordered_results(step, len(calls))
        if results is None:
            if step.get("control_generation") != generation:
                dropped.append(step_id)
                continue
            raise ContextError("PENDING_TOOLS")
        tool_messages: list[dict[str, Any]] = []
        for call, view in zip(calls, results, strict=True):
            target = view.get("target_id")
            if isinstance(target, str) and target not in authorized_targets:
                payload: Mapping[str, Any] = revoked_view(view)
            else:
                payload = view
                citation = delivered_view(
                    view,
                    evidence_context=context,
                    authorized_targets=authorized_targets,
                )
                if citation is not None:
                    delivered.append(citation)
                    evidence_ids.append(citation.evidence_id)
            tool_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": canonical(payload),
                }
            )
        try:
            group = pair_tool_results(
                assistant_message(
                    content=content, reasoning_content=reasoning, tool_calls=calls
                ),
                tool_messages,
                require_reasoning=True,
            )
        except PairingError as exc:
            # A missing ``reasoning_content`` cannot be replayed to DeepSeek
            # with tools attached; C3 §5 says block and hand off, never guess.
            raise ContextError(
                "INCOMPATIBLE_STATE"
                if exc.code == "PRIVATE_PROTOCOL_MISSING"
                else "INCONSISTENT_STATE"
            ) from None
        messages.extend(group)
    return Transcript(
        run_id=run_id,
        input=input,
        evidence_context=context,
        messages=messages,
        delivered=delivered,
        evidence_ids=list(dict.fromkeys(evidence_ids)),
        next_round=max_round + 1,
        segment=segment,
        live_steps=live,
        dropped_groups=tuple(dropped),
        pending_publish=pending_publish,
    )
