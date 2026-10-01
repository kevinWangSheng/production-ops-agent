"""The OTel Demo tool profile: real read-only Prometheus and Jaeger queries.

The second tool *profile* next to :mod:`opspilot.tools.fixture`, and the
first real one: the same executor, ledger and evidence path, with a real
HTTP transport over the pinned OTel Demo 2.0.2 lab (technical plan §12) and
a real ``ControlSnapshot`` source read from the durable store, so the
executor's own pause/suspension/generation checks are live.

What a Run may read (technical plan §8):

* one registered target, the lab integration ``m0-otel-20260909`` (the
  ``opspilot.integration.id`` resource attribute baked into the pinned
  configuration); the Prometheus and Jaeger instances are dedicated to it,
  so target isolation is by instance, not by a spliced-in label filter;
* ``metrics_range_query``: a PromQL range query over a query window inside
  the Run's authorized frame (batch B, B2). ``start``/``end`` are optional
  tool parameters the model may use to pick a narrower window than the
  frame; omitted, the query defaults to the 1 hour ending at the frame's
  end (upstream ``holmes/plugins/toolsets/utils.py:111-113`` semantics).
  A window outside the frame is refused before any request goes out and the
  refusal states the frame. The raw evidence is the exact Prometheus
  response; the model view is its leading ``data.result`` series. ``offset``,
  ``@`` and a range selector longer than the *query* window are refused
  before any request goes out; a selector up to the query window's length is
  allowed and the query's ``start`` is pushed forward by its length, so every
  evaluated point reads samples from inside the query window only (a
  window-length selector evaluates once, at the window end, as the
  whole-window aggregate) -- the view reports the length as
  ``lookback_seconds`` and the earliest instant read as ``lookback_start_at``
  (the query window's start) next to ``source_start_at``;
* ``traces_search``: Jaeger's trace search for one service inside a query
  window chosen the same way (``start``/``end``, same default and frame
  refusal as above). Raw Jaeger responses are 300 KB-600 KB for 20 traces,
  so the raw evidence here is the transport's projected observation record
  -- the M0 trace view v3 sampling policy ported to product code -- which
  carries the SHA-256 and byte count of the wire response it was projected
  from. That is a weaker provenance than the metrics tool's exact bytes and
  is stated as such in the tool description.

The authorized frame is fixed per Run when the workbench records the input
(``otel_demo_face``): the 24 h ending at submission (batch B, B2; upstream
default lookback scaled to a runner-level authorization frame rather than a
fixed 300 s v4 acceptance-packet window -- see ``OBSERVATION_SECONDS``). The
executor factory re-reads that frame from the Run's recorded input rather
than trusting the model or the clock, so every attempt of a Run is
authorized over the same interval; the model still picks a narrower query
window inside it per call, as above.

Credentials: the lab backends are anonymous. A bearer token, if one is
configured, is looked up by ``credential_ref`` inside the transport and
sent on the request only; it never appears in a registration, an evidence
record, a log line or model context (PRODUCT-CONSTRAINTS, "Data flow
contract").

Following upstream: HolmesGPT's Prometheus toolset issues the same
``api/v1/query_range`` request (query, start, end, step, timeout) and
Jaeger is what the M0 environment already read (``read_proxy.py``,
``observe_window.py``); the trace projection is ``trace_view.py``'s.
"""

from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from http.client import HTTPResponse
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from opspilot.investigation.context import ContextError, InvestigationInput
from opspilot.investigation.inputs import ToolFace
from opspilot.investigation.loop import DISCIPLINE_VARIANT, investigation_versions
from opspilot.investigation.runner import ExecutorFactory
from opspilot.persistence import DurableStore, Lease, PersistenceError
from opspilot.tools.executor import (
    MAX_OPERATIONS_PER_RUN,
    MAX_TOOL_SECONDS_PER_RUN,
    Clock,
    ControlSnapshot,
    ControlUnavailable,
    EvidenceSink,
    QueryScope,
    ReadOnlyToolExecutor,
    TransportError,
    TransportRequest,
    TransportResponse,
    TransportResultTooLarge,
    TransportTimeout,
    TransportUnavailable,
)
from opspilot.tools.ledger import DurableToolLedger
from opspilot.tools.outcomes import Window
from opspilot.tools.registry import (
    ParameterSpec,
    RegisteredTarget,
    TargetRegistry,
    ToolContractError,
    ToolDescription,
    ToolRegistration,
    ToolRegistry,
    canonical,
    canonical_hash,
)
from opspilot.tools.tokens import TokenCounter, default_counter

__all__ = [
    "CREDENTIAL_REF",
    "METRICS_TOOL",
    "OBSERVATION_SECONDS",
    "SERVICES",
    "SOURCE",
    "TARGET_ID",
    "TOOL_SCHEMAS",
    "TOOL_SCHEMA_REVISION",
    "TRACES_TOOL",
    "TRACE_PROJECTION",
    "DurableControl",
    "OtelDemoConfig",
    "OtelDemoTransport",
    "otel_demo_executor_factory",
    "otel_demo_face",
    "otel_demo_versions",
    "project_traces",
    "promql_lookback_seconds",
    "promql_problem",
    "scope_window",
]

TARGET_ID = "m0-otel-20260909"
SOURCE = "otel-demo"
METRICS_TOOL = "metrics_range_query"
TRACES_TOOL = "traces_search"
CREDENTIAL_REF = "otel-demo-ro"
TRACE_PROJECTION = "otel-demo-traces-v1"
_TIME_POLICY = "policy-window-1"

# This is a view annotation, rather than model-facing tool guidance.  It is
# attached only to successful calls whose PromQL contains the span-metrics
# counter, where the counter's unit and UNSET status semantics matter.
SERIES_NOTE = (
    "counts spans of every operation of the service (internal, client and "
    "server spans alike), summed over the labels kept in this query; it is "
    "not a count of requests. status_code STATUS_CODE_UNSET means the span "
    "carried no status; it does not mean the call succeeded."
)

#: The services the pinned demo emits telemetry for (M0 ``read_proxy.py``).
SERVICES: tuple[str, ...] = (
    "accounting",
    "ad",
    "cart",
    "checkout",
    "currency",
    "email",
    "fraud-detection",
    "frontend",
    "frontend-proxy",
    "image-provider",
    "kafka",
    "load-generator",
    "payment",
    "product-catalog",
    "quote",
    "recommendation",
    "shipping",
)

#: The runner's default per-Run authorization frame (batch B, B2 --
#: "runner 仍决定一个授权外框（默认提交时刻前 24 小时至提交时刻）"), superseding
#: the v4 acceptance packet's fixed 300 s observation window. The model
#: chooses a narrower *query* window inside this frame per call (``start``/
#: ``end`` tool parameters, default: the 1 hour ending at the frame's end,
#: upstream ``holmes/plugins/toolsets/utils.py:111-113``); a query window
#: outside the frame is refused. The name is kept (not renamed to e.g.
#: ``AUTHORIZED_FRAME_SECONDS``) because it is a stable public export several
#: test modules import; only its value and role changed.
OBSERVATION_SECONDS = 24 * 3600
DEFAULT_STEP_SECONDS = 30
MIN_STEP_SECONDS = 15
MAX_PROMQL_CHARS = 2000
# B1 (docs/tasks/2026-09-28-m1-01-upstream-alignment-b.md): no more count
# ceiling on a trace search -- upstream's own trace-search tools (Grafana
# Tempo, the closest upstream analog to a Jaeger trace search; no direct
# upstream Jaeger toolset exists) enforce no ``limit`` upper bound either
# (``holmes/plugins/toolsets/grafana/toolset_grafana_tempo.py:542,635``) and
# default to 20 (``params.get("limit") or 20``) -- both the removed ceiling
# and the new default of 20 (was 10) are taken from that value; there was no
# upstream Jaeger-specific value to match instead.
DEFAULT_TRACE_LIMIT = 20
# The one reason a trace search is ``incomplete`` (round 2 rule B): Jaeger
# answered with as many traces as were asked for.
TRACES_INCOMPLETE_REASON = (
    "backend returned as many traces as requested; more may exist"
)
#: Jaeger's response for 20 traces has been 300-600 KB in this lab; the
#: transport stops reading past this and the operation fails as
#: ``RESULT_TOO_LARGE`` rather than projecting a partial body. Unaffected by
#: B1 (a raw-response-size guard, not the view row/byte cap).
TRACE_SOURCE_READ_BYTES = 4 * 1024 * 1024
# The model view's limit for both tools, in real DeepSeek tokens: upstream's
# per-tool single-result cap ``TOOL_MAX_ALLOCATED_CONTEXT_WINDOW_TOKENS = 25000``
# (``holmes/common/env_vars.py:142``); upstream's other bound, 15% of the
# window (``:137``), is 150_000 tokens at this project's 1_000_000-token
# context, so 25_000 is the one that binds. Metered on the whole view with the
# vendored DeepSeek tokenizer (``opspilot.tools.tokens``); an over-limit view
# is refused, never truncated (docs/tasks/2026-09-29-m1-01-view-bytes-timeout.md).
# This replaces B1's 100 KiB byte cap, which was "about 25k tokens" only at
# the estimator's 4 bytes/token; measured on DeepSeek the same views run
# 2.3-3.1 bytes/token, i.e. 33k-44k tokens.
MAX_VIEW_TOKENS = 25_000
MAX_DETAIL_FIELDS_PER_SPAN = 4
MAX_DETAIL_BYTES_PER_FIELD = 600
MAX_OPERATION_CHARS = 150
ERROR_BODY_BYTES = 8192

_DETAIL_KEYS = frozenset(
    {
        "error_description",
        "error.description",
        "otel.status_description",
        "otel.status_message",
        "exception.type",
        "exception.message",
        "exception.stacktrace",
        "grpc.error_message",
        "grpc.error_name",
    }
)
_STATUS_KEYS = ("error", "otel.status_code", "rpc.grpc.status_code", "http.status_code")
_DURATION = re.compile(r"[1-9][0-9]*[smh]")
_DURATIONS = {"s": 1, "m": 60, "h": 3600}

TOOL_SCHEMAS: tuple[Mapping[str, Any], ...] = (
    {
        "type": "function",
        "function": {
            "name": METRICS_TOOL,
            "description": (
                "Evaluate one PromQL range query against the registered "
                "target's Prometheus inside a query window you may choose "
                "within the authorized window. "
                "The result is the raw response series: data.result holds "
                "one entry per label set with [timestamp, value] points. "
                "Counters such as *_calls_total are cumulative. "
                "Wrap a counter in rate() or increase() for per-window "
                "activity. "
                "A range selector may be at most the query window length. "
                "A point at time t with range selector [r] covers [t - r, t]. "
                "A subquery (inner)[a:b] covers a plus what inner covers, "
                "and that total may be at most the query window length. "
                "Every point covers samples inside the query window only. "
                "No point reads samples from before the query window. "
                "The expression's total lookback is its longest range "
                "selector, or a subquery range plus its inner lookback. "
                "The first point is at the query window start plus the "
                "total lookback. "
                "The view reports the total lookback as lookback_seconds "
                "and the earliest instant read as lookback_start_at. "
                "source_start_at is the first returned sample inside the "
                "query window. "
                "A whole-window total, maximum or minimum comes from one "
                "evaluation of a range selector equal to the query window's "
                "length in seconds, written as increase(x[Ws]), "
                "max_over_time(x[Ws]) or min_over_time(x[Ws]) where W is "
                "that many seconds. "
                "Do not compute totals, maxima or minima from the point list "
                "yourself. "
                "A range selector longer than the query window is refused. "
                "offset is refused. "
                "@ is refused. "
                "A result whose whole view exceeds 25,000 tokens is refused "
                "with RESULT_TOO_LARGE and no series are returned; narrow "
                "the query window, add label filters or aggregate. "
                "A series that is not returned is unknown, not zero. "
                "A returned series does not by itself prove the service is "
                "healthy or unhealthy. "
                "Available metrics include "
                "traces_span_metrics_calls_total{service_name,span_name,"
                "status_code}, rpc_client_duration_milliseconds_count"
                "{service_name,rpc_service,rpc_method,rpc_grpc_status_code}, "
                "rpc_server_duration_milliseconds_bucket, "
                "app_payment_transactions_total, "
                "http_server_request_duration_seconds_count."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "expr": {
                        "type": "string",
                        "description": (
                            "The PromQL expression to evaluate; label values "
                            "come from the service names listed for "
                            "traces_search."
                        ),
                    },
                    "step_seconds": {
                        "type": "integer",
                        "description": (
                            "Resolution between returned points, 15 to the "
                            "query window length; omitted means 30."
                        ),
                    },
                    "start": {
                        "type": "string",
                        "description": (
                            "Absolute ISO-8601 start of the query window. "
                            "Give both start and end, or neither. "
                            "Must lie inside the authorized window; a query "
                            "window outside it is refused and the refusal "
                            "states the authorized window."
                        ),
                    },
                    "end": {
                        "type": "string",
                        "description": (
                            "Absolute ISO-8601 end of the query window. "
                            "Give both start and end, or neither. "
                            "Omitted with start also omitted, the query "
                            "window defaults to the 1 hour ending at the "
                            "authorized window's end."
                        ),
                    },
                },
                "required": ["expr"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": TRACES_TOOL,
            "description": (
                "Search the registered target's Jaeger for traces of one "
                "service inside a query window you may choose within the "
                "authorized window, and return a projected sample of their "
                "spans: spans of the requested service first, then spans "
                "with an error status tag, then the longest, each with "
                "trace_id, span_id, service, operation, start_us, "
                "duration_us, status tags, parent references and up to 4 "
                "clipped error detail fields. A result whose whole view exceeds "
                "25,000 tokens is refused with RESULT_TOO_LARGE and no spans "
                "are returned; narrow the time window or lower limit. "
                "traces_requested is the number of traces asked of the "
                "backend. "
                "backend_traces_returned is the number of traces the backend "
                "returned. "
                "backend_spans_returned is the number of spans in those "
                "traces. "
                "spans_shown is the number of span rows in this view. "
                "spans_omitted is the number of backend spans not shown "
                "because the adapter sampled them out. "
                "span_groups summarizes the shown spans by (service, "
                "operation): rows, status counts, error_rows and "
                "duration_us_min/max, computed from every span in spans_shown. "
                "incomplete_reason states why incomplete is true, or is null. "
                "status_state is recorded when the row has at least one "
                "status tag and not_recorded when it has none. "
                "not_recorded means the span carries no status tag, which is "
                "Unset in OTel terms, not status code 0 and not OK. "
                "This is a biased sample, not the complete "
                "trace graph or a failure rate: an omitted span is unknown, "
                "an error detail applies only to the span it is shown on, and "
                "an empty result means no trace of that service was returned "
                "for the query window, not that the service made no calls. "
                'Available services: ["accounting", "ad", "cart", '
                '"checkout", "currency", "email", "fraud-detection", '
                '"frontend", "frontend-proxy", "image-provider", '
                '"kafka", "load-generator", "payment", '
                '"product-catalog", "quote", "recommendation", '
                '"shipping"]; any other value returns an error.'
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {
                        "type": "string",
                        "description": "Exact value from the available services list.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            "How many traces to ask the backend for, at "
                            "least 1; omitted means 20."
                        ),
                    },
                    "start": {
                        "type": "string",
                        "description": (
                            "Absolute ISO-8601 start of the query window. "
                            "Give both start and end, or neither. "
                            "Must lie inside the authorized window; a query "
                            "window outside it is refused and the refusal "
                            "states the authorized window."
                        ),
                    },
                    "end": {
                        "type": "string",
                        "description": (
                            "Absolute ISO-8601 end of the query window. "
                            "Give both start and end, or neither. "
                            "Omitted with start also omitted, the query "
                            "window defaults to the 1 hour ending at the "
                            "authorized window's end."
                        ),
                    },
                },
                "required": ["service"],
            },
        },
    },
)


def _registrations() -> tuple[ToolRegistration, ToolRegistration]:
    metrics = ToolRegistration(
        name=METRICS_TOOL,
        version="v1",
        source=SOURCE,
        verb="query",
        description=ToolDescription(
            returns=(
                "The exact Prometheus range-query response of the registered "
                "target: data.result holds one series per label set with "
                "[timestamp, value] points at the requested step."
            ),
            window_format=(
                "The authorized absolute query window, appended as {window}; "
                "start/end/step are bound by the gateway, never by the query. "
                "start/end tool parameters (batch B) pick a query window "
                "inside it; omitted, the query window defaults to the 1 "
                "hour ending at the authorized window's end, and a chosen "
                "query window outside the authorized window is refused."
            ),
            values_format=(
                "The authorized target and service enumeration for this Run "
                "is appended as {values}. "
                "offset is refused. "
                "@ is refused. "
                "A range selector longer than the query window is refused. "
                "A range selector up to the query window length moves the "
                "first evaluated point forward by its length, so every "
                "point reads samples inside the query window only."
            ),
            limits=(
                "The whole view is limited to the registered max_view_tokens "
                "(25,000 DeepSeek tokens): a larger result is refused with "
                "RESULT_TOO_LARGE and no series, never truncated or "
                "summarized."
            ),
            cannot_prove=(
                "A returned series does not prove the service is healthy or "
                "unhealthy, and a series that is not returned is unknown, not "
                "zero; cumulative counters are not rates; a per-step point is "
                "not a whole-window total, maximum or minimum."
            ),
        ),
        may_contain_secrets=False,
        parameters={
            "expr": ParameterSpec(
                "string",
                required=True,
                description="The PromQL expression to evaluate over the query window.",
            ),
            "step_seconds": ParameterSpec(
                "integer",
                description=(
                    "Resolution between returned points, 15 to the query window length."
                ),
            ),
            # B2: model-chosen sub-window, validated by the transport
            # (``_query_window``) against the authorized frame carried as
            # ``TransportRequest.window`` -- not a gateway-level ceiling, so
            # ``max_window_seconds`` below still bounds only the frame.
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
                    "Absolute ISO-8601 end of the query window; give both "
                    "start and end or neither."
                ),
            ),
        },
        result_path=("data", "result"),
        request_timeout_seconds=30.0,
        max_result_bytes=1024 * 1024,
        max_view_tokens=MAX_VIEW_TOKENS,
        # B2: raised from 3600 to accommodate ``OBSERVATION_SECONDS``'s new
        # 24 h authorization frame, which the loop always carries as the
        # top-level ``ToolRequest.window`` (``_run_tools`` ->
        # ``request.scope.window``) regardless of the model's own, narrower
        # ``start``/``end`` selection -- the per-call volume limit that used
        # to live here moved into the transport's frame-containment check.
        max_window_seconds=OBSERVATION_SECONDS,
        # ``503`` is nominal: 5xx raises ``TransportUnavailable`` inside the
        # transport before classification. The rest are the transport's own
        # pre-dispatch refusal codes.
        error_classes={
            "400": "INVALID_PARAMS",
            "422": "INVALID_PARAMS",
            "503": "SOURCE_UNAVAILABLE",
            "QUERY_OUT_OF_WINDOW": "INVALID_PARAMS",
            "INVALID_STEP": "INVALID_PARAMS",
        },
        incomplete_marker="warnings",
    )
    traces = ToolRegistration(
        name=TRACES_TOOL,
        version="v1",
        source=SOURCE,
        verb="query",
        description=ToolDescription(
            returns=(
                "A projected observation record over the Jaeger trace search "
                "for one service: sampled spans under data.sampled_spans, each "
                "with status_state recorded or not_recorded, backend "
                "trace/span counts per service, and the SHA-256 and byte "
                "count of the wire response it was projected from; the view "
                "adds traces_requested, backend_traces_returned, "
                "backend_spans_returned, spans_shown, spans_omitted and "
                "incomplete_reason."
            ),
            window_format=(
                "The authorized absolute query window, appended as {window}; "
                "the search start/end are bound by the gateway. "
                "start/end tool parameters (batch B) pick a query window "
                "inside it; omitted, the query window defaults to the 1 "
                "hour ending at the authorized window's end, and a chosen "
                "query window outside the authorized window is refused."
            ),
            values_format=(
                "The authorized service enumeration for this Run, appended as "
                "{values}; any other service returns an error."
            ),
            limits=(
                "No cap on traces requested or spans sampled (requested "
                "service first, error status first, longest first); the "
                "whole view is limited to the registered max_view_tokens "
                "(25,000 DeepSeek tokens): a larger result is refused with "
                "RESULT_TOO_LARGE and no spans, never truncated."
            ),
            cannot_prove=(
                "A biased sample, not the complete trace graph or a failure "
                "rate: an omitted span is unknown, an error detail applies only "
                "to the span it is shown on, and an empty result is not proof "
                "that the service made no calls."
            ),
        ),
        may_contain_secrets=False,
        parameters={
            "service": ParameterSpec(
                "string",
                required=True,
                description="Exact value from the available services list.",
            ),
            "limit": ParameterSpec(
                "integer",
                description="How many traces to ask the backend for, at least 1.",
            ),
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
                    "Absolute ISO-8601 end of the query window; give both "
                    "start and end or neither."
                ),
            ),
        },
        result_path=("data", "sampled_spans"),
        request_timeout_seconds=30.0,
        # Raised from 256 KiB alongside B1's row-cap removal (not itself
        # part of the contract's numbers): the projected record's raw JSON
        # -- unlike the view -- is not row-capped, so a large ``limit`` on a
        # busy trace search can now project past the old ceiling before the
        # view's own token limit ever applies. Matched to the
        # metrics tool's own raw-body ceiling rather than left tight.
        max_result_bytes=1024 * 1024,
        max_view_tokens=MAX_VIEW_TOKENS,
        # B2: see the metrics registration's comment; raised for the same
        # reason (accommodates ``OBSERVATION_SECONDS``'s new 24 h frame).
        max_window_seconds=OBSERVATION_SECONDS,
        error_classes={
            "400": "INVALID_PARAMS",
            "503": "SOURCE_UNAVAILABLE",
            "SERVICE_NOT_AVAILABLE": "INVALID_PARAMS",
            "INVALID_LIMIT": "INVALID_PARAMS",
            "QUERY_OUT_OF_WINDOW": "INVALID_PARAMS",
        },
        incomplete_marker="incomplete",
    )
    return metrics, traces


def _tool_schema_revision() -> str:
    """C3 §5: a content hash of the template bytes, never a hand-kept number.

    Covers the model-visible tools array, the registry fingerprint (both
    description faces, parameters, limits, error classes) and the trace
    projection id. Endpoints are instance data and stay out.
    """
    digest = canonical_hash(
        {
            "tool_schemas": [dict(item) for item in TOOL_SCHEMAS],
            "registry": ToolRegistry(_registrations()).revision,
            "trace_projection": TRACE_PROJECTION,
        }
    )
    return f"otel-demo-{digest[:12]}"


TOOL_SCHEMA_REVISION = _tool_schema_revision()


# -- configuration -------------------------------------------------------


def _clean_url(value: str, name: str) -> str:
    parts = urlsplit(value)
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or "@" in parts.netloc
        or parts.query
        or parts.fragment
    ):
        raise ToolContractError(f"INVALID_{name}_URL")
    return value.rstrip("/")


@dataclass(frozen=True)
class OtelDemoConfig:
    """Where the lab backends are, and the one credential slot.

    ``token`` is the value behind ``CREDENTIAL_REF``; ``None`` means the
    backend is anonymous (the lab). It is held here only to hand to the
    transport and is never rendered.
    """

    prometheus_url: str = "http://127.0.0.1:19090"
    jaeger_url: str = "http://127.0.0.1:16686/jaeger/ui"
    token: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "prometheus_url", _clean_url(self.prometheus_url, "PROMETHEUS")
        )
        object.__setattr__(self, "jaeger_url", _clean_url(self.jaeger_url, "JAEGER"))
        if self.token is not None and not isinstance(self.token, str):
            raise ToolContractError("INVALID_CREDENTIAL")

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> OtelDemoConfig:
        return cls(
            prometheus_url=env.get("OPSPILOT_OTEL_PROMETHEUS_URL")
            or cls.prometheus_url,
            jaeger_url=env.get("OPSPILOT_OTEL_JAEGER_URL") or cls.jaeger_url,
            token=env.get("OPSPILOT_OTEL_TOKEN") or None,
        )


def _target(config: OtelDemoConfig) -> RegisteredTarget:
    # One target, two backends: the registered endpoint is Prometheus and
    # the trace backend travels in the selector, so both are fingerprinted
    # in the target registry revision and reach the transport through the
    # request alone. ``credential_ref`` names the slot the transport resolves.
    return RegisteredTarget(
        target_id=TARGET_ID,
        source=SOURCE,
        endpoint=config.prometheus_url,
        credential_ref=CREDENTIAL_REF,
        selector={
            "opspilot.integration.id": TARGET_ID,
            "traces_endpoint": config.jaeger_url,
        },
        display_name="OTel Demo 2.0.2 lab",
    )


# -- model-visible face ---------------------------------------------------


def _evidence_context(run_id: str, window: Window) -> Mapping[str, Any]:
    return {
        "type": "opspilot-evidence-context-v4",
        "run_id": run_id,
        "time_policies": [
            {
                "id": _TIME_POLICY,
                "mode": "historical_window",
                "all_authorized_targets": True,
                "window": window.as_json(),
                "reference_rule": "response_received_at",
            }
        ],
    }


def otel_demo_face(clock: Clock) -> ToolFace:
    """The face the workbench records: the window is fixed at submission.

    ``clock`` is the trusted clock of the process recording the input (the
    database clock in the workbench); the window is the ``OBSERVATION_SECONDS``
    ending now, rounded down to whole seconds so the recorded interval is
    what the transport sends.
    """

    def evidence_context(run_id: str) -> Mapping[str, Any]:
        end = clock.now().astimezone(timezone.utc).replace(microsecond=0)
        window = Window(end - timedelta(seconds=OBSERVATION_SECONDS), end)
        return _evidence_context(run_id, window)

    return ToolFace(
        tool_schemas=TOOL_SCHEMAS,
        variant_id=DISCIPLINE_VARIANT,
        evidence_context=evidence_context,
    )


def otel_demo_versions() -> dict[str, str]:
    return {**investigation_versions(), "tool_schema_revision": TOOL_SCHEMA_REVISION}


def scope_window(input: InvestigationInput) -> Window:
    """The window this Run was authorized for, from its own recorded input.

    Every attempt reads the interval the workbench fixed at submission; a
    Run whose input carries no usable policy window cannot be scoped and is
    blocked (``ContextError``), never scoped from the clock.
    """
    context = input.evidence_context
    policies = context.get("time_policies") if isinstance(context, Mapping) else None
    if isinstance(policies, Sequence):
        for policy in policies:
            if isinstance(policy, Mapping) and policy.get("id") == _TIME_POLICY:
                window = Window.parse(policy.get("window"))
                if window is not None:
                    return window
    raise ContextError("SCOPE_WINDOW_MISSING")


# -- control ---------------------------------------------------------------


class DurableControl:
    """``ControlAuthority`` over the durable store: what a human decided.

    The subject generation and both suspension layers are read in one
    snapshot; a store that cannot answer raises ``ControlUnavailable`` and
    the executor refuses the call.
    """

    def __init__(self, store: DurableStore) -> None:
        self._store = store

    def snapshot(self, scope: QueryScope) -> ControlSnapshot:
        try:
            state = self._store.control_state(UUID(scope.subject_id))
        except (PersistenceError, ValueError) as exc:
            raise ControlUnavailable(str(exc)) from exc
        return ControlSnapshot(
            control_generation=int(state["incident_generation"]),
            global_suspension_generation=int(state["global_generation"]),
            target_suspension_generation=int(state["target_generation"]),
            suspended=bool(state["global_suspended"] or state["target_suspended"]),
        )


# -- transport -------------------------------------------------------------


def promql_problem(expr: str, window_seconds: float) -> str | None:
    """Why this PromQL must not be sent, or ``None`` (M0 ``read_proxy`` rules).

    ``offset`` and ``@`` move the evaluation outside the window and an
    expression whose one-point read (``_promql_lookback``: range selectors,
    nested subqueries added up) is longer than the window is refused; a
    read up to the window length is accepted and ``_metrics`` starts the
    evaluation that far after the window start, so no point reads before it
    (Prometheus' staleness lookback for instant selectors aside) -- the
    returned sample timestamps are what the executor checks against the
    scope. Any ``[...]`` that is not ``<n>[smh]`` is refused too (a label
    regex such as ``[a-z]+`` is over-refused, safely).
    """
    if not expr or len(expr) > MAX_PROMQL_CHARS:
        return "QUERY_OUT_OF_WINDOW"
    # PromQL keywords are case-insensitive (``OFFSET`` parses), so the word
    # is matched case-folded (fresh-context contract test finding).
    if "@" in expr or re.search(r"\boffset\b", expr, re.IGNORECASE):
        return "QUERY_OUT_OF_WINDOW"
    lookback = _promql_lookback(expr, window_seconds)
    if lookback is None or lookback > window_seconds:
        return "QUERY_OUT_OF_WINDOW"
    return None


def _promql_lookback(expr: str, window_seconds: float) -> int | None:
    """Total seconds one evaluation point of ``expr`` reads back, or ``None``.

    A range selector ``x[r]`` reads ``r``; a subquery ``(<inner>)[a:b]``
    reads ``a`` plus what one point of ``inner`` reads, so nesting adds up
    (independent review P2: ``max_over_time(rate(x[1m])[5m:30s])`` reads
    360 s, not 300). Parallel selectors take the maximum. ``None`` when a
    ``[...]`` is not ``<n>[smh]`` parts, a part alone exceeds the window
    (``m[5m:6m]`` stays refused), or a ``)`` has no ``(`` -- all refused by
    ``promql_problem``.

    The walk is a bracket-depth scan, not a PromQL parser: a selector
    applies to the operand just before it, which is a parenthesised group
    (its own max lookback) or a bare selector/metric name (0).
    """
    frames: list[int] = [0]  # max lookback seen inside each open ``(``
    last = 0  # lookback of the operand a following ``[...]`` applies to
    index = 0
    while index < len(expr):
        char = expr[index]
        if char == "(":
            frames.append(0)
            last = 0
        elif char == ")":
            if len(frames) == 1:
                return None
            last = frames.pop()
            frames[-1] = max(frames[-1], last)
        elif char == "[":
            close = expr.find("]", index)
            if close < 0:
                return None
            parts = [part for part in expr[index + 1 : close].split(":") if part]
            if not parts:
                return None
            seconds: list[int] = []
            for part in parts:
                if not _DURATION.fullmatch(part):
                    return None
                seconds.append(int(part[:-1]) * _DURATIONS[part[-1]])
                if seconds[-1] > window_seconds:
                    return None
            last = seconds[0] + last
            frames[-1] = max(frames[-1], last)
            index = close
        elif not char.isspace():
            last = 0
        index += 1
    return max(frames)


def promql_lookback_seconds(expr: str) -> int:
    """How far each evaluated point of an accepted expression reads back, in seconds.

    ``_promql_lookback`` with an unbounded window: the total of nested range
    and subquery selectors, the maximum over parallel ones; 0 when there is
    none. ``_metrics`` starts the range query this far after the window
    start so the first point's read begins at the window start. Only
    meaningful for an expression ``promql_problem`` accepted, which used the
    same computation, so the value is at most the window length. Prometheus'
    staleness lookback for instant selectors is not counted: it is a server
    default the query does not state.
    """
    return _promql_lookback(expr, float("inf")) or 0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


@dataclass(frozen=True)
class _Fetched:
    status: int
    body: bytes


class OtelDemoTransport:
    """The one outbound seam: GET only, no proxy, no redirects, bounded read.

    ``credentials`` maps ``credential_ref`` to a bearer token or ``None``;
    an unknown ref is refused (``TransportError``), never sent anonymously.
    """

    def __init__(
        self,
        *,
        credentials: Mapping[str, str | None],
        opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        self._credentials = dict(credentials)
        self._opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )

    def fetch(self, request: TransportRequest) -> TransportResponse:
        if request.credential_ref not in self._credentials:
            raise TransportError("CREDENTIAL_REF_UNKNOWN")
        if request.tool == METRICS_TOOL:
            return self._metrics(request)
        if request.tool == TRACES_TOOL:
            return self._traces(request)
        raise TransportError("TOOL_NOT_SUPPORTED")

    # -- HTTP --------------------------------------------------------------

    def _get(self, url: str, request: TransportRequest, *, max_bytes: int) -> _Fetched:
        headers = {"Accept": "application/json"}
        token = self._credentials[request.credential_ref]
        if token:
            headers["Authorization"] = f"Bearer {token}"
        http_request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            response: HTTPResponse
            with self._opener.open(
                http_request, timeout=request.timeout_seconds
            ) as response:
                body = response.read(max_bytes + 1)
                status = int(response.status)
        except urllib.error.HTTPError as error:
            body = error.read(ERROR_BODY_BYTES)
            code = int(error.code)
            if code >= 500:
                raise TransportUnavailable(str(code)) from None
            return _Fetched(code, body)
        except (TimeoutError, socket.timeout):
            raise TransportTimeout("TIMEOUT") from None
        except urllib.error.URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                raise TransportTimeout("TIMEOUT") from None
            raise TransportUnavailable("UNREACHABLE") from None
        except OSError:
            raise TransportUnavailable("UNREACHABLE") from None
        if len(body) > max_bytes:
            raise TransportResultTooLarge(str(max_bytes))
        return _Fetched(status, body)

    # -- metrics ------------------------------------------------------------

    def _metrics(self, request: TransportRequest) -> TransportResponse:
        expr = request.params.get("expr")
        if not isinstance(expr, str):
            return _refused("QUERY_OUT_OF_WINDOW")
        window, refusal = _query_window(request)
        if refusal is not None:
            return refusal
        assert window is not None
        problem = promql_problem(expr, window.seconds)
        if problem is not None:
            return _refused(problem)
        step = request.params.get("step_seconds", DEFAULT_STEP_SECONDS)
        if (
            type(step) is not int
            or step < MIN_STEP_SECONDS
            or step > int(window.seconds)
        ):
            return _refused("INVALID_STEP")
        # Evaluate from window start + longest range selector, so every
        # point's ``[t - range, t]`` lies inside the window; a window-length
        # selector gives start == end, one point: the whole-window aggregate
        # (v4 rerun, 3 of 4 Runs reported the pre-window first point as
        # in-window).
        lookback = promql_lookback_seconds(expr)
        start = window.start + timedelta(seconds=lookback)
        url = f"{request.endpoint}/api/v1/query_range?" + urllib.parse.urlencode(
            {
                "query": expr,
                "start": f"{start.timestamp():.3f}",
                "end": f"{window.end.timestamp():.3f}",
                "step": str(step),
                "timeout": f"{int(request.timeout_seconds)}s",
            }
        )
        fetched = self._get(url, request, max_bytes=request.max_result_bytes)
        if fetched.status != 200:
            return TransportResponse(
                body=fetched.body, source_status=str(fetched.status)
            )
        start_at, end_at = _prometheus_bounds(fetched.body)
        return TransportResponse(
            body=fetched.body,
            data_as_of=end_at,
            source_start_at=start_at,
            source_end_at=end_at,
            lookback_seconds=lookback,
            # B2 review disposition P1: the view must record the window
            # actually queried, not the wider authorized frame.
            query_window=window,
            view_fields=(
                {"series_note": SERIES_NOTE}
                if "traces_span_metrics_calls_total" in expr
                else None
            ),
        )

    # -- traces -------------------------------------------------------------

    def _traces(self, request: TransportRequest) -> TransportResponse:
        service = request.params.get("service")
        if not isinstance(service, str) or service not in SERVICES:
            return _refused("SERVICE_NOT_AVAILABLE")
        # B1: no more upper bound (was ``MAX_TRACE_LIMIT = 20``); the view's
        # token limit (``MAX_VIEW_TOKENS``) is what now bounds a large
        # request's view, by refusing it.
        limit = request.params.get("limit", DEFAULT_TRACE_LIMIT)
        if type(limit) is not int or limit < 1:
            return _refused("INVALID_LIMIT")
        window, refusal = _query_window(request)
        if refusal is not None:
            return refusal
        assert window is not None
        base = request.selector.get("traces_endpoint", "")
        if not base:
            raise TransportError("TRACES_ENDPOINT_MISSING")
        url = f"{base}/api/traces?" + urllib.parse.urlencode(
            {
                "service": service,
                "start": int(window.start.timestamp() * 1_000_000),
                "end": int(window.end.timestamp() * 1_000_000),
                "limit": limit,
            }
        )
        fetched = self._get(url, request, max_bytes=TRACE_SOURCE_READ_BYTES)
        if fetched.status != 200:
            return TransportResponse(
                body=fetched.body, source_status=str(fetched.status)
            )
        try:
            payload = json.loads(fetched.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            # Hand the executor bytes it will classify as MALFORMED_RESULT.
            return TransportResponse(body=fetched.body[: request.max_result_bytes])
        record, start_at, end_at = project_traces(
            payload,
            service=service,
            limit=limit,
            window=window,
            source_sha256=sha256(fetched.body).hexdigest(),
            source_bytes=len(fetched.body),
        )
        body = canonical(record).encode("utf-8")
        if len(body) > request.max_result_bytes:
            raise TransportResultTooLarge(str(request.max_result_bytes))
        return TransportResponse(
            body=body,
            data_as_of=end_at,
            source_start_at=start_at,
            source_end_at=end_at,
            # B2 review disposition P1: the view must record the window
            # actually searched, not the wider authorized frame.
            query_window=window,
            # Round 2 rule B: counts with their unit, from the record's own
            # fields; the executor adds spans_shown / spans_omitted.
            row_unit="spans",
            backend_rows_returned=record["backend_returned_span_count"],
            view_fields={
                "traces_requested": limit,
                "backend_traces_returned": record["backend_returned_trace_count"],
                "incomplete_reason": TRACES_INCOMPLETE_REASON
                if record["incomplete"]
                else None,
                # C3: span_groups summarizes the spans shown in content --
                # all of ``sampled_spans``, since a view is whole or refused.
                "span_groups": _span_groups(record["data"]["sampled_spans"]),
                "span_groups_note": (
                    "span_groups summarizes the spans shown in content; "
                    "spans the adapter sampled out are not counted in any "
                    "group."
                ),
            },
        )


def _query_window(
    request: TransportRequest,
) -> tuple[Window | None, TransportResponse | None]:
    """B2: the model's per-call ``start``/``end`` params, validated against
    the runner's authorized frame (``request.window`` -- the top-level
    window the loop always sets to the full authorized frame; see
    ``opspilot.investigation.loop._run_tools``). Returns ``(window, None)``
    on success, or ``(None, refusal)`` when the pair is malformed or the
    chosen window is not fully inside the frame.

    Upstream default when both are omitted (``holmes/plugins/toolsets/utils
    .py:111-113``): the hour ending at the frame's end, clamped forward to
    the frame's own start so a frame narrower than an hour (a test fixture,
    never a real Run post-B2) is unaffected.
    """
    frame = request.window
    start_text = request.params.get("start")
    end_text = request.params.get("end")
    if start_text is None and end_text is None:
        end = frame.end
        start = max(frame.start, end - timedelta(hours=1))
        return Window(start, end), None
    if not isinstance(start_text, str) or not isinstance(end_text, str):
        return None, _refused("QUERY_OUT_OF_WINDOW")
    window = Window.parse({"start": start_text, "end": end_text})
    if window is None:
        return None, _refused("QUERY_OUT_OF_WINDOW")
    if not frame.contains(window):
        # The refusal states the frame so the model can retry inside it
        # (PRODUCT-CONSTRAINTS "Query scope ... constrained").
        return None, _refused("QUERY_OUT_OF_WINDOW", frame=frame)
    return window, None


def _refused(code: str, *, frame: Window | None = None) -> TransportResponse:
    """A fixed-code refusal decided before any request went out.

    Carried as ``source_status`` so the registration's ``error_classes``
    classify it as ``INVALID_PARAMS``, with ``sent=False`` so the executor
    audits it as never dispatched and with no source contact. ``frame``
    (B2), when given, states the authorized window in the body so a
    caller reading the raw ``TransportResponse`` (not just the eventual
    model view, which already carries the same bound generically via
    ``operation.window``) can discover it.
    """
    body: dict[str, Any] = {"error": code}
    if frame is not None:
        body["authorized_window"] = frame.as_json()
    return TransportResponse(
        body=canonical(body).encode(), source_status=code, sent=False
    )


def _utc(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def _prometheus_bounds(body: bytes) -> tuple[datetime | None, datetime | None]:
    """The first and last sample timestamps across every returned series."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, None
    data = payload.get("data") if isinstance(payload, Mapping) else None
    result = data.get("result") if isinstance(data, Mapping) else None
    if not isinstance(result, list):
        return None, None
    stamps: list[float] = []
    for series in result:
        if not isinstance(series, Mapping):
            continue
        values = series.get("values")
        if isinstance(values, list):
            for point in values:
                if (
                    isinstance(point, list)
                    and point
                    and isinstance(point[0], (int, float))
                ):
                    stamps.append(float(point[0]))
        value = series.get("value")
        if isinstance(value, list) and value and isinstance(value[0], (int, float)):
            stamps.append(float(value[0]))
    if not stamps:
        return None, None
    return _utc(min(stamps)), _utc(max(stamps))


# -- trace projection (M0 trace_view v3, ported) -------------------------


def _details(span: Mapping[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for tag in span.get("tags") or []:
        if isinstance(tag, Mapping) and tag.get("key") in _DETAIL_KEYS:
            result.append(
                {"location": "span_tag", "key": tag["key"], "value": tag.get("value")}
            )
    for log in span.get("logs") or []:
        if not isinstance(log, Mapping):
            continue
        for entry in log.get("fields") or []:
            if isinstance(entry, Mapping) and entry.get("key") in _DETAIL_KEYS:
                result.append(
                    {
                        "location": "span_log",
                        "key": entry["key"],
                        "value": entry.get("value"),
                        "timestamp_us": log.get("timestamp"),
                    }
                )
    return result


def _clipped(field: Mapping[str, Any]) -> dict[str, Any]:
    value = field["value"]
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    raw = text.encode("utf-8")
    return {
        **field,
        "value": raw[:MAX_DETAIL_BYTES_PER_FIELD].decode("utf-8", errors="ignore"),
        "original_utf8_bytes": len(raw),
        "value_truncated": len(raw) > MAX_DETAIL_BYTES_PER_FIELD,
    }


def project_traces(
    payload: object,
    *,
    service: str,
    limit: int,
    window: Window,
    source_sha256: str,
    source_bytes: int,
) -> tuple[dict[str, Any], datetime | None, datetime | None]:
    """Project a Jaeger ``/api/traces`` response into the observation record.

    Returns the record and the source interval it covers: the earliest span
    start and latest span end over *all* returned spans, clipped to the query
    window (Jaeger returns whole traces, whose spans may run past either
    bound; ``spans_outside_window`` counts the ones that do).
    """
    traces = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(traces, list):
        traces = []
    spans: list[dict[str, Any]] = []
    counts: dict[str, dict[str, int]] = {}
    raw_fields = 0
    raw_detail_spans = 0
    outside = 0
    window_start_us = int(window.start.timestamp() * 1_000_000)
    window_end_us = int(window.end.timestamp() * 1_000_000)
    stamps: list[int] = []
    for trace in traces:
        if not isinstance(trace, Mapping):
            continue
        processes = trace.get("processes") or {}
        for span in trace.get("spans") or []:
            if not isinstance(span, Mapping):
                continue
            process = (
                processes.get(span.get("processID"), {})
                if isinstance(processes, Mapping)
                else {}
            )
            span_service = str(process.get("serviceName", "unknown"))
            tags = {
                t.get("key"): t.get("value")
                for t in span.get("tags") or []
                if isinstance(t, Mapping)
            }
            process_tags = {
                t.get("key"): t.get("value")
                for t in process.get("tags") or []
                if isinstance(t, Mapping)
            }
            error = (
                tags.get("error") is True
                or tags.get("otel.status_code") == "ERROR"
                or str(tags.get("rpc.grpc.status_code", "0")) not in {"0", "None"}
                or str(tags.get("http.status_code", "0")).startswith("5")
            )
            fields = _details(span)
            raw_fields += len(fields)
            raw_detail_spans += bool(fields)
            count = counts.setdefault(
                span_service,
                {
                    "span_count": 0,
                    "error_spans_by_listed_status_tags": 0,
                    "max_duration_us": 0,
                },
            )
            count["span_count"] += 1
            count["error_spans_by_listed_status_tags"] += int(error)
            duration = span.get("duration")
            duration_us = int(duration) if isinstance(duration, int) else 0
            count["max_duration_us"] = max(count["max_duration_us"], duration_us)
            start = span.get("startTime")
            if isinstance(start, int):
                stamps.append(start)
                stamps.append(start + duration_us)
                if start < window_start_us or start + duration_us > window_end_us:
                    outside += 1
            operation = str(span.get("operationName", ""))
            spans.append(
                {
                    "trace_id": trace.get("traceID"),
                    "span_id": span.get("spanID"),
                    "service": span_service,
                    "service_version": process_tags.get("service.version"),
                    "operation": operation[:MAX_OPERATION_CHARS],
                    "operation_truncated": len(operation) > MAX_OPERATION_CHARS,
                    "start_us": start,
                    "duration_us": span.get("duration"),
                    "error_by_visible_tags": error,
                    "status_tags": {k: tags[k] for k in _STATUS_KEYS if k in tags},
                    # Round 2 rule A: a span with no status key is Unset in
                    # OTel terms, not status 0 / OK; say so instead of leaving
                    # an empty mapping to be read as "no error".
                    "status_state": "recorded"
                    if any(k in tags for k in _STATUS_KEYS)
                    else "not_recorded",
                    "parent_references": [
                        {k: ref.get(k) for k in ("refType", "traceID", "spanID")}
                        for ref in span.get("references") or []
                        if isinstance(ref, Mapping)
                    ],
                    "error_details": [
                        _clipped(f) for f in fields[:MAX_DETAIL_FIELDS_PER_SPAN]
                    ],
                    "raw_error_detail_field_count": len(fields),
                    "omitted_error_detail_field_count": max(
                        0, len(fields) - MAX_DETAIL_FIELDS_PER_SPAN
                    ),
                }
            )
    spans.sort(
        key=lambda s: (
            s["service"] != service,
            not s["error_by_visible_tags"],
            -float(s["duration_us"] or 0),
            str(s["trace_id"]),
            str(s["span_id"]),
        )
    )
    # B1: no more row cap here (was ``spans[:MAX_SAMPLED_SPANS]``, 20) --
    # every span from the returned traces is sampled and sorted; the view's
    # token limit refuses an over-large result whole rather than dropping
    # rows.
    sampled = spans
    # Judged over the sampled set, which is exactly what the view shows.
    visible_keys = {(s["trace_id"], s["span_id"]) for s in sampled}
    for span in sampled:
        for ref in span["parent_references"]:
            ref["parent_is_visible"] = (ref.get("traceID"), ref.get("spanID")) in (
                visible_keys
            )
    record: dict[str, Any] = {
        "source": "jaeger",
        "projection": TRACE_PROJECTION,
        "query": {"service": service, "limit": limit, "window": window.as_json()},
        "source_response_sha256": source_sha256,
        "source_response_bytes": source_bytes,
        "backend_returned_trace_count": len(traces),
        "backend_returned_span_count": len(spans),
        "backend_trace_ids": [
            t.get("traceID") for t in traces if isinstance(t, Mapping)
        ],
        "backend_counts_by_service": counts,
        "spans_outside_window": outside,
        "sampled_span_count": len(sampled),
        "omitted_span_count": len(spans) - len(sampled),
        "error_detail_coverage": {
            "covered_keys": sorted(_DETAIL_KEYS),
            "raw_detail_span_count": raw_detail_spans,
            "raw_detail_field_count": raw_fields,
            "visible_detail_field_count": sum(len(s["error_details"]) for s in sampled),
        },
        "limits": {
            "source_query_limit": limit,
            # B1: the fixed row cap this field once named is gone; there is
            # no longer a display-max-spans constant to report here.
            "detail_max_fields_per_span": MAX_DETAIL_FIELDS_PER_SPAN,
            "detail_max_utf8_bytes_per_field": MAX_DETAIL_BYTES_PER_FIELD,
        },
        "selection_policy": (
            "Requested service first, then listed error status, then longest "
            "duration with stable trace/span tie-break. Biased sample, not a "
            "complete graph or population failure rate."
        ),
        # Jaeger answered with as many traces as were asked for: more may
        # exist in the window.
        "incomplete": len(traces) >= limit,
        "data": {"sampled_spans": sampled},
    }
    if not stamps:
        return record, None, None
    start_us = max(min(stamps), window_start_us)
    end_us = min(max(stamps), window_end_us)
    if start_us > end_us:
        # Every span lies outside the window: nothing in-window is covered.
        return record, None, None
    return record, _utc(start_us / 1_000_000), _utc(end_us / 1_000_000)


# -- span_groups (C3, docs/tasks/2026-09-28-m1-01-alignment-c.md) -----------


def _span_status_key(row: Mapping[str, Any]) -> str:
    """One row's status, as a single grouping key.

    Ported from the replay-ablation experiment
    (``docs/evidence/m1-01-replay-ablation/scripts/span_groups.py``,
    ``status_key``) unchanged: every present ``status_tags`` entry, joined
    ``key=value`` in sorted-key order (a row's ``status_tags`` may legitimately
    carry more than one of the four status keys at once), or ``"not_recorded"``
    when the row has none. The contract's own literal example
    (``"rpc.grpc.status_code=0": 13``) is the single-key case, which this
    produces unchanged for numeric tag values.
    """
    tags = row.get("status_tags")
    if (
        row.get("status_state") == "not_recorded"
        or not isinstance(tags, Mapping)
        or not tags
    ):
        return "not_recorded"
    return ";".join(
        f"{k}={json.dumps(tags[k])}" if isinstance(tags[k], str) else f"{k}={tags[k]}"
        for k in sorted(tags)
    )


def _span_groups(rows: Sequence[object]) -> list[dict[str, Any]]:
    """Per-(service, operation) summary of the rows actually shown (C3).

    Algorithm ported from the replay-ablation experiment
    (``docs/evidence/m1-01-replay-ablation/scripts/span_groups.py``,
    ``span_groups``): grouped by ``(service, operation)``, each group giving
    ``rows``, ``status`` (a count per :func:`_span_status_key`),
    ``error_rows`` (``error_by_visible_tags`` true), and
    ``duration_us_min``/``duration_us_max`` over the group's own rows.

    One required difference from that script (the contract text, not an
    implementation choice): groups here are returned **sorted by
    ``(service, operation)``**, not in first-appearance order. The reference
    script groups in encounter order, which is a legitimate summary but not
    what C3 asks for (and, for a query whose requested service is not first
    alphabetically, gives a different group order than this function).

    Takes ``rows`` -- the view's own ``content``, every sampled span (a view
    is whole or refused, so nothing shown is ever a subset) -- so a row the
    adapter sampled out is never counted here either, matching the
    contract's "只汇总已展示行".
    """
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        key = (str(row.get("service")), str(row.get("operation")))
        group = groups.get(key)
        if group is None:
            group = groups[key] = {
                "service": key[0],
                "operation": key[1],
                "rows": 0,
                "status": {},
                "error_rows": 0,
                "duration_us_min": None,
                "duration_us_max": None,
            }
        group["rows"] += 1
        status = _span_status_key(row)
        group["status"][status] = group["status"].get(status, 0) + 1
        if row.get("error_by_visible_tags") is True:
            group["error_rows"] += 1
        duration = row.get("duration_us")
        if isinstance(duration, int):
            lo, hi = group["duration_us_min"], group["duration_us_max"]
            group["duration_us_min"] = duration if lo is None else min(lo, duration)
            group["duration_us_max"] = duration if hi is None else max(hi, duration)
    return [groups[key] for key in sorted(groups)]


# -- composition -------------------------------------------------------------


def otel_demo_executor_factory(
    store: DurableStore,
    *,
    evidence: EvidenceSink,
    clock: Clock,
    config: OtelDemoConfig | None = None,
    token_counter: TokenCounter | None = None,
) -> ExecutorFactory:
    """An ``ExecutorFactory`` for ``InvestigationRunner`` over this profile.

    The scope is issued per lease from the committed Run row (deadline,
    generations) and the Run's recorded input (window); the ledger, the
    evidence sink and the control source are the durable ones.
    """
    config = config or OtelDemoConfig()
    # Resolved when the factory is built, not per Run: a worker with no usable
    # tokenizer fails at startup (``TOKENIZER_UNAVAILABLE``), not mid-Run.
    counter = token_counter if token_counter is not None else default_counter()
    tools = ToolRegistry(_registrations())
    targets = TargetRegistry([_target(config)])
    transport = OtelDemoTransport(credentials={CREDENTIAL_REF: config.token})
    control = DurableControl(store)

    def factory(lease: Lease, input: InvestigationInput) -> ReadOnlyToolExecutor:
        run = store.rebuild(lease.incident_id)["run"]
        scope = QueryScope(
            scope_id=f"otel-demo:{lease.run_id}:{lease.epoch}",
            subject_kind="incident",
            subject_id=str(lease.incident_id),
            run_id=str(lease.run_id),
            control_generation=lease.control_generation,
            global_suspension_generation=lease.global_suspension_generation,
            target_suspension_generation=lease.target_suspension_generation,
            registry_revision=targets.revision,
            tool_registry_revision=tools.revision,
            target_ids=frozenset({TARGET_ID}),
            tool_names=frozenset({METRICS_TOOL, TRACES_TOOL}),
            window=scope_window(input),
            deadline=run["deadline"],
        )
        return ReadOnlyToolExecutor(
            scope=scope,
            tools=tools,
            targets=targets,
            transport=transport,
            evidence=evidence,
            control=control,
            clock=clock,
            ledger=DurableToolLedger(
                store,
                lease,
                max_operations=MAX_OPERATIONS_PER_RUN,
                max_tool_seconds=MAX_TOOL_SECONDS_PER_RUN,
            ),
            token_counter=counter,
        )

    return factory
