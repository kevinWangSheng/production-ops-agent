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
import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Literal, Protocol, cast
from uuid import UUID, uuid4, uuid5

from markupsafe import Markup
from pydantic import ValidationError

from opspilot.acceptance_alert import alert_intake_outcome
from opspilot.alertmanager import (
    Alert,
    InvalidAlert,
    parse_alert,
    question_for,
    record_of,
    resolve_target,
)
from opspilot.intake import (
    IntakeEnvelope,
    IntakeRequest,
    Principal,
    _reject_ambiguous_identifier,
    _reject_ambiguous_text,
    classify_intake_delivery,
)
from opspilot.investigation.context import (
    AFFECTED_SERVICE,
    CONCLUSION_KIND,
    INPUT_CONTENT_FIELD_MAX_CHARS,
    ContextError,
    conclusion_publishable,
)
from opspilot.investigation.inputs import ToolFace, continuation_input
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
from opspilot.observer.health_profile import HealthProfile, canonical_content
from opspilot.observer.replay import replay_history
from opspilot.persistence import Lease, PersistenceError
from opspilot.tools.executor import EvidenceSink
from opspilot.web.charts import MAX_FIGURES, evidence_chart
from opspilot.web.events import EventLog, SubjectEvent
from opspilot.web.evidence import EvidenceStore, StoredEvidence
from opspilot.web.store import (
    ControlAudit,
    IncidentStore,
    IncidentSummary,
    TargetRegistry,
    WebLedger,
)

_INTAKE_NAMESPACE = UUID("0f4c9d3e-2b7a-4a6e-9c1d-5e8f7a6b3c21")
_log = logging.getLogger(__name__)
#: Lease granted per attempt and re-extended before every committer call.
#: The loop is synchronous, so nothing can renew while one model request is
#: in flight: the lease must outlast the longest request plus a margin.
#: Takeover after a hard kill therefore waits at most this long, not the Run
#: wall. The frozen Run ceilings are untouched (deadline still caps it).
#: The margin assumes the client's request timeout is a wall bound; it is a
#: per-socket-operation timeout today, so a source dripping bytes could
#: still outlast the lease (pre-existing client property, recorded here).
LEASE_SECONDS = int(MODEL_REQUEST_TIMEOUT_SECONDS) + 60
#: Same ceiling ``context.project_input_content`` enforces on a committed
#: input row's ``text``/``channel``/``question`` fields. A control text over
#: this length must be refused here, before it is ever persisted, rather
#: than silently cut down to the limit once it reaches the model (ROADMAP
#: M1-01 已决 2026-09-24, item 3) -- one constant, not a second ceiling that
#: could drift from it.
_MAX_TEXT = INPUT_CONTENT_FIELD_MAX_CHARS
_EVENT_PAGE = 1000

ControlAction = Literal[
    "follow_up",
    "correct",
    "cancel",
    "pause",
    "resume",
    "new_run",
    "register_remediation",
    "takeover",
]
#: "The incident was handled outside the system": increments the control
#: generation, authorizes an observation session and moves the incident to
#: ``observing_recovery`` (M1-02 step 3, C3 section 10).
REGISTER_REMEDIATION = "register_remediation"
#: Human takes the incident over (C3 section 10, #121): the generation steps,
#: the observation authorization is withdrawn and automatic investigation
#: stops; ``register_remediation`` may still authorize an observation under
#: ``human_owned``, which does not resume investigation.
TAKEOVER = "takeover"
CONTROL_ACTIONS: frozenset[str] = frozenset(
    {
        "follow_up",
        "correct",
        "cancel",
        "pause",
        "resume",
        "new_run",
        REGISTER_REMEDIATION,
        TAKEOVER,
    }
)
#: Report claim categories in page order; chart order follows it.
CHART_CATEGORIES = (
    "facts",
    "hypotheses",
    "counter_evidence",
    "rejected_hypotheses",
    "recommendations",
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
class AlertResult:
    """One alert's answer in the ``/intake/alertmanager`` response (I2)."""

    fingerprint: str | None
    starts_at: str | None
    status: str | None
    outcome: str
    incident_id: str | None
    run_id: str | None
    delivery_key: str | None
    reason: str | None

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


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
    #: The model-visible tool profile every Run this workbench creates is
    #: recorded against. With it, each new Run row carries the investigation
    #: input the real driver rebuilds from (``INPUT_MISSING`` otherwise);
    #: ``None`` keeps the test-only ``run_once`` path, which needs no input.
    tool_face: ToolFace | None = None
    #: The versioned HealthProfile a "register remediation" fixes the
    #: observation session by (deadline, sample budget, cadence, sustained
    #: window; C3 section 10). ``None`` refuses the action: without a profile
    #: nothing bounded can be authorized.
    health_profile: HealthProfile | None = None
    #: The configured identities (``OPSPILOT_TARGET_IDENTITIES``) a
    #: remediation completes the registry row from the first time it is
    #: registered on a target (migration 0004). ``None``, or an id the file
    #: does not list, leaves a bare row and the registration is refused for
    #: lack of identity -- nothing is guessed, and intake never needs it.
    targets: TargetRegistry | None = None
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
        deadline = self.incidents.now() + timedelta(seconds=self.run_seconds)
        # Bind the incident to the registered identity of its target so a
        # target suspension fences its Runs; without it the incident row
        # carried no target and only the global gate applied (PR #54 bot
        # review P1). The intake string is the resource uid (M1-01 intake
        # contract, kept by user decision 2026-10-07); the rest of the
        # identity is completed when a remediation is registered. The tool
        # gateway still resolves authorization from its own registry.
        self.incidents.accept(
            incident_id,
            run_id,
            key,
            deadline=deadline,
            budget_limit=self.budget_limit,
            versions=dict(self.run_versions),
            input=self._fresh_input(envelope.request, run_id, deadline),
            target_id=self.incidents.register_target(envelope.request.target_id),
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

    # -- Alertmanager intake (M1-04 step 2) -----------------------------

    def intake_alert(
        self, principal: Principal, item: object, received_at: datetime
    ) -> AlertResult:
        """Accept one alert of a webhook in its own transaction (I2, E1).

        The identity ``fingerprint:startsAt`` alone decides a replay (r1-B):
        a repeated notification, with or without changed annotations, adds
        a delivery record and nothing else. A first firing alert opens an
        incident: with a Run when exactly one registry entry matches its
        labels, handed to a human without one otherwise (E5, E7). A
        resolved alert is only recorded, attached to the incident of the
        same identity when there is one (E8). A storage failure is
        ``failed`` and the caller answers 5xx so Alertmanager retries.
        """
        try:
            alert = parse_alert(item)
        except InvalidAlert as exc:
            return _invalid_alert(item, exc.code)
        key = alert.delivery_key
        record = record_of(alert)
        delivery = {
            "delivery_key": key,
            "fingerprint": alert.fingerprint,
            "starts_at": alert.starts_at_time,
            "starts_at_raw": alert.starts_at_raw,
            "status": alert.status,
            "actor": principal.actor_id,
            "raw_sha256": record.raw_sha256,
            "annotations_sha256": record.annotations_sha256,
            "alert_json": record.alert_json,
            "truncated": record.truncated,
        }

        def answer(
            outcome: str,
            incident_id: object = None,
            run_id: object = None,
            reason: str | None = None,
        ) -> AlertResult:
            return AlertResult(
                fingerprint=alert.fingerprint,
                starts_at=alert.starts_at,
                status=alert.status,
                outcome=outcome,
                incident_id=None if incident_id is None else str(incident_id),
                run_id=None if run_id is None else str(run_id),
                delivery_key=key,
                reason=reason,
            )

        try:
            opening = None
            if alert.status == "firing":
                try:
                    opening = self._alert_opening(principal, alert, received_at)
                except (ValidationError, ValueError):
                    # Only a registry entry whose resource_uid is not a valid
                    # target id gets here; a retry cannot repair it.
                    return _invalid_alert(item, "TARGET_ID_INVALID")
            committed = self.incidents.record_alert(delivery, opening=opening)
            outcome = str(committed["outcome"])
            if outcome in ("created", "replayed"):
                # Exactly-once like ``submit``; a replay repairs an
                # announcement a crash after the commit lost.
                incident_id = cast(UUID, committed["incident_id"])
                intake = self.ledger.get("intake", f"intake:{key}")
                if intake is not None:
                    self._announce_intake(
                        incident_id,
                        UUID(str(intake["run_id"])),
                        _envelope_from_json(intake["envelope"]),
                    )
        except PersistenceError as exc:
            _log.warning("alert intake failed key=%s code=%s", key, exc)
            return answer("failed", reason=str(exc))
        return answer(
            outcome,
            committed["incident_id"],
            committed["run_id"],
            committed.get("handoff_reason"),
        )

    def _alert_opening(
        self, principal: Principal, alert: Alert, received_at: datetime
    ) -> dict[str, Any]:
        """The incident a first firing delivery of ``alert`` opens (I3-I6).

        Ids derive from the delivery key exactly as ``submit`` derives them
        from an idempotency key, so every later control (``new_run``, notes,
        remediation) treats the incident like any other. The store uses
        this only when the identity is new.
        """
        key = alert.delivery_key
        intake_key = f"intake:{key}"
        incident_id = uuid5(_INTAKE_NAMESPACE, intake_key)
        matcher = getattr(self.targets, "match_alert", None)
        resolution = resolve_target(() if matcher is None else matcher(alert.labels))
        if resolution.target_id is None:
            return {
                "incident_id": incident_id,
                "intake_key": intake_key,
                "handoff_reason": resolution.handoff_reason,
            }
        run_id = uuid5(_INTAKE_NAMESPACE, f"{intake_key}:run:0")
        request = IntakeRequest(
            target_id=resolution.target_id,
            question=question_for(alert, resolution),
            idempotency_key=key,
        )
        envelope = IntakeEnvelope(
            request_id=str(uuid4()),
            principal=principal,
            request=request,
            received_at=received_at,
        )
        service = (
            None
            if resolution.namespace is None or resolution.workload is None
            else {"namespace": resolution.namespace, "workload": resolution.workload}
        )
        deadline = self.incidents.now() + timedelta(seconds=self.run_seconds)
        return {
            "incident_id": incident_id,
            "intake_key": intake_key,
            "run_id": run_id,
            "resource_uid": resolution.target_id,
            "namespace": resolution.namespace,
            "workload": resolution.workload,
            "deadline": deadline,
            "budget_limit": self.budget_limit,
            "versions": dict(self.run_versions),
            "input": self._fresh_input(
                request, run_id, deadline, affected_service=service
            ),
            "ledger": [
                (
                    "intake",
                    intake_key,
                    {
                        "incident_id": str(incident_id),
                        "run_id": str(run_id),
                        "envelope": _envelope_json(envelope),
                        "source": "alertmanager",
                    },
                )
            ],
        }

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
        revision: str | None = None,
    ) -> ControlResult:
        """Apply one human decision through the ledger/audit idempotency path.

        ``text`` belongs to ``follow_up``/``correct``; ``revision`` belongs to
        ``register_remediation`` (M1-02 step 3): the target revision after the
        handling, recorded on the audit row and the observation session (not
        identity, user decision 2026-10-07). Each is refused on an action that
        does not take it.
        """
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
            if revision is not None:
                _reject_ambiguous_identifier(revision)
                if not (1 <= len(revision) <= 256):
                    raise ValueError
        except ValueError:
            raise WorkbenchError("INVALID_INPUT") from None
        if action in _TEXT_ACTIONS and text is None:
            raise WorkbenchError("TEXT_REQUIRED")
        if action not in _TEXT_ACTIONS and text is not None:
            raise WorkbenchError("TEXT_NOT_ALLOWED")
        if action == REGISTER_REMEDIATION and revision is None:
            raise WorkbenchError("REVISION_REQUIRED")
        if action != REGISTER_REMEDIATION and revision is not None:
            raise WorkbenchError("REVISION_NOT_ALLOWED")
        if action == REGISTER_REMEDIATION and self.health_profile is None:
            # The session's deadline, budget, cadence and sustained window
            # come from a HealthProfile; without one there is nothing bounded
            # to authorize (C3 section 10, interface contract item 5).
            raise WorkbenchError("HEALTH_PROFILE_REQUIRED")
        summary = self.incidents.find_incident(incident_id)
        if summary is None:
            raise WorkbenchError("UNKNOWN_INCIDENT")
        if summary.handoff_only:
            # An alert handed to a human at intake (E5) has no Run to steer
            # and no bound target to observe; the human handles it outside.
            raise WorkbenchError("HANDOFF_ONLY")
        key = f"{incident_id}:{idempotency_key}"
        intent: dict[str, Any] = {
            "action": action,
            "actor_id": actor_id,
            "expected_generation": expected_generation,
        }
        if text is not None:
            intent["text"] = text
        if revision is not None:
            intent["revision"] = revision
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
                summary, action, expected_generation, actor_id, text, revision, key
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
        revision: str | None = None,
        key: str | None = None,
    ) -> tuple[int, UUID | None]:
        # The run id is derived from ``expected + 1`` so a stale form either
        # conflicts on identity or replays the very run it already created;
        # DurableStore.new_run on main fences on ``expected`` as well.
        run_id = uuid5(_INTAKE_NAMESPACE, f"{summary.intake_key}:run:{expected + 1}")
        now = self.incidents.now()
        deadline = now + timedelta(seconds=self.run_seconds)
        if action == REGISTER_REMEDIATION:
            # The session id is derived like the run id, so a retry of the
            # same decision re-creates the same session or conflicts on it.
            assert self.health_profile is not None and revision is not None
            parameters = self.health_profile.session
            identity = None
            intake = self.ledger.get("intake", summary.intake_key)
            if self.targets is not None and intake is not None:
                identity = self.targets.resolve(
                    _envelope_from_json(intake["envelope"]).request.target_id
                )
            # Without a configured identity there is nothing to observe
            # (fail closed before any write, even for a registry row that an
            # earlier registration completed: the profile binding below
            # cannot be checked without the entry). The profile must be the
            # one declared for this target (identity file
            # ``health_profile_id``): a single loaded profile must not certify
            # an unrelated target's recovery (bot review P1).
            if identity is None:
                raise PersistenceError("TARGET_IDENTITY_MISSING")
            if identity.health_profile_id != self.health_profile.profile_id:
                raise PersistenceError("HEALTH_PROFILE_TARGET_MISMATCH")
            generation = self.incidents.register_remediation(
                summary.incident_id,
                expected_generation=expected,
                actor=actor_id,
                revision=revision,
                deadline_at=now + timedelta(seconds=parameters.deadline_seconds),
                max_samples=parameters.max_samples,
                sample_interval_seconds=parameters.sample_interval_seconds,
                sustained_window_seconds=parameters.sustained_window_seconds,
                health_profile_revision=self.health_profile.revision,
                health_profile=canonical_content(self.health_profile),
                session_id=uuid5(
                    _INTAKE_NAMESPACE,
                    f"{summary.intake_key}:observation:{expected + 1}",
                ),
                identity=identity,
                payload={"channel": "web", "idempotency_key": key},
            )
            return generation, None
        if action != "new_run":
            # The note travels with the decision: DurableStore.control (PR #31)
            # writes it to opspilot_controls.payload and opspilot_inputs, from
            # where begin_round() hands it to the next model round. The web
            # ledger keeps a copy for idempotency and display only. A note on
            # a timed-out Run starts a fresh Run with the same id derivation
            # and wall as new_run (user decision 2026-09-25); the store
            # decides that from the row, and the changed pointer says so.
            # Every decision writes its ledger key into the audit row in the
            # store's transaction (#128, as register_remediation does), so a
            # crash before the confirm row can be reconciled only by the
            # request that made it; an action without text used to leave the
            # row anonymous. Inputs copy the payload, where the model-facing
            # projection drops the key (INPUT_CONTENT_FIELDS).
            payload: dict[str, Any] = {"channel": "web", "idempotency_key": key}
            if text is not None:
                payload["text"] = text
            renewal: dict[str, Any] = {}
            if action in _TEXT_ACTIONS:
                renewal = {
                    "renew_run_id": run_id,
                    "renew_deadline": deadline,
                    "renew_input": self._successor_input(summary, run_id, deadline),
                }
            generation = self.incidents.control(
                summary.incident_id, expected, action, actor_id, payload, **renewal
            )
            current = self.incidents.find_incident(summary.incident_id)
            if (
                current is None
                or current.current_run_id is None
                or current.current_run_id == summary.current_run_id
            ):
                return generation, None
            return generation, current.current_run_id
        if summary.control_generation != expected:
            raise PersistenceError("CONTROL_CONFLICT")
        generation = self.incidents.new_run(
            summary.incident_id,
            run_id,
            expected_generation=expected,
            deadline=deadline,
            budget_limit=self.budget_limit,
            versions=dict(self.run_versions),
            actor=actor_id,
            input=self._successor_input(summary, run_id, deadline),
            payload={"channel": "web", "idempotency_key": key},
        )
        return generation, run_id

    # -- investigation inputs -------------------------------------------

    def _fresh_input(
        self,
        request: IntakeRequest,
        run_id: UUID,
        deadline: datetime,
        *,
        affected_service: Mapping[str, str] | None = None,
    ) -> dict[str, Any] | None:
        """A first Run's input over the intake question (``None`` without a face).

        ``affected_service`` (an alert's resolved namespace + workload, E11)
        is recorded in the scope facts as focus; it grants nothing.
        """
        if self.tool_face is None:
            return None
        fresh = self.tool_face.input_for(
            run_id=str(run_id),
            question=request.question,
            target_id=request.target_id,
            deadline=deadline,
            model_requests=self.budget_limit,
        )
        if affected_service is not None:
            fresh = replace(
                fresh,
                scope_facts={
                    **fresh.scope_facts,
                    AFFECTED_SERVICE: dict(affected_service),
                },
            )
        return fresh.as_json()

    def _successor_input(
        self, summary: IncidentSummary, run_id: UUID, deadline: datetime
    ) -> dict[str, Any] | None:
        """The input a Run that replaces the current one starts from.

        The C3 continuation of the current Run (carried evidence re-bound
        to the new id, handoff note) whenever its rows allow it; a Run with
        no snapshot (rows older than the face) or rows this version cannot
        continue gets a fresh input over the intake question instead, so
        the successor is always runnable. The renewal input is built before
        the store decides whether the note renews at all (only an overdue
        Run does); an unused one costs a rebuild and nothing else.
        """
        if self.tool_face is None:
            return None
        intake = self.ledger.get("intake", summary.intake_key)
        if intake is None:
            # No intake row: nothing to build a question from. The Run is
            # created without input and the runner blocks it (INPUT_MISSING)
            # rather than this path guessing one.
            _log.warning("intake row missing incident=%s", summary.incident_id)
            return None
        request = _envelope_from_json(intake["envelope"]).request
        try:
            return continuation_input(
                self.incidents.rebuild(summary.incident_id),
                new_run_id=str(run_id),
                deadline=deadline,
                authorized_targets=frozenset({request.target_id}),
            ).as_json()
        except ContextError as exc:
            if exc.code != "INPUT_MISSING":
                # Rows this version cannot continue: fail closed with the
                # fixed code (the control action is refused, nothing is
                # created) rather than silently dropping carried evidence.
                raise PersistenceError(exc.code) from exc
            # The only case a fresh input is right for: a Run created before
            # the face existed has no snapshot to continue.
            _log.warning(
                "successor input falls back to a fresh input incident=%s code=%s",
                summary.incident_id,
                exc.code,
            )
            return self._fresh_input(request, run_id, deadline)
        # A PersistenceError (storage outage, lock timeout) propagates: the
        # control action fails closed and the operator retries (bot review,
        # PR #52).

    def _audit_matches(
        self,
        intent: Mapping[str, Any],
        audit: ControlAudit,
        *,
        key: str,
        unconfirmed_peers: int,
        unconfirmed_same_text: int,
    ) -> bool:
        """Whether ``audit`` provably is the decision ``intent`` (ledger ``key``) asked for.

        Action, expected generation and actor must match. Every decision
        writes its ledger key into the audit payload in the store's
        transaction (register_remediation since PR #120, the rest since
        #128), so a row that carries a key is the decision of that key and
        no other: two unconfirmed intents can never take each other's row.
        A row without a key predates #128 (or a store without payloads,
        PR #31): a text action is then matched by its text, and only when
        this is the single unconfirmed intent with that text (on a store
        without payloads: the single unconfirmed intent at all) that could
        have produced it; an action without text is never matched, because
        nothing distinguishes the request that made it from a peer that
        never ran. An unmatched retry is refused with the current
        generation rather than reported as a replay.
        """
        if (
            audit.action != intent["action"]
            or audit.expected_generation != intent["expected_generation"]
            or audit.actor != intent["actor_id"]
        ):
            return False
        payload = audit.payload or {}
        text = intent.get("text")
        if "idempotency_key" in payload:
            if payload["idempotency_key"] != key:
                return False
            if intent["action"] == REGISTER_REMEDIATION:
                return bool(payload.get("revision") == intent.get("revision"))
            return text is None or bool(payload.get("text") == text)
        if text is None:
            return False
        if audit.payload is not None:
            return (
                bool(audit.payload.get("text") == text) and unconfirmed_same_text == 1
            )
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
                if self._audit_matches(
                    intent,
                    audit,
                    key=key,
                    unconfirmed_peers=len(peers),
                    unconfirmed_same_text=_same_text(
                        (other for _, other in peers), intent
                    ),
                )
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
            if self._audit_matches(
                intent,
                audit,
                key=key,
                unconfirmed_peers=len(peers),
                unconfirmed_same_text=_same_text(peers, intent),
            ):
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
        Repairs a missing parked ``run_handoff`` projection after a Run row
        landed in ``waiting_human``; unavailable provenance is recorded as
        unknown.
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
        if run_id is None:
            return
        try:
            rebuilt = self.incidents.rebuild(incident_id)
        except PersistenceError:
            # As on main: an unreadable row is the claim path's to report
            # (INCONSISTENT_STATE handoff), not a reason for reconcile to fail.
            return
        run = rebuilt["run"]
        if run["state"] == "waiting_human":
            try:
                generation = int(rebuilt["control_generation"])
                self.events.append_once(
                    incident_id,
                    "run_handoff",
                    {
                        "run_id": str(run_id),
                        "published": False,
                        "execution": "unknown",
                        "handoff": True,
                        "parked": True,
                        "reasons": [],
                        "report_sha256": None,
                        "evidence_ids": [],
                        "reconciled": True,
                        "control_generation": generation,
                    },
                    key=(
                        {"run_id": str(run_id), "parked": True}
                        if generation == 0
                        else {
                            "run_id": str(run_id),
                            "parked": True,
                            "control_generation": generation,
                        }
                    ),
                )
            except PersistenceError:
                pass
        if not summary.concluded:
            return
        if self.ledger.get("completion", str(run_id)) is not None:
            return
        if run["state"] != "completed":
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
        if summary.handoff_only:
            return {
                "incident": summary,
                "handoff_only": True,
                "alert": self._alert_view(incident_id),
                "events": [],
                "latest_sequence": self.events.latest(incident_id),
            }
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
        outcome = _select_snapshot_outcome(
            events, str(run["run_id"]), int(rebuilt["control_generation"]), runnable
        )
        report = _report_view(rebuilt.get("conclusion"))
        observation_sessions = self.incidents.observation_sessions(incident_id)
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
            "charts": self._charts(incident_id, report or handoff_report),
            "outcome": None if outcome is None else dict(outcome.payload),
            "controls": self._controls(incident_id),
            # Recovery observation (M1-02): shown apart from the investigation
            # report, with the sampling basis behind each verdict and the
            # offline replay's agreement with it (step 5, #87).
            "observation_sessions": [
                _session_view(row) for row in observation_sessions
            ],
            "recovery": [
                self._recovery_view(cast(UUID, row["session_id"]))
                for row in observation_sessions
            ],
            "health_profile_revision": (
                None if self.health_profile is None else self.health_profile.revision
            ),
            "events": [_event_view(e) for e in events[-50:]],
            "latest_sequence": events[-1].sequence
            if events
            else self.events.latest(incident_id),
            "handoff_only": False,
            "alert": self._alert_view(incident_id),
        }

    def _alert_view(self, incident_id: UUID) -> dict[str, Any] | None:
        """The alert section of the page (I9): the acceptance projection
        (``opspilot.acceptance_alert``) of the same committed records, plus
        the stored redacted annotations of the latest delivery and every
        resolved notification. ``None`` for an incident no alert opened."""
        reader = getattr(self.incidents, "alert_records", None)
        records = None if reader is None else reader(incident_id)
        if records is None:
            return None
        outcome = alert_intake_outcome(
            SimpleNamespace(scenario_id="workbench", subject_id=str(incident_id)),
            records,
        )
        rows = records.deliveries
        latest = rows[-1] if rows else None
        return {
            "outcome": outcome,
            "fingerprint": records.identity["fingerprint"]
            if records.identity
            else None,
            "starts_at_raw": records.identity["starts_at_raw"]
            if records.identity
            else None,
            "latest": None
            if latest is None
            else {
                "annotation_revision": int(latest["annotation_revision"]),
                "received_at": latest["received_at"],
                "truncated": bool(latest["truncated"]),
                **_stored_alert(latest["alert_json"]),
            },
            "resolved": [
                {
                    "received_at": row["received_at"],
                    "actor": row["actor"],
                    "annotation_revision": int(row["annotation_revision"]),
                    "raw_sha256": row["raw_sha256"],
                    **_stored_alert(row["alert_json"]),
                }
                for row in rows
                if row["status"] == "resolved"
            ],
        }

    def _recovery_view(self, session_id: UUID) -> dict[str, Any]:
        """One session's recovery verdict with its basis: every stored sample
        and reading (query, window, source, value, hash) and the offline
        replay recomputed from the stored rows (``opspilot.observer.replay``).
        Read-only and deterministic; nothing here queries telemetry or a
        model. A replay that cannot run leaves the basis on the page and says
        so instead of taking the page down."""
        history = self.incidents.observation_history(session_id)
        try:
            result = replay_history(history)
        except Exception:  # noqa: BLE001 - the page must still render the rows
            logging.getLogger(__name__).exception(
                "replay failed for session %s", session_id
            )
            return _recovery_basis_only(history)
        samples = [
            {
                "sample_id": str(sample.sample_id),
                "sequence": sample.sequence,
                "window_start": sample.window_start.isoformat(),
                "window_end": sample.window_end.isoformat(),
                "submitted_at": (
                    None
                    if sample.submitted_at is None
                    else sample.submitted_at.isoformat()
                ),
                "stored_outcome": sample.stored_outcome,
                "stored_required_signals_present": sample.stored_required_signals_present,
                "replayed_outcome": sample.replayed_outcome,
                "replayed_required_signals_present": sample.replayed_required_signals_present,
                "replayed_reason": sample.replayed_reason,
                "recompute_skipped": sample.recompute_skipped,
                "disposition": sample.decision.stored[0],
                "reason": sample.decision.stored[1],
                "confirms_health": sample.decision.stored[2],
                "health_basis": sample.decision.stored[3],
                "transition": sample.decision.stored[4],
                "replayed_decision": sample.decision.replayed,
                "integrity": sample.integrity,
                "consistent": sample.consistent,
                "readings": [
                    {
                        "signal_name": reading.signal_name,
                        "query": reading.query,
                        "source": reading.source,
                        "window_start": reading.window_start.isoformat(),
                        "window_end": reading.window_end.isoformat(),
                        "raw_sha256": reading.raw_sha256,
                        "raw_verified": reading.raw_verified,
                        "stored": reading.stored,
                        "replayed": reading.replayed,
                        "verdict": reading.verdict,
                        "reason": reading.reason,
                    }
                    for reading in sample.readings
                ],
            }
            for sample in result.samples
        ]
        return {
            "session_id": str(result.session_id),
            "state": result.stored_session_state,
            "ended_reason": history["session"].get("ended_reason"),
            "health_profile_revision": result.health_profile_revision,
            "handled_at": result.handled_at.isoformat(),
            "replay": {
                "available": True,
                "consistent": result.consistent,
                "integrity": result.integrity,
                "reasons": result.reasons,
                "recovery_verdict": result.recovery_verdict,
                "recovery_confirmed": result.recovery_confirmed,
                "recomputed_verdict": result.recomputed_verdict,
                "latest_sample_verdict": result.latest_sample_verdict,
                "healthy_window_seconds": result.healthy_window_seconds,
                "expected_lifecycle": result.expected_lifecycle,
                "recorded_lifecycle": result.recorded_lifecycle,
                "stored_session_state": result.stored_session_state,
                "replayed_session_state": result.replayed_session_state,
            },
            "samples": samples,
        }

    def _charts(
        self, incident_id: UUID, report: Mapping[str, Any] | None
    ) -> dict[str, Any] | None:
        """Charts for the metrics evidence the displayed report cites.

        Derived from the claims' citations and read from the stored evidence
        view only; the model chooses nothing here. ``None`` when there is no
        parsed report or nothing cited turned into a chart or placeholder.
        """
        if report is None or not report.get("parsed"):
            return None
        cited: dict[str, list[str]] = {}
        for category in CHART_CATEGORIES:
            for claim in report.get(category, ()):
                for evidence_id in claim["evidence_ids"]:
                    kinds = cited.setdefault(evidence_id, [])
                    if category not in kinds:
                        kinds.append(category)
        items: list[Markup] = []
        figures = overflow = 0
        for evidence_id, kinds in cited.items():
            record = self.evidence_for(incident_id, evidence_id)
            fragment = None
            if record is not None:
                try:
                    fragment = evidence_chart(record, kinds)
                except Exception:
                    # A chart is decoration: one that cannot be drawn must
                    # not take the incident page down.
                    logging.getLogger(__name__).exception(
                        "chart rendering failed for %s", evidence_id
                    )
            if fragment is None:
                continue
            if fragment.kind == "figure":
                figures += 1
                if figures > MAX_FIGURES:
                    overflow += 1
                    continue
            items.append(fragment.html)
        if not items and not overflow:
            return None
        return {"items": items, "overflow": overflow}

    def evidence_for(
        self, incident_id: UUID, evidence_id: str
    ) -> StoredEvidence | None:
        record = self.evidence.get(evidence_id)
        if record is None:
            return None
        if record.run_id not in self.incidents.run_ids(incident_id):
            return None
        return record

    def recovery_reading_for(
        self, incident_id: UUID, evidence_id: str
    ) -> dict[str, Any] | None:
        """The stored observation reading a postmortem cites as recovery
        evidence (``<sample_id>:<signal_name>``, the catalog id of D2), looked
        up only among this incident's sessions. Read-only; ``None`` when the
        id is malformed or names no reading of this incident."""
        raw_sample, sep, signal_name = evidence_id.partition(":")
        if not sep or not signal_name:
            return None
        try:
            sample_id = UUID(raw_sample)
        except ValueError:
            return None
        for session in self.incidents.observation_sessions(incident_id):
            history = self.incidents.observation_history(session["session_id"])
            for sample in history["samples"]:
                if sample["sample_id"] != sample_id:
                    continue
                for reading in sample.get("readings", ()):
                    if reading["signal_name"] == signal_name:
                        return {
                            "session_id": session["session_id"],
                            "target": session["target"],
                            "sample": sample,
                            "reading": reading,
                        }
                return None
        return None

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
                control_generation=lease.control_generation,
            )


# -- views ----------------------------------------------------------------


def _invalid_alert(item: object, code: str) -> AlertResult:
    """R2: an alert that cannot be identified; nothing is recorded for it.
    Echoes the identity fields only when they are well-formed strings."""
    fields = item if isinstance(item, dict) else {}
    fingerprint = fields.get("fingerprint")
    status = fields.get("status")
    return AlertResult(
        fingerprint=fingerprint
        if isinstance(fingerprint, str) and 0 < len(fingerprint) <= 256
        else None,
        starts_at=None,
        status=status if status in ("firing", "resolved") else None,
        outcome="invalid",
        incident_id=None,
        run_id=None,
        delivery_key=None,
        reason=code,
    )


def _stored_alert(alert_json: str) -> dict[str, Any]:
    """The stored (redacted, bounded) alert for display: its annotations and
    ``endsAt`` when the record is whole JSON, else the cut text as is."""
    try:
        stored = json.loads(alert_json)
    except ValueError:
        return {"annotations": None, "ends_at": None, "record_text": alert_json}
    annotations = stored.get("annotations") if isinstance(stored, dict) else None
    ends_at = stored.get("endsAt") if isinstance(stored, dict) else None
    return {
        "annotations": annotations if isinstance(annotations, dict) else None,
        "ends_at": ends_at if isinstance(ends_at, str) else None,
        "record_text": None,
    }


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


def _same_text(peers: Iterable[Mapping[str, Any]], intent: Mapping[str, Any]) -> int:
    """How many unconfirmed peers (``intent`` included) carry ``intent``'s text."""
    text = intent.get("text")
    return sum(1 for other in peers if other.get("text") == text)


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


def _recovery_basis_only(history: Mapping[str, Any]) -> dict[str, Any]:
    """The stored rows without a replay: verdict unknown, nothing claimed."""
    row = history["session"]
    samples = []
    for stored in history["samples"]:
        samples.append(
            {
                "sample_id": str(stored["sample_id"]),
                "sequence": int(stored["sequence"]),
                "window_start": stored["window_start"].isoformat(),
                "window_end": stored["window_end"].isoformat(),
                "submitted_at": (
                    None
                    if stored.get("submitted_at") is None
                    else stored["submitted_at"].isoformat()
                ),
                "stored_outcome": str(stored["outcome"]),
                "stored_required_signals_present": bool(
                    stored["required_signals_present"]
                ),
                "replayed_outcome": None,
                "replayed_required_signals_present": None,
                "replayed_reason": None,
                "recompute_skipped": "REPLAY_FAILED",
                "disposition": str(stored["disposition"]),
                "reason": str(stored["reason"]),
                "confirms_health": bool(stored["confirms_health"]),
                "health_basis": str(stored["health_basis"]),
                "transition": stored.get("transition"),
                "replayed_decision": None,
                "integrity": ("REPLAY_FAILED",),
                "consistent": False,
                "readings": [
                    {
                        "signal_name": str(r["signal_name"]),
                        "query": str(r["query"]),
                        "source": str(r["source"]),
                        "window_start": r["window_start"].isoformat(),
                        "window_end": r["window_end"].isoformat(),
                        "raw_sha256": r.get("raw_sha256"),
                        "raw_verified": None,
                        "stored": (
                            str(r["status"]),
                            r.get("value"),
                            r.get("sample_count"),
                        ),
                        "replayed": None,
                        "verdict": None,
                        "reason": None,
                    }
                    for r in stored.get("readings", ())
                ],
            }
        )
    return {
        "session_id": str(row["session_id"]),
        "state": str(row["state"]),
        "ended_reason": row.get("ended_reason"),
        "health_profile_revision": row.get("health_profile_revision"),
        "handled_at": row["authorized_at"].isoformat(),
        "replay": {
            "available": False,
            "consistent": False,
            "integrity": ("REPLAY_FAILED",),
            "reasons": ("REPLAY_FAILED",),
            "recovery_verdict": "unknown",
            "recovery_confirmed": False,
            "recomputed_verdict": "unknown",
            "latest_sample_verdict": None,
            "healthy_window_seconds": 0,
            "expected_lifecycle": None,
            "recorded_lifecycle": None,
            "stored_session_state": str(row["state"]),
            "replayed_session_state": None,
        },
        "samples": samples,
    }


def _session_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """The page's projection of one observation session row."""
    target = row.get("target") or {}
    return {
        "session_id": str(row["session_id"]),
        "state": str(row["state"]),
        "ended_reason": row.get("ended_reason"),
        "authorized_by": str(row["authorized_by"]),
        "authorized_at": row["authorized_at"],
        "subject_control_generation": int(row["subject_control_generation"]),
        "observation_generation": int(row["observation_generation"]),
        "health_profile_revision": row.get("health_profile_revision"),
        "target_revision": target.get("revision"),
        "deadline_at": row["deadline_at"],
        "max_samples": int(row["max_samples"]),
        "adopted_count": int(row.get("adopted_count", 0)),
        "active_sample_due_at": row.get("active_sample_due_at"),
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


def _is_original_handoff(payload: Mapping[str, Any]) -> bool:
    # Events written before r3 have no marker and are original announcers.
    return payload.get("reconciled") is not True


def _select_snapshot_outcome(
    events: tuple[SubjectEvent, ...],
    run_id: str,
    generation: int,
    runnable: bool,
) -> SubjectEvent | None:
    """The newest ``run_completed``, else the newest eligible ``run_handoff``.

    Among handoffs of the current park (the incident's generation, r3-B) an
    original announcer's event wins over a reconcile backfill, so a backfill
    that raced ahead never hides the real reason.
    """
    mine = [e for e in reversed(events) if e.payload.get("run_id") == run_id]
    completed = next((e for e in mine if e.kind == "run_completed"), None)
    if completed is not None:
        return completed
    handoffs = [
        e
        for e in mine
        if e.kind == "run_handoff"
        and not (runnable and e.payload.get("parked") is False)
    ]
    current = [
        e
        for e in handoffs
        if e.payload.get("parked") is True
        and e.payload.get("control_generation", 0) == generation
    ]
    original = next((e for e in current if _is_original_handoff(e.payload)), None)
    return (
        original
        or (current[0] if current else None)
        or (handoffs[0] if handoffs else None)
    )


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
