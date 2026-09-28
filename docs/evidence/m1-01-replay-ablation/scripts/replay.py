"""Replay the rebuilt final-report request under one experiment group.

usage: M0_ENV_FILE=<private .env> ABLATION_WORK=<work dir> \\
       .venv/bin/python replay.py --group N [--repeats 5] [--cases ...] [--workers 3]

Groups (only the listed change is applied; everything else is byte-identical
to the request the live Run sent, see ``rebuild_final_requests.py``):

  0  baseline: as rebuilt.
  1  rule: RULE_SENTENCE appended to the FINAL_REPORT_INSTRUCTION user message.
  2  coverage: COVERAGE_SENTENCE appended to the same message.
  3  span_groups: every traces_search tool-result view gains ``span_groups``
     (``span_groups.py``); no instruction text changes.
  4  model: messages unchanged, model ``deepseek-v4-pro`` instead of
     ``deepseek-flash``; every other request parameter kept.
  5  combination (phase 2): group 1's RULE_SENTENCE and group 3's
     ``span_groups`` together, text and algorithm unchanged.
  6  official-style final instruction (phase C): the run coverage message is
     moved BEFORE the final instruction (so the instruction is the last user
     message) and the instruction text is replaced by OFFICIAL_INSTRUCTION;
     the system prompt and everything else are unchanged.
  6b group 6 plus ``span_groups`` (same algorithm as group 3).

Every HTTP call is appended to ``$ABLATION_WORK/ledger.jsonl`` (usage, model,
finish reason, request sha256, elapsed). The script refuses to start a call
once the ledger holds ``MAX_CALLS`` entries. ``reasoning_content`` is a
private protocol field and is never written anywhere; only its length is.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from opspilot.investigation.client import DeepSeekClient
from opspilot.investigation.loop import ModelCall, ModelError, serialized_request
from opspilot.investigation.reports import FINAL_REPORT_INSTRUCTION

sys.path.insert(0, str(Path(__file__).resolve().parent))
from span_groups import transform_messages  # noqa: E402

# Phase 1 ran under 130; the lead raised the ceiling to 250 for phase 2 (the
# ledger is cumulative, so this bounds the whole experiment).
MAX_CALLS = 250
CASES = ("normal-1", "normal-2", "fault-1", "fault-2")
RULE_SENTENCE = (
    "State only values that appear in the cited view. A field that does not "
    "appear was not recorded; do not fill in a default such as status code 0 "
    "or HTTP 200."
)
COVERAGE_SENTENCE = (
    "When a statement summarizes several rows, give the number of rows that "
    "support it out of the rows shown (for example 13 of 20 rows carry "
    "rpc.grpc.status_code 0; 7 have no status recorded), and do not use "
    "all/every/none unless every shown row supports it."
)
STRONGER_MODEL = "deepseek-v4-pro"
OFFICIAL_INSTRUCTION = (
    "This is the final report request. Use only the delivered views and the "
    "report schema already provided. Return exactly one json object; do not "
    "emit prose, Markdown fences, tool calls, or fresh queries.\n"
    "\n"
    "- State only values that appear in the cited view.\n"
    "- If a field is absent, it was not recorded; never substitute a default "
    "such as status code 0 or HTTP 200.\n"
    "- When summarizing multiple rows, use the counts, ranges, and grouped "
    "summaries provided by the view; do not recalculate them from individual "
    "rows.\n"
    "- Treat omitted, sampled, truncated, empty, or non-ok data as an unknown "
    "or gap unless the view explicitly states otherwise.\n"
    "- Cite complete evidence_id values."
)


def parse_group(text: str):
    """Groups 0-5 are ints; the phase C groups are the strings "6" and "6b"."""
    return text if text in ("6", "6b") else int(text)


def read_key() -> str:
    path = os.environ.get("M0_ENV_FILE")
    if not path:
        raise SystemExit("set M0_ENV_FILE")
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line.startswith("DEEPSEEK_API_KEY="):
            value = line.split("=", 1)[1].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            return value
    raise SystemExit("DEEPSEEK_API_KEY missing")


def append_sentence(messages: list[dict], sentence: str) -> list[dict]:
    hits = [i for i, m in enumerate(messages)
            if m.get("role") == "user" and m.get("content") == FINAL_REPORT_INSTRUCTION]
    if len(hits) != 1:
        raise SystemExit("final report instruction message not found exactly once")
    out = [dict(m) for m in messages]
    out[hits[0]]["content"] = FINAL_REPORT_INSTRUCTION + " " + sentence
    return out


def official_final(messages: list[dict]) -> list[dict]:
    """Group 6: swap the two trailing user messages (coverage first) and
    replace the final instruction text."""
    hits = [i for i, m in enumerate(messages)
            if m.get("role") == "user" and m.get("content") == FINAL_REPORT_INSTRUCTION]
    if len(hits) != 1 or hits[0] != len(messages) - 2 or messages[-1].get("role") != "user":
        raise SystemExit("final instruction + coverage message not found as the trailing pair")
    coverage = dict(messages[-1])
    return [*messages[:-2], coverage, {"role": "user", "content": OFFICIAL_INSTRUCTION}]


def build_call(group: str, messages: list[dict], params: dict) -> ModelCall:
    model = params["model"]
    if group == 1:
        messages = append_sentence(messages, RULE_SENTENCE)
    elif group == 2:
        messages = append_sentence(messages, COVERAGE_SENTENCE)
    elif group == 3:
        messages = transform_messages(messages)
    elif group == 4:
        model = STRONGER_MODEL
    elif group == 5:
        messages = transform_messages(append_sentence(messages, RULE_SENTENCE))
    elif group == "6":
        messages = official_final(messages)
    elif group == "6b":
        messages = transform_messages(official_final(messages))
    elif group != 0:
        raise SystemExit("unknown group")
    return ModelCall(
        messages=tuple(messages),
        tools=None,
        json_mode=params["json_mode"],
        max_tokens=params["max_tokens"],
        timeout_seconds=params["timeout_seconds"],
        model=model,
    )


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()

    def count(self) -> int:
        if not self.path.exists():
            return 0
        return sum(1 for line in self.path.read_text().splitlines() if line.strip())

    def reserve(self) -> bool:
        with self.lock:
            return self.count() < MAX_CALLS

    def append(self, entry: dict) -> None:
        with self.lock:
            with self.path.open("a") as f:
                f.write(json.dumps(entry) + "\n")


def run_sample(client: DeepSeekClient, ledger: Ledger, group, case: str,
               rep: int, work: Path, samples: Path) -> dict:
    sample_id = f"g{group}-{case}-r{rep}"
    out = samples / sample_id
    if (out / "meta.json").exists():
        return json.load(open(out / "meta.json"))
    req = work / "requests" / case
    messages = json.load(open(req / "messages.json"))
    params = json.load(open(req / "params.json"))
    call = build_call(group, messages, params)
    body = serialized_request(call)
    request_sha = hashlib.sha256(body).hexdigest()
    if not ledger.reserve():
        raise SystemExit(f"MAX_CALLS={MAX_CALLS} reached; refusing {sample_id}")
    started = time.monotonic()
    entry = {"sample_id": sample_id, "group": group, "case": case, "rep": rep,
             "model": call.model, "request_sha256": request_sha,
             "request_bytes": len(body), "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    try:
        reply = client.complete(call)
    except ModelError as exc:
        entry.update({"error": exc.code, "elapsed_s": round(time.monotonic() - started, 2)})
        ledger.append(entry)
        out.mkdir(parents=True, exist_ok=True)
        (out / "meta.json").write_text(json.dumps(entry, indent=1))
        return entry
    entry.update({
        "elapsed_s": round(time.monotonic() - started, 2),
        "response_model": reply.response_model,
        "response_id": reply.raw.get("id"),
        "finish_reason": reply.finish_reason,
        "usage": dict(reply.usage),
        "content_chars": len(reply.content or ""),
        "reasoning_chars": len(reply.reasoning_content or ""),
        "tool_calls": len(reply.tool_calls),
    })
    ledger.append(entry)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.txt").write_text(reply.content or "")
    (out / "meta.json").write_text(json.dumps(entry, indent=1))
    return entry


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", type=parse_group, required=True)
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--cases", nargs="*", default=list(CASES))
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    work = Path(os.environ["ABLATION_WORK"])
    samples = work / "samples"
    ledger = Ledger(work / "ledger.jsonl")
    if args.dry_run:
        for case in args.cases:
            req = work / "requests" / case
            call = build_call(args.group, json.load(open(req / "messages.json")),
                              json.load(open(req / "params.json")))
            body = serialized_request(call)
            print(case, call.model, len(body), hashlib.sha256(body).hexdigest())
        return
    client = DeepSeekClient(read_key())
    jobs = [(case, rep) for rep in range(1, args.repeats + 1) for case in args.cases]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run_sample, client, ledger, args.group, case, rep, work, samples)
                   for case, rep in jobs]
        for future in futures:
            entry = future.result()
            usage = entry.get("usage") or {}
            print(entry["sample_id"], entry.get("error") or entry.get("finish_reason"),
                  usage.get("prompt_tokens"), usage.get("prompt_cache_hit_tokens"),
                  usage.get("completion_tokens"), entry.get("elapsed_s"), flush=True)


if __name__ == "__main__":
    main()
