"""Pod logs: ``fetch_pod_logs`` (tool expansion A3, #555).

The upstream HolmesGPT ``fetch_pod_logs`` tool (``kubernetes_logs.py`` and
``logging_utils/logging_api.py`` at ``a0009195``) with the same name and
parameters, served by direct GETs through the A2 client
(:class:`opspilot.tools.kubernetes_core.KubernetesClient`) instead of a
command-line client. Decision table r3 A3-1 to A3-11:

* ``start_time``/``end_time`` are absolute RFC3339 only, checked by the
  same rule as ``traces_search``'s ``start``/``end`` (A3-1): both or neither;
  neither is the alert Run's default query window, else the hour ending at
  the authorized window's end; outside the authorized window is refused
  with it echoed.
* The Pod is read first for its container names; then every container's
  current and ``previous`` log stream, once each -- 1 + 2C GETs charged as
  one operation (A3-2). All streams share one 4 MiB read budget; a stream
  cut at it keeps what it read, the result is incomplete and states the
  moment read through (A3-3). Lines after the window's end (and before its
  start: ``sinceTime`` has second precision) are dropped here.
* A stream answered 400 (no previous instance, a container not started) is
  no data plus a note; 401/403 is ``SOURCE_FORBIDDEN``; 429/5xx
  ``SOURCE_UNAVAILABLE`` (A3-4). A missing Pod is ``INVALID_PARAMS`` with
  the fixed code ``POD_NOT_FOUND`` (A3-5). Names are checked before
  anything is sent (A3-6).
* ``filter``/``exclude_filter`` run after the window filter, in the bounded
  Python ``re`` child of ``kubernetes_core.filter_lines``, case-insensitive;
  a pattern that does not compile falls back to a substring match (A3-7).
* ``limit`` defaults to 100, 1 to 1000; the newest lines are kept and the
  view counts ``lines`` (A3-8). Every line carries its timestamp, container,
  instance and text; a line without a timestamp is dropped and counted
  (A3-9); text is cut to 2000 characters and marked (A3-10).
"""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from opspilot.tools import otel_demo
from opspilot.tools.executor import (
    TransportError,
    TransportRequest,
    TransportResponse,
    TransportResultTooLarge,
)
from opspilot.tools.kubernetes_core import (
    DEFAULT_NAMESPACE,
    ENDPOINT_SELECTOR,
    ERROR_CLASSES,
    MAX_PATTERN_CHARS,
    MAX_SOURCE_READ_BYTES,
    KubernetesClient,
    check_namespace,
    check_object_name,
    filter_lines,
    pattern_problem,
    pod_url,
    refused,
    require_python,
)
from opspilot.tools.kubernetes_core import _monotonic as _monotonic
from opspilot.tools.outcomes import Window
from opspilot.tools.registry import (
    MAX_VIEW_TOKENS,
    ParameterSpec,
    ToolDescription,
    ToolRegistration,
    canonical,
)

__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "MAX_LINE_CHARS",
    "POD_LOGS_TOOL",
    "TOOL_NAMES",
    "TOOL_SCHEMAS",
    "PodLogsTransport",
    "pod_logs_registrations",
]

POD_LOGS_TOOL = "fetch_pod_logs"
TOOL_NAMES: tuple[str, ...] = (POD_LOGS_TOOL,)

#: R8 / A3-8.
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
#: A3-10.
MAX_LINE_CHARS = 2000
#: A3-3: shared by every stream of one call (the A2 cumulative cap).
READ_LIMIT_BYTES = MAX_SOURCE_READ_BYTES
REQUEST_TIMEOUT_SECONDS = 30.0
MAX_RESULT_BYTES = 2 * 1024 * 1024
#: A 400 Status message quoted into a note is cut this far.
_NOTE_MESSAGE_CHARS = 300

#: RFC3339 as the API server's ``timestamps=true`` prefix writes it
#: (``2026-09-14T00:10:00.123456789Z``), then one space, then the text.
_TIMESTAMP = re.compile(
    r"(?P<ts>(?P<base>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<frac>\d+))?(?P<tz>Z|[+-]\d{2}:\d{2}))(?: |$)"
)

PREVIOUS = "previous"
CURRENT = "current"


# -- parsing -------------------------------------------------------------------


@dataclass(frozen=True)
class _Line:
    moment: datetime
    nanos: int
    stamp: str
    stream: int
    index: int
    container: str
    instance: str
    text: str

    def key(self) -> tuple[datetime, int, int, int]:
        return (self.moment, self.nanos, self.stream, self.index)


def _parse_stamp(match: re.Match[str]) -> tuple[datetime, int] | None:
    frac = match.group("frac") or ""
    tz = match.group("tz")
    text = match.group("base") + ("+00:00" if tz == "Z" else tz)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    nanos = int((frac + "000000000")[:9])
    moment = moment.replace(microsecond=nanos // 1000).astimezone(timezone.utc)
    return moment, nanos


def _split(body: bytes, *, cut: bool) -> tuple[list[str], bool]:
    """Physical lines of one stream; a stream cut mid-line loses its last,
    partial line (``True`` in the second place when it did)."""
    text = body.decode("utf-8", errors="replace")
    pieces = text.split("\n")
    partial = False
    if cut and pieces and pieces[-1]:
        pieces.pop()
        partial = True
    if pieces and not pieces[-1]:
        pieces.pop()
    return pieces, partial


def _status_message(body: bytes) -> str | None:
    """The ``message`` of a Kubernetes ``Status`` error body, bounded."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    message = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(message, str) or not message.isprintable():
        return None
    return message[:_NOTE_MESSAGE_CHARS]


def _containers(body: bytes) -> list[str] | None:
    """Every container name of a Pod object: init, app, then ephemeral
    containers, the order the upstream all-containers read visits them."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    spec = payload.get("spec") if isinstance(payload, dict) else None
    if not isinstance(spec, dict):
        return None
    names: list[str] = []
    for field in ("initContainers", "containers", "ephemeralContainers"):
        entries = spec.get(field)
        if entries is None:
            continue
        if not isinstance(entries, list):
            return None
        for entry in entries:
            name = entry.get("name") if isinstance(entry, dict) else None
            if not isinstance(name, str) or not name or not name.isprintable():
                return None
            if name not in names:
                names.append(name)
    return names


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _since(moment: datetime) -> str:
    """``sinceTime`` at second precision, floored (never after the start)."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- transport -------------------------------------------------------------------


def _window(
    request: TransportRequest,
) -> tuple[Window | None, TransportResponse | None]:
    """A3-1: ``start_time``/``end_time`` by ``traces_search``'s rule; every
    refusal states the authorized window."""
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
    if refusal is not None:
        return None, otel_demo._refused("QUERY_OUT_OF_WINDOW", frame=request.window)
    return window, None


def _optional_pattern(
    params: Mapping[str, object], name: str
) -> tuple[bool, str | None]:
    value = params.get(name)
    if value is None or value == "":
        return True, None
    if pattern_problem(value):
        return False, None
    assert isinstance(value, str)
    return True, value


def _pod_not_found(namespace: str, pod_name: str) -> TransportResponse:
    return TransportResponse(
        body=canonical(
            {"error": "POD_NOT_FOUND", "namespace": namespace, "pod_name": pod_name}
        ).encode(),
        source_status="POD_NOT_FOUND",
        http_status=404,
        source_error_type="pod_not_found",
        source_error_text=(
            f"POD_NOT_FOUND: no pod {pod_name} in namespace {namespace}; list "
            "the pods with kubernetes_jq_query (kind=pods) and retry with an "
            "exact name."
        ),
    )


def _source_error(status: int, body: bytes) -> TransportResponse:
    return TransportResponse(body=body, source_status=str(status), http_status=status)


class PodLogsTransport:
    """Serves ``fetch_pod_logs`` over the A2 client (GET only, ``k8s-ro``)."""

    def __init__(
        self,
        *,
        credentials: Mapping[str, str | None] | None = None,
        client: KubernetesClient | None = None,
        python_path: str | None = None,
        opener: Any = None,
        ca_file: str | None = None,
        application_namespace: str = DEFAULT_NAMESPACE,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if client is None:
            client = KubernetesClient(
                credentials=credentials or {}, opener=opener, ca_file=ca_file
            )
        self._client = client
        # ``None``: resolved from PATH on first filtered call (the worker
        # resolves it at startup and passes it in, A3-7).
        self._python_path = python_path
        self._application_namespace = application_namespace
        self._monotonic = monotonic or _monotonic

    def fetch(self, request: TransportRequest) -> TransportResponse:
        if request.tool != POD_LOGS_TOOL:
            raise TransportError("TOOL_NOT_SUPPORTED")
        deadline = self._monotonic() + request.timeout_seconds
        params = request.params
        pod_name = params.get("pod_name")
        if not check_object_name(pod_name):
            return refused("INVALID_POD_NAME")
        namespace = params.get("namespace")
        if not check_namespace(namespace):
            return refused("INVALID_NAMESPACE")
        assert isinstance(pod_name, str) and isinstance(namespace, str)
        limit = params.get("limit", DEFAULT_LIMIT)
        if limit is None:
            limit = DEFAULT_LIMIT
        if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
            return refused("INVALID_LIMIT")
        ok_include, include = _optional_pattern(params, "filter")
        ok_exclude, exclude = _optional_pattern(params, "exclude_filter")
        if not (ok_include and ok_exclude):
            return refused("INVALID_FILTER")
        window, refusal = _window(request)
        if refusal is not None:
            return refusal
        assert window is not None
        self._client.token()  # a missing slot fails before anything is sent
        endpoint = request.selector.get(ENDPOINT_SELECTOR, "")
        if not endpoint:
            raise TransportError("K8S_ENDPOINT_MISSING")
        if (include or exclude) and self._python_path is None:
            self._python_path = require_python()
        return _Read(
            client=self._client,
            endpoint=endpoint,
            namespace=namespace,
            pod_name=pod_name,
            window=window,
            deadline=deadline,
        ).run(
            request,
            limit=limit,
            include=include,
            exclude=exclude,
            python_path=self._python_path,
            application_namespace=self._application_namespace,
        )


@dataclass
class _Stream:
    container: str
    instance: str
    status: str = "not_read"
    bytes_read: int = 0
    lines_read: int = 0
    cut: bool = False
    last_stamp: datetime | None = None
    message: str | None = None

    def as_json(self) -> dict[str, object]:
        return {
            "container": self.container,
            "instance": self.instance,
            "status": self.status,
            "bytes_read": self.bytes_read,
            "lines_read": self.lines_read,
            "read_limit_reached": self.cut,
            "read_through": None if self.last_stamp is None else _iso(self.last_stamp),
        }


@dataclass
class _Read:
    client: KubernetesClient
    endpoint: str
    namespace: str
    pod_name: str
    window: Window
    deadline: float

    def run(
        self,
        request: TransportRequest,
        *,
        limit: int,
        include: str | None,
        exclude: str | None,
        python_path: str | None,
        application_namespace: str,
    ) -> TransportResponse:
        pod = self.client.get(
            pod_url(self.endpoint, self.namespace, self.pod_name),
            deadline=self.deadline,
            max_bytes=READ_LIMIT_BYTES,
        )
        if pod.status == 404:
            return _pod_not_found(self.namespace, self.pod_name)
        if pod.status != 200:
            return _source_error(pod.status, pod.body)
        containers = _containers(pod.body)
        if containers is None:
            return TransportResponse(body=b"")  # MALFORMED_RESULT
        notes: list[str] = []
        # The previous instances first: in a crash loop they hold the crash,
        # and they are bounded by one instance's life.
        streams = [_Stream(c, PREVIOUS) for c in containers] + [
            _Stream(c, CURRENT) for c in containers
        ]
        lines: list[_Line] = []
        untimed = partial_lines = 0
        used = 0
        for number, stream in enumerate(streams):
            budget = READ_LIMIT_BYTES - used
            if budget <= 0:
                continue  # stays "not_read"
            query = [
                ("container", stream.container),
                ("timestamps", "true"),
                ("sinceTime", _since(self.window.start)),
                ("limitBytes", str(budget)),
            ]
            if stream.instance == PREVIOUS:
                query.append(("previous", "true"))
            fetched = self.client.get(
                pod_url(self.endpoint, self.namespace, self.pod_name, log=True)
                + "?"
                + urllib.parse.urlencode(query),
                deadline=self.deadline,
                max_bytes=budget,
                accept="*/*",
                cut=True,
            )
            if fetched.status == 400:
                stream.status = "unavailable"
                stream.message = _status_message(fetched.body)
                continue
            if fetched.status == 404:
                return _pod_not_found(self.namespace, self.pod_name)
            if fetched.status != 200:
                return _source_error(fetched.status, fetched.body)
            used += len(fetched.body)
            stream.bytes_read = len(fetched.body)
            stream.status = "read"
            stream.cut = fetched.cut or len(fetched.body) >= budget
            texts, partial = _split(fetched.body, cut=stream.cut)
            partial_lines += int(partial)
            for index, raw in enumerate(texts):
                match = _TIMESTAMP.match(raw)
                parsed = None if match is None else _parse_stamp(match)
                if match is None or parsed is None:
                    untimed += 1
                    continue
                moment, nanos = parsed
                body_text = raw[match.end() :]
                stream.lines_read += 1
                stream.last_stamp = moment
                lines.append(
                    _Line(
                        moment=moment,
                        nanos=nanos,
                        stamp=match.group("ts"),
                        stream=number,
                        index=index,
                        container=stream.container,
                        instance=stream.instance,
                        text=body_text,
                    )
                )
        lines_read = len(lines)
        before = [line for line in lines if line.moment < self.window.start]
        after = [line for line in lines if line.moment > self.window.end]
        lines = [
            line
            for line in lines
            if self.window.start <= line.moment <= self.window.end
        ]
        modes: dict[str, str | None] = {"filter": None, "exclude_filter": None}
        removed_include = removed_exclude = 0
        if lines and (include is not None or exclude is not None):
            assert python_path is not None
            decided = filter_lines(
                [line.text for line in lines],
                include=include,
                exclude=exclude,
                ignore_case=True,
                python_path=python_path,
                deadline=self.deadline,
            )
            if decided is None:
                return TransportResponse(
                    body=canonical({"error": "FILTER_FAILED"}).encode(),
                    source_status="FILTER_FAILED",
                )
            lines = [line for line, kept in zip(lines, decided.keep) if kept]
            modes = {
                "filter": decided.include_mode,
                "exclude_filter": decided.exclude_mode,
            }
            removed_include = decided.removed_include
            removed_exclude = decided.removed_exclude
        elif include is not None or exclude is not None:
            modes = {
                "filter": None if include is None else "not_run",
                "exclude_filter": None if exclude is None else "not_run",
            }
        lines.sort(key=_Line.key)
        matched = len(lines)
        shown = lines[-limit:] if matched > limit else lines
        rows: list[dict[str, object]] = []
        long_lines = 0
        for line in shown:
            text_truncated = len(line.text) > MAX_LINE_CHARS
            long_lines += int(text_truncated)
            rows.append(
                {
                    "timestamp": line.stamp,
                    "container": line.container,
                    "previous": line.instance == PREVIOUS,
                    "text": line.text[:MAX_LINE_CHARS],
                    "text_truncated": text_truncated,
                }
            )

        # -- notes and completeness ---------------------------------------
        for stream in streams:
            if stream.status == "unavailable":
                what = (
                    "has no previous instance"
                    if stream.instance == PREVIOUS
                    else "has no current log (not started)"
                )
                detail = f": {stream.message}" if stream.message else ""
                notes.append(
                    f"Container {stream.container} {what} (HTTP 400{detail}); "
                    "the other streams are unaffected."
                )
        cut = [s for s in streams if s.cut]
        unread = [s for s in streams if s.status == "not_read"]
        read_through: datetime | None = None
        if cut or unread:
            stamps = [s.last_stamp for s in cut if s.last_stamp is not None]
            read_through = min(stamps) if stamps and not unread else None
            names = ", ".join(f"{s.container}/{s.instance}" for s in cut + unread)
            where = (
                f"read through {_iso(read_through)}; move start_time later, to "
                "about that moment, to read the lines after it"
                if read_through is not None
                else "move start_time later to read the lines after what was read"
            )
            notes.append(
                f"The {READ_LIMIT_BYTES // (1024 * 1024)} MiB read limit shared "
                f"by all streams was reached ({names}): lines after the cut are "
                f"not read and unknown; {where}. filter and exclude_filter do "
                "not reduce what is read."
            )
        if untimed:
            notes.append(
                f"{untimed} lines without a timestamp were dropped (they cannot "
                "be shown to lie inside the window)."
            )
        if partial_lines:
            notes.append(
                f"{partial_lines} partial lines cut at the read limit were dropped."
            )
        if before or after:
            notes.append(
                f"{len(before) + len(after)} lines outside the query window "
                f"were dropped ({len(before)} before its start, {len(after)} "
                "after its end)."
            )
        for name, mode in modes.items():
            if mode == "substring":
                notes.append(
                    f"{name} is not a valid regular expression; it was matched "
                    "as a case-insensitive substring."
                )
        if long_lines:
            notes.append(
                f"{long_lines} shown lines were longer than {MAX_LINE_CHARS} "
                "characters and are cut there (text_truncated)."
            )
        reasons: list[str] = []
        if matched > limit:
            reasons.append(
                f"limit: the newest {limit} of {matched} matching lines are shown"
            )
        if cut or unread:
            reasons.append("read_limit: the 4 MiB read limit was reached")
        incomplete = bool(reasons)

        body = canonical(
            {"pod_name": self.pod_name, "namespace": self.namespace, "lines": rows}
        ).encode("utf-8")
        if len(body) > request.max_result_bytes:
            raise TransportResultTooLarge(str(request.max_result_bytes))
        start_at = shown[0].moment if shown else None
        end_at = shown[-1].moment if shown else None
        return TransportResponse(
            body=body,
            source_start_at=start_at,
            source_end_at=end_at,
            query_window=self.window,
            row_unit="lines",
            backend_rows_returned=matched,
            incomplete=incomplete,
            view_fields={
                "pod": {
                    "namespace": self.namespace,
                    "name": self.pod_name,
                    "application_namespace": application_namespace,
                },
                "containers": list(containers),
                "streams": [stream.as_json() for stream in streams],
                "lines_requested": limit,
                "lines_read": lines_read,
                "lines_without_timestamp": untimed,
                "lines_before_window": len(before),
                "lines_after_window": len(after),
                "lines_removed_by_filter": removed_include,
                "lines_removed_by_exclude_filter": removed_exclude,
                "long_lines_cut": long_lines,
                "filter_mode": modes,
                "bytes_read": used,
                "read_limit_bytes": READ_LIMIT_BYTES,
                "read_limit_reached": bool(cut or unread),
                "read_through": None if read_through is None else _iso(read_through),
                "incomplete_reason": "; ".join(reasons) or None,
                "notes": notes,
                "cannot_prove": _CANNOT_PROVE,
            },
        )


# -- model-visible face and registration ---------------------------------------

_CANNOT_PROVE = (
    "Only what the containers wrote to standard output and standard error "
    "and the node still keeps: the current instance and the one before it, "
    "nothing older and nothing from a pod that no longer exists. No matching "
    "line is not proof that something did not happen; lines cut by limit or "
    "by the read limit, and lines not read, are unknown, not absent."
)

_DESCRIPTION = (
    "Fetch the logs of every container of one pod, the current instance and "
    "the previous (crashed or restarted) one, merged in time order; returns "
    "the newest lines, each with timestamp, container and previous (true "
    "for the previous instance). Use "
    "filter to find errors (e.g. filter='err|error|fatal|fail|exception|panic') "
    "and exclude_filter to cut noise (e.g. exclude_filter='health|metrics|ping'). "
    "start_time and end_time are absolute RFC3339 times inside the "
    "authorized window, both or neither; relative seconds are refused. "
    "Neither: the alert Run's default query window, else the hour ending at "
    "the authorized window's end -- narrower than the upstream 7-day default. "
    f"limit defaults to {DEFAULT_LIMIT}, at most {MAX_LIMIT}. A pod that does "
    "not exist is INVALID_PARAMS (POD_NOT_FOUND): list pods with "
    "kubernetes_jq_query (kind=pods) to find the exact name. HTTP 401/403 is "
    "SOURCE_FORBIDDEN. Logs cannot prove an absence: only what the "
    "containers wrote to standard output and the node still keeps is read, "
    "and lines cut by limit or the 4 MiB read limit are unknown."
)

TOOL_SCHEMAS: tuple[Mapping[str, Any], ...] = (
    {
        "type": "function",
        "function": {
            "name": POD_LOGS_TOOL,
            "description": _DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "pod_name": {
                        "type": "string",
                        "description": "The exact Kubernetes pod name.",
                    },
                    "namespace": {
                        "type": "string",
                        "description": "Kubernetes namespace of the pod.",
                    },
                    "start_time": {
                        "type": "string",
                        "description": (
                            "RFC3339 start, e.g. '2026-09-14T10:30:00Z'; give "
                            "both start_time and end_time or neither."
                        ),
                    },
                    "end_time": {
                        "type": "string",
                        "description": (
                            "RFC3339 end, e.g. '2026-09-14T11:30:00Z'; give "
                            "both start_time and end_time or neither."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            f"Maximum number of lines to return, 1 to "
                            f"{MAX_LIMIT}. Default: {DEFAULT_LIMIT}."
                        ),
                    },
                    "filter": {
                        "type": "string",
                        "description": (
                            "Optional keyword/phrase or case-insensitive regex "
                            "to match, e.g. filter='err|error|fatal|fail|"
                            "exception|panic' for errors. If no results, "
                            "broaden the pattern or drop the filter."
                        ),
                    },
                    "exclude_filter": {
                        "type": "string",
                        "description": (
                            "Optional keyword or case-insensitive regex to "
                            "exclude, e.g. exclude_filter='health|metrics|ping' "
                            "to cut noise when hitting the line limit."
                        ),
                    },
                },
                "required": ["pod_name", "namespace"],
            },
        },
    },
)

_RETURNS = (
    "Log lines of every container of one pod, read from the cluster's API "
    "server: the current and the previous instance of each container, "
    "merged in time order, the newest lines kept, under lines; each line has "
    "timestamp (as the API server wrote it), container, previous (true for "
    "the previous instance), text and text_truncated. The view adds backend_lines_returned "
    "(lines matching the window and filters), lines_shown, lines_omitted, "
    "lines_requested, containers, streams (each stream's status: read, "
    "unavailable or not_read, bytes and lines read, read_limit_reached, "
    "read_through), lines_read, lines_without_timestamp, lines_before_window, "
    "lines_after_window, lines_removed_by_filter, "
    "lines_removed_by_exclude_filter, long_lines_cut, filter_mode, bytes_read, "
    "read_limit_bytes, read_limit_reached, read_through, incomplete_reason "
    "and notes."
)
_WINDOW_FORMAT = (
    "The authorized absolute window is appended as {window}. start_time and "
    "end_time pick a query window inside it, as absolute RFC3339 times, both "
    "or neither; relative times are refused. Omitted, the query window is the "
    "time policy's default_query_window when it states one, otherwise the 1 "
    "hour ending at the authorized window's end (narrower than the upstream "
    "7-day default). A chosen window outside the authorized window, or only "
    "one of the two, is refused with the authorized window stated. Lines "
    "are read from the query window's start; lines whose own timestamp is "
    "outside the query window are dropped and counted."
)
_VALUES_FORMAT = (
    "The authorized target is appended as {values}. namespace is a DNS label "
    "and pod_name a DNS subdomain name, both required and checked before "
    "anything is sent; the containers are the pod's own (init, app and "
    f"ephemeral). filter and exclude_filter are at most {MAX_PATTERN_CHARS} "
    "characters."
)
_LIMITS = (
    f"limit defaults to {DEFAULT_LIMIT}, at most {MAX_LIMIT}; past it the "
    "newest lines are kept, incomplete is true and lines_omitted counts the "
    "rest. Every stream of the call shares one 4 MiB read limit: when it is "
    "reached, what was read is kept, incomplete is true and read_through "
    "states the moment read to -- move start_time later to read further. "
    "filter and exclude_filter run after the window filter in a separate "
    "process with Python re, case-insensitive, matching the line text only; "
    "a pattern that does not compile is matched as a substring (noted); a "
    "pattern still running at the end of the call's 30 s is stopped and the "
    "call is timeout/TOOL_TIMEOUT. Lines without a timestamp are dropped and "
    f"counted; text longer than {MAX_LINE_CHARS} characters is cut "
    "(text_truncated). A missing previous instance or a container not yet "
    "started is a note, not an error. The whole view is limited to the "
    "registered max_view_tokens (25,000 DeepSeek tokens): a larger result "
    "is refused with RESULT_TOO_LARGE, never truncated, so prefer the "
    "default limit and filters over a large limit. A missing pod is "
    "INVALID_PARAMS (POD_NOT_FOUND); HTTP 401/403 is denied/SOURCE_FORBIDDEN, "
    "429 and 5xx SOURCE_UNAVAILABLE."
)

_ERROR_CLASSES: Mapping[str, str] = {
    **ERROR_CLASSES,
    "POD_NOT_FOUND": "INVALID_PARAMS",
    "INVALID_POD_NAME": "INVALID_PARAMS",
    "INVALID_LIMIT": "INVALID_PARAMS",
    "INVALID_FILTER": "INVALID_PARAMS",
    "QUERY_OUT_OF_WINDOW": "INVALID_PARAMS",
    "FILTER_FAILED": "SOURCE_ERROR",
}


def pod_logs_registrations(*, max_window_seconds: int) -> tuple[ToolRegistration, ...]:
    """The one registration (A3-11: verb ``logs``, ``source="otel-demo"``,
    ``may_contain_secrets=True``)."""
    return (
        ToolRegistration(
            name=POD_LOGS_TOOL,
            version="v1",
            source=otel_demo.SOURCE,
            verb="logs",
            may_contain_secrets=True,
            description=ToolDescription(
                returns=_RETURNS,
                window_format=_WINDOW_FORMAT,
                values_format=_VALUES_FORMAT,
                limits=_LIMITS,
                cannot_prove=_CANNOT_PROVE,
            ),
            parameters={
                "pod_name": ParameterSpec(
                    "string", required=True, description="The exact pod name."
                ),
                "namespace": ParameterSpec(
                    "string", required=True, description="Namespace of the pod."
                ),
                "start_time": ParameterSpec(
                    "string",
                    description="Absolute RFC3339 start; both or neither.",
                ),
                "end_time": ParameterSpec(
                    "string",
                    description="Absolute RFC3339 end; both or neither.",
                ),
                "limit": ParameterSpec(
                    "integer",
                    description=f"Lines to return, 1 to {MAX_LIMIT}.",
                ),
                "filter": ParameterSpec(
                    "string", description="Case-insensitive regex to keep lines."
                ),
                "exclude_filter": ParameterSpec(
                    "string", description="Case-insensitive regex to drop lines."
                ),
            },
            result_path=("lines",),
            request_timeout_seconds=REQUEST_TIMEOUT_SECONDS,
            max_result_bytes=MAX_RESULT_BYTES,
            max_view_tokens=MAX_VIEW_TOKENS,
            max_window_seconds=max_window_seconds,
            error_classes=dict(_ERROR_CLASSES),
        ),
    )


def _registrations() -> tuple[ToolRegistration, ...]:
    """The registration under the profiles' 24 h frame (inventory seam)."""
    return pod_logs_registrations(max_window_seconds=otel_demo.OBSERVATION_SECONDS)
