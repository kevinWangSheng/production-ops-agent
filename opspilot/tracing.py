"""OpenTelemetry spans for one Run, exported to LangSmith in the lab only.

ADR-0006: the product code depends on the OpenTelemetry SDK alone; LangSmith
is reached through its OTLP/HTTP endpoint and the attribute conventions it
documents (``langsmith.span.kind``, ``langsmith.metadata.*``, ``gen_ai.*``).
Switching backends is an endpoint change, not an instrumentation change.

Modes (``OPSPILOT_TRACE``):

* ``off`` (default, and anything unrecognised): ``NullTracer``. No provider,
  no exporter, no network, no queue; every span call is a method on a shared
  no-op object.
* ``lab``: spans carry model inputs/outputs and tool views, and are exported
  only when the process can prove it is running against the synthetic lab.
  The proof is **fail-closed**: ``check_lab_target`` runs at ``configure()``
  (startup) and again at the start of every Run (``Tracer.run``); any failed
  check means the whole Run exports nothing and the codes are logged. A model
  or tool span outside an open, exporting Run span is a no-op, so a Run can
  never be exported in part.

Data flow contract (PRODUCT-CONSTRAINTS, C3 lines 440-441): span attributes
are written only by the three ``_*_attributes`` constructors in this module.
Each takes the typed object the business path already holds (``ModelCall``,
``ModelReply``, ``ToolRequest``, ``ToolOutcome``) and copies an explicit
allowlist of fields; no raw dict and no provider response ever reaches a
span. ``reasoning_content`` (a DeepSeek-private protocol field), ``raw``,
``usage`` beyond the three token counts, ``credential_ref`` and every header
are outside the allowlists and are therefore dropped, whatever the mode.

Export never touches the business path: a bounded batch queue on a
background thread, a per-export timeout, and a counter of dropped spans that
is logged (C3 §11 allows bounded loss of trace data).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import threading
from collections.abc import Mapping, Sequence
from types import TracebackType
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SpanExporter
    from opentelemetry.trace import Span

    from opspilot.investigation.loop import ModelCall, ModelReply
    from opspilot.tools.executor import ToolRequest
    from opspilot.tools.outcomes import ToolOutcome

__all__ = [
    "LAB_PROJECT_PREFIX",
    "TRACE_ENV",
    "NullTracer",
    "Tracer",
    "check_lab_target",
    "configure",
    "reset",
    "shutdown",
    "tracer",
]

_log = logging.getLogger("opspilot.tracing")

TRACE_ENV = "OPSPILOT_TRACE"
TraceMode = Literal["off", "lab"]
LAB_PROJECT_PREFIX = "opspilot-lab-"
LAB_TOOL_PROFILES = frozenset({"fixture", "otel-demo"})
# LangSmith SaaS regions (docs: trace-with-opentelemetry). A self-hosted or
# unknown host is refused: the lab exports to the account's own SaaS project.
_LANGSMITH_HOSTS = frozenset(
    {
        "api.smith.langchain.com",
        "eu.api.smith.langchain.com",
        "apac.api.smith.langchain.com",
        "aws.api.smith.langchain.com",
    }
)
_DEFAULT_LANGSMITH_ENDPOINT = "https://api.smith.langchain.com"
# Upstream HolmesGPT truncates every attribute by length; LangSmith documents
# no limit, so this is the project's own bound on one attribute value.
_MAX_ATTR_CHARS = 20_000
_TRUNCATED = "...[truncated]"
# Export bounds: queue, batch cadence and the per-export HTTP timeout. The
# queue drops when full; the exporter wrapper below counts what was lost.
_MAX_QUEUE = 2048
_SCHEDULE_MS = 2_000.0
_EXPORT_TIMEOUT_S = 10.0
# ``versions`` keys that may travel as ``langsmith.metadata.*``.
_VERSION_KEYS = ("prompt_revision", "tool_schema_revision", "context_policy_revision")
# Fixed-vocabulary codes only (``MODEL_UNAVAILABLE``, ``LEASE_ACTIVE``, ...):
# anything else that reaches ``settle``/``error.type`` is free text from an
# exception and is replaced, never copied (independent review, PR #108 #5).
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_UNCLASSIFIED = "UNCLASSIFIED"
# Parameter names that look like credential slots are dropped even from the
# name list (second line behind the allowlist; independent review #4).
_CREDENTIAL_KEY = re.compile(
    r"auth|token|secret|password|passwd|api[_-]?key|cookie|credential|bearer",
    re.IGNORECASE,
)
_MAX_PARAM_NAMES = 32
_EXPORT_BATCH = 512
# kind in-cluster service suffixes, matched on a label boundary.
_CLUSTER_SUFFIXES = (".svc", ".svc.cluster.local")


# -- lab target verification -------------------------------------------------


def _is_lab_host(url: str) -> bool:
    """A real loopback address, exact ``localhost``, or a kind in-cluster
    service name (``*.svc``, ``*.svc.cluster.local``); nothing else.

    Independent review (PR #108 #1): a prefix test accepted
    ``127.attacker.com`` and ignored userinfo (``user@127.0.0.1``). The
    host is parsed, userinfo is refused, IP literals go through
    ``ipaddress`` and names are compared on a label boundary after
    lower-casing and stripping one trailing dot.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return False
    if not host or parts.username is not None or parts.password is not None:
        return False
    host = host.lower().rstrip(".")
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    if host == "localhost":
        return True
    for suffix in _CLUSTER_SUFFIXES:
        if host.endswith(suffix):
            labels = host[: -len(suffix)].split(".")
            return all(labels) and bool(labels[0])
    return False


def check_lab_target(env: Mapping[str, str]) -> tuple[str, ...]:
    """Codes for every lab precondition ``env`` fails; empty means lab.

    Each check reads the environment directly -- not a value cached at
    startup -- so the per-Run call in ``Tracer.run`` is an independent
    verification, as the task record requires.
    """
    failures: list[str] = []
    if (env.get(TRACE_ENV) or "off") != "lab":
        failures.append("TRACE_MODE_NOT_LAB")
    profile = env.get("OPSPILOT_TOOL_PROFILE") or "fixture"
    if profile not in LAB_TOOL_PROFILES:
        failures.append("TOOL_PROFILE_NOT_LAB")
    if not _is_lab_host(env.get("OPSPILOT_OTEL_PROMETHEUS_URL") or "http://127.0.0.1"):
        failures.append("PROMETHEUS_NOT_LAB")
    if not _is_lab_host(env.get("OPSPILOT_OTEL_JAEGER_URL") or "http://127.0.0.1"):
        failures.append("JAEGER_NOT_LAB")
    project = env.get("LANGSMITH_PROJECT") or ""
    if not project.startswith(LAB_PROJECT_PREFIX):
        failures.append("PROJECT_NOT_LAB")
    if not env.get("LANGSMITH_API_KEY"):
        failures.append("LANGSMITH_KEY_MISSING")
    endpoint = env.get("LANGSMITH_ENDPOINT") or _DEFAULT_LANGSMITH_ENDPOINT
    try:
        parts = urlsplit(endpoint)
    except ValueError:
        parts = None
    if (
        parts is None
        or parts.scheme != "https"
        or parts.hostname not in _LANGSMITH_HOSTS
    ):
        failures.append("LANGSMITH_ENDPOINT_NOT_ALLOWED")
    return tuple(failures)


# -- attribute constructors (the only writers) --------------------------------

AttrValue = str | bool | int | float | Sequence[str]


def _bounded(text: str) -> str:
    if len(text) <= _MAX_ATTR_CHARS:
        return text
    return text[: _MAX_ATTR_CHARS - len(_TRUNCATED)] + _TRUNCATED


def _json_text(value: object) -> str:
    try:
        return _bounded(
            json.dumps(value, ensure_ascii=False, sort_keys=True, default=repr)
        )
    except (TypeError, ValueError, RecursionError):
        return "<unserializable>"


def _message_view(message: Mapping[str, Any]) -> tuple[str, str]:
    """``(role, content)`` of one Chat Completions message, allowlisted.

    Kept: ``role``, ``content``, ``tool_calls`` (id, function name and
    arguments), ``tool_call_id``. Dropped: ``reasoning_content`` and every
    other key -- provider-private fields never leave the provider's channel.
    """
    role = message.get("role")
    role_text = role if isinstance(role, str) else "unknown"
    content = message.get("content")
    calls = message.get("tool_calls")
    if isinstance(calls, Sequence) and not isinstance(calls, str) and calls:
        kept_calls = []
        for call in calls:
            if not isinstance(call, Mapping):
                continue
            function = call.get("function")
            function = function if isinstance(function, Mapping) else {}
            kept_calls.append(
                {
                    "id": call.get("id"),
                    "name": function.get("name"),
                    "arguments": function.get("arguments"),
                }
            )
        return role_text, _json_text({"content": content, "tool_calls": kept_calls})
    if isinstance(content, str):
        return role_text, _bounded(content)
    return role_text, _json_text(content)


def _run_attributes(
    *, incident_id: str, run_id: str, versions: Mapping[str, str]
) -> dict[str, AttrValue]:
    from opspilot.tools.outcomes import PROJECTION_REVISION

    attributes: dict[str, AttrValue] = {
        "langsmith.span.kind": "chain",
        "langsmith.metadata.incident_id": incident_id,
        "langsmith.metadata.run_id": run_id,
        "langsmith.metadata.projection_revision": PROJECTION_REVISION,
        "opspilot.trace.mode": "lab",
    }
    for key in _VERSION_KEYS:
        value = versions.get(key)
        if isinstance(value, str):
            attributes[f"langsmith.metadata.{key}"] = _bounded(value)
    return attributes


def _model_call_attributes(call: ModelCall, *, seq: int) -> dict[str, AttrValue]:
    from opspilot.investigation.loop import ModelCall

    if not isinstance(call, ModelCall):
        raise TypeError("span attributes are built from ModelCall only")
    attributes: dict[str, AttrValue] = {
        "langsmith.span.kind": "llm",
        "langsmith.metadata.model_call_seq": seq,
        "gen_ai.system": "deepseek",
        "gen_ai.request.model": call.model,
        "gen_ai.request.max_tokens": call.max_tokens,
        "opspilot.model.json_mode": call.json_mode,
        "opspilot.model.timeout_seconds": float(call.timeout_seconds),
        "opspilot.model.tool_schema_count": 0
        if call.tools is None
        else len(call.tools),
    }
    for index, message in enumerate(call.messages):
        role, content = _message_view(message)
        attributes[f"gen_ai.prompt.{index}.role"] = role
        attributes[f"gen_ai.prompt.{index}.content"] = content
    return attributes


def _model_reply_attributes(reply: ModelReply) -> dict[str, AttrValue]:
    """Allowlist: finish reason, response model, three token counts, the
    visible completion. ``reasoning_content``, ``raw`` and the rest of
    ``usage`` are not copied."""
    from opspilot.investigation.loop import ModelReply

    if not isinstance(reply, ModelReply):
        raise TypeError("span attributes are built from ModelReply only")
    attributes: dict[str, AttrValue] = {
        "gen_ai.response.model": reply.response_model,
        "gen_ai.response.finish_reasons": [reply.finish_reason],
        "opspilot.model.tool_call_count": len(reply.tool_calls),
    }
    for source, target in (
        ("prompt_tokens", "gen_ai.usage.input_tokens"),
        ("completion_tokens", "gen_ai.usage.output_tokens"),
        ("total_tokens", "gen_ai.usage.total_tokens"),
    ):
        value = reply.usage.get(source)
        if isinstance(value, int) and not isinstance(value, bool):
            attributes[target] = value
    role, content = _message_view(
        {"role": "assistant", "content": reply.content, "tool_calls": reply.tool_calls}
    )
    attributes["gen_ai.completion.0.role"] = role
    attributes["gen_ai.completion.0.content"] = content
    return attributes


def _tool_request_attributes(request: ToolRequest, *, seq: int) -> dict[str, AttrValue]:
    """Allowlist: tool name and target ref (strings only), the window's
    ``start``/``end`` strings, and the *names* of the proposed parameters
    (credential-looking names dropped). Parameter values are model-proposed
    arbitrary content and are not copied here; the accepted query -- declared
    parameters only, after ``_accept_params`` -- travels on the outcome as
    ``opspilot.tool.query`` (independent review, PR #108 #4)."""
    from opspilot.tools.executor import ToolRequest

    if not isinstance(request, ToolRequest):
        raise TypeError("span attributes are built from ToolRequest only")
    name = request.tool_name if isinstance(request.tool_name, str) else None
    prompt: dict[str, object] = {}
    if name is not None:
        prompt["tool"] = _bounded(name)
    if isinstance(request.target_ref, str):
        prompt["target"] = _bounded(request.target_ref)
    if isinstance(request.window, Mapping):
        window = {
            key: request.window[key]
            for key in ("start", "end")
            if isinstance(request.window.get(key), str)
        }
        if window:
            prompt["window"] = window
    if isinstance(request.params, Mapping):
        prompt["param_names"] = sorted(
            _bounded(key)[:128]
            for key in request.params
            if isinstance(key, str) and not _CREDENTIAL_KEY.search(key)
        )[:_MAX_PARAM_NAMES]
    attributes: dict[str, AttrValue] = {
        "langsmith.span.kind": "tool",
        "langsmith.metadata.tool_call_seq": seq,
        "langsmith.metadata.step_id": _bounded(request.step_id),
        "opspilot.tool.index": request.tool_index,
        "gen_ai.prompt": _json_text(prompt),
    }
    if name is not None:
        attributes["gen_ai.tool.name"] = _bounded(name)
    return attributes


def _tool_outcome_attributes(outcome: ToolOutcome) -> dict[str, AttrValue]:
    """Allowlist: resolved tool/target/source, status class and reason,
    source contact, query and window, the model-visible view and its size,
    the evidence id and raw byte count. ``credential_ref`` and the raw
    source bytes are not copied."""
    from opspilot.tools.outcomes import ToolOutcome

    if not isinstance(outcome, ToolOutcome):
        raise TypeError("span attributes are built from ToolOutcome only")
    operation = outcome.operation
    view_text = _json_text(outcome.model_view)
    attributes: dict[str, AttrValue] = {
        "opspilot.tool.status": outcome.status,
        "opspilot.tool.source_contact": outcome.source_contact,
        "opspilot.tool.view_bytes": len(
            json.dumps(outcome.model_view, ensure_ascii=False, default=repr)
        ),
        "gen_ai.completion": view_text,
    }
    if outcome.reason is not None:
        attributes["opspilot.tool.reason"] = outcome.reason
    for source, target in (
        (operation.tool, "opspilot.tool.name"),
        (operation.source, "opspilot.tool.source"),
        (operation.target_id, "opspilot.tool.target_id"),
        (operation.query, "opspilot.tool.query"),
    ):
        if isinstance(source, str):
            attributes[target] = _bounded(source)
    if operation.window is not None:
        attributes["opspilot.tool.window_start"] = operation.window.start.isoformat()
        attributes["opspilot.tool.window_end"] = operation.window.end.isoformat()
    evidence_id = outcome.model_view.get("evidence_id")
    if isinstance(evidence_id, str):
        attributes["opspilot.tool.evidence_id"] = _bounded(evidence_id)
    if outcome.evidence is not None:
        attributes["opspilot.tool.raw_bytes"] = len(outcome.evidence.raw)
        attributes["opspilot.tool.result_count"] = outcome.evidence.result_count
    return attributes


# -- spans ---------------------------------------------------------------------


def _code_only(value: object, *, lower: bool = False) -> str:
    """``value`` when it is a fixed-vocabulary code, else ``UNCLASSIFIED``.

    ``RunnerOutcome.reason`` may carry ``str(exc)`` of a storage or transport
    error; the span keeps the upper-case code vocabulary and never the
    message. ``lower`` admits the ``RunnerStatus`` literals instead
    (``handed_off``, ``control_denied``, ...).
    """
    if isinstance(value, str):
        if lower:
            if value.islower() and value.isidentifier() and len(value) <= 32:
                return value
        elif _CODE.match(value):
            return value
    return _UNCLASSIFIED


class _NullSpan:
    """Every span method, doing nothing. One shared instance per process."""

    __slots__ = ()

    def __enter__(self) -> _NullSpan:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def reply(self, reply: ModelReply) -> None:
        return None

    def outcome(self, outcome: ToolOutcome) -> None:
        return None

    def settle(self, status: str, reason: str | None) -> None:
        return None

    @property
    def trace_id(self) -> str | None:
        return None


_NULL_SPAN = _NullSpan()


class _LiveSpan:
    """An exporting OTel span. Attributes arrive only via the constructors."""

    def __init__(self, span: Span) -> None:
        self._span = span

    def __enter__(self) -> _LiveSpan:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        from opentelemetry.trace import Status, StatusCode

        if exc is not None:
            # Fixed codes only: ``ModelError.code`` / the exception type.
            # Never the message -- a transport message could quote a body.
            code = getattr(exc, "code", None)
            self._span.set_attribute(
                "error.type",
                _code_only(code) if code is not None else type(exc).__name__,
            )
            # No description and no ``record_exception``: either would carry
            # the exception text (independent review, PR #108 #5).
            self._span.set_status(Status(StatusCode.ERROR))
        self._span.end()

    def _set(self, attributes: Mapping[str, AttrValue]) -> None:
        for key, value in attributes.items():
            self._span.set_attribute(key, value)

    def reply(self, reply: ModelReply) -> None:
        self._set(_model_reply_attributes(reply))

    def outcome(self, outcome: ToolOutcome) -> None:
        self._set(_tool_outcome_attributes(outcome))

    def settle(self, status: str, reason: str | None) -> None:
        self._span.set_attribute("opspilot.run.status", _code_only(status, lower=True))
        if reason is not None:
            self._span.set_attribute("opspilot.run.reason", _code_only(reason))

    @property
    def trace_id(self) -> str | None:
        return format(self._span.get_span_context().trace_id, "032x")


class _RefusedRunSpan(_NullSpan):
    """A Run whose lab proof failed: while it is open, no child exports.

    Returning the shared ``_NULL_SPAN`` left the enclosing Run's context in
    place, so a refused inner Run's model/tool spans attached to the outer
    exporting trace (independent review, PR #108 #3). This span clears the
    active-Run slot on entry and restores the previous value on exit.
    """

    __slots__ = ("_owner", "_previous")

    def __init__(self, owner: Tracer) -> None:
        self._owner = owner
        self._previous: _RunSpan | None = None

    def __enter__(self) -> _RefusedRunSpan:
        self._previous = getattr(self._owner._current, "run", None)
        self._owner._current.run = None
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._owner._current.run = self._previous


class _RunSpan(_LiveSpan):
    """The Run's root span plus the per-Run sequence counters."""

    def __init__(self, span: Span, owner: Tracer) -> None:
        super().__init__(span)
        self._owner = owner
        self._previous: _RunSpan | None = None
        self.model_calls = 0
        self.tool_calls = 0

    def __enter__(self) -> _RunSpan:
        self._previous = getattr(self._owner._current, "run", None)
        self._owner._current.run = self
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._owner._current.run = self._previous
        self._span.set_attribute("opspilot.run.model_calls", self.model_calls)
        self._span.set_attribute("opspilot.run.tool_calls", self.tool_calls)
        super().__exit__(exc_type, exc, tb)


# -- tracers -------------------------------------------------------------------


class NullTracer:
    """``off`` mode, and ``lab`` mode whose startup check failed."""

    mode: TraceMode = "off"

    def run(
        self, *, incident_id: object, run_id: object, versions: Mapping[str, str]
    ) -> _NullSpan | _RefusedRunSpan:
        return _NULL_SPAN

    def model_call(self, call: ModelCall) -> _NullSpan:
        return _NULL_SPAN

    def tool_call(self, request: ToolRequest) -> _NullSpan:
        return _NULL_SPAN

    @property
    def last_trace_id(self) -> str | None:
        return None

    def shutdown(self, timeout_seconds: float = _EXPORT_TIMEOUT_S) -> None:
        return None


class Tracer:
    """``lab`` mode with a live provider; each Run re-proves the lab target."""

    mode: TraceMode = "lab"

    def __init__(
        self,
        provider: TracerProvider,
        exporter: _CountingExporter,
        env: Mapping[str, str],
    ) -> None:
        self._provider = provider
        self._exporter = exporter
        self._env = env
        self._otel = provider.get_tracer("opspilot")
        self._current = threading.local()
        self._last_trace_id: str | None = None

    @property
    def dropped_spans(self) -> int:
        return self._exporter.dropped

    @property
    def last_trace_id(self) -> str | None:
        return self._last_trace_id

    def run(
        self, *, incident_id: object, run_id: object, versions: Mapping[str, str]
    ) -> _RunSpan | _RefusedRunSpan:
        """The Run's root span, or a refused span when the lab proof fails now.

        Verified independently of ``configure()``: the environment (mode
        included) is read again here, and a failure means this Run -- and
        every model/tool span opened inside it -- exports nothing at all.
        """
        failures = check_lab_target(self._env)
        if failures:
            _log.warning(
                "trace export disabled for run=%s: %s", run_id, ",".join(failures)
            )
            return _RefusedRunSpan(self)
        span = self._otel.start_span("opspilot.run")
        for key, value in _run_attributes(
            incident_id=str(incident_id), run_id=str(run_id), versions=versions
        ).items():
            span.set_attribute(key, value)
        wrapped = _RunSpan(span, self)
        self._last_trace_id = wrapped.trace_id
        return wrapped

    def _child(self, name: str, attributes: Mapping[str, AttrValue]) -> _LiveSpan:
        from opentelemetry.trace import set_span_in_context

        parent: _RunSpan = self._current.run
        span = self._otel.start_span(name, context=set_span_in_context(parent._span))
        for key, value in attributes.items():
            span.set_attribute(key, value)
        return _LiveSpan(span)

    def model_call(self, call: ModelCall) -> _LiveSpan | _NullSpan:
        parent = getattr(self._current, "run", None)
        if parent is None:
            return _NULL_SPAN
        parent.model_calls += 1
        return self._child(
            "deepseek.chat.completions",
            _model_call_attributes(call, seq=parent.model_calls),
        )

    def tool_call(self, request: ToolRequest) -> _LiveSpan | _NullSpan:
        parent = getattr(self._current, "run", None)
        if parent is None:
            return _NULL_SPAN
        parent.tool_calls += 1
        name = request.tool_name if isinstance(request.tool_name, str) else "tool"
        return self._child(
            f"tool:{_bounded(name)[:80]}",
            _tool_request_attributes(request, seq=parent.tool_calls),
        )

    def shutdown(self, timeout_seconds: float = _EXPORT_TIMEOUT_S) -> None:
        """Flush what the queue still holds, bounded, then stop the thread."""
        millis = int(timeout_seconds * 1000)
        self._provider.force_flush(millis)
        self._provider.shutdown()
        if self._exporter.dropped:
            _log.warning("trace export dropped spans=%d", self._exporter.dropped)


class _CountingExporter:
    """Delegates to the real exporter; counts and logs what it failed to send.

    Implements the ``SpanExporter`` surface the batch processor calls. A
    failed or raising export is a dropped batch, never an exception on the
    processor thread (and never on the business thread, which is not here).
    """

    def __init__(self, inner: SpanExporter) -> None:
        self._inner = inner
        self.dropped = 0

    def export(self, spans: Sequence[Any]) -> Any:
        from opentelemetry.sdk.trace.export import SpanExportResult

        try:
            result = self._inner.export(spans)
        except Exception as exc:  # noqa: BLE001 - export failure is bounded loss
            result = SpanExportResult.FAILURE
            _log.warning("trace export raised %s", type(exc).__name__)
        if result is not SpanExportResult.SUCCESS:
            self.dropped += len(spans)
            _log.warning(
                "trace export failed: dropped spans=%d total=%d",
                len(spans),
                self.dropped,
            )
        return result

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self._inner.force_flush(timeout_millis)


# -- process-wide configuration ----------------------------------------------

_tracer: NullTracer | Tracer = NullTracer()
_lock = threading.Lock()


def tracer() -> NullTracer | Tracer:
    """The process tracer; ``NullTracer`` until ``configure`` says otherwise."""
    return _tracer


def _langsmith_exporter(env: Mapping[str, str]) -> SpanExporter:
    from opentelemetry.exporter.otlp.proto.http import Compression
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    base = (env.get("LANGSMITH_ENDPOINT") or _DEFAULT_LANGSMITH_ENDPOINT).rstrip("/")
    headers = {
        "x-api-key": env["LANGSMITH_API_KEY"],
        "Langsmith-Project": env["LANGSMITH_PROJECT"],
    }
    workspace = env.get("LANGSMITH_WORKSPACE_ID")
    if workspace:
        headers["x-tenant-id"] = workspace
    # The constructor endpoint is used verbatim (only the env-var form has
    # ``/v1/traces`` appended by the exporter), so spell the signal path out.
    # Every tunable is explicit so ``OTEL_EXPORTER_OTLP_*`` in the ambient
    # environment neither changes the destination nor breaks startup.
    return OTLPSpanExporter(
        endpoint=f"{base}/otel/v1/traces",
        headers=headers,
        timeout=_EXPORT_TIMEOUT_S,
        compression=Compression.NoCompression,
    )


def configure(
    env: Mapping[str, str], *, exporter: SpanExporter | None = None
) -> NullTracer | Tracer:
    """Install the process tracer from ``env``; returns it.

    ``off`` installs ``NullTracer`` without importing the SDK. ``lab`` runs
    the startup lab check first: a failure installs ``NullTracer`` and logs
    the codes (fail-closed). ``exporter`` replaces the LangSmith exporter
    (tests: in-memory, or a deliberately unreachable endpoint).
    """
    global _tracer
    mode = env.get(TRACE_ENV) or "off"
    with _lock:
        _tracer.shutdown()
        if mode != "lab":
            if mode != "off":
                _log.error("%s=%r is not a mode; tracing stays off", TRACE_ENV, mode)
            _tracer = NullTracer()
            return _tracer
        failures = check_lab_target(env)
        if failures:
            _log.error("trace export disabled at startup: %s", ",".join(failures))
            _tracer = NullTracer()
            return _tracer
        try:
            _tracer = _lab_tracer(env, exporter)
        except Exception as exc:  # noqa: BLE001 - fail closed, never break startup
            # Type only: an SDK error message can quote environment values
            # (independent review, PR #108 #7).
            _log.error("TRACE_CONFIGURE_FAILED error=%s", type(exc).__name__)
            _tracer = NullTracer()
            return _tracer
        _log.info("trace export enabled mode=lab")
        return _tracer


def _lab_tracer(env: Mapping[str, str], exporter: SpanExporter | None) -> Tracer:
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    counting = _CountingExporter(
        exporter if exporter is not None else _langsmith_exporter(env)
    )
    # ``Resource(...)``, not ``Resource.create()``: the latter merges
    # ``OTEL_RESOURCE_ATTRIBUTES``/``OTEL_SERVICE_NAME`` and SDK detectors,
    # which are outside the allowlist (independent review, PR #108 #6).
    provider = TracerProvider(resource=Resource({"service.name": "opspilot"}))
    # Every bound explicit: the processor otherwise reads ``OTEL_BSP_*`` and
    # raises on an invalid value at startup (independent review #7).
    provider.add_span_processor(
        BatchSpanProcessor(
            counting,  # type: ignore[arg-type]  # structural SpanExporter
            max_queue_size=_MAX_QUEUE,
            schedule_delay_millis=_SCHEDULE_MS,
            max_export_batch_size=_EXPORT_BATCH,
            export_timeout_millis=_EXPORT_TIMEOUT_S * 1000,
        )
    )
    # ``env`` itself, not a copy: with ``os.environ`` the per-Run check
    # in ``Tracer.run`` reads the live environment, not a startup snapshot.
    return Tracer(provider, counting, env)


def shutdown(timeout_seconds: float = _EXPORT_TIMEOUT_S) -> None:
    """Flush and stop the process tracer (bounded); it becomes ``NullTracer``."""
    global _tracer
    with _lock:
        _tracer.shutdown(timeout_seconds)
        _tracer = NullTracer()


reset = shutdown
