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


def test_a_follow_up_after_a_forced_final_handoff_resumes_the_same_run():
    h = Harness(budget_limit=8, model_requests=8)
    first = h.runner([*_tool_rounds(1), _bad, _bad]).resume(h.incident)
    assert first.status == "handed_off", first
    assert first.reason == "REPORT_INVALID"
    final_row = next(s for s in h.rows()["steps"] if s["logical_key"] == "ctx0:round-3")
    assert final_row["response"]["context"]["final"] is True
    assert h.rows()["run"]["state"] == "waiting_human"

    generation = h.store.control(
        h.incident, 0, "follow_up", "operator", {"question": "check payments too"}
    )
    assert generation == 1
    sent: list = []

    def record_then_report(call):
        sent.append(list(call.messages))
        return report_from_transcript(call)

    outcome = h.runner([record_then_report]).resume(h.incident)

    assert outcome.status == "published", outcome
    assert len(sent) == 1
    assert '"question":"check payments too"' in sent[0][-1]["content"]
    assert h.store.run_usage(h.run)["model_requests_used"] == 4
    assert h.rows()["run"]["state"] == "completed"


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
