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

B2 (#561, docs/tasks/2026-11-12-tool-expansion-b2.md, decision table r3
B2-1 to B2-6) adds three no-window state tools on the same transport,
credential and error classes (B2-6):

* ``list_prometheus_rules(type?, rule_name?, rule_group?, file?, match?)``
  forwards the filters to ``/api/v1/rules`` as given (B2-2: ``type`` only
  ``alert``/``record``, no jq) and keeps the exact bytes; the active alerts
  inside the rules come back as returned, with no source time (B2-3).
* ``get_prometheus_target(target_name)`` reads ``/api/v1/targets?state=active``
  and keeps the targets whose ``labels.job`` equals ``target_name``, without
  ``discoveredLabels`` (B2-5); the record keeps the source bytes' sha256.
* ``get_metric_metadata(metric?)`` reads ``/api/v1/metadata`` with a fixed
  ``limit=100``; 100 metrics or a warning is incomplete (B2-4).

None of them has a time range: each shows the state at the moment of the
query, and every view says so in ``notes``.

Errors are classified as for the ``otel-demo`` discovery tools (A5-8): 400/422
are ``INVALID_PARAMS`` with Prometheus's own error echoed, 503 with
``errorType=timeout`` is a confirmed timeout, other 5xx ``SOURCE_UNAVAILABLE``.

Transport (A5-7): the requests go through the profile's one
``OtelDemoTransport`` -- the same GET-only, no-proxy, no-redirect, bounded
reader and the same basic-auth credential -- built with these three names
added to its ``prometheus_tools`` (with B2, all six).
``otel_demo.PROMETHEUS_TOOLS`` is not
changed: that set is part of ``otel-demo``'s version hash.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any

from opspilot.redaction import has_duplicate_keys
from opspilot.tools import otel_demo
from opspilot.tools.executor import (
    TransportError,
    TransportRequest,
    TransportResponse,
    TransportResultTooLarge,
)
from opspilot.tools.outcomes import Window
from opspilot.tools.registry import (
    ParameterSpec,
    ToolDescription,
    ToolRegistration,
    canonical,
)

__all__ = [
    "INSTANT_QUERY_TOOL",
    "LABELS_TOOL",
    "LIST_INCOMPLETE_REASON",
    "LIST_LIMIT",
    "METADATA_TOOL",
    "PROMETHEUS_EXT_TOOLS",
    "RULES_TOOL",
    "SERIES_TOOL",
    "STATE_TOOL_NAMES",
    "TARGET_TOOL",
    "TOOL_NAMES",
    "TOOL_SCHEMAS",
    "PrometheusExtTransport",
    "overflow_summary",
    "prometheus_ext_registrations",
]

INSTANT_QUERY_TOOL = "execute_prometheus_instant_query"
SERIES_TOOL = "get_series"
LABELS_TOOL = "get_all_labels"
RULES_TOOL = "list_prometheus_rules"
TARGET_TOOL = "get_prometheus_target"
METADATA_TOOL = "get_metric_metadata"
#: B2 (#561): the three no-window state tools.
STATE_TOOL_NAMES: tuple[str, ...] = (RULES_TOOL, TARGET_TOOL, METADATA_TOOL)
TOOL_NAMES: tuple[str, ...] = (
    INSTANT_QUERY_TOOL,
    SERIES_TOOL,
    LABELS_TOOL,
    *STATE_TOOL_NAMES,
)
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

_QUERY_SCHEMAS: tuple[Mapping[str, Any], ...] = (
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

# -- B2 (#561): rules, scrape targets and metric metadata ------------------------

#: B2-2: the two rule types Prometheus's ``type`` filter accepts.
RULE_TYPES: tuple[str, ...] = ("alert", "record")
#: B2-4: the fixed ``limit`` of every metadata request (upstream
#: ``PROMETHEUS_METADATA_API_LIMIT``, the discovery tools' value).
METADATA_LIMIT = otel_demo.DISCOVERY_LIMIT
#: B2-5: a job name is at most this long (the A4-3 name bound).
MAX_NAME_CHARS = 200
# B2-4: the classic Prometheus metric-name pattern; checked before sending.
_METRIC_NAME = re.compile(r"[A-Za-z_:][A-Za-z0-9_:]*")
#: B2-5: how many distinct job names ``jobs_seen`` shows at most.
JOBS_SEEN_LIMIT = 100
STATE_NOTE = (
    "This tool has no time range: it shows Prometheus's state at the moment "
    "of the query, which can be after the authorized window; it cannot show "
    "the state when the incident happened."
)
RULES_INCOMPLETE_REASON = (
    "Prometheus returned warnings with this result or a next-page token, so "
    "more rule groups may exist; narrow the result with type, rule_name, "
    "rule_group, file or match."
)
METADATA_INCOMPLETE_REASON = (
    "Prometheus returned warnings with this result or exactly the 100-metric "
    "limit, so more metrics may exist; give metric to look up one metric."
)
_TARGET_ERRORS_TEXT = (
    "A result whose whole view exceeds 25,000 tokens is refused with "
    "RESULT_TOO_LARGE and nothing is returned."
)

_STATE_SCHEMAS: tuple[Mapping[str, Any], ...] = (
    {
        "type": "function",
        "function": {
            "name": RULES_TOOL,
            "description": (
                "List the alerting and recording rules loaded in the "
                "registered target's Prometheus, with the pending or firing "
                "alerts of each alerting rule. "
                "Use it to see which alerts are firing now and what a rule's "
                "expression, labels and annotations are. "
                "content is the list of rule groups, each {name, file, rules: "
                "[{name, query, type, health, labels, annotations, state, "
                "alerts, ...}]}. "
                "The filters are sent to Prometheus as given; without any, "
                "every rule is returned. "
                "There is no time range: the result is the rules and alerts "
                "at the moment of the query, which can be after the "
                "authorized window, so it cannot prove which alerts were "
                "firing or which rules existed when the incident happened. "
                "A result whose whole view exceeds 25,000 tokens is refused "
                "with RESULT_TOO_LARGE and nothing is returned; narrow it "
                "with the filters. A rule that is not listed is unknown, "
                "not proven absent."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "description": (
                            "Optional rule type: alert for alerting rules, "
                            "record for recording rules."
                        ),
                    },
                    "rule_name": {
                        "type": "string",
                        "description": "Optional exact rule name.",
                    },
                    "rule_group": {
                        "type": "string",
                        "description": "Optional exact rule group name.",
                    },
                    "file": {
                        "type": "string",
                        "description": "Optional exact rule file path.",
                    },
                    "match": {
                        "type": "string",
                        "description": (
                            "Optional label selector on the rules' own "
                            'labels, for example {severity="critical"}.'
                        ),
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": TARGET_TOOL,
            "description": (
                "Get the active scrape targets of one scrape job in the "
                "registered target's Prometheus: each target's labels, "
                "scrape pool, scrape URL, health (up, down or unknown), last "
                "error, last scrape time and duration; the raw "
                "service-discovery labels (discoveredLabels) are removed. "
                "Use it to check whether Prometheus can currently scrape a "
                "service's metrics. "
                "content is the list of active targets whose job label "
                "equals target_name; jobs_seen lists the job names of all "
                "active targets so a name that matched nothing can be "
                "corrected. "
                "There is no time range: the result is the state at the "
                "moment of the query, which can be after the authorized "
                "window; lastScrape and lastError describe only the most "
                "recent scrape, so it cannot prove a target's health when "
                "the incident happened. " + _TARGET_ERRORS_TEXT
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_name": {
                        "type": "string",
                        "description": (
                            "The scrape job name, the value of the targets' "
                            "job label; non-empty, at most 200 characters."
                        ),
                    },
                },
                "required": ["target_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": METADATA_TOOL,
            "description": (
                "Get the type (counter, gauge, histogram, summary, ...), help "
                "text and unit of metrics in the registered target's "
                "Prometheus. "
                "Use it after finding metric names to learn what a metric "
                "measures and how to query it. "
                "content is one row per metric and metadata entry: {metric, "
                "type, help, unit}. "
                "At most 100 metrics are returned (a fixed limit you cannot "
                "change); when exactly 100 come back, or Prometheus returned "
                "any warning, incomplete is true: give metric to look one "
                "up. "
                "There is no time range: it is the metadata Prometheus holds "
                "at the moment of the query, which can be after the "
                "authorized window, so it cannot prove what a metric's "
                "metadata was when the incident happened. Metadata says "
                "nothing about "
                "values or the service's health, and a metric that is not "
                "listed is unknown, not proven absent. "
                "A result whose whole view exceeds 25,000 tokens is refused "
                "with RESULT_TOO_LARGE and nothing is returned."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "metric": {
                        "type": "string",
                        "description": (
                            "Optional exact metric name, for example "
                            "traces_span_metrics_calls_total; omitted, up to "
                            "100 metrics."
                        ),
                    },
                },
                "required": [],
            },
        },
    },
)

TOOL_SCHEMAS = (*_QUERY_SCHEMAS, *_STATE_SCHEMAS)

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
    return instant, series, labels, *_state_registrations(common)


_STATE_WINDOW_FORMAT = (
    "The authorized absolute window is appended as {window}, but this tool "
    "has no time range: it reads Prometheus's state at the moment of the "
    "query, which can be after that window, and no source time is reported."
)
_STATE_ERROR_CLASSES = {
    "400": "INVALID_PARAMS",
    "422": "INVALID_PARAMS",
    "503": "SOURCE_UNAVAILABLE",
}


def _state_registrations(
    common: Mapping[str, Any],
) -> tuple[ToolRegistration, ...]:
    """B2: rules, scrape targets and metric metadata on the A5 envelope (B2-6)."""
    rules = ToolRegistration(
        name=RULES_TOOL,
        verb="list",
        description=ToolDescription(
            returns=(
                "The exact Prometheus rules response of the registered "
                "target: data.groups holds the rule groups with their "
                "alerting and recording rules, and each alerting rule's "
                "pending or firing alerts; the view adds incomplete_reason "
                "and notes."
            ),
            window_format=_STATE_WINDOW_FORMAT,
            values_format=(
                "The authorized target for this Run is appended as {values}. "
                "type, when given, is alert or record; rule_name, rule_group "
                "and file, when given, are non-empty strings of at most 2000 "
                "characters without control characters, sent as Prometheus's "
                "rule_name[], rule_group[] and file[]; match, when given, is "
                "a non-empty label selector sent as match[]. A value outside "
                "these rules is INVALID_PARAMS and nothing is sent."
            ),
            limits=(
                "No limit or page size is sent. A warning or a next-page "
                "token sets incomplete true with incomplete_reason. The whole "
                "view is limited to the registered max_view_tokens (25,000 "
                "DeepSeek tokens): a larger result is refused with "
                "RESULT_TOO_LARGE, never truncated. " + _ERROR_LIMITS
            ),
            cannot_prove=(
                "Rules and alerts at the moment of the query only, not when "
                "the incident happened: an alert firing now may not have "
                "fired then and the reverse; a rule that is not listed is "
                "unknown, not proven absent; a rule's expression says "
                "nothing about the service's health by itself."
            ),
        ),
        parameters={
            "type": ParameterSpec(
                "string", description="Optional rule type: alert or record."
            ),
            "rule_name": ParameterSpec(
                "string", description="Optional exact rule name."
            ),
            "rule_group": ParameterSpec(
                "string", description="Optional exact rule group name."
            ),
            "file": ParameterSpec("string", description="Optional exact rule file."),
            "match": ParameterSpec(
                "string", description="Optional label selector on rule labels."
            ),
        },
        result_path=("data", "groups"),
        error_classes={
            **_STATE_ERROR_CLASSES,
            "RULE_TYPE_INVALID": "INVALID_PARAMS",
            "RULE_FILTER_INVALID": "INVALID_PARAMS",
            "MATCH_INVALID": "INVALID_PARAMS",
        },
        **common,
    )
    target = ToolRegistration(
        name=TARGET_TOOL,
        verb="get",
        description=ToolDescription(
            returns=(
                "The registered target's active Prometheus scrape targets "
                "whose job label equals target_name, under targets, each "
                "without discoveredLabels; the record keeps the sha256 and "
                "size of the exact source response. The view adds jobs_seen "
                "(the job names of all active targets, at most 100), "
                "active_targets_total and notes."
            ),
            window_format=_STATE_WINDOW_FORMAT,
            values_format=(
                "The authorized target for this Run is appended as {values}. "
                "target_name is a non-empty job name of at most 200 "
                "characters without control characters; otherwise "
                "INVALID_PARAMS and nothing is sent."
            ),
            limits=(
                "Prometheus is asked for active targets only "
                "(state=active); every one whose job matches is shown. The "
                "whole view is limited to the registered max_view_tokens "
                "(25,000 DeepSeek tokens): a larger result is refused with "
                "RESULT_TOO_LARGE, never truncated. " + _ERROR_LIMITS
            ),
            cannot_prove=(
                "Target state at the moment of the query only: health, "
                "lastScrape and lastError describe the most recent scrape, "
                "not the incident; an empty list does not prove the service "
                "is not scraped under another job name (see jobs_seen)."
            ),
        ),
        parameters={
            "target_name": ParameterSpec(
                "string",
                required=True,
                description="The scrape job name (the targets' job label).",
            ),
        },
        result_path=("targets",),
        error_classes={
            **_STATE_ERROR_CLASSES,
            "TARGET_NAME_INVALID": "INVALID_PARAMS",
        },
        **common,
    )
    metadata = ToolRegistration(
        name=METADATA_TOOL,
        verb="list",
        description=ToolDescription(
            returns=(
                "The registered target's Prometheus metric metadata, one "
                "row per metric and entry under metadata: {metric, type, "
                "help, unit}; the record keeps the sha256 and size of the "
                "exact source response. The view adds incomplete_reason, "
                "limit and notes."
            ),
            window_format=_STATE_WINDOW_FORMAT,
            values_format=(
                "The authorized target for this Run is appended as {values}. "
                "metric, when given, is a metric name matching "
                "[A-Za-z_:][A-Za-z0-9_:]* of at most 200 characters; "
                "otherwise INVALID_PARAMS and nothing is sent."
            ),
            limits=(
                "Every request asks Prometheus for at most 100 metrics (a "
                "fixed limit the model cannot change). Exactly 100 metrics "
                "or any Prometheus warning sets incomplete true with "
                "incomplete_reason. The whole view is limited to the "
                "registered max_view_tokens (25,000 DeepSeek tokens): a "
                "larger result is refused with RESULT_TOO_LARGE, never "
                "truncated. " + _ERROR_LIMITS
            ),
            cannot_prove=(
                "Metadata is what Prometheus holds at the moment of the "
                "query, not when the incident happened; it says nothing "
                "about values or the service's "
                "health, and a metric that is not listed is unknown, not "
                "proven absent (the list may be cut at 100)."
            ),
        ),
        parameters={
            "metric": ParameterSpec(
                "string", description="Optional exact metric name."
            ),
        },
        result_path=("metadata",),
        error_classes={
            **_STATE_ERROR_CLASSES,
            "METRIC_INVALID": "INVALID_PARAMS",
        },
        **common,
    )
    return rules, target, metadata


_ROW_UNITS = {
    SERIES_TOOL: "series",
    LABELS_TOOL: "label_names",
    RULES_TOOL: "rule_groups",
    TARGET_TOOL: "targets",
    METADATA_TOOL: "metadata_entries",
}


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


def _filter_ok(value: object, max_chars: int) -> bool:
    """B2-2/B2-5: a non-empty string within ``max_chars``, no control characters."""
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= max_chars
        and not any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)
    )


def _success_payload(body: bytes) -> Mapping[str, Any] | None:
    """A Prometheus ``status: success`` envelope without duplicate keys, else
    ``None`` (the caller hands the raw bytes on for MALFORMED_RESULT)."""
    try:
        text = body.decode("utf-8")
        payload = json.loads(text)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(payload, Mapping) or has_duplicate_keys(text):
        return None
    if payload.get("status") != "success":
        return None
    return payload


def _record_body(record: Mapping[str, Any], request: TransportRequest) -> bytes:
    body = canonical(record).encode("utf-8")
    if len(body) > request.max_result_bytes:
        raise TransportResultTooLarge(str(request.max_result_bytes))
    return body


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
        if request.tool == RULES_TOOL:
            return self._rules(request)
        if request.tool == TARGET_TOOL:
            return self._target(request)
        if request.tool == METADATA_TOOL:
            return self._metadata(request)
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

    # -- B2 -------------------------------------------------------------------

    def _rules(self, request: TransportRequest) -> TransportResponse:
        """B2-2/B2-3: filters forwarded as given; exact bytes kept."""
        params = request.params
        query: list[tuple[str, str]] = []
        kind = params.get("type")
        if kind is not None:
            if kind not in RULE_TYPES:
                return otel_demo._refused("RULE_TYPE_INVALID")
            query.append(("type", str(kind)))
        for name in ("rule_name", "rule_group", "file"):
            value = params.get(name)
            if value is None:
                continue
            if not _filter_ok(value, otel_demo.MAX_PROMQL_CHARS):
                return otel_demo._refused("RULE_FILTER_INVALID")
            query.append((f"{name}[]", str(value)))
        match = params.get("match")
        if match is not None:
            if not otel_demo._match_ok(match):
                return otel_demo._refused("MATCH_INVALID")
            query.append(("match[]", str(match)))
        url = f"{request.endpoint}/api/v1/rules"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        fetched = self._base._get(url, request, max_bytes=request.max_result_bytes)
        if fetched.status != 200:
            return self._base.source_error(fetched, request.tool)
        payload = _success_payload(fetched.body)
        if payload is None:
            return TransportResponse(body=fetched.body)
        data = payload.get("data")
        # Risk 2 of the task record: no group_limit is sent, so v3.5 should
        # not page; a next-page token would still mean groups are missing.
        paged = isinstance(data, Mapping) and bool(data.get("groupNextToken"))
        warned = bool(payload.get("warnings"))
        # B2-3: no source time; the rules and alerts are the query moment's.
        return TransportResponse(
            body=fetched.body,
            incomplete=paged,
            view_fields={
                "incomplete_reason": (
                    RULES_INCOMPLETE_REASON if paged or warned else None
                ),
                "notes": [STATE_NOTE],
            },
        )

    def _target(self, request: TransportRequest) -> TransportResponse:
        """B2-5: active targets of one job, ``discoveredLabels`` removed."""
        name = request.params.get("target_name")
        if not _filter_ok(name, MAX_NAME_CHARS):
            return otel_demo._refused("TARGET_NAME_INVALID")
        assert isinstance(name, str)
        url = f"{request.endpoint}/api/v1/targets?" + urllib.parse.urlencode(
            {"state": "active"}
        )
        fetched = self._base._get(url, request, max_bytes=request.max_result_bytes)
        if fetched.status != 200:
            return self._base.source_error(fetched, request.tool)
        payload = _success_payload(fetched.body)
        data = None if payload is None else payload.get("data")
        active = data.get("activeTargets") if isinstance(data, Mapping) else None
        if payload is None or not isinstance(active, list):
            return TransportResponse(body=fetched.body)
        kept: list[dict[str, Any]] = []
        jobs: set[str] = set()
        for item in active:
            labels = item.get("labels") if isinstance(item, Mapping) else None
            job = labels.get("job") if isinstance(labels, Mapping) else None
            if not isinstance(job, str):
                continue
            jobs.add(job)
            if job == name:
                assert isinstance(item, Mapping)
                kept.append({k: v for k, v in item.items() if k != "discoveredLabels"})
        record: dict[str, Any] = {
            "source": "prometheus",
            "query": {"target_name": name, "state": "active"},
            "source_response_sha256": sha256(fetched.body).hexdigest(),
            "source_response_bytes": len(fetched.body),
            "targets": kept,
        }
        if payload.get("warnings"):
            record["warnings"] = payload["warnings"]
        seen = sorted(jobs)
        return TransportResponse(
            body=_record_body(record, request),
            view_fields={
                "jobs_seen": seen[:JOBS_SEEN_LIMIT],
                "jobs_seen_truncated": len(seen) > JOBS_SEEN_LIMIT,
                "active_targets_total": len(active),
                "notes": [STATE_NOTE],
            },
        )

    def _metadata(self, request: TransportRequest) -> TransportResponse:
        """B2-4: fixed ``limit=100``, optional exact metric name."""
        metric = request.params.get("metric")
        query: list[tuple[str, str]] = [("limit", str(METADATA_LIMIT))]
        if metric is not None:
            if not (
                isinstance(metric, str)
                and len(metric) <= MAX_NAME_CHARS
                and _METRIC_NAME.fullmatch(metric)
            ):
                return otel_demo._refused("METRIC_INVALID")
            query.append(("metric", metric))
        url = f"{request.endpoint}/api/v1/metadata?" + urllib.parse.urlencode(query)
        fetched = self._base._get(url, request, max_bytes=request.max_result_bytes)
        if fetched.status != 200:
            return self._base.source_error(fetched, request.tool)
        payload = _success_payload(fetched.body)
        data = None if payload is None else payload.get("data")
        if payload is None or not isinstance(data, Mapping):
            return TransportResponse(body=fetched.body)
        rows: list[dict[str, Any]] = []
        for name in sorted(data):
            entries = data[name]
            if not isinstance(entries, list):
                return TransportResponse(body=fetched.body)
            for entry in entries:
                if not isinstance(entry, Mapping):
                    return TransportResponse(body=fetched.body)
                rows.append(
                    {
                        "metric": name,
                        "type": entry.get("type"),
                        "help": entry.get("help"),
                        "unit": entry.get("unit"),
                    }
                )
        record: dict[str, Any] = {
            "source": "prometheus",
            "query": {"metric": metric, "limit": METADATA_LIMIT},
            "source_response_sha256": sha256(fetched.body).hexdigest(),
            "source_response_bytes": len(fetched.body),
            "metadata": rows,
        }
        warned = bool(payload.get("warnings"))
        if warned:
            record["warnings"] = payload["warnings"]
        full = len(data) >= METADATA_LIMIT
        return TransportResponse(
            body=_record_body(record, request),
            incomplete=full,
            view_fields={
                "incomplete_reason": (
                    METADATA_INCOMPLETE_REASON if full or warned else None
                ),
                "limit": METADATA_LIMIT,
                "notes": [STATE_NOTE],
            },
        )
