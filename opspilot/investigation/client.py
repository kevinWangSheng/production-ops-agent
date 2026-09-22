"""Thin DeepSeek Chat Completions client for the investigation loop.

Credentials travel only on the Authorization header and never enter
``ModelCall`` or ``ModelReply``. 400/422 are not retried. 429 and network
errors surface as ``MODEL_UNAVAILABLE`` so the loop can count, re-reserve
and re-check the deadline before any retry.
"""

from __future__ import annotations

import json
import socket
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPSConnection
from typing import Any, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from opspilot.investigation.limits import MAX_HTTP_RESPONSE_BYTES
from opspilot.investigation.loop import (
    ACCEPTED_RESPONSE_MODEL,
    ModelCall,
    ModelError,
    ModelReply,
    serialized_request,
)

_ENDPOINT = "https://api.deepseek.com/v1/chat/completions"
_RETRYABLE_STATUS = frozenset({429, 500, 503})


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class _Opener(Protocol):
    """The one method this client uses from ``urllib``'s ``OpenerDirector``."""

    def open(self, request: Request, timeout: float) -> Any: ...


class _RecordingHTTPSHandler(HTTPSHandler):
    """``HTTPSHandler`` that keeps a handle on the connection it opens.

    ``urllib`` hides the ``HTTPSConnection`` it creates, so once
    ``opener.open()`` is blocked inside the transport thread (TLS
    handshake, status line, headers) nothing on the calling thread can
    reach the socket to stop it. Recording the connection lets ``_post``
    shut the socket down at the deadline, which unblocks the transport
    thread with an ``OSError`` instead of leaving it -- and a live request
    to the provider -- running until the peer decides to stop (bot review
    finding, PR #29). One client serves one loop thread, so a single
    ``current`` slot is enough.
    """

    def __init__(self) -> None:
        super().__init__()
        self.current: HTTPSConnection | None = None

    def https_open(self, req: Request) -> Any:
        # Same call the stdlib handler makes, with our recording factory in
        # place of ``HTTPSConnection``; ``_context`` is the handler's SSL
        # context attribute, absent from the typeshed stub.
        return self.do_open(
            self._connect,
            req,
            context=getattr(self, "_context", None),
        )

    def _connect(self, host: str, **kwargs: Any) -> HTTPSConnection:
        connection = HTTPSConnection(host, **kwargs)
        self.current = connection
        return connection

    def abort(self) -> None:
        """Best-effort: tear down the connection in flight, from any thread."""
        connection, self.current = self.current, None
        if connection is None:
            return
        sock = getattr(connection, "sock", None)
        try:
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            connection.close()
        except Exception:
            pass


class MonotonicClock(Protocol):
    """Wall-clock elapsed time only -- never wall-clock-of-day.

    Deliberately narrower than ``opspilot.tools.executor.Clock``: this
    client never makes a lease/deadline decision (that is the loop's job,
    against ``request.scope.deadline``), so it has no business reading
    calendar time at all, and ``opspilot``'s product code is not allowed to
    call ``datetime.now()`` outside the database clock
    (``DurableStore._db_now``) regardless. Any object with ``.monotonic()``
    -- including a real ``Clock`` or a ``FakeClock`` -- already satisfies
    this.
    """

    def monotonic(self) -> float: ...


class _SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()


class DeepSeekClient:
    """Stdlib HTTPS client. One complete response per ``complete`` call."""

    def __init__(
        self,
        api_key: str,
        *,
        endpoint: str = _ENDPOINT,
        model: str = ACCEPTED_RESPONSE_MODEL,
        clock: MonotonicClock | None = None,
        opener: _Opener | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key:
            raise ModelError("MODEL_REJECTED")
        if not endpoint.startswith("https://"):
            raise ModelError("MODEL_REJECTED")
        self._api_key = api_key
        self._endpoint = endpoint
        self._model = model
        self._clock = clock if clock is not None else _SystemClock()
        self._https: _RecordingHTTPSHandler | None = None
        if opener is None:
            self._https = _RecordingHTTPSHandler()
            self._opener: _Opener = cast(
                _Opener, build_opener(ProxyHandler({}), _NoRedirect(), self._https)
            )
        else:
            self._opener = opener

    def complete(self, call: ModelCall) -> ModelReply:
        """One physical HTTPS request. Retries are the loop's budget decision."""
        body = serialized_request(call)
        raw, status = self._post(body, call.timeout_seconds)
        if status in {400, 401, 402, 422}:
            raise ModelError("MODEL_REJECTED")
        if status in _RETRYABLE_STATUS or status < 200 or status >= 300:
            raise ModelError("MODEL_UNAVAILABLE")
        try:
            payload = json.loads(raw)
        except (ValueError, RecursionError) as exc:
            # RecursionError covers a body nested deep enough to exceed the
            # decoder's recursion limit (bot review finding, PR #29): it is
            # not a ValueError subclass, so it escaped this call and then
            # the loop's _call_model() (which only handles ModelError),
            # crashing the run without a handoff even though the physical
            # request had already completed and been counted. Same pattern
            # already used for adversarial JSON elsewhere in this codebase
            # (opspilot/tools/executor.py, opspilot/investigation/reports.py,
            # opspilot/investigation/loop.py).
            raise ModelError("MODEL_UNAVAILABLE") from exc
        return _parse_reply(payload)

    def _post(self, body: bytes, timeout: float) -> tuple[bytes, int]:
        if timeout <= 0:
            # ``loop.py``'s ``_remaining_timeout()`` already halts with
            # DEADLINE_EXCEEDED/WALL_TIME_EXHAUSTED before ever calling this
            # client with a non-positive value; this is a defensive
            # boundary for any other caller. Fail before any dispatch --
            # never silently substitute a floor (bot review finding, PR
            # #29: a positive-but-sub-100ms budget must not be extended
            # either, see ``budget`` below).
            raise ModelError("MODEL_UNAVAILABLE")
        request = Request(
            self._endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": "Bearer " + self._api_key,
                "Content-Type": "application/json",
            },
        )
        # ``timeout`` here already is ``min(360s frozen limit, remaining
        # deadline, remaining wall)`` (the loop's ``_remaining_timeout``).
        # ``urlopen``'s ``timeout`` only bounds each individual socket
        # operation, not the whole request: a slow trickle -- many small
        # reads, each arriving just under that per-operation timeout --
        # could keep ``_read_capped`` (the body) looping far past this
        # budget, or keep ``opener.open()`` itself (connection
        # establishment, status line, headers) blocking past it before a
        # response object even exists to check anything against (bot
        # review finding, PR #29: no check on *this* thread can run until
        # ``open()`` returns). ``future.result(timeout=budget)`` is the
        # only wall-clock-real backstop that covers both phases: it bounds
        # how long *this* thread waits, unconditionally, regardless of
        # what ``_fetch`` is still doing in the background.
        # ``_tighten_socket_deadline`` below remains the finer-grained,
        # faster-to-fire mechanism for the body-read phase once a response
        # object exists to reach into.
        #
        # ``budget`` is exactly ``timeout`` -- no floor. A floor here used
        # to be a soft, mostly-harmless minimum on a per-socket-operation
        # timeout; now that it also bounds ``future.result(timeout=budget)``
        # (the real wall-clock wait on *this* thread), flooring it would
        # let a request run past an authorized sub-100ms remaining budget,
        # i.e. past when the Run's own deadline/wall limit actually expired
        # (bot review finding, PR #29). The guard above already rules out
        # timeout <= 0, so this is always a well-defined positive value.
        budget = timeout
        deadline = self._clock.monotonic() + budget

        def _fetch() -> tuple[bytes, int]:
            with self._opener.open(request, timeout=budget) as response:
                raw = _read_capped(response, self._clock, deadline)
                return raw, int(response.status)

        pool = ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(_fetch)
            try:
                return future.result(timeout=budget)
            except TimeoutError:
                # The caller's wait is over; do not leave the transport
                # thread (and the provider-side request) running on its own.
                self._abort_transport()
                raise
        except HTTPError as exc:
            # ``exc.read()`` is a raw, untimed blocking call (bot review
            # finding, PR #29): a 429/500/503 whose error body trickles
            # slowly -- each individual read still completing, just slowly
            # -- can hold this call open indefinitely, the exact same gap
            # ``future.result(timeout=...)`` above closes for the success
            # path. Reuse the same pool/backstop rather than a second
            # mechanism; ``OSError`` already covers a timed-out drain
            # (``TimeoutError`` is an ``OSError`` subclass) same as before.
            remaining = deadline - self._clock.monotonic()
            if remaining > 0:
                try:
                    pool.submit(exc.read, MAX_HTTP_RESPONSE_BYTES + 1).result(
                        timeout=remaining
                    )
                except OSError:
                    self._abort_transport()
            else:
                self._abort_transport()
            return b"", int(exc.code)
        except (URLError, TimeoutError, OSError) as exc:
            # ``concurrent.futures.TimeoutError`` (``future.result``'s own
            # timeout, i.e. ``_fetch`` did not finish within ``budget`` at
            # all) is ``TimeoutError`` itself as of this project's pinned
            # Python version -- no separate branch needed.
            raise ModelError("MODEL_UNAVAILABLE") from exc
        finally:
            # Never wait for the fetch here: this call returns within
            # ``budget`` regardless. On a timeout the socket has already been
            # shut down above, so the transport thread is unblocked and
            # exits on its own rather than lingering with a live request.
            pool.shutdown(wait=False)

    def _abort_transport(self) -> None:
        if self._https is not None:
            self._https.abort()


def _tighten_socket_deadline(response: Any, remaining: float) -> None:
    """Best-effort: shrink the underlying socket's own timeout to what is
    actually left of the wall budget.

    Reaches into ``http.client``/``io``/``socket`` internals
    (``response.fp.raw._sock``) that are not a documented contract, so any
    failure to reach them -- a fake test double, a future stdlib change, a
    transport that isn't socket-backed at all -- is silently ignored. The
    per-iteration check in ``_read_capped`` still bounds the number of
    *further* reads either way; this only tightens the one already-slow
    read in flight, which is the gap that check alone cannot close
    (independent review finding, PR #29).
    """
    try:
        response.fp.raw._sock.settimeout(max(remaining, 0.001))
    except AttributeError:
        pass


def _read_capped(response: Any, clock: MonotonicClock, deadline: float) -> bytes:
    chunks = bytearray()
    while True:
        remaining = deadline - clock.monotonic()
        if remaining <= 0:
            raise ModelError("MODEL_UNAVAILABLE")
        _tighten_socket_deadline(response, remaining)
        piece = response.read(65536)
        if not piece:
            break
        chunks.extend(piece)
        if len(chunks) > MAX_HTTP_RESPONSE_BYTES:
            raise ModelError("RESPONSE_TOO_LARGE")
    return bytes(chunks)


def _parse_reply(payload: Mapping[str, Any]) -> ModelReply:
    try:
        choices = payload["choices"]
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError
        choice = choices[0]
        if not isinstance(choice, Mapping):
            raise ValueError
        message = choice["message"]
        if not isinstance(message, Mapping) or message.get("role") != "assistant":
            raise ValueError
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise ValueError
        reasoning = message.get("reasoning_content")
        if reasoning is not None and not isinstance(reasoning, str):
            raise ValueError
        if "tool_calls" not in message or message["tool_calls"] is None:
            calls: list[Any] = []
        else:
            calls = message["tool_calls"]
            if not isinstance(calls, list) or any(
                not isinstance(call, Mapping) for call in calls
            ):
                raise ValueError
        finish = choice.get("finish_reason")
        if not isinstance(finish, str) or not finish:
            raise ValueError
        reported = payload.get("model")
        if not isinstance(reported, str) or not reported:
            raise ValueError
        usage_obj = payload.get("usage")
        usage: dict[str, Any] = dict(usage_obj) if isinstance(usage_obj, dict) else {}
        response_id = payload.get("id")
        if not isinstance(response_id, str) or not response_id:
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise ModelError("MODEL_UNAVAILABLE") from exc
    return ModelReply(
        content=content,
        reasoning_content=reasoning,
        tool_calls=tuple(dict(call) for call in calls),
        finish_reason=finish,
        response_model=reported,
        usage=usage,
        raw={"id": response_id, "usage": usage},
    )
