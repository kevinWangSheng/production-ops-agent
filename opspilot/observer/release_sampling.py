"""Serial release telemetry collection; storage owns adoption and the verdict."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import uuid4

from opspilot.domain.evidence import QueryWindow
from opspilot.observation.release_comparison import (
    COMPARISON_ATTEMPT_SECONDS,
    COMPARISON_MAX_ATTEMPTS,
    ComparisonLease,
    ComparisonReceipt,
    ReleaseComparisonEvidence,
    before_window,
    build_release_comparison_plan,
    compare_evidence,
    mapped_old_instances,
    needs_retry,
)
from opspilot.observation.release_policy import sha256
from opspilot.observation.release_types import (
    ReleaseEvaluation,
    ReleaseQueryResult,
    ReleaseSampleEvidence,
    ReleaseSampleLease,
    ReleaseSampleReceipt,
)
from opspilot.observation.store import ObservationStore
from opspilot.observer.prometheus import VectorResult
from opspilot.observer.release_evaluation import (
    build_release_query_plan,
    evaluate_release_sample,
)
from opspilot.observer.sampler import SUBMIT_MARGIN_SECONDS, InstantSource

__all__ = [
    "ReleaseComparisonTaken",
    "ReleaseSource",
    "ReleaseSampleTaken",
    "take_release_comparison",
    "take_release_sample",
]

_log = logging.getLogger("opspilot.observer")


class ReleaseSource(InstantSource, Protocol):
    def instant_vector(
        self, expr: str, *, at: datetime, timeout_seconds: int
    ) -> VectorResult: ...


@dataclass(frozen=True)
class ReleaseSampleTaken:
    receipt: ReleaseSampleReceipt
    requests_issued: int
    scope_interrupted: bool
    budget_exhausted: bool = False


def take_release_sample(
    lease: ReleaseSampleLease,
    source: ReleaseSource,
    store: ObservationStore,
    *,
    scope_current: Callable[[ReleaseSampleLease], bool] | None = None,
) -> ReleaseSampleTaken:
    """Collect mapping/context, derive U, collect after, then submit raw evidence.

    Unavailable frozen context or an invalid plan raises without submitting a
    fabricated zero-query sample. An interrupted scope submits the evidence
    already collected; the store applies its current adoption fences.
    """
    context = store.release_context(lease.session_id)
    if (
        context.session_id != lease.session_id
        or context.release.observation_id != lease.observation_id
        or context.health_profile.revision != lease.health_profile_revision
        or context.policy.revision != lease.calibrated_policy_revision
    ):
        raise ValueError("RELEASE_CONTEXT_MISMATCH")
    check = scope_current or store.lease_scope_current
    window_end = store.current_time()
    budget_end = (
        time.monotonic()
        + (lease.lease_until - window_end).total_seconds()
        - SUBMIT_MARGIN_SECONDS
    )
    window = QueryWindow(
        start=window_end
        - timedelta(seconds=context.health_profile.evaluation_window_seconds),
        end=window_end,
    )
    sample_id = uuid4()
    plan = build_release_query_plan(context, window_end=window_end, instance_uids=())
    gathered: list[ReleaseQueryResult] = []
    issued = 0
    interrupted = exhausted = False

    def evidence() -> ReleaseSampleEvidence:
        return ReleaseSampleEvidence(
            format_version=1,
            sample_id=sample_id,
            window=window,
            queries=tuple(gathered),
        )

    def collect(queries: dict[str, str]) -> None:
        nonlocal issued, interrupted, exhausted
        for key, expr in queries.items():
            left = budget_end - time.monotonic()
            if not exhausted and left >= 1.0:
                if not check(lease):
                    interrupted = True
                    return
                left = budget_end - time.monotonic()
            if exhausted or left < 1.0:
                exhausted = True
                result = VectorResult(
                    expr=expr,
                    status="timeout",
                    series=(),
                    body=b"",
                    http_status=None,
                    detail="LEASE_BUDGET",
                    body_complete=False,
                    sent_at=None,
                )
            else:
                result = source.instant_vector(
                    expr,
                    at=window_end,
                    timeout_seconds=min(
                        context.health_profile.query_timeout_seconds, int(left)
                    ),
                )
                issued += 1
            gathered.append(
                ReleaseQueryResult(
                    query_key=key,
                    query=expr,
                    source="prometheus",
                    evaluated_at=window_end,
                    sent_at=result.sent_at,
                    status=result.status,
                    http_status=result.http_status,
                    detail=result.detail,
                    body_complete=result.body_complete,
                    raw=result.body,
                    raw_sha256=sha256(result.body),
                )
            )

    collect(plan)
    if not interrupted:
        mapped = evaluate_release_sample(context, evidence(), decision_at=window_end)
        full_plan = build_release_query_plan(
            context,
            window_end=window_end,
            instance_uids=tuple(instance.pod_uid for instance in mapped.instances),
        )
        collect({key: expr for key, expr in full_plan.items() if key not in plan})
    receipt = store.submit_release_sample(lease, evidence())
    _log.info(
        "release sample observation_id=%s session=%s sequence=%s requests=%s interrupted=%s budget_exhausted=%s disposition=%s reason=%s outcome=%s state=%s release_status=%s",
        lease.observation_id,
        lease.session_id,
        lease.sequence,
        issued,
        interrupted,
        exhausted,
        receipt.disposition,
        receipt.reason,
        receipt.evaluation.outcome,
        receipt.session_state,
        receipt.release_status,
    )
    return ReleaseSampleTaken(receipt, issued, interrupted, exhausted)


@dataclass(frozen=True)
class ReleaseComparisonTaken:
    receipt: ComparisonReceipt | None
    requests_issued: int
    attempts: int
    scope_interrupted: bool
    # the Observer is stopping: the attempt was abandoned, nothing submitted
    stopped: bool = False


def take_release_comparison(
    lease: ComparisonLease,
    source: ReleaseSource,
    store: ObservationStore,
    *,
    scope_current: Callable[[ComparisonLease], bool] | None = None,
    stopped: Callable[[], bool] | None = None,
) -> ReleaseComparisonTaken:
    """Collect the before evidence at ``released_at``, then submit it once.

    An attempt with failed or timed-out queries claims the next durable
    attempt row while attempts remain; the last attempt's raw evidence is
    submitted and storage computes the record. Attempts are rows claimed
    before their queries, so the total stays within
    ``COMPARISON_MAX_ATTEMPTS`` plans per ending across Observers and
    crashes. A suspended scope
    stops the collection without a submission: the ending stays pending and
    is collected again after the suspension is lifted. ``stopped`` (the
    Observer shutting down) is checked before every query, before the next
    attempt and before submission; it abandons the attempt unsubmitted.
    """
    context = store.comparison_context(lease)
    check = scope_current or store.comparison_scope_current
    window = before_window(context)
    at = window.end
    issued = 0
    halted = False
    gathered: list[ReleaseQueryResult] = []

    def evidence(attempt: int) -> ReleaseComparisonEvidence:
        return ReleaseComparisonEvidence(
            format_version=1,
            comparison_id=lease.comparison_id,
            attempts=attempt,
            window=window,
            queries=tuple(gathered),
        )

    def collect(queries: dict[str, str], budget_end: float) -> bool:
        """False when the scope or an Observer stop ended the collection."""
        nonlocal issued, halted
        for key, expr in queries.items():
            if stopped is not None and stopped():
                halted = True
                return False
            if not check(lease):
                return False
            left = budget_end - time.monotonic()
            if left < 1.0:
                result = VectorResult(
                    expr=expr,
                    status="timeout",
                    series=(),
                    body=b"",
                    http_status=None,
                    detail="COMPARISON_BUDGET",
                    body_complete=False,
                    sent_at=None,
                )
            else:
                result = source.instant_vector(
                    expr,
                    at=at,
                    timeout_seconds=min(
                        context.health_profile.query_timeout_seconds, int(left)
                    ),
                )
                issued += 1
            gathered.append(
                ReleaseQueryResult(
                    query_key=key,
                    query=expr,
                    source="prometheus",
                    evaluated_at=at,
                    sent_at=result.sent_at,
                    status=result.status,
                    http_status=result.http_status,
                    detail=result.detail,
                    body_complete=result.body_complete,
                    raw=result.body,
                    raw_sha256=sha256(result.body),
                )
            )
        return True

    # r3: every attempt is a durable row claimed before its first query; a
    # spent ending (``exhausted``) is filed unavailable without any query.
    attempt = lease.attempt or COMPARISON_MAX_ATTEMPTS
    while not lease.exhausted:
        attempt = lease.attempt or COMPARISON_MAX_ATTEMPTS
        gathered.clear()
        budget_end = time.monotonic() + COMPARISON_ATTEMPT_SECONDS
        try:
            plan: dict[str, str] | None = build_release_comparison_plan(
                context, released_at=at, after_window_end=lease.window_after.end
            )
        except ValueError:
            # Unsupported profile: nothing to query; storage files it unavailable.
            plan = None
        if plan is not None:
            if not collect(plan, budget_end):
                return ReleaseComparisonTaken(None, issued, attempt, not halted, halted)
            full = build_release_comparison_plan(
                context,
                released_at=at,
                after_window_end=lease.window_after.end,
                instance_uids=mapped_old_instances(context, evidence(attempt)),
            )
            if not collect(
                {key: expr for key, expr in full.items() if key not in plan},
                budget_end,
            ):
                return ReleaseComparisonTaken(None, issued, attempt, not halted, halted)
            draft = compare_evidence(
                context,
                observation_id=lease.observation_id,
                ending_id=lease.ending_id,
                sample_id=lease.sample_id,
                window_after=lease.window_after,
                anomaly=ReleaseEvaluation(
                    verdict="unknown",
                    outcome="no_data",
                    evidence_sufficient=False,
                    reason_codes=(),
                    instances=(),
                    signals=(),
                ),
                evidence=evidence(attempt),
                decision_at=at,
            )
            if needs_retry(draft) and attempt < COMPARISON_MAX_ATTEMPTS:
                _log.info(
                    "release comparison retry observation_id=%s attempt=%s requests=%s",
                    lease.observation_id,
                    attempt,
                    issued,
                )
                # The next durable attempt row: a crash from here on still
                # counts it, so the total never exceeds three plans.
                if stopped is not None and stopped():
                    return ReleaseComparisonTaken(None, issued, attempt, False, True)
                lease = store.claim_next_attempt(lease)
                continue
        break
    if stopped is not None and stopped():
        # Stop wins over submission; the claimed attempt stays spent and the
        # ending is taken over under the lease rules.
        return ReleaseComparisonTaken(None, issued, attempt, False, True)
    receipt = store.submit_comparison(lease, evidence(attempt))
    _log.info(
        "release comparison observation_id=%s ending=%s attempts=%s requests=%s status=%s replayed=%s",
        lease.observation_id,
        lease.ending_id,
        attempt,
        issued,
        receipt.comparison.status,
        receipt.replayed,
    )
    return ReleaseComparisonTaken(receipt, issued, attempt, False)
