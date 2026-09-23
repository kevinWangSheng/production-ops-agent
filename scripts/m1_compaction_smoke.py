"""One bounded real-provider check of the compacted transcript shape.

Question answered (design draft §9.3 / P2 precondition): does DeepSeek accept,
with ``tools`` attached and thinking enabled, a transcript whose earlier tool
turns were replaced by the compaction user message, while a *later* assistant
turn keeps its real ``reasoning_content``? Two HTTP requests, synthetic
question and synthetic tool view, no business data, no persistence.

Reads DEEPSEEK_API_KEY from the private env file (never printed) and writes a
ledger without prompt/response text to docs/evidence/m1-01-loop-long-horizon/.
Development script under the standing authorization; not product code.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from opspilot.instructions.discipline import render
from opspilot.investigation.client import DeepSeekClient
from opspilot.investigation.context import (
    COMPACTION_INSTRUCTION,
    compaction_message,
    fold_digest,
)
from opspilot.investigation.loop import (
    DISCIPLINE_VARIANT,
    ModelCall,
    ModelError,
    serialized_request,
)
from opspilot.investigation.messages import assistant_message, pair_tool_results
from opspilot.investigation.reports import REPORT_CONTRACT
from opspilot.tools.registry import canonical

ROOT = Path(__file__).resolve().parents[1]
MAIN_ENV = Path("/Users/shenghuikevin/dev/AI/production-ops-agent/.env")
OUT = ROOT / "docs/evidence/m1-01-loop-long-horizon/compaction-smoke.json"

TOOL = {
    "type": "function",
    "function": {
        "name": "metrics_range_query",
        "description": (
            "Return a projected Prometheus range query for one registered target "
            "inside the authorized window. Available expressions: "
            '["rate(http_errors[5m])"]. Missing series are unknown, not zero.'
        ),
        "parameters": {
            "type": "object",
            "properties": {"expr": {"type": "string"}},
            "required": ["expr"],
        },
    },
}
SYNTHETIC_VIEW = {
    "evidence_id": "smoke-op-1:dispatch-1",
    "operation_id": "smoke-op-1",
    "status": "ok",
    "adopted": True,
    "tool": "metrics_range_query",
    "source": "prometheus",
    "target_id": "checkout-prod",
    "window": {
        "start": "2026-09-14T00:00:00+00:00",
        "end": "2026-09-14T01:00:00+00:00",
    },
    "observed_at": "2026-09-14T01:05:00+00:00",
    "freshness_seconds": 300,
    "truncated": False,
    "content": [{"metric": "http_errors_rate", "value": 0.042}],
}


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


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main() -> int:
    key = read_key()
    client = DeepSeekClient(key, clock=Clock())
    del key
    system = render(
        DISCIPLINE_VARIANT, model_requests=4, report_contract=REPORT_CONTRACT
    )
    question = (
        "Synthetic smoke question: checkout shows elevated HTTP errors in the "
        "authorized window. Continue the investigation with the authorized tool."
    )
    prefix = [
        {"role": "system", "content": system},
        {"role": "user", "content": question},
    ]
    # The folded history: one earlier tool turn, exactly as the loop would fold it.
    folded = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-folded-1",
                    "type": "function",
                    "function": {
                        "name": "metrics_range_query",
                        "arguments": '{"expr":"rate(http_errors[5m])"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-folded-1",
            "content": canonical(SYNTHETIC_VIEW),
        },
    ]
    digest = fold_digest(folded)
    summary = (
        "Working summary: one metrics query was made (evidence_id "
        "smoke-op-1:dispatch-1) showing http_errors_rate 0.042 on checkout-prod in "
        "the authorized window. Cause unknown. Next: one more query, then report."
    )
    compacted = [*prefix, compaction_message(1, digest, summary)]
    ledger: dict = {
        "experiment": "m1-01-loop-long-horizon-compaction-smoke",
        "started": datetime.now(timezone.utc).isoformat(),
        "compaction_instruction_sha256": sha(COMPACTION_INSTRUCTION),
        "requests": [],
    }

    def record(name: str, call: ModelCall, reply=None, error=None) -> None:
        entry: dict = {
            "name": name,
            "request_sha256": sha(serialized_request(call).decode("utf-8")),
            "request_bytes": len(serialized_request(call)),
            "messages": [m["role"] for m in call.messages],
            "tools_attached": call.tools is not None,
        }
        if reply is not None:
            entry.update(
                {
                    "http": "200",
                    "response_model": reply.response_model,
                    "finish_reason": reply.finish_reason,
                    "usage": dict(reply.usage),
                    "tool_call_count": len(reply.tool_calls),
                    "content_chars": len(reply.content or ""),
                    "reasoning_chars": len(reply.reasoning_content or ""),
                }
            )
        if error is not None:
            entry["error"] = error
        ledger["requests"].append(entry)

    # Request 1: the compacted context with tools attached (the loop's next round).
    call1 = ModelCall(
        messages=tuple(compacted),
        tools=(TOOL,),
        json_mode=False,
        max_tokens=2048,
        timeout_seconds=120.0,
    )
    try:
        reply1 = client.complete(call1)
    except ModelError as exc:
        record("compacted+tools", call1, error=exc.code)
        OUT.write_text(json.dumps(ledger, indent=2, ensure_ascii=False) + "\n")
        print(json.dumps({"status": "request_1_failed", "error": exc.code}))
        return 1
    record("compacted+tools", call1, reply1)
    # Request 2: after the compaction message, a retained assistant turn with
    # its real reasoning_content and tool result, tools still attached.
    if reply1.tool_calls:
        assistant = assistant_message(
            content=reply1.content,
            reasoning_content=reply1.reasoning_content,
            tool_calls=reply1.tool_calls,
        )
        results = [
            {
                "role": "tool",
                "tool_call_id": call["id"],
                "content": canonical(
                    {
                        **SYNTHETIC_VIEW,
                        "evidence_id": "smoke-op-2:dispatch-1",
                        "operation_id": "smoke-op-2",
                    }
                ),
            }
            for call in reply1.tool_calls
        ]
        group = pair_tool_results(assistant, results, require_reasoning=True)
        second = [*compacted, *group]
    else:
        second = [
            *compacted,
            assistant_message(
                content=reply1.content,
                reasoning_content=reply1.reasoning_content,
                tool_calls=(),
            ),
            {
                "role": "user",
                "content": "Continue with one authorized query before the report.",
            },
        ]
    call2 = ModelCall(
        messages=tuple(second),
        tools=(TOOL,),
        json_mode=False,
        max_tokens=2048,
        timeout_seconds=120.0,
    )
    try:
        reply2 = client.complete(call2)
    except ModelError as exc:
        record("compacted+retained-turn+tools", call2, error=exc.code)
        OUT.write_text(json.dumps(ledger, indent=2, ensure_ascii=False) + "\n")
        print(json.dumps({"status": "request_2_failed", "error": exc.code}))
        return 1
    record("compacted+retained-turn+tools", call2, reply2)
    ledger["ended"] = datetime.now(timezone.utc).isoformat()
    ledger["prompt_tokens"] = sum(
        int(r["usage"].get("prompt_tokens") or 0) for r in ledger["requests"]
    )
    ledger["completion_tokens"] = sum(
        int(r["usage"].get("completion_tokens") or 0) for r in ledger["requests"]
    )
    OUT.write_text(json.dumps(ledger, indent=2, ensure_ascii=False) + "\n")
    print(
        json.dumps(
            {
                "status": "ok",
                "requests": len(ledger["requests"]),
                "out": str(OUT.relative_to(ROOT)),
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
