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

from opspilot.instructions.discipline import prompt_revision, render
from opspilot.investigation.limits import (
    MAX_HTTP_REQUEST_BYTES,
    MAX_MODEL_REQUESTS_PER_RUN,
    MAX_OUTPUT_TOKENS,
    MODEL_REQUEST_TIMEOUT_SECONDS,
    RUN_WALL_SECONDS,
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
    eligible_time_policies,
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

    Known gap, not fixed here: ``discipline.template_projection`` in this
    branch only hashes ``LAYER_TEMPLATE`` segments, so reordering an L1b/L2
    slot relative to the L1a segments would change ``render()``'s actual
    bytes without moving this value (PR #27, not yet merged, already fixes
    this upstream; porting that fix here would re-copy an un-merged PR's
    implementation rather than depend on it, so it is deferred to the #27 ->
    #29 merge, per this branch's dependency convention -- not silently
    ignored). No known variant in this branch has ever reordered those
    segments, so this does not affect any revision value recorded so far.
    """
    return {
        "prompt_revision": prompt_revision(variant_id, report_contract=report_contract)
    }


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


@dataclass
class InvestigationLoop:
    """One worker-side attempt. Collaborators are injected; none are created here."""

    model: ModelClient
    executor: ReadOnlyToolExecutor
    store: StepCommitter
    clock: Clock
    accepted_response_model: str = ACCEPTED_RESPONSE_MODEL
    _physical_requests: int = field(default=0, init=False, repr=False)
    _steps_committed: int = field(default=0, init=False, repr=False)

    def run(self, request: InvestigationRequest) -> LoopOutcome:
        if not isinstance(request, InvestigationRequest):
            raise ValueError("INVALID_INPUT")
        if (
            type(request.model_requests) is not int
            or request.model_requests < 1
            or request.model_requests > MAX_MODEL_REQUESTS_PER_RUN
        ):
            raise ValueError("INVALID_INPUT")
        if (
            request.run_id != request.scope.run_id
            or request.scope.run_id != self.executor.scope.run_id
            or request.run_id != self.store.authorized_run_id
        ):
            raise ValueError("INVALID_INPUT")
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
        started = self.clock.monotonic()
        system = render(
            request.variant_id,
            model_requests=request.model_requests,
            report_contract=REPORT_CONTRACT,
        )
        revision = prompt_revision_versions(request.variant_id)["prompt_revision"]
        face = hashlib.sha256(system.encode("utf-8")).hexdigest()
        question_sha = hashlib.sha256(request.question.encode("utf-8")).hexdigest()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": request.question},
        ]
        if request.evidence_context is not None:
            messages.append(
                {
                    "role": "user",
                    "content": canonical(request.evidence_context),
                }
            )
        evidence_ids: list[str] = []
        delivered: list[DeliveredView] = delivered_from_context(
            request.evidence_context, run_id=request.run_id
        )
        evidence_ids.extend(view.evidence_id for view in delivered)
        self._physical_requests = 0
        self._steps_committed = 0
        try:
            for ordinal in range(1, request.model_requests + 1):
                remaining = request.model_requests - self._physical_requests
                if remaining <= 0:
                    raise _LoopHalt("budget_exhausted", ("BUDGET_EXHAUSTED",))
                final = ordinal == request.model_requests or remaining <= 1
                outcome = self._round(
                    request,
                    messages,
                    ordinal=ordinal,
                    final=final,
                    started=started,
                    evidence_ids=evidence_ids,
                    delivered=delivered,
                )
                if outcome is not None:
                    return self._finish(
                        execution=outcome[0],
                        reasons=outcome[1],
                        report=outcome[2],
                        content=outcome[3],
                        evidence_ids=evidence_ids,
                        used=self._physical_requests,
                        steps=self._steps_committed,
                        revision=revision,
                        face=face,
                        question_sha=question_sha,
                    )
        except _LoopHalt as halt:
            return self._finish(
                execution=halt.execution,
                reasons=halt.reasons,
                report=None,
                content=None,
                evidence_ids=evidence_ids,
                used=self._physical_requests,
                steps=self._steps_committed,
                revision=revision,
                face=face,
                question_sha=question_sha,
            )
        return self._finish(
            execution="failed",
            reasons=("EMPTY_REPORT",),
            report=None,
            content=None,
            evidence_ids=evidence_ids,
            used=self._physical_requests,
            steps=self._steps_committed,
            revision=revision,
            face=face,
            question_sha=question_sha,
        )

    def _round(
        self,
        request: InvestigationRequest,
        messages: list[dict[str, Any]],
        *,
        ordinal: int,
        final: bool,
        started: float,
        evidence_ids: list[str],
        delivered: list[DeliveredView],
    ) -> tuple[LoopExecution, tuple[str, ...], ReportV2 | None, str | None] | None:
        timeout = self._remaining_timeout(request, started)
        outbound = list(messages)
        if final:
            outbound.append({"role": "user", "content": FINAL_REPORT_INSTRUCTION})
        call = ModelCall(
            messages=tuple(outbound),
            tools=None if final else tuple(request.tool_schemas),
            json_mode=final,
            max_tokens=MAX_OUTPUT_TOKENS,
            timeout_seconds=timeout,
            model=self.accepted_response_model,
        )
        self._reject_oversized(call)
        logical_key = f"round-{ordinal}"
        try:
            reply, dispatched = self._call_model(
                call,
                request=request,
                started=started,
                logical_key=logical_key,
                final=final,
            )
        except _RoundAborted:
            # Keep the last physical slot for the final-report request.
            return None
        assistant = assistant_message(
            content=reply.content,
            reasoning_content=reply.reasoning_content,
            tool_calls=reply.tool_calls,
        )
        step_id = self._commit_step(logical_key, assistant, reply, dispatched)
        try:
            calls = validate_tool_calls(assistant)
        except PairingError as exc:
            raise _LoopHalt("failed", (exc.code,)) from exc
        if final and calls:
            raise _LoopHalt("failed", ("TOOL_PLAN_ON_FINAL",))
        if calls and reply.finish_reason != "tool_calls":
            reason = (
                "OUTPUT_LENGTH"
                if reply.finish_reason == "length"
                else "TOOL_PAIRING_INVALID"
            )
            raise _LoopHalt("failed", (reason,))
        if not calls:
            report, reason = parse_report(
                reply.content, finish_reason=reply.finish_reason
            )
            if report is not None and unsupported_citations(
                report,
                views=delivered,
                authorized_targets=request.scope.target_ids,
                time_policy_ids=context_time_policy_ids(request.evidence_context),
                target_catalog=context_target_catalog(
                    request.evidence_context,
                    authorized_targets=request.scope.target_ids,
                ),
            ):
                report, reason = None, "REPORT_INVALID"
            if report is not None:
                content = reply.content or ""
                if report.assessment_status == "incomplete":
                    return (
                        "completed",
                        ("INCOMPLETE_INVESTIGATION",),
                        report,
                        content,
                    )
                return "completed", (), report, content
            if final:
                return "failed", (reason,), None, reply.content
            # Candidate text failed validation; keep the reserved last request.
            messages.append(assistant)
            return None
        results = self._run_tools(request, step_id, calls, evidence_ids, delivered)
        try:
            group = pair_tool_results(assistant, results, require_reasoning=True)
        except PairingError as exc:
            raise _LoopHalt("failed", (exc.code,)) from exc
        messages.extend(group)
        return None

    def _run_tools(
        self,
        request: InvestigationRequest,
        step_id: UUID,
        calls: Sequence[Mapping[str, Any]],
        evidence_ids: list[str],
        delivered: list[DeliveredView],
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        target_id = _bound_target(request)
        window = request.scope.window.as_json()
        for index, call in enumerate(calls):
            function = call["function"]
            params = _parse_arguments(function["arguments"])  # untrusted JSON
            tool_request = ToolRequest(
                step_id=str(step_id),
                tool_index=index,
                tool_name=function["name"],
                target_ref=target_id,
                params=params,
                window=window,
            )
            outcome = self.executor.execute(tool_request)
            view = dict(outcome.model_view)
            try:
                self.store.commit_tool(step_id, index, view)
            except StepStoreError as exc:
                raise _halt_from_store(exc) from exc
            evidence_id = view.get("evidence_id")
            if outcome.adopted and isinstance(evidence_id, str) and evidence_id:
                evidence_ids.append(evidence_id)
                target = view.get("target_id")
                registry = target if isinstance(target, str) else None
                catalog = context_target_catalog(
                    request.evidence_context,
                    authorized_targets=request.scope.target_ids,
                )
                aliases = frozenset(
                    key for key, mapped in catalog.items() if mapped == registry
                )
                target_ids = (
                    frozenset({registry}) if registry else frozenset()
                ) | aliases
                ctx = request.evidence_context
                delivered.append(
                    DeliveredView(
                        evidence_id=evidence_id,
                        target_ids=target_ids,
                        status=outcome.status,
                        time_scope_refs=eligible_time_policies(
                            ctx.get("time_policies")
                            if isinstance(ctx, Mapping)
                            else None,
                            source=view.get("source"),
                            tool=view.get("tool"),
                            target_ids=target_ids,
                            window=view.get("window"),
                            freshness_seconds=view.get("freshness_seconds"),
                            source_start_at=view.get("source_start_at"),
                            source_end_at=view.get("source_end_at"),
                            reference_at=view.get("observed_at"),
                        ),
                    )
                )
            results.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": canonical(view),
                }
            )
        return results

    def _reserve(self, run_id: str, logical_key: str) -> None:
        try:
            self.store.reserve_budget(reservation_id_for(run_id, logical_key), 1)
        except StepStoreError as exc:
            raise _halt_from_store(exc) from exc

    def _settle(self, run_id: str, logical_key: str, outcome: str) -> None:
        try:
            self.store.settle_budget(reservation_id_for(run_id, logical_key), outcome)
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
        logical_key: str,
        assistant: Mapping[str, Any],
        reply: ModelReply,
        dispatched: ModelCall,
    ) -> UUID:
        response_id = reply.raw.get("id")
        payload = {
            "assistant": dict(assistant),
            "finish_reason": reply.finish_reason,
            "response_model": reply.response_model,
            "usage": dict(reply.usage),
            "request_sha256": hashlib.sha256(
                serialized_request(dispatched)
            ).hexdigest(),
            "response_id": response_id if isinstance(response_id, str) else None,
        }
        try:
            step_id = self.store.commit_step(logical_key, payload)
        except StepStoreError as exc:
            raise _halt_from_store(exc) from exc
        self._steps_committed += 1
        return step_id

    def _call_model(
        self,
        call: ModelCall,
        *,
        request: InvestigationRequest,
        started: float,
        logical_key: str,
        final: bool,
    ) -> tuple[ModelReply, ModelCall]:
        last_error: ModelError | None = None
        for attempt in (1, 2):
            if self._physical_requests >= MAX_MODEL_REQUESTS_PER_RUN:
                raise _LoopHalt("budget_exhausted", ("BUDGET_EXHAUSTED",))
            timed = replace(
                call, timeout_seconds=self._remaining_timeout(request, started)
            )
            reservation = f"{logical_key}#a{attempt}"
            self._reserve(request.run_id, reservation)
            self._physical_requests += 1
            try:
                reply = self.model.complete(timed)
            except ModelError as exc:
                # The request went out and its cost is not known: the
                # reservation stays occupied as ``unknown`` (C3 §13).
                self._settle(request.run_id, reservation, "unknown")
                last_error = exc
                slots_left = request.model_requests - self._physical_requests
                preserve_final = not final and slots_left <= 1
                if preserve_final:
                    raise _RoundAborted() from exc
                if (
                    exc.code != "MODEL_UNAVAILABLE"
                    or attempt == 2
                    or self._physical_requests >= MAX_MODEL_REQUESTS_PER_RUN
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
            self._settle(request.run_id, reservation, "spent")
            if reply.response_model != self.accepted_response_model:
                raise _LoopHalt("failed", ("MODEL_IDENTITY_MISMATCH",))
            return reply, timed
        assert last_error is not None
        raise _LoopHalt("failed", (last_error.code,)) from last_error

    def _remaining_timeout(
        self, request: InvestigationRequest, started: float
    ) -> float:
        now = self.clock.now()
        remaining_deadline = (request.scope.deadline - now).total_seconds()
        remaining_wall = RUN_WALL_SECONDS - (self.clock.monotonic() - started)
        if remaining_deadline <= 0:
            raise _LoopHalt("failed", ("DEADLINE_EXCEEDED",))
        if remaining_wall <= 0:
            raise _LoopHalt("failed", ("WALL_TIME_EXHAUSTED",))
        return min(MODEL_REQUEST_TIMEOUT_SECONDS, remaining_deadline, remaining_wall)

    def _reject_oversized(self, call: ModelCall) -> None:
        if len(serialized_request(call)) > MAX_HTTP_REQUEST_BYTES:
            raise _LoopHalt("failed", ("REQUEST_TOO_LARGE",))

    def _finish(
        self,
        *,
        execution: LoopExecution,
        reasons: tuple[str, ...],
        report: ReportV2 | None,
        content: str | None,
        evidence_ids: Sequence[str],
        used: int,
        steps: int,
        revision: str,
        face: str,
        question_sha: str,
    ) -> LoopOutcome:
        # Completed + incomplete finding still handoffs. Budget/pairing
        # failures never look like a completed investigation.
        handoff = execution != "completed" or bool(reasons)
        digest = (
            None
            if content is None
            else hashlib.sha256(content.encode("utf-8")).hexdigest()
        )
        return LoopOutcome(
            execution=execution,
            handoff=handoff,
            handoff_reasons=reasons,
            report=report,
            report_content=content,
            report_content_sha256=digest,
            evidence_ids=tuple(dict.fromkeys(evidence_ids)),
            model_requests_used=used,
            prompt_revision=revision,
            prompt_face_sha256=face,
            question_sha256=question_sha,
            steps_committed=steps,
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
    """Keep model JSON intact, including reserved keys, so the executor denies them."""
    try:
        parsed = json.loads(raw) if raw else {}
    except ValueError:
        return None
    return parsed
