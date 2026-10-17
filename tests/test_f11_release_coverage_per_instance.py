"""Independent contract tests: after-slice coverage is counted per instance.

Written from ``docs/tasks/2026-10-13-f11-pr2-contract.md`` (切片覆盖, and the
r8 addendum 「切片覆盖按实例计」) without reading the body of
``build_release_query_plan``. The after coverage of every slice signal is::

    min(max by (<instances labels>) (count_over_time(<request/duration selector>[Ws])))

so status_code (and ``le``) split series are merged per instance with
``max`` before ``min`` across instances. The instance-count check (the set of
covered ``service_instance_id`` equals U), every ``minimum_samples`` and every
threshold are unchanged.

The evaluator judges raw Prometheus bodies, so the coverage body has to come
from somewhere: :class:`FakePrometheus` evaluates the *planned* coverage and
instances expressions over raw per-series sample timestamps with PromQL
semantics for the few shapes involved (``count_over_time`` over a selector,
``max by``/``count by`` grouping, outer ``min``). It never hard-codes a value,
and it also evaluates the old ``min(count_over_time(...))`` shape so the
scenario is shown to discriminate between the two.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pytest

from opspilot.observation.release_types import ReleaseEvaluation
from tests.f11_release_support import (
    NAMESPACE,
    PROFILE,
    SERVICE,
    SLICE_SIGNALS,
    WINDOW,
    EvidenceBuilder,
    Pod,
    planned_texts,
    pods_for,
)
from tests.test_f11_release_evaluation import (
    AFTER_IMAGES,
    WINDOW_END,
    _assert_unknown,
    _context,
    _evaluate,
    _sufficient,
)

W_SECONDS = int(WINDOW.total_seconds())
CALLS = "traces_span_metrics_calls_total"
DURATION = "traces_span_metrics_duration_milliseconds_bucket"
SPAN_NAME = "oteldemo.CheckoutService/PlaceOrder"
LE_BUCKETS = ("100", "500", "+Inf")


def _minimum(signal: str) -> int:
    spec = PROFILE.signal(signal)
    assert spec is not None
    return spec.minimum_samples


# --- a tiny PromQL evaluator for the planned coverage/instances shapes ------

_MATCHER = re.compile(
    r'\s*([A-Za-z_][A-Za-z0-9_]*)\s*(=~|!~|!=|=)\s*"((?:[^"\\]|\\.)*)"\s*,?'
)
_CORE = re.compile(
    r"^\s*count_over_time\s*\(\s*([A-Za-z_:][A-Za-z0-9_:]*)\s*\{(.*)\}"
    r"\s*\[\s*(\d+)s\s*\]\s*\)\s*$",
    re.S,
)
_PREFIX_GROUP = re.compile(r"^\s*(max|count)\s+by\s*\(([^)]*)\)\s*\((.*)\)\s*$", re.S)
_SUFFIX_GROUP = re.compile(r"^\s*(max|count)\s*\((.*)\)\s*by\s*\(([^)]*)\)\s*$", re.S)
_OUTER_MIN = re.compile(r"^\s*min\s*\((.*)\)\s*$", re.S)


def _string_literal(text: str) -> str:
    """A PromQL double-quoted string body -> its value (``\\x`` -> ``x``)."""
    return re.sub(r"\\(.)", r"\1", text)


@dataclass(frozen=True)
class Core:
    metric: str
    matchers: tuple[tuple[str, str, str], ...]
    range_seconds: int


@dataclass(frozen=True)
class Shape:
    """``[min(] [agg by (labels)] (count_over_time(sel[Ws])) [)]``."""

    outer_min: bool
    aggregation: str | None
    group_labels: tuple[str, ...]
    core: Core


def parse_core(text: str) -> Core:
    match = _CORE.match(text)
    assert match, f"not count_over_time(<selector>[Ns]): {text!r}"
    metric, body, seconds = match.groups()
    matchers: list[tuple[str, str, str]] = []
    position = 0
    while position < len(body):
        item = _MATCHER.match(body, position)
        assert item, f"unparseable matcher at {body[position:]!r}"
        name, op, literal = item.groups()
        matchers.append((name, op, _string_literal(literal)))
        position = item.end()
    return Core(metric, tuple(matchers), int(seconds))


def parse_shape(text: str) -> Shape:
    outer = _OUTER_MIN.match(text)
    inner = outer.group(1) if outer else text
    aggregation: str | None = None
    labels: tuple[str, ...] = ()
    grouped = _PREFIX_GROUP.match(inner)
    if grouped:
        aggregation, raw_labels, inner = grouped.groups()
    else:
        grouped = _SUFFIX_GROUP.match(inner)
        if grouped:
            aggregation, inner, raw_labels = grouped.groups()
    if grouped:
        labels = tuple(x.strip() for x in raw_labels.split(",") if x.strip())
    return Shape(outer is not None, aggregation, labels, parse_core(inner))


@dataclass(frozen=True)
class Series:
    labels: Mapping[str, str]  # including ``__name__``
    stamps: tuple[float, ...]  # raw sample timestamps (epoch seconds)


def _selects(core: Core, labels: Mapping[str, str]) -> bool:
    if labels.get("__name__") != core.metric:
        return False
    for name, op, value in core.matchers:
        actual = labels.get(name, "")
        if op == "=" and actual != value:
            return False
        if op == "!=" and actual == value:
            return False
        if op == "=~" and not re.fullmatch(value, actual):
            return False
        if op == "!~" and re.fullmatch(value, actual):
            return False
    return True


class FakePrometheus:
    """Evaluates the parsed shapes at ``at`` over raw series (range ``(at-W, at]``)."""

    def __init__(self, series: Sequence[Series], at: datetime) -> None:
        self.series, self.at = tuple(series), at.timestamp()

    def count_over_time(self, core: Core) -> list[tuple[dict[str, str], float]]:
        start = self.at - core.range_seconds
        out = []
        for s in self.series:
            if not _selects(core, s.labels):
                continue
            n = sum(1 for t in s.stamps if start < t <= self.at)
            if n:  # no point in range -> no output series
                labels = {k: v for k, v in s.labels.items() if k != "__name__"}
                out.append((labels, float(n)))
        return out

    def evaluate(self, text: str) -> list[tuple[dict[str, str], float]]:
        shape = parse_shape(text)
        vector = self.count_over_time(shape.core)
        if shape.aggregation is not None:
            groups: dict[tuple[tuple[str, str], ...], list[float]] = {}
            for labels, value in vector:
                key = tuple((k, labels[k]) for k in shape.group_labels if k in labels)
                groups.setdefault(key, []).append(value)
            fold = max if shape.aggregation == "max" else len
            vector = [
                (dict(key), float(fold(values)))  # type: ignore[operator]
                for key, values in groups.items()
            ]
        if shape.outer_min:
            return [({}, min(v for _, v in vector))] if vector else []
        return vector


# --- raw span-metric series of the after instances --------------------------


def _stamps(count: int, at: datetime = WINDOW_END) -> tuple[float, ...]:
    """``count`` scrapes strictly inside ``(at - W, at]``, newest first."""
    assert count * 30 + 10 < W_SECONDS
    return tuple(
        (at - timedelta(seconds=10 + 30 * k)).timestamp() for k in range(count)
    )


def _span_series(pod: Pod, *, unset: int, error: int) -> list[Series]:
    """One instance's calls/duration series split by status_code (and ``le``);
    a status with 0 points has no series at all (it never appeared)."""
    base = {
        "k8s_namespace_name": NAMESPACE,
        "service_name": SERVICE,
        "service_instance_id": str(pod.uid),
        "k8s_pod_name": pod.name,
        "k8s_pod_uid": str(pod.uid),
    }
    out: list[Series] = []
    for status, count in (("STATUS_CODE_UNSET", unset), ("STATUS_CODE_ERROR", error)):
        if count == 0:
            continue
        stamps = _stamps(count)
        server = {**base, "span_kind": "SPAN_KIND_SERVER", "span_name": SPAN_NAME}
        out.append(Series({"__name__": CALLS, **server, "status_code": status}, stamps))
        for le in LE_BUCKETS:
            out.append(
                Series(
                    {"__name__": DURATION, **server, "status_code": status, "le": le},
                    stamps,
                )
            )
        client = {
            **base,
            "span_kind": "SPAN_KIND_CLIENT",
            "span_name": "oteldemo.PaymentService/Charge",
        }
        out.append(Series({"__name__": CALLS, **client, "status_code": status}, stamps))
    return out


def _scenario(
    counts: Sequence[tuple[int, int] | None],
    *,
    after: Mapping[str, float] | None = None,
) -> tuple[Any, EvidenceBuilder, FakePrometheus, dict[str, str]]:
    """``counts[i]`` = (UNSET points, ERROR points) of pod ``i``; ``None`` = the
    pod is mapped but has no span series. Coverage and instances bodies of
    every after signal are the fake's evaluation of the planned text."""
    pods = pods_for(AFTER_IMAGES, len(counts))
    context, pods, builder = _sufficient(pods=pods, after_values=after)
    plan = planned_texts(context, WINDOW_END, [p.uid for p in pods])
    raw: list[Series] = []
    for pod, count in zip(pods, counts, strict=True):
        if count is not None:
            raw.extend(_span_series(pod, unset=count[0], error=count[1]))
    fake = FakePrometheus(raw, WINDOW_END)
    for signal in SLICE_SIGNALS:
        for part in ("coverage", "instances"):
            key = f"after:{signal}:{part}"
            builder.results[key] = builder.series(key, fake.evaluate(plan[key]))
    return context, builder, fake, plan


def _coverage(fake: FakePrometheus, plan: Mapping[str, str], signal: str) -> float:
    result = fake.evaluate(plan[f"after:{signal}:coverage"])
    assert len(result) == 1 and result[0][0] == {}, result
    return result[0][1]


def _old_coverage(fake: FakePrometheus, plan: Mapping[str, str], signal: str) -> float:
    """The pre-r8 ``min(count_over_time(<same selector>[Ws]))``."""
    core = parse_shape(plan[f"after:{signal}:coverage"]).core
    matchers = ",".join(
        f'{n}{op}"{v.replace(chr(92), chr(92) * 2)}"' for n, op, v in core.matchers
    )
    old = f"min(count_over_time({core.metric}{{{matchers}}}[{core.range_seconds}s]))"
    return fake.evaluate(old)[0][1]


def _insufficient_after(evaluation: ReleaseEvaluation) -> list[str]:
    return [
        c
        for c in evaluation.reason_codes
        if c.startswith("INSUFFICIENT_SAMPLES:after:")
    ]


# ===========================================================================
# 1. plan shape
# ===========================================================================


def test_after_coverage_is_min_over_instances_of_max_per_instance() -> None:
    pods = pods_for(AFTER_IMAGES, 2)
    plan = planned_texts(_context(), WINDOW_END, [p.uid for p in pods])
    for signal in SLICE_SIGNALS:
        coverage = plan[f"after:{signal}:coverage"]
        instances = plan[f"after:{signal}:instances"]
        assert re.match(r"^\s*min\s*\(", coverage), coverage
        assert re.search(r"max\s+by\s*\(|max\s*\(.*\)\s*by\s*\(", coverage, re.S), (
            coverage
        )
        assert "count_over_time(" in "".join(coverage.split()), coverage
        assert "service_instance_id=~" in "".join(coverage.split()), coverage

        shape = parse_shape(coverage)
        assert shape.outer_min and shape.aggregation == "max", (signal, shape)
        assert shape.core.range_seconds == W_SECONDS, (signal, coverage)

        counted = parse_shape(instances)
        assert counted.aggregation == "count" and not counted.outer_min, instances
        assert set(shape.group_labels) == set(counted.group_labels), (signal,)
        assert "service_instance_id" in shape.group_labels, (signal, shape)
        # the request/duration selector, never an error-only series
        assert shape.core.metric in (CALLS, DURATION), (signal, shape.core.metric)
        assert set(shape.core.matchers) == set(counted.core.matchers), signal
        assert all(name != "status_code" for name, _, _ in shape.core.matchers)
        uid_matchers = [
            v for n, op, v in shape.core.matchers if n == "service_instance_id"
        ]
        assert len(uid_matchers) == 1 and [
            op for n, op, _ in shape.core.matchers if n == "service_instance_id"
        ] == ["=~"]
        for pod in pods:
            assert re.fullmatch(uid_matchers[0], str(pod.uid)), (signal, uid_matchers)


# ===========================================================================
# 2. regression: a newly appeared error series does not pull coverage down
# ===========================================================================


def test_the_scenario_discriminates_old_and_new_coverage() -> None:
    _, _, fake, plan = _scenario([(5, 1)])
    for signal in SLICE_SIGNALS:
        assert _coverage(fake, plan, signal) == 5.0, signal
        assert _old_coverage(fake, plan, signal) == 1.0, signal
        assert _minimum(signal) == 3


def test_a_new_error_series_with_one_point_is_not_insufficient_and_degrades() -> None:
    context, builder, _, _ = _scenario([(5, 1)], after={"error_ratio": 0.5})

    evaluation = _evaluate(context, builder)

    assert _insufficient_after(evaluation) == [], evaluation.reason_codes
    assert evaluation.verdict == "degraded", evaluation.reason_codes
    assert evaluation.evidence_sufficient is True


def test_a_new_error_series_with_one_point_keeps_a_healthy_sample_healthy() -> None:
    context, builder, _, _ = _scenario([(5, 1)])

    evaluation = _evaluate(context, builder)

    assert _insufficient_after(evaluation) == [], evaluation.reason_codes
    assert evaluation.verdict == "healthy", evaluation.reason_codes


# ===========================================================================
# 3. a real per-instance shortfall is still insufficient
# ===========================================================================


@pytest.mark.parametrize("error_points", [0, 1])
def test_an_instance_with_too_few_points_is_still_insufficient(
    error_points: int,
) -> None:
    context, builder, fake, plan = _scenario(
        [(2, error_points)], after={"error_ratio": 0.5}
    )
    for signal in SLICE_SIGNALS:
        assert _coverage(fake, plan, signal) == 2.0 < _minimum(signal)

    evaluation = _evaluate(context, builder)

    _assert_unknown(
        evaluation,
        *(f"INSUFFICIENT_SAMPLES:after:{s}" for s in SLICE_SIGNALS),
        outcome="no_data",
    )


def test_one_sufficient_and_one_short_instance_is_insufficient() -> None:
    context, builder, fake, plan = _scenario(
        [(5, 1), (2, 1)], after={"error_ratio": 0.5}
    )
    for signal in SLICE_SIGNALS:
        assert _coverage(fake, plan, signal) == 2.0, signal

    evaluation = _evaluate(context, builder)

    _assert_unknown(
        evaluation,
        *(f"INSUFFICIENT_SAMPLES:after:{s}" for s in SLICE_SIGNALS),
        outcome="no_data",
    )


def test_two_sufficient_instances_with_new_error_series_are_judged() -> None:
    context, builder, _, _ = _scenario([(5, 1), (4, 2)], after={"error_ratio": 0.5})

    evaluation = _evaluate(context, builder)

    assert _insufficient_after(evaluation) == [], evaluation.reason_codes
    assert evaluation.verdict == "degraded", evaluation.reason_codes


# ===========================================================================
# 4. the instance-count check still guards a mapped instance without telemetry
# ===========================================================================


def test_a_mapped_instance_without_coverage_is_still_unknown() -> None:
    context, builder, fake, plan = _scenario([(5, 1), None], after={"error_ratio": 0.5})
    # the per-instance min alone cannot see the silent instance ...
    for signal in SLICE_SIGNALS:
        assert _coverage(fake, plan, signal) == 5.0, signal
        assert len(fake.evaluate(plan[f"after:{signal}:instances"])) == 1

    evaluation = _evaluate(context, builder)

    # ... the unchanged instance-set check (r1 rule 5) still refuses it
    _assert_unknown(evaluation, "VERSION_INSTANCE_MISMATCH")
