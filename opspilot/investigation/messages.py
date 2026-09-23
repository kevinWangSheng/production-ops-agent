"""Transcript pairing for model tool calls and ToolOperation results.

Technical plan section 5: every tool call must have a complete result or
failure record; compression and continuation keep complete message groups.
Section 7: reconstruction must not leave a dangling tool pairing. DeepSeek
Flash further requires that ``reasoning_content`` from earlier tool-using
turns is sent back on any later request that includes ``tools``, otherwise
the provider returns 400.

This module does not execute tools. It only accepts or rejects a pairing.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any


class PairingError(Exception):
    """Fixed-code pairing failure. The loop must not execute tools after this."""

    def __init__(self, code: str) -> None:
        if code not in {"TOOL_PAIRING_INVALID", "PRIVATE_PROTOCOL_MISSING"}:
            raise ValueError(f"unknown pairing code {code!r}")
        super().__init__(code)
        self.code = code


def _as_mapping(value: object) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, Mapping) else None


def validate_tool_calls(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the tool_calls list, or raise if the container is unusable.

    A missing or empty list is valid and means the model produced no calls.
    A non-list container, including ``{}`` and ``""``, is fail-closed: the
    compressor regression for this exact mistake is in the M0 record.
    """
    if "tool_calls" not in message or message["tool_calls"] in (None, []):
        return []
    calls = message["tool_calls"]
    if not isinstance(calls, list) or any(not isinstance(call, dict) for call in calls):
        raise PairingError("TOOL_PAIRING_INVALID")
    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for call in calls:
        function = call.get("function")
        if not isinstance(function, Mapping):
            raise PairingError("TOOL_PAIRING_INVALID")
        call_id = call.get("id")
        name = function.get("name")
        arguments = function.get("arguments")
        if (
            not isinstance(call_id, str)
            or not call_id
            or call.get("type") != "function"
            or not isinstance(name, str)
            or not name
            or not isinstance(arguments, str)
        ):
            raise PairingError("TOOL_PAIRING_INVALID")
        if call_id in seen:
            raise PairingError("TOOL_PAIRING_INVALID")
        seen.add(call_id)
        cleaned.append(
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
        )
    return cleaned


def pair_tool_results(
    assistant: Mapping[str, Any],
    results: Sequence[Mapping[str, Any]],
    *,
    require_reasoning: bool,
) -> list[dict[str, Any]]:
    """Build one complete ``[assistant, *tool]`` group.

    ``results`` must be 1:1 with ``assistant.tool_calls`` by ``tool_call_id``.
    Unknown provider fields are not forwarded. When ``require_reasoning`` is
    true, ``reasoning_content`` must be a string (possibly empty) so a later
    tools request can echo it.
    """
    message = _as_mapping(assistant)
    if message is None or message.get("role") != "assistant":
        raise PairingError("TOOL_PAIRING_INVALID")
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise PairingError("TOOL_PAIRING_INVALID")
    calls = validate_tool_calls(message)
    if not isinstance(results, Sequence) or isinstance(results, (str, bytes)):
        raise PairingError("TOOL_PAIRING_INVALID")
    cleaned_results: list[dict[str, Any]] = []
    for result in results:
        item = _as_mapping(result)
        if (
            item is None
            or set(item) != {"role", "tool_call_id", "content"}
            or item["role"] != "tool"
            or not isinstance(item["tool_call_id"], str)
            or not item["tool_call_id"]
            or not isinstance(item["content"], str)
        ):
            raise PairingError("TOOL_PAIRING_INVALID")
        cleaned_results.append(
            {
                "role": "tool",
                "tool_call_id": item["tool_call_id"],
                "content": item["content"],
            }
        )
    call_ids = [call["id"] for call in calls]
    result_ids = [item["tool_call_id"] for item in cleaned_results]
    if (
        not call_ids
        or len(set(call_ids)) != len(call_ids)
        or sorted(call_ids) != sorted(result_ids)
    ):
        raise PairingError("TOOL_PAIRING_INVALID")
    reasoning = message.get("reasoning_content")
    if require_reasoning and not isinstance(reasoning, str):
        raise PairingError("PRIVATE_PROTOCOL_MISSING")
    kept: dict[str, Any] = {
        "role": "assistant",
        "content": content,
        "tool_calls": copy.deepcopy(calls),
    }
    if isinstance(reasoning, str):
        kept["reasoning_content"] = reasoning
    # Rebuild results in tool_calls order so the transcript is stable.
    by_id = {item["tool_call_id"]: item for item in cleaned_results}
    ordered = [by_id[call_id] for call_id in call_ids]
    return [kept, *ordered]


def assistant_message(
    *,
    content: str | None,
    reasoning_content: str | None,
    tool_calls: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """The subset of an assistant turn that may re-enter a later request."""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if reasoning_content is not None:
        message["reasoning_content"] = reasoning_content
    if tool_calls:
        message["tool_calls"] = [copy.deepcopy(dict(call)) for call in tool_calls]
    return message
