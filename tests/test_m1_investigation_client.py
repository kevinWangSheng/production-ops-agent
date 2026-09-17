"""DeepSeekClient's wall clamp on the response body (lease-wire-33 report §6).

``ModelCall.timeout_seconds`` is already ``min(360s frozen limit, remaining
deadline, remaining wall)`` by the time it reaches the client
(``InvestigationLoop._remaining_timeout``). ``urlopen``'s ``timeout``
argument only bounds each individual socket operation, not the whole
response: a slow trickle of small chunks, each arriving just under that
per-read timeout, could keep the read loop going far past the budget the
value was meant to represent. These tests use a fake transport that
advances a fake clock on every ``read()`` instead of sleeping, so the
wall-clamp path is exercised deterministically and instantly.
"""

import json

import pytest

from opspilot.investigation.client import DeepSeekClient
from opspilot.investigation.loop import ModelCall, ModelError
from tests.m1_tool_support import FakeClock


class _SlowResponse:
    """Never runs out of data; the wall clamp must be what stops it."""

    def __init__(self, clock: FakeClock, advance_per_read: float) -> None:
        self._clock = clock
        self._advance = advance_per_read
        self.status = 200

    def __enter__(self) -> "_SlowResponse":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def read(self, size: int = -1) -> bytes:
        self._clock.advance(self._advance)
        return b"x"


class _SlowOpener:
    def __init__(self, clock: FakeClock, advance_per_read: float) -> None:
        self._clock = clock
        self._advance = advance_per_read

    def open(self, request: object, timeout: float) -> _SlowResponse:
        return _SlowResponse(self._clock, self._advance)


class _FixedResponse:
    def __init__(self, body: bytes, *, status: int = 200) -> None:
        self._chunks = [body, b""]
        self.status = status

    def __enter__(self) -> "_FixedResponse":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def read(self, size: int = -1) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


class _FixedOpener:
    def __init__(self, body: bytes, *, status: int = 200) -> None:
        self._body = body
        self._status = status

    def open(self, request: object, timeout: float) -> _FixedResponse:
        return _FixedResponse(self._body, status=self._status)


class _RecordingSocket:
    """Stands in for the ``socket.socket`` at the end of
    ``response.fp.raw._sock``. Records every ``settimeout`` call instead of
    actually bounding anything -- the fake transport never blocks for real,
    so what matters here is only that the client *tries* to shrink it."""

    def __init__(self) -> None:
        self.timeouts: list[float] = []

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)


class _Raw:
    def __init__(self, sock: _RecordingSocket) -> None:
        self._sock = sock


class _Fp:
    def __init__(self, sock: _RecordingSocket) -> None:
        self.raw = _Raw(sock)


class _SlowResponseWithSocketShape(_SlowResponse):
    """Same infinite trickle as ``_SlowResponse``, plus the
    ``fp.raw._sock`` attribute chain a real ``http.client.HTTPResponse``
    exposes, so ``_tighten_socket_deadline`` has something to reach."""

    def __init__(
        self, clock: FakeClock, advance_per_read: float, sock: _RecordingSocket
    ) -> None:
        super().__init__(clock, advance_per_read)
        self.fp = _Fp(sock)


class _SlowOpenerWithSocketShape:
    def __init__(
        self, clock: FakeClock, advance_per_read: float, sock: _RecordingSocket
    ) -> None:
        self._clock = clock
        self._advance = advance_per_read
        self._sock = sock

    def open(self, request: object, timeout: float) -> _SlowResponseWithSocketShape:
        return _SlowResponseWithSocketShape(self._clock, self._advance, self._sock)


def _call(timeout_seconds: float) -> ModelCall:
    return ModelCall(
        messages=({"role": "user", "content": "hi"},),
        tools=None,
        json_mode=True,
        max_tokens=10,
        timeout_seconds=timeout_seconds,
    )


def test_a_slow_trickling_response_is_aborted_as_wall_exhausted():
    clock = FakeClock()
    client = DeepSeekClient(
        "test-key", clock=clock, opener=_SlowOpener(clock, advance_per_read=50)
    )
    with pytest.raises(ModelError, match="MODEL_UNAVAILABLE"):
        client.complete(_call(200))


def test_a_response_finishing_inside_the_wall_budget_still_completes():
    """Regression: an ordinary response well inside the wall clamp is
    unaffected by adding it."""
    clock = FakeClock()
    payload = json.dumps(
        {
            "id": "resp-1",
            "model": "deepseek-flash",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
        }
    ).encode("utf-8")
    client = DeepSeekClient("test-key", clock=clock, opener=_FixedOpener(payload))
    reply = client.complete(_call(200))
    assert reply.content == "ok"
    assert reply.finish_reason == "stop"


def test_wall_clamp_also_shrinks_the_underlying_socket_timeout_per_read():
    """Independent review finding: the per-iteration deadline check alone
    cannot stop a *single* already-in-flight ``read()`` call from outlasting
    the deadline, because ``io.BufferedReader.read(n)`` -- what
    ``http.client.HTTPResponse.read`` actually calls -- loops internally
    over the raw socket until it collects the full ``n`` bytes, with no
    chance for our check to run in between. ``_tighten_socket_deadline``
    must reach ``response.fp.raw._sock`` and shrink its timeout before every
    top-level read, so even that inner loop is bounded by the shrinking
    remainder instead of the original, larger per-call timeout."""
    clock = FakeClock()
    sock = _RecordingSocket()
    client = DeepSeekClient(
        "test-key",
        clock=clock,
        opener=_SlowOpenerWithSocketShape(clock, advance_per_read=50, sock=sock),
    )
    with pytest.raises(ModelError, match="MODEL_UNAVAILABLE"):
        client.complete(_call(200))
    assert len(sock.timeouts) >= 2
    assert sock.timeouts[0] <= 200
    # Each shrink is strictly smaller than the last: the remaining budget
    # keeps shrinking as fake time advances between reads.
    assert all(a > b for a, b in zip(sock.timeouts, sock.timeouts[1:]))


def test_missing_socket_shape_is_ignored_without_breaking_the_wall_clamp():
    """Regression: a transport double (or a future non-socket transport)
    with no ``fp.raw._sock`` chain at all must not crash -- the outer
    per-iteration check (proven above) still applies on its own."""
    clock = FakeClock()
    client = DeepSeekClient(
        "test-key", clock=clock, opener=_SlowOpener(clock, advance_per_read=50)
    )
    with pytest.raises(ModelError, match="MODEL_UNAVAILABLE"):
        client.complete(_call(200))
