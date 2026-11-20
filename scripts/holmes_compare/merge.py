"""One table for OpsPilot and Holmes on the same questions (#668).

    python -m scripts.holmes_compare.merge --opspilot-evidence DIR \\
        --holmes-evidence DIR --out-md F --out-json F --opspilot-es-mode external \\
        [--questions a,b]

``--opspilot-es-mode fake`` is refused (``DATA_PLANE_DIFFERS``): both agents
must have run on the same real ES (R8-12). Holmes trials judged on different
judge paths are refused (``HOLMES_JUDGE_PATH_MIXED``).

Both sides are counted with the same rule (``scripts.holmes_evals.scoring``:
trials 1-3, only a judged ``A`` passes; a missing trial, no answer, or a judge
without a verdict scores 0 and stays in the denominator). The two totals are
reported side by side and never combined. Rows carry only counts, flags and
codes; nothing answer-derived or secret is read into them (see ``records``).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from scripts.holmes_compare.records import TrialRow, load_rows
from scripts.holmes_compare.runner import TOOL_FACE_NOTE
from scripts.holmes_evals.questions import (
    B1_QUESTIONS,
    R8_13_SKILL_ASSERTION_DROPPED,
)
from scripts.holmes_evals.scoring import MAIN_TRIALS, score
from scripts.holmes_evals.shift import FIXED_TIME

SCHEMA = "holmes-compare-table-1"
JUDGE_PATH_NOTES = {
    "opspilot": "Holmes 判分口径：默认 opspilot（上游 harness 建的 judge 文本，用 OpsPilot #666 "
    "同一 judge 判分，两家同一 judge 口径）",
    "upstream": "Holmes 判分口径：upstream（可选对照，上游 autoevals 分类器原样，只换 judge "
    "端点与模型；默认口径是 opspilot）",
}
ES_MODES = ("external", "fake")


class MergeRefused(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


AGENTS = ("opspilot", "holmes")
LABELS = {"opspilot": "OpsPilot", "holmes": "Holmes"}
NOTES = (
    TOOL_FACE_NOTE,
    "两家总分分列，不合并、不相减；同一计分规则：每题 3 遍，只有 judge 判 A 算通过，"
    "缺遍、无回答、judge 无结论均记 0 并留在分母。",
    "同模型（DeepSeek deepseek-flash，thinking 开、effort high）、同题、同 judge 接收方与模型"
    "（Z.ai glm-5.3-flashx）。",
    "R8-13：标「去 skill 断言」的题两家都不判 SuggestSkills 建议条数，也不跑 replay。",
    "R10-1：固定时间题两家都按同一平移规则改写数据与题面，送 judge 前反平移；"
    "平移量随各自运行时刻不同。",
)


def _side(rows: Sequence[TrialRow], questions: Sequence[str]) -> dict[str, Any]:
    known = set(questions)
    rows = [r for r in rows if r.qid in known]
    scored = score([r.as_scoring() for r in rows], list(questions))
    by_q: dict[str, dict[int, TrialRow]] = {q: {} for q in questions}
    for r in rows:
        by_q[r.qid][r.trial] = r
    return {"score": scored, "rows": by_q}


def _cell(
    qid: str, scored: Mapping[str, Any], rows: Mapping[int, TrialRow]
) -> dict[str, Any]:
    pq = scored["per_question"][qid]
    main = [rows.get(i) for i in MAIN_TRIALS]
    return {
        "passes": pq["passes"],
        "of": pq["denominator"],
        "rate": pq["rate"],
        "swing": pq["swing"],
        "trials": [
            None if r is None else r.passed and r.category == "ok" for r in main
        ],
        "missing": sum(1 for r in main if r is None or r.category == "env_broken"),
        "no_answer": sum(
            1 for r in main if r is not None and r.category == "no_report"
        ),
        "judge_failed": sum(
            1 for r in main if r is not None and r.category == "judge_exhausted"
        ),
        "shift_applied": any(r is not None and r.shift_applied for r in main),
        "rerun_passes": pq["rerun_passes"],
        "rerun_trials": pq["rerun_trials"],
    }


def merge(
    opspilot: Sequence[TrialRow],
    holmes: Sequence[TrialRow],
    questions: Sequence[str] = B1_QUESTIONS,
) -> dict[str, Any]:
    paths = sorted({r.judge_path for r in holmes if r.judge_path is not None})
    if len(paths) > 1:
        raise MergeRefused("HOLMES_JUDGE_PATH_MIXED")
    judge_path = paths[0] if paths else None
    sides = {"opspilot": _side(opspilot, questions), "holmes": _side(holmes, questions)}
    table = []
    for qid in questions:
        row: dict[str, Any] = {"qid": qid}
        for agent in AGENTS:
            row[agent] = _cell(qid, sides[agent]["score"], sides[agent]["rows"][qid])
        row["skill_assertion_dropped"] = qid in R8_13_SKILL_ASSERTION_DROPPED
        row["fixed_time"] = qid in FIXED_TIME
        table.append(row)
    totals = {
        agent: {
            **sides[agent]["score"]["main"],
            "swing_questions": len(sides[agent]["score"]["swing_questions"]),
            "missing_trials": len(sides[agent]["score"]["missing_trials"]),
        }
        for agent in AGENTS
    }
    return {
        "schema": SCHEMA,
        "questions": len(questions),
        "trials_per_question": len(MAIN_TRIALS),
        "rows": table,
        "totals": totals,
        "totals_merged": False,
        "holmes_judge_path": judge_path,
        "notes": (
            [JUDGE_PATH_NOTES[judge_path]] if judge_path in JUDGE_PATH_NOTES else []
        )
        + list(NOTES),
    }


def _fmt_rate(rate: float | None) -> str:
    return "—" if rate is None else f"{rate:.0%}"


def _flags(cell: Mapping[str, Any]) -> str:
    parts = []
    if cell["swing"]:
        parts.append("摇摆")
    for key, label in (
        ("missing", "缺"),
        ("no_answer", "无答"),
        ("judge_failed", "判分失败"),
    ):
        if cell[key]:
            parts.append(f"{label}{cell[key]}")
    return "、".join(parts)


def to_markdown(result: Mapping[str, Any]) -> str:
    lines = [
        "# HolmesGPT 公开评测集：OpsPilot 与 Holmes 同题同模型对照",
        "",
        f"> {TOOL_FACE_NOTE}",
        *(
            [">", f"> {JUDGE_PATH_NOTES[result['holmes_judge_path']]}"]
            if result.get("holmes_judge_path") in JUDGE_PATH_NOTES
            else []
        ),
        "",
        "| 题 | OpsPilot 通过/3 | 通过率 | 标记 | Holmes 通过/3 | 通过率 | 标记 | 去 skill 断言 | 时间平移（O/H） |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in result["rows"]:
        o, h = row["opspilot"], row["holmes"]
        shift = (
            f"{'是' if o['shift_applied'] else '否'}/{'是' if h['shift_applied'] else '否'}"
            if row["fixed_time"]
            else "—"
        )
        lines.append(
            f"| {row['qid']} | {o['passes']} | {_fmt_rate(o['rate'])} | {_flags(o)} "
            f"| {h['passes']} | {_fmt_rate(h['rate'])} | {_flags(h)} "
            f"| {'是' if row['skill_assertion_dropped'] else ''} | {shift} |"
        )
    lines += ["", "## 总分（两轨分列，不合并）", ""]
    for agent in AGENTS:
        t = result["totals"][agent]
        lines.append(
            f"- {LABELS[agent]}：{t['passes']}/{t['denominator']}（{_fmt_rate(t['rate'])}），"
            f"摇摆 {t['swing_questions']} 题，缺遍 {t['missing_trials']}"
        )
    lines += ["", "## 说明", ""] + [f"- {n}" for n in result["notes"]]
    return "\n".join(lines) + "\n"


def _stand_in_rows(evidence: Path, questions: Sequence[str]) -> list[str]:
    """OpsPilot trials whose ``holmes_eval.data_plane.mode`` is ``fake``."""
    known = set(questions)
    out = []
    for path in sorted(evidence.glob("*/trial-*/summary.json")):
        try:
            block = json.loads(path.read_text()).get("holmes_eval") or {}
        except (OSError, ValueError, AttributeError):
            continue
        plane = block.get("data_plane") if isinstance(block, dict) else None
        if (
            block.get("qid") in known
            and isinstance(plane, dict)
            and plane.get("mode") == "fake"
        ):
            out.append(str(block.get("qid")))
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--opspilot-evidence", type=Path, required=True)
    parser.add_argument("--holmes-evidence", type=Path, required=True)
    parser.add_argument("--out-md", type=Path, required=True)
    parser.add_argument("--out-json", type=Path, required=True)
    parser.add_argument("--questions")
    # The OpsPilot driver's summary does not record its ES mode; the operator
    # states it. Lead ruling: with the fake ES the data planes differ, so the
    # table is refused rather than labelled.
    parser.add_argument("--opspilot-es-mode", choices=ES_MODES, required=True)
    args = parser.parse_args(argv)
    if args.opspilot_es_mode != "external":
        print(json.dumps({"status": "refused", "code": "DATA_PLANE_DIFFERS"}))
        return 2
    questions: Sequence[str] = B1_QUESTIONS
    if args.questions:
        from scripts.holmes_evals.driver import resolve_questions

        questions = resolve_questions(args.questions)
    if _stand_in_rows(args.opspilot_evidence, questions):
        # PR-C: OpsPilot summaries record their data plane; a stand-in
        # backend (fake ES, Loki or VictoriaLogs) is never compared.
        print(json.dumps({"status": "refused", "code": "DATA_PLANE_DIFFERS"}))
        return 2
    try:
        result = merge(
            load_rows(args.opspilot_evidence, "opspilot"),
            load_rows(args.holmes_evidence, "holmes"),
            questions,
        )
    except MergeRefused as exc:
        print(json.dumps({"status": "refused", "code": exc.code}))
        return 2
    args.out_json.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n")
    args.out_md.write_text(to_markdown(result), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
