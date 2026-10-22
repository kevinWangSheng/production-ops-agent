"""#304 acceptance on real PostgreSQL rows: a forced final round (L1a/B3)
survives a restart and a human follow_up.

Drives the public ``InvestigationRunner`` / ``DurableStore.control`` path with
scripted models (no model HTTP). Opt in with ``M1_DURABLE_POSTGRES=1``; point
``OPSPILOT_LAB_DSN`` at a throwaway server to leave the 55431 lab alone.
"""

from __future__ import annotations

import json
import os

import psycopg
import pytest

from opspilot.investigation.loop import InvestigationLoop
from opspilot.investigation.store import DurableStepStore
from opspilot.worker import Worker
from scripts.m0.postgres_lab import DSN
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


def test_follow_up_after_a_rejected_forced_final_round_resumes_and_publishes():
    h = Harness(budget_limit=4, model_requests=4)
    first = _forced_final_handoff(h)
    assert h.store.run_usage(h.run)["model_requests_used"] == 3
    queries = h.transport_requests

    assert (
        h.store.control(h.incident, 0, "follow_up", "operator", {"question": FOLLOW_UP})
        == 1
    )
    assert h.rows()["run"]["state"] == "queued"

    runner = h.runner([report_from_transcript])
    outcome = runner.resume(h.incident)
    assert outcome.status == "published", outcome  # not blocked(INCOMPATIBLE_STATE)
    assert outcome.epoch > first.epoch
    assert len(runner.model.calls) >= 1
    assert FOLLOW_UP in json.dumps(runner.model.calls[0].messages)
    rows = h.rows()
    assert rows["run"]["run_id"] == h.run and rows["run"]["state"] == "completed"
    assert rows["conclusion"]["conclusion"]["execution"] == "completed"
    assert h.store.run_usage(h.run)["model_requests_used"] == 3 + len(
        runner.model.calls
    )
    assert h.transport_requests == queries  # nothing was re-queried


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


def test_a_budget_driven_final_round_still_follows_up_without_blocking():
    h = Harness(budget_limit=2, model_requests=2)
    outcome = h.runner([*_tool_rounds(1), _bad]).resume(h.incident)
    assert outcome.status == "handed_off" and outcome.reason == "REPORT_INVALID", (
        outcome
    )
    assert (
        h.store.control(h.incident, 0, "follow_up", "operator", {"question": FOLLOW_UP})
        == 1
    )
    again = h.runner([report_from_transcript]).resume(h.incident)
    assert again.status != "blocked" and again.reason != "INCOMPATIBLE_STATE", again
    assert h.rows()["run"]["state"] != "blocked"


def test_an_edited_committed_reply_still_blocks_the_follow_up():
    h = Harness(budget_limit=4, model_requests=4)
    _forced_final_handoff(h)
    with psycopg.connect(DSN, autocommit=True) as conn:
        changed = conn.execute(
            "UPDATE opspilot_steps SET response = jsonb_set(response, "
            "'{assistant,content}', '\"edited after the fact\"') "
            "WHERE run_id = %s AND logical_key = 'ctx0:round-2'",
            (h.run,),
        ).rowcount
    assert changed == 1
    assert (
        h.store.control(h.incident, 0, "follow_up", "operator", {"question": FOLLOW_UP})
        == 1
    )
    runner = h.runner([report_from_transcript])
    outcome = runner.resume(h.incident)
    assert outcome.status == "blocked" and outcome.reason == "INCOMPATIBLE_STATE", (
        outcome
    )
    assert runner.model.calls == []
    assert h.rows()["run"]["state"] == "blocked"
