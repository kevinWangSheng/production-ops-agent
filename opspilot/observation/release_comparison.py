"""Before/after release comparison after an anomaly ending (F11 PR-4a, P4-01=A).

One bounded, deterministic collection per anomaly ending, by the Observer.
The before window is ``[released_at - evaluation_window_seconds,
released_at]`` and every before query is evaluated at ``released_at`` (S1),
never at collection time. Two before scopes are queried:

* ``before_service`` -- the profile's whole-service signals;
* ``before_old`` -- the old version's instance slice, by PR-2's mapping and
  evaluation unchanged, with the before identity (``before_pod_template_hash``
  and ``before.images``) in the "after" slot (S2). Instance ownership and
  liveness are proven by the KSM mapping only; span series supply values
  (S7). The UID set must equal the mapping exactly (S4).

``after_service`` and ``after_new`` are the anomaly sample's own context and
after verdicts, never queried again. The record lists data and evidence
quality only: no verdict, and the release status is never touched (A5).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal, cast
from uuid import UUID, uuid5

from psycopg.types.json import Jsonb
from pydantic import AfterValidator, Field, model_validator

from opspilot.domain.base import DTO, Count
from opspilot.domain.evidence import QueryWindow
from opspilot.observation.release_commands import (
    _release_session_columns,
    release_row,
    release_scope,
    utc_values,
)
from opspilot.observation.release_policy import (
    FormatVersion,
    ReleaseText,
    canonical_json,
    sha256,
)
from opspilot.observation.release_samples import (
    _fingerprint_json,
    _ReleaseSampleOps,
    context_in,
)
from opspilot.observation.release_types import (
    ReleaseEvaluation,
    ReleaseInstance,
    ReleaseQueryResult,
    ReleaseSampleEvidence,
    ReleaseSignalVerdict,
    UtcDatetime,
)
from opspilot.observer.release_evaluation import (
    AFTER_SIGNALS,
    ReleaseEvaluationContext,
    build_release_query_plan,
    evaluate_release_sample,
    query_evidence_consistent,
    supported_profile,
)
from opspilot.persistence.base import Connection, PersistenceError

__all__ = [
    "COMPARISON_ATTEMPT_SECONDS",
    "COMPARISON_MAX_ATTEMPTS",
    "COMPARISON_MAX_QUERIES",
    "ComparisonLease",
    "ComparisonReceipt",
    "ComparisonSignal",
    "ReleaseComparison",
    "ReleaseComparisonEvidence",
    "before_context",
    "before_window",
    "build_release_comparison_plan",
    "comparison_fingerprint",
    "comparison_id_for",
    "compare_evidence",
    "evaluate_release_comparison",
    "mapped_old_instances",
    "needs_retry",
]

# S6: one attempt is the mapping (9), before_service (8 signals x 3) and the
# old slice (4 after signals x 4) -- 49 queries; at most 3 attempts.
COMPARISON_MAX_ATTEMPTS = 3
COMPARISON_MAX_QUERIES = 49
# Wall-clock budget of one attempt; queries past it are filed as timeouts
# without being sent, so a hung source cannot hold the Observer loop.
COMPARISON_ATTEMPT_SECONDS = 120

ComparisonScope = Literal["before_service", "before_old", "after_service", "after_new"]
SCOPES: tuple[ComparisonScope, ...] = (
    "before_service",
    "before_old",
    "after_service",
    "after_new",
)
# PR-2 evaluation scopes, read through the before identity / the anomaly.
_BEFORE_SCOPES = {"context": "before_service", "after": "before_old"}
_AFTER_SCOPES = {"context": "after_service", "after": "after_new"}
# Evidence-quality codes are kept; verdict codes (DEGRADED_SIGNAL,
# CONTEXT_DEGRADED_ONLY, INSUFFICIENT_TRAFFIC) and the authorization fence
# are conclusions or sampling rules, not comparison facts.
_QUALITY_PREFIXES = (
    "VERSION_",
    "QUERY_FAILED:",
    "QUERY_TIMEOUT:",
    "STALE_TELEMETRY:",
    "INSUFFICIENT_SAMPLES:",
    "REQUIRED_TELEMETRY_MISSING:",
    "HEALTH_PROFILE_INVALID",
)
EVIDENCE_INCONSISTENT = "QUERY_EVIDENCE_INCONSISTENT"


class ComparisonSignal(DTO):
    scope: ComparisonScope
    signal_name: ReleaseText
    # A value only when the signal had fresh, sufficient evidence; otherwise
    # None and the reason codes say why.
    value: float | None = Field(allow_inf_nan=False)
    sample_count: Count | None
    latest_sample_at: UtcDatetime | None
    query_keys: tuple[ReleaseText, ...]
    reason_codes: tuple[ReleaseText, ...]


class ReleaseComparison(DTO):
    """Identity, attempts and the after window are None only when the pure
    public evaluator is called without a stored ending; a stored record
    always carries them (DB CHECK on the jsonb copy)."""

    format_version: FormatVersion
    comparison_id: UUID | None
    observation_id: UUID
    ending_id: UUID | None
    sample_id: UUID | None
    status: Literal["complete", "partial", "unavailable"]
    attempts: Annotated[int, Field(ge=1, le=COMPARISON_MAX_ATTEMPTS)] | None
    window_before: QueryWindow
    window_after: QueryWindow | None
    before_instances: tuple[ReleaseInstance, ...]
    after_instances: tuple[ReleaseInstance, ...]
    signals: tuple[ComparisonSignal, ...]
    reason_codes: tuple[ReleaseText, ...]


class ComparisonLease(DTO):
    """A pending comparison: one anomaly ending without a comparison record."""

    comparison_id: UUID
    observation_id: UUID
    ending_id: UUID
    session_id: UUID
    sample_id: UUID
    anomaly_key: ReleaseText
    released_at: UtcDatetime
    window_after: QueryWindow
    global_suspension_generation: Count
    target_suspension_generation: Count

    @model_validator(mode="after")
    def derived_identity(self) -> ComparisonLease:
        if self.comparison_id != comparison_id_for(self.observation_id, self.ending_id):
            raise ValueError("COMPARISON_IDENTITY_MISMATCH")
        return self


class ReleaseComparisonEvidence(DTO):
    format_version: FormatVersion
    comparison_id: UUID
    attempts: Annotated[int, Field(ge=1, le=COMPARISON_MAX_ATTEMPTS)]
    window: Annotated[
        QueryWindow,
        AfterValidator(
            lambda value: QueryWindow(
                start=value.start.astimezone(timezone.utc),
                end=value.end.astimezone(timezone.utc),
            )
        ),
    ]
    queries: tuple[ReleaseQueryResult, ...] = Field(max_length=COMPARISON_MAX_QUERIES)

    @model_validator(mode="after")
    def unique_queries(self) -> ReleaseComparisonEvidence:
        if len({q.query_key for q in self.queries}) != len(self.queries):
            raise ValueError("DUPLICATE_QUERY_KEY")
        return self


class ComparisonReceipt(DTO):
    comparison_id: UUID
    observation_id: UUID
    ending_id: UUID
    replayed: bool
    comparison: ReleaseComparison


def comparison_id_for(observation_id: UUID, ending_id: UUID) -> UUID:
    return uuid5(observation_id, "comparison:" + str(ending_id))


def before_window(context: ReleaseEvaluationContext) -> QueryWindow:
    end = context.release.released_at.astimezone(timezone.utc)
    return QueryWindow(
        start=end - timedelta(seconds=context.health_profile.evaluation_window_seconds),
        end=end,
    )


def before_context(context: ReleaseEvaluationContext) -> ReleaseEvaluationContext:
    """PR-2's evaluation context with the before identity in the after slot.

    Equal pod-template hashes name the same ReplicaSet: both revisions become
    the before revision so PR-2's ``version_identity`` reports
    ``VERSION_SLICE_NOT_DISTINGUISHABLE`` (S3). The authorization fence does
    not apply to a window that precedes the release by construction.
    """
    release = context.release
    aux = release.auxiliary
    if (
        aux.before_pod_template_hash is not None
        and aux.before_pod_template_hash == aux.after_pod_template_hash
    ):
        swapped = release.model_copy(
            update={
                "after_revision": release.before_revision,
                "auxiliary": aux.model_copy(
                    update={"after_pod_template_hash": aux.before_pod_template_hash}
                ),
            }
        )
    else:
        swapped = release.model_copy(
            update={
                "before_revision": release.after_revision,
                "after_revision": release.before_revision,
                "auxiliary": aux.model_copy(
                    update={
                        "before_pod_template_hash": aux.after_pod_template_hash,
                        "after_pod_template_hash": aux.before_pod_template_hash,
                    }
                ),
            }
        )
    return context.model_copy(
        update={"release": swapped, "authorized_at": before_window(context).start}
    )


# Stored keys name the comparison scope; PR-2 keys feed PR-2's evaluator.
_KEY_PREFIXES = (
    ("before_old:mapping:", "mapping:"),
    ("before_service:", "context:"),
    ("before_old:", "after:"),
)


def _to_pr2_key(key: str) -> str:
    for stored, pr2 in _KEY_PREFIXES:
        if key.startswith(stored):
            return pr2 + key[len(stored) :]
    return key


def _to_stored_key(key: str) -> str:
    for stored, pr2 in _KEY_PREFIXES:
        if key.startswith(pr2):
            return stored + key[len(pr2) :]
    return key


def build_release_comparison_plan(
    context: ReleaseEvaluationContext,
    *,
    released_at: datetime,
    after_window_end: datetime,
    instance_uids: Sequence[UUID] = (),
) -> dict[str, str]:
    """Mapping and before_service first; a proven old UID set adds the slice.

    Contract §4 signature. PromQL instant queries carry no time: the caller
    evaluates every query at ``released_at`` (S1) and the after scopes come
    from the anomaly sample ending at ``after_window_end``, so neither
    changes the text. ``instance_uids`` (the old UIDs the mapping results
    prove, see ``mapped_old_instances``) adds the 16 slice queries.
    Raises ``ValueError("HEALTH_PROFILE_INVALID")`` like PR-2's plan.
    """
    del released_at, after_window_end
    plan = build_release_query_plan(
        before_context(context),
        window_end=before_window(context).end,
        instance_uids=instance_uids,
    )
    return {_to_stored_key(key): expr for key, expr in plan.items()}


def _pr2_evidence(evidence: ReleaseComparisonEvidence) -> ReleaseSampleEvidence:
    return ReleaseSampleEvidence(
        format_version=1,
        sample_id=evidence.comparison_id,
        window=evidence.window,
        queries=tuple(
            q.model_copy(update={"query_key": _to_pr2_key(q.query_key)})
            for q in evidence.queries
        ),
    )


def mapped_old_instances(
    context: ReleaseEvaluationContext, evidence: ReleaseComparisonEvidence
) -> tuple[UUID, ...]:
    """The old UID set the mapping queries prove, for the slice queries."""
    evaluation = evaluate_release_sample(
        before_context(context),
        _pr2_evidence(evidence),
        decision_at=evidence.window.end,
    )
    return tuple(instance.pod_uid for instance in evaluation.instances)


def _code(code: str) -> str | None:
    """A PR-2 reason code in comparison terms, or None when not a fact."""
    if not code.startswith(_QUALITY_PREFIXES):
        return None
    if code.startswith("VERSION_"):
        return code + ":before_old"
    head, _, rest = code.partition(":")
    if head in ("QUERY_FAILED", "QUERY_TIMEOUT"):
        return f"{head}:{_to_stored_key(rest)}"
    if rest:
        scope, _, signal = rest.partition(":")
        return f"{head}:{_BEFORE_SCOPES.get(scope, scope)}:{signal}"
    return code


def _signal(
    scope: ComparisonScope,
    name: str,
    verdict: ReleaseSignalVerdict | None,
    query_keys: tuple[str, ...],
    reasons: tuple[str, ...],
) -> ComparisonSignal:
    usable = verdict is not None and verdict.verdict in ("healthy", "degraded")
    return ComparisonSignal(
        scope=scope,
        signal_name=name,
        value=verdict.value if usable and verdict is not None else None,
        sample_count=verdict.sample_count if verdict is not None else None,
        latest_sample_at=verdict.latest_sample_at if verdict is not None else None,
        query_keys=query_keys,
        reason_codes=reasons,
    )


def _scoped(codes: Sequence[str], scope: str, name: str) -> tuple[str, ...]:
    own = []
    for code in codes:
        head, _, rest = code.partition(":")
        if (
            rest == f"{scope}:{name}"
            or rest.startswith(f"{scope}:{name}:")
            or (scope == "before_old" and code.startswith("VERSION_"))
            or (
                scope == "before_old"
                and head in ("QUERY_FAILED", "QUERY_TIMEOUT")
                and rest.startswith("before_old:mapping:")
            )
        ):
            own.append(code)
    return tuple(sorted(own))


def compare_evidence(
    context: ReleaseEvaluationContext,
    *,
    observation_id: UUID,
    ending_id: UUID | None,
    sample_id: UUID | None,
    window_after: QueryWindow | None,
    anomaly: ReleaseEvaluation,
    evidence: ReleaseComparisonEvidence,
    decision_at: datetime,
    attempts: int | None = None,
) -> ReleaseComparison:
    """Pure: the stored before evidence and the anomaly sample's verdicts.

    Evidence that does not reproduce the plan (keys, text, evaluation time or
    window) is ``unavailable`` with ``QUERY_EVIDENCE_INCONSISTENT``; storage
    keeps the rows and replay recomputes the same answer.
    """
    profile = context.health_profile
    names = [signal.name for signal in profile.signals]
    old = before_context(context)
    window = before_window(context)
    pr2 = _pr2_evidence(evidence)
    codes: set[str] = set()
    before: dict[tuple[str, str], ReleaseSignalVerdict] = {}
    instances: tuple[ReleaseInstance, ...] = ()
    profile_valid = supported_profile(profile)
    if not profile_valid:
        codes.add("HEALTH_PROFILE_INVALID")
    elif evidence.window != window or not query_evidence_consistent(
        old, pr2, decision_at
    ):
        codes.add(EVIDENCE_INCONSISTENT)
    else:
        evaluation = evaluate_release_sample(old, pr2, decision_at=decision_at)
        instances = evaluation.instances
        for code in evaluation.reason_codes:
            mapped = _code(code)
            if mapped is not None:
                codes.add(mapped)
        for verdict in evaluation.signals:
            before[(_BEFORE_SCOPES[verdict.scope], verdict.signal_name)] = verdict
    after = {
        (_AFTER_SCOPES[v.scope], v.signal_name): v
        for v in anomaly.signals
        if v.scope in _AFTER_SCOPES
    }
    ordered = sorted(codes)
    signals: list[ComparisonSignal] = []
    for scope in SCOPES:
        for name in names:
            if scope in ("before_old", "after_new") and name not in AFTER_SIGNALS:
                continue
            found = (before if scope.startswith("before") else after).get((scope, name))
            keys = found.query_keys if found else ()
            if scope.startswith("before"):
                signals.append(
                    _signal(
                        scope,
                        name,
                        found,
                        tuple(_to_stored_key(key) for key in keys),
                        _scoped(ordered, scope, name),
                    )
                )
            else:
                signals.append(_signal(scope, name, found, keys, ()))
    befores = [s for s in signals if s.scope.startswith("before")]
    if not any(s.value is not None for s in befores):
        status: Literal["complete", "partial", "unavailable"] = "unavailable"
    elif all(s.value is not None for s in befores) and not ordered:
        status = "complete"
    else:
        status = "partial"
    return ReleaseComparison(
        format_version=1,
        comparison_id=evidence.comparison_id,
        observation_id=observation_id,
        ending_id=ending_id,
        sample_id=sample_id,
        status=status,
        attempts=evidence.attempts if attempts is None else attempts,
        window_before=window,
        window_after=window_after,
        before_instances=instances,
        after_instances=anomaly.instances,
        signals=tuple(signals),
        reason_codes=tuple(ordered),
    )


_NO_ANOMALY = ReleaseEvaluation(
    verdict="unknown",
    outcome="no_data",
    evidence_sufficient=False,
    reason_codes=(),
    instances=(),
    signals=(),
)


def evaluate_release_comparison(
    context: ReleaseEvaluationContext,
    plan: Mapping[str, str],
    results: Mapping[str, ReleaseQueryResult] | Sequence[ReleaseQueryResult],
) -> ReleaseComparison:
    """Contract §4: the comparison from raw results alone, without I/O.

    ``results`` holds the before-window results under the plan's keys
    (``before_service:*``, ``before_old:*``) and, optionally, the anomaly
    sample's own rows under PR-2's keys; the after scopes are evaluated from
    those rows with PR-2's evaluator at their ``evaluated_at``. A ``plan``
    other than this context's mapping plan, or results not matching the plan
    (keys, text, evaluation time), make the comparison ``unavailable`` with
    ``QUERY_EVIDENCE_INCONSISTENT``. Identity, attempts and the after window
    are None: only the store knows the ending.
    """
    rows = list(results.values()) if isinstance(results, Mapping) else list(results)
    before = tuple(
        q for q in rows if q.query_key.startswith(("before_service:", "before_old:"))
    )
    after = tuple(q for q in rows if q not in before)
    window = before_window(context)
    anomaly = _NO_ANOMALY
    window_after: QueryWindow | None = None
    ends = {q.evaluated_at for q in after}
    if len(ends) == 1:
        (end,) = ends
        window_after = QueryWindow(
            start=end
            - timedelta(seconds=context.health_profile.evaluation_window_seconds),
            end=end,
        )
        anomaly = evaluate_release_sample(
            context,
            ReleaseSampleEvidence(
                format_version=1,
                sample_id=uuid5(context.release.observation_id, "after:" + str(end)),
                window=window_after,
                queries=after,
            ),
            decision_at=end,
        )
    evidence = ReleaseComparisonEvidence(
        format_version=1,
        comparison_id=uuid5(context.release.observation_id, "pure-comparison"),
        attempts=1,
        window=window,
        queries=before,
    )
    comparison = compare_evidence(
        context,
        observation_id=context.release.observation_id,
        ending_id=None,
        sample_id=None,
        window_after=window_after,
        anomaly=anomaly,
        evidence=evidence,
        decision_at=window.end,
    )
    try:
        expected = build_release_comparison_plan(
            context, released_at=window.end, after_window_end=window.end
        )
    except ValueError:
        expected = {}
    if dict(plan) != expected or len(ends) > 1:
        comparison = comparison.model_copy(
            update={
                "status": "unavailable",
                "reason_codes": tuple(
                    sorted({*comparison.reason_codes, EVIDENCE_INCONSISTENT})
                ),
                "signals": tuple(
                    signal.model_copy(update={"value": None})
                    for signal in comparison.signals
                ),
            }
        )
    return comparison.model_copy(update={"comparison_id": None, "attempts": None})


def needs_retry(comparison: ReleaseComparison) -> bool:
    """Only transport failures are worth another attempt."""
    return any(
        code.startswith(("QUERY_FAILED:", "QUERY_TIMEOUT:"))
        for code in comparison.reason_codes
    )


def comparison_fingerprint(evidence: ReleaseComparisonEvidence) -> str:
    data = evidence.model_dump(exclude={"queries"})
    data["queries"] = [
        {**q.model_dump(exclude={"raw"}), "raw": q.raw.hex()}
        for q in sorted(evidence.queries, key=lambda q: q.query_key)
    ]
    return sha256(canonical_json(_fingerprint_json(data)).encode())


_COMPARISON_COLUMNS = "comparison_id,observation_id,ending_id,session_id,sample_id,format_version,status,attempts,window_before_start,window_before_end,window_after_start,window_after_end,decision_at,evidence_sha256,comparison,recorded_at"
_QUERY_COLUMNS = "comparison_id,query_key,query,source,evaluated_at,sent_at,status,http_status,detail,body_complete,raw,raw_sha256"


class _ReleaseComparisonOps(_ReleaseSampleOps):
    def pending_comparisons(self, *, limit: int = 1) -> tuple[ComparisonLease, ...]:
        """Anomaly endings without a comparison, outside suspended scopes."""
        if type(limit) is not int or limit <= 0:
            raise PersistenceError("INVALID_INPUT")
        with self._release_transaction(snapshot=True) as conn:
            rows = conn.execute(
                "SELECT e.ending_id,e.session_id,e.release_observation_id AS observation_id,e.sample_id,e.anomaly_key,r.released_at,m.window_start,m.window_end,g.global_generation,t.generation AS target_generation FROM opspilot_observation_endings e JOIN opspilot_release_observations r ON r.observation_id=e.release_observation_id JOIN opspilot_observation_samples m ON m.sample_id=e.sample_id JOIN opspilot_scope_controls g ON g.scope_id=1 JOIN opspilot_target_suspensions t ON t.target_id=r.target_id WHERE r.status='anomalous' AND e.anomaly_key IS NOT NULL AND NOT g.global_suspended AND NOT t.suspended AND NOT EXISTS(SELECT 1 FROM opspilot_release_comparisons c WHERE c.ending_id=e.ending_id) ORDER BY e.recorded_at,e.ending_id LIMIT %s",
                (limit,),
            ).fetchall()
        return tuple(
            ComparisonLease(
                comparison_id=comparison_id_for(r["observation_id"], r["ending_id"]),
                observation_id=r["observation_id"],
                ending_id=r["ending_id"],
                session_id=r["session_id"],
                sample_id=r["sample_id"],
                anomaly_key=r["anomaly_key"],
                released_at=r["released_at"],
                window_after=QueryWindow(start=r["window_start"], end=r["window_end"]),
                global_suspension_generation=r["global_generation"],
                target_suspension_generation=r["target_generation"],
            )
            for r in utc_values(rows)
        )

    def comparison_scope_current(self, lease: ComparisonLease) -> bool:
        """No query may run once the release's scope is suspended (C3 F7)."""
        with self._release_transaction(snapshot=True) as conn:
            release = release_row(conn, lease.observation_id)
            scope = release_scope(conn, release["target_id"])
        return bool(
            not scope["global_suspended"]
            and not scope["target_suspended"]
            and scope["global_generation"] == lease.global_suspension_generation
            and scope["target_generation"] == lease.target_suspension_generation
        )

    def comparison_context(self, lease: ComparisonLease) -> ReleaseEvaluationContext:
        context = self.release_context(lease.session_id)
        if context.release.observation_id != lease.observation_id:
            raise PersistenceError("INVALID_INPUT")
        return context

    def submit_comparison(
        self, lease: ComparisonLease, evidence: ReleaseComparisonEvidence
    ) -> ComparisonReceipt:
        """Storage evaluates the raw evidence; the first record is kept.

        A second submission for the same ending -- a retry, or a concurrent
        Observer -- returns the stored record with ``replayed=True``.
        """
        if not isinstance(lease, ComparisonLease) or not isinstance(
            evidence, ReleaseComparisonEvidence
        ):
            raise PersistenceError("INVALID_INPUT")
        try:
            lease = ComparisonLease.model_validate(lease.model_dump())
            evidence = ReleaseComparisonEvidence.model_validate(evidence.model_dump())
        except ValueError as exc:
            raise PersistenceError("INVALID_INPUT") from exc
        if evidence.comparison_id != lease.comparison_id:
            raise PersistenceError("INVALID_INPUT")
        with self._release_transaction() as conn:
            stored = self._stored_comparison(conn, lease.comparison_id)
            if stored is not None:
                return stored
            ending = conn.execute(
                "SELECT ending_id,session_id,release_observation_id,sample_id,anomaly_key FROM opspilot_observation_endings WHERE ending_id=%s",
                (lease.ending_id,),
            ).fetchone()
            if (
                ending is None
                or ending["release_observation_id"] != lease.observation_id
                or ending["session_id"] != lease.session_id
                or ending["sample_id"] != lease.sample_id
                or ending["anomaly_key"] != lease.anomaly_key
            ):
                raise PersistenceError("INVALID_INPUT")
            release = release_row(conn, lease.observation_id)
            if release["status"] != "anomalous":
                raise PersistenceError("INVALID_INPUT")
            scope = release_scope(conn, release["target_id"])
            if (
                scope["global_suspended"]
                or scope["target_suspended"]
                or scope["global_generation"] != lease.global_suspension_generation
                or scope["target_generation"] != lease.target_suspension_generation
            ):
                raise PersistenceError("SCOPE_CHANGED")
            session = utc_values(
                conn.execute(
                    "SELECT "
                    + _release_session_columns()
                    + " FROM opspilot_observation_sessions WHERE session_id=%s",
                    (lease.session_id,),
                ).fetchone()
            )
            sample = utc_values(
                conn.execute(
                    "SELECT window_start,window_end,release_evaluation FROM opspilot_observation_samples WHERE sample_id=%s",
                    (lease.sample_id,),
                ).fetchone()
            )
            if session is None or sample is None:
                raise PersistenceError("INVALID_INPUT")
            window_after = QueryWindow(
                start=sample["window_start"], end=sample["window_end"]
            )
            if window_after != lease.window_after:
                raise PersistenceError("INVALID_INPUT")
            context = context_in(conn, session, release)
            now = self._db_now(conn).astimezone(timezone.utc)
            comparison = compare_evidence(
                context,
                observation_id=lease.observation_id,
                ending_id=lease.ending_id,
                sample_id=lease.sample_id,
                window_after=window_after,
                anomaly=ReleaseEvaluation.model_validate_json(
                    canonical_json(sample["release_evaluation"])
                ),
                evidence=evidence,
                decision_at=now,
            )
            inserted = conn.execute(
                "INSERT INTO opspilot_release_comparisons(comparison_id,observation_id,ending_id,session_id,sample_id,format_version,status,attempts,window_before_start,window_before_end,window_after_start,window_after_end,decision_at,evidence_sha256,comparison) VALUES(%s,%s,%s,%s,%s,1,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING comparison_id",
                (
                    lease.comparison_id,
                    lease.observation_id,
                    lease.ending_id,
                    lease.session_id,
                    lease.sample_id,
                    comparison.status,
                    evidence.attempts,
                    evidence.window.start,
                    evidence.window.end,
                    window_after.start,
                    window_after.end,
                    now,
                    comparison_fingerprint(evidence),
                    Jsonb(comparison.model_dump(mode="json")),
                ),
            ).fetchone()
            if inserted is None:
                # A concurrent submission committed first; it is the record.
                stored = self._stored_comparison(conn, lease.comparison_id)
                if stored is None:
                    raise PersistenceError("INTEGRITY_REFUSED")
                return stored
            for q in evidence.queries:
                data = q.model_dump()
                fields = ["comparison_id", *data]
                conn.execute(
                    "INSERT INTO opspilot_release_comparison_queries("
                    + ",".join(fields)
                    + ") VALUES("
                    + ",".join(["%s"] * len(fields))
                    + ")",
                    [lease.comparison_id, *data.values()],
                )
            return ComparisonReceipt(
                comparison_id=lease.comparison_id,
                observation_id=lease.observation_id,
                ending_id=lease.ending_id,
                replayed=False,
                comparison=comparison,
            )

    @staticmethod
    def _stored_comparison(
        conn: Connection, comparison_id: UUID
    ) -> ComparisonReceipt | None:
        row = conn.execute(
            "SELECT comparison_id,observation_id,ending_id,comparison FROM opspilot_release_comparisons WHERE comparison_id=%s",
            (comparison_id,),
        ).fetchone()
        if row is None:
            return None
        comparison = ReleaseComparison.model_validate_json(
            canonical_json(row["comparison"])
        )
        return ComparisonReceipt(
            comparison_id=row["comparison_id"],
            observation_id=row["observation_id"],
            ending_id=row["ending_id"],
            replayed=True,
            comparison=comparison,
        )

    def _release_comparisons_in(
        self, conn: Connection, observation_id: UUID
    ) -> list[dict[str, Any]]:
        comparisons = conn.execute(
            "SELECT "
            + _COMPARISON_COLUMNS
            + " FROM opspilot_release_comparisons WHERE observation_id=%s ORDER BY recorded_at,comparison_id",
            (observation_id,),
        ).fetchall()
        for comparison in comparisons:
            comparison["queries"] = conn.execute(
                "SELECT "
                + _QUERY_COLUMNS
                + ' FROM opspilot_release_comparison_queries WHERE comparison_id=%s ORDER BY query_key COLLATE "C"',
                (comparison["comparison_id"],),
            ).fetchall()
        return cast(list[dict[str, Any]], utc_values(comparisons))

    def release_comparisons(self, observation_id: UUID) -> list[dict[str, Any]]:
        """Stored comparison rows with their raw queries, for replay.

        Kept out of ``release_history`` (PR-2's fixed shape); the session
        history that ``replay_session`` reads carries them as
        ``comparisons``."""
        if not isinstance(observation_id, UUID):
            raise PersistenceError("INVALID_INPUT")
        with self._release_transaction(snapshot=True) as conn:
            return self._release_comparisons_in(conn, observation_id)


def iter_comparison_queries(rows: Sequence[Mapping[str, Any]]) -> Iterator[Any]:
    """Stored query rows as DTOs; raw bytes re-hashed by the DTO validator."""
    for row in rows:
        data = {k: v for k, v in row.items() if k != "comparison_id"}
        raw = data["raw"]
        data["raw"] = raw if isinstance(raw, bytes) else bytes(raw)
        yield ReleaseQueryResult.model_validate(data)
