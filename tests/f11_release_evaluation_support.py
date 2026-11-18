"""Independent contract tests for F11 PR-2a: the pure release-sample
evaluator, version-mapping sufficiency and the release evidence DTOs.

Written from ``docs/tasks/2026-10-13-f11-pr2-contract.md`` 「接口定稿（r1）」
(公开接口 3/4, 固定语义 4–7) without reading the implementation. Inputs are
raw Prometheus responses built by ``tests.f11_release_support``; only the
public ``evaluate_release_sample`` and DTOs are exercised. No database.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from opspilot.observation.release_policy import ReleasePolicy
from opspilot.observation.release_types import (
    ReleaseEvaluation,
    ReleaseQueryResult,
    ReleaseSampleEvidence,
)
from opspilot.observation.releases import (
    ReleaseAuxiliary,
    ReleaseRecord,
    ReleaseRevision,
)
from opspilot.observer.release_evaluation import (
    ReleaseEvaluationContext,
    evaluate_release_sample,
)
from tests.f11_release_support import (
    AFTER_HASH,
    BEFORE_HASH,
    NAMESPACE,
    PROFILE,
    PROFILE_REVISION,
    SERVICE,
    WINDOW,
    EvidenceBuilder,
    Pod,
    canonical_json,
    digest,
    pods_for,
    policy_content,
    policy_entry,
    sha256_hex,
    texts_if_bound,
    utc,
)

RELEASED_AT = utc(2026, 10, 13, 10, 0, 0)
AUTHORIZED_AT = RELEASED_AT + timedelta(seconds=500)
WINDOW_END = AUTHORIZED_AT + WINDOW  # T - W == A: the first fully-after window
DECISION_AT = WINDOW_END + timedelta(seconds=2)
AFTER_IMAGES = {"checkout": digest("checkout-after")}
BEFORE_IMAGES = {"checkout": digest("checkout-before")}
IDENTITY = {
    "integration_id": "test-integration",
    "cluster_uid": "test-cluster",
    "namespace": NAMESPACE,
    "resource_uid": "deployment/checkout-eval",
    "workload": SERVICE,
}


# --- context construction ------------------------------------------------


def _record(
    *,
    before_images: Mapping[str, str] = BEFORE_IMAGES,
    after_images: Mapping[str, str] = AFTER_IMAGES,
    auxiliary: ReleaseAuxiliary | None = None,
    policy: ReleasePolicy,
) -> ReleaseRecord:
    session = PROFILE.session
    return ReleaseRecord(
        observation_id=uuid4(),
        format_version=1,
        source="argo-rollouts",
        source_release_id=f"rel-{uuid4().hex}",
        target_id=uuid4(),
        target_snapshot=dict(IDENTITY),
        before_revision=ReleaseRevision(
            images=dict(before_images), pod_template_digest=digest("tmpl-before")
        ),
        after_revision=ReleaseRevision(
            images=dict(after_images), pod_template_digest=digest("tmpl-after")
        ),
        auxiliary=auxiliary
        or ReleaseAuxiliary(
            before_pod_template_hash=BEFORE_HASH, after_pod_template_hash=AFTER_HASH
        ),
        semantic_sha256=sha256_hex("semantic"),
        health_profile_revision=PROFILE_REVISION,
        timing_policy_version=1,
        released_at=RELEASED_AT,
        received_at=RELEASED_AT + timedelta(seconds=1),
        deadline_at=RELEASED_AT + timedelta(seconds=session.deadline_seconds),
        deadline_seconds=session.deadline_seconds,
        min_tracking_seconds=policy.content.min_tracking_seconds,
        sample_interval_seconds=session.sample_interval_seconds,
        sustained_window_seconds=session.sustained_window_seconds,
        max_samples=session.max_samples,
        status="observing",
        control_generation=0,
        observation_generation=1,
        paused=False,
        ended_reason=None,
        ended_at=None,
        incident_id=None,
        supersedes_observation_id=None,
        created_at=RELEASED_AT + timedelta(seconds=1),
        updated_at=AUTHORIZED_AT,
        used_sample_count=0,
        calibrated_policy_revision=policy.revision,
        calibrated_policy_content=canonical_json(
            policy.content.model_dump(mode="json")
        ),
        calibrated_policy_sha256=policy.content_sha256,
    )


def _policy() -> ReleasePolicy:
    # strict DTO (公开接口 r1 通用约定); the catalog is JSON-loaded, so validate JSON
    return ReleasePolicy.model_validate_json(
        json.dumps(policy_entry(policy_content(IDENTITY, min_tracking_seconds=600)))
    )


def _context(
    *, authorized_at: datetime = AUTHORIZED_AT, **record: Any
) -> ReleaseEvaluationContext:
    policy = _policy()
    return ReleaseEvaluationContext(
        release=_record(policy=policy, **record),
        session_id=uuid4(),
        authorized_at=authorized_at,
        policy=policy,
        health_profile=PROFILE,
    )


def _builder(
    context: ReleaseEvaluationContext,
    pods: tuple[Pod, ...],
    *,
    window_end: datetime = WINDOW_END,
    **kwargs: Any,
) -> EvidenceBuilder:
    texts = texts_if_bound(context, window_end, [p.uid for p in pods])
    return EvidenceBuilder(window_end=window_end, texts=texts, **kwargs)


def _evaluate(
    context: ReleaseEvaluationContext,
    builder: EvidenceBuilder,
    *,
    decision_at: datetime | None = None,
) -> ReleaseEvaluation:
    decision = decision_at or builder.window_end + timedelta(seconds=2)
    return evaluate_release_sample(context, builder.evidence(), decision_at=decision)


def _sufficient(
    *,
    context: ReleaseEvaluationContext | None = None,
    pods: tuple[Pod, ...] | None = None,
    ctx_values: Mapping[str, float] | None = None,
    after_values: Mapping[str, float] | None = None,
    **builder_kwargs: Any,
) -> tuple[ReleaseEvaluationContext, tuple[Pod, ...], EvidenceBuilder]:
    context = context or _context()
    pods = pods or pods_for(AFTER_IMAGES)
    builder = _builder(context, pods, **builder_kwargs).sufficient(
        pods, context=ctx_values, after=after_values
    )
    return context, pods, builder


def _assert_unknown(
    evaluation: ReleaseEvaluation, *codes: str, outcome: str | None = None
) -> None:
    assert evaluation.verdict == "unknown"
    assert evaluation.evidence_sufficient is False or "CONTEXT_DEGRADED_ONLY" in codes
    for code in codes:
        assert code in evaluation.reason_codes, (code, evaluation.reason_codes)
    if outcome is not None:
        assert evaluation.outcome == outcome
    assert evaluation.outcome not in ("healthy", "degraded")
    _assert_codes_sorted(evaluation)


def _assert_codes_sorted(evaluation: ReleaseEvaluation) -> None:
    codes = list(evaluation.reason_codes)
    assert codes == sorted(set(codes)), "reason codes: deduplicated, sorted"


def _has_prefix(evaluation: ReleaseEvaluation, *prefixes: str) -> bool:
    return any(code.startswith(prefixes) for code in evaluation.reason_codes)


def _verdict(evaluation: ReleaseEvaluation, scope: str, signal: str) -> Any:
    matches = [
        s for s in evaluation.signals if s.scope == scope and s.signal_name == signal
    ]
    assert len(matches) == 1, (scope, signal, evaluation.signals)
    return matches[0]


# --- the verdict table (固定语义 7) -----------------------------------------


# --- authorization window (固定语义 4) --------------------------------------


# --- version identity (固定语义 5) ------------------------------------------


# --- mapping sufficiency rules 1–7 (固定语义 5) -----------------------------


def _with_images(
    pods: tuple[Pod, ...], image_ids: Mapping[str, str]
) -> tuple[Pod, ...]:
    first = Pod(
        name=pods[0].name,
        uid=pods[0].uid,
        replicaset=pods[0].replicaset,
        image_ids=dict(image_ids),
    )
    return (first, *pods[1:])


# --- telemetry sufficiency and SampleOutcome severity (固定语义 6/7) --------


# --- evidence DTOs (公开接口 3) ---------------------------------------------


def _query(
    key: str = "context:error_ratio:query", raw: bytes = b"{}", **kw: Any
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "query_key": key,
        "query": "q",
        "source": "prometheus",
        "evaluated_at": WINDOW_END,
        "sent_at": WINDOW_END,
        "status": "ok",
        "http_status": 200,
        "detail": "",
        "body_complete": True,
        "raw": raw,
        "raw_sha256": sha256_hex(raw),
    }
    fields.update(kw)
    return fields


def _evidence(queries: list[ReleaseQueryResult]) -> ReleaseSampleEvidence:
    return ReleaseSampleEvidence.model_validate(
        {
            "format_version": 1,
            "sample_id": uuid4(),
            "window": {"start": WINDOW_END - WINDOW, "end": WINDOW_END},
            "queries": tuple(queries),
        }
    )


# --- query plan (固定语义 5/6; through the seam, see support module) --------


def _plan(uids: tuple[UUID, ...]) -> dict[str, str]:
    from tests.f11_release_support import planned_texts

    return planned_texts(_context(), WINDOW_END, uids)


_UID_MATCHER = re.compile(r'service_instance_id=~"((?:[^"\\]|\\.)*)"')


def _unescape(text: str) -> str:
    return re.sub(r"\\(.)", r"\1", text)


def _compact(text: str) -> str:
    return "".join(text.split()).replace("'", '"')
