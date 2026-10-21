"""由实现方补充，非独立作者编写：工具超时的持久结果投影和页面提示。"""

from uuid import UUID, uuid4

import pytest

from tests.m1_web_support import basic, build_workbench, call, submit_incident


@pytest.mark.parametrize(
    ("status", "contact", "visible"),
    [
        ("timeout", "possible", True),
        ("timeout", "none", False),
        ("denied", "possible", False),
        ("timeout", None, False),
    ],
)
def test_tool_row_renders_stored_source_contact(status, contact, visible):
    app, workbench, _ = build_workbench()
    incident = submit_incident(app).json()["incident_id"]
    store = workbench.incidents
    run = next(r for r in store.runs.values() if r["incident_id"] == UUID(incident))
    result = {"tool": "metrics_range_query", "status": status, "adopted": False}
    if contact is not None:
        result["source_contact"] = contact
    run["steps"].append(
        {
            "step_id": uuid4(),
            "run_id": run["run_id"],
            "sequence": 1,
            "logical_key": "step-1",
            "status": "tool_result_committed",
            "response": {},
            "control_generation": 0,
            "tool_results": [{"ordinal": 0, "result": result}],
        }
    )
    response = call(app, "GET", f"/incidents/{incident}", headers=basic())
    assert response.status == 200
    assert f'<span class="badge">{status}</span>' in response.text
    assert ("source may have received the request" in response.text) is visible
