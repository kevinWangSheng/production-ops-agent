"""Human control outranks the investigator (PRODUCT-CONSTRAINTS "Runtime and
human control requirements"; C3 section 4 "人工控制与结果所有权").

A human decision taken through the workbench while a Run is mid-flight must
win: the Run's late completion is not published over it, and the external
``IncidentOutcome`` reports the human decision, not the model's report.
Drives the real web app, store and runner on PostgreSQL with a scripted model
(``tests/workbench_driver.py``).
"""

from __future__ import annotations

from uuid import UUID

import pytest

from tests.m1_investigation_support import report_from_transcript
from tests.workbench_driver import (  # noqa: F401 -- shared pytest fixtures
    tool_round,
    workbench_database_fixture,
    workbench_fixture,
)

#: Control action taken mid-Run -> the state the outcome must report.
DECISIONS = {
    "pause": "paused",
    "cancel": "cancelled",
}


@pytest.mark.parametrize("action", sorted(DECISIONS))
def test_a_decision_taken_mid_run_wins_over_the_runs_late_report(workbench, action):
    scenario = workbench.from_alert(f"safety-human-{action}")
    incident = UUID(scenario.subject_id)

    def decide_then_report(call):
        response = workbench.control(incident, action)
        assert response.status in {200, 303}, response.text
        return report_from_transcript(call)

    result, _ = workbench.run(scenario, [tool_round(), decide_then_report])

    assert result.status != "published", result
    rows = workbench.store.rebuild(incident)
    assert rows["conclusion"] is None
    outcome = workbench.project(scenario)
    assert outcome.final_state == DECISIONS[action]
    assert outcome.report_available is False


def test_a_paused_incident_is_not_claimed_again_until_resumed(workbench):
    scenario = workbench.from_alert("safety-human-pause-claim")
    incident = UUID(scenario.subject_id)
    assert workbench.control(incident, "pause").status in {200, 303}

    result, model = workbench.run(scenario, [tool_round(), report_from_transcript])

    assert model.calls == [], "a paused incident must not reach the model"
    rows = workbench.store.rebuild(incident)
    assert rows["conclusion"] is None
    assert rows["state"] == "paused", rows["state"]
