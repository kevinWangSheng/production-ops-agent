#!/usr/bin/env python3
"""Reconcile recorded provider usage with the old and vendored estimators.

The script only accepts an evidence record when the same record contains the
complete message and tool arrays and an integer ``usage.prompt_tokens``.  A
request hash, byte count, role-only message summary, or a separately stored
usage row is deliberately insufficient.  This prevents reconstructed input
from being presented as a real provider request.

Run from the repository root with ``python3 .../reconcile.py``.  It writes a
machine-readable inventory beside this script; no network or model call is
made.
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[3]
EVIDENCE = ROOT / "docs" / "evidence"
OUT = Path(__file__).resolve().parent / "reconcile.json"
sys.path.insert(0, str(ROOT))


def walk(
    value: Any, source: Path, path: str = "$"
) -> Iterator[tuple[dict[str, Any], Path, str]]:
    if isinstance(value, dict):
        yield value, source, path
        for key, child in value.items():
            yield from walk(child, source, f"{path}.{key}")
    elif isinstance(value, list):
        for i, child in enumerate(value):
            yield from walk(child, source, f"{path}[{i}]")


def complete_messages(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(
            isinstance(item, dict)
            and isinstance(item.get("role"), str)
            and "content" in item
            for item in value
        )
    )


def complete_tools(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, dict) for item in value)


def find_records() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    eligible: list[dict[str, Any]] = []
    usage_only: list[dict[str, Any]] = []
    for source in sorted(EVIDENCE.rglob("*.json")) + sorted(EVIDENCE.rglob("*.jsonl")):
        if source == OUT:
            continue
        try:
            if source.suffix == ".jsonl":
                values = [
                    json.loads(line)
                    for line in source.read_text().splitlines()
                    if line.strip()
                ]
            else:
                values = [json.loads(source.read_text())]
        except (OSError, json.JSONDecodeError):
            continue
        for value in values:
            for obj, path, object_path in walk(value, source):
                usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else obj
                prompt = usage.get("prompt_tokens") if isinstance(usage, dict) else None
                if type(prompt) is not int:
                    continue
                messages = obj.get("messages")
                tools = obj.get("tools")
                if complete_messages(messages) and complete_tools(tools):
                    eligible.append(
                        {
                            "source": str(path.relative_to(ROOT)),
                            "object_path": object_path,
                            "prompt_tokens": prompt,
                            "messages": messages,
                            "tools": tools,
                        }
                    )
                elif any(
                    k in obj
                    for k in ("request_sha256", "request_bytes", "messages", "tools")
                ):
                    usage_only.append(
                        {
                            "source": str(path.relative_to(ROOT)),
                            "object_path": object_path,
                            "prompt_tokens": prompt,
                            "request_sha256": obj.get("request_sha256"),
                            "request_bytes": obj.get("request_bytes"),
                            "messages_summary": obj.get("messages"),
                            "tools_attached": obj.get("tools_attached"),
                        }
                    )
    return eligible, usage_only


def calculate(record: dict[str, Any]) -> dict[str, Any]:
    # Imported only when a complete body exists; unavailable environments do
    # not turn a hash-only record into a synthetic calculation.
    from opspilot.investigation.context import estimate_tokens
    from opspilot.tools.registry import canonical
    from opspilot.tools.tokens import count_tokens

    messages = record["messages"]
    tools = record["tools"]
    old = estimate_tokens(messages, tools)
    tokenized = sum(count_tokens(canonical(dict(message))) for message in messages)
    tokenized += count_tokens(canonical([dict(tool) for tool in tools])) if tools else 0
    return {
        "source": record["source"],
        "object_path": record["object_path"],
        "message_count": len(messages),
        "tool_count": len(tools),
        "old_estimate_tokens": old,
        "tokenizer_content_tokens": tokenized,
        "prompt_tokens": record["prompt_tokens"],
        "old_ratio": old / record["prompt_tokens"],
        "tokenizer_ratio": tokenized / record["prompt_tokens"],
    }


def main() -> None:
    eligible, usage_only = find_records()
    rows = [calculate(record) for record in eligible]
    overhead = None
    if rows:
        # Provider minus tokenizer content, divided by number of messages;
        # report median as the robust fixed per-message fit.
        overheads = [
            (row["prompt_tokens"] - row["tokenizer_content_tokens"])
            / row["message_count"]
            for row in rows
            if row["message_count"]
        ]
        overhead = {
            "method": "median(provider - content) / message_count",
            "value": statistics.median(overheads),
        }
    result = {
        "eligible_count": len(rows),
        "hash_or_summary_only_count": len(usage_only),
        "rows": rows,
        "hash_or_summary_only_sample": usage_only[:20],
        "hash_or_summary_only_prompt_tokens": {
            "min": min((r["prompt_tokens"] for r in usage_only), default=None),
            "max": max((r["prompt_tokens"] for r in usage_only), default=None),
            "over_500k_count": sum(r["prompt_tokens"] > 500_000 for r in usage_only),
        },
        "fixed_message_overhead_fit": overhead,
        "network_calls": 0,
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                k: result[k]
                for k in (
                    "eligible_count",
                    "hash_or_summary_only_count",
                    "network_calls",
                )
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
