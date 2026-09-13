"""One real provider request using the isolated deterministic compressor."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx2

from scripts.m0.compressor import VERSION, compress, validate_pairing
from scripts.m0.config import load_config

ENDPOINT = "https://api.deepseek.com/v1/chat/completions"
OUT = Path("docs/evidence/m0-real-investigation/round-07-compressor-real-run.json")
ENV_FILE = Path("/Users/shenghuikevin/dev/AI/production-ops-agent/.env")


def group(i: int):
    call_id = f"compress-call-{i}"
    return [
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": f"PRIVATE-{i}",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "otel_metrics", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps(
                {"evidence_id": f"compress-e{i}", "rows": ["x" * 80]}
            ),
        },
    ]


def main():
    messages = [{"role": "system", "content": "Use only supplied evidence."}]
    messages.append(
        {"role": "user", "content": "Investigate the fixed read-only window."}
    )
    for i in range(6):
        messages.extend(group(i))
    before = hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest()
    estimated_before = sum(
        len(json.dumps(item, sort_keys=True)) // 4 + 4 for item in messages
    )
    compressed, report = compress(
        messages, threshold=estimated_before // 2, keep_groups=1
    )
    if validate_pairing(compressed) != 6 - report["folded_groups"]:
        raise SystemExit("compressor pairing check failed")
    body = {
        "model": "deepseek-v4-flash",
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
        "max_tokens": 1024,
        "messages": [
            *compressed,
            {"role": "user", "content": 'Return JSON {"status":"observed"}.'},
        ],
        "response_format": {"type": "json_object"},
    }
    key = load_config(ENV_FILE).values["DEEPSEEK_API_KEY"]
    with httpx2.Client(timeout=90, trust_env=False, follow_redirects=False) as client:
        response = client.post(
            ENDPOINT,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json=body,
        )
    raw = response.content
    payload = json.loads(raw)
    choice = (payload.get("choices") or [{}])[0]
    result = {
        "compressor_version": VERSION,
        "input_messages": len(messages),
        "output_messages": len(compressed),
        "before_estimated_tokens": estimated_before,
        "report": report,
        "pairing_count": validate_pairing(compressed),
        "original_messages_sha256": before,
        "compressed_messages_sha256": hashlib.sha256(
            json.dumps(compressed, sort_keys=True).encode()
        ).hexdigest(),
        "http_status": response.status_code,
        "response_sha256": hashlib.sha256(raw).hexdigest(),
        "response_bytes": len(raw),
        "response_model": payload.get("model"),
        "finish_reason": choice.get("finish_reason"),
        "usage": payload.get("usage"),
        "trace_uploads": 0,
        "product_compressor": False,
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "compressor_version": VERSION,
                "folded_groups": report["folded_groups"],
                "http_status": response.status_code,
                "finish_reason": choice.get("finish_reason"),
                "trace_uploads": 0,
            }
        )
    )


if __name__ == "__main__":
    main()
