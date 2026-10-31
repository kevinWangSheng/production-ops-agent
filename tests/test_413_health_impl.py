"""#413 implementer tests: the health listener, readiness rules, metrics text
and the model-call counter (F8-R1–R4, contract r1). No PostgreSQL needed."""

from __future__ import annotations

import http.client
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from opspilot import __version__, health
from opspilot.health import (
    ModelCallCounter,
    ModelCallStats,
    Readiness,
    poll_readiness,
    start_health_server,
    web_readiness,
)
from opspilot.investigation.loop import ModelError

ROOT = Path(__file__).resolve().parents[1]


def _request(
    port: int, path: str, method: str = "GET"
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path)
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


@pytest.fixture
def serve():
    servers: list[health.HealthServer] = []

    def go(readiness=lambda: Readiness(True, None), metrics=lambda: "m 1\n"):
        server = start_health_server(
            "127.0.0.1:0", readiness=readiness, metrics=metrics
        )
        assert server is not None
        servers.append(server)
        return server.port

    yield go
    for server in servers:
        server.close()


# -- listener -----------------------------------------------------------------


@pytest.mark.parametrize("bind", [None, ""])
def test_empty_bind_disables_the_listener(bind):
    assert (
        start_health_server(bind, readiness=lambda: Readiness(True, None), metrics=str)
        is None
    )


def test_healthz_answers_ok_and_the_version_only(serve):
    status, headers, body = _request(serve(), "/healthz")
    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(body) == {"status": "ok", "version": __version__}


def test_readyz_is_200_when_ready_and_503_with_the_code_when_not(serve):
    status, _, body = _request(serve(), "/readyz")
    assert (status, json.loads(body)) == (
        200,
        {"status": "ready", "version": __version__},
    )
    port = serve(readiness=lambda: Readiness(False, "POLL_STALE"))
    status, _, body = _request(port, "/readyz")
    assert (status, json.loads(body)) == (
        503,
        {"status": "not_ready", "reason": "POLL_STALE"},
    )


def test_metrics_serves_the_prometheus_text(serve):
    status, headers, body = _request(serve(metrics=lambda: "a 1\nb 2\n"), "/metrics")
    assert status == 200
    assert headers["Content-Type"].startswith("text/plain; version=0.0.4")
    assert body == b"a 1\nb 2\n"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/"),
        ("GET", "/healthz/"),
        ("GET", "/incidents"),
        ("POST", "/healthz"),
        ("PUT", "/readyz"),
        ("DELETE", "/metrics"),
        ("HEAD", "/healthz"),
        ("OPTIONS", "/healthz"),
        ("PATCH", "/metrics"),
        ("BREW", "/healthz"),
    ],
)
def test_anything_but_get_on_the_three_routes_is_404(serve, method, path):
    status, _, _ = _request(serve(), path, method)
    assert status == 404


def test_responses_are_capped_at_4_kib_on_a_line_boundary(serve):
    big = "".join(f"opspilot_x{i} {i}\n" for i in range(2000))
    status, _, body = _request(serve(metrics=lambda: big), "/metrics")
    assert status == 200
    assert len(body) <= 4096
    assert body.endswith(b"\n")
    assert big.encode().startswith(body)


def test_a_failing_probe_answers_500_without_detail(serve):
    def boom() -> Readiness:
        raise RuntimeError("provider said: secret text")

    status, _, body = _request(serve(readiness=boom), "/readyz")
    assert status == 500
    assert json.loads(body) == {"status": "error"}


def test_server_header_does_not_name_the_python_version(serve):
    _, headers, _ = _request(serve(), "/healthz")
    assert "Python" not in headers.get("Server", "")


def test_close_releases_the_port():
    server = start_health_server(
        "127.0.0.1:0", readiness=lambda: Readiness(True, None), metrics=str
    )
    assert server is not None
    port = server.port
    server.close()
    with pytest.raises(OSError):
        _request(port, "/healthz")


# -- bind from the environment -----------------------------------------------


def test_bind_defaults_to_loopback_9464_and_empty_disables():
    assert health.health_bind({}) == "127.0.0.1:9464"
    assert health.health_bind({"OPSPILOT_HEALTH_BIND": "0.0.0.0:9464"}) == (
        "0.0.0.0:9464"
    )
    assert health.health_bind({"OPSPILOT_HEALTH_BIND": ""}) is None


def test_serve_health_refuses_a_malformed_bind():
    with pytest.raises(SystemExit):
        health.serve_health(
            {"OPSPILOT_HEALTH_BIND": "no-port"},
            readiness=lambda: Readiness(True, None),
            metrics=str,
        )


def test_serve_health_keeps_the_process_running_when_the_port_is_taken():
    first = start_health_server(
        "127.0.0.1:0", readiness=lambda: Readiness(True, None), metrics=str
    )
    assert first is not None
    try:
        second = health.serve_health(
            {"OPSPILOT_HEALTH_BIND": f"127.0.0.1:{first.port}"},
            readiness=lambda: Readiness(True, None),
            metrics=str,
        )
        assert second is None
    finally:
        first.close()


# -- readiness rules ------------------------------------------------------------


def test_poll_readiness():
    def ready(last, now, stopping=False):
        return poll_readiness(
            last_success_monotonic=last,
            poll_interval_seconds=2.0,
            now_monotonic=now,
            stopping=stopping,
        )

    assert ready(None, 100.0) == Readiness(False, "POLL_STALE")
    assert ready(100.0, 106.0) == Readiness(True, None)
    assert ready(100.0, 106.5) == Readiness(False, "POLL_STALE")
    assert ready(100.0, 101.0, stopping=True) == Readiness(False, "STOPPING")
    assert ready(None, 100.0, stopping=True) == Readiness(False, "STOPPING")


def test_web_readiness():
    assert web_readiness(schema_at_head=True, pool_ok=True) == Readiness(True, None)
    assert web_readiness(schema_at_head=False, pool_ok=True) == Readiness(
        False, "SCHEMA_NOT_AT_HEAD"
    )
    assert web_readiness(schema_at_head=False, pool_ok=False) == Readiness(
        False, "POOL_UNAVAILABLE"
    )


# -- metrics text ---------------------------------------------------------------


def _samples(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            name, value = line.rsplit(" ", 1)
            out[name] = value
    return out


def test_web_metrics_names_and_labels(monkeypatch):
    monkeypatch.setenv("OPSPILOT_REVISION", "abc123")
    text = health.web_metrics(
        schema_at_head=True, queue_depth={"queued": 3, "running_lapsed": 1}
    )
    assert _samples(text) == {
        f'opspilot_build_info{{version="{__version__}",revision="abc123"}}': "1",
        "opspilot_schema_at_head": "1",
        'opspilot_queue_depth{state="queued"}': "3",
        'opspilot_queue_depth{state="running_lapsed"}': "1",
    }
    unavailable = _samples(health.web_metrics(schema_at_head=False, queue_depth=None))
    assert unavailable["opspilot_schema_at_head"] == "0"
    assert not any(k.startswith("opspilot_queue_depth") for k in unavailable)


def test_revision_label_is_sanitized(monkeypatch):
    monkeypatch.setenv("OPSPILOT_REVISION", 'x"}\nevil 1')
    text = health.web_metrics(schema_at_head=True, queue_depth=None)
    assert 'revision="unknown"' in text
    monkeypatch.delenv("OPSPILOT_REVISION")
    assert 'revision="unknown"' in health.web_metrics(
        schema_at_head=True, queue_depth=None
    )


def test_worker_metrics_names_and_labels():
    stats = ModelCallStats()
    stats.record("ok")
    stats.record("MODEL_UNAVAILABLE")
    stats.record("MODEL_UNAVAILABLE")
    samples = _samples(
        health.worker_metrics(last_poll_age=1.5, in_flight=2, model=stats.snapshot())
    )
    assert samples["opspilot_last_poll_age_seconds"] == "1.5"
    assert samples["opspilot_in_flight_attempts"] == "2"
    assert samples['opspilot_model_calls_total{category="ok"}'] == "1"
    assert samples['opspilot_model_calls_total{category="MODEL_UNAVAILABLE"}'] == "2"
    assert samples["opspilot_model_consecutive_failures"] == "2"
    assert samples[
        'opspilot_last_model_call_category{category="MODEL_UNAVAILABLE"}'
    ] == ("1")
    fresh = _samples(
        health.worker_metrics(
            last_poll_age=None, in_flight=0, model=ModelCallStats().snapshot()
        )
    )
    assert fresh["opspilot_last_poll_age_seconds"] == "+Inf"
    assert fresh["opspilot_model_consecutive_failures"] == "0"
    assert not any(k.startswith("opspilot_last_model_call_category") for k in fresh)


def test_observer_metrics_names():
    samples = _samples(health.observer_metrics(last_poll_age=0.25, observation_due=4))
    assert samples["opspilot_last_poll_age_seconds"] == "0.25"
    assert samples["opspilot_observation_due"] == "4"
    assert "opspilot_model_consecutive_failures" not in samples


# -- model-call counter -----------------------------------------------------------


class _Client:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.calls: list[Any] = []

    def complete(self, call: Any) -> Any:
        self.calls.append(call)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def test_counter_passes_calls_through_and_classifies_them():
    reply = object()
    client = _Client(
        [ModelError("MODEL_UNAVAILABLE"), ModelError("MODEL_UNAVAILABLE"), reply]
    )
    counter = ModelCallCounter(client)
    call = object()
    for _ in range(2):
        with pytest.raises(ModelError) as raised:
            counter.complete(call)
        assert raised.value.code == "MODEL_UNAVAILABLE"
    assert counter.snapshot() == {
        "calls": {"MODEL_UNAVAILABLE": 2},
        "consecutive_failures": 2,
        "last_category": "MODEL_UNAVAILABLE",
    }
    assert counter.complete(call) is reply
    assert client.calls == [call, call, call]
    assert counter.snapshot() == {
        "calls": {"MODEL_UNAVAILABLE": 2, "ok": 1},
        "consecutive_failures": 0,
        "last_category": "ok",
    }


def test_counter_reraises_other_errors_without_counting_them():
    counter = ModelCallCounter(_Client([KeyError("x")]))
    with pytest.raises(KeyError):
        counter.complete(object())
    assert counter.snapshot() == {
        "calls": {},
        "consecutive_failures": 0,
        "last_category": None,
    }


def test_counters_sharing_stats_add_up_and_odd_codes_are_bounded():
    stats = ModelCallStats()
    first = ModelCallCounter(_Client([ModelError("RESPONSE_TOO_LARGE")]), stats)
    second = ModelCallCounter(_Client([ModelError('bad "code"\n')]), stats)
    for counter in (first, second):
        with pytest.raises(ModelError):
            counter.complete(object())
    assert first.snapshot() == second.snapshot() == stats.snapshot()
    assert stats.snapshot()["calls"] == {"RESPONSE_TOO_LARGE": 1, "UNKNOWN": 1}
    assert stats.snapshot()["consecutive_failures"] == 2


# -- loop hooks -----------------------------------------------------------------


class _Sweeper:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def sweep_expired_runs_with_generations(self, *, incident_id=None, limit=100):
        from opspilot.persistence import PersistenceError

        if self.fail:
            raise PersistenceError("STORAGE_UNAVAILABLE")
        return ()


class _Queue:
    def claimable_incidents(self, *, limit: int = 20):
        return ()


class _Runner:
    def resume(self, incident_id):  # pragma: no cover - nothing is queued
        raise AssertionError


def _run_worker_loop(sweeper: _Sweeper):
    from opspilot.worker_main import WorkerLoop

    stop = threading.Event()
    loop = WorkerLoop(
        queue=_Queue(),
        runner=_Runner(),
        sweeper=sweeper,  # type: ignore[arg-type]
        events=None,
        stop=stop,
        poll_seconds=0.05,
    )
    assert loop.last_poll_success is None
    thread = threading.Thread(target=loop.run, daemon=True)
    thread.start()
    time.sleep(0.2)
    stop.set()
    thread.join(5)
    return loop


def test_worker_loop_records_its_last_successful_poll():
    before = time.monotonic()
    loop = _run_worker_loop(_Sweeper())
    assert loop.last_poll_success is not None
    assert loop.last_poll_success >= before
    assert loop.in_flight_attempts() == 0


def test_worker_loop_does_not_record_a_refused_poll():
    assert _run_worker_loop(_Sweeper(fail=True)).last_poll_success is None


def test_observer_loop_calls_its_poll_hook_after_each_pass():
    from opspilot.observer.loop import ObserverLoop

    class _Store:
        def sweep_expired_sessions(self, *, limit: int):
            return ()

        def claim_due_samples(self, owner, *, limit, lease_seconds):
            return []

    stop = threading.Event()
    seen: list[int] = []

    def hook() -> None:
        seen.append(1)
        if len(seen) >= 2:
            stop.set()

    loop = ObserverLoop(
        store=_Store(),  # type: ignore[arg-type]
        source=object(),  # type: ignore[arg-type]
        stop=stop,
        owner=uuid4(),
        poll_seconds=0.01,
        on_poll=hook,
    )
    loop.run()
    assert len(seen) >= 2


def test_observer_entry_still_does_not_load_the_investigation_side():
    script = (
        "import sys, opspilot.observer.__main__, opspilot.health\n"
        "print(sorted(m for m in sys.modules if m.startswith('opspilot')))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
    )
    loaded = proc.stdout
    for side in ("opspilot.investigation", "opspilot.web", "opspilot.worker"):
        assert f"'{side}" not in loaded, side
