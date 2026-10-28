"""F11 PR-4a implementer tests: the pure before/after comparison (S1-S9).

Inputs are raw Prometheus bodies from ``tests.f11_release_support``; the old
slice is PR-2's mapping and evaluation with the before identity, so these
tests pin the comparison's own rules: windows, scope naming, the strict UID
set, what is unavailable and when an attempt is retried.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from opspilot.domain.evidence import QueryWindow
from opspilot.observation.release_comparison import (
    ComparisonLease,
    ReleaseComparisonEvidence,
    before_context,
    build_release_comparison_plan,
    compare_evidence,
    comparison_id_for,
    needs_retry,
)
from opspilot.observation.releases import ReleaseAuxiliary
from opspilot.observer.release_evaluation import (
    build_release_query_plan,
    evaluate_release_sample,
)
from tests.f11_release_support import (
    AFTER_HASH,
    BEFORE_HASH,
    DEGRADED_AFTER,
    HEALTHY_AFTER,
    HEALTHY_CONTEXT,
    SERVICE,
    WINDOW,
    EvidenceBuilder,
    pods_for,
)
from tests.test_f11_release_evaluation import (
    AFTER_IMAGES,
    BEFORE_IMAGES,
    RELEASED_AT,
    WINDOW_END,
    _context,
    _sufficient,
)

OLD_RS = f"{SERVICE}-{BEFORE_HASH}"
TIMES = {"released_at": RELEASED_AT, "after_window_end": WINDOW_END}
DECISION = WINDOW_END + timedelta(seconds=5)


def _anomaly(context):
    _, pods, builder = _sufficient(context=context, after_values=DEGRADED_AFTER)
    evaluation = evaluate_release_sample(
        context, builder.evidence(), decision_at=WINDOW_END + timedelta(seconds=2)
    )
    assert evaluation.verdict == "degraded"
    return evaluation


def _before(context, pods, *, extra_instance=None, coverage=20, at=None):
    """Raw before evidence, PR-2 keys renamed to comparison keys."""
    old = before_context(context)
    at = at or RELEASED_AT
    uids = [p.uid for p in pods]
    texts = build_release_query_plan(old, window_end=RELEASED_AT, instance_uids=uids)
    builder = EvidenceBuilder(window_end=at, texts=texts, coverage=coverage)
    if pods:
        builder.mapping(pods, replicaset=OLD_RS)
    builder.signals("context", HEALTHY_CONTEXT)
    if pods:
        builder.signals(
            "after",
            HEALTHY_AFTER,
            instances=uids + ([extra_instance] if extra_instance else []),
        )
    return builder


def _evidence(builder, *, attempts=1, rename=None):
    prefixes = (
        ("mapping:", "before_old:mapping:"),
        ("context:", "before_service:"),
        ("after:", "before_old:"),
    )

    def key(k):
        for pr2, new in prefixes:
            if k.startswith(pr2):
                return new + k[len(pr2) :]
        return k

    evidence = builder.evidence()
    return ReleaseComparisonEvidence(
        format_version=1,
        comparison_id=uuid4(),
        attempts=attempts,
        window=evidence.window,
        queries=tuple(
            q.model_copy(update={"query_key": key(q.query_key)})
            for q in (rename(evidence.queries) if rename else evidence.queries)
        ),
    )


def _compare(context, evidence, anomaly=None):
    anomaly = anomaly or _anomaly(context)
    return compare_evidence(
        context,
        observation_id=context.release.observation_id,
        ending_id=uuid4(),
        sample_id=uuid4(),
        window_after=QueryWindow(start=WINDOW_END - WINDOW, end=WINDOW_END),
        anomaly=anomaly,
        evidence=evidence,
        decision_at=DECISION,
    )


def test_plan_is_evaluated_for_the_old_replicaset_with_scope_keys():
    context = _context()
    pods = pods_for(BEFORE_IMAGES, replicaset=OLD_RS)
    mapping_only = build_release_comparison_plan(context, **TIMES)
    full = build_release_comparison_plan(
        context, **TIMES, instance_uids=[p.uid for p in pods]
    )
    assert len(mapping_only) == 9 + 24 and len(full) == 49
    assert all(k.startswith(("before_service:", "before_old:")) for k in full)
    text = " ".join(full.values())
    assert f'"{OLD_RS}"' in text and f"{SERVICE}-{AFTER_HASH}" not in text


def test_complete_comparison_lists_four_scopes_without_a_verdict():
    context = _context()
    pods = pods_for(BEFORE_IMAGES, replicaset=OLD_RS)
    anomaly = _anomaly(context)
    comparison = _compare(context, _evidence(_before(context, pods)), anomaly)
    assert comparison.status == "complete" and comparison.reason_codes == ()
    assert comparison.window_before == QueryWindow(
        start=RELEASED_AT - WINDOW, end=RELEASED_AT
    )
    counts = {}
    for s in comparison.signals:
        counts[s.scope] = counts.get(s.scope, 0) + 1
        assert s.value is not None, s
    assert counts == {
        "before_service": 8,
        "before_old": 4,
        "after_service": 8,
        "after_new": 4,
    }
    assert {i.pod_uid for i in comparison.before_instances} == {p.uid for p in pods}
    assert comparison.after_instances == anomaly.instances
    after_error = next(
        s
        for s in comparison.signals
        if s.scope == "after_new" and s.signal_name == "error_ratio"
    )
    assert after_error.value == DEGRADED_AFTER["error_ratio"]
    assert "verdict" not in comparison.model_dump()


def test_missing_before_hash_keeps_only_the_service_scope():
    context = _context(auxiliary=ReleaseAuxiliary(after_pod_template_hash=AFTER_HASH))
    plan = build_release_comparison_plan(context, **TIMES)
    assert len(plan) == 24 and all(k.startswith("before_service:") for k in plan)
    comparison = _compare(context, _evidence(_before(context, ())))
    assert comparison.status == "partial"
    assert "VERSION_IDENTITY_MISSING:before_old" in comparison.reason_codes
    old = [s for s in comparison.signals if s.scope == "before_old"]
    assert old and all(
        s.value is None and "VERSION_IDENTITY_MISSING:before_old" in s.reason_codes
        for s in old
    )
    assert all(
        s.value is not None for s in comparison.signals if s.scope == "before_service"
    )


def test_equal_hashes_are_not_distinguishable_but_equal_images_are():
    same = _context(
        auxiliary=ReleaseAuxiliary(
            before_pod_template_hash=AFTER_HASH, after_pod_template_hash=AFTER_HASH
        )
    )
    comparison = _compare(same, _evidence(_before(same, ())))
    assert "VERSION_SLICE_NOT_DISTINGUISHABLE:before_old" in comparison.reason_codes
    # A configuration release keeps the digest; the ReplicaSet tells them apart.
    config = _context(before_images=AFTER_IMAGES)
    pods = pods_for(AFTER_IMAGES, replicaset=OLD_RS)
    assert _compare(config, _evidence(_before(config, pods))).status == "complete"


def test_old_slice_requires_exactly_the_mapped_uid_set():
    context = _context()
    pods = pods_for(BEFORE_IMAGES, replicaset=OLD_RS)
    comparison = _compare(
        context, _evidence(_before(context, pods, extra_instance=uuid4()))
    )
    assert comparison.status == "partial"
    assert "VERSION_INSTANCE_MISMATCH:before_old" in comparison.reason_codes
    assert all(s.value is None for s in comparison.signals if s.scope == "before_old")


def test_insufficient_before_samples_are_named_by_scope():
    context = _context()
    pods = pods_for(BEFORE_IMAGES, replicaset=OLD_RS)
    builder = _before(context, pods)
    builder.results["context:error_ratio:coverage"] = builder.scalar(
        "context:error_ratio:coverage", 2.0
    )
    comparison = _compare(context, _evidence(builder))
    assert comparison.status == "partial"
    assert "INSUFFICIENT_SAMPLES:before_service:error_ratio" in comparison.reason_codes
    entry = next(
        s
        for s in comparison.signals
        if s.scope == "before_service" and s.signal_name == "error_ratio"
    )
    assert entry.value is None and entry.reason_codes == (
        "INSUFFICIENT_SAMPLES:before_service:error_ratio",
    )


def test_all_timeouts_are_unavailable_and_retried():
    context = _context()
    # Mapping timed out, so no slice query was planned or sent.
    builder = _before(context, ())
    for key in builder.texts:
        builder.results[key] = builder.timeout(key)
    assert len(builder.results) == 33
    comparison = _compare(context, _evidence(builder, attempts=3))
    assert comparison.status == "unavailable" and comparison.attempts == 3
    assert needs_retry(comparison)
    assert any(c.startswith("QUERY_TIMEOUT:before_") for c in comparison.reason_codes)


def test_evidence_not_taken_at_released_at_is_inconsistent():
    context = _context()
    pods = pods_for(BEFORE_IMAGES, replicaset=OLD_RS)
    late = _before(context, pods, at=RELEASED_AT + timedelta(seconds=30))
    comparison = _compare(context, _evidence(late))
    assert comparison.status == "unavailable"
    assert comparison.reason_codes == ("QUERY_EVIDENCE_INCONSISTENT",)
    assert not needs_retry(comparison)


def test_lease_identity_is_derived_from_observation_and_ending():
    observation, ending = uuid4(), uuid4()
    fields = dict(
        observation_id=observation,
        ending_id=ending,
        session_id=uuid4(),
        sample_id=uuid4(),
        anomaly_key="release-anomaly:" + str(observation),
        released_at=RELEASED_AT,
        window_after=QueryWindow(start=WINDOW_END - WINDOW, end=WINDOW_END),
        global_suspension_generation=0,
        target_suspension_generation=0,
    )
    lease = ComparisonLease(
        comparison_id=comparison_id_for(observation, ending), **fields
    )
    assert lease.comparison_id == comparison_id_for(observation, ending)
    with pytest.raises(ValidationError):
        ComparisonLease(comparison_id=uuid4(), **fields)


def test_after_scopes_without_a_sufficient_anomaly_are_never_complete():
    context = _context()
    pods = pods_for(BEFORE_IMAGES, replicaset=OLD_RS)
    anomaly = _anomaly(context)
    for broken in (
        anomaly.model_copy(update={"evidence_sufficient": False}),
        anomaly.model_copy(update={"signals": ()}),
        anomaly.model_copy(update={"verdict": "healthy"}),
    ):
        comparison = _compare(context, _evidence(_before(context, pods)), broken)
        assert comparison.status == "partial"
        assert "AFTER_EVIDENCE_INSUFFICIENT" in comparison.reason_codes
