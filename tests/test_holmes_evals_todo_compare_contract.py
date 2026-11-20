"""#666 PR-E contract: the C1-U2 comparison table (``todo_compare``).

Independent contract tests written from the task record's "PR-E 接口"
section ("对照表") and the C1 record section 11, without reading the PR-E
implementation. The three rounds are synthetic trial summaries on disk in
the driver's evidence layout (``<evidence-root>/holmes-evals-<round>/<qid>/
trial-<n>/summary.json``); nothing runs.

Rules under test: rows by ``(qid, todo.enabled)`` over ``C1_QUESTIONS +
TODO_HARD_QUESTIONS`` only, main trials 1-3 only; ``forced`` and
``question`` both taken; a row in two rounds exits non-zero with
``TODO_ROW_DUPLICATE``; each column's measure (passes as ``trial_record``:
only a judged A passes, missing and judge-exhausted trials count 0;
``pass_rate = passes / 3``; model requests, queries, TodoWrite calls, trials
with a list, the last list's completed/total or ``—``, seconds); a missing
row is written with ``trials: 0``; json and md carry the same rows and the
"separate table" note; no score threshold; nothing answer-derived leaks.

Assumptions beyond the interface (also marked ``# ASSUMPTION:``):

* ``todo_compare.main(argv) -> int``; the json is ``{"rows": [...]}`` (a bare
  list is accepted too).
* ``todo`` is a boolean, or ``on``/``off``, or ``开``/``关``.
* Per-trial columns (``model_requests``, ``queries``, ``seconds``) are
  ``{"values": [...trial order...], "mean": x}``; ``last_completed`` is a
  list with one ``"c/t"`` or ``"—"`` per trial with a summary.
* Revision r2 (lead, 2026-11-20): the query count is the number of
  ``run.tool_requests`` entries (gateway audit rows), not
  ``todo.stats.queries``; an off pass has no ``stats`` (TodoWrite calls 0,
  ``—``). The synthetic ``stats.queries`` is set off by 100 so reading it
  instead would fail.
* ``run.model_requests_used`` is read from the summary's ``run`` block (the
  synthetic summaries carry the value at ``run`` and ``run.run``).
* A ``status: missing`` summary carries no ``todo`` block in PR-B's shape,
  so which row it belongs to is not defined by the interface (reported as a
  gap); the tests use trials without a summary instead, and judge-exhausted
  and ``no_report`` trials for "counts 0".
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.holmes_evals_e_support import (
    HARD,
    Q184,
    Q185,
    Q193,
    Q275,
    Q280,
    Q283,
    stats,
    todo_block,
)
from tests.holmes_evals_support import REPO, mod
from tests.test_holmes_evals_driver_contract import _completed, _missing

MAIN = "main-r3"
OFF = "c1-todo-off-283"
ON = "c1-todo-on-hard"
LEAK = "EXPECTED-LEAK-SENTINEL-c1"
NOTE = "单独成表，不并入数据源工具的分数"


def summary(
    qid: str,
    trial: int,
    kind: str,
    *,
    enabled: bool,
    source: str,
    mode: str,
    requests: int = 10,
    st: dict[str, Any] | None = None,
    seconds: float = 40.0,
    queries: int = 6,
) -> dict[str, Any]:
    if kind == "missing":
        return _missing(qid, trial)
    s = _completed(qid, trial, kind)
    s["run"] = {
        "run_id": "22222222-2222-4222-8222-222222222222",
        "model_requests_used": requests,
        "run": {"model_requests_used": requests},
        "tool_requests": [
            {"tool": "elasticsearch_search", "n": i} for i in range(queries)
        ],
    }
    he = s["holmes_eval"]
    he["run_wait"] = {"seconds": seconds}
    he["todo"] = todo_block(enabled, source, mode, st)
    he["judge"] = dict(he["judge"], expected=LEAK)
    return s


def put(root: Path, round_: str, s: dict[str, Any]) -> None:
    he = s["holmes_eval"]
    path = (
        root / f"holmes-evals-{round_}" / he["qid"] / f"trial-{he['trial']}"
        / "summary.json"
    )  # fmt: skip
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(s))


def _st(ok: int, refused: int, queries: int, done: int, total: int) -> dict:
    last = None
    if ok:
        last = {"total": total, "completed": done, "failed": 0,
                "in_progress": total - done, "pending": 0}  # fmt: skip
    return stats(ok, refused, queries + 100, last)  # not the table's measure


def build(root: Path, *, duplicate: bool = False, drop_193_on: bool = False) -> None:
    q = dict(source="question", mode="question")
    # Main round: B1 hard questions off (question), 283 on (question), and
    # an unrelated B1 question that must stay out of the table.
    for qid in HARD:
        for t, kind in ((1, "pass"), (2, "fail"), (3, "pass")):
            put(root, MAIN, summary(qid, t, kind, enabled=False, requests=20 + t,
                                    queries=10 + t, seconds=60.0 + t,
                                    **q))  # fmt: skip
    # A swing trial (4) of 184: never in the table.
    put(root, MAIN, summary(Q184, 4, "pass", enabled=False, **q))
    # Trial 3 left no summary (see the module docstring on missing trials).
    for t, kind in ((1, "pass"), (2, "exhausted")):
        put(root, MAIN, summary(Q283, t, kind, enabled=True, requests=30 + t,
                                st=_st(2, 1, 8 + t, 3, 4), queries=8 + t,
                                seconds=90.0, **q))  # fmt: skip
    for t in (1, 2, 3):
        put(root, MAIN, summary(Q275, t, "pass", enabled=False, **q))
    if duplicate:
        put(root, MAIN, summary(Q283, 1, "pass", enabled=False, **q))
    # 283 off (forced).
    f_off = dict(source="forced", mode="off")
    for t, kind in ((1, "fail"), (2, "fail"), (3, "no_report")):
        put(root, OFF, summary(Q283, t, kind, enabled=False, requests=12,
                               queries=5, **f_off))  # fmt: skip
    # Hard questions on (forced); 185 never called the list in trial 2.
    f_on = dict(source="forced", mode="on")
    for qid in HARD:
        if drop_193_on and qid == Q193:
            continue
        for t in (1, 2, 3):
            st = _st(0, 0, 9, 0, 0) if (qid == Q185 and t == 2) else _st(1, 0, 9, 2, 5)
            put(root, ON, summary(qid, t, "pass", enabled=True, st=st, requests=40,
                                  queries=9,
                                  **f_on))  # fmt: skip


def run(root: Path, out: Path, rounds: str = f"{MAIN},{OFF},{ON}") -> int:
    tc = mod("todo_compare")
    return tc.main(
        ["--rounds", rounds, "--out", str(out), "--evidence-root", str(root)]
    )


def rows(out: Path) -> list[dict[str, Any]]:
    doc = json.loads((out / "todo-compare.json").read_text(encoding="utf-8"))
    return doc["rows"] if isinstance(doc, dict) else doc


def on(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    assert value in ("on", "off", "开", "关"), value
    return value in ("on", "开")


def row(rs: list[dict[str, Any]], qid: str, enabled: bool) -> dict[str, Any]:
    (r,) = [r for r in rs if r["qid"] == qid and on(r["todo"]) is enabled]
    return r


def values(col: Any) -> list[Any]:
    return list(col["values"])


@pytest.fixture
def table(tmp_path: Path) -> tuple[int, Path]:
    root, out = tmp_path / "evidence", tmp_path / "out"
    build(root)
    return run(root, out), out


def test_rows_by_question_and_switch(table: tuple[int, Path]) -> None:
    code, out = table
    assert code == 0
    rs = rows(out)
    keys = sorted((r["qid"], on(r["todo"])) for r in rs)
    assert keys == sorted((q, e) for q in (Q283, *HARD) for e in (True, False))
    assert Q275 not in {r["qid"] for r in rs}
    assert row(rs, Q283, True)["round"] == MAIN
    assert row(rs, Q283, False)["round"] == OFF
    for qid in HARD:
        assert row(rs, qid, True)["round"] == ON
        assert row(rs, qid, False)["round"] == MAIN
    assert row(rs, Q283, True)["source"] == "question"
    assert row(rs, Q283, False)["source"] == "forced"
    assert row(rs, Q184, True)["source"] == "forced"
    assert row(rs, Q184, False)["source"] == "question"


def test_passes_follow_trial_record(table: tuple[int, Path]) -> None:
    _, out = table
    rs = rows(out)
    r = row(rs, Q283, True)  # pass, judge exhausted, no summary
    assert r["trials"] == 2
    assert r["passes"] == 1
    assert r["pass_rate"] == pytest.approx(1 / 3)
    r = row(rs, Q283, False)  # fail, fail, no_report
    assert (r["trials"], r["passes"], r["pass_rate"]) == (3, 0, 0)
    r = row(rs, Q184, False)  # pass, fail, pass; trial 4 ignored
    assert (r["trials"], r["passes"]) == (3, 2)
    assert r["pass_rate"] == pytest.approx(2 / 3)
    assert row(rs, Q185, True)["passes"] == 3


def test_per_trial_measures(table: tuple[int, Path]) -> None:
    _, out = table
    rs = rows(out)
    r = row(rs, Q193, False)
    assert values(r["model_requests"]) == [21, 22, 23]
    assert r["model_requests"]["mean"] == pytest.approx(22)
    assert values(r["queries"]) == [11, 12, 13]
    assert r["queries"]["mean"] == pytest.approx(12)
    assert values(r["seconds"]) == [61.0, 62.0, 63.0]
    assert (r["todo_calls_ok"], r["todo_calls_refused"]) == (0, 0)
    assert r["trials_with_todo"] == 0
    assert list(r["last_completed"]) == ["—", "—", "—"]
    on283 = row(rs, Q283, True)
    assert values(on283["model_requests"]) == [31, 32]
    assert values(on283["queries"]) == [9, 10]
    assert (on283["todo_calls_ok"], on283["todo_calls_refused"]) == (4, 2)
    assert on283["trials_with_todo"] == 2
    assert list(on283["last_completed"]) == ["3/4", "3/4"]
    r185 = row(rs, Q185, True)
    assert r185["trials_with_todo"] == 2
    assert r185["todo_calls_ok"] == 2
    assert list(r185["last_completed"]) == ["2/5", "—", "2/5"]


def test_missing_row_is_written_with_zero_trials(tmp_path: Path) -> None:
    root, out = tmp_path / "evidence", tmp_path / "out"
    build(root, drop_193_on=True)
    assert run(root, out) == 0
    r = row(rows(out), Q193, True)
    assert r["trials"] == 0
    assert r["passes"] == 0
    assert r["pass_rate"] == 0


def test_row_in_two_rounds_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, out = tmp_path / "evidence", tmp_path / "out"
    build(root, duplicate=True)  # 283 off in the main round and in OFF
    code = run(root, out)
    captured = capsys.readouterr()
    assert code != 0
    assert "TODO_ROW_DUPLICATE" in captured.out + captured.err


def test_json_and_md_hold_the_same_rows(table: tuple[int, Path]) -> None:
    _, out = table
    md = (out / "todo-compare.md").read_text(encoding="utf-8")
    assert NOTE in md
    for r in rows(out):
        assert r["qid"] in md
    assert "3/4" in md and "—" in md
    dumped = md + (out / "todo-compare.json").read_text(encoding="utf-8")
    assert LEAK not in dumped


def test_no_threshold_all_failing_still_exits_zero(tmp_path: Path) -> None:
    root, out = tmp_path / "evidence", tmp_path / "out"
    kw = dict(source="question", mode="question")
    for qid in (Q283, *HARD):
        for t in (1, 2, 3):
            put(root, MAIN, summary(qid, t, "fail", enabled=qid == Q283, **kw))
    assert run(root, out, MAIN) == 0
    rs = rows(out)
    assert all(r["passes"] == 0 for r in rs)
    assert row(rs, Q280, True)["trials"] == 0  # not run anywhere


def test_module_entry_point(tmp_path: Path) -> None:
    root, out = tmp_path / "evidence", tmp_path / "out"
    build(root)
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.holmes_evals.todo_compare",
         "--rounds", f"{MAIN},{OFF},{ON}", "--out", str(out),
         "--evidence-root", str(root)],
        cwd=REPO, capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    assert (out / "todo-compare.json").is_file()
    assert (out / "todo-compare.md").is_file()
