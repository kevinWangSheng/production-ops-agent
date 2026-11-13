"""#581: replay deliveries (traces, alert webhook) never follow a redirect, so
a 30x cannot carry the request or its Authorization header to a new address.
Two loopback HTTP servers only."""

from __future__ import annotations

import threading
import urllib.error
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scripts.itbench_lite import alerts
from scripts.itbench_lite.traces import post_json_loopback


class _Server:
    def __init__(self, location: str | None) -> None:
        self.hits: list[tuple[str, str | None]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                outer.hits.append((self.path, self.headers.get("Authorization")))
                if location is None:
                    self.send_response(200)
                else:
                    self.send_response(302)
                    self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_POST  # noqa: N815

            def log_message(self, *args: object) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def servers() -> Iterator[tuple[_Server, _Server]]:
    target = _Server(None)
    redirector = _Server(target.url + "/elsewhere")
    try:
        yield redirector, target
    finally:
        redirector.close()
        target.close()


def test_plain_delivery_reaches_the_endpoint(servers):
    _, target = servers
    assert post_json_loopback(target.url + "/v1/traces", {"resourceSpans": []}) == 200
    assert target.hits == [("/v1/traces", None)]


def test_trace_post_does_not_follow_a_redirect(servers):
    redirector, target = servers
    with pytest.raises(urllib.error.HTTPError) as caught:
        post_json_loopback(redirector.url + "/v1/traces", {"resourceSpans": []})
    assert caught.value.code == 302
    assert redirector.hits == [("/v1/traces", None)]
    assert target.hits == []


@pytest.mark.parametrize("code_path", ["/intake/alertmanager"])
def test_webhook_post_does_not_carry_the_token_through_a_redirect(servers, code_path):
    redirector, target = servers
    with pytest.raises(urllib.error.HTTPError):
        alerts.post_webhook(
            redirector.url, "aaaaaaaa357", {"version": "4", "alerts": []}
        )
    assert redirector.hits == [(code_path, "Bearer aaaaaaaa357")]
    assert target.hits == []
