"""#520: the cross-segment void registry only links the schedule-check rows
of the scored systems and this dataset's scenarios. Synthetic ids only."""

from __future__ import annotations

from typing import Any

import pytest

from scripts.eval_compare_scoring import BinaryJudgeError, registry_voided_attempts
from scripts.eval_dev_round import VOIDS_SCHEMA

SCORED = [{"attempt_id": "r1", "system": "opspilot", "scenario_id": "s1"}]
REGISTRY = {
    "schema": VOIDS_SCHEMA,
    "voided_attempts": [
        {
            "system": "opspilot",
            "scenario_id": "s1",
            "voided_attempt_id": "v1",
            "rerun_attempt_id": "r1",
            "basis": "synthetic",
        }
    ],
}


def _check(**row: Any) -> dict[str, Any]:
    return {"voided_attempts": [row]}


@pytest.mark.parametrize(
    "row",
    [
        # another dataset's scenario voiding the same id
        {
            "system": "opspilot",
            "scenario_id": "other",
            "voided_attempt_id": "v1",
            "rerun_attempt_id": "x1",
        },
        # a system not scored here naming the same rerun
        {
            "system": "holmes",
            "scenario_id": "s1",
            "voided_attempt_id": "h1",
            "rerun_attempt_id": "r1",
        },
        # another dataset's row extending the chain past the scored rerun
        {
            "system": "opspilot",
            "scenario_id": "other",
            "voided_attempt_id": "r1",
            "rerun_attempt_id": "x2",
        },
    ],
)
def test_rows_outside_scored_set_are_ignored(row: dict[str, Any]) -> None:
    assert registry_voided_attempts(REGISTRY, SCORED, [_check(**row)], {"s1"}) == ["v1"]


def test_in_scope_duplicate_still_fails_closed() -> None:
    row = {
        "system": "opspilot",
        "scenario_id": "s1",
        "voided_attempt_id": "v1",
        "rerun_attempt_id": "x1",
    }
    with pytest.raises(BinaryJudgeError, match="VOID_DUPLICATE:v1"):
        registry_voided_attempts(REGISTRY, SCORED, [_check(**row)], {"s1"})
