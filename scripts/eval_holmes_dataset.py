"""HolmesGPT dataset runner entry points (F1 closure S7b).

The acceptance contract (``tests/acceptance/test_s7b_holmes_dataset.py``)
names this module; the implementation lives in
``scripts.holmes_baseline.dataset`` (plan, rows, ``run_attempt`` for the
paired schedule, ``summarize`` and the judge inputs).
"""

from __future__ import annotations

from scripts.holmes_baseline.dataset import (
    holmes_plan,
    judge_inputs,
    result_row,
    run_attempt,
    select_scenarios,
    summarize,
)

__all__ = [
    "holmes_plan",
    "judge_inputs",
    "result_row",
    "run_attempt",
    "select_scenarios",
    "summarize",
]
