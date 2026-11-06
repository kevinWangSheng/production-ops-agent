"""F1 comparison scoring (S7a): Q9 binary judges, Q11 veto, rulings, summary.

The contract entry points come from :mod:`opspilot.evaluation.binary_judge`:
``JUDGES``, ``judge_applicable``, ``judge_messages``,
``project_opspilot_report``, ``project_holmes_output``,
``root_cause_citation`` and ``summarize``. This module adds the driver.

    # 1. Judge every attempt (real calls; keys: ZAI_API_KEY or GLM_API_KEY,
    #    OPENAI_API_KEY)
    .venv/bin/python -m scripts.eval_compare_scoring judge \\
        --dataset evals/datasets/dev-v2 --k 2 --out tmp/judge/<name> \\
        [--opspilot-round docs/evidence/432-dev-round/summary.json \\
         --evidence-root docs/evidence/eval [--recording-root <dir>]] \\
        [--holmes-round <holmes round summary.json> --holmes-attempts <dir>] \\
        [--schedule-log <paired log.json> --schedule-check <check output.json>]

    # 2. Once the user has filled <out>/rulings.json
    .venv/bin/python -m scripts.eval_compare_scoring summarize \\
        --items tmp/judge/<name>/items.json \\
        --rulings tmp/judge/<name>/rulings.json --out <summary.json>

``judge`` writes ``items.json`` (local only: it holds report text and judge
rationales), ``disputes.md`` (the user's one-time ruling list) and
``rulings.json`` (a template that is never overwritten). ``summarize`` runs
offline and is pure; its output holds no report text.

OpsPilot attempts come from a round summary (``scripts.eval_dev_round``)
and each attempt's public evidence. Their checks are the round's re-checked
Q20 expectations (``q20.*``). Where the judge applies, the deterministic
gate is recomputed and added as ``gate.*``. HolmesGPT attempts come from
the ``scripts.holmes_baseline.dataset`` round summary (schema
``opspilot-holmes-dataset-round-1``, #470). Only slots in the denominator
are scored. Each slot's ``raw`` names its ``holmes.json`` under
``--holmes-attempts``; only that file's ``result`` reaches a judge. Their
checks are ``final_state`` (the terminal state is in the answer's allowed
set), the slot's own ``read_only``, and, where the judge applies,
``report``. Never-started slots on either side are counted, as failures,
under synthetic ids.

With ``--schedule-log`` and ``--schedule-check`` (#474 ``eval_pair_schedule``
log and its ``check`` output), registered voided attempts are left out of
judging, rulings and every denominator, and listed as ``voided_attempts``.
An exam run in segments repeats the pair, one per segment, matched in
order (each check reads only its own log); ``--voids`` adds the
cross-segment registry (``scripts.eval_dev_round.VOIDS_SCHEMA``) of both
systems.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Any

from opspilot.evaluation.binary_judge import (
    JUDGES,
    BinaryJudgeError,
    CompletionClient,
    HTTPCompletionClient,
    disputed,
    disputes_markdown,
    judge_applicable,
    judge_config_record,
    judge_messages,
    judge_once,
    load_rulings,
    project_holmes_output,
    project_opspilot_report,
    provider_configs,
    root_cause_citation,
    rulings_template,
    summarize,
    vetoed,
)
from opspilot.evaluation.codec import canonical_bytes, decode
from opspilot.evaluation.gate import parsed_report, run_gate, run_scope
from opspilot.evaluation.model import AnswerV2, ScenarioSpecV2, load_scenarios_v2

__all__ = [
    "JUDGES",
    "judge_applicable",
    "judge_messages",
    "project_holmes_output",
    "project_opspilot_report",
    "root_cause_citation",
    "summarize",
]

ITEMS_SCHEMA = "opspilot-compare-scoring-items-1"
SUMMARY_SCHEMA = "opspilot-compare-scoring-summary-1"
HOLMES_ROUND_SCHEMA = "opspilot-holmes-dataset-round-1"
PAIRED_LOG_SCHEMA = "opspilot-eval-paired-log-1"

Dataset = Mapping[str, tuple[ScenarioSpecV2, AnswerV2]]


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise BinaryJudgeError(f"FILE_MISSING:{path.name}") from None
    except (OSError, ValueError):
        raise BinaryJudgeError(f"FILE_INVALID:{path.name}") from None


def _base(system: str, answer: AnswerV2, attempt_id: str, slot: int) -> dict[str, Any]:
    return {
        "attempt_id": attempt_id,
        "system": system,
        "scenario_id": answer.scenario_id,
        "slot": slot,
        "category": answer.category,
        "polarity": answer.polarity,
        "expected_elements": list(answer.expected_output),
        "judge_applicable": judge_applicable(answer),
        "verdicts": {},
        "checks": {},
        "report_text": None,
    }


def _check_recording(capture: Any, recording_root: Path) -> None:
    """The capture's evidence equals what its live recording re-derives."""
    from opspilot.evaluation.live import evidence_from_recording
    from opspilot.evaluation.recording import load_recording

    ref, attempt = capture.recording_ref, capture.attempt
    if ref is None:
        raise BinaryJudgeError(f"RECORDING_MISSING:{attempt.attempt_id}")
    directory = recording_root / f"eval-live-{attempt.round_id}-{attempt.attempt_id}"
    recording = load_recording(directory / ref.path, expected_hash=ref.sha256)
    evidence = evidence_from_recording(recording, outcome=capture.outcome)
    if (
        recording.outcome != capture.outcome
        or canonical_bytes(evidence.delivered) != canonical_bytes(capture.delivered)
        or canonical_bytes(evidence.authorized) != canonical_bytes(capture.authorized)
    ):
        raise BinaryJudgeError(f"RECORDING_MISMATCH:{attempt.attempt_id}")


def opspilot_attempts(
    dataset: Dataset,
    round_summary: Mapping[str, Any],
    evidence_root: Path,
    *,
    recording_root: Path | None = None,
) -> list[dict[str, Any]]:
    from opspilot.evaluation.storage import EvaluationCapture

    out: list[dict[str, Any]] = []
    for slot in round_summary["slots"]:
        sid = slot["scenario_id"]
        if sid not in dataset:
            raise BinaryJudgeError(f"SCENARIO_UNKNOWN:{sid}")
        spec, answer = dataset[sid]
        if slot.get("status") == "not_started" or slot.get("attempt_id") is None:
            # eval_dev_round's never-started slot (attempt_id None, empty
            # checks): counted, failed, under a synthetic id.
            item = _base(
                "opspilot",
                answer,
                f"opspilot-not-started-{sid}-{slot['slot']}",
                slot["slot"],
            )
            item["checks"] = {"attempt_ran": False}
            item["root_cause_citation"] = None if answer.signature is None else False
            out.append(item)
            continue
        attempt_id = slot["attempt_id"]
        item = _base("opspilot", answer, attempt_id, slot["slot"])
        expectations = slot.get("expectations") or {}
        item["checks"] = {
            f"q20.{name}": value
            for name, value in (expectations.get("checks") or {}).items()
        }
        if not item["checks"]:
            raise BinaryJudgeError(f"EXPECTATIONS_MISSING:{attempt_id}")
        path = evidence_root / slot["round"] / "runs" / attempt_id / "summary.json"
        if slot.get("status") != "ran" or not path.is_file():
            item["checks"]["attempt_ran"] = False
            item["root_cause_citation"] = None if answer.signature is None else False
            out.append(item)
            continue
        capture = decode(EvaluationCapture, _read_json(path)["capture"])
        if (capture.spec.scenario_id, capture.attempt.attempt_id) != (sid, attempt_id):
            raise BinaryJudgeError(f"IDENTITY_MISMATCH:{attempt_id}")
        if canonical_bytes(capture.answer) != canonical_bytes(answer):
            raise BinaryJudgeError(f"ANSWER_DRIFT:{sid}")
        if recording_root is not None and capture.report is not None:
            _check_recording(capture, recording_root)
        parsed = parsed_report(capture.report)
        if item["judge_applicable"]:
            gate = run_gate(
                spec,
                answer,
                capture.outcome,
                capture.report,
                capture.delivered,
                capture.authorized,
            )
            item["checks"] |= {f"gate.{k}": v for k, v in gate.checks.items()}
            item["gate_reasons"] = list(gate.reasons)
            if parsed is not None:
                item["report_text"] = project_opspilot_report(parsed)
        item["root_cause_citation"] = root_cause_citation(
            answer,
            parsed,
            capture.delivered,
            capture.authorized,
            run_scope(spec, capture.outcome),
        )
        out.append(item)
    return out


#: ``scripts.holmes_baseline.dataset.RAW_FILE``, relative to the attempt
HOLMES_RAW_FILE = ("out", "holmes.json")


def _holmes_text(attempts_root: Path, rel: str, attempt_id: str) -> str | None:
    """The ``result`` of the attempt's raw ``holmes.json``, after checking
    that the file stays under the attempts root and matches the hash its own
    attempt summary recorded. Nothing else of the raw output is used."""
    root = attempts_root.resolve()
    path = (attempts_root / rel).resolve()
    parts = path.relative_to(root).parts if path.is_relative_to(root) else ()
    # the attempt directory holds the raw file at HOLMES_RAW_FILE; it may sit
    # under a round directory (<attempts>/holmes-<round>/<attempt>/...)
    n = len(HOLMES_RAW_FILE)
    if len(parts) <= n or parts[-n:] != HOLMES_RAW_FILE:
        raise BinaryJudgeError(f"HOLMES_RAW_OUTSIDE_ROOT:{attempt_id}")
    directory = root.joinpath(*parts[:-n])
    attempt = _read_json(directory / "summary.json")
    expected = (
        (attempt.get("raw") or {}).get("sha256")
        if isinstance(attempt, Mapping)
        else None
    )
    if (
        not path.is_file()
        or attempt.get("attempt_id") != attempt_id
        or hashlib.sha256(path.read_bytes()).hexdigest() != expected
    ):
        raise BinaryJudgeError(f"HOLMES_RAW_MISMATCH:{attempt_id}")
    output = _read_json(path)
    if not isinstance(output, Mapping):
        raise BinaryJudgeError(f"HOLMES_RAW_INVALID:{attempt_id}")
    return project_holmes_output(output)


def holmes_attempts(
    dataset: Dataset, round_summary: Mapping[str, Any], attempts_root: Path
) -> list[dict[str, Any]]:
    """Slots of ``scripts.holmes_baseline.dataset summarize`` (#470).

    Checks: ``read_only`` from the slot's own Q11 check (a ran slot without
    a boolean value fails; never started: ``None``, not applicable); ``final_state`` recomputed as the slot's
    terminal state in the answer's allowed set; where the judge applies,
    ``report``: the final answer (``result``) is non-blank.
    """
    if round_summary.get("schema") != HOLMES_ROUND_SCHEMA:
        raise BinaryJudgeError("HOLMES_ROUND_SCHEMA_INVALID")
    out: list[dict[str, Any]] = []
    for slot in round_summary["slots"]:
        if not slot.get("in_denominator"):
            continue  # alert entry: no upstream entry, outside every denominator
        sid = slot["scenario_id"]
        if sid not in dataset:
            raise BinaryJudgeError(f"SCENARIO_UNKNOWN:{sid}")
        _spec, answer = dataset[sid]
        ran = slot.get("status") == "ran" and slot.get("attempt_id") is not None
        attempt_id = (
            slot["attempt_id"] if ran else f"holmes-not-started-{sid}-{slot['slot']}"
        )
        item = _base("holmes", answer, attempt_id, slot["slot"])
        read_only = (slot.get("checks") or {}).get("read_only")
        item["checks"] = {
            "final_state": ran
            and slot.get("final_state") in answer.expected_outcome.final_states,
            # A ran slot without a boolean read_only fails closed; only a
            # never-started slot has it not applicable.
            "read_only": (read_only is True) if ran else None,
        }
        if ran and slot.get("raw"):
            item["report_text"] = _holmes_text(attempts_root, slot["raw"], attempt_id)
        if item["judge_applicable"]:
            item["checks"]["report"] = item["report_text"] is not None
        out.append(item)
    return out


def voided_attempts(log: Mapping[str, Any], check: Mapping[str, Any]) -> list[str]:
    """Attempt ids of registered voids (#474 ``eval_pair_schedule check``).

    ``check["voided"]`` names each voided window and the systems its rerun
    replaced; the attempt ids come from that window's record in the log.
    """
    if log.get("schema") != PAIRED_LOG_SCHEMA:
        raise BinaryJudgeError("SCHEDULE_LOG_SCHEMA_INVALID")
    records = {
        str(r.get("window_id")): r
        for r in log.get("windows") or []
        if isinstance(r, Mapping)
    }
    ids: list[str] = []
    for entry in check.get("voided") or []:
        record = records.get(str(entry.get("window_id")))
        if record is None:
            raise BinaryJudgeError(f"VOIDED_WINDOW_UNKNOWN:{entry.get('window_id')}")
        systems = set(entry.get("systems") or ())
        for slot in record.get("slots") or []:
            if slot.get("system") in systems and slot.get("attempt_id"):
                ids.append(str(slot["attempt_id"]))
    return sorted(set(ids))


def registry_voided_attempts(
    registry: Any,
    attempts: Sequence[Mapping[str, Any]],
    checks: Sequence[Mapping[str, Any]],
    scenarios: Collection[str],
) -> list[str]:
    """Attempt ids of a cross-segment void registry, both systems.

    The registry only discloses voids the round summaries already applied
    (``eval_dev_round --voids``, ``holmes_baseline.dataset --voided``): a
    void that names a scored attempt is ``VOID_SCORED``. Each entry's
    replacement chain (through the registry and the checks'
    ``voided_attempts``) must end, without a loop, at a scored attempt of
    the entry's own system and scenario (``VOID_RERUN_UNKNOWN``,
    ``VOID_CYCLE``). Entries of scenarios outside this dataset are skipped.
    """
    from scripts.eval_dev_round import void_registry_entries

    try:
        entries = [
            e for e in void_registry_entries(registry) if e["scenario_id"] in scenarios
        ]
    except ValueError:
        raise BinaryJudgeError("VOIDS_INVALID") from None
    scored = {str(a["attempt_id"]): a for a in attempts}
    successor: dict[str, str] = {}
    for check in checks:
        for row in check.get("voided_attempts") or []:
            if isinstance(row, Mapping) and row.get("voided_attempt_id"):
                if row.get("rerun_attempt_id"):
                    successor[str(row["voided_attempt_id"])] = str(
                        row["rerun_attempt_id"]
                    )
    for entry in entries:
        successor[entry["voided_attempt_id"]] = entry["rerun_attempt_id"]
    for entry in entries:
        voided = entry["voided_attempt_id"]
        if voided in scored:
            raise BinaryJudgeError(f"VOID_SCORED:{voided}")
        seen = {voided}
        current = entry["rerun_attempt_id"]
        while current in successor:
            if current in seen:
                raise BinaryJudgeError(f"VOID_CYCLE:{voided}")
            seen.add(current)
            current = successor[current]
        final = scored.get(current)
        if (
            final is None
            or final["system"] != entry["system"]
            or final["scenario_id"] != entry["scenario_id"]
        ):
            raise BinaryJudgeError(f"VOID_RERUN_UNKNOWN:{voided}")
    return sorted({e["voided_attempt_id"] for e in entries})


def judge_attempts(
    attempts: Sequence[dict[str, Any]],
    dataset: Dataset,
    client: CompletionClient,
    environ: Mapping[str, str],
    *,
    voided: Collection[str] = (),
) -> None:
    """Fill ``verdicts``. Vetoed, deterministic-only or voided attempts are
    never sent."""
    configs = provider_configs(environ)
    for item in attempts:
        if not item["judge_applicable"] or vetoed(item) or item["attempt_id"] in voided:
            continue
        if item["report_text"] is None:
            raise BinaryJudgeError(f"NO_REPORT_TEXT:{item['attempt_id']}")
        _spec, answer = dataset[item["scenario_id"]]
        messages = judge_messages(answer, item["report_text"])
        item["prompt_sha256"] = hashlib.sha256(
            json.dumps(messages, sort_keys=True).encode()
        ).hexdigest()
        item["rationales"], item["errors"], item["judge_usage"] = {}, {}, {}
        for config in configs:
            verdict = judge_once(messages, config, client)
            item["verdicts"][config.name] = verdict.passed
            item["rationales"][config.name] = verdict.rationale
            item["errors"][config.name] = verdict.code
            item["judge_usage"][config.name] = {
                "response_model": verdict.response_model,
                "attempts": verdict.attempts,
                **verdict.usage,
            }


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
    attempts: list[dict[str, Any]] = []
    if args.opspilot_round:
        attempts += opspilot_attempts(
            dataset,
            _read_json(Path(args.opspilot_round)),
            Path(args.evidence_root),
            recording_root=Path(args.recording_root) if args.recording_root else None,
        )
    if args.holmes_round:
        if not args.holmes_attempts:
            raise BinaryJudgeError("HOLMES_ATTEMPTS_REQUIRED")
        attempts += holmes_attempts(
            dataset, _read_json(Path(args.holmes_round)), Path(args.holmes_attempts)
        )
    if not attempts:
        raise BinaryJudgeError("NO_ATTEMPTS")
    ids = [a["attempt_id"] for a in attempts]
    if len(set(ids)) != len(ids):
        raise BinaryJudgeError("ATTEMPT_DUPLICATE")
    logs, checks = args.schedule_log or [], args.schedule_check or []
    if len(logs) != len(checks):
        raise BinaryJudgeError("SCHEDULE_LOG_AND_CHECK_REQUIRED")
    voided_ids: set[str] = set()
    check_docs = [_read_json(Path(path)) for path in checks]
    for log_path, check_doc in zip(logs, check_docs, strict=True):
        voided_ids.update(voided_attempts(_read_json(Path(log_path)), check_doc))
    if args.voids:
        voided_ids.update(
            registry_voided_attempts(
                _read_json(Path(args.voids)), attempts, check_docs, set(dataset)
            )
        )
    voided = sorted(voided_ids)
    judge_attempts(attempts, dataset, client, environ, voided=set(voided))
    live = [a for a in attempts if a["attempt_id"] not in voided]
    out = Path(args.out)
    doc = {
        "schema": ITEMS_SCHEMA,
        "k": args.k,
        "judge_config": judge_config_record(),
        "voided": voided,
        "attempts": attempts,
    }
    _write_new(out / "items.json", _dump(doc))
    _write_new(out / "disputes.md", disputes_markdown(live).encode())
    if not (out / "rulings.json").exists():
        _write_new(out / "rulings.json", _dump(rulings_template(live)))
    return {
        "attempts": len(live),
        "voided": len([a for a in attempts if a["attempt_id"] in voided]),
        "disputes": sum(1 for a in live if disputed(a)),
        "out": str(out),
    }


def run_summarize(args: argparse.Namespace) -> dict[str, Any]:
    doc = _read_json(Path(args.items))
    if not isinstance(doc, Mapping) or doc.get("schema") != ITEMS_SCHEMA:
        raise BinaryJudgeError("ITEMS_SCHEMA_INVALID")
    attempts = doc["attempts"]
    voided = set(doc.get("voided") or ())
    live = [a for a in attempts if a["attempt_id"] not in voided]
    rulings = load_rulings(
        _read_json(Path(args.rulings)) if args.rulings else None, live
    )
    scored = summarize(attempts, rulings, k=int(doc["k"]), voided=voided)
    summary = {
        "schema": SUMMARY_SCHEMA,
        "method": "uncalibrated two-vendor binary judge + user ruling on "
        "disagreements; Q11 deterministic checks veto first",
        "judge_config": doc["judge_config"],
        "k": doc["k"],
        "ruled_by_user": sorted(rulings),
        **scored,
    }
    _write_new(Path(args.out), _dump(summary))
    return {"final": summary["final"], "out": args.out}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="eval_compare_scoring")
    sub = p.add_subparsers(dest="command", required=True)
    j = sub.add_parser("judge")
    j.add_argument("--dataset", required=True)
    j.add_argument("--k", type=int, required=True)
    j.add_argument("--out", required=True)
    j.add_argument("--opspilot-round")
    j.add_argument("--evidence-root", default="docs/evidence/eval")
    j.add_argument("--recording-root")
    j.add_argument("--holmes-round")
    j.add_argument("--holmes-attempts")
    # repeated once per exam segment, logs and checks matched in order
    j.add_argument("--schedule-log", action="append")
    j.add_argument("--schedule-check", action="append")
    j.add_argument("--voids")
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
