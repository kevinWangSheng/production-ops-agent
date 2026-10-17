"""Local fault injection proxy: bounded stall selection and transparent forwarding."""

import http.client
import io
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from scripts.f2_stall_proxy import StallProxy


def test_first_n_matching_requests_stall_then_forward():
    received = []

    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            received.append(
                (self.command, self.path, self.headers.get("Authorization"), body)
            )
            self.send_response(201)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "7")
            self.send_header("X-Upstream", "yes")
            self.end_headers()
            self.wfile.write(b"success")

        def log_message(self, *args):
            pass

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    log = io.StringIO()
    proxy = StallProxy(
        ("127.0.0.1", 0), f"http://127.0.0.1:{upstream.server_port}", log=log
    )
    threads = [threading.Thread(target=s.serve_forever) for s in (upstream, proxy)]
    for thread in threads:
        thread.start()
    try:
        # Nonmatching requests must not consume the two fault injections.
        for path, stalled in [
            ("/ready", False),
            ("/api/v1/query_range", True),
            ("/api/v1/query_range", True),
            ("/api/v1/query_range", False),
        ]:
            conn = http.client.HTTPConnection(
                "127.0.0.1", proxy.server_port, timeout=0.15
            )
            conn.request(
                "POST",
                path + "?query=up",
                b"payload",
                {"Authorization": "Bearer unit-only"},
            )
            response = conn.getresponse()
            if stalled:
                assert response.status == 200
                try:
                    response.read(1)
                except socket.timeout:
                    pass
                else:
                    raise AssertionError("stalled response completed before first drip")
                # Close the response too: it owns the Connection: close socket.
                response.close()
            else:
                assert response.status == 201
                assert response.getheader("X-Upstream") == "yes"
                assert response.read() == b"success"
            conn.close()
        deadline = time.monotonic() + 2
        while len(log.getvalue().splitlines()) < 4 and time.monotonic() < deadline:
            time.sleep(0.02)
        records = [json.loads(line) for line in log.getvalue().splitlines()]
        assert len(records) == 4
        stalled_records = [r for r in records if r["stalled"]]
        assert len(stalled_records) == 2
        assert all(r["client_disconnected_at"] for r in stalled_records)
        assert all(r["time"] and "?" not in r["path"] for r in records)
        assert received == [
            ("POST", "/ready?query=up", "Bearer unit-only", b"payload"),
            ("POST", "/api/v1/query_range?query=up", "Bearer unit-only", b"payload"),
        ]
        assert "Bearer" not in log.getvalue()
    finally:
        for server in (proxy, upstream):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(2)
