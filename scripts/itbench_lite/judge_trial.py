"""Judging process for one ITBench-Lite trial (#604, L-D14, L9).

The only process of the driver that may read an answer: the driver starts it
with the ground-truth root, the scenario directory name and the attempt's
outputs root, and reads back only the numeric scores this writes. Here:

* the deterministic score: ``<ground truth root>/<scenario dir>/ground_truth.yaml``
  (or ``.yml``) is loaded with ``yaml.safe_load`` and passed to
  ``entity_match.score``; keys are prefixed ``det_`` (L-D15);
* the official score (``--judge``): ``official_eval.run`` in this attempt's own
  work directory; a reportless trial is never sent and scores 0 (L5);
  ``root_cause_entity_pass`` = ``root_cause_entity_f1 == 1`` (L-D15).

The match detail (which ground-truth entity a prediction hit) goes to
``--detail`` in the ignored directory and never into the scores file or a
summary. Exit status 0 on success, 3 when the judge call failed.

    python -m scripts.itbench_lite.judge_trial --ground-truth <root> \\
        --scenario-dir <name> --outputs <outputs root> --trial <n> \\
        --work-dir <dir> --out <scores.json> --detail <detail.json> [--judge]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from scripts.itbench_lite import entity_match, official_eval
from scripts.itbench_lite.coverage import scenario_key

GT_NAMES = ("ground_truth.yaml", "ground_truth.yml")


def numeric(values: Mapping[str, Any]) -> dict[str, float]:
    """Numbers only; booleans as 1.0/0.0, everything else dropped."""
    out: dict[str, float] = {}
    for key, value in values.items():
        if isinstance(value, bool):
            out[key] = 1.0 if value else 0.0
        elif isinstance(value, (int, float)):
            out[key] = float(value)
    return out


def deterministic(
    agent_output: Mapping[str, Any], gt: Mapping[str, Any]
) -> tuple[dict[str, float], dict[str, Any]]:
    result = entity_match.score(agent_output, gt)
    scores = {f"det_{k}": v for k, v in numeric(result).items()}
    return scores, result


def official_row(scores_file: Path, scenario_dir: str, trial: int) -> dict[str, float]:
    table = json.loads(scores_file.read_text())
    key = scenario_key(scenario_dir)
    rows = table.get(key) or []
    # trial_scores keeps the outputs-root order; this root holds one trial.
    return numeric(rows[0]) if rows else {}


def _load_gt(root: Path, scenario_dir: str) -> Any:
    import yaml  # type: ignore[import-untyped]

    for name in GT_NAMES:
        path = root / scenario_dir / name
        if path.is_file():
            with path.open(encoding="utf-8") as handle:
                return yaml.safe_load(handle)
    raise FileNotFoundError(f"no ground truth for {scenario_dir}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ground-truth", required=True, type=Path)
    parser.add_argument("--scenario-dir", required=True)
    parser.add_argument("--outputs", required=True, type=Path)
    parser.add_argument("--trial", required=True, type=int)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--detail", required=True, type=Path)
    parser.add_argument("--judge", action="store_true")
    args = parser.parse_args(argv)

    trial_dir = args.outputs / args.scenario_dir / str(args.trial)
    agent_output = json.loads((trial_dir / "outputs" / "agent_output.json").read_text())
    present = official_eval.report_present(trial_dir)
    gt = _load_gt(args.ground_truth, args.scenario_dir)
    det, detail = deterministic(agent_output, gt if isinstance(gt, Mapping) else {})
    args.detail.parent.mkdir(parents=True, exist_ok=True)
    args.detail.write_text(json.dumps(detail, ensure_ascii=False, indent=1) + "\n")

    official: dict[str, float] | None = None
    labels: dict[str, object] | None = None
    if args.judge:
        if present:
            config = official_eval.judge_config()
            try:
                scores_file = official_eval.run(
                    official_eval.evaluator_dir(),
                    args.ground_truth,
                    args.outputs,
                    args.work_dir,
                    config,
                )
            except Exception as error:  # noqa: BLE001 - the driver retries
                print(f"judge failed: {type(error).__name__}", file=sys.stderr)
                return 3
            official = official_row(scores_file, args.scenario_dir, args.trial)
            labels = config.labels()
        else:
            official = {metric: 0.0 for metric in official_eval.METRIC_KEYS}
        official["root_cause_entity_pass"] = (
            1.0 if official.get("root_cause_entity_f1") == 1.0 else 0.0
        )
    out = {
        "report_present": present,
        "det": det,
        "official": official,
        "judged": args.judge,
        "labels": labels,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
