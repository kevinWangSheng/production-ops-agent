"""Acceptance seams for #357 trace-export fallback redaction.

The tracing module's single ``set_attribute`` call site is the private
``_write`` helper.  The public model/tool attribute constructors only emit
allowlisted fields, so they cannot inject all of the synthetic ``inputs``,
``outputs`` and ``metadata`` string/list shapes required by this contract.
Using this module-level seam exercises the same fallback that every exported
string attribute takes before reaching an exporter.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from opspilot import redaction, tracing


class _CaptureSpan:
    """Minimal span surface needed by ``tracing._write``."""

    def __init__(self) -> None:
        self.attributes: dict[str, Any] = {}

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value


def _write_values(values: dict[str, object]) -> dict[str, Any]:
    span = _CaptureSpan()
    for key, value in values.items():
        tracing._write(span, key, value)  # type: ignore[arg-type]
    return span.attributes


def _all_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def test_trace_fallback_scrubs_shared_credential_shapes_in_strings_and_lists():
    literal = r"//user:secret@host"
    slash_escaped = r"\/\/user:secret@host"
    unicode_escaped = r"\u002f\u002fuser:secret@host"
    assignment = "password=aaaaaaaa357"
    bearer = "Bearer bbbbbbbb357"

    attributes = _write_values(
        {
            "inputs": f"request endpoint {literal}",
            "inputs.list": [slash_escaped, unicode_escaped],
            "outputs": f"response had {assignment}",
            "outputs.list": [bearer, f"retry {literal}"],
            "metadata": f"export metadata: {unicode_escaped}",
            "metadata.list": [assignment, slash_escaped],
        }
    )

    exported_text = "\n".join(
        item for value in attributes.values() for item in _all_strings(value)
    )
    for secret in (
        "user:secret@host",
        "aaaaaaaa357",
        "bbbbbbbb357",
    ):
        assert secret not in exported_text


def test_trace_fallback_delegates_new_credential_shapes_to_shared_redactor(
    monkeypatch: pytest.MonkeyPatch,
):
    original = redaction.redact_credentials
    calls: list[str] = []

    def shared_wrapper(text: str) -> str:
        calls.append(text)
        return original(text).replace("ZZ-SECRET-357", "[SHARED_357]")

    # Patch both possible import styles: the implementation may retain a
    # module import or bind the function directly in tracing.py.  Match any
    # direct alias as well, so the test does not depend on its private name.
    monkeypatch.setattr(redaction, "redact_credentials", shared_wrapper)
    monkeypatch.setattr(tracing, "redact_credentials", shared_wrapper, raising=False)
    for name, value in vars(tracing).items():
        if value is original:
            monkeypatch.setattr(tracing, name, shared_wrapper)

    attributes = _write_values(
        {
            "inputs": "new shared shape ZZ-SECRET-357",
            "outputs": ["list item ZZ-SECRET-357"],
            "metadata": "ordinary metadata",
        }
    )

    assert calls
    assert "[SHARED_357]" in attributes["inputs"]
    assert attributes["outputs"] == ["list item [SHARED_357]"]
    assert "ZZ-SECRET-357" not in json.dumps(attributes)


def test_trace_fallback_keeps_shared_policy_nonmatches_byte_identical():
    values = {
        "inputs": [
            'INFO checkout request completed: status=200 path="/api/orders"',
            'rate(http_requests_total{job="checkout",code="200"}[5m])',
        ],
        "outputs": '{"service":"checkout","status":200,"healthy":true}',
        "metadata": [
            "window completed after 300 seconds",
            '{"retry_count":0,"sample_count":12}',
        ],
    }

    assert _write_values(values) == values


def test_trace_fallback_is_idempotent():
    text = (
        r"password=aaaaaaaa357 //user:secret@host "
        r"Bearer bbbbbbbb357"
    )
    once = tracing._scrub(text)
    assert tracing._scrub(once) == once


def test_trace_fallback_preserves_keys_and_nonstring_values():
    attributes = _write_values(
        {
            "inputs": "ordinary input",
            "outputs": 17,
            "metadata": True,
        }
    )
    assert set(attributes) == {"inputs", "outputs", "metadata"}
    assert attributes["outputs"] == 17
    assert attributes["metadata"] is True

    # OpenTelemetry attributes do not accept None.  The serialized trace
    # payload still carries JSON null, so verify that its non-string values
    # and object keys survive the string scrub unchanged.
    payload = json.dumps(
        {"inputs": "ordinary input", "outputs": 17, "metadata": None},
        separators=(",", ":"),
    )
    assert json.loads(tracing._scrub(payload)) == json.loads(payload)


def test_trace_fallback_retains_environment_and_key_shape_scrubbing(monkeypatch):
    env_secret = "trace-env-secret-value-357"
    sk_key = "sk-357secretvalue123456"
    lsv2_key = "lsv2_357secretvalue123456"
    monkeypatch.setenv("TRACE_SCRUB_357_SECRET", env_secret)

    attributes = _write_values(
        {
            "outputs": f"env={env_secret} {sk_key} {lsv2_key}",
            "metadata": [f"{env_secret}", sk_key, lsv2_key],
        }
    )
    exported_text = json.dumps(attributes)
    for secret in (env_secret, sk_key, lsv2_key):
        assert secret not in exported_text
    assert "[REDACTED]" in exported_text
