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
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import HTTPResponse
from typing import Any, Literal

__all__ = ["InstantResult", "PrometheusReadOnlySource", "RESPONSE_LIMIT_BYTES"]

#: Largest response body kept per query (the M0 response limit; the stored
#: raw bundle of a reading is bound by the same figure in migration 0003).
RESPONSE_LIMIT_BYTES = 131072
# A failed status line's body is kept only this far: enough for the
# Prometheus error object, not enough to carry anything surprising.
_ERROR_BODY_BYTES = 2048

InstantStatus = Literal["ok", "no_data", "timeout", "failed"]


@dataclass(frozen=True)
class InstantResult:
    """One instant query as returned: status, the single scalar it yielded
    (``None`` unless ``ok``), the exact body bytes and the HTTP status."""

    expr: str
    status: InstantStatus
    value: float | None
    body: bytes
    http_status: int | None
    # fixed codes only: never a provider message
    detail: str = ""


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
        self._base = base_url.rstrip("/")
        self._token = token or None
        self._opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )

    @property
    def base_url(self) -> str:
        return self._base

    def instant(
        self, expr: str, *, at: datetime, timeout_seconds: int
    ) -> InstantResult:
        """Evaluate ``expr`` at ``at``; a vector of one sample becomes the value.

        An empty vector is ``no_data``; more than one series, a non-vector
        result, a non-finite value, a non-200 status or an unparsable body is
        ``failed`` (the profile must aggregate to one series); a timeout is
        ``timeout``. Nothing here retries: the sampling job retries later
        under its lease.
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
        try:
            response: HTTPResponse
            with self._opener.open(request, timeout=timeout_seconds) as response:
                body = response.read(RESPONSE_LIMIT_BYTES + 1)
                http_status = int(response.status)
        except urllib.error.HTTPError as error:
            body = error.read(_ERROR_BODY_BYTES)
            return InstantResult(expr, "failed", None, body, int(error.code), "HTTP")
        except (TimeoutError, socket.timeout):
            return InstantResult(expr, "timeout", None, b"", None, "TIMEOUT")
        except urllib.error.URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                return InstantResult(expr, "timeout", None, b"", None, "TIMEOUT")
            return InstantResult(expr, "failed", None, b"", None, "UNREACHABLE")
        except OSError:
            return InstantResult(expr, "failed", None, b"", None, "UNREACHABLE")
        if len(body) > RESPONSE_LIMIT_BYTES:
            return InstantResult(
                expr, "failed", None, body[:_ERROR_BODY_BYTES], http_status, "TOO_LARGE"
            )
        status, value, detail = _single_value(body)
        return InstantResult(expr, status, value, body, http_status, detail)


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
