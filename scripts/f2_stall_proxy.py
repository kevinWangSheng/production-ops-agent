#!/usr/bin/env python3
"""Host-only fault injection: stall the first N matching requests.

Example: python scripts/f2_stall_proxy.py --listen 127.0.0.1:19091 \
    --upstream http://127.0.0.1:19090 --count 2
Logs omit credentials and query values. Stalled requests never reach upstream.
"""

from __future__ import annotations

import argparse
import http.client
import json
import select
import socket
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TextIO
from urllib.parse import urlsplit

HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


class StallProxy(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        upstream: str,
        *,
        path: str = "/api/v1/query_range",
        count: int = 2,
        log: TextIO | None = None,
    ) -> None:
        parsed = urlsplit(upstream)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("upstream must be an HTTP(S) URL")
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("upstream must not contain query, fragment or credentials")
        if count < 0:
            raise ValueError("count must be nonnegative")
        self.upstream = parsed
        self.match_path = path
        self.remaining = count
        self.lock = threading.Lock()
        self.log = log
        self.stopping = threading.Event()
        super().__init__(address, ProxyHandler)

    def server_close(self) -> None:
        self.stopping.set()
        super().server_close()


class ProxyHandler(BaseHTTPRequestHandler):
    server: StallProxy

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _request_body(self) -> bytes:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            parts = []
            while True:
                size = int(self.rfile.readline().split(b";", 1)[0], 16)
                if not size:
                    while self.rfile.readline() not in {b"\r\n", b""}:
                        pass
                    return b"".join(parts)
                parts.append(self.rfile.read(size))
                if self.rfile.read(2) != b"\r\n":
                    raise ValueError("invalid chunk")
        return self.rfile.read(int(self.headers.get("Content-Length", "0")))

    def _handle(self) -> None:
        path = urlsplit(self.path).path
        with self.server.lock:
            stalled = path == self.server.match_path and self.server.remaining > 0
            if stalled:
                self.server.remaining -= 1
        record: dict[str, object] = {
            "time": datetime.now(timezone.utc).isoformat(),
            "path": path,
            "stalled": stalled,
            "client_disconnected_at": None,
        }
        self.close_connection = True
        try:
            if stalled:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.flush()
                next_byte = time.monotonic() + 5
                while not self.server.stopping.is_set():
                    readable, _, _ = select.select([self.connection], [], [], 0.1)
                    if readable:
                        if not self.connection.recv(4096, socket.MSG_PEEK):
                            record["client_disconnected_at"] = datetime.now(
                                timezone.utc
                            ).isoformat()
                            break
                        # Drain any request body so readability does not spin.
                        self.connection.recv(4096)
                    if time.monotonic() >= next_byte:
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        next_byte = time.monotonic() + 5
            else:
                self._forward()
        except (OSError, http.client.HTTPException, ValueError):
            record["client_disconnected_at"] = datetime.now(timezone.utc).isoformat()
        finally:
            with self.server.lock:
                print(json.dumps(record), file=self.server.log, flush=True)

    def _forward(self) -> None:
        upstream = self.server.upstream
        cls = (
            http.client.HTTPSConnection
            if upstream.scheme == "https"
            else http.client.HTTPConnection
        )
        conn = cls(upstream.hostname, upstream.port, timeout=30)
        blocked = HOP_HEADERS | {
            part.strip().lower()
            for part in self.headers.get("Connection", "").split(",")
        }
        headers = {
            k: v
            for k, v in self.headers.items()
            if k.lower() not in blocked | {"host", "content-length"}
        }
        body = self._request_body()
        if body or self.headers.get("Content-Length") is not None:
            headers["Content-Length"] = str(len(body))
        try:
            conn.request(
                self.command, upstream.path.rstrip("/") + self.path, body, headers
            )
            response = conn.getresponse()
            self.send_response(response.status, response.reason)
            blocked = HOP_HEADERS | {
                part.strip().lower()
                for part in response.getheader("Connection", "").split(",")
            }
            for key, value in response.getheaders():
                if key.lower() not in blocked:
                    self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                while chunk := response.read1(65536):
                    self.wfile.write(chunk)
        finally:
            conn.close()

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _handle


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", default="127.0.0.1:19091", help="HOST:PORT")
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--path", default="/api/v1/query_range")
    parser.add_argument("--count", type=int, default=2)
    args = parser.parse_args()
    host, port = args.listen.rsplit(":", 1)
    server = StallProxy(
        (host, int(port)), args.upstream, path=args.path, count=args.count
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
