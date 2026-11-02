"""Reference reports of the v2 capability group (#432, V2-R6): engineer CLI.

Interface: ``~/.agent/handoffs/m3-b3-2026-11-02/seams-432.md`` sections 3-4
and 10.

    .venv/bin/python -m scripts.eval_reference export --round R --attempt A \\
        --output-root ~/.agent/eval-probes/432 --out <scenario_dir>
    .venv/bin/python -m scripts.eval_reference check <scenario_dir> [...]

``export`` reads one probe attempt's summary back through the verifying
loader (the same refs and hashes the round aggregate checks) and freezes
only what its model was actually shown: the delivered views, the authorized
windows and the Run's evidence IDs, as ``reference/evidence.json``; it never
copies the probe model's own report or anything from the answer. ``check``
runs the offline gate on every given scenario's reference (or, for a
scenario whose reference is its probe projection, ``check_expectations``)
and exits 1 when any fails. Offline only: no lab, model or network call.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from opspilot.evaluation import EvaluationCapture
from opspilot.evaluation.codec import decode, parse
from opspilot.evaluation.model import investigator_input, load_scenario_v2
from opspilot.evaluation.reference import (
    EVIDENCE_FILE,
    EVIDENCE_SCHEMA,
    PROJECTION_FILE,
    ReferenceError,
    check_reference,
    check_reference_projection,
    record_json,
    window_json,
)
from opspilot.evaluation.storage import load_summary_capture

ROOT = Path(__file__).resolve().parents[1]


def evidence_document(capture: EvaluationCapture, round_id: str) -> dict[str, Any]:
    """``reference/evidence.json`` of one verified probe capture."""
    attempt = capture.attempt
    trace_url = next((t.url for t in capture.traces if t.url), None)
    delivered = [
        record_json(record)
        for _eid, record in sorted(capture.delivered.items())
        if record.delivered
    ]
    return {
        "schema": EVIDENCE_SCHEMA,
        "scenario_id": capture.spec.scenario_id,
        "probe": {
            "round": round_id,
            "attempt_id": attempt.attempt_id,
            "product_run_id": attempt.product_run_id,
            "manifest_hash": attempt.manifest_hash,
            "trace_url": trace_url,
        },
        "stimulus": dict(investigator_input(capture.spec)),  # type: ignore[arg-type]
        "target_id": capture.spec.stimulus.target_id,
        "authorized": {
            key: window_json(window)
            for key, window in sorted(capture.authorized.items())
        },
        "outcome_evidence_ids": []
        if capture.outcome is None
        else list(capture.outcome.evidence_ids),
        "delivered": delivered,
    }


def projection_document(run_dir: Path) -> dict[str, Any]:
    """Decision D6: ``reference/projection.json`` from a probe attempt's
    public ``projection.json`` and ``intake.json``."""
    projection = json.loads((run_dir / "projection.json").read_text())
    intake_path = run_dir / "intake.json"
    intake = json.loads(intake_path.read_text()) if intake_path.exists() else None
    return {
        "schema": "opspilot-eval-reference-projection-1",
        "probe": {"round": run_dir.parents[1].name, "attempt_id": run_dir.name},
        "projection": projection,
        "intake": intake,
    }


def export(
    round_id: str,
    attempt_id: str,
    *,
    evidence_root: Path,
    attempt_root: Path,
    recording_root: Path,
    out: Path,
    projection: bool = False,
) -> Path:
    """Freeze one probe attempt's reference input under ``out`` (a scenario
    directory); returns the written file. Never overwrites."""
    from scripts.eval_live import attempt_resolver

    run_dir = evidence_root / round_id / "runs" / attempt_id
    spec, _answer = load_scenario_v2(out)
    if projection:
        document = projection_document(run_dir)
        target = out / PROJECTION_FILE
    else:
        path = run_dir / "summary.json"
        stored = decode(EvaluationCapture, parse(path.read_bytes())["capture"])
        _result, capture = load_summary_capture(
            path,
            manifest=stored.manifest,
            resolve=attempt_resolver(
                round_id,
                attempt_id,
                ledger_root=attempt_root,
                recording_root=recording_root,
            ),
        )
        if capture.spec.scenario_id != spec.scenario_id:
            raise ReferenceError("REFERENCE_SCENARIO_MISMATCH")
        document = evidence_document(capture, round_id)
        target = out / EVIDENCE_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x") as stream:
        stream.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
    return target


def check(scenario_dirs: Sequence[Path]) -> tuple[int, list[dict[str, Any]]]:
    results: list[dict[str, Any]] = []
    for directory in scenario_dirs:
        item: dict[str, Any] = {"scenario": directory.name}
        try:
            _spec, answer = load_scenario_v2(directory)
            if (
                answer.reference_report is None
                and (directory / PROJECTION_FILE).exists()
            ):
                expected = check_reference_projection(directory)
                item.update(kind="projection", **expected.as_json())
            else:
                gate = check_reference(directory)
                item.update(
                    kind="report",
                    passed=gate.passed,
                    checks=dict(gate.checks),
                    reasons=list(gate.reasons),
                )
        except ReferenceError as exc:
            item.update(passed=False, reasons=[exc.code])
        except Exception as exc:  # noqa: BLE001 - one fixed code per scenario
            item.update(
                passed=False, reasons=[getattr(exc, "code", type(exc).__name__)]
            )
        results.append(item)
    code = 0 if results and all(r.get("passed") is True for r in results) else 1
    return code, results


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scripts.eval_reference")
    sub = parser.add_subparsers(dest="command", required=True)
    out = sub.add_parser("export")
    out.add_argument("--round", required=True)
    out.add_argument("--attempt", required=True)
    out.add_argument("--output-root", required=True)
    out.add_argument("--recording-root")
    out.add_argument("--out", required=True)
    out.add_argument("--projection", action="store_true")
    chk = sub.add_parser("check")
    chk.add_argument("scenario", nargs="+")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "check":
        code, results = check([Path(p) for p in args.scenario])
        print(json.dumps({"exit_code": code, "results": results}, sort_keys=True))
        return code
    root = Path(args.output_root).expanduser()
    try:
        written = export(
            args.round,
            args.attempt,
            evidence_root=root / "evidence",
            attempt_root=root / "ledgers",
            recording_root=Path(args.recording_root)
            if args.recording_root
            else ROOT / "tmp/eval",
            out=Path(args.out),
            projection=args.projection,
        )
    except (ReferenceError, OSError, ValueError, KeyError) as exc:
        reason: Mapping[str, Any] = {
            "exit_code": 1,
            "reason": getattr(exc, "code", type(exc).__name__),
        }
        print(json.dumps(reason, sort_keys=True))
        return 1
    print(json.dumps({"exit_code": 0, "written": str(written)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
