"""Replay the recorded real deepseek-flash reports through the live-Run loop.

The two saved reports are the model's actual final texts from the 2026-09-17
bounded Runs (``docs/evidence/m1-01-acceptance/real-run-report*.json``). Both
Runs ended ``REPORT_INVALID``. Replaying them offline pins the reason: the
harness handed the loop a time policy without ``mode``/``window``, so no
delivered view could wear ``policy-window-1`` and every fact citing it failed
the bind. No provider request is made here.
"""

import json
import re
from datetime import timedelta
from pathlib import Path

from opspilot.acceptance import IncidentScenario, outcome_from_loop
from opspilot.investigation.loop import ModelReply
from opspilot.investigation.reports import parse_report
from scripts.m1_live_flash_loop import EVIDENCE_CONTEXT, LIVE_TOOL, build_run
from tests.m1_tool_support import NOW, FakeClock

EVIDENCE = Path(__file__).parents[2] / "docs/evidence/m1-01-acceptance"
RECORDED_EVIDENCE_ID = re.compile(r"[0-9a-f-]{36}-t0")
# What scripts/m1_live_flash_loop.py sent before the fix: an id with no policy
# body. eligible_time_policies() skips it, so the bind fails closed.
UNBOUND_CONTEXT = {
    "type": "opspilot-evidence-context-v4",
    "time_policies": [{"id": "policy-window-1"}],
}


class ReplayModel:
    """Round 1 asks for the fixture metric; round 2 returns the saved report.

    The saved report cites the evidence id of its own Run; the replay swaps in
    the id the loop actually delivered, which is what the real model did.
    """

    def __init__(self, report_text: str):
        self.report_text = report_text

    def complete(self, call) -> ModelReply:
        tool_messages = [m for m in call.messages if m.get("role") == "tool"]
        if not tool_messages:
            return ModelReply(
                content=None,
                reasoning_content="r",
                tool_calls=(
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": LIVE_TOOL,
                            "arguments": json.dumps({"expr": "rate(http_errors[5m])"}),
                        },
                    },
                ),
                finish_reason="tool_calls",
                usage={},
                response_model="deepseek-flash",
                raw={"id": "replay-1"},
            )
        delivered = json.loads(tool_messages[-1]["content"])["evidence_id"]
        return ModelReply(
            content=RECORDED_EVIDENCE_ID.sub(delivered, self.report_text),
            reasoning_content="r",
            tool_calls=(),
            finish_reason="stop",
            usage={},
            response_model="deepseek-flash",
            raw={"id": "replay-2"},
        )


def replay(report_text: str, evidence_context=None):
    clock = FakeClock()
    loop, request = build_run(
        ReplayModel(report_text),
        clock=clock,
        deadline=NOW + timedelta(minutes=12),
        run_id="replay-run",
        evidence_context=evidence_context,
    )
    outcome = loop.run(request)
    scenario = IncidentScenario(
        scenario_id="m1-01-real-replay",
        feature_id="F3",
        acceptance_step="external IncidentScenario -> IncidentOutcome",
        kind="real-deepseek",
        subject_id="incident-acceptance",
    )
    return outcome, outcome_from_loop(scenario, outcome)


def test_second_real_report_is_a_usable_report_under_the_live_context():
    text = (EVIDENCE / "real-run-report-2.json").read_text()
    outcome, accepted = replay(text)
    assert outcome.execution == "completed"
    assert outcome.handoff is False
    assert outcome.report is not None
    assert outcome.report.schema_version == "m0-report-v2"
    assert accepted.final_state == "completed"
    assert accepted.decision == "report_available"
    assert accepted.report_available is True


def test_second_real_report_was_rejected_only_by_the_unbound_policy_id():
    text = (EVIDENCE / "real-run-report-2.json").read_text()
    assert parse_report(text, finish_reason="stop")[0] is not None
    outcome, accepted = replay(text, evidence_context=UNBOUND_CONTEXT)
    assert outcome.execution == "failed"
    assert outcome.handoff_reasons == ("REPORT_INVALID",)
    assert accepted.decision == "handoff"
    assert accepted.report_available is False


def test_live_context_policy_is_a_bindable_historical_window():
    (policy,) = EVIDENCE_CONTEXT["time_policies"]
    assert policy["id"] == "policy-window-1"
    assert policy["mode"] == "historical_window"
    assert policy["window"] == {
        "start": "2026-09-14T00:00:00+00:00",
        "end": "2026-09-14T01:00:00+00:00",
    }


def test_first_real_report_stays_invalid_because_it_is_two_json_objects():
    text = (EVIDENCE / "real-run-report.json").read_text()
    first, end = json.JSONDecoder().raw_decode(text)
    assert text[end:].strip() == '{"type":"json_object"}'
    assert first["schema_version"] == "m0-report-v2"
    # The contract asks for exactly one json object; the appended echo of the
    # response_format is a model output fault, not a harness or parser defect.
    assert parse_report(text, finish_reason="stop") == (None, "REPORT_INVALID")
    outcome, _ = replay(text)
    assert outcome.handoff_reasons == ("REPORT_INVALID",)
    # Without the trailer the same text is a well-formed incomplete report.
    outcome, accepted = replay(text[:end])
    assert outcome.execution == "completed"
    assert outcome.handoff_reasons == ("INCOMPLETE_INVESTIGATION",)
    assert accepted.report_available is True
