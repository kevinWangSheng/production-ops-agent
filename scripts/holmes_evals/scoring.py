"""Scores for the HolmesGPT public eval track (#666 H-17, H-18).

* Main score = passes over (questions × 3), trials 1-3 only. A trial with no
  report, with the judge out of retries, or with a broken environment scores
  0 and stays in the denominator; so does a trial that is missing.
* A question passing 1 or 2 of its 3 main trials swings; its reruns
  (trials 4-6) are reported on their own and never enter the main score.
* Extra rows: the 10 multi-cluster questions, the questions flagged as
  limited by the authorised time box (H-19, decided before the run), and the
  score without the questions that had a broken-environment trial
  ("question or harness broken").
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from scripts.holmes_evals.questions import MULTI_CLUSTER

Category = Literal["ok", "no_report", "judge_exhausted", "env_broken"]
CATEGORIES: tuple[str, ...] = ("ok", "no_report", "judge_exhausted", "env_broken")
MAIN_TRIALS = (1, 2, 3)
RERUN_TRIALS = (4, 5, 6)
TRACK = "HolmesGPT 公开评测集（竞品自评集）"


@dataclass(frozen=True)
class Trial:
    qid: str
    index: int  # 1..3 main, 4..6 rerun
    passed: bool
    category: Category
    time_box_limited: bool = False


def _counts(trial: Trial) -> bool:
    """Only a judged trial can pass; every other category scores 0."""
    return trial.category == "ok" and trial.passed is True


def _rate(passes: int, denominator: int) -> float | None:
    return passes / denominator if denominator else None


def _group(qids: Iterable[str], main: dict[str, dict[int, Trial]]) -> dict[str, Any]:
    members = sorted(qids)
    passes = sum(1 for q in members for t in main[q].values() if _counts(t))
    denominator = len(members) * len(MAIN_TRIALS)
    return {
        "questions": members,
        "passes": passes,
        "denominator": denominator,
        "rate": _rate(passes, denominator),
    }


def score(trials: Sequence[Trial], questions: Sequence[str]) -> dict[str, Any]:
    if len(set(questions)) != len(questions):
        raise ValueError("QUESTION_DUPLICATE")
    known = set(questions)
    main: dict[str, dict[int, Trial]] = {q: {} for q in questions}
    rerun: dict[str, dict[int, Trial]] = {q: {} for q in questions}
    for trial in trials:
        if trial.qid not in known:
            raise ValueError(f"TRIAL_QUESTION_UNKNOWN:{trial.qid}")
        if trial.category not in CATEGORIES:
            raise ValueError(f"TRIAL_CATEGORY_INVALID:{trial.category}")
        if trial.index in MAIN_TRIALS:
            bucket = main[trial.qid]
        elif trial.index in RERUN_TRIALS:
            bucket = rerun[trial.qid]
        else:
            raise ValueError(f"TRIAL_INDEX_INVALID:{trial.qid}:{trial.index}")
        if trial.index in bucket:
            raise ValueError(f"TRIAL_DUPLICATE:{trial.qid}:{trial.index}")
        bucket[trial.index] = trial

    per_question: dict[str, Any] = {}
    swing: list[str] = []
    for q in questions:
        passes = sum(1 for t in main[q].values() if _counts(t))
        swings = passes in (1, 2)
        if swings:
            swing.append(q)
        per_question[q] = {
            "passes": passes,
            "denominator": len(MAIN_TRIALS),
            "rate": _rate(passes, len(MAIN_TRIALS)),
            "trials": {
                str(i): (
                    {"passed": _counts(t), "category": t.category}
                    if (t := main[q].get(i)) is not None
                    else None
                )
                for i in MAIN_TRIALS
            },
            "swing": swings,
            "rerun_passes": sum(1 for t in rerun[q].values() if _counts(t)),
            "rerun_trials": len(rerun[q]),
        }

    zero: dict[str, list[str]] = {c: [] for c in CATEGORIES if c != "ok"}
    missing: list[str] = []
    for q in questions:
        for i in MAIN_TRIALS:
            t = main[q].get(i)
            if t is None:
                missing.append(f"{q}#{i}")
            elif t.category != "ok":
                zero[t.category].append(f"{q}#{i}")

    rerun_rows = {
        q: {
            "passes": sum(1 for t in rerun[q].values() if _counts(t)),
            "trials": len(rerun[q]),
        }
        for q in questions
        if rerun[q]
    }
    rerun_passes = sum(r["passes"] for r in rerun_rows.values())
    rerun_n = sum(r["trials"] for r in rerun_rows.values())

    broken = sorted(
        q
        for q in questions
        if any(t.category == "env_broken" for t in main[q].values())
    )
    limited = [
        q
        for q in questions
        if any(t.time_box_limited for t in (*main[q].values(), *rerun[q].values()))
    ]
    excluding = _group((q for q in questions if q not in set(broken)), main)
    excluding["excluded"] = broken

    return {
        "track": TRACK,
        "main": {k: v for k, v in _group(questions, main).items() if k != "questions"},
        "per_question": per_question,
        "swing_questions": swing,
        "rerun": {
            "questions": rerun_rows,
            "passes": rerun_passes,
            "trials": rerun_n,
            "rate": _rate(rerun_passes, rerun_n),
            "in_main_score": False,
        },
        "zero_scored": zero,
        "missing_trials": missing,
        "multi_cluster": _group((q for q in questions if q in MULTI_CLUSTER), main),
        "time_box_limited": _group(limited, main),
        "excluding_env_broken": excluding,
    }
