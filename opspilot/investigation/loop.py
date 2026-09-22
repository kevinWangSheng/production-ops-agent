"""The Flash investigation loop (technical plan section 5).

``assemble context → call model → commit response and tool plan → execute
and commit tool observations → next round``.

The loop does not take intake, does not render UI, and does not invent a
report when the model fails. Budget, deadline and pairing failures become
an explicit handoff; they never look like a completed investigation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol
from uuid import UUID

from opspilot.instructions.discipline import prompt_revision
from opspilot.investigation.context import (
    COMPACTION_INSTRUCTION,
    COMPACTION_KIND,
    CONCLUSION_KIND,
    CONTEXT_POLICY,
    INITIAL_SEGMENT,
    InvestigationInput,
    Transcript,
    calibrated,
    compaction_message,
    context_policy_versions,
    delivered_view,
    estimate_tokens,
    fold_digest,
    initial_messages,
    messages_hash,
    step_key,
    visible_view,
)
from opspilot.investigation.limits import (
    M1_FROZEN_LIMITS,
    MAX_MODEL_REQUESTS_PER_RUN,
    RunLimits,
)
from opspilot.investigation.messages import (
    PairingError,
    assistant_message,
    pair_tool_results,
    validate_tool_calls,
)
from opspilot.investigation.reports import (
    FINAL_REPORT_INSTRUCTION,
    REPORT_CONTRACT,
    DeliveredView,
    ReportV2,
    context_target_catalog,
    context_time_policy_ids,
    delivered_from_context,
    evidence_context_projection,
    parse_report,
    unsupported_citations,
)
from opspilot.investigation.store import (
    StepCommitter,
    StepStoreError,
    reservation_id_for,
)
from opspilot.tools.executor import Clock, QueryScope, ReadOnlyToolExecutor, ToolRequest
from opspilot.tools.registry import canonical

DISCIPLINE_VARIANT = "replay-candidate"
ACCEPTED_RESPONSE_MODEL = "deepseek-flash"

LoopExecution = Literal["completed", "failed", "blocked", "budget_exhausted"]


def prompt_revision_versions(
    variant_id: str = DISCIPLINE_VARIANT, *, report_contract: str = REPORT_CONTRACT
) -> dict[str, str]:
    """``ModelProfile.prompt_revision``'s entry in the ``versions`` a caller's
    ``DurableStore.accept``/``claim`` must compare (C3 §5, "指令分层与版本").

    This is the loop's single source for that one field -- content-hashed
    from ``discipline.prompt_revision``, never hand-typed -- so the ledger
    value the loop reports and the value a future Run-creation caller feeds
    into the recovery-version barrier are guaranteed to be the same
    computation. Instance values (budget, authorized services, evidence
    context) never reach ``prompt_revision`` and so never appear here either;
    two Runs that differ only in those get the same dict and do not
    spuriously block each other on reclaim.

    ``tool_schema_revision`` is the L3a half of the same C3 §5 ``versions``
    comparison. It is not this loop's to compute (it belongs to the tool
    registry, PR #20); a caller building a full ``versions`` dict must merge
    it in separately.

    ``discipline.template_projection`` (single source on ``main`` since
    PR #27) hashes every segment in variant order, so reordering an L1b/L2
    slot relative to the L1a segments moves this value together with
    ``render()``'s bytes. The earlier snapshot of ``discipline.py`` carried
    on this branch hashed only ``LAYER_TEMPLATE`` segments; it was replaced
    wholesale by the merged single source, so values recorded before that
    merge (see the task record) are not comparable with values computed
    here.
    """
    return {
        "prompt_revision": prompt_revision(variant_id, report_contract=report_contract)
    }


# Halt reasons that mean the store itself refused this attempt's writes; no
# conclusion step is attempted after them (it would only become late history).
def investigation_versions(variant_id: str = DISCIPLINE_VARIANT) -> dict[str, str]:
    """Every ``versions`` key the loop's behaviour depends on (C3 §5).

    ``prompt_revision`` (L1a + L2) and ``context_policy_revision`` (the
    compaction path). ``tool_schema_revision`` belongs to the tool registry and
    is merged in by the caller that builds the Run's ``versions``.
    """
    return {**prompt_revision_versions(variant_id), **context_policy_versions()}


_STORE_REFUSALS = frozenset(
    {"CONTROL_DENIED", "INCOMPATIBLE_STATE", "STORAGE_UNAVAILABLE"}
)

_HANDOFF_FROM_STORE: dict[str, tuple[LoopExecution, str]] = {
    "BUDGET_EXHAUSTED": ("budget_exhausted", "BUDGET_EXHAUSTED"),
    "CONTROL_DENIED": ("failed", "CONTROL_DENIED"),
    "INCOMPATIBLE_STATE": ("blocked", "INCOMPATIBLE_STATE"),
    "STORAGE_UNAVAILABLE": ("failed", "STORAGE_UNAVAILABLE"),
}


class ModelError(Exception):
    """Fixed-code model-adapter failure. Never carries provider text outward."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ModelCall:
    """One outbound Chat Completions request. Credentials never appear here."""

    messages: tuple[Mapping[str, Any], ...]
    tools: tuple[Mapping[str, Any], ...] | None
    json_mode: bool
    max_tokens: int
    timeout_seconds: float
    model: str = ACCEPTED_RESPONSE_MODEL


def serialized_request(call: ModelCall) -> bytes:
    """Exact Chat Completions body the DeepSeek client will POST.

    Timeout is a transport deadline, not a JSON field. Size checks and
    request hashes must use these bytes, not a reduced projection.
    """
    payload: dict[str, Any] = {
        "model": call.model,
        "messages": [dict(message) for message in call.messages],
        "max_tokens": call.max_tokens,
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
        "stream": False,
    }
    if call.json_mode:
        payload["response_format"] = {"type": "json_object"}
    if call.tools is not None:
        payload["tools"] = [dict(tool) for tool in call.tools]
        payload["tool_choice"] = "auto"
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


@dataclass(frozen=True)
class ModelReply:
    """Normalized provider reply after identity and completeness checks."""

    content: str | None
    reasoning_content: str | None
    tool_calls: tuple[Mapping[str, Any], ...]
    finish_reason: str
    response_model: str
    usage: Mapping[str, Any]
    raw: Mapping[str, Any]


class ModelClient(Protocol):
    def complete(self, call: ModelCall) -> ModelReply: ...


@dataclass(frozen=True)
class InvestigationRequest:
    """Caller-supplied input for one bounded Run. No intake wiring."""

    run_id: str
    question: str
    scope: QueryScope
    tool_schemas: tuple[Mapping[str, Any], ...]
    model_requests: int = MAX_MODEL_REQUESTS_PER_RUN
    evidence_context: Mapping[str, Any] | None = None
    bound_target_id: str | None = None
    variant_id: str = DISCIPLINE_VARIANT
    # Per-Run ceilings. The frozen M1 instance is the default; a caller may
    # pass a smaller or (in tests) larger one -- the product boundary that
    # admits a Run enforces ``limits.within(M1_FROZEN_LIMITS)``.
    limits: RunLimits = M1_FROZEN_LIMITS

    def as_input(self) -> InvestigationInput:
        """The persistable snapshot a later attempt rebuilds this request from."""
        return InvestigationInput(
            question=self.question,
            model_requests=self.model_requests,
            limits=self.limits,
            tool_schemas=tuple(dict(item) for item in self.tool_schemas),
            # Persist what the model may see, never the caller's raw mapping.
            evidence_context=evidence_context_projection(
                self.evidence_context, run_id=self.run_id
            ),
            variant_id=self.variant_id,
            bound_target_id=self.bound_target_id,
            scope_facts={
                "target_ids": sorted(self.scope.target_ids),
                "window": self.scope.window.as_json(),
                "deadline": self.scope.deadline.isoformat(),
            },
        )


@dataclass(frozen=True)
class LoopOutcome:
    """Observable result of one loop attempt. Not an IncidentOutcome DTO."""

    execution: LoopExecution
    handoff: bool
    handoff_reasons: tuple[str, ...]
    report: ReportV2 | None
    report_content: str | None
    report_content_sha256: str | None
    evidence_ids: tuple[str, ...]
    model_requests_used: int
    prompt_revision: str
    prompt_face_sha256: str
    question_sha256: str
    steps_committed: int
    # Terminal ``conclusion`` step committed for this attempt (None when the
    # store fenced it). The runner publishes ``conclusion`` against it; the
    # payload carries no private protocol fields.
    final_step_id: UUID | None = None
    conclusion: Mapping[str, Any] | None = None
    model_seconds_used: float = 0.0


@dataclass
class _State:
    """One attempt's in-memory working set; everything durable is in the store."""

    request: InvestigationRequest
    messages: list[dict[str, Any]]
    delivered: list[DeliveredView]
    evidence_ids: list[str]
    next_round: int
    segment: str
    used: int
    prior_active: float
    attempt_started: float
    revision: str
    face: str
    question_sha: str
    prefix_len: int
    calibration: float = 1.0
    folded_since: list[UUID] = field(default_factory=list)
    compactions: int = 0
    compaction_attempts: int = 0
    steps_committed: int = 0
    seconds_used: float = 0.0
    # Model seconds earlier attempts already put in the ledger; the Run-level
    # figures reported at the end are ``prior_model_seconds + seconds_used``,
    # cumulative like ``used`` (bot review finding, PR #29).
    prior_model_seconds: float = 0.0
    # The tool ledger's reading when this attempt opened; the live reading
    # minus it is the tool time this attempt has added.
    tool_seconds_at_open: float = 0.0
    last_step_id: UUID | None = None


@dataclass
class InvestigationLoop:
    """One worker-side attempt. Collaborators are injected; none are created here.

    ``run()`` starts a Run from its request; ``resume()`` continues one from a
    transcript rebuilt out of committed rows. Both share ``_drive``: the only
    difference is where the first messages and the used budget come from.
    """

    model: ModelClient
    executor: ReadOnlyToolExecutor
    store: StepCommitter
    clock: Clock
    accepted_response_model: str = ACCEPTED_RESPONSE_MODEL

    def _check_identity(self, request: InvestigationRequest) -> None:
        if not isinstance(request, InvestigationRequest):
            raise ValueError("INVALID_INPUT")
        if not isinstance(request.limits, RunLimits):
            raise ValueError("INVALID_INPUT")
        if (
            type(request.model_requests) is not int
            or request.model_requests < 1
            or request.model_requests > request.limits.model_requests
        ):
            raise ValueError("INVALID_INPUT")
        if (
            request.run_id != request.scope.run_id
            or request.scope.run_id != self.executor.scope.run_id
            or request.run_id != self.store.authorized_run_id
        ):
            raise ValueError("INVALID_INPUT")

    def _open_state(
        self,
        request: InvestigationRequest,
        *,
        messages: list[dict[str, Any]],
        delivered: list[DeliveredView],
        evidence_ids: list[str],
        next_round: int,
        segment: str,
        system: str,
        prefix_len: int,
        calibration: float = 1.0,
        folded_since: Sequence[UUID] = (),
        compactions: int = 0,
        compaction_attempts: int = 0,
    ) -> _State:
        usage = self.store.usage()
        return _State(
            request=request,
            messages=messages,
            delivered=delivered,
            evidence_ids=evidence_ids,
            next_round=next_round,
            segment=segment,
            used=usage.model_requests_used,
            # Active time already spent by earlier attempts: model requests
            # from the reservation ledger, tool time from the tool ledger the
            # executor was built from (C3 §13: restarts never reset it).
            prior_active=usage.model_seconds_used + self.executor.tool_seconds_used,
            prior_model_seconds=usage.model_seconds_used,
            tool_seconds_at_open=self.executor.tool_seconds_used,
            attempt_started=self.clock.monotonic(),
            revision=prompt_revision_versions(request.variant_id)["prompt_revision"],
            face=hashlib.sha256(system.encode("utf-8")).hexdigest(),
            question_sha=hashlib.sha256(request.question.encode("utf-8")).hexdigest(),
            prefix_len=prefix_len,
            calibration=calibration,
            folded_since=list(folded_since),
            compactions=compactions,
            compaction_attempts=compaction_attempts,
        )

    def run(self, request: InvestigationRequest) -> LoopOutcome:
        """Start a Run from its request (first attempt, no committed steps)."""
        self._check_identity(request)
        # Project once, here, before anything else reads it: every later use
        # of ``evidence_context`` (the prompt message and every citation
        # check below) sees only this Run's own, allowlisted v4 fields --
        # never a foreign context (bot review finding, PR #29) and never a
        # nested key that should not have reached the model (redline P3-4).
        request = replace(
            request,
            evidence_context=evidence_context_projection(
                request.evidence_context, run_id=request.run_id
            ),
        )
        messages, system = initial_messages(
            request.as_input(), evidence_context=request.evidence_context
        )
        delivered = delivered_from_context(
            request.evidence_context, run_id=request.run_id
        )
        state = self._open_state(
            request,
            messages=messages,
            delivered=list(delivered),
            evidence_ids=[view.evidence_id for view in delivered],
            next_round=1,
            segment=INITIAL_SEGMENT,
            system=system,
            prefix_len=len(messages),
        )
        return self._drive(state)

    def resume(self, transcript: Transcript) -> LoopOutcome:
        """Continue a Run from a transcript rebuilt out of committed rows.

        The transcript already excludes late results, superseded incomplete
        groups and revoked evidence; the budget continues from the store.
        """
        if not isinstance(transcript, Transcript):
            raise ValueError("INVALID_INPUT")
        input = transcript.input
        request = InvestigationRequest(
            run_id=transcript.run_id,
            question=input.question,
            scope=self.executor.scope,
            tool_schemas=tuple(input.tool_schemas),
            model_requests=input.model_requests,
            evidence_context=transcript.evidence_context,
            bound_target_id=input.bound_target_id,
            variant_id=input.variant_id,
            limits=input.limits,
        )
        self._check_identity(request)
        system = transcript.messages[0]["content"] if transcript.messages else ""
        if not isinstance(system, str) or not system:
            raise ValueError("INVALID_INPUT")
        state = self._open_state(
            request,
            messages=list(transcript.messages),
            delivered=list(transcript.delivered),
            evidence_ids=list(transcript.evidence_ids),
            next_round=transcript.next_round,
            segment=transcript.segment,
            system=system,
            prefix_len=transcript.prefix_len,
            calibration=transcript.calibration,
            folded_since=transcript.folded_since,
            compactions=transcript.compactions,
            compaction_attempts=transcript.compaction_attempts,
        )
        state.last_step_id = transcript.last_step_id
        last = transcript.last_round
        if last is not None and not last.concluded:
            # C3 §7 row 5: the last committed round already settles how this
            # attempt ends (an accepted report, a refused plan, a failed
            # final request); a restart between that row and the conclusion
            # row reaches the same verdict through the same rule as the live
            # round, without spending a request.
            verdict = self._round_verdict(
                state,
                tool_round=last.tool_round,
                content=last.content,
                finish_reason=last.finish_reason,
                final=last.final,
                rejection=last.rejection,
            )
            if verdict is not None:
                return self._finish(
                    state,
                    execution=verdict[0],
                    reasons=verdict[1],
                    report=verdict[2],
                    content=verdict[3],
                )
        return self._drive(state)

    def _round_verdict(
        self,
        state: _State,
        *,
        tool_round: bool,
        content: str | None,
        finish_reason: str | None,
        final: bool,
        rejection: str | None,
    ) -> tuple[LoopExecution, tuple[str, ...], ReportV2 | None, str | None] | None:
        """What one committed round settles for the attempt, or ``None`` when
        the loop goes on. The single rule for the live round and for a resume
        over that round's row."""
        if rejection is not None:
            return "failed", (rejection,), None, None
        if tool_round:
            return None
        report, reason = self._validated_report(
            state, content or "", finish_reason=finish_reason
        )
        if report is not None:
            reasons: tuple[str, ...] = (
                ("INCOMPLETE_INVESTIGATION",)
                if report.assessment_status == "incomplete"
                else ()
            )
            return "completed", reasons, report, content or ""
        if final:
            return "failed", (reason,), None, content
        return None

    def _validated_report(
        self, state: _State, content: str, *, finish_reason: str | None
    ) -> tuple[ReportV2 | None, str]:
        """``parse_report`` plus the citation check, as the live round applies them."""
        report, reason = parse_report(
            content, finish_reason=finish_reason if finish_reason else "unknown"
        )
        if report is None:
            return None, reason
        request = state.request
        if unsupported_citations(
            report,
            views=state.delivered,
            authorized_targets=request.scope.target_ids,
            time_policy_ids=context_time_policy_ids(request.evidence_context),
            target_catalog=context_target_catalog(
                request.evidence_context, authorized_targets=request.scope.target_ids
            ),
        ):
            return None, "REPORT_INVALID"
        return report, ""

    def _drive(self, state: _State) -> LoopOutcome:
        request = state.request
        try:
            while True:
                remaining = request.model_requests - state.used
                if remaining <= 0:
                    raise _LoopHalt("budget_exhausted", ("BUDGET_EXHAUSTED",))
                outcome = self._round(state, final=remaining <= 1)
                if outcome is not None:
                    return self._finish(
                        state,
                        execution=outcome[0],
                        reasons=outcome[1],
                        report=outcome[2],
                        content=outcome[3],
                    )
        except _LoopHalt as halt:
            return self._finish(
                state,
                execution=halt.execution,
                reasons=halt.reasons,
                report=None,
                content=None,
            )

    def _round(
        self, state: _State, *, final: bool
    ) -> tuple[LoopExecution, tuple[str, ...], ReportV2 | None, str | None] | None:
        request = state.request
        tools = None if final else tuple(request.tool_schemas)
        extra = (
            ({"role": "user", "content": FINAL_REPORT_INSTRUCTION},) if final else ()
        )
        self._manage_context(state, tools=tools, extra=extra)
        if not final and request.model_requests - state.used <= 1:
            # The compaction took a slot: what is left is the reserved
            # final-report request, so this round becomes it.
            final = True
            tools = None
            extra = ({"role": "user", "content": FINAL_REPORT_INSTRUCTION},)
        timeout = self._remaining_timeout(state)
        outbound = [*state.messages, *extra]
        call = ModelCall(
            messages=tuple(outbound),
            tools=tools,
            json_mode=final,
            max_tokens=request.limits.output_tokens,
            timeout_seconds=timeout,
            model=self.accepted_response_model,
        )
        self._reject_oversized(call, request.limits)
        logical_key = step_key(state.segment, state.next_round)
        try:
            reply, dispatched = self._call_model(
                call, state=state, logical_key=logical_key, final=final
            )
        except _RoundAborted:
            # Keep the last physical slot for the final-report request. The
            # logical round is consumed even though no step was committed;
            # a later attempt that rebuilds from rows may reuse the number,
            # which is safe because its reservations live in a new epoch.
            state.next_round += 1
            return None
        assistant = assistant_message(
            content=reply.content,
            reasoning_content=reply.reasoning_content,
            tool_calls=reply.tool_calls,
        )
        # Decide *before* committing whether the plan may ever execute. A
        # rejected plan is still persisted in full, but under ``rejected_plan``
        # rather than ``assistant.tool_calls``, so no later attempt can read
        # it back as pending work (bot/independent review finding).
        rejection: str | None = None
        calls: list[dict[str, Any]] = []
        try:
            calls = validate_tool_calls(assistant)
        except PairingError as exc:
            rejection = exc.code
        if rejection is None and final and calls:
            rejection = "TOOL_PLAN_ON_FINAL"
        if rejection is None and calls and reply.finish_reason != "tool_calls":
            rejection = (
                "OUTPUT_LENGTH"
                if reply.finish_reason == "length"
                else "TOOL_PAIRING_INVALID"
            )
        rejected_plan = None
        if rejection is not None:
            rejected_plan = {
                "reason": rejection,
                "tool_calls": assistant.pop("tool_calls", None),
            }
        step_id = self._commit_step(
            state,
            logical_key,
            assistant,
            reply,
            dispatched,
            final=final,
            rejected_plan=rejected_plan,
        )
        state.folded_since.append(step_id)
        state.next_round += 1
        verdict = self._round_verdict(
            state,
            tool_round=bool(calls),
            content=reply.content,
            finish_reason=reply.finish_reason,
            final=final,
            rejection=rejection,
        )
        if verdict is not None:
            return verdict
        if not calls:
            # Candidate text failed validation; keep the reserved last request.
            state.messages.append(assistant)
            return None
        results = self._run_tools(state, step_id, calls)
        try:
            group = pair_tool_results(assistant, results, require_reasoning=True)
        except PairingError as exc:
            raise _LoopHalt("failed", (exc.code,)) from exc
        state.messages.extend(group)
        return None

    def _run_tools(
        self,
        state: _State,
        step_id: UUID,
        calls: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        request = state.request
        results: list[dict[str, Any]] = []
        target_id = _bound_target(request)
        window = request.scope.window.as_json()
        for index, call in enumerate(calls):
            outcome = self.executor.execute(
                tool_request_for(
                    step_id, index, call, target_ref=target_id, window=window
                )
            )
            view = dict(outcome.model_view)
            try:
                self.store.commit_tool(step_id, index, view)
            except StepStoreError as exc:
                raise _halt_from_store(exc) from exc
            citation = delivered_view(
                view,
                evidence_context=request.evidence_context,
                authorized_targets=request.scope.target_ids,
            )
            if citation is not None:
                state.evidence_ids.append(citation.evidence_id)
                state.delivered.append(citation)
            results.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    # Mechanism 1: an oversized view reaches the model as a
                    # provenance stub; the row above keeps the full view.
                    "content": canonical(visible_view(view, limits=request.limits)),
                }
            )
        return results

    # -- context management (HolmesGPT mechanism 2, C3 §5) -------------------

    def _manage_context(
        self,
        state: _State,
        *,
        tools: tuple[Mapping[str, Any], ...] | None,
        extra: Sequence[Mapping[str, Any]],
    ) -> None:
        """Compact the history before a call that would not fit the budget.

        Threshold and insufficiency rule follow Holmes: compact when
        ``estimate + output allowance > budget * pct``; if the compacted
        context still does not fit, stop with ``CONTEXT_EXHAUSTED`` rather
        than send a request that silently truncates evidence references.
        A compaction is one physical model request charged to this Run, so it
        needs two remaining slots (its own and the next call's).
        """
        limits = state.request.limits
        budget = limits.context_tokens - limits.output_tokens
        projected = (
            estimate_tokens(state.messages, tools, extra=extra) * state.calibration
        )
        if projected <= budget * CONTEXT_POLICY.compaction_pct:
            return
        if state.request.model_requests - state.used < 2:
            raise _LoopHalt("failed", ("CONTEXT_EXHAUSTED",))
        self._compact(state)
        projected = (
            estimate_tokens(state.messages, tools, extra=extra) * state.calibration
        )
        if projected > budget:
            raise _LoopHalt("failed", ("CONTEXT_EXHAUSTED",))

    def _compact(self, state: _State) -> None:
        request = state.request
        revision = state.compactions + 1
        # Keyed by attempt, not by accepted revision: a refused row keeps its
        # key, and ``commit_step`` would hand that old row back for a reused
        # key while the in-memory context moved on (independent review).
        logical_key = f"{state.segment}:compact-{state.compaction_attempts + 1}"
        outbound = [
            *state.messages,
            {"role": "user", "content": COMPACTION_INSTRUCTION},
        ]
        call = ModelCall(
            messages=tuple(outbound),
            tools=None,
            json_mode=False,
            max_tokens=request.limits.output_tokens,
            timeout_seconds=self._remaining_timeout(state),
            model=self.accepted_response_model,
        )
        self._reject_oversized(call, request.limits)
        try:
            reply, dispatched = self._call_model(
                call, state=state, logical_key=logical_key, final=False
            )
        except _RoundAborted:
            # The retry would have taken the reserved final slot: the old
            # context stays as it is and the Run hands off (C3 §5).
            raise _LoopHalt("failed", ("COMPACTION_FAILED",)) from None
        summary = reply.content if isinstance(reply.content, str) else ""
        accepted = (
            not reply.tool_calls
            and reply.finish_reason == "stop"
            and bool(summary.strip())
        )
        folded = state.messages[state.prefix_len :]
        digest = fold_digest(folded)
        to_segment = f"ctx{revision}"
        estimated = estimate_tokens(dispatched.messages, dispatched.tools)
        state.calibration = calibrated(
            state.calibration, estimated, reply.usage.get("prompt_tokens")
        )
        payload = {
            "kind": COMPACTION_KIND,
            # A summary that called tools is rejected; its calls are kept for
            # the record but never in an executable position.
            "assistant": dict(
                assistant_message(
                    content=reply.content,
                    reasoning_content=reply.reasoning_content,
                    tool_calls=(),
                )
            ),
            "rejected_plan": None
            if not reply.tool_calls
            else {
                "reason": "COMPACTION_FAILED",
                "tool_calls": [dict(call) for call in reply.tool_calls],
            },
            "finish_reason": reply.finish_reason,
            "response_model": reply.response_model,
            "usage": dict(reply.usage),
            "request_sha256": hashlib.sha256(
                serialized_request(dispatched)
            ).hexdigest(),
            "context": {
                "segment": state.segment,
                "input_snapshot_hash": messages_hash(dispatched.messages),
                "final": False,
                "estimated_prompt_tokens": estimated,
                "calibration": state.calibration,
            },
            "compaction": {
                "revision": revision,
                "accepted": accepted,
                "from_segment": state.segment,
                "to_segment": to_segment,
                "folded_step_ids": [str(step_id) for step_id in state.folded_since],
                "digest": digest,
                "estimated_before": estimate_tokens(state.messages),
            },
        }
        try:
            self.store.commit_step(logical_key, payload)
        except StepStoreError as exc:
            raise _halt_from_store(exc) from exc
        state.steps_committed += 1
        state.compaction_attempts += 1
        if not accepted:
            # Keep the old context untouched and hand off; never continue on
            # a summary that is missing or tried to call tools.
            raise _LoopHalt("failed", ("COMPACTION_FAILED",))
        state.messages = [
            *state.messages[: state.prefix_len],
            compaction_message(revision, digest, summary),
        ]
        state.segment = to_segment
        state.folded_since = []
        state.compactions = revision

    def _reserve(self, run_id: str, logical_key: str, *, seconds: float) -> None:
        try:
            self.store.reserve_budget(
                reservation_id_for(run_id, logical_key), 1, seconds=seconds
            )
        except StepStoreError as exc:
            raise _halt_from_store(exc) from exc

    def _settle(
        self, run_id: str, logical_key: str, outcome: str, *, seconds: float | None
    ) -> None:
        try:
            self.store.settle_budget(
                reservation_id_for(run_id, logical_key), outcome, seconds=seconds
            )
        except StepStoreError as exc:
            if exc.code == "CONTROL_DENIED":
                # The lease was fenced while the request was in flight. The
                # reservation stays occupied (still counted against the
                # limit) and the next fenced write records the late response
                # as history and halts; settlement must not pre-empt that.
                return
            raise _halt_from_store(exc) from exc

    def _commit_step(
        self,
        state: _State,
        logical_key: str,
        assistant: Mapping[str, Any],
        reply: ModelReply,
        dispatched: ModelCall,
        *,
        final: bool,
        rejected_plan: Mapping[str, Any] | None = None,
    ) -> UUID:
        response_id = reply.raw.get("id")
        estimated = estimate_tokens(dispatched.messages, dispatched.tools)
        # The provider's own count calibrates the estimator for the rest of
        # this Run (and, through the row, for any later attempt).
        state.calibration = calibrated(
            state.calibration, estimated, reply.usage.get("prompt_tokens")
        )
        payload = {
            "assistant": dict(assistant),
            "finish_reason": reply.finish_reason,
            "response_model": reply.response_model,
            "usage": dict(reply.usage),
            "request_sha256": hashlib.sha256(
                serialized_request(dispatched)
            ).hexdigest(),
            "response_id": response_id if isinstance(response_id, str) else None,
            # C3 §7 step identity beyond the key: which context representation
            # this round saw, a hash of exactly what was sent, and the
            # measured-vs-estimated prompt size behind the context budget.
            "context": {
                "segment": state.segment,
                "round": state.next_round,
                "input_snapshot_hash": messages_hash(dispatched.messages),
                "final": final,
                "estimated_prompt_tokens": estimated,
                "calibration": state.calibration,
            },
        }
        if rejected_plan is not None:
            payload["rejected_plan"] = dict(rejected_plan)
        try:
            step_id = self.store.commit_step(logical_key, payload)
        except StepStoreError as exc:
            raise _halt_from_store(exc) from exc
        state.steps_committed += 1
        state.last_step_id = step_id
        return step_id

    def _call_model(
        self,
        call: ModelCall,
        *,
        state: _State,
        logical_key: str,
        final: bool,
    ) -> tuple[ModelReply, ModelCall]:
        request = state.request
        last_error: ModelError | None = None
        for attempt in (1, 2):
            # ``request.model_requests`` bounds the logical rounds this Run
            # plans; ``limits.model_requests`` is the physical ceiling a
            # provider retry may still use (the frozen 4 on the product path).
            if state.used >= request.limits.model_requests:
                raise _LoopHalt("budget_exhausted", ("BUDGET_EXHAUSTED",))
            timed = replace(call, timeout_seconds=self._remaining_timeout(state))
            reservation = f"{logical_key}#a{attempt}"
            self._reserve(request.run_id, reservation, seconds=timed.timeout_seconds)
            state.used += 1
            started = self.clock.monotonic()
            try:
                reply = self.model.complete(timed)
            except ModelError as exc:
                # The request went out and its cost is not known: the
                # reservation stays occupied as ``unknown`` at its reserved
                # upper bound (C3 §13).
                self._settle(request.run_id, reservation, "unknown", seconds=None)
                state.seconds_used += timed.timeout_seconds
                last_error = exc
                slots_left = request.model_requests - state.used
                preserve_final = not final and slots_left <= 1
                if preserve_final:
                    raise _RoundAborted() from exc
                if (
                    exc.code != "MODEL_UNAVAILABLE"
                    or attempt == 2
                    or state.used >= request.limits.model_requests
                ):
                    execution: LoopExecution = (
                        "budget_exhausted"
                        if exc.code == "BUDGET_EXHAUSTED"
                        else "failed"
                    )
                    raise _LoopHalt(execution, (exc.code,)) from exc
                continue
            # The provider answered: settle the reservation as real usage
            # before anything else can halt the round.
            elapsed = max(0.0, self.clock.monotonic() - started)
            self._settle(request.run_id, reservation, "spent", seconds=elapsed)
            state.seconds_used += elapsed
            if reply.response_model != self.accepted_response_model:
                raise _LoopHalt("failed", ("MODEL_IDENTITY_MISMATCH",))
            return reply, timed
        assert last_error is not None
        raise _LoopHalt("failed", (last_error.code,)) from last_error

    def _remaining_timeout(self, state: _State) -> float:
        request = state.request
        now = self.clock.now()
        remaining_deadline = (request.scope.deadline - now).total_seconds()
        # What this attempt has spent is the larger of its wall time and what
        # it charged to the ledger: a fast failure is charged its full timeout
        # (settled unknown), and the next request must fit under the ceiling
        # after that charge, or the durable usage overshoots ``active_seconds``
        # (bot review finding, PR #29).
        charged = state.seconds_used + (
            self.executor.tool_seconds_used - state.tool_seconds_at_open
        )
        attempt_spent = max(self.clock.monotonic() - state.attempt_started, charged)
        remaining_active = request.limits.active_seconds - (
            state.prior_active + attempt_spent
        )
        if remaining_deadline <= 0:
            raise _LoopHalt("failed", ("DEADLINE_EXCEEDED",))
        if remaining_active <= 0:
            raise _LoopHalt("failed", ("WALL_TIME_EXHAUSTED",))
        return min(
            request.limits.model_request_timeout_seconds,
            remaining_deadline,
            remaining_active,
        )

    def _reject_oversized(self, call: ModelCall, limits: RunLimits) -> None:
        if len(serialized_request(call)) > limits.request_bytes:
            raise _LoopHalt("failed", ("REQUEST_TOO_LARGE",))

    def _finish(
        self,
        state: _State,
        *,
        execution: LoopExecution,
        reasons: tuple[str, ...],
        report: ReportV2 | None,
        content: str | None,
    ) -> LoopOutcome:
        # Completed + incomplete finding still handoffs. Budget/pairing
        # failures never look like a completed investigation.
        handoff = execution != "completed" or bool(reasons)
        digest = (
            None
            if content is None
            else hashlib.sha256(content.encode("utf-8")).hexdigest()
        )
        evidence_ids = tuple(dict.fromkeys(state.evidence_ids))
        conclusion: dict[str, Any] = {
            "kind": CONCLUSION_KIND,
            # Shape a rebuild validates as a tool-free model step; the real
            # content lives under ``conclusion`` and carries no private
            # protocol fields, so it can be published and exported as is.
            "assistant": {"role": "assistant", "content": None},
            "conclusion": {
                "execution": execution,
                "handoff": handoff,
                "handoff_reasons": list(reasons),
                "report_schema_version": None
                if report is None
                else report.schema_version,
                "report_content": content,
                "report_content_sha256": digest,
                "evidence_ids": list(evidence_ids),
                "model_requests_used": state.used,
                "model_seconds_used": state.prior_model_seconds + state.seconds_used,
                "prompt_revision": state.revision,
                "prompt_face_sha256": state.face,
                "question_sha256": state.question_sha,
                "source_step_id": None
                if state.last_step_id is None
                else str(state.last_step_id),
                "segment": state.segment,
                "rounds": state.next_round - 1,
                "compactions": state.compactions,
                "calibration": state.calibration,
            },
        }
        final_step_id: UUID | None = None
        if not set(reasons) & _STORE_REFUSALS:
            # The key carries the control generation: after a follow-up
            # supersedes an unpublished conclusion, a resumed attempt that
            # halts before dispatch lands on the same segment/round, and
            # ``commit_step`` would otherwise hand back the old-generation
            # row, which can never be published (bot review finding).
            key = (
                f"conclusion:g{self.store.control_generation}:"
                f"{state.segment}:round-{state.next_round - 1}"
            )
            try:
                final_step_id = self.store.commit_step(key, conclusion)
            except StepStoreError:
                # Fenced or storage gone: the outcome still reports truthfully,
                # the runner just has nothing to publish against.
                final_step_id = None
        return LoopOutcome(
            execution=execution,
            handoff=handoff,
            handoff_reasons=reasons,
            report=report,
            report_content=content,
            report_content_sha256=digest,
            evidence_ids=evidence_ids,
            model_requests_used=state.used,
            prompt_revision=state.revision,
            prompt_face_sha256=state.face,
            question_sha256=state.question_sha,
            steps_committed=state.steps_committed,
            final_step_id=final_step_id,
            conclusion=conclusion if final_step_id is not None else None,
            model_seconds_used=state.prior_model_seconds + state.seconds_used,
        )


def tool_request_for(
    step_id: UUID | str,
    index: int,
    call: Mapping[str, Any],
    *,
    target_ref: str | None,
    window: Mapping[str, Any],
) -> ToolRequest:
    """The executor request for one committed tool call.

    Shared by the live loop and the recovery replay so a replayed call is
    the same operation (same step, same index, same untrusted arguments).
    """
    function = call["function"]
    return ToolRequest(
        step_id=str(step_id),
        tool_index=index,
        tool_name=function["name"],
        target_ref=target_ref,
        params=_parse_arguments(function["arguments"]),  # untrusted JSON
        window=window,
    )


@dataclass
class _LoopHalt(Exception):
    execution: LoopExecution
    reasons: tuple[str, ...]


class _RoundAborted(Exception):
    """This logical round gave up a retry so the reserved final request can run."""


def _halt_from_store(exc: StepStoreError) -> _LoopHalt:
    mapped = _HANDOFF_FROM_STORE.get(exc.code)
    if mapped is None:
        return _LoopHalt("failed", (exc.code,))
    return _LoopHalt(mapped[0], (mapped[1],))


def _bound_target(request: InvestigationRequest) -> str | None:
    if request.bound_target_id is not None:
        return request.bound_target_id
    if len(request.scope.target_ids) == 1:
        return next(iter(request.scope.target_ids))
    return None


def _parse_arguments(raw: str) -> object:
    """Keep model JSON intact, including reserved keys, so the executor denies them.

    ``json.loads`` raises ``RecursionError`` (not a ``ValueError``) on a
    pathologically deep container instead of a decode error -- the same
    stdlib gotcha PR #20's ``_result_rows`` (``opspilot/tools/executor.py``)
    hit for tool *response* bodies. ``None`` here already flows into
    ``_accept_params``/``_refuse`` as an ordinary malformed-params refusal.
    """
    try:
        parsed = json.loads(raw) if raw else {}
    except (ValueError, RecursionError):
        return None
    return parsed
