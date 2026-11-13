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

Only numbers are persisted. The match detail (which ground-truth entity a
prediction hit) is never written, and the evaluator's work directory (its
staged outputs and judge results, which carry answer-derived judge text) is
removed once the numeric row is read. Nothing answer-derived is printed; the
driver also discards this process's output. Exit status 0 on success, 3 when
the judge call failed; the deterministic score is written either way.

    python -m scripts.itbench_lite.judge_trial --ground-truth <root> \\
        --scenario-dir <name> --outputs <outputs root> --trial <n> \\
        --work-dir <dir> --out <scores.json> [--judge]
"""

from __future__ import annotations

import argparse
import json
import shutil
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
) -> dict[str, float]:
    """``det_*`` numbers only; the per-prediction match list is dropped."""
    result = entity_match.score(agent_output, gt)
    return {f"det_{k}": v for k, v in numeric(result).items()}


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
    parser.add_argument("--judge", action="store_true")
    args = parser.parse_args(argv)

    trial_dir = args.outputs / args.scenario_dir / str(args.trial)
    agent_output = json.loads((trial_dir / "outputs" / "agent_output.json").read_text())
    present = official_eval.report_present(trial_dir)
    gt = _load_gt(args.ground_truth, args.scenario_dir)
    det = deterministic(agent_output, gt if isinstance(gt, Mapping) else {})
    del gt

    official: dict[str, float] | None = None
    labels: dict[str, object] | None = None
    judge_error: str | None = None
    if args.judge:
        if present:
            try:
                config = official_eval.judge_config()
                scores_file = official_eval.run(
                    official_eval.evaluator_dir(),
                    args.ground_truth,
                    args.outputs,
                    args.work_dir,
                    config,
                )
                official = official_row(scores_file, args.scenario_dir, args.trial)
                labels = config.labels()
            except Exception as error:  # noqa: BLE001 - the driver retries
                judge_error = type(error).__name__
            finally:
                shutil.rmtree(args.work_dir, ignore_errors=True)
        else:
            official = {metric: 0.0 for metric in official_eval.METRIC_KEYS}
        if official is not None:
            official["root_cause_entity_pass"] = (
                1.0 if official.get("root_cause_entity_f1") == 1.0 else 0.0
            )
    out = {
        "report_present": present,
        "det": det,
        "official": official,
        "judged": args.judge and judge_error is None,
        "judge_error": judge_error,
        "labels": labels,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    partial = args.out.with_suffix(".partial")
    partial.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n")
    partial.replace(args.out)
    return 3 if judge_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
