"""Minimal deterministic context compressor for M0 experiments only.

Folds the oldest complete assistant/tool groups into one summary message once
the token estimate exceeds a threshold. A group is never split, so every
``assistant.tool_calls`` id keeps its ``tool`` reply; evidence_id references
found in folded tool content are carried into the summary verbatim. Private
provider fields (reasoning_content) of folded groups are dropped, never
summarized. No model call, no persistence and no credentials are involved.
"""

import copy
import hashlib
import json

from .protocol import ProtocolError

VERSION = "m0-compressor-v1"
SUMMARY_PREFIX = "[compressed history "


def estimate_tokens(messages):
    """Conservative provider-agnostic estimate: about four bytes per token."""
    return sum(
        len(json.dumps(m, ensure_ascii=False, sort_keys=True)) // 4 + 4
        for m in messages
    )


def evidence_ids(value):
    """Collect evidence_id strings from nested JSON, in first-seen order."""
    found = []

    def walk(item):
        if isinstance(item, dict):
            if isinstance(item.get("evidence_id"), str):
                found.append(item["evidence_id"])
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    return list(dict.fromkeys(found))


def segments(messages):
    """Split into atomic segments: each tool group is one segment.

    Raises TOOL_PAIRING_INVALID for any assistant tool_calls without exactly
    its tool replies immediately following, or any orphan tool message.
    """
    if not isinstance(messages, list):
        raise ProtocolError("TOOL_PAIRING_INVALID")
    result = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if not isinstance(message, dict) or not isinstance(message.get("role"), str):
            raise ProtocolError("TOOL_PAIRING_INVALID")
        if message["role"] == "tool":
            raise ProtocolError("TOOL_PAIRING_INVALID")
        calls = message.get("tool_calls") or []
        if message["role"] == "assistant" and calls:
            ids = [c.get("id") if isinstance(c, dict) else None for c in calls]
            if any(not isinstance(i, str) or not i for i in ids) or len(
                set(ids)
            ) != len(ids):
                raise ProtocolError("TOOL_PAIRING_INVALID")
            group = [message]
            for call_id in ids:
                index += 1
                reply = messages[index] if index < len(messages) else None
                if (
                    not isinstance(reply, dict)
                    or reply.get("role") != "tool"
                    or reply.get("tool_call_id") != call_id
                    or not isinstance(reply.get("content"), str)
                ):
                    raise ProtocolError("TOOL_PAIRING_INVALID")
                group.append(reply)
            result.append({"kind": "group", "messages": group})
        else:
            result.append({"kind": "message", "messages": [message]})
        index += 1
    return result


def validate_pairing(messages):
    """Strict seam: every group paired, no orphans. Returns the group count."""
    return sum(1 for s in segments(messages) if s["kind"] == "group")


def compress(messages, *, threshold, keep_groups=1, estimate=estimate_tokens):
    """Return (messages, report). Deterministic for identical input.

    Folding order is oldest group first and stops as soon as the estimate is at
    or under the threshold or only ``keep_groups`` newest groups remain. The
    summary is a user message inserted where the first folded group stood.
    """
    if (
        type(threshold) is not int
        or threshold <= 0
        or type(keep_groups) is not int
        or keep_groups < 0
    ):
        raise ProtocolError("COMPRESSOR_INPUT_INVALID")
    parts = segments(messages)
    before = estimate(messages)
    groups = [i for i, s in enumerate(parts) if s["kind"] == "group"]
    folded, ids, refs, contents = [], [], [], []
    position = None
    current = before
    for index in groups:
        if current <= threshold or len(groups) - len(folded) <= keep_groups:
            break
        segment = parts[index]
        if position is None:
            position = index
        folded.append(index)
        for message in segment["messages"]:
            if message["role"] == "assistant":
                ids.extend(c["id"] for c in message["tool_calls"])
            else:
                contents.append(message["content"])
                try:
                    refs.extend(evidence_ids(json.loads(message["content"])))
                except ValueError:
                    pass
        remaining = [
            m for i, s in enumerate(parts) if i not in folded for m in s["messages"]
        ]
        current = estimate(remaining)
    if not folded:
        return copy.deepcopy(messages), {
            "version": VERSION,
            "before_tokens": before,
            "after_tokens": before,
            "folded_groups": 0,
            "tool_call_ids": [],
            "evidence_ids": [],
            "over_threshold": before > threshold,
        }
    refs = list(dict.fromkeys(refs))
    content_sha256 = hashlib.sha256("\n".join(contents).encode("utf-8")).hexdigest()
    summary = {
        "role": "user",
        "content": SUMMARY_PREFIX
        + json.dumps(
            {
                "compressor": VERSION,
                "folded_groups": len(folded),
                "tool_call_ids": ids,
                "evidence_ids": refs,
                "tool_content_sha256": content_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "] Earlier tool results were folded; cite only the listed evidence_ids.",
    }
    output = []
    for index, segment in enumerate(parts):
        if index == position:
            output.append(summary)
        if index in folded:
            continue
        output.extend(copy.deepcopy(segment["messages"]))
    validate_pairing(output)
    return output, {
        "version": VERSION,
        "before_tokens": before,
        "after_tokens": estimate(output),
        "folded_groups": len(folded),
        "tool_call_ids": ids,
        "evidence_ids": refs,
        "tool_content_sha256": content_sha256,
        "over_threshold": estimate(output) > threshold,
    }
