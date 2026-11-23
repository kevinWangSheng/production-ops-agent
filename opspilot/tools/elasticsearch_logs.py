"""Elasticsearch 8 / OpenSearch 2 log tools (tool expansion B1, #560).

The three tools of upstream HolmesGPT's ``elasticsearch/data`` toolset
(``holmes/plugins/toolsets/elasticsearch/elasticsearch.py`` at ``a0009195``),
same names, served only by ``otel-demo-ext`` and only when
``OPSPILOT_ES_URL`` is set (decision table r9, B1-U1, B1-U2, B1-1 to B1-13;
docs/tasks/2026-11-16-tool-expansion-b1.md):

* ``elasticsearch_search`` -- ``POST /{index}/_search``, the one POST in
  product code. The body is built here from checked JSON-string parameters
  (``query``, ``aggregations``, ``sort``, ``source``) and fixed fields
  (``track_total_hits: true``, ``timeout``); scripts, runtime mappings,
  ``wrapper``, ``global``, ``pit``, ``scroll`` and stored scripts are refused
  at any depth before anything is sent (B1-6).
* ``elasticsearch_mappings`` -- ``GET /{index}/_mapping``, optionally through
  jq (the A2-8 child).
* ``elasticsearch_list_indices`` -- ``GET /_cat/indices/{pattern}?format=json``
  with whitelisted columns, sort, health, bytes and expand_wildcards,
  optionally through jq.

Time (B1-U1): before searching, the tool reads the indices' mapping. When a
matched index maps the time field (``@timestamp`` unless the model names
another date field that exists), the incident window is always added as a
``range`` filter the model cannot remove, the newest documents come first and
the source time is the min/max of that field over the matched documents (two
hidden aggregations, dropped from the view). Only when no matched index maps
any date field is no filter added: the source time stays empty and the view's
``cannot_prove`` says the documents cannot be proven to belong to the
incident window.

Read-only by construction (B1-3): fixed methods and paths; the model only
supplies index names, each checked against ``[a-z0-9._*-]`` (one leading
``-`` for exclusion, no leading ``_`` except ``_all``) and URL-encoded; no
redirects, no proxy. The ``es-ro`` credential (an API key, or a user name and
password) is this transport's own slot and is never rendered (B1-4).
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from opspilot.tools import otel_demo
from opspilot.tools.executor import (
    TransportError,
    TransportRequest,
    TransportResponse,
    TransportResultTooLarge,
    TransportTimeout,
    TransportUnavailable,
)
from opspilot.tools.http_deadline import DeadlineHandlers, RequestSockets
from opspilot.tools.kubernetes_core import (
    _Jq,
    _jq_problem,
    _output_lines,
    kubernetes_opener,
    require_jq,
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
    "API_KEY_ENV",
    "CA_FILE_ENV",
    "CREDENTIAL_REF",
    "ENDPOINT_SELECTOR",
    "FORBIDDEN_KEYS",
    "LIST_INDICES_TOOL",
    "MAPPINGS_TOOL",
    "PASSWORD_ENV",
    "SEARCH_TOOL",
    "TOOL_NAMES",
    "TOOL_SCHEMAS",
    "URL_ENV",
    "USERNAME_ENV",
    "ElasticsearchClient",
    "ElasticsearchConfig",
    "ElasticsearchTransport",
    "elasticsearch_enabled",
    "elasticsearch_registrations",
    "overflow_summary",
]

SEARCH_TOOL = "elasticsearch_search"
MAPPINGS_TOOL = "elasticsearch_mappings"
LIST_INDICES_TOOL = "elasticsearch_list_indices"
TOOL_NAMES: tuple[str, ...] = (SEARCH_TOOL, MAPPINGS_TOOL, LIST_INDICES_TOOL)

#: B1-4: this transport's own credential slot; never another backend's.
CREDENTIAL_REF = "es-ro"
#: B1-9: where the address travels in the lab target's selector.
ENDPOINT_SELECTOR = "elasticsearch_endpoint"

URL_ENV = "OPSPILOT_ES_URL"
API_KEY_ENV = "OPSPILOT_ES_API_KEY"
USERNAME_ENV = "OPSPILOT_ES_USERNAME"
PASSWORD_ENV = "OPSPILOT_ES_PASSWORD"
CA_FILE_ENV = "OPSPILOT_ES_CA_FILE"
_CREDENTIAL_ENVS = (API_KEY_ENV, USERNAME_ENV, PASSWORD_ENV, CA_FILE_ENV)

REQUEST_TIMEOUT_SECONDS = 30.0
#: B1-8: raw read and returned body ceiling (the registry maximum).
READ_LIMIT_BYTES = MAX_RESULT_BYTES
#: A 4xx error body is read this far.
ERROR_BODY_BYTES = 8192
#: B1-2.
DEFAULT_SIZE = 100
MAX_SIZE = 500
MAX_WINDOW_DOCS = 10_000
#: B1-6.
MAX_DSL_CHARS = 8000
#: B1-8.
MAX_VALUE_CHARS = 2000
#: B1-3: the whole index parameter.
MAX_INDEX_CHARS = 1000
MAX_TIME_FIELD_CHARS = 200
DEFAULT_TIME_FIELD = "@timestamp"
#: B1-6: refused anywhere in the model's JSON, compared case-folded. A key
#: with ``script`` as one of its ``_``-separated tokens is refused as well
#: (``SCRIPT_TOKEN``), so Painless cannot arrive under a name this list does
#: not spell out (``minimum_should_match_script``, ``bucket_script``,
#: ``map_script``, ``_script``), while field names that merely contain the
#: letters (``description``, ``transcript_id``) are sent. ``indexed_shape``
#: reads a shape from another document (independent review, PR #705).
FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {
        "script",
        "script_score",
        "script_fields",
        "scripted_metric",
        "runtime_mappings",
        "wrapper",
        "global",
        "pit",
        "scroll",
        "stored_scripts",
        "indexed_shape",
    }
)
SCRIPT_TOKEN = "script"
#: Refused inside ``aggregations`` only, where they are aggregation types:
#: ``significant_terms``/``significant_text`` take their background counts
#: from the whole index, outside the window, and ``children``/``parent`` join
#: other documents. In ``query`` or ``sort`` such a key is a field name.
FORBIDDEN_AGGREGATIONS: frozenset[str] = frozenset(
    {"significant_terms", "significant_text", "children", "parent"}
)
#: B1-7: the two hidden aggregations that report the source time.
_TIME_MIN_AGG = "opspilot_source_time_min"
_TIME_MAX_AGG = "opspilot_source_time_max"
_DATE_TYPES = frozenset({"date", "date_nanos"})
#: How many index or field names a note quotes at most.
_NOTE_NAMES = 20

#: Upstream's ``_cat/indices`` columns (``elasticsearch.py:812-816``).
CAT_COLUMNS: tuple[str, ...] = (
    "index",
    "health",
    "status",
    "pri",
    "rep",
    "docs.count",
    "docs.deleted",
    "store.size",
    "pri.store.size",
    "creation.date",
    "creation.date.string",
)
DEFAULT_COLUMNS = "index,health,status,docs.count,store.size"
DEFAULT_CAT_SORT = "index"
HEALTH_VALUES: tuple[str, ...] = ("green", "yellow", "red")
BYTES_VALUES: tuple[str, ...] = ("b", "kb", "mb", "gb", "tb", "pb")
EXPAND_WILDCARDS_VALUES: tuple[str, ...] = ("open", "closed", "hidden", "none", "all")

_INDEX_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789._*-")
#: The one path a GET may carry a body to (#808).
_EXPLAIN_PATH = "/_cluster/allocation/explain"


# -- configuration -------------------------------------------------------------


def elasticsearch_enabled(env: Mapping[str, str]) -> bool:
    """B1-U2: whether the extended profile shows the three tools. Only the
    address is read -- the web calls this too and never reads a credential."""
    return bool(env.get(URL_ENV))


def _printable(value: str) -> bool:
    return bool(value) and value.isprintable() and value == value.strip()


def _clean_url(value: str) -> str:
    try:
        parts = urllib.parse.urlsplit(value)
        parts.port
    except ValueError:
        raise ToolContractError("ES_CONFIG_INVALID") from None
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or "@" in parts.netloc
        or parts.query
        or parts.fragment
        or parts.path not in ("", "/")
    ):
        raise ToolContractError("ES_CONFIG_INVALID")
    return value.rstrip("/")


@dataclass(frozen=True)
class ElasticsearchConfig:
    """The worker's ``es-ro`` connection (B1-4): address, one of an API key
    or a user name and password, and a CA file for HTTPS.

    Read only by the worker when it builds the executor; the web reads the
    address alone (``elasticsearch_enabled``). The secret values are never
    rendered.
    """

    url: str
    api_key: str | None = field(default=None, repr=False)
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    ca_file: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url:
            raise ToolContractError("ES_CONFIG_MISSING")
        object.__setattr__(self, "url", _clean_url(self.url))
        has_key = self.api_key is not None
        has_basic = self.username is not None or self.password is not None
        if not has_key and not has_basic:
            raise ToolContractError("ES_CONFIG_MISSING")
        if has_key and has_basic:
            raise ToolContractError("ES_CONFIG_INVALID")
        # Half a user/password pair is a malformed value, not a missing one.
        values = [self.api_key] if has_key else [self.username, self.password]
        if any(not isinstance(value, str) or not _printable(value) for value in values):
            raise ToolContractError("ES_CONFIG_INVALID")
        if has_key and any(ch.isspace() for ch in str(self.api_key)):
            raise ToolContractError("ES_CONFIG_INVALID")
        if has_basic and ":" in str(self.username):
            raise ToolContractError("ES_CONFIG_INVALID")
        if self.ca_file is not None:
            if not self.url.startswith("https:"):
                raise ToolContractError("ES_CONFIG_INVALID")
            path = Path(self.ca_file)
            if not path.is_file() or not os.access(path, os.R_OK):
                raise ToolContractError("ES_CONFIG_INVALID")

    @property
    def authorization(self) -> str:
        """The ``Authorization`` header value behind ``CREDENTIAL_REF``."""
        if self.api_key is not None:
            return f"ApiKey {self.api_key}"
        pair = f"{self.username}:{self.password}".encode()
        return "Basic " + base64.b64encode(pair).decode("ascii")

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> ElasticsearchConfig | None:
        """``OPSPILOT_ES_*`` (B1-4). No address: ``None`` -- unless a
        credential or CA file is set, which is ``ES_CONFIG_INVALID`` so a
        credential is never silently ignored. With an address, neither an API
        key nor a user name and password is ``ES_CONFIG_MISSING``, both (or
        half a pair) ``ES_CONFIG_INVALID``."""
        url = env.get(URL_ENV) or ""
        values = {name: env.get(name) or None for name in _CREDENTIAL_ENVS}
        if not url:
            if any(values.values()):
                raise ToolContractError("ES_CONFIG_INVALID")
            return None
        return cls(
            url=url,
            api_key=values[API_KEY_ENV],
            username=values[USERNAME_ENV],
            password=values[PASSWORD_ENV],
            ca_file=values[CA_FILE_ENV],
        )


# -- HTTP ----------------------------------------------------------------------


def es_opener(ca_file: str | None) -> urllib.request.OpenerDirector:
    """The A2 opener: no proxy, no redirects; HTTPS verified against
    ``ca_file`` only when one is configured, else urllib's default verified
    context (the system roots), never unverified."""
    return kubernetes_opener(ca_file)


@dataclass(frozen=True)
class _Fetched:
    status: int
    body: bytes


def _monotonic() -> float:
    return time.monotonic()


def _remaining(deadline: float) -> float:
    left = deadline - _monotonic()
    if left <= 0:
        raise TransportTimeout("TIMEOUT", source_contact="possible")
    return left


def index_path(index: str) -> str:
    """The URL path segment of a checked index expression: each
    comma-separated name percent-encoded on its own (``*`` kept)."""
    return ",".join(urllib.parse.quote(part, safe="*") for part in index.split(","))


class ElasticsearchClient:
    """The two requests this backend may receive: a GET of a fixed read path
    (with a body only to ``/_cluster/allocation/explain``, #808) and
    ``POST /{index}/_search``; nothing else can be built (B1-3).

    ``credentials`` maps ``CREDENTIAL_REF`` to the ``Authorization`` header
    value; a missing slot is ``CREDENTIAL_REF_UNKNOWN`` before anything is
    sent, and no other slot is ever tried. ``opener`` replaces the default.
    """

    def __init__(
        self,
        *,
        credentials: Mapping[str, str | None],
        opener: urllib.request.OpenerDirector | None = None,
        ca_file: str | None = None,
    ) -> None:
        self._credentials = dict(credentials)
        self._opener = opener or es_opener(ca_file)
        self._deadline_handlers = DeadlineHandlers(self._opener)

    def authorization(self) -> str:
        value = self._credentials.get(CREDENTIAL_REF)
        if not value:
            raise TransportError("CREDENTIAL_REF_UNKNOWN")
        return value

    def get_mapping(
        self, endpoint: str, index: str, *, deadline: float, max_bytes: int
    ) -> _Fetched:
        return self._send(
            "GET",
            f"{endpoint}/{index_path(index)}/_mapping",
            None,
            deadline=deadline,
            max_bytes=max_bytes,
        )

    def cat_indices(
        self,
        endpoint: str,
        pattern: str,
        query: Sequence[tuple[str, str]],
        *,
        deadline: float,
        max_bytes: int,
    ) -> _Fetched:
        url = (
            f"{endpoint}/_cat/indices/{index_path(pattern)}?"
            + urllib.parse.urlencode([("format", "json"), *query])
        )
        return self._send("GET", url, None, deadline=deadline, max_bytes=max_bytes)

    def get(
        self,
        endpoint: str,
        path: str,
        query: Sequence[tuple[str, str]],
        body: Mapping[str, Any] | None,
        *,
        deadline: float,
        max_bytes: int,
    ) -> _Fetched:
        """A GET of a fixed read path built from checked values (#808); a
        body only for ``/_cluster/allocation/explain``, as upstream sends."""
        url = endpoint + path
        if query:
            url += "?" + urllib.parse.urlencode(list(query))
        data: bytes | None = None
        if body is not None:
            text = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
            data = text.encode("utf-8")
        return self._send("GET", url, data, deadline=deadline, max_bytes=max_bytes)

    def search(
        self,
        endpoint: str,
        index: str,
        body: Mapping[str, Any],
        *,
        deadline: float,
        max_bytes: int,
    ) -> _Fetched:
        """The only POST: ``/{index}/_search``, no query string."""
        data = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        return self._send(
            "POST",
            f"{endpoint}/{index_path(index)}/_search",
            data.encode("utf-8"),
            deadline=deadline,
            max_bytes=max_bytes,
        )

    def _send(
        self,
        method: str,
        url: str,
        data: bytes | None,
        *,
        deadline: float,
        max_bytes: int,
    ) -> _Fetched:
        """One request bounded by ``deadline`` (monotonic) and ``max_bytes``.

        2xx and 4xx come back (a 4xx body cut at ``ERROR_BODY_BYTES``); 5xx is
        ``TransportUnavailable``; a 2xx body past ``max_bytes`` is
        ``TransportResultTooLarge``; the deadline is ``TransportTimeout``.
        """
        if method == "POST":
            path = urllib.parse.urlsplit(url)
            if not path.path.endswith("/_search") or path.query or data is None:
                raise TransportError("METHOD_NOT_ALLOWED")
        elif method != "GET":
            raise TransportError("METHOD_NOT_ALLOWED")
        elif data is not None:
            # #808: the one GET with a body is allocation explain naming its
            # shard (upstream sends it so; ES 8 and OpenSearch 2 both read it).
            path = urllib.parse.urlsplit(url)
            if not path.path.endswith(_EXPLAIN_PATH) or path.query:
                raise TransportError("METHOD_NOT_ALLOWED")
        headers = {
            "Accept": "application/json",
            "Authorization": self.authorization(),
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        http_request = urllib.request.Request(
            url, data=data, headers=headers, method=method
        )
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

        thread = threading.Thread(target=run, daemon=True, name="es-query")
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


# -- input checks ----------------------------------------------------------------


def _refused(code: str, text: str) -> TransportResponse:
    """A fixed-code refusal decided before anything was sent; ``notes`` carry
    the code and what to change (shown on the refusal view)."""
    return TransportResponse(
        body=canonical({"error": code}).encode(),
        source_status=code,
        sent=False,
        view_fields={"notes": [f"{code}: {text}"]},
    )


def _answered(code: str, text: str) -> TransportResponse:
    """A fixed-code refusal after the backend was read (mapping, jq)."""
    return TransportResponse(
        body=canonical({"error": code}).encode(),
        source_status=code,
        view_fields={"notes": [f"{code}: {text}"]},
    )


def check_index(value: object) -> bool:
    """B1-3: a comma-separated list of index names or patterns."""
    if not isinstance(value, str) or not value or len(value) > MAX_INDEX_CHARS:
        return False
    if ".." in value:
        return False
    for part in value.split(","):
        name = part[1:] if part.startswith("-") else part
        if not name or name.startswith("-"):
            return False
        if name.startswith("_") and name != "_all":
            return False
        if name != "_all" and not set(name) <= _INDEX_CHARS:
            return False
    return True


class _Forbidden(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def _references_document(key: str, inner: object) -> bool:
    """Whether a clause reads data from another index or document by
    reference, past the index check and the time filter: a ``terms`` lookup
    (``{"terms": {field: {"index": .., "id": ..}}}``), a ``more_like_this``
    ``like``/``unlike`` document naming ``_index``, or a ``percolate`` of a
    stored document (``index``/``id``). A ``terms`` aggregation's own object
    values (``order``, ``include`` partitions) carry neither key."""
    if not isinstance(inner, Mapping):
        return False
    if key == "terms":
        return any(
            isinstance(value, Mapping) and ({"index", "id"} & set(value))
            for value in inner.values()
        )
    if key == "more_like_this":
        for side in ("like", "unlike"):
            items = inner.get(side)
            items = items if isinstance(items, list) else [items]
            if any(isinstance(doc, Mapping) and "_index" in doc for doc in items):
                return True
        return False
    if key == "percolate":
        return bool({"index", "id"} & set(inner))
    return False


def _forbidden_name(name: str, *, aggregations: bool) -> bool:
    if name in FORBIDDEN_KEYS or SCRIPT_TOKEN in name.split("_"):
        return True
    return aggregations and name in FORBIDDEN_AGGREGATIONS


def _check_forbidden(value: object, *, aggregations: bool = False) -> None:
    """Raise ``_Forbidden`` for the first forbidden construct at any depth;
    ``aggregations`` adds the aggregation-only types."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, Mapping):
            for key, inner in item.items():
                if isinstance(key, str):
                    name = key.lower()
                    if _forbidden_name(name, aggregations=aggregations):
                        raise _Forbidden(name)
                    if _references_document(name, inner):
                        raise _Forbidden(f"{name} by document reference")
                stack.append(inner)
        elif isinstance(item, list):
            stack.extend(item)


_JSON_KINDS: Mapping[str, tuple[type, ...]] = {
    "query": (dict,),
    "aggregations": (dict,),
    "sort": (list, dict, str),
    "source": (list, dict, str, bool),
}


def _dsl_params(
    params: Mapping[str, object],
) -> tuple[dict[str, Any], TransportResponse | None]:
    """B1-6: the four JSON-string parameters, decoded and checked."""
    decoded: dict[str, Any] = {}
    for name, kinds in _JSON_KINDS.items():
        text = params.get(name)
        if text is None:
            continue
        if not isinstance(text, str) or len(text) > MAX_DSL_CHARS:
            return {}, _refused(
                "INVALID_JSON",
                f"{name} must be a JSON string of at most {MAX_DSL_CHARS} characters.",
            )
        try:
            value = json.loads(text)
        except (ValueError, RecursionError):
            return {}, _refused("INVALID_JSON", f"{name} is not valid JSON.")
        if type(value) not in kinds:
            return {}, _refused(
                "INVALID_JSON",
                f"{name} must be JSON of type "
                + " or ".join(_kind_name(kind) for kind in kinds)
                + ".",
            )
        try:
            _check_forbidden(value, aggregations=name == "aggregations")
        except _Forbidden as found:
            return {}, _refused(
                "QUERY_CONSTRUCT_NOT_ALLOWED",
                f"{name} contains {found.key}, which this tool refuses before "
                "sending: no key with script as a _-separated part, no "
                "runtime_mappings, wrapper, global, pit, scroll or indexed_shape, "
                "no significant_terms, significant_text, children or parent "
                "aggregation, and no terms lookup, "
                "more_like_this document with _index or percolate of a stored "
                "document, at any depth.",
            )
        decoded[name] = value
    aggregations = decoded.get("aggregations")
    if isinstance(aggregations, dict) and (
        _TIME_MIN_AGG in aggregations or _TIME_MAX_AGG in aggregations
    ):
        return {}, _refused(
            "AGGREGATION_NAME_RESERVED",
            f"aggregation names {_TIME_MIN_AGG} and {_TIME_MAX_AGG} are used by "
            "the tool; rename the aggregation.",
        )
    return decoded, None


def _kind_name(kind: type) -> str:
    return {dict: "object", list: "array", str: "string", bool: "boolean"}[kind]


def _query_window(
    request: TransportRequest,
) -> tuple[Window | None, TransportResponse | None]:
    """``start_time``/``end_time`` by the A3-1 rule (``traces_search``'s):
    both or neither; neither is the default query window."""
    params = request.params
    renamed = replace(
        request,
        params={
            key: value
            for key, value in (
                ("start", params.get("start_time")),
                ("end", params.get("end_time")),
            )
            if value is not None
        },
    )
    window, refusal = otel_demo._query_window(renamed)
    if refusal is not None or window is None:
        frame = request.window.as_json()
        return None, _refused(
            "QUERY_OUT_OF_WINDOW",
            "start_time and end_time must both be absolute RFC3339 times with "
            "a timezone, or both omitted, and lie inside the authorized window "
            f"{frame['start']} to {frame['end']}.",
        )
    return window, None


def _csv_ok(value: str, allowed: Sequence[str], *, sort: bool = False) -> bool:
    parts = value.split(",")
    for part in parts:
        name = part
        if sort and ":" in part:
            name, direction = part.rsplit(":", 1)
            if direction not in ("asc", "desc"):
                return False
        if name not in allowed:
            return False
    return bool(parts)


# -- responses -------------------------------------------------------------------


def _decode(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None


def _source_error(fetched: _Fetched, index: str) -> TransportResponse:
    """B1-12: a 4xx as its fixed class, echoing ``error.type`` and the
    reason (the executor redacts and bounds the text); a missing index is
    ``INDEX_NOT_FOUND``."""
    payload = _decode(fetched.body)
    error = payload.get("error") if isinstance(payload, dict) else None
    error_type: str | None = None
    reason: str | None = None
    if isinstance(error, dict):
        kind = error.get("type")
        error_type = kind if isinstance(kind, str) else None
        text = error.get("reason")
        reason = text if isinstance(text, str) else None
        causes = error.get("root_cause")
        if isinstance(causes, list) and causes and isinstance(causes[0], dict):
            cause_type = causes[0].get("type")
            cause_reason = causes[0].get("reason")
            if (
                isinstance(cause_reason, str)
                and cause_reason != reason
                and isinstance(cause_type, str)
            ):
                reason = (
                    f"{reason or error_type}; root cause {cause_type}: {cause_reason}"
                )
    elif isinstance(error, str):
        reason = error
    if fetched.status == 404 and error_type in (
        "index_not_found_exception",
        "index_not_found",
    ):
        return TransportResponse(
            body=canonical({"error": "INDEX_NOT_FOUND", "index": index}).encode(),
            source_status="INDEX_NOT_FOUND",
            http_status=404,
            source_error_type=error_type,
            source_error_text=(
                f"INDEX_NOT_FOUND: {reason or 'no such index'}; list the indices "
                "with elasticsearch_list_indices and retry with an existing "
                "name or a wildcard pattern."
            ),
        )
    return TransportResponse(
        body=fetched.body,
        source_status=str(fetched.status),
        http_status=fetched.status,
        source_error_type=error_type,
        source_error_text=reason,
    )


def _cut_strings(value: Any, path: str, cut: list[str]) -> Any:
    """B1-8: every string value cut to ``MAX_VALUE_CHARS``; ``cut`` collects
    the dotted paths (list positions omitted) of the values that were."""
    if isinstance(value, str):
        if len(value) > MAX_VALUE_CHARS:
            if path not in cut:
                cut.append(path)
            return value[:MAX_VALUE_CHARS]
        return value
    if isinstance(value, dict):
        return {
            key: _cut_strings(inner, f"{path}.{key}" if path else str(key), cut)
            for key, inner in value.items()
        }
    if isinstance(value, list):
        return [_cut_strings(inner, path, cut) for inner in value]
    return value


def _date_fields(properties: object, prefix: str = "") -> list[str]:
    """Dotted names of the ``date``/``date_nanos`` fields of a mapping's
    ``properties``, objects and multi-fields (``fields``) included."""
    found: list[str] = []
    if not isinstance(properties, dict):
        return found
    for name, spec in properties.items():
        if not isinstance(spec, dict):
            continue
        dotted = f"{prefix}{name}"
        if spec.get("type") in _DATE_TYPES:
            found.append(dotted)
        inner = spec.get("properties")
        if isinstance(inner, dict):
            found.extend(_date_fields(inner, dotted + "."))
        multi = spec.get("fields")
        if isinstance(multi, dict):
            # A date multi-field ``name.sub`` is a time field too (fail
            # closed, B1-U1).
            found.extend(_date_fields(multi, dotted + "."))
    return found


def _mapping_date_fields(mappings: dict[str, Any]) -> list[str]:
    """Every date field an index maps: ``properties`` and the ``runtime``
    section (a runtime date field can be filtered, so an index whose only
    time field is defined there is still filtered, B1-U1)."""
    found = _date_fields(mappings.get("properties"))
    runtime = mappings.get("runtime")
    if isinstance(runtime, dict):
        for name, spec in runtime.items():
            if (
                isinstance(spec, dict)
                and spec.get("type") in _DATE_TYPES
                and str(name) not in found
            ):
                found.append(str(name))
    return found


def _index_date_fields(payload: object) -> dict[str, list[str]] | None:
    """``{index: [date fields]}`` of a ``_mapping`` answer; ``None`` when it
    is not that shape."""
    if not isinstance(payload, dict):
        return None
    result: dict[str, list[str]] = {}
    for index, entry in payload.items():
        if not isinstance(entry, dict):
            return None
        mappings = entry.get("mappings")
        if not isinstance(mappings, dict):
            return None
        result[str(index)] = _mapping_date_fields(mappings)
    return result


def _epoch_millis(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _names(names: Sequence[str]) -> str:
    shown = ", ".join(names[:_NOTE_NAMES])
    more = len(names) - _NOTE_NAMES
    return shown + (f" and {more} more" if more > 0 else "")


# -- transport -------------------------------------------------------------------


class ElasticsearchTransport:
    """Serves the three tools over one ``ElasticsearchClient`` (``es-ro``).

    Every refusal of the model's input is decided before anything is sent;
    the search's time-field choice is decided after the mapping read and
    before the search is sent.
    """

    def __init__(
        self,
        *,
        credentials: Mapping[str, str | None],
        jq_path: str | None = None,
        opener: urllib.request.OpenerDirector | None = None,
        ca_file: str | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._client = ElasticsearchClient(
            credentials=credentials, opener=opener, ca_file=ca_file
        )
        self._jq_path = jq_path
        self._monotonic = monotonic or _monotonic

    def fetch(self, request: TransportRequest) -> TransportResponse:
        if request.tool not in TOOL_NAMES:
            raise TransportError("TOOL_NOT_SUPPORTED")
        deadline = self._monotonic() + request.timeout_seconds
        if request.tool == SEARCH_TOOL:
            return self._search(request, deadline)
        if request.tool == MAPPINGS_TOOL:
            return self._mappings(request, deadline)
        return self._list_indices(request, deadline)

    def _endpoint(self, request: TransportRequest) -> str:
        self._client.authorization()  # a missing slot fails before sending
        endpoint = request.selector.get(ENDPOINT_SELECTOR, "")
        if not endpoint:
            raise TransportError("ES_ENDPOINT_MISSING")
        return endpoint.rstrip("/")

    def _jq(self) -> _Jq:
        if self._jq_path is None:
            self._jq_path = require_jq()
        return _Jq(self._jq_path)

    # -- elasticsearch_search ------------------------------------------------

    def _search(self, request: TransportRequest, deadline: float) -> TransportResponse:
        params = request.params
        index = params.get("index")
        if not check_index(index):
            return _refused("INVALID_INDEX", _INDEX_RULE)
        assert isinstance(index, str)
        size = params.get("size", DEFAULT_SIZE)
        if size is None:
            size = DEFAULT_SIZE
        if type(size) is not int or not 0 <= size <= MAX_SIZE:
            return _refused("INVALID_SIZE", f"size must be 0 to {MAX_SIZE}.")
        offset = params.get("from_offset", 0)
        if offset is None:
            offset = 0
        if type(offset) is not int or offset < 0 or offset + size > MAX_WINDOW_DOCS:
            return _refused(
                "INVALID_FROM",
                f"from_offset must be 0 or more and from_offset + size at most "
                f"{MAX_WINDOW_DOCS}.",
            )
        profile = params.get("profile")
        time_field = params.get("time_field")
        if time_field is not None and (
            not isinstance(time_field, str)
            or not time_field
            or len(time_field) > MAX_TIME_FIELD_CHARS
            or not time_field.isprintable()
        ):
            return _refused(
                "INVALID_TIME_FIELD",
                f"time_field must be a field name of at most "
                f"{MAX_TIME_FIELD_CHARS} characters.",
            )
        dsl, refusal = _dsl_params(params)
        if refusal is not None:
            return refusal
        window, refusal = _query_window(request)
        if refusal is not None:
            return refusal
        assert window is not None
        endpoint = self._endpoint(request)

        # B1-U1: the mapping decides the time field before the search is sent.
        mapped = self._client.get_mapping(
            endpoint, index, deadline=deadline, max_bytes=READ_LIMIT_BYTES
        )
        if mapped.status != 200:
            return _source_error(mapped, index)
        fields = _index_date_fields(_decode(mapped.body))
        if fields is None:
            return TransportResponse(body=b"")  # MALFORMED_RESULT
        all_dates = sorted({name for names in fields.values() for name in names})
        notes: list[str] = []
        chosen = time_field if isinstance(time_field, str) else DEFAULT_TIME_FIELD
        chosen_field: str | None = None
        if not fields:
            pass  # no index matched: nothing is searched below
        elif not all_dates:
            # B1-U1: only an index whose mapping has no date field at all is
            # searched without a time filter.
            if time_field is not None:
                notes.append(
                    f"time_field {time_field} was ignored: no matched index maps "
                    "any date field."
                )
        else:
            timed = sorted(name for name, dates in fields.items() if chosen in dates)
            untimed = sorted(
                name for name, dates in fields.items() if chosen not in dates
            )
            if not timed:
                if time_field is not None:
                    return _answered(
                        "TIME_FIELD_NOT_FOUND",
                        f"time_field {time_field} is not a date field in the "
                        "mapping of any matched index; the mapped date fields "
                        f"are {_names(all_dates)}.",
                    )
                return _answered(
                    "TIME_FIELD_REQUIRED",
                    f"the matched indices do not map {DEFAULT_TIME_FIELD} but map "
                    f"other date fields ({_names(all_dates)}); give one of them "
                    "as time_field. The time filter cannot be turned off.",
                )
            chosen_field = chosen
            if untimed:
                notes.append(
                    f"{len(untimed)} matched indices do not map {chosen} as a date "
                    "field, so none of their documents can match the time filter "
                    f"and none is returned: {_names(untimed)}."
                )

        if not fields:
            # B1-12: a wildcard that matches no index is no data, not an error.
            return self._empty_search(index, size, notes)

        body: dict[str, Any] = {}
        user_query = dsl.get("query")
        if chosen_field is not None:
            time_range = {
                "range": {
                    chosen_field: {
                        "gte": window.start.isoformat(),
                        "lte": window.end.isoformat(),
                        "format": "strict_date_optional_time",
                    }
                }
            }
            boolean: dict[str, Any] = {"filter": [time_range]}
            if user_query:
                boolean["must"] = [user_query]
            body["query"] = {"bool": boolean}
        elif user_query:
            body["query"] = user_query
        body["size"] = size
        if offset:
            body["from"] = offset
        if "sort" in dsl:
            body["sort"] = dsl["sort"]
        elif chosen_field is not None:
            body["sort"] = [{chosen_field: {"order": "desc"}}]
        if "source" in dsl:
            body["_source"] = dsl["source"]
        aggregations = dict(dsl.get("aggregations") or {})
        if chosen_field is not None:
            aggregations[_TIME_MIN_AGG] = {"min": {"field": chosen_field}}
            aggregations[_TIME_MAX_AGG] = {"max": {"field": chosen_field}}
        if aggregations:
            body["aggs"] = aggregations
        if profile is True:
            body["profile"] = True
        body["track_total_hits"] = True
        body["timeout"] = f"{max(1, int(_remaining(deadline)))}s"

        fetched = self._client.search(
            endpoint, index, body, deadline=deadline, max_bytes=READ_LIMIT_BYTES
        )
        if fetched.status != 200:
            return _source_error(fetched, index)
        payload = _decode(fetched.body)
        if not isinstance(payload, dict):
            return TransportResponse(body=b"")
        return self._project(
            request,
            payload,
            index=index,
            size=size,
            window=window,
            chosen_field=chosen_field,
            indices_matched=len(fields),
            requested_aggregations="aggregations" in dsl,
            notes=notes,
        )

    @staticmethod
    def _empty_search(index: str, size: int, notes: list[str]) -> TransportResponse:
        notes = [
            *notes,
            f"No index matches {index}; nothing was searched. List the indices "
            "with elasticsearch_list_indices.",
        ]
        total = {"value": 0, "relation": "eq"}
        body = canonical({"results": [], "total": total}).encode("utf-8")
        return TransportResponse(
            body=body,
            row_unit="summaries" if size == 0 else "documents",
            backend_rows_returned=0,
            view_fields={
                "index": index,
                "indices_matched": 0,
                "time_filter": {"applied": False, "field": None},
                "total": total,
                "notes": notes,
                "incomplete_reason": None,
                "cannot_prove": _SEARCH_CANNOT_PROVE,
            },
        )

    def _project(
        self,
        request: TransportRequest,
        payload: dict[str, Any],
        *,
        index: str,
        size: int,
        window: Window,
        chosen_field: str | None,
        indices_matched: int,
        requested_aggregations: bool,
        notes: list[str],
    ) -> TransportResponse:
        hits_block = payload.get("hits")
        if not isinstance(hits_block, dict):
            return TransportResponse(body=b"")
        raw_hits = hits_block.get("hits")
        total_raw = hits_block.get("total")
        if not isinstance(raw_hits, list):
            return TransportResponse(body=b"")
        if isinstance(total_raw, dict):
            total = {
                "value": total_raw.get("value"),
                "relation": total_raw.get("relation"),
            }
        elif type(total_raw) is int:
            total = {"value": total_raw, "relation": "eq"}
        else:
            return TransportResponse(body=b"")
        if type(total["value"]) is not int:
            return TransportResponse(body=b"")
        aggregations_raw = payload.get("aggregations")
        aggregations: dict[str, Any] = (
            dict(aggregations_raw) if isinstance(aggregations_raw, dict) else {}
        )
        start_at = end_at = None
        if chosen_field is not None:
            low = aggregations.pop(_TIME_MIN_AGG, None)
            high = aggregations.pop(_TIME_MAX_AGG, None)
            start_at = _epoch_millis(
                low.get("value") if isinstance(low, dict) else None
            )
            end_at = _epoch_millis(
                high.get("value") if isinstance(high, dict) else None
            )
            if start_at is None or end_at is None:
                start_at = end_at = None
            else:
                # The filter keeps both inside the window; clamp a rounding.
                start_at = min(max(start_at, window.start), window.end)
                end_at = max(min(end_at, window.end), start_at)
        cut: list[str] = []
        hits: list[dict[str, Any]] = []
        for hit in raw_hits:
            if not isinstance(hit, dict):
                return TransportResponse(body=b"")
            row: dict[str, Any] = {"_index": hit.get("_index"), "_id": hit.get("_id")}
            for key in ("_source", "fields", "sort"):
                if key in hit:
                    row[key] = hit[key]
            row_cut: list[str] = []
            row = _cut_strings(row, "", row_cut)
            if row_cut:
                row["_truncated_fields"] = row_cut
                cut.extend(path for path in row_cut if path not in cut)
            hits.append(row)
        aggregations = _cut_strings(aggregations, "aggregations", cut)
        shards_raw = payload.get("_shards")
        shards = {
            key: shards_raw.get(key)
            for key in ("total", "successful", "skipped", "failed")
            if isinstance(shards_raw, dict) and key in shards_raw
        }
        timed_out = payload.get("timed_out") is True
        failed = shards.get("failed")
        failed_count = failed if type(failed) is int else 0

        reasons: list[str] = []
        if size and (total["value"] > len(hits) or total["relation"] != "eq"):
            reasons.append(
                f"total: {total['value']} documents matched and {len(hits)} are "
                "shown; narrow the query or the window, aggregate with "
                "aggregations, or page with from_offset"
            )
        if timed_out:
            reasons.append(
                "timed_out: Elasticsearch stopped searching at the request "
                "timeout and returned what it had collected"
            )
        if failed_count > 0:
            reasons.append(
                f"shards: {failed_count} shards failed and their documents are "
                "missing from the result"
            )
        # B1-8: each reason is also stated in notes, beside incomplete_reason.
        notes.extend(f"Incomplete result ({reason})." for reason in reasons)
        if cut:
            notes.append(
                f"String values longer than {MAX_VALUE_CHARS} characters are cut "
                f"there ({_names(cut)}); each hit lists its cut fields under "
                "_truncated_fields."
            )
        if chosen_field is not None:
            notes.append(
                f"The tool filtered {chosen_field} to the query window; this "
                "filter cannot be turned off."
            )
        else:
            notes.append(
                "No matched index maps a date field, so no time filter was "
                "applied and the source time is unknown."
            )

        searched_none = shards.get("total") == 0
        if searched_none:
            notes.append(
                f"No index matched {index} when the search ran (0 shards "
                "searched); nothing was searched."
            )
        if size == 0:
            # B1-12: only a search over at least one index has a summary; a
            # pattern that matched none is no data, also with size 0.
            rows: list[dict[str, Any]] = (
                []
                if searched_none
                else [{"total": total, "aggregations": aggregations}]
            )
            row_unit = "summaries"
        else:
            rows = hits
            row_unit = "documents"
        document: dict[str, Any] = {
            "results": rows,
            "total": total,
            "took": payload.get("took"),
            "timed_out": timed_out,
            "shards": shards,
        }
        if size and requested_aggregations:
            document["aggregations"] = aggregations
        profile = payload.get("profile")
        if profile is not None:
            document["profile"] = profile
        body = canonical(document).encode("utf-8")
        if len(body) > request.max_result_bytes:
            raise TransportResultTooLarge(str(request.max_result_bytes))
        view_fields: dict[str, Any] = {
            "index": index,
            "indices_matched": indices_matched,
            "time_filter": (
                {
                    "applied": True,
                    "field": chosen_field,
                    "start": window.start.isoformat(),
                    "end": window.end.isoformat(),
                }
                if chosen_field is not None
                else {"applied": False, "field": None}
            ),
            "total": total,
            "took": document["took"],
            "timed_out": timed_out,
            "shards": shards,
            "long_values_cut": len(cut),
            "incomplete_reason": "; ".join(reasons) or None,
            "notes": notes,
            "cannot_prove": (
                _SEARCH_CANNOT_PROVE
                if chosen_field is not None
                else _SEARCH_CANNOT_PROVE + " " + _UNTIMED_CANNOT_PROVE
            ),
        }
        if "aggregations" in document:
            view_fields["aggregations"] = aggregations
        if profile is not None:
            view_fields["profile"] = profile
        return TransportResponse(
            body=body,
            source_start_at=start_at,
            source_end_at=end_at,
            query_window=window if chosen_field is not None else None,
            row_unit=row_unit,
            backend_rows_returned=len(rows),
            incomplete=bool(reasons),
            view_fields=view_fields,
        )

    # -- elasticsearch_mappings ------------------------------------------------

    def _mappings(
        self, request: TransportRequest, deadline: float
    ) -> TransportResponse:
        params = request.params
        index = params.get("index")
        if not check_index(index):
            return _refused("INVALID_INDEX", _INDEX_RULE)
        assert isinstance(index, str)
        expr = params.get("jq")
        if expr is not None and _jq_problem(expr):
            return _refused("INVALID_JQ", _JQ_RULE)
        endpoint = self._endpoint(request)
        jq = None if expr is None else self._jq()
        fetched = self._client.get_mapping(
            endpoint, index, deadline=deadline, max_bytes=READ_LIMIT_BYTES
        )
        if fetched.status != 200:
            return _source_error(fetched, index)
        payload = _decode(fetched.body)
        if not isinstance(payload, dict) or _index_date_fields(payload) is None:
            return TransportResponse(body=b"")
        view_fields: dict[str, Any] = {
            "index": index,
            "indices_matched": len(payload),
            "notes": [_STATE_NOTE],
            "cannot_prove": _STATE_CANNOT_PROVE,
        }
        if jq is not None:
            assert isinstance(expr, str)
            return self._jq_rows(jq, expr, fetched.body, deadline, request, view_fields)
        rows = [
            {"index": name, "mappings": payload[name].get("mappings")}
            for name in sorted(payload)
        ]
        body = canonical({"results": rows}).encode("utf-8")
        if len(body) > request.max_result_bytes:
            raise TransportResultTooLarge(str(request.max_result_bytes))
        return TransportResponse(
            body=body,
            row_unit="indices",
            backend_rows_returned=len(rows),
            view_fields=view_fields,
        )

    # -- elasticsearch_list_indices --------------------------------------------

    def _list_indices(
        self, request: TransportRequest, deadline: float
    ) -> TransportResponse:
        params = request.params
        pattern = params.get("pattern")
        if pattern is None or pattern == "":
            pattern = "*"
        if not check_index(pattern):
            return _refused("INVALID_INDEX", _INDEX_RULE)
        assert isinstance(pattern, str)
        columns = params.get("columns") or DEFAULT_COLUMNS
        if not isinstance(columns, str) or not _csv_ok(columns, CAT_COLUMNS):
            return _refused(
                "INVALID_COLUMNS",
                "columns must be a comma-separated list of: "
                + ", ".join(CAT_COLUMNS)
                + ".",
            )
        sort = params.get("sort") or DEFAULT_CAT_SORT
        if not isinstance(sort, str) or not _csv_ok(sort, CAT_COLUMNS, sort=True):
            return _refused(
                "INVALID_SORT",
                "sort must be a comma-separated list of column or column:asc / "
                "column:desc, with columns from: " + ", ".join(CAT_COLUMNS) + ".",
            )
        query: list[tuple[str, str]] = [("h", columns), ("s", sort)]
        for name, allowed, many in (
            ("health", HEALTH_VALUES, False),
            ("bytes", BYTES_VALUES, False),
            ("expand_wildcards", EXPAND_WILDCARDS_VALUES, True),
        ):
            value = params.get(name)
            if value is None or value == "":
                continue
            ok = isinstance(value, str) and (
                _csv_ok(value, allowed) if many else value in allowed
            )
            if not ok:
                return _refused(
                    f"INVALID_{name.upper()}",
                    f"{name} must be "
                    + ("a comma-separated list of " if many else "one of ")
                    + ", ".join(allowed)
                    + ".",
                )
            assert isinstance(value, str)
            query.append((name, value))
        if params.get("pri") is True:
            query.append(("pri", "true"))
        expr = params.get("jq")
        if expr is not None and _jq_problem(expr):
            return _refused("INVALID_JQ", _JQ_RULE)
        endpoint = self._endpoint(request)
        jq = None if expr is None else self._jq()
        fetched = self._client.cat_indices(
            endpoint, pattern, query, deadline=deadline, max_bytes=READ_LIMIT_BYTES
        )
        if fetched.status != 200:
            return _source_error(fetched, pattern)
        payload = _decode(fetched.body)
        if not isinstance(payload, list) or not all(
            isinstance(item, dict) for item in payload
        ):
            return TransportResponse(body=b"")
        view_fields: dict[str, Any] = {
            "pattern": pattern,
            "columns": columns.split(","),
            "notes": [_STATE_NOTE],
            "cannot_prove": _STATE_CANNOT_PROVE,
        }
        if jq is not None:
            assert isinstance(expr, str)
            return self._jq_rows(jq, expr, fetched.body, deadline, request, view_fields)
        body = canonical({"results": payload}).encode("utf-8")
        if len(body) > request.max_result_bytes:
            raise TransportResultTooLarge(str(request.max_result_bytes))
        return TransportResponse(
            body=body,
            row_unit="indices",
            backend_rows_returned=len(payload),
            view_fields=view_fields,
        )

    def _jq_rows(
        self,
        jq: _Jq,
        expr: str,
        document: bytes,
        deadline: float,
        request: TransportRequest,
        view_fields: dict[str, Any],
    ) -> TransportResponse:
        """jq once over the backend's JSON answer (upstream ``filter_result``)."""
        status, output = jq.evaluate(expr, document, deadline=deadline)
        if status == 3:
            return _answered("JQ_COMPILE_ERROR", "the jq expression does not compile.")
        if status == 5:
            return _answered(
                "JQ_RUNTIME_ERROR", "the jq expression failed on the data."
            )
        lines = None if status != 0 else _output_lines(output)
        if lines is None:
            return _answered("JQ_FAILED", "jq did not produce JSON output.")
        rows = [value for _, value in lines]
        body = canonical({"results": rows}).encode("utf-8")
        if len(body) > request.max_result_bytes:
            raise TransportResultTooLarge(str(request.max_result_bytes))
        return TransportResponse(
            body=body,
            row_unit="results",
            backend_rows_returned=len(rows),
            view_fields={**view_fields, "jq_applied": True},
        )


def overflow_summary(
    tool: str, rows: Sequence[object], query: Mapping[str, object]
) -> dict[str, object]:
    """The over-limit refusal summary (#308 U5) with B1-8's suggestion."""
    if tool == SEARCH_TOOL:
        unit = "summaries" if query.get("size") == 0 else "documents"
        return {
            "row_unit": unit,
            "suggestion": (
                "Select fewer fields with source, lower size, narrow the query "
                "or the window, or count with aggregations and size 0."
            ),
        }
    unit = "results" if query.get("jq") is not None else "indices"
    return {
        "row_unit": unit,
        "suggestion": (
            "Narrow the index pattern or select fewer fields with jq."
            if tool == MAPPINGS_TOOL
            else "Narrow the pattern, choose fewer columns, or filter with jq."
        ),
    }


# -- model-visible face and registrations ---------------------------------------

_INDEX_RULE = (
    "index names are lower case letters, digits and . _ * - only, "
    "comma-separated; one leading - excludes a pattern; a name may not start "
    "with _ (except _all) and may not contain / ? # : .. or spaces."
)
_JQ_RULE = (
    "jq must be a non-empty expression of at most 2000 characters without "
    "import or include."
)
_STATE_NOTE = (
    "This tool has no time range: it shows the cluster's indices and mappings "
    "at the moment of the query, which can be after the authorized window."
)
_STATE_CANNOT_PROVE = (
    "This is the state at the moment of the query only: it cannot prove which "
    "indices, fields or document counts existed during the incident window, "
    "nor that any document lies in that window. A field that is mapped is not "
    "proof that documents carry it."
)
_SEARCH_CANNOT_PROVE = (
    "A document that is not returned is unknown, not proof that nothing was "
    "logged: only size documents are shown, and retention, ingest delay and "
    "the index pattern can leave gaps. The time field is the writer's "
    "timestamp, not necessarily when the event happened. total and "
    "aggregations cover only the matched indices inside the query window."
)
_UNTIMED_CANNOT_PROVE = (
    "These indices have no time field in their mapping, so no time filter was "
    "applied: the documents cannot be proven to belong to the incident window."
)
_ERRORS_TEXT = (
    "A missing index is INVALID_PARAMS with INDEX_NOT_FOUND; a query "
    "Elasticsearch cannot parse is INVALID_PARAMS with its error type and "
    "reason; a wildcard that matches no index is no data; HTTP 401/403 are "
    "SOURCE_FORBIDDEN; 408, 429 and 5xx are SOURCE_UNAVAILABLE; a call past "
    "its 30 s deadline is a timeout."
)
_TOO_LARGE_TEXT = (
    "A source answer over 2 MiB, or a view over 25,000 tokens, is refused "
    "whole with RESULT_TOO_LARGE (never truncated)."
)

_SEARCH_DESCRIPTION = (
    "Search documents (logs) in the registered Elasticsearch or OpenSearch "
    "with Query DSL, optionally with aggregations. "
    "Call elasticsearch_list_indices and elasticsearch_mappings first to find "
    "the index and field names. "
    "query, aggregations, sort and source are JSON text. "
    "Time: when the matched indices map the time field (@timestamp, or the "
    "date field you name in time_field), the tool always limits the search "
    "to the query window on that field and you cannot turn this off; "
    "start_time and end_time choose the query window inside the authorized "
    "window (omitted: the time policy's default_query_window when it states "
    "one, otherwise the 1 hour ending at the authorized window's end). "
    "Without sort the newest documents come first. "
    "When no matched index maps any date field there is no time filter and "
    "the result cannot be tied to the incident window. "
    "size is 0 to 500 (default 100), from_offset + size at most 10000. "
    "With size > 0, content is the documents {_index, _id, _source, sort}; "
    "with size 0, content is one summary {total, aggregations} (use it for "
    "counts and aggregations). The view adds total, aggregations, took, "
    "timed_out, shards, time_filter, notes and incomplete_reason. "
    "String values are cut to 2000 characters and listed in "
    "_truncated_fields. "
    "incomplete is true when more documents matched than are shown, the "
    "search timed out or shards failed. "
    "Any key with script as a _-separated part (script, bucket_script, "
    "_script ...), runtime_mappings, wrapper, pit, scroll, "
    "indexed_shape, the global, significant_terms, significant_text, children "
    "and parent aggregations, terms lookups, more_like_this documents with "
    "_index and percolate of a stored document are refused before sending "
    "(INVALID_PARAMS, QUERY_CONSTRUCT_NOT_ALLOWED). "
    + _TOO_LARGE_TEXT
    + " "
    + _ERRORS_TEXT
    + " A document that is not returned is unknown, not proof nothing was "
    "logged."
)
_MAPPINGS_DESCRIPTION = (
    "Get the field mappings (schema) of an index or index pattern in the "
    "registered Elasticsearch or OpenSearch: field names, data types and "
    "analyzers. Use it before writing a search to find the time field and "
    "the field names. "
    "content is one entry {index, mappings} per matched index; with jq, the "
    "jq outputs over the raw mapping answer {index: {mappings: ...}} (for "
    "example jq='.[].mappings.properties | keys' lists the top-level fields). "
    "There is no time range: this is the mapping when the query ran. "
    + _TOO_LARGE_TEXT
    + " "
    + _ERRORS_TEXT
)
_LIST_DESCRIPTION = (
    "List the indices of the registered Elasticsearch or OpenSearch that "
    "match a pattern, with document counts and storage size. "
    "content is one object per index with the chosen columns (values are "
    "strings); with jq, the jq outputs over that list. "
    "There is no time range: this is the list when the query ran, and a "
    "document count is not a count inside the incident window. "
    + _TOO_LARGE_TEXT
    + " "
    + _ERRORS_TEXT
)

_INDEX_TEXT = (
    "Index name or pattern, wildcards allowed (for example logs-*); "
    "comma-separated for several; a leading - excludes a pattern."
)
_JQ_TEXT = (
    "Optional jq expression applied to the answer (at most 2000 characters, "
    "no import or include)."
)

TOOL_SCHEMAS: tuple[Mapping[str, Any], ...] = (
    {
        "type": "function",
        "function": {
            "name": SEARCH_TOOL,
            "description": _SEARCH_DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {"type": "string", "description": _INDEX_TEXT},
                    "query": {
                        "type": "string",
                        "description": (
                            "Optional Query DSL query object as JSON text, for "
                            'example {"bool": {"must": [{"match": {"level": '
                            '"ERROR"}}]}}; omitted, every document in the '
                            "window. Use match for full text and term for "
                            "exact values. At most 8000 characters."
                        ),
                    },
                    "aggregations": {
                        "type": "string",
                        "description": (
                            "Optional aggregations as JSON text, for example "
                            '{"by_service": {"terms": {"field": '
                            '"service.keyword", "size": 10}}}; terms, '
                            "date_histogram, avg, sum, min, max and "
                            "cardinality are common. global, significant_terms, "
                            "significant_text, children and parent are refused."
                        ),
                    },
                    "sort": {
                        "type": "string",
                        "description": (
                            'Optional sort as JSON text, for example [{"'
                            '@timestamp": "asc"}]; omitted, newest first on '
                            "the time field."
                        ),
                    },
                    "source": {
                        "type": "string",
                        "description": (
                            "Optional _source filter as JSON text: an array of "
                            'fields ["@timestamp", "message", "level"], an '
                            'object {"includes": [...], "excludes": [...]}, '
                            "or false for metadata only. Selecting fields "
                            "keeps large results under the view limit."
                        ),
                    },
                    "size": {
                        "type": "integer",
                        "description": (
                            "Documents to return, 0 to 500 (default 100); 0 "
                            "returns only total and aggregations."
                        ),
                    },
                    "from_offset": {
                        "type": "integer",
                        "description": (
                            "Documents to skip for paging (default 0); "
                            "from_offset + size at most 10000."
                        ),
                    },
                    "profile": {
                        "type": "boolean",
                        "description": (
                            "Optional: return Elasticsearch's query profile "
                            "(time spent per query component)."
                        ),
                    },
                    "time_field": {
                        "type": "string",
                        "description": (
                            "Optional date field the time filter uses "
                            "(default @timestamp); it must be a date field in "
                            "the mapping. It cannot turn the filter off."
                        ),
                    },
                    "start_time": {
                        "type": "string",
                        "description": (
                            "Optional absolute RFC3339 start of the query "
                            "window, e.g. '2026-09-14T10:30:00Z'; give "
                            "start_time and end_time together or neither."
                        ),
                    },
                    "end_time": {
                        "type": "string",
                        "description": (
                            "Optional absolute RFC3339 end of the query "
                            "window, e.g. '2026-09-14T11:30:00Z'; give "
                            "start_time and end_time together or neither."
                        ),
                    },
                },
                "required": ["index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": MAPPINGS_TOOL,
            "description": _MAPPINGS_DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {"type": "string", "description": _INDEX_TEXT},
                    "jq": {"type": "string", "description": _JQ_TEXT},
                },
                "required": ["index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": LIST_INDICES_TOOL,
            "description": _LIST_DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": (
                            "Optional index name pattern, wildcards allowed "
                            "(for example logs-*); default * (all)."
                        ),
                    },
                    "sort": {
                        "type": "string",
                        "description": (
                            "Optional sort: column or column:desc, "
                            "comma-separated, for example docs.count:desc; "
                            "default index."
                        ),
                    },
                    "columns": {
                        "type": "string",
                        "description": (
                            "Optional comma-separated columns from: "
                            + ", ".join(CAT_COLUMNS)
                            + "; default "
                            + DEFAULT_COLUMNS
                            + "."
                        ),
                    },
                    "health": {
                        "type": "string",
                        "description": "Optional health filter: green, yellow or red.",
                    },
                    "bytes": {
                        "type": "string",
                        "description": (
                            "Optional unit for sizes: b, kb, mb, gb, tb or pb; "
                            "default human-readable."
                        ),
                    },
                    "pri": {
                        "type": "boolean",
                        "description": "Optional: primary shard statistics only.",
                    },
                    "expand_wildcards": {
                        "type": "string",
                        "description": (
                            "Optional, comma-separated: open, closed, hidden, "
                            "none or all; default open."
                        ),
                    },
                    "jq": {"type": "string", "description": _JQ_TEXT},
                },
                "required": [],
            },
        },
    },
)

_ERROR_CLASSES: dict[str, str] = {
    "400": "INVALID_PARAMS",
    "404": "INVALID_PARAMS",
    "INDEX_NOT_FOUND": "INVALID_PARAMS",
    "401": "SOURCE_FORBIDDEN",
    "403": "SOURCE_FORBIDDEN",
    "408": "SOURCE_UNAVAILABLE",
    "429": "SOURCE_UNAVAILABLE",
    "INVALID_INDEX": "INVALID_PARAMS",
    "INVALID_SIZE": "INVALID_PARAMS",
    "INVALID_FROM": "INVALID_PARAMS",
    "INVALID_TIME_FIELD": "INVALID_PARAMS",
    "INVALID_JSON": "INVALID_PARAMS",
    "QUERY_CONSTRUCT_NOT_ALLOWED": "INVALID_PARAMS",
    "AGGREGATION_NAME_RESERVED": "INVALID_PARAMS",
    "QUERY_OUT_OF_WINDOW": "INVALID_PARAMS",
    "TIME_FIELD_NOT_FOUND": "INVALID_PARAMS",
    "TIME_FIELD_REQUIRED": "INVALID_PARAMS",
    "INVALID_COLUMNS": "INVALID_PARAMS",
    "INVALID_SORT": "INVALID_PARAMS",
    "INVALID_HEALTH": "INVALID_PARAMS",
    "INVALID_BYTES": "INVALID_PARAMS",
    "INVALID_EXPAND_WILDCARDS": "INVALID_PARAMS",
    "INVALID_JQ": "INVALID_PARAMS",
    "JQ_COMPILE_ERROR": "INVALID_PARAMS",
    "JQ_RUNTIME_ERROR": "INVALID_PARAMS",
    "JQ_FAILED": "SOURCE_ERROR",
}

_LIMITS = (
    "One call reads at most 2 MiB from the backend and returns at most 2 MiB "
    "(RESULT_TOO_LARGE otherwise, whole call); the view is limited to 25,000 "
    "tokens and refused whole above it, never truncated. jq, when given, runs "
    "once in an isolated child (2000-character expression, no modules). "
    "30 s per call. " + _ERRORS_TEXT
)


def elasticsearch_registrations(
    *, max_window_seconds: int
) -> tuple[ToolRegistration, ...]:
    """The three registrations (B1-9: ``source="otel-demo"``, the lab
    target's; ``may_contain_secrets=True``; search is ``query``, the other
    two ``get``)."""
    common: dict[str, Any] = {
        "version": "v1",
        "source": otel_demo.SOURCE,
        "may_contain_secrets": True,
        "request_timeout_seconds": REQUEST_TIMEOUT_SECONDS,
        "max_result_bytes": READ_LIMIT_BYTES,
        "max_view_tokens": MAX_VIEW_TOKENS,
        "max_window_seconds": max_window_seconds,
        "error_classes": dict(_ERROR_CLASSES),
        "result_path": ("results",),
    }
    search = ToolRegistration(
        name=SEARCH_TOOL,
        verb="query",
        description=ToolDescription(
            returns=(
                "Documents from the registered Elasticsearch or OpenSearch "
                "matching a Query DSL search, projected to _index, _id, "
                "_source (or fields) and sort, under results; with size 0, one "
                "summary of total and aggregations. The view adds total, "
                "aggregations, took, timed_out, shards, time_filter, "
                "long_values_cut, notes and incomplete_reason."
            ),
            window_format=(
                "The authorized absolute window is appended as {window}. When "
                "the matched indices map the time field the tool always "
                "filters it to the query window (start_time/end_time inside "
                "the authorized window, default the last hour or the alert's "
                "default window); source_start_at/source_end_at are the "
                "earliest and latest time-field values of the matched "
                "documents. Without any mapped date field there is no filter "
                "and no source time."
            ),
            values_format=(
                "The authorized target is appended as {values}. index follows "
                "the index name rule; size 0 to 500; from_offset + size at "
                "most 10000; time_field must be a mapped date field."
            ),
            limits=(
                "At most 500 documents; string values cut to 2000 characters "
                "and marked. " + _LIMITS
            ),
            cannot_prove=_SEARCH_CANNOT_PROVE + " " + _UNTIMED_CANNOT_PROVE,
        ),
        parameters={
            "index": ParameterSpec("string", required=True, description=_INDEX_TEXT),
            "query": ParameterSpec("string", description="Query DSL query as JSON."),
            "aggregations": ParameterSpec(
                "string", description="Aggregations as JSON."
            ),
            "sort": ParameterSpec("string", description="Sort as JSON."),
            "source": ParameterSpec("string", description="_source filter as JSON."),
            "size": ParameterSpec("integer", description="Documents, 0 to 500."),
            "from_offset": ParameterSpec("integer", description="Documents to skip."),
            "profile": ParameterSpec("boolean", description="Return the profile."),
            "time_field": ParameterSpec(
                "string", description="Mapped date field for the time filter."
            ),
            "start_time": ParameterSpec(
                "string", description="Absolute RFC3339 start; both or neither."
            ),
            "end_time": ParameterSpec(
                "string", description="Absolute RFC3339 end; both or neither."
            ),
        },
        **common,
    )
    state_window = (
        "The authorized absolute window is appended as {window}. There is no "
        "time range: the answer is the state when the query ran and carries "
        "no source time."
    )
    mappings = ToolRegistration(
        name=MAPPINGS_TOOL,
        verb="get",
        description=ToolDescription(
            returns=(
                "The field mappings of each index matching the index "
                "expression, one {index, mappings} entry each under results, "
                "or the jq outputs over the raw mapping answer."
            ),
            window_format=state_window,
            values_format=(
                "The authorized target is appended as {values}. index follows "
                "the index name rule."
            ),
            limits=_LIMITS,
            cannot_prove=_STATE_CANNOT_PROVE,
        ),
        parameters={
            "index": ParameterSpec("string", required=True, description=_INDEX_TEXT),
            "jq": ParameterSpec("string", description="Optional jq expression."),
        },
        **common,
    )
    list_indices = ToolRegistration(
        name=LIST_INDICES_TOOL,
        verb="get",
        description=ToolDescription(
            returns=(
                "The _cat/indices rows of the indices matching the pattern "
                "with the chosen columns (string values) under results, or the "
                "jq outputs over them."
            ),
            window_format=state_window,
            values_format=(
                "The authorized target is appended as {values}. columns and "
                "sort columns: "
                + ", ".join(CAT_COLUMNS)
                + "; health: green, yellow, red; bytes: b, kb, mb, gb, tb, pb; "
                "expand_wildcards: open, closed, hidden, none, all; other "
                "values are INVALID_PARAMS."
            ),
            limits=_LIMITS,
            cannot_prove=_STATE_CANNOT_PROVE,
        ),
        parameters={
            "pattern": ParameterSpec("string", description="Index pattern."),
            "sort": ParameterSpec("string", description="Sort columns."),
            "columns": ParameterSpec("string", description="Columns to return."),
            "health": ParameterSpec("string", description="Health filter."),
            "bytes": ParameterSpec("string", description="Size unit."),
            "pri": ParameterSpec("boolean", description="Primary shards only."),
            "expand_wildcards": ParameterSpec(
                "string", description="Which indices wildcards expand to."
            ),
            "jq": ParameterSpec("string", description="Optional jq expression."),
        },
        **common,
    )
    return search, mappings, list_indices
