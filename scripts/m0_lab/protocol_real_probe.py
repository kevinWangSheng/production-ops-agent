"""Bounded isolated DeepSeek protocol probes; never a product runtime path."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

import httpx2

from scripts.m0.config import load_config

ENDPOINT = "https://api.deepseek.com/v1/chat/completions"
MODEL = "deepseek-v4-flash"
ENV_FILE = Path("/Users/shenghuikevin/dev/AI/production-ops-agent/.env")
FIXTURE = Path("tests/fixtures/m0/protocol-v1.json")


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def summary(status: int, raw: bytes, elapsed_ms: int, *, interrupted=False):
    result = {
        "http_status": status,
        "response_sha256": sha256(raw),
        "response_bytes": len(raw),
        "elapsed_ms": elapsed_ms,
        "interrupted": interrupted,
    }
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return result
    if isinstance(payload, dict):
        result["model"] = payload.get("model")
        result["usage"] = payload.get("usage")
        choice = (payload.get("choices") or [{}])[0]
        if isinstance(choice, dict):
            result["finish_reason"] = choice.get("finish_reason")
            message = choice.get("message") or {}
            if isinstance(message, dict):
                result["role"] = message.get("role")
                result["content_sha256"] = (
                    sha256(message["content"].encode())
                    if isinstance(message.get("content"), str)
                    else None
                )
                calls = message.get("tool_calls") or []
                result["tool_call_ids"] = [
                    call.get("id")
                    for call in calls
                    if isinstance(call, dict) and isinstance(call.get("id"), str)
                ]
    return result


def call(client, key, body, *, stream=False, interrupt=False):
    started = time.monotonic()
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if not stream:
        response = client.post(ENDPOINT, headers=headers, json=body)
        return summary(
            response.status_code,
            response.content,
            int((time.monotonic() - started) * 1000),
        )
    with client.stream("POST", ENDPOINT, headers=headers, json=body) as response:
        first = b""
        for chunk in response.iter_bytes():
            if chunk:
                first = chunk
                break
        # Deliberately close immediately after the first non-empty SSE chunk.
        if interrupt:
            response.close()
        return summary(
            response.status_code,
            first,
            int((time.monotonic() - started) * 1000),
            interrupted=interrupt,
        )


def run(mode: str):
    key = load_config(ENV_FILE).values["DEEPSEEK_API_KEY"]
    fixture = json.loads(FIXTURE.read_text())
    base = {
        "model": MODEL,
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
        "max_tokens": 1024,
        "messages": fixture["messages"],
    }
    records = []
    with httpx2.Client(timeout=90, trust_env=False, follow_redirects=False) as client:
        if mode == "stream-interrupt":
            records.append(
                call(client, key, {**base, "stream": True}, stream=True, interrupt=True)
            )
            records.append(call(client, key, {**base, "stream": False}))
            verdict = "evidence_insufficient_product_stream_contract_fixed_false"
        elif mode == "tool-error":
            first = call(client, key, {**base, "tools": fixture["tools"]})
            records.append(first)
            verdict = "evidence_insufficient_no_tool_call"
            if first.get("tool_call_ids"):
                call_id = first["tool_call_ids"][0]
                follow = {
                    **base,
                    "messages": [
                        *fixture["messages"],
                        {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": call_id,
                                    "type": "function",
                                    "function": {
                                        "name": "otel_services",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        },
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": "HTTP 404: synthetic read-only tool unavailable",
                        },
                    ],
                    "response_format": {"type": "json_object"},
                }
                records.append(call(client, key, follow))
                verdict = "provider_tool_error_continuation_attempted"
        elif mode == "context-compression":
            call_id = "probe-tool-1"
            compressed = {
                "model": MODEL,
                "thinking": {"type": "enabled"},
                "reasoning_effort": "high",
                "max_tokens": 1024,
                "messages": [
                    {"role": "system", "content": "Use only the supplied evidence."},
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": "otel_services",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": '{"services":[]}',
                    },
                    {
                        "role": "user",
                        "content": "Return a JSON object with status=observed.",
                    },
                ],
                "response_format": {"type": "json_object"},
            }
            records.append(call(client, key, compressed))
            verdict = (
                "provider_accepts_hand_constructed_paired_view_not_product_compressor"
            )
        else:
            raise ValueError("unknown mode")
    return {
        "mode": mode,
        "model": MODEL,
        "http_attempts": len(records),
        "records": records,
        "verdict": verdict,
        "trace_uploads": 0,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode", choices=("stream-interrupt", "tool-error", "context-compression")
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.mode)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                k: result[k]
                for k in ("mode", "http_attempts", "verdict", "trace_uploads")
            }
        )
    )


if __name__ == "__main__":
    main()
