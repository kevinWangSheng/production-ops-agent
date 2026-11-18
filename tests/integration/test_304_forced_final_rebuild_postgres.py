"""#304 acceptance on real PostgreSQL rows: a forced final round (L1a/B3)
survives a restart and a human follow_up.

Drives the public ``InvestigationRunner`` / ``DurableStore.control`` path with
scripted models (no model HTTP). Opt in with ``M1_DURABLE_POSTGRES=1``; point
``OPSPILOT_LAB_DSN`` at a throwaway server to leave the 55431 lab alone.
"""

from __future__ import annotations

import os

import pytest

from opspilot.investigation.loop import InvestigationLoop
from opspilot.investigation.store import DurableStepStore
from opspilot.worker import Worker
from tests.integration.test_m1_loop_resume_postgres import (
    VERSIONS,
    Crash,
    Harness,
    _tool_rounds,
)
from tests.m1_investigation_support import (
    ScriptedModel,
    reply,
    report_from_transcript,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

FOLLOW_UP = "also compare against the payments service"


def _bad(call):
    return reply(content="not a report")


def _forced_final_handoff(h):
    """Round 1 tool, round 2 unacceptable (no tools), round 3 the forced final
    round, unacceptable again -> REPORT_INVALID hand-off."""
    outcome = h.runner([*_tool_rounds(1), _bad, _bad]).resume(h.incident)
    assert outcome.status == "handed_off" and outcome.reason == "REPORT_INVALID", (
        outcome
    )
    rows = h.rows()
    assert rows["run"]["state"] == "waiting_human"
    final_row = next(s for s in rows["steps"] if s["logical_key"] == "ctx0:round-3")
    assert final_row["response"]["context"]["final"] is True
    return outcome


def test_restart_after_an_accepted_forced_final_report_publishes_without_a_model_call():
    h = Harness(budget_limit=4, model_requests=4)
    worker = Worker.create(h.store, dict(VERSIONS))
    session = worker.resume(h.incident, lease_seconds=30, renew_seconds=30)
    executor = h.executor_factory(session.lease, None)
    from opspilot.investigation.context import InvestigationInput, rebuild_transcript

    transcript = rebuild_transcript(
        h.rows(),
        run_id=str(h.run),
        authorized_targets=executor.scope.target_ids,
        input=InvestigationInput.from_json(h.rows()["run"]["input"]),
    )

    class DieOnConclusion(DurableStepStore):
        def commit_step(self, logical_key, response):
            if logical_key.startswith("conclusion:"):
                raise Crash("killed before the conclusion row")
            return super().commit_step(logical_key, response)

    loop = InvestigationLoop(
        model=ScriptedModel([*_tool_rounds(1), _bad, report_from_transcript]),
        executor=executor,
        store=DieOnConclusion(h.store, session.lease),
        clock=h.clock,
    )
    with pytest.raises(Crash):
        loop.resume(transcript)
    h.store.abandon(session.lease)  # the process died; the lease is released
    rows = h.rows()
    keys = [s["logical_key"] for s in rows["steps"]]
    assert "ctx0:round-3" in keys and not any(k.startswith("conclusion:") for k in keys)
    assert (
        next(s for s in rows["steps"] if s["logical_key"] == "ctx0:round-3")[
            "response"
        ]["context"]["final"]
        is True
    )

    runner = h.runner([])  # any model call would raise
    outcome = runner.resume(h.incident)
    assert outcome.status == "published", outcome
    assert runner.model.calls == []
    assert h.store.run_usage(h.run)["model_requests_used"] == 3
    assert h.rows()["conclusion"] is not None
