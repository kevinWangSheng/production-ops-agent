"""#304 on real PostgreSQL rows: a Run whose forced final round (L1a/B3)
carried retry feedback rebuilds after a human follow_up and after a crash
before its conclusion row, instead of blocking on ``INCOMPATIBLE_STATE``.

Implementer's self-test; deterministic doubles only (no model HTTP).
"""

from __future__ import annotations

import os

import pytest

from opspilot.investigation.store import DurableStepStore
from tests.integration.test_m1_loop_resume_postgres import (
    Crash,
    Harness,
    _tool_rounds,
)
from tests.m1_investigation_support import reply, report_from_transcript

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required",
)


def _bad(call):
    return reply(content="not a report")


def test_a_valid_forced_final_report_is_published_after_a_crash_before_its_conclusion(
    monkeypatch,
):
    h = Harness(budget_limit=8, model_requests=8)
    original = DurableStepStore.commit_step
    armed = {"on": True}

    def die_on_conclusion(self, logical_key, response):
        if armed["on"] and logical_key.startswith("conclusion:"):
            armed["on"] = False
            raise Crash("killed before the conclusion row")
        return original(self, logical_key, response)

    monkeypatch.setattr(DurableStepStore, "commit_step", die_on_conclusion)
    with pytest.raises(Crash):
        h.runner([*_tool_rounds(1), _bad, report_from_transcript]).resume(h.incident)
    assert h.live_keys() == ["ctx0:round-1", "ctx0:round-2", "ctx0:round-3"]
    assert h.rows()["conclusion"] is None
    h.wait_lease()

    outcome = h.runner([]).resume(h.incident)  # a model call here would raise

    assert outcome.status == "published", outcome
    assert h.store.run_usage(h.run)["model_requests_used"] == 3
    assert h.rows()["run"]["state"] == "completed"
