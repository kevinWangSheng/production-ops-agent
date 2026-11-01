"""Per-request socket ownership for urllib's whole-request deadline.

Keep sockets independently of HTTPConnection.sock: urllib clears that slot
before reading a Connection: close response. Shutdown precedes response.close
so a reader holding the buffered file's lock can first leave recv().
"""

from __future__ import annotations

import ipaddress
import socket
import threading
import time
import urllib.request
from concurrent.futures import Future
from http.client import HTTPConnection, HTTPSConnection
from typing import Any

# #290 (Q9, 2026-11-01): getaddrinfo cannot be interrupted, so a stuck
# resolver used to strand one daemon thread per query. Hostname lookups now
# take one of a fixed number of slots shared by every query transport
# (Prometheus and Jaeger alike). A slot is held until getaddrinfo really
# returns, so resolver threads never exceed RESOLVER_SLOTS; a caller waits
# for a slot only within its own remaining deadline, then times out.
RESOLVER_SLOTS = 2
_resolver_slots = threading.BoundedSemaphore(RESOLVER_SLOTS)

_AddrInfo = list[tuple[Any, Any, int, str, Any]]


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def resolve(host: str, port: int, deadline: float) -> _AddrInfo:
    """``getaddrinfo`` for a TCP peer, bounded by ``deadline`` (monotonic).

    IP literals need no lookup and bypass the slots; any hostname, including
    ``localhost``, takes a slot. Raises ``TimeoutError`` when the deadline
    passes first, whether waiting for a slot or for the lookup.
    """
    if _is_ip_literal(host):
        return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not _resolver_slots.acquire(timeout=remaining):
        raise TimeoutError
    result: Future[_AddrInfo] = Future()

    def lookup() -> None:
        try:
            result.set_result(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except BaseException as error:
            result.set_exception(error)
        finally:
            _resolver_slots.release()

    try:
        threading.Thread(target=lookup, daemon=True, name="otel-resolve").start()
    except BaseException:
        _resolver_slots.release()
        raise
    # concurrent.futures.TimeoutError is the builtin TimeoutError (3.11+).
    return result.result(timeout=max(0.0, deadline - time.monotonic()))


class RequestSockets:
    def __init__(self, timeout: float) -> None:
        self.deadline = time.monotonic() + timeout
        self._lock = threading.Lock()
        self._sockets: list[socket.socket] = []
        self._aborted = False

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        return remaining

    def keep(self, sock: socket.socket) -> None:
        with self._lock:
            if self._aborted:
                self._close(sock)
                raise TimeoutError
            self._sockets.append(sock)

    @staticmethod
    def _close(sock: socket.socket) -> None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()

    def abort(self) -> None:
        with self._lock:
            self._aborted = True
            for sock in self._sockets:
                self._close(sock)

    def connect(
        self, address: tuple[str, int], timeout: Any = None, source_address: Any = None
    ) -> socket.socket:
        # Register before connect(), including proxy TCP connections. Each
        # address attempt consumes the same deadline rather than a new budget.
        # DNS cannot be interrupted by socket shutdown; lookups go through the
        # bounded shared resolver above (#290) so a stuck resolver costs at
        # most RESOLVER_SLOTS threads, not one per query.
        last: OSError | None = None
        host, port = address
        for family, kind, proto, _, peer in resolve(host, port, self.deadline):
            sock = socket.socket(family, kind, proto)
            self.keep(sock)
            try:
                sock.settimeout(self.remaining())
                if source_address:
                    sock.bind(source_address)
                sock.connect(peer)
                return sock
            except OSError as error:
                last = error
                sock.close()
        if last is not None:
            raise last
        raise OSError("No addresses")


class DeadlineHandlers:
    """Track each caller separately, also when a transport is shared."""

    local: threading.local

    def __init__(self, opener: urllib.request.OpenerDirector) -> None:
        existing = getattr(opener, "_opspilot_deadline_handlers", None)
        if isinstance(existing, DeadlineHandlers):
            # An injected opener may serve multiple transport instances. Its
            # installed handlers must use the same per-thread request slot.
            self.local = existing.local
            return
        self.local = threading.local()
        if isinstance(opener, urllib.request.OpenerDirector):
            setattr(opener, "_opspilot_deadline_handlers", self)
            owner = self

            class HTTPHandler(urllib.request.HTTPHandler):
                handler_order = 499

                def http_open(self, req: urllib.request.Request) -> Any:
                    return self.do_open(owner.http_connection, req)

            class HTTPSHandler(urllib.request.HTTPSHandler):
                handler_order = 499

                def https_open(self, req: urllib.request.Request) -> Any:
                    return self.do_open(
                        owner.https_connection,
                        req,
                        context=self._context,  # type: ignore[attr-defined]
                    )

            # Preserve an injected opener's TLS context and proxy/auth handlers.
            context = next(
                (
                    getattr(h, "_context", None)
                    for h in getattr(opener, "handlers", [])
                    if isinstance(h, urllib.request.HTTPSHandler)
                ),
                None,
            )
            opener.add_handler(HTTPHandler())
            opener.add_handler(HTTPSHandler(context=context))

    def http_connection(self, *args: Any, **kwargs: Any) -> HTTPConnection:
        conn = HTTPConnection(*args, **kwargs)
        conn._create_connection = self.local.sockets.connect  # type: ignore[attr-defined]
        return conn

    def https_connection(self, *args: Any, **kwargs: Any) -> HTTPSConnection:
        sockets = self.local.sockets

        class Connection(HTTPSConnection):
            def connect(self) -> None:
                # HTTPConnection handles CONNECT tunnelling using the recorded
                # raw socket. Register the TLS socket before its blocking handshake.
                HTTPConnection.connect(self)
                host = getattr(self, "_tunnel_host", None) or self.host
                self.sock = self._context.wrap_socket(  # type: ignore[attr-defined]
                    self.sock, server_hostname=host, do_handshake_on_connect=False
                )
                sockets.keep(self.sock)
                self.sock.settimeout(sockets.remaining())
                self.sock.do_handshake()

        conn = Connection(*args, **kwargs)
        conn._create_connection = sockets.connect  # type: ignore[attr-defined]
        return conn
