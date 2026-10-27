"""Independent acceptance tests for #319 escaped URL-userinfo redaction.

These tests use only the public redaction, tool, Observer and Alertmanager
entry points.  The values are deliberately synthetic and must never occur in
any returned view, stored body or model-facing projection.
"""

from __future__ import annotations

import io
import json
import time
import urllib.error
from datetime import datetime, timezone

import pytest

from opspilot.alertmanager import (
    Resolution,
    alert_context,
    parse_alert,
    question_for,
    record_of,
)
from opspilot.observer.prometheus import PrometheusReadOnlySource
from opspilot.redaction import (
    REDACTED_CREDENTIAL,
    redact_credentials,
    redact_json_bytes,
)
from opspilot.tools.otel_demo import TRACES_TOOL
from tests.acceptance.f7_hostile_support import (
    jaeger_payload,
    prometheus_payload,
    text_of,
)
from tests.acceptance.test_311_source_errors import _envelope, _SourceStub
from tests.acceptance.test_f7_step3_source_secrets import _call as tool_call
from tests.acceptance.test_f7_step3_source_secrets import _executor as tool_executor
from tests.test_m1_otel_demo_contract import GOOD_EXPR, FakeOpener

SECRET = "alice:s3cr3t-319"
HOST = "db.example.test"
ESCAPED_PREFIX = r"\/\/"
UNICODE_PREFIX = r"\u002f\u002f"


@pytest.fixture
def stub():
    server = _SourceStub()
    try:
        yield server
    finally:
        server.close()


@pytest.mark.parametrize(
    "prefix",
    [
        r"//",
        r"\/\/",
        UNICODE_PREFIX,
        r"\u002F\u002F",
        r"\/\u002f",
        r"\u002f\/",
        r"\u002F\/",
    ],
)
@pytest.mark.parametrize("scheme", ["", "https:"])
def test_each_authority_prefix_shape_is_redacted_exactly(
    prefix: str, scheme: str
) -> None:
    source = f"{scheme}{prefix}{SECRET}@{HOST}/orders"
    expected = f"{scheme}{prefix}{REDACTED_CREDENTIAL}@{HOST}/orders"
    assert redact_credentials(source) == expected


def test_html_pre_and_truncated_json_text_are_redacted() -> None:
    html = f'<html><pre>{{"dsn":"https:{ESCAPED_PREFIX}{SECRET}@{HOST}/orders"}}</pre></html>'
    truncated = f'{{"dsn":"https:{UNICODE_PREFIX}{SECRET}@{HOST}/orders"'
    for source in (html, truncated):
        output = redact_credentials(source)
        assert SECRET not in output
        assert REDACTED_CREDENTIAL in output
        output_bytes = redact_json_bytes(source.encode())
        assert SECRET.encode() not in output_bytes
        assert REDACTED_CREDENTIAL.encode() in output_bytes


def test_redact_json_bytes_redacts_nested_json_and_html_body() -> None:
    # In scope (pre-review §0): one JSON decode leaves an escaped authority.
    # ``json.dumps`` of a string that already holds the escape would escape
    # its backslash again (double escape, M3 / #359), so write the inner
    # JSON text literally.
    inner = f'{{"dsn":"https:{UNICODE_PREFIX}{SECRET}@{HOST}/orders"}}'
    body = json.dumps({"message": inner}).encode()
    redacted = redact_json_bytes(body)
    assert SECRET.encode() not in redacted
    assert REDACTED_CREDENTIAL.encode() in redacted

    html = (
        f'<pre>{{"dsn":"https:{ESCAPED_PREFIX}{SECRET}@{HOST}/orders"}}</pre>'.encode()
    )
    redacted_html = redact_json_bytes(html)
    assert SECRET.encode() not in redacted_html
    assert REDACTED_CREDENTIAL.encode() in redacted_html


def test_existing_literal_authority_and_three_no_false_positive_examples_are_byte_stable() -> (
    None
):
    unchanged = {
        '{"a":"\\/\\/x","b":"me@corp.com"}',
        r"\/\/registry.npmjs.org\/@scope\/pkg",
        r"see \/\/wiki and mail ops@corp.com",
    }
    for source in unchanged:
        assert redact_credentials(source) == source
        assert redact_json_bytes(source.encode()) == source.encode()

    literal = f"https://{SECRET}@{HOST}/orders"
    assert redact_credentials(literal) == (
        f"https://{REDACTED_CREDENTIAL}@{HOST}/orders"
    )


def test_redaction_is_idempotent() -> None:
    source = (
        f"https:{UNICODE_PREFIX}{SECRET}@{HOST}/orders "
        f"password={SECRET} "
        f'{{"nested":"https:{UNICODE_PREFIX}{SECRET}@{HOST}"}}'
    )
    once = redact_credentials(source)
    assert redact_credentials(once) == once
    json_once = redact_json_bytes(source.encode())
    assert redact_json_bytes(json_once) == json_once


def test_512_kib_pathological_input_is_linear_enough_for_ci() -> None:
    # Many almost-authorities exercise the scanner without relying on a
    # provider, socket or database.  Compare two sizes to avoid a fragile
    # absolute benchmark while still detecting quadratic rescans.
    for prefix in (ESCAPED_PREFIX, UNICODE_PREFIX):
        half = (prefix + ("a" * 4093)) * 64
        full = half + half
        start = time.perf_counter()
        redact_credentials(full)
        elapsed = time.perf_counter() - start
        assert len(full) >= 512 * 1024
        assert elapsed < 3.0


def test_tool_span_log_value_has_no_escaped_userinfo_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opener = FakeOpener(
        by_query={GOOD_EXPR: prometheus_payload()},
        routes={
            "/api/traces": jaeger_payload(
                {
                    "key": "exception.message",
                    "value": f"https:{UNICODE_PREFIX}{SECRET}@{HOST}/orders",
                }
            )
        },
    )
    executor, sink = tool_executor(monkeypatch, opener)
    outcome = executor.execute(tool_call(TRACES_TOOL, {"service": "checkout"}))
    visible = text_of(
        {
            "model_view": outcome.model_view,
            "evidence": None if outcome.evidence is None else outcome.evidence.raw,
            "sink": [record.raw for record in sink.records],
        }
    )
    assert outcome.status == "ok", outcome.model_view
    assert SECRET not in visible
    assert REDACTED_CREDENTIAL in visible


def test_prometheus_400_error_envelope_with_nested_escaped_json_is_redacted(
    stub,
) -> None:
    # The outer envelope JSON escapes the inner JSON string a second time.
    from tests.acceptance.test_311_source_errors import _call as source_call
    from tests.acceptance.test_311_source_errors import _executor as source_executor

    # Inner JSON text written literally so one envelope decode leaves
    # ``\/\/`` (pre-review §0); json.dumps would double-escape it (M3).
    inner = f'{{"url":"https:{ESCAPED_PREFIX}{SECRET}@{HOST}"}}'
    stub.always(400, _envelope(inner))
    executor, sink, _store = source_executor(stub)
    outcome = executor.execute(source_call())
    visible = text_of(outcome.model_view)
    assert outcome.status == "error"
    assert SECRET not in visible
    assert REDACTED_CREDENTIAL in visible
    assert sink.records == []


class _Body:
    def __init__(self, body: bytes, status: int = 200):
        self._stream = io.BytesIO(body)
        self.status = status
        self.headers = {}
        self.fp = self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, amount: int = -1) -> bytes:
        return self._stream.read(amount)

    def read1(self, amount: int = -1) -> bytes:
        return self._stream.read(amount)


class _ObserverOpener:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self.body = body

    def open(self, request, timeout):
        if self.status >= 400:
            raise urllib.error.HTTPError(
                request.full_url,
                self.status,
                "synthetic",
                {},
                io.BytesIO(self.body),
            )
        return _Body(self.body, self.status)


def _observer_source(status: int, body: bytes) -> PrometheusReadOnlySource:
    return PrometheusReadOnlySource(
        "http://observer.example.test",
        opener=_ObserverOpener(status, body),
    )


def test_observer_stores_complete_non_json_error_body_redacted() -> None:
    body = f"upstream error at https:{ESCAPED_PREFIX}{SECRET}@{HOST}/orders".encode()
    result = _observer_source(400, body).instant(
        "up", at=datetime(2026, 10, 26, tzinfo=timezone.utc), timeout_seconds=2
    )
    assert result.status == "failed"
    assert result.body_complete is True
    assert SECRET.encode() not in result.body
    assert REDACTED_CREDENTIAL.encode() in result.body


@pytest.mark.parametrize(
    "prefix", ["//", r"\u002f\u002f", r"\u002F\u002F", r"\/\u002F"]
)
def test_observer_truncation_path_redacts_open_literal_authority(prefix: str) -> None:
    # The read stops inside an open authority (literal or JSON-escaped
    # slashes followed by ``alice:...``) with no ``@`` or host yet, so the
    # whole fragment must be dropped.
    body = b"x" * 2040 + f"{prefix}{SECRET}".encode() + b"x" * 4096
    result = _observer_source(400, body).instant(
        "up", at=datetime(2026, 10, 26, tzinfo=timezone.utc), timeout_seconds=2
    )
    assert result.status == "failed"
    assert result.body_complete is False
    assert SECRET.encode() not in result.body


def test_alert_annotations_and_model_question_projection_have_no_secret() -> None:
    item = {
        "status": "firing",
        "fingerprint": "synthetic-319",
        "startsAt": "2026-10-26T00:00:00Z",
        "labels": {"alertname": "checkout", "service": "checkout"},
        "annotations": {
            "summary": f"backend https:\\/\\/{SECRET}@{HOST}/orders",
        },
        "generatorURL": "https://prometheus.example.test/graph?g0.expr=up",
    }
    alert = parse_alert(item)
    resolution = Resolution(
        target_id="target-319",
        namespace="default",
        workload="checkout",
        handoff_reason=None,
    )
    projected = text_of(alert_context(alert.labels, alert.annotations))
    question = question_for(alert, resolution)
    audit = record_of(alert).alert_json
    assert SECRET not in projected + question + audit
    assert REDACTED_CREDENTIAL in projected + audit
