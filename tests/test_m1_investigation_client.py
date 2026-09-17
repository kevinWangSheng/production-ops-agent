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
