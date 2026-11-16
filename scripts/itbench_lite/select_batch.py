"""Integration-batch selection for ITBench-Lite (#646).

Rule (written into docs/tasks/2026-11-15-itbench-lite-root-cause.md before
any run): for each coverage tier in ``tiers.json`` (``{"S<id>": tier}``),
order the tier's scenario keys by ``sha256(salt + key)`` hex ascending and
take the first ``per_tier``.  Uses only the tier file, never scores or
answers.

Usage::

    python -m scripts.itbench_lite.select_batch \\
        docs/evidence/itbench-lite-sre-lite-full-1114/tiers.json \\
        --salt 646: --per-tier 4

prints one JSON object (tier -> keys) and, last, the driver's
``--scenarios`` value (numeric ids, comma-separated).
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path


def select(tiers: Mapping[str, str], salt: str, per_tier: int) -> dict[str, list[str]]:
    by_tier: dict[str, list[str]] = {}
    for key, tier in tiers.items():
        by_tier.setdefault(tier, []).append(key)
    return {
        tier: sorted(
            keys, key=lambda k: hashlib.sha256((salt + k).encode("utf-8")).hexdigest()
        )[:per_tier]
        for tier, keys in sorted(by_tier.items())
    }


def scenarios_arg(selected: Mapping[str, Sequence[str]]) -> str:
    return ",".join(key.removeprefix("S") for keys in selected.values() for key in keys)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tiers", type=Path)
    parser.add_argument("--salt", required=True)
    parser.add_argument("--per-tier", type=int, required=True)
    args = parser.parse_args(argv)
    tiers = json.loads(args.tiers.read_text())
    selected = select(tiers, args.salt, args.per_tier)
    print(json.dumps(selected, ensure_ascii=False))
    print(scenarios_arg(selected))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
