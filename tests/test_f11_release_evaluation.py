"""Independent contract tests for F11 PR-2a: the pure release-sample
evaluator, version-mapping sufficiency and the release evidence DTOs.

Written from ``docs/tasks/2026-10-13-f11-pr2-contract.md`` 「接口定稿（r1）」
(公开接口 3/4, 固定语义 4–7) without reading the implementation. Inputs are
raw Prometheus responses built by ``tests.f11_release_support``; only the
public ``evaluate_release_sample`` and DTOs are exercised. No database.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

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
    ALL_KEYS,
    BEFORE_HASH,
    CONTEXT_SIGNALS,
    DEGRADED_AFTER,
    FRESHNESS,
    MAPPING_MINIMUM,
    MAX_PODS,
    MAX_QUERIES,
    NAMESPACE,
    PROFILE,
    PROFILE_REVISION,
    RAW_LIMIT,
    REPLICASET,
    SERVICE,
    SLICE_SIGNALS,
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
    vector,
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
    return ReleasePolicy.model_validate(
        policy_entry(policy_content(IDENTITY, min_tracking_seconds=600))
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


def test_sufficient_healthy_evidence_is_healthy_with_no_reason_codes() -> None:
    context, pods, builder = _sufficient()

    evaluation = _evaluate(context, builder)

    assert evaluation.verdict == "healthy"
    assert evaluation.outcome == "healthy"
    assert evaluation.evidence_sufficient is True
    assert evaluation.reason_codes == ()
    # the mapped instance set: exactly the pods, each with its full image map
    assert {
        (i.pod_uid, i.pod_name, i.replicaset_name, frozenset(i.images.items()))
        for i in evaluation.instances
    } == {(p.uid, p.name, REPLICASET, frozenset(AFTER_IMAGES.items())) for p in pods}
    for name in CONTEXT_SIGNALS:
        assert _verdict(evaluation, "context", name).verdict == "healthy"
    for name in SLICE_SIGNALS:
        assert _verdict(evaluation, "after", name).verdict == "healthy"
    assert _verdict(evaluation, "after", "error_ratio").value == 0.0
    # pure: the same inputs give the same evaluation
    assert _evaluate(context, builder) == evaluation


@pytest.mark.parametrize(("signal", "value"), sorted(DEGRADED_AFTER.items()))
def test_one_after_signal_over_its_threshold_is_degraded(
    signal: str, value: float
) -> None:
    context, _, builder = _sufficient(after_values={signal: value})

    evaluation = _evaluate(context, builder)

    assert evaluation.verdict == "degraded"
    assert evaluation.outcome == "degraded"
    assert evaluation.evidence_sufficient is True
    assert f"DEGRADED_SIGNAL:after:{signal}" in evaluation.reason_codes
    assert _verdict(evaluation, "after", signal).verdict == "degraded"
    assert _verdict(evaluation, "after", signal).value == value
    _assert_codes_sorted(evaluation)


def test_after_degradation_wins_over_context_degradation() -> None:
    context, _, builder = _sufficient(
        ctx_values={"deployment_ready_replicas": 0.0},
        after_values={"error_ratio": 0.5},
    )

    evaluation = _evaluate(context, builder)

    assert evaluation.verdict == "degraded"
    assert "DEGRADED_SIGNAL:after:error_ratio" in evaluation.reason_codes


@pytest.mark.parametrize(
    ("signal", "value"),
    [
        ("deployment_ready_replicas", 0.0),
        ("dependency_deployments_available", 7.0),
        ("error_ratio", 0.5),  # whole-service errors, new version healthy
    ],
)
def test_context_only_degradation_is_unknown_no_data(signal: str, value: float) -> None:
    context, _, builder = _sufficient(ctx_values={signal: value})

    evaluation = _evaluate(context, builder)

    _assert_unknown(evaluation, "CONTEXT_DEGRADED_ONLY", outcome="no_data")
    assert not _has_prefix(evaluation, "DEGRADED_SIGNAL:after:")


def test_insufficient_new_version_traffic_is_unknown_even_with_service_traffic() -> (
    None
):
    minimum = PROFILE.effective_traffic.minimum
    context, _, builder = _sufficient(
        ctx_values={"request_rate_per_second": 10.0},
        after_values={"request_rate_per_second": minimum / 4},
    )

    evaluation = _evaluate(context, builder)

    _assert_unknown(evaluation, "INSUFFICIENT_TRAFFIC", outcome="no_data")


def test_a_degraded_reading_without_sufficient_mapping_is_not_degraded() -> None:
    # insufficient evidence never yields a release anomaly
    context, pods, builder = _sufficient(after_values={"error_ratio": 0.5})
    builder.results.pop("mapping:replicaset_owner:query")

    evaluation = _evaluate(context, builder)

    _assert_unknown(evaluation)
    assert evaluation.verdict != "degraded"


# --- authorization window (固定语义 4) --------------------------------------


def test_a_window_starting_before_authorization_is_unknown() -> None:
    context = _context(authorized_at=WINDOW_END - WINDOW + timedelta(seconds=1))
    context, _, builder = _sufficient(context=context)

    evaluation = _evaluate(context, builder)

    _assert_unknown(evaluation, "WINDOW_BEFORE_AUTHORIZATION", outcome="no_data")


def test_a_window_starting_exactly_at_authorization_counts() -> None:
    context = _context(authorized_at=WINDOW_END - WINDOW)
    context, _, builder = _sufficient(context=context)

    assert _evaluate(context, builder).verdict == "healthy"


# --- version identity (固定语义 5) ------------------------------------------


@pytest.mark.parametrize(
    "after_hash",
    [None, "Not_A_K8s_Name!", "-leading-dash", "x" * 300],
    ids=["missing", "illegal", "leading-dash", "too-long"],
)
def test_a_missing_or_illegal_after_hash_is_version_identity_missing(
    after_hash: str | None,
) -> None:
    auxiliary = ReleaseAuxiliary(
        before_pod_template_hash=BEFORE_HASH, after_pod_template_hash=after_hash
    )
    context, _, builder = _sufficient(context=_context(auxiliary=auxiliary))

    _assert_unknown(_evaluate(context, builder), "VERSION_IDENTITY_MISSING")


def test_a_config_only_release_with_distinct_hashes_is_judged() -> None:
    context = _context(before_images=AFTER_IMAGES)  # images unchanged
    context, _, builder = _sufficient(context=context)

    assert _evaluate(context, builder).verdict == "healthy"


@pytest.mark.parametrize(
    "before_hash", [AFTER_HASH, None], ids=["same-hash", "missing-before-hash"]
)
def test_a_config_only_release_needs_distinct_before_and_after_hashes(
    before_hash: str | None,
) -> None:
    context = _context(
        before_images=AFTER_IMAGES,
        auxiliary=ReleaseAuxiliary(
            before_pod_template_hash=before_hash, after_pod_template_hash=AFTER_HASH
        ),
    )
    context, _, builder = _sufficient(context=context)

    _assert_unknown(_evaluate(context, builder), "VERSION_SLICE_NOT_DISTINGUISHABLE")


# --- mapping sufficiency rules 1–7 (固定语义 5) -----------------------------


def test_rule1_no_replicaset_owner_is_mapping_missing() -> None:
    context, pods, builder = _sufficient()
    builder.mapping(pods, replicaset_owners=[])

    _assert_unknown(_evaluate(context, builder), "VERSION_MAPPING_MISSING")


@pytest.mark.parametrize(
    "conflict",
    [
        {"owner_name": "checkout-canary"},
        {"owner_kind": "StatefulSet", "owner_name": SERVICE},
    ],
    ids=["other-deployment", "other-kind"],
)
def test_rule1_a_conflicting_replicaset_owner_is_ambiguous(
    conflict: dict[str, str],
) -> None:
    context, pods, builder = _sufficient()
    owners = [
        EvidenceBuilder.replicaset_labels(),
        EvidenceBuilder.replicaset_labels(**conflict),
    ]
    builder.mapping(pods, replicaset_owners=owners)

    _assert_unknown(_evaluate(context, builder), "VERSION_MAPPING_AMBIGUOUS")


def test_rule2_one_uid_under_two_pod_names_is_ambiguous() -> None:
    context, pods, builder = _sufficient()
    twin = Pod(
        name=pods[0].name + "-x",
        uid=pods[0].uid,
        replicaset=REPLICASET,
        image_ids=pods[0].image_ids,
    )
    builder.relation(
        "pod_owner",
        [builder.pod_labels(p) for p in (*pods, twin)],
        "kube_pod_owner",
    )

    _assert_unknown(_evaluate(context, builder), "VERSION_MAPPING_AMBIGUOUS")


def test_rule2_an_instance_seen_in_the_window_but_gone_now_is_unknown() -> None:
    context, pods, builder = _sufficient()
    gone = pods_for(AFTER_IMAGES, 1)[0]
    # coverage keeps the window's owner relations: the vanished pod is in
    # the window, not in the current mapping
    builder.results["mapping:pod_owner:coverage"] = builder.series(
        "mapping:pod_owner:coverage",
        [(builder.pod_labels(p), float(builder.coverage)) for p in (*pods, gone)],
    )

    evaluation = _evaluate(context, builder)

    _assert_unknown(evaluation)
    assert {"VERSION_MAPPING_STALE", "VERSION_MAPPING_MISSING"} & set(
        evaluation.reason_codes
    ), evaluation.reason_codes


@pytest.mark.parametrize(
    "relation", ["replicaset_owner", "pod_owner", "container_info"]
)
def test_rule6_stale_mapping_metadata_is_mapping_stale(relation: str) -> None:
    context, pods, builder = _sufficient()
    stale = EvidenceBuilder(
        window_end=builder.window_end,
        texts=builder.texts,
        freshness_age=FRESHNESS + 60,
    )
    stale.mapping(pods)
    key = f"mapping:{relation}:freshness"
    builder.results[key] = stale.results[key]

    _assert_unknown(_evaluate(context, builder), "VERSION_MAPPING_STALE")


@pytest.mark.parametrize(
    "relation", ["replicaset_owner", "pod_owner", "container_info"]
)
def test_rule6_mapping_coverage_below_the_largest_minimum_is_insufficient(
    relation: str,
) -> None:
    context, pods, builder = _sufficient()
    thin = EvidenceBuilder(
        window_end=builder.window_end,
        texts=builder.texts,
        coverage=MAPPING_MINIMUM - 1,
    )
    thin.mapping(pods)
    key = f"mapping:{relation}:coverage"
    builder.results[key] = thin.results[key]

    _assert_unknown(_evaluate(context, builder), "VERSION_MAPPING_INSUFFICIENT_SAMPLES")


def test_rule6_mapping_coverage_at_the_minimum_suffices() -> None:
    context, _, builder = _sufficient(coverage=MAPPING_MINIMUM)

    assert _evaluate(context, builder).verdict == "healthy"


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


@pytest.mark.parametrize(
    "image_ids",
    [
        {"checkout": f"registry.example/otel/checkout@{digest('other')}"},
        {
            "checkout": f"registry.example/otel/checkout@{AFTER_IMAGES['checkout']}",
            "istio-proxy": f"registry.example/istio@{digest('sidecar')}",
        },
        {"other": f"registry.example/otel/checkout@{AFTER_IMAGES['checkout']}"},
    ],
    ids=["other-digest", "extra-container", "renamed-container"],
)
def test_rule3_the_full_container_image_map_must_equal_after_images(
    image_ids: dict[str, str],
) -> None:
    context, pods, builder = _sufficient()
    builder.mapping(_with_images(pods, image_ids))

    _assert_unknown(_evaluate(context, builder), "VERSION_IMAGE_MISMATCH")


def test_rule3_a_pod_missing_one_container_of_a_multi_image_release() -> None:
    images = {"checkout": digest("c-after"), "init-config": digest("i-after")}
    context = _context(
        before_images={"checkout": digest("c-before"), "init-config": digest("i-b")},
        after_images=images,
    )
    pods = pods_for(images)
    context, pods, builder = _sufficient(context=context, pods=pods)
    builder.mapping(
        _with_images(pods, {"checkout": f"repo/checkout@{images['checkout']}"})
    )

    _assert_unknown(_evaluate(context, builder), "VERSION_IMAGE_MISMATCH")


@pytest.mark.parametrize(
    "form",
    ["bare", "docker-pullable", "plain-repo"],
)
def test_rule4_accepted_image_id_forms(form: str) -> None:
    value = AFTER_IMAGES["checkout"]
    image_id = {
        "bare": value,
        "docker-pullable": f"docker-pullable://ghcr.io/otel/demo@{value}",
        "plain-repo": f"ghcr.io/open-telemetry/demo@{value}",
    }[form]
    context, pods, builder = _sufficient()
    builder.mapping(_with_images(pods, {"checkout": image_id}))

    evaluation = _evaluate(context, builder)

    assert evaluation.verdict == "healthy"
    assert all(i.images == AFTER_IMAGES for i in evaluation.instances)


@pytest.mark.parametrize(
    "image_id",
    [
        "ghcr.io/open-telemetry/demo:2.0.2-checkout",
        "sha256:" + AFTER_IMAGES["checkout"][7:].upper(),
        AFTER_IMAGES["checkout"][:-1],
        AFTER_IMAGES["checkout"] + "0",
        "",
    ],
    ids=["tag-only", "uppercase", "short", "long", "empty"],
)
def test_rule4_other_image_id_values_are_not_digests(image_id: str) -> None:
    context, pods, builder = _sufficient()
    builder.mapping(_with_images(pods, {"checkout": image_id}))

    evaluation = _evaluate(context, builder)

    _assert_unknown(evaluation)
    assert _has_prefix(evaluation, "VERSION_"), evaluation.reason_codes


@pytest.mark.parametrize("change", ["extra", "missing", "foreign"])
def test_rule5_service_instance_ids_must_equal_the_mapped_pod_uids(
    change: str,
) -> None:
    context, pods, builder = _sufficient()
    uids = [p.uid for p in pods]
    if change == "extra":
        uids.append(uuid4())
    elif change == "missing":
        uids = uids[:-1]
    else:
        uids = [*uids[:-1], uuid4()]
    builder.signals("after", {"request_rate_per_second": 0.05}, instances=uids)

    _assert_unknown(_evaluate(context, builder), "VERSION_INSTANCE_MISMATCH")


def test_rule7_more_than_64_candidate_pods_is_slice_too_large() -> None:
    pods = pods_for(AFTER_IMAGES, MAX_PODS + 1)
    context, _, builder = _sufficient(pods=pods)

    _assert_unknown(_evaluate(context, builder), "VERSION_SLICE_TOO_LARGE")


def test_rule7_exactly_64_candidate_pods_are_judged() -> None:
    pods = pods_for(AFTER_IMAGES, MAX_PODS)
    context, _, builder = _sufficient(pods=pods)

    evaluation = _evaluate(context, builder)

    assert evaluation.verdict == "healthy"
    assert len(evaluation.instances) == MAX_PODS


# --- telemetry sufficiency and SampleOutcome severity (固定语义 6/7) --------


def test_a_missing_context_signal_is_required_telemetry_missing() -> None:
    context, _, builder = _sufficient()
    builder.results.pop("context:deployment_ready_replicas:query")

    _assert_unknown(
        _evaluate(context, builder),
        "REQUIRED_TELEMETRY_MISSING:context:deployment_ready_replicas",
        outcome="no_data",
    )


def test_a_missing_after_signal_is_required_telemetry_missing() -> None:
    context, _, builder = _sufficient()
    builder.results.pop("after:latency_p95_milliseconds:query")

    _assert_unknown(
        _evaluate(context, builder),
        "REQUIRED_TELEMETRY_MISSING:after:latency_p95_milliseconds",
        outcome="no_data",
    )


def test_after_coverage_below_the_signal_minimum_is_insufficient_samples() -> None:
    context, _, builder = _sufficient()
    signal = "latency_p95_milliseconds"
    minimum = PROFILE.signal(signal).minimum_samples  # type: ignore[union-attr]
    builder.results[f"after:{signal}:coverage"] = builder.scalar(
        f"after:{signal}:coverage", float(minimum - 1)
    )

    _assert_unknown(
        _evaluate(context, builder),
        f"INSUFFICIENT_SAMPLES:after:{signal}",
        outcome="no_data",
    )


def test_stale_after_telemetry_is_stale() -> None:
    context, _, builder = _sufficient()
    signal = "error_ratio"
    builder.results[f"after:{signal}:freshness"] = builder.scalar(
        f"after:{signal}:freshness",
        (builder.window_end - timedelta(seconds=FRESHNESS + 60)).timestamp(),
    )

    _assert_unknown(
        _evaluate(context, builder), f"STALE_TELEMETRY:after:{signal}", outcome="stale"
    )


def test_a_timed_out_query_is_query_timeout() -> None:
    context, _, builder = _sufficient()
    key = "after:error_ratio:query"
    builder.results[key] = builder.timeout(key)

    _assert_unknown(
        _evaluate(context, builder), f"QUERY_TIMEOUT:{key}", outcome="timeout"
    )


def test_a_failed_query_is_query_failed() -> None:
    context, _, builder = _sufficient()
    key = "context:deployment_available_replicas:query"
    builder.results[key] = builder.failed(key)

    _assert_unknown(
        _evaluate(context, builder), f"QUERY_FAILED:{key}", outcome="failed"
    )


def test_an_incomplete_response_body_is_unknown() -> None:
    context, _, builder = _sufficient()
    key = "after:request_rate_per_second:query"
    builder.results[key] = builder.result(
        key, vector([({}, 0.05)], builder.window_end), body_complete=False
    )

    _assert_unknown(_evaluate(context, builder))


def test_unknown_outcome_takes_the_most_severe_and_keeps_every_reason() -> None:
    context, _, builder = _sufficient()
    failed = "context:dependency_error_ratio:query"
    timeout = "after:latency_p95_milliseconds:query"
    builder.results[failed] = builder.failed(failed)
    builder.results[timeout] = builder.timeout(timeout)
    builder.results["after:error_ratio:freshness"] = builder.scalar(
        "after:error_ratio:freshness",
        (builder.window_end - timedelta(seconds=FRESHNESS + 60)).timestamp(),
    )
    builder.results.pop("mapping:replicaset_owner:query")

    evaluation = _evaluate(context, builder)

    _assert_unknown(
        evaluation,
        f"QUERY_FAILED:{failed}",
        f"QUERY_TIMEOUT:{timeout}",
        "STALE_TELEMETRY:after:error_ratio",
        outcome="failed",
    )


def test_timeout_outranks_stale() -> None:
    context, _, builder = _sufficient()
    timeout = "after:latency_p95_milliseconds:query"
    builder.results[timeout] = builder.timeout(timeout)
    builder.results["after:error_ratio:freshness"] = builder.scalar(
        "after:error_ratio:freshness",
        (builder.window_end - timedelta(seconds=FRESHNESS + 60)).timestamp(),
    )

    _assert_unknown(_evaluate(context, builder), outcome="timeout")


def test_an_empty_sample_is_unknown_no_data() -> None:
    context = _context()
    builder = _builder(context, ())

    evaluation = _evaluate(context, builder)

    _assert_unknown(evaluation, outcome="no_data")
    assert evaluation.instances == ()


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
            "queries": queries,
        }
    )


@pytest.mark.parametrize(
    "fields",
    [
        _query(raw_sha256=sha256_hex(b"other")),
        _query(raw=b"x" * (RAW_LIMIT + 1)),
        _query(detail="nul\x00inside"),
        _query(source="loki"),
        _query(raw_sha256="A" * 64),
        _query(status="unknown"),
        _query(extra_field=1),
    ],
    ids=[
        "hash-mismatch",
        "raw-over-limit",
        "nul",
        "source",
        "upper-hash",
        "status",
        "unknown-field",
    ],
)
def test_query_result_dto_rejects_malformed_results(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ReleaseQueryResult.model_validate(fields)


def test_query_result_dto_accepts_a_body_at_the_limit() -> None:
    result = ReleaseQueryResult.model_validate(_query(raw=b"x" * RAW_LIMIT))
    assert len(result.raw) == RAW_LIMIT


def test_evidence_rejects_duplicate_query_keys() -> None:
    one = ReleaseQueryResult.model_validate(_query())
    with pytest.raises(ValidationError):
        _evidence([one, one])


def test_evidence_holds_at_most_64_query_results() -> None:
    results = [
        ReleaseQueryResult.model_validate(_query(key=f"context:s{i}:query"))
        for i in range(MAX_QUERIES + 1)
    ]
    assert len(_evidence(results[:MAX_QUERIES]).queries) == MAX_QUERIES
    with pytest.raises(ValidationError):
        _evidence(results)


def test_the_sufficient_fixture_uses_exactly_the_fixed_query_keys() -> None:
    # guards this file's fixtures against drifting from 固定语义 6
    _, _, builder = _sufficient()
    assert set(builder.results) == set(ALL_KEYS)


# --- query plan (固定语义 5/6; through the seam, see support module) --------


def _plan(uids: tuple[UUID, ...]) -> dict[str, str]:
    from tests.f11_release_support import planned_texts

    return planned_texts(_context(), WINDOW_END, uids)


def _compact(text: str) -> str:
    return "".join(text.split()).replace("'", '"')


def test_plan_has_exactly_the_fixed_keys() -> None:
    uids = (uuid4(), uuid4())
    assert set(_plan(uids)) == set(ALL_KEYS)


def test_plan_context_queries_are_the_unsliced_profile_queries() -> None:
    plan = _plan((uuid4(),))
    for signal in PROFILE.signals:
        assert _compact(plan[f"context:{signal.name}:query"]) == _compact(signal.query)
        assert "service_instance_id" not in plan[f"context:{signal.name}:query"]


def test_plan_binds_the_owner_relations_to_exact_strings() -> None:
    plan = {k: _compact(v) for k, v in _plan((uuid4(),)).items()}
    rs = plan["mapping:replicaset_owner:query"]
    for matcher in (
        f'namespace="{NAMESPACE}"',
        f'replicaset="{REPLICASET}"',
        'owner_kind="Deployment"',
        f'owner_name="{SERVICE}"',
    ):
        assert matcher in rs, (matcher, rs)
    for key in ("mapping:pod_owner:query", "mapping:container_info:query"):
        assert 'owner_kind="ReplicaSet"' in plan[key]
        assert f'owner_name="{REPLICASET}"' in plan[key]
    assert "kube_pod_container_info" in plan["mapping:container_info:query"]
    assert "on(namespace,pod,uid)" in plan["mapping:container_info:query"]
    for relation in ("replicaset_owner", "pod_owner", "container_info"):
        assert "count_over_time(" in plan[f"mapping:{relation}:coverage"]
        assert "timestamp(" in plan[f"mapping:{relation}:freshness"]


def test_plan_slices_every_after_selector_by_the_exact_uid_set() -> None:
    uids = (uuid4(), uuid4())
    plan = _plan(uids)
    for signal in SLICE_SIGNALS:
        for part in ("query", "coverage", "freshness", "instances"):
            text = plan[f"after:{signal}:{part}"]
            assert "service_instance_id=~" in _compact(text), text
            for uid in uids:
                assert str(uid) in text


def test_plan_never_widens_an_empty_uid_set_to_the_whole_service() -> None:
    try:
        plan = _plan(())
    except Exception:
        return  # refusing to plan an empty slice is acceptable
    for key, text in plan.items():
        if key.startswith("after:"):
            assert "service_instance_id=~" in _compact(text), (key, text)
