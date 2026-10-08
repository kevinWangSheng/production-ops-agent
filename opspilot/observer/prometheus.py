"""The Observer's own read-only Prometheus access (C3 section 3, decision D3).

One outbound seam, GET only, no proxy, no redirects, bounded read, bounded
time. It is deliberately not ``opspilot.tools.otel_demo``: the Observer holds
its own endpoint and credential (``OPSPILOT_OBSERVER_PROMETHEUS_*``) and
never loads the investigation gateway or the credentials it keeps.

Every call is an *instant* query evaluated at the sample's window end. The
profile's ``query`` carries its own range selector, so an instant evaluation
at ``window_end`` aggregates exactly ``[window_end - range, window_end]``;
``coverage_query`` counts the raw points in that range and
``freshness_query`` returns the newest raw sample's timestamp.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import (
    HTTPConnection,
    HTTPException,
    HTTPResponse,
    HTTPSConnection,
    IncompleteRead,
)
from typing import Any, Literal
from urllib.parse import urlsplit

__all__ = [
    "IN_FLIGHT_LIMIT",
    "InstantResult",
    "InstantStatus",
    "PrometheusReadOnlySource",
    "RESPONSE_LIMIT_BYTES",
]

#: Largest response body kept per query. Three of them, base64-encoded,
#: must fit the stored raw bundle of one reading (128 KiB, migration 0003):
#: 3 x 30 KiB x 4/3 = 120 KiB, leaving room for the bundle's own fields, so
#: a bundle of complete bodies always fits (``sampler._bundle`` asserts it).
#: An instant vector of one series is a few hundred bytes; a body this large
#: is many series, which the profile must have aggregated away in any case.
RESPONSE_LIMIT_BYTES = 30720
_READ_CHUNK = 8192
# A failed status line's body is kept only this far: enough for the
# Prometheus error object, not enough to carry anything surprising.
_ERROR_BODY_BYTES = 2048

InstantStatus = Literal["ok", "no_data", "timeout", "failed"]


@dataclass(frozen=True)
class InstantResult:
    """One instant query as returned: status, the single scalar it yielded
    (``None`` unless ``ok``), the exact body bytes and the HTTP status.

    ``body_complete`` is False when ``body`` is a truncated prefix (the
    response exceeded the limit or the read was cut by the deadline): such a
    result is never ``ok`` and the stored bundle says so.
    """

    expr: str
    status: InstantStatus
    value: float | None
    body: bytes
    http_status: int | None
    # fixed codes only: never a provider message
    detail: str = ""
    body_complete: bool = True


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class PrometheusReadOnlySource:
    """``GET /api/v1/query`` with the Observer's own base URL and token."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        basic_auth: tuple[str, str] | None = None,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.startswith(
            ("http://", "https://")
        ):
            raise ValueError("PROMETHEUS_URL_INVALID")
        parts = urlsplit(base_url)
        if parts.username is not None or parts.password is not None:
            # A credential belongs in the token variable, never in a URL that
            # is logged, stored or echoed (PRODUCT-CONSTRAINTS: credentials
            # stay outside logs and traces; PR #119 review P2-6).
            raise ValueError("PROMETHEUS_URL_HAS_USERINFO")
        if not parts.hostname:
            raise ValueError("PROMETHEUS_URL_INVALID")
        self._base = base_url.rstrip("/")
        self._endpoint = f"{parts.scheme}://{parts.hostname}" + (
            f":{parts.port}" if parts.port else ""
        )
        self._token = token or None
        self._basic_auth = basic_auth
        # ``None``: the deadline-bounded ``_fetch`` path (no proxy, no
        # redirects by construction). An injected opener is the test seam.
        self._opener = opener

    @property
    def base_url(self) -> str:
        return self._base

    @property
    def endpoint(self) -> str:
        """Scheme, host and port only: what logs may say about the source."""
        return self._endpoint

    def instant(
        self, expr: str, *, at: datetime, timeout_seconds: int
    ) -> InstantResult:
        """Evaluate ``expr`` at ``at``; a vector of one sample becomes the value.

        An empty vector is ``no_data``; more than one series, a non-vector
        result, a non-finite value, a non-200 status or an unparsable body is
        ``failed`` (the profile must aggregate to one series); a timeout is
        ``timeout``. Nothing here retries: the sampling job retries later
        under its lease.

        ``timeout_seconds`` bounds the *whole* request -- connect, request,
        response headers and the complete body, success or error -- as one
        absolute deadline (C3 section 4 bounded cancellation, section 8
        gateway total timeout; PR #119 review P2-2 and recheck): a server
        that keeps trickling header or body bytes cannot hold the sampler
        past it (``_fetch``).
        """
        url = f"{self._base}/api/v1/query?" + urllib.parse.urlencode(
            {
                "query": expr,
                "time": f"{at.timestamp():.3f}",
                "timeout": f"{int(timeout_seconds)}s",
            }
        )
        headers = {"Accept": "application/json"}
        if self._basic_auth is not None:
            raw = f"{self._basic_auth[0]}:{self._basic_auth[1]}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        elif self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            if self._opener is not None:
                http_status, body, complete = self._fetch_via_opener(
                    url, headers, timeout_seconds
                )
            else:
                http_status, body, complete = _fetch(url, headers, timeout_seconds)
        except _Timeout as cut:
            # what arrived before the deadline is kept, marked incomplete
            return InstantResult(
                expr,
                "timeout",
                None,
                cut.partial[:_ERROR_BODY_BYTES],
                None,
                "TIMEOUT",
                body_complete=False,
            )
        except _Overloaded:
            return InstantResult(expr, "failed", None, b"", None, "IN_FLIGHT_LIMIT")
        except _Protocol as cut:
            # ``HTTPException`` (``BadStatusLine`` on a non-HTTP answer,
            # ``IncompleteRead`` on a body cut mid-way, ...) is not an
            # ``OSError``: the same transport failure, classified like the
            # investigation client does (issue #126), never a crash out of
            # the sampler. A body that had started to arrive is kept as the
            # prefix it is and marked incomplete (PR #131 codex P2): the
            # bundle never describes an empty body as the full response, and
            # a status line that did arrive (a cut 200 or 401) is recorded
            # rather than filed as "no HTTP response" (codex P2, round 2).
            return InstantResult(
                expr,
                "failed",
                None,
                cut.partial[:_ERROR_BODY_BYTES],
                cut.http_status,
                "UNREACHABLE",
                body_complete=cut.body_complete,
            )
        except (urllib.error.URLError, OSError):
            return InstantResult(expr, "failed", None, b"", None, "UNREACHABLE")
        if http_status != 200:
            return InstantResult(
                expr,
                "failed",
                None,
                body[:_ERROR_BODY_BYTES],
                http_status,
                "HTTP",
                body_complete=complete,
            )
        if not complete or len(body) > RESPONSE_LIMIT_BYTES:
            return InstantResult(
                expr,
                "failed",
                None,
                body[:_ERROR_BODY_BYTES],
                http_status,
                "TOO_LARGE",
                body_complete=False,
            )
        status, value, detail = _single_value(body)
        return InstantResult(expr, status, value, body, http_status, detail)

    def _fetch_via_opener(
        self, url: str, headers: dict[str, str], timeout_seconds: int
    ) -> tuple[int, bytes, bool]:
        """The injected-opener path (tests): same deadline on the body read."""
        assert self._opener is not None
        deadline = time.monotonic() + timeout_seconds
        request = urllib.request.Request(url, headers=headers, method="GET")
        chunks: list[bytes] = []
        status: int | None = None
        try:
            response: HTTPResponse
            with self._opener.open(request, timeout=timeout_seconds) as response:
                status = int(response.status)
                complete = _read_until(
                    response, deadline, RESPONSE_LIMIT_BYTES + 1, chunks
                )
                return status, b"".join(chunks), complete
        except urllib.error.HTTPError as error:
            status = int(error.code)
            try:
                return status, error.read(_ERROR_BODY_BYTES), True
            except HTTPException as cut:
                # the error body was cut mid-read: same classification as
                # below, with the status line that did arrive
                raise _Protocol.of(cut, chunks, status) from None
        except (TimeoutError, socket.timeout):
            raise _Timeout(b"".join(chunks)) from None
        except urllib.error.URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                raise _Timeout(b"".join(chunks)) from None
            raise
        except HTTPException as error:
            raise _Protocol.of(error, chunks, status) from None


class _Timeout(Exception):
    """The absolute deadline passed; ``partial`` is what had arrived."""

    def __init__(self, partial: bytes = b"") -> None:
        super().__init__("deadline")
        self.partial = partial


class _Protocol(Exception):
    """The peer broke the HTTP protocol (``http.client.HTTPException``);
    ``partial`` is what had arrived of the body and ``body_complete`` is
    False once any of it had -- the bytes are a prefix, not the response.
    ``http_status`` is the status line that had arrived before the break
    (None when none had, e.g. ``BadStatusLine``)."""

    def __init__(
        self, partial: bytes, body_complete: bool, http_status: int | None
    ) -> None:
        super().__init__("protocol")
        self.partial = partial
        self.body_complete = body_complete
        self.http_status = http_status

    @classmethod
    def of(
        cls, error: HTTPException, chunks: list[bytes], http_status: int | None
    ) -> _Protocol:
        # ``IncompleteRead`` carries the bytes of the read it cut short; the
        # chunks before it are what the caller had already accumulated
        partial = b"".join(chunks) + (
            bytes(error.partial) if isinstance(error, IncompleteRead) else b""
        )
        started = bool(partial) or isinstance(error, IncompleteRead)
        return cls(partial, body_complete=not started, http_status=http_status)


#: Workers that may still be blocked after their request timed out: a
#: name resolution or a TCP connect cannot be interrupted from outside, so
#: each such worker lives until the OS gives up. One sampler issues requests
#: one at a time (at most one in flight in steady state); four leaves room
#: for a few consecutive timeouts against a hung resolver while keeping the
#: process bounded. Past the limit a request fails immediately without a
#: thread (C3 section 8: cleanup within bounded time; PR #119 recheck 2).
IN_FLIGHT_LIMIT = 4


class _InFlight:
    """Count of HTTP workers started and not yet finished."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._count = 0
        self._lock = threading.Lock()
        self.refused = 0

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

    def acquire(self) -> bool:
        with self._lock:
            if self._count >= self.limit:
                self.refused += 1
                return False
            self._count += 1
            return True

    def release(self) -> None:
        with self._lock:
            self._count -= 1


_IN_FLIGHT = _InFlight(IN_FLIGHT_LIMIT)
_log = logging.getLogger("opspilot.observer")


class _Overloaded(Exception):
    """Too many timed-out workers are still blocked; no new request started."""


def _fetch(
    url: str, headers: dict[str, str], timeout_seconds: int
) -> tuple[int, bytes, bool]:
    """One GET under one absolute deadline: connect, request, response headers
    and body (success or error) together may take ``timeout_seconds``.

    The standard library has no deadline of its own and a peer that trickles
    bytes resets every per-call socket timeout, so the request runs on a
    worker thread that is joined for exactly the time left: at the deadline
    the caller cuts the socket -- the reference taken right after
    ``connect()``, not ``conn.sock``, which ``getresponse()`` hands to the
    response object and clears -- and returns ``timeout`` with whatever
    arrived. Shutting that socket down ends the worker whether it was waiting
    for a header line or a body chunk. Name resolution and TCP connect
    cannot be cut this way; the number of such still-blocked workers is
    bounded by ``IN_FLIGHT_LIMIT`` and a request beyond it fails at once.
    ``http.client`` is driven directly: no proxy, and a redirect is a
    non-200 status (never followed). Returns ``(status, body, complete)``;
    ``complete`` is False when the body hit the read limit.
    """
    parts = urlsplit(url)
    if parts.hostname is None:
        raise OSError("no host")
    deadline = time.monotonic() + timeout_seconds
    connection_class = HTTPSConnection if parts.scheme == "https" else HTTPConnection
    conn = connection_class(parts.hostname, parts.port, timeout=timeout_seconds)
    chunks: list[bytes] = []
    outcome: dict[str, Any] = {}
    held: dict[str, socket.socket] = {}
    seen: dict[str, int] = {}

    def work() -> None:
        try:
            outcome["result"] = _perform(
                conn, parts, headers, deadline, chunks, held, seen
            )
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
            outcome["error"] = exc
        finally:
            conn.close()
            _close(held.get("sock"))
            _IN_FLIGHT.release()

    if not _IN_FLIGHT.acquire():
        _log.warning(
            "prometheus request refused: %s workers still blocked (limit %s), refused so far %s",
            _IN_FLIGHT.count,
            _IN_FLIGHT.limit,
            _IN_FLIGHT.refused,
        )
        raise _Overloaded()
    try:
        worker = threading.Thread(
            target=work, name="opspilot-observer-http", daemon=True
        )
        worker.start()
    except BaseException as exc:  # noqa: BLE001 - e.g. RuntimeError: can't start new thread
        # The slot was taken but no worker will ever release it: give it back
        # here, close the (unconnected) connection and report a failure, so a
        # transient thread shortage cannot pin the process at the limit
        # (PR #119 recheck 3). Each acquired slot is released exactly once.
        _IN_FLIGHT.release()
        conn.close()
        raise OSError(f"worker start failed: {type(exc).__name__}") from None
    worker.join(max(0.0, deadline - time.monotonic()))
    if worker.is_alive():
        _close(held.get("sock"))
        raise _Timeout(b"".join(chunks))
    error = outcome.get("error")
    if isinstance(error, (TimeoutError, socket.timeout)):
        raise _Timeout(b"".join(chunks)) from None
    if isinstance(error, HTTPException):
        raise _Protocol.of(error, chunks, seen.get("status")) from None
    if error is not None:
        raise error
    result: tuple[int, bytes, bool] = outcome["result"]
    return result


def _close(sock: socket.socket | None) -> None:
    """Shut down and close a socket from any thread; a blocked reader on it
    gets EOF or an error and ends."""
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def _perform(
    conn: HTTPConnection,
    parts: Any,
    headers: dict[str, str],
    deadline: float,
    chunks: list[bytes],
    held: dict[str, socket.socket],
    seen: dict[str, int],
) -> tuple[int, bytes, bool]:
    conn.connect()
    if conn.sock is not None:
        # the one reference the cancel path can reach after getresponse()
        held["sock"] = conn.sock
    _arm(conn, deadline)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    conn.putrequest("GET", path, skip_accept_encoding=True)
    for name, value in headers.items():
        conn.putheader(name, value)
    conn.endheaders()
    _arm(conn, deadline)
    # the status line is parsed before the headers: record it the moment it
    # is in, so a header that then breaks the protocol (``LineTooLong``,
    # ...) still reports the status the peer sent (codex review P2)
    conn.response_class = _recording_response(seen)
    response = conn.getresponse()
    status = int(response.status)
    limit = RESPONSE_LIMIT_BYTES + 1 if status == 200 else _ERROR_BODY_BYTES
    complete = _read_until(response, deadline, limit, chunks)
    return status, b"".join(chunks), complete


def _recording_response(seen: dict[str, int]) -> type[HTTPResponse]:
    """An ``HTTPResponse`` that writes the status code into ``seen`` as soon
    as the status line is parsed, before the headers are. ``_read_status``
    is ``http.client`` internal (same reach as the investigation client's
    socket handling); should a Python upgrade rename it the only effect is
    ``http_status`` None again on a header failure, which the real-socket
    tests would show."""

    class Recording(HTTPResponse):
        def _read_status(self) -> tuple[str, int, str]:
            version, status, reason = super()._read_status()  # type: ignore[misc]
            seen["status"] = int(status)
            return version, status, reason

    return Recording


def _arm(conn: HTTPConnection, deadline: float) -> None:
    """Socket timeout := time left before the deadline (or fail now)."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("deadline")
    if conn.sock is not None:
        conn.sock.settimeout(remaining)


def _read_until(
    response: HTTPResponse, deadline: float, limit: int, chunks: list[bytes]
) -> bool:
    """Append at most ``limit`` bytes to ``chunks`` before ``deadline``
    (monotonic seconds); True when the body ended within the limit.

    The socket timeout is re-armed to the remaining time before every chunk,
    so neither a slow first byte nor a slow trickle can run past the
    deadline; running out of time raises ``TimeoutError`` and the caller
    keeps what ``chunks`` holds.
    """
    received = 0
    while received < limit:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("deadline")
        _rearm(response, remaining)
        # ``read1`` returns what one underlying read yields (one chunk of a
        # chunked body) instead of looping until ``amt`` bytes arrived, so the
        # deadline is checked between chunks, not only between calls.
        reader = getattr(response, "read1", response.read)
        chunk = reader(min(_READ_CHUNK, limit - received))
        if not chunk:
            return True
        chunks.append(chunk)
        received += len(chunk)
    return False


def _rearm(response: HTTPResponse, seconds: float) -> None:
    """Set the underlying socket's timeout to ``seconds``; a response without
    a reachable socket (a test double, an already closed stream) keeps the
    timeout the opener set."""
    raw = getattr(getattr(response, "fp", None), "raw", None)
    sock = getattr(raw, "_sock", None)
    if sock is not None and hasattr(sock, "settimeout"):
        sock.settimeout(seconds)


def _single_value(body: bytes) -> tuple[InstantStatus, float | None, str]:
    try:
        payload = json.loads(body)
    except ValueError:
        return "failed", None, "NOT_JSON"
    if not isinstance(payload, dict) or payload.get("status") != "success":
        return "failed", None, "STATUS_NOT_SUCCESS"
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("resultType") != "vector":
        return "failed", None, "NOT_A_VECTOR"
    result = data.get("result")
    if not isinstance(result, list):
        return "failed", None, "NOT_A_VECTOR"
    if not result:
        return "no_data", None, ""
    if len(result) != 1:
        return "failed", None, "MANY_SERIES"
    sample = result[0].get("value") if isinstance(result[0], dict) else None
    if not isinstance(sample, list) or len(sample) != 2:
        return "failed", None, "MALFORMED_SAMPLE"
    try:
        value = float(sample[1])
    except (TypeError, ValueError):
        return "failed", None, "MALFORMED_SAMPLE"
    if not math.isfinite(value):
        # histogram_quantile over an empty histogram is NaN: no data, not a
        # value to judge.
        return "no_data", None, ""
    return "ok", value, ""


def epoch_to_datetime(value: float) -> datetime:
    """A Prometheus ``timestamp()`` result as an aware UTC datetime."""
    return datetime.fromtimestamp(value, tz=timezone.utc)
