"""Grafana Loki log search (tool expansion C5a, #569).

The one tool of upstream HolmesGPT's ``grafana/loki`` toolset
(``holmes/plugins/toolsets/grafana/loki/toolset_grafana_loki.py`` at
``a0009195``), same name and parameters, served only by ``otel-demo-ext`` and
only when ``OPSPILOT_LOKI_URL`` is set (decision table r10, C5-U1, C5-0 to
C5-15; B1-U1 and B1-U2 carried over; docs/tasks/2026-11-18-tool-expansion-c5.md):

* ``grafana_loki_query`` -- ``GET /loki/api/v1/query_range`` with the query
  string built here from fixed keys (``query``, ``start``, ``end``,
  ``limit``, ``direction=backward``), as upstream's ``loki_api.py``. Nothing
  else can be sent: no POST, no other path (C5-3).

Time (B1-U1, C5-6, C5-8): every call is bounded to the query window
(``start``/``end`` inside the authorized frame, default the last hour of it);
the model cannot remove it. Outside the LogQL string literals, ``offset`` and
``@`` are refused, every range selector ``[d]`` must parse as a Prometheus
duration no longer than the query window, and a metric query is evaluated
from the window start plus its longest (nested ranges added) range, so no
evaluation point reads a line before the window -- the PromQL rule of
``otel_demo.promql_problem``. Inside string literals a ``[0-9]`` is a regex,
not a range.

Read-only by construction (C5-3): one fixed GET; no redirects, no proxy. The
``loki-ro`` credential (Basic user and password, or a bearer token) is this
transport's own slot and is never rendered; the tenant (``X-Scope-OrgID``)
comes only from configuration (C5-0). The read-only gateway in front of Loki
is the second layer (C5-U1).

The address may carry a path prefix -- a gateway's or Grafana's
datasource-proxy base path (``/api/datasources/proxy/uid/<uid>``) -- and the
request goes to ``<prefix>/loki/api/v1/query_range`` (lead decision
2026-11-18); userinfo, a query string or a fragment in it is
``LOKI_CONFIG_INVALID``.
"""

from __future__ import annotations

import base64
import json
import math
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from opspilot.tools import otel_demo
from opspilot.tools.elasticsearch_logs import (
    _cut_strings,
    _Fetched,
    _monotonic,
    _names,
    _printable,
    _remaining,
    es_opener,
)
from opspilot.tools.executor import (
    TransportError,
    TransportRequest,
    TransportResponse,
    TransportResultTooLarge,
    TransportTimeout,
    TransportUnavailable,
)
from opspilot.tools.http_deadline import DeadlineHandlers, RequestSockets
from opspilot.tools.outcomes import Window
from opspilot.tools.registry import (
    MAX_RESULT_BYTES,
    MAX_VIEW_TOKENS,
    ParameterSpec,
    ToolContractError,
    ToolDescription,
    ToolRegistration,
    canonical,
)

__all__ = [
    "BEARER_TOKEN_ENV",
    "CA_FILE_ENV",
    "CREDENTIAL_REF",
    "ENDPOINT_SELECTOR",
    "PASSWORD_ENV",
    "QUERY_PATH",
    "QUERY_TOOL",
    "TENANT_ID_ENV",
    "TOOL_NAMES",
    "TOOL_SCHEMAS",
    "URL_ENV",
    "USERNAME_ENV",
    "LokiClient",
    "LokiConfig",
    "LokiTransport",
    "logql_problem",
    "loki_enabled",
    "loki_registrations",
    "overflow_summary",
]

QUERY_TOOL = "grafana_loki_query"
TOOL_NAMES: tuple[str, ...] = (QUERY_TOOL,)

#: C5-4: this transport's own credential slot; never another backend's.
CREDENTIAL_REF = "loki-ro"
#: C5-10: where the address travels in the lab target's selector.
ENDPOINT_SELECTOR = "loki_endpoint"
#: C5-3: the only path this tool sends to.
QUERY_PATH = "/loki/api/v1/query_range"

URL_ENV = "OPSPILOT_LOKI_URL"
USERNAME_ENV = "OPSPILOT_LOKI_USERNAME"
PASSWORD_ENV = "OPSPILOT_LOKI_PASSWORD"
BEARER_TOKEN_ENV = "OPSPILOT_LOKI_BEARER_TOKEN"
CA_FILE_ENV = "OPSPILOT_LOKI_CA_FILE"
TENANT_ID_ENV = "OPSPILOT_LOKI_TENANT_ID"
_OTHER_ENVS = (
    USERNAME_ENV,
    PASSWORD_ENV,
    BEARER_TOKEN_ENV,
    CA_FILE_ENV,
    TENANT_ID_ENV,
)

REQUEST_TIMEOUT_SECONDS = 30.0
#: C5-10: raw read and returned body ceiling (the registry maximum, 2 MiB).
READ_LIMIT_BYTES = MAX_RESULT_BYTES
#: A 4xx error body is read this far.
ERROR_BODY_BYTES = 8192
#: C5-10.
DEFAULT_LIMIT = 100
MAX_LIMIT = 500
MAX_VALUE_CHARS = 2000
#: C5-6.
MAX_QUERY_CHARS = 4000
#: Loki's own tenant ID limit (``tenant.MaxTenantIDLength``).
MAX_TENANT_CHARS = 150

# Loki's lexer runs Go's text/scanner: "..." with escapes and raw `...`
# without, ' starts a char literal, /* */ and // are comments, and LogQL
# adds # comments to the end of the line (independent review, PR #724).
_DOUBLE_QUOTED = re.compile(r'"(?:\\.|[^"\\])*"')
_OFFSET = re.compile(r"\boffset\b", re.IGNORECASE)
# Prometheus duration syntax (``model.ParseDuration``): units in descending
# order, each at most once.
_DURATION = re.compile(
    r"(?:(\d+)y)?(?:(\d+)w)?(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?(?:(\d+)ms)?"
)
_DURATION_SECONDS = (365 * 86400, 7 * 86400, 86400, 3600, 60, 1, 0.001)


# -- configuration -------------------------------------------------------------


def loki_enabled(env: Mapping[str, str]) -> bool:
    """B1-U2 for Loki: whether the extended profile shows the tool. Only the
    address is read -- the web calls this too and never reads a credential."""
    return bool(env.get(URL_ENV))


def _clean_url(value: str) -> str:
    try:
        parts = urllib.parse.urlsplit(value)
        parts.port
    except ValueError:
        raise ToolContractError("LOKI_CONFIG_INVALID") from None
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or "@" in parts.netloc
        or parts.query
        or parts.fragment
        or "?" in value
        or "#" in value
    ):
        raise ToolContractError("LOKI_CONFIG_INVALID")
    # A path prefix (gateway or Grafana datasource proxy) is kept; the query
    # path is appended to it.
    return value.rstrip("/")


@dataclass(frozen=True)
class LokiConfig:
    """The worker's ``loki-ro`` connection (C5-4): address, one of a user
    name and password or a bearer token (required, C5-U1), a CA file for
    HTTPS and the tenant the operator configured (C5-0).

    Read only by the worker when it builds the executor; the web reads the
    address alone (``loki_enabled``). The secret values are never rendered.
    """

    url: str
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    bearer_token: str | None = field(default=None, repr=False)
    ca_file: str | None = None
    tenant_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url:
            raise ToolContractError("LOKI_CONFIG_MISSING")
        object.__setattr__(self, "url", _clean_url(self.url))
        has_token = self.bearer_token is not None
        has_basic = self.username is not None or self.password is not None
        if not has_token and not has_basic:
            raise ToolContractError("LOKI_CONFIG_MISSING")
        if has_token and has_basic:
            raise ToolContractError("LOKI_CONFIG_INVALID")
        # Half a user/password pair is a malformed value, not a missing one.
        values = [self.bearer_token] if has_token else [self.username, self.password]
        if any(not isinstance(value, str) or not _printable(value) for value in values):
            raise ToolContractError("LOKI_CONFIG_INVALID")
        if has_token and any(ch.isspace() for ch in str(self.bearer_token)):
            raise ToolContractError("LOKI_CONFIG_INVALID")
        if has_basic and ":" in str(self.username):
            raise ToolContractError("LOKI_CONFIG_INVALID")
        if self.tenant_id is not None and not _tenant_ok(self.tenant_id):
            # C5-0: one tenant per deployment; ``a|b`` would be Loki's
            # cross-tenant query federation.
            raise ToolContractError("LOKI_CONFIG_INVALID")
        if self.ca_file is not None:
            if not self.url.startswith("https:"):
                raise ToolContractError("LOKI_CONFIG_INVALID")
            path = Path(self.ca_file)
            if not path.is_file() or not os.access(path, os.R_OK):
                raise ToolContractError("LOKI_CONFIG_INVALID")

    @property
    def authorization(self) -> str:
        """The ``Authorization`` header value behind ``CREDENTIAL_REF``."""
        if self.bearer_token is not None:
            return f"Bearer {self.bearer_token}"
        pair = f"{self.username}:{self.password}".encode()
        return "Basic " + base64.b64encode(pair).decode("ascii")

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> LokiConfig | None:
        """``OPSPILOT_LOKI_*`` (C5-4). No address: ``None`` -- unless a
        credential, CA file or tenant is set, which is
        ``LOKI_CONFIG_INVALID`` so a setting is never silently ignored. With
        an address, neither a user name and password nor a bearer token is
        ``LOKI_CONFIG_MISSING``; both (or half a pair) ``LOKI_CONFIG_INVALID``."""
        url = env.get(URL_ENV) or ""
        values = {name: env.get(name) or None for name in _OTHER_ENVS}
        if not url:
            if any(values.values()):
                raise ToolContractError("LOKI_CONFIG_INVALID")
            return None
        return cls(
            url=url,
            username=values[USERNAME_ENV],
            password=values[PASSWORD_ENV],
            bearer_token=values[BEARER_TOKEN_ENV],
            ca_file=values[CA_FILE_ENV],
            tenant_id=values[TENANT_ID_ENV],
        )


def _tenant_ok(value: object) -> bool:
    return (
        isinstance(value, str)
        and _printable(value)
        and len(value) <= MAX_TENANT_CHARS
        and "|" not in value
        and not any(ch.isspace() for ch in value)
    )


# -- HTTP ----------------------------------------------------------------------


class LokiClient:
    """The one request this backend may receive: ``GET`` of ``QUERY_PATH``
    with the query string built here (C5-3).

    ``credentials`` maps ``CREDENTIAL_REF`` to the ``Authorization`` header
    value; a missing slot is ``CREDENTIAL_REF_UNKNOWN`` before anything is
    sent, and no other slot is ever tried. ``tenant_id`` is the configured
    ``X-Scope-OrgID`` (C5-0). ``opener`` replaces the default.
    """

    def __init__(
        self,
        *,
        credentials: Mapping[str, str | None],
        tenant_id: str | None = None,
        opener: urllib.request.OpenerDirector | None = None,
        ca_file: str | None = None,
    ) -> None:
        self._credentials = dict(credentials)
        self._tenant_id = tenant_id
        self._opener = opener or es_opener(ca_file)
        self._deadline_handlers = DeadlineHandlers(self._opener)

    def authorization(self) -> str:
        value = self._credentials.get(CREDENTIAL_REF)
        if not value:
            raise TransportError("CREDENTIAL_REF_UNKNOWN")
        return value

    def query_range(
        self,
        endpoint: str,
        query: Sequence[tuple[str, str]],
        *,
        deadline: float,
        max_bytes: int,
    ) -> _Fetched:
        url = f"{endpoint}{QUERY_PATH}?" + urllib.parse.urlencode(list(query))
        headers = {
            "Accept": "application/json",
            "Authorization": self.authorization(),
        }
        if self._tenant_id is not None:
            headers["X-Scope-OrgID"] = self._tenant_id
        http_request = urllib.request.Request(url, headers=headers, method="GET")
        sockets = RequestSockets(_remaining(deadline))

        def read() -> _Fetched:
            self._deadline_handlers.local.sockets = sockets
            try:
                try:
                    with self._opener.open(
                        http_request, timeout=sockets.remaining()
                    ) as response:
                        body = response.read(max_bytes + 1)
                        status = int(response.status)
                except urllib.error.HTTPError as error:
                    with error:
                        body = error.read(ERROR_BODY_BYTES)
                    code = int(error.code)
                    if code >= 500:
                        raise TransportUnavailable(str(code)) from None
                    return _Fetched(code, body)
                if len(body) > max_bytes:
                    raise TransportResultTooLarge(str(max_bytes))
                return _Fetched(status, body)
            finally:
                del self._deadline_handlers.local.sockets

        future: Future[_Fetched] = Future()

        def run() -> None:
            try:
                future.set_result(read())
            except BaseException as error:
                future.set_exception(error)

        thread = threading.Thread(target=run, daemon=True, name="loki-query")
        try:
            thread.start()
            return future.result(timeout=sockets.remaining())
        except TimeoutError:
            raise TransportTimeout("TIMEOUT") from None
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise TransportTimeout("TIMEOUT") from None
            raise TransportUnavailable("UNREACHABLE") from None
        except OSError:
            raise TransportUnavailable("UNREACHABLE") from None
        finally:
            sockets.abort()


# -- LogQL checks ------------------------------------------------------------------


class _Construct(Exception):
    """A token the outside-string scan refuses (``'``, ``/*``, ``//``)."""


def _without_strings(query: str) -> str:
    """The query with every string literal blanked to ``""`` and a ``#``
    comment dropped, scanned left to right as Loki's tokenizer reads it.

    Raises ``_Construct`` for a char literal (``'``) or a ``/*``/``//``
    comment outside a string: Loki skips or re-reads what they cover, so a
    quote inside one would desynchronise this scan from Loki's. An
    unterminated literal stays visible (fail closed; Loki refuses it).
    """
    out: list[str] = []
    index = 0
    while index < len(query):
        char = query[index]
        if char == '"':
            match = _DOUBLE_QUOTED.match(query, index)
            if match is None:
                out.append(query[index:])
                break
            out.append('""')
            index = match.end()
            continue
        if char == "`":
            close = query.find("`", index + 1)
            if close < 0:
                out.append(query[index:])
                break
            out.append('""')
            index = close + 1
            continue
        if char == "'":
            raise _Construct("'")
        if char == "/" and query[index + 1 : index + 2] in ("*", "/"):
            raise _Construct(query[index : index + 2])
        if char == "#":
            break  # a comment to the end of the (single-line) query
        out.append(char)
        index += 1
    return "".join(out)


def _duration_seconds(text: str) -> float | None:
    """A Prometheus duration in seconds, or ``None`` when it does not parse
    or is zero."""
    match = _DURATION.fullmatch(text)
    if not text or match is None:
        return None
    total = sum(
        int(part) * unit
        for part, unit in zip(match.groups(), _DURATION_SECONDS, strict=True)
        if part is not None
    )
    return total if total > 0 else None


def _logql_lookback(stripped: str) -> float | None:
    """Total seconds one evaluation point of ``stripped`` (a query with its
    string literals blanked) reads back, or ``None`` when a ``[...]`` is not
    one Prometheus duration or a ``)`` has no ``(``.

    The bracket-depth scan of ``otel_demo._promql_lookback``: a range applies
    to the operand just before it (a parenthesised group adds its own
    lookback); parallel ranges take the maximum. LogQL has no subqueries, so
    ``[a:b]`` does not parse.
    """
    frames: list[float] = [0.0]
    last = 0.0
    index = 0
    while index < len(stripped):
        char = stripped[index]
        if char == "(":
            frames.append(0.0)
            last = 0.0
        elif char == ")":
            if len(frames) == 1:
                return None
            last = frames.pop()
            frames[-1] = max(frames[-1], last)
        elif char == "[":
            close = stripped.find("]", index)
            if close < 0:
                return None
            seconds = _duration_seconds(stripped[index + 1 : close].strip())
            if seconds is None:
                return None
            last = seconds + last
            frames[-1] = max(frames[-1], last)
            index = close
        elif char == "]":
            return None
        elif not char.isspace():
            last = 0.0
        index += 1
    return max(frames)


def logql_problem(query: str, window_seconds: float) -> tuple[str, str] | None:
    """``(code, text)`` when this LogQL must not be sent, else ``None``
    (C5-6). Decided before anything is sent."""
    if (
        not isinstance(query, str)
        or not query.strip()
        or len(query) > MAX_QUERY_CHARS
        or not query.isprintable()
    ):
        return "INVALID_QUERY", (
            f"query must be a non-empty LogQL query of at most {MAX_QUERY_CHARS} "
            "printable characters (no line breaks or tabs)."
        )
    try:
        stripped = _without_strings(query)
    except _Construct as found:
        return "QUERY_CONSTRUCT_NOT_ALLOWED", (
            f"{found} outside a string literal is refused: no char literals "
            "('...') and no /* */ or // comments; quote strings with \"...\" "
            "or `...`. A # comment is allowed."
        )
    if _OFFSET.search(stripped) or "@" in stripped:
        return "QUERY_OUT_OF_WINDOW", (
            "offset and @ move the evaluation outside the query window and "
            "are refused; choose the window with start and end instead."
        )
    lookback = _logql_lookback(stripped)
    if lookback is None:
        return "QUERY_OUT_OF_WINDOW", (
            "every [...] outside a string literal must be one range duration "
            "such as [5m], [1h] or [90s] (no subquery [a:b])."
        )
    # Rounded up as reported (``lookback_seconds`` is whole seconds).
    if math.ceil(lookback) > window_seconds:
        return "QUERY_OUT_OF_WINDOW", (
            f"the query reads {_seconds_text(lookback)} back from each point, "
            f"longer than the {_seconds_text(window_seconds)} query window; use "
            "a shorter range or a wider start/end inside the authorized window."
        )
    return None


def logql_lookback_seconds(query: str) -> int:
    """Whole seconds (rounded up) each evaluation point of an accepted query
    reads back; 0 for a log (stream) query or one that does not parse."""
    try:
        lookback = _logql_lookback(_without_strings(query)) or 0.0
    except _Construct:
        return 0
    return math.ceil(lookback)


def metric_step_seconds(range_seconds: float) -> int:
    """The step sent with a metric query (lead decision, PR #724 review):
    Loki's own default, ``max(floor(range / 250), 1)`` seconds."""
    return max(math.floor(range_seconds / 250), 1)


def _aligned(start: datetime, end: datetime, step: int) -> tuple[datetime, datetime]:
    """``start`` rounded up and ``end`` down to epoch multiples of ``step``
    seconds, so Loki's own alignment (start down, end up) changes nothing."""
    low = math.ceil(start.timestamp() / step) * step
    high = math.floor(end.timestamp() / step) * step
    return (
        datetime.fromtimestamp(low, tz=timezone.utc),
        datetime.fromtimestamp(high, tz=timezone.utc),
    )


def _seconds_text(seconds: float) -> str:
    return f"{seconds:g} s"


# -- responses -------------------------------------------------------------------


def _decode(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None


def _error_text(body: bytes) -> str | None:
    """Loki's error body: plain text, or JSON with ``error``/``message``."""
    text = body.decode("utf-8", errors="replace").strip()
    payload = _decode(body)
    if isinstance(payload, dict):
        for key in ("error", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return text or None


def _source_error(fetched: _Fetched) -> TransportResponse:
    """C5-11: a 4xx as its fixed class; a 400 (LogQL error, Loki limit)
    echoes Loki's text (the executor redacts and bounds it)."""
    text = _error_text(fetched.body) if fetched.status == 400 else None
    return TransportResponse(
        body=fetched.body,
        source_status=str(fetched.status),
        http_status=fetched.status,
        source_error_text=text,
    )


def _ns_time(value: object) -> int | None:
    if not isinstance(value, str) or not value.isdigit():
        return None
    return int(value)


def _rfc3339_ns(ns: int) -> str:
    seconds, fraction = divmod(ns, 1_000_000_000)
    moment = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + f".{fraction:09d}Z"


def _from_ns(ns: int) -> datetime:
    seconds, fraction = divmod(ns, 1_000_000_000)
    return datetime.fromtimestamp(seconds, tz=timezone.utc) + timedelta(
        microseconds=fraction // 1000
    )


def _clamp(low: datetime, high: datetime, window: Window) -> tuple[datetime, datetime]:
    low = min(max(low, window.start), window.end)
    high = max(min(high, window.end), low)
    return low, high


def _z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# -- transport -------------------------------------------------------------------


class LokiTransport:
    """Serves ``grafana_loki_query`` over one ``LokiClient`` (``loki-ro``).

    Every refusal of the model's input is decided before anything is sent.
    """

    def __init__(
        self,
        *,
        credentials: Mapping[str, str | None],
        tenant_id: str | None = None,
        opener: urllib.request.OpenerDirector | None = None,
        ca_file: str | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._client = LokiClient(
            credentials=credentials,
            tenant_id=tenant_id,
            opener=opener,
            ca_file=ca_file,
        )
        self._monotonic = monotonic or _monotonic

    def fetch(self, request: TransportRequest) -> TransportResponse:
        if request.tool not in TOOL_NAMES:
            raise TransportError("TOOL_NOT_SUPPORTED")
        deadline = self._monotonic() + request.timeout_seconds
        return self._query(request, deadline)

    def _endpoint(self, request: TransportRequest) -> str:
        self._client.authorization()  # a missing slot fails before sending
        endpoint = request.selector.get(ENDPOINT_SELECTOR, "")
        if not endpoint:
            raise TransportError("LOKI_ENDPOINT_MISSING")
        return endpoint.rstrip("/")

    def _query(self, request: TransportRequest, deadline: float) -> TransportResponse:
        params = request.params
        query = params.get("query")
        if not isinstance(query, str) or not query.strip():
            return _refused("INVALID_QUERY", _QUERY_RULE)
        limit = params.get("limit", DEFAULT_LIMIT)
        if limit is None:
            limit = DEFAULT_LIMIT
        if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
            return _refused("INVALID_LIMIT", f"limit must be 1 to {MAX_LIMIT}.")
        window, refusal = _query_window(request)
        if refusal is not None:
            return refusal
        assert window is not None
        problem = logql_problem(query, window.seconds)
        if problem is not None:
            return _refused(*problem)
        lookback = logql_lookback_seconds(query)
        keys: list[tuple[str, str]] = [("query", query)]
        read_window = window
        if lookback:
            # C5-6: a metric query is evaluated from the window start plus
            # its lookback, so the first point reads from the window start.
            # Loki rounds start down and end up to the step (PR #724
            # review), so the step is sent and both are pre-aligned inside.
            start = window.start + timedelta(seconds=lookback)
            step = metric_step_seconds((window.end - start).total_seconds())
            start, end = _aligned(start, window.end, step)
            if start > end:
                return _refused(
                    "QUERY_OUT_OF_WINDOW",
                    f"after aligning to the {step} s step no evaluation point "
                    f"lies in the window; widen start/end or use a range "
                    f"shorter than {lookback} s.",
                )
            read_window = Window(start - timedelta(seconds=lookback), end)
            keys += [("start", _z(start)), ("end", _z(end)), ("step", f"{step}s")]
        else:
            keys += [("start", _z(window.start)), ("end", _z(window.end))]
        keys += [("limit", str(limit)), ("direction", "backward")]
        endpoint = self._endpoint(request)
        fetched = self._client.query_range(
            endpoint, keys, deadline=deadline, max_bytes=READ_LIMIT_BYTES
        )
        if fetched.status != 200:
            return _source_error(fetched)
        payload = _decode(fetched.body)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get("result"), list):
            return TransportResponse(body=b"")  # MALFORMED_RESULT
        result_type = data.get("resultType")
        if result_type == "streams":
            return self._streams(request, data["result"], window, limit)
        if result_type == "matrix":
            return self._matrix(request, data["result"], read_window, lookback)
        return TransportResponse(body=b"")

    def _streams(
        self,
        request: TransportRequest,
        result: list[Any],
        window: Window,
        limit: int,
    ) -> TransportResponse:
        entries: list[tuple[int, int, dict[str, Any], str]] = []
        for order, stream in enumerate(result):
            if not isinstance(stream, dict):
                return TransportResponse(body=b"")
            labels = stream.get("stream")
            values = stream.get("values")
            if not isinstance(labels, dict) or not isinstance(values, list):
                return TransportResponse(body=b"")
            for value in values:
                if not isinstance(value, list) or len(value) < 2:
                    return TransportResponse(body=b"")
                ns = _ns_time(value[0])
                line = value[1]
                if ns is None or not isinstance(line, str):
                    return TransportResponse(body=b"")
                entries.append((ns, order, labels, line))
        # Newest first across streams (Loki orders within a stream only).
        entries.sort(key=lambda entry: (-entry[0], entry[1]))
        returned = len(entries)
        kept = entries[:limit]
        cut: list[str] = []
        rows: list[dict[str, Any]] = []
        for ns, _, labels, line in kept:
            row_cut: list[str] = []
            row: dict[str, Any] = _cut_strings(
                {"timestamp": _rfc3339_ns(ns), "labels": dict(labels), "line": line},
                "",
                row_cut,
            )
            if row_cut:
                row["_truncated_fields"] = row_cut
                cut.extend(path for path in row_cut if path not in cut)
            rows.append(row)
        start_at = end_at = None
        if kept:
            start_at, end_at = _clamp(
                _from_ns(min(entry[0] for entry in kept)),
                _from_ns(max(entry[0] for entry in kept)),
                window,
            )
        reasons: list[str] = []
        if returned >= limit:
            reasons.append(
                f"limit: {limit} lines were returned, the limit; older lines in "
                "the window were not returned (Loki reports no total). Narrow "
                "the selector or line filters, shorten the window, or count "
                "with count_over_time"
            )
        notes = [f"Incomplete result ({reason})." for reason in reasons]
        if cut:
            notes.append(
                f"String values longer than {MAX_VALUE_CHARS} characters are cut "
                f"there ({_names(cut)}); each line lists its cut fields under "
                "_truncated_fields."
            )
        notes.append(_window_note(window))
        return self._respond(
            request,
            rows,
            result_type="streams",
            row_unit="lines",
            window=window,
            start_at=start_at,
            end_at=end_at,
            lookback=None,
            limit=limit,
            cut=cut,
            reasons=reasons,
            notes=notes,
            extra={"streams_returned": len(result)},
        )

    def _matrix(
        self,
        request: TransportRequest,
        result: list[Any],
        window: Window,
        lookback: int,
    ) -> TransportResponse:
        times: list[float] = []
        cut: list[str] = []
        rows: list[dict[str, Any]] = []
        for series in result:
            if not isinstance(series, dict):
                return TransportResponse(body=b"")
            metric = series.get("metric")
            values = series.get("values")
            if not isinstance(metric, dict) or not isinstance(values, list):
                return TransportResponse(body=b"")
            for point in values:
                if (
                    not isinstance(point, list)
                    or len(point) != 2
                    or isinstance(point[0], bool)
                    or not isinstance(point[0], (int, float))
                    or not math.isfinite(point[0])
                ):
                    return TransportResponse(body=b"")
                times.append(float(point[0]))
            row_cut: list[str] = []
            row: dict[str, Any] = _cut_strings(
                {"metric": dict(metric), "values": values}, "", row_cut
            )
            if row_cut:
                row["_truncated_fields"] = row_cut
                cut.extend(path for path in row_cut if path not in cut)
            rows.append(row)
        start_at = end_at = None
        if times:
            start_at, end_at = _clamp(
                datetime.fromtimestamp(min(times), tz=timezone.utc),
                datetime.fromtimestamp(max(times), tz=timezone.utc),
                window,
            )
        notes: list[str] = []
        if cut:
            notes.append(
                f"String values longer than {MAX_VALUE_CHARS} characters are cut "
                f"there ({_names(cut)})."
            )
        notes.append(_window_note(window))
        if lookback:
            notes.append(
                f"The longest range reads {lookback} s back, so evaluation "
                f"started at {_z(window.start + timedelta(seconds=lookback))}: "
                f"the requested window start plus {lookback} s, rounded up to "
                "the step, at most one step later. No point counts a line from "
                f"before {_z(window.start)}."
            )
        return self._respond(
            request,
            rows,
            result_type="matrix",
            row_unit="series",
            window=window,
            start_at=start_at,
            end_at=end_at,
            lookback=lookback,
            limit=None,
            cut=cut,
            reasons=[],
            notes=notes,
            extra={},
        )

    @staticmethod
    def _respond(
        request: TransportRequest,
        rows: list[dict[str, Any]],
        *,
        result_type: str,
        row_unit: str,
        window: Window,
        start_at: datetime | None,
        end_at: datetime | None,
        lookback: int | None,
        limit: int | None,
        cut: list[str],
        reasons: list[str],
        notes: list[str],
        extra: Mapping[str, Any],
    ) -> TransportResponse:
        body = canonical({"results": rows, "result_type": result_type}).encode("utf-8")
        if len(body) > request.max_result_bytes:
            raise TransportResultTooLarge(str(request.max_result_bytes))
        view_fields: dict[str, Any] = {
            "result_type": result_type,
            "direction": "backward",
            "long_values_cut": len(cut),
            "incomplete_reason": "; ".join(reasons) or None,
            "notes": notes,
            "cannot_prove": _CANNOT_PROVE,
            **extra,
        }
        if limit is not None:
            view_fields["limit"] = limit
        return TransportResponse(
            body=body,
            source_start_at=start_at,
            source_end_at=end_at,
            lookback_seconds=lookback or None,
            query_window=window,
            row_unit=row_unit,
            backend_rows_returned=len(rows),
            incomplete=bool(reasons),
            view_fields=view_fields,
        )


def _refused(code: str, text: str) -> TransportResponse:
    """A fixed-code refusal decided before anything was sent; ``notes`` carry
    the code and what to change (shown on the refusal view, B1-6)."""
    return TransportResponse(
        body=canonical({"error": code}).encode(),
        source_status=code,
        sent=False,
        view_fields={"notes": [f"{code}: {text}"]},
    )


def _query_window(
    request: TransportRequest,
) -> tuple[Window | None, TransportResponse | None]:
    """``start``/``end`` by the A3-1 rule (C5-8, ``otel_demo._query_window``):
    both or neither; neither is the default query window."""
    window, refusal = otel_demo._query_window(request)
    if refusal is not None or window is None:
        frame = request.window.as_json()
        return None, _refused(
            "QUERY_OUT_OF_WINDOW",
            "start and end must both be absolute RFC3339 times with a timezone, "
            "or both omitted, and lie inside the authorized window "
            f"{frame['start']} to {frame['end']}.",
        )
    return window, None


def _window_note(window: Window) -> str:
    return (
        f"The tool limited the query to the window {_z(window.start)} to "
        f"{_z(window.end)}; this filter cannot be turned off."
    )


def overflow_summary(
    tool: str, rows: Sequence[object], query: Mapping[str, object]
) -> dict[str, object]:
    """The over-limit refusal summary (#308 U5) with C5-10's suggestion."""
    text = query.get("query")
    metric = isinstance(text, str) and logql_lookback_seconds(text) > 0
    return {
        "row_unit": "series" if metric else "lines",
        "suggestion": (
            "Lower limit, narrow the label selector, add a line filter such as "
            '|= "error", shorten the window, or count with count_over_time.'
        ),
    }


# -- model-visible face and registrations ---------------------------------------

_QUERY_RULE = (
    f"query must be a non-empty LogQL query of at most {MAX_QUERY_CHARS} "
    "printable characters (no line breaks or tabs)."
)
_CANNOT_PROVE = (
    "A line that is not returned is unknown, not proof that nothing was "
    "logged: only limit lines are shown (the newest), and retention, ingest "
    "delay, the label selector and line filters can leave gaps. The timestamp "
    "is when the line was ingested or parsed, not necessarily when the event "
    "happened. A metric query counts only lines inside the query window."
)
_ERRORS_TEXT = (
    "A LogQL syntax error or a Loki limit is INVALID_PARAMS with Loki's "
    "message; no matching lines is no data; HTTP 401/403 (including the "
    "read-only gateway's) are SOURCE_FORBIDDEN; 404 is SOURCE_ERROR; 408, 429 "
    "and 5xx are SOURCE_UNAVAILABLE; a call past its 30 s deadline is a "
    "timeout."
)
_TOO_LARGE_TEXT = (
    "A source answer over 2 MiB, or a view over 25,000 tokens, is refused "
    "whole with RESULT_TOO_LARGE (never truncated)."
)

_DESCRIPTION = (
    "Run a query against Grafana Loki using LogQL query language. "
    "Loki indexes log lines by labels: start with a stream selector in braces "
    'and narrow with line filters, for example {namespace="prod", '
    'app="backend-api"} |= "error". A default Kubernetes label set looks like '
    '{namespace="prod", app="backend-api", container="api", '
    'pod="backend-api-68b7d9df9c-xyz12", stream="stdout"}. '
    "Kubernetes pod names carry a deployment hash and replica suffix: unless "
    "you have the complete pod name, match with a regex such as "
    'pod=~"nginx-.*" or pod=~".*ingress.*" (= is an exact match and does not '
    "take wildcards). When you have what looks like a complete pod name, query "
    "it directly without checking that the pod still exists: Loki keeps the "
    "logs of deleted pods. If the labels are unknown, query a broad selector "
    'such as {namespace="prod"} and read the labels of the returned lines. '
    "If asked to check for logs, always try one or two of the best queries "
    "you can construct. "
    "Time: the tool always limits the query to the query window and you "
    "cannot turn this off; start and end choose it inside the authorized "
    "window (omitted: the time policy's default_query_window when it states "
    "one, otherwise the 1 hour ending at the authorized window's end). "
    "Metric queries such as count_over_time({...}[5m]) or "
    'sum by (pod) (rate({...} |= "error" [1m])) are evaluated from the '
    "window start plus their range (aligned to the step the tool sends), so "
    "every point counts only lines inside the window; a range longer than "
    "the window, offset and @ are refused (INVALID_PARAMS, "
    'QUERY_OUT_OF_WINDOW). Quote strings with "..." or `...`; single '
    "quotes and /* */ or // comments are refused (QUERY_CONSTRUCT_NOT_ALLOWED). "
    "A log query returns at most limit lines (default 100, at most 500), the "
    "newest first, as {timestamp, labels, line}; reaching limit marks the "
    "result incomplete because older lines in the window were not returned. "
    "A metric query returns one {metric, values} row per series. "
    "String values are cut to 2000 characters and listed in "
    "_truncated_fields. " + _TOO_LARGE_TEXT + " " + _ERRORS_TEXT
)

TOOL_SCHEMAS: tuple[Mapping[str, Any], ...] = (
    {
        "type": "function",
        "function": {
            "name": QUERY_TOOL,
            "description": _DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "LogQL query string, for example "
                            '{namespace="prod", pod=~"checkout-.*"} |= "error". '
                            f"At most {MAX_QUERY_CHARS} characters on one line."
                        ),
                    },
                    "start": {
                        "type": "string",
                        "description": (
                            "Optional absolute RFC3339 start of the query "
                            "window, e.g. '2026-09-14T10:30:00Z'; give start "
                            "and end together or neither."
                        ),
                    },
                    "end": {
                        "type": "string",
                        "description": (
                            "Optional absolute RFC3339 end of the query "
                            "window, e.g. '2026-09-14T11:30:00Z'; give start "
                            "and end together or neither."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            f"Maximum number of log lines to return, 1 to "
                            f"{MAX_LIMIT} (default: {DEFAULT_LIMIT})."
                        ),
                    },
                },
                "required": ["query"],
            },
        },
    },
)

_ERROR_CLASSES: dict[str, str] = {
    "400": "INVALID_PARAMS",
    "401": "SOURCE_FORBIDDEN",
    "403": "SOURCE_FORBIDDEN",
    "404": "SOURCE_ERROR",
    "408": "SOURCE_UNAVAILABLE",
    "429": "SOURCE_UNAVAILABLE",
    "INVALID_QUERY": "INVALID_PARAMS",
    "INVALID_LIMIT": "INVALID_PARAMS",
    "QUERY_OUT_OF_WINDOW": "INVALID_PARAMS",
    "QUERY_CONSTRUCT_NOT_ALLOWED": "INVALID_PARAMS",
}

_LIMITS = (
    "At most 500 lines; string values cut to 2000 characters and marked. One "
    "call reads at most 2 MiB from Loki and returns at most 2 MiB "
    "(RESULT_TOO_LARGE otherwise, whole call); the view is limited to 25,000 "
    "tokens and refused whole above it, never truncated. 30 s per call, no "
    "retry. " + _ERRORS_TEXT
)


def loki_registrations(*, max_window_seconds: int) -> tuple[ToolRegistration, ...]:
    """The one registration (C5-10: ``source="otel-demo"``, the lab target's;
    ``may_contain_secrets=True``; verb ``query``)."""
    return (
        ToolRegistration(
            name=QUERY_TOOL,
            version="v1",
            source=otel_demo.SOURCE,
            verb="query",
            may_contain_secrets=True,
            request_timeout_seconds=REQUEST_TIMEOUT_SECONDS,
            max_result_bytes=READ_LIMIT_BYTES,
            max_view_tokens=MAX_VIEW_TOKENS,
            max_window_seconds=max_window_seconds,
            error_classes=dict(_ERROR_CLASSES),
            result_path=("results",),
            description=ToolDescription(
                returns=(
                    "Log lines from the registered Loki matching a LogQL log "
                    "query, newest first, each {timestamp, labels, line}, "
                    "under results; or, for a metric query, one {metric, "
                    "values} row per series. The view adds result_type, "
                    "limit, streams_returned, long_values_cut, notes and "
                    "incomplete_reason."
                ),
                window_format=(
                    "The authorized absolute window is appended as {window}. "
                    "The tool always bounds the query to the query window "
                    "(start/end inside the authorized window, default the "
                    "last hour or the alert's default window); a metric query "
                    "is evaluated from the window start plus its longest "
                    "range (lookback_seconds, lookback_start_at). "
                    "source_start_at/source_end_at are the earliest and "
                    "latest returned line or point."
                ),
                values_format=(
                    "The authorized target is appended as {values}. query is "
                    f"LogQL of at most {MAX_QUERY_CHARS} printable characters "
                    "without offset or @, single quotes, /* */ or // "
                    "comments, every range a Prometheus duration no longer "
                    "than the window; limit 1 to 500."
                ),
                limits=_LIMITS,
                cannot_prove=_CANNOT_PROVE,
            ),
            parameters={
                "query": ParameterSpec(
                    "string", required=True, description="LogQL query."
                ),
                "start": ParameterSpec(
                    "string", description="Absolute RFC3339 start; both or neither."
                ),
                "end": ParameterSpec(
                    "string", description="Absolute RFC3339 end; both or neither."
                ),
                "limit": ParameterSpec("integer", description="Lines, 1 to 500."),
            },
        ),
    )
