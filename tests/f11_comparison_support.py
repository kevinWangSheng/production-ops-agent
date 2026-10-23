"""Shared helpers for the independent F11 PR-4a acceptance tests.

Written from ``docs/tasks/2026-10-21-f11-pr4-contract.md`` (P4-01=A, R12, 第 4
节接口草案, 第 6 节第 11 项, r2 补充 S1-S9) without reading the PR-4a
implementation.

The contract fixes behaviour and a few public names (``build_release_comparison_plan``,
``evaluate_release_comparison``, ``ObservationStore.pending_comparisons``) but
NOT the table name, column names, query keys or the exact shape of ``results``.
Every such guess is concentrated here, so the tests themselves only assert
contract behaviour. If the implementation spells one of them differently, fix
this file, not the tests. The guesses:

* the comparison tables are the ``public`` tables whose name contains
  ``comparison``; the table(s) holding raw evidence have ``raw`` and
  ``raw_sha256`` columns (PR-2 ``opspilot_release_query_results`` convention),
  all rows are keyed by a ``comparison_id`` column, which the draft defines as
  ``uuid5(observation_id, "comparison:" + ending_id)``;
* a ``status`` column on the record table carries the DTO status;
* ``results`` for ``evaluate_release_comparison`` is ``{query_key:
  ReleaseQueryResult}`` (a tuple of results is tried when a dict is refused);
* ``ReleaseComparison.signals[]`` items carry ``scope``, ``value``,
  ``sample_count``, ``reason_codes`` (P4-01 field list) and a signal name under
  ``signal`` or ``name``;
* Prometheus is faked semantically (by PromQL shape, as PR-2 defines it), not
  by query key, because the comparison plan's keys are not in the contract.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid5

from opspilot.observation.release_types import ReleaseQueryResult
from opspilot.observer.prometheus import InstantResult
from tests.f11_release_support import (
    AFTER_HASH,
    BEFORE_HASH,
    HEALTHY_CONTEXT,
    PROFILE,
    SERVICE,
    EvidenceBuilder,
    Pod,
    sha256_hex,
)
from tests.test_f11_pr2b_acceptance import SENT_AT, _vector_result

OLD_RS = f"{SERVICE}-{BEFORE_HASH}"
NEW_RS = f"{SERVICE}-{AFTER_HASH}"
SLICE_SIGNAL_NAMES = (
    "request_rate_per_second",
    "error_ratio",
    "latency_p95_milliseconds",
    "dependency_error_ratio",
)
#: Distinct readings so that a slice is never confused with the whole service.
SERVICE_VALUES: dict[str, float] = {
    **HEALTHY_CONTEXT,
    "request_rate_per_second": 0.05,
    "error_ratio": 0.004,
    "latency_p95_milliseconds": 120.0,
    "dependency_error_ratio": 0.0,
}
OLD_SLICE_VALUES: dict[str, float] = {
    "request_rate_per_second": 0.03,
    "error_ratio": 0.008,
    "latency_p95_milliseconds": 150.0,
    "dependency_error_ratio": 0.0,
}


def _norm(text: str) -> str:
    return "".join(text.split())


_PROFILE_TEXT: dict[str, tuple[str, str]] = {}
for _signal in PROFILE.signals:
    for _kind, _field in (
        ("query", "query"),
        ("coverage", "coverage_query"),
        ("freshness", "freshness_query"),
    ):
        _PROFILE_TEXT.setdefault(_norm(getattr(_signal, _field)), (_signal.name, _kind))


@dataclass
class ComparisonWorld:
    """A fake Prometheus answering by PromQL shape.

    ``old_pods`` are the pods of the *before* ReplicaSet (``checkout-<before
    hash>``) as kube-state-metrics shows them at the queried instant;
    whole-service queries (the profile's own text) answer
    ``SERVICE_VALUES``; span queries sliced to instance UIDs answer
    ``OLD_SLICE_VALUES``. Every call is recorded; expressions of no known shape
    land in ``unclassified`` and answer an empty vector.
    """

    old_pods: Sequence[Pod]
    new_pods: Sequence[Pod] = ()
    mapping_ok: bool = True
    #: the old pods are visible to KSM only for instants strictly before this
    #: (None = always); models "released_at later than the rollout" (S5)
    visible_before: datetime | None = None
    #: what the slice ``instances`` query answers (None = exactly the old pods)
    instance_uids: Sequence[UUID] | None = None
    slice_coverage: int = 20
    service_coverage: int = 20
    mapping_coverage: int = 20
    fail_all: bool = False
    delay: float = 0.0
    calls: list[tuple[str, datetime, int]] = field(default_factory=list)
    unclassified: list[str] = field(default_factory=list)
    new_world_queries: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # -- InstantSource (incident leases another test may have left due)
    def instant(self, expr: str, *, at: Any, timeout_seconds: int) -> InstantResult:
        body = b'{"status":"success","data":{"resultType":"vector","result":[]}}'
        return InstantResult(expr, "no_data", None, body, 200)

    # -- ReleaseSource
    def instant_vector(self, expr: str, *, at: datetime, timeout_seconds: int) -> Any:
        with self._lock:
            self.calls.append((expr, at, timeout_seconds))
        if self.delay:
            time.sleep(self.delay)
        builder = EvidenceBuilder(
            window_end=at, coverage=self.service_coverage, freshness_age=15.0
        )
        key = "probe"
        if self.fail_all:
            result = builder.failed(key)
        else:
            result = self._answer(builder, key, expr, at)
        return _vector_result(expr, result, SENT_AT)

    def _answer(
        self, builder: EvidenceBuilder, key: str, expr: str, at: datetime
    ) -> ReleaseQueryResult:
        n = _norm(expr)
        empty = builder.series(key, [])
        kind = (
            "coverage"
            if n.startswith("count_over_time(")
            else "freshness"
            if n.startswith("timestamp(")
            else "query"
        )
        mapping = re.match(
            r"(?:count_over_time\(|timestamp\()?"
            r"(kube_replicaset_owner|kube_pod_owner|kube_pod_container_info)\{",
            n,
        )
        if mapping:
            relation = {
                "kube_replicaset_owner": "replicaset_owner",
                "kube_pod_owner": "pod_owner",
                "kube_pod_container_info": "container_info",
            }[mapping.group(1)]
            if NEW_RS in n:
                self.new_world_queries.append(expr)
                return empty
            if OLD_RS not in n:
                self.unclassified.append(expr)
                return empty
            hidden = self.visible_before is not None and at >= self.visible_before
            if not self.mapping_ok or hidden:
                return empty
            built = EvidenceBuilder(
                window_end=at, coverage=self.mapping_coverage, freshness_age=15.0
            )
            built.mapping(self.old_pods, replicaset=OLD_RS)
            return built.results[f"mapping:{relation}:{kind}"]
        if n in _PROFILE_TEXT:
            name, kind = _PROFILE_TEXT[n]
            return self._scalar(builder, key, kind, SERVICE_VALUES[name], False)
        if "traces_span_metrics" in n and "service_instance_id" in n:
            if any(str(p.uid) in n for p in self.new_pods):
                self.new_world_queries.append(expr)
                return empty
            if n.startswith("countby("):
                uids = (
                    [p.uid for p in self.old_pods]
                    if self.instance_uids is None
                    else list(self.instance_uids)
                )
                return builder.series(
                    key,
                    [
                        ({"service_instance_id": str(u)}, float(self.slice_coverage))
                        for u in uids
                    ],
                )
            if "timestamp(" in n:
                kind = "freshness"
            elif "count_over_time" in n:
                kind = "coverage"
            else:
                kind = "query"
            name = (
                "latency_p95_milliseconds"
                if "duration_milliseconds_bucket" in n
                else "dependency_error_ratio"
                if "SPAN_KIND_CLIENT" in n
                else "error_ratio"
                if "STATUS_CODE_ERROR" in n
                else "request_rate_per_second"
            )
            return self._scalar(builder, key, kind, OLD_SLICE_VALUES[name], True)
        self.unclassified.append(expr)
        return empty

    def _scalar(
        self,
        builder: EvidenceBuilder,
        key: str,
        kind: str,
        value: float,
        sliced: bool,
    ) -> ReleaseQueryResult:
        if kind == "coverage":
            count = self.slice_coverage if sliced else self.service_coverage
            return builder.scalar(key, float(count))
        if kind == "freshness":
            return builder.scalar(key, builder.stamp())
        return builder.scalar(key, value)

    # -- observations for assertions
    def times(self) -> set[datetime]:
        return {at for _, at, _ in self.calls}


# --- stored comparison, read the way replay does (S8) -------------------------


def comparison_id(observation_id: UUID, ending_id: UUID) -> UUID:
    return uuid5(observation_id, "comparison:" + str(ending_id))


def comparison_tables(rows: Any) -> dict[str, set[str]]:
    """``{table: {columns}}`` of the public tables named like a comparison."""
    found: dict[str, set[str]] = {}
    for row in rows(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name LIKE 'opspilot%%comparison%%'"
    ):
        found.setdefault(row["table_name"], set()).add(row["column_name"])
    return found


def to_result(row: Mapping[str, Any]) -> ReleaseQueryResult:
    return ReleaseQueryResult(
        query_key=row["query_key"],
        query=row["query"],
        source=row.get("source") or "prometheus",
        evaluated_at=row["evaluated_at"],
        sent_at=row.get("sent_at"),
        status=row["status"],
        http_status=row.get("http_status"),
        detail=row.get("detail") or "",
        body_complete=row.get("body_complete", True),
        raw=bytes(row["raw"]),
        raw_sha256=row["raw_sha256"],
    )


def evaluate(evaluator: Any, context: Any, plan: Any, results: dict[str, Any]) -> Any:
    try:
        return evaluator(context, plan, results)
    except (TypeError, AttributeError):
        return evaluator(context, plan, tuple(results.values()))


def by_scope(comparison: Any) -> dict[str, list[Any]]:
    scopes: dict[str, list[Any]] = {}
    for signal in comparison.signals:
        scopes.setdefault(signal.scope, []).append(signal)
    return scopes


def valued(signals: Sequence[Any]) -> list[Any]:
    return [s for s in signals if getattr(s, "value", None) is not None]


def all_reasons(comparison: Any) -> set[str]:
    reasons = set(comparison.reason_codes)
    for signal in comparison.signals:
        reasons.update(getattr(signal, "reason_codes", ()) or ())
    return reasons


def has_reason(comparison: Any, code: str) -> bool:
    return any(code in reason for reason in all_reasons(comparison))


def bounds(window: Any) -> tuple[datetime, datetime]:
    if hasattr(window, "start"):
        return window.start, window.end
    return window[0], window[1]


def tamper_digest(raw: bytes) -> str:
    return sha256_hex(raw + b" ")


WINDOW_SECONDS = PROFILE.evaluation_window_seconds
QUERY_TIMEOUT = PROFILE.query_timeout_seconds
BEFORE_WINDOW = timedelta(seconds=WINDOW_SECONDS)
