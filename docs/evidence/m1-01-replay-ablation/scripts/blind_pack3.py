"""Phase-3 (unified Opus round) blind packing: every sample goes to two independent reviewers.

usage: ABLATION_WORK=<work dir> .venv/bin/python blind_pack2.py --round N \\
           --samples 'g0-*' 'g1-*' ... [--per-reviewer 10] [--copies 2]

Writes ``$ABLATION_WORK/blind3/``: ``mapping.json`` (anonymous id -> sample,
case, Run alias, the reviewers holding it, packing round), ``views/R<k>/``
(baseline final-round views of every Run present under ``requests/``, one
pretty JSON per view; never the span_groups variant) and
``packets/reviewer-<n>/{index.json,samples/<anon>.txt}``. Reviewer numbers
continue across rounds so result files never collide. Each sample is placed
with ``copies`` distinct reviewers chosen by lowest current load.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import random
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--round", type=int, required=True)
    ap.add_argument("--samples", nargs="+", required=True)
    ap.add_argument("--per-reviewer", type=int, default=10)
    ap.add_argument("--copies", type=int, default=2)
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()
    work = Path(os.environ["ABLATION_WORK"])
    blind = work / "blind3"
    rng = random.Random(
        args.seed if args.seed is not None else int.from_bytes(os.urandom(4), "big")
    )
    mapping_path = blind / "mapping.json"
    previous = (
        json.load(open(mapping_path))
        if mapping_path.exists()
        else {"run_alias": {}, "samples": {}}
    )
    run_alias: dict[str, str] = dict(previous["run_alias"])
    cases = sorted(
        p.name for p in (work / "requests").iterdir() if (p / "messages.json").exists()
    )
    new_cases = [c for c in cases if c not in run_alias]
    rng.shuffle(new_cases)
    for case in new_cases:
        run_alias[case] = f"R{len(run_alias) + 1}"
    for case in cases:
        vdir = blind / "views" / run_alias[case]
        if vdir.exists():
            continue
        vdir.mkdir(parents=True)
        n = 0
        for message in json.load(open(work / "requests" / case / "messages.json")):
            if message.get("role") != "tool":
                continue
            view = json.loads(message["content"])
            n += 1
            (vdir / f"{n:02d}-{view.get('tool')}.json").write_text(
                json.dumps(view, indent=1, ensure_ascii=False)
            )
    done = {m["sample_id"] for m in previous["samples"].values()}
    used = set(previous["samples"])
    samples = sorted(
        p
        for p in (work / "samples").iterdir()
        if (p / "report.txt").exists()
        and p.name not in done
        and any(fnmatch.fnmatch(p.name, pat) for pat in args.samples)
    )
    rng.shuffle(samples)
    reviewers_needed = -(-len(samples) * args.copies // args.per_reviewer)
    first = 1 + max(
        (r for m in previous["samples"].values() for r in m["reviewers"]), default=0
    )
    reviewers = list(range(first, first + reviewers_needed))
    load = {r: 0 for r in reviewers}
    packets: dict[int, list[dict]] = {r: [] for r in reviewers}
    mapping = dict(previous["samples"])
    for path in samples:
        aid = f"S{rng.randrange(1000, 9999):04d}"
        while aid in used:
            aid = f"S{rng.randrange(1000, 9999):04d}"
        used.add(aid)
        meta = json.load(open(path / "meta.json"))
        alias = run_alias[meta["case"]]
        order = sorted(reviewers, key=lambda r: (load[r], rng.random()))
        chosen = order[: args.copies]
        for r in chosen:
            load[r] += 1
            packets[r].append(
                {"sample": aid, "run": alias, "file": f"samples/{aid}.txt"}
            )
            pdir = blind / "packets" / f"reviewer-{r}" / "samples"
            pdir.mkdir(parents=True, exist_ok=True)
            (pdir / f"{aid}.txt").write_text(
                f"sample: {aid}\nrun: {alias}\n\n{(path / 'report.txt').read_text()}"
            )
        mapping[aid] = {
            "sample_id": path.name,
            "case": meta["case"],
            "run_alias": alias,
            "reviewers": chosen,
            "round": args.round,
        }
    for r, index in packets.items():
        (blind / "packets" / f"reviewer-{r}" / "index.json").write_text(
            json.dumps(index, indent=1)
        )
    blind.mkdir(parents=True, exist_ok=True)
    mapping_path.write_text(
        json.dumps({"run_alias": run_alias, "samples": mapping}, indent=1)
    )
    print(
        "runs",
        run_alias,
        "new samples",
        len(samples),
        "reviewers",
        reviewers,
        "load",
        load,
    )


if __name__ == "__main__":
    main()
