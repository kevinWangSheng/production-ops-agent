"""Prometheus instant query, series and label-name discovery (tool expansion A5).

Three upstream HolmesGPT Prometheus tools (``prometheus/prometheus.py`` at
``a0009195``: ``execute_prometheus_instant_query``, ``get_series``,
``get_all_labels``), served only by the ``otel-demo-ext`` profile
(docs/tasks/2026-11-12-tool-expansion-a5.md, decision table r3 A5-1 to A5-8):

* ``execute_prometheus_instant_query(query, time?)`` evaluates PromQL at one
  instant. The instant is always sent explicitly (A5-2): omitted, it is the
  query window's end (an alert Run's ``default_query_window`` end, otherwise
  the authorized frame's end), never "now", which lies after the frame. A
  given ``time`` must be an absolute ISO-8601 instant inside the frame, and
  the expression's lookback (range selectors and subqueries, as
  ``metrics_range_query`` counts them) must not reach before the frame's
  start; ``offset`` and ``@`` are refused as in the range query. No
  ``limit`` is sent (A5-3): a view over 25,000 tokens is refused as a whole
  with the range query's series summary and ``topk`` suggestion.
* ``get_series(match, start?, end?)`` and ``get_all_labels(match?, start?,
  end?)`` call ``/api/v1/series`` and ``/api/v1/labels`` with a fixed
  ``limit=100`` (A5-4) inside a query window chosen by ``_query_window``
  (A5-5: omitted, 1 hour). A result is incomplete when Prometheus sent
  warnings or exactly 100 entries came back (whether v3.5 warns on that
  truncation is unconfirmed, so both count). ``match`` follows ``_match_ok``
  (A5-6): required for series, optional for labels.

Errors are classified as for the ``otel-demo`` discovery tools (A5-8): 400/422
are ``INVALID_PARAMS`` with Prometheus's own error echoed, 503 with
``errorType=timeout`` is a confirmed timeout, other 5xx ``SOURCE_UNAVAILABLE``.

Transport (A5-7): the requests go through the profile's one
``OtelDemoTransport`` -- the same GET-only, no-proxy, no-redirect, bounded
reader and the same basic-auth credential -- built with these three names
added to its ``prometheus_tools``. ``otel_demo.PROMETHEUS_TOOLS`` is not
changed: that set is part of ``otel-demo``'s version hash.
"""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from opspilot.tools import otel_demo
from opspilot.tools.executor import TransportError, TransportRequest, TransportResponse
from opspilot.tools.outcomes import Window
from opspilot.tools.registry import ParameterSpec, ToolDescription, ToolRegistration

__all__ = [
    "INSTANT_QUERY_TOOL",
    "LABELS_TOOL",
    "LIST_INCOMPLETE_REASON",
    "LIST_LIMIT",
    "PROMETHEUS_EXT_TOOLS",
    "SERIES_TOOL",
    "TOOL_NAMES",
    "TOOL_SCHEMAS",
    "PrometheusExtTransport",
    "overflow_summary",
    "prometheus_ext_registrations",
]

INSTANT_QUERY_TOOL = "execute_prometheus_instant_query"
SERIES_TOOL = "get_series"
LABELS_TOOL = "get_all_labels"
TOOL_NAMES: tuple[str, ...] = (INSTANT_QUERY_TOOL, SERIES_TOOL, LABELS_TOOL)
PROMETHEUS_EXT_TOOLS: frozenset[str] = frozenset(TOOL_NAMES)

#: A5-4: the fixed ``limit`` of every series and label-name request
#: (upstream ``PROMETHEUS_METADATA_API_LIMIT``, the discovery tools' value).
LIST_LIMIT = otel_demo.DISCOVERY_LIMIT
LIST_INCOMPLETE_REASON = (
    "Prometheus returned warnings with this result or exactly the 100-entry "
    "limit, so more entries may exist; use a more specific match selector "
    "or a narrower window to narrow the result."
)
#: A5-1: fixed, not a model parameter.
REQUEST_TIMEOUT_SECONDS = 30.0

_WINDOW_PARAMETER_TEXT = {
    "start": (
        "Absolute ISO-8601 start of the query window. Give both start and "
        "end, or neither. Must lie inside the authorized window; a query "
        "window outside it is refused and the refusal states the authorized "
        "window."
    ),
    "end": (
        "Absolute ISO-8601 end of the query window. Give both start and end, "
        "or neither. Omitted with start also omitted, the query window is "
        "the time policy's default_query_window when it states one, "
        "otherwise the 1 hour ending at the authorized window's end."
    ),
}
_LIST_DESCRIPTION_TAIL = (
    "At most 100 entries are returned (a fixed limit you cannot change). "
    "When exactly 100 come back, or Prometheus returned any warning, "
    "incomplete is true and incomplete_reason says to narrow the match "
    "selector or the window. "
    "A result whose whole view exceeds 25,000 tokens is refused with "
    "RESULT_TOO_LARGE and nothing is returned. "
    "A listed entry does not prove the series has samples in the query "
    "window, nor anything about values or the service's health; an entry "
    "that is not listed is unknown, not proven absent: the list may be cut "
    "at 100 or limited by the selector and window."
)

TOOL_SCHEMAS: tuple[Mapping[str, Any], ...] = (
    {
        "type": "function",
        "function": {
            "name": INSTANT_QUERY_TOOL,
            "description": (
                "Evaluate a PromQL expression at one instant in the "
                "registered target's Prometheus. "
                "Use it for a current or point-in-time value, such as a "
                "rate over the last 5 minutes at a given moment; use "
                "metrics_range_query for how a value changed over time. "
                "content is the instant query's data.result. "
                "time is an absolute ISO-8601 instant inside the authorized "
                "window; omitted, it is the end of the default query window "
                "(the time policy's default_query_window when it states "
                "one, otherwise the authorized window's end), not the "
                "current time. "
                "offset and @ are refused, and so is an expression whose "
                "range selectors or subqueries read back to before the "
                "authorized window's start. "
                "The query times out after 30 seconds. "
                "A result whose whole view exceeds 25,000 tokens is refused "
                "with RESULT_TOO_LARGE and no series; the refusal gives "
                "series and label counts and a topk suggestion. "
                "A returned value does not prove the service is healthy or "
                "unhealthy, and a series that is not returned is unknown, "
                "not zero; a value at one instant cannot show what happened "
                "before or after it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "The PromQL expression to evaluate, for example "
                            "sum(rate(traces_span_metrics_calls_total"
                            '{service_name="checkout"}[5m])).'
                        ),
                    },
                    "time": {
                        "type": "string",
                        "description": (
                            "Optional absolute ISO-8601 evaluation instant "
                            "with a timezone, inside the authorized window; "
                            "omitted, the end of the default query window."
                        ),
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": SERIES_TOOL,
            "description": (
                "List the time series (their full label sets) in the "
                "registered target's Prometheus that match a selector, "
                "inside a query window you may choose within the authorized "
                "window. "
                "Use it to see which label combinations exist before "
                "writing a query. "
                "content is the list of label sets returned. " + _LIST_DESCRIPTION_TAIL
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "match": {
                        "type": "string",
                        "description": (
                            "Required, non-empty PromQL series selector, for "
                            'example {__name__="traces_span_metrics_calls_total",'
                            'service_name="checkout"}.'
                        ),
                    },
                    "start": {
                        "type": "string",
                        "description": _WINDOW_PARAMETER_TEXT["start"],
                    },
                    "end": {
                        "type": "string",
                        "description": _WINDOW_PARAMETER_TEXT["end"],
                    },
                },
                "required": ["match"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": LABELS_TOOL,
            "description": (
                "List the label names in the registered target's "
                "Prometheus, optionally only on series matching a selector, "
                "inside a query window you may choose within the authorized "
                "window. "
                "Use it to find which labels exist before listing their "
                "values with get_label_values. "
                "content is the list of label names returned. " + _LIST_DESCRIPTION_TAIL
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "match": {
                        "type": "string",
                        "description": (
                            "Optional PromQL series selector that limits the "
                            "series the label names come from; when given it "
                            "must not be empty."
                        ),
                    },
                    "start": {
                        "type": "string",
                        "description": _WINDOW_PARAMETER_TEXT["start"],
                    },
                    "end": {
                        "type": "string",
                        "description": _WINDOW_PARAMETER_TEXT["end"],
                    },
                },
                "required": [],
            },
        },
    },
)

_ERROR_LIMITS = (
    "Prometheus HTTP 400/422 is error/INVALID_PARAMS and its refusal's "
    "source_error carries Prometheus's own errorType and error text "
    "(untrusted, credential-redacted, never citable as a fact) so the query "
    "can be corrected; other 4xx carry only the HTTP status. Prometheus HTTP "
    "503 with JSON errorType=timeout is timeout/TOOL_TIMEOUT with confirmed "
    "source contact; other 5xx responses are error/SOURCE_UNAVAILABLE."
)
_LIST_LIMITS = (
    "Every request asks Prometheus for at most 100 entries (a fixed limit "
    "the model cannot change). Exactly 100 entries or any Prometheus warning "
    "sets incomplete true with incomplete_reason asking for a narrower "
    "selector or window; rows are never dropped by the view (truncated "
    "stays false). The whole view is also limited to the registered "
    "max_view_tokens (25,000 DeepSeek tokens): a larger result is refused "
    "with RESULT_TOO_LARGE, never truncated. " + _ERROR_LIMITS
)
_LIST_CANNOT_PROVE = (
    "A listed series or label name does not prove the series has samples in "
    "the query window, and says nothing about its values or the service's "
    "health; one that is not listed is unknown, not proof that it does not "
    "exist (the list may be cut at 100 or narrowed by the selector and "
    "window)."
)
_LIST_ERROR_CLASSES = {
    "400": "INVALID_PARAMS",
    "422": "INVALID_PARAMS",
    "503": "SOURCE_UNAVAILABLE",
    "QUERY_OUT_OF_WINDOW": "INVALID_PARAMS",
    "MATCH_INVALID": "INVALID_PARAMS",
}


def _window_parameters() -> dict[str, ParameterSpec]:
    return {
        "start": ParameterSpec(
            "string",
            description=(
                "Absolute ISO-8601 start of the query window; give both "
                "start and end or neither."
            ),
        ),
        "end": ParameterSpec(
            "string",
            description=(
                "Absolute ISO-8601 end of the query window; give both start "
                "and end or neither."
            ),
        ),
    }


def prometheus_ext_registrations() -> tuple[ToolRegistration, ...]:
    """The three registrations: the ``otel-demo`` Prometheus envelope (1 MiB
    raw, 25k-token view, 30 s request, the 24 h frame as window ceiling,
    ``source="otel-demo"``, exact response bytes as raw evidence)."""
    common: dict[str, Any] = {
        "version": "v1",
        "source": otel_demo.SOURCE,
        "may_contain_secrets": False,
        "request_timeout_seconds": REQUEST_TIMEOUT_SECONDS,
        "max_result_bytes": 1024 * 1024,
        "max_view_tokens": otel_demo.MAX_VIEW_TOKENS,
        "max_window_seconds": otel_demo.OBSERVATION_SECONDS,
        "incomplete_marker": "warnings",
    }
    instant = ToolRegistration(
        name=INSTANT_QUERY_TOOL,
        verb="query",
        description=ToolDescription(
            returns=(
                "The exact Prometheus instant-query response of the "
                "registered target: data.result holds the value of the "
                "expression at one evaluation instant (a vector of series "
                "with one [timestamp, value] each, a matrix for a bare range "
                "selector, or a scalar); the view adds evaluation_time."
            ),
            window_format=(
                "The authorized absolute window, appended as {window}. The "
                "evaluation instant is always sent: time, when given, must "
                "be an absolute instant inside it; omitted, it is the time "
                "policy's default_query_window end when it states one, "
                "otherwise the authorized window's end, never the current "
                "time."
            ),
            values_format=(
                "The authorized target for this Run is appended as {values}. "
                "offset is refused. "
                "@ is refused. "
                "An expression whose range selectors and subqueries read "
                "back from the evaluation instant to before the authorized "
                "window's start is refused; lookback_seconds and "
                "lookback_start_at state how far an accepted one reads."
            ),
            limits=(
                "No limit is sent. The whole view is limited to the "
                "registered max_view_tokens (25,000 DeepSeek tokens): a "
                "larger result is refused with RESULT_TOO_LARGE and no "
                "series, never truncated; the refusal states counts only "
                "(series, points, label names with their value counts), a "
                "topk suggestion and, when the result was kept, its "
                "evidence_id for read_evidence. The request times out after "
                "30 seconds. " + _ERROR_LIMITS
            ),
            cannot_prove=(
                "A returned value holds at the evaluation instant only; it "
                "does not prove the service is healthy or unhealthy, a series "
                "that is not returned is unknown, not zero, and cumulative "
                "counters are not rates."
            ),
        ),
        parameters={
            "query": ParameterSpec(
                "string",
                required=True,
                description="The PromQL expression to evaluate at one instant.",
            ),
            "time": ParameterSpec(
                "string",
                description=(
                    "Absolute ISO-8601 evaluation instant inside the "
                    "authorized window; omitted, the default query window's end."
                ),
            ),
        },
        result_path=("data", "result"),
        error_classes={
            "400": "INVALID_PARAMS",
            "422": "INVALID_PARAMS",
            "503": "SOURCE_UNAVAILABLE",
            "QUERY_OUT_OF_WINDOW": "INVALID_PARAMS",
        },
        **common,
    )
    series = ToolRegistration(
        name=SERIES_TOOL,
        verb="list",
        description=ToolDescription(
            returns=(
                "The exact Prometheus series response of the registered "
                "target: data holds the label set of every series matching "
                "the selector inside the query window; the view adds "
                "incomplete_reason and limit."
            ),
            window_format=otel_demo._DISCOVERY_WINDOW_FORMAT,
            values_format=(
                "The authorized target for this Run is appended as {values}. "
                "match is a required, non-empty PromQL series selector; "
                "Prometheus parses it and a selector it rejects is "
                "INVALID_PARAMS."
            ),
            limits=_LIST_LIMITS,
            cannot_prove=_LIST_CANNOT_PROVE,
        ),
        parameters={
            "match": ParameterSpec(
                "string",
                required=True,
                description="PromQL series selector the series must match.",
            ),
            **_window_parameters(),
        },
        result_path=("data",),
        error_classes=_LIST_ERROR_CLASSES,
        **common,
    )
    labels = ToolRegistration(
        name=LABELS_TOOL,
        verb="list",
        description=ToolDescription(
            returns=(
                "The exact Prometheus label-names response of the registered "
                "target: data holds the label names on series inside the "
                "query window, optionally only series matching the "
                "selector; the view adds incomplete_reason and limit."
            ),
            window_format=otel_demo._DISCOVERY_WINDOW_FORMAT,
            values_format=(
                "The authorized target for this Run is appended as {values}. "
                "match, when given, is a non-empty PromQL series selector; "
                "Prometheus parses it and a selector it rejects is "
                "INVALID_PARAMS."
            ),
            limits=_LIST_LIMITS,
            cannot_prove=_LIST_CANNOT_PROVE,
        ),
        parameters={
            "match": ParameterSpec(
                "string",
                description="Optional PromQL series selector that limits the series.",
            ),
            **_window_parameters(),
        },
        result_path=("data",),
        error_classes=_LIST_ERROR_CLASSES,
        **common,
    )
    return instant, series, labels


_ROW_UNITS = {SERIES_TOOL: "series", LABELS_TOOL: "label_names"}


def overflow_summary(
    tool: str, rows: Sequence[object], query: Mapping[str, object]
) -> dict[str, object]:
    """The over-limit refusal summary of these tools (A5-3): the instant query
    gets the range query's series summary and topk suggestion, computed on
    its ``query`` parameter; the lists name their row unit."""
    if tool == INSTANT_QUERY_TOOL:
        return otel_demo.overflow_summary(
            otel_demo.METRICS_TOOL, rows, {"expr": query.get("query")}
        )
    unit = _ROW_UNITS.get(tool)
    return {} if unit is None else {"row_unit": unit}


def _instant_time(
    request: TransportRequest,
) -> tuple[datetime | None, TransportResponse | None]:
    """A5-2: the evaluation instant, or a refusal stating the frame."""
    frame = request.window
    text = request.params.get("time")
    if text is None:
        if request.default_window is not None:
            return request.default_window.end, None
        return frame.end, None
    refused = otel_demo._refused("QUERY_OUT_OF_WINDOW", frame=frame)
    if not isinstance(text, str):
        return None, refused
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None, refused
    if moment.tzinfo is None or moment.utcoffset() is None:
        return None, refused
    try:
        moment = moment.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None, refused
    if not frame.start <= moment <= frame.end:
        return None, refused
    return moment, None


def _instant_bounds(body: bytes) -> tuple[datetime | None, datetime | None]:
    """Sample timestamps of an instant answer: vector and matrix through
    ``otel_demo._prometheus_bounds``; a scalar or string result is one
    ``[timestamp, value]`` pair."""
    start, end = otel_demo._prometheus_bounds(body)
    if start is not None:
        return start, end
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, None
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, Mapping) or data.get("resultType") not in (
        "scalar",
        "string",
    ):
        return None, None
    result = data.get("result")
    if (
        isinstance(result, list)
        and result
        and isinstance(result[0], (int, float))
        and not isinstance(result[0], bool)
    ):
        try:
            moment = otel_demo._utc(float(result[0]))
        except (OverflowError, OSError, ValueError):
            return None, None
        return moment, moment
    return None, None


class PrometheusExtTransport:
    """The three tools over the profile's ``OtelDemoTransport`` (A5-7).

    ``base`` must have been built with ``PROMETHEUS_EXT_TOOLS`` in its
    ``prometheus_tools`` so basic auth, the 503 timeout and the 400/422 echo
    apply; a credential ref the base does not hold is refused, never sent
    anonymously.
    """

    def __init__(self, base: otel_demo.OtelDemoTransport) -> None:
        self._base = base

    def fetch(self, request: TransportRequest) -> TransportResponse:
        if request.credential_ref not in self._base._credentials:
            raise TransportError("CREDENTIAL_REF_UNKNOWN")
        if request.tool == INSTANT_QUERY_TOOL:
            return self._instant(request)
        if request.tool in (SERIES_TOOL, LABELS_TOOL):
            return self._list(request)
        raise TransportError("TOOL_NOT_SUPPORTED")

    def _instant(self, request: TransportRequest) -> TransportResponse:
        query = request.params.get("query")
        if not isinstance(query, str):
            return otel_demo._refused("QUERY_OUT_OF_WINDOW")
        moment, refusal = _instant_time(request)
        if refusal is not None:
            return refusal
        assert moment is not None
        # The lookback may reach back to the frame's start, not before it.
        reach = (moment - request.window.start).total_seconds()
        problem = otel_demo.promql_problem(query, reach)
        if problem is not None:
            return otel_demo._refused(problem, frame=request.window)
        lookback = otel_demo.promql_lookback_seconds(query)
        url = f"{request.endpoint}/api/v1/query?" + urllib.parse.urlencode(
            {
                "query": query,
                "time": f"{moment.timestamp():.3f}",
                "timeout": f"{int(request.timeout_seconds)}s",
            }
        )
        fetched = self._base._get(url, request, max_bytes=request.max_result_bytes)
        if fetched.status != 200:
            return self._base.source_error(fetched, request.tool)
        start_at, end_at = _instant_bounds(fetched.body)
        fields: dict[str, object] = {"evaluation_time": moment.isoformat()}
        if otel_demo._has_calls_total_selector(query):
            fields["series_note"] = otel_demo.SERIES_NOTE
        # With a lookback the read covers [instant - lookback, instant]; the
        # executor shows its start as lookback_start_at. Without one the
        # instant alone is read, which no ``Window`` can hold, so the view
        # keeps the authorized window and evaluation_time states the instant.
        reads_back = lookback > 0
        return TransportResponse(
            body=fetched.body,
            data_as_of=end_at,
            source_start_at=start_at,
            source_end_at=end_at,
            lookback_seconds=lookback if reads_back else None,
            query_window=(
                Window(moment - timedelta(seconds=lookback), moment)
                if reads_back
                else None
            ),
            view_fields=fields,
        )

    def _list(self, request: TransportRequest) -> TransportResponse:
        match = request.params.get("match")
        if request.tool == SERIES_TOOL or match is not None:
            if not otel_demo._match_ok(match):
                return otel_demo._refused("MATCH_INVALID")
        window, refusal = otel_demo._query_window(request)
        if refusal is not None:
            return refusal
        assert window is not None
        query: list[tuple[str, str]] = []
        if isinstance(match, str):
            query.append(("match[]", match))
        query += [
            ("start", f"{window.start.timestamp():.3f}"),
            ("end", f"{window.end.timestamp():.3f}"),
            ("limit", str(LIST_LIMIT)),
        ]
        path = "series" if request.tool == SERIES_TOOL else "labels"
        url = f"{request.endpoint}/api/v1/{path}?" + urllib.parse.urlencode(query)
        fetched = self._base._get(url, request, max_bytes=request.max_result_bytes)
        if fetched.status != 200:
            return self._base.source_error(fetched, request.tool)
        try:
            payload = json.loads(fetched.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            # Hand the executor the bytes; it classifies them MALFORMED_RESULT.
            return TransportResponse(body=fetched.body)
        warned = isinstance(payload, Mapping) and bool(payload.get("warnings"))
        data = payload.get("data") if isinstance(payload, Mapping) else None
        full = isinstance(data, list) and len(data) >= LIST_LIMIT
        incomplete = warned or full
        return TransportResponse(
            body=fetched.body,
            query_window=window,
            incomplete=incomplete,
            view_fields={
                "incomplete_reason": LIST_INCOMPLETE_REASON if incomplete else None,
                "limit": LIST_LIMIT,
            },
        )
