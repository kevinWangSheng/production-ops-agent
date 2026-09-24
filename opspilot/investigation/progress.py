"""Progress announcements shared by the two drivers (C3 section 9 projection).

The workbench page consumes one event vocabulary per subject: ``run_claimed``,
``step_committed``, ``tool_committed``, ``run_completed``, ``run_handoff``,
``run_claim_refused``. This module is the single place that shapes those
payloads, so a Run driven by ``InvestigationRunner`` (the real driver) and one
driven by ``Workbench.run_once`` (tests and demos) look identical on the page.

Nothing here carries authority: every append happens after the business row
committed, and a reader that misses an event still finds the truth in the
rows (``DurableStore.rebuild``). The ``ProgressLog`` and
``EvidenceProjection`` protocols are the structural slice of the web layer's
``EventLog`` / ``EvidenceStore`` this package may depend on without importing
``opspilot.web``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol
from uuid import UUID

from opspilot.investigation.context import CONCLUSION_KIND
from opspilot.investigation.store import BudgetUsage, StepCommitter, StepStoreError
from opspilot.persistence import Lease, PersistenceError


class ProgressLog(Protocol):
    def append(
        self, subject_id: UUID, kind: str, payload: Mapping[str, Any]
    ) -> int: ...

    def append_once(
        self,
        subject_id: UUID,
        kind: str,
        payload: Mapping[str, Any],
        *,
        key: Mapping[str, Any] | None = None,
    ) -> int: ...


class EvidenceProjection(Protocol):
    def commit(self, evidence_id: str, view: Mapping[str, Any]) -> None: ...


def announce_claimed(log: ProgressLog, lease: Lease) -> int:
    return log.append(
        lease.incident_id,
        "run_claimed",
        {
            "run_id": str(lease.run_id),
            "epoch": lease.epoch,
            "control_generation": lease.control_generation,
        },
    )


def announce_claim_refused(
    log: ProgressLog, subject_id: UUID, run_id: UUID, code: str
) -> int:
    return log.append(
        subject_id, "run_claim_refused", {"run_id": str(run_id), "code": code}
    )


def announce_completed(
    log: ProgressLog,
    subject_id: UUID,
    run_id: UUID,
    *,
    execution: str,
    report_sha256: str | None,
    evidence_ids: tuple[str, ...],
    model_requests_used: int | None,
    prompt_revision: str | None,
) -> int:
    """Keyed by run: a run publishes at most once, so a retry or the page's
    reconciler that finds the fact already announced reuses that event."""
    return log.append_once(
        subject_id,
        "run_completed",
        {
            "run_id": str(run_id),
            "published": True,
            "execution": execution,
            "handoff": False,
            "handoff_reasons": [],
            "report_sha256": report_sha256,
            "evidence_ids": list(evidence_ids),
            "model_requests_used": model_requests_used,
            "prompt_revision": prompt_revision,
        },
        key={"run_id": str(run_id)},
    )


def announce_handoff(
    log: ProgressLog,
    subject_id: UUID,
    run_id: UUID,
    execution: str,
    reasons: tuple[str, ...],
    *,
    report_sha256: str | None = None,
    evidence_ids: tuple[str, ...] = (),
    parked: bool = True,
) -> int:
    """``parked`` says whether the Run really landed in ``waiting_human``.

    False records what a fenced or crashed attempt saw without claiming a
    durable park: the Run row is the authority for where it is now.
    """
    return log.append(
        subject_id,
        "run_handoff",
        {
            "run_id": str(run_id),
            "published": False,
            "execution": execution,
            "handoff": True,
            "parked": parked,
            "reasons": list(reasons),
            "report_sha256": report_sha256,
            "evidence_ids": list(evidence_ids),
        },
    )


#: The handoff reason a swept Run carries (ADR-0005 decision 2).
DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"


class ExpirySweeper(Protocol):
    def sweep_expired_runs(
        self, *, incident_id: UUID | None = None, limit: int = 100
    ) -> tuple[tuple[UUID, UUID], ...]: ...


def announce_deadline_exceeded(log: ProgressLog, subject_id: UUID, run_id: UUID) -> int:
    """The ``run_handoff`` a timeout park shows on the page, keyed by run.

    A sweep parks a Run at most once, and the key makes a second announcer
    of the same park (a racing sweep, a retry) a no-op instead of a
    duplicate. It is at-most-once, not exactly-once: a sweeper that dies
    between the park and this append leaves the row ``waiting_human`` with
    no event, and nothing here repairs that -- the row cannot say *why* it
    was parked, so a repair would have to guess between a loop handoff and
    a timeout. The rows stay the authority (ADR-0003); the projection
    repair is the follow-up recorded in ROADMAP.
    """
    return log.append_once(
        subject_id,
        "run_handoff",
        {
            "run_id": str(run_id),
            "published": False,
            "execution": "failed",
            "handoff": True,
            "parked": True,
            "reasons": [DEADLINE_EXCEEDED],
            "report_sha256": None,
            "evidence_ids": [],
        },
        key={"run_id": str(run_id), "parked": True, "reasons": [DEADLINE_EXCEEDED]},
    )


def sweep_expired(
    store: ExpirySweeper, log: ProgressLog | None, *, incident_id: UUID | None = None
) -> tuple[tuple[UUID, UUID], ...]:
    """Park overdue ``running`` Runs and announce each park (ADR-0005 §2).

    The store's sweep is the authority (state + deadline re-checked under row
    locks); the event is a projection appended after the row committed, like
    every other announcement here. Returns what this call parked.
    """
    parked = store.sweep_expired_runs(incident_id=incident_id)
    if log is not None:
        for subject_id, run_id in parked:
            announce_deadline_exceeded(log, subject_id, run_id)
    return parked


def _tool_committed_payload(
    run_id: UUID, step_id: UUID, ordinal: int, result: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "run_id": str(run_id),
        "step_id": str(step_id),
        "ordinal": ordinal,
        "evidence_id": result.get("evidence_id"),
        "status": result.get("status"),
        "adopted": result.get("adopted"),
        "tool": result.get("tool"),
        "target_id": result.get("target_id"),
    }


def _pin_evidence(
    evidence: EvidenceProjection | None, result: Mapping[str, Any]
) -> None:
    """Pin the projection to the committed tool result (the authority).

    A projection that cannot find the row is a visible gap, not something to
    run past: ``StepStoreError("EVIDENCE_PROJECTION_FAILED")`` makes the
    caller hand off with a fixed reason. The usual cause is an executor that
    registered into another evidence store (independent review, P2-2).
    """
    evidence_id = result.get("evidence_id")
    if evidence is None or not isinstance(evidence_id, str):
        return
    try:
        evidence.commit(evidence_id, result)
    except PersistenceError as exc:
        raise StepStoreError(
            "EVIDENCE_PROJECTION_FAILED:" + str(exc)
            if str(exc) != "UNKNOWN_IDENTITY"
            else "EVIDENCE_PROJECTION_FAILED"
        ) from None


def announce_recovered_tools(
    log: ProgressLog,
    subject_id: UUID,
    run_id: UUID,
    steps: Any,
    pending: Any,
    *,
    evidence: EvidenceProjection | None = None,
) -> int:
    """Announce tool results a recovery replay committed outside the loop.

    ``RecoverySession.execute_pending`` commits through the store, not the
    committer, so the page would never see those results live (bot review,
    PR #44). ``pending`` is the plan the session replayed and ``steps`` the
    rows read after it: every planned ordinal that is committed now is
    announced with the same payload the loop path uses, and its evidence is
    pinned the same way. Returns how many were announced.
    """
    by_step: dict[Any, Mapping[str, Any]] = {
        step["step_id"]: step for step in steps if isinstance(step, Mapping)
    }
    count = 0
    for item in pending:
        step = by_step.get(item["step_id"])
        if step is None or step.get("status") == "late_result":
            continue
        for entry in step.get("tool_results") or ():
            if not isinstance(entry, Mapping) or entry.get("ordinal") != int(
                item["ordinal"]
            ):
                continue
            result = entry.get("result")
            if not isinstance(result, Mapping):
                continue
            _pin_evidence(evidence, result)
            log.append(
                subject_id,
                "tool_committed",
                _tool_committed_payload(
                    run_id, item["step_id"], int(item["ordinal"]), result
                ),
            )
            count += 1
    return count


class EmittingCommitter:
    """Wrap the loop's committer so each committed step/tool becomes an event.

    The event is appended after the business commit returns, so a reader
    never sees progress that did not persist. Refusals pass through
    untouched: the loop's handoff semantics stay the loop's.
    """

    def __init__(
        self,
        base: StepCommitter,
        events: ProgressLog,
        subject_id: UUID,
        run_id: UUID,
        *,
        renew: Callable[[], object] | None = None,
        evidence: EvidenceProjection | None = None,
    ) -> None:
        self._evidence = evidence
        self._base = base
        self._events = events
        self._subject_id = subject_id
        self._run_id = run_id
        self._renew_lease = renew
        self.last_step: tuple[UUID, dict[str, Any]] | None = None
        self.steps_seen = 0
        self.renewals = 0
        self.renewal_refused = False

    @property
    def authorized_run_id(self) -> str:
        return self._base.authorized_run_id

    @property
    def control_generation(self) -> int:
        return self._base.control_generation

    def usage(self) -> BudgetUsage:
        return self._base.usage()

    def renew(self) -> None:
        """The loop's pre-dispatch renewal (PR #29 / #35 on main).

        With ``renew`` given, the base committer was built without
        ``renew_seconds`` and the lease is renewed here; a refusal must
        surface, because the loop calls this right before reading production
        again and relies on ``StepStoreError`` to halt first
        (``StepCommitter.renew``). Without ``renew`` the base renews itself
        (``DurableStepStore(renew_seconds=...)``) and this forwards to it.
        """
        if self._renew_lease is None:
            self._base.renew()
            return
        self._renew()
        if self.renewal_refused:
            raise StepStoreError("CONTROL_DENIED")

    def _renew(self) -> None:
        """Extend the lease before touching the store (C3 section 6).

        A refused renewal (``CONTROL_DENIED``) means this attempt no longer
        owns the Run. The call is still forwarded: the store re-checks the
        identical fence, refuses the same way, and, for a step or tool
        result, records the late result as history exactly as an expired
        lease would. Any other storage failure stops the attempt here.
        """
        if self._renew_lease is None:
            return
        try:
            self._renew_lease()
        except PersistenceError as exc:
            if str(exc) == "CONTROL_DENIED":
                self.renewal_refused = True
                return
            raise StepStoreError(str(exc)) from None
        self.renewals += 1

    def reserve_budget(
        self, reservation_id: UUID, amount: int, *, seconds: float = 0.0
    ) -> None:
        self._renew()
        self._base.reserve_budget(reservation_id, amount, seconds=seconds)

    def settle_budget(
        self, reservation_id: UUID, outcome: str, *, seconds: float | None = None
    ) -> None:
        self._renew()
        self._base.settle_budget(reservation_id, outcome, seconds=seconds)

    def begin_round(self, logical_key: str) -> Any:
        # The renewal before a model round is the one that matters most: it
        # guarantees the lease covers the whole request that follows.
        self._renew()
        return self._base.begin_round(logical_key)

    def assert_current(self) -> None:
        self._renew()
        self._base.assert_current()

    def commit_step(self, logical_key: str, response: Mapping[str, Any]) -> UUID:
        self._renew()
        step_id = self._base.commit_step(logical_key, response)
        self.last_step = (step_id, dict(response))
        if response.get("kind") == CONCLUSION_KIND:
            # The loop's terminal record, not a model round: the
            # ``run_completed``/``run_handoff`` event that follows announces
            # it together with the outcome, so it is not a progress step.
            return step_id
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
        self._renew()
        self._base.commit_tool(step_id, ordinal, result)
        # The committed tool result is the authority for what was consumed:
        # pin the evidence projection to it so a stale replay (an expired
        # worker returning the same bytes later) cannot replace it.
        _pin_evidence(self._evidence, result)
        self._events.append(
            self._subject_id,
            "tool_committed",
            _tool_committed_payload(self._run_id, step_id, ordinal, result),
        )
