"""Stratified subset of Cloud-OpsBench, frozen before any Run (#871 U5 = a).

Strata are (system, fault category, root-cause label), read from each case's
``metadata.json`` -- the only place the selector looks, and only for those
three facts; no result of any Run is read. Every stratum gets at least one
case; the rest of the target (about 100) is shared out in proportion to the
strata's sizes (floor of the proportional quota, then the largest remainders,
ties by stratum key), never more than a stratum holds. Within a stratum the
cases are drawn with ``random.Random(f"{seed}:{stratum}")`` from the sorted
ids, so the draw does not depend on file-system order or on other strata.

Outputs: ``subset.json`` (seed, target, the case ids only) and
``strata.json`` (each stratum's full-set and subset size, for the weighted
estimate). Which case is in which stratum is not written; the scorer
recomputes it on the rental host.

Usage::

    python -m scripts.cloudopsbench_eval.subset --dataset <checkout> \\
        --seed 871 --target 100 --out-dir docs/evidence/cloudopsbench-871
"""

from __future__ import annotations

import argparse
import json
import math
import random
from collections.abc import Mapping, Sequence
from pathlib import Path

from scripts.cloudopsbench_eval.views import SYSTEMS, CaseRef

SCHEMA = "cloudopsbench-subset-1"


def read_strata(dataset: Path) -> dict[str, str]:
    """``case id -> "system/category/label"`` for every case of the set."""
    strata: dict[str, str] = {}
    for system_dir in SYSTEMS:
        root = dataset / "benchmark" / system_dir
        for meta in sorted(root.glob("*/*/metadata.json")):
            ref = CaseRef.parse(
                f"{system_dir}/{meta.parent.parent.name}/{meta.parent.name}"
            )
            data = json.loads(meta.read_text(encoding="utf-8"))
            label = str((data.get("result") or {}).get("root_cause") or "").strip()
            if not label:
                raise ValueError(f"LABEL_MISSING:{ref.id}")
            strata[ref.id] = f"{ref.system_dir}/{ref.category}/{label}"
    return strata


def _key(case_id: str) -> tuple[str, str, int]:
    ref = CaseRef.parse(case_id)
    return ref.system_dir, ref.category, int(ref.number)


def allocate(sizes: Mapping[str, int], target: int) -> dict[str, int]:
    total = sum(sizes.values())
    quota = {k: n * target / total for k, n in sizes.items()}
    alloc = {k: min(sizes[k], max(1, math.floor(q))) for k, q in quota.items()}
    while sum(alloc.values()) < target:
        open_ = [k for k in sorted(sizes) if alloc[k] < sizes[k]]
        if not open_:
            break
        best = max(open_, key=lambda k: (quota[k] - alloc[k], -sorted(sizes).index(k)))
        alloc[best] += 1
    return alloc


def select(strata: Mapping[str, str], *, seed: int, target: int) -> list[str]:
    members: dict[str, list[str]] = {}
    for case_id, stratum in strata.items():
        members.setdefault(stratum, []).append(case_id)
    alloc = allocate({k: len(v) for k, v in members.items()}, target)
    chosen: list[str] = []
    for stratum in sorted(members):
        ids = sorted(members[stratum], key=_key)
        chosen += random.Random(f"{seed}:{stratum}").sample(ids, alloc[stratum])
    return sorted(chosen, key=_key)


def frozen(
    strata: Mapping[str, str], chosen: Sequence[str], *, seed: int, target: int
) -> tuple[dict[str, object], dict[str, object]]:
    picked = set(chosen)
    rows: dict[str, dict[str, int]] = {}
    for case_id, stratum in strata.items():
        row = rows.setdefault(stratum, {"full": 0, "subset": 0})
        row["full"] += 1
        row["subset"] += case_id in picked
    subset = {
        "schema": SCHEMA,
        "seed": seed,
        "target": target,
        "count": len(chosen),
        "cases": list(chosen),
    }
    weights = {
        "schema": SCHEMA,
        "seed": seed,
        "full_total": len(strata),
        "strata": [{"stratum": k, **rows[k]} for k in sorted(rows)],
    }
    return subset, weights


def weighted_mean(
    scores: Mapping[str, float], strata: Mapping[str, str]
) -> float | None:
    """The full-set estimate: each stratum's mean over its scored cases,
    weighted by the stratum's share of the full set. ``None`` when a stratum
    has no scored case (its weight cannot be carried)."""
    sizes: dict[str, int] = {}
    for stratum in strata.values():
        sizes[stratum] = sizes.get(stratum, 0) + 1
    per: dict[str, list[float]] = {}
    for case_id, value in scores.items():
        per.setdefault(strata[case_id], []).append(value)
    if set(per) != set(sizes):
        return None
    total = len(strata)
    return sum(sizes[k] / total * (sum(v) / len(v)) for k, v in per.items())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.cloudopsbench_eval.subset")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--target", type=int, default=100)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    strata = read_strata(args.dataset)
    chosen = select(strata, seed=args.seed, target=args.target)
    subset, weights = frozen(strata, chosen, seed=args.seed, target=args.target)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for name, value in (("subset.json", subset), ("strata.json", weights)):
        (args.out_dir / name).write_text(json.dumps(value, indent=1) + "\n")
    print(
        f"strata={len(weights['strata'])} cases={len(chosen)} "  # type: ignore[arg-type]
        f"full={len(strata)} -> {args.out_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
