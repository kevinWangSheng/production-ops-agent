"""#444 contract: a stuck model-endpoint DNS lookup stays bounded.

``getaddrinfo`` cannot be interrupted. When it blocks forever for the model
endpoint, every ``DeepSeekClient.complete`` must still end within its own
time budget as ``MODEL_UNAVAILABLE``, and repeated requests must not strand
one blocked resolver thread each: the model client owns at most 2
resolution slots (B3-R8/B3-R9). Those slots are its own, so the tool-query
resolver (``opspilot.tools.http_deadline.resolve``) keeps working meanwhile.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from opspilot.investigation.client import DeepSeekClient
from opspilot.investigation.loop import ModelCall, ModelError
from opspilot.tools import http_deadline

MODEL_HOST = "model-dns-stuck.example"
TOOL_HOST = "tool-dns-ok.example"
FAKE_KEY = "fake-test-key-not-a-credential"
BUDGET = 0.3  # seconds per model request
SLACK = 0.7  # scheduling allowance on top of the budget
REQUESTS = 5
MODEL_SLOTS = 2


def _call() -> ModelCall:
    return ModelCall(
        messages=({"role": "user", "content": "ping"},),
        tools=None,
        json_mode=False,
        max_tokens=16,
        timeout_seconds=BUDGET,
    )


class _StuckResolver:
    """``getaddrinfo`` that never returns for ``MODEL_HOST`` until released."""

    def __init__(self, real):
        self._real = real
        self._release = threading.Event()
        self._lock = threading.Lock()
        self.blocked = 0
        self.peak = 0

    def __call__(self, host, port, *args, **kwargs):
        if host == TOOL_HOST:
            return self._real("127.0.0.1", port, *args, **kwargs)
        if host != MODEL_HOST:
            return self._real(host, port, *args, **kwargs)
        with self._lock:
            self.blocked += 1
            self.peak = max(self.peak, self.blocked)
        try:
            # Bounded only so a broken teardown can never hang the suite.
            self._release.wait(timeout=60)
            raise socket.gaierror(socket.EAI_NONAME, "released by test teardown")
        finally:
            with self._lock:
                self.blocked -= 1

    def release(self) -> None:
        self._release.set()


@pytest.fixture
def stuck_dns(monkeypatch):
    resolver = _StuckResolver(socket.getaddrinfo)
    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    try:
        yield resolver
    finally:
        resolver.release()
        end = time.monotonic() + 5
        while resolver.blocked and time.monotonic() < end:
            time.sleep(0.01)


def _settled_thread_count(ceiling: int, wait: float = 2.0) -> int:
    end = time.monotonic() + wait
    count = threading.active_count()
    while count > ceiling and time.monotonic() < end:
        time.sleep(0.02)
        count = threading.active_count()
    return count


def test_stuck_model_dns_returns_unavailable_in_budget_and_threads_stay_bounded(
    stuck_dns,
):
    baseline_threads = threading.active_count()
    client = DeepSeekClient(
        FAKE_KEY, endpoint=f"https://{MODEL_HOST}/v1/chat/completions"
    )

    for attempt in range(REQUESTS):
        started = time.monotonic()
        with pytest.raises(ModelError) as raised:
            client.complete(_call())
        elapsed = time.monotonic() - started
        assert raised.value.code == "MODEL_UNAVAILABLE", attempt
        assert elapsed <= BUDGET + SLACK, (attempt, elapsed)

    # No more than the model client's own slots are ever stuck in the resolver.
    assert stuck_dns.peak <= MODEL_SLOTS, stuck_dns.peak
    assert stuck_dns.blocked <= MODEL_SLOTS, stuck_dns.blocked
    # Transport threads end once their deadline passes; only the stuck
    # lookups (at most MODEL_SLOTS) may outlive the requests.
    ceiling = baseline_threads + MODEL_SLOTS
    assert _settled_thread_count(ceiling) <= ceiling

    # The tool-query resolver's slots are separate: with the model resolver
    # stuck, a tool-side hostname lookup still resolves promptly.
    started = time.monotonic()
    infos = http_deadline.resolve(TOOL_HOST, 443, time.monotonic() + 1.0)
    assert infos and time.monotonic() - started < 1.0
