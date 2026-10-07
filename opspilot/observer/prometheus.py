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

import json
import math
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import HTTPResponse
from typing import Any, Literal
from urllib.parse import urlsplit

__all__ = [
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
        self._opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )

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

        ``timeout_seconds`` bounds the *whole* request -- connect, headers
        and the complete body read -- as one absolute deadline (C3 section 4
        bounded cancellation, section 8 gateway total timeout; PR #119 review
        P2-2): a server that keeps trickling bytes cannot hold the sampler
        past it.
        """
        url = f"{self._base}/api/v1/query?" + urllib.parse.urlencode(
            {
                "query": expr,
                "time": f"{at.timestamp():.3f}",
                "timeout": f"{int(timeout_seconds)}s",
            }
        )
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(url, headers=headers, method="GET")
        deadline = time.monotonic() + timeout_seconds
        body = b""
        try:
            response: HTTPResponse
            with self._opener.open(request, timeout=timeout_seconds) as response:
                http_status = int(response.status)
                body = _read_until(response, deadline, RESPONSE_LIMIT_BYTES + 1)
        except urllib.error.HTTPError as error:
            body = error.read(_ERROR_BODY_BYTES)
            return InstantResult(expr, "failed", None, body, int(error.code), "HTTP")
        except (TimeoutError, socket.timeout):
            # what arrived before the deadline is kept, marked incomplete
            return InstantResult(
                expr,
                "timeout",
                None,
                body[:_ERROR_BODY_BYTES],
                None,
                "TIMEOUT",
                body_complete=False,
            )
        except urllib.error.URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                return InstantResult(expr, "timeout", None, b"", None, "TIMEOUT")
            return InstantResult(expr, "failed", None, b"", None, "UNREACHABLE")
        except OSError:
            return InstantResult(expr, "failed", None, b"", None, "UNREACHABLE")
        if len(body) > RESPONSE_LIMIT_BYTES:
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


def _read_until(response: HTTPResponse, deadline: float, limit: int) -> bytes:
    """Read at most ``limit`` bytes before ``deadline`` (monotonic seconds).

    The socket timeout is re-armed to the remaining time before every chunk,
    so neither a slow first byte nor a slow trickle can run past the
    deadline; running out of time raises ``TimeoutError`` with the partial
    body dropped by the caller.
    """
    chunks: list[bytes] = []
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
            break
        chunks.append(chunk)
        received += len(chunk)
    return b"".join(chunks)


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
