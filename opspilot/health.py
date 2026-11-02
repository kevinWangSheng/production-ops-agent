"""Process health listener for web, worker and Observer (F8 step 1, #413).

Each runtime starts one small HTTP listener of its own, separate from the
workbench's 8080 app (F8-Q2): ``OPSPILOT_HEALTH_BIND`` is ``host:port``,
``127.0.0.1:9464`` by default for a bare process (the image sets
``0.0.0.0:9464``), and an empty value turns it off; any other value that
cannot be bound refuses to start the process. It answers GET
``/healthz``, ``/readyz`` and ``/metrics`` and 404 to anything else, without
authentication, so every body is fixed keys, fixed codes and the version:
never a report, evidence, provider text or a credential (F8-R1).

* ``/healthz``: the process is alive.
* ``/readyz``: the process can do its work, else 503 with one reason code
  (F8-R4). The web checks the schema head and its pool on every request; the
  worker and the Observer never touch the database from here (the worker's
  pool has no spare connection) and read what their polling thread last
  recorded instead.
* ``/metrics``: Prometheus text, process-level series only (F8-R2/R3); no
  Run or incident label.

stdlib only (``http.server``), and nothing from the investigation side at
import time: the Observer imports this module (C3 section 3, decision D3).
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import socket
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol, TypeVar

from opspilot import __version__

__all__ = [
    "BIND_ENV",
    "DEFAULT_BIND",
    "HealthServer",
    "ModelCallCounter",
    "ModelCallStats",
    "OUTBOX_DROP_REASONS",
    "OutboxStats",
    "Readiness",
    "health_bind",
    "observer_metrics",
    "poll_readiness",
    "serve_health",
    "start_health_server",
    "ttl_cache",
    "web_metrics",
    "web_readiness",
    "worker_metrics",
]

_log = logging.getLogger("opspilot.health")

BIND_ENV = "OPSPILOT_HEALTH_BIND"
DEFAULT_BIND = "127.0.0.1:9464"
REVISION_ENV = "OPSPILOT_REVISION"
MAX_RESPONSE_BYTES = 4096
#: Requests handled at once; any beyond get 503 ``busy`` at once, never a
#: queue or a new thread, so a flood from the container network cannot pile
#: up threads (or, on the web, pool connections).
MAX_CONCURRENT_REQUESTS = 8
#: How long the web reuses one probe's result: at most one transaction per
#: TTL whatever the request rate; below the lab scrape interval (15s).
PROBE_TTL_SECONDS = 5.0

SCHEMA_NOT_AT_HEAD = "SCHEMA_NOT_AT_HEAD"
POOL_UNAVAILABLE = "POOL_UNAVAILABLE"
POLL_STALE = "POLL_STALE"
STOPPING = "STOPPING"

#: A poll older than this many intervals is stale (F8-R4).
STALE_POLL_INTERVALS = 3

_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_REVISION = re.compile(r"[0-9A-Za-z._-]{1,64}")


@dataclass(frozen=True)
class Readiness:
    ready: bool
    reason: str | None = None


_READY = Readiness(True, None)


def poll_readiness(
    *,
    last_success_monotonic: float | None,
    poll_interval_seconds: float,
    now_monotonic: float,
    stopping: bool,
) -> Readiness:
    """Worker/Observer: ready while not stopping and the last successful
    poll is at most three intervals old; no success yet is stale."""
    if stopping:
        return Readiness(False, STOPPING)
    if (
        last_success_monotonic is None
        or now_monotonic - last_success_monotonic
        > STALE_POLL_INTERVALS * poll_interval_seconds
    ):
        return Readiness(False, POLL_STALE)
    return _READY


def web_readiness(*, schema_at_head: bool, pool_ok: bool) -> Readiness:
    """Web: a pool that cannot serve a transaction says nothing about the
    schema, so it is reported first."""
    if not pool_ok:
        return Readiness(False, POOL_UNAVAILABLE)
    if not schema_at_head:
        return Readiness(False, SCHEMA_NOT_AT_HEAD)
    return _READY


# -- metrics text ----------------------------------------------------------------


def _number(value: float) -> str:
    if isinstance(value, float) and math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    return repr(value) if isinstance(value, float) else str(int(value))


def _family(
    name: str, kind: str, samples: list[tuple[dict[str, str], float]]
) -> list[str]:
    lines = [f"# TYPE {name} {kind}"]
    for labels, value in samples:
        rendered = ",".join(f'{key}="{val}"' for key, val in labels.items())
        series = f"{name}{{{rendered}}}" if rendered else name
        lines.append(f"{series} {_number(value)}")
    return lines


def _revision() -> str:
    raw = os.environ.get(REVISION_ENV, "")
    return raw if _REVISION.fullmatch(raw) else "unknown"


def _build_info() -> list[str]:
    return _family(
        "opspilot_build_info",
        "gauge",
        [({"version": __version__, "revision": _revision()}, 1)],
    )


def _poll_age(last_poll_age: float | None) -> list[str]:
    return _family(
        "opspilot_last_poll_age_seconds",
        "gauge",
        [({}, math.inf if last_poll_age is None else last_poll_age)],
    )


def _text(lines: list[str]) -> str:
    return "\n".join(lines) + "\n"


def web_metrics(*, schema_at_head: bool, queue_depth: Mapping[str, int] | None) -> str:
    """``queue_depth`` (``queued``/``running_lapsed``) is ``None`` when the
    database could not be read; the series is then absent, not zero."""
    lines = _build_info()
    lines += _family(
        "opspilot_schema_at_head", "gauge", [({}, 1 if schema_at_head else 0)]
    )
    if queue_depth is not None:
        lines += _family(
            "opspilot_queue_depth",
            "gauge",
            [({"state": state}, count) for state, count in queue_depth.items()],
        )
    return _text(lines)


def worker_metrics(
    *,
    last_poll_age: float | None,
    in_flight: int,
    model: Mapping[str, Any],
    outbox: Mapping[str, Any] | None = None,
) -> str:
    """``model`` is a :meth:`ModelCallStats.snapshot`, ``outbox`` an
    :meth:`OutboxStats.snapshot` (#428, OB-R7; absent = no outbox series)."""
    lines = _build_info() + _poll_age(last_poll_age)
    lines += _family("opspilot_in_flight_attempts", "gauge", [({}, in_flight)])
    calls: Mapping[str, int] = model["calls"]
    lines += _family(
        "opspilot_model_calls_total",
        "counter",
        [({"category": category}, count) for category, count in sorted(calls.items())],
    )
    lines += _family(
        "opspilot_model_consecutive_failures",
        "gauge",
        [({}, model["consecutive_failures"])],
    )
    last = model["last_category"]
    lines += _family(
        "opspilot_last_model_call_category",
        "gauge",
        [] if last is None else [({"category": last}, 1)],
    )
    if outbox is not None:
        lines += _outbox_lines(outbox)
    return _text(lines)


def _outbox_lines(outbox: Mapping[str, Any]) -> list[str]:
    pending = outbox["pending"]
    lines = _family(
        "opspilot_trace_outbox_pending",
        "gauge",
        [] if pending is None else [({}, pending)],
    )
    dropped: Mapping[str, int] = outbox["dropped"]
    lines += _family(
        "opspilot_trace_outbox_dropped_total",
        "counter",
        [
            ({"reason": reason}, dropped.get(reason, 0))
            for reason in sorted(OUTBOX_DROP_REASONS)
        ],
    )
    age = outbox["last_delivery_age"]
    lines += _family(
        "opspilot_trace_outbox_last_delivery_age_seconds",
        "gauge",
        [({}, math.inf if age is None else age)],
    )
    lines += _family(
        "opspilot_trace_export_enabled",
        "gauge",
        [
            ({"reason": code}, 1 if code == "ENABLED" else 0)
            for code in outbox["export_status"]
        ],
    )
    return lines


def observer_metrics(
    *, last_poll_age: float | None, observation_due: int | None
) -> str:
    """``observation_due`` is ``None`` until the polling thread has counted."""
    lines = _build_info() + _poll_age(last_poll_age)
    if observation_due is not None:
        lines += _family("opspilot_observation_due", "gauge", [({}, observation_due)])
    return _text(lines)


# -- model calls -------------------------------------------------------------------


#: ``opspilot_trace_outbox_dropped_total`` reasons (OB-R5), always exported.
OUTBOX_DROP_REASONS = frozenset(
    {"BACKLOG", "MAX_ATTEMPTS", "REJECTED", "UNSERIALIZABLE"}
)


class OutboxStats:
    """The worker outbox thread's readings for ``/metrics`` (OB-R7): the
    thread writes, the health listener reads; never a query. ``export_status``
    is fixed at startup (``tracing.export_status``)."""

    def __init__(
        self,
        export_status: tuple[str, ...] = ("TRACE_MODE_OFF",),
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lock = threading.Lock()
        self._monotonic = monotonic
        self._export_status = tuple(export_status)
        self._pending: int | None = None
        self._dropped: dict[str, int] = dict.fromkeys(OUTBOX_DROP_REASONS, 0)
        self._last_delivery: float | None = None

    def set_pending(self, count: int) -> None:
        with self._lock:
            self._pending = count

    def record_dropped(self, reason: str, count: int = 1) -> None:
        if count <= 0:
            return
        with self._lock:
            self._dropped[reason] = self._dropped.get(reason, 0) + count

    def record_delivery(self) -> None:
        with self._lock:
            self._last_delivery = self._monotonic()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            last = self._last_delivery
            return {
                "pending": self._pending,
                "dropped": dict(self._dropped),
                "last_delivery_age": (
                    None if last is None else max(0.0, self._monotonic() - last)
                ),
                "export_status": self._export_status,
            }


class ModelCallStats:
    """Process-wide model-call tally shared by every wrapped client: calls
    per category, the failures since the last success, the last category."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: dict[str, int] = {}
        self._consecutive = 0
        self._last: str | None = None

    def record(self, category: str) -> None:
        with self._lock:
            self._calls[category] = self._calls.get(category, 0) + 1
            self._consecutive = 0 if category == "ok" else self._consecutive + 1
            self._last = category

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "calls": dict(self._calls),
                "consecutive_failures": self._consecutive,
                "last_category": self._last,
            }


class _Completes(Protocol):
    def complete(self, call: Any) -> Any: ...


class ModelCallCounter:
    """``client.complete`` unchanged (same argument, reply and exception),
    recording a success as ``ok`` and a ``ModelError`` as its fixed code
    (F8-R3). Any other exception passes through uncounted: it is not a
    provider outcome. A code outside the fixed-code shape is recorded as
    ``UNKNOWN`` so the label set stays bounded."""

    def __init__(self, client: _Completes, stats: ModelCallStats | None = None) -> None:
        # Imported here, not at module level: the Observer imports this
        # module and must not load the investigation side (C3 D3).
        from opspilot.investigation.loop import ModelError

        self._client = client
        self._error = ModelError
        self.stats = stats if stats is not None else ModelCallStats()

    def complete(self, call: Any) -> Any:
        try:
            reply = self._client.complete(call)
        except self._error as exc:
            code = exc.code if _CODE.fullmatch(str(exc.code)) else "UNKNOWN"
            self.stats.record(code)
            raise
        self.stats.record("ok")
        return reply

    def snapshot(self) -> dict[str, Any]:
        return self.stats.snapshot()


# -- listener ------------------------------------------------------------------------

_T = TypeVar("_T")


def ttl_cache(
    probe: Callable[[], _T],
    ttl_seconds: float,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> Callable[[], _T]:
    """``probe`` at most once per ``ttl_seconds``: concurrent callers wait
    for the one in flight and share its result. A probe that raises is not
    cached."""
    lock = threading.Lock()
    cached: list[tuple[float, _T]] = []

    def get() -> _T:
        with lock:
            if cached and clock() - cached[0][0] < ttl_seconds:
                return cached[0][1]
            value = probe()
            cached[:] = [(clock(), value)]
            return value

    return get


_BUSY = (
    b"HTTP/1.0 503 Service Unavailable\r\n"
    b"Content-Type: application/json\r\n"
    b"Content-Length: 18\r\n"
    b"Cache-Control: no-store\r\n"
    b"Connection: close\r\n\r\n"
    b'{"status": "busy"}'
)


class _BoundedServer(ThreadingHTTPServer):
    """At most ``max_concurrent`` handler threads; a connection beyond that
    is answered 503 on the accepting thread and closed."""

    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        max_concurrent: int,
    ) -> None:
        self._slots = threading.BoundedSemaphore(max_concurrent)
        super().__init__(address, handler)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            self._refuse(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def _refuse(self, request: socket.socket) -> None:
        try:
            # Read what the client already sent (bounded wait), so closing
            # does not reset the connection before it sees the answer.
            request.settimeout(0.1)
            try:
                request.recv(4096)
            except OSError:
                pass
            request.sendall(_BUSY)
        except OSError:
            pass
        finally:
            self.shutdown_request(request)


class HealthServer:
    def __init__(self, server: ThreadingHTTPServer, thread: threading.Thread) -> None:
        self._server = server
        self._thread = thread

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(5)


def _split_bind(bind: str) -> tuple[str, int]:
    host, separator, port = bind.rpartition(":")
    if not separator or not host or not port.isdigit() or int(port) > 65535:
        raise ValueError(f"{BIND_ENV} must be host:port")
    return host, int(port)


def _handler(
    readiness: Callable[[], Readiness], metrics: Callable[[], str]
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "opspilot-health"
        sys_version = ""
        # A client that opens a connection and sends nothing gives its
        # thread back after this many seconds.
        timeout = 5

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            if len(body) > MAX_RESPONSE_BYTES:
                # Cut on a line boundary so the metrics text stays parseable.
                body = body[: body.rfind(b"\n", 0, MAX_RESPONSE_BYTES) + 1]
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, payload: Mapping[str, str]) -> None:
            self._send(status, "application/json", json.dumps(payload).encode())

        def _not_found(self) -> None:
            self._json(404, {"status": "not_found"})

        def do_GET(self) -> None:  # noqa: N802 - http.server naming
            path = self.path.split("?", 1)[0]
            try:
                if path == "/healthz":
                    self._json(200, {"status": "ok", "version": __version__})
                elif path == "/readyz":
                    state = readiness()
                    if state.ready:
                        self._json(200, {"status": "ready", "version": __version__})
                    else:
                        self._json(
                            503, {"status": "not_ready", "reason": str(state.reason)}
                        )
                elif path == "/metrics":
                    self._send(
                        200,
                        "text/plain; version=0.0.4; charset=utf-8",
                        metrics().encode(),
                    )
                else:
                    self._not_found()
            except Exception as exc:  # noqa: BLE001 - the listener stays up
                # Only the type: a message could quote a driver or provider.
                _log.warning(
                    "health probe failed path=%s error=%s", path, type(exc).__name__
                )
                self._json(500, {"status": "error"})

        def __getattr__(self, name: str) -> Any:
            # Every other method (POST, HEAD, BREW, ...) is a 404, not 501.
            if name.startswith("do_"):
                return self._not_found
            raise AttributeError(name)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            # One line per probe every few seconds is noise; nothing to log.
            return

    return Handler


def start_health_server(
    bind: str | None,
    *,
    readiness: Callable[[], Readiness],
    metrics: Callable[[], str],
    max_concurrent: int = MAX_CONCURRENT_REQUESTS,
) -> HealthServer | None:
    """Serve on ``bind`` from a daemon thread; ``None``/empty is off. Raises
    ``ValueError`` for a malformed bind and ``OSError`` when it cannot bind.
    At most ``max_concurrent`` requests are handled at once."""
    if not bind:
        return None
    host, port = _split_bind(bind)
    server = _BoundedServer((host, port), _handler(readiness, metrics), max_concurrent)
    thread = threading.Thread(
        target=server.serve_forever, name="opspilot-health", daemon=True
    )
    thread.start()
    return HealthServer(server, thread)


def health_bind(env: Mapping[str, str]) -> str | None:
    return env.get(BIND_ENV, DEFAULT_BIND).strip() or None


def serve_health(
    env: Mapping[str, str],
    *,
    readiness: Callable[[], Readiness],
    metrics: Callable[[], str],
) -> HealthServer | None:
    """The entry points' start. A malformed ``OPSPILOT_HEALTH_BIND`` or one
    that cannot be bound (a port already taken included) refuses to start:
    a process without its health surface would hide from external detection.
    Only the empty value runs without a listener, so two bare processes on
    one host need distinct binds (or an empty one)."""
    bind = health_bind(env)
    try:
        server = start_health_server(bind, readiness=readiness, metrics=metrics)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    except OSError as exc:
        message = (
            f"{BIND_ENV}={bind} cannot be bound (errno={exc.errno}); set a free "
            f"host:port, or an empty value to run without the health listener"
        )
        _log.error(message)
        raise SystemExit(message) from None
    if server is not None:
        _log.info("health listener bind=%s port=%d", bind, server.port)
    return server
