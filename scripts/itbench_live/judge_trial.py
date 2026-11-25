"""Judging process for one ITBench live trial (#564, A564-4).

Lite's judging process (``scripts.itbench_lite.judge_trial``) with the live
deterministic check (``live_match``) in place of Lite's ``entity_match`` on
Lite's answer schema. The only process of the harness that reads an answer:
``<ground truth root>/<scenario>/ground_truth.yaml`` from the pinned ITBench
library. Only numbers are written; the evaluator's work directory (staged
outputs, judge text derived from the answer) is removed once the numeric row
is read; the driver discards this process's output.

Only diagnosis metrics exist here: the official evaluator's root-cause,
propagation and localisation keys. Remediation metrics do not apply to a
read-only product and are never computed.

    python -m scripts.itbench_live.judge_trial --ground-truth <root> \\
        --scenario <id> --outputs <outputs root> --trial <n> \\
        --work-dir <dir> --out <scores.json> [--judge]
"""

from __future__ import annotations

import argparse
import json
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path

from scripts.itbench_lite import official_eval
from scripts.itbench_lite.judge_trial import _load_gt, numeric, official_row
from scripts.itbench_live import live_match

#: One evaluator call; a hung judge is a failed try the driver retries.
JUDGE_TIMEOUT_S = 1800.0


def deterministic(
    agent_output: Mapping[str, object], gt: Mapping[str, object]
) -> dict[str, float]:
    result = live_match.score(agent_output, gt)
    return {f"det_{k}": v for k, v in numeric(result).items()}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ground-truth", required=True, type=Path)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--outputs", required=True, type=Path)
    parser.add_argument("--trial", required=True, type=int)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--judge", action="store_true")
    args = parser.parse_args(argv)

    trial_dir = args.outputs / args.scenario / str(args.trial)
    agent_output = json.loads((trial_dir / "outputs" / "agent_output.json").read_text())
    present = official_eval.report_present(trial_dir)
    gt = _load_gt(args.ground_truth, args.scenario)
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
                    timeout=JUDGE_TIMEOUT_S,
                )
                official = official_row(scores_file, args.scenario, args.trial)
                labels = config.labels()
                if official_eval.missing_official_keys(official):
                    judge_error, official, labels = "OFFICIAL_KEYS_MISSING", None, None
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
