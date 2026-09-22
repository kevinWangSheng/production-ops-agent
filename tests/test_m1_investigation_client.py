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
import time
from urllib.error import HTTPError

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


class _StalledOpenOpener:
    """Simulates a peer that stalls during connection establishment or
    header receipt -- entirely inside ``opener.open()`` itself, before any
    response object (and so no ``_read_capped``/``_tighten_socket_deadline``
    chance) exists at all."""

    def __init__(self, delay: float, body: bytes) -> None:
        self._delay = delay
        self._body = body

    def open(self, request: object, timeout: float) -> _FixedResponse:
        time.sleep(self._delay)
        return _FixedResponse(self._body, status=200)


def test_a_stalled_connect_or_header_phase_is_also_bounded():
    """Bot review finding (comment 4042176056): ``opener.open()`` itself --
    connection establishment and header receipt -- can stall arbitrarily
    long on a slow-trickling peer; ``urlopen``'s ``timeout`` only bounds
    each individual socket operation inside it, and this client's own
    wall-clock deadline only starts being checked once ``open()`` has
    already returned and ``_read_capped`` begins. A real, small sleep
    stands in for a slow peer here: the bound under test is necessarily
    wall-clock-real (a background thread's ``future.result(timeout=...)``),
    not the injectable fake clock the other tests in this file use, since
    Python cannot inject a fake clock into a blocking C-level socket call."""
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
    client = DeepSeekClient(
        "test-key", opener=_StalledOpenOpener(delay=0.3, body=payload)
    )
    started = time.monotonic()
    with pytest.raises(ModelError, match="MODEL_UNAVAILABLE"):
        client.complete(_call(0.05))
    assert time.monotonic() - started < 0.3


class _StalledErrorBody:
    """A fake HTTP error body whose single ``read()`` call blocks for real
    time -- stands in for a 4xx/5xx peer that trickles its error body
    slowly enough that no individual read ever hits a timeout on its own,
    the same shape of gap the connect/header-phase test above covers for
    the success path."""

    def __init__(self, delay: float) -> None:
        self._delay = delay

    def read(self, size: int = -1) -> bytes:
        time.sleep(self._delay)
        return b""

    def close(self) -> None:
        pass


class _ErroringOpener:
    def __init__(self, code: int, body_delay: float) -> None:
        self._code = code
        self._body_delay = body_delay

    def open(self, request: object, timeout: float):
        raise HTTPError(
            "https://api.deepseek.com/v1/chat/completions",
            self._code,
            "error",
            {},
            _StalledErrorBody(self._body_delay),
        )


def test_a_stalled_http_error_body_drain_is_also_bounded():
    """Bot review finding (comment 4044473894): the ``except HTTPError``
    branch drains the error body with a raw, untimed ``HTTPError.read()``
    call in the calling thread -- not through ``_read_capped``'s deadline-
    checked loop -- so a 429/500/503 whose body trickles slowly (each read
    still completing, just slowly) can hold this call open indefinitely,
    bypassing the model timeout and Run wall limit exactly the way the
    connect/header-phase gap above did. A real, small sleep stands in for
    that slow drain; the bound under test is the same wall-clock-real
    ``future.result(timeout=...)`` backstop, reused for this second
    blocking call rather than invented anew."""
    opener = _ErroringOpener(code=500, body_delay=0.3)
    client = DeepSeekClient("test-key", opener=opener)
    started = time.monotonic()
    with pytest.raises(ModelError, match="MODEL_UNAVAILABLE"):
        client.complete(_call(0.05))
    assert time.monotonic() - started < 0.3


class _RecordingOpener:
    """Records whether ``open()`` was ever reached, to prove a call never
    dispatches at all."""

    def __init__(self) -> None:
        self.called = False

    def open(self, request: object, timeout: float) -> _FixedResponse:
        self.called = True
        return _FixedResponse(b"unused", status=200)


def test_a_non_positive_timeout_fails_before_dispatch():
    """A defensive boundary: ``loop.py``'s ``_remaining_timeout()`` already
    halts before ever calling the client with a non-positive value, but the
    client itself must not silently treat zero/negative as "dispatch
    anyway" -- it must fail closed without ever reaching the transport."""
    opener = _RecordingOpener()
    client = DeepSeekClient("test-key", opener=opener)
    with pytest.raises(ModelError, match="MODEL_UNAVAILABLE"):
        client.complete(_call(0))
    assert opener.called is False
    with pytest.raises(ModelError, match="MODEL_UNAVAILABLE"):
        client.complete(_call(-1))
    assert opener.called is False


def test_a_deeply_nested_response_body_is_unavailable_not_a_crash():
    """Bot review finding (comment 4045569482, P2): a successful (status
    200) response whose body is deeply-nested JSON can make
    ``json.loads(raw)`` raise ``RecursionError`` instead of a
    ``json.JSONDecodeError`` (a ``ValueError`` subclass). ``complete()``
    only caught ``ValueError`` around that call, so the ``RecursionError``
    escaped both ``complete()`` and the loop's ``_call_model()`` (which only
    handles ``ModelError``), crashing the run without a handoff even though
    the physical request already completed and was counted. Same adversarial
    body used elsewhere in this codebase for the identical decoder-limit gap
    (``tests/test_m1_tool_outcomes.py``,
    ``tests/test_m1_investigation_loop.py``)."""
    body = b"[" * 20_000 + b"]" * 20_000
    opener = _FixedOpener(body, status=200)
    client = DeepSeekClient("test-key", opener=opener)
    with pytest.raises(ModelError, match="MODEL_UNAVAILABLE"):
        client.complete(_call(5.0))


class _TruncatedResponse(_FixedResponse):
    """A 2xx whose chunked body ends early: ``http.client`` raises
    ``IncompleteRead`` (an ``HTTPException``, not an ``OSError``)."""

    def read(self, size: int = -1) -> bytes:
        from http.client import IncompleteRead

        raise IncompleteRead(b"partial")


class _TruncatedOpener:
    def open(self, request: object, timeout: float) -> _TruncatedResponse:
        return _TruncatedResponse(b"", status=200)


def test_a_truncated_chunked_body_is_unavailable_not_a_crash():
    """Bot review (PR #29, comment 4068748982): ``IncompleteRead`` escaped
    the ``(URLError, TimeoutError, OSError)`` handler and the loop's
    ``ModelError``-only handler, crashing the Run after the request was
    reserved. It is a transport failure like the others."""
    client = DeepSeekClient("test-key", opener=_TruncatedOpener())
    with pytest.raises(ModelError, match="MODEL_UNAVAILABLE"):
        client.complete(_call(5.0))


def test_a_sub_100ms_deadline_is_not_extended_past_the_authorized_budget():
    """Bot review finding (comment 4044987306, P1): ``budget = max(timeout,
    0.1)`` silently extended an authorized sub-100ms remaining budget
    (Run deadline or wall-time nearly exhausted) up to 100ms. Once the
    earlier wall-clamp fix turned this same ``budget`` into the real
    wall-clock bound ``future.result(timeout=budget)`` blocks the calling
    thread on, this floor stopped being a soft, mostly-harmless minimum on
    a per-socket-operation timeout: it became a genuine deadline/control-
    boundary violation, letting the request (and its background transport
    thread) keep running up to 5x past when the Run's authorization
    actually expired. ``_remaining_timeout()`` in loop.py already
    guarantees a strictly positive ``timeout_seconds`` reaches this client
    (it halts with ``DEADLINE_EXCEEDED``/``WALL_TIME_EXHAUSTED`` before
    ever calling it otherwise), so no floor is needed to keep
    ``future.result(timeout=...)`` well-defined -- only the true remaining
    budget must be honoured."""
    opener = _StalledOpenOpener(delay=0.3, body=b"unused")
    client = DeepSeekClient("test-key", opener=opener)
    started = time.monotonic()
    with pytest.raises(ModelError, match="MODEL_UNAVAILABLE"):
        client.complete(_call(0.02))
    assert time.monotonic() - started < 0.08


def test_a_timed_out_transport_is_shut_down_not_orphaned(monkeypatch):
    """Bot review (PR #29, comment 4067523571): ``future.result(timeout=...)``
    bounded only the caller; the transport thread and its socket kept running.
    At the deadline the client now shuts the connection down, which unblocks
    the transport thread instead of leaving a live request behind."""
    import threading

    from opspilot.investigation import client as client_module

    released = threading.Event()
    closed = threading.Event()

    class HangingConnection:
        """Stands in for ``HTTPSConnection`` against a peer that never answers."""

        def __init__(self, host, **kwargs):
            self.sock = None
            self.timeout = kwargs.get("timeout")

        def set_debuglevel(self, level):
            pass

        def request(self, method, url, body=None, headers=None, **kwargs):
            released.wait(5)  # blocked in the handshake/headers phase

        def getresponse(self):
            raise OSError("connection was shut down")

        def close(self):
            closed.set()
            released.set()

    monkeypatch.setattr(client_module, "HTTPSConnection", HangingConnection)
    before = {t.ident for t in threading.enumerate()}
    client = DeepSeekClient("test-key", endpoint="https://provider.invalid/v1")
    started = time.monotonic()
    with pytest.raises(ModelError, match="MODEL_UNAVAILABLE"):
        client._post(b"{}", 0.3)
    assert time.monotonic() - started < 2.0
    assert closed.wait(1.0)  # the deadline tore the connection down
    deadline = time.monotonic() + 2.0
    lingering = []
    while time.monotonic() < deadline:
        lingering = [
            t for t in threading.enumerate() if t.ident not in before and t.is_alive()
        ]
        if not lingering:
            break
        time.sleep(0.05)
    assert lingering == []
    assert client._https is not None and client._https.current is None
