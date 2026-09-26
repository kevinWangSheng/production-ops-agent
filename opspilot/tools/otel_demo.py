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
* ``metrics_range_query``: a PromQL range query over the Run's absolute
  window. The raw evidence is the exact Prometheus response; the model view
  is its leading ``data.result`` series. A query that reads outside the
  window (``offset``, ``@``, a range selector longer than the window) is
  refused before any request goes out;
* ``traces_search``: Jaeger's trace search for one service in the window.
  Raw Jaeger responses are 300 KB-600 KB for 20 traces, so the raw evidence
  here is the transport's projected observation record -- the M0 trace
  view v3 sampling policy ported to product code -- which carries the
  SHA-256 and byte count of the wire response it was projected from. That
  is a weaker provenance than the metrics tool's exact bytes and is stated
  as such in the tool description.

The observation window is fixed per Run when the workbench records the
input (``otel_demo_face``): the 300 s ending at submission, as the frozen
v4 acceptance packet requires. The executor factory re-reads that window
from the Run's recorded input rather than trusting the model or the clock,
so every attempt of a Run reads the same interval.

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
    TransportRefused,
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

#: The v4 packet's per-Run observation window.
OBSERVATION_SECONDS = 300
DEFAULT_STEP_SECONDS = 30
MIN_STEP_SECONDS = 15
MAX_PROMQL_CHARS = 2000
DEFAULT_TRACE_LIMIT = 10
MAX_TRACE_LIMIT = 20
#: Jaeger's response for 20 traces has been 300-600 KB in this lab; the
#: transport stops reading past this and the operation fails as
#: ``RESULT_TOO_LARGE`` rather than projecting a partial body.
TRACE_SOURCE_READ_BYTES = 4 * 1024 * 1024
MAX_SAMPLED_SPANS = 20
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
_RANGE_SELECTOR = re.compile(r"\[([^\]]+)\]")
_DURATION = re.compile(r"[1-9][0-9]*[smh]")
_DURATIONS = {"s": 1, "m": 60, "h": 3600}

TOOL_SCHEMAS: tuple[Mapping[str, Any], ...] = (
    {
        "type": "function",
        "function": {
            "name": METRICS_TOOL,
            "description": (
                "Evaluate one PromQL range query against the registered "
                "target's Prometheus inside the authorized window and return "
                "the raw response series (data.result: one entry per label set "
                "with [timestamp, value] points). Counters such as "
                "*_calls_total are cumulative; wrap them in rate()/increase() "
                "for per-window activity. Range selectors, offset and @ must "
                "stay inside the window; a query that reads outside it returns "
                "an error. At most 24 KiB of series are shown; when truncated "
                "the view sets truncated true and reports omitted_rows. A "
                "series that is not returned is unknown, not zero, and a "
                "returned series does not by itself prove the service is "
                "healthy or unhealthy. Available metrics include "
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
                            "window length; omitted means 30."
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
                "service inside the authorized window and return a projected "
                "sample of their spans: spans of the requested service first, "
                "then spans with an error status tag, then the longest, up to "
                "20 spans in total, each with trace_id, span_id, service, "
                "operation, start_us, duration_us, status tags, parent "
                "references and up to 4 clipped error detail fields. At most "
                "16 KiB of sampled spans are shown; when truncated the view "
                "sets truncated true and reports omitted_rows. Backend trace "
                "and span counts are kept in the stored evidence record, not "
                "in this view. This is a biased sample, not the complete "
                "trace graph or a failure rate: an omitted span is unknown, "
                "an error detail applies only to the span it is shown on, and "
                "an empty result means no trace of that service was returned "
                "for the window, not that the service made no calls. "
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
                            "How many traces to ask the backend for, 1 to 20; "
                            "omitted means 10."
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
                "start/end/step are bound by the gateway, never by the query."
            ),
            values_format=(
                "The authorized target and service enumeration for this Run, "
                "appended as {values}; a query reading outside the window is "
                "refused."
            ),
            limits=(
                "Truncated at the registered max_view_bytes: whole leading "
                "series are kept, later series are dropped and counted in "
                "omitted_rows, never summarized."
            ),
            cannot_prove=(
                "A returned series does not prove the service is healthy or "
                "unhealthy, and a series that is not returned is unknown, not "
                "zero; cumulative counters are not rates."
            ),
        ),
        may_contain_secrets=False,
        parameters={
            "expr": ParameterSpec(
                "string",
                required=True,
                description="The PromQL expression to evaluate over the window.",
            ),
            "step_seconds": ParameterSpec(
                "integer",
                description=(
                    "Resolution between returned points, 15 to the window length."
                ),
            ),
        },
        result_path=("data", "result"),
        request_timeout_seconds=30.0,
        max_result_bytes=1024 * 1024,
        max_view_bytes=24 * 1024,
        max_window_seconds=3600,
        # 5xx never reaches classification (the transport raises
        # ``TransportUnavailable``) and pre-dispatch refusals raise
        # ``TransportRefused``; neither is a source status.
        error_classes={"400": "INVALID_PARAMS", "422": "INVALID_PARAMS"},
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
                "for one service: sampled spans under data.sampled_spans, "
                "backend trace/span counts per service, and the SHA-256 and "
                "byte count of the wire response it was projected from."
            ),
            window_format=(
                "The authorized absolute query window, appended as {window}; "
                "the search start/end are bound by the gateway."
            ),
            values_format=(
                "The authorized service enumeration for this Run, appended as "
                "{values}; any other service returns an error."
            ),
            limits=(
                "At most 20 traces requested and 20 spans sampled (requested "
                "service first, error status first, longest first); truncated "
                "at the registered max_view_bytes by dropping trailing spans."
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
                description="How many traces to ask the backend for, 1 to 20.",
            ),
        },
        result_path=("data", "sampled_spans"),
        request_timeout_seconds=30.0,
        max_result_bytes=256 * 1024,
        max_view_bytes=16 * 1024,
        max_window_seconds=3600,
        error_classes={"400": "INVALID_PARAMS"},
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

    ``offset`` and ``@`` move the evaluation outside the window and a range
    selector longer than the window is refused; a selector at the window
    start still looks back up to its own length before it (plus Prometheus'
    lookback delta), which is bounded and stays on the dedicated instance --
    the returned sample timestamps are what the executor checks against the
    scope. Any ``[...]`` that is not ``<n>[smh]`` is refused too (a label
    regex such as ``[a-z]+`` is over-refused, safely).
    """
    if not expr or len(expr) > MAX_PROMQL_CHARS:
        return "QUERY_OUT_OF_WINDOW"
    # PromQL keywords are case-insensitive (``OFFSET`` parses), so the word
    # is matched case-folded (fresh-context contract test finding).
    if "@" in expr or re.search(r"\boffset\b", expr, re.IGNORECASE):
        return "QUERY_OUT_OF_WINDOW"
    for selector in _RANGE_SELECTOR.findall(expr):
        for part in selector.split(":"):
            if not part:
                continue
            if not _DURATION.fullmatch(part):
                return "QUERY_OUT_OF_WINDOW"
            if int(part[:-1]) * _DURATIONS[part[-1]] > window_seconds:
                return "QUERY_OUT_OF_WINDOW"
    return None


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
        window = request.window
        if not isinstance(expr, str):
            raise _refused("QUERY_OUT_OF_WINDOW")
        problem = promql_problem(expr, window.seconds)
        if problem is not None:
            raise _refused(problem)
        step = request.params.get("step_seconds", DEFAULT_STEP_SECONDS)
        if (
            type(step) is not int
            or step < MIN_STEP_SECONDS
            or step > int(window.seconds)
        ):
            raise _refused("INVALID_STEP")
        url = f"{request.endpoint}/api/v1/query_range?" + urllib.parse.urlencode(
            {
                "query": expr,
                "start": f"{window.start.timestamp():.3f}",
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
        )

    # -- traces -------------------------------------------------------------

    def _traces(self, request: TransportRequest) -> TransportResponse:
        service = request.params.get("service")
        if not isinstance(service, str) or service not in SERVICES:
            raise _refused("SERVICE_NOT_AVAILABLE")
        limit = request.params.get("limit", DEFAULT_TRACE_LIMIT)
        if type(limit) is not int or not 1 <= limit <= MAX_TRACE_LIMIT:
            raise _refused("INVALID_LIMIT")
        base = request.selector.get("traces_endpoint", "")
        if not base:
            raise TransportError("TRACES_ENDPOINT_MISSING")
        window = request.window
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
        )


def _refused(code: str) -> TransportRefused:
    """A fixed-code refusal decided before any request went out.

    The executor reports it as ``INVALID_PARAMS`` with no source contact and
    no dispatch; ``code`` names the rule (fixed vocabulary, never source
    text) and stays inside the exception.
    """
    return TransportRefused(code)


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
    sampled = spans[:MAX_SAMPLED_SPANS]
    # Judged over the sampled set: the executor's byte cap may still drop
    # trailing sampled spans, so a "visible" parent is one that was sampled,
    # not necessarily one the model was shown.
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
            "display_max_spans": MAX_SAMPLED_SPANS,
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


# -- composition -------------------------------------------------------------


def otel_demo_executor_factory(
    store: DurableStore,
    *,
    evidence: EvidenceSink,
    clock: Clock,
    config: OtelDemoConfig | None = None,
) -> ExecutorFactory:
    """An ``ExecutorFactory`` for ``InvestigationRunner`` over this profile.

    The scope is issued per lease from the committed Run row (deadline,
    generations) and the Run's recorded input (window); the ledger, the
    evidence sink and the control source are the durable ones.
    """
    config = config or OtelDemoConfig()
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
        )

    return factory
