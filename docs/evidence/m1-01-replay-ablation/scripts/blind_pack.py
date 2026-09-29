"""Shuffle and anonymize the replayed samples into reviewer packets.

usage: ABLATION_WORK=<work dir> .venv/bin/python blind_pack.py --reviewers 4 [--seed N]

Writes ``$ABLATION_WORK/blind/``:

- ``mapping.json``       anonymous id -> sample id (kept by the experimenter
                          until every review is in; published afterwards)
- ``views/R<k>/``        the final-round tool-result views of each Run, one
                          pretty-printed JSON per view, always from the
                          baseline messages (never the group 3 variant, so
                          the packet cannot reveal the group)
- ``packets/reviewer-<n>/samples/<anon>.txt``  the report text of each
                          sample, headed only by the anonymous id and the Run
                          alias whose views it must be checked against
- ``packets/reviewer-<n>/index.json``           the list a reviewer walks

Runs are aliased R1..R4 in a shuffled order so the alias says nothing
about normal/fault either.
"""

from __future__ import annotations

import argparse
import json
import os
import random
from pathlib import Path

CASES = ("normal-1", "normal-2", "fault-1", "fault-2")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reviewers", type=int, default=4)
    ap.add_argument("--seed", type=int, default=None)
    # A later round packs only samples absent from the existing mapping,
    # keeps the Run aliases, and numbers its reviewers after the earlier ones.
    ap.add_argument("--round", type=int, default=1)
    args = ap.parse_args()
    work = Path(os.environ["ABLATION_WORK"])
    blind = work / "blind"
    rng = random.Random(
        args.seed if args.seed is not None else int.from_bytes(os.urandom(4), "big")
    )
    mapping_path = blind / "mapping.json"
    previous = (
        json.load(open(mapping_path))
        if args.round > 1
        else {"run_alias": None, "samples": {}}
    )
    if previous["run_alias"]:
        run_alias = previous["run_alias"]
    else:
        aliases = list(CASES)
        rng.shuffle(aliases)
        run_alias = {case: f"R{i + 1}" for i, case in enumerate(aliases)}
    done = {m["sample_id"] for m in previous["samples"].values()}
    used_ids = set(previous["samples"])
    samples = sorted(
        p
        for p in (work / "samples").iterdir()
        if (p / "report.txt").exists() and p.name not in done
    )
    rng.shuffle(samples)
    anon = {}
    for p in samples:
        aid = f"S{rng.randrange(1000, 9999):04d}"
        while aid in anon or aid in used_ids:
            aid = f"S{rng.randrange(1000, 9999):04d}"
        anon[aid] = p
    mapping = dict(previous["samples"])
    first_reviewer = 1 + max(
        (m["reviewer"] for m in previous["samples"].values()), default=0
    )
    (blind / "views").mkdir(parents=True, exist_ok=True)
    for case in CASES:
        messages = json.load(open(work / "requests" / case / "messages.json"))
        vdir = blind / "views" / run_alias[case]
        vdir.mkdir(parents=True, exist_ok=True)
        n = 0
        for message in messages:
            if message.get("role") != "tool":
                continue
            view = json.loads(message["content"])
            n += 1
            (vdir / f"{n:02d}-{view.get('tool')}.json").write_text(
                json.dumps(view, indent=1, ensure_ascii=False)
            )
    ids = list(anon)
    per = [ids[i :: args.reviewers] for i in range(args.reviewers)]
    for r, chunk in enumerate(per, start=first_reviewer):
        pdir = blind / "packets" / f"reviewer-{r}" / "samples"
        pdir.mkdir(parents=True, exist_ok=True)
        index = []
        for aid in chunk:
            path = anon[aid]
            meta = json.load(open(path / "meta.json"))
            alias = run_alias[meta["case"]]
            mapping[aid] = {
                "sample_id": path.name,
                "case": meta["case"],
                "run_alias": alias,
                "reviewer": r,
                "round": args.round,
            }
            text = (path / "report.txt").read_text()
            (pdir / f"{aid}.txt").write_text(f"sample: {aid}\nrun: {alias}\n\n{text}")
            index.append({"sample": aid, "run": alias, "file": f"samples/{aid}.txt"})
        (pdir.parent / "index.json").write_text(json.dumps(index, indent=1))
    (blind / "mapping.json").write_text(
        json.dumps({"run_alias": run_alias, "samples": mapping}, indent=1)
    )
    print(
        "runs",
        run_alias,
        "samples",
        len(mapping),
        "per reviewer",
        [len(c) for c in per],
    )


if __name__ == "__main__":
    main()
