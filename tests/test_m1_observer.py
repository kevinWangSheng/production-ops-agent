"""The Observer process (M1-02 step 4, #86): sampler, source, isolation.

Contract under test: C3 §3 (the Observer's isolation reaches credentials and
imports, not only a process boundary; decision D3), §4 (the control scope is
checked before every request; a request in flight is not recalled), §10
(sample time after the queries, readings record success / no data / stale /
timeout / failure with the source, one atomic submission). No database here;
``tests/integration/test_m1_02_observer_postgres.py`` runs the same loop
against PostgreSQL under the Observer role.
"""

import ast
import base64
import hashlib
import io
import json
import pathlib
import subprocess
import sys
import threading
import time
import urllib.error
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from opspilot.domain.observation import HealthSample
from opspilot.observation.store import (
    READING_RAW_LIMIT,
    SampleLease,
    SampleReceipt,
    SignalReading,
)
from opspilot.observer import PROFILE_DIRECTORY, HealthProfile, load_health_profile
from opspilot.observer.__main__ import build_loop
from opspilot.observer.loop import ObserverLoop
from opspilot.observer.prometheus import (
    RESPONSE_LIMIT_BYTES,
    InstantResult,
    PrometheusReadOnlySource,
)
from opspilot.observer.sampler import replay_readings, take_sample
from opspilot.persistence.base import PersistenceError

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)

PROFILE = HealthProfile.model_validate(
    {
        "format_version": 1,
        "profile_id": "unit",
        "description": "unit profile",
        "subject": {"service": "svc", "kubernetes_namespace": "ns"},
        "source": "prometheus",
        "calibration_source": "unit test",
        "evaluation_window_seconds": 300,
        "freshness_seconds": 90,
        "query_timeout_seconds": 10,
        "session": {
            "deadline_seconds": 3600,
            "max_samples": 20,
            "sample_interval_seconds": 60,
            "sustained_window_seconds": 600,
        },
        "effective_traffic": {"signal": "rate", "minimum": 0.5},
        "signals": [
            {
                "name": "rate",
                "description": "traffic",
                "query": "sum(rate(x[5m]))",
                "coverage_query": "max(count_over_time(x[5m]))",
                "freshness_query": "max(timestamp(x))",
                "minimum_samples": 3,
                "traffic_dependent": False,
                "healthy": {"min": 0},
            },
            {
                "name": "errors",
                "description": "error ratio",
                "query": "e / t",
                "coverage_query": "max(count_over_time(t[5m]))",
                "freshness_query": "max(timestamp(t))",
                "minimum_samples": 3,
                "traffic_dependent": True,
                "healthy": {"min": 0, "max": 0.01},
            },
        ],
    }
)


def vector(value) -> bytes:
    return json.dumps(
        {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [{"metric": {}, "value": [NOW.timestamp(), str(value)]}],
            },
        }
    ).encode()


EMPTY = json.dumps(
    {"status": "success", "data": {"resultType": "vector", "result": []}}
).encode()


class FakeSource:
    """Instant results by expression; records every request in order."""

    def __init__(self, answers: dict[str, InstantResult]):
        self.answers = answers
        self.requests: list[tuple[str, datetime]] = []

    def instant(self, expr, *, at, timeout_seconds):
        self.requests.append((expr, at))
        answer = self.answers[expr]
        return InstantResult(
            expr,
            answer.status,
            answer.value,
            answer.body,
            200,
            answer.detail,
            body_complete=answer.body_complete,
        )


def ok(value: float) -> InstantResult:
    return InstantResult("", "ok", value, vector(value), 200)


def no_data() -> InstantResult:
    return InstantResult("", "no_data", None, EMPTY, 200)


def healthy_answers(latest=None):
    latest = NOW - timedelta(seconds=30) if latest is None else latest
    return {
        "sum(rate(x[5m]))": ok(2.0),
        "max(count_over_time(x[5m]))": ok(5),
        "max(timestamp(x))": ok(latest.timestamp()),
        "e / t": ok(0.0),
        "max(count_over_time(t[5m]))": ok(5),
        "max(timestamp(t))": ok(latest.timestamp()),
    }


class FakeStore:
    """Only what ``take_sample`` touches: the clock, the scope check, submit."""

    def __init__(self, *, clock=None, scope=None):
        self.clock = clock or [NOW, NOW + timedelta(seconds=3)]
        self.scope_answers = scope
        self.scope_calls = 0
        self.submitted: list[tuple[SampleLease, HealthSample, list[SignalReading]]] = []

    def current_time(self):
        return self.clock.pop(0) if len(self.clock) > 1 else self.clock[0]

    def lease_scope_current(self, lease):
        self.scope_calls += 1
        if self.scope_answers is None:
            return True
        return self.scope_answers(self.scope_calls)

    def submit_sample(self, lease, sample, readings):
        self.submitted.append((lease, sample, list(readings)))
        return SampleReceipt(
            sample_id=uuid4(),
            accepted=True,
            disposition="adopted",
            reason="adopted",
            confirms_health=sample.outcome == "healthy",
            health_basis="confirmed"
            if sample.outcome == "healthy"
            else "outcome_not_healthy",
            session_state="authorized",
            incident_lifecycle="observing_recovery",
            transition=None,
            next_sample_due_at=None,
        )


def lease(revision=PROFILE.revision) -> SampleLease:
    return SampleLease(
        session_id=uuid4(),
        incident_id=uuid4(),
        job_id=uuid4(),
        sequence=1,
        owner=uuid4(),
        epoch=1,
        lease_until=NOW + timedelta(seconds=120),
        subject_control_generation=0,
        observation_generation=1,
        health_profile_revision=revision,
        deadline_at=NOW + timedelta(hours=1),
        sample_interval_seconds=60,
        adopted_window_end=None,
    )


# --- one sample: queries, window, sample time, readings, raw bundle


def test_a_sample_queries_every_signal_three_times_at_the_window_end_and_submits():
    source = FakeSource(healthy_answers())
    store = FakeStore()

    taken = take_sample(lease(), PROFILE, source, store)

    assert taken.requests_issued == 6 and not taken.scope_interrupted
    assert [expr for expr, _ in source.requests] == [
        "sum(rate(x[5m]))",
        "max(count_over_time(x[5m]))",
        "max(timestamp(x))",
        "e / t",
        "max(count_over_time(t[5m]))",
        "max(timestamp(t))",
    ]
    assert {at for _, at in source.requests} == {NOW}
    # the scope is checked before every request
    assert store.scope_calls == 6
    ((_, sample, readings),) = store.submitted
    assert (sample.window.start, sample.window.end) == (NOW - timedelta(minutes=5), NOW)
    assert (sample.outcome, sample.required_signals_present) == ("healthy", True)
    assert sample.sequence == 1 and sample.health_profile_revision == PROFILE.revision
    assert taken.evaluation is not None and taken.evaluation.outcome == "healthy"
    by_name = {reading.signal_name: reading for reading in readings}
    assert by_name["rate"].status == "ok" and by_name["rate"].value == 2.0
    assert by_name["rate"].sample_count == 5
    assert (
        by_name["rate"].query == "sum(rate(x[5m]))"
        and by_name["rate"].source == "prometheus"
    )
    # the raw bundle carries the exact response bytes and hashes to raw_sha256
    raw = by_name["rate"].raw
    assert raw is not None and len(raw) <= READING_RAW_LIMIT
    assert hashlib.sha256(raw).hexdigest() == by_name["rate"].raw_sha256
    bundle = json.loads(raw)
    assert base64.b64decode(bundle["query"]["body_b64"]) == vector(2.0)
    assert bundle["freshness"]["expr"] == "max(timestamp(x))"
    assert bundle["evaluated_at"] == NOW.isoformat()


def test_sample_time_is_taken_after_the_queries_return():
    """Issue #86 (PR #113 review): ``evaluate_readings`` judges a window from
    the future as stale and tolerates no skew, so the sample time must be
    read after the last query, never before."""
    before, after = NOW, NOW + timedelta(seconds=7)
    store = FakeStore(clock=[before, after])
    source = FakeSource(healthy_answers())

    taken = take_sample(lease(), PROFILE, source, store)

    assert taken.evaluation is not None and taken.evaluation.outcome == "healthy"
    # the window ended at the first clock read, the queries ran at it
    assert {at for _, at in source.requests} == {before}


def test_stopped_scrapes_make_the_reading_stale_and_the_sample_unknown():
    """Issue #86: the value and the point count still answer after scrapes
    stop; the newest raw sample timestamp does not. The stored reading says
    stale (value dropped, raw kept), the sample is stale and cannot confirm."""
    store = FakeStore()
    source = FakeSource(healthy_answers(latest=NOW - timedelta(seconds=91)))

    taken = take_sample(lease(), PROFILE, source, store)

    assert taken.evaluation is not None
    assert (taken.evaluation.outcome, taken.evaluation.required_signals_present) == (
        "stale",
        False,
    )
    ((_, sample, readings),) = store.submitted
    assert sample.outcome == "stale" and not sample.required_signals_present
    assert {reading.status for reading in readings} == {"stale"}
    assert all(
        reading.value is None and reading.raw is not None for reading in readings
    )


def test_too_few_raw_points_are_stored_as_no_data():
    answers = healthy_answers()
    answers["max(count_over_time(x[5m]))"] = ok(2)
    store = FakeStore()
    take_sample(lease(), PROFILE, FakeSource(answers), store)
    ((_, sample, readings),) = store.submitted
    rate = next(reading for reading in readings if reading.signal_name == "rate")
    assert (sample.outcome, rate.status, rate.value) == ("no_data", "no_data", None)


def test_no_data_timeout_and_failure_keep_their_status_and_raw():
    answers = healthy_answers()
    answers["sum(rate(x[5m]))"] = no_data()
    answers["e / t"] = InstantResult("", "timeout", None, b"", None, "TIMEOUT")
    store = FakeStore()
    taken = take_sample(lease(), PROFILE, FakeSource(answers), store)
    ((_, sample, readings),) = store.submitted
    by_name = {reading.signal_name: reading for reading in readings}
    assert by_name["rate"].status == "no_data" and by_name["errors"].status == "timeout"
    # worst unknown first: timeout > stale > no_data
    assert sample.outcome == "timeout" and taken.evaluation.outcome == "timeout"
    assert json.loads(by_name["errors"].raw)["query"]["detail"] == "TIMEOUT"


def test_a_scope_change_stops_the_requests_and_submits_the_partial_sample():
    """C3 §4: the scope is checked before every request; after it moved no
    further request is issued (the one in flight is not recalled) and what
    was gathered is submitted so the store can file it as suspended."""
    source = FakeSource(healthy_answers())
    # the 4th check (first request of the second signal) says the scope moved
    store = FakeStore(scope=lambda n: n < 4)

    taken = take_sample(lease(), PROFILE, source, store)

    assert taken.scope_interrupted and taken.requests_issued == 3
    assert len(source.requests) == 3
    ((_, sample, readings),) = store.submitted
    assert [reading.signal_name for reading in readings] == ["rate"]
    # the signal never queried is missing: unknown, never healthy
    assert (sample.outcome, sample.required_signals_present) == ("no_data", False)


def test_the_lease_must_carry_the_profile_revision_being_sampled():
    with pytest.raises(ValueError, match="PROFILE_REVISION_MISMATCH"):
        take_sample(lease("unit@000000000000"), PROFILE, FakeSource({}), FakeStore())


def test_an_incomplete_body_is_filed_as_failed_and_the_bundle_says_so():
    """PR #119 review P2-4: a truncated response is not the actual return.
    The reading fails closed (never covers), the stored bundle keeps the
    bytes that did arrive and marks the body incomplete; the bundle itself
    always fits the store's raw limit, so its hash is never a substitute's."""
    answers = healthy_answers()
    prefix = b'{"status":"success","data":{"resultType":"vector","result":[{"m'
    answers["sum(rate(x[5m]))"] = InstantResult(
        "", "failed", None, prefix, 200, "TOO_LARGE", body_complete=False
    )
    store = FakeStore()
    take_sample(lease(), PROFILE, FakeSource(answers), store)
    ((_, sample, readings),) = store.submitted
    rate = next(reading for reading in readings if reading.signal_name == "rate")
    assert rate.status == "failed" and rate.raw is not None
    bundle = json.loads(rate.raw)
    assert bundle["query"]["body_complete"] is False
    assert base64.b64decode(bundle["query"]["body_b64"]) == prefix
    assert bundle["query"]["detail"] == "TOO_LARGE"
    assert sample.outcome == "failed"


def test_three_full_size_bodies_always_fit_the_stored_raw_limit():
    big = vector(1.0) + b" " * (RESPONSE_LIMIT_BYTES - len(vector(1.0)))
    assert len(big) == RESPONSE_LIMIT_BYTES
    answers = {
        expr: InstantResult("", "ok", 2.0, big, 200) for expr in healthy_answers()
    }
    answers["max(timestamp(x))"] = InstantResult(
        "", "ok", (NOW - timedelta(seconds=30)).timestamp(), big, 200
    )
    answers["max(timestamp(t))"] = answers["max(timestamp(x))"]
    store = FakeStore()
    take_sample(lease(), PROFILE, FakeSource(answers), store)
    ((_, _, readings),) = store.submitted
    for reading in readings:
        assert reading.raw is not None and len(reading.raw) <= READING_RAW_LIMIT
        assert hashlib.sha256(reading.raw).hexdigest() == reading.raw_sha256


def test_the_bundle_records_the_sample_time_and_replay_judges_with_it():
    """PR #119 review P2-3: the instant the verdict judged freshness against
    is stored in every reading's bundle; ``replay_readings`` uses it and
    nothing else, so the same bytes judged at another time give another
    verdict only when the stored time differs."""
    latest = NOW - timedelta(seconds=89)
    store = FakeStore(clock=[NOW, NOW + timedelta(seconds=1)])
    take_sample(lease(), PROFILE, FakeSource(healthy_answers(latest=latest)), store)
    ((_, sample, readings),) = store.submitted
    assert sample.outcome == "healthy"
    rows = [
        {
            "signal_name": r.signal_name,
            "query": r.query,
            "source": r.source,
            "window_start": r.window_start,
            "window_end": r.window_end,
            "raw": r.raw,
            "raw_sha256": r.raw_sha256,
        }
        for r in readings
    ]
    for row in rows:
        assert (
            json.loads(row["raw"])["sample_time"]
            == (NOW + timedelta(seconds=1)).isoformat()
        )
    replayed = replay_readings(PROFILE, rows)
    assert (replayed.outcome, replayed.required_signals_present) == ("healthy", True)
    # the same bodies, judged two seconds later: stale -- proven by rewriting
    # only the recorded instant (the hash is recomputed, as the store would
    # have stored it had the Observer judged then)
    later_rows = []
    for row in rows:
        bundle = json.loads(row["raw"])
        bundle["sample_time"] = (NOW + timedelta(seconds=2)).isoformat()
        raw = json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode()
        later_rows.append(
            {**row, "raw": raw, "raw_sha256": hashlib.sha256(raw).hexdigest()}
        )
    assert replay_readings(PROFILE, later_rows).outcome == "stale"
    # a tampered bundle (hash no longer matches) never counts
    broken = [{**rows[0], "raw": rows[0]["raw"] + b" "}, rows[1]]
    assert replay_readings(PROFILE, broken).outcome == "failed"


def test_a_stale_dependency_among_fresh_ones_blocks_health():
    """PR #119 review P2-1: a signal over several series is as fresh as its
    stalest required series. The shipped profile asks Prometheus for the
    oldest of the newest samples (``min(timestamp(...))``); seven fresh
    dependencies and one 120 s old give an old timestamp, the reading is
    stale and the sample cannot confirm or extend health."""
    shipped = load_health_profile(PROFILE_DIRECTORY / "otel-demo-checkout.json")
    assert all(s.freshness_query.startswith("min(timestamp(") for s in shipped.signals)
    dependency = shipped.signal("dependency_deployments_available")
    assert dependency is not None
    fresh = (NOW - timedelta(seconds=20)).timestamp()
    stalest = (NOW - timedelta(seconds=120)).timestamp()
    answers: dict[str, InstantResult] = {}
    healthy = {
        "deployment_available_replicas": 1.0,
        "request_rate_per_second": 0.0125,
        "error_ratio": 0.0,
        "latency_p95_milliseconds": 100.0,
        "pods_running": 1.0,
        "pod_restarts_in_window": 0.0,
        "dependency_deployments_available": 8.0,  # all eight still report
        "dependency_error_ratio": 0.0,
    }
    for signal in shipped.signals:
        answers[signal.query] = ok(healthy[signal.name])
        answers[signal.coverage_query] = ok(5)
        # min over the eight dependencies is the stale one; every other
        # signal's series are fresh
        answers[signal.freshness_query] = ok(
            stalest if signal.name == dependency.name else fresh
        )
    store = FakeStore()
    taken = take_sample(lease(shipped.revision), shipped, FakeSource(answers), store)
    assert taken.evaluation is not None
    assert (taken.evaluation.outcome, taken.evaluation.required_signals_present) == (
        "stale",
        False,
    )
    verdict = next(
        v for v in taken.evaluation.verdicts if v.signal_name == dependency.name
    )
    assert verdict.verdict == "stale"
    ((_, sample, readings),) = store.submitted
    assert sample.outcome == "stale" and not sample.required_signals_present
    dep_row = next(r for r in readings if r.signal_name == dependency.name)
    assert dep_row.status == "stale" and dep_row.value is None
    assert sum(1 for r in readings if r.status == "ok") == 7


# --- the Prometheus source: one instant query, bounded, GET only


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200):
        self._buffer, self.status = io.BytesIO(body), status

    def read(self, n: int = -1) -> bytes:
        return self._buffer.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, body=b"", status=200, error=None):
        self.body, self.status, self.error = body, status, error
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        if self.error is not None:
            raise self.error
        return FakeResponse(self.body, self.status)


def source(**kwargs):
    opener = FakeOpener(**kwargs)
    return PrometheusReadOnlySource(
        "http://127.0.0.1:19090/", token=None, opener=opener
    ), opener


@pytest.mark.parametrize(
    "body, expected",
    [
        (vector(1.5), ("ok", 1.5, "")),
        (EMPTY, ("no_data", None, "")),
        (vector("NaN"), ("no_data", None, "")),
        (b"not json", ("failed", None, "NOT_JSON")),
        (
            json.dumps({"status": "error", "errorType": "bad_data"}).encode(),
            ("failed", None, "STATUS_NOT_SUCCESS"),
        ),
        (
            json.dumps(
                {"status": "success", "data": {"resultType": "matrix", "result": []}}
            ).encode(),
            ("failed", None, "NOT_A_VECTOR"),
        ),
        (
            json.dumps(
                {
                    "status": "success",
                    "data": {
                        "resultType": "vector",
                        "result": [{"value": [1, "1"]}, {"value": [1, "2"]}],
                    },
                }
            ).encode(),
            ("failed", None, "MANY_SERIES"),
        ),
    ],
)
def test_instant_query_parses_exactly_one_sample(body, expected):
    src, opener = source(body=body)
    result = src.instant("up", at=NOW, timeout_seconds=10)
    assert (result.status, result.value, result.detail) == expected
    assert result.body == body and result.http_status == 200
    request, timeout = opener.requests[0]
    assert request.get_method() == "GET" and timeout == 10
    assert request.full_url.startswith("http://127.0.0.1:19090/api/v1/query?")
    assert (
        f"time={NOW.timestamp():.3f}" in request.full_url
        and "timeout=10s" in request.full_url
    )
    assert not request.has_header("Authorization")


def test_instant_query_reports_timeout_unreachable_http_error_and_size():
    src, _ = source(error=TimeoutError())
    assert src.instant("up", at=NOW, timeout_seconds=1).status == "timeout"
    src, _ = source(error=urllib.error.URLError(ConnectionRefusedError()))
    result = src.instant("up", at=NOW, timeout_seconds=1)
    assert (result.status, result.detail) == ("failed", "UNREACHABLE")
    src, _ = source(error=urllib.error.HTTPError("u", 422, "bad", {}, None))
    result = src.instant("up", at=NOW, timeout_seconds=1)
    assert (result.status, result.http_status, result.detail) == ("failed", 422, "HTTP")
    src, _ = source(body=b"x" * (RESPONSE_LIMIT_BYTES + 1))
    result = src.instant("up", at=NOW, timeout_seconds=1)
    assert (result.status, result.detail) == ("failed", "TOO_LARGE")
    assert len(result.body) < RESPONSE_LIMIT_BYTES


def test_the_token_is_the_observers_own_and_sent_as_bearer_only():
    opener = FakeOpener(body=EMPTY)
    src = PrometheusReadOnlySource("http://prom", token="obs-token", opener=opener)
    src.instant("up", at=NOW, timeout_seconds=1)
    request, _ = opener.requests[0]
    assert request.get_header("Authorization") == "Bearer obs-token"
    with pytest.raises(ValueError, match="PROMETHEUS_URL_INVALID"):
        PrometheusReadOnlySource("file:///etc/passwd")


def test_a_url_with_userinfo_is_refused_and_logs_see_only_the_endpoint(caplog):
    """PR #119 review P2-6: a credential in the URL would be logged at start
    and stored in evidence; it is refused outright, and what the loop logs is
    scheme, host and port."""
    with pytest.raises(ValueError, match="PROMETHEUS_URL_HAS_USERINFO"):
        PrometheusReadOnlySource("https://observer:example-secret@prometheus.invalid")
    with pytest.raises(SystemExit, match="must be an http"):
        build_loop(
            {
                "OPSPILOT_OBSERVER_DSN": "host=db dbname=opspilot user=observer",
                "OPSPILOT_OBSERVER_PROMETHEUS_URL": "https://observer:example-secret@prometheus.invalid",
            },
            stop=threading.Event(),
        )
    src = PrometheusReadOnlySource("https://prometheus.invalid:9443/prefix/?x=1")
    assert src.endpoint == "https://prometheus.invalid:9443"
    with caplog.at_level("INFO", logger="opspilot.observer"):
        loop = build_loop(
            {
                "OPSPILOT_OBSERVER_DSN": "host=db dbname=opspilot user=observer",
                "OPSPILOT_OBSERVER_PROMETHEUS_URL": "https://prometheus.invalid:9443/prefix/?x=1",
            },
            stop=threading.Event(),
        )
    loop.store.close()
    assert "prometheus=https://prometheus.invalid:9443" in caplog.text
    assert "prefix" not in caplog.text and "x=1" not in caplog.text


def test_the_request_deadline_covers_a_slow_trickling_body():
    """PR #119 review P2-2: ``timeout_seconds`` is an absolute bound on the
    whole request. A server that sends a chunk every 0.6 s against a 1 s
    timeout is cut off as a timeout within about the timeout, not read to
    the end."""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Trickle(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for piece in (
                b'{"status":"success",',
                b'"data":{"resultType":"vector",',
                b'"result":[]}}',
            ):
                try:
                    self.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
                    self.wfile.flush()
                    time.sleep(0.6)
                except (BrokenPipeError, ConnectionResetError):
                    return
            try:
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                return

        def log_message(self, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), Trickle)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        src = PrometheusReadOnlySource(f"http://127.0.0.1:{server.server_port}")
        started = time.monotonic()
        result = src.instant("up", at=NOW, timeout_seconds=1)
        elapsed = time.monotonic() - started
    finally:
        server.shutdown()
    assert result.status == "timeout" and result.detail == "TIMEOUT"
    assert not result.body_complete
    assert elapsed < 1.6, elapsed


def _slow_server(handler_factory):
    from http.server import HTTPServer

    server = HTTPServer(("127.0.0.1", 0), handler_factory)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _timed_instant(server, timeout_seconds=1):
    src = PrometheusReadOnlySource(f"http://127.0.0.1:{server.server_port}")
    started = time.monotonic()
    try:
        result = src.instant("up", at=NOW, timeout_seconds=timeout_seconds)
        elapsed = time.monotonic() - started
    finally:
        # the single-threaded test server finishes its sleeps before
        # shutdown returns; that wait is the server's, not the client's
        server.shutdown()
    return result, elapsed


def test_the_request_deadline_covers_slow_response_headers():
    """PR #119 recheck: a server that sends nothing (or trickles header lines)
    for longer than the timeout is cut off at the deadline, not after each
    socket wait resets."""
    from http.server import BaseHTTPRequestHandler

    class SlowHeaders(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            try:
                # status line, then a header line every 0.6 s: each byte
                # resets a plain socket timeout, only a deadline stops it
                self.wfile.write(b"HTTP/1.1 200 OK\r\n")
                self.wfile.flush()
                for n in range(6):
                    time.sleep(0.6)
                    self.wfile.write(f"X-Slow-{n}: 1\r\n".encode())
                    self.wfile.flush()
                self.wfile.write(b"Content-Length: 0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        def log_message(self, *args):
            return

    result, elapsed = _timed_instant(_slow_server(SlowHeaders))
    assert result.status == "timeout" and result.detail == "TIMEOUT"
    assert elapsed < 1.6, elapsed


def test_the_request_deadline_covers_a_slow_error_body():
    """PR #119 recheck: a 401 whose body trickles is bounded like a 200."""
    from http.server import BaseHTTPRequestHandler

    class SlowUnauthorized(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            self.send_response(401)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                for piece in (b"Unauth", b"orized", b"\n", b"..."):
                    self.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
                    self.wfile.flush()
                    time.sleep(0.6)
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        def log_message(self, *args):
            return

    result, elapsed = _timed_instant(_slow_server(SlowUnauthorized))
    assert result.status == "timeout" and not result.body_complete
    assert elapsed < 1.6, elapsed


def test_a_prompt_error_status_is_reported_with_its_body():
    from http.server import BaseHTTPRequestHandler

    class Unauthorized(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            body = b"Unauthorized\n"
            self.send_response(401)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return

    result, elapsed = _timed_instant(_slow_server(Unauthorized), timeout_seconds=5)
    assert (result.status, result.http_status, result.detail) == ("failed", 401, "HTTP")
    assert result.body == b"Unauthorized\n" and result.body_complete
    assert elapsed < 1


def _http_workers() -> int:
    return sum(1 for t in threading.enumerate() if t.name == "opspilot-observer-http")


def _settle(predicate, seconds=3.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_timed_out_workers_do_not_accumulate_when_the_response_owns_the_socket():
    """PR #119 recheck 2: HTTP/1.0 + chunked makes ``getresponse()`` hand the
    socket to the response and clear ``conn.sock``; a server that trickles
    the chunk-size line forever used to leave one blocked worker per timeout.
    The cancel path now closes the socket reference taken after connect, so
    the workers end right after each timeout."""
    from http.server import BaseHTTPRequestHandler

    stop = threading.Event()

    class Trickle(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_GET(self):  # noqa: N802 - http.server API
            try:
                self.wfile.write(
                    b"HTTP/1.0 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                )
                self.wfile.flush()
                while not stop.is_set():
                    self.wfile.write(b"f")
                    self.wfile.flush()
                    time.sleep(0.2)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        def log_message(self, *args):
            return

    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), Trickle)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        src = PrometheusReadOnlySource(f"http://127.0.0.1:{server.server_port}")
        before = _http_workers()
        for _ in range(3):
            result = src.instant("up", at=NOW, timeout_seconds=1)
            assert result.status == "timeout"
        assert _settle(lambda: _http_workers() == before, seconds=2.0), _http_workers()
    finally:
        stop.set()
        server.shutdown()


def test_blocked_resolution_is_bounded_by_the_in_flight_limit(monkeypatch):
    """PR #119 recheck 2: a hung resolver cannot be interrupted; the number
    of such workers is capped, further requests fail at once without a
    thread, and the cap is released when the workers end."""
    import socket as socket_module

    from opspilot.observer import prometheus

    release = threading.Event()
    real = socket_module.getaddrinfo

    def hanging(host, *args, **kwargs):
        if host == "resolver.hang.invalid":
            release.wait(10)
            raise OSError("resolution gave up")
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket_module, "getaddrinfo", hanging)
    src = PrometheusReadOnlySource("http://resolver.hang.invalid:9090")
    before = _http_workers()
    limit = prometheus.IN_FLIGHT_LIMIT
    try:
        started = time.monotonic()
        for _ in range(limit):
            result = src.instant("up", at=NOW, timeout_seconds=1)
            assert result.status == "timeout"
        assert _http_workers() - before == limit
        # beyond the cap: refused immediately, no new worker
        for _ in range(3):
            refused = src.instant("up", at=NOW, timeout_seconds=1)
            assert (refused.status, refused.detail) == ("failed", "IN_FLIGHT_LIMIT")
        assert _http_workers() - before == limit
        assert time.monotonic() - started < limit * 1.0 + 1.5
    finally:
        release.set()
    assert _settle(lambda: _http_workers() == before), _http_workers()
    # the cap is free again
    monkeypatch.setattr(socket_module, "getaddrinfo", real)
    src = PrometheusReadOnlySource("http://127.0.0.1:9")
    assert src.instant("up", at=NOW, timeout_seconds=1).detail in (
        "UNREACHABLE",
        "TIMEOUT",
    )


def test_a_failed_worker_start_releases_its_slot(monkeypatch):
    """PR #119 recheck 3: ``RuntimeError: can't start new thread`` after the
    slot was taken must not leak the slot; four such failures used to pin the
    process at IN_FLIGHT_LIMIT for good."""
    from opspilot.observer import prometheus

    def cannot_start(self):
        raise RuntimeError("can't start new thread")

    before = prometheus._IN_FLIGHT.count
    monkeypatch.setattr(threading.Thread, "start", cannot_start)
    src = PrometheusReadOnlySource("http://127.0.0.1:9")
    for _ in range(prometheus.IN_FLIGHT_LIMIT + 2):
        result = src.instant("up", at=NOW, timeout_seconds=1)
        assert (result.status, result.detail) == ("failed", "UNREACHABLE")
    assert prometheus._IN_FLIGHT.count == before
    monkeypatch.undo()
    # threads start again and the slots are all available: a normal request
    # goes through (refused by the closed port, not by the limit)
    result = src.instant("up", at=NOW, timeout_seconds=1)
    assert result.detail in ("UNREACHABLE", "TIMEOUT")
    assert prometheus._IN_FLIGHT.count == before


# --- isolation (C3 §3, D3): own variables, own imports, no model


INVESTIGATION_SIDE = (
    "opspilot.tools",
    "opspilot.investigation",
    "opspilot.worker",
    "opspilot.worker_main",
    "opspilot.web",
    "opspilot.tracing",
    "opspilot.acceptance",
    "opspilot.intake",
    "opspilot.recovery",
)


def _imports(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_observer_packages_import_nothing_from_the_investigation_side():
    offenders = []
    for package in ("observer", "observation"):
        for path in sorted((REPO_ROOT / "opspilot" / package).rglob("*.py")):
            bad = sorted(
                name for name in _imports(path) if name.startswith(INVESTIGATION_SIDE)
            )
            if bad:
                offenders.append((path.relative_to(REPO_ROOT).as_posix(), bad))
    assert not offenders, offenders


def test_the_observer_process_loads_no_gateway_worker_model_or_web_module():
    """In a fresh interpreter: importing the entry point and the loop pulls in
    none of the modules that hold the investigation credentials or the model
    client. (``opspilot.persistence`` is imported as a package by the shared
    store base; that is code, the DSN decides what the database allows.)"""
    script = (
        "import sys, opspilot.observer.__main__, opspilot.observer.loop\n"
        "print(sorted(m for m in sys.modules if m.startswith('opspilot')))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO_ROOT,
    )
    loaded = set(eval(proc.stdout.strip()))  # noqa: S307 - our own print
    leaked = sorted(m for m in loaded if m.startswith(INVESTIGATION_SIDE))
    assert not leaked, leaked
    assert "opspilot.observer.sampler" in loaded


def test_build_loop_reads_only_observer_variables_and_never_falls_back():
    """The investigation side's DSN, Prometheus URL, token and model key are
    all present and all ignored: without the Observer's own two variables the
    process refuses to start, and the token comes only from the Observer's."""
    investigation = {
        "OPSPILOT_DSN": "host=db dbname=opspilot user=worker",
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://investigation-prometheus:9090",
        "OPSPILOT_OTEL_TOKEN": "investigation-token",
        "DEEPSEEK_API_KEY": "sk-model-key",
    }
    with pytest.raises(SystemExit, match="OPSPILOT_OBSERVER_DSN is required"):
        build_loop(investigation, stop=threading.Event())
    with pytest.raises(
        SystemExit, match="OPSPILOT_OBSERVER_PROMETHEUS_URL is required"
    ):
        build_loop(
            {
                **investigation,
                "OPSPILOT_OBSERVER_DSN": "host=db dbname=opspilot user=observer",
            },
            stop=threading.Event(),
        )
    loop = build_loop(
        {
            **investigation,
            "OPSPILOT_OBSERVER_DSN": "host=db dbname=opspilot user=observer",
            "OPSPILOT_OBSERVER_PROMETHEUS_URL": "http://observer-prometheus:9090",
            "OPSPILOT_OBSERVER_BATCH": "3",
        },
        stop=threading.Event(),
    )
    assert isinstance(loop, ObserverLoop) and loop.batch == 3
    assert loop.source.base_url == "http://observer-prometheus:9090"
    assert loop.source._token is None  # the investigation token was not adopted
    assert loop.store.dsn == "host=db dbname=opspilot user=observer"
    loop.store.close()


def test_build_loop_token_comes_from_the_observer_env_file(tmp_path):
    env_file = tmp_path / "observer.env"
    env_file.write_text(
        "OPSPILOT_OTEL_TOKEN=not-mine\nOPSPILOT_OBSERVER_PROMETHEUS_TOKEN='mine'\n"
    )
    loop = build_loop(
        {
            "OPSPILOT_OBSERVER_DSN": "host=db dbname=opspilot user=observer",
            "OPSPILOT_OBSERVER_PROMETHEUS_URL": "http://observer-prometheus:9090",
            "OPSPILOT_OBSERVER_ENV_FILE": str(env_file),
        },
        stop=threading.Event(),
    )
    assert loop.source._token == "mine"
    loop.store.close()


def test_the_loop_handles_a_refused_or_crashed_sample_and_keeps_going():
    class Store:
        def __init__(self):
            self.calls = []

        def sweep_expired_sessions(self, *, limit):
            self.calls.append("sweep")
            return []

        def claim_due_samples(self, owner, *, limit):
            self.calls.append("claim")
            return [lease(), lease(), lease()]

        def health_profile(self, revision):
            raise PersistenceError("UNKNOWN_IDENTITY")

        def current_time(self):
            return NOW

        def submit_sample(self, lease, sample, readings):
            # a profile that cannot be read: filed as failed, no query issued
            assert sample.outcome == "failed" and readings == []
            self.calls.append("submit")
            if len(self.calls) == 4:
                raise PersistenceError("TIMEOUT")
            if len(self.calls) == 5:
                raise RuntimeError("boom")
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

    store = Store()
    loop = ObserverLoop(store=store, source=FakeSource({}))
    results = loop.poll_once()
    assert [receipt.disposition for _, receipt in results] == ["adopted"]
    assert store.calls == ["sweep", "claim", "submit", "submit", "submit"]
