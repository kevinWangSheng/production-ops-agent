"""Worker-side composition: resume a Run from PostgreSQL and drive the loop.

This is the only module that knows ``Worker``/``RecoverySession`` (lease,
version gate, pending-tool replay), the transcript rebuild and the
investigation loop at the same time. It owns no policy of its own: every
refusal comes from the store fence, the version gate, the transcript rules
or the loop's own halts.

C3 §7 breakpoints, in the order this module walks them:

1. conclusion already published  -> report ``already_completed``;
2. response committed, tools pending -> ``RecoverySession.execute_pending``;
3. tool results complete            -> ``rebuild_transcript`` + ``loop.resume``;
4. conclusion step committed, not settled -> ``publish`` or ``hand_off`` only;
5. malformed / incompatible rows    -> durable ``blocked`` handoff.

Settling (ADR-0005): only a qualified report with no handoff is published
(``conclusion_publishable``). Every other ending parks the Run for a human
(``DurableStore.hand_off`` -> ``waiting_human``); the incident stays open,
the committed conclusion step stays readable, and ``control()`` still takes
follow_up / correct / cancel (then ``new_run``). A parked Run is never
claimed again by this runner until a human acts.

With ``events`` set, the attempt announces the same progress the workbench
page consumes (``opspilot.investigation.progress``): ``run_claimed``, one
``step_committed``/``tool_committed`` per committed row, then exactly one
``run_completed`` or ``run_handoff`` per *settled* attempt. An attempt that
crashes (an exception out of the model client or the executor) announces no
terminal event and keeps its lease to expiry, exactly like a killed worker;
the next attempt resumes from the rows. Tool results replayed by breakpoint
2 are committed by the session, not the loop, and are announced right after
the replay from the committed rows (``announce_recovered_tools``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from opspilot.investigation.context import (
    ContextError,
    InvestigationInput,
    conclusion_publishable,
    pending_conclusion,
    rebuild_transcript,
)
from opspilot.investigation.limits import M1_FROZEN_LIMITS
from opspilot.investigation.loop import (
    InvestigationLoop,
    LoopOutcome,
    ModelClient,
    tool_request_for,
)
from opspilot.investigation.messages import PairingError, validate_tool_calls
from opspilot.investigation.progress import (
    DEADLINE_EXCEEDED,
    EmittingCommitter,
    EvidenceProjection,
    ProgressLog,
    announce_claim_refused,
    announce_claimed,
    announce_completed,
    announce_handoff,
    announce_recovered_tools,
    sweep_expired,
)
from opspilot.investigation.store import (
    DurableStepStore,
    StepCommitter,
    StepStoreError,
)
from opspilot.persistence import DurableStore, Lease, PersistenceError
from opspilot.tools.executor import Clock, ReadOnlyToolExecutor
from opspilot.tools.registry import ToolContractError
from opspilot.worker import DEFAULT_LEASE_SECONDS, RecoverySession, Worker

RunnerStatus = Literal[
    "published",  # a qualified conclusion step was committed and published
    "handed_off",  # the Run is parked for a human (this attempt, or already)
    "unpublished",  # a publishable conclusion was refused (late result)
    "already_completed",
    "control_denied",
    "blocked",
    "aborted",  # the attempt could not start; its lease was released
]

ExecutorFactory = Callable[[Lease, InvestigationInput], ReadOnlyToolExecutor]


@dataclass(frozen=True)
class RunnerOutcome:
    status: RunnerStatus
    reason: str | None = None
    loop: LoopOutcome | None = None
    epoch: int | None = None
    replayed_tools: int = 0


@dataclass
class InvestigationRunner:
    store: DurableStore
    worker: Worker
    model: ModelClient
    executor_factory: ExecutorFactory
    clock: Clock
    lease_seconds: int = DEFAULT_LEASE_SECONDS
    # Workbench projections (C3 section 9). ``None`` runs silently.
    # ``evidence`` must be the same store the executor built by
    # ``executor_factory`` registers evidence into: it pins rows that already
    # exist, and a missing row is an ``EVIDENCE_PROJECTION_FAILED`` handoff.
    events: ProgressLog | None = None
    evidence: EvidenceProjection | None = None

    def resume(self, incident_id: UUID) -> RunnerOutcome:
        """Continue (or start) the incident's current Run from committed rows."""
        # ADR-0005 decision 2: the poll is where an overdue Run gets swept.
        # Its deadline fences every worker write, so nothing else can settle
        # it; parking it here (and announcing it once) is what lets the page
        # stop showing "investigating" for ever.
        try:
            swept = {
                run_id
                for _, run_id in sweep_expired(
                    self.store, self.events, incident_id=incident_id
                )
            }
        except PersistenceError:
            # A lock timeout or an outage while sweeping: the Run stays as
            # it is and the next poll sweeps it; the attempt below then
            # meets the same fence ``claim()`` always applied.
            swept = set()
        try:
            snapshot = self.store.rebuild(incident_id)
        except PersistenceError as exc:
            if str(exc) != "INCONSISTENT_STATE":
                raise
            return self._block_undecodable(incident_id)
        if snapshot["conclusion"] is not None or snapshot["run"]["state"] in {
            "completed",
            "cancelled",
        }:
            return RunnerOutcome("already_completed")
        if snapshot["run"]["run_id"] in swept:
            return RunnerOutcome("handed_off", reason=DEADLINE_EXCEEDED)
        if snapshot["run"]["state"] == "waiting_human":
            # Parked by a handoff: a human re-queues (follow_up/correct) or
            # cancels it. Neither a claim nor an event -- the page already
            # holds the ``run_handoff`` that parked it.
            return RunnerOutcome("handed_off", reason="AWAITING_HUMAN")
        try:
            session = self.worker.resume(
                incident_id,
                lease_seconds=self.lease_seconds,
                renew_seconds=self.lease_seconds,
            )
        except PersistenceError as exc:
            code = str(exc)
            if code == "INCONSISTENT_STATE":
                return self._block_undecodable(incident_id)
            if (
                self.events is not None
                and code != "LEASE_ACTIVE"
                and snapshot["state"] not in {"paused", "cancelled", "completed"}
                and snapshot["run"]["state"] in {"queued", "running"}
            ):
                # A live lease elsewhere is the normal state while another
                # worker runs, and a paused incident or a paused/blocked Run
                # is refused on every poll by design; announcing those would
                # flood the page and push the terminal event out of its
                # newest page. Only a refusal of a Run that looked runnable
                # is news.
                announce_claim_refused(
                    self.events, incident_id, snapshot["run"]["run_id"], code
                )
            return RunnerOutcome(
                "blocked" if code == "INCOMPATIBLE_STATE" else "control_denied",
                reason=code,
            )
        lease = session.lease
        if self.events is not None:
            announce_claimed(self.events, lease)
        try:
            return self._continue(incident_id, session, snapshot)
        except ContextError as exc:
            # Malformed or incompatible business rows: C3 §7 says block and
            # hand off, never guess. ``block`` is fenced like every write, so
            # a Run that human control already moved on is left alone.
            return self._blocked(lease, exc.code)
        except PersistenceError as exc:
            # A fence or storage refusal met after the claim (a pending tool
            # commit, a rebuild, a publish): a typed outcome, not a crash.
            code = str(exc)
            return RunnerOutcome(
                "blocked" if code == "INCOMPATIBLE_STATE" else "control_denied",
                reason=code,
                epoch=lease.epoch,
            )
        except (ToolContractError, StepStoreError) as exc:
            # The attempt could not open: the executor's tool ledger or the
            # step store's usage read was unavailable. Nothing ran, so
            # release this exact lease instead of holding it to expiry, and
            # say so (bot review findings, PR #29).
            try:
                self.store.abandon(lease)
            except PersistenceError:
                pass
            return RunnerOutcome("aborted", reason=str(exc), epoch=lease.epoch)

    def _continue(
        self, incident_id: UUID, session: RecoverySession, snapshot: Mapping[str, Any]
    ) -> RunnerOutcome:
        lease = session.lease
        # Re-read after the claim: a cancel + new_run between the first
        # ``rebuild()`` and ``Worker.resume()`` replaces the current Run, and
        # the leased Run's own input snapshot -- never the earlier Run's --
        # is what this attempt may run with (bot review finding, PR #29).
        try:
            snapshot = self.store.rebuild(incident_id)
        except PersistenceError as exc:
            if str(exc) != "INCONSISTENT_STATE":
                raise
            # Rows this version cannot decode, met after the claim: the
            # fenced block path below is the C3 §7 handoff for them.
            raise ContextError("INCONSISTENT_STATE") from exc
        if str(snapshot["run"]["run_id"]) != str(lease.run_id):
            raise ContextError("RUN_MISMATCH")
        recorded = snapshot["run"].get("input")
        if recorded is None:
            raise ContextError("INPUT_MISSING")
        input = InvestigationInput.from_json(recorded)
        if not input.limits.within(M1_FROZEN_LIMITS):
            # The product boundary: a Run admitted with ceilings above the
            # frozen ones does not run, whatever its row says.
            raise ContextError("LIMITS_EXCEED_FREEZE")
        executor = self.executor_factory(lease, input)
        if executor.scope.run_id != str(lease.run_id):
            raise ContextError("RUN_MISMATCH")
        target_ref = input.bound_target_id
        if target_ref is None and len(executor.scope.target_ids) == 1:
            target_ref = next(iter(executor.scope.target_ids))
        window = executor.scope.window.as_json()

        def execute(item: Mapping[str, Any]) -> Mapping[str, Any]:
            outcome = executor.execute(
                tool_request_for(
                    item["step_id"],
                    int(item["ordinal"]),
                    item["tool_call"],
                    target_ref=target_ref,
                    window=window,
                )
            )
            return dict(outcome.model_view)

        # Breakpoint 4 first: a Run that already concluded must not execute
        # anything it left behind, whatever ``pending_tools`` says.
        concluded = pending_conclusion(self.store.rebuild(incident_id))
        if concluded is not None:
            step_id, conclusion = concluded
            return self._settle(session, step_id, conclusion)
        # Breakpoint 2: finish the committed plan first (only still-pending
        # ordinals are dispatched; the session re-checks the fence per item).
        # ``DurableStore.rebuild()`` only checks that a committed tool plan is
        # a list of dicts (INCONSISTENT_STATE otherwise); it accepts a call
        # shaped like ``{}``, which reaches this replay before
        # ``rebuild_transcript()`` gets a chance to run the same
        # ``validate_tool_calls()`` the live loop uses. Unvalidated, that call
        # would reach ``tool_request_for()`` and crash on a bare ``KeyError``
        # instead of a durable handoff (bot review finding). Validate every
        # pending call's shape first, one at a time so calls recovered from
        # different steps are never compared against each other's ids.
        # ``recovery.rebuild_plan`` freezes every nested dict into a
        # ``MappingProxyType``, which ``validate_tool_calls`` -- built for a
        # freshly parsed provider message -- rejects with its literal
        # ``isinstance(call, dict)`` check even when the call is well formed;
        # unfreeze the one level it inspects that way before validating.
        for item in session.plan.pending_tools:
            try:
                validate_tool_calls({"tool_calls": [dict(item["tool_call"])]})
            except PairingError as exc:
                raise ContextError("INCONSISTENT_STATE") from exc
        replayed = session.execute_pending(execute)
        current = self.store.rebuild(incident_id)
        if self.events is not None and replayed:
            try:
                announce_recovered_tools(
                    self.events,
                    incident_id,
                    lease.run_id,
                    current["steps"],
                    session.plan.pending_tools,
                    evidence=self.evidence,
                )
            except StepStoreError as exc:
                # Same disposition as the loop path: the rows are committed,
                # the projection is not, and that is a visible handoff.
                return self._hand_off(
                    lease,
                    "failed",
                    (exc.code,),
                    report_sha256=None,
                    evidence_ids=(),
                    loop=None,
                    replayed=replayed,
                )
        transcript = rebuild_transcript(
            current,
            run_id=str(lease.run_id),
            authorized_targets=executor.scope.target_ids,
            input=input,
        )
        # Breakpoint 4: a conclusion was committed but never settled.
        if transcript.pending_publish is not None:
            step_id, conclusion = transcript.pending_publish
            return self._settle(session, step_id, conclusion, replayed=replayed)
        # Breakpoint 3: continue the model/tool loop from the rebuilt context.
        committer: StepCommitter = DurableStepStore(
            self.store, lease, renew_seconds=self.lease_seconds
        )
        if self.events is not None:
            committer = EmittingCommitter(
                committer,
                self.events,
                incident_id,
                lease.run_id,
                evidence=self.evidence,
            )
        loop = InvestigationLoop(
            model=self.model, executor=executor, store=committer, clock=self.clock
        )
        outcome = loop.resume(transcript)
        if outcome.final_step_id is None or outcome.conclusion is None:
            # The store fenced the terminal step (or refused it): there is no
            # row to publish, and what the loop saw is still a handoff.
            return self._hand_off(
                lease,
                outcome.execution,
                outcome.handoff_reasons,
                report_sha256=outcome.report_content_sha256,
                evidence_ids=outcome.evidence_ids,
                loop=outcome,
                replayed=replayed,
            )
        return self._settle(
            session,
            outcome.final_step_id,
            outcome.conclusion,
            loop=outcome,
            replayed=replayed,
        )

    def _settle(
        self,
        session: RecoverySession,
        step_id: UUID,
        conclusion: Mapping[str, Any],
        *,
        loop: LoopOutcome | None = None,
        replayed: int = 0,
    ) -> RunnerOutcome:
        """Publish a qualified conclusion row, or park the Run on a handoff row.

        ``conclusion`` is the committed terminal step (from the loop just now
        or from the rows after a restart); ``conclusion_publishable`` is the
        one rule both drivers apply to it.
        """
        lease = session.lease
        body = conclusion.get("conclusion")
        body = body if isinstance(body, Mapping) else {}
        if not conclusion_publishable(conclusion):
            reasons = tuple(
                str(item) for item in (body.get("handoff_reasons") or ()) if item
            )
            evidence_ids = tuple(
                str(item) for item in (body.get("evidence_ids") or ()) if item
            )
            return self._hand_off(
                lease,
                str(body.get("execution") or "failed"),
                reasons,
                report_sha256=body.get("report_content_sha256"),
                evidence_ids=evidence_ids,
                loop=loop,
                replayed=replayed,
            )
        try:
            published = session.publish(dict(conclusion), step_id=step_id)
        except PersistenceError:
            # The session already released its lease; the conclusion step is
            # still in the rows, so the next attempt republishes it.
            published = False
        if not published:
            return RunnerOutcome(
                "unpublished",
                reason="PUBLISH_REFUSED",
                loop=loop,
                epoch=lease.epoch,
                replayed_tools=replayed,
            )
        if self.events is not None:
            announce_completed(
                self.events,
                lease.incident_id,
                lease.run_id,
                execution=str(body.get("execution")),
                report_sha256=body.get("report_content_sha256"),
                evidence_ids=tuple(
                    str(item) for item in (body.get("evidence_ids") or ()) if item
                ),
                model_requests_used=body.get("model_requests_used"),
                prompt_revision=body.get("prompt_revision"),
            )
        return RunnerOutcome(
            "published", loop=loop, epoch=lease.epoch, replayed_tools=replayed
        )

    def _hand_off(
        self,
        lease: Lease,
        execution: str,
        reasons: tuple[str, ...],
        *,
        report_sha256: str | None,
        evidence_ids: tuple[str, ...],
        loop: LoopOutcome | None,
        replayed: int,
    ) -> RunnerOutcome:
        """Park the Run under ``lease`` and report what actually landed.

        Like ``_blocked``: ``handed_off`` only when the fenced write
        succeeded. A refusal (human control moved the Run on, the lease
        lapsed, storage failed) is ``control_denied`` and nothing is
        announced -- the Run is someone else's now, or the next attempt
        converges on the same rows.
        """
        try:
            self.store.hand_off(lease)
        except PersistenceError as exc:
            return RunnerOutcome(
                "control_denied",
                reason=str(exc),
                loop=loop,
                epoch=lease.epoch,
                replayed_tools=replayed,
            )
        if self.events is not None:
            announce_handoff(
                self.events,
                lease.incident_id,
                lease.run_id,
                execution,
                reasons,
                report_sha256=report_sha256,
                evidence_ids=evidence_ids,
            )
        return RunnerOutcome(
            "handed_off",
            reason=reasons[0] if reasons else None,
            loop=loop,
            epoch=lease.epoch,
            replayed_tools=replayed,
        )

    def _block_undecodable(self, incident_id: UUID) -> RunnerOutcome:
        """Malformed committed rows met before any lease was held.

        ``rebuild()`` refuses to decode them, so the Run can never be run
        again by this version; without a durable ``blocked`` state every
        retry would crash at the same read instead of handing off (bot
        review finding, PR #29). Claim through the metadata read -- which
        decodes nothing -- then block under that lease, fenced like every
        other write. A claim refused by human control or another holder
        leaves the Run as it is.
        """
        try:
            metadata = self.store.recovery_metadata(incident_id)
            lease = self.worker.claim(
                incident_id, metadata["run_id"], lease_seconds=self.lease_seconds
            )
        except PersistenceError as exc:
            code = str(exc)
            # ``claim`` itself blocks a version-incompatible Run durably.
            return RunnerOutcome(
                "blocked" if code == "INCOMPATIBLE_STATE" else "control_denied",
                reason=code,
            )
        return self._blocked(lease, "INCONSISTENT_STATE")

    def _blocked(self, lease: Lease, reason: str) -> RunnerOutcome:
        """Block under ``lease`` and report what actually landed.

        The outcome is evidence of state: ``blocked`` only when the fenced
        write succeeded; a refusal (human control moved the Run on, or
        storage failed) is reported as such, and the next attempt converges.
        """
        try:
            self.store.block(lease)
        except PersistenceError as exc:
            return RunnerOutcome("control_denied", reason=str(exc), epoch=lease.epoch)
        if self.events is not None:
            # ``blocked`` is durable but not ``waiting_human``: control does
            # not re-queue it, so the event must not claim a park.
            announce_handoff(
                self.events,
                lease.incident_id,
                lease.run_id,
                "blocked",
                (reason,),
                parked=False,
            )
        return RunnerOutcome("blocked", reason=reason, epoch=lease.epoch)
