"""Bounded latency measurement of large-context DeepSeek requests.

Question answered (docs/tasks/2026-09-29-m1-01-view-bytes-timeout.md, B): with
the M1 frozen ``MODEL_REQUEST_TIMEOUT_SECONDS`` (360 s), how long does one
non-streaming request take at ~100k / 300k / 600k / 900k prompt tokens, sent
through the product's own ``DeepSeekClient`` and ``serialized_request`` (same
body: thinking enabled, reasoning_effort high, tools attached, stream false)?

Model calls only; synthetic metric-shaped tool views, no business data, no
persistence beyond the ledger. Every request carries unique content (seeded
random numbers plus a nonce in the system message) so the provider's KV cache
cannot answer it. Ledger holds sizes, usage and timings, never prompt or
response text. Reads DEEPSEEK_API_KEY from the private env file and never
prints it. Development script under the standing authorization; not product
code.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from opspilot.investigation.client import DeepSeekClient
from opspilot.investigation.limits import (
    MAX_HTTP_REQUEST_BYTES,
    MODEL_REQUEST_TIMEOUT_SECONDS,
)
from opspilot.investigation.loop import ModelCall, ModelError, serialized_request
from opspilot.investigation.messages import assistant_message, pair_tool_results
from opspilot.tools.registry import canonical

ROOT = Path(__file__).resolve().parents[1]
MAIN_ENV = Path("/Users/shenghuikevin/dev/AI/production-ops-agent/.env")
OUT_DIR = ROOT / "docs/evidence/m1-01-view-bytes-timeout"

TOOL = {
    "type": "function",
    "function": {
        "name": "metrics_range_query",
        "description": "Return a projected Prometheus range query.",
        "parameters": {
            "type": "object",
            "properties": {"expr": {"type": "string"}},
            "required": ["expr"],
        },
    },
}
QUESTION = (
    "Synthetic latency probe. Earlier tool results are noise. Do not call any "
    "tool; answer with the single word OK."
)
DECODE_QUESTION = (
    "Synthetic decode-speed probe. Earlier tool results are noise. Do not call "
    "any tool. Write every integer from 1 to 30000 inclusive, one per line, "
    "without skipping, stopping early, or commenting."
)
VIEW_BYTES = 80_000  # under the 100 KiB per-view cap, as a real view would be


class Clock:
    def monotonic(self) -> float:
        return time.monotonic()


def read_key() -> str:
    explicit = os.environ.get("M0_ENV_FILE")
    for path in [Path(p).expanduser() for p in [explicit] if p] + [
        ROOT / ".env",
        MAIN_ENV,
    ]:
        if path.is_file():
            for line in path.read_text().splitlines():
                if line.strip().startswith("DEEPSEEK_API_KEY="):
                    value = line.split("=", 1)[1].strip().strip("'\"")
                    if value:
                        return value
    raise SystemExit("credential file unavailable")


def balance(key: str) -> dict:
    request = urllib.request.Request(
        "https://api.deepseek.com/user/balance",
        headers={"Authorization": "Bearer " + key, "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def view(rng: random.Random, index: int) -> str:
    """One metric-shaped tool view of about VIEW_BYTES canonical bytes."""
    series = []
    size = 0
    while size < VIEW_BYTES:
        base = rng.random() * 100
        row = {
            "metric": {"job": f"svc-{rng.randrange(10**6)}", "i": str(len(series))},
            "values": [
                [1_790_000_000 + 15 * k, f"{base + rng.random():.9f}"]
                for k in range(60)
            ],
        }
        size += len(canonical(row).encode("utf-8"))
        series.append(row)
    return canonical(
        {
            "evidence_id": f"lat-{index}:d",
            "status": "ok",
            "adopted": True,
            "tool": "metrics_range_query",
            "content": series,
        }
    )


def build(
    nonce: str, views: int, seed: int, question: str = QUESTION
) -> tuple[dict, ...]:
    rng = random.Random(seed)
    messages: list[dict] = [
        {
            "role": "system",
            "content": f"[latency probe {nonce}] You are a terse assistant.",
        },
        {"role": "user", "content": question},
    ]
    for index in range(views):
        call = {
            "id": f"call-{index}",
            "type": "function",
            "function": {"name": "metrics_range_query", "arguments": '{"expr":"up"}'},
        }
        messages.extend(
            pair_tool_results(
                assistant_message(
                    content=None, reasoning_content="checking", tool_calls=[call]
                ),
                [
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": view(rng, index),
                    }
                ],
                require_reasoning=True,
            )
        )
    if question is DECODE_QUESTION:
        # Long noisy histories make the model drop a leading instruction; the
        # decode probe repeats it last so the output actually runs long.
        messages.append({"role": "user", "content": question})
    return tuple(messages)


def probe(
    client: DeepSeekClient,
    label: str,
    views: int,
    seed: int,
    timeout: float,
    question: str = QUESTION,
    max_tokens: int = 512,
):
    call = ModelCall(
        messages=build(f"{label}-{seed}", views, seed, question),
        tools=(TOOL,),
        json_mode=False,
        max_tokens=max_tokens,
        timeout_seconds=timeout,
    )
    entry: dict = {
        "label": label,
        "views": views,
        "request_bytes": len(serialized_request(call)),
        "within_request_bytes_ceiling": len(serialized_request(call))
        <= MAX_HTTP_REQUEST_BYTES,
        "client_timeout_seconds": timeout,
        "started": datetime.now(timezone.utc).isoformat(),
    }
    started = time.monotonic()
    try:
        reply = client.complete(call)
        entry["seconds"] = round(time.monotonic() - started, 2)
        entry.update(
            http="200",
            finish_reason=reply.finish_reason,
            usage=dict(reply.usage),
            tool_call_count=len(reply.tool_calls),
            reasoning_chars=len(reply.reasoning_content or ""),
            content_chars=len(reply.content or ""),
        )
    except ModelError as exc:
        entry["seconds"] = round(time.monotonic() - started, 2)
        entry["error"] = exc.code
    return entry


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--targets", default="100000,300000,600000,900000")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=700.0)
    parser.add_argument("--out", default="ledger.json")
    parser.add_argument(
        "--decode-views",
        type=int,
        default=3,
        help="tool views in the decode probe's context (3 is about 100k tokens)",
    )
    parser.add_argument(
        "--decode-max-tokens",
        type=int,
        default=0,
        help="instead of the prefill ladder, one ~100k request that must emit "
        "up to this many tokens (decode-rate probe)",
    )
    args = parser.parse_args()
    key = read_key()
    client = DeepSeekClient(key, clock=Clock())
    ledger: dict = {
        "experiment": "m1-01-view-bytes-timeout-latency",
        "frozen_model_request_timeout_seconds": MODEL_REQUEST_TIMEOUT_SECONDS,
        "started": datetime.now(timezone.utc).isoformat(),
        "balance_before": balance(key),
        "requests": [],
    }
    out = OUT_DIR / args.out
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    def save() -> None:
        out.write_text(json.dumps(ledger, indent=2, ensure_ascii=False) + "\n")

    save()
    # Calibrate bytes-per-token of this generator on a small request first.
    cal = probe(client, "calibration", 3, 1, 120.0)
    ledger["requests"].append(cal)
    save()
    if "usage" not in cal:
        print(json.dumps({"status": "calibration_failed", "error": cal.get("error")}))
        return 1
    bytes_per_token = cal["request_bytes"] / cal["usage"]["prompt_tokens"]
    ledger["bytes_per_token"] = round(bytes_per_token, 3)
    if args.decode_max_tokens:
        entry = probe(
            client,
            "decode",
            args.decode_views,
            201,
            args.timeout,
            DECODE_QUESTION,
            args.decode_max_tokens,
        )
        ledger["requests"].append(entry)
        ledger["ended"] = datetime.now(timezone.utc).isoformat()
        ledger["balance_after"] = balance(key)
        save()
        print(json.dumps({k: entry.get(k) for k in ("seconds", "error", "usage")}))
        return 0
    seed = 100
    for target in [int(t) for t in args.targets.split(",")]:
        views = max(1, round(target * bytes_per_token / (VIEW_BYTES + 400)))
        for repeat in range(args.repeats):
            seed += 1
            entry = probe(client, f"target-{target}", views, seed, args.timeout)
            entry["target_prompt_tokens"] = target
            entry["repeat"] = repeat + 1
            ledger["requests"].append(entry)
            save()
            print(
                json.dumps(
                    {
                        k: entry.get(k)
                        for k in ("label", "seconds", "error", "finish_reason")
                    }
                    | {"prompt_tokens": entry.get("usage", {}).get("prompt_tokens")}
                ),
                flush=True,
            )
            if "error" in entry:
                # A failed large request says the ceiling is below this level;
                # larger levels would only spend more to say the same.
                ledger["stopped_after_error"] = entry["label"]
                break
        else:
            continue
        break
    ledger["ended"] = datetime.now(timezone.utc).isoformat()
    ledger["balance_after"] = balance(key)
    save()
    print(json.dumps({"status": "done", "out": str(out.relative_to(ROOT))}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
