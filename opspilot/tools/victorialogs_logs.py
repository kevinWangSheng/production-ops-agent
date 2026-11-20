"""VictoriaLogs log search (tool expansion C5b, #569).

The five tools of upstream HolmesGPT's ``victorialogs`` toolset
(``holmes/plugins/toolsets/victorialogs/victorialogs.py`` at ``a0009195``),
same names and parameters, served only by ``otel-demo-ext`` and only when
``OPSPILOT_VICTORIALOGS_URL`` is set (decision table r10, C5-U1, C5-0 to
C5-15; B1-U1 and B1-U2 carried over; the C5b public interface in
docs/tasks/2026-11-18-tool-expansion-c5.md):

* ``victorialogs_query`` -- ``GET /select/logsql/query``
* ``victorialogs_streams`` -- ``GET /select/logsql/streams``
* ``victorialogs_field_names`` -- ``GET /select/logsql/field_names``
* ``victorialogs_field_values`` -- ``GET /select/logsql/field_values``
* ``victorialogs_hits`` -- ``GET /select/logsql/hits``

Upstream POSTs a form; here every call is a GET with the query string built
from a fixed key set, so the product still has the one B1 POST only (C5-3).

Time (B1-U1, C5-8, C5-9): ``start`` and ``end`` are always sent, the query
window inside the authorized frame (default its last hour); a ``_time:``
filter in the query only narrows it. LogsQL constructs that might read
outside the window or are not verified yet -- ``options``, ``join``,
``union``, ``stream_context`` and subqueries in ``in``/``contains_any``/
``contains_all`` -- are refused outside string literals (C5-7, fail closed).

Read-only by construction (C5-3): fixed GETs; no redirects, no proxy. The
``vlogs-ro`` credential is this transport's own slot and is never rendered;
the tenant (``AccountID``/``ProjectID``) comes only from configuration
(C5-0). The read-only gateway in front is the second layer (C5-U1).
"""

from __future__ import annotations

import base64
import math
import os
import re
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
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
    es_opener,
)
from opspilot.tools.executor import (
    TransportError,
    TransportRequest,
    TransportResponse,
    TransportResultTooLarge,
)
from opspilot.tools.http_deadline import DeadlineHandlers
from opspilot.tools.loki_logs import (
    _clamp,
    _decode,
    _query_window,
    _refused,
    _source_error,
    _window_note,
    _z,
    http_get,
)
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
    "ACCOUNT_ID_ENV",
    "BEARER_TOKEN_ENV",
    "CA_FILE_ENV",
    "CREDENTIAL_REF",
    "ENDPOINT_SELECTOR",
    "FIELD_NAMES_TOOL",
    "FIELD_VALUES_TOOL",
    "HITS_TOOL",
    "PASSWORD_ENV",
    "PATHS",
    "PROJECT_ID_ENV",
    "QUERY_TOOL",
    "STREAMS_TOOL",
    "TOOL_NAMES",
    "TOOL_SCHEMAS",
    "URL_ENV",
    "USERNAME_ENV",
    "VictoriaLogsClient",
    "VictoriaLogsConfig",
    "VictoriaLogsTransport",
    "logsql_problem",
    "overflow_summary",
    "victorialogs_enabled",
    "victorialogs_registrations",
]

QUERY_TOOL = "victorialogs_query"
STREAMS_TOOL = "victorialogs_streams"
FIELD_NAMES_TOOL = "victorialogs_field_names"
FIELD_VALUES_TOOL = "victorialogs_field_values"
HITS_TOOL = "victorialogs_hits"
TOOL_NAMES = frozenset(
    {QUERY_TOOL, STREAMS_TOOL, FIELD_NAMES_TOOL, FIELD_VALUES_TOOL, HITS_TOOL}
)
#: C5-3: the only paths, one per tool.
PATHS: Mapping[str, str] = {
    QUERY_TOOL: "/select/logsql/query",
    STREAMS_TOOL: "/select/logsql/streams",
    FIELD_NAMES_TOOL: "/select/logsql/field_names",
    FIELD_VALUES_TOOL: "/select/logsql/field_values",
    HITS_TOOL: "/select/logsql/hits",
}
#: C5-4: the transport's own credential slot.
CREDENTIAL_REF = "vlogs-ro"
#: C5-10: where the address sits in the registered target's selector.
ENDPOINT_SELECTOR = "victorialogs_endpoint"

URL_ENV = "OPSPILOT_VICTORIALOGS_URL"
USERNAME_ENV = "OPSPILOT_VICTORIALOGS_USERNAME"
PASSWORD_ENV = "OPSPILOT_VICTORIALOGS_PASSWORD"
BEARER_TOKEN_ENV = "OPSPILOT_VICTORIALOGS_BEARER_TOKEN"
CA_FILE_ENV = "OPSPILOT_VICTORIALOGS_CA_FILE"
ACCOUNT_ID_ENV = "OPSPILOT_VICTORIALOGS_ACCOUNT_ID"
PROJECT_ID_ENV = "OPSPILOT_VICTORIALOGS_PROJECT_ID"
_OTHER_ENVS = (
    USERNAME_ENV,
    PASSWORD_ENV,
    BEARER_TOKEN_ENV,
    CA_FILE_ENV,
    ACCOUNT_ID_ENV,
    PROJECT_ID_ENV,
)

REQUEST_TIMEOUT_SECONDS = 30.0
READ_LIMIT_BYTES = MAX_RESULT_BYTES
DEFAULT_LIMIT = 100
MAX_LIMIT = 500
MAX_VALUE_CHARS = 2000
MAX_QUERY_CHARS = 4000
DEFAULT_STEP = "5m"
MAX_BUCKETS = 1440
MAX_FIELD_CHARS = 200
_UINT32_MAX = 2**32 - 1

_STEP = re.compile(r"[1-9][0-9]{0,5}[smh]")
_STEP_SECONDS = {"s": 1, "m": 60, "h": 3600}
_FIELD = re.compile(r"[A-Za-z0-9_.:-]{1,%d}" % MAX_FIELD_CHARS)
_TENANT = re.compile(r"[0-9]{1,10}")
#: C5-7: refused wherever they stand outside a string literal.
_WORDS = re.compile(r"\b(options|join|union|stream_context)\b", re.IGNORECASE)
_SUBQUERY = re.compile(r"\b(in|contains_any|contains_all)\s*\(", re.IGNORECASE)
_RFC3339 = re.compile(
    r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)"
)


# -- configuration -------------------------------------------------------------


def victorialogs_enabled(env: Mapping[str, str]) -> bool:
    """B1-U2 for VictoriaLogs: whether the extended profile shows the five
    tools. Only the address is read -- the web never reads a credential."""
    return bool(env.get(URL_ENV))


def _clean_url(value: str) -> str:
    try:
        parts = urllib.parse.urlsplit(value)
        parts.port
    except ValueError:
        raise ToolContractError("VICTORIALOGS_CONFIG_INVALID") from None
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or "@" in parts.netloc
        or parts.query
        or parts.fragment
        or "?" in value
        or "#" in value
    ):
        raise ToolContractError("VICTORIALOGS_CONFIG_INVALID")
    # A path prefix (gateway, vmauth) is kept; the select path is appended.
    return value.rstrip("/")


def _tenant_ok(value: object) -> bool:
    return (
        isinstance(value, str)
        and _TENANT.fullmatch(value) is not None
        and int(value) <= _UINT32_MAX
    )


@dataclass(frozen=True)
class VictoriaLogsConfig:
    """The worker's ``vlogs-ro`` connection (C5-4): address, one of a user
    name and password or a bearer token (required, C5-U1), a CA file for
    HTTPS and the tenant the operator configured (C5-0)."""

    url: str
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    bearer_token: str | None = field(default=None, repr=False)
    ca_file: str | None = None
    account_id: str | None = None
    project_id: str | None = None

    def __post_init__(self) -> None:
        invalid = ToolContractError("VICTORIALOGS_CONFIG_INVALID")
        if not isinstance(self.url, str) or not self.url:
            raise ToolContractError("VICTORIALOGS_CONFIG_MISSING")
        object.__setattr__(self, "url", _clean_url(self.url))
        has_token = self.bearer_token is not None
        has_basic = self.username is not None or self.password is not None
        if not has_token and not has_basic:
            raise ToolContractError("VICTORIALOGS_CONFIG_MISSING")
        if has_token and has_basic:
            raise invalid
        values = [self.bearer_token] if has_token else [self.username, self.password]
        if any(not isinstance(value, str) or not _printable(value) for value in values):
            raise invalid
        if has_token and any(ch.isspace() for ch in str(self.bearer_token)):
            raise invalid
        if has_basic and ":" in str(self.username):
            raise invalid
        for tenant in (self.account_id, self.project_id):
            if tenant is not None and not _tenant_ok(tenant):
                raise invalid
        if self.ca_file is not None:
            if not self.url.startswith("https:"):
                raise invalid
            path = Path(self.ca_file)
            if not path.is_file() or not os.access(path, os.R_OK):
                raise invalid

    @property
    def authorization(self) -> str:
        """The ``Authorization`` header value behind ``CREDENTIAL_REF``."""
        if self.bearer_token is not None:
            return f"Bearer {self.bearer_token}"
        pair = f"{self.username}:{self.password}".encode()
        return "Basic " + base64.b64encode(pair).decode("ascii")

    @property
    def tenant_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.account_id is not None:
            headers["AccountID"] = self.account_id
        if self.project_id is not None:
            headers["ProjectID"] = self.project_id
        return headers

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> VictoriaLogsConfig | None:
        """``OPSPILOT_VICTORIALOGS_*`` (C5-4). No address: ``None`` -- unless
        another of the variables is set, which is
        ``VICTORIALOGS_CONFIG_INVALID`` so a setting is never silently
        ignored."""
        url = env.get(URL_ENV) or ""
        values = {name: env.get(name) or None for name in _OTHER_ENVS}
        if not url:
            if any(values.values()):
                raise ToolContractError("VICTORIALOGS_CONFIG_INVALID")
            return None
        return cls(
            url=url,
            username=values[USERNAME_ENV],
            password=values[PASSWORD_ENV],
            bearer_token=values[BEARER_TOKEN_ENV],
            ca_file=values[CA_FILE_ENV],
            account_id=values[ACCOUNT_ID_ENV],
            project_id=values[PROJECT_ID_ENV],
        )


# -- HTTP ----------------------------------------------------------------------


class VictoriaLogsClient:
    """The only requests this backend may receive: ``GET`` of one of
    ``PATHS`` with the query string built here (C5-3).

    ``credentials`` maps ``CREDENTIAL_REF`` to the ``Authorization`` value; a
    missing slot is ``CREDENTIAL_REF_UNKNOWN`` before anything is sent.
    ``tenant_headers`` are the configured ``AccountID``/``ProjectID``.
    """

    def __init__(
        self,
        *,
        credentials: Mapping[str, str | None],
        tenant_headers: Mapping[str, str] | None = None,
        opener: urllib.request.OpenerDirector | None = None,
        ca_file: str | None = None,
    ) -> None:
        self._credentials = dict(credentials)
        self._tenant_headers = dict(tenant_headers or {})
        self._opener = opener or es_opener(ca_file)
        self._deadline_handlers = DeadlineHandlers(self._opener)

    def authorization(self) -> str:
        value = self._credentials.get(CREDENTIAL_REF)
        if not value:
            raise TransportError("CREDENTIAL_REF_UNKNOWN")
        return value

    def select(
        self,
        endpoint: str,
        tool: str,
        query: Sequence[tuple[str, str]],
        *,
        deadline: float,
        max_bytes: int,
    ) -> _Fetched:
        url = f"{endpoint}{PATHS[tool]}?" + urllib.parse.urlencode(list(query))
        headers = {
            "Accept": "application/json",
            "Authorization": self.authorization(),
            **self._tenant_headers,
        }
        return http_get(
            self._opener,
            self._deadline_handlers,
            url,
            headers,
            deadline=deadline,
            max_bytes=max_bytes,
            thread_name="victorialogs-select",
        )


# -- LogsQL checks -----------------------------------------------------------------


class _Construct(Exception):
    """A token the outside-string scan refuses."""


class _Unterminated(Exception):
    """A string literal that never closes."""


def _without_strings(query: str) -> str:
    """The query with every string literal blanked to ``""``: ``"..."`` with
    backslash escapes and ``` `...` ``` without. A ``'`` outside them is
    refused (the tool and VictoriaLogs must agree on what is quoted); ``#``
    is not treated as a comment, so the whole query is scanned (C5-7)."""
    out: list[str] = []
    index = 0
    size = len(query)
    while index < size:
        char = query[index]
        if char == '"':
            index += 1
            while index < size and query[index] != '"':
                index += 2 if query[index] == "\\" else 1
            if index >= size:
                raise _Unterminated
            out.append(' "" ')
            index += 1
        elif char == "`":
            close = query.find("`", index + 1)
            if close < 0:
                raise _Unterminated
            out.append(' "" ')
            index = close + 1
        elif char == "'":
            raise _Construct("'")
        else:
            out.append(char)
            index += 1
    return "".join(out)


def _group(stripped: str, start: int) -> str:
    """The text inside the parenthesis opened just before ``start``
    (to the end of the query when it never closes: fail closed)."""
    depth = 1
    for index in range(start, len(stripped)):
        if stripped[index] == "(":
            depth += 1
        elif stripped[index] == ")":
            depth -= 1
            if depth == 0:
                return stripped[start:index]
    return stripped[start:]


_QUERY_RULE = (
    f"query must be a non-empty LogsQL query of at most {MAX_QUERY_CHARS} "
    "printable characters (no line breaks or tabs) whose string literals all "
    "close."
)
_CONSTRUCT_TEXT = (
    "outside a string literal is refused: options(...), join, union, "
    "stream_context and subqueries in in(...), contains_any(...) or "
    "contains_all(...) might read outside the query window; single quotes are "
    'refused too. Quote words with "..." or `...` to search for them.'
)


def logsql_problem(query: str) -> tuple[str, str] | None:
    """``(code, text)`` when this LogsQL must not be sent, else ``None``
    (C5-7). Decided before anything is sent."""
    if (
        not isinstance(query, str)
        or not query.strip()
        or len(query) > MAX_QUERY_CHARS
        or not query.isprintable()
    ):
        return "INVALID_QUERY", _QUERY_RULE
    try:
        stripped = _without_strings(query)
    except _Unterminated:
        return "INVALID_QUERY", _QUERY_RULE
    except _Construct as found:
        return "QUERY_CONSTRUCT_NOT_ALLOWED", f"{found} {_CONSTRUCT_TEXT}"
    word = _WORDS.search(stripped)
    if word is not None:
        return "QUERY_CONSTRUCT_NOT_ALLOWED", f"{word.group(1)} {_CONSTRUCT_TEXT}"
    for match in _SUBQUERY.finditer(stripped):
        inner = _group(stripped, match.end())
        if "|" in inner or ":" in inner:
            return (
                "QUERY_CONSTRUCT_NOT_ALLOWED",
                f"a subquery in {match.group(1)}(...) {_CONSTRUCT_TEXT}",
            )
    return None


def _step_seconds(step: object) -> int | None:
    if not isinstance(step, str) or _STEP.fullmatch(step) is None:
        return None
    return int(step[:-1]) * _STEP_SECONDS[step[-1]]


# -- responses -------------------------------------------------------------------


def _time_ns(value: object) -> int | None:
    """Nanoseconds since the epoch of VictoriaLogs' RFC3339 ``_time``."""
    if not isinstance(value, str):
        return None
    match = _RFC3339.fullmatch(value)
    if match is None:
        return None
    zone = "+00:00" if match.group(3) == "Z" else match.group(3)
    try:
        whole = datetime.fromisoformat(match.group(1) + zone)
    except ValueError:
        return None
    seconds = int(whole.timestamp())
    return seconds * 1_000_000_000 + int((match.group(2) or "").ljust(9, "0"))


def _from_ns(ns: int) -> datetime:
    seconds, fraction = divmod(ns, 1_000_000_000)
    return datetime.fromtimestamp(seconds, tz=timezone.utc) + timedelta(
        microseconds=fraction // 1000
    )


def _parse_jsonl(body: bytes) -> list[dict[str, Any]] | None:
    """VictoriaLogs' JSON Lines answer; ``None`` when a line is not an
    object (a malformed answer is never shown in part)."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return None
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        row = _decode(line.encode("utf-8"))
        if not isinstance(row, dict):
            return None
        rows.append(row)
    return rows


def _hits_count(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _cut_rows(rows: list[dict[str, Any]], cut: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        row_cut: list[str] = []
        shown: dict[str, Any] = _cut_strings(row, "", row_cut)
        if row_cut:
            shown["_truncated_fields"] = row_cut
            cut.extend(path for path in row_cut if path not in cut)
        out.append(shown)
    return out


# -- transport -------------------------------------------------------------------


@dataclass(frozen=True)
class _Plan:
    keys: list[tuple[str, str]]
    window: Window
    limit: int | None = None
    field: str | None = None
    step: str | None = None


class VictoriaLogsTransport:
    """Serves the five tools over one ``VictoriaLogsClient`` (``vlogs-ro``).

    Every refusal of the model's input is decided before anything is sent.
    """

    def __init__(
        self,
        *,
        credentials: Mapping[str, str | None],
        tenant_headers: Mapping[str, str] | None = None,
        opener: urllib.request.OpenerDirector | None = None,
        ca_file: str | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._client = VictoriaLogsClient(
            credentials=credentials,
            tenant_headers=tenant_headers,
            opener=opener,
            ca_file=ca_file,
        )
        self._monotonic = monotonic or _monotonic

    def fetch(self, request: TransportRequest) -> TransportResponse:
        if request.tool not in TOOL_NAMES:
            raise TransportError("TOOL_NOT_SUPPORTED")
        deadline = self._monotonic() + request.timeout_seconds
        plan = self._plan(request)
        if isinstance(plan, TransportResponse):
            return plan
        endpoint = self._endpoint(request)
        fetched = self._client.select(
            endpoint,
            request.tool,
            plan.keys,
            deadline=deadline,
            max_bytes=READ_LIMIT_BYTES,
        )
        if fetched.status != 200:
            return _source_error(fetched)
        if request.tool == QUERY_TOOL:
            return self._entries(request, fetched.body, plan)
        if request.tool == HITS_TOOL:
            return self._hits(request, fetched.body, plan)
        return self._values(request, fetched.body, plan)

    def _endpoint(self, request: TransportRequest) -> str:
        self._client.authorization()  # a missing slot fails before sending
        endpoint = request.selector.get(ENDPOINT_SELECTOR, "")
        if not endpoint:
            raise TransportError("VICTORIALOGS_ENDPOINT_MISSING")
        return endpoint.rstrip("/")

    @staticmethod
    def _plan(request: TransportRequest) -> _Plan | TransportResponse:
        tool = request.tool
        params = request.params
        query = params.get("query")
        if tool in (QUERY_TOOL, HITS_TOOL):
            if not isinstance(query, str) or not query.strip():
                return _refused("INVALID_QUERY", _QUERY_RULE)
        elif query is None or (isinstance(query, str) and not query.strip()):
            query = "*"
        if not isinstance(query, str):
            return _refused("INVALID_QUERY", _QUERY_RULE)
        limit: int | None = None
        if tool in (QUERY_TOOL, STREAMS_TOOL, FIELD_VALUES_TOOL):
            raw_limit = params.get("limit", DEFAULT_LIMIT)
            if raw_limit is None:
                raw_limit = DEFAULT_LIMIT
            if type(raw_limit) is not int or not 1 <= raw_limit <= MAX_LIMIT:
                return _refused("INVALID_LIMIT", f"limit must be 1 to {MAX_LIMIT}.")
            limit = raw_limit
        field_name: str | None = None
        if tool == FIELD_VALUES_TOOL or (
            tool == HITS_TOOL and params.get("field") is not None
        ):
            raw_field = params.get("field")
            if not isinstance(raw_field, str) or _FIELD.fullmatch(raw_field) is None:
                return _refused(
                    "INVALID_FIELD",
                    f"field must be 1 to {MAX_FIELD_CHARS} characters of "
                    "A-Z, a-z, 0-9, _ . : -",
                )
            field_name = raw_field
        window, refusal = _query_window(request)
        if refusal is not None:
            return refusal
        assert window is not None
        step: str | None = None
        if tool == HITS_TOOL:
            raw_step = params.get("step")
            seconds = _step_seconds(DEFAULT_STEP if raw_step is None else raw_step)
            if seconds is None or math.ceil(window.seconds / seconds) > MAX_BUCKETS:
                return _refused(
                    "INVALID_STEP",
                    "step must be <n>s, <n>m or <n>h (n 1 to 999999) giving at "
                    f"most {MAX_BUCKETS} buckets over the query window of "
                    f"{window.seconds:g} s.",
                )
            step = DEFAULT_STEP if raw_step is None else str(raw_step)
        problem = logsql_problem(query)
        if problem is not None:
            return _refused(*problem)
        keys: list[tuple[str, str]] = [("query", query)]
        if tool == FIELD_VALUES_TOOL:
            assert field_name is not None
            keys.append(("field", field_name))
        keys += [("start", _z(window.start)), ("end", _z(window.end))]
        if limit is not None:
            # One more than shown, to tell a complete answer from a cut one.
            keys.append(("limit", str(limit + 1)))
        if tool == HITS_TOOL:
            assert step is not None
            keys.append(("step", step))
            if field_name is not None:
                keys.append(("field", field_name))
            # With a step, VictoriaLogs widens start down and end up to whole
            # steps before filtering (``alignStartEndToStep`` in
            # app/vlselect/logsql at v1.54.0), which counted entries outside
            # the window (PR #731 CI). The same window as a filter of its own
            # is ANDed after that widening, so only entries inside count.
            keys.append(
                ("extra_filters", f"_time:[{_z(window.start)}, {_z(window.end)})")
            )
        return _Plan(keys=keys, window=window, limit=limit, field=field_name, step=step)

    def _entries(
        self, request: TransportRequest, body: bytes, plan: _Plan
    ) -> TransportResponse:
        rows = _parse_jsonl(body)
        if rows is None:
            return TransportResponse(body=b"")  # MALFORMED_RESULT
        assert plan.limit is not None
        timed: list[tuple[int, int, dict[str, Any]]] = []
        untimed: list[dict[str, Any]] = []
        for order, row in enumerate(rows):
            ns = _time_ns(row.get("_time"))
            if ns is None:
                untimed.append(row)
            else:
                timed.append((ns, order, row))
        timed.sort(key=lambda entry: (-entry[0], entry[1]))
        ordered = [row for _, _, row in timed] + untimed
        returned = len(ordered)
        kept = ordered[: plan.limit]
        kept_times = [ns for ns, _, _ in timed[: plan.limit]]
        cut: list[str] = []
        shown = _cut_rows(kept, cut)
        start_at: datetime | None = None
        end_at: datetime | None = None
        if kept_times:
            start_at, end_at = _clamp(
                _from_ns(min(kept_times)), _from_ns(max(kept_times)), plan.window
            )
        elif kept:
            start_at, end_at = plan.window.start, plan.window.end
        reasons: list[str] = []
        if returned > plan.limit:
            reasons.append(
                f"limit: more than {plan.limit} entries matched; only the "
                f"newest {plan.limit} are shown. Narrow the stream selector or "
                "filters, shorten the window, or count with victorialogs_hits"
            )
        notes = [f"Incomplete result ({reason})." for reason in reasons]
        if cut:
            notes.append(
                f"String values longer than {MAX_VALUE_CHARS} characters are cut "
                f"there ({_names(cut)}); each entry lists its cut fields under "
                "_truncated_fields."
            )
        notes.append(_window_note(plan.window))
        return _respond(
            request,
            shown,
            row_unit="entries",
            window=plan.window,
            start_at=start_at,
            end_at=end_at,
            cut=cut,
            reasons=reasons,
            notes=notes,
            extra={"limit": plan.limit},
            cannot_prove=_CANNOT_PROVE_QUERY,
        )

    def _values(
        self, request: TransportRequest, body: bytes, plan: _Plan
    ) -> TransportResponse:
        payload = _decode(body)
        values = payload.get("values") if isinstance(payload, dict) else None
        if not isinstance(values, list):
            return TransportResponse(body=b"")
        key, row_unit, noun = {
            STREAMS_TOOL: ("stream", "streams", "streams"),
            FIELD_NAMES_TOOL: ("field", "fields", "field names"),
            FIELD_VALUES_TOOL: ("value", "values", "values"),
        }[request.tool]
        parsed: list[tuple[int, str]] = []
        for item in values:
            if not isinstance(item, dict):
                return TransportResponse(body=b"")
            value = item.get("value")
            hits = _hits_count(item.get("hits"))
            if not isinstance(value, str) or hits is None:
                return TransportResponse(body=b"")
            parsed.append((hits, value))
        parsed.sort(key=lambda entry: (-entry[0], entry[1]))
        kept = parsed if plan.limit is None else parsed[: plan.limit]
        cut: list[str] = []
        shown = _cut_rows([{key: value, "hits": hits} for hits, value in kept], cut)
        reasons: list[str] = []
        if plan.limit is not None and len(parsed) > plan.limit:
            reasons.append(
                f"limit: more than {plan.limit} {noun} matched; the "
                f"{plan.limit} with the most hits are shown. Narrow the query "
                "or shorten the window"
            )
        notes = [f"Incomplete result ({reason})." for reason in reasons]
        if cut:
            notes.append(
                f"String values longer than {MAX_VALUE_CHARS} characters are cut "
                f"there ({_names(cut)})."
            )
        notes.append(_window_note(plan.window))
        extra: dict[str, Any] = {}
        if plan.limit is not None:
            extra["limit"] = plan.limit
        if plan.field is not None:
            extra["field"] = plan.field
        return _respond(
            request,
            shown,
            row_unit=row_unit,
            window=plan.window,
            start_at=plan.window.start if shown else None,
            end_at=plan.window.end if shown else None,
            cut=cut,
            reasons=reasons,
            notes=notes,
            extra=extra,
            cannot_prove=_CANNOT_PROVE_META,
        )

    def _hits(
        self, request: TransportRequest, body: bytes, plan: _Plan
    ) -> TransportResponse:
        payload = _decode(body)
        series = payload.get("hits") if isinstance(payload, dict) else None
        if not isinstance(series, list):
            return TransportResponse(body=b"")
        rows: list[dict[str, Any]] = []
        for item in series:
            if not isinstance(item, dict):
                return TransportResponse(body=b"")
            fields = item.get("fields", {})
            timestamps = item.get("timestamps", [])
            values = item.get("values", [])
            total = _hits_count(item.get("total"))
            if (
                not isinstance(fields, dict)
                or not isinstance(timestamps, list)
                or not isinstance(values, list)
                or total is None
            ):
                return TransportResponse(body=b"")
            if total == 0:
                continue
            rows.append(
                {
                    "fields": fields,
                    "timestamps": timestamps,
                    "values": values,
                    "total": total,
                }
            )
        cut: list[str] = []
        shown = _cut_rows(rows, cut)
        notes: list[str] = []
        if cut:
            notes.append(
                f"String values longer than {MAX_VALUE_CHARS} characters are cut "
                f"there ({_names(cut)})."
            )
        notes.append(_window_note(plan.window))
        notes.append(
            f"Buckets are {plan.step} wide and aligned to the step, so the "
            "first bucket's timestamp can be earlier than the window start; "
            "every bucket counts only entries inside the window."
        )
        extra: dict[str, Any] = {"step": plan.step}
        if plan.field is not None:
            extra["field"] = plan.field
        return _respond(
            request,
            shown,
            row_unit="series",
            window=plan.window,
            start_at=plan.window.start if shown else None,
            end_at=plan.window.end if shown else None,
            cut=cut,
            reasons=[],
            notes=notes,
            extra=extra,
            cannot_prove=_CANNOT_PROVE_META,
        )


def _respond(
    request: TransportRequest,
    rows: list[dict[str, Any]],
    *,
    row_unit: str,
    window: Window,
    start_at: datetime | None,
    end_at: datetime | None,
    cut: list[str],
    reasons: list[str],
    notes: list[str],
    extra: Mapping[str, Any],
    cannot_prove: str,
) -> TransportResponse:
    body = canonical({"results": rows}).encode("utf-8")
    if len(body) > request.max_result_bytes:
        raise TransportResultTooLarge(str(request.max_result_bytes))
    view_fields: dict[str, Any] = {
        **extra,
        "long_values_cut": len(cut),
        "incomplete_reason": "; ".join(reasons) or None,
        "notes": notes,
        "cannot_prove": cannot_prove,
    }
    return TransportResponse(
        body=body,
        source_start_at=start_at,
        source_end_at=end_at,
        query_window=window,
        row_unit=row_unit,
        backend_rows_returned=len(rows),
        incomplete=bool(reasons),
        view_fields=view_fields,
    )


def overflow_summary(
    tool: str, rows: Sequence[object], query: Mapping[str, object]
) -> dict[str, object]:
    """The over-limit refusal summary (#308 U5) with C5-10's suggestion."""
    unit = {
        QUERY_TOOL: "entries",
        STREAMS_TOOL: "streams",
        FIELD_NAMES_TOOL: "fields",
        FIELD_VALUES_TOOL: "values",
        HITS_TOOL: "series",
    }.get(tool, "rows")
    return {
        "row_unit": unit,
        "suggestion": (
            'Lower limit, add a stream selector such as {namespace="prod"} or '
            "a filter such as level:error, shorten the window, use a larger "
            "step, or count with victorialogs_hits."
        ),
    }


# -- model-visible face and registrations ---------------------------------------

_CANNOT_PROVE_QUERY = (
    "An entry that is not returned is unknown, not proof that nothing was "
    "logged: only limit entries are shown (the newest), and retention, ingest "
    "delay and the filters can leave gaps. _time is when the entry was "
    "ingested or parsed, not necessarily when the event happened."
)
_CANNOT_PROVE_META = (
    "This only says which streams, fields, values or counts exist in this "
    "window; it does not show the content of any log entry. Retention and "
    "ingest delay can leave gaps."
)
_ERRORS_TEXT = (
    "A LogsQL syntax error is INVALID_PARAMS with VictoriaLogs' message; no "
    "matching entries is no data; HTTP 401/403 (including the read-only "
    "gateway's) are SOURCE_FORBIDDEN; 404 is SOURCE_ERROR; 408, 429 and 5xx "
    "are SOURCE_UNAVAILABLE; a call past its 30 s deadline is a timeout."
)
_TOO_LARGE_TEXT = (
    "A source answer over 2 MiB, or a view over 25,000 tokens, is refused "
    "whole with RESULT_TOO_LARGE (never truncated)."
)
_TIME_TEXT = (
    "Time: the tool always limits the call to the query window and you cannot "
    "turn this off; start and end choose it inside the authorized window "
    "(omitted: the time policy's default_query_window when it states one, "
    "otherwise the 1 hour ending at the authorized window's end). Do not put "
    "_time: in the query; it can only narrow the window."
)
_LOGSQL_TEXT = (
    'LogsQL: word or phrase filters (error, "connection refused"), field '
    "filters (level:error, service:checkout), stream selectors (fastest) "
    '{namespace="app-1",service="checkout"}, regex ~"5\\\\d\\\\d", AND/OR/NOT '
    "(level:error -healthcheck, (app:nginx OR app:apache)), and pipes such as "
    "| stats, | sort, | limit, | fields. Prefer stream selectors when you know "
    "the labels; otherwise call victorialogs_streams or "
    "victorialogs_field_names first. For Kubernetes pod logs common fields are "
    "namespace, pod and container or service; with a partial pod name list "
    "values first (victorialogs_field_values with field=pod, query=*). If "
    "nothing is returned, broaden the query (drop a filter, widen the window). "
    "Refused before sending (QUERY_CONSTRUCT_NOT_ALLOWED): options(...), "
    "join, union, stream_context, subqueries in in(...), contains_any(...) "
    "or contains_all(...), and single quotes; to search for one of these "
    'words, quote it with "..." or `...`.'
)
_COMMON = " ".join((_TIME_TEXT, _LOGSQL_TEXT, _TOO_LARGE_TEXT, _ERRORS_TEXT))

_START = {
    "type": "string",
    "description": (
        "Optional absolute RFC3339 start of the query window, e.g. "
        "'2026-09-14T10:30:00Z'; give start and end together or neither."
    ),
}
_END = {
    "type": "string",
    "description": (
        "Optional absolute RFC3339 end of the query window, e.g. "
        "'2026-09-14T11:30:00Z'; give start and end together or neither."
    ),
}


def _limit(noun: str) -> dict[str, Any]:
    return {
        "type": "integer",
        "description": (
            f"Maximum number of {noun} to return, 1 to {MAX_LIMIT} (default: "
            f"{DEFAULT_LIMIT})."
        ),
    }


def _optional_query(default_text: str) -> dict[str, Any]:
    return {
        "type": "string",
        "description": (
            f"LogsQL query (default '*' {default_text}). At most "
            f"{MAX_QUERY_CHARS} characters on one line."
        ),
    }


def _schema(
    name: str, description: str, properties: Mapping[str, Any], required: list[str]
) -> Mapping[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": dict(properties),
                "required": required,
            },
        },
    }


TOOL_SCHEMAS: tuple[Mapping[str, Any], ...] = (
    _schema(
        QUERY_TOOL,
        "Search VictoriaLogs using a LogsQL query. Returns matching log entries "
        "as JSON objects (_time, _stream, _msg and the other fields), the "
        "newest first; entries without _time (for example | stats results) "
        "follow in VictoriaLogs' order. Always include the most specific "
        'filters you can (stream selectors like {namespace="foo"}, word/phrase '
        "filters, field filters such as level:error). At most limit entries "
        "(default 100, at most 500); more matches mark the result incomplete. "
        "String values are cut to 2000 characters and listed in "
        "_truncated_fields. " + _COMMON,
        {
            "query": {
                "type": "string",
                "description": (
                    "LogsQL query string, for example "
                    '{namespace="app-1",service="checkout"} level:error or '
                    "error. Use * to match everything. At most "
                    f"{MAX_QUERY_CHARS} characters on one line."
                ),
            },
            "start": _START,
            "end": _END,
            "limit": _limit("log entries"),
        },
        ["query"],
    ),
    _schema(
        STREAMS_TOOL,
        "List log streams matching a LogsQL query in the query window, each "
        'with its stream label set (e.g. {namespace="app-1",service="checkout"}) '
        "and hit count, most hits first. Useful for discovering which "
        "apps/namespaces have logs in the window. " + _COMMON,
        {
            "query": _optional_query("to list all streams in the window"),
            "start": _START,
            "end": _END,
            "limit": _limit("streams"),
        },
        [],
    ),
    _schema(
        FIELD_NAMES_TOOL,
        "List field names present in logs matching a LogsQL query in the "
        "query window, with hit counts. Useful before composing a query to "
        "discover which fields exist (e.g. level, service). " + _COMMON,
        {
            "query": _optional_query("to list fields across all logs"),
            "start": _START,
            "end": _END,
        },
        [],
    ),
    _schema(
        FIELD_VALUES_TOOL,
        "List unique values of a given field in logs matching a LogsQL query "
        "in the query window, with hit counts, most hits first. Useful for "
        "discovering valid label values (e.g. all distinct namespaces or "
        "service names). " + _COMMON,
        {
            "field": {
                "type": "string",
                "description": (
                    "Name of the field to enumerate (e.g. level, namespace): "
                    f"1 to {MAX_FIELD_CHARS} characters of A-Z, a-z, 0-9, _ . : -"
                ),
            },
            "query": _optional_query(""),
            "start": _START,
            "end": _END,
            "limit": _limit("values"),
        },
        ["field"],
    ),
    _schema(
        HITS_TOOL,
        "Return time-bucketed log counts for a LogsQL query in the query "
        "window: one {fields, timestamps, values, total} series (one per "
        "distinct value when field is set). Useful for spotting bursts of "
        "errors or traffic patterns. " + _COMMON,
        {
            "query": {
                "type": "string",
                "description": (
                    "LogsQL query (e.g. level:error). At most "
                    f"{MAX_QUERY_CHARS} characters on one line."
                ),
            },
            "start": _START,
            "end": _END,
            "step": {
                "type": "string",
                "description": (
                    "Bucket size <n>s, <n>m or <n>h (e.g. 1m, 5m, 1h; default "
                    f"{DEFAULT_STEP}); at most {MAX_BUCKETS} buckets over the "
                    "query window."
                ),
            },
            "field": {
                "type": "string",
                "description": (
                    "Optional field to group results by (e.g. level); returns "
                    "one series per distinct value."
                ),
            },
        },
        ["query"],
    ),
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
    "INVALID_STEP": "INVALID_PARAMS",
    "INVALID_FIELD": "INVALID_PARAMS",
    "QUERY_OUT_OF_WINDOW": "INVALID_PARAMS",
    "QUERY_CONSTRUCT_NOT_ALLOWED": "INVALID_PARAMS",
}

_LIMITS = (
    "At most 500 entries, streams or values; string values cut to 2000 "
    "characters and marked. One call reads at most 2 MiB from VictoriaLogs "
    "and returns at most 2 MiB (RESULT_TOO_LARGE otherwise, whole call); the "
    "view is limited to 25,000 tokens and refused whole above it, never "
    "truncated. 30 s per call, no retry. " + _ERRORS_TEXT
)
_WINDOW_FORMAT = (
    "The authorized absolute window is appended as {window}. The tool always "
    "bounds the call to the query window (start/end inside the authorized "
    "window, default the last hour or the alert's default window). "
)
_VALUES_FORMAT = (
    "The authorized target is appended as {values}. query is LogsQL of at "
    f"most {MAX_QUERY_CHARS} printable characters without options, join, "
    "union, stream_context, subqueries or single quotes outside string "
    "literals."
)


def _registration(
    name: str,
    verb: str,
    returns: str,
    window_extra: str,
    values_extra: str,
    parameters: Mapping[str, ParameterSpec],
    cannot_prove: str,
    max_window_seconds: int,
) -> ToolRegistration:
    return ToolRegistration(
        name=name,
        version="v1",
        source=otel_demo.SOURCE,
        verb=verb,
        may_contain_secrets=True,
        request_timeout_seconds=REQUEST_TIMEOUT_SECONDS,
        max_result_bytes=READ_LIMIT_BYTES,
        max_view_tokens=MAX_VIEW_TOKENS,
        max_window_seconds=max_window_seconds,
        error_classes=dict(_ERROR_CLASSES),
        result_path=("results",),
        description=ToolDescription(
            returns=returns,
            window_format=_WINDOW_FORMAT + window_extra,
            values_format=_VALUES_FORMAT + values_extra,
            limits=_LIMITS,
            cannot_prove=cannot_prove,
        ),
        parameters=dict(parameters),
    )


_P_START = ParameterSpec(
    "string", description="Absolute RFC3339 start; both or neither."
)
_P_END = ParameterSpec("string", description="Absolute RFC3339 end; both or neither.")
_META_WINDOW = "source_start_at/source_end_at are the query window itself."


def victorialogs_registrations(
    *, max_window_seconds: int
) -> tuple[ToolRegistration, ...]:
    """The five registrations (C5-10: ``source="otel-demo"``;
    ``may_contain_secrets=True``; verb ``query`` for the search, ``get`` for
    the four metadata tools)."""
    return (
        _registration(
            QUERY_TOOL,
            "query",
            "Log entries from the registered VictoriaLogs matching a LogsQL "
            "query, newest first, each the JSON object VictoriaLogs returned, "
            "under results. The view adds limit, long_values_cut, notes and "
            "incomplete_reason.",
            "source_start_at/source_end_at are the earliest and latest "
            "returned _time (the query window when no entry has one).",
            " limit 1 to 500.",
            {
                "query": ParameterSpec(
                    "string", required=True, description="LogsQL query."
                ),
                "start": _P_START,
                "end": _P_END,
                "limit": ParameterSpec("integer", description="Entries, 1 to 500."),
            },
            _CANNOT_PROVE_QUERY,
            max_window_seconds,
        ),
        _registration(
            STREAMS_TOOL,
            "get",
            "Log streams matching a LogsQL query in the window, each "
            "{stream, hits}, most hits first, under results.",
            _META_WINDOW,
            " query defaults to *; limit 1 to 500.",
            {
                "query": ParameterSpec("string", description="LogsQL query."),
                "start": _P_START,
                "end": _P_END,
                "limit": ParameterSpec("integer", description="Streams, 1 to 500."),
            },
            _CANNOT_PROVE_META,
            max_window_seconds,
        ),
        _registration(
            FIELD_NAMES_TOOL,
            "get",
            "Field names in logs matching a LogsQL query in the window, each "
            "{field, hits}, most hits first, under results.",
            _META_WINDOW,
            " query defaults to *.",
            {
                "query": ParameterSpec("string", description="LogsQL query."),
                "start": _P_START,
                "end": _P_END,
            },
            _CANNOT_PROVE_META,
            max_window_seconds,
        ),
        _registration(
            FIELD_VALUES_TOOL,
            "get",
            "Values of one field in logs matching a LogsQL query in the "
            "window, each {value, hits}, most hits first, under results.",
            _META_WINDOW,
            " field is 1 to 200 characters of A-Z a-z 0-9 _ . : -; query "
            "defaults to *; limit 1 to 500.",
            {
                "field": ParameterSpec(
                    "string", required=True, description="Field name."
                ),
                "query": ParameterSpec("string", description="LogsQL query."),
                "start": _P_START,
                "end": _P_END,
                "limit": ParameterSpec("integer", description="Values, 1 to 500."),
            },
            _CANNOT_PROVE_META,
            max_window_seconds,
        ),
        _registration(
            HITS_TOOL,
            "get",
            "Time-bucketed counts for a LogsQL query in the window, one "
            "{fields, timestamps, values, total} series per group, under "
            "results.",
            _META_WINDOW,
            f" step <n>s|m|h, at most {MAX_BUCKETS} buckets; field optional.",
            {
                "query": ParameterSpec(
                    "string", required=True, description="LogsQL query."
                ),
                "start": _P_START,
                "end": _P_END,
                "step": ParameterSpec("string", description="Bucket size."),
                "field": ParameterSpec("string", description="Group-by field."),
            },
            _CANNOT_PROVE_META,
            max_window_seconds,
        ),
    )
