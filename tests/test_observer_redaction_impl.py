"""Implementer self-checks for #254: the Observer's Prometheus client keeps and
parses only credential-redacted bodies. Real sockets against a local server.

The independent acceptance and contract tests live elsewhere; these only
pin the implementation's own edges (redaction before parse, non-UTF-8,
growth past the limit, error and cut bodies)."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from opspilot.observer.prometheus import (
    RESPONSE_LIMIT_BYTES,
    PrometheusReadOnlySource,
)
from opspilot.redaction import REDACTED_CREDENTIAL

AT = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
SECRET = "s3cr3t-observer-zz9"


def _vector(metrics: list[dict[str, str]], value: str = "0.5") -> bytes:
    return json.dumps(
        {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {"metric": m, "value": [1792670400.123, value]} for m in metrics
                ],
            },
        }
    ).encode()


def _serve(status: int, body: bytes, *, cut: bool = False):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body) + (100 if cut else 0)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def query():
    servers = []

    def run(status: int, body: bytes, *, cut: bool = False, vector: bool = False):
        server = _serve(status, body, cut=cut)
        servers.append(server)
        src = PrometheusReadOnlySource(f"http://127.0.0.1:{server.server_port}")
        call = src.instant_vector if vector else src.instant
        return call("up", at=AT, timeout_seconds=3)

    yield run
    for server in servers:
        server.shutdown()
        server.server_close()


def test_a_secret_label_is_redacted_before_parse_and_the_value_is_unchanged(query):
    body = _vector([{"job": "x", "session": SECRET}])
    result = query(200, body)
    assert result.status == "ok" and result.value == 0.5
    assert SECRET.encode() not in result.body
    assert REDACTED_CREDENTIAL.encode() in result.body
    vec = query(200, body, vector=True)
    assert vec.status == "ok" and SECRET.encode() not in vec.body
    assert vec.series[0].labels["session"] == REDACTED_CREDENTIAL


def test_a_clean_body_is_kept_byte_for_byte(query):
    body = _vector([{"job": "checkout"}])
    assert query(200, body).body == body
    assert query(200, body, vector=True).body == body


def test_error_and_cut_bodies_are_redacted(query):
    error = query(401, f"Authorization: Bearer {SECRET} rejected".encode())
    assert (error.status, error.detail, error.http_status) == ("failed", "HTTP", 401)
    assert SECRET.encode() not in error.body and error.body
    cut = query(200, _vector([{"url": f"https://u:{SECRET}@h/x"}]), cut=True)
    assert cut.status == "failed" and cut.body_complete is False
    assert SECRET.encode() not in cut.body


def test_a_non_utf8_body_is_kept_as_no_bytes_and_fails(query):
    utf16 = _vector([{"job": "x"}]).decode().encode("utf-16")
    result = query(200, utf16)
    assert (result.status, result.detail, result.body) == ("failed", "NOT_JSON", b"")
    error = query(500, b"\xff\xfe" + SECRET.encode())
    assert error.body == b"" and error.http_status == 500


def test_two_series_that_differ_only_by_a_secret_fail_closed(query):
    a = json.loads(_vector([{"job": "x", "token": "a1"}], "1"))
    b = json.loads(_vector([{"job": "x", "token": "b2"}], "2"))
    a["data"]["result"] += b["data"]["result"]
    body = json.dumps(a).encode()
    vec = query(200, body, vector=True)
    assert (vec.status, vec.detail, vec.series) == ("failed", "CONFLICTING_SERIES", ())
    assert b"a1" not in vec.body and b"b2" not in vec.body


def test_redaction_that_outgrows_the_limit_is_too_large_not_an_error(query):
    # many tiny credential-named labels: under the limit as sent, over it
    # once each value becomes the marker
    body = _vector([{f"token{i}": "x" for i in range(1500)}])
    assert len(body) <= RESPONSE_LIMIT_BYTES
    for vector in (False, True):
        result = query(200, body, vector=vector)
        assert (result.status, result.detail) == ("failed", "TOO_LARGE")
        assert result.body_complete is False
        assert len(result.body) <= 2048
