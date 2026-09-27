"""Join ledger, validation and blind-review results into the per-group table.

usage: ABLATION_WORK=<work dir> .venv/bin/python summarize.py \\
           --validation <validation.json> --reviews <reviews dir> [--out summary.json]

``reviews dir`` holds one ``reviewer-<n>.jsonl`` per reviewer (one JSON object
per sample, as the rubric specifies). Ground truth for the upstream verdict:
normal-* Runs -> ``no_failure``; fault-* Runs -> ``failure_located`` with a
target naming payment / Charge.

Prices (USD per 1M tokens, DeepSeek pricing page, off-peak) are applied to the
ledger's usage so each group gets an estimated cost; the balance difference
in the README is the settled figure.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import defaultdict
from pathlib import Path

PRICE = {  # USD per 1M tokens, off-peak
    "deepseek-flash": {"hit": 0.003, "miss": 0.15, "out": 0.6},
    "deepseek-v4-pro": {"hit": 0.022, "miss": 0.66, "out": 1.98},
}
USD_CNY = 7.1
GROUPS = {0: "baseline", 1: "rule", 2: "coverage", 3: "span_groups", 4: "v4-pro"}


def cost_cny(model: str, usage: dict) -> float:
    p = PRICE[model]
    hit = usage.get("prompt_cache_hit_tokens", 0)
    miss = usage.get("prompt_cache_miss_tokens", usage.get("prompt_tokens", 0) - hit)
    out = usage.get("completion_tokens", 0)
    return (hit * p["hit"] + miss * p["miss"] + out * p["out"]) / 1e6 * USD_CNY


def verdict_correct(case: str, review: dict) -> bool:
    verdict = review.get("verdict")
    if case.startswith("normal"):
        return verdict == "no_failure"
    target = (review.get("verdict_target") or "").lower()
    return verdict == "failure_located" and bool(re.search(r"payment|charge", target))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--validation", required=True)
    ap.add_argument("--reviews", required=True)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    work = Path(os.environ["ABLATION_WORK"])
    mapping = json.load(open(work / "blind" / "mapping.json"))["samples"]
    validation = json.load(open(args.validation))
    reviews = {}
    for path in sorted(Path(args.reviews).glob("reviewer-*.jsonl")):
        for line in path.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                reviews[r["sample"]] = r
    ledger = {}
    for line in (work / "ledger.jsonl").read_text().splitlines():
        if line.strip():
            e = json.loads(line)
            ledger[e["sample_id"]] = e
    rows = []
    for anon, m in mapping.items():
        sid = m["sample_id"]
        e = ledger[sid]
        r = reviews.get(anon)
        v = validation.get(sid, {})
        rows.append({
            "sample_id": sid, "anon": anon, "group": e["group"], "case": m["case"],
            "model": e["model"], "usage": e.get("usage", {}),
            "cost_cny": cost_cny(e["model"], e.get("usage", {})),
            "accepted": v.get("accepted"), "reject_reason": v.get("reason"),
            "a": None if r is None else len(r.get("a_errors", [])),
            "e": None if r is None else len(r.get("e_errors", [])),
            "verdict": None if r is None else r.get("verdict"),
            "verdict_correct": None if r is None else verdict_correct(m["case"], r),
            "reviewed": r is not None,
        })
    by_group = defaultdict(list)
    for row in rows:
        by_group[row["group"]].append(row)
    table = []
    for g in sorted(by_group):
        rs = by_group[g]
        reviewed = [r for r in rs if r["reviewed"]]
        n = len(reviewed)
        entry = {
            "group": g, "name": GROUPS[g], "samples": len(rs), "reviewed": n,
            "a_total": sum(r["a"] for r in reviewed),
            "e_total": sum(r["e"] for r in reviewed),
            "samples_with_a": sum(1 for r in reviewed if r["a"]),
            "samples_with_e": sum(1 for r in reviewed if r["e"]),
            "samples_with_a_or_e": sum(1 for r in reviewed if r["a"] or r["e"]),
            "validation_accepted": sum(1 for r in rs if r["accepted"]),
            "verdict_correct": sum(1 for r in reviewed if r["verdict_correct"]),
            "mean_completion_tokens": round(sum(r["usage"].get("completion_tokens", 0) for r in rs) / max(len(rs), 1)),
            "mean_reasoning_tokens": round(sum((r["usage"].get("completion_tokens_details") or {}).get("reasoning_tokens", 0) for r in rs) / max(len(rs), 1)),
            "cost_cny": round(sum(r["cost_cny"] for r in rs), 3),
            "by_case": {},
        }
        for case in ("normal-1", "normal-2", "fault-1", "fault-2"):
            cs = [r for r in reviewed if r["case"] == case]
            entry["by_case"][case] = {
                "n": len(cs), "a": sum(r["a"] for r in cs), "e": sum(r["e"] for r in cs),
                "with_a_or_e": sum(1 for r in cs if r["a"] or r["e"]),
                "accepted": sum(1 for r in by_group[g] if r["case"] == case and r["accepted"]),
                "verdict_correct": sum(1 for r in cs if r["verdict_correct"]),
            }
        table.append(entry)
    for t in table:
        print(json.dumps({k: v for k, v in t.items() if k != "by_case"}))
        for case, c in t["by_case"].items():
            print("   ", case, c)
    if args.out:
        Path(args.out).write_text(json.dumps({"groups": table, "samples": rows}, indent=1))


if __name__ == "__main__":
    main()
