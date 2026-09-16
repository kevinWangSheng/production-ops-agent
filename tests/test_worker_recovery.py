from uuid import uuid4

from opspilot.recovery import rebuild_plan
from opspilot.worker import Worker


def test_rebuild_plan_only_resumes_committed_pending_work():
    run = uuid4()
    plan = rebuild_plan(
        {
            "incident_id": uuid4(),
            "control_generation": 0,
            "run": {"run_id": run, "state": "running"},
            "steps": [{"step_id": uuid4()}],
            "pending_tools": [{"ordinal": 1}],
            "conclusion": None,
        }
    )
    assert plan.candidate
    assert plan.pending_tools == ({"ordinal": 1},)


def test_pending_tool_keeps_stable_operation_and_call_details():
    plan = rebuild_plan(
        {
            "incident_id": uuid4(),
            "control_generation": 0,
            "run": {"run_id": uuid4(), "state": "running"},
            "steps": [],
            "pending_tools": [
                {
                    "step_id": uuid4(),
                    "ordinal": 0,
                    "operation_id": "s:0",
                    "tool_call": {"name": "query", "arguments": {"x": 1}},
                }
            ],
            "conclusion": None,
        }
    )
    assert plan.pending_tools[0]["operation_id"] == "s:0"
    assert plan.pending_tools[0]["tool_call"]["name"] == "query"


def test_terminal_plan_is_not_resumable():
    plan = rebuild_plan(
        {
            "incident_id": uuid4(),
            "control_generation": 0,
            "run": {"run_id": uuid4(), "state": "blocked"},
            "steps": [],
            "pending_tools": [],
            "conclusion": None,
        }
    )
    assert not plan.candidate


def test_worker_uses_unique_owner():
    worker = Worker.create(object(), {"schema": "v1"})
    assert worker.owner
