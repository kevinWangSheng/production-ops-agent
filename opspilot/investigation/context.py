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
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
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
    parse_report,
    view_targets_authorized,
)
from opspilot.tools.registry import canonical, redact_credentials

INITIAL_SEGMENT = "ctx0"
CONCLUSION_KIND = "conclusion"
COMPACTION_KIND = "compaction"
INPUT_VERSION = "opspilot-investigation-input-v1"

# --- context policy (HolmesGPT's two mechanisms, C3 §5 constraints) ---------
#
# Mechanism 1: a single tool result above ``single_tool_pct`` of the context
# budget is replaced, in the model-visible message only, by a provenance stub
# (Holmes spills to disk; here the full view already sits in ``tool_results``
# and the evidence store). Mechanism 2: before each model call, when the
# estimated prompt plus the output allowance passes ``compaction_pct`` of the
# budget, the history after the fixed prefix is summarised by one model
# request and replaced by a deterministic provenance digest plus that summary.
# Everything model-visible here is versioned into ``context_policy_revision``.

COMPACTION_INSTRUCTION = (
    "Context compaction request. The investigation continues after this, so "
    "write the working summary you need to continue it: observed facts with "
    "their exact evidence_id values, hypotheses and counter-evidence, what is "
    "still unknown, which queries were already made so they are not repeated, "
    "and the next intended step. Cite evidence_id values exactly as given. "
    "Do not call tools. Plain text only, no JSON, no Markdown fences."
)
COMPACTION_PREAMBLE = (
    "Earlier turns were compacted to keep the context within budget. The "
    "provenance digest below lists every evidence_id that was collected; the "
    "summary after it replaces the earlier turns:"
)
COMPACTION_SUFFIX = (
    "Continue the investigation from here. Cite only evidence_id values "
    "listed above or collected after this point; do not repeat listed queries."
)
STUB_REASON = "VIEW_TOO_LARGE"
_VIEW_PROVENANCE_KEYS = (
    "evidence_id",
    "operation_id",
    "status",
    "adopted",
    "tool",
    "source",
    "target_id",
    "window",
    "observed_at",
    "freshness_seconds",
    "truncated",
)
_TOKENS_PER_BYTE = 0.25
_MESSAGE_OVERHEAD_TOKENS = 4


@dataclass(frozen=True)
class ContextPolicy:
    version: str = "ctx-policy-v1"
    compaction_pct: float = 0.8
    single_tool_pct: float = 0.25
    stub_preview_chars: int = 512


CONTEXT_POLICY = ContextPolicy()


def context_policy_revision(policy: ContextPolicy = CONTEXT_POLICY) -> str:
    """Content hash of everything the compaction path shows the model or does.

    C3 §5 revision rule 4: recomputable from code alone. Changing the
    instruction text, the stub shape or a threshold moves this value, and a
    Run reclaimed under a different value is ``blocked(INCOMPATIBLE_STATE)``.
    """
    digest = hashlib.sha256(
        canonical(
            {
                "policy": {
                    "version": policy.version,
                    "compaction_pct": policy.compaction_pct,
                    "single_tool_pct": policy.single_tool_pct,
                    "stub_preview_chars": policy.stub_preview_chars,
                },
                "instruction": COMPACTION_INSTRUCTION,
                "preamble": COMPACTION_PREAMBLE,
                "suffix": COMPACTION_SUFFIX,
                "stub_keys": list(_VIEW_PROVENANCE_KEYS),
                "estimator": {"tokens_per_byte": _TOKENS_PER_BYTE},
            }
        ).encode("utf-8")
    ).hexdigest()
    return f"ctx-{policy.version}-{digest[:12]}"


def context_policy_versions() -> dict[str, str]:
    """The ``versions`` entry a Run creator/claimer must include (C3 §5)."""
    return {"context_policy_revision": context_policy_revision()}


def estimate_tokens(
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]] | None = None,
    *,
    extra: Sequence[Mapping[str, Any]] = (),
) -> int:
    """Conservative provider-agnostic estimate of a request's prompt tokens.

    Bytes of the canonical JSON at four bytes per token plus a per-message
    overhead, over messages *and* the tools array (the M0 estimator counted
    messages only, which under-reported every tools request). The provider's
    ``usage.prompt_tokens`` calibrates it upward per Run; see ``Calibration``.
    """
    total = 0
    for message in (*messages, *extra):
        size = len(canonical(dict(message)).encode("utf-8"))
        total += int(size * _TOKENS_PER_BYTE) + _MESSAGE_OVERHEAD_TOKENS
    if tools:
        size = len(canonical([dict(tool) for tool in tools]).encode("utf-8"))
        total += int(size * _TOKENS_PER_BYTE)
    return total


def calibrated(factor: float, estimated: int, observed: object) -> float:
    """The next calibration factor after the provider reported ``observed``.

    Monotone: it only grows, so once a Run has seen the estimator under-count
    it stays conservative. ``observed`` comes from ``usage.prompt_tokens`` and
    is ignored unless it is a positive integer.
    """
    if type(observed) is not int or observed <= 0 or estimated <= 0:
        return factor
    return max(factor, observed / estimated)


def view_stub(view: Mapping[str, Any], *, preview_chars: int) -> dict[str, Any]:
    """Model-visible replacement for a tool view above the single-tool cap.

    Keeps every provenance field (the citation is unaffected: ``delivered_view``
    reads the persisted full view), marks the omission, and carries a short
    preview. Deterministic, so a rebuild reproduces the same bytes.
    """
    full = canonical(view)
    stub: dict[str, Any] = {key: view.get(key) for key in _VIEW_PROVENANCE_KEYS}
    stub["truncated"] = True
    stub["spilled"] = True
    stub["reason"] = STUB_REASON
    stub["full_view_sha256"] = hashlib.sha256(full.encode("utf-8")).hexdigest()
    stub["full_view_bytes"] = len(full.encode("utf-8"))
    stub["preview"] = full[:preview_chars]
    return stub


def visible_view(
    view: Mapping[str, Any],
    *,
    limits: RunLimits,
    policy: ContextPolicy = CONTEXT_POLICY,
) -> Mapping[str, Any]:
    """The view the model sees for one committed tool result (mechanism 1)."""
    cap = int((limits.context_tokens - limits.output_tokens) * policy.single_tool_pct)
    if estimate_tokens([{"role": "tool", "content": canonical(view)}]) > cap:
        return view_stub(view, preview_chars=policy.stub_preview_chars)
    return view


def fold_digest(messages: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Deterministic provenance digest of the messages a compaction folds.

    Every tool view (or stub) contributes its provenance fields verbatim;
    assistant turns contribute their tool-call ids and a hash of their text.
    Private protocol fields are never read.
    """
    views: list[dict[str, Any]] = []
    tool_calls: list[dict[str, str]] = []
    texts: list[str] = []
    for message in messages:
        role = message.get("role")
        if role == "tool":
            try:
                payload = json.loads(str(message.get("content")))
            except ValueError:
                payload = None
            entry = (
                {key: payload.get(key) for key in _VIEW_PROVENANCE_KEYS}
                if isinstance(payload, Mapping)
                else {key: None for key in _VIEW_PROVENANCE_KEYS}
            )
            entry["tool_call_id"] = message.get("tool_call_id")
            views.append(entry)
        elif role == "assistant":
            for call in message.get("tool_calls") or ():
                if isinstance(call, Mapping) and isinstance(
                    call.get("function"), Mapping
                ):
                    tool_calls.append(
                        {
                            "id": str(call.get("id")),
                            "name": str(call["function"].get("name")),
                            "arguments_sha256": hashlib.sha256(
                                str(call["function"].get("arguments")).encode("utf-8")
                            ).hexdigest(),
                        }
                    )
            content = message.get("content")
            if isinstance(content, str) and content:
                texts.append(hashlib.sha256(content.encode("utf-8")).hexdigest())
    return {
        "folded_messages": len(messages),
        "tool_calls": tool_calls,
        "views": views,
        "evidence_ids": list(
            dict.fromkeys(
                v["evidence_id"]
                for v in views
                if isinstance(v.get("evidence_id"), str) and v.get("adopted") is True
            )
        ),
        "assistant_text_sha256": texts,
    }


def compaction_message(
    revision: int, digest: Mapping[str, Any], summary: str
) -> dict[str, Any]:
    """The single user message that replaces the folded turns (Holmes shape)."""
    return {
        "role": "user",
        "content": (
            f"[context compacted rev={revision}] {COMPACTION_PREAMBLE}\n"
            f"{canonical(digest)}\n\n{summary.strip()}\n\n{COMPACTION_SUFFIX}"
        ),
    }


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


@dataclass(frozen=True)
class CommittedRound:
    """How the last replayed round ended, read from its committed row.

    The loop judges a round from exactly these facts -- on the live path
    straight from the provider reply, on resume from the row -- through one
    function, so a restart between the row and its conclusion reaches the
    verdict the live attempt would have (bot review findings, PR #29).
    """

    finish_reason: str | None
    final: bool
    rejection: str | None
    tool_round: bool
    content: str | None
    # A conclusion under a superseded generation followed this round: the
    # Run was concluded once and a human moved it on, so this round is
    # history, not a verdict to redo. A round replayed after that conclusion
    # starts a fresh record.
    concluded: bool = False


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
    prefix_len: int
    last_step_id: UUID | None = None
    calibration: float = 1.0
    folded_since: tuple[UUID, ...] = ()
    compactions: int = 0
    # Every compaction row, accepted or refused: the next attempt's key must
    # not collide with a refused row that ``commit_step`` would hand back.
    compaction_attempts: int = 0
    dropped_groups: tuple[UUID, ...] = ()
    pending_publish: tuple[UUID, dict[str, Any]] | None = None
    last_round: CommittedRound | None = None


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
    prefix_len = len(messages)
    delivered = delivered_from_context(context, run_id=run_id)
    evidence_ids = [view.evidence_id for view in delivered]
    steps = snapshot.get("steps")
    if not isinstance(steps, Sequence):
        raise ContextError("INCONSISTENT_STATE")
    # Human inputs the Run received; a rebuild that omits them for a round
    # that saw them cannot reproduce its bytes and fails closed below.
    inputs = snapshot.get("inputs") or ()
    if not isinstance(inputs, Sequence):
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
    last_step_id: UUID | None = None
    last_round: CommittedRound | None = None
    calibration = 1.0
    diverged = False  # a revoked view changed the visible bytes; hashes no longer apply
    folded_since: list[UUID] = []
    compactions = 0
    compaction_attempts = 0
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
            # A conclusion committed under a superseded generation can never be
            # published (publish fences on the step's generation); a human
            # follow-up moved the Run on, so the loop continues instead.
            if snapshot.get("conclusion") is None:
                if step.get("control_generation") == generation:
                    pending_publish = (step_id, dict(response))
                elif last_round is not None:
                    last_round = replace(last_round, concluded=True)
            continue
        calibration = _calibration_from_row(response, calibration)
        if kind == COMPACTION_KIND:
            record = response.get("compaction")
            if not isinstance(record, Mapping):
                raise ContextError("INCONSISTENT_STATE")
            compaction_attempts += 1
            if record.get("accepted") is not True:
                # The live attempt handed off on this row (COMPACTION_FAILED);
                # a resume must reach the same verdict instead of compacting
                # again onto the same key (independent review, PR #29).
                last_round = CommittedRound(
                    finish_reason=None,
                    final=False,
                    rejection="COMPACTION_FAILED",
                    tool_round=False,
                    content=None,
                    # Older than the current generation: a human decision
                    # (follow_up / correct) came after it, so it is history
                    # here too, exactly as for model rows.
                    concluded=step.get("control_generation") != generation,
                )
                continue
            if (
                record.get("from_segment") != segment
                or list(record.get("folded_step_ids") or ())
                != [str(i) for i in folded_since]
                or not isinstance(record.get("digest"), Mapping)
                or type(record.get("revision")) is not int
                or not isinstance(record.get("to_segment"), str)
            ):
                # The row claims to fold steps this rebuild does not see (or
                # sees differently): never guess which context the model had.
                raise ContextError("INCOMPATIBLE_STATE")
            summary = (response.get("assistant") or {}).get("content")
            if not isinstance(summary, str) or not summary.strip():
                raise ContextError("INCONSISTENT_STATE")
            if not diverged:
                _check_snapshot_hash(
                    response,
                    [*messages, {"role": "user", "content": COMPACTION_INSTRUCTION}],
                )
            messages = [
                *messages[:prefix_len],
                compaction_message(int(record["revision"]), record["digest"], summary),
            ]
            segment = str(record["to_segment"])
            folded_since = []
            compactions += 1
            continue
        if kind is not None:
            raise ContextError("INCOMPATIBLE_STATE")
        parsed = parse_step_key(step.get("logical_key"))
        if parsed is None:
            raise ContextError("INCOMPATIBLE_STATE")
        if parsed[0] != segment:
            # A round committed under a context representation this rebuild
            # did not reproduce (a lost compaction row, or rows out of order).
            raise ContextError("INCOMPATIBLE_STATE")
        if not diverged:
            _check_snapshot_hash(response, messages, inputs=inputs)
        last_step_id = step_id
        assistant = response.get("assistant")
        if not isinstance(assistant, Mapping):
            raise ContextError("INCONSISTENT_STATE")
        try:
            calls = validate_tool_calls(assistant)
        except PairingError:
            raise ContextError("INCONSISTENT_STATE") from None
        max_round = max(max_round, parsed[1])
        live += 1
        folded_since.append(step_id)
        content = assistant.get("content")
        reasoning = assistant.get("reasoning_content")
        if content is not None and not isinstance(content, str):
            raise ContextError("INCONSISTENT_STATE")
        if reasoning is not None and not isinstance(reasoning, str):
            raise ContextError("INCONSISTENT_STATE")
        finish = response.get("finish_reason")
        row_context = response.get("context")
        rejected = response.get("rejected_plan")
        rejection = rejected.get("reason") if isinstance(rejected, Mapping) else None
        committed = CommittedRound(
            finish_reason=finish if isinstance(finish, str) else None,
            final=isinstance(row_context, Mapping) and row_context.get("final") is True,
            rejection=rejection if isinstance(rejection, str) else None,
            tool_round=bool(calls),
            content=content,
            # A row from an older control generation predates a human
            # decision (follow_up / correct) whether or not its conclusion
            # row landed before the fence: history, not a verdict to redo
            # (bot review finding, PR #29).
            concluded=step.get("control_generation") != generation,
        )
        if not calls:
            messages.append(
                assistant_message(
                    content=content, reasoning_content=reasoning, tool_calls=()
                )
            )
            last_round = committed
            continue
        results = _ordered_results(step, len(calls))
        if results is None:
            if step.get("control_generation") != generation:
                dropped.append(step_id)
                continue
            raise ContextError("PENDING_TOOLS")
        last_round = committed
        tool_messages: list[dict[str, Any]] = []
        for call, view in zip(calls, results, strict=True):
            target = view.get("target_id")
            if isinstance(target, str) and target not in authorized_targets:
                payload: Mapping[str, Any] = revoked_view(view)
                diverged = True
            else:
                payload = visible_view(view, limits=input.limits)
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
        prefix_len=prefix_len,
        last_step_id=last_step_id,
        calibration=calibration,
        folded_since=tuple(folded_since),
        compactions=compactions,
        compaction_attempts=compaction_attempts,
        dropped_groups=tuple(dropped),
        pending_publish=pending_publish,
        last_round=last_round,
    )


def messages_hash(messages: Sequence[Mapping[str, Any]]) -> str:
    """The hash a step records for exactly the messages it sent."""
    return hashlib.sha256(
        canonical([dict(message) for message in messages]).encode("utf-8")
    ).hexdigest()


# --- human inputs (follow_up / correct / event) shown to the model -----------
#
# Field allowlist for ``opspilot_inputs.content`` (human follow-up/correction
# text and intake events) before it reaches the model prompt. That column has
# no reviewed schema -- it is whatever a caller (e.g. the web control layer)
# passed to ``DurableStore.control``/``append_input`` -- so a key-name
# blocklist only catches names someone thought of in advance and never
# protects a nested value or an unlisted credential-shaped key (redline
# P3-4/P3-5 analog for the input channel, not just ``evidence_context``). An
# allowlist drops everything else, including a dict/list value smuggled under
# an allowed key, before it can reach the outbound prompt. ``read_inputs()``
# (human playback, not the model path) intentionally stays unfiltered -- this
# projection only guards what the loop sends to the model. Known limit, not a
# gap this projection can close: a credential pasted directly into the
# ``text`` free-text string itself still reaches the model -- only
# structured-field smuggling is in scope here. ``question`` is included
# alongside ``text``/``channel`` because it is the payload shape the
# follow_up path persists and reads back (``IntakeRequest.question``);
# dropping it would silently empty out a real follow-up's content instead of
# blocking a credential.
INPUT_CONTENT_FIELDS = frozenset({"text", "channel", "question"})

# Defensive per-field cap on the allowlisted free-text values, not one of the
# frozen resource ceilings in investigation/limits.py (that table tracks the
# *whole* serialized request against the 2026-09-13 freeze, not any one field
# -- this is a locally-chosen default, flagged for the user to confirm or
# replace with a formally frozen number). Without a bound, a single oversized
# value persisted through control()/append_input() would be stuck forever:
# begin_round()'s watermark is cumulative, so the same entry is re-selected on
# every future round, _reject_oversized() halts with REQUEST_TOO_LARGE each
# time, and nothing in the current control() surface can remove or replace
# one bad input -- only a direct database repair would recover the incident.
INPUT_CONTENT_FIELD_MAX_CHARS = 8192

INPUTS_MESSAGE_KEY = "investigation_inputs"


def project_input_content(content: object) -> dict[str, Any]:
    """The allowlisted, bounded, credential-redacted projection of one input
    row's ``content``.

    Free text in the allowlisted fields is redacted with the registry's
    credential rules (``redact_credentials``) before it can reach the model:
    PRODUCT-CONSTRAINTS, "Credentials and secret-bearing raw inputs must not
    enter prompts or exported traces". Model-facing only -- the stored row
    keeps the operator's raw text for human playback (``read_inputs``).
    Redaction runs before truncation so a cut never exposes a partial secret.
    """
    if not isinstance(content, Mapping):
        return {}
    projected: dict[str, Any] = {}
    for key, value in content.items():
        if key not in INPUT_CONTENT_FIELDS or isinstance(value, (Mapping, list, tuple)):
            continue
        if isinstance(value, str):
            value = redact_credentials(value)
            if len(value) > INPUT_CONTENT_FIELD_MAX_CHARS:
                value = value[:INPUT_CONTENT_FIELD_MAX_CHARS] + " …[truncated]"
        projected[key] = value
    return projected


def input_watermark(inputs: Sequence[Mapping[str, Any]]) -> int:
    """The highest input ``sequence`` a round saw (0 when it saw none)."""
    return max(
        (int(row["sequence"]) for row in inputs if type(row.get("sequence")) is int),
        default=0,
    )


def inputs_message(
    inputs: Sequence[Mapping[str, Any]], *, watermark: int | None = None
) -> dict[str, Any] | None:
    """The one trailing user message carrying the inputs a round may see.

    The live round (``InvestigationLoop._round``) builds it from what
    ``begin_round()`` returned; the rebuild builds it from the persisted
    ``inputs`` up to the step row's recorded ``context.input_watermark``.
    Both go through this function, so the bytes -- and therefore the
    recorded ``input_snapshot_hash`` -- agree. It is a per-round trailing
    message, never part of the durable transcript or of a compaction fold.
    """
    rows = sorted(
        (
            row
            for row in inputs
            if isinstance(row, Mapping)
            and type(row.get("sequence")) is int
            and (watermark is None or row["sequence"] <= watermark)
        ),
        key=lambda row: int(row["sequence"]),
    )
    if not rows:
        return None
    return {
        "role": "user",
        "content": canonical(
            {
                INPUTS_MESSAGE_KEY: [
                    {
                        "sequence": row["sequence"],
                        "kind": row.get("kind"),
                        "content": project_input_content(row.get("content")),
                    }
                    for row in rows
                ]
            }
        ),
    }


def _check_snapshot_hash(
    response: Mapping[str, Any],
    sent_before: Sequence[Mapping[str, Any]],
    *,
    inputs: Sequence[Mapping[str, Any]] = (),
) -> None:
    """Refuse a rebuild whose bytes differ from what that step actually sent.

    ``sent_before`` is the transcript as rebuilt up to (not including) the
    step; the step's own trailing messages -- the inputs message up to the
    recorded ``context.input_watermark`` (from ``inputs``), then the
    final-report or the compaction instruction -- are added from the
    recorded ``context`` / the row kind, in the order ``_round`` sends them.
    Rows without a recorded hash (pre-segment rows) are not checked.
    """
    context = response.get("context")
    if not isinstance(context, Mapping):
        return
    recorded = context.get("input_snapshot_hash")
    if not isinstance(recorded, str):
        return
    sent = list(sent_before)
    watermark = context.get("input_watermark")
    if type(watermark) is int and watermark > 0:
        message = inputs_message(inputs, watermark=watermark)
        if message is not None:
            sent.append(message)
    if context.get("final") is True:
        from opspilot.investigation.reports import FINAL_REPORT_INSTRUCTION

        sent.append({"role": "user", "content": FINAL_REPORT_INSTRUCTION})
    if messages_hash(sent) != recorded:
        raise ContextError("INCOMPATIBLE_STATE")


def pending_conclusion(
    snapshot: Mapping[str, Any],
) -> tuple[UUID, dict[str, Any]] | None:
    """A current-generation conclusion row that was never published, if any.

    Read before replaying pending tools: once a Run has concluded, nothing it
    left behind (including a rejected plan) may be executed.
    """
    if snapshot.get("conclusion") is not None:
        return None
    generation = snapshot.get("control_generation")
    for step in snapshot.get("steps") or ():
        if not isinstance(step, Mapping) or step.get("status") == "late_result":
            continue
        response = step.get("response")
        if (
            isinstance(response, Mapping)
            and response.get("kind") == CONCLUSION_KIND
            and step.get("control_generation") == generation
            and isinstance(step.get("step_id"), UUID)
        ):
            return step["step_id"], dict(response)
    return None


def conclusion_publishable(response: Mapping[str, Any]) -> bool:
    """ADR-0005: only a qualified report with no handoff becomes the conclusion.

    ``response`` is a committed ``conclusion`` step (``LoopOutcome.conclusion``
    or the row ``pending_conclusion`` found). Anything else -- a budget or
    pairing failure, an incomplete report, a fenced attempt -- is a handoff:
    the row stays readable, the Run is parked for a human, nothing is
    published. Both drivers (runner and workbench) decide with this one rule.
    """
    body = response.get("conclusion")
    if not isinstance(body, Mapping):
        return False
    content = body.get("report_content")
    # A recovered row is only as trustworthy as its own shape: the report
    # must be present as text, match its recorded digest and still parse as
    # the report the live path validated (same ``parse_report``), else the
    # row is a handoff to a human rather than something to publish (bot
    # review, PR #44). ``publish()`` still compares the row to the
    # conclusion. Citations are not re-checked here: they were checked
    # against the delivered views when the row was written.
    if (
        body.get("execution") != "completed"
        or body.get("handoff") is not False
        or not isinstance(content, str)
        or hashlib.sha256(content.encode("utf-8")).hexdigest()
        != body.get("report_content_sha256")
    ):
        return False
    report, _reason = parse_report(content, finish_reason="stop")
    return report is not None and report.schema_version == body.get(
        "report_schema_version"
    )


def _calibration_from_row(response: Mapping[str, Any], factor: float) -> float:
    context = response.get("context")
    usage = response.get("usage")
    if not isinstance(context, Mapping) or not isinstance(usage, Mapping):
        return factor
    estimated = context.get("estimated_prompt_tokens")
    if type(estimated) is not int:
        return factor
    return calibrated(factor, estimated, usage.get("prompt_tokens"))


def committed_views(
    snapshot: Mapping[str, Any], *, run_id: str
) -> list[Mapping[str, Any]]:
    """Every tool view this Run committed, in step/ordinal order.

    Business facts only (C3 §7): late results are skipped, terminal and
    compaction rows carry no views, and a group that never completed still
    contributes the ordinals it did commit -- those views were adopted and
    remain reusable history even though the group can no longer be replayed
    to the model. Nothing here depends on prompt bytes or on
    ``reasoning_content``, which is why cross-Run continuation reads views
    from this walk rather than from ``rebuild_transcript``: the Run that
    hands off is often exactly the one whose transcript can no longer be
    replayed (a prompt/context-policy revision bump blocked it).
    """
    steps = snapshot.get("steps")
    if not isinstance(steps, Sequence):
        raise ContextError("INCONSISTENT_STATE")
    ordered = sorted(
        (step for step in steps if isinstance(step, Mapping)),
        key=lambda step: (int(step.get("sequence", 0)), str(step.get("step_id"))),
    )
    views: list[Mapping[str, Any]] = []
    for step in ordered:
        if step.get("status") == "late_result":
            continue
        if str(step.get("run_id")) != run_id:
            raise ContextError("INCONSISTENT_STATE")
        response = step.get("response")
        if not isinstance(response, Mapping):
            raise ContextError("INCONSISTENT_STATE")
        if response.get("kind") is not None:
            continue
        raw = step.get("tool_results")
        if not isinstance(raw, list):
            raise ContextError("INCONSISTENT_STATE")
        by_ordinal: dict[int, Mapping[str, Any]] = {}
        for item in raw:
            if not isinstance(item, Mapping):
                raise ContextError("INCONSISTENT_STATE")
            ordinal = item.get("ordinal")
            result = item.get("result")
            if type(ordinal) is not int or not isinstance(result, Mapping):
                raise ContextError("INCONSISTENT_STATE")
            by_ordinal[ordinal] = result
        views.extend(by_ordinal[o] for o in sorted(by_ordinal))
    return views


# --- cross-Run continuation (C3 §7 "基于业务事实的新 Run 接续") -----------------


@dataclass(frozen=True)
class Continuation:
    """What a successor Run receives from a Run that handed off.

    ``evidence_context`` is a v4 evidence context bound to the *new* Run id
    whose ``view_bindings`` name every adopted, still-authorized view of the
    previous Run, so the successor can cite them without re-querying;
    Carried views are historical evidence: their ``current``-mode policy refs
    are not carried, and opaque target refs are resolved under the previous
    Run's own authorization before the successor's filter applies.
    ``handoff_note`` is deterministic text for the successor's question: the
    previous outcome codes, the carried evidence ids and the ``gaps`` /
    ``next_steps`` of the previous *validated* report (model text that already
    passed citation checks -- the only model prose that crosses over). No
    private protocol field or non-adopted result crosses over. Creating the
    successor Run stays a human/Controller action.
    """

    previous_run_id: str
    evidence_context: dict[str, Any]
    handoff_note: str
    evidence_ids: tuple[str, ...]


def continuation_context(
    snapshot: Mapping[str, Any],
    *,
    new_run_id: str,
    authorized_targets: frozenset[str],
) -> Continuation:
    run = snapshot.get("run")
    if not isinstance(run, Mapping) or run.get("input") is None:
        raise ContextError("INPUT_MISSING")
    if not isinstance(new_run_id, str) or not new_run_id:
        raise ContextError("INVALID_INPUT")
    previous_run_id = str(run.get("run_id"))
    if previous_run_id == new_run_id:
        raise ContextError("INVALID_INPUT")
    previous = InvestigationInput.from_json(run["input"])
    context = evidence_context_projection(
        previous.evidence_context, run_id=previous_run_id
    )
    steps = snapshot.get("steps")
    if not isinstance(steps, Sequence):
        raise ContextError("INCONSISTENT_STATE")
    # What the successor may cite: the views the previous Run inherited
    # through its own input context plus every view it committed, each
    # deriving its citation through ``delivered_view`` exactly as a live
    # attempt does, then filtered once by ``view_targets_authorized``. Read
    # from business rows, never by replaying the transcript (see
    # ``committed_views``).
    # Resolve opaque refs the way the *previous* Run resolved them (its own
    # recorded authorization decides which sole target an unmapped catalog
    # entry bound to); only then filter that stable mapping against the
    # successor's authorization. Resolving against the successor would let a
    # differently-scoped successor re-bind old evidence to its own target.
    previous_targets = frozenset(
        t
        for t in (previous.scope_facts.get("target_ids") or ())
        if isinstance(t, str) and t
    )
    catalog = context_target_catalog(context, authorized_targets=previous_targets)
    adopted: list[DeliveredView] = list(
        delivered_from_context(context, run_id=previous_run_id)
    )
    for view in committed_views(snapshot, run_id=previous_run_id):
        citation = delivered_view(
            view, evidence_context=context, authorized_targets=previous_targets
        )
        if citation is not None:
            adopted.append(citation)
    # Carried evidence is historical: a ``current``-mode policy's eligibility
    # (freshness against ``max_source_age_seconds``) was judged when the view
    # was delivered to the previous Run and is not re-judged here, so its
    # refs do not cross over. A successor that needs a current fact
    # re-observes it.
    current_policies = frozenset(
        str(policy.get("id"))
        for policy in (context or {}).get("time_policies") or ()
        if isinstance(policy, Mapping) and policy.get("mode") == "current"
    )
    bindings: dict[str, Any] = {}
    for citation in adopted:
        if not view_targets_authorized(
            citation, authorized_targets=authorized_targets, target_catalog=catalog
        ):
            continue
        bindings[citation.evidence_id] = {
            "status": citation.status,
            "target_refs": sorted(citation.target_ids),
            "time_scope_refs": sorted(citation.time_scope_refs - current_policies),
        }
    carried: dict[str, Any] = {
        "type": "opspilot-evidence-context-v4",
        "run_id": new_run_id,
        "view_bindings": bindings,
    }
    if isinstance(context, Mapping):
        if "time_policies" in context:
            carried["time_policies"] = context["time_policies"]
        if isinstance(context.get("target_catalog"), Mapping):
            # Carry the catalog with the mapping the previous Run resolved
            # it under: an entry that bound to the predecessor's sole target
            # keeps that registry id, so a differently-scoped successor can
            # neither re-resolve the ref to its own target nor let a fresh
            # view inherit the old alias (bot review finding, PR #29).
            carried["target_catalog"] = {
                key: (
                    {**entry, "target_id": (catalog or {}).get(key)}
                    if isinstance(entry, Mapping)
                    and not entry.get("target_id")  # absent or "": unmapped
                    and (catalog or {}).get(key)
                    else entry
                )
                for key, entry in context["target_catalog"].items()
            }
    projected = evidence_context_projection(carried, run_id=new_run_id)
    if projected is None:
        raise ContextError("INCONSISTENT_STATE")
    conclusion = snapshot.get("conclusion")
    if not isinstance(conclusion, Mapping):
        # The latest conclusion row: after a follow-up superseded an earlier
        # one, the handoff note describes the outcome the Run actually ended
        # on (independent review, PR #29).
        conclusion = next(
            (
                s["response"]
                for s in sorted(
                    (s for s in steps if isinstance(s, Mapping)),
                    key=lambda s: (int(s.get("sequence", 0)), str(s.get("step_id"))),
                    reverse=True,
                )
                if isinstance(s.get("response"), Mapping)
                and s["response"].get("kind") == CONCLUSION_KIND
                and s.get("status") != "late_result"
            ),
            None,
        )
    summary = conclusion.get("conclusion") if isinstance(conclusion, Mapping) else None
    summary = summary if isinstance(summary, Mapping) else {}
    gaps: list[str] = []
    next_steps: list[str] = []
    report_text = summary.get("report_content")
    # Only a *validated* report's text crosses over: ``_finish`` records the
    # final text of a failed attempt too (schema version ``None``), and that
    # candidate never passed schema or citation checks (bot review finding,
    # PR #29).
    if not isinstance(summary.get("report_schema_version"), str):
        report_text = None
    if isinstance(report_text, str):
        try:
            report = json.loads(report_text)
        except ValueError:
            report = None
        if isinstance(report, Mapping):
            gaps = [g for g in report.get("gaps") or () if isinstance(g, str)]
            next_steps = [
                n for n in report.get("next_steps") or () if isinstance(n, str)
            ]
    evidence_ids = tuple(bindings)
    lines = [
        f"Continuation of investigation Run {previous_run_id}.",
        f"Previous outcome: execution={summary.get('execution', 'unknown')}, "
        f"handoff_reasons={list(summary.get('handoff_reasons') or ())}, "
        f"rounds={summary.get('rounds', 'unknown')}.",
        f"Carried evidence ({len(evidence_ids)} adopted views, cite by evidence_id): "
        + (", ".join(evidence_ids) if evidence_ids else "none"),
    ]
    if gaps:
        lines.append("Previous gaps: " + " | ".join(gaps))
    if next_steps:
        lines.append("Previous next steps: " + " | ".join(next_steps))
    lines.append(
        "Carried views keep their original observation time; query again for "
        "current state instead of re-dating them."
    )
    return Continuation(
        previous_run_id=previous_run_id,
        evidence_context=dict(projected),
        handoff_note="\n".join(lines),
        evidence_ids=evidence_ids,
    )
