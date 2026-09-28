"""Find the samples whose two blind reviewers disagree and pack them for a third.

usage: ABLATION_WORK=<work dir> .venv/bin/python adjudicate_pack.py --round N [--per-reviewer 8]

Disagreement = the two reviewers' a-class counts differ, or their e-class
counts differ, or their verdicts differ. Each disagreeing sample goes to one
adjudicator (numbered after the last packed reviewer, ``adjudicator-<n>``)
with the report, the Run alias, and the two reviews labelled A and B (the
reviewer numbers are not shown). Packs only samples of the given packing
round that have no adjudication line yet. Writes
``$ABLATION_WORK/blind4/adjudication/adjudicator-<n>/{index.json,samples/}``
and records the assignment in ``blind4/adjudication.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import random
from pathlib import Path


def load_reviews(work: Path) -> dict[tuple[int, str], dict]:
    out = {}
    for path in (work / "reviews4").glob("reviewer-*.jsonl"):
        n = int(path.stem.split("-")[1])
        for line in path.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                out[(n, r["sample"])] = r
    return out


def disagree(a: dict, b: dict) -> bool:
    return (len(a.get("a_errors", [])) != len(b.get("a_errors", []))
            or len(a.get("e_errors", [])) != len(b.get("e_errors", []))
            or a.get("verdict") != b.get("verdict"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--round", type=int, required=True)
    ap.add_argument("--per-reviewer", type=int, default=8)
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()
    work = Path(os.environ["ABLATION_WORK"])
    blind = work / "blind4"
    rng = random.Random(args.seed if args.seed is not None else int.from_bytes(os.urandom(4), "big"))
    mapping = json.load(open(blind / "mapping.json"))["samples"]
    reviews = load_reviews(work)
    adj_path = blind / "adjudication.json"
    adjudication = json.load(open(adj_path)) if adj_path.exists() else {}
    todo = []
    for aid, m in mapping.items():
        if m["round"] != args.round or aid in adjudication:
            continue
        pair = [reviews.get((r, aid)) for r in m["reviewers"]]
        if any(p is None for p in pair):
            raise SystemExit(f"{aid}: missing review from {m['reviewers']}")
        if disagree(*pair):
            todo.append((aid, m, pair))
    rng.shuffle(todo)
    last = max([int(p.name.split("-")[1]) for p in (blind / "packets").iterdir()]
               + [int(k) for v in adjudication.values() for k in [v["adjudicator"]]], default=0)
    n_adj = -(-len(todo) // args.per_reviewer)
    for i in range(n_adj):
        num = last + 1 + i
        chunk = todo[i::n_adj]
        pdir = blind / "adjudication" / f"adjudicator-{num}" / "samples"
        pdir.mkdir(parents=True, exist_ok=True)
        index = []
        for aid, m, pair in chunk:
            labelled = [{**p, "sample": aid, "reviewer": label} for p, label in zip(pair, "AB")]
            report = (work / "samples" / m["sample_id"] / "report.txt").read_text()
            (pdir / f"{aid}.txt").write_text(f"sample: {aid}\nrun: {m['run_alias']}\n\n{report}")
            (pdir / f"{aid}.reviews.json").write_text(json.dumps(labelled, indent=1, ensure_ascii=False))
            index.append({"sample": aid, "run": m["run_alias"], "file": f"samples/{aid}.txt",
                          "reviews": f"samples/{aid}.reviews.json"})
            adjudication[aid] = {"adjudicator": num, "round": args.round}
        (pdir.parent / "index.json").write_text(json.dumps(index, indent=1))
    adj_path.write_text(json.dumps(adjudication, indent=1))
    agreed = sum(1 for aid, m in mapping.items() if m["round"] == args.round) - len(todo)
    print(f"round {args.round}: {len(todo)} disagreements, {agreed} agreed; adjudicators", [last + 1 + i for i in range(n_adj)])


if __name__ == "__main__":
    main()
