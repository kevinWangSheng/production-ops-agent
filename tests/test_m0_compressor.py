"""Deterministic compressor contracts: pairing survives, evidence ids preserved."""

import json

import pytest

from scripts.m0.compressor import (
    SUMMARY_PREFIX,
    compress,
    estimate_tokens,
    validate_pairing,
)
from scripts.m0.protocol import ProtocolError


def group(i, evidence):
    return [
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": f"PRIVATE_{i}",
            "tool_calls": [
                {
                    "id": f"call-{i}",
                    "type": "function",
                    "function": {"name": "read_fixture", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": f"call-{i}",
            "content": json.dumps(
                {"evidence_id": evidence, "tool": "read_fixture", "rows": ["x" * 40]}
            ),
        },
    ]


def transcript(n):
    messages = [{"role": "system", "content": "Use only supplied evidence."}]
    messages.append({"role": "user", "content": "Investigate m0-target-a."})
    for i in range(n):
        messages += group(i, f"ev-{i}")
    return messages


def test_no_fold_under_threshold_returns_identical_copy():
    messages = transcript(2)
    out, report = compress(messages, threshold=10_000)
    assert out == messages and out is not messages
    assert report["folded_groups"] == 0 and report["over_threshold"] is False


def test_fold_keeps_pairing_and_evidence_ids_and_drops_private_reasoning():
    messages = transcript(6)
    before = estimate_tokens(messages)
    out, report = compress(messages, threshold=before // 2, keep_groups=1)
    assert report["folded_groups"] >= 1
    assert validate_pairing(out) == 6 - report["folded_groups"]
    summary = out[2]
    assert summary["role"] == "user" and summary["content"].startswith(SUMMARY_PREFIX)
    folded = report["folded_groups"]
    assert report["evidence_ids"] == [f"ev-{i}" for i in range(folded)]
    assert report["tool_call_ids"] == [f"call-{i}" for i in range(folded)]
    for i in range(folded):
        assert f"ev-{i}" in summary["content"]
        assert f"PRIVATE_{i}" not in json.dumps(out)
    # Newest group and leading messages survive verbatim.
    assert out[:2] == messages[:2]
    assert out[-2:] == messages[-2:]
    assert report["after_tokens"] < report["before_tokens"]


def test_compression_is_deterministic():
    a = compress(transcript(5), threshold=50)
    b = compress(transcript(5), threshold=50)
    assert a == b


def test_keep_groups_floor_never_splits_a_group():
    out, report = compress(transcript(3), threshold=1, keep_groups=2)
    assert report["folded_groups"] == 1
    assert validate_pairing(out) == 2
    assert report["over_threshold"] is True


@pytest.mark.parametrize(
    "messages",
    [
        [{"role": "tool", "tool_call_id": "x", "content": ""}],
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "a", "type": "function", "function": {}}],
            }
        ],
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "a", "type": "function", "function": {}}],
            },
            {"role": "tool", "tool_call_id": "b", "content": ""},
        ],
        [{"role": "assistant", "content": "", "tool_calls": {}}],
        [{"role": "assistant", "content": "", "tool_calls": ""}],
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "", "type": "function", "function": {}}],
            },
            {"role": "tool", "tool_call_id": "", "content": ""},
        ],
    ],
)
def test_orphans_and_bad_ids_fail_closed(messages):
    with pytest.raises(ProtocolError, match="TOOL_PAIRING_INVALID"):
        compress(messages, threshold=1)


@pytest.mark.parametrize("threshold,keep", [(0, 1), ("5", 1), (5, -1), (5, 1.0)])
def test_invalid_compressor_input(threshold, keep):
    with pytest.raises(ProtocolError, match="COMPRESSOR_INPUT_INVALID"):
        compress(transcript(1), threshold=threshold, keep_groups=keep)
