"""Bounded live Flash investigation: product loop + fixture tools + usage ledger.

Reads DEEPSEEK_API_KEY from a private env file, never prints it, and writes a
business ledger under docs/evidence/. This is not product intake wiring.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from opspilot.investigation.client import DeepSeekClient
from opspilot.investigation.loop import (
    InvestigationLoop,
    InvestigationRequest,
    ModelCall,
    ModelError,
    ModelReply,
)
from opspilot.investigation.store import MemoryStepStore
from opspilot.tools import TransportResponse
from tests.m1_tool_support import WINDOW_END, WINDOW_START, body, build, registration

LIVE_TOOL = "metrics_range_query"
TOOL_SCHEMAS = (
    {
        "type": "function",
        "function": {
            "name": LIVE_TOOL,
            "description": (
                "Return a projected Prometheus range query for one registered "
                "target inside the authorized window. The view is sampled: a "
                "missing series is unknown, not zero. Listing a series does "
                "not prove health. Available expressions: "
                '["rate(http_errors[5m])"]; any other value returns an error. '
                "At most 512 view bytes; truncated views set truncated true."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "expr": {
                        "type": "string",
                        "description": "Exact PromQL string from the available list.",
                    }
                },
                "required": ["expr"],
            },
        },
    },
)

ROOT = Path(__file__).resolve().parents[1]
MAIN_ENV = Path("/Users/shenghuikevin/dev/AI/production-ops-agent/.env")
OUT = ROOT / "docs/evidence/m1-01-investigation-loop"
CNY_PER_USD = 7.3
INPUT_USD_PER_M = 0.3
OUTPUT_USD_PER_M = 1.2


class SystemClock:
    def now(self):
        return datetime.now(timezone.utc)

    def monotonic(self):
        return time.monotonic()


class RecordingClient:
    def __init__(self, inner: DeepSeekClient):
        self.inner = inner
        self.attempts: list[dict] = []

    def complete(self, call: ModelCall) -> ModelReply:
        started = time.monotonic()
        try:
            reply = self.inner.complete(call)
        except ModelError as exc:
            self.attempts.append(
                {
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "error": exc.code,
                    "usage": None,
                    "json_mode": call.json_mode,
                }
            )
            raise
        self.attempts.append(
            {
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "finish_reason": reply.finish_reason,
                "response_model": reply.response_model,
                "usage": dict(reply.usage),
                "tool_call_count": len(reply.tool_calls),
                "json_mode": call.json_mode,
            }
        )
        return reply


def read_key(path: Path) -> str:
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith("DEEPSEEK_API_KEY="):
            value = line.split("=", 1)[1].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            return value
    return ""


def resolve_env_file() -> Path:
    explicit = os.environ.get("M0_ENV_FILE")
    candidates = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.extend([ROOT / ".env", MAIN_ENV])
    for path in candidates:
        if path.is_file():
            return path.resolve()
    raise SystemExit("credential file unavailable")


def cost_cny(usage: dict) -> float:
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    usd = (
        prompt * INPUT_USD_PER_M / 1_000_000 + completion * OUTPUT_USD_PER_M / 1_000_000
    )
    return round(usd * CNY_PER_USD, 6)


def main() -> int:
    env_file = resolve_env_file()
    key = read_key(env_file)
    if not key:
        print(
            json.dumps(
                {"status": "failed", "failure": "trusted credential unavailable"}
            )
        )
        return 2
    clock = SystemClock()
    deadline = clock.now() + timedelta(minutes=12)
    run_id = str(uuid4())
    executor, transport, _sink, _ = build(
        clock=clock,
        registrations=[registration(name=LIVE_TOOL)],
        scope_overrides={
            "deadline": deadline,
            "tool_names": frozenset({LIVE_TOOL}),
            "run_id": run_id,
        },
    )
    transport.response = TransportResponse(
        body=body([{"metric": "http_errors_rate", "value": 0.042}]),
        data_as_of=WINDOW_START,
    )
    store = MemoryStepStore(
        budget_limit=4, deadline=deadline, clock=clock, run_id=run_id
    )
    recorder = RecordingClient(DeepSeekClient(key, clock=clock))
    del key
    loop = InvestigationLoop(
        model=recorder, executor=executor, store=store, clock=clock
    )
    request = InvestigationRequest(
        run_id=executor.scope.run_id,
        question=(
            "Checkout appears to show elevated HTTP errors. Query the authorized "
            "metrics and return a json investigation report for the authorized window."
        ),
        scope=executor.scope,
        tool_schemas=TOOL_SCHEMAS,
        model_requests=2,
        # The context must carry this Run's own ``run_id`` or the loop's
        # projection discards it wholesale (bot review finding, PR #29), and
        # the policy must carry every field ``eligible_time_policies`` reads
        # for the fixture view (``historical_window`` over the authorized
        # window, judged against the response-received instant).
        evidence_context={
            "type": "opspilot-evidence-context-v4",
            "run_id": run_id,
            "time_policies": [
                {
                    "id": "policy-window-1",
                    "mode": "historical_window",
                    "reference_rule": "response_received_at",
                    "all_authorized_targets": True,
                    "window": {
                        "start": WINDOW_START.isoformat(),
                        "end": WINDOW_END.isoformat(),
                    },
                }
            ],
        },
    )
    started = clock.now()
    outcome = loop.run(request)
    ended = clock.now()
    usages = [
        item["usage"]
        for item in recorder.attempts
        if isinstance(item.get("usage"), dict)
    ]
    ledger = {
        "experiment": "m1-01-flash-loop",
        "run_id": request.run_id,
        "ledger_id": str(uuid4()),
        "started": started.isoformat(),
        "ended": ended.isoformat(),
        "model": "deepseek-flash",
        "http_count": len(recorder.attempts),
        "attempts": recorder.attempts,
        "known_cost_cny_upper": round(sum(cost_cny(u) for u in usages), 6),
        "prompt_tokens": sum(int(u.get("prompt_tokens") or 0) for u in usages),
        "completion_tokens": sum(int(u.get("completion_tokens") or 0) for u in usages),
        "execution": outcome.execution,
        "handoff": outcome.handoff,
        "handoff_reasons": list(outcome.handoff_reasons),
        "report_schema_version": (
            outcome.report.schema_version if outcome.report is not None else None
        ),
        "report_content_sha256": outcome.report_content_sha256,
        "evidence_ids": list(outcome.evidence_ids),
        "steps_committed": outcome.steps_committed,
        "prompt_revision": outcome.prompt_revision,
        "question_sha256": outcome.question_sha256,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "ledger.json").write_text(json.dumps(ledger, indent=2, ensure_ascii=False))
    if outcome.report_content is not None:
        (OUT / "report.json").write_text(outcome.report_content)
    if outcome.report is not None:
        (OUT / "report-parsed.json").write_text(
            outcome.report.model_dump_json(indent=2)
        )
    summary = {
        "status": outcome.execution,
        "handoff": outcome.handoff,
        "http_count": ledger["http_count"],
        "known_cost_cny_upper": ledger["known_cost_cny_upper"],
        "report_schema_version": ledger["report_schema_version"],
        "handoff_reasons": ledger["handoff_reasons"],
    }
    print(json.dumps(summary))
    return 0 if outcome.execution == "completed" and outcome.report is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
