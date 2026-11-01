"""#290 implementer tests: bounded shared resolver for query transports (Q9)."""

from __future__ import annotations

import socket
import threading
import time

import pytest

from opspilot.tools import http_deadline
from opspilot.tools.http_deadline import RESOLVER_SLOTS, resolve

HUNG = "resolver.hang.invalid"


@pytest.fixture
def stuck(monkeypatch):
    release = threading.Event()
    state = {"inside": 0, "peak": 0, "calls": []}
    lock = threading.Lock()
    real = socket.getaddrinfo

    def fake(host, *args, **kwargs):
        with lock:
            state["calls"].append(host)
        if host != HUNG:
            return real(host, *args, **kwargs)
        with lock:
            state["inside"] += 1
            state["peak"] = max(state["peak"], state["inside"])
        try:
            release.wait(10)
            return real("127.0.0.1", *args, **kwargs)
        finally:
            with lock:
                state["inside"] -= 1

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    try:
        yield release, state
    finally:
        release.set()
        deadline = time.monotonic() + 5
        while state["inside"] and time.monotonic() < deadline:
            time.sleep(0.01)


def _resolver_threads() -> int:
    return sum(t.name == "otel-resolve" for t in threading.enumerate())


def test_slots_bound_threads_and_each_call_ends_at_its_deadline(stuck):
    release, state = stuck
    for _ in range(6):
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            resolve(HUNG, 80, time.monotonic() + 0.3)
        assert time.monotonic() - started < 0.3 + 0.3
    assert state["peak"] == RESOLVER_SLOTS
    assert _resolver_threads() == RESOLVER_SLOTS
    release.set()
    deadline = time.monotonic() + 3
    while _resolver_threads() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert _resolver_threads() == 0
    assert resolve(HUNG, 80, time.monotonic() + 1)  # slots free again


def test_waiter_takes_a_slot_freed_inside_its_deadline(stuck):
    release, state = stuck
    for _ in range(RESOLVER_SLOTS):
        with pytest.raises(TimeoutError):
            resolve(HUNG, 80, time.monotonic() + 0.1)
    threading.Timer(0.3, release.set).start()
    assert resolve(HUNG, 80, time.monotonic() + 2.0)


@pytest.mark.parametrize("literal", ["127.0.0.1", "::1"])
def test_ip_literals_bypass_full_slots(stuck, literal):
    release, state = stuck
    for _ in range(RESOLVER_SLOTS):
        with pytest.raises(TimeoutError):
            resolve(HUNG, 80, time.monotonic() + 0.1)
    if literal == "::1" and not socket.has_ipv6:
        pytest.skip("no IPv6")
    assert resolve(literal, 80, time.monotonic() + 0.5)
    assert state["peak"] == RESOLVER_SLOTS


def test_localhost_takes_a_slot(stuck, monkeypatch):
    release, state = stuck
    for _ in range(RESOLVER_SLOTS):
        with pytest.raises(TimeoutError):
            resolve(HUNG, 80, time.monotonic() + 0.1)
    with pytest.raises(TimeoutError):
        resolve("localhost", 80, time.monotonic() + 0.2)
    assert "localhost" not in state["calls"]


def test_failed_thread_start_returns_the_slot(monkeypatch):
    def cannot_start(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", cannot_start)
    for _ in range(RESOLVER_SLOTS + 2):
        with pytest.raises(RuntimeError):
            resolve("example.invalid", 80, time.monotonic() + 0.2)
    monkeypatch.undo()
    assert http_deadline._resolver_slots._value == RESOLVER_SLOTS
