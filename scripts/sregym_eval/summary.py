"""Per-trial summary block and scores for the SREGym Lite track (#667).

The block is evaluation-side evidence (``docs/evidence/sregym-lite-<round>/``):
it may carry the problem id, but never the question text, the diagnosis
text, the judge's reasoning or anything the results whitelist drops (S-8).
Scores reuse the #666 rules (``scripts.holmes_evals.scoring``): passes over
17 × 3 main trials, swings rerun on their own, unjudged trials score 0 and
stay in the denominator.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Literal

from scripts.holmes_evals import scoring
from scripts.sregym_eval.question import (
    QUESTION_TEMPLATE_SHA256,
    idempotency_key,
)
from scripts.sregym_eval.results import AttemptOutcome
from scripts.sregym_eval.upstream import LITE_PROBLEMS, UPSTREAM_COMMIT, problem_seq

SUMMARY_SCHEMA = "sregym-eval-trial-1"
TRACK = "SREGym Lite 诊断段（中立第三方现场题，自测）"
ClusterPath = Literal["filter_proxy_readonly", "direct_readonly"]
CLUSTER_PATHS: tuple[str, ...] = ("filter_proxy_readonly", "direct_readonly")
ALWAYS_DISCLOSED: tuple[str, ...] = (
    "JUDGE_ZAI_GLM",
    "DIAGNOSIS_STAGE_ONLY",
    "INTERNET_OPEN",
    "NON_CONTAINER_AGENT",
    "QUESTION_TEMPLATE_OURS",
    "OBSERVABILITY_DIRECT_UNFILTERED",
    "NO_SECRET_READ",
    "SELF_RUN_NOT_LEADERBOARD",
)
_VISIBILITY = {
    "filter_proxy_readonly": "VISIBILITY_SAME_FILTER_PROXY",
    "direct_readonly": "VISIBILITY_WIDER_THAN_OTHER_AGENTS",
}
_DROPPED_SCORE_GROUPS = ("multi_cluster", "time_box_limited")


@dataclass(frozen=True)
class Submission:
    submitted: bool
    empty: bool
    status_code: int | None
    error_code: str | None
    text_sha256: str | None
    chars: int


def _check_path(cluster_path: str) -> None:
    if cluster_path not in CLUSTER_PATHS:
        raise ValueError(f"CLUSTER_PATH_INVALID:{cluster_path}")


def disclosures(cluster_path: ClusterPath) -> list[str]:
    _check_path(cluster_path)
    return [*ALWAYS_DISCLOSED, _VISIBILITY[cluster_path]]


def category_of(
    outcome: AttemptOutcome | None, submission: Submission
) -> tuple[scoring.Category, bool]:
    """(category, passed): no result or a failed deploy is ``env_broken``;
    nothing or an empty string submitted is ``no_report``; an unscored
    judgment is ``judge_exhausted``; otherwise ``ok``."""
    if outcome is None or outcome.deploy_failed:
        return "env_broken", False
    if not submission.submitted or submission.empty:
        return "no_report", False
    if outcome.success is None:
        return "judge_exhausted", False
    return "ok", outcome.success is True


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def trial_block(
    *,
    round: str,  # noqa: A002
    problem_id: str,
    trial: int,
    question: str,
    cluster_path: ClusterPath,
    submission: Submission,
    conversion_reason: str | None,
    outcome: AttemptOutcome | None,
) -> dict[str, Any]:
    _check_path(cluster_path)
    seq = problem_seq(problem_id)
    category, passed = category_of(outcome, submission)
    return {
        "schema": SUMMARY_SCHEMA,
        "track": TRACK,
        "upstream_commit": UPSTREAM_COMMIT,
        "round": round,
        "problem_id": problem_id,
        "problem_seq": seq,
        "trial": trial,
        "question_template_sha256": QUESTION_TEMPLATE_SHA256,
        "question_sha256": _sha256(question),
        "idempotency_key": idempotency_key(round, seq, trial),
        "cluster_path": cluster_path,
        "submission": asdict(submission),
        "conversion_reason": conversion_reason,
        "result": None if outcome is None else asdict(outcome),
        "category": category,
        "passed": passed,
        "disclosures": disclosures(cluster_path),
    }


def trial_record(block: Mapping[str, Any]) -> scoring.Trial:
    if block.get("schema") != SUMMARY_SCHEMA:
        raise ValueError("SUMMARY_SCHEMA_MISMATCH")
    category = block.get("category")
    if category not in scoring.CATEGORIES:
        raise ValueError(f"TRIAL_CATEGORY_INVALID:{category}")
    return scoring.Trial(
        qid=str(block["problem_id"]),
        index=int(block["trial"]),
        passed=block.get("passed") is True,
        category=category,
    )


def scores(trials: list[scoring.Trial]) -> dict[str, Any]:
    result = scoring.score(trials, LITE_PROBLEMS)
    for key in _DROPPED_SCORE_GROUPS:
        result.pop(key, None)
    result["track"] = TRACK
    return result
