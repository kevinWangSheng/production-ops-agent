"""Versioned, fail-closed offline storage (PR-B1, P10/P12/P13)."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast

from opspilot.acceptance import IncidentOutcome
from opspilot.evaluation.codec import (
    StorageError as StorageError,
)
from opspilot.evaluation.codec import (
    canonical_bytes,
    decode,
    exact,
    invalid,
    parse,
    validated,
)
from opspilot.evaluation.gate import parsed_report
from opspilot.evaluation.model import (
    Answer,
    AttemptRecord,
    DeliveredRecord,
    JudgeResult,
    ReportCapture,
    RunEvaluation,
    ScenarioError,
    ScenarioSpec,
    TraceEntry,
    Window,
    canonical_view_hash,
    load_scenarios,
)
from opspilot.evaluation.rounds import (
    Aggregate,
    AggregateError,
    CalibrationError,
    CalibrationResult,
    Manifest,
    ManifestError,
    PlannedSlot,
    aggregate,
    calibrate,
    manifest_hash,
    validate_manifest,
)
from opspilot.evaluation.runs import evaluate
from opspilot.investigation.reports import ClaimV2, ReportV2


@dataclass(frozen=True)
class ArtifactRef:
    path: str
    sha256: str
    bytes: int
    media_type: str
    visibility: Literal["private", "public"]


Resolver = Callable[[ArtifactRef], bytes]


@dataclass(frozen=True)
class TraceRecord:
    entry: TraceEntry
    attempt_id: str
    evaluation_id: str
    project: str | None
    url: str | None
    raw_read_back: str | None
    checked_at: datetime | None


@dataclass(frozen=True)
class EvaluationCapture:
    manifest: Manifest
    spec: ScenarioSpec
    answer: Answer
    attempt: AttemptRecord
    outcome: IncidentOutcome | None
    report: ReportCapture | None
    delivered: Mapping[str, DeliveredRecord]
    authorized: Mapping[str, Window]
    traces: tuple[TraceRecord, ...]
    judge: JudgeResult | None
    delivery_refs: Mapping[str, ArtifactRef]
    recording_ref: ArtifactRef | None = None
    ledger_ref: ArtifactRef | None = None
    source_run_id: str | None = None
    replay_run_id: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    usage: Mapping[str, Any] | None = None
    cost: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class AttemptJournal:
    attempt: AttemptRecord
    spec: ScenarioSpec
    answer: Answer
    started_at: datetime | None = None


@dataclass(frozen=True)
class CalibrationCapture:
    labels: Mapping[str, bool]
    verdicts: tuple[tuple[str, bool], ...]
    judge_config_hash: str
    calibration_pack_hash: str


@dataclass(frozen=True)
class RoundCapture:
    manifest: Manifest
    planned: tuple[PlannedSlot, ...]
    journals: tuple[ArtifactRef, ...]
    summaries: Mapping[str, ArtifactRef]
    calibration_ref: ArtifactRef | None = None


@dataclass(frozen=True)
class LegacySummary:
    payload: Mapping[str, Any]
    sha256: str
    trace_entries: tuple[Mapping[str, Any], ...]
    verified: bool = False


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _ref(ref: ArtifactRef) -> None:
    validated(ArtifactRef, ref)
    parts = ref.path.split("/")
    if (
        not ref.path
        or "\\" in ref.path
        or "\x00" in ref.path
        or any(part in ("", ".", "..") for part in parts)
        or not re.fullmatch(r"[0-9a-f]{64}", ref.sha256)
        or ref.bytes < 0
        or not ref.media_type.strip()
    ):
        invalid()


def _resolve(ref: ArtifactRef, resolve: Resolver) -> bytes:
    _ref(ref)
    try:
        raw = resolve(ref)
    except Exception:
        raw = None
    if type(raw) is not bytes:
        raise StorageError("EVIDENCE_UNAVAILABLE")
    if len(raw) != ref.bytes or _sha(raw) != ref.sha256:
        raise StorageError("HASH_MISMATCH")
    return raw


def _parent(path: Path) -> tuple[int, str]:
    """Walk using directory fds so a symlink swap cannot redirect IO."""
    absolute = path.absolute()
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in absolute.parts[1:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = child
        return descriptor, absolute.name
    except OSError:
        pass
    _close(descriptor)
    raise StorageError("EVIDENCE_UNAVAILABLE") from None


def _close(descriptor: int) -> bool:
    try:
        os.close(descriptor)
    except OSError:
        return False
    return True


def _read(path: Path) -> bytes:
    parent, name = _parent(path)
    raw = None
    try:
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
        with os.fdopen(descriptor, "rb") as file:
            if stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                raw = file.read()
    except OSError:
        pass
    finally:
        if not _close(parent):
            raw = None
    if raw is None:
        raise StorageError("EVIDENCE_UNAVAILABLE") from None
    return raw


def _publish(path: Path, raw: bytes, media_type: str) -> ArtifactRef:
    parent, name = _parent(path)
    temporary = f".eval-{secrets.token_hex(16)}.tmp"
    created = False
    failure: str | None = None
    try:
        # Report existing targets consistently, even non-regular/symlink ones.
        try:
            os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            failure = "OUTPUT_EXISTS"
        if failure is None:
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent
            )
            created = True
            with os.fdopen(descriptor, "wb") as file:
                file.write(raw)
                file.flush()
                os.fsync(file.fileno())
            os.link(
                temporary,
                name,
                src_dir_fd=parent,
                dst_dir_fd=parent,
                follow_symlinks=False,
            )
            os.fsync(parent)
    except FileExistsError:
        failure = "OUTPUT_EXISTS"
    except OSError:
        failure = "EVIDENCE_UNAVAILABLE"
    finally:
        try:
            if created:
                os.unlink(temporary, dir_fd=parent)
        except OSError:
            failure = "EVIDENCE_UNAVAILABLE"
        if not _close(parent):
            failure = "EVIDENCE_UNAVAILABLE"
    if failure is not None:
        raise StorageError(failure) from None
    return ArtifactRef(name, _sha(raw), len(raw), media_type, "private")


def _envelope(raw: bytes, schema: str, keys: set[str]) -> dict[str, Any]:
    obj = exact(parse(raw), keys | {"schema"})
    if obj["schema"] != schema:
        invalid()
    return obj


def _identity(
    spec: ScenarioSpec,
    answer: Answer,
    attempt: AttemptRecord,
    outcome: IncidentOutcome | None = None,
) -> None:
    if (
        not all(
            (spec.scenario_id, spec.version) == pair
            for pair in (
                (answer.scenario_id, answer.version),
                (attempt.scenario_id, attempt.scenario_version),
            )
        )
        or spec.incident.scenario_id != spec.scenario_id
        or (outcome is not None and outcome.scenario_id != spec.scenario_id)
    ):
        raise StorageError("IDENTITY_MISMATCH")
    if (
        not answer.expected_output
        or any(not t.strip() for t in answer.expected_output)
        or attempt.repetition < 1
        or not attempt.attempt_id.strip()
        or not spec.scenario_id.strip()
        or not spec.version.strip()
        or not spec.stimulus.question.strip()
        or not spec.stimulus.target_id.strip()
        or not spec.stimulus.subject_id.strip()
    ):
        invalid()
    if spec.kind == "normal":
        if answer.injected is not None or answer.signature is not None:
            invalid()
    elif answer.injected is None or answer.signature is None:
        invalid()
    if answer.signature is not None and (
        not answer.signature.any_of
        or answer.signature.min_matches < 1
        or not answer.signature.rule_id.strip()
        or any(
            not m.service.strip() or not m.operation.strip()
            for m in answer.signature.any_of
        )
    ):
        invalid()


def _manifest(manifest: Manifest) -> None:
    validated(Manifest, manifest)
    try:
        validate_manifest(manifest)
    except ManifestError:
        pass
    else:
        return
    raise StorageError("SCHEMA_INVALID") from None


def _binding(attempt: AttemptRecord, manifest: Manifest) -> None:
    if attempt.round_id != manifest.round_id:
        raise StorageError("IDENTITY_MISMATCH")
    if attempt.manifest_hash != manifest_hash(manifest):
        raise StorageError("HASH_MISMATCH")


def _judge_binding(manifest: Manifest, judge_hash: str, pack_hash: str) -> None:
    for name, used in (("judge_config", judge_hash), ("calibration_pack", pack_hash)):
        dimension = manifest.dimensions.get(name)
        if dimension is None or dimension.kind != "hash" or dimension.value != used:
            raise StorageError("JUDGE_IDENTITY_MISMATCH")


def _capture(capture: EvaluationCapture, manifest: Manifest) -> RunEvaluation:
    _manifest(manifest)
    if not capture.attempt.started:
        raise StorageError("ATTEMPT_NOT_STARTED")
    if capture.manifest != manifest:
        raise StorageError("IDENTITY_MISMATCH")
    _identity(capture.spec, capture.answer, capture.attempt, capture.outcome)
    _binding(capture.attempt, manifest)
    if capture.replay_run_id is not None and (
        capture.replay_run_id != capture.attempt.product_run_id
    ):
        raise StorageError("IDENTITY_MISMATCH")
    for key, record in capture.delivered.items():
        if key != record.evidence_id:
            raise StorageError("IDENTITY_MISMATCH")
    if set(capture.delivery_refs) - set(capture.delivered):
        raise StorageError("IDENTITY_MISMATCH")
    if set(capture.delivered) - set(capture.delivery_refs):
        raise StorageError("EVIDENCE_UNAVAILABLE")
    for record in capture.delivered.values():
        if record.view_sha256 != canonical_view_hash(record.view):
            raise StorageError("HASH_MISMATCH")
    for ref in capture.delivery_refs.values():
        _ref(ref)
    for optional_ref in (capture.recording_ref, capture.ledger_ref):
        if optional_ref is not None:
            _ref(optional_ref)
    for trace in capture.traces:
        if (
            trace.attempt_id != capture.attempt.attempt_id
            or (trace.evaluation_id != capture.attempt.attempt_id)
            or (
                trace.entry.required
                and (
                    trace.entry.role == "source"
                    or trace.entry.role.startswith("source_")
                )
            )
        ):
            raise StorageError("IDENTITY_MISMATCH")
    if capture.judge is not None:
        _judge_binding(
            manifest, capture.judge.config_hash, capture.judge.calibration_hash
        )
    try:
        return evaluate(
            capture.spec,
            capture.answer,
            capture.attempt,
            capture.outcome,
            capture.report,
            capture.delivered,
            capture.authorized,
            [t.entry for t in capture.traces],
            capture.judge,
        )
    except (ValueError, TypeError, KeyError, OverflowError):
        pass
    raise StorageError("SCHEMA_INVALID") from None


def _report_fields(capture: EvaluationCapture) -> tuple[Any, str | None]:
    raw = capture.report.raw if capture.report is not None else None
    parsed = parsed_report(capture.report)
    return (
        parsed.model_dump(mode="json") if parsed is not None else None,
        _sha(raw.encode("utf-8")) if raw is not None else None,
    )


def write_summary(path: Path, capture: EvaluationCapture) -> ArtifactRef:
    capture = validated(EvaluationCapture, capture)
    result = _capture(capture, capture.manifest)
    parsed, report_hash = _report_fields(capture)
    raw = canonical_bytes(
        {
            "schema": "opspilot-eval-summary-1",
            "capture": capture,
            "evaluation": result,
            "report_parsed": parsed,
            "report_sha256": report_hash,
            "trace": {"entries": capture.traces},
        }
    )
    return _publish(path, raw, "application/json")


def _load_summary(
    raw: bytes, manifest: Manifest, resolve: Resolver
) -> tuple[RunEvaluation, EvaluationCapture]:
    if "schema" not in parse(raw):
        raise StorageError("EVIDENCE_UNAVAILABLE")
    obj = _envelope(
        raw,
        "opspilot-eval-summary-1",
        {"capture", "evaluation", "report_parsed", "report_sha256", "trace"},
    )
    capture = cast(EvaluationCapture, decode(EvaluationCapture, obj["capture"]))
    stored = decode(RunEvaluation, obj["evaluation"])
    if not stored.attempt.started:
        raise StorageError("ATTEMPT_NOT_STARTED")
    # Validate identity/schema before calling any user resolver.
    result = _capture(capture, manifest)
    _binding(stored.attempt, manifest)
    if stored.attempt != capture.attempt:
        raise StorageError("IDENTITY_MISMATCH")
    if (stored.judge_config_hash, stored.judge_calibration_hash) != (
        result.judge_config_hash,
        result.judge_calibration_hash,
    ):
        raise StorageError("JUDGE_IDENTITY_MISMATCH")
    trace = exact(obj["trace"], {"entries"})
    entries = decode(tuple[TraceRecord, ...], trace["entries"])
    if entries != capture.traces:
        raise StorageError("DERIVED_RESULT_MISMATCH")
    for key, ref in capture.delivery_refs.items():
        record = decode(DeliveredRecord, parse(_resolve(ref, resolve)))
        if record.evidence_id != key:
            raise StorageError("IDENTITY_MISMATCH")
        if record.view_sha256 != canonical_view_hash(record.view):
            raise StorageError("HASH_MISMATCH")
        if record != capture.delivered[key]:
            raise StorageError("DERIVED_RESULT_MISMATCH")
    for optional_ref in (capture.recording_ref, capture.ledger_ref):
        if optional_ref is not None:
            _resolve(optional_ref, resolve)
    parsed, report_hash = _report_fields(capture)
    decode(str | None, obj["report_sha256"])
    if obj["report_sha256"] != report_hash:
        raise StorageError("HASH_MISMATCH")
    saved_report = obj["report_parsed"]
    if saved_report is not None:
        exact(saved_report, set(ReportV2.model_fields))
        if type(saved_report["claims"]) is not list:
            invalid()
        for claim in saved_report["claims"]:
            exact(claim, set(ClaimV2.model_fields))
        report_valid = True
        try:
            ReportV2.model_validate(saved_report, strict=True)
        except ValueError:
            report_valid = False
        if not report_valid:
            raise StorageError("SCHEMA_INVALID") from None
    if stored != result or canonical_bytes(obj["report_parsed"]) != canonical_bytes(
        parsed
    ):
        raise StorageError("DERIVED_RESULT_MISMATCH")
    return result, capture


def load_summary(path: Path, *, manifest: Manifest, resolve: Resolver) -> RunEvaluation:
    return _load_summary(_read(path), manifest, resolve)[0]


def write_attempt(path: Path, journal: AttemptJournal) -> ArtifactRef:
    journal = validated(AttemptJournal, journal)
    _identity(journal.spec, journal.answer, journal.attempt)
    return _publish(
        path,
        canonical_bytes({"schema": "opspilot-eval-attempt-1", "journal": journal}),
        "application/json",
    )


def _load_attempt(raw: bytes) -> AttemptJournal:
    obj = _envelope(raw, "opspilot-eval-attempt-1", {"journal"})
    journal = cast(AttemptJournal, decode(AttemptJournal, obj["journal"]))
    _identity(journal.spec, journal.answer, journal.attempt)
    return journal


def load_attempt(path: Path) -> AttemptJournal:
    return _load_attempt(_read(path))


def _calibration(capture: CalibrationCapture) -> CalibrationResult:
    try:
        return calibrate(
            capture.labels,
            capture.verdicts,
            judge_config_hash=capture.judge_config_hash,
            calibration_pack_hash=capture.calibration_pack_hash,
        )
    except CalibrationError:
        pass
    raise StorageError("SCHEMA_INVALID") from None


def write_calibration(path: Path, capture: CalibrationCapture) -> ArtifactRef:
    capture = validated(CalibrationCapture, capture)
    result = _calibration(capture)
    return _publish(
        path,
        canonical_bytes(
            {
                "schema": "opspilot-eval-calibration-1",
                "capture": capture,
                "result": result,
            }
        ),
        "application/json",
    )


def _load_calibration(
    raw: bytes, expected_pack_hash: str, expected_judge_hash: str
) -> CalibrationResult:
    obj = _envelope(raw, "opspilot-eval-calibration-1", {"capture", "result"})
    capture = cast(CalibrationCapture, decode(CalibrationCapture, obj["capture"]))
    stored = decode(CalibrationResult, obj["result"])
    if (stored.judge_config_hash, stored.calibration_pack_hash) != (
        expected_judge_hash,
        expected_pack_hash,
    ):
        raise StorageError("JUDGE_IDENTITY_MISMATCH")
    if (
        capture.judge_config_hash != expected_judge_hash
        or capture.calibration_pack_hash != expected_pack_hash
    ):
        raise StorageError("JUDGE_IDENTITY_MISMATCH")
    result = _calibration(capture)
    if result != stored:
        raise StorageError("DERIVED_RESULT_MISMATCH")
    return result


def load_calibration(
    path: Path, *, expected_pack_hash: str, expected_judge_hash: str
) -> CalibrationResult:
    return _load_calibration(_read(path), expected_pack_hash, expected_judge_hash)


def _planned(attempt: AttemptRecord) -> PlannedSlot:
    return PlannedSlot(
        attempt.scenario_id, attempt.scenario_version, attempt.mode, attempt.repetition
    )


def _round(capture: RoundCapture, resolve: Resolver) -> Aggregate:
    _manifest(capture.manifest)
    if len(set(capture.planned)) != len(capture.planned) or any(
        s.repetition < 1 for s in capture.planned
    ):
        invalid()
    journals: dict[str, AttemptJournal] = {}
    slots: set[PlannedSlot] = set()
    for ref in capture.journals:
        journal = _load_attempt(_resolve(ref, resolve))
        attempt = journal.attempt
        _binding(attempt, capture.manifest)
        slot = _planned(attempt)
        if (
            attempt.attempt_id in journals
            or slot in slots
            or slot not in capture.planned
        ):
            invalid()
        slots.add(slot)
        journals[attempt.attempt_id] = journal
    if set(capture.summaries) - set(journals):
        raise StorageError("IDENTITY_MISMATCH")
    evaluations: list[RunEvaluation] = []
    claimed_calibrated = False
    for key, journal in journals.items():
        summary_ref = capture.summaries.get(key)
        if summary_ref is not None:
            if not journal.attempt.started:
                raise StorageError("ATTEMPT_NOT_STARTED")
            evaluation, summary = _load_summary(
                _resolve(summary_ref, resolve), capture.manifest, resolve
            )
            old, new = journal.attempt, evaluation.attempt
            if (
                replace(
                    old,
                    status=new.status,
                    replay_miss=new.replay_miss,
                    product_run_id=new.product_run_id,
                )
                != new
                or (
                    old.product_run_id is not None
                    and old.product_run_id != new.product_run_id
                )
                or journal.spec != summary.spec
                or journal.answer != summary.answer
            ):
                raise StorageError("IDENTITY_MISMATCH")
            evaluations.append(evaluation)
            claimed_calibrated |= summary.judge is not None and summary.judge.calibrated
        elif journal.attempt.started:
            evaluations.append(
                evaluate(
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
            )
    calibration = None
    if capture.calibration_ref is not None:
        judge = capture.manifest.dimensions.get("judge_config")
        pack = capture.manifest.dimensions.get("calibration_pack")
        if judge is None or pack is None or judge.kind != "hash" or pack.kind != "hash":
            raise StorageError("JUDGE_IDENTITY_MISMATCH")
        calibration = _load_calibration(
            _resolve(capture.calibration_ref, resolve),
            cast(str, pack.value),
            cast(str, judge.value),
        )
    if claimed_calibrated and (calibration is None or not calibration.passed):
        raise StorageError("DERIVED_RESULT_MISMATCH")
    try:
        return aggregate(capture.manifest, capture.planned, evaluations, calibration)
    except (AggregateError, ManifestError):
        pass
    raise StorageError("SCHEMA_INVALID") from None


def write_round(path: Path, capture: RoundCapture, *, resolve: Resolver) -> ArtifactRef:
    capture = validated(RoundCapture, capture)
    result = _round(capture, resolve)
    return _publish(
        path,
        canonical_bytes(
            {
                "schema": "opspilot-eval-aggregate-1",
                "capture": capture,
                "aggregate": result,
            }
        ),
        "application/json",
    )


def load_round(path: Path, *, resolve: Resolver) -> Aggregate:
    obj = _envelope(_read(path), "opspilot-eval-aggregate-1", {"capture", "aggregate"})
    capture = cast(RoundCapture, decode(RoundCapture, obj["capture"]))
    stored = decode(Aggregate, obj["aggregate"])
    result = _round(capture, resolve)
    if stored.round_id != result.round_id:
        raise StorageError("IDENTITY_MISMATCH")
    if stored.manifest_hash != result.manifest_hash:
        raise StorageError("HASH_MISMATCH")
    if stored != result:
        raise StorageError("DERIVED_RESULT_MISMATCH")
    return _baseline_layout(result, capture.planned)


def _baseline_layout(result: Aggregate, planned: tuple[PlannedSlot, ...]) -> Aggregate:
    """Apply the B1 completion policy after validating the PR-A cache."""
    root = Path(__file__).resolve().parents[2] / "evals" / "scenarios"
    try:
        scenarios = load_scenarios(root)
    except (OSError, ScenarioError):
        scenarios = ()
    scenario_ids = {spec.scenario_id for spec, _answer in scenarios}
    expected = {
        PlannedSlot(spec.scenario_id, spec.version, "replay", repetition)
        for spec, _answer in scenarios
        for repetition in range(1, 6)
    } | {
        PlannedSlot(spec.scenario_id, spec.version, "live", repetition)
        for spec, _answer in scenarios
        for repetition in range(1, 3)
    }
    if (
        len(scenarios) == len(scenario_ids) == 6
        and len(planned) == len(expected)
        and set(planned) == expected
    ):
        return result
    return replace(
        result,
        baseline_complete=False,
        incomplete_reasons=(*result.incomplete_reasons, "BASELINE_LAYOUT_MISMATCH"),
    )


def read_legacy_summary(path: Path) -> LegacySummary:
    raw = _read(path)
    obj = parse(raw)
    if "schema" in obj:
        invalid()
    trace = obj.get("trace")
    entries: tuple[Mapping[str, Any], ...] = ()
    if isinstance(trace, dict):
        if "entries" in trace:
            items = trace["entries"]
            if type(items) is not list or any(type(i) is not dict for i in items):
                invalid()
            entries = tuple(items)
        else:
            entries = (trace,)
    elif trace is not None:
        invalid()
    return LegacySummary(obj, _sha(raw), entries)
