"""Shared builders for the independent F11 PR-2a acceptance tests.

Written from ``docs/tasks/2026-10-13-f11-pr2-contract.md`` (「接口定稿（r1）」)
without reading the implementation. Two kinds of helpers live here:

* calibrated-policy catalog entries (公开接口 1): canonical content JSON,
  ``content_sha256`` and ``revision = policy_id@sha[:12]``;
* raw Prometheus instant-vector responses keyed by the fixed query keys
  (固定语义 6) for the checkout profile's eight signals, the four slice
  signals and the three mapping relations (固定语义 5).

Query plan: r1 fixes the query *keys* and the PromQL *relations* but not
whitespace or matcher order, and the store refuses evidence whose query text
differs from its own plan (固定语义 7, ``readings_inconsistent``). The r2
addendum (lead decision, reversible) makes the builder public as
``opspilot.observer.release_evaluation.build_release_query_plan``;
:data:`PLAN_BUILDER` is bound to it.

The raw *shape* each key answers with is also this file's reading of the
contract (documented on :class:`EvidenceBuilder`), not a contract fact.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from opspilot.domain.evidence import QueryWindow
from opspilot.observation.release_types import (
    ReleaseQueryResult,
    ReleaseSampleEvidence,
)
from opspilot.observer import (
    PROFILE_DIRECTORY,
    canonical_content,
    load_health_profile,
    profile_revision,
)
from opspilot.observer.release_evaluation import build_release_query_plan

PROFILE = load_health_profile(PROFILE_DIRECTORY / "otel-demo-checkout.json")
PROFILE_CONTENT = canonical_content(PROFILE)
PROFILE_REVISION = profile_revision(PROFILE)
PROFILE_SHA256 = hashlib.sha256(PROFILE_CONTENT.encode("utf-8")).hexdigest()
WINDOW = timedelta(seconds=PROFILE.evaluation_window_seconds)  # W = 300 s
INTERVAL = timedelta(seconds=PROFILE.session.sample_interval_seconds)  # I = 60 s
SUSTAINED = timedelta(seconds=PROFILE.session.sustained_window_seconds)  # H = 600 s
FRESHNESS = PROFILE.freshness_seconds  # 90 s
MAPPING_MINIMUM = max(s.minimum_samples for s in PROFILE.signals)  # 3
NAMESPACE = PROFILE.subject.kubernetes_namespace  # N
SERVICE = PROFILE.subject.service  # S
AFTER_HASH = "7d4b9c8f6d"  # after_pod_template_hash h
BEFORE_HASH = "5f6b7c8d9e"
REPLICASET = f"{SERVICE}-{AFTER_HASH}"  # RS = S + "-" + h
RAW_LIMIT = 30_720
MAX_QUERIES = 64
MAX_PODS = 64
EVALUATOR = "release-after-v1"
MAPPING = "pod-uid-replicaset-image-v1"
EMPTY_CATALOG = '{"format_version":1,"policies":[]}'

APPROVED_POLICY_REVISION = "otel-demo-checkout-release@e2ed7b69b96b"
APPROVED_POLICY_SHA256 = (
    "e2ed7b69b96bf50d50e6632c022f27da35d0592c412ea70dcf5fac249d0633ee"
)
_REPO_ROOT = Path(__file__).resolve().parents[1]


def assert_shipped_catalog_is_the_approved_policy() -> None:
    """The shipped catalog holds exactly the one user-approved policy (2026-10-22, M=1500)."""
    from opspilot.observation import release_policy

    shipped = (
        Path(release_policy.__file__).resolve().parent
        / "policies"
        / "release-calibrations.json"
    )
    assert Path(release_policy.RELEASE_POLICY_CATALOG_PATH).resolve() == shipped
    raw = json.loads(shipped.read_text(encoding="utf-8"))
    assert set(raw) == {"format_version", "policies"}
    assert raw["format_version"] == 1
    (entry,) = raw["policies"]
    assert set(entry) == {"policy_id", "revision", "content_sha256", "content"}
    assert entry["policy_id"] == "otel-demo-checkout-release"
    assert entry["revision"] == APPROVED_POLICY_REVISION
    assert entry["content_sha256"] == APPROVED_POLICY_SHA256
    content = entry["content"]
    assert content["target"] == {
        "integration_id": "m0-otel-20260909",
        "cluster_uid": "c8001471-8b7b-4449-83c0-924e6f46b857",
        "namespace": "otel-demo",
        "resource_uid": "m0-otel-20260909",
        "workload": "checkout",
    }
    assert content["health_profile_revision"] == "otel-demo-checkout@b72bbe2e30be"
    assert content["health_profile_sha256"] == (
        "b72bbe2e30bea4775790519787da4ce107dbc1500ce87b87a2ee2af3f57cbfa4"
    )
    assert content["min_tracking_seconds"] == 1500
    assert content["evaluator"] == "release-after-v1"
    assert content["mapping"] == "pod-uid-replicaset-image-v1"
    assert content["evidence_path"] == "docs/evidence/f11-u2-calibration/summary.json"
    # the pinned evidence digest is the digest of the committed summary bytes
    summary = (_REPO_ROOT / content["evidence_path"]).read_bytes()
    assert content["evidence_sha256"] == hashlib.sha256(summary).hexdigest()
    assert content["evidence_sha256"] == (
        "908838acb33923046a446b813cbd12add660df8d4ca56f015478253db4693364"
    )
    # the loader (hash and revision validation) accepts it as the only policy
    from opspilot.observation.release_policy import load_release_policy_catalog

    loaded = load_release_policy_catalog().policies
    assert [p.revision for p in loaded] == [APPROVED_POLICY_REVISION]
    assert loaded[0].content.min_tracking_seconds == 1500


TARGET_KEYS = ("integration_id", "cluster_uid", "namespace", "resource_uid", "workload")

#: The four slice signals (固定语义 5) and the eight context signals.
SLICE_SIGNALS = (
    "request_rate_per_second",
    "error_ratio",
    "latency_p95_milliseconds",
    "dependency_error_ratio",
)
CONTEXT_SIGNALS = tuple(signal.name for signal in PROFILE.signals)
MAPPINGS = ("replicaset_owner", "pod_owner", "container_info")
PARTS = ("query", "coverage", "freshness")

#: In-bounds readings of every profile signal (profile ``healthy`` bounds).
HEALTHY_CONTEXT = {
    "deployment_available_replicas": 1.0,
    "request_rate_per_second": 0.05,
    "error_ratio": 0.0,
    "latency_p95_milliseconds": 120.0,
    "deployment_ready_replicas": 1.0,
    "deployment_available_replicas_min_in_window": 1.0,
    "dependency_deployments_available": 8.0,
    "dependency_error_ratio": 0.0,
}
HEALTHY_AFTER = {name: HEALTHY_CONTEXT[name] for name in SLICE_SIGNALS}
#: One out-of-bounds reading per slice signal (``request_rate`` has only
#: ``min: 0``; a negative rate is not a reading, so it has no degraded case).
DEGRADED_AFTER = {
    "error_ratio": 0.5,
    "latency_p95_milliseconds": 900.0,
    "dependency_error_ratio": 0.2,
}

assert set(CONTEXT_SIGNALS) == set(HEALTHY_CONTEXT), "profile drifted"
assert PROFILE.effective_traffic.signal == "request_rate_per_second"


def mapping_keys() -> tuple[str, ...]:
    return tuple(f"mapping:{m}:{p}" for m in MAPPINGS for p in PARTS)


def context_keys() -> tuple[str, ...]:
    return tuple(f"context:{s}:{p}" for s in CONTEXT_SIGNALS for p in PARTS)


def after_keys() -> tuple[str, ...]:
    return tuple(f"after:{s}:{p}" for s in SLICE_SIGNALS for p in (*PARTS, "instances"))


ALL_KEYS = mapping_keys() + context_keys() + after_keys()
assert len(ALL_KEYS) == 49 <= MAX_QUERIES


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def digest(seed: str) -> str:
    return "sha256:" + sha256_hex(seed)


# --- calibrated policy catalog (公开接口 1) ----------------------------------

APPROVED_AT = "2026-10-01T00:00:00Z"
EVIDENCE_PATH = "docs/evidence/f11-test-calibration/run.md"


def canonical_json(value: Mapping[str, Any]) -> str:
    """UTF-8, sorted keys, no extra whitespace (公开接口 1)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def policy_content(
    target: Mapping[str, str],
    *,
    min_tracking_seconds: int,
    health_profile_revision: str = PROFILE_REVISION,
    health_profile_sha256: str = PROFILE_SHA256,
    **overrides: Any,
) -> dict[str, Any]:
    """A ``ReleasePolicyContent`` as JSON. Only values whose JSON form is
    already canonical are used (``approved_at`` in the ``Z`` spelling), so the
    file text and the model's canonical dump hash the same."""
    content: dict[str, Any] = {
        "format_version": 1,
        "target": {key: target[key] for key in TARGET_KEYS},
        "health_profile_revision": health_profile_revision,
        "health_profile_sha256": health_profile_sha256,
        "min_tracking_seconds": min_tracking_seconds,
        "evaluator": EVALUATOR,
        "mapping": MAPPING,
        "evidence_path": EVIDENCE_PATH,
        "evidence_sha256": sha256_hex("independent test calibration evidence"),
        "approval_ref": "test-only: not a formal calibration",
        "approved_at": APPROVED_AT,
    }
    content.update(overrides)
    return content


def policy_entry(
    content: Mapping[str, Any],
    *,
    policy_id: str | None = None,
    revision: str | None = None,
    content_sha256: str | None = None,
) -> dict[str, Any]:
    """One catalog entry; ``revision``/``content_sha256`` default to the
    contract formula and can be overridden to build a corrupt entry."""
    policy_id = policy_id or f"checkout-release-test-{uuid4().hex[:8]}"
    true_sha = sha256_hex(canonical_json(content))
    sha = content_sha256 if content_sha256 is not None else true_sha
    return {
        "policy_id": policy_id,
        "revision": revision if revision is not None else f"{policy_id}@{sha[:12]}",
        "content_sha256": sha,
        "content": dict(content),
    }


def catalog_text(*entries: Mapping[str, Any]) -> str:
    return canonical_json({"format_version": 1, "policies": list(entries)})


# --- query-plan seam (r2 addendum, see module docstring) --------------------


def _r2_plan(
    context: Any, window_end: datetime, uids: tuple[UUID, ...]
) -> Mapping[str, str]:
    return build_release_query_plan(context, window_end=window_end, instance_uids=uids)


#: ``(context, window_end, instance_uids) -> {query_key: query_text}``.
PLAN_BUILDER: Callable[[Any, datetime, tuple[UUID, ...]], Mapping[str, str]] | None = (
    _r2_plan
)


def planned_texts(
    context: Any, window_end: datetime, uids: Iterable[UUID]
) -> dict[str, str]:
    """The implementation's planned query text per key; fails the test while
    the seam is unbound (only tests that need a plan-conforming sample)."""
    if PLAN_BUILDER is None:
        pytest.fail(
            "QUERY_PLAN_SEAM_UNBOUND: r1 names no public query-plan builder; "
            "bind tests.f11_release_support.PLAN_BUILDER",
            pytrace=False,
        )
    return dict(PLAN_BUILDER(context, window_end, tuple(uids)))


def texts_if_bound(
    context: Any, window_end: datetime, uids: Iterable[UUID]
) -> dict[str, str] | None:
    """Planned text when available, else ``None`` (placeholder text): the
    pure evaluator judges by key and raw bytes; text conformity is the
    store's (``readings_inconsistent``) and replay's (``QUERY_MISMATCH``)."""
    if PLAN_BUILDER is None:
        return None
    return dict(PLAN_BUILDER(context, window_end, tuple(uids)))


# --- raw Prometheus responses ------------------------------------------------


def _number(value: float) -> str:
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def vector(series: Sequence[tuple[Mapping[str, str], float]], at: datetime) -> bytes:
    """A Prometheus ``/api/v1/query`` instant-vector body evaluated at ``at``."""
    stamp = at.timestamp()
    body = {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": dict(labels), "value": [stamp, _number(value)]}
                for labels, value in series
            ],
        },
    }
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class Pod:
    """One candidate instance as kube-state-metrics exposes it."""

    name: str
    uid: UUID
    replicaset: str
    #: container name -> ``image_id`` label value exactly as exposed
    image_ids: Mapping[str, str]


def image_id_for(image_digest: str, container: str) -> str:
    """The usual containerd ``image_id`` spelling: ``<repo>@sha256:<hex>``."""
    return f"registry.example/otel/{container}@{image_digest}"


#: Module-wide Pod name suffix sequence: names stay unique within one call and
#: across calls on the same ReplicaSet (#323: random 5-hex suffixes collided).
_POD_SEQ = itertools.count()


def pods_for(
    images: Mapping[str, str], count: int = 2, *, replicaset: str = REPLICASET
) -> tuple[Pod, ...]:
    return tuple(
        Pod(
            name=f"{replicaset}-{next(_POD_SEQ):05x}",
            uid=uuid4(),
            replicaset=replicaset,
            image_ids={c: image_id_for(d, c) for c, d in images.items()},
        )
        for _ in range(count)
    )


@dataclass
class EvidenceBuilder:
    """Builds every query result of one sample evaluated at ``window_end``.

    Raw shapes (this file's reading of 固定语义 5/6, documented as such):

    * ``mapping:<m>:query`` — the raw kube-state-metrics series (with
      ``__name__``); ``:coverage`` — ``count_over_time`` per series (same
      labels, no ``__name__``); ``:freshness`` — ``timestamp`` per series.
    * ``context:<s>:*`` — the profile's own aggregated query, coverage and
      freshness queries: one unlabelled sample each.
    * ``after:<s>:{query,coverage,freshness}`` — the sliced aggregate: one
      unlabelled sample each (``min(count_over_time)`` / ``min(timestamp)``).
    * ``after:<s>:instances`` — one series per covered
      ``service_instance_id`` (value: its raw sample count).
    """

    window_end: datetime
    texts: Mapping[str, str] | None = None
    coverage: int = 20
    freshness_age: float = 15.0
    results: dict[str, ReleaseQueryResult] = field(default_factory=dict)

    # -- single results

    def text(self, key: str) -> str:
        if self.texts is None:
            return f"placeholder for {key}"
        return self.texts.get(key, f"unplanned {key}")

    def result(
        self,
        key: str,
        raw: bytes,
        *,
        status: str = "ok",
        http_status: int | None = 200,
        body_complete: bool = True,
        detail: str = "",
        evaluated_at: datetime | None = None,
    ) -> ReleaseQueryResult:
        assert len(raw) <= RAW_LIMIT, (key, len(raw))
        return ReleaseQueryResult(
            query_key=key,
            query=self.text(key),
            source="prometheus",
            evaluated_at=evaluated_at or self.window_end,
            sent_at=self.window_end,
            status=status,
            http_status=http_status,
            detail=detail,
            body_complete=body_complete,
            raw=raw,
            raw_sha256=sha256_hex(raw),
        )

    def series(
        self, key: str, series: Sequence[tuple[Mapping[str, str], float]]
    ) -> ReleaseQueryResult:
        if not series:
            return self.result(key, vector([], self.window_end), status="no_data")
        return self.result(key, vector(series, self.window_end))

    def scalar(self, key: str, value: float) -> ReleaseQueryResult:
        return self.series(key, [({}, value)])

    def timeout(self, key: str) -> ReleaseQueryResult:
        return self.result(
            key,
            b"",
            status="timeout",
            http_status=None,
            body_complete=False,
            detail="deadline exceeded",
        )

    def failed(self, key: str) -> ReleaseQueryResult:
        return self.result(
            key,
            b'{"status":"error","errorType":"execution","error":"boom"}',
            status="failed",
            http_status=500,
            detail="HTTP 500",
        )

    def stamp(self) -> float:
        return (self.window_end - timedelta(seconds=self.freshness_age)).timestamp()

    # -- relation series

    @staticmethod
    def replicaset_labels(
        replicaset: str = REPLICASET,
        owner_name: str = SERVICE,
        owner_kind: str = "Deployment",
    ) -> dict[str, str]:
        return {
            "namespace": NAMESPACE,
            "replicaset": replicaset,
            "owner_kind": owner_kind,
            "owner_name": owner_name,
            "owner_is_controller": "true",
        }

    @staticmethod
    def pod_labels(pod: Pod) -> dict[str, str]:
        return {
            "namespace": NAMESPACE,
            "pod": pod.name,
            "uid": str(pod.uid),
            "owner_kind": "ReplicaSet",
            "owner_name": pod.replicaset,
            "owner_is_controller": "true",
        }

    @staticmethod
    def container_labels(pod: Pod, container: str, image_id: str) -> dict[str, str]:
        return {
            "namespace": NAMESPACE,
            "pod": pod.name,
            "uid": str(pod.uid),
            "container": container,
            "image_id": image_id,
        }

    def relation(
        self, name: str, labels: Sequence[Mapping[str, str]], metric: str
    ) -> None:
        """Query, coverage and freshness of one mapping relation."""
        self.results[f"mapping:{name}:query"] = self.series(
            f"mapping:{name}:query",
            [({"__name__": metric, **lb}, 1.0) for lb in labels],
        )
        self.results[f"mapping:{name}:coverage"] = self.series(
            f"mapping:{name}:coverage", [(lb, float(self.coverage)) for lb in labels]
        )
        self.results[f"mapping:{name}:freshness"] = self.series(
            f"mapping:{name}:freshness", [(lb, self.stamp()) for lb in labels]
        )

    def mapping(
        self,
        pods: Sequence[Pod],
        *,
        replicaset: str = REPLICASET,
        replicaset_owners: Sequence[Mapping[str, str]] | None = None,
    ) -> None:
        owners = (
            [self.replicaset_labels(replicaset)]
            if replicaset_owners is None
            else list(replicaset_owners)
        )
        self.relation("replicaset_owner", owners, "kube_replicaset_owner")
        self.relation("pod_owner", [self.pod_labels(p) for p in pods], "kube_pod_owner")
        self.relation(
            "container_info",
            [
                self.container_labels(p, c, i)
                for p in pods
                for c, i in sorted(p.image_ids.items())
            ],
            "kube_pod_container_info",
        )

    def signals(
        self,
        scope: str,
        values: Mapping[str, float],
        *,
        instances: Sequence[UUID] = (),
    ) -> None:
        for name, value in values.items():
            self.results[f"{scope}:{name}:query"] = self.scalar(
                f"{scope}:{name}:query", value
            )
            self.results[f"{scope}:{name}:coverage"] = self.scalar(
                f"{scope}:{name}:coverage", float(self.coverage)
            )
            self.results[f"{scope}:{name}:freshness"] = self.scalar(
                f"{scope}:{name}:freshness", self.stamp()
            )
            if scope == "after":
                self.results[f"after:{name}:instances"] = self.series(
                    f"after:{name}:instances",
                    [
                        ({"service_instance_id": str(uid)}, float(self.coverage))
                        for uid in instances
                    ],
                )

    def sufficient(
        self,
        pods: Sequence[Pod],
        *,
        context: Mapping[str, float] | None = None,
        after: Mapping[str, float] | None = None,
    ) -> EvidenceBuilder:
        """Every key of a sufficient sample; ``context``/``after`` override
        single readings on top of the healthy values."""
        self.mapping(pods)
        self.signals("context", {**HEALTHY_CONTEXT, **(context or {})})
        self.signals(
            "after",
            {**HEALTHY_AFTER, **(after or {})},
            instances=[p.uid for p in pods],
        )
        return self

    def evidence(self, sample_id: UUID | None = None) -> ReleaseSampleEvidence:
        return ReleaseSampleEvidence(
            format_version=1,
            sample_id=sample_id or uuid4(),
            window=QueryWindow(start=self.window_end - WINDOW, end=self.window_end),
            queries=tuple(self.results[key] for key in sorted(self.results)),
        )


def empty_evidence(
    window_end: datetime, sample_id: UUID | None = None
) -> ReleaseSampleEvidence:
    """A sample with no query result at all (e.g. a scope interruption before
    the first request): legal, adoptable, unknown (固定语义 6/7)."""
    return ReleaseSampleEvidence(
        format_version=1,
        sample_id=sample_id or uuid4(),
        window=QueryWindow(start=window_end - WINDOW, end=window_end),
        queries=(),
    )


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)
