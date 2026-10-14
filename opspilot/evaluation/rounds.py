"""Round-level contracts: manifest (r7 P4), judge calibration (P7), aggregate (P10/P12)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from opspilot.evaluation.model import Mode, RunEvaluation
from opspilot.tools.registry import canonical_hash

REQUIRED_DIMENSIONS: tuple[str, ...] = (
    "code",
    "upstream",
    "scenarios",
    "answers",
    "evidence_rules",
    "model_request",
    "prompt",
    "tools_schema",
    "projection",
    "registry",
    "profile",
    "adapter",
    "evaluator",
    "judge_config",
    "calibration_pack",
    "permissions",
    "limits",
    "kind_environment",
    "subject_initial_state",
    "session",
    "human_feedback",
    "workload",
    "knowledge_snapshot",
    "replay_fixture",
    "n",
    "seed",
    "command",
    "clock_policy",
)


class ManifestError(ValueError):
    """A manifest dimension is malformed."""


class AggregateError(ValueError):
    """The evaluations cannot be summed into one round."""


class CalibrationError(ValueError):
    """Calibration labels and verdicts do not line up."""


@dataclass(frozen=True)
class DimensionValue:
    kind: Literal["hash", "version", "none", "unknown"]
    value: str | None
    reason: str | None


@dataclass(frozen=True)
class Manifest:
    round_id: str
    dimensions: Mapping[str, DimensionValue]


@dataclass(frozen=True)
class PlannedSlot:
    scenario_id: str
    scenario_version: str
    mode: Mode
    repetition: int


@dataclass(frozen=True)
class CalibrationResult:
    passed: bool
    items: int
    comparisons: int
    agreement: int
    negatives: int
    false_passes: int
    reasons: tuple[str, ...]
    # Which judge and frozen validation pack this result is for; ``aggregate``
    # accepts it only for the round that froze the same two hashes.
    judge_config_hash: str | None = None
    calibration_pack_hash: str | None = None


@dataclass(frozen=True)
class AggregateRow:
    scenario_id: str
    scenario_version: str
    mode: Mode
    planned: int
    attempted: int
    not_started: int
    passed: int
    failed: int
    pass_rate: float | None
    replay_miss: int
    replay_miss_rate: float | None
    failure_classes: Mapping[str, int]


@dataclass(frozen=True)
class Aggregate:
    round_id: str
    manifest_hash: str
    rows: tuple[AggregateRow, ...]
    baseline_complete: bool
    incomplete_reasons: tuple[str, ...]


def validate_manifest(manifest: Manifest) -> None:
    extra = sorted(set(manifest.dimensions) - set(REQUIRED_DIMENSIONS))
    if extra:
        raise ManifestError(f"UNKNOWN_DIMENSION:{','.join(extra)}")
    for name, value in manifest.dimensions.items():
        if value.kind not in ("hash", "version", "none", "unknown"):
            # Persisted JSON gets no protection from the Literal annotation.
            raise ManifestError(f"BAD_KIND:{name}")
        if value.kind in ("hash", "version") and not value.value:
            raise ManifestError(f"MISSING_VALUE:{name}")
        if value.kind == "none" and not value.reason:
            raise ManifestError(f"NONE_WITHOUT_REASON:{name}")


def manifest_unknowns(manifest: Manifest) -> tuple[str, ...]:
    return tuple(
        name
        for name in REQUIRED_DIMENSIONS
        if name not in manifest.dimensions
        or manifest.dimensions[name].kind == "unknown"
    )


def manifest_hash(manifest: Manifest) -> str:
    return canonical_hash(
        {
            "round_id": manifest.round_id,
            "dimensions": {
                name: {"kind": v.kind, "value": v.value, "reason": v.reason}
                for name, v in manifest.dimensions.items()
            },
        }
    )


def calibrate(
    labels: Mapping[str, bool],
    verdicts: Sequence[tuple[str, bool]],
    *,
    min_items: int = 24,
    runs_per_item: int = 2,
    min_agreement: int = 46,
    min_negatives: int = 12,
    judge_config_hash: str | None = None,
    calibration_pack_hash: str | None = None,
) -> CalibrationResult:
    runs: Counter[str] = Counter()
    agreement = 0
    false_passes = 0
    for item, verdict in verdicts:
        if item not in labels:
            raise CalibrationError(f"UNLABELLED_ITEM:{item}")
        runs[item] += 1
        agreement += verdict == labels[item]
        false_passes += (not labels[item]) and verdict
    short = sorted(item for item in labels if runs[item] != runs_per_item)
    if short:
        raise CalibrationError(f"WRONG_RUN_COUNT:{','.join(short)}")
    items = len(labels)
    negatives = sum(1 for label in labels.values() if not label)
    reasons: list[str] = []
    if items < min_items:
        reasons.append("TOO_FEW_ITEMS")
    if agreement < min_agreement:
        reasons.append("AGREEMENT_BELOW_THRESHOLD")
    if negatives < min_negatives:
        reasons.append("TOO_FEW_NEGATIVES")
    if false_passes:
        reasons.append("FALSE_PASS")
    return CalibrationResult(
        passed=not reasons,
        items=items,
        comparisons=items * runs_per_item,
        agreement=agreement,
        negatives=negatives,
        false_passes=false_passes,
        reasons=tuple(reasons),
        judge_config_hash=judge_config_hash,
        calibration_pack_hash=calibration_pack_hash,
    )


def _frozen_hash(manifest: Manifest, name: str) -> str | None:
    value = manifest.dimensions.get(name)
    return value.value if value is not None and value.kind == "hash" else None


def _slot(evaluation: RunEvaluation) -> PlannedSlot:
    a = evaluation.attempt
    return PlannedSlot(a.scenario_id, a.scenario_version, a.mode, a.repetition)


def _validate_calibration(calibration: CalibrationResult) -> None:
    """Defend the DTO seam; persistence separately recomputes raw verdicts."""
    counts = (
        calibration.items,
        calibration.comparisons,
        calibration.agreement,
        calibration.negatives,
        calibration.false_passes,
    )
    if any(type(n) is not int or n < 0 for n in counts):
        raise AggregateError("CALIBRATION_INCONSISTENT")
    items, comparisons, agreement, negatives, false_passes = counts
    if (
        comparisons != items * 2
        or agreement > comparisons
        or negatives > items
        or false_passes > 2 * negatives
    ):
        raise AggregateError("CALIBRATION_INCONSISTENT")
    disagreements = comparisons - agreement
    if calibration.passed is True and (
        false_passes > disagreements
        or disagreements - false_passes > 2 * (items - negatives)
    ):
        raise AggregateError("CALIBRATION_INCONSISTENT")
    # r2: failed DTOs cannot raise the pass rate; preserve I:99 compatibility.
    reasons = tuple(
        code
        for failed, code in (
            (items < 24, "TOO_FEW_ITEMS"),
            (agreement < 46, "AGREEMENT_BELOW_THRESHOLD"),
            (negatives < 12, "TOO_FEW_NEGATIVES"),
            (false_passes != 0, "FALSE_PASS"),
        )
        if failed
    )
    if type(calibration.passed) is not bool or (
        calibration.passed != (not reasons) or calibration.reasons != reasons
    ):
        raise AggregateError("CALIBRATION_INCONSISTENT")


def aggregate(
    manifest: Manifest,
    planned: Sequence[PlannedSlot],
    evaluations: Sequence[RunEvaluation],
    calibration: CalibrationResult | None,
) -> Aggregate:
    validate_manifest(manifest)
    if calibration is not None:
        _validate_calibration(calibration)
    if len(set(planned)) != len(planned):
        raise AggregateError("DUPLICATE_PLANNED_SLOT")
    expected_hash = manifest_hash(manifest)
    frozen_judge = (
        _frozen_hash(manifest, "judge_config"),
        _frozen_hash(manifest, "calibration_pack"),
    )
    seen_attempts: set[str] = set()
    by_slot: dict[PlannedSlot, RunEvaluation] = {}
    for evaluation in evaluations:
        attempt = evaluation.attempt
        if attempt.started is not True:
            raise AggregateError("ATTEMPT_NOT_STARTED")
        if attempt.round_id != manifest.round_id:
            raise AggregateError("ROUND_MISMATCH")
        if attempt.manifest_hash != expected_hash:
            raise AggregateError("MANIFEST_HASH_MISMATCH")
        used = (evaluation.judge_config_hash, evaluation.judge_calibration_hash)
        judged = evaluation.judge_status not in ("skipped", "not_run")
        if (used != (None, None) or judged) and (
            used != frozen_judge or None in frozen_judge
        ):
            # A stale or uncalibrated judge identity (bot review P1).
            raise AggregateError("JUDGE_IDENTITY_MISMATCH")
        if attempt.attempt_id in seen_attempts:
            raise AggregateError("DUPLICATE_ATTEMPT")
        seen_attempts.add(attempt.attempt_id)
        slot = _slot(evaluation)
        if slot not in planned:
            raise AggregateError("UNPLANNED_ATTEMPT")
        if slot in by_slot:
            raise AggregateError("SLOT_ATTEMPTED_TWICE")
        by_slot[slot] = evaluation

    groups: dict[tuple[str, str, Mode], list[PlannedSlot]] = {}
    for slot in planned:
        groups.setdefault(
            (slot.scenario_id, slot.scenario_version, slot.mode), []
        ).append(slot)
    rows: list[AggregateRow] = []
    for key in sorted(groups):
        scenario_id, version, mode = key
        done = [by_slot[s] for s in groups[key] if s in by_slot]
        attempted = len(done)
        passed = sum(1 for e in done if e.overall)
        misses = sum(1 for e in done if e.attempt.replay_miss)
        classes = Counter(
            e.failure_class for e in done if not e.overall and e.failure_class
        )
        rows.append(
            AggregateRow(
                scenario_id=scenario_id,
                scenario_version=version,
                mode=mode,
                planned=len(groups[key]),
                attempted=attempted,
                not_started=len(groups[key]) - attempted,
                passed=passed,
                failed=attempted - passed,
                pass_rate=None
                if mode == "structural" or attempted == 0
                else passed / attempted,
                replay_miss=misses,
                replay_miss_rate=misses / attempted
                if mode == "replay" and attempted
                else None,
                failure_classes=dict(sorted(classes.items())),
            )
        )

    reasons: list[str] = []
    if len(by_slot) < len(planned):
        reasons.append("SLOTS_NOT_STARTED")
    if any(e.evidence_status != "complete" for e in by_slot.values()):
        reasons.append("EVIDENCE_INCOMPLETE")
    if manifest_unknowns(manifest):
        reasons.append("MANIFEST_UNKNOWN")
    if (
        calibration is None
        or not calibration.passed
        or None in frozen_judge
        or (calibration.judge_config_hash, calibration.calibration_pack_hash)
        != frozen_judge
    ):
        reasons.append("JUDGE_NOT_CALIBRATED")
    if not any(slot.mode != "structural" for slot in planned):
        reasons.append("NO_QUALITY_SLOTS")
    return Aggregate(
        round_id=manifest.round_id,
        manifest_hash=expected_hash,
        rows=tuple(rows),
        baseline_complete=not reasons,
        incomplete_reasons=tuple(reasons),
    )
