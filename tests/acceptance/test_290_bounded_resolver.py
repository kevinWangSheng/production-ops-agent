"""Issue #290 acceptance: bounded, deadline-aware hostname resolution.

These tests deliberately observe resolver call concurrency and process thread
counts.  They do not inspect the resolver implementation or its private state.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from opspilot.tools import (
    TransportRequest,
    TransportResponse,
    TransportTimeout,
    TransportUnavailable,
    Window,
)
from opspilot.tools.otel_demo import (
    CREDENTIAL_REF,
    METRICS_TOOL,
    SOURCE,
    TARGET_ID,
    TRACES_TOOL,
    OtelDemoTransport,
)
from tests.m1_tool_support import build
from tests.m1_tool_support import request as tool_request

TIMEOUT = 0.8
SETTLE = 3.0
BLOCKED_HOST = "resolver-stuck.invalid"
SUCCESS_HOST = "resolver-success.invalid"


def _thread_count() -> int:
    return sum(thread.is_alive() for thread in threading.enumerate())


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _request(endpoint: str, *, timeout: float = TIMEOUT, tool: str = METRICS_TOOL):
    now = datetime.now(timezone.utc)
    window = Window(now - timedelta(minutes=5), now)
    params = {"expr": "up"}
    selector = {"opspilot.integration.id": TARGET_ID, "traces_endpoint": endpoint}
    if tool == TRACES_TOOL:
        params = {"service": "checkout", "limit": 1}
    return TransportRequest(
        operation_id="issue-290",
        source=SOURCE,
        verb="query",
        endpoint=endpoint,
        selector=selector,
        params=params,
        window=window,
        timeout_seconds=timeout,
        max_result_bytes=1_048_576,
        credential_ref=CREDENTIAL_REF,
        tool=tool,
    )


def _transport() -> OtelDemoTransport:
    return OtelDemoTransport(credentials={CREDENTIAL_REF: None})


def _guarded(call, timeout: float = 3.0):
    result: dict[str, object] = {}
    started = time.monotonic()

    def run():
        try:
            result["value"] = call()
        except BaseException as error:  # noqa: BLE001 - asserted by the test
            result["error"] = error
        finally:
            result["elapsed"] = time.monotonic() - started

    thread = threading.Thread(target=run, daemon=True, name="issue-290-guard")
    thread.start()
    thread.join(timeout)
    return thread, result


class _QueryHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server API
        body = json.dumps(
            {
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": [
                        {
                            "metric": {"service_name": "checkout"},
                            "values": [[time.time(), "1"]],
                        }
                    ],
                },
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


@pytest.fixture
def local_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _QueryHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def _resolver_fake(
    monkeypatch,
    *,
    release_events,
    success_port: int | None = None,
    block_hosts: set[str] | None = None,
):
    real = socket.getaddrinfo
    lock = threading.Lock()
    active = 0
    maximum = 0
    started = 0
    started_event = threading.Event()

    def fake(host, port, *args, **kwargs):
        nonlocal active, maximum, started
        if host in (block_hosts or {BLOCKED_HOST}):
            with lock:
                active += 1
                maximum = max(maximum, active)
                started += 1
                event_index = min(started - 1, len(release_events) - 1)
                started_event.set()
            event = release_events[event_index]
            try:
                event.wait(5)
                return real("127.0.0.1", port, *args, **kwargs)
            finally:
                with lock:
                    active -= 1
        if host == SUCCESS_HOST:
            assert success_port is not None
            return real("127.0.0.1", success_port, *args, **kwargs)
        return real(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake)

    def stats():
        with lock:
            return started, maximum, active

    return started_event, stats


def test_sequential_stuck_queries_are_bounded_and_deadline_limited(monkeypatch):
    release = threading.Event()
    started_event, stats = _resolver_fake(monkeypatch, release_events=[release])
    baseline = _thread_count()
    calls = []
    try:
        for _ in range(6):
            thread, result = _guarded(
                lambda: _transport().fetch(
                    _request(f"http://{BLOCKED_HOST}:4318")
                )
            )
            calls.append((thread, result))
            assert not thread.is_alive()
            assert isinstance(result.get("error"), TransportTimeout)
            assert result["elapsed"] <= TIMEOUT + 1.0
        assert stats()[1] <= 2
        assert stats()[0] == 2
    finally:
        release.set()
    assert _wait_until(lambda: _thread_count() <= baseline + 2, SETTLE)


def test_concurrent_stuck_queries_share_two_resolver_slots(monkeypatch):
    release = threading.Event()
    started_event, stats = _resolver_fake(
        monkeypatch, release_events=[release], block_hosts={BLOCKED_HOST, "localhost"}
    )
    baseline = _thread_count()
    calls = []
    try:
        for tool in (METRICS_TOOL, TRACES_TOOL, METRICS_TOOL, TRACES_TOOL):
            calls.append(
                _guarded(
                    lambda tool=tool: _transport().fetch(
                        _request(f"http://{BLOCKED_HOST}:4318", tool=tool)
                    )
                )
            )
        assert started_event.wait(1.0)
        for thread, result in calls:
            thread.join(2)
            assert not thread.is_alive()
            assert isinstance(result.get("error"), TransportTimeout)
            assert result["elapsed"] <= TIMEOUT + 1.0
        assert stats()[1] <= 2
    finally:
        release.set()
    assert _wait_until(lambda: _thread_count() <= baseline + 2, SETTLE)


def test_waiting_query_takes_freed_slot_and_succeeds(monkeypatch, local_server):
    first = threading.Event()
    second = threading.Event()
    started_event, stats = _resolver_fake(
        monkeypatch,
        release_events=[first, second],
        success_port=local_server.server_port,
    )
    endpoint = f"http://{SUCCESS_HOST}:{local_server.server_port}"
    first_two = [
        _guarded(lambda: _transport().fetch(_request(f"http://{BLOCKED_HOST}:4318")))
        for _ in range(2)
    ]
    assert started_event.wait(1.0)
    assert _wait_until(lambda: stats()[0] == 2)
    waiting_thread, waiting_result = _guarded(
        lambda: _transport().fetch(_request(endpoint, timeout=2.0))
    )
    time.sleep(0.2)
    assert stats()[0] == 2, "waiting query started a resolver call before a slot freed"
    first.set()
    waiting_thread.join(3)
    assert not waiting_thread.is_alive()
    assert waiting_result.get("error") is None, waiting_result
    assert isinstance(waiting_result.get("value"), TransportResponse)
    second.set()
    for thread, _ in first_two:
        thread.join(3)


def test_released_resolver_recovers_for_later_hostname_queries(monkeypatch, local_server):
    release = threading.Event()
    started_event, stats = _resolver_fake(
        monkeypatch, release_events=[release], success_port=local_server.server_port
    )
    blocked = _guarded(
        lambda: _transport().fetch(_request(f"http://{BLOCKED_HOST}:4318"))
    )
    assert started_event.wait(1.0)
    blocked[0].join(2)
    assert isinstance(blocked[1].get("error"), TransportTimeout)
    release.set()
    assert _wait_until(lambda: stats()[2] == 0, SETTLE)
    baseline = _thread_count()
    result = _transport().fetch(
        _request(f"http://{SUCCESS_HOST}:{local_server.server_port}", timeout=2.0)
    )
    assert isinstance(result, TransportResponse)
    assert _wait_until(lambda: _thread_count() <= baseline + 1, SETTLE)


def test_ip_literals_bypass_stuck_resolver_and_localhost_does_not(monkeypatch, local_server):
    release = threading.Event()
    started_event, stats = _resolver_fake(
        monkeypatch, release_events=[release], block_hosts={BLOCKED_HOST, "localhost"}
    )
    blocked = [
        _guarded(
            lambda: _transport().fetch(_request(f"http://{BLOCKED_HOST}:4318"))
        )
        for _ in range(2)
    ]
    assert started_event.wait(1.0)
    assert _wait_until(lambda: stats()[0] == 2)
    try:
        ipv4 = _transport().fetch(
            _request(f"http://127.0.0.1:{local_server.server_port}", timeout=2.0)
        )
        assert isinstance(ipv4, TransportResponse)
        try:
            ipv6 = _transport().fetch(
                _request(f"http://[::1]:{local_server.server_port}", timeout=2.0)
            )
        except (TransportTimeout, TransportUnavailable, OSError):
            ipv6 = None
        if ipv6 is not None:
            assert isinstance(ipv6, TransportResponse)
        localhost = _guarded(
            lambda: _transport().fetch(
                _request(f"http://localhost:{local_server.server_port}")
            )
        )
        localhost[0].join(2)
        assert isinstance(localhost[1].get("error"), TransportTimeout)
        assert stats()[0] >= 2
    finally:
        release.set()
    for thread, _ in blocked:
        thread.join(2)


def test_timeout_mapping_preserves_possible_contact_and_executor_code():
    class TimeoutTransport:
        def fetch(self, _request):
            raise TransportTimeout("TIMEOUT")

    executor, _, _, _ = build(transport=TimeoutTransport())
    outcome = executor.execute(tool_request())
    assert (outcome.status, outcome.reason, outcome.source_contact) == (
        "timeout",
        "TOOL_TIMEOUT",
        "possible",
    )
