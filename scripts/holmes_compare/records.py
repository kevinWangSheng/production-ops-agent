"""Per-trial records of both agents in one shape (#668).

OpsPilot's are read from its #666 summaries (schema ``holmes-eval-trial-1``,
category rule of ``scripts.holmes_evals.driver.trial_record``); Holmes' from
this package's summaries (schema ``holmes-compare-trial-1``). A row keeps
only: question, trial, passed, category, judge error code, seconds, tool-call
count, whether the data and question were time-shifted. No answer, judge
rationale, judge input or credential can reach a row: those fields are never
copied.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from scripts.holmes_evals.scoring import CATEGORIES, Category, Trial

Agent = Literal["opspilot", "holmes"]
COMPARE_SCHEMA = "holmes-compare-trial-1"
OPSPILOT_BLOCK = "holmes_eval"
HOLMES_BLOCK = "holmes_compare"


@dataclass(frozen=True)
class TrialRow:
    agent: Agent
    qid: str
    trial: int
    passed: bool
    category: Category
    judge_error_code: str | None
    seconds: float | None
    tool_calls: int | None
    shift_applied: bool

    def as_scoring(self) -> Trial:
        return Trial(
            qid=self.qid, index=self.trial, passed=self.passed, category=self.category
        )


def _num(value: Any) -> float | None:
    return (
        float(value)
        if isinstance(value, int | float) and not isinstance(value, bool)
        else None
    )


def _map(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _code(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def opspilot_row(summary: Mapping[str, Any]) -> TrialRow:
    from scripts.holmes_evals.driver import trial_record

    trial = trial_record(summary)
    block = _map(summary.get(OPSPILOT_BLOCK))
    judge = _map(block.get("judge"))
    run = _map(summary.get("run"))
    requests = run.get("tool_requests")
    wait = _map(block.get("run_wait"))
    shift = _map(block.get("shift"))
    return TrialRow(
        agent="opspilot",
        qid=trial.qid,
        trial=trial.index,
        passed=trial.passed,
        category=trial.category,
        judge_error_code=_code(judge.get("error_code")),
        seconds=_num(wait.get("seconds")),
        tool_calls=len(requests) if isinstance(requests, list) else None,
        shift_applied=bool(shift.get("applied")),
    )


def holmes_row(summary: Mapping[str, Any]) -> TrialRow:
    if summary.get("schema") != COMPARE_SCHEMA:
        raise ValueError("HOLMES_SUMMARY_SCHEMA")
    block = _map(summary.get(HOLMES_BLOCK))
    judge = _map(block.get("judge"))
    shift = _map(block.get("shift"))
    category: Category
    if summary.get("status") == "missing":
        category = "env_broken"
    else:
        raw = block.get("category")
        if raw not in CATEGORIES:
            raise ValueError("HOLMES_SUMMARY_CATEGORY")
        category = raw
    if category == "ok" and judge.get("choice") not in ("A", "B"):
        category = "judge_exhausted"
    calls = block.get("tool_calls")
    return TrialRow(
        agent="holmes",
        qid=str(block["qid"]),
        trial=int(block["trial"]),
        passed=category == "ok" and judge.get("choice") == "A",
        category=category,
        judge_error_code=_code(judge.get("error_code")),
        seconds=_num(block.get("seconds")),
        tool_calls=calls
        if isinstance(calls, int) and not isinstance(calls, bool)
        else None,
        shift_applied=bool(shift.get("applied")),
    )


def load_rows(evidence_dir: Path, agent: Agent) -> list[TrialRow]:
    reader = opspilot_row if agent == "opspilot" else holmes_row
    marker = OPSPILOT_BLOCK if agent == "opspilot" else HOLMES_BLOCK
    rows = []
    for path in sorted(evidence_dir.glob("*/trial-*/summary.json")):
        summary = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(summary, Mapping) and isinstance(summary.get(marker), Mapping):
            rows.append(reader(summary))
    return rows


# -- Holmes trial classification ---------------------------------------------------------


def classify_holmes(
    harness: Mapping[str, Any] | None,
    judge: Mapping[str, Any] | None,
    *,
    timed_out: bool,
) -> tuple[Category, str | None]:
    """Category and code of one Holmes attempt, from the plugin's
    ``harness.json`` and the judge result (None when not judged).

    ``env_broken`` (setup, shift or harness failed before Holmes ran) is what
    the runner retries, as OpsPilot's driver retries infrastructure failures.
    Holmes ran but produced no judge text: ``no_report``. Text but no verdict:
    ``judge_exhausted``. Only a verdict is ``ok``.
    """
    if harness is None:
        return "env_broken", "PYTEST_TIMEOUT" if timed_out else "HARNESS_NO_STATE"
    if harness.get("selected") != 1:
        return "env_broken", "HARNESS_SELECT_FAILED"
    if harness.get("setup_failed"):
        return "env_broken", "SETUP_FAILED"
    shift = harness.get("shift")
    if isinstance(shift, Mapping) and shift.get("status") != "ok":
        return "env_broken", _code(shift.get("code")) or "SHIFT_FAILED"
    errors = _map(harness.get("errors"))
    if not harness.get("captured"):
        if not isinstance(shift, Mapping):
            return (
                "env_broken",
                "PYTEST_TIMEOUT" if timed_out else "HARNESS_SETUP_ERROR",
            )
        if timed_out:
            return "no_report", "HOLMES_TIMEOUT"
        return "no_report", "HOLMES_NO_ANSWER"
    if judge is None:
        return "judge_exhausted", "JUDGE_NOT_RUN"
    if judge.get("choice") in ("A", "B"):
        return "ok", None
    return "judge_exhausted", _code(judge.get("error_code")) or (
        "JUDGE_NO_CHOICE" if not errors.get("call") else "UPSTREAM_JUDGE_ERROR"
    )
