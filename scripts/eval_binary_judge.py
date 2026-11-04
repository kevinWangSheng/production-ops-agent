"""Q9 binary judge driver (F1 closure S7a): judge, then summarize.

    # 1. Judge every attempt (real calls; keys from ZAI_API_KEY / OPENAI_API_KEY)
    .venv/bin/python -m scripts.eval_binary_judge judge \\
        --dataset evals/datasets/dev-v2 \\
        --dev-round docs/evidence/432-dev-round/summary.json \\
        --evidence-root docs/evidence/eval \\
        [--recording-root <dir of eval-live-<round>-<attempt>/>] \\
        [--holmes <holmes inputs .json>] --k 2 --out tmp/judge/<name>

    # 2. After the user fills <out>/rulings.json
    .venv/bin/python -m scripts.eval_binary_judge summarize \\
        --items tmp/judge/<name>/items.json \\
        --rulings tmp/judge/<name>/rulings.json --out <summary.json>

``judge`` writes ``items.json`` (local: report text and judge rationales),
``disputes.md`` (the user's one-time ruling list) and ``rulings.json`` (a
template, never overwritten). ``summarize`` is offline and pure.

HolmesGPT inputs (``--holmes``), one entry per started attempt::

    {"schema": "opspilot-holmes-judge-inputs-1",
     "attempts": [{"scenario_id": "dv2-...", "attempt_id": "...", "slot": 1,
                   "holmes_json": "<path relative to this file>",
                   "read_only": true}]}

``holmes_json`` is the object ``holmes ask --json-output-file`` wrote, or
``null`` when the attempt started but produced none (counted, vetoed).
``read_only`` is the runner's proxy-log verdict (Q11, HolmesGPT side).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from opspilot.evaluation.binary_judge import (
    HOLMES_INPUTS_SCHEMA,
    BinaryJudgeError,
    CompletionClient,
    HTTPCompletionClient,
    JudgeSubject,
    disputes_markdown,
    holmes_subject,
    items_document,
    judge_config_record,
    judge_subjects,
    load_items,
    load_rulings,
    not_started_subject,
    opspilot_subject,
    provider_configs,
    rulings_template,
    summarize,
)
from opspilot.evaluation.codec import canonical_bytes, decode
from opspilot.evaluation.model import AnswerV2, ScenarioSpecV2, load_scenarios_v2


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise BinaryJudgeError(f"FILE_MISSING:{path.name}") from None
    except (OSError, ValueError):
        raise BinaryJudgeError(f"FILE_INVALID:{path.name}") from None


def _check_recording(capture: Any, recording_root: Path) -> None:
    """The capture's evidence equals what its live recording re-derives."""
    from opspilot.evaluation.live import evidence_from_recording
    from opspilot.evaluation.recording import load_recording

    ref = capture.recording_ref
    if ref is None:
        raise BinaryJudgeError("RECORDING_MISSING")
    attempt = capture.attempt
    directory = recording_root / f"eval-live-{attempt.round_id}-{attempt.attempt_id}"
    recording = load_recording(directory / ref.path, expected_hash=ref.sha256)
    if recording.outcome != capture.outcome:
        raise BinaryJudgeError(f"RECORDING_MISMATCH:{attempt.attempt_id}")
    evidence = evidence_from_recording(recording, outcome=capture.outcome)
    if canonical_bytes(evidence.delivered) != canonical_bytes(
        capture.delivered
    ) or canonical_bytes(evidence.authorized) != canonical_bytes(capture.authorized):
        raise BinaryJudgeError(f"RECORDING_MISMATCH:{attempt.attempt_id}")


def opspilot_subjects(
    dataset: Mapping[str, tuple[ScenarioSpecV2, AnswerV2]],
    dev_round: Mapping[str, Any],
    evidence_root: Path,
    *,
    recording_root: Path | None = None,
) -> list[JudgeSubject]:
    """One subject per slot of a dev-round (or holdout) summary."""
    from opspilot.evaluation.storage import EvaluationCapture

    subjects: list[JudgeSubject] = []
    for slot in dev_round["slots"]:
        sid, attempt_id = slot["scenario_id"], slot["attempt_id"]
        if sid not in dataset:
            raise BinaryJudgeError(f"SCENARIO_UNKNOWN:{sid}")
        _spec, answer = dataset[sid]
        path = evidence_root / slot["round"] / "runs" / attempt_id / "summary.json"
        if slot.get("status") != "ran" or not path.is_file():
            subjects.append(
                not_started_subject(
                    "opspilot",
                    answer,
                    attempt_id=attempt_id,
                    slot=slot["slot"],
                    reason="ATTEMPT_NOT_RAN",
                )
            )
            continue
        capture = decode(EvaluationCapture, _read_json(path)["capture"])
        if (capture.spec.scenario_id, capture.attempt.attempt_id) != (sid, attempt_id):
            raise BinaryJudgeError(f"IDENTITY_MISMATCH:{attempt_id}")
        if canonical_bytes(capture.answer) != canonical_bytes(answer):
            raise BinaryJudgeError(f"ANSWER_DRIFT:{sid}")
        if recording_root is not None and capture.report is not None:
            _check_recording(capture, recording_root)
        subjects.append(opspilot_subject(capture, slot=slot["slot"]))
    return subjects


def holmes_subjects(
    dataset: Mapping[str, tuple[ScenarioSpecV2, AnswerV2]], inputs_path: Path
) -> list[JudgeSubject]:
    doc = _read_json(inputs_path)
    if not isinstance(doc, Mapping) or doc.get("schema") != HOLMES_INPUTS_SCHEMA:
        raise BinaryJudgeError("HOLMES_INPUTS_SCHEMA_INVALID")
    subjects: list[JudgeSubject] = []
    for entry in doc["attempts"]:
        sid = entry["scenario_id"]
        if sid not in dataset:
            raise BinaryJudgeError(f"SCENARIO_UNKNOWN:{sid}")
        spec, answer = dataset[sid]
        if spec.entry != "question":
            # HolmesGPT's denominator holds question-entry scenarios only.
            raise BinaryJudgeError(f"HOLMES_NO_ENTRY:{sid}")
        if type(entry.get("read_only")) is not bool:
            raise BinaryJudgeError("HOLMES_INPUTS_SCHEMA_INVALID")
        rel = entry.get("holmes_json")
        if rel is None:
            subjects.append(
                not_started_subject(
                    "holmesgpt",
                    answer,
                    attempt_id=entry["attempt_id"],
                    slot=entry["slot"],
                    reason="NO_OUTPUT",
                )
            )
            continue
        output = _read_json(inputs_path.parent / rel)
        if not isinstance(output, Mapping):
            raise BinaryJudgeError("HOLMES_OUTPUT_INVALID")
        subjects.append(
            holmes_subject(
                output,
                answer,
                attempt_id=entry["attempt_id"],
                slot=entry["slot"],
                read_only=entry["read_only"],
            )
        )
    return subjects


def _write_new(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "xb") as handle:
        handle.write(data)


def _dump(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    ).encode()


def run_judge(
    args: argparse.Namespace, *, client: CompletionClient, environ: Mapping[str, str]
) -> dict[str, Any]:
    dataset = {
        spec.scenario_id: (spec, answer)
        for spec, answer in load_scenarios_v2(Path(args.dataset))
    }
    subjects: list[JudgeSubject] = []
    if args.dev_round:
        subjects += opspilot_subjects(
            dataset,
            _read_json(Path(args.dev_round)),
            Path(args.evidence_root),
            recording_root=Path(args.recording_root) if args.recording_root else None,
        )
    if args.holmes:
        subjects += holmes_subjects(dataset, Path(args.holmes))
    if not subjects:
        raise BinaryJudgeError("NO_SUBJECTS")
    configs = provider_configs(environ)
    items = judge_subjects(subjects, configs, client)
    out = Path(args.out)
    doc = items_document(items, config=judge_config_record(configs), k=args.k)
    _write_new(out / "items.json", _dump(doc))
    _write_new(out / "disputes.md", disputes_markdown(items).encode())
    rulings = out / "rulings.json"
    if not rulings.exists():
        _write_new(rulings, _dump(rulings_template(items)))
    return {
        "items": len(items),
        "disputes": sum(1 for i in items if i.needs_ruling),
        "out": str(out),
    }


def run_summarize(args: argparse.Namespace) -> dict[str, Any]:
    items, k, config = load_items(_read_json(Path(args.items)))
    rulings_doc = _read_json(Path(args.rulings)) if args.rulings else None
    summary = summarize(
        items, load_rulings(rulings_doc, items), k=k, judge_config=config
    )
    _write_new(Path(args.out), _dump(summary))
    return {"status": summary["status"], "out": args.out}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="eval_binary_judge")
    sub = p.add_subparsers(dest="command", required=True)
    j = sub.add_parser("judge")
    j.add_argument("--dataset", required=True)
    j.add_argument("--dev-round")
    j.add_argument("--evidence-root", default="docs/evidence/eval")
    j.add_argument("--recording-root")
    j.add_argument("--holmes")
    j.add_argument("--k", type=int, required=True)
    j.add_argument("--out", required=True)
    s = sub.add_parser("summarize")
    s.add_argument("--items", required=True)
    s.add_argument("--rulings")
    s.add_argument("--out", required=True)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "judge":
            result = run_judge(args, client=HTTPCompletionClient(), environ=os.environ)
        else:
            result = run_summarize(args)
    except BinaryJudgeError as exc:
        print(json.dumps({"error": exc.code}), file=sys.stderr)
        return 1
    except FileExistsError as exc:
        print(
            json.dumps({"error": "OUTPUT_EXISTS", "path": exc.filename}),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
