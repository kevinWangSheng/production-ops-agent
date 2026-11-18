"""The judging subprocess of the HolmesGPT track (#666 H-3, H-16).

The driver never reads an answer: this process does, through PR-A's
``judge_trial``, and writes only the ``JudgeResult`` fields to ``--out``. The
rationale (answer-derived) goes to ``--rationale-dir`` under the git-ignored
trial directory. The parent discards this process's stdout and stderr, and
gives it only the judge's variables (:func:`judge_env`).

Usage (by the driver)::

    python -m scripts.holmes_evals.judge_run --upstream <checkout> --qid <dir> \\
        --text-file <judge input> --rationale-dir <dir> --out <result.json>
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from scripts.itbench_lite.services import base_env

JUDGE_KEYS = ("ZAI_API_KEY", "GLM_API_KEY", "ZAI_BASE_URL")


def judge_env(environ: Mapping[str, str]) -> dict[str, str]:
    """Base paths, ``PYTHON_DOTENV_DISABLED=1`` and the judge's variables."""
    env = base_env(environ)
    env.update({k: environ[k] for k in JUDGE_KEYS if environ.get(k)})
    env["PYTHON_DOTENV_DISABLED"] = "1"
    return {str(k): str(v) for k, v in env.items()}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--qid", required=True)
    parser.add_argument("--text-file", type=Path, required=True)
    parser.add_argument("--rationale-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    from opspilot.evaluation.binary_judge import HTTPCompletionClient
    from scripts.holmes_evals.judge import judge_trial, zai_config
    from scripts.holmes_evals.questions import verify_upstream

    verify_upstream(args.upstream)
    config = zai_config(os.environ)
    output = args.text_file.read_text(encoding="utf-8")
    result = judge_trial(
        HTTPCompletionClient(),
        config,
        args.upstream,
        args.qid,
        output,
        args.rationale_dir,
    )
    partial = args.out.with_suffix(".partial")
    partial.write_text(json.dumps(dataclasses.asdict(result)) + "\n")
    partial.replace(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
