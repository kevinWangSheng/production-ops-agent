"""Coverage-tiered score table for the ITBench-Lite SRE run (#582, L5/L11).

All scenarios stay in the denominator: a missing trial or a trial without a
score counts as 0, never as a dropped run.  Scenarios are grouped by a
caller-supplied tier file and the two scenarios with incomplete snapshot data
(S38, S18) are flagged.  The tier file must be built from the census columns
that carry no answer information (L11); this module only reads the file it is
given.

Tier file: JSON object ``{"<scenario id>": "<tier label>", ...}``.
Scores file: JSON object ``{"<scenario id>": [{"<metric>": number, ...}, ...]}``,
one mapping per trial.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

# The 35 SRE scenario directory names of ITBench-Lite v0.2, as listed (names
# only) by the 2026-11-04 replay survey and the 2026-11-12 cluster-object
# census outside the repo; both lists agree.  Ids only, no answer content.
SCENARIO_IDS: frozenset[str] = frozenset(
    "1 2 4 5 6 7 8 9 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 29 31 33 34 "
    "35 38 80 81 83 91 102 105".split()
)
EXPECTED_SCENARIOS = len(SCENARIO_IDS)
EXPECTED_TRIALS = 3
DATA_INCOMPLETE = frozenset({"38", "18"})
DATA_INCOMPLETE_LABEL = "数据残缺"
PROPAGATION_LABEL = "受转换限制"
NOTES = (
    "根因实体由评测侧确定性转换得到，「是否根因」为启发式（决定表 L2）。",
    "传播链只抽报告总结里的显式箭头，相关分数受转换限制（L3）。",
    "没有报告或缺失的 Run 计 0 分并留在分母（L5）；全部场景都在分母里。",
    "S38、S18 快照数据残缺，照常计入分母并单独标出。",
)

_ID_PATTERNS = (
    re.compile(r"^[Ss](\d+)$"),
    re.compile(r"^[Ss]cenario[-_]?(\d+)$"),
    re.compile(r"^[Ii]ncident[-_]?(\d+)$"),
    re.compile(r"^(\d+)$"),
)


def scenario_key(value: str) -> str:
    """``S38``, ``Scenario-38``, ``scenario_38`` and ``38`` all become ``38``."""

    text = str(value).strip()
    for pattern in _ID_PATTERNS:
        match = pattern.match(text)
        if match:
            return str(int(match.group(1)))
    raise ValueError(f"unrecognised scenario id: {value!r}")


def load_tiers(
    tiers: Mapping[str, str], *, expected: frozenset[str] = SCENARIO_IDS
) -> dict[str, str]:
    """Validate and canonicalise a tier mapping."""

    result: dict[str, str] = {}
    for raw_id, tier in tiers.items():
        key = scenario_key(raw_id)
        if key in result:
            raise ValueError(f"duplicate scenario id: {raw_id!r}")
        if not isinstance(tier, str) or not tier.strip():
            raise ValueError(f"empty tier for scenario {raw_id!r}")
        result[key] = tier.strip()
    if result.keys() != expected:
        missing = sorted(expected - result.keys(), key=int)
        extra = sorted(result.keys() - expected, key=int)
        raise ValueError(
            f"tier file must list exactly the {len(expected)} official scenarios;"
            f" missing {missing}, unexpected {extra}"
        )
    if not DATA_INCOMPLETE <= result.keys():
        raise ValueError("data-incomplete scenarios are not in the scenario set")
    return result


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _value(trial: Mapping[str, Any], metric: str) -> float:
    value = trial.get(metric)
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def build_table(
    tiers: Mapping[str, str],
    scores: Mapping[str, Sequence[Mapping[str, Any]]],
    metrics: Sequence[str],
    *,
    expected_trials: int = EXPECTED_TRIALS,
) -> dict[str, Any]:
    """Per-scenario, per-tier and overall means over every scenario in ``tiers``."""

    by_key: dict[str, Sequence[Mapping[str, Any]]] = {}
    for raw_id, trials in scores.items():
        key = scenario_key(raw_id)
        if key not in tiers:
            raise ValueError(f"score for scenario {raw_id!r} that has no tier")
        if key in by_key:
            raise ValueError(f"duplicate score entry for scenario {raw_id!r}")
        by_key[key] = trials

    rows = []
    for key in sorted(tiers, key=int):
        trials = list(by_key.get(key, ()))
        if len(trials) > expected_trials:
            raise ValueError(
                f"scenario {key} has {len(trials)} trials, expected {expected_trials}"
            )
        padded = trials + [{}] * (expected_trials - len(trials))
        rows.append(
            {
                "scenario": key,
                "tier": tiers[key],
                "data_incomplete": key in DATA_INCOMPLETE,
                "trials_scored": len(trials),
                "trials_missing": expected_trials - len(trials),
                "means": {m: _mean([_value(t, m) for t in padded]) for m in metrics},
            }
        )

    tier_rows = []
    for tier in sorted({row["tier"] for row in rows}):
        members = [row for row in rows if row["tier"] == tier]
        tier_rows.append(
            {
                "tier": tier,
                "scenarios": len(members),
                "data_incomplete": sorted(
                    (r["scenario"] for r in members if r["data_incomplete"]), key=int
                ),
                "means": {m: _mean([r["means"][m] for r in members]) for m in metrics},
            }
        )
    return {
        "metrics": list(metrics),
        "expected_trials": expected_trials,
        "scenarios": rows,
        "tiers": tier_rows,
        "overall": {
            "scenarios": len(rows),
            "means": {m: _mean([r["means"][m] for r in rows]) for m in metrics},
        },
        "notes": list(NOTES),
    }


def _metric_header(metric: str) -> str:
    return (
        f"{metric}（{PROPAGATION_LABEL}）"
        if "propagation" in metric.casefold()
        else metric
    )


def render_markdown(table: Mapping[str, Any]) -> str:
    metrics = table["metrics"]
    header = " | ".join(_metric_header(m) for m in metrics)
    rule = "|".join(["---"] * len(metrics))
    lines = [
        "## 按覆盖分档",
        "",
        f"| 档位 | 场景数 | 数据残缺 | {header} |",
        f"|---|---|---|{rule}|",
    ]
    for row in table["tiers"]:
        flagged = "、".join(f"S{s}" for s in row["data_incomplete"]) or "—"
        values = " | ".join(f"{row['means'][m]:.3f}" for m in metrics)
        lines.append(f"| {row['tier']} | {row['scenarios']} | {flagged} | {values} |")
    overall = table["overall"]
    values = " | ".join(f"{overall['means'][m]:.3f}" for m in metrics)
    lines.append(f"| 全部 | {overall['scenarios']} | S18、S38 | {values} |")
    lines += [
        "",
        "## 每题",
        "",
        f"| 场景 | 档位 | 有分 Run | {header} |",
        f"|---|---|---|{rule}|",
    ]
    for row in table["scenarios"]:
        label = f"S{row['scenario']}" + (
            f"（{DATA_INCOMPLETE_LABEL}）" if row["data_incomplete"] else ""
        )
        values = " | ".join(f"{row['means'][m]:.3f}" for m in metrics)
        scored = f"{row['trials_scored']}/{table['expected_trials']}"
        lines.append(f"| {label} | {row['tier']} | {scored} | {values} |")
    lines += ["", "## 说明", ""] + [f"- {note}" for note in table["notes"]]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tiers", required=True)
    parser.add_argument("--scores", required=True)
    parser.add_argument("--metric", action="append", required=True)
    parser.add_argument("--trials", type=int, default=EXPECTED_TRIALS)
    parser.add_argument("--json-out")
    parser.add_argument("--markdown-out")
    args = parser.parse_args(argv)
    tiers = load_tiers(json.loads(Path(args.tiers).read_text()))
    scores = json.loads(Path(args.scores).read_text())
    table = build_table(tiers, scores, args.metric, expected_trials=args.trials)
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(table, ensure_ascii=False, indent=2) + "\n"
        )
    markdown = render_markdown(table)
    if args.markdown_out:
        Path(args.markdown_out).write_text(markdown)
    else:
        print(markdown, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
