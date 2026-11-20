"""#666 PR-E implementer unit checks: the task-list switch (C1, #565) in the
eval driver, the comparison table and the Holmes merge refusal. Synthetic
question directories and in-process stand-ins only; no real upstream
``expected_output`` or ``before_test`` is read, no model, PostgreSQL, web or
worker is started."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from scripts.holmes_compare import merge as compare_merge
from scripts.holmes_compare.records import TrialRow
from scripts.holmes_evals import driver, fake_es, questions, task_list, todo_compare
from scripts.holmes_evals.log_backends import LOG_QUESTIONS, backend_of
from scripts.holmes_evals.questions import Question
from scripts.holmes_evals.tool_calls import tool_calls_from_rows
from tests.test_holmes_evals_b_impl_units import (
    QID,
    _backends,
    _cfg,
    _upstream,
    es,  # noqa: F401 - fixture
)

TODO = "TodoWrite"


def _q(enable_todo: bool) -> Question:
    return Question(user_prompt="q", enable_todo=enable_todo)


def test_question_lists_and_283_spec() -> None:
    assert questions.C1_QUESTIONS == ("283_todowrite_multistep_audit",)
    assert (
        questions.ALL_QUESTIONS[-8] == "283_todowrite_multistep_audit"
    )  # PR-F appends C6
    assert driver.resolve_questions("283") == questions.C1_QUESTIONS
    assert driver.resolve_questions("todo-hard") == questions.TODO_HARD_QUESTIONS
    assert set(questions.TODO_HARD_QUESTIONS) <= set(questions.B1_QUESTIONS)
    spec = LOG_QUESTIONS["283_todowrite_multistep_audit"]
    assert backend_of(spec.qid) == "kubernetes"
    assert spec.namespace is None and spec.namespace_prefix == "app-283-"
    assert spec.needs_k8s and spec.host_ports == ()
    assert all(
        s.namespace_prefix is None for q, s in LOG_QUESTIONS.items() if q != spec.qid
    )


def test_todo_for_modes() -> None:
    assert task_list.todo_for("question", _q(True)) == (True, "question")
    assert task_list.todo_for("question", _q(False)) == (False, "question")
    assert task_list.todo_for("on", _q(False)) == (True, "forced")
    assert task_list.todo_for("off", _q(True)) == (False, "forced")
    with pytest.raises(ValueError):
        task_list.todo_for("yes", _q(True))


def test_env_switch_both_sides_and_default_bytes(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    assert driver.tool_env(cfg) == driver.tool_env(cfg, todo=False)
    assert "OPSPILOT_TODO_WRITE" not in driver.tool_env(cfg)
    on = driver.tool_env(cfg, "kubernetes", todo=True)
    assert on["OPSPILOT_TODO_WRITE"] == "1" and "OPSPILOT_ES_URL" not in on


def _view(tool: str, **extra: Any) -> dict[str, Any]:
    return {"tool": tool, **extra}


def test_task_list_stats_from_rows() -> None:
    steps = [
        {
            "step_id": "b",
            "sequence": 2,
            "status": "committed",
            "tool_results": [
                {"ordinal": 1, "result": _view(TODO, status="denied", reason="INVALID_TODOS")},
                {"ordinal": 0, "result": _view(TODO, status="ok", todos=[
                    {"id": "1", "content": "a", "status": "completed"},
                    {"id": "2", "content": "b", "status": "failed"},
                    {"id": "3", "content": "c", "status": "pending"},
                ])},
            ],
        },
        {
            "step_id": "a",
            "sequence": 1,
            "status": "committed",
            "tool_results": [
                {"ordinal": 0, "result": _view(TODO, status="ok", todos=[
                    {"id": "1", "content": "a", "status": "in_progress"},
                ])},
                {"ordinal": 1, "result": _view("kubectl_get", status="ok", evidence_id="ev-1")},
                {"ordinal": 2, "result": _view(TODO, status="denied", evidence_id="ev-x",
                                               reason="TOOL_NOT_REGISTERED")},
            ],
        },
        {
            "step_id": "c",
            "sequence": 3,
            "status": "late_result",
            "tool_results": [{"ordinal": 0, "result": _view(TODO, status="ok", todos=[])}],
        },
    ]  # fmt: skip
    stats = task_list.task_list_stats_from_rows(steps)
    assert stats == {
        "calls_ok": 2,
        "calls_refused": 1,
        "queries": 2,  # the query and the gateway's refusal of an unregistered name
        "last": {
            "total": 3,
            "completed": 1,
            "failed": 1,
            "in_progress": 0,
            "pending": 1,
        },
    }
    assert task_list.task_list_stats_from_rows([])["last"] is None


def test_accepted_todowrite_is_a_call_with_output() -> None:
    planned = {
        "assistant": {
            "tool_calls": [
                {"function": {"name": TODO, "arguments": json.dumps({"todos": []})}},
                {"function": {"name": TODO, "arguments": "{}"}},
            ]
        }
    }
    accepted = {"tool": TODO, "status": "ok", "todos": [], "message": "saved"}
    refused = {"tool": TODO, "status": "denied", "reason": "INVALID_TODOS"}
    calls = tool_calls_from_rows(
        [
            {
                "step_id": "s",
                "sequence": 1,
                "status": "committed",
                "response": planned,
                "tool_results": [
                    {"ordinal": 0, "result": accepted},
                    {"ordinal": 1, "result": refused},
                ],
            }
        ],
        {},
    )
    assert calls[0].tool == TODO and calls[0].refused_code is None
    assert calls[0].output == accepted and calls[0].params == {"todos": []}
    assert (calls[1].refused_code, calls[1].refused_reason) == (
        "denied",
        "INVALID_TODOS",
    )


def _summary(evidence: Path, round_: str, qid: str, trial: int, **todo: Any) -> None:
    path = evidence / f"holmes-evals-{round_}" / qid / f"trial-{trial}" / "summary.json"
    path.parent.mkdir(parents=True)
    block: dict[str, Any] = {
        "qid": qid,
        "trial": trial,
        "category": "ok",
        "judge": {"choice": "A" if trial == 1 else "B"},
        "run_wait": {"seconds": 10.0 * trial},
    }
    if todo:
        block["todo"] = todo
    path.write_text(
        json.dumps(
            {
                "status": "completed",
                "run": {
                    "model_requests_used": 5 + trial,
                    "tool_requests": [{}] * trial,
                },
                "holmes_eval": block,
            }
        )
    )


def test_check_todo_mode(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.evidence_root = tmp_path / "ev"
    _summary(cfg.evidence_root, cfg.round, QID, 1)  # no block: question mode
    driver.check_todo_mode(cfg)
    cfg.todo_mode = "on"
    with pytest.raises(driver.RoundAbort, match="TODO_MODE_CHANGED"):
        driver.check_todo_mode(cfg)


def test_compare_table_rows_and_duplicates(tmp_path: Path) -> None:
    ev = tmp_path / "ev"
    q283, hard = questions.C1_QUESTIONS[0], questions.TODO_HARD_QUESTIONS[0]
    stats = {"calls_ok": 2, "calls_refused": 0, "queries": 9,
             "last": {"total": 4, "completed": 3, "failed": 0, "in_progress": 1, "pending": 0}}  # fmt: skip
    for t in (1, 2, 3):
        _summary(
            ev,
            "main",
            q283,
            t,
            mode="question",
            enabled=True,
            source="question",
            stats=stats,
        )
        _summary(ev, "main", hard, t, mode="question", enabled=False, source="question")
        _summary(ev, "off283", q283, t, mode="off", enabled=False, source="forced")
    _summary(ev, "main", "254_elasticsearch_dr_test_log_check", 1)  # not compared
    missing = ev / "holmes-evals-off283" / q283 / "trial-3" / "summary.json"
    missing.write_text(
        json.dumps(
            {
                "status": "missing",
                "holmes_eval": {"qid": q283, "trial": 3, "category": "env_broken"},
            }
        )
    )
    result = todo_compare.table(todo_compare.collect(ev, ["main", "off283"]))
    rows = {(r["qid"], r["todo"]): r for r in result["rows"]}
    on = rows[(q283, True)]
    assert (on["round"], on["trials"], on["passes"]) == ("main", 3, 1)
    assert on["model_requests"] == {"values": [6.0, 7.0, 8.0], "mean": 7.0}
    assert on["queries"]["values"] == [1.0, 2.0, 3.0]
    assert on["todo_calls_ok"] == 6 and on["trials_with_todo"] == 3
    assert on["last_completed"] == ["3/4"] * 3
    off = rows[(q283, False)]
    assert (off["round"], off["trials"], off["missing"], off["passes"]) == (
        "off283",
        3,
        1,
        1,
    )
    assert off["queries"]["values"] == [1.0, 2.0]
    assert rows[(hard, False)]["last_completed"] == ["—"] * 3
    assert rows[(hard, True)]["round"] is None and rows[(hard, True)]["trials"] == 0
    assert "TodoWrite" in todo_compare.to_markdown(result)
    _summary(ev, "again", q283, 1, mode="question", enabled=True, source="question")
    with pytest.raises(todo_compare.CompareRefused, match="TODO_ROW_DUPLICATE"):
        todo_compare.table(todo_compare.collect(ev, ["main", "again"]))


def _row(agent: str, qid: str, enabled: bool | None, source: str | None) -> TrialRow:
    return TrialRow(agent, qid, 1, True, "ok", None, 1.0, 1, False,  # type: ignore[arg-type]
                    todo_enabled=enabled, todo_source=source)  # fmt: skip


def test_merge_refuses_forced_or_differing_task_list() -> None:
    q = questions.C1_QUESTIONS
    same = compare_merge.merge(
        [_row("opspilot", q[0], True, "question")],
        [_row("holmes", q[0], True, "question")],
        q,
    )
    assert same["questions"] == 1
    with pytest.raises(compare_merge.MergeRefused, match="TODO_MODE_DIFFERS"):
        compare_merge.merge(
            [_row("opspilot", q[0], False, "forced")],
            [_row("holmes", q[0], True, "question")],
            q,
        )
    with pytest.raises(compare_merge.MergeRefused, match="TODO_MODE_DIFFERS"):
        compare_merge.merge(
            [_row("opspilot", q[0], False, "question")],
            [_row("holmes", q[0], True, "question")],
            q,
        )
    # Summaries from before PR-E carry no task-list facts: nothing to compare.
    compare_merge.merge(
        [_row("opspilot", q[0], None, None)],
        [_row("holmes", q[0], True, "question")],
        q,
    )


def test_trial_with_task_list_on(
    tmp_path: Path,
    es: tuple[fake_es.FakeElasticsearch, str],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPSPILOT_LEDGER_DIR", str(tmp_path / "ledger"))
    store, url = es
    cfg = _cfg(tmp_path)
    cfg.es_url = url
    cfg.product_es["OPSPILOT_ES_URL"] = url
    cfg.questions = (QID,)
    cfg.todo_mode = "on"
    _upstream(cfg.upstream)
    seen: dict[str, Any] = {}
    b = _backends(store, url, seen)
    b.now = lambda: datetime(2026, 11, 18, 19, 0, 50, tzinfo=UTC)
    stats = {"calls_ok": 1, "calls_refused": 0, "queries": 3, "last": None}
    b.run_tool_names = lambda dsn, run: ["kubectl_get", TODO]
    b.task_list_stats = lambda dsn, run: stats
    report = driver.run_question(cfg, b, QID, [1])
    assert report["trials"][1]["judge"].get("choice") == "A", report
    assert seen["env_web"]["OPSPILOT_TODO_WRITE"] == "1"
    assert seen["env_worker"]["OPSPILOT_TODO_WRITE"] == "1"
    block = json.loads(cfg.summary_path(QID, 1).read_text())["holmes_eval"]
    assert block["todo"]["mode"] == "on" and block["todo"]["source"] == "forced"
    assert block["todo"]["offered"] is True and block["todo"]["stats"] == stats
    assert {"TODO_FORCED", "TODO_UPSTREAM_REMINDER_NOT_COPIED"} <= set(
        block["disclosures"]
    )


def test_switch_mismatch_is_deterministic(
    tmp_path: Path,
    es: tuple[fake_es.FakeElasticsearch, str],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPSPILOT_LEDGER_DIR", str(tmp_path / "ledger"))
    store, url = es
    cfg = _cfg(tmp_path)
    cfg.es_url = url
    cfg.product_es["OPSPILOT_ES_URL"] = url
    cfg.todo_mode = "on"
    _upstream(cfg.upstream)
    b = _backends(store, url, {})
    b.now = lambda: datetime(2026, 11, 18, 19, 0, 50, tzinfo=UTC)
    b.run_tool_names = lambda dsn, run: ["kubectl_get"]
    report = driver.run_question(cfg, b, QID, [1])
    assert report["trials"][1]["status"] == "missing"
    summary = json.loads(cfg.summary_path(QID, 1).read_text())
    assert "TODO_SWITCH_MISMATCH" in json.dumps(summary)
    # Review B1: the missing trial records the forced switch, so the round
    # resumes only in its own mode.
    assert summary["holmes_eval"]["todo"] == {
        "mode": "on",
        "enabled": True,
        "source": "forced",
    }
    driver.check_todo_mode(cfg)
    cfg.todo_mode = "question"
    with pytest.raises(driver.RoundAbort, match="TODO_MODE_CHANGED"):
        driver.check_todo_mode(cfg)


def test_all_missing_row_lands_in_its_cell(tmp_path: Path) -> None:
    ev = tmp_path / "ev"
    hard = questions.TODO_HARD_QUESTIONS[1]
    for t in (1, 2, 3):
        path = ev / "holmes-evals-onhard" / hard / f"trial-{t}" / "summary.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "status": "missing",
                    "holmes_eval": {
                        "qid": hard,
                        "trial": t,
                        "category": "env_broken",
                        "todo": {"mode": "on", "enabled": True, "source": "forced"},
                    },
                }
            )
        )
    result = todo_compare.table(todo_compare.collect(ev, ["onhard"]))
    cells = [r for r in result["rows"] if r["qid"] == hard]
    on = [r for r in cells if r["todo"] is True]
    assert len(on) == 1 and on[0]["round"] == "onhard"
    assert (on[0]["trials"], on[0]["missing"], on[0]["passes"]) == (3, 3, 0)
    assert not any(r["todo"] is None for r in cells)
