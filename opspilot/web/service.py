"""The workbench: intake, human control, snapshots and one bounded Run.

This is the composition point between the authenticated entry points and
the business-state authority. It never resolves a target, never calls a
model and never decides state on its own: ``IncidentStore`` decides, the
``EventLog`` reflects, and the ``Investigator`` collaborator supplies the
loop (real or deterministic stand-in) for ``run_once``.

Ordering rule for every mutation: the idempotency row (which carries the
operator's content) is inserted *before* the business write, so a retry
after a lost acknowledgement finds the content and can re-run the
idempotent business step; the event-log append comes last and is only a
projection.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4, uuid5

from opspilot.intake import (
    IntakeEnvelope,
    IntakeRequest,
    Principal,
    _reject_ambiguous_identifier,
    _reject_ambiguous_text,
    classify_intake_delivery,
)
from opspilot.investigation.limits import MAX_MODEL_REQUESTS_PER_RUN, RUN_WALL_SECONDS
from opspilot.investigation.loop import LoopOutcome
from opspilot.investigation.reports import ReportV2, parse_report
from opspilot.investigation.store import StepCommitter, StepStoreError
from opspilot.persistence import Lease, PersistenceError
from opspilot.tools.executor import EvidenceSink
from opspilot.web.events import EventLog, SubjectEvent
from opspilot.web.evidence import EvidenceStore, StoredEvidence
from opspilot.web.store import IncidentStore, IncidentSummary, WebLedger

_INTAKE_NAMESPACE = UUID("0f4c9d3e-2b7a-4a6e-9c1d-5e8f7a6b3c21")
_MAX_TEXT = 16_384
_EVENT_PAGE = 1000

ControlAction = Literal["follow_up", "correct", "cancel", "pause", "resume", "new_run"]
CONTROL_ACTIONS: frozenset[str] = frozenset(
    {"follow_up", "correct", "cancel", "pause", "resume", "new_run"}
)
_TEXT_ACTIONS = frozenset({"follow_up", "correct"})


class WorkbenchError(Exception):
    """Fixed-code refusal from the workbench; ``code`` is safe to render."""

    def __init__(self, code: str, *, current_generation: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.current_generation = current_generation


@dataclass(frozen=True)
class IntakeResult:
    incident_id: UUID
    run_id: UUID
    replayed: bool
    sequence: int


@dataclass(frozen=True)
class ControlResult:
    incident_id: UUID
    action: str
    generation: int
    replayed: bool
    sequence: int


@dataclass(frozen=True)
class RunContext:
    """Everything the investigator may know about the Run it is executing."""

    incident_id: UUID
    run_id: UUID
    control_generation: int
    question: str
    target_id: str
    deadline: datetime
    budget_limit: int


class Investigator(Protocol):
    def investigate(
        self, context: RunContext, committer: StepCommitter, evidence: EvidenceSink
    ) -> LoopOutcome: ...


class _EmittingCommitter:
    """Wrap the loop's committer so each committed step/tool becomes an event.

    The event is appended after the business commit returns, so a reader
    never sees progress that did not persist. Refusals pass through
    untouched: the loop's handoff semantics stay the loop's.
    """

    def __init__(
        self, base: StepCommitter, events: EventLog, subject_id: UUID, run_id: UUID
    ) -> None:
        self._base = base
        self._events = events
        self._subject_id = subject_id
        self._run_id = run_id
        self.last_step: tuple[UUID, dict[str, Any]] | None = None
        self.steps_seen = 0

    @property
    def authorized_run_id(self) -> str:
        return self._base.authorized_run_id

    def reserve_budget(self, reservation_id: UUID, amount: int) -> None:
        self._base.reserve_budget(reservation_id, amount)

    # PR #29 adds ``settle_budget`` to the committer seam; forwarded the same
    # way as the PR #31 methods above until that protocol lands on this base.
    def settle_budget(self, reservation_id: UUID, outcome: str) -> None:
        getattr(self._base, "settle_budget")(reservation_id, outcome)

    # PR #31 extends the committer seam with ``begin_round``/``assert_current``.
    # Forward them when the base has them so this wrapper stays transparent
    # after that merge; on this branch the loop never calls them.
    def begin_round(self, logical_key: str) -> Any:
        return getattr(self._base, "begin_round")(logical_key)

    def assert_current(self) -> None:
        getattr(self._base, "assert_current")()

    def commit_step(self, logical_key: str, response: Mapping[str, Any]) -> UUID:
        step_id = self._base.commit_step(logical_key, response)
        self.last_step = (step_id, dict(response))
        self.steps_seen += 1
        assistant = response.get("assistant")
        calls = assistant.get("tool_calls") if isinstance(assistant, Mapping) else None
        self._events.append(
            self._subject_id,
            "step_committed",
            {
                "run_id": str(self._run_id),
                "step_id": str(step_id),
                "logical_key": logical_key,
                "finish_reason": response.get("finish_reason"),
                "planned_tools": len(calls) if isinstance(calls, list) else 0,
            },
        )
        return step_id

    def commit_tool(
        self, step_id: UUID, ordinal: int, result: Mapping[str, Any]
    ) -> None:
        self._base.commit_tool(step_id, ordinal, result)
        self._events.append(
            self._subject_id,
            "tool_committed",
            {
                "run_id": str(self._run_id),
                "step_id": str(step_id),
                "ordinal": ordinal,
                "evidence_id": result.get("evidence_id"),
                "status": result.get("status"),
                "adopted": result.get("adopted"),
                "tool": result.get("tool"),
                "target_id": result.get("target_id"),
            },
        )


@dataclass
class Workbench:
    incidents: IncidentStore
    events: EventLog
    evidence: EvidenceStore
    ledger: WebLedger
    run_versions: dict[str, str]
    budget_limit: int = MAX_MODEL_REQUESTS_PER_RUN
    run_seconds: float = RUN_WALL_SECONDS
    _owner: UUID = field(default_factory=uuid4)

    # -- intake ---------------------------------------------------------

    def submit(self, envelope: IntakeEnvelope) -> IntakeResult:
        envelope = IntakeEnvelope.model_validate(envelope)
        key = f"intake:{envelope.request.idempotency_key}"
        incident_id = uuid5(_INTAKE_NAMESPACE, key)
        run_id = uuid5(_INTAKE_NAMESPACE, f"{key}:run:0")
        # Ledger first: whoever inserts the row owns the key's content. A
        # loser, or a retry after a lost acknowledgement, is compared against
        # the stored envelope rather than trusted.
        stored, inserted = self.ledger.put(
            "intake",
            key,
            {
                "incident_id": str(incident_id),
                "run_id": str(run_id),
                "envelope": _envelope_json(envelope),
            },
        )
        if not inserted:
            previous = _envelope_from_json(stored["envelope"])
            if classify_intake_delivery(previous, envelope) != "same_request":
                raise WorkbenchError("INTAKE_KEY_CONFLICT")
        # accept() is idempotent on (incident_id, run_id, key); calling it on
        # the replay path closes the crash window between ledger and accept.
        now = self.incidents.now()
        self.incidents.accept(
            incident_id,
            run_id,
            key,
            deadline=now + timedelta(seconds=self.run_seconds),
            budget_limit=self.budget_limit,
            versions=dict(self.run_versions),
        )
        if not inserted:
            return IntakeResult(
                incident_id, run_id, True, self.events.latest(incident_id)
            )
        sequence = self.events.append(
            incident_id,
            "intake_accepted",
            {
                "run_id": str(run_id),
                "request_id": envelope.request_id,
                "actor_id": envelope.principal.actor_id,
                "channel": envelope.principal.channel,
                "target_id": envelope.request.target_id,
                "question": envelope.request.question,
                "received_at": envelope.received_at.isoformat(),
            },
        )
        return IntakeResult(incident_id, run_id, False, sequence)

    # -- human control --------------------------------------------------

    def control(
        self,
        incident_id: UUID,
        *,
        actor_id: str,
        action: str,
        expected_generation: int,
        idempotency_key: str,
        text: str | None = None,
    ) -> ControlResult:
        if action not in CONTROL_ACTIONS:
            raise WorkbenchError("INVALID_ACTION")
        if type(expected_generation) is not int or expected_generation < 0:
            raise WorkbenchError("INVALID_INPUT")
        try:
            _reject_ambiguous_identifier(idempotency_key)
            if not (1 <= len(idempotency_key) <= 256):
                raise ValueError
            if text is not None:
                if not text.strip() or len(text) > _MAX_TEXT:
                    raise ValueError
                _reject_ambiguous_text(text)
        except ValueError:
            raise WorkbenchError("INVALID_INPUT") from None
        if action in _TEXT_ACTIONS and text is None:
            raise WorkbenchError("TEXT_REQUIRED")
        if action not in _TEXT_ACTIONS and text is not None:
            raise WorkbenchError("TEXT_NOT_ALLOWED")
        summary = self.incidents.find_incident(incident_id)
        if summary is None:
            raise WorkbenchError("UNKNOWN_INCIDENT")
        key = f"{incident_id}:{idempotency_key}"
        intent: dict[str, Any] = {
            "action": action,
            "actor_id": actor_id,
            "expected_generation": expected_generation,
        }
        if text is not None:
            intent["text"] = text
        # Intent first: the operator's text is durable before the store
        # commits the decision, so neither a crash nor event-log retention
        # can lose what the next Run must read.
        stored, inserted = self.ledger.put("control_intent", key, intent)
        if not inserted:
            if dict(stored) != intent:
                raise WorkbenchError("CONTROL_KEY_CONFLICT")
            result = self.ledger.get("control", key)
            if result is not None:
                return self._control_replay(incident_id, result)
            # Intent recorded, decision not confirmed: fall through and apply.
        try:
            generation, new_run_id = self._apply(
                summary, action, expected_generation, actor_id, text
            )
        except PersistenceError as exc:
            code = str(exc)
            if code == "CONTROL_CONFLICT":
                # A concurrent request under this very key may have won, or
                # our own earlier attempt committed but never confirmed.
                later = self.ledger.get("control", key)
                if later is not None:
                    return self._control_replay(incident_id, later)
                # Only a key whose intent a PRIOR attempt recorded may be
                # reconciled against the audit: a fresh key is a fresh
                # request, and a stale form must be refused, not confirmed.
                if not inserted:
                    reconciled = self._confirm_from_audit(incident_id, key, intent)
                    if reconciled is not None:
                        return reconciled
                current = self.incidents.find_incident(incident_id)
            else:
                current = None
            if inserted:
                # Nothing was applied under this key: do not let the refused
                # intent block a corrected resubmission.
                self.ledger.delete("control_intent", key)
            raise WorkbenchError(
                code,
                current_generation=(
                    None if current is None else current.control_generation
                ),
            ) from None
        payload = dict(intent, generation=generation)
        if new_run_id is not None:
            payload["run_id"] = str(new_run_id)
        sequence = self.events.append(incident_id, "control_applied", payload)
        result_row, _ = self.ledger.put(
            "control",
            key,
            {"action": action, "generation": generation, "sequence": sequence},
        )
        return ControlResult(
            incident_id,
            action,
            int(result_row["generation"]),
            False,
            int(result_row["sequence"]),
        )

    def _apply(
        self,
        summary: IncidentSummary,
        action: str,
        expected: int,
        actor_id: str,
        text: str | None,
    ) -> tuple[int, UUID | None]:
        if action != "new_run":
            # The note travels with the decision: DurableStore.control (PR #31)
            # writes it to opspilot_controls.payload and opspilot_inputs, from
            # where begin_round() hands it to the next model round. The web
            # ledger keeps a copy for idempotency and display only.
            payload = None if text is None else {"text": text, "channel": "web"}
            return self.incidents.control(
                summary.incident_id, expected, action, actor_id, payload
            ), None
        # DurableStore.new_run takes no expected version; the run id is
        # derived from ``expected + 1`` so a stale form either conflicts on
        # identity or replays the very run it already created.
        if summary.control_generation != expected:
            raise PersistenceError("CONTROL_CONFLICT")
        run_id = uuid5(_INTAKE_NAMESPACE, f"{summary.intake_key}:run:{expected + 1}")
        now = self.incidents.now()
        generation = self.incidents.new_run(
            summary.incident_id,
            run_id,
            deadline=now + timedelta(seconds=self.run_seconds),
            budget_limit=self.budget_limit,
            versions=dict(self.run_versions),
            actor=actor_id,
        )
        return generation, run_id

    def _confirm_from_audit(
        self, incident_id: UUID, key: str, intent: Mapping[str, Any]
    ) -> ControlResult | None:
        """Close the window where the store applied a decision we never confirmed.

        The store's own audit row is the authority: if one matches this
        intent on action, expected generation and actor, write the confirm
        row now so the note reaches the next Run, and report the decision
        as replayed. Best effort until the audit carries text/key (PR #31):
        two crashed attempts by one actor with different texts could still
        cross-confirm.
        """
        for audit in self.incidents.control_audit(incident_id):
            if (
                audit.action == intent["action"]
                and audit.expected_generation == intent["expected_generation"]
                and audit.actor == intent["actor_id"]
            ):
                sequence = self.events.append(
                    incident_id,
                    "control_applied",
                    dict(intent, generation=audit.resulting_generation),
                )
                row, _ = self.ledger.put(
                    "control",
                    key,
                    {
                        "action": audit.action,
                        "generation": audit.resulting_generation,
                        "sequence": sequence,
                    },
                )
                return self._control_replay(incident_id, row)
        return None

    @staticmethod
    def _control_replay(incident_id: UUID, stored: Mapping[str, Any]) -> ControlResult:
        return ControlResult(
            incident_id,
            str(stored["action"]),
            int(stored["generation"]),
            True,
            int(stored["sequence"]),
        )

    def _notes(self, incident_id: UUID) -> list[dict[str, Any]]:
        """Confirmed follow-up/correction notes, oldest generation first."""
        intents = dict(self.ledger.list_prefix("control_intent", f"{incident_id}:"))
        notes = []
        for key, result in self.ledger.list_prefix("control", f"{incident_id}:"):
            intent = intents.get(key)
            if intent is None or "text" not in intent:
                continue
            notes.append(
                dict(
                    intent,
                    generation=int(result["generation"]),
                    sequence=int(result["sequence"]),
                )
            )
        notes.sort(key=lambda note: note["generation"])
        return notes

    # -- reads ----------------------------------------------------------

    def list_incidents(self) -> tuple[IncidentSummary, ...]:
        return self.incidents.list_incidents()

    def _events_from_floor(self, incident_id: UUID) -> tuple[SubjectEvent, ...]:
        floor = self.events.floor(incident_id)
        return self.events.read_after(incident_id, max(floor - 1, 0), limit=_EVENT_PAGE)

    def snapshot(self, incident_id: UUID) -> dict[str, Any]:
        """Everything the incident page renders, read from committed rows."""
        summary = self.incidents.find_incident(incident_id)
        if summary is None:
            raise WorkbenchError("UNKNOWN_INCIDENT")
        try:
            rebuilt = self.incidents.rebuild(incident_id)
        except PersistenceError as exc:
            raise WorkbenchError(str(exc)) from None
        intake = self.ledger.get("intake", summary.intake_key)
        request = (
            None if intake is None else _envelope_from_json(intake["envelope"]).request
        )
        events = self._events_from_floor(incident_id)
        run = rebuilt["run"]
        steps = [_step_view(step) for step in rebuilt["steps"]]
        outcome = next(
            (
                e
                for e in reversed(events)
                if e.kind in {"run_completed", "run_handoff"}
                and e.payload.get("run_id") == str(run["run_id"])
            ),
            None,
        )
        report = _report_view(rebuilt.get("conclusion"))
        handoff_report = None
        if report is None and outcome is not None and outcome.kind == "run_handoff":
            handoff_report = _report_view(_last_live_response(rebuilt["steps"]))
        return {
            "incident": summary,
            "question": None if request is None else request.question,
            "target_id": None if request is None else request.target_id,
            "run": {
                "run_id": str(run["run_id"]),
                "state": run["state"],
                "epoch": int(run["epoch"]),
                "control_generation": int(run["control_generation"]),
                "budget_limit": int(run["budget_limit"]),
                "budget_reserved": int(run["budget_reserved"]),
                "budget_spent": int(run["budget_spent"]),
                "budget_unknown": int(run["budget_unknown"]),
                "deadline": run["deadline"],
            },
            "steps": steps,
            "pending_tools": len(rebuilt["pending_tools"]),
            "report": report,
            "handoff_report": handoff_report,
            "outcome": None if outcome is None else dict(outcome.payload),
            "controls": sorted(
                self._notes(incident_id)
                + [
                    dict(e.payload)
                    for e in events
                    if e.kind == "control_applied" and "text" not in e.payload
                ],
                key=lambda item: int(item["generation"]),
            ),
            "events": [_event_view(e) for e in events[-50:]],
            "latest_sequence": self.events.latest(incident_id),
        }

    def evidence_for(
        self, incident_id: UUID, evidence_id: str
    ) -> StoredEvidence | None:
        record = self.evidence.get(evidence_id)
        if record is None:
            return None
        if record.run_id not in self.incidents.run_ids(incident_id):
            return None
        return record

    # -- one bounded attempt --------------------------------------------

    def run_once(
        self,
        incident_id: UUID,
        investigator: Investigator,
        *,
        lease_seconds: int | None = None,
    ) -> LoopOutcome | None:
        """Claim the current Run, execute one attempt, publish or hand off.

        Returns the loop outcome, or ``None`` when the store refused the
        claim or the attempt raised. Once a lease was granted every path
        ends with the lease released and a ``run_completed`` or
        ``run_handoff`` event. ``DurableStore`` has no lease renewal, so the
        lease defaults to the Run wall (``run_seconds``): a lease shorter
        than the attempt would fence the attempt's own commits.
        """
        summary = self.incidents.find_incident(incident_id)
        if summary is None:
            raise WorkbenchError("UNKNOWN_INCIDENT")
        if summary.current_run_id is None:
            raise WorkbenchError("INCONSISTENT_STATE")
        run_id = summary.current_run_id
        intake = self.ledger.get("intake", summary.intake_key)
        if intake is None:
            raise WorkbenchError("INCONSISTENT_STATE")
        request = _envelope_from_json(intake["envelope"]).request
        seconds = int(self.run_seconds) if lease_seconds is None else lease_seconds
        try:
            lease = self.incidents.claim(
                incident_id, run_id, self._owner, dict(self.run_versions), seconds
            )
        except PersistenceError as exc:
            # A still-valid lease held elsewhere is the normal state while
            # another worker runs (or a killed worker's lease runs down); a
            # polling worker would otherwise append one event per poll.
            if str(exc) != "LEASE_ACTIVE":
                self.events.append(
                    incident_id,
                    "run_claim_refused",
                    {"run_id": str(run_id), "code": str(exc)},
                )
            return None
        try:
            return self._attempt(incident_id, run_id, lease, investigator, request)
        except (StepStoreError, PersistenceError) as exc:
            self._handoff(incident_id, lease, run_id, "failed", (str(exc),))
            return None
        except BaseException:
            self._handoff(incident_id, lease, run_id, "failed", ("UNEXPECTED_ERROR",))
            raise

    def _attempt(
        self,
        incident_id: UUID,
        run_id: UUID,
        lease: Lease,
        investigator: Investigator,
        request: IntakeRequest,
    ) -> LoopOutcome:
        rebuilt = self.incidents.rebuild(incident_id)
        # Human notes are read only once the lease is held: a note applied
        # after this point advances the generation and fences this attempt's
        # commits, so an attempt can never publish over a newer decision
        # while carrying older notes. With PR #31 the notes reach the model
        # through begin_round() inputs, frozen per round by the store; only
        # the pre-#31 base composes them into the question here.
        notes = [] if self.incidents.payload_supported else self._notes(incident_id)
        self.events.append(
            incident_id,
            "run_claimed",
            {
                "run_id": str(run_id),
                "epoch": lease.epoch,
                "control_generation": lease.control_generation,
            },
        )
        committer = _EmittingCommitter(
            self.incidents.committer(lease), self.events, incident_id, run_id
        )
        context = RunContext(
            incident_id=incident_id,
            run_id=run_id,
            control_generation=lease.control_generation,
            question=_compose_question(request.question, notes),
            target_id=request.target_id,
            deadline=rebuilt["run"]["deadline"],
            budget_limit=int(rebuilt["run"]["budget_limit"]),
        )
        outcome = investigator.investigate(context, committer, self.evidence)
        # A handoff (incomplete finding, budget, pairing failure) is never
        # published as the conclusion: publishing would freeze human control
        # (DurableStore.control refuses once a conclusion exists) exactly
        # when a follow-up is needed. The committed step stays readable.
        if outcome.report is None or outcome.handoff or committer.last_step is None:
            self._handoff(
                incident_id,
                lease,
                run_id,
                outcome.execution,
                outcome.handoff_reasons,
                report_sha256=outcome.report_content_sha256,
                evidence_ids=outcome.evidence_ids,
            )
            return outcome
        step_id, payload = committer.last_step
        published = self.incidents.publish(lease, payload, step_id=step_id)
        if not published:
            self._handoff(
                incident_id,
                lease,
                run_id,
                outcome.execution,
                ("LATE_RESULT",),
                report_sha256=outcome.report_content_sha256,
                evidence_ids=outcome.evidence_ids,
            )
            return outcome
        self.events.append(
            incident_id,
            "run_completed",
            {
                "run_id": str(run_id),
                "published": True,
                "execution": outcome.execution,
                "handoff": False,
                "handoff_reasons": [],
                "report_sha256": outcome.report_content_sha256,
                "evidence_ids": list(outcome.evidence_ids),
                "model_requests_used": outcome.model_requests_used,
                "prompt_revision": outcome.prompt_revision,
            },
        )
        return outcome

    def _handoff(
        self,
        incident_id: UUID,
        lease: Lease,
        run_id: UUID,
        execution: str,
        reasons: tuple[str, ...],
        *,
        report_sha256: str | None = None,
        evidence_ids: tuple[str, ...] = (),
    ) -> None:
        try:
            self.incidents.abandon(lease)
        finally:
            self.events.append(
                incident_id,
                "run_handoff",
                {
                    "run_id": str(run_id),
                    "published": False,
                    "execution": execution,
                    "handoff": True,
                    "reasons": list(reasons),
                    "report_sha256": report_sha256,
                    "evidence_ids": list(evidence_ids),
                },
            )


# -- views ----------------------------------------------------------------


def _compose_question(original: str, notes: list[dict[str, Any]]) -> str:
    """Original question plus every confirmed human note, in generation order."""
    if not notes:
        return original
    lines = [original, ""]
    for note in notes:
        lines.append(
            f"[human {note['action']} at generation {note['generation']} "
            f"by {note['actor_id']}] {note['text']}"
        )
    return "\n".join(lines)


def _envelope_json(envelope: IntakeEnvelope) -> dict[str, Any]:
    return {
        "request_id": envelope.request_id,
        "principal": {
            "actor_id": envelope.principal.actor_id,
            "channel": envelope.principal.channel,
            "auth_revision": envelope.principal.auth_revision,
        },
        "request": {
            "target_id": envelope.request.target_id,
            "question": envelope.request.question,
            "idempotency_key": envelope.request.idempotency_key,
        },
        "received_at": envelope.received_at.isoformat(),
    }


def _envelope_from_json(stored: Mapping[str, Any]) -> IntakeEnvelope:
    """Rebuild the strict envelope from its JSON form (datetime is a string there)."""
    return IntakeEnvelope(
        request_id=stored["request_id"],
        principal=Principal(**stored["principal"]),
        request=IntakeRequest(**stored["request"]),
        received_at=datetime.fromisoformat(stored["received_at"]),
    )


def _last_live_response(steps: list[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    for step in reversed(steps):
        response = step.get("response")
        if step["status"] != "late_result" and isinstance(response, Mapping):
            return dict(response)
    return None


def _step_view(step: Mapping[str, Any]) -> dict[str, Any]:
    response = step.get("response") or {}
    tools = []
    for item in step.get("tool_results") or []:
        result = item.get("result") or {}
        tools.append(
            {
                "ordinal": item.get("ordinal"),
                "evidence_id": result.get("evidence_id"),
                "status": result.get("status"),
                "adopted": result.get("adopted"),
                "tool": result.get("tool"),
                "target_id": result.get("target_id"),
            }
        )
    assistant = response.get("assistant") if isinstance(response, Mapping) else None
    planned = assistant.get("tool_calls") if isinstance(assistant, Mapping) else None
    return {
        "step_id": str(step["step_id"]),
        "sequence": int(step.get("sequence", 0)),
        "logical_key": step["logical_key"],
        "status": step["status"],
        "control_generation": int(step["control_generation"]),
        "finish_reason": response.get("finish_reason")
        if isinstance(response, Mapping)
        else None,
        "planned_tools": len(planned) if isinstance(planned, list) else 0,
        "tools": tools,
        "history_only": step["status"] == "late_result",
    }


def _event_view(event: SubjectEvent) -> dict[str, Any]:
    return {
        "sequence": event.sequence,
        "kind": event.kind,
        "payload": dict(event.payload),
        "recorded_at": (
            None if event.recorded_at is None else event.recorded_at.isoformat()
        ),
    }


def _report_view(conclusion: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Split a committed step payload into the v4 display categories.

    The text is re-parsed here so the page shows what was committed, not a
    cached projection. Text that no longer parses is shown as unparseable
    rather than as an empty report.
    """
    if not isinstance(conclusion, Mapping):
        return None
    assistant = conclusion.get("assistant")
    content = assistant.get("content") if isinstance(assistant, Mapping) else None
    finish = conclusion.get("finish_reason")
    report, reason = parse_report(
        content if isinstance(content, str) else None,
        finish_reason=finish if isinstance(finish, str) else "unknown",
    )
    digest = (
        hashlib.sha256(content.encode("utf-8")).hexdigest()
        if isinstance(content, str)
        else None
    )
    if report is None:
        return {"parsed": False, "reason": reason, "content_sha256": digest}
    return dict(categorize_report(report), parsed=True, content_sha256=digest)


def categorize_report(report: ReportV2) -> dict[str, Any]:
    by_kind: dict[str, list[dict[str, Any]]] = {
        "fact": [],
        "hypothesis": [],
        "counter_evidence": [],
        "rejected_hypothesis": [],
        "recommendation": [],
    }
    for claim in report.claims:
        by_kind[claim.kind].append(
            {
                "text": claim.text,
                "evidence_ids": list(claim.evidence_ids),
                "target_refs": list(claim.target_refs),
                "time_scope_ref": claim.time_scope_ref,
            }
        )
    return {
        "schema_version": report.schema_version,
        "assessment_status": report.assessment_status,
        "conclusion": report.conclusion,
        "summary": report.summary,
        "facts": by_kind["fact"],
        "hypotheses": by_kind["hypothesis"],
        "counter_evidence": by_kind["counter_evidence"],
        "rejected_hypotheses": by_kind["rejected_hypothesis"],
        "recommendations": by_kind["recommendation"],
        "unknown": list(report.gaps),
        "next_steps": list(report.next_steps),
        "incomplete": report.assessment_status == "incomplete",
    }
