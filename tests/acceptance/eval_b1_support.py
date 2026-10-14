"""Independent fixtures for the PR-B1 storage contract; no B1 implementation read.

Authority: docs/tasks/2026-10-14-eval-pr-b1-interface.md (cited as I:<line>)
and docs/tasks/2026-10-14-eval-pr-b-contract.md section 4 (C:<line>). The
serializer below is written from the DTO serialization rules (I:64-68); a test
cross-checks it against the writer, so forged files differ from real ones only
in the field a test tampers with.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import fields, is_dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from eval_contract_support import (
    DIMENSIONS,
    EID,
    START,
    calibration_inputs,
    case,
    trace,
)

from opspilot.evaluation import (
    ArtifactRef,
    AttemptJournal,
    CalibrationCapture,
    DimensionValue,
    EvaluationCapture,
    JudgeResult,
    Manifest,
    PlannedSlot,
    RoundCapture,
    TraceRecord,
    aggregate,
    calibrate,
    evaluate,
    manifest_hash,
    write_attempt,
    write_calibration,
    write_round,
    write_summary,
)
from opspilot.investigation.reports import parse_report

try:  # I:8 says the listed names are exported; StorageError sits outside the list.
    from opspilot.evaluation import StorageError
except ImportError:  # pragma: no cover - see EVAL_B1_AMBIGUITIES.md item 1
    from opspilot.evaluation.storage import StorageError

SUMMARY_SCHEMA = "opspilot-eval-summary-1"
ATTEMPT_SCHEMA = "opspilot-eval-attempt-1"
CALIBRATION_SCHEMA = "opspilot-eval-calibration-1"
ROUND_SCHEMA = "opspilot-eval-aggregate-1"

JUDGE_HASH = hashlib.sha256(b"b1-judge-config").hexdigest()
PACK_HASH = hashlib.sha256(b"b1-calibration-pack").hexdigest()
OTHER_HASH = hashlib.sha256(b"something-else").hexdigest()
ROUND_ID = "round-b1"
DELIVERY_NAME = "delivery-ev-cart-visible.json"
RECORDING_NAME = "recording.bin"
FAULT = "contract-fault"
NORMAL = "contract-normal"


# ---------------------------------------------------------------- serialization


def to_json(value: Any) -> Any:
    """DTO -> JSON per I:64: every dataclass field, frozenset sorted, UTC +00:00."""
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_json(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, frozenset):
        return [to_json(item) for item in sorted(value)]
    if isinstance(value, (tuple, list)):
        return [to_json(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): to_json(item) for key, item in value.items()}
    return value


def canonical(doc: Any, *, allow_nan: bool = False) -> bytes:
    """The canonical JSON bytes of I:68."""
    return json.dumps(
        doc,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=allow_nan,
    ).encode("utf-8")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_doc(path: Path) -> dict[str, Any]:
    return json.loads(path.read_bytes())


def write_doc(path: Path, doc: Any, *, allow_nan: bool = False) -> Path:
    path.write_bytes(canonical(doc, allow_nan=allow_nan))
    return path


# ---------------------------------------------------------------- bundle


class Bundle:
    """A package root; the resolver maps ref.path inside it (I:52)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def resolve(self, ref: ArtifactRef) -> bytes:
        return (self.root / ref.path).read_bytes()

    def put(self, name: str, data: bytes, media_type: str = "application/json"):
        (self.root / name).write_bytes(data)
        return ArtifactRef(
            path=name,
            sha256=sha(data),
            bytes=len(data),
            media_type=media_type,
            visibility="private",
        )

    def path(self, name: str) -> Path:
        return self.root / name


def frozen_manifest(**changes: Any) -> Manifest:
    dims = {name: DimensionValue("version", "v1", None) for name in DIMENSIONS}
    dims["judge_config"] = DimensionValue("hash", JUDGE_HASH, None)
    dims["calibration_pack"] = DimensionValue("hash", PACK_HASH, None)
    return replace(Manifest(round_id=ROUND_ID, dimensions=dims), **changes)


def trace_record(attempt_id: str, **entry_changes: Any) -> TraceRecord:
    return TraceRecord(
        entry=trace(**entry_changes),
        attempt_id=attempt_id,
        evaluation_id=attempt_id,
        project="opspilot-eval",
        url=None,
        raw_read_back="found",
        checked_at=START,
    )


def source_trace_record(attempt_id: str, **entry_changes: Any) -> TraceRecord:
    """A source-role trace (I:72): never required, other product Run."""
    changes = {
        "role": "source_run",
        "required": False,
        "run_id": "source-run-0",
        "expected_run_id": "source-run-0",
        "product_run_id": "source-run-0",
    }
    changes.update(entry_changes)
    return trace_record(attempt_id, **changes)


def make_capture(
    bundle: Bundle,
    *,
    fault: bool = True,
    attempt_id: str = "attempt-1",
    repetition: int = 1,
    mode: str = "replay",
    manifest: Manifest | None = None,
    **attempt_changes: Any,
) -> EvaluationCapture:
    """A complete, passing capture whose delivery and recording refs exist."""
    frozen = manifest if manifest is not None else frozen_manifest()
    data = case(fault=fault)
    attempt = replace(
        data["attempt"],
        attempt_id=attempt_id,
        round_id=frozen.round_id,
        manifest_hash=manifest_hash(frozen),
        repetition=repetition,
        mode=mode,
        **attempt_changes,
    )
    delivered = data["delivered"]
    delivery_ref = bundle.put(DELIVERY_NAME, canonical(to_json(delivered[EID])))
    recording_ref = bundle.put(
        RECORDING_NAME, b"\x00opaque-recording\x01", "application/octet-stream"
    )
    judge: JudgeResult = replace(
        data["judge"], config_hash=JUDGE_HASH, calibration_hash=PACK_HASH
    )
    return EvaluationCapture(
        manifest=frozen,
        spec=data["spec"],
        answer=data["answer"],
        attempt=attempt,
        outcome=data["outcome"],
        report=data["report"],
        delivered=delivered,
        authorized=data["authorized"],
        traces=(trace_record(attempt_id), source_trace_record(attempt_id)),
        judge=judge,
        delivery_refs={EID: delivery_ref},
        recording_ref=recording_ref,
        ledger_ref=None,
        source_run_id="source-run-0",
        replay_run_id=attempt.product_run_id,
        started_at=START,
        ended_at=START.replace(minute=4),
        usage=None,
        cost=None,
    )


def evaluate_capture(cap: EvaluationCapture):
    """PR-A evaluate() on the capture's raw inputs (I:72)."""
    return evaluate(
        cap.spec,
        cap.answer,
        cap.attempt,
        cap.outcome,
        cap.report,
        cap.delivered,
        cap.authorized,
        tuple(record.entry for record in cap.traces),
        cap.judge,
    )


def summary_doc(cap: EvaluationCapture, evaluation: Any = None) -> dict[str, Any]:
    """The summary layout of I:58, built from DTOs without the B1 writer."""
    if evaluation is None:
        evaluation = evaluate_capture(cap)
    parsed = None
    report_sha = None
    if cap.report is not None:
        report, _reason = parse_report(
            cap.report.raw, finish_reason=cap.report.finish_reason
        )
        parsed = None if report is None else report.model_dump(mode="json")
        if cap.report.raw is not None:
            report_sha = sha(cap.report.raw.encode("utf-8"))
    return {
        "schema": SUMMARY_SCHEMA,
        "capture": to_json(cap),
        "evaluation": to_json(evaluation),
        "report_parsed": parsed,
        "report_sha256": report_sha,
        "trace": {"entries": [to_json(record) for record in cap.traces]},
    }


def forge_summary(
    bundle: Bundle, name: str, cap: EvaluationCapture, evaluation: Any = None
) -> Path:
    """A summary consistent in every derived field, bypassing writer checks."""
    return write_doc(bundle.path(name), summary_doc(cap, evaluation))


def retarget_delivery(
    bundle: Bundle, cap: EvaluationCapture, record: Any, name: str
) -> EvaluationCapture:
    """Swap the delivered record and its external ref consistently (I:62)."""
    ref = bundle.put(name, canonical(to_json(record)))
    return replace(cap, delivered={EID: record}, delivery_refs={EID: ref})


def tamper(src: Path, dst: Path, mutate: Callable[[dict[str, Any]], None]) -> Path:
    doc = read_doc(src)
    mutate(doc)
    return write_doc(dst, doc, allow_nan=True)


def code_of(excinfo) -> str:
    error = excinfo.value
    assert isinstance(error, StorageError)
    assert isinstance(error, ValueError)
    assert str(error) == error.code
    return error.code


# ---------------------------------------------------------------- calibration


def calibration_capture(*, items=24, negatives=12, flip=0, **changes):
    """Frozen labels and two verdicts per item; ``flip`` false-passes flipped."""
    labels, verdicts = calibration_inputs(items=items, negatives=negatives)
    verdicts = list(verdicts)
    for index in range(flip):
        item, _ = verdicts[index * 2]
        verdicts[index * 2] = (item, True)
    base = CalibrationCapture(
        labels=labels,
        verdicts=tuple(verdicts),
        judge_config_hash=JUDGE_HASH,
        calibration_pack_hash=PACK_HASH,
    )
    return replace(base, **changes)


def recalibrate(cc: CalibrationCapture):
    """Default P7 parameters, hashes bound from the capture (I:73)."""
    return calibrate(
        cc.labels,
        list(cc.verdicts),
        judge_config_hash=cc.judge_config_hash,
        calibration_pack_hash=cc.calibration_pack_hash,
    )


def calibration_doc(cc: CalibrationCapture, result: Any = None) -> dict[str, Any]:
    return {
        "schema": CALIBRATION_SCHEMA,
        "capture": to_json(cc),
        "result": to_json(recalibrate(cc) if result is None else result),
    }


# ---------------------------------------------------------------- round


def journal_for(cap: EvaluationCapture, **attempt_changes: Any) -> AttemptJournal:
    """The pre-setup journal: same frozen inputs, status not yet known."""
    changes = {"status": "unknown", "replay_miss": False, "product_run_id": None}
    changes.update(attempt_changes)
    return AttemptJournal(
        attempt=replace(cap.attempt, **changes),
        spec=cap.spec,
        answer=cap.answer,
        started_at=START,
    )


def unknown_evaluation(journal: AttemptJournal):
    """I:74: started journal without summary scores as an unknown failure."""
    return evaluate(
        journal.spec,
        journal.answer,
        replace(journal.attempt, status="unknown"),
        None,
        None,
        {},
        {},
        (),
        None,
    )


def round_doc(rc: RoundCapture, agg: Any) -> dict[str, Any]:
    return {"schema": ROUND_SCHEMA, "capture": to_json(rc), "aggregate": to_json(agg)}


def build_round(root: Path) -> dict[str, Any]:
    """Seven planned slots covering every denominator rule of I:74.

    contract-fault/replay r1 passes; r2 replay_miss; r3 started without summary;
    r4 frozen started=False; r5 no journal. contract-normal/structural r1
    passes; contract-normal/replay r1 has no journal (zero denominator).
    """
    bundle = Bundle(root)
    frozen = frozen_manifest()
    planned = tuple(
        [PlannedSlot(FAULT, "v1", "replay", rep) for rep in range(1, 6)]
        + [
            PlannedSlot(NORMAL, "v1", "structural", 1),
            PlannedSlot(NORMAL, "v1", "replay", 1),
        ]
    )
    caps = {
        "attempt-1": make_capture(bundle, attempt_id="attempt-1", repetition=1),
        "attempt-2": make_capture(
            bundle, attempt_id="attempt-2", repetition=2, replay_miss=True
        ),
        "attempt-3": make_capture(bundle, attempt_id="attempt-3", repetition=3),
        "attempt-4": make_capture(bundle, attempt_id="attempt-4", repetition=4),
        "attempt-6": make_capture(
            bundle, fault=False, attempt_id="attempt-6", mode="structural"
        ),
    }
    journals = {aid: journal_for(cap) for aid, cap in caps.items()}
    journals["attempt-4"] = journal_for(caps["attempt-4"], started=False)
    journal_refs = {
        aid: write_attempt(bundle.path(f"journal-{aid}.json"), journal)
        for aid, journal in journals.items()
    }
    with_summary = ("attempt-1", "attempt-2", "attempt-6")
    summary_refs = {
        aid: write_summary(bundle.path(f"summary-{aid}.json"), caps[aid])
        for aid in with_summary
    }
    cc = calibration_capture()
    calibration_ref = write_calibration(bundle.path("calibration.json"), cc)
    rc = RoundCapture(
        manifest=frozen,
        planned=planned,
        journals=tuple(journal_refs.values()),
        summaries=summary_refs,
        calibration_ref=calibration_ref,
    )
    evaluations = [evaluate_capture(caps[aid]) for aid in with_summary]
    evaluations.insert(2, unknown_evaluation(journals["attempt-3"]))
    expected = aggregate(frozen, planned, evaluations, recalibrate(cc))
    round_ref = write_round(bundle.path("round.json"), rc, resolve=bundle.resolve)
    return {
        "bundle": bundle,
        "manifest": frozen,
        "planned": planned,
        "caps": caps,
        "journals": journals,
        "journal_refs": journal_refs,
        "summary_refs": summary_refs,
        "calibration": cc,
        "calibration_ref": calibration_ref,
        "round_capture": rc,
        "round_ref": round_ref,
        "round_path": bundle.path("round.json"),
        "expected": expected,
    }
