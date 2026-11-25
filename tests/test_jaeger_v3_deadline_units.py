"""#859 review F1: the version probe and the read of one trace tool call share
one deadline (PRODUCT-CONSTRAINTS: bounded tool/query timeouts). A slow
v3-only Jaeger must not stretch a call to several full timeouts, and no GET
may leave after the call's deadline."""

from __future__ import annotations

import io
import time
import urllib.error
from datetime import UTC, datetime, timedelta

import pytest

from opspilot.tools import jaeger_query, jaeger_v3, otel_demo
from opspilot.tools.executor import TransportRequest, TransportTimeout
from opspilot.tools.outcomes import Window

TIMEOUT = 1.0
SLACK = 0.3
NOW = datetime(2026, 11, 25, 12, tzinfo=UTC)


class _Reply(io.BytesIO):
    def __init__(self, body: bytes):
        super().__init__(body)
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class SlowV3Jaeger:
    """Answers like a v3-only Jaeger, each request after ``delay`` seconds;
    records when each request was sent."""

    def __init__(self, delay: float):
        self.delay = delay
        self.sent: list[tuple[float, str]] = []

    def open(self, request, timeout=None):
        url = request.full_url
        self.sent.append((time.monotonic(), url))
        time.sleep(self.delay)
        path = url.split("?", 1)[0]
        if path.endswith("/api/services"):
            raise urllib.error.HTTPError(
                url, 404, "nf", {}, io.BytesIO(b"404 page not found\n")
            )
        if path.endswith("/api/v3/services"):
            return _Reply(b'{"services":["checkout"]}')
        if path.endswith("/api/v3/operations"):
            return _Reply(b'{"operations":[{"name":"x","spanKind":"server"}]}')
        return _Reply(b'{"result":{"resourceSpans":[]}}')


def _request(tool: str, params: dict[str, object]) -> TransportRequest:
    return TransportRequest(
        operation_id="deadline-op",
        source="jaeger",
        verb="query",
        endpoint="e",
        selector={"traces_endpoint": "http://jaeger.invalid:16686"},
        params=params,
        window=Window(NOW - timedelta(hours=1), NOW),
        timeout_seconds=TIMEOUT,
        max_result_bytes=1_000_000,
        credential_ref=otel_demo.CREDENTIAL_REF,
        tool=tool,
    )


def _a4(opener):
    return jaeger_query.JaegerQueryTransport(
        credentials={otel_demo.CREDENTIAL_REF: None}, opener=opener
    )


def _traces_search(opener):
    sources = otel_demo.OtelDemoTransport(
        credentials={otel_demo.CREDENTIAL_REF: None}, opener=opener
    )
    return jaeger_v3.ExtTracesTransport(sources, jaeger_v3.JaegerApiProbe())


CASES = [
    (_a4, jaeger_query.LIST_OPERATIONS_TOOL, {"service": "checkout"}),
    (_traces_search, otel_demo.TRACES_TOOL, {"service": "checkout"}),
]


@pytest.mark.parametrize(("build", "tool", "params"), CASES)
def test_probe_and_read_share_one_deadline(build, tool, params):
    opener = SlowV3Jaeger(delay=0.8)  # probe 404, probe v3, read: 2.4 s serial
    transport = build(opener)
    start = time.monotonic()
    with pytest.raises(TransportTimeout):
        transport.fetch(_request(tool, params))
    elapsed = time.monotonic() - start
    assert elapsed <= TIMEOUT + SLACK, elapsed
    assert opener.sent, "the call never reached the source"
    assert all(at <= start + TIMEOUT for at, _ in opener.sent), opener.sent


@pytest.mark.parametrize(("build", "tool", "params"), CASES)
def test_a_fast_source_still_completes_within_the_deadline(build, tool, params):
    opener = SlowV3Jaeger(delay=0.05)
    transport = build(opener)
    start = time.monotonic()
    response = transport.fetch(_request(tool, params))
    assert time.monotonic() - start <= TIMEOUT
    assert response.body
    assert (
        [url for _, url in opener.sent][-1]
        .split("?")[0]
        .endswith(("/api/v3/operations", "/api/v3/traces"))
    )
