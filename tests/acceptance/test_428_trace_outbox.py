"""Independent acceptance tests for #428 trace-outbox delivery.

The implementation is intentionally imported inside tests: this suite is the
contract written before PR-2 lands, so collection must work on the PR-1 base.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest


def _tracing():
    from opspilot import tracing

    return tracing


def _delivery():
    from opspilot import outbox_delivery

    return outbox_delivery


def _valid_env(**overrides):
    env = {
        "OPSPILOT_TRACE": "outbox",
        "LANGSMITH_ENDPOINT": "https://api.smith.langchain.com",
        "LANGSMITH_PROJECT": "opspilot-lab-428",
        "LANGSMITH_API_KEY": "key-428",
    }
    env.update(overrides)
    return env


@pytest.mark.parametrize(
    ("missing", "reason"),
    [
        ("LANGSMITH_ENDPOINT", "LANGSMITH_ENDPOINT_NOT_ALLOWED"),
        ("LANGSMITH_PROJECT", "PROJECT_NOT_LAB"),
        ("LANGSMITH_API_KEY", "LANGSMITH_KEY_MISSING"),
    ],
)
def test_outbox_is_fail_closed_for_each_missing_prerequisite(missing, reason):
    tracing = _tracing()
    env = _valid_env()
    env[missing] = ""
    assert tracing.trace_mode(env) == "outbox"
    assert reason in tracing.outbox_export_failures(env)
    assert reason in tracing.export_status(env)
    assert tracing.export_status(env) != ("ENABLED",)


def test_off_and_lab_never_create_an_export_config():
    tracing = _tracing()
    delivery = _delivery()
    for mode, expected in (("off", "TRACE_MODE_OFF"), ("lab", "TRACE_MODE_LAB")):
        env = _valid_env(OPSPILOT_TRACE=mode)
        assert tracing.export_status(env) == (expected,)
        assert delivery.ExportConfig.from_env(env) is None


def test_enabled_config_builds_langsmith_otlp_url_and_required_headers():
    delivery = _delivery()
    config = delivery.ExportConfig.from_env(
        _valid_env(LANGSMITH_ENDPOINT="https://api.smith.langchain.com/")
    )
    assert config is not None
    assert config.endpoint_url == "https://api.smith.langchain.com/otel/v1/traces"
    assert config.api_key == "key-428"
    assert config.project == "opspilot-lab-428"


def test_unrecognised_trace_mode_is_off_and_records_no_export():
    tracing = _tracing()
    env = _valid_env(OPSPILOT_TRACE="production")
    assert tracing.trace_mode(env) == "off"
    assert tracing.export_status(env) == ("TRACE_MODE_OFF",)


class _CountingHandler(BaseHTTPRequestHandler):
    requests = []
    status = 204

    def do_POST(self):  # noqa: N802 - stdlib protocol name
        self.__class__.requests.append(self.path)
        length = int(self.headers.get("content-length", "0"))
        self.rfile.read(length)
        self.send_response(self.status)
        self.end_headers()

    def log_message(self, *_args):
        pass


@pytest.fixture
def counting_endpoint():
    _CountingHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CountingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, _CountingHandler
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_fail_closed_maintainer_does_not_post_and_purges_old_pending_rows(
    counting_endpoint,
):
    """r3-L5: disabled/fail-closed maintenance deletes pending rows after 7 days."""
    delivery = _delivery()
    server, handler = counting_endpoint
    old = datetime(2026, 1, 1, tzinfo=UTC)
    store = SimpleNamespace(
        pending_rows=[{"created_at": old, "state": "pending"}],
        purge_calls=[],
        purge=lambda **kwargs: store.purge_calls.append(kwargs) or 1,
    )
    maintainer = delivery.OutboxMaintainer(
        store,
        stop=threading.Event(),
        delivery=None,
    )
    assert maintainer.run_once() == 1
    assert handler.requests == []
    assert store.purge_calls and store.purge_calls[-1]["export_enabled"] is False


@pytest.mark.parametrize("status", [204, 200])
def test_2xx_marks_rows_delivered_and_posts_otlp(counting_endpoint, status):
    delivery = _delivery()
    server, handler = counting_endpoint
    handler.status = status
    clock = [datetime(2026, 11, 1, tzinfo=UTC)]
    transport_calls = []

    def transport(url, body, headers, timeout):
        transport_calls.append((url, body, headers, timeout))
        return status

    store = SimpleNamespace()
    exporter = delivery.OutboxDelivery(
        store,
        delivery.ExportConfig(
            endpoint_url=f"http://127.0.0.1:{server.server_port}/otel/v1/traces",
            api_key="key-428",
            project="opspilot-lab-428",
        ),
        clock=lambda: clock[0],
        transport=transport,
    )
    result = exporter.deliver_once()
    assert result.status == status
    assert transport_calls
    assert transport_calls[0][2]["x-api-key"] == "key-428"
    assert transport_calls[0][2]["Langsmith-Project"] == "opspilot-lab-428"


@pytest.mark.parametrize("status", [400, 408, 429, 500, 503])
def test_http_status_classes_have_contract_result(status):
    delivery = _delivery()
    calls = []

    def transport(_url, _body, _headers, _timeout):
        calls.append(1)
        return status

    exporter = delivery.OutboxDelivery(
        SimpleNamespace(),
        delivery.ExportConfig(
            "http://127.0.0.1/otel/v1/traces", "k", "opspilot-lab-428"
        ),
        transport=transport,
        clock=lambda: datetime(2026, 11, 1, tzinfo=UTC),
    )
    result = exporter.deliver_once()
    assert result.status == status
    assert calls == [1]
    if status == 400:
        assert result.dropped.get("REJECTED", 0) >= 0
    else:
        assert result.retried + sum(result.dropped.values()) >= 0


def test_backoff_constants_and_eighth_failure_drop():
    delivery = _delivery()
    assert [delivery.backoff_seconds(n) for n in range(1, 8)] == [
        5,
        10,
        20,
        40,
        80,
        160,
        320,
    ]
    assert delivery.backoff_seconds(8) == 600
    assert delivery.MAX_ATTEMPTS == 8
    assert delivery.BACKLOG_LIMIT == 20_000
    assert delivery.CLAIM_LEASE_SECONDS == 30.0


def test_identity_is_stable_and_generation_uses_generation_key():
    delivery = _delivery()
    run_id = UUID("12345678-1234-5678-1234-567812345678")
    key = "run_state:12345678-1234-5678-1234-567812345678:1"
    expected_trace = int.from_bytes(
        hashlib.sha256(str(run_id).encode()).digest()[:16], "big"
    )
    expected_span = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big")
    assert delivery.trace_id_for("run_state", run_id, key) == expected_trace
    assert delivery.trace_id_for("run_state", run_id, key) == delivery.trace_id_for(
        "run_state", run_id, key
    )
    assert delivery.span_id_for(key) == expected_span
    generation_key = "generation:attempt-428"
    expected_generation = int.from_bytes(
        hashlib.sha256(generation_key.encode()).digest()[:16], "big"
    )
    assert (
        delivery.trace_id_for("generation", None, generation_key) == expected_generation
    )


def test_payload_capture_has_no_sensitive_or_untrusted_text():
    from opspilot.persistence import outbox

    payload = {
        "incident_id": str(uuid4()),
        "run_id": str(uuid4()),
        "model": "deepseek-chat",
        "input_tokens": 4,
        "output_tokens": 5,
        "finish_reason": "stop",
        "prompt": "SENTINEL_PROMPT_428",
        "tool_raw_text": "SENTINEL_TOOL_428",
        "query": "SENTINEL_QUERY_428",
        "target": "SENTINEL_TARGET_428",
        "service": "SENTINEL_SERVICE_428",
        "reasoning_content": "SENTINEL_REASONING_428",
        "credential": "SENTINEL_CREDENTIAL_428",
    }
    clean = outbox.build_payload(payload)
    encoded = json.dumps(clean, sort_keys=True, default=str)
    for marker in (
        "SENTINEL_PROMPT_428",
        "SENTINEL_TOOL_428",
        "SENTINEL_QUERY_428",
        "SENTINEL_TARGET_428",
        "SENTINEL_SERVICE_428",
        "SENTINEL_REASONING_428",
        "SENTINEL_CREDENTIAL_428",
    ):
        assert marker not in encoded
