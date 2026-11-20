"""Independent contract tests for C1 (#565) ``TodoWrite`` on PostgreSQL.

Written from the C1 task record (docs/tasks/2026-11-20-tool-expansion-c1.md,
"公开接口" §3, §5, §6, §8 and contract tests 4, 7, 8, 9, 11) and decision
table r11 C1-U1=a, without reading the implementation. Red until it lands.

The real web app, durable store, runner, event log and evidence store run
on the lab PostgreSQL (``M1_DURABLE_POSTGRES=1``; a fresh database per test
from ``tests/workbench_driver.py``); the model is scripted and the tool
profile is the product fixture one. The Runs are recorded with the fixture
tools plus ``TODO_WRITE_SCHEMA`` -- what a web with the switch on records --
because the loop dispatches on the Run input's ``tool_schemas`` (C1 §3), not
on the profile or the environment. The fixture executor is wrapped only to
count what reaches the gateway.

Baseline note: written on origin/main a8cf46d9 + interface 725a70c6, before
C2b (PR #741) and #722 (PR #725); nothing here depends on either.

Seams assumed beyond the interface (each also marked ``# ASSUMPTION:``):

* The page's step entry carries ``plan`` as ``{done, total, todos}`` and the
  Run start entry carries ``plan`` in a shape whose ``todos`` (or the value
  itself, when a list) is the task list; both read via ``_plan_todos``.
* The workbench draws the plan card's status marks as the literal upstream
  ``[ ]`` ``[~]`` ``[✓]`` ``[✗]`` text.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any
from uuid import UUID

import pytest

from opspilot.investigation.inputs import ToolFace
from opspilot.investigation.runner import InvestigationRunner
from opspilot.tools.fixture import (
    FIXTURE_TOOL,
    fixture_executor_factory,
    fixture_face,
    fixture_versions,
)
from opspilot.tools.registry import canonical
from opspilot.worker import Worker
from tests.m1_investigation_support import (
    ScriptedModel,
    reply,
    report_json,
    tool_call,
)
from tests.workbench_driver import (  # noqa: F401 -- shared pytest fixtures
    basic,
    call,
    workbench_database_fixture,
    workbench_fixture,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

TOOL = "TodoWrite"
LEASE_SECONDS = 2


class Crash(Exception):
    """A worker dying mid-attempt (not a product error)."""


def _todo():
    from opspilot.investigation import todo

    return todo


def _items(*statuses: str, prefix: str = "t") -> list[dict[str, str]]:
    return [
        {"id": f"{prefix}{i}", "content": f"Check part {i}", "status": status}
        for i, status in enumerate(statuses, start=1)
    ]


def _todo_call(todos, call_id="call-todo"):
    return tool_call(call_id=call_id, name=TOOL, arguments=json.dumps({"todos": todos}))


def _metrics_call(call_id="call-metrics"):
    return tool_call(call_id=call_id, name=FIXTURE_TOOL)


def _round(*calls):
    return reply(tool_calls=list(calls), finish="tool_calls")


def _tool_views(model_call) -> list[dict[str, Any]]:
    return [
        json.loads(m["content"]) for m in model_call.messages if m.get("role") == "tool"
    ]


def _cite_metrics(model_call):
    eids = [v["evidence_id"] for v in _tool_views(model_call) if v.get("evidence_id")]
    return reply(content=report_json(evidence_id=eids[-1] if eids else "missing"))


class Counting:
    """The fixture gateway, counting the requests that reach it."""

    def __init__(self, inner, seen: list, crash: dict | None = None):
        self._inner, self._seen, self._crash = inner, seen, crash
        self.scope = inner.scope

    @property
    def tool_seconds_used(self):
        return self._inner.tool_seconds_used

    def execute(self, request):
        if self._crash is not None and self._crash.get("armed"):
            self._crash["armed"] = False
            raise Crash("killed before the query was dispatched")
        self._seen.append(request.tool_name)
        return self._inner.execute(request)


def _todo_face() -> ToolFace:
    base = fixture_face()
    return ToolFace(
        tool_schemas=(*base.tool_schemas, _todo().TODO_WRITE_SCHEMA),
        variant_id=base.variant_id,
        evidence_context=base.evidence_context,
    )


@pytest.fixture(name="todo_workbench")
def todo_workbench_fixture(workbench):
    # The Runs this workbench records carry TodoWrite in their input.
    workbench.workbench.tool_face = _todo_face()
    return workbench


def _runner(wb, script, *, seen, crash=None, lease_seconds=30, parallelism=1):
    inner = fixture_executor_factory(wb.store, evidence=wb.evidence, clock=wb.clock)

    def factory(lease, run_input):
        return Counting(inner(lease, run_input), seen, crash)

    model = ScriptedModel(script)
    runner = InvestigationRunner(
        store=wb.store,
        worker=Worker.create(wb.store, fixture_versions()),
        model=model,
        executor_factory=factory,
        clock=wb.clock,
        lease_seconds=lease_seconds,
        events=wb.events,
        evidence=wb.evidence,
        tool_parallelism=parallelism,
    )
    return runner, model


def _run(wb, scenario, script, **kwargs):
    seen: list[str] = []
    runner, model = _runner(wb, script, seen=seen, **kwargs)
    return runner.resume(UUID(scenario.subject_id)), model, seen


def _results(wb, incident: UUID) -> list[list[dict[str, Any]]]:
    return [
        [item["result"] for item in step.get("tool_results") or []]
        for step in wb.steps(incident)
    ]


def _db(wb, sql: str, params: tuple) -> list[dict[str, Any]]:
    with wb.store.transaction(snapshot=True) as conn:
        return conn.execute(sql, params).fetchall()


def _events(wb, incident: UUID, kind: str) -> list[dict[str, Any]]:
    return [
        dict(e.payload)
        for e in wb.events.read_after(incident, 0, limit=1000)
        if e.kind == kind
    ]


# -- 4. one call: no gateway, no evidence, no charge; stored with the step ----------


def test_a_task_list_is_stored_with_its_step_and_produces_no_evidence(todo_workbench):
    wb = todo_workbench
    scenario = wb.from_alert("c1-store")
    incident = UUID(scenario.subject_id)
    todos = _items("completed", "in_progress", "pending")

    result, _, seen = _run(
        wb, scenario, [_round(_metrics_call(), _todo_call(todos)), _cite_metrics]
    )

    assert result.status == "published", result
    assert seen == [FIXTURE_TOOL]  # the task list never reached the gateway
    run_id = wb.store.rebuild(incident)["run"]["run_id"]
    first = _results(wb, incident)[0]
    assert [r["tool"] for r in first] == [FIXTURE_TOOL, TOOL]
    query, plan = first
    assert plan["status"] == "ok" and plan["todos"] == todos
    assert plan["adopted"] is False and "evidence_id" not in plan
    # The same row in opspilot_steps.tool_results, at its ordinal.
    rows = _db(
        wb,
        "SELECT tool_results FROM opspilot_steps WHERE run_id=%s "
        "AND tool_results::text LIKE %s",
        (run_id, f"%{TOOL}%"),
    )
    assert len(rows) == 1
    stored = {item["ordinal"]: item["result"] for item in rows[0]["tool_results"]}
    assert stored[1] == plan
    # Evidence and charges: the query only.
    evidence = _db(
        wb, "SELECT evidence_id FROM opspilot_evidence WHERE run_id=%s", (str(run_id),)
    )
    assert [e["evidence_id"] for e in evidence] == [query["evidence_id"]]
    assert not any(e["evidence_id"].startswith(plan["operation_id"]) for e in evidence)
    charged = {
        r["operation_id"]
        for r in _db(
            wb,
            "SELECT operation_id FROM opspilot_tool_charges WHERE run_id=%s",
            (run_id,),
        )
    }
    assert charged == {query["operation_id"]}
    # The progress event names the tool and carries no evidence id.
    committed = [
        p for p in _events(wb, incident, "tool_committed") if p["tool"] == TOOL
    ]
    assert len(committed) == 1
    assert committed[0]["evidence_id"] is None
    assert committed[0]["ordinal"] == 1 and committed[0]["status"] == "ok"
    assert committed[0]["adopted"] is False
    # The report cites the query, never the plan.
    assert plan["operation_id"] not in json.dumps(
        wb.incident(incident)["conclusion"], default=str
    )


def test_parallel_dispatch_commits_by_ordinal_and_keeps_both_lists(todo_workbench):
    wb = todo_workbench
    scenario = wb.from_alert("c1-parallel")
    incident = UUID(scenario.subject_id)
    early, late = _items("pending", "pending"), _items("completed", "in_progress")

    result, _, seen = _run(
        wb,
        scenario,
        [
            _round(
                _todo_call(early, "c-0"), _metrics_call("c-1"), _todo_call(late, "c-2")
            ),
            _cite_metrics,
        ],
        parallelism=2,
    )

    assert result.status == "published", result
    assert seen == [FIXTURE_TOOL]
    first = _results(wb, incident)[0]
    assert [r["tool"] for r in first] == [TOOL, FIXTURE_TOOL, TOOL]
    assert first[0]["todos"] == early and first[2]["todos"] == late
    assert first[1]["status"] == "ok" and first[1]["evidence_id"]
    ordinals = sorted(p["ordinal"] for p in _events(wb, incident, "tool_committed"))
    assert ordinals == [0, 1, 2]


# -- 9. pause and cancel apply as usual ---------------------------------------------


@pytest.mark.parametrize("action,state", [("pause", "paused"), ("cancel", "cancelled")])
def test_a_decision_before_the_commit_keeps_the_task_list_out(
    todo_workbench, action, state
):
    wb = todo_workbench
    scenario = wb.from_alert(f"c1-{action}")
    incident = UUID(scenario.subject_id)

    def decide_then_plan(_call):
        assert wb.control(incident, action).status in {200, 303}
        return _round(_todo_call(_items("in_progress")))

    result, _, seen = _run(wb, scenario, [decide_then_plan, _cite_metrics])

    assert result.status != "published", result
    assert seen == []
    assert all(
        TOOL not in [r.get("tool") for r in step] for step in _results(wb, incident)
    )
    snapshot = wb.store.rebuild(incident)
    assert snapshot["conclusion"] is None
    assert wb.project(scenario).final_state == state


# -- 8. fault replay and rebuild ------------------------------------------------------


def test_a_pending_task_list_is_replayed_byte_identical_without_the_gateway(
    todo_workbench,
):
    wb = todo_workbench
    scenario = wb.from_alert("c1-replay")
    incident = UUID(scenario.subject_id)
    todos = _items("in_progress", "pending")
    crash = {"armed": True}

    with pytest.raises(Crash):
        _run(
            wb,
            scenario,
            [_round(_metrics_call("c-0"), _todo_call(todos, "c-1"))],
            crash=crash,
            lease_seconds=LEASE_SECONDS,
        )
    killed = wb.store.rebuild(incident)
    assert len(killed["pending_tools"]) == 2
    assert _results(wb, incident)[0] == []

    time.sleep(LEASE_SECONDS + 0.5)
    seen: list[str] = []
    runner, model = _runner(wb, [_cite_metrics], seen=seen, lease_seconds=LEASE_SECONDS)
    resumed = runner.resume(incident)

    assert resumed.status == "published", resumed
    assert resumed.replayed_tools == 2
    assert seen == [FIXTURE_TOOL]  # the task list was not sent to the gateway
    first = _results(wb, incident)[0]
    assert [r["tool"] for r in first] == [FIXTURE_TOOL, TOOL]
    plan = first[1]
    # The same pure function of the committed arguments, byte for byte.
    expected = _todo().todo_write_view(plan["operation_id"], {"todos": todos})
    assert canonical(plan) == canonical(dict(expected))
    # The rebuilt conversation the next request carried holds the list.
    assert [
        v.get("todos") for v in _tool_views(model.calls[0]) if v.get("tool") == TOOL
    ] == [todos]
    replay_events = [
        p for p in _events(wb, incident, "tool_committed") if p["tool"] == TOOL
    ]
    assert len(replay_events) == 1 and replay_events[0]["evidence_id"] is None


def test_a_committed_task_list_is_in_the_rebuilt_conversation(todo_workbench):
    wb = todo_workbench
    scenario = wb.from_alert("c1-rebuild")
    incident = UUID(scenario.subject_id)
    todos = _items("completed", "pending")

    def die(_call):
        raise Crash("killed during the second model request")

    with pytest.raises(Crash):
        _run(
            wb,
            scenario,
            [_round(_todo_call(todos), _metrics_call()), die],
            lease_seconds=LEASE_SECONDS,
        )
    time.sleep(LEASE_SECONDS + 0.5)
    seen: list[str] = []
    runner, model = _runner(wb, [_cite_metrics], seen=seen, lease_seconds=LEASE_SECONDS)
    resumed = runner.resume(incident)

    assert resumed.status == "published", resumed
    assert resumed.replayed_tools == 0 and seen == []
    lists = [
        v.get("todos") for v in _tool_views(model.calls[0]) if v.get("tool") == TOOL
    ]
    assert lists == [todos]


# -- 11. the workbench ------------------------------------------------------------------


def _plan_todos(plan: Any) -> list[dict[str, Any]]:
    # ASSUMPTION: ``{done, total, todos}``, or the list itself.
    if isinstance(plan, dict):
        return list(plan["todos"])
    return list(plan)


def _html(wb, incident: UUID) -> str:
    shown = call(wb.app, "GET", f"/incidents/{incident}", headers=basic())
    assert shown.status == 200, shown.text[:500]
    return shown.text


_DOT = r"(?:·|&middot;|&#183;|&#xb7;)"


def test_the_page_shows_the_plan_and_counts_only_queries(todo_workbench):
    wb = todo_workbench
    scenario = wb.from_alert("c1-page")
    incident = UUID(scenario.subject_id)
    plan_a = _items("completed", "in_progress", "pending", prefix="a")
    too_many = _items(*["pending"] * 51, prefix="z")

    result, _, _ = _run(
        wb,
        scenario,
        [
            _round(_metrics_call("c-0"), _todo_call(plan_a, "c-1")),
            _round(_todo_call(too_many, "d-0")),
            _cite_metrics,
        ],
    )
    assert result.status == "published", result

    log = wb.page(incident)["log"]
    steps = [e for e in log["entries"] if e.get("kind") == "step" and e.get("number")]
    first, second = steps[0], steps[1]
    assert [c["tool"] for c in first["calls"]] == [FIXTURE_TOOL]
    assert first["plan"]["done"] == 1 and first["plan"]["total"] == 3
    assert _plan_todos(first["plan"]) == plan_a
    assert not first.get("plan_rejected")
    assert second["calls"] == []
    assert second.get("plan_rejected") is True
    assert log["calls"] == 1  # the header counts queries only
    [start] = [e for e in log["entries"] if e.get("kind") == "run"]
    # The Run's plan is its last accepted list; the refused one changed nothing.
    assert _plan_todos(start["plan"]) == plan_a

    html = _html(wb, incident)
    assert "Investigation plan" in html
    assert re.search(rf"Plan updated\s*{_DOT}\s*1 of 3 done", html), "no plan line"
    for mark in ("[✓]", "[~]", "[ ]"):  # ASSUMPTION: literal upstream marks
        assert mark in html, mark
    for todo in plan_a:
        assert todo["content"] in html
    assert "2 queries" not in html
    assert re.search(r"\b1 query\b", html)


def test_a_failed_task_shows_as_failed_and_unfinished_ones_stay_open(todo_workbench):
    wb = todo_workbench
    scenario = wb.from_alert("c1-page-final")
    incident = UUID(scenario.subject_id)
    final_plan = _items("completed", "failed", "pending", prefix="f")

    result, _, _ = _run(
        wb,
        scenario,
        [_round(_metrics_call(), _todo_call(final_plan)), _cite_metrics],
    )
    assert result.status == "published", result
    [start] = [e for e in wb.page(incident)["log"]["entries"] if e.get("kind") == "run"]
    assert [t["status"] for t in _plan_todos(start["plan"])] == [
        "completed",
        "failed",
        "pending",
    ]
    html = _html(wb, incident)
    assert "[✗]" in html and "[ ]" in html


def test_a_run_without_a_task_list_renders_as_before(workbench):
    # The plain fixture face: no TodoWrite in the Run input at all.
    scenario = workbench.from_alert("c1-old-run")
    incident = UUID(scenario.subject_id)
    result, _, _ = _run(workbench, scenario, [_round(_metrics_call()), _cite_metrics])
    assert result.status == "published", result

    log = workbench.page(incident)["log"]
    for entry in log["entries"]:
        assert not entry.get("plan"), entry
        assert not entry.get("plan_rejected"), entry
    [start] = [e for e in log["entries"] if e.get("kind") == "run"]
    assert start.get("plan") is None
    assert log["calls"] == 1
    html = _html(workbench, incident)
    assert "Investigation plan" not in html
    assert "Plan updated" not in html


def test_no_table_or_column_is_added_for_the_task_list(todo_workbench):
    columns = _db(
        todo_workbench,
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_name LIKE 'opspilot_%%' AND "
        "(column_name ILIKE %s OR table_name ILIKE %s)",
        ("%todo%", "%todo%"),
    )
    assert columns == []
