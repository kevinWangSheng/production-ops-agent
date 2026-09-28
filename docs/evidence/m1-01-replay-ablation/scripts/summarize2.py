"""Phase-2 summary: two blind reviews per sample, third-reviewer adjudication.

usage: ABLATION_WORK=<work dir> .venv/bin/python summarize2.py --validation <validation.json> [--out summary2.json]

Per sample the final counts are the adjudicator's when the two reviewers
disagreed (a count, e count or verdict), otherwise the agreed values.
Agreement rates: exact (both a and e counts equal), presence (both agree on
whether a >= 1 and whether e >= 1), verdict. Upstream verdict correctness:
normal-* -> no_failure; fault-* -> failure_located naming payment/Charge;
held-out cases carry their own expected target (``EXPECTED``).
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import defaultdict
from pathlib import Path

GROUPS = {"0": "baseline", "1": "rule", "3": "span_groups", "5": "rule+span_groups",
          "6": "official-prompt", "6b": "official-prompt+span_groups"}
EXPECTED = {  # case prefix -> regex the blamed target must match
    "fault": r"payment|charge",
    "pc-fault": r"product.?catalog|getproduct",
    "cart-fault": r"cart|emptycart",
}
PRICE = {"deepseek-flash": {"hit": 0.003, "miss": 0.15, "out": 0.6},
         "deepseek-v4-pro": {"hit": 0.022, "miss": 0.66, "out": 1.98}}


def expected_ok(case: str, review: dict) -> bool:
    if case.startswith("normal"):
        return review.get("verdict") == "no_failure"
    for prefix, pattern in EXPECTED.items():
        if case.startswith(prefix):
            target = (review.get("verdict_target") or "").lower()
            return review.get("verdict") == "failure_located" and bool(re.search(pattern, target))
    return False


def group_of(sample_id: str) -> str:
    return sample_id.split("-")[0][1:]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--validation", required=True)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    work = Path(os.environ["ABLATION_WORK"])
    blind = work / "blind2"
    mapping = json.load(open(blind / "mapping.json"))["samples"]
    validation = json.load(open(args.validation))
    reviews: dict[tuple[int, str], dict] = {}
    for path in (work / "reviews2").glob("reviewer-*.jsonl"):
        n = int(path.stem.split("-")[1])
        for line in path.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                reviews[(n, r["sample"])] = r
    adjudicated: dict[str, dict] = {}
    for path in (work / "reviews2").glob("adjudicator-*.jsonl"):
        for line in path.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                adjudicated[r["sample"]] = r
    ledger = {}
    for line in (work / "ledger.jsonl").read_text().splitlines():
        if line.strip():
            e = json.loads(line)
            ledger[e["sample_id"]] = e
    rows = []
    for aid, m in mapping.items():
        pair = [reviews.get((r, aid)) for r in m["reviewers"]]
        if any(p is None for p in pair):
            continue
        a_counts = [len(p.get("a_errors", [])) for p in pair]
        e_counts = [len(p.get("e_errors", [])) for p in pair]
        verdicts = [p.get("verdict") for p in pair]
        exact = a_counts[0] == a_counts[1] and e_counts[0] == e_counts[1]
        presence = (a_counts[0] > 0) == (a_counts[1] > 0) and (e_counts[0] > 0) == (e_counts[1] > 0)
        verdict_agree = verdicts[0] == verdicts[1]
        disagreed = not (exact and verdict_agree)
        final = adjudicated.get(aid)
        if final is None:
            if disagreed:
                continue  # awaiting adjudication
            final = pair[0]
        sid = m["sample_id"]
        e = ledger[sid]
        usage = e.get("usage", {})
        p = PRICE[e["model"]]
        hit = usage.get("prompt_cache_hit_tokens", 0)
        miss = usage.get("prompt_cache_miss_tokens", usage.get("prompt_tokens", 0) - hit)
        cost = (hit * p["hit"] + miss * p["miss"] + usage.get("completion_tokens", 0) * p["out"]) / 1e6 * 7.1
        rows.append({
            "sample_id": sid, "anon": aid, "group": group_of(sid), "case": m["case"],
            "a": len(final.get("a_errors", [])), "e": len(final.get("e_errors", [])),
            "verdict": final.get("verdict"), "verdict_correct": expected_ok(m["case"], final),
            "adjudicated": aid in adjudicated, "exact_agree": exact, "presence_agree": presence,
            "verdict_agree": verdict_agree, "reviewer_a": a_counts, "reviewer_e": e_counts,
            "accepted": validation.get(sid, {}).get("accepted"), "reject_reason": validation.get(sid, {}).get("reason"),
            "reject_details": validation.get(sid, {}).get("details") or ([validation[sid]["reason"]] if sid in validation and not validation[sid]["accepted"] else []),
            "completion_tokens": usage.get("completion_tokens", 0),
            "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0),
            "cost_cny": cost,
        })
    pending = sum(1 for aid, m in mapping.items()
                  if all(reviews.get((r, aid)) for r in m["reviewers"]) and aid not in {r["anon"] for r in rows})
    by = defaultdict(list)
    for r in rows:
        by[(r["case"].split("-")[0] if not r["case"].startswith(("pc", "cart")) else "heldout", r["group"])].append(r)
    table = []
    for (scenario, g), rs in sorted(by.items(), key=lambda kv: (kv[0][0], kv[0][1].ljust(2))):
        n = len(rs)
        entry = {
            "scenario": scenario, "group": g, "name": GROUPS.get(g, g), "n": n,
            "a_total": sum(r["a"] for r in rs), "e_total": sum(r["e"] for r in rs),
            "samples_with_a": sum(1 for r in rs if r["a"]), "samples_with_e": sum(1 for r in rs if r["e"]),
            "samples_with_a_or_e": sum(1 for r in rs if r["a"] or r["e"]),
            "validation_accepted": sum(1 for r in rs if r["accepted"]),
            "reject_reasons": dict(sorted(__import__("collections").Counter(
                d for r in rs for d in r["reject_details"]).items())),
            "verdict_correct": sum(1 for r in rs if r["verdict_correct"]),
            "adjudicated": sum(1 for r in rs if r["adjudicated"]),
            "exact_agree": sum(1 for r in rs if r["exact_agree"]),
            "presence_agree": sum(1 for r in rs if r["presence_agree"]),
            "verdict_agree": sum(1 for r in rs if r["verdict_agree"]),
            "mean_completion_tokens": round(sum(r["completion_tokens"] for r in rs) / max(n, 1)),
            "mean_reasoning_tokens": round(sum(r["reasoning_tokens"] for r in rs) / max(n, 1)),
            "cost_cny": round(sum(r["cost_cny"] for r in rs), 3),
            "by_case": {},
        }
        for case in sorted({r["case"] for r in rs}):
            cs = [r for r in rs if r["case"] == case]
            entry["by_case"][case] = {"n": len(cs), "a": sum(r["a"] for r in cs), "e": sum(r["e"] for r in cs),
                                      "with_a_or_e": sum(1 for r in cs if r["a"] or r["e"]),
                                      "verdict_correct": sum(1 for r in cs if r["verdict_correct"])}
        table.append(entry)
    overall = {
        "samples": len(rows), "pending_adjudication": pending,
        "exact_agree": sum(1 for r in rows if r["exact_agree"]),
        "presence_agree": sum(1 for r in rows if r["presence_agree"]),
        "verdict_agree": sum(1 for r in rows if r["verdict_agree"]),
        "adjudicated": sum(1 for r in rows if r["adjudicated"]),
    }
    print(json.dumps(overall))
    for t in table:
        print(json.dumps({k: v for k, v in t.items() if k != "by_case"}))
        for case, c in t["by_case"].items():
            print("   ", case, c)
    if args.out:
        Path(args.out).write_text(json.dumps({"overall": overall, "groups": table, "samples": rows}, indent=1))


if __name__ == "__main__":
    main()
