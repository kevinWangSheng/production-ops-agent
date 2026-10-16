"""Credential redaction at JSON, tool evidence and projection boundaries."""

import json
from hashlib import sha256

import pytest

from opspilot.redaction import (
    REDACTED_CREDENTIAL as MARKER,
)
from opspilot.redaction import redact_json_bytes, redact_payload
from opspilot.tools import ControlSnapshot, TransportResponse
from opspilot.tools.otel_demo import (
    MAX_DETAIL_BYTES_PER_FIELD,
    MAX_OPERATION_CHARS,
    _clipped,
    project_traces,
)
from opspilot.tools.registry import canonical
from opspilot.web.app import _raw_fields, _recovery_reading
from tests.m1_tool_support import (
    NOW,
    FixedControl,
    Window,
    body,
    build,
    registration,
    request,
)


def test_payload_walk_is_pure_and_preserves_keys_and_scalars():
    original = {
        "api_key": "synthetic-secret",
        "nested": [{"X-Api-Key": "another-secret"}, "Bearer abcdefgh12345"],
        "ordinary": [1, True, None, 2.5],
        "token": 17,
    }
    result = redact_payload(original)
    assert result == {
        "api_key": MARKER,
        "nested": [{"X-Api-Key": MARKER}, f"Bearer {MARKER}"],
        "ordinary": [1, True, None, 2.5],
        # A credential-named field is replaced whatever it holds.
        "token": MARKER,
    }
    assert original["api_key"] == "synthetic-secret"
    assert redact_payload(result) is result


@pytest.mark.parametrize("value", ["plain", 1, True, None, {"token": MARKER}])
def test_unchanged_payload_identity(value):
    assert redact_payload(value) is value


@pytest.mark.parametrize(
    "raw", [b' { "x": [1, true, null] }\n', b"plain text", b"\xffpassword=secret"]
)
def test_unchanged_bytes_identity(raw):
    assert redact_json_bytes(raw) is raw


def test_json_bytes_use_registry_canonical_serialization():
    raw = b'{ "z": "caf\\u00e9", "api_key": "synthetic-secret" }'
    assert (
        redact_json_bytes(raw) == canonical({"z": "café", "api_key": MARKER}).encode()
    )
    assert (
        redact_json_bytes(b"password=synthetic-secret") == f"password={MARKER}".encode()
    )


def test_nested_payload_does_not_depend_on_python_recursion():
    payload = {"password": "synthetic-secret"}
    for _ in range(2000):
        payload = {"child": payload}
    result = redact_payload(payload)
    for _ in range(2000):
        result = result["child"]
    assert result == {"password": MARKER}


@pytest.mark.parametrize("history", [False, True])
@pytest.mark.parametrize("late", [False, True])
def test_tool_evidence_is_redacted_before_recording(history, late):
    control = (
        FixedControl(later=ControlSnapshot(7, 0, 0, suspended=True), later_after=2)
        if history
        else FixedControl()
    )
    executor, transport, sink, clock = build(control=control)
    if late:
        # Failure path calls _history directly; adoption uses _inspect too.
        transport.clock, transport.duration = clock, 15.0
    raw = body([{"api_key": "synthetic-secret"}], extra={"password": "meta-secret"})
    transport.response = TransportResponse(
        body=raw, view_fields={"annotation": {"token": "view-secret"}}
    )
    outcome = executor.execute(request())
    if late and not history:
        assert not sink.records
        return
    record = sink.records[0]
    assert record.raw_sha256 == sha256(record.raw).hexdigest()
    assert json.loads(record.raw)["data"]["result"] == [{"api_key": MARKER}]
    assert "secret" not in record.raw.decode()
    assert "secret" not in canonical(record.view)
    assert "secret" not in canonical(outcome.model_view)
    if not history:
        assert record.view["annotation"] == {"token": MARKER}


def test_tool_preserves_unchanged_source_bytes():
    executor, transport, sink, _ = build()
    raw = b' {"data": {"result": [{"value": 1}]}}\n'
    transport.response = TransportResponse(body=raw)
    assert executor.execute(request()).status == "ok"
    assert sink.records[0].raw is raw


def test_redaction_expansion_is_subject_to_result_limit():
    raw = body([{"token": "x"}])
    executor, transport, sink, _ = build(
        registrations=[registration(max_result_bytes=len(raw))]
    )
    transport.response = TransportResponse(body=raw)
    outcome = executor.execute(request())
    assert outcome.reason == "RESULT_TOO_LARGE"
    assert not sink.records


def test_detail_redaction_precedes_byte_clipping():
    text = "x" * (MAX_DETAIL_BYTES_PER_FIELD - 10) + " Bearer abcdefgh123456789"
    result = _clipped({"key": "exception.message", "value": text})
    assert "abcdef" not in result["value"]
    expected = "x" * (MAX_DETAIL_BYTES_PER_FIELD - 10) + f" Bearer {MARKER}"
    assert result["original_utf8_bytes"] == len(expected.encode())
    assert result["value_truncated"] is True
    structured = _clipped({"key": "detail", "value": {"password": "synthetic-secret"}})
    assert "synthetic-secret" not in structured["value"]


def test_old_web_raw_is_redacted_without_mutating_storage_bytes():
    raw = b'{"token":"synthetic-secret"}'
    assert _raw_fields(raw) == ({"raw_utf8": canonical({"token": MARKER})}, True)
    assert raw == b'{"token":"synthetic-secret"}'
    assert _raw_fields(b"\xff") == ({"raw_base64": "/w=="}, False)
    assert _raw_fields(b'{"ok": 1}') == ({"raw_utf8": '{"ok": 1}'}, False)


def test_operation_redaction_precedes_character_clipping():
    operation = "x" * (MAX_OPERATION_CHARS - 10) + " Bearer abcdefgh123456789"
    record, _, _ = project_traces(
        {"data": [{"spans": [{"operationName": operation}]}]},
        service="checkout",
        limit=1,
        window=Window(NOW, NOW.replace(year=NOW.year + 1)),
        source_sha256="0" * 64,
        source_bytes=0,
    )
    span = record["data"]["sampled_spans"][0]
    assert "abcdef" not in span["operation"]
    assert span["operation_truncated"] is True


def test_recovery_display_redacts_values_and_raw_but_verifies_stored_hash():
    raw = b'{"password":"synthetic-secret"}'
    found = {
        "session_id": "session",
        "target": {},
        "sample": {
            "sample_id": "sample",
            "sequence": 1,
            "disposition": "healthy",
            "readings_consistent": True,
        },
        "reading": {
            "raw": raw,
            "raw_sha256": sha256(raw).hexdigest(),
            "signal_name": "latency",
            "status": "ok",
            "value": {"token": "value-secret"},
            "sample_count": 1,
            "query": "api_key=query-secret",
            "window_start": NOW,
            "window_end": NOW,
            "source": "fixture",
        },
    }
    displayed = _recovery_reading("evidence", found)
    assert displayed["hashes_verified"] is True
    # The hash verifies the stored bytes; the page says what it shows differs.
    assert displayed["display_redacted"] is True
    assert displayed["raw_sha256"] == sha256(raw).hexdigest()
    assert "secret" not in canonical(displayed)
    assert found["reading"]["raw"] is raw


def test_credentials_in_object_keys_are_redacted_and_collisions_kept_apart():
    payload = {
        "https://alice:synthetic-pass@example.com": "a",
        "https://bob:other-pass@example.com": "b",
        "plain": "c",
    }
    result = redact_payload(payload)
    assert "pass" not in canonical(result)
    host = f"https://{MARKER}@example.com"
    assert sorted(result) == sorted([host, f"{host}#2", "plain"])
    assert sorted(result[k] for k in result if k.startswith("https")) == ["a", "b"]


def test_executor_redacts_credentials_in_result_keys():
    executor, transport, sink, _ = build()
    transport.response = TransportResponse(
        body=body([{"https://alice:synthetic-pass@example.com": "ordinary"}])
    )
    outcome = executor.execute(request())
    assert outcome.status == "ok"
    assert "synthetic-pass" not in sink.records[0].raw.decode()
    assert "synthetic-pass" not in canonical(sink.records[0].view)
    assert "synthetic-pass" not in canonical(outcome.model_view)


def test_jaeger_service_name_key_is_redacted_in_projection():
    executor_payload = {
        "data": [
            {
                "traceID": "t",
                "processes": {"p": {"serviceName": "password=synthetic-pass"}},
                "spans": [{"spanID": "s", "processID": "p", "operationName": "op"}],
            }
        ]
    }
    record, _, _ = project_traces(
        executor_payload,
        service="checkout",
        limit=1,
        window=Window(NOW, NOW.replace(year=NOW.year + 1)),
        source_sha256="0" * 64,
        source_bytes=0,
    )
    # The adapter keeps the name; the executor's payload walk removes it
    # from keys and values alike before anything is recorded.
    assert "synthetic-pass" not in canonical(redact_payload(record))
