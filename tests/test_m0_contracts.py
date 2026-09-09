from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from scripts.m0.contracts import BudgetError, RunContext, amount, remaining_seconds


def test_deadline_is_absolute_and_caps_each_request():
    now = datetime.now(timezone.utc)
    run = RunContext(uuid4(), uuid4(), "deepseek", now + timedelta(seconds=3))
    assert remaining_seconds(run, now, 10) == 3
    with pytest.raises(BudgetError, match="DEADLINE_EXCEEDED"):
        remaining_seconds(run, now + timedelta(seconds=3), 10)


@pytest.mark.parametrize("value", [True, -1, 1.2, float("nan"), "1"])
def test_money_rejects_ambiguous_inputs(value):
    with pytest.raises(BudgetError, match="INVALID_INPUT"):
        amount(value)


def test_identity_and_naive_deadline_rejected():
    with pytest.raises(BudgetError, match="INVALID_INPUT"):
        RunContext(uuid4(), uuid4(), "deepseek", datetime(2026, 1, 1))
