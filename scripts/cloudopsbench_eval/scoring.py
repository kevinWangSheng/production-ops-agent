"""Score a line with the benchmark's own ``evaluation.py`` (#871 A5, U6).

Both lines -- OpsPilot's converted answers and the bundled agent's own
traces -- are scored by the same, unmodified scorer, run as its own process
under the benchmark venv's interpreter with ``-I``. The scorer is driven by
its config file next to it (``configs/model_configs.yaml``), so each scoring
pass copies ``evaluation.py`` and ``evaluation_utils/`` from the checkout into
a scratch directory and writes the config there: one config per (system,
category), restricted to the scored cases through the scorer's own
``case_split`` file. The checkout itself is never written.

Only the per-case metrics are read back from the scorer's detail file; its
ground-truth fields stay in the scratch directory (A6).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from scripts.cloudopsbench_eval.views import CaseRef
from scripts.itbench_lite import services

METRICS = (
    "CA",
    "FA",
    "JRA",
    "MC",
    "EOC",
    "ECR",
    "EE",
    "steps",
    "RAR",
    "invalid_actions",
)
SCORER_TIMEOUT_S = 600


def answer_path(answers_root: Path, line: str, ref: CaseRef) -> Path:
    """Where the scorer looks: ``<root>/<system>/<line>/<category>/<n>/<n>.json``."""
    return (
        answers_root
        / ref.system_dir
        / line
        / ref.category
        / ref.number
        / f"{ref.number}.json"
    )


def scorer_config(
    *,
    line: str,
    ref: CaseRef,
    checkout: Path,
    answers_root: Path,
    split_file: Path,
) -> str:
    """The scorer's YAML; quoted scalars only (its fallback parser reads
    two-level ``key: value`` lines)."""
    rows = {
        "model": {"model": line, "provider": "openai_compatible"},
        "diagnosis": {
            "system": ref.system_dir,
            "fault_category": ref.category,
            "case_name": "",
            "case_split": "test",
            "case_split_file": str(split_file),
            "dataset_root": str(checkout),
            "save_root": str(answers_root),
        },
    }
    lines = []
    for section, values in rows.items():
        lines.append(f"{section}:")
        lines += [f"  {k}: {json.dumps(v)}" for k, v in values.items()]
    return "\n".join(lines) + "\n"


def _groups(case_ids: Sequence[str]) -> dict[tuple[str, str], list[CaseRef]]:
    groups: dict[tuple[str, str], list[CaseRef]] = {}
    for case_id in case_ids:
        ref = CaseRef.parse(case_id)
        groups.setdefault((ref.system_dir, ref.category), []).append(ref)
    return groups


def score(
    *,
    line: str,
    case_ids: Sequence[str],
    checkout: Path,
    cob_python: Path,
    answers_root: Path,
    work_dir: Path,
    run: Any = subprocess.run,
) -> dict[str, dict[str, Any]]:
    """``case id -> {"metrics": {...}, "error": code | None}`` for every
    case asked for. A case the scorer did not report is an error row with
    zero metrics (A7)."""
    scorer = work_dir / "scorer"
    if scorer.exists():
        shutil.rmtree(scorer)
    source = checkout / "agents" / "cloudops_agent"
    scorer.mkdir(parents=True)
    shutil.copyfile(source / "evaluation.py", scorer / "evaluation.py")
    shutil.copytree(
        source / "evaluation_utils",
        scorer / "evaluation_utils",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (scorer / "configs").mkdir()
    results: dict[str, dict[str, Any]] = {}
    for (system_dir, category), refs in sorted(_groups(case_ids).items()):
        split = work_dir / f"split-{system_dir}-{category}.json"
        split.write_text(
            json.dumps(
                {"splits": [{"system": system_dir, "test": [r.id for r in refs]}]}
            )
        )
        (scorer / "configs" / "model_configs.yaml").write_text(
            scorer_config(
                line=line,
                ref=refs[0],
                checkout=checkout,
                answers_root=answers_root,
                split_file=split,
            )
        )
        done = run(
            [str(cob_python), "-I", str(scorer / "evaluation.py")],
            cwd=scorer,
            env=services.base_env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=SCORER_TIMEOUT_S,
            check=False,
        )
        details_path = (
            answers_root / system_dir / line / f"evaluation_v2_{category}_details.json"
        )
        details: list[Mapping[str, Any]] = []
        if done.returncode == 0 and details_path.is_file():
            details = json.loads(details_path.read_text(encoding="utf-8"))
        by_name = {str(d.get("case_name")): d for d in details}
        for ref in refs:
            detail = by_name.get(ref.number)
            if detail is None:
                results[ref.id] = {
                    "metrics": dict.fromkeys(METRICS, 0.0),
                    "error": "SCORER_NO_RESULT"
                    if done.returncode == 0
                    else "SCORER_FAILED",
                }
                continue
            metrics = detail.get("metrics") or {}
            results[ref.id] = {
                "metrics": {k: float(metrics.get(k, 0.0)) for k in METRICS},
                "error": detail.get("error"),
                "final_answer_source": detail.get("final_answer_source"),
            }
    return results
