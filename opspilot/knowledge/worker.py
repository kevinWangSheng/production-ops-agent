"""The worker's postmortem pass (M1-03 step 2, F13, contract r3).

One pass: repair stale marks a business transaction missed (D18
compensation), then generate for up to ``batch`` candidates (D17, D27), one
at a time (D17: each worker runs at most one generation). One generation:

1. claim the incident's generation lease and open an attempt (D17, D22);
2. read the incident in one snapshot and build the model input (D21) --
   over a limit: the attempt fails ``INPUT_TOO_LARGE``, nothing generated;
3. one model call against the attempt's own budget (D23); if the reply is
   not the fixed JSON object (R1) or a citation fails (D2), one repair call;
4. reply still invalid: the attempt fails ``OUTPUT_INVALID`` (no version);
   citations still failing: a draft with those statements ``uncertain``
   that never enters review (D22); otherwise a version that enters review in
   the same transaction (D15) -- either way through
   ``KnowledgeStore.record_draft`` with the watermark read in step 2;
5. close the attempt and release the lease; a provider failure is recorded
   as ``MODEL_REJECTED`` / ``MODEL_UNAVAILABLE`` with bounded backoff (D22).
   The same write records each model request's finish reason, token split,
   outcome and problem codes (``model_calls``, r10 D54) -- never reply
   text, reasoning or prompt.

The generation never claims an investigation Run, and nothing here calls a
review primitive (``tests/test_knowledge_store_callers.py``). Logs carry
codes and ids only.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID, uuid4

from opspilot import tracing
from opspilot.investigation.limits import MODEL_REQUEST_TIMEOUT_SECONDS
from opspilot.investigation.loop import (
    ACCEPTED_RESPONSE_MODEL,
    ModelCall,
    ModelClient,
    ModelError,
    ModelReply,
)
from opspilot.knowledge.generation import (
    MAX_MODEL_REQUESTS_PER_GENERATION,
    MAX_OUTPUT_TOKENS,
    GenerationInput,
    InputTooLarge,
    ModelOutput,
    OutputInvalid,
    assemble,
    build_input,
    citation_errors,
    generation_versions,
    messages,
    parse_model_output,
    problem_codes,
    proposal_problems,
    repair_notes,
)
from opspilot.knowledge.jobs import Claim, GenerationStore
from opspilot.knowledge.store import Actor, KnowledgeStore
from opspilot.persistence import PersistenceError

_log = logging.getLogger("opspilot.postmortem")

# A provider's finish reason is a short code; anything longer is cut.
_MAX_FINISH_REASON = 64

# record_draft refusals that mean "the incident moved or another writer
# won": nothing to retry at this watermark.
_SUPERSEDED = {
    "WATERMARK_MOVED": "WATERMARK_MOVED",
    "GENERATION_CONFLICT": "GENERATION_CONFLICT",
    "OPEN_VERSION_EXISTS": "GENERATION_CONFLICT",
    "ILLEGAL_TRANSITION": "GENERATION_CONFLICT",
}


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def model_call(
    seq: int, reply: ModelReply | None, problems: list[str]
) -> dict[str, Any]:
    """One ``model_calls`` entry (r10, D54): what the provider reported,
    ``None`` where it reported nothing; ``outcome`` is settled by the
    caller. ``reply`` is ``None`` for a request that failed."""
    usage: Mapping[str, Any] = {} if reply is None else reply.usage
    details = usage.get("completion_tokens_details")
    completion = _count(usage.get("completion_tokens"))
    reasoning = (
        _count(details.get("reasoning_tokens"))
        if isinstance(details, Mapping)
        else None
    )
    finish = None if reply is None else reply.finish_reason
    return {
        "seq": seq,
        "purpose": "generate" if seq == 1 else "repair",
        "finish_reason": finish[:_MAX_FINISH_REASON]
        if isinstance(finish, str) and finish
        else None,
        "prompt_tokens": _count(usage.get("prompt_tokens")),
        "completion_tokens": completion,
        "reasoning_tokens": reasoning,
        "visible_completion_tokens": None
        if completion is None or reasoning is None
        else completion - reasoning,
        "outcome": "provider_error" if reply is None else "rejected",
        "problems": problems,
    }


class Events(Protocol):
    def append_once(
        self,
        subject_id: UUID,
        kind: str,
        payload: dict[str, Any],
        *,
        key: dict[str, Any] | None = None,
    ) -> int: ...


@dataclass(frozen=True)
class GenerationOutcome:
    incident_id: UUID
    attempt_id: UUID
    status: str  # contract AttemptStatus
    error_code: str | None = None
    version: int | None = None
    state: str | None = None
    postmortem_id: UUID | None = None
    generation: int | None = None
    model_requests: int = 0
    trace_id: str | None = None


@dataclass
class PostmortemWorker:
    knowledge: KnowledgeStore
    jobs: GenerationStore
    model: ModelClient
    events: Events | None = None
    owner: UUID = field(default_factory=uuid4)
    batch: int = 5
    stale_batch: int = 50
    model_name: str = ACCEPTED_RESPONSE_MODEL
    stop: threading.Event = field(default_factory=threading.Event)
    # test seam: what to build the input with (secrets default to the
    # process environment)
    input_builder: Callable[[dict[str, Any]], GenerationInput] = build_input

    @property
    def actor(self) -> Actor:
        return Actor(f"worker:{self.owner}", "worker")

    def poll_once(self) -> list[GenerationOutcome]:
        try:
            self.compensate_stale()
        except PersistenceError as exc:
            _log.warning("stale scan refused code=%s", exc)
        try:
            candidates = self.jobs.candidates(
                limit=self.batch,
                input_policy_version=generation_versions(self.model_name)[
                    "input_policy_version"
                ],
            )
        except PersistenceError as exc:
            _log.warning("candidate scan refused code=%s", exc)
            return []
        outcomes: list[GenerationOutcome] = []
        for incident_id in candidates:
            if self.stop.is_set():
                break
            try:
                outcome = self.generate(incident_id)
            except PersistenceError as exc:
                _log.warning("generation refused incident=%s code=%s", incident_id, exc)
                continue
            except Exception as exc:  # noqa: BLE001 - keep the worker alive
                # the lease lapses and the next claim closes the attempt as
                # abandoned; only the type is logged
                _log.error(
                    "generation crashed incident=%s error=%s",
                    incident_id,
                    type(exc).__name__,
                )
                continue
            if outcome is not None:
                _log.info(
                    "postmortem incident=%s attempt=%s status=%s code=%s version=%s",
                    incident_id,
                    outcome.attempt_id,
                    outcome.status,
                    outcome.error_code,
                    outcome.version,
                )
                outcomes.append(outcome)
        return outcomes

    def run(self, poll_seconds: float) -> None:
        while not self.stop.is_set():
            self.poll_once()
            self.stop.wait(poll_seconds)

    def compensate_stale(self) -> int:
        """D18 compensation: mark what the business transactions missed."""
        marked = 0
        for row in self.jobs.stale_versions(limit=self.stale_batch):
            try:
                result = self.knowledge.mark_stale(
                    row["postmortem_id"],
                    row["version"],
                    reason=row["reason"],
                    expected_generation=row["generation"],
                    idempotency_key=f"stale-scan:{row['postmortem_id']}:{row['version']}",
                    actor=self.actor,
                )
            except PersistenceError as exc:
                # moved on concurrently; the next pass sees the new state
                _log.info("stale scan skipped version code=%s", exc)
                continue
            marked += 1
            self._event(
                row["incident_id"],
                "postmortem_stale",
                {
                    "postmortem_id": str(result.object_id),
                    "version": result.version,
                    "state": result.state,
                    "generation": result.generation,
                    "reason": row["reason"],
                },
            )
        return marked

    def generate(self, incident_id: UUID) -> GenerationOutcome | None:
        versions = generation_versions(self.model_name)
        claim = self.jobs.claim(
            incident_id,
            owner=self.owner,
            versions=versions,
            max_model_requests=MAX_MODEL_REQUESTS_PER_GENERATION,
        )
        if claim is None:
            return None
        with tracing.tracer().generation(
            incident_id=incident_id, attempt_id=claim.attempt_id, versions=versions
        ) as span:
            outcome = self._attempt(claim)
            span.settle(outcome.status, outcome.error_code)
            trace_id = span.trace_id
        outcome = GenerationOutcome(**{**outcome.__dict__, "trace_id": trace_id})
        if outcome.status == "succeeded":
            self._event(
                incident_id,
                "postmortem_generated",
                {
                    "postmortem_id": str(outcome.postmortem_id),
                    "version": outcome.version,
                    "state": outcome.state,
                    "generation": outcome.generation,
                },
            )
        elif outcome.status == "failed":
            self._event(
                incident_id,
                "postmortem_generation_failed",
                {"attempt_id": str(claim.attempt_id), "reason": outcome.error_code},
            )
        return outcome

    def _attempt(self, claim: Claim) -> GenerationOutcome:
        raw = self.jobs.read_input(claim.incident_id)
        watermark = raw["watermark"]
        try:
            built = self.input_builder(raw)
        except InputTooLarge as exc:
            _log.warning(
                "postmortem input over limit incident=%s limit=%s",
                claim.incident_id,
                exc.limit,
            )
            return self._finish(claim, "failed", watermark, "INPUT_TOO_LARGE")
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        common: dict[str, Any] = {
            "input_sha256": built.payload_sha256,
            "input_bytes": built.payload_bytes,
        }
        used = 0
        reply: ModelReply | None = None
        output: ModelOutput | None = None
        problems: list[str] = []
        errors: dict[str, list[str]] = {}
        repaired = False
        calls: list[dict[str, Any]] = []
        common["model_calls"] = calls
        try:
            for round_ in range(claim.max_model_requests):
                if round_ and not (problems or errors):
                    break
                notes = problems + repair_notes(errors)
                used = self.jobs.reserve_model_request(claim)
                if calls:
                    calls[-1]["outcome"] = "repair_requested"
                try:
                    reply = self.model.complete(
                        ModelCall(
                            messages=messages(
                                built,
                                repair=notes if round_ else (),
                                previous=None if reply is None else reply.content,
                            ),
                            tools=None,
                            json_mode=True,
                            max_tokens=MAX_OUTPUT_TOKENS,
                            timeout_seconds=MODEL_REQUEST_TIMEOUT_SECONDS,
                            model=self.model_name,
                        )
                    )
                except ModelError:
                    calls.append(model_call(len(calls) + 1, None, []))
                    raise
                repaired = round_ > 0
                for key in usage:
                    value = reply.usage.get(key)
                    if isinstance(value, int) and not isinstance(value, bool):
                        usage[key] += value
                try:
                    output = parse_model_output(reply.content)
                    problems = proposal_problems(
                        output, built.catalog, built.published_entries
                    )
                    errors = {} if problems else citation_errors(output, built.catalog)
                except OutputInvalid as exc:
                    output, problems, errors = None, list(exc.problems), {}
                calls.append(
                    model_call(len(calls) + 1, reply, problem_codes(problems, errors))
                )
            if output is not None and not problems and calls:
                # used as written: a version, or a draft whose citations failed
                calls[-1]["outcome"] = "accepted"
        except ModelError as exc:
            code = (
                "MODEL_REJECTED"
                if exc.code == "MODEL_REJECTED"
                else "MODEL_UNAVAILABLE"
            )
            return self._finish(
                claim, "failed", watermark, code, used=used, usage=usage, **common
            )
        except PersistenceError as exc:
            if str(exc) not in ("LEASE_LOST", "BUDGET_EXHAUSTED"):
                raise
            return self._finish(
                claim,
                "superseded",
                watermark,
                "LEASE_LOST",
                used=used,
                usage=usage,
                **common,
            )
        response_model = None if reply is None else reply.response_model
        if output is None or problems:
            return self._finish(
                claim,
                "failed",
                watermark,
                "OUTPUT_INVALID",
                used=used,
                usage=usage,
                response_model=response_model,
                **common,
            )
        record = {
            "attempt_id": str(claim.attempt_id),
            **generation_versions(self.model_name),
            "response_model": response_model,
            "input_sha256": built.payload_sha256,
            "input_bytes": built.payload_bytes,
            "model_requests": used,
            "max_model_requests": claim.max_model_requests,
            "usage": dict(usage),
            "repaired": repaired,
            "revises_version": built.revises_version,
            "return_reason": built.return_reason,
        }
        draft = assemble(built, output, errors, record=record)
        try:
            result = self.knowledge.record_draft(
                claim.incident_id,
                expected_generation=built.expected_generation,
                idempotency_key=f"generate:{claim.attempt_id}",
                actor=self.actor,
                content=draft.content,
                watermark=built.watermark,
                conclusions=draft.conclusions,
                proposals=draft.proposals,
                disputes=draft.disputes,
                revises_version=built.revises_version,
            )
        except PersistenceError as exc:
            superseded = _SUPERSEDED.get(str(exc))
            if superseded is None:
                raise
            return self._finish(
                claim,
                "superseded",
                watermark,
                superseded,
                used=used,
                usage=usage,
                response_model=response_model,
                **common,
            )
        return self._finish(
            claim,
            "succeeded",
            watermark,
            None,
            version=result.version,
            state=result.state,
            generation=result.generation,
            postmortem_id=result.object_id,
            citations_failed=not draft.citations_valid,
            used=used,
            usage=usage,
            response_model=response_model,
            **common,
        )

    def _finish(
        self,
        claim: Claim,
        status: str,
        watermark: Any,
        error_code: str | None,
        *,
        version: int | None = None,
        state: str | None = None,
        generation: int | None = None,
        postmortem_id: UUID | None = None,
        citations_failed: bool = False,
        used: int = 0,
        usage: dict[str, int] | None = None,
        response_model: str | None = None,
        input_sha256: str | None = None,
        input_bytes: int | None = None,
        model_calls: list[dict[str, Any]] | None = None,
    ) -> GenerationOutcome:
        usage = usage or {}
        try:
            self.jobs.finish(
                claim,
                status=status,
                watermark=watermark,
                error_code=error_code,
                version=version,
                response_model=response_model,
                input_sha256=input_sha256,
                input_bytes=input_bytes,
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                citations_failed=citations_failed,
                model_calls=model_calls or [],
            )
        except PersistenceError as exc:
            if str(exc) != "LEASE_LOST":
                raise
            _log.warning("generation lease lost incident=%s", claim.incident_id)
        return GenerationOutcome(
            incident_id=claim.incident_id,
            attempt_id=claim.attempt_id,
            status=status,
            error_code=error_code,
            version=version,
            state=state,
            postmortem_id=postmortem_id,
            generation=generation,
            model_requests=used,
        )

    def _event(
        self,
        incident_id: UUID,
        kind: str,
        payload: dict[str, Any],
    ) -> None:
        """R6: ids, version, state, generation, reason only; a projection
        (best-effort after commit), never authority."""
        if self.events is None:
            return
        try:
            self.events.append_once(incident_id, kind, payload, key=dict(payload))
        except Exception as exc:  # noqa: BLE001 - the event log is a projection
            _log.warning(
                "postmortem event not appended kind=%s error=%s",
                kind,
                type(exc).__name__,
            )


__all__ = ["GenerationOutcome", "PostmortemWorker"]
