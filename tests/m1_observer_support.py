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
import io
import json
import pathlib
import threading
import time
import urllib.error
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from opspilot.domain.observation import HealthSample
from opspilot.observation.store import (
    SampleLease,
    SampleReceipt,
    SignalReading,
)
from opspilot.observer import HealthProfile
from opspilot.observer.prometheus import (
    InstantResult,
    PrometheusReadOnlySource,
)

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
                "query": "sum(rate(x{namespace='ns',service='svc'}[5m]))",
                "coverage_query": "max(count_over_time(x{namespace='ns',service='svc'}[5m]))",
                "freshness_query": "max(timestamp(x{namespace='ns',service='svc'}))",
                "minimum_samples": 3,
                "traffic_dependent": False,
                "healthy": {"min": 0},
                "scope": {"namespace_label": "namespace", "workload_label": "service"},
            },
            {
                "name": "errors",
                "description": "error ratio",
                "query": "e{namespace='ns',service='svc'} / t{namespace='ns',service='svc'}",
                "coverage_query": "max(count_over_time(t{namespace='ns',service='svc'}[5m]))",
                "freshness_query": "max(timestamp(t{namespace='ns',service='svc'}))",
                "minimum_samples": 3,
                "traffic_dependent": True,
                "healthy": {"min": 0, "max": 0.01},
                "scope": {"namespace_label": "namespace", "workload_label": "service"},
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
        "sum(rate(x{namespace='ns',service='svc'}[5m]))": ok(2.0),
        "max(count_over_time(x{namespace='ns',service='svc'}[5m]))": ok(5),
        "max(timestamp(x{namespace='ns',service='svc'}))": ok(latest.timestamp()),
        "e{namespace='ns',service='svc'} / t{namespace='ns',service='svc'}": ok(0.0),
        "max(count_over_time(t{namespace='ns',service='svc'}[5m]))": ok(5),
        "max(timestamp(t{namespace='ns',service='svc'}))": ok(latest.timestamp()),
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


def _http_workers() -> int:
    return sum(1 for t in threading.enumerate() if t.name == "opspilot-observer-http")


def _settle(predicate, seconds=3.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


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


def _slow_scope_setup(monkeypatch, scope_cost):
    """A controllable monotonic clock; ``scope_cost(calls_so_far)`` s per scope check."""
    from opspilot.observer import sampler

    clock = {"now": 1000.0}
    monkeypatch.setattr(sampler.time, "monotonic", lambda: clock["now"])

    class Store(FakeStore):
        def lease_scope_current(self, lease):
            clock["now"] += scope_cost(self.scope_calls)
            return super().lease_scope_current(lease)

    class Source:
        def __init__(self):
            self.timeouts: list[int] = []

        def instant(self, expr, *, at, timeout_seconds):
            self.timeouts.append(timeout_seconds)
            clock["now"] += 1.0
            return ok(1.0)

    short = lease().__class__(
        **{**lease().__dict__, "lease_until": NOW + timedelta(seconds=25)}
    )
    return clock, Store(), Source(), short


def _raw_server(payload: bytes, *, stall: float = 0.0):
    """A TCP server that writes ``payload`` verbatim to the first client and
    closes: what a peer that declares a length it does not deliver, or an
    oversized error body, looks like on the wire."""
    import socketserver

    class Raw(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.recv(65536)
            self.request.sendall(payload)
            time.sleep(stall)

    server = socketserver.TCPServer(("127.0.0.1", 0), Raw)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


_WIRE_ELAPSED = [0.0]


def _wire_instant(payload: bytes, *, via_opener: bool, stall: float = 0.0, timeout=5):
    server = _raw_server(payload, stall=stall)
    try:
        opener = (
            urllib.request.build_opener(urllib.request.ProxyHandler({}))
            if via_opener
            else None
        )
        src = PrometheusReadOnlySource(
            f"http://127.0.0.1:{server.server_address[1]}", opener=opener
        )
        started = time.monotonic()
        result = src.instant("up", at=NOW, timeout_seconds=timeout)
        _WIRE_ELAPSED[0] = time.monotonic() - started
        return result
    finally:
        server.shutdown()
        server.server_close()
