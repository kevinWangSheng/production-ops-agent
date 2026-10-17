"""Independent acceptance and contract tests for F11 PR-2b (no database).

Written from ``docs/tasks/2026-10-13-f11-pr2-contract.md`` (「拆分」, r5
B01–B04 with B01 as revised by r8 -- the ``time`` parameter is floored to the
millisecond -- and 「5. PR-2b 遥测接口」) plus the pre-review fixed semantics
handed to the test author, without reading the PR-2b implementation:

* ``PrometheusReadOnlySource.instant_vector``: request shape, B01 ``time``
  parameter, parsing and error classification;
* the old scalar ``instant`` is unchanged (green on the current code);
* ``take_release_sample`` with a fake source and a fake store: B02 serial
  plan (mapping/context before after, after omitted without a mapping),
  lease budget, scope interruption, request counting, B03 failures;
* ``ObserverLoop.poll_once`` (B04): incident sweep, release sweep, shared
  claim, dispatch by lease type.

PR-2b names that do not exist yet make the dependent tests *fail* (not a
collection error) through the ``pr2b`` decorator.
"""

from __future__ import annotations

import calendar
import functools
import io
import json
import threading
import time
import urllib.error
import urllib.parse
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from decimal import ROUND_FLOOR, Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from uuid import UUID, uuid4

import pytest

from opspilot.domain.subjects import SubjectRef
from opspilot.observation.release_types import (
    ReleaseQueryResult,
    ReleaseSampleEvidence,
    ReleaseSampleLease,
    ReleaseSampleReceipt,
)
from opspilot.observation.store import SampleLease, SampleReceipt
from opspilot.observer.health_profile import canonical_content
from opspilot.observer.loop import ObserverLoop
from opspilot.observer.prometheus import (
    RESPONSE_LIMIT_BYTES,
    InstantResult,
    PrometheusReadOnlySource,
)
from opspilot.observer.release_evaluation import (
    ReleaseEvaluationContext,
    build_release_query_plan,
    evaluate_release_sample,
)
from opspilot.persistence.base import PersistenceError
from tests.f11_release_support import (
    PROFILE,
    PROFILE_REVISION,
    WINDOW,
    EvidenceBuilder,
    Pod,
    digest,
    pods_for,
)
from tests.test_f11_release_evaluation import AUTHORIZED_AT, _context
from tests.test_m1_observer import PROFILE as UNIT_PROFILE

try:  # PR-2b public names (公开接口 5)
    from opspilot.observer.prometheus import PrometheusSeries, VectorResult
    from opspilot.observer.release_sampling import take_release_sample

    _PR2B_MISSING: str | None = None
except ImportError as exc:  # implementation not merged yet: fail, do not error
    _PR2B_MISSING = f"{type(exc).__name__}: {exc}"

try:  # not a contract name; only used to compare PromQL leniently
    from opspilot.observer.release_evaluation import normalize_query
except ImportError:  # pragma: no cover

    def normalize_query(query: str) -> str:
        return "".join(query.split())


def pr2b(test: Callable[..., None]) -> Callable[..., None]:
    """Fail (not error) while the PR-2b names are missing."""

    @functools.wraps(test)
    def run(*args: Any, **kwargs: Any) -> None:
        if _PR2B_MISSING is not None:
            pytest.fail(f"PR2B_INTERFACE_MISSING: {_PR2B_MISSING}", pytrace=False)
        test(*args, **kwargs)

    return run


AFTER_IMAGES = {"checkout": digest("checkout-after")}
#: T: a window end with microseconds, fully after the authorization.
T = AUTHORIZED_AT + WINDOW + timedelta(seconds=40, microseconds=123456)
SENT_AT = T + timedelta(milliseconds=7)
PROFILE_TIMEOUT = PROFILE.query_timeout_seconds  # 20
SUBMIT_MARGIN = 5  # 剩余 lease − 5 秒


def _exact_epoch(at: datetime) -> Decimal:
    return Decimal(calendar.timegm(at.utctimetuple())) + Decimal(
        at.microsecond
    ) / Decimal(1_000_000)


# ===========================================================================
# 1. instant_vector: request, B01, parsing, error classes
# ===========================================================================


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200):
        self._buffer, self.status = io.BytesIO(body), status

    def read(self, n: int = -1) -> bytes:
        return self._buffer.read(n)

    def close(self) -> None:
        pass

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class FakeOpener:
    """The injected-opener seam the existing Observer tests use."""

    def __init__(
        self, body: bytes = b"", status: int = 200, error: Exception | None = None
    ):
        self.body, self.status, self.error = body, status, error
        self.requests: list[tuple[Any, Any]] = []

    def open(self, request: Any, timeout: Any = None) -> FakeResponse:
        self.requests.append((request, timeout))
        if self.error is not None:
            raise self.error
        return FakeResponse(self.body, self.status)


def _source(**kwargs: Any) -> tuple[PrometheusReadOnlySource, FakeOpener]:
    opener = FakeOpener(**kwargs)
    return (
        PrometheusReadOnlySource(
            "http://127.0.0.1:19090/", token="obs-token", opener=opener
        ),
        opener,
    )


def _body(
    result: Any, *, result_type: str = "vector", status: str = "success"
) -> bytes:
    return json.dumps(
        {"status": status, "data": {"resultType": result_type, "result": result}}
    ).encode()


STAMP = T.timestamp()
A = {
    "__name__": "kube_pod_owner",
    "namespace": "otel-demo",
    "pod": "checkout-a",
    "uid": "u-a",
}
B = {
    "__name__": "kube_pod_owner",
    "namespace": "otel-demo",
    "pod": "checkout-b",
    "uid": "u-b",
}


def _series_set(series: Sequence[Any]) -> set[tuple[Any, ...]]:
    return {(tuple(sorted(s.labels.items())), s.timestamp, s.value) for s in series}


@pr2b
def test_instant_vector_returns_every_series_with_labels_time_and_value() -> None:
    body = _body(
        [
            {"metric": A, "value": [STAMP, "1"]},
            {"metric": B, "value": [STAMP - 15, "2.5"]},
        ]
    )
    src, opener = _source(body=body)

    result = src.instant_vector("kube_pod_owner{}", at=T, timeout_seconds=9)

    assert isinstance(result, VectorResult)
    assert (result.status, result.http_status, result.body_complete) == (
        "ok",
        200,
        True,
    )
    assert result.expr == "kube_pod_owner{}"
    assert result.body == body
    assert result.sent_at is not None and result.sent_at.tzinfo is not None
    stamp = datetime.fromtimestamp(STAMP, timezone.utc)
    assert _series_set(result.series) == {
        (tuple(sorted(A.items())), stamp, 1.0),
        (
            tuple(sorted(B.items())),
            datetime.fromtimestamp(STAMP - 15, timezone.utc),
            2.5,
        ),
    }
    assert all(isinstance(s, PrometheusSeries) for s in result.series)
    # one GET to /api/v1/query with the Observer's own credential, no retry
    assert len(opener.requests) == 1
    request, _ = opener.requests[0]
    parts = urllib.parse.urlsplit(request.full_url)
    assert request.get_method() == "GET"
    assert (parts.scheme, parts.netloc, parts.path) == (
        "http",
        "127.0.0.1:19090",
        "/api/v1/query",
    )
    params = urllib.parse.parse_qs(parts.query)
    assert params["query"] == ["kube_pod_owner{}"]
    assert request.get_header("Authorization") == "Bearer obs-token"


#: r8 B01 修订: microsecond parts around every millisecond rounding edge.
B01_MICROS = [0, 499, 500, 123456, 999999, 999500, 743743]


def _sent_time(opener: FakeOpener) -> str:
    request, _ = opener.requests[0]
    (sent,) = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)[
        "time"
    ]
    return sent


def _assert_floored_to_milliseconds(sent: str, at: datetime) -> None:
    """r8 B01: the ``time`` parameter is ``at`` truncated (floored) to the
    millisecond -- never rounded, never after ``at``, less than 1 ms before it,
    written with exactly three decimals."""
    sent_value, exact = Decimal(sent), _exact_epoch(at)
    assert sent_value <= exact, (sent, exact)  # never after T
    assert exact - sent_value < Decimal("0.001"), (sent, exact)  # within 1 ms
    whole, _, fraction = sent.partition(".")
    assert whole.isdigit() and len(fraction) == 3 and fraction.isdigit(), sent
    # equivalently: the exact millisecond floor of T
    assert sent_value == exact.quantize(Decimal("0.001"), rounding=ROUND_FLOOR)


@pytest.mark.parametrize("micro", B01_MICROS)
@pr2b
def test_b01_instant_vector_time_parameter_is_floored_to_the_millisecond(
    micro: int,
) -> None:
    at = T.replace(microsecond=micro)
    src, opener = _source(body=_body([]))

    src.instant_vector("up", at=at, timeout_seconds=5)

    _assert_floored_to_milliseconds(_sent_time(opener), at)


@pytest.mark.parametrize("micro", B01_MICROS)
def test_b01_old_instant_time_parameter_is_floored_to_the_millisecond(
    micro: int,
) -> None:
    at = T.replace(microsecond=micro)
    src, opener = _source(body=_body([]))

    src.instant("up", at=at, timeout_seconds=5)

    _assert_floored_to_milliseconds(_sent_time(opener), at)


@pr2b
def test_an_empty_vector_is_no_data() -> None:
    src, _ = _source(body=_body([]))
    result = src.instant_vector("up", at=T, timeout_seconds=5)
    assert (result.status, result.series, result.http_status) == ("no_data", (), 200)


@pr2b
def test_identical_duplicate_series_are_deduplicated() -> None:
    row = {"metric": A, "value": [STAMP, "1"]}
    src, _ = _source(body=_body([row, dict(row)]))
    result = src.instant_vector("up", at=T, timeout_seconds=5)
    assert result.status == "ok"
    assert len(result.series) == 1
    assert dict(result.series[0].labels) == A and result.series[0].value == 1.0


_FAILED_BODIES = {
    "not_json": b"not json",
    "error_json_200": json.dumps(
        {"status": "error", "errorType": "bad_data", "error": "parse error"}
    ).encode(),
    "matrix": _body([{"metric": A, "values": [[STAMP, "1"]]}], result_type="matrix"),
    "scalar": json.dumps(
        {"status": "success", "data": {"resultType": "scalar", "result": [STAMP, "1"]}}
    ).encode(),
    "string": json.dumps(
        {"status": "success", "data": {"resultType": "string", "result": [STAMP, "x"]}}
    ).encode(),
    "result_not_list": _body({"metric": A, "value": [STAMP, "1"]}),
    "no_data_object": json.dumps({"status": "success"}).encode(),
    "metric_missing": _body([{"value": [STAMP, "1"]}]),
    "metric_not_object": _body([{"metric": ["x"], "value": [STAMP, "1"]}]),
    "label_not_string": _body([{"metric": {"pod": 3}, "value": [STAMP, "1"]}]),
    "value_missing": _body([{"metric": A}]),
    "value_wrong_length": _body([{"metric": A, "value": [STAMP]}]),
    "value_not_number": _body([{"metric": A, "value": [STAMP, "abc"]}]),
    "timestamp_not_number": _body([{"metric": A, "value": ["soon", "1"]}]),
    "nan": _body([{"metric": A, "value": [STAMP, "NaN"]}]),
    "plus_inf": _body([{"metric": A, "value": [STAMP, "+Inf"]}]),
    "minus_inf": _body([{"metric": A, "value": [STAMP, "-Inf"]}]),
    "nan_among_finite": _body(
        [{"metric": A, "value": [STAMP, "1"]}, {"metric": B, "value": [STAMP, "NaN"]}]
    ),
    "same_labels_same_time_other_value": _body(
        [
            {"metric": A, "value": [STAMP, "1"]},
            {"metric": dict(A), "value": [STAMP, "2"]},
        ]
    ),
    "same_labels_same_value_other_time": _body(
        [
            {"metric": A, "value": [STAMP, "1"]},
            {"metric": dict(A), "value": [STAMP - 1, "1"]},
        ]
    ),
}


@pytest.mark.parametrize("case", sorted(_FAILED_BODIES))
@pr2b
def test_a_malformed_non_vector_non_finite_or_conflicting_body_is_failed(
    case: str,
) -> None:
    body = _FAILED_BODIES[case]
    src, _ = _source(body=body)
    result = src.instant_vector("up", at=T, timeout_seconds=5)
    assert result.status == "failed", (case, result)
    assert result.series == ()
    assert result.http_status == 200 and result.body == body


@pr2b
def test_a_non_200_status_is_failed_and_records_the_status() -> None:
    src, opener = _source(
        error=urllib.error.HTTPError("u", 503, "unavailable", {}, None)
    )  # type: ignore[arg-type]
    result = src.instant_vector("up", at=T, timeout_seconds=5)
    assert (result.status, result.http_status, result.series) == ("failed", 503, ())
    assert len(opener.requests) == 1  # no internal retry


@pr2b
def test_a_transport_timeout_is_timeout() -> None:
    src, opener = _source(error=TimeoutError())
    result = src.instant_vector("up", at=T, timeout_seconds=1)
    assert (result.status, result.series) == ("timeout", ())
    assert len(opener.requests) == 1


@pr2b
def test_an_unreachable_source_is_failed() -> None:
    src, _ = _source(error=urllib.error.URLError(ConnectionRefusedError()))
    result = src.instant_vector("up", at=T, timeout_seconds=1)
    assert (result.status, result.series) == ("failed", ())


@pr2b
def test_a_body_over_the_size_limit_is_failed_incomplete_and_not_parsed() -> None:
    # a valid vector prefix that would parse if it were cut at the limit
    rows = [
        {
            "metric": {**A, "pod": f"checkout-{i}", "uid": f"u-{i}"},
            "value": [STAMP, "1"],
        }
        for i in range(400)
    ]
    body = _body(rows)
    assert len(body) > RESPONSE_LIMIT_BYTES
    src, _ = _source(body=body)
    result = src.instant_vector("up", at=T, timeout_seconds=5)
    assert result.status == "failed"
    assert result.body_complete is False
    assert result.series == ()
    assert len(result.body) <= RESPONSE_LIMIT_BYTES


class _Counting(BaseHTTPRequestHandler):
    paths: list[str] = []
    status = 302

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        type(self).paths.append(self.path)
        if self.path.startswith("/elsewhere"):
            body = _body([{"metric": A, "value": [STAMP, "1"]}])
            self.send_response(200)
        else:
            body = b'{"status":"error","errorType":"internal","error":"x"}'
            self.send_response(type(self).status)
            if type(self).status == 302:
                self.send_header("Location", "/elsewhere/api/v1/query?query=up")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.mark.parametrize("status", [302, 500])
@pr2b
def test_a_real_server_redirect_or_error_is_neither_followed_nor_retried(
    status: int,
) -> None:
    handler = type("Handler", (_Counting,), {"paths": [], "status": status})
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        src = PrometheusReadOnlySource(f"http://127.0.0.1:{server.server_port}")
        result = src.instant_vector("up", at=T, timeout_seconds=3)
    finally:
        server.shutdown()
        server.server_close()
    assert result.status == "failed" and result.http_status == status
    assert result.series == ()
    assert len(handler.paths) == 1 and handler.paths[0].startswith("/api/v1/query?")


# ===========================================================================
# 2. the old scalar instant is unchanged (green on the current code)
# ===========================================================================


def _many() -> bytes:
    return _body(
        [{"metric": A, "value": [STAMP, "1"]}, {"metric": B, "value": [STAMP, "2"]}]
    )


def test_old_instant_still_refuses_many_series() -> None:
    src, _ = _source(body=_many())
    result = src.instant("up", at=T, timeout_seconds=5)
    assert isinstance(result, InstantResult)
    assert (result.status, result.value, result.detail) == (
        "failed",
        None,
        "MANY_SERIES",
    )


def test_old_instant_still_reads_nan_as_no_data() -> None:
    src, _ = _source(body=_body([{"metric": {}, "value": [STAMP, "NaN"]}]))
    result = src.instant("up", at=T, timeout_seconds=5)
    assert (result.status, result.value) == ("no_data", None)


def test_old_instant_still_sends_a_millisecond_time_parameter() -> None:
    src, opener = _source(body=_body([{"metric": {}, "value": [STAMP, "3"]}]))
    result = src.instant("up", at=T, timeout_seconds=7)
    assert (result.status, result.value) == ("ok", 3.0)
    request, timeout = opener.requests[0]
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
    assert params["time"] == [f"{T.timestamp():.3f}"]
    assert params["timeout"] == ["7s"] and timeout == 7


# ===========================================================================
# 3. take_release_sample with a fake source and a fake store
# ===========================================================================


def _vector_result(
    expr: str, result: ReleaseQueryResult, sent_at: datetime | None
) -> Any:
    series: list[Any] = []
    if result.status == "ok":
        payload = json.loads(result.raw)
        for row in payload["data"]["result"]:
            stamp, value = row["value"]
            series.append(
                PrometheusSeries(
                    labels=dict(row["metric"]),
                    timestamp=datetime.fromtimestamp(float(stamp), timezone.utc),
                    value=float(value),
                )
            )
    return VectorResult(
        expr=expr,
        status=result.status,
        series=tuple(series),
        body=result.raw,
        http_status=result.http_status,
        detail=result.detail,
        body_complete=result.body_complete,
        sent_at=sent_at,
    )


class PlannedSource:
    """Answers the release query plan for ``pods`` with raw responses built by
    ``tests.f11_release_support`` at whatever ``at`` it is asked; anything
    unplanned (or everything, with ``mapping_ok=False`` for the mapping keys)
    is an empty vector. Records every call and the peak concurrency."""

    def __init__(
        self,
        context: ReleaseEvaluationContext,
        pods: Sequence[Pod],
        *,
        mapping_ok: bool = True,
        delay: float = 0.0,
        sent_at: datetime | None = SENT_AT,
    ) -> None:
        self.context, self.pods = context, tuple(pods)
        self.mapping_ok, self.delay, self.sent_at = mapping_ok, delay, sent_at
        self.calls: list[tuple[str, datetime, int]] = []
        self.returned: dict[str, bytes] = {}
        self.instant_calls = 0
        self._active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def plan(self, at: datetime) -> dict[str, str]:
        return build_release_query_plan(
            self.context, window_end=at, instance_uids=[p.uid for p in self.pods]
        )

    def key_of(self, expr: str, at: datetime) -> str | None:
        wanted = normalize_query(expr)
        for key, text in self.plan(at).items():
            if normalize_query(text) == wanted:
                return key
        return None

    def instant(
        self, expr: str, *, at: datetime, timeout_seconds: int
    ) -> InstantResult:
        self.instant_calls += 1
        raise AssertionError("release queries must use instant_vector (B02)")

    def instant_vector(self, expr: str, *, at: datetime, timeout_seconds: int) -> Any:
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            self.calls.append((expr, at, timeout_seconds))
            if self.delay:
                time.sleep(self.delay)
            key = self.key_of(expr, at)
            builder = EvidenceBuilder(window_end=at, texts=self.plan(at)).sufficient(
                self.pods
            )
            if key is None or (not self.mapping_ok and key.startswith("mapping:")):
                result = builder.series(key or "unplanned", [])
            else:
                result = builder.results[key]
            self.returned[expr] = result.raw
            return _vector_result(expr, result, self.sent_at)
        finally:
            with self._lock:
                self._active -= 1


class FakeReleaseStore:
    """Only what a release sample may touch: the DB clock, the scope check,
    the frozen context and ``submit_release_sample`` (which judges like the
    store: from the raw evidence, with the public evaluator)."""

    def __init__(
        self,
        context: ReleaseEvaluationContext,
        *,
        now: datetime = T,
        scope: Callable[[int], bool] | None = None,
        context_error: BaseException | None = None,
    ) -> None:
        self.context, self.now = context, now
        self.scope, self.scope_calls = scope, 0
        self.context_error = context_error
        self.submitted: list[tuple[ReleaseSampleLease, ReleaseSampleEvidence]] = []
        self.events: list[str] = []

    def current_time(self) -> datetime:
        return self.now

    def lease_scope_current(self, lease: Any) -> bool:
        self.scope_calls += 1
        return True if self.scope is None else self.scope(self.scope_calls)

    def release_context(self, session_id: UUID) -> ReleaseEvaluationContext:
        self.events.append("release_context")
        if self.context_error is not None:
            raise self.context_error
        assert session_id == self.context.session_id
        return self.context

    def submit_release_sample(
        self, lease: ReleaseSampleLease, evidence: ReleaseSampleEvidence
    ) -> ReleaseSampleReceipt:
        self.events.append("submit_release_sample")
        self.submitted.append((lease, evidence))
        evaluation = evaluate_release_sample(
            self.context,
            evidence,
            decision_at=evidence.window.end + timedelta(seconds=2),
        )
        return ReleaseSampleReceipt(
            sample_id=evidence.sample_id,
            session_id=lease.session_id,
            observation_id=lease.observation_id,
            replayed=False,
            accepted=True,
            disposition="adopted",
            reason="adopted",
            evaluation=evaluation,
            session_state="authorized",
            release_status="observing",
            used_sample_count=1,
            tracking_seconds=0.0,
            healthy_window_seconds=0.0,
            ending_id=None,
            next_sample_due_at=evidence.window.end + timedelta(seconds=60),
        )


def _lease(
    context: ReleaseEvaluationContext, *, lease_seconds: float = 120
) -> ReleaseSampleLease:
    oid = context.release.observation_id
    return ReleaseSampleLease(
        session_id=context.session_id,
        observation_id=oid,
        purpose="release_observation",
        subject=SubjectRef(kind="release_observation", id=str(oid)),
        job_id=uuid4(),
        sequence=1,
        owner=uuid4(),
        epoch=1,
        lease_until=T + timedelta(seconds=lease_seconds),
        subject_control_generation=0,
        observation_generation=1,
        health_profile_revision=PROFILE_REVISION,
        calibrated_policy_revision=context.policy.revision,
        deadline_at=context.release.deadline_at,
        sample_interval_seconds=PROFILE.session.sample_interval_seconds,
        adopted_window_end=None,
        global_suspension_generation=0,
        target_suspension_generation=0,
    )


def _setup(
    **source: Any,
) -> tuple[ReleaseEvaluationContext, tuple[Pod, ...], PlannedSource]:
    context = _context()
    pods = pods_for(AFTER_IMAGES)
    return context, pods, PlannedSource(context, pods, **source)


def _keys(evidence: ReleaseSampleEvidence) -> list[str]:
    return [q.query_key for q in evidence.queries]


def _is_mc(key: str) -> bool:
    return key.startswith(("mapping:", "context:"))


@pr2b
def test_a_release_sample_runs_the_whole_plan_serially_and_submits_raw_evidence() -> (
    None
):
    context, pods, source = _setup()
    store = FakeReleaseStore(context)
    plan = source.plan(T)
    assert any(k.startswith("after:") for k in plan) and len(plan) == 49

    taken = take_release_sample(_lease(context), source, store)

    assert (taken.requests_issued, taken.scope_interrupted, taken.budget_exhausted) == (
        len(plan),
        False,
        False,
    )
    assert taken.requests_issued == len(source.calls)
    assert source.instant_calls == 0
    assert source.max_active == 1  # B02: serial
    # every query at the same Prometheus time T, timeout = profile timeout
    assert {at for _, at, _ in source.calls} == {T}
    assert {timeout for _, _, timeout in source.calls} == {PROFILE_TIMEOUT}
    # Several plan keys share one query text (coverage/freshness reuse the
    # signal query), so compare issued texts as a multiset against the plan.
    issued = [normalize_query(expr) for expr, _, _ in source.calls]
    assert Counter(issued) == Counter(normalize_query(t) for t in plan.values())
    mc_texts = {normalize_query(t) for k, t in plan.items() if _is_mc(k)}
    after_texts = {normalize_query(t) for k, t in plan.items() if not _is_mc(k)}
    assert not mc_texts & after_texts
    # B02: every mapping:* and context:* before the first after:*
    first_after = min(i for i, t in enumerate(issued) if t in after_texts)
    assert all(t in mc_texts for t in issued[:first_after])
    assert all(t in after_texts for t in issued[first_after:])
    # scope checked before every request
    assert store.scope_calls >= taken.requests_issued
    # one submission: window [T-W, T], raw bytes as returned, planned text
    ((lease, evidence),) = store.submitted
    assert (evidence.window.start, evidence.window.end) == (T - WINDOW, T)
    assert sorted(_keys(evidence)) == sorted(plan)
    for query in evidence.queries:
        assert normalize_query(query.query) == normalize_query(plan[query.query_key])
        assert query.raw == source.returned[query.query]
        assert query.source == "prometheus"
        assert query.evaluated_at == T
        assert query.sent_at == SENT_AT
        assert (query.status, query.http_status, query.body_complete) == (
            "ok",
            200,
            True,
        )
    assert taken.receipt is not None and taken.receipt.sample_id == evidence.sample_id
    # the store's recomputation from these raw responses: a sufficient healthy sample
    assert taken.receipt.evaluation.evidence_sufficient is True
    assert taken.receipt.evaluation.verdict == "healthy"
    assert {i.pod_uid for i in taken.receipt.evaluation.instances} == {
        p.uid for p in pods
    }


@pr2b
def test_a_passed_scope_callable_is_asked_before_every_request() -> None:
    context, _, source = _setup()
    store = FakeReleaseStore(context)
    asked: list[ReleaseSampleLease] = []

    def scope(lease: ReleaseSampleLease) -> bool:
        asked.append(lease)
        return True

    lease = _lease(context)
    taken = take_release_sample(lease, source, store, scope_current=scope)
    assert len(asked) >= taken.requests_issued == len(source.calls) > 0
    assert all(item == lease for item in asked)


@pr2b
def test_without_a_mapping_the_after_queries_are_omitted() -> None:
    context, _, source = _setup(mapping_ok=False)
    store = FakeReleaseStore(context)

    taken = take_release_sample(_lease(context), source, store)

    keys = {source.key_of(expr, T) for expr, _, _ in source.calls}
    assert keys and all(k is not None and _is_mc(k) for k in keys)
    ((_, evidence),) = store.submitted
    expected = {k for k in source.plan(T) if _is_mc(k)}
    assert set(_keys(evidence)) == expected
    assert taken.requests_issued == len(expected) == 33
    assert not taken.scope_interrupted and not taken.budget_exhausted
    assert taken.receipt.evaluation.verdict == "unknown"


@pr2b
def test_a_scope_change_stops_requests_and_submits_what_was_collected() -> None:
    context, _, source = _setup()
    store = FakeReleaseStore(context, scope=lambda n: n <= 5)

    taken = take_release_sample(_lease(context), source, store)

    assert (taken.requests_issued, taken.scope_interrupted, taken.budget_exhausted) == (
        5,
        True,
        False,
    )
    assert len(source.calls) == 5
    ((_, evidence),) = store.submitted
    issued = {source.key_of(expr, T) for expr, _, _ in source.calls}
    # assumption (see report): the collected results only, nothing fabricated
    assert set(_keys(evidence)) == issued
    assert all(q.status == "ok" and q.sent_at == SENT_AT for q in evidence.queries)


@pr2b
def test_an_interruption_before_the_first_request_submits_a_zero_query_sample() -> None:
    context, _, source = _setup()
    store = FakeReleaseStore(context, scope=lambda n: False)

    taken = take_release_sample(_lease(context), source, store)

    assert (taken.requests_issued, taken.scope_interrupted, taken.budget_exhausted) == (
        0,
        True,
        False,
    )
    assert source.calls == []
    ((_, evidence),) = store.submitted
    assert evidence.queries == ()
    assert (evidence.window.start, evidence.window.end) == (T - WINDOW, T)
    assert taken.receipt.evaluation.verdict == "unknown"
    assert taken.receipt.evaluation.outcome == "no_data"


@pr2b
def test_each_request_timeout_is_bounded_by_the_whole_seconds_left_in_the_lease() -> (
    None
):
    context, _, source = _setup()
    store = FakeReleaseStore(context)
    # lease_until − T − 5 s margin = 7.5 s  ->  min(20, 7) = 7
    taken = take_release_sample(
        _lease(context, lease_seconds=SUBMIT_MARGIN + 7.5), source, store
    )
    assert taken.requests_issued == len(source.calls) > 0
    timeouts = [timeout for _, _, timeout in source.calls]
    assert timeouts[0] == 7
    assert all(1 <= timeout <= 7 for timeout in timeouts)


@pr2b
def test_no_budget_left_sends_nothing_and_records_the_plan_as_timeouts() -> None:
    context, _, source = _setup()
    store = FakeReleaseStore(context)

    taken = take_release_sample(
        _lease(context, lease_seconds=SUBMIT_MARGIN + 0.5), source, store
    )

    assert (taken.requests_issued, taken.scope_interrupted, taken.budget_exhausted) == (
        0,
        False,
        True,
    )
    assert source.calls == []
    ((_, evidence),) = store.submitted
    expected = {k for k in source.plan(T) if _is_mc(k)}
    assert set(_keys(evidence)) == expected  # mapping failed: no after keys
    assert all(q.status == "timeout" and q.sent_at is None for q in evidence.queries)
    assert all(q.evaluated_at == T for q in evidence.queries)
    assert taken.receipt.evaluation.verdict == "unknown"
    assert taken.receipt.evaluation.outcome == "timeout"


@pr2b
def test_a_budget_running_out_mid_sample_stops_and_records_the_rest_as_timeouts() -> (
    None
):
    context, _, source = _setup(delay=0.8)
    store = FakeReleaseStore(context)

    taken = take_release_sample(
        _lease(context, lease_seconds=SUBMIT_MARGIN + 1.6), source, store
    )

    assert taken.budget_exhausted is True and taken.scope_interrupted is False
    assert 1 <= taken.requests_issued == len(source.calls) <= 2
    assert all(timeout == 1 for _, _, timeout in source.calls)
    ((_, evidence),) = store.submitted
    sent = {source.key_of(expr, T) for expr, _, _ in source.calls}
    assert set(_keys(evidence)) == {k for k in source.plan(T) if _is_mc(k)}
    for query in evidence.queries:
        if query.query_key in sent:
            assert query.sent_at == SENT_AT
        else:
            assert (query.status, query.sent_at) == ("timeout", None)


@pr2b
def test_b03_an_unreadable_context_raises_without_queries_or_submission() -> None:
    context, _, source = _setup()
    store = FakeReleaseStore(
        context, context_error=PersistenceError("STORAGE_UNAVAILABLE")
    )
    with pytest.raises(Exception):
        take_release_sample(_lease(context), source, store)
    assert source.calls == [] and store.submitted == []


@pr2b
def test_b03_a_plan_that_cannot_be_built_raises_without_queries_or_submission() -> None:
    supported = _context()
    unsupported = supported.model_copy(update={"health_profile": UNIT_PROFILE})
    with pytest.raises(ValueError):  # sanity: the public builder refuses it
        build_release_query_plan(unsupported, window_end=T, instance_uids=())
    source = PlannedSource(supported, pods_for(AFTER_IMAGES))
    store = FakeReleaseStore(unsupported)
    with pytest.raises(Exception):
        take_release_sample(_lease(supported), source, store)
    assert source.calls == [] and store.submitted == []


# ===========================================================================
# 4. B04: one Observer tick
# ===========================================================================


def _incident_lease() -> SampleLease:
    return SampleLease(
        session_id=uuid4(),
        incident_id=uuid4(),
        job_id=uuid4(),
        sequence=1,
        owner=uuid4(),
        epoch=1,
        lease_until=T + timedelta(seconds=120),
        subject_control_generation=0,
        observation_generation=1,
        health_profile_revision=UNIT_PROFILE.revision,
        deadline_at=T + timedelta(hours=1),
        sample_interval_seconds=60,
        adopted_window_end=None,
    )


class TickStore(FakeReleaseStore):
    def __init__(
        self,
        context: ReleaseEvaluationContext,
        leases: list[Any],
        *,
        fail: Sequence[str] = (),
    ) -> None:
        super().__init__(context)
        self.leases, self.fail = leases, set(fail)
        self.incident_submitted: list[Any] = []

    def sweep_expired_sessions(self, *, limit: int = 20) -> list[UUID]:
        self.events.append("sweep_expired_sessions")
        if "sessions" in self.fail:
            raise PersistenceError("STORAGE_UNAVAILABLE")
        return []

    def sweep_expired_releases(self, *, limit: int = 20) -> list[UUID]:
        self.events.append("sweep_expired_releases")
        if "releases" in self.fail:
            raise PersistenceError("STORAGE_UNAVAILABLE")
        return []

    def claim_due_samples(
        self, owner: UUID, *, limit: int = 20, lease_seconds: int = 120
    ) -> list[Any]:
        self.events.append("claim_due_samples")
        return [self.leases.pop(0)] if self.leases else []

    def health_profile(self, revision: str) -> dict[str, Any]:
        assert revision == UNIT_PROFILE.revision
        return {"content": canonical_content(UNIT_PROFILE)}

    def submit_sample(
        self, lease: SampleLease, sample: Any, readings: Any
    ) -> SampleReceipt:
        self.events.append("submit_sample")
        self.incident_submitted.append(lease)
        return SampleReceipt(
            sample_id=uuid4(),
            accepted=True,
            disposition="adopted",
            reason="adopted",
            confirms_health=False,
            health_basis="outcome_not_healthy",
            session_state="authorized",
            incident_lifecycle="observing_recovery",
            transition=None,
            next_sample_due_at=None,
        )


class TickSource(PlannedSource):
    def instant(
        self, expr: str, *, at: datetime, timeout_seconds: int
    ) -> InstantResult:
        self.instant_calls += 1
        empty = _body([])
        return InstantResult(expr, "no_data", None, empty, 200)


_TICK = ("sweep_expired_sessions", "sweep_expired_releases", "claim_due_samples")


@pytest.mark.parametrize(
    "fail", [(), ("sessions",), ("releases",), ("sessions", "releases")]
)
@pr2b
def test_b04_a_tick_sweeps_incidents_then_releases_then_dispatches_shared_claims(
    fail: tuple[str, ...],
) -> None:
    context = _context()
    pods = pods_for(AFTER_IMAGES)
    incident, release = _incident_lease(), _lease(context)
    store = TickStore(context, [incident, release], fail=fail)
    source = TickSource(context, pods)
    loop = ObserverLoop(store=store, source=source, batch=5)  # type: ignore[arg-type]

    loop.poll_once()

    events = [
        e
        for e in store.events
        if e in (*_TICK, "submit_sample", "submit_release_sample")
    ]
    assert events[:2] == ["sweep_expired_sessions", "sweep_expired_releases"]
    assert events[2:] == [
        "claim_due_samples",
        "submit_sample",
        "claim_due_samples",
        "submit_release_sample",
        "claim_due_samples",
    ]
    # each lease went to its own sampler
    assert store.incident_submitted == [incident]
    ((submitted_lease, evidence),) = store.submitted
    assert submitted_lease == release
    assert source.instant_calls > 0  # the incident sample used the scalar path
    assert len(source.calls) == len(evidence.queries) == 49  # release: instant_vector


@pr2b
def test_b04_a_crashing_release_sample_does_not_stop_the_tick() -> None:
    context = _context()
    release, incident = _lease(context), _incident_lease()
    store = TickStore(context, [release, incident])
    store.context_error = PersistenceError("STORAGE_UNAVAILABLE")  # B03 path
    loop = ObserverLoop(store=store, source=TickSource(context, ()), batch=5)  # type: ignore[arg-type]

    loop.poll_once()

    assert store.submitted == []  # no fake zero-query sample for a B03 failure
    assert store.incident_submitted == [incident]
    assert store.events.count("claim_due_samples") == 3
