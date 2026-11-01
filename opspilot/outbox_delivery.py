"""Trace outbox delivery to LangSmith over OTLP (#428, #243 PR-2; C3 section 11).

Runs in the worker's outbox thread (``OutboxMaintainer``) only when
``OPSPILOT_TRACE=outbox`` and the three Q15 preconditions hold
(``tracing.outbox_export_failures``); otherwise this module is never
constructed and nothing leaves the process (fail-closed).

At least once, with stable identities (OB-R3): a claimed row is sent as one
OTLP span whose trace id is derived from its Run (or, for a postmortem
generation, from its key) and whose span id from its ``idempotency_key``,
so a redelivery carries the same ids. Bounded (OB-R5): exponential backoff
from 5 s, at most 8 attempts, a 10 s timeout per batch, and a 20 000-row
pending backlog; rejected (4xx other than 408/429) and unserializable rows
are dropped. Every drop is a row state and a counter; nothing touches a
Run (OB-R6).

Span attributes come only from the row's payload, which ``build_payload``
already limited to the Q14 allowlist; this module reads no other table
than the outbox and adds only fixed names (span names, ``deepseek``).
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from uuid import UUID

from opspilot import tracing
from opspilot.persistence.outbox import (
    PAYLOAD_KEYS,
    ClaimedRow,
    OutboxMaintainer,  # the thread that runs this module (re-exported, seams-428)
    OutboxStore,
)

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import ReadableSpan

    from opspilot.health import OutboxStats

__all__ = [
    "BACKLOG_LIMIT",
    "BATCH_SIZE",
    "BATCH_TIMEOUT_SECONDS",
    "CLAIM_LEASE_SECONDS",
    "DELIVERY_INTERVAL_SECONDS",
    "DROP_REASONS",
    "MAX_ATTEMPTS",
    "OTLP_PATH",
    "DeliveryResult",
    "OutboxMaintainer",
    "ExportConfig",
    "OutboxDelivery",
    "Transport",
    "backoff_seconds",
    "root_span_id_for",
    "span_id_for",
    "trace_id_for",
]

_log = logging.getLogger("opspilot.outbox_delivery")

BATCH_SIZE = 100
BACKLOG_LIMIT = 20_000
MAX_ATTEMPTS = 8
BATCH_TIMEOUT_SECONDS = 10.0
CLAIM_LEASE_SECONDS = 30.0
DELIVERY_INTERVAL_SECONDS = 2.0
DROP_REASONS: frozenset[str] = frozenset(
    {"MAX_ATTEMPTS", "REJECTED", "UNSERIALIZABLE", "BACKLOG"}
)
OTLP_PATH = "/otel/v1/traces"
_BACKOFF_BASE = 5.0
_BACKOFF_CAP = 600.0
# Run states after which a Run is stopped (its root span is sent).
_RUNNING = frozenset({"queued", "running"})
_ERROR_TOOL = frozenset({"error", "timeout", "denied"})
# The shape of every string a payload holds (UUIDs, identifiers, codes,
# ISO times); anything else means the row is not what PR-1 writes.
_VALUE = re.compile(r"^[A-Za-z0-9._:+-]{1,64}$")
_TOKEN_KEYS = ("input_tokens", "output_tokens", "total_tokens")
_METADATA_KEYS = tuple(
    sorted(PAYLOAD_KEYS - {"model", "started_at", "ended_at", *_TOKEN_KEYS})
)
_RESPONSE_READ_LIMIT = 65_536

#: (url, body, headers, timeout seconds) -> HTTP status; raises ``OSError``
#: when no status was received (connection refused, DNS, timeout).
Transport = Callable[[str, bytes, Mapping[str, str], float], int]


def backoff_seconds(failures: int) -> float:
    """Delay after the ``failures``-th failed attempt: 5 s doubling, capped
    at 10 minutes."""
    return float(min(_BACKOFF_BASE * 2.0 ** max(failures - 1, 0), _BACKOFF_CAP))


def _digest(text: str, size: int) -> int:
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:size], "big")


def trace_id_for(kind: str, run_id: UUID | None, idempotency_key: str) -> int:
    """sha256(run_id) first 16 bytes; a ``generation`` row (no Run) uses its
    key, ``generation:<attempt id>`` (OB-R3)."""
    if run_id is not None:
        return _digest(str(run_id), 16)
    if kind != "generation":
        raise ValueError("only a generation row has no run_id")
    return _digest(idempotency_key, 16)


def span_id_for(idempotency_key: str) -> int:
    """sha256(idempotency_key) first 8 bytes (OB-R3)."""
    return _digest(idempotency_key, 8)


def root_span_id_for(run_id: UUID) -> int:
    """The Run root span: sha256(``run:<run_id>``) first 8 bytes."""
    return _digest(f"run:{run_id}", 8)


@dataclass(frozen=True)
class ExportConfig:
    """Where and as whom to deliver. ``repr`` never shows the key."""

    endpoint_url: str
    api_key: str = field(repr=False)
    project: str
    workspace_id: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> ExportConfig | None:
        """``None`` unless ``OPSPILOT_TRACE=outbox`` and every precondition
        holds (fail-closed)."""
        if tracing.export_status(env) != ("ENABLED",):
            return None
        base = (
            env.get("LANGSMITH_ENDPOINT") or "https://api.smith.langchain.com"
        ).rstrip("/")
        return cls(
            endpoint_url=base + OTLP_PATH,
            api_key=env["LANGSMITH_API_KEY"],
            project=env["LANGSMITH_PROJECT"],
            workspace_id=env.get("LANGSMITH_WORKSPACE_ID") or None,
        )

    def headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/x-protobuf",
            "x-api-key": self.api_key,
            "Langsmith-Project": self.project,
        }
        if self.workspace_id:
            headers["x-tenant-id"] = self.workspace_id
        return headers


def _is_loopback(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def urllib_transport(
    url: str, body: bytes, headers: Mapping[str, str], timeout: float
) -> int:
    """POST with the standard library. Proxy variables apply except to a
    loopback endpoint (tests); redirects are not followed (the endpoint is
    exact). The response body is read (bounded) and discarded."""
    handlers: list[urllib.request.BaseHandler] = [_NoRedirect()]
    if _is_loopback(url):
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(
        url, data=body, headers=dict(headers), method="POST"
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            response.read(_RESPONSE_READ_LIMIT)
            return int(response.status)
    except urllib.error.HTTPError as exc:
        exc.close()
        return int(exc.code)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


@dataclass
class DeliveryResult:
    """One ``deliver_once`` cycle."""

    claimed: int = 0
    delivered: int = 0
    retried: int = 0
    dropped: dict[str, int] = field(default_factory=dict)
    status: int | None = None


class _Unserializable(ValueError):
    pass


def _text(value: object) -> str:
    if not isinstance(value, str) or not _VALUE.fullmatch(value):
        raise _Unserializable("value shape")
    return value


def _count(value: object) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _Unserializable("number")
    return value


def _ns(value: object) -> int:
    moment = value if isinstance(value, datetime) else None
    if moment is None:
        try:
            moment = datetime.fromisoformat(_text(value))
        except ValueError as exc:
            raise _Unserializable("time") from exc
    if moment.tzinfo is None:
        raise _Unserializable("naive time")
    return int(moment.astimezone(UTC).timestamp() * 1_000_000) * 1_000


class OutboxDelivery:
    """Claims, sends and settles outbox rows; see the module docstring.

    ``clock`` (tz-aware) replaces the database clock for due checks and
    the written times; ``transport`` replaces the HTTP POST; ``stats`` is
    the ``/metrics`` snapshot the cycle updates.
    """

    batch_size = BATCH_SIZE

    def __init__(
        self,
        store: OutboxStore,
        config: ExportConfig,
        *,
        stats: OutboxStats | None = None,
        clock: Callable[[], datetime] | None = None,
        transport: Transport | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self.stats = stats
        self.clock = clock
        self.transport = transport if transport is not None else urllib_transport
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.util.instrumentation import InstrumentationScope

        self._resource = Resource({"service.name": "opspilot"})
        self._scope = InstrumentationScope("opspilot.outbox")

    def _now(self) -> datetime | None:
        return None if self.clock is None else self.clock()

    def _drop(self, ids: Sequence[int], reason: str, result: DeliveryResult) -> None:
        count = self.store.mark_dropped(ids, reason, now=self._now())
        if count:
            result.dropped[reason] = result.dropped.get(reason, 0) + count
            if self.stats is not None:
                self.stats.record_dropped(reason, count)

    def deliver_once(self) -> DeliveryResult:
        result = DeliveryResult()
        trimmed = self.store.trim_backlog(limit=BACKLOG_LIMIT, now=self._now())
        if trimmed:
            result.dropped["BACKLOG"] = trimmed
            if self.stats is not None:
                self.stats.record_dropped("BACKLOG", trimmed)
            _log.warning("trace outbox backlog dropped=%d", trimmed)
        rows = self.store.claim(
            limit=BATCH_SIZE,
            lease=timedelta(seconds=CLAIM_LEASE_SECONDS),
            now=self._now(),
        )
        result.claimed = len(rows)
        if rows:
            self._send(rows, result)
        if self.stats is not None:
            self.stats.set_pending(self.store.count_pending())
        return result

    def _send(self, rows: Sequence[ClaimedRow], result: DeliveryResult) -> None:
        spans: list[ReadableSpan] = []
        roots: dict[UUID, ReadableSpan] = {}
        sent: list[ClaimedRow] = []
        unserializable: list[int] = []
        for row in rows:
            try:
                span, root = self._spans(row)
            except Exception:  # noqa: BLE001 - one bad row never blocks the batch
                unserializable.append(row.entry_id)
                continue
            spans.append(span)
            if root is not None and row.run_id is not None:
                roots[row.run_id] = root
            sent.append(row)
        self._drop(unserializable, "UNSERIALIZABLE", result)
        if not sent:
            return
        try:
            from opentelemetry.exporter.otlp.proto.common.trace_encoder import (
                encode_spans,
            )

            body = encode_spans([*spans, *roots.values()]).SerializeToString()
        except Exception:  # noqa: BLE001
            self._drop([row.entry_id for row in sent], "UNSERIALIZABLE", result)
            return
        try:
            status: int | None = self.transport(
                self.config.endpoint_url,
                body,
                self.config.headers(),
                BATCH_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 - OSError, http.client errors: retry
            # type only: a socket error can quote the endpoint, never the key
            status = None
            _log.warning(
                "trace outbox delivery failed rows=%d error=%s",
                len(sent),
                type(exc).__name__,
            )
        result.status = status
        ids = [row.entry_id for row in sent]
        if status is not None and 200 <= status < 300:
            result.delivered = self.store.mark_delivered(ids, now=self._now())
            if self.stats is not None:
                self.stats.record_delivery()
            return
        if status is not None and 400 <= status < 500 and status not in (408, 429):
            _log.warning("trace outbox rejected rows=%d status=%d", len(sent), status)
            self._drop(ids, "REJECTED", result)
            return
        if status is not None:
            _log.warning("trace outbox retry rows=%d status=%d", len(sent), status)
        exhausted = [row.entry_id for row in sent if row.attempts >= MAX_ATTEMPTS]
        retries = {
            row.entry_id: timedelta(seconds=backoff_seconds(row.attempts))
            for row in sent
            if row.attempts < MAX_ATTEMPTS
        }
        self._drop(exhausted, "MAX_ATTEMPTS", result)
        result.retried = self.store.schedule_retry(retries, now=self._now())

    # -- span construction --------------------------------------------------

    def _spans(self, row: ClaimedRow) -> tuple[ReadableSpan, ReadableSpan | None]:
        """The row's span and, for a Run's stop state, its Run root span."""
        from opentelemetry.trace import SpanContext, TraceFlags

        payload = row.payload
        if not isinstance(payload, Mapping):
            raise _Unserializable("payload")
        trace_id = trace_id_for(row.kind, row.run_id, row.idempotency_key)
        span_id = span_id_for(row.idempotency_key)
        if not trace_id or not span_id:
            raise _Unserializable("zero id")
        flags = TraceFlags(TraceFlags.SAMPLED)
        context = SpanContext(trace_id, span_id, is_remote=False, trace_flags=flags)
        end = _ns(payload["ended_at"]) if "ended_at" in payload else _ns(row.created_at)
        start = _ns(payload["started_at"]) if "started_at" in payload else end
        start = min(start, end)
        attributes = self._attributes(row.kind, payload)
        parent = None
        root = None
        if row.run_id is not None:
            root_context = SpanContext(
                trace_id,
                root_span_id_for(row.run_id),
                is_remote=False,
                trace_flags=flags,
            )
            parent = root_context
            state = payload.get("run_state")
            if (
                row.kind == "run_state"
                and isinstance(state, str)
                and state not in _RUNNING
            ):
                root_start = (
                    _ns(row.run_started_at) if row.run_started_at is not None else end
                )
                root = self._span(
                    "opspilot.run",
                    root_context,
                    None,
                    min(root_start, end),
                    end,
                    {**self._metadata(payload), "langsmith.span.kind": "chain"},
                    error=state == "failed",
                    description=None,
                )
        error, description = self._error(payload)
        span = self._span(
            self._name(row.kind, payload),
            context,
            parent,
            start,
            end,
            attributes,
            error=error,
            description=description,
        )
        return span, root

    def _span(
        self,
        name: str,
        context: Any,
        parent: Any,
        start: int,
        end: int,
        attributes: Mapping[str, Any],
        *,
        error: bool,
        description: str | None,
    ) -> ReadableSpan:
        from opentelemetry.sdk.trace import ReadableSpan
        from opentelemetry.trace import SpanKind
        from opentelemetry.trace.status import Status, StatusCode

        return ReadableSpan(
            name=name,
            context=context,
            parent=parent,
            resource=self._resource,
            attributes=dict(attributes),
            kind=SpanKind.INTERNAL,
            status=Status(StatusCode.ERROR, description)
            if error
            else Status(StatusCode.UNSET),
            start_time=start,
            end_time=end,
            instrumentation_scope=self._scope,
        )

    @staticmethod
    def _name(kind: str, payload: Mapping[str, Any]) -> str:
        if kind == "tool_result":
            tool = payload.get("tool_name")
            return f"opspilot.tool.{_text(tool)}" if tool else "opspilot.tool_result"
        if kind == "run_state":
            return (
                f"opspilot.run_state.{_text(payload.get('run_state', 'UNCLASSIFIED'))}"
            )
        if kind in ("model_step", "generation"):
            return f"opspilot.{kind}"
        raise _Unserializable("kind")

    @staticmethod
    def _metadata(payload: Mapping[str, Any]) -> dict[str, Any]:
        attributes: dict[str, Any] = {}
        for key in _METADATA_KEYS:
            if key not in payload:
                continue
            value = payload[key]
            attributes[f"langsmith.metadata.{key}"] = (
                _count(value)
                if key in ("reasoning_tokens", "budget_seconds")
                else _text(value)
            )
        return attributes

    def _attributes(self, kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        attributes = self._metadata(payload)
        if kind in ("model_step", "generation"):
            attributes["langsmith.span.kind"] = "llm"
            attributes["gen_ai.system"] = "deepseek"
            attributes["langsmith.metadata.ls_provider"] = "deepseek"
            if "model" in payload:
                model = _text(payload["model"])
                attributes["gen_ai.request.model"] = model
                attributes["gen_ai.response.model"] = model
                attributes["langsmith.metadata.ls_model_name"] = model
            for key in _TOKEN_KEYS:
                if key in payload:
                    attributes[f"gen_ai.usage.{key}"] = _count(payload[key])
            if "reasoning_tokens" in payload:
                attributes["gen_ai.usage.reasoning.output_tokens"] = _count(
                    payload["reasoning_tokens"]
                )
        elif kind == "tool_result":
            attributes["langsmith.span.kind"] = "tool"
        elif kind == "run_state":
            attributes["langsmith.span.kind"] = "chain"
        else:
            raise _Unserializable("kind")
        return attributes

    @staticmethod
    def _error(payload: Mapping[str, Any]) -> tuple[bool, str | None]:
        if payload.get("tool_status_class") in _ERROR_TOOL:
            reason = payload.get("tool_reason")
            return True, _text(reason) if reason else None
        if payload.get("run_state") == "failed":
            return True, None
        return False, None
