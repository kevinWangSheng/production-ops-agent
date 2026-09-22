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
4. conclusion step committed, publish unconfirmed -> ``publish`` only;
5. malformed / incompatible rows    -> durable ``blocked`` handoff.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from opspilot.investigation.context import (
    ContextError,
    InvestigationInput,
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
from opspilot.investigation.store import DurableStepStore
from opspilot.persistence import DurableStore, Lease, PersistenceError
from opspilot.tools.executor import Clock, ReadOnlyToolExecutor
from opspilot.tools.registry import ToolContractError
from opspilot.worker import DEFAULT_LEASE_SECONDS, RecoverySession, Worker

RunnerStatus = Literal[
    "published",  # a conclusion step was committed and published
    "unpublished",  # the loop ended but nothing could be published
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

    def resume(self, incident_id: UUID) -> RunnerOutcome:
        """Continue (or start) the incident's current Run from committed rows."""
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
            return RunnerOutcome(
                "blocked" if code == "INCOMPATIBLE_STATE" else "control_denied",
                reason=code,
            )
        lease = session.lease
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
        except ToolContractError as exc:
            # The executor could not be built (its tool ledger unavailable):
            # nothing ran, so release this exact lease instead of holding it
            # to expiry, and say so (bot review finding, PR #29).
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
            published = self._publish(session, conclusion, step_id)
            return RunnerOutcome(
                "published" if published else "unpublished",
                reason=None if published else "PUBLISH_REFUSED",
                epoch=lease.epoch,
            )
        # Breakpoint 2: finish the committed plan first (only still-pending
        # ordinals are dispatched; the session re-checks the fence per item).
        replayed = session.execute_pending(execute)
        current = self.store.rebuild(incident_id)
        transcript = rebuild_transcript(
            current,
            run_id=str(lease.run_id),
            authorized_targets=executor.scope.target_ids,
            input=input,
        )
        # Breakpoint 4: a conclusion was committed but never published.
        if transcript.pending_publish is not None:
            step_id, conclusion = transcript.pending_publish
            published = self._publish(session, conclusion, step_id)
            return RunnerOutcome(
                "published" if published else "unpublished",
                reason=None if published else "PUBLISH_REFUSED",
                epoch=lease.epoch,
                replayed_tools=replayed,
            )
        # Breakpoint 3: continue the model/tool loop from the rebuilt context.
        loop = InvestigationLoop(
            model=self.model,
            executor=executor,
            store=DurableStepStore(self.store, lease, renew_seconds=self.lease_seconds),
            clock=self.clock,
        )
        outcome = loop.resume(transcript)
        if outcome.final_step_id is None or outcome.conclusion is None:
            return RunnerOutcome(
                "unpublished",
                reason=outcome.handoff_reasons[0] if outcome.handoff_reasons else None,
                loop=outcome,
                epoch=lease.epoch,
                replayed_tools=replayed,
            )
        published = self._publish(session, outcome.conclusion, outcome.final_step_id)
        return RunnerOutcome(
            "published" if published else "unpublished",
            reason=None if published else "PUBLISH_REFUSED",
            loop=outcome,
            epoch=lease.epoch,
            replayed_tools=replayed,
        )

    @staticmethod
    def _publish(
        session: RecoverySession, conclusion: Mapping[str, Any], step_id: UUID
    ) -> bool:
        try:
            return session.publish(dict(conclusion), step_id=step_id)
        except PersistenceError:
            # The session already released its lease; the conclusion step is
            # still in the rows, so the next attempt republishes it.
            return False

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
        return RunnerOutcome("blocked", reason=reason, epoch=lease.epoch)
