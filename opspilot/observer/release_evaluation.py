"""Deterministic release query plans and raw-response evaluation, without I/O."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, field_validator

from opspilot.domain.base import DTO
from opspilot.domain.evidence import QueryWindow
from opspilot.observation.release_policy import ReleasePolicy, sha256, storable_text
from opspilot.observation.release_types import (
    ReleaseEvaluation,
    ReleaseInstance,
    ReleaseQueryResult,
    ReleaseSampleEvidence,
    ReleaseSignalVerdict,
)
from opspilot.observation.releases import ReleaseRecord
from opspilot.observer.health_profile import HealthProfile, canonical_content

AFTER_SIGNALS = frozenset(
    {
        "request_rate_per_second",
        "error_ratio",
        "latency_p95_milliseconds",
        "dependency_error_ratio",
    }
)
_SUPPORTED_QUERIES: dict[str, dict[str, str]] = {
    "deployment_available_replicas": {
        "query": 'min(kube_deployment_status_replicas_available{namespace="otel-demo",deployment="checkout"})',
        "coverage_query": 'min(count_over_time(kube_deployment_status_replicas_available{namespace="otel-demo",deployment="checkout"}[5m]))',
        "freshness_query": 'min(timestamp(kube_deployment_status_replicas_available{namespace="otel-demo",deployment="checkout"}))',
    },
    "request_rate_per_second": {
        "query": 'sum(rate(traces_span_metrics_calls_total{k8s_namespace_name="otel-demo",service_name="checkout",span_kind="SPAN_KIND_SERVER",span_name=~".*CheckoutService/PlaceOrder"}[5m]))',
        "coverage_query": 'max(count_over_time(traces_span_metrics_calls_total{k8s_namespace_name="otel-demo",service_name="checkout",span_kind="SPAN_KIND_SERVER",span_name=~".*CheckoutService/PlaceOrder"}[5m]))',
        "freshness_query": 'min(timestamp(traces_span_metrics_calls_total{k8s_namespace_name="otel-demo",service_name="checkout",span_kind="SPAN_KIND_SERVER",span_name=~".*CheckoutService/PlaceOrder"}))',
    },
    "error_ratio": {
        "query": '(sum(rate(traces_span_metrics_calls_total{k8s_namespace_name="otel-demo",service_name="checkout",span_kind="SPAN_KIND_SERVER",span_name=~".*CheckoutService/PlaceOrder",status_code="STATUS_CODE_ERROR"}[5m])) or vector(0)) / sum(rate(traces_span_metrics_calls_total{k8s_namespace_name="otel-demo",service_name="checkout",span_kind="SPAN_KIND_SERVER",span_name=~".*CheckoutService/PlaceOrder"}[5m]))',
        "coverage_query": 'max(count_over_time(traces_span_metrics_calls_total{k8s_namespace_name="otel-demo",service_name="checkout",span_kind="SPAN_KIND_SERVER",span_name=~".*CheckoutService/PlaceOrder"}[5m]))',
        "freshness_query": 'min(timestamp(traces_span_metrics_calls_total{k8s_namespace_name="otel-demo",service_name="checkout",span_kind="SPAN_KIND_SERVER",span_name=~".*CheckoutService/PlaceOrder"}))',
    },
    "latency_p95_milliseconds": {
        "query": 'histogram_quantile(0.95, sum by (le) (rate(traces_span_metrics_duration_milliseconds_bucket{k8s_namespace_name="otel-demo",service_name="checkout",span_kind="SPAN_KIND_SERVER",span_name=~".*CheckoutService/PlaceOrder"}[5m])))',
        "coverage_query": 'max(count_over_time(traces_span_metrics_duration_milliseconds_bucket{k8s_namespace_name="otel-demo",service_name="checkout",span_kind="SPAN_KIND_SERVER",span_name=~".*CheckoutService/PlaceOrder"}[5m]))',
        "freshness_query": 'min(timestamp(traces_span_metrics_duration_milliseconds_bucket{k8s_namespace_name="otel-demo",service_name="checkout",span_kind="SPAN_KIND_SERVER",span_name=~".*CheckoutService/PlaceOrder"}))',
    },
    "deployment_ready_replicas": {
        "query": 'min(kube_deployment_status_replicas_ready{namespace="otel-demo",deployment="checkout"})',
        "coverage_query": 'min(count_over_time(kube_deployment_status_replicas_ready{namespace="otel-demo",deployment="checkout"}[5m]))',
        "freshness_query": 'min(timestamp(kube_deployment_status_replicas_ready{namespace="otel-demo",deployment="checkout"}))',
    },
    "deployment_available_replicas_min_in_window": {
        "query": 'min(min_over_time(kube_deployment_status_replicas_available{namespace="otel-demo",deployment="checkout"}[5m]))',
        "coverage_query": 'min(count_over_time(kube_deployment_status_replicas_available{namespace="otel-demo",deployment="checkout"}[5m]))',
        "freshness_query": 'min(timestamp(kube_deployment_status_replicas_available{namespace="otel-demo",deployment="checkout"}))',
    },
    "dependency_deployments_available": {
        "query": 'count(kube_deployment_status_replicas_available{namespace="otel-demo",deployment=~"payment|cart|valkey-cart|product-catalog|currency|shipping|email|flagd"} >= 1)',
        "coverage_query": 'min(count_over_time(kube_deployment_status_replicas_available{namespace="otel-demo",deployment=~"payment|cart|valkey-cart|product-catalog|currency|shipping|email|flagd"}[5m]))',
        "freshness_query": 'min(timestamp(kube_deployment_status_replicas_available{namespace="otel-demo",deployment=~"payment|cart|valkey-cart|product-catalog|currency|shipping|email|flagd"}))',
    },
    "dependency_error_ratio": {
        "query": '(sum(rate(traces_span_metrics_calls_total{k8s_namespace_name="otel-demo",service_name="checkout",span_kind="SPAN_KIND_CLIENT",status_code="STATUS_CODE_ERROR"}[5m])) or vector(0)) / sum(rate(traces_span_metrics_calls_total{k8s_namespace_name="otel-demo",service_name="checkout",span_kind="SPAN_KIND_CLIENT"}[5m]))',
        "coverage_query": 'max(count_over_time(traces_span_metrics_calls_total{k8s_namespace_name="otel-demo",service_name="checkout",span_kind="SPAN_KIND_CLIENT"}[5m]))',
        "freshness_query": 'min(timestamp(traces_span_metrics_calls_total{k8s_namespace_name="otel-demo",service_name="checkout",span_kind="SPAN_KIND_CLIENT"}))',
    },
}

CONTEXT_SIGNALS = AFTER_SIGNALS | {
    "deployment_available_replicas",
    "deployment_ready_replicas",
    "deployment_unavailable_replicas",
    "dependencies_available_replicas",
}


class ReleaseEvaluationContext(DTO):
    release: ReleaseRecord
    session_id: UUID
    authorized_at: AwareDatetime
    policy: ReleasePolicy
    health_profile: HealthProfile

    @field_validator("authorized_at")
    @classmethod
    def utc_authorization(cls, value: datetime) -> datetime:
        return value.astimezone(timezone.utc)


def supported_profile(profile: HealthProfile) -> bool:
    """This adapter accepts threshold revisions, not arbitrary metric algorithms."""
    if profile.source != "prometheus" or {s.name for s in profile.signals} != set(
        _SUPPORTED_QUERIES
    ):
        return False
    for signal in profile.signals:
        if not signal.required:
            return False
        for field, original in _SUPPORTED_QUERIES[signal.name].items():
            expected = original.replace(
                "otel-demo", profile.subject.kubernetes_namespace
            ).replace("checkout", profile.subject.service)
            expected = re.sub(
                r"\[[0-9]+[smhd]\]", f"[{profile.evaluation_window_seconds}s]", expected
            )
            actual = re.sub(
                r"\[[0-9]+[smhd]\]",
                f"[{profile.evaluation_window_seconds}s]",
                getattr(signal, field),
            )
            if normalize_query(expected) != normalize_query(actual):
                return False
    return profile.effective_traffic.signal in AFTER_SIGNALS


def normalize_query(query: str) -> str:
    """Ignore PromQL whitespace and matcher order, preserving quoted contents."""
    tokens = re.findall(r'"(?:\\.|[^"\\])*"|\s+|[^\s"]+', query)
    compact = "".join(t for t in tokens if not t.isspace())

    def sort_matchers(match: re.Match[str]) -> str:
        parts = re.findall(
            r'[a-zA-Z_][a-zA-Z0-9_]*(?:=~|!~|!=|=)"(?:\\.|[^"\\])*"', match.group(1)
        )
        if ",".join(parts) != match.group(1):
            return match.group(0)
        return "{" + ",".join(sorted(parts)) + "}"

    return re.sub(r'\{((?:[^"{}]|"(?:\\.|[^"\\])*")*)\}', sort_matchers, compact)


def version_identity(
    context: ReleaseEvaluationContext,
) -> tuple[str | None, str | None]:
    release = context.release
    h = release.auxiliary.after_pod_template_hash
    rs = f"{context.health_profile.subject.service}-{h}"
    if (
        h is None
        or len(h) > 63
        or re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", h) is None
        or len(rs) > 253
        or re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*", rs
        )
        is None
    ):
        return None, "VERSION_IDENTITY_MISSING"
    if release.before_revision.images == release.after_revision.images and (
        not release.auxiliary.before_pod_template_hash
        or release.auxiliary.before_pod_template_hash == h
    ):
        return None, "VERSION_SLICE_NOT_DISTINGUISHABLE"
    return rs, None


def build_release_query_plan(
    context: ReleaseEvaluationContext,
    *,
    window_end: datetime,
    instance_uids: Sequence[UUID],
) -> dict[str, str]:
    """Mapping/context first; the proven UID set adds the after queries."""
    profile = context.health_profile
    if not supported_profile(profile):
        raise ValueError("HEALTH_PROFILE_INVALID")
    n, s = profile.subject.kubernetes_namespace, profile.subject.service
    rs, _ = version_identity(context)
    plan: dict[str, str] = {}
    w = profile.evaluation_window_seconds
    if rs is not None:
        ns = "namespace=" + json.dumps(n)
        expected_owners = {
            "replicaset_owner": "kube_replicaset_owner{"
            + ns
            + ",replicaset="
            + json.dumps(rs)
            + ',owner_kind="Deployment",owner_name='
            + json.dumps(s)
            + "}",
            "pod_owner": "kube_pod_owner{"
            + ns
            + ',owner_kind="ReplicaSet",owner_name='
            + json.dumps(rs)
            + "}",
        }
        selectors = {
            "replicaset_owner": "kube_replicaset_owner{"
            + ns
            + ",replicaset="
            + json.dumps(rs)
            + "}",
            "pod_owner": "kube_pod_owner{" + ns + "}",
            "container_info": "kube_pod_container_info{" + ns + "}",
        }
        for name, selector in selectors.items():
            expressions = {
                "query": selector,
                "coverage": f"count_over_time({selector}[{w}s])",
                "freshness": f"timestamp({selector})",
            }
            if name in expected_owners:
                owner = expected_owners[name]
                join = (
                    "namespace,replicaset"
                    if name == "replicaset_owner"
                    else "namespace,pod,uid"
                )
                expressions["query"] += f" and on({join}) {owner}"
                expressions["freshness"] += f" and on({join}) timestamp({owner})"
                expressions["coverage"] += (
                    f" and on({join}) count_over_time({owner}[{w}s])"
                )
            else:
                owner = expected_owners["pod_owner"]
                expressions["query"] += f" and on(namespace,pod,uid) {owner}"
                expressions["freshness"] += f" and on(namespace,pod,uid) {owner}"
                expressions["coverage"] += (
                    f" and on(namespace,pod,uid) count_over_time({owner}[{w}s])"
                )
            for kind, expr in expressions.items():
                plan[f"mapping:{name}:{kind}"] = expr
    for signal in profile.signals:
        for kind, field in (
            ("query", "query"),
            ("coverage", "coverage_query"),
            ("freshness", "freshness_query"),
        ):
            plan[f"context:{signal.name}:{kind}"] = getattr(signal, field)
    if instance_uids:
        union = "|".join(
            re.escape(str(uid)) for uid in sorted(set(instance_uids), key=str)
        )
        matcher = "service_instance_id=~" + json.dumps(union)
        instance_labels = "service_instance_id, k8s_pod_name, k8s_pod_uid, k8s_namespace_name, service_name"
        for signal in profile.signals:
            if signal.name not in AFTER_SIGNALS:
                continue
            sliced = {
                kind: re.sub(
                    r"\{([^{}]*)\}",
                    lambda m: "{" + m.group(1) + "," + matcher + "}",
                    getattr(signal, field),
                )
                for kind, field in (
                    ("query", "query"),
                    ("coverage", "coverage_query"),
                    ("freshness", "freshness_query"),
                )
            }
            coverage = normalize_query(
                re.sub(r"\[[0-9]+[smhd]\]", f"[{w}s]", sliced["coverage"])
            )
            # The same request/duration selector proves instance presence, not error numerators.
            selector_match = re.fullmatch(
                r"max\(count_over_time\((.+)\[" + str(w) + r"s\]\)\)", coverage
            )
            if selector_match is None:
                raise ValueError("HEALTH_PROFILE_INVALID")
            selector = selector_match.group(1)
            sliced["coverage"] = (
                f"min(max by ({instance_labels}) (count_over_time({selector}[{w}s])))"
            )
            sliced["instances"] = (
                f"count by ({instance_labels}) (count_over_time({selector}[{w}s]))"
            )
            for kind, expr in sliced.items():
                plan[f"after:{signal.name}:{kind}"] = expr
    return plan


def parse_vector(
    result: ReleaseQueryResult | None, reasons: set[str]
) -> list[tuple[dict[str, str], float, float]]:
    if result is None:
        return []
    if (
        result.status != "ok"
        or not result.body_complete
        or (result.http_status is not None and not 200 <= result.http_status < 300)
    ):
        if result.status == "timeout":
            reasons.add("QUERY_TIMEOUT:" + result.query_key)
        elif (
            result.status == "failed"
            or not result.body_complete
            or (result.http_status is not None and not 200 <= result.http_status < 300)
        ):
            reasons.add("QUERY_FAILED:" + result.query_key)
        return []
    try:
        body = json.loads(result.raw)
        if body["status"] != "success" or body["data"]["resultType"] != "vector":
            raise ValueError
        rows = body["data"]["result"]
        if not isinstance(rows, list):
            raise ValueError
        vectors: dict[
            tuple[tuple[str, str], ...], tuple[dict[str, str], float, float]
        ] = {}
        for row in rows:
            labels = row["metric"]
            if not isinstance(labels, dict) or any(
                not isinstance(k, str) or not isinstance(v, str)
                for k, v in labels.items()
            ):
                raise ValueError
            for key, text in labels.items():
                storable_text(key)
                storable_text(text)
            t, v = row["value"]
            if isinstance(t, bool) or isinstance(v, bool):
                raise ValueError
            stamp, value = float(t), float(v)
            if (
                not math.isfinite(stamp)
                or not math.isfinite(value)
                or stamp > result.evaluated_at.timestamp()
            ):
                raise ValueError
            identity = tuple(sorted(labels.items()))
            vector = (labels, stamp, value)
            if identity in vectors and vectors[identity] != vector:
                raise ValueError
            vectors[identity] = vector
        return list(vectors.values())
    except (KeyError, TypeError, ValueError, OverflowError):
        reasons.add("QUERY_FAILED:" + result.query_key)
        return []


def _mapped_instances(
    context: ReleaseEvaluationContext,
    queries: Mapping[str, ReleaseQueryResult],
    at: datetime,
    reasons: set[str],
) -> tuple[ReleaseInstance, ...]:
    rs, error = version_identity(context)
    if error:
        reasons.add(error)
        return ()
    profile = context.health_profile
    minimum = max(s.minimum_samples for s in profile.signals)
    groups: dict[str, list[tuple[dict[str, str], float, float]]] = {}
    for name in ("replicaset_owner", "pod_owner", "container_info"):
        current = parse_vector(queries.get(f"mapping:{name}:query"), reasons)
        coverage = parse_vector(queries.get(f"mapping:{name}:coverage"), reasons)
        fresh = parse_vector(queries.get(f"mapping:{name}:freshness"), reasons)
        if not current or not coverage or not fresh:
            reasons.add("VERSION_MAPPING_MISSING")
        if name in ("replicaset_owner", "pod_owner"):
            expected_kind = "Deployment" if name == "replicaset_owner" else "ReplicaSet"
            expected_name = (
                profile.subject.service if name == "replicaset_owner" else rs
            )
            if any(
                labels.get("owner_kind") != expected_kind
                or labels.get("owner_name") != expected_name
                for labels, _, _ in current + coverage + fresh
            ):
                reasons.add("VERSION_MAPPING_AMBIGUOUS")

        def identity(labels: dict[str, str]) -> tuple[tuple[str, str], ...]:
            return tuple(sorted((k, v) for k, v in labels.items() if k != "__name__"))

        cur = {identity(labels): (labels, t, v) for labels, t, v in current}
        cov = {identity(labels): v for labels, _, v in coverage}
        freshness = {identity(labels): v for labels, _, v in fresh}
        if set(cov) != set(cur) or set(freshness) != set(cur):
            reasons.add("VERSION_MAPPING_STALE")
        for key in cov:
            if cov[key] < minimum or not cov[key].is_integer():
                reasons.add("VERSION_MAPPING_INSUFFICIENT_SAMPLES")
        for key in cur:
            stamp = freshness.get(key)
            if (
                stamp is None
                or not 0 <= at.timestamp() - stamp <= profile.freshness_seconds
            ):
                reasons.add("VERSION_MAPPING_STALE")
        if (
            name == "pod_owner"
            and len({labels.get("uid", "") for labels, _, _ in current + coverage}) > 64
        ):
            reasons.add("VERSION_SLICE_TOO_LARGE")
        groups[name] = current
    n, s = profile.subject.kubernetes_namespace, profile.subject.service
    rsrows = groups["replicaset_owner"]
    if len(rsrows) != 1 or any(
        labels.get("namespace") != n
        or labels.get("replicaset") != rs
        or labels.get("owner_kind") != "Deployment"
        or labels.get("owner_name") != s
        or v != 1
        for labels, _, v in rsrows
    ):
        reasons.add("VERSION_MAPPING_AMBIGUOUS")
    pods: dict[str, str] = {}
    for labels, _, v in groups["pod_owner"]:
        uid, pod = labels.get("uid", ""), labels.get("pod", "")
        try:
            UUID(uid)
        except ValueError:
            reasons.add("VERSION_INSTANCE_MISMATCH")
            continue
        if (
            not pod
            or labels.get("namespace") != n
            or labels.get("owner_kind") != "ReplicaSet"
            or labels.get("owner_name") != rs
            or v != 1
            or (uid in pods and pods[uid] != pod)
            or (pod in pods.values() and pods.get(uid) != pod)
        ):
            reasons.add("VERSION_MAPPING_AMBIGUOUS")
        pods[uid] = pod
    if len(pods) > 64:
        reasons.add("VERSION_SLICE_TOO_LARGE")
        return ()
    images: dict[str, dict[str, str]] = {uid: {} for uid in pods}
    for labels, _, v in groups["container_info"]:
        uid = labels.get("uid", "")
        if (
            uid not in pods
            or labels.get("namespace") != n
            or labels.get("pod") != pods.get(uid)
            or v != 1
        ):
            reasons.add("VERSION_INSTANCE_MISMATCH")
            continue
        container = labels.get("container", "")
        image = labels.get("image_id", "")
        digest = re.fullmatch(r"(?:[^\s]+@)?(sha256:[0-9a-f]{64})", image)
        if not container or digest is None:
            reasons.add("VERSION_IMAGE_MISMATCH")
            continue
        if container in images[uid] and images[uid][container] != digest.group(1):
            reasons.add("VERSION_MAPPING_AMBIGUOUS")
        images[uid][container] = digest.group(1)
    for uid in pods:
        if images[uid] != context.release.after_revision.images:
            reasons.add("VERSION_IMAGE_MISMATCH")
    if any(r.startswith(("VERSION_", "QUERY_")) for r in reasons):
        return ()
    return tuple(
        ReleaseInstance(
            pod_uid=UUID(uid),
            pod_name=pods[uid],
            replicaset_name=rs or "",
            images=images[uid],
        )
        for uid in sorted(pods)
    )


def query_evidence_consistent(
    context: ReleaseEvaluationContext,
    evidence: ReleaseSampleEvidence,
    decision_at: datetime,
    *,
    require_complete: bool = True,
) -> bool:
    if not supported_profile(context.health_profile):
        return False
    reasons: set[str] = set()
    instances = _mapped_instances(
        context,
        {q.query_key: q for q in evidence.queries},
        evidence.window.end,
        reasons,
    )
    return query_plan_consistent(
        context,
        evidence.window,
        decision_at,
        [(q.query_key, q.query, q.source, q.evaluated_at) for q in evidence.queries],
        tuple(i.pod_uid for i in instances),
        require_complete=require_complete,
    )


def query_plan_consistent(
    context: ReleaseEvaluationContext,
    window: QueryWindow,
    decision_at: datetime,
    queries: Sequence[tuple[str, str, str, datetime]],
    instance_uids: Sequence[UUID],
    *,
    require_complete: bool = True,
) -> bool:
    """The part of ``query_evidence_consistent`` that reads no response body:
    each ``(query_key, query, source, evaluated_at)`` against the plan for
    ``instance_uids``, the window length and ``window.end <= decision_at``.
    A replay whose bodies were cleaned (#244) runs it with the instances the
    stored evaluation recorded."""
    if not supported_profile(context.health_profile):
        return False
    plan = build_release_query_plan(
        context,
        window_end=window.end,
        instance_uids=tuple(instance_uids),
    )
    keys = [key for key, _, _, _ in queries]
    if len(set(keys)) != len(keys):
        return False
    # Empty evidence is a counted unknown; partial evidence must match the
    # complete plan (after keys exist only when mapping produces a UID set).
    if require_complete and queries and not set(plan).issubset(keys):
        return False
    if (
        window.end - window.start
    ).total_seconds() != context.health_profile.evaluation_window_seconds or (
        window.end > decision_at
    ):
        return False
    return all(
        key in plan
        and normalize_query(query) == normalize_query(plan[key])
        and source == "prometheus"
        and evaluated_at == window.end
        for key, query, source, evaluated_at in queries
    )


def evaluate_release_sample(
    context: ReleaseEvaluationContext,
    evidence: ReleaseSampleEvidence,
    *,
    decision_at: AwareDatetime,
) -> ReleaseEvaluation:
    reasons: set[str] = set()
    profile = context.health_profile
    if (
        not supported_profile(profile)
        or profile.revision != context.release.health_profile_revision
        or sha256(canonical_content(profile).encode())
        != context.policy.content.health_profile_sha256
    ):
        reasons.add("HEALTH_PROFILE_INVALID")
        if evidence.window.start < context.authorized_at:
            reasons.add("WINDOW_BEFORE_AUTHORIZATION")
        for query in evidence.queries:
            parse_vector(query, reasons)
        return ReleaseEvaluation(
            verdict="unknown",
            outcome="failed",
            evidence_sufficient=False,
            reason_codes=tuple(sorted(reasons)),
            instances=(),
            signals=(),
        )
    if evidence.window.start < context.authorized_at:
        reasons.add("WINDOW_BEFORE_AUTHORIZATION")
    queries = {q.query_key: q for q in evidence.queries}
    instances = _mapped_instances(context, queries, evidence.window.end, reasons)
    plan = build_release_query_plan(
        context,
        window_end=evidence.window.end,
        instance_uids=tuple(i.pod_uid for i in instances),
    )
    inconsistent = not query_evidence_consistent(context, evidence, decision_at)
    # Inconsistency is an adoption fence; the pure result still reports unknown safely.
    if inconsistent:
        for q in evidence.queries:
            if (
                q.query_key not in plan
                or normalize_query(q.query) != normalize_query(plan[q.query_key])
                or q.evaluated_at != evidence.window.end
            ):
                reasons.add("QUERY_FAILED:" + q.query_key)
    verdicts: list[ReleaseSignalVerdict] = []
    for scope in ("context", "after"):
        for signal in profile.signals:
            if scope == "after" and signal.name not in AFTER_SIGNALS:
                continue
            kinds = (
                ("query", "coverage", "freshness", "instances")
                if scope == "after"
                else ("query", "coverage", "freshness")
            )
            keys = tuple(f"{scope}:{signal.name}:{kind}" for kind in kinds)
            parsed = [parse_vector(queries.get(key), reasons) for key in keys]
            value: float | None = None
            count: int | None = None
            latest: datetime | None = None
            verdict: Any = "missing"
            uid_names = {
                str(instance.pod_uid): instance.pod_name for instance in instances
            }
            for vectors in parsed:
                for labels, _, _ in vectors:
                    if (
                        labels.get(
                            "k8s_namespace_name", profile.subject.kubernetes_namespace
                        )
                        != profile.subject.kubernetes_namespace
                        or labels.get("service_name", profile.subject.service)
                        != profile.subject.service
                        or (
                            scope == "after"
                            and "service_instance_id" in labels
                            and labels["service_instance_id"] not in uid_names
                        )
                        or (
                            scope == "after"
                            and "k8s_pod_name" in labels
                            and labels["k8s_pod_name"] not in uid_names.values()
                        )
                    ):
                        reasons.add("VERSION_INSTANCE_MISMATCH")
                    uid = labels.get("service_instance_id")
                    if (
                        scope == "after"
                        and uid is not None
                        and uid in uid_names
                        and labels.get("k8s_pod_name", uid_names[uid]) != uid_names[uid]
                    ):
                        reasons.add("VERSION_INSTANCE_MISMATCH")
                    if (
                        scope == "after"
                        and uid is not None
                        and labels.get("k8s_pod_uid", uid) != uid
                    ):
                        reasons.add("VERSION_INSTANCE_MISMATCH")

            insufficient_samples = stale = False
            if len(parsed[1]) == 1:
                rawcount = parsed[1][0][2]
                if rawcount >= 0 and rawcount.is_integer():
                    count = int(rawcount)
                insufficient_samples = count is None or count < signal.minimum_samples
                if insufficient_samples:
                    reasons.add(f"INSUFFICIENT_SAMPLES:{scope}:{signal.name}")
            if len(parsed[2]) == 1:
                timestamp = parsed[2][0][2]
                try:
                    latest = datetime.fromtimestamp(timestamp, timezone.utc)
                except (ValueError, OverflowError, OSError):
                    latest = None
                stale = latest is None or not (
                    0
                    <= evidence.window.end.timestamp() - timestamp
                    <= profile.freshness_seconds
                )
                if stale:
                    reasons.add(f"STALE_TELEMETRY:{scope}:{signal.name}")
            if all(len(v) == 1 for v in parsed[:3]):
                value = parsed[0][0][2]
                instance_mismatch = scope == "after" and (
                    len(parsed[3]) != len(uid_names)
                    or {labels.get("service_instance_id") for labels, _, _ in parsed[3]}
                    != set(uid_names)
                )
                if instance_mismatch:
                    reasons.add("VERSION_INSTANCE_MISMATCH")
                if stale:
                    verdict = "stale"
                elif insufficient_samples:
                    verdict = "insufficient_samples"
                elif instance_mismatch:
                    verdict = "missing"
                else:
                    verdict = (
                        "healthy" if signal.healthy.contains(value) else "degraded"
                    )
                    if verdict == "degraded":
                        reasons.add(f"DEGRADED_SIGNAL:{scope}:{signal.name}")
            else:
                reasons.add(f"REQUIRED_TELEMETRY_MISSING:{scope}:{signal.name}")
                statuses = {queries[k].status for k in keys if k in queries}
                if "failed" in statuses or any(
                    "QUERY_FAILED:" + k in reasons for k in keys
                ):
                    verdict = "failed"
                elif "timeout" in statuses:
                    verdict = "timeout"
            verdicts.append(
                ReleaseSignalVerdict(
                    scope=scope,
                    signal_name=signal.name,
                    verdict=verdict,
                    value=value,
                    sample_count=count,
                    latest_sample_at=latest,
                    query_keys=keys,
                )
            )
    gate = next(
        (
            v
            for v in verdicts
            if v.scope == "after" and v.signal_name == profile.effective_traffic.signal
        ),
        None,
    )
    if (
        gate is None
        or gate.value is None
        or gate.value < profile.effective_traffic.minimum
    ):
        reasons.add("INSUFFICIENT_TRAFFIC")
    sufficient = (
        bool(instances)
        and not inconsistent
        and all(v.verdict in ("healthy", "degraded") for v in verdicts)
        and not any(not r.startswith("DEGRADED_SIGNAL:") for r in reasons)
    )
    after_degraded = any(
        v.scope == "after" and v.verdict == "degraded" for v in verdicts
    )
    context_degraded = any(
        v.scope == "context" and v.verdict == "degraded" for v in verdicts
    )
    release_verdict: Literal["healthy", "degraded", "unknown"] = "unknown"
    outcome: Any = "no_data"
    if sufficient and after_degraded:
        release_verdict, outcome = "degraded", "degraded"
    elif sufficient and not context_degraded:
        release_verdict, outcome = "healthy", "healthy"
    elif sufficient and context_degraded:
        reasons.add("CONTEXT_DEGRADED_ONLY")
    if release_verdict == "unknown":
        for severity, prefix in (
            ("failed", "QUERY_FAILED:"),
            ("timeout", "QUERY_TIMEOUT:"),
            ("stale", "STALE_TELEMETRY:"),
        ):
            if any(r.startswith(prefix) for r in reasons):
                outcome = severity
                break
        if outcome == "no_data" and "VERSION_MAPPING_STALE" in reasons:
            outcome = "stale"
    return ReleaseEvaluation(
        verdict=release_verdict,
        outcome=outcome,
        evidence_sufficient=sufficient,
        reason_codes=tuple(sorted(reasons)) if release_verdict != "healthy" else (),
        instances=instances,
        signals=tuple(verdicts),
    )


def advance_release_window(
    context: ReleaseEvaluationContext,
    session: Mapping[str, Any],
    evaluation: ReleaseEvaluation,
    *,
    window_start: datetime,
    window_end: datetime,
    decision_at: datetime,
    used_sample_count: int,
) -> tuple[dict[str, Any], str | None, str | None]:
    """The release-only coverage fold, shared with offline reconstruction."""
    tracking_since = session.get("tracking_since")
    tracking_until = session.get("tracking_until")
    healthy_since = session.get("healthy_since")
    gap = tracking_until is None or window_start > tracking_until
    if evaluation.evidence_sufficient and evaluation.verdict != "unknown":
        tracking_since = window_start if gap else tracking_since
        tracking_until = window_end
    else:
        tracking_since = tracking_until = None
    if evaluation.verdict == "healthy":
        if gap or healthy_since is None:
            healthy_since = window_end
    else:
        healthy_since = None
    marks = {
        "tracking_since": tracking_since,
        "tracking_until": tracking_until,
        "healthy_since": healthy_since,
    }
    release = context.release
    status = reason = None
    if evaluation.evidence_sufficient and evaluation.verdict == "degraded":
        status, reason = "anomalous", "anomaly_detected"
    elif (
        evaluation.verdict == "healthy"
        and tracking_since is not None
        and healthy_since is not None
        and window_end
        >= release.released_at.astimezone(timezone.utc)
        + timedelta(seconds=release.min_tracking_seconds)
        and (window_end - tracking_since).total_seconds()
        >= release.min_tracking_seconds
        and (window_end - healthy_since).total_seconds()
        >= release.sustained_window_seconds
        and decision_at < release.deadline_at
    ):
        status, reason = "healthy", "healthy_confirmed"
    elif used_sample_count >= release.max_samples:
        status, reason = "unknown", "max_samples_exhausted"
    return marks, status, reason
