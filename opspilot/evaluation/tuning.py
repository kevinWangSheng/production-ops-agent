"""#398 judge tuning round: formal-shape scorer and triage rows (pure).

A tuning round judges every debug item once and every validation item twice,
in the order ``run_calibration`` uses, and is counted with the formal
per-observation rule (``judged_observation``). Unlike formal, invalid answers
and false passes count over every observation, debug items included, and every
started attempt stays in the denominator. Missed diagnoses are counted as
reference only (P7-D24); ``legacy=True`` reproduces the pre-D24 rule so rounds
scored under it can still be verified.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from opspilot.evaluation.calibration_flow import (
    BlindLabel,
    CalibrationItem,
    LabelPack,
    judged_observation,
)
from opspilot.evaluation.judge import MAX_ATTEMPTS, JudgeError, JudgeObservation

PASS_AGREEMENT = 46
Hint = Literal[
    "transport",
    "http",
    "timeout",
    "output_invalid",
    "missed_diagnosis",
    "false_pass",
    "disagreement",
]
Outcome = Literal["passed", "pending_user_decision", "failed"]
CATEGORIES = ("eval_env", "judge_error", "real_failure")
_ERROR_HINTS: Mapping[str, Hint] = {
    "JUDGE_TRANSPORT_ERROR": "transport",
    "JUDGE_HTTP_ERROR": "http",
    "JUDGE_TIMEOUT": "timeout",
}


class TuningRecord(Protocol):
    """A per-call record, e.g. the driver's DebugRecord."""

    @property
    def item_id(self) -> str: ...
    @property
    def input_hash(self) -> str: ...
    @property
    def observation(self) -> JudgeObservation: ...


@dataclass(frozen=True)
class TuningScore:
    agreement: int  # validation observations only (48)
    invalid: int
    missed_diagnoses: int
    false_passes: int
    started: int
    passed: bool


@dataclass(frozen=True)
class TriageRow:
    item_id: str
    provider: str
    repetition: int
    hint: Hint
    category: str | None = None


def schedule(pack: LabelPack) -> tuple[tuple[CalibrationItem, int], ...]:
    """Formal shape and order: pack order, debug once, validation twice."""
    return tuple(
        (item, repetition)
        for item in pack.items
        for repetition in range(1, (2 if item.split == "validation" else 1) + 1)
    )


def assess_tuning(
    pack: LabelPack,
    labels: Mapping[str, BlindLabel],
    records: Sequence[TuningRecord],
    *,
    legacy: bool = False,
) -> tuple[TuningScore, tuple[TriageRow, ...]]:
    """One provider's records in schedule order: its score and triage rows."""
    planned = schedule(pack)
    # A stopped round is scored on its prefix; it can never pass.
    if len(records) > len(planned):
        raise JudgeError("SCHEMA_INVALID")
    providers = {r.observation.provider for r in records}
    if len(providers) > 1:
        raise JudgeError("IDENTITY_MISMATCH")
    config_hashes = {r.observation.result.config_hash for r in records}
    if len(config_hashes) > 1:
        raise JudgeError("DERIVED_RESULT_MISMATCH")
    agreement = invalid = missed = false_passes = 0
    rows: list[TriageRow] = []
    for record, (item, repetition) in zip(records, planned, strict=False):
        observation = record.observation
        if (
            record.item_id != item.item_id
            or record.input_hash != item.input_hash
            or getattr(record, "repetition", repetition) != repetition
        ):
            raise JudgeError("IDENTITY_MISMATCH")
        if (
            observation.result.calibrated
            or observation.passed
            or type(observation.attempts) is not int
            or not 1 <= observation.attempts <= MAX_ATTEMPTS
        ):
            raise JudgeError("DERIVED_RESULT_MISMATCH")
        judged = judged_observation(
            item, labels[item.item_id], observation.provider, observation
        )
        invalid += not judged.valid or judged.recheck_failed
        missed += judged.missing
        false_passes += judged.false_pass
        if item.split == "validation":
            withheld = judged.compromised or legacy and judged.missing
            agreement += judged.agrees and not withheld
        hint: Hint | None = None
        if not judged.valid:
            hint = _ERROR_HINTS.get(observation.error_code or "", "output_invalid")
        elif judged.recheck_failed:
            hint = "output_invalid"
        elif judged.false_pass:
            hint = "false_pass"
        elif legacy and judged.missing:
            hint = "missed_diagnosis"
        elif not judged.agrees:
            hint = "disagreement"
        elif judged.missing:
            hint = "missed_diagnosis"
        if hint is not None:
            rows.append(TriageRow(item.item_id, observation.provider, repetition, hint))
    started = len(records)
    score = TuningScore(
        agreement=agreement,
        invalid=invalid,
        missed_diagnoses=missed,
        false_passes=false_passes,
        started=started,
        passed=agreement >= PASS_AGREEMENT
        and invalid == 0
        and (missed == 0 or not legacy)
        and false_passes == 0
        and started == len(planned),
    )
    return score, tuple(rows)


def score_tuning(
    pack: LabelPack,
    labels: Mapping[str, BlindLabel],
    records: Sequence[TuningRecord],
) -> TuningScore:
    """One provider's records, in schedule order."""
    return assess_tuning(pack, labels, records)[0]


def triage_rows(
    pack: LabelPack,
    labels: Mapping[str, BlindLabel],
    records: Sequence[TuningRecord],
) -> tuple[TriageRow, ...]:
    """Every started attempt that is not a clean agreement, uncategorised."""
    return assess_tuning(pack, labels, records)[1]


# Reference-only triage hint under P7-D24: never blocks a rerun.
REFERENCE_HINTS: frozenset[str] = frozenset({"missed_diagnosis"})


def round_outcome(scores: Mapping[str, TuningScore]) -> Outcome:
    """Q10: FlashX (zai) passing passes; Luna-only stops (P7-D14); DeepSeek never."""
    if "zai" in scores and scores["zai"].passed:
        return "passed"
    if "openai" in scores and scores["openai"].passed:
        return "pending_user_decision"
    return "failed"
