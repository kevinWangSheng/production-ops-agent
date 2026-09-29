"""Replay the rebuilt report-producing request of one Run under one variant.

usage: M0_ENV_FILE=<private .env> ECLASS_WORK=<dir> \
       .venv/bin/python replay_eclass.py --variant base --cases fault-1 fault-2 --repeats 8

``$ECLASS_WORK/requests/<case>/`` comes from ``rebuild_offline.py``. Every HTTP
call is appended to ``$ECLASS_WORK/ledger.jsonl`` (usage, finish reason,
request sha256, elapsed); the script refuses to start a call once the ledger
holds MAX_CALLS entries. ``reasoning_content`` returned by the model is never
written; only its length is.

Variants (only the listed change is applied; the rest is byte-identical to
what ``rebuild_offline.py`` produced, which itself lacks the earlier turns'
private reasoning -- see README):

  base          as rebuilt.
  metric_note   every ``metrics_range_query`` tool-result view whose PromQL
                mentions ``traces_span_metrics_calls_total`` gains a
                ``series_note`` field stating what that series counts.
  prov_feedback (second stage) the model's own first reply is checked
                deterministically for numbers absent from the cited views and
                a repair message naming them is appended; see
                ``provenance.py``. Implemented in ``replay_repair.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from opspilot.investigation.client import DeepSeekClient
from opspilot.investigation.loop import ModelCall, ModelError, serialized_request
from opspilot.tools.registry import canonical

MAX_CALLS = 170
SERIES_NOTE = (
    "traces_span_metrics_calls_total counts spans of every operation of the "
    "service (internal, client and server spans alike), summed over the labels "
    "kept in this query; it is not a count of requests. status_code "
    "STATUS_CODE_UNSET means the span carried no status; it does not mean the "
    "call succeeded."
)


def read_key() -> str:
    for line in Path(os.environ["M0_ENV_FILE"]).read_text().splitlines():
        line = line.strip()
        if line.startswith("DEEPSEEK_API_KEY="):
            v = line.split("=", 1)[1].strip()
            return v[1:-1] if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"" else v
    raise SystemExit("DEEPSEEK_API_KEY missing")


def apply_metric_note(messages: list[dict]) -> list[dict]:
    out, changed = [], 0
    for m in messages:
        if m.get("role") == "tool":
            try:
                payload = json.loads(m["content"])
            except (TypeError, ValueError):
                payload = None
            if (
                isinstance(payload, dict)
                and payload.get("tool") == "metrics_range_query"
                and "traces_span_metrics_calls_total"
                in str((payload.get("query") or {}).get("expr"))
            ):
                payload["series_note"] = SERIES_NOTE
                m = {**m, "content": canonical(payload)}
                changed += 1
        out.append(m)
    if not changed:
        raise SystemExit("no calls_total view found")
    return out


def build(variant: str, work: Path, case: str) -> ModelCall:
    req = work / "requests" / case
    messages = json.load(open(req / "messages.json"))
    params = json.load(open(req / "params.json"))
    tools = json.load(open(req / "tools.json"))
    if variant == "metric_note":
        messages = apply_metric_note(messages)
    elif variant != "base":
        raise SystemExit(f"unknown variant {variant}")
    return ModelCall(
        messages=tuple(messages),
        tools=tuple(tools) if tools else None,
        json_mode=params["json_mode"],
        max_tokens=params["max_tokens"],
        timeout_seconds=params["timeout_seconds"],
        model=params["model"],
    )


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path, self.lock = path, threading.Lock()

    def count(self) -> int:
        if not self.path.exists():
            return 0
        return sum(1 for line in self.path.read_text().splitlines() if line.strip())

    def reserve(self) -> bool:
        with self.lock:
            return self.count() < MAX_CALLS

    def append(self, entry: dict) -> None:
        with self.lock, self.path.open("a") as f:
            f.write(json.dumps(entry) + "\n")


def run_sample(client, ledger, variant, case, rep, work, samples) -> dict:
    sample_id = f"{variant}-{case}-r{rep}"
    out = samples / sample_id
    if (out / "meta.json").exists():
        return json.load(open(out / "meta.json"))
    call = build(variant, work, case)
    body = serialized_request(call)
    if not ledger.reserve():
        raise SystemExit(f"MAX_CALLS={MAX_CALLS} reached; refusing {sample_id}")
    started = time.monotonic()
    entry = {
        "sample_id": sample_id,
        "variant": variant,
        "case": case,
        "rep": rep,
        "request_sha256": hashlib.sha256(body).hexdigest(),
        "request_bytes": len(body),
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    out.mkdir(parents=True, exist_ok=True)
    try:
        reply = client.complete(call)
    except ModelError as exc:
        entry.update({"error": exc.code, "elapsed_s": round(time.monotonic() - started, 2)})
        ledger.append(entry)
        return entry  # no meta.json: a failed call is retried on the next run
    entry.update(
        {
            "elapsed_s": round(time.monotonic() - started, 2),
            "response_model": reply.response_model,
            "finish_reason": reply.finish_reason,
            "usage": dict(reply.usage),
            "content_chars": len(reply.content or ""),
            "reasoning_chars": len(reply.reasoning_content or ""),
            "tool_calls": len(reply.tool_calls),
        }
    )
    ledger.append(entry)
    (out / "report.txt").write_text(reply.content or "")
    (out / "meta.json").write_text(json.dumps(entry, indent=1))
    return entry


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", required=True)
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--cases", nargs="+", required=True)
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()
    work = Path(os.environ["ECLASS_WORK"])
    samples = work / "samples"
    ledger = Ledger(work / "ledger.jsonl")
    client = DeepSeekClient(read_key())
    jobs = [(c, r) for r in range(1, args.repeats + 1) for c in args.cases]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = [
            pool.submit(run_sample, client, ledger, args.variant, c, r, work, samples)
            for c, r in jobs
        ]
        for f in futs:
            e = f.result()
            u = e.get("usage") or {}
            print(
                e["sample_id"],
                e.get("error") or e.get("finish_reason"),
                u.get("prompt_tokens"),
                u.get("prompt_cache_hit_tokens"),
                u.get("completion_tokens"),
                e.get("elapsed_s"),
                flush=True,
            )


if __name__ == "__main__":
    main()
