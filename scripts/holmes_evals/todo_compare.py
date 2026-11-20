"""C1-U2 task-list comparison table (#565, #666 PR-E).

    python -m scripts.holmes_evals.todo_compare \\
        --rounds <main round>,c1-todo-off-283,c1-todo-on-hard --out DIR \\
        [--evidence-root docs/evidence]

Reads the main trials (1-3) of ``C1_QUESTIONS + TODO_HARD_QUESTIONS`` from
each round's ``*/trial-*/summary.json`` and writes one row per
``(question, task list on/off)``: the main run gives 283 on and the hard
questions off; ``c1-todo-off-283`` gives 283 off; ``c1-todo-on-hard`` the
hard questions on. A row found in two rounds is refused
(``TODO_ROW_DUPLICATE``); a round whose trials of one question disagree
on the switch is refused (``TODO_ROW_MIXED``). Rows are located by round and
question; a missing trial counts in its row and never passes. Pass counts use the driver's ``trial_record`` rule
(R8-3: only a judged A passes; missing and judge-exhausted trials score 0).
No score threshold and no significance test: the table is reported as it
is, on its own, never folded into the data-source tool scores.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from scripts.holmes_evals.questions import C1_QUESTIONS, TODO_HARD_QUESTIONS
from scripts.holmes_evals.scoring import MAIN_TRIALS

SCHEMA = "holmes-evals-todo-compare-1"
QUESTIONS: tuple[str, ...] = C1_QUESTIONS + TODO_HARD_QUESTIONS
NOTES = (
    "调查行为工具（TodoWrite）的开关对照，单独成表，不并入数据源工具的分数（范围原则 1）。",
    "通过：同一 judge、R8-3 口径，只有 judge 判 A 算通过；缺遍与 judge 无结论记 0，分母恒为 3。行按轮名加题号定位；缺遍计入该行遍数，不算通过。",
    "查询数为审计记录的网关查询条数（run.tool_requests），TodoWrite 不经网关、不计入，被拒的重复调用也不计入；模型请求数取 Run 行的 model_requests_used。",
    "耗时为驱动从投递到终态的等待秒数，按约 5 秒轮询，粒度约 5 秒。",
    "每格 3 遍，样本小，只照实报告，不设门槛、不做显著性判断。",
)


class CompareRefused(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code, self.detail = code, detail
        super().__init__(f"{code} {detail}".strip())


def _map(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _mean(values: Sequence[float | None]) -> float | None:
    known = [v for v in values if v is not None]
    return round(sum(known) / len(known), 3) if known else None


def _trial(summary: Mapping[str, Any]) -> dict[str, Any] | None:
    """One main trial of a compared question; a missing trial (no ``todo``
    block) is kept with ``todo`` None: it counts in its row and never passes."""
    from scripts.holmes_evals.driver import trial_record  # noqa: PLC0415 - heavy

    block = _map(summary.get("holmes_eval"))
    if block.get("qid") not in QUESTIONS:
        return None
    if int(block.get("trial") or 0) not in MAIN_TRIALS:
        return None
    todo = _map(block.get("todo"))
    enabled = todo.get("enabled")
    record = trial_record(summary)
    stats = _map(todo.get("stats"))
    last = stats.get("last")
    run = _map(summary.get("run"))
    requests = run.get("tool_requests")
    return {
        "qid": record.qid,
        "trial": record.index,
        "todo": enabled if isinstance(enabled, bool) else None,
        "source": todo.get("source"),
        "missing": summary.get("status") == "missing",
        "passed": record.passed,
        "model_requests": _num(run.get("model_requests_used")),
        # Gateway queries the audit recorded; TodoWrite never reaches the
        # gateway, so both on and off rows count the same way.
        "queries": _num(len(requests)) if isinstance(requests, list) else None,
        "calls_ok": int(stats.get("calls_ok") or 0),
        "calls_refused": int(stats.get("calls_refused") or 0),
        "last": dict(last) if isinstance(last, Mapping) else None,
        "seconds": _num(_map(block.get("run_wait")).get("seconds")),
    }


def collect(evidence_root: Path, rounds: Sequence[str]) -> list[dict[str, Any]]:
    """Every main trial of the five questions, tagged with its round."""
    from scripts.holmes_evals.driver import EVIDENCE_PREFIX

    out = []
    for name in rounds:
        directory = evidence_root / f"{EVIDENCE_PREFIX}{name}"
        for path in sorted(directory.glob("*/trial-*/summary.json")):
            try:
                summary = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(summary, Mapping):
                continue
            row = _trial(summary)
            if row is not None:
                out.append({**row, "round": name})
    return out


def _row(
    qid: str, round_: str | None, enabled: bool | None, members: list[Mapping[str, Any]]
) -> dict[str, Any]:
    members = sorted(members, key=lambda t: t["trial"])
    present = [t for t in members if not t["missing"]]
    passes = sum(1 for t in members if t["passed"])
    requests = [t["model_requests"] for t in present]
    queries = [t["queries"] for t in present]
    seconds = [t["seconds"] for t in present]
    sources = [t["source"] for t in present if t["source"]]
    return {
        "qid": qid,
        "todo": enabled,
        "round": round_,
        "source": sources[0] if sources else None,
        "trials": len(members),
        "missing": len(members) - len(present),
        "passes": passes,
        "pass_rate": passes / len(MAIN_TRIALS),
        "model_requests": requests,
        "model_requests_mean": _mean(requests),
        "queries": queries,
        "queries_mean": _mean(queries),
        "todo_calls_ok": sum(t["calls_ok"] for t in present),
        "todo_calls_refused": sum(t["calls_refused"] for t in present),
        "trials_with_todo": sum(1 for t in present if t["calls_ok"] > 0),
        "last_completed": [
            None
            if t["last"] is None
            else f"{t['last'].get('completed', 0)}/{t['last'].get('total', 0)}"
            for t in present
        ],
        "seconds": seconds,
        "seconds_mean": _mean(seconds),
    }


def table(trials: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Rows located by round and question (lead ruling 2026-11-20); a
    missing trial counts in its row's ``trials`` and never passes."""
    order: list[str] = []
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for t in trials:
        key = (str(t["round"]), str(t["qid"]))
        if key[0] not in order:
            order.append(key[0])
        groups.setdefault(key, []).append(t)
    rows = []
    seen: dict[tuple[str, bool], str] = {}
    for qid in QUESTIONS:
        for round_ in order:
            members = groups.get((round_, qid))
            if not members:
                continue
            states = {t["todo"] for t in members if t["todo"] is not None}
            if len(states) > 1:
                raise CompareRefused("TODO_ROW_MIXED", f"{round_} {qid}")
            enabled = next(iter(states)) if states else None
            if enabled is not None:
                if (qid, enabled) in seen:
                    raise CompareRefused(
                        "TODO_ROW_DUPLICATE",
                        f"{qid} todo={enabled}: {seen[(qid, enabled)]}, {round_}",
                    )
                seen[(qid, enabled)] = round_
            rows.append(_row(qid, round_, enabled, members))
        for enabled in (True, False):
            if (qid, enabled) not in seen:
                # An expected cell no round produced: shown, never filled in.
                rows.append(_row(qid, None, enabled, []))
    return {
        "schema": SCHEMA,
        "questions": list(QUESTIONS),
        "trials_per_cell": len(MAIN_TRIALS),
        "rows": rows,
        "notes": list(NOTES),
    }


def _cell(values: Sequence[Any]) -> str:
    return " / ".join("—" if v is None else f"{v:g}" if isinstance(v, float) else str(v)
                      for v in values) or "—"  # fmt: skip


def _switch(value: bool | None) -> str:
    return "—" if value is None else "开" if value else "关"


def _mean_text(value: float | None) -> str:
    return "—" if value is None else f"{value:g}"


def to_markdown(result: Mapping[str, Any]) -> str:
    lines = [
        "# TodoWrite 开关对照（C1-U2）",
        "",
        "| 题 | TodoWrite | 轮 | 遍数 | 通过/3 | 模型请求数（各遍；均值） | 查询数（各遍；均值）"
        " | 清单成功/被拒 | 调用过的遍数 | 最后清单完成数 | 耗时秒（各遍；均值） |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in result["rows"]:
        lines.append(
            f"| {r['qid']} | {_switch(r['todo'])} | {r['round'] or '—'} "
            f"| {r['trials']}（缺 {r['missing']}） | {r['passes']} "
            f"| {_cell(r['model_requests'])}；{_mean_text(r['model_requests_mean'])} "
            f"| {_cell(r['queries'])}；{_mean_text(r['queries_mean'])} "
            f"| {r['todo_calls_ok']}/{r['todo_calls_refused']} | {r['trials_with_todo']} "
            f"| {_cell(r['last_completed'])} "
            f"| {_cell(r['seconds'])}；{_mean_text(r['seconds_mean'])} |"
        )
    lines += ["", "## 说明", ""] + [f"- {n}" for n in result["notes"]]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rounds", required=True, help="comma-separated round names")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path)
    args = parser.parse_args(argv)
    rounds = [r.strip() for r in args.rounds.split(",") if r.strip()]
    from scripts.holmes_evals.driver import EVIDENCE_ROOT

    try:
        result = table(collect(args.evidence_root or EVIDENCE_ROOT, rounds))
    except CompareRefused as error:
        print(
            json.dumps(
                {"status": "refused", "code": error.code, "detail": error.detail}
            )
        )
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "todo-compare.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    (args.out / "todo-compare.md").write_text(to_markdown(result), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
