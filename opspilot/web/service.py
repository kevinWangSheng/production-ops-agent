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
from opspilot.investigation.context import CONCLUSION_KIND, conclusion_publishable
from opspilot.investigation.limits import (
    MAX_MODEL_REQUESTS_PER_RUN,
    MODEL_REQUEST_TIMEOUT_SECONDS,
    RUN_WALL_SECONDS,
)
from opspilot.investigation.loop import LoopOutcome
from opspilot.investigation.progress import (
    EmittingCommitter,
    announce_claim_refused,
    announce_claimed,
    announce_completed,
    announce_handoff,
    sweep_expired,
)
from opspilot.investigation.reports import ReportV2, parse_report
from opspilot.investigation.store import StepCommitter, StepStoreError
from opspilot.persistence import Lease, PersistenceError
from opspilot.tools.executor import EvidenceSink
from opspilot.web.events import EventLog, SubjectEvent
from opspilot.web.evidence import EvidenceStore, StoredEvidence
from opspilot.web.store import (
    ControlAudit,
    IncidentStore,
    IncidentSummary,
    WebLedger,
)

_INTAKE_NAMESPACE = UUID("0f4c9d3e-2b7a-4a6e-9c1d-5e8f7a6b3c21")
#: Lease granted per attempt and re-extended before every committer call.
#: The loop is synchronous, so nothing can renew while one model request is
#: in flight: the lease must outlast the longest request plus a margin.
#: Takeover after a hard kill therefore waits at most this long, not the Run
#: wall. The frozen Run ceilings are untouched (deadline still caps it).
#: The margin assumes the client's request timeout is a wall bound; it is a
#: per-socket-operation timeout today, so a source dripping bytes could
#: still outlast the lease (pre-existing client property, recorded here).
LEASE_SECONDS = int(MODEL_REQUEST_TIMEOUT_SECONDS) + 60
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
            # A retry after the process died between accept() and the
            # event append must repair the missing projection, not skip it.
            self._announce_intake(
                incident_id, run_id, _envelope_from_json(stored["envelope"])
            )
            return IntakeResult(
                incident_id, run_id, True, self.events.latest(incident_id)
            )
        sequence = self._announce_intake(incident_id, run_id, envelope)
        return IntakeResult(incident_id, run_id, False, sequence)

    def _announce_intake(
        self, incident_id: UUID, run_id: UUID, envelope: IntakeEnvelope
    ) -> int:
        """Emit ``intake_accepted`` exactly once per incident.

        Two guards: the ``intake_event`` ledger row survives event pruning
        and short-circuits every later call; ``append_once`` fences the
        window before that row exists, where concurrent retries (or a retry
        racing a page-load reconcile) have all seen no marker yet. The
        marker is still written after the append so a crash in between is
        repaired by the next replay or reconcile rather than hidden.
        """
        announced = self.ledger.get("intake_event", str(incident_id))
        if announced is not None:
            return int(announced["sequence"])
        sequence = self.events.append_once(
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
        row, _ = self.ledger.put(
            "intake_event", str(incident_id), {"sequence": sequence}
        )
        return int(row["sequence"])

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
        # Keyed by generation: the store hands each applied decision one
        # generation, so a retry or reconciler that finds this generation
        # applied but its ``control`` marker lost reuses the published
        # event instead of announcing the decision twice.
        sequence = self.events.append_once(
            incident_id, "control_applied", payload, key={"generation": generation}
        )
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
        # The run id is derived from ``expected + 1`` so a stale form either
        # conflicts on identity or replays the very run it already created;
        # DurableStore.new_run on main fences on ``expected`` as well.
        if summary.control_generation != expected:
            raise PersistenceError("CONTROL_CONFLICT")
        run_id = uuid5(_INTAKE_NAMESPACE, f"{summary.intake_key}:run:{expected + 1}")
        now = self.incidents.now()
        generation = self.incidents.new_run(
            summary.incident_id,
            run_id,
            expected_generation=expected,
            deadline=now + timedelta(seconds=self.run_seconds),
            budget_limit=self.budget_limit,
            versions=dict(self.run_versions),
            actor=actor_id,
        )
        return generation, run_id

    def _audit_matches(
        self,
        intent: Mapping[str, Any],
        audit: ControlAudit,
        *,
        unconfirmed_peers: int,
    ) -> bool:
        """Whether ``audit`` provably is the decision ``intent`` asked for.

        Action, expected generation and actor must match. A text action
        additionally needs the audit payload (PR #31) to carry the same
        text; on a store without payloads the audit is accepted only when
        this is the single unconfirmed intent that could have produced it.
        """
        if (
            audit.action != intent["action"]
            or audit.expected_generation != intent["expected_generation"]
            or audit.actor != intent["actor_id"]
        ):
            return False
        text = intent.get("text")
        if text is None:
            return True
        if audit.payload is not None:
            return bool(audit.payload.get("text") == text)
        return not self.incidents.payload_supported and unconfirmed_peers == 1

    def _confirm(
        self,
        incident_id: UUID,
        key: str,
        intent: Mapping[str, Any],
        audit: ControlAudit,
    ) -> ControlResult:
        sequence = self.events.append_once(
            incident_id,
            "control_applied",
            dict(intent, generation=audit.resulting_generation, reconciled=True),
            key={"generation": audit.resulting_generation},
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

    def _reconcile_pending_notes(self, incident_id: UUID) -> None:
        """Confirm every intent whose decision the store already applied.

        A worker that claims a generation produced by another worker's note
        must see that note even though the other worker crashed before its
        confirm row; the store's audit row is the proof.
        """
        prefix = f"{incident_id}:"
        confirmed = dict(self.ledger.list_prefix("control", prefix))
        taken = {int(row["generation"]) for row in confirmed.values()}
        pending = [
            (key, intent)
            for key, intent in self.ledger.list_prefix("control_intent", prefix)
            if key not in confirmed
        ]
        for audit in self.incidents.control_audit(incident_id):
            if audit.resulting_generation in taken:
                continue
            peers = [
                (key, intent)
                for key, intent in pending
                if audit.action == intent["action"]
                and audit.expected_generation == intent["expected_generation"]
                and audit.actor == intent["actor_id"]
            ]
            matches = [
                (key, intent)
                for key, intent in peers
                if self._audit_matches(intent, audit, unconfirmed_peers=len(peers))
            ]
            if len(matches) != 1:
                continue
            key, intent = matches[0]
            self._confirm(incident_id, key, intent, audit)
            taken.add(audit.resulting_generation)
            pending = [item for item in pending if item[0] != key]

    def _confirm_from_audit(
        self, incident_id: UUID, key: str, intent: Mapping[str, Any]
    ) -> ControlResult | None:
        """Close the window where the store applied a decision we never confirmed.

        Same proof rule as ``_reconcile_pending_notes``; the store's audit
        row is the authority and a different key can never be confirmed as
        another intent's decision.
        """
        prefix = f"{incident_id}:"
        confirmed = dict(self.ledger.list_prefix("control", prefix))
        taken = {int(row["generation"]) for row in confirmed.values()}
        peers = [
            other
            for other_key, other in self.ledger.list_prefix("control_intent", prefix)
            if other_key not in confirmed
            and other["action"] == intent["action"]
            and other["expected_generation"] == intent["expected_generation"]
            and other["actor_id"] == intent["actor_id"]
        ]
        for audit in self.incidents.control_audit(incident_id):
            if audit.resulting_generation in taken:
                continue
            if self._audit_matches(intent, audit, unconfirmed_peers=len(peers)):
                return self._confirm(incident_id, key, intent, audit)
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

    def reconcile(self, incident_id: UUID) -> None:
        """Repair projections from the authoritative rows (idempotent).

        Sweeps the incident's Run if it is ``running`` past its deadline
        (ADR-0005 decision 2: nothing else can settle it, so a page load
        parks it as a ``DEADLINE_EXCEEDED`` handoff, announced once by run).
        Confirms notes the store already applied and emits a missing
        ``run_completed`` for a Run that published its conclusion but whose
        worker died before appending the event; the ledger remembers which
        runs were announced, and the append is keyed by run so a worker
        that died between the append and the marker is repaired, not
        announced twice.
        """
        summary = self.incidents.find_incident(incident_id)
        if summary is None:
            return
        try:
            sweep_expired(self.incidents, self.events, incident_id=incident_id)
        except PersistenceError:
            # A page load must not fail because the sweep could not take its
            # row lock right now; the next load (or the next poll) sweeps.
            pass
        intake = self.ledger.get("intake", summary.intake_key)
        if intake is not None:
            self._announce_intake(
                incident_id,
                UUID(intake["run_id"]),
                _envelope_from_json(intake["envelope"]),
            )
        self._reconcile_pending_notes(incident_id)
        run_id = summary.current_run_id
        if not summary.concluded or run_id is None:
            return
        if self.ledger.get("completion", str(run_id)) is not None:
            return
        rebuilt = self.incidents.rebuild(incident_id)
        if rebuilt["run"]["state"] != "completed":
            return
        # This stub can be the retained record: a page load that races the
        # worker between its publish and its append wins the run_id key, so
        # readers must not rely on the worker-only fields (evidence_ids,
        # model_requests_used, prompt_revision) being present.
        sequence = self.events.append_once(
            incident_id,
            "run_completed",
            {
                "run_id": str(run_id),
                "published": True,
                "execution": "completed",
                "handoff": False,
                "handoff_reasons": [],
                "reconciled": True,
                "report_sha256": _content_sha256(rebuilt.get("conclusion")),
                "evidence_ids": [],
            },
            key={"run_id": str(run_id)},
        )
        self.ledger.put("completion", str(run_id), {"sequence": sequence})

    def _newest_events(self, incident_id: UUID) -> tuple[SubjectEvent, ...]:
        """The newest retained page, so its last item is the true watermark.

        An ascending read from the floor would stop at the page limit and
        leave a gap between the page and ``latest_sequence`` that the SSE
        resume could never fill.
        """
        floor = self.events.floor(incident_id)
        latest = self.events.latest(incident_id)
        cursor = max(floor - 1, latest - _EVENT_PAGE, 0)
        return self.events.read_after(incident_id, cursor, limit=_EVENT_PAGE)

    def _controls(self, incident_id: UUID) -> list[dict[str, Any]]:
        """Every confirmed decision from the ledger rows, in generation order."""
        intents = dict(self.ledger.list_prefix("control_intent", f"{incident_id}:"))
        rows = []
        for key, result in self.ledger.list_prefix("control", f"{incident_id}:"):
            intent = intents.get(key)
            if intent is None:
                continue
            rows.append(
                dict(
                    intent,
                    generation=int(result["generation"]),
                    sequence=int(result["sequence"]),
                )
            )
        rows.sort(key=lambda item: item["generation"])
        return rows

    def snapshot(self, incident_id: UUID) -> dict[str, Any]:
        """Everything the incident page renders, read from committed rows."""
        summary = self.incidents.find_incident(incident_id)
        if summary is None:
            raise WorkbenchError("UNKNOWN_INCIDENT")
        self.reconcile(incident_id)
        try:
            rebuilt = self.incidents.rebuild(incident_id)
        except PersistenceError as exc:
            raise WorkbenchError(str(exc)) from None
        intake = self.ledger.get("intake", summary.intake_key)
        request = (
            None if intake is None else _envelope_from_json(intake["envelope"]).request
        )
        events = self._newest_events(incident_id)
        run = rebuilt["run"]
        steps = [_step_view(step) for step in rebuilt["steps"]]
        # A published conclusion is terminal: a ``run_handoff`` the crashed
        # worker's exit path appended after it is history, not the outcome.
        # A ``run_handoff`` that did not park (``parked: false``: the attempt
        # was fenced by human control or crashed) is only the outcome while
        # the Run row itself is not runnable; a queued/running row means the
        # next attempt owns the state and the page must not show the Run as
        # handed off (bot review, PR #44). The event stays in the list.
        runnable = run["state"] in {"queued", "running"}
        outcome = next(
            (
                e
                for kind in ("run_completed", "run_handoff")
                for e in reversed(events)
                if e.kind == kind
                and e.payload.get("run_id") == str(run["run_id"])
                and not (
                    kind == "run_handoff"
                    and runnable
                    and e.payload.get("parked") is False
                )
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
            "controls": self._controls(incident_id),
            "events": [_event_view(e) for e in events[-50:]],
            "latest_sequence": events[-1].sequence
            if events
            else self.events.latest(incident_id),
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
        ``run_handoff`` event. The lease is short (``LEASE_SECONDS``) and is
        renewed before every committer call when the store offers
        ``renew_lease`` (PR #35); it must outlast one maximal model request
        because the loop cannot renew while a request blocks. A store
        without the capability (pre-#35) keeps the previous behaviour: one
        lease for the whole Run wall, so an attempt is never fenced by its
        own unrenewable lease.
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
        # Notes applied by another worker but not yet confirmed must be
        # visible to this attempt (pre-#31 fallback composes them).
        self.reconcile(incident_id)
        if lease_seconds is not None:
            seconds = lease_seconds
        elif self.incidents.renewal_supported:
            seconds = LEASE_SECONDS
        else:
            seconds = int(self.run_seconds)
        try:
            lease = self.incidents.claim(
                incident_id, run_id, self._owner, dict(self.run_versions), seconds
            )
        except PersistenceError as exc:
            # A still-valid lease held elsewhere is the normal state while
            # another worker runs (or a killed worker's lease runs down); a
            # polling worker would otherwise append one event per poll.
            if str(exc) != "LEASE_ACTIVE":
                announce_claim_refused(self.events, incident_id, run_id, str(exc))
            return None
        try:
            return self._attempt(
                incident_id, run_id, lease, investigator, request, seconds
            )
        except (StepStoreError, PersistenceError) as exc:
            # A store refusal: the fence already took this lease, or storage
            # failed. Not a loop handoff, so the Run is not parked -- the
            # next claim converges on the committed rows.
            self._handoff(incident_id, lease, run_id, "failed", (str(exc),), park=False)
            return None
        except BaseException:
            # A crash, not a handoff: like a killed worker, the Run stays
            # claimable and the next attempt resumes from the rows (C3
            # section 7). The event still tells the page what happened.
            self._handoff(
                incident_id,
                lease,
                run_id,
                "failed",
                ("UNEXPECTED_ERROR",),
                park=False,
            )
            raise

    def _attempt(
        self,
        incident_id: UUID,
        run_id: UUID,
        lease: Lease,
        investigator: Investigator,
        request: IntakeRequest,
        seconds: int,
    ) -> LoopOutcome:
        rebuilt = self.incidents.rebuild(incident_id)
        # Human notes are read only once the lease is held: a note applied
        # after this point advances the generation and fences this attempt's
        # commits, so an attempt can never publish over a newer decision
        # while carrying older notes. With PR #31 the notes reach the model
        # through begin_round() inputs, frozen per round by the store; only
        # the pre-#31 base composes them into the question here.
        # Under the lease: a note the store applied before this claim (and
        # whose handler died before its confirm row) is confirmed here, so
        # the attempt that now owns that generation composes it. A note
        # applied after the claim bumps the generation and fences this
        # attempt's commits instead.
        self._reconcile_pending_notes(incident_id)
        notes = [] if self.incidents.payload_supported else self._notes(incident_id)
        announce_claimed(self.events, lease)
        committer = EmittingCommitter(
            self.incidents.committer(lease),
            self.events,
            incident_id,
            run_id,
            renew=lambda: self.incidents.renew_lease(lease, seconds),
            evidence=self.evidence,
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
        # published as the conclusion (ADR-0005): publishing would freeze
        # human control (DurableStore.control refuses once a conclusion
        # exists) exactly when a follow-up is needed. The committed step
        # stays readable and the Run is parked for a human. ``final_step_id``
        # is None when the store fenced the terminal step: nothing this
        # attempt may publish. Same rule as ``InvestigationRunner``.
        if (
            outcome.final_step_id is None
            or outcome.conclusion is None
            or not conclusion_publishable(outcome.conclusion)
        ):
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
        published = self.incidents.publish(
            lease, dict(outcome.conclusion), step_id=outcome.final_step_id
        )
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
        sequence = announce_completed(
            self.events,
            incident_id,
            run_id,
            execution=outcome.execution,
            report_sha256=outcome.report_content_sha256,
            evidence_ids=outcome.evidence_ids,
            model_requests_used=outcome.model_requests_used,
            prompt_revision=outcome.prompt_revision,
        )
        self.ledger.put("completion", str(run_id), {"sequence": sequence})
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
        park: bool = True,
    ) -> None:
        # ``park``: the loop ended in a handoff, so the Run waits for a human
        # (``waiting_human``, lease released; ADR-0005). A refusal means
        # human control already moved this Run on, or the lease lapsed: then
        # only this exact lease is released, best effort. The event still
        # records what this attempt saw (the page shows it as history), but
        # says ``parked: false`` so it never claims a durable park the Run
        # row does not show (bot review, PR #44).
        parked = False
        try:
            if park:
                try:
                    self.incidents.hand_off(lease)
                    parked = True
                except PersistenceError:
                    self.incidents.abandon(lease)
            else:
                self.incidents.abandon(lease)
        finally:
            announce_handoff(
                self.events,
                incident_id,
                run_id,
                execution,
                reasons,
                report_sha256=report_sha256,
                evidence_ids=evidence_ids,
                parked=parked,
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


def _committed_report(payload: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """The report text and finish reason a committed step payload carries.

    The loop on main ends an attempt with a ``conclusion`` step whose text
    sits under ``conclusion.report_content`` (published as is); a model
    round carries it as the assistant content with its finish reason.
    """
    if payload.get("kind") == CONCLUSION_KIND:
        inner = payload.get("conclusion")
        if not isinstance(inner, Mapping):
            return None, None
        content = inner.get("report_content")
        reasons = inner.get("handoff_reasons")
        truncated = isinstance(reasons, list) and "OUTPUT_LENGTH" in reasons
        return (
            content if isinstance(content, str) else None,
            "length" if truncated else "stop",
        )
    assistant = payload.get("assistant")
    content = assistant.get("content") if isinstance(assistant, Mapping) else None
    finish = payload.get("finish_reason")
    return (
        content if isinstance(content, str) else None,
        finish if isinstance(finish, str) else None,
    )


def _content_sha256(conclusion: object) -> str | None:
    if not isinstance(conclusion, Mapping):
        return None
    content, _ = _committed_report(conclusion)
    if content is None:
        return None
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _report_view(conclusion: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Split a committed step payload into the v4 display categories.

    The text is re-parsed here so the page shows what was committed, not a
    cached projection. Text that no longer parses is shown as unparseable
    rather than as an empty report.
    """
    if not isinstance(conclusion, Mapping):
        return None
    content, finish = _committed_report(conclusion)
    report, reason = parse_report(content, finish_reason=finish or "unknown")
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
