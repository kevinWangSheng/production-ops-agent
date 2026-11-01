"""Independent pure acceptance contract for #427's trace outbox payload."""

from __future__ import annotations

import json

import pytest

EXPECTED_KEYS = {
    "incident_id",
    "run_id",
    "step_id",
    "attempt_id",
    "image_version",
    "image_revision",
    "prompt_revision",
    "tool_schema_revision",
    "context_policy_revision",
    "model",
    "span_kind",
    "started_at",
    "ended_at",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "reasoning_tokens",
    "finish_reason",
    "run_state",
    "handoff_reason",
    "tool_status_class",
    "tool_reason",
    "tool_name",
    "source_contact",
    "budget_seconds",
}


def _outbox_module():
    # Keep collection independent of the implementation branch, which is being
    # written in parallel.  These tests should fail at execution until it lands.
    from opspilot.persistence import outbox

    return outbox


def test_payload_is_a_strict_whitelist_and_does_not_copy_sensitive_content():
    outbox = _outbox_module()
    fields = {
        key: f"value-{key}" for key in EXPECTED_KEYS
    }
    fields.update(
        {
            "prompt": "SENTINEL_PROMPT_427",
            "reasoning_content": "SENTINEL_REASONING_427",
            "tool_raw_output": "SENTINEL_TOOL_OUTPUT_427",
            "credential": "SENTINEL_CREDENTIAL_427",
            "query": "SENTINEL_QUERY_427",
            "response": {"text": "SENTINEL_RESPONSE_427"},
        }
    )
    payload = outbox.build_payload(fields)
    encoded = json.dumps(payload, sort_keys=True, default=str)

    assert set(payload) <= outbox.PAYLOAD_KEYS
    assert set(payload) <= EXPECTED_KEYS
    for sentinel in (
        "SENTINEL_PROMPT_427",
        "SENTINEL_REASONING_427",
        "SENTINEL_TOOL_OUTPUT_427",
        "SENTINEL_CREDENTIAL_427",
        "SENTINEL_QUERY_427",
        "SENTINEL_RESPONSE_427",
    ):
        assert sentinel not in encoded


@pytest.mark.parametrize(
    "key",
    ["handoff_reason", "tool_status_class", "tool_reason"],
)
def test_non_code_reason_values_are_unclassified(key: str):
    outbox = _outbox_module()
    payload = outbox.build_payload({key: "not a fixed CODE; SENTINEL"})
    assert payload[key] == "UNCLASSIFIED"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("stop", "stop"),
        ("length", "length"),
        ("tool_calls", "tool_calls"),
        ("STOP", "UNCLASSIFIED"),
        ("tool calls", "UNCLASSIFIED"),
        ("", "UNCLASSIFIED"),
    ],
)
def test_finish_reason_uses_lowercase_vendor_code_rule(value: str, expected: str):
    outbox = _outbox_module()
    assert outbox.build_payload({"finish_reason": value})["finish_reason"] == expected


@pytest.mark.parametrize("value", ["none", "possible", "confirmed"])
def test_source_contact_accepts_the_three_contract_values(value: str):
    outbox = _outbox_module()
    assert outbox.build_payload({"source_contact": value})["source_contact"] == value


def test_source_contact_other_values_are_unclassified():
    outbox = _outbox_module()
    assert outbox.build_payload({"source_contact": "SENTINEL_CONTACT"})[
        "source_contact"
    ] == "UNCLASSIFIED"


def test_token_and_budget_numbers_are_preserved_exactly_including_suffix_tokens():
    outbox = _outbox_module()
    fields = {
        "input_tokens": 11,
        "output_tokens": 22,
        "total_tokens": 33,
        "reasoning_tokens": 44,
        "budget_seconds": 12.5,
        "sample_tokens": 987654321,
    }
    payload = outbox.build_payload(fields)
    assert payload["input_tokens"] == 11
    assert payload["output_tokens"] == 22
    assert payload["total_tokens"] == 33
    assert payload["reasoning_tokens"] == 44
    assert payload["budget_seconds"] == 12.5
    assert "sample_tokens" not in payload
