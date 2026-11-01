"""v2 injectors of the live driver (#431, V2-R5): harness only.

Interface: ``~/.agent/handoffs/m3-batch4-2026-10-31/seams-431.md`` sections
3-5. The whitelist and the restore intent are in ``scripts.eval_live_lab``;
this module holds what each injector does once validated:

* ``unreachable_config``: the attempt's investigator config with one source
  pointed at a loopback port nothing listens on (process-local).
* ``ModelTransportFault`` / ``model_transport_factory``: a 503 in front of the
  model client's opener at the ``client_factory`` seam (process-local); an
  intercepted request never leaves the process.
* ``span_payload`` / ``OtlpSpanWriter``: one synthetic OTLP/JSON span written
  to the lab collector and read back from Jaeger.
* ``alert_sequence`` / ``alert_body`` / ``AlertIntake``: Alertmanager webhook
  v4 deliveries to the attempt's own in-process product web app, with the
  lab's Alertmanager token and actor; no new role or permission.

PRODUCT-CONSTRAINTS reserves fault injection for isolated harnesses: nothing
here is reachable from the product, the worker or the model.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import secrets
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from email.message import Message
from typing import Any, Literal, Protocol

from scripts.eval_live_lab import (
    LAB_NAMESPACE,
    UNREACHABLE_PORT,
    UNREACHABLE_URLS,
    LabError,
    LiveClock,
    checkpoint,
)

# -- source_unreachable --------------------------------------------------------


def unreachable_config(config: Any, source: str | None) -> Any:
    """``config`` (an ``OtelDemoConfig``) with ``source`` pointed at the
    unreachable loopback URL; unchanged for None."""
    if source is None:
        return config
    if source == "jaeger":
        return replace(config, jaeger_url=UNREACHABLE_URLS["jaeger"])
    if source == "prometheus":
        return replace(config, prometheus_url=UNREACHABLE_URLS["prometheus"])
    raise LabError("INJECTOR_NOT_WHITELISTED")


def connection_refused(url: str, timeout: float = 2.0) -> bool:
    """The probe's self-check: a TCP connect to the URL's port is refused."""
    parts = urllib.parse.urlsplit(url)
    try:
        with socket.create_connection(
            (parts.hostname or "127.0.0.1", parts.port or UNREACHABLE_PORT),
            timeout=timeout,
        ):
            return False
    except OSError:
        return True


# -- model_transport -----------------------------------------------------------

ModelMode = Literal["always_503", "first_503"]
UNAVAILABLE_BODY = b'{"error":{"message":"Service Unavailable"}}'


class Opener(Protocol):
    def open(self, request: urllib.request.Request, timeout: float) -> Any: ...


class ModelTransportFault:
    """A model opener answering 503 in place of the provider.

    ``always_503``: every request; ``first_503``: only the first one this
    instance sees. An intercepted request never reaches ``inner``; the error
    carries a complete body with a matching ``Content-Length``, so the
    recording keeps it as a whole 503 response (``recording.py`` http rule).
    """

    def __init__(self, mode: ModelMode, inner: Opener) -> None:
        if mode not in ("always_503", "first_503"):
            raise LabError("INJECTOR_NOT_WHITELISTED")
        self.mode = mode
        self.inner = inner
        self.intercepted = 0
        self.forwarded = 0

    def open(self, request: urllib.request.Request, timeout: float) -> Any:
        if self.mode == "always_503" or self.intercepted + self.forwarded == 0:
            self.intercepted += 1
            headers = Message()
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(UNAVAILABLE_BODY))
            raise urllib.error.HTTPError(
                request.full_url,
                503,
                "Service Unavailable",
                headers,
                io.BytesIO(UNAVAILABLE_BODY),
            )
        self.forwarded += 1
        return self.inner.open(request, timeout=timeout)


def model_transport_factory(
    mode: ModelMode,
    *,
    inner: Opener | None = None,
    seen: list[ModelTransportFault] | None = None,
) -> Callable[[str, LiveClock], Any]:
    """A ``client_factory`` for ``OtelDemoInvestigator``: each call builds a
    ``DeepSeekClient`` and a fresh ``ModelTransportFault`` in front of
    ``inner`` (None: the client's own opener). ``seen`` collects the faults
    so a caller can read their counters."""
    if mode not in ("always_503", "first_503"):
        raise LabError("INJECTOR_NOT_WHITELISTED")

    def build(key: str, clock: LiveClock) -> Any:
        from opspilot.investigation.client import DeepSeekClient

        client = DeepSeekClient(key, clock=clock)
        fault = front_client(client, mode, inner=inner)
        if seen is not None:
            seen.append(fault)
        return client

    return build


def front_client(
    client: Any, mode: ModelMode, *, inner: Opener | None = None
) -> ModelTransportFault:
    """Put a ``ModelTransportFault`` in front of ``client``'s opener (or of
    ``inner``). The client keeps its recording HTTPS handler (abort on
    timeout); only the opener it calls is replaced."""
    fault = ModelTransportFault(mode, inner if inner is not None else client._opener)
    client._opener = fault  # noqa: SLF001 - harness-side seam (#431)
    return fault


# -- otlp_span -----------------------------------------------------------------

OTLP_URL = "http://127.0.0.1:18080/otlp-http/v1/traces"
JAEGER_API = "http://127.0.0.1:16686/jaeger/ui/api/traces"
SPAN_SERVICE_NAMESPACE = "opentelemetry-demo"
SPAN_INTEGRATION_ID = "m0-otel-20260909"
SPAN_LEAD_SECONDS = 5
SPAN_DURATION_MS = 50
SPAN_VISIBLE_SECONDS = 60.0
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@dataclass(frozen=True)
class SyntheticSpan:
    trace_id: str
    span_id: str
    start: datetime
    end: datetime
    payload: bytes


def _nanos(at: datetime) -> str:
    return str(int(at.timestamp() * 1_000_000) * 1000)


def span_payload(
    params: Mapping[str, str],
    *,
    now: datetime,
    trace_id: str | None = None,
    span_id: str | None = None,
) -> SyntheticSpan:
    """One OTLP/JSON ``ExportTraceServiceRequest`` with a single error span
    (seams section 3): INTERNAL kind, ``exception`` event carrying the
    scenario's message, resource attributes of a demo service."""
    trace_id = trace_id or secrets.token_hex(16)
    span_id = span_id or secrets.token_hex(8)
    start = now - timedelta(seconds=SPAN_LEAD_SECONDS)
    end = start + timedelta(milliseconds=SPAN_DURATION_MS)

    def attr(key: str, value: str) -> dict[str, Any]:
        return {"key": key, "value": {"stringValue": value}}

    body = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        attr("service.name", params["service"]),
                        attr("service.namespace", SPAN_SERVICE_NAMESPACE),
                        attr("opspilot.integration.id", SPAN_INTEGRATION_ID),
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": params["service"]},
                        "spans": [
                            {
                                "traceId": trace_id,
                                "spanId": span_id,
                                "name": params["operation"],
                                "kind": 1,  # SPAN_KIND_INTERNAL
                                "startTimeUnixNano": _nanos(start),
                                "endTimeUnixNano": _nanos(end),
                                "attributes": [],
                                "events": [
                                    {
                                        "timeUnixNano": _nanos(end),
                                        "name": "exception",
                                        "attributes": [
                                            attr("exception.type", "Error"),
                                            attr(
                                                "exception.message",
                                                params["message"],
                                            ),
                                        ],
                                    }
                                ],
                                "status": {"code": 2},  # STATUS_CODE_ERROR
                            }
                        ],
                    }
                ],
            }
        ]
    }
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return SyntheticSpan(trace_id, span_id, start, end, raw)


class SpanWriter(Protocol):
    def write(self, payload: bytes, timeout: float) -> int: ...
    def visible(self, trace_id: str, timeout: float) -> bool: ...


class OtlpSpanWriter:
    """The lab path: POST the OTLP/JSON request, read the trace back by id."""

    def __init__(self, url: str = OTLP_URL, jaeger_api: str = JAEGER_API) -> None:
        self.url = url
        self.jaeger_api = jaeger_api.rstrip("/")

    def write(self, payload: bytes, timeout: float) -> int:
        request = urllib.request.Request(
            self.url,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with _NO_PROXY.open(request, timeout=timeout) as response:
                response.read(64 * 1024)
                return int(response.status)
        except urllib.error.HTTPError as error:
            return int(error.code)
        except (urllib.error.URLError, OSError):
            return 0

    def visible(self, trace_id: str, timeout: float) -> bool:
        try:
            with _NO_PROXY.open(
                f"{self.jaeger_api}/{trace_id}", timeout=timeout
            ) as response:
                data = json.loads(response.read(1024 * 1024))
        except (urllib.error.URLError, OSError, ValueError):
            return False
        rows = data.get("data") if isinstance(data, dict) else None
        return bool(rows) and any(
            span.get("traceID") == trace_id
            for row in rows
            if isinstance(row, dict)
            for span in row.get("spans") or []
            if isinstance(span, dict)
        )


def write_span(
    params: Mapping[str, str],
    *,
    writer: SpanWriter,
    clock: LiveClock,
    on_planned: Callable[[SyntheticSpan], None],
    budget_seconds: float = SPAN_VISIBLE_SECONDS,
    poll_seconds: float = 5.0,
) -> SyntheticSpan:
    """Plan the span, record it (``on_planned`` writes the restore intent
    first), POST it and wait until Jaeger returns it by trace id."""
    span = span_payload(params, now=clock.now())
    on_planned(span)
    checkpoint()
    status = writer.write(span.payload, timeout=10.0)
    checkpoint()
    if not 200 <= status < 300:
        raise LabError("OTLP_SPAN_WRITE_FAILED")
    started = clock.monotonic()
    while True:
        checkpoint()
        if writer.visible(span.trace_id, timeout=5.0):
            return span
        if clock.monotonic() - started >= budget_seconds:
            raise LabError("OTLP_SPAN_NOT_VISIBLE")
        clock.sleep(poll_seconds)


# -- alert_delivery ------------------------------------------------------------

ALERTNAME = "CheckoutPlaceOrderErrorRatioHigh"
ALERT_LABELS: Mapping[str, str] = {
    "alertname": ALERTNAME,
    "severity": "critical",
    "cluster": "opspilot-m1",
    "opspilot_integration_id": SPAN_INTEGRATION_ID,
    "namespace": LAB_NAMESPACE,
    "service": "checkout",
}
ALERT_ANNOTATIONS: Mapping[str, str] = {
    "summary": "checkout PlaceOrder error ratio above 50%",
    "description": (
        "100% of checkout PlaceOrder server spans ended in error over the last "
        "5 minutes."
    ),
}
ZERO_TIME = "0001-01-01T00:00:00Z"
GENERATOR_URL = "http://prometheus:9090/graph?g0.expr=checkout_placeorder_error_ratio"
INTAKE_PATH = "/intake/alertmanager"
ALERT_ACTOR = "alertmanager"
#: The one registry entry the attempt's workbench resolves alerts against.
ALERT_TARGET: Mapping[str, Any] = {
    "integration_id": SPAN_INTEGRATION_ID,
    "cluster_uid": "opspilot-m1",
    "namespace": LAB_NAMESPACE,
    "workload": "checkout",
    "health_profile_id": "otel-demo-checkout",
    "match": {
        "version": 1,
        "labels": {"namespace": LAB_NAMESPACE, "service": "checkout"},
    },
}
EXPECTED_OUTCOMES: Mapping[str, tuple[str, ...]] = {
    "single": ("created",),
    "duplicate": ("created", "replayed", "replayed"),
    "refire": ("created", "resolved_attached", "attached"),
    "unresolved_labels": ("handoff_created",),
}


def _utc(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Delivery:
    """One planned webhook: what to send, and the wait before sending it."""

    seq: int
    status: Literal["firing", "resolved"]
    episode: int  # 1 = startsAt of the first episode, 2 = the re-fired one
    wait_seconds: int


def alert_sequence(params: Mapping[str, Any]) -> tuple[Delivery, ...]:
    """The ordered deliveries of a validated ``alert_delivery`` (seams §5)."""
    pattern = params["pattern"]
    if pattern in ("single", "unresolved_labels"):
        return (Delivery(1, "firing", 1, 0),)
    interval = int(params["interval_seconds"])
    if pattern == "duplicate":
        return tuple(
            Delivery(i + 1, "firing", 1, 0 if i == 0 else interval)
            for i in range(int(params["deliveries"]))
        )
    if pattern == "refire":
        return (
            Delivery(1, "firing", 1, 0),
            Delivery(2, "resolved", 1, interval),
            Delivery(3, "firing", 2, interval),
        )
    raise LabError("INJECTOR_NOT_WHITELISTED")


def alert_labels(params: Mapping[str, Any]) -> dict[str, str]:
    labels = dict(ALERT_LABELS)
    if params["pattern"] == "unresolved_labels":
        labels["namespace"] = str(params["namespace"])
    return labels


def alert_body(
    params: Mapping[str, Any],
    *,
    status: Literal["firing", "resolved"],
    fingerprint: str,
    starts_at: datetime,
    ends_at: datetime | None,
) -> dict[str, Any]:
    """An Alertmanager webhook v4 body with exactly one alert."""
    labels = alert_labels(params)
    alert = {
        "status": status,
        "labels": labels,
        "annotations": dict(ALERT_ANNOTATIONS),
        "startsAt": _utc(starts_at),
        "endsAt": ZERO_TIME if ends_at is None else _utc(ends_at),
        "generatorURL": GENERATOR_URL,
        "fingerprint": fingerprint,
    }
    return {
        "version": "4",
        "groupKey": '{}:{alertname="%s"}' % ALERTNAME,
        "truncatedAlerts": 0,
        "status": status,
        "receiver": "opspilot",
        "groupLabels": {"alertname": ALERTNAME},
        "commonLabels": labels,
        "commonAnnotations": dict(ALERT_ANNOTATIONS),
        "externalURL": "http://alertmanager:9093",
        "alerts": [alert],
    }


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def _asgi(
    app: Any, path: str, headers: Mapping[str, str], body: bytes
) -> tuple[int, bytes]:
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    raw_headers.append((b"content-length", str(len(body)).encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": raw_headers,
        "client": ("127.0.0.1", 0),
        "server": ("127.0.0.1", 0),
    }
    out: dict[str, Any] = {"status": 0, "body": bytearray()}
    sent = False
    done = asyncio.Event()

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await done.wait()
        return {"type": "http.disconnect"}

    async def send(message: Mapping[str, Any]) -> None:
        if message["type"] == "http.response.start":
            out["status"] = int(message["status"])
        elif message["type"] == "http.response.body":
            out["body"] += message.get("body", b"")
            if not message.get("more_body", False):
                done.set()

    await app(scope, receive, send)
    return out["status"], bytes(out["body"])


class AlertTransport(Protocol):
    def post(
        self, path: str, headers: Mapping[str, str], body: bytes
    ) -> tuple[int, bytes]: ...


class AsgiTransport:
    """In-process: the product's own route and authenticator, no port."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.requests: list[tuple[str, dict[str, str], bytes]] = []

    def post(
        self, path: str, headers: Mapping[str, str], body: bytes
    ) -> tuple[int, bytes]:
        self.requests.append((path, dict(headers), body))
        return asyncio.run(_asgi(self.app, path, headers, body))


@dataclass
class DeliveryResult:
    seq: int
    status: str
    starts_at: str
    http_status: int
    outcome: str | None
    reason: str | None
    incident_id: str | None
    run_id: str | None

    def as_json(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "status": self.status,
            "starts_at": self.starts_at,
            "http_status": self.http_status,
            "outcome": self.outcome,
            "reason": self.reason,
            "incident_id": self.incident_id,
            "run_id": self.run_id,
        }


@dataclass
class AlertRun:
    """What the deliveries produced, in order."""

    fingerprint: str
    deliveries: list[DeliveryResult] = field(default_factory=list)

    @property
    def first(self) -> DeliveryResult:
        return self.deliveries[0]


def deliver_alerts(
    params: Mapping[str, Any],
    *,
    transport: AlertTransport,
    token: str,
    clock: LiveClock,
    first_starts_at: datetime,
    fingerprint: str | None = None,
) -> AlertRun:
    """Send the sequence in order, waiting ``wait_seconds`` before each; every
    delivery must be answered 200 (else ``ALERT_DELIVERY_FAILED``)."""
    fingerprint = fingerprint or secrets.token_hex(8)
    run = AlertRun(fingerprint)
    starts = {1: first_starts_at.replace(microsecond=0)}
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    for delivery in alert_sequence(params):
        if delivery.wait_seconds:
            clock.sleep(delivery.wait_seconds)
        checkpoint()
        if delivery.episode not in starts:
            starts[delivery.episode] = clock.now().replace(microsecond=0)
        ends = clock.now() if delivery.status == "resolved" else None
        body = alert_body(
            params,
            status=delivery.status,
            fingerprint=fingerprint,
            starts_at=starts[delivery.episode],
            ends_at=ends,
        )
        raw = json.dumps(body, sort_keys=True).encode()
        status, answer = transport.post(INTAKE_PATH, headers, raw)
        checkpoint()
        item: Mapping[str, Any] = {}
        try:
            results = json.loads(answer).get("results") or []
            item = results[0] if results else {}
        except (ValueError, AttributeError):
            item = {}
        run.deliveries.append(
            DeliveryResult(
                seq=delivery.seq,
                status=delivery.status,
                starts_at=_utc(starts[delivery.episode]),
                http_status=status,
                outcome=item.get("outcome"),
                reason=item.get("reason"),
                incident_id=item.get("incident_id"),
                run_id=item.get("run_id"),
            )
        )
        if status != 200:
            raise LabError("ALERT_DELIVERY_FAILED")
    return run


def intake_counts(dsn: str) -> dict[str, int]:
    """incidents / runs / episodes as committed in the attempt's PostgreSQL
    (seams §5): episodes are distinct firing (fingerprint, starts_at)."""
    import psycopg

    with psycopg.connect(dsn) as conn:
        incidents = conn.execute("SELECT count(*) FROM opspilot_incidents").fetchone()
        runs = conn.execute("SELECT count(*) FROM opspilot_runs").fetchone()
        episodes = conn.execute(
            "SELECT count(*) FROM (SELECT DISTINCT fingerprint, starts_at FROM "
            "opspilot_alert_deliveries WHERE status='firing') AS e"
        ).fetchone()
    return {
        "incidents": int(incidents[0]),
        "runs": int(runs[0]),
        "episodes": int(episodes[0]),
    }


def intake_record(
    entry: str,
    run: AlertRun | None,
    counts: Mapping[str, int | None],
) -> dict[str, Any]:
    """``runs/<attempt_id>/intake.json`` (S5)."""
    return {
        "entry": entry,
        "deliveries": [] if run is None else [d.as_json() for d in run.deliveries],
        "counts": {
            "incidents": counts.get("incidents"),
            "runs": counts.get("runs"),
            "episodes": counts.get("episodes"),
        },
    }


def alert_entry(
    params: Mapping[str, Any],
    *,
    web: Any,
    face: Any,
    versions: Mapping[str, str],
    clock: LiveClock,
    round_id: str,
    first_starts_at: datetime,
    token: str | None = None,
    transport_seen: list[AsgiTransport] | None = None,
) -> AlertRun:
    """Deliver ``params`` to the attempt's own product web app (seams §5):
    a workbench over ``web`` (the attempt PG's web role) with the one lab
    target, authenticated by the lab Alertmanager token as ``alertmanager``."""
    from opspilot.investigation.limits import M1_FROZEN_LIMITS, RUN_WALL_SECONDS
    from opspilot.tools import otel_demo
    from opspilot.web import (
        AuthConfig,
        Authenticator,
        DurableClock,
        DurableEventLog,
        DurableEvidenceStore,
        DurableIncidentStore,
        DurableWebLedger,
        Workbench,
        create_app,
    )
    from opspilot.web.store import MappingTargetRegistry
    from scripts import kind_lab

    token = kind_lab.webhook_token() if token is None else token
    seats = (DurableEventLog(web), DurableEvidenceStore(web), DurableWebLedger(web))
    for seat in seats:
        checkpoint()
        seat.install()
    events, evidence, ledger = seats
    workbench = Workbench(
        incidents=DurableIncidentStore(web),
        events=events,
        evidence=evidence,
        ledger=ledger,
        run_versions=dict(versions),
        budget_limit=M1_FROZEN_LIMITS.model_requests,
        run_seconds=RUN_WALL_SECONDS,
        tool_face=face,
        targets=MappingTargetRegistry({otel_demo.TARGET_ID: dict(ALERT_TARGET)}),
    )
    auth = AuthConfig(
        ui_users={},
        event_tokens={token_digest(token): ALERT_ACTOR},
        auth_revision=f"eval-live-{round_id}",
    )
    transport = AsgiTransport(
        create_app(workbench, Authenticator(auth), DurableClock(web))
    )
    if transport_seen is not None:
        transport_seen.append(transport)
    return deliver_alerts(
        params,
        transport=transport,
        token=token,
        clock=clock,
        first_starts_at=first_starts_at,
    )
