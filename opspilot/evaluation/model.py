"""Types and scenario loading for the scenario evaluation suite (PR-A, contract r7).

The suite is a development baseline, not product runtime: nothing here is
imported by the investigation loop. ``scenario.json`` carries only what the
investigated model may see; ``answer.json`` stays on the evaluator side and is
never an input of :func:`investigator_input` (r7 P11).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

from opspilot.acceptance import IncidentScenario
from opspilot.tools.registry import canonical_hash

Mode = Literal["replay", "live", "structural"]
ScenarioKind = Literal["fault", "normal"]
AttemptStatus = Literal["ran", "setup_failed", "aborted", "unknown"]
JudgeStatus = Literal["ok", "error", "timeout", "invalid", "oversize"]
TraceReadBack = Literal[
    "found", "root_run_not_found", "no_trace_id", "not_exported", "error"
]
FailureClass = Literal[
    "setup",
    "replay_miss",
    "execution",
    "deterministic_gate",
    "judge_error",
    "diagnosis",
    "evidence_incomplete",
]
# Highest priority first (r7 P12): the primary class never hides the others,
# which all stay in ``failure_reasons``.
FAILURE_PRIORITY: tuple[FailureClass, ...] = (
    "setup",
    "replay_miss",
    "execution",
    "deterministic_gate",
    "judge_error",
    "diagnosis",
    "evidence_incomplete",
)


class ScenarioError(ValueError):
    """A scenario directory violates the evaluator contract."""


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        for bound in (self.start, self.end):
            if bound.utcoffset() != timedelta(0):
                # Naive or non-UTC bounds would break span-time comparison.
                raise ValueError("WINDOW_NOT_UTC")
        if self.start > self.end:
            raise ValueError("WINDOW_REVERSED")

    def contains(self, other: Window) -> bool:
        return self.start <= other.start and other.end <= self.end


@dataclass(frozen=True)
class Stimulus:
    question: str
    subject_id: str
    target_id: str


@dataclass(frozen=True)
class ScenarioSpec:
    scenario_id: str
    version: str
    kind: ScenarioKind
    incident: IncidentScenario
    stimulus: Stimulus


@dataclass(frozen=True)
class SpanMatcher:
    service: str
    operation: str


@dataclass(frozen=True)
class SignatureRule:
    rule_id: str
    any_of: tuple[SpanMatcher, ...]
    min_matches: int = 1


@dataclass(frozen=True)
class InjectedFault:
    service: str
    service_aliases: tuple[str, ...]
    operation: str
    operation_aliases: tuple[str, ...]
    flag: str
    variant: str


@dataclass(frozen=True)
class Answer:
    scenario_id: str
    version: str
    expected_output: tuple[str, ...]
    injected: InjectedFault | None
    signature: SignatureRule | None


@dataclass(frozen=True)
class DeliveredRecord:
    evidence_id: str
    target_id: str
    status: str
    citable_as_fact: bool
    time_scope_refs: frozenset[str]
    window: Window | None
    view: Mapping[str, Any]
    view_sha256: str
    delivered: bool


@dataclass(frozen=True)
class ReportCapture:
    raw: str | None
    finish_reason: str


@dataclass(frozen=True)
class TraceEntry:
    role: str
    required: bool
    read_back: TraceReadBack
    run_id: str | None
    expected_run_id: str | None
    # The product Run this trace belongs to; it must be the attempt's own
    # ``product_run_id`` for the evidence to count as complete (r7 P2).
    product_run_id: str | None = None


@dataclass(frozen=True)
class AttemptRecord:
    attempt_id: str
    round_id: str
    manifest_hash: str
    scenario_id: str
    scenario_version: str
    mode: Mode
    repetition: int
    started: bool
    status: AttemptStatus
    replay_miss: bool
    product_run_id: str | None


@dataclass(frozen=True)
class JudgedDiagnosis:
    location: str
    quote: str
    factual_causal: bool
    service: str | None
    operation: str | None
    cited_evidence_ids: tuple[str, ...]
    linked_claim: int | None
    supported: bool
    reason: str


@dataclass(frozen=True)
class JudgeResult:
    status: JudgeStatus
    calibrated: bool
    diagnoses: tuple[JudgedDiagnosis, ...]
    verdict: bool | None
    config_hash: str
    calibration_hash: str


@dataclass(frozen=True)
class GateResult:
    passed: bool
    checks: Mapping[str, bool]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class RunEvaluation:
    attempt: AttemptRecord
    gate: GateResult
    signature_matches: Mapping[str, tuple[str, ...]]
    judge_status: Literal[
        "ok",
        "error",
        "timeout",
        "invalid",
        "oversize",
        "uncalibrated",
        "skipped",
        "not_run",
    ]
    diagnosis_pass: bool | None
    evidence_status: Literal["complete", "incomplete"]
    deterministic_pass: bool
    judge_pass: bool
    overall: bool
    failure_class: FailureClass | None
    failure_reasons: tuple[str, ...]
    # The judge identity that produced the verdict, checked by ``aggregate``
    # against the round's frozen ``judge_config`` and ``calibration_pack``.
    judge_config_hash: str | None = None
    judge_calibration_hash: str | None = None


def canonical_view_hash(view: Mapping[str, Any]) -> str:
    """The product's ``view_sha256`` algorithm (``registry.canonical_hash``)."""
    return canonical_hash(dict(view))


# ``scenario.json`` keys; anything else could leak into a model-visible place.
_SCENARIO_KEYS = frozenset({"scenario_id", "version", "kind", "incident", "stimulus"})
_INCIDENT_KEYS = frozenset({"feature_id", "acceptance_step", "kind", "subject_id"})
_STIMULUS_KEYS = frozenset({"question", "subject_id", "target_id"})
_ANSWER_KEYS = frozenset(
    {"scenario_id", "version", "expected_output", "injected", "signature"}
)
_INJECTED_KEYS = frozenset(
    {"service", "service_aliases", "operation", "operation_aliases", "flag", "variant"}
)


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ScenarioError(f"UNREADABLE:{path.name}") from exc
    if not isinstance(data, dict):
        raise ScenarioError(f"NOT_AN_OBJECT:{path.name}")
    return data


def _text(data: Mapping[str, Any], key: str, where: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ScenarioError(f"MISSING_TEXT:{where}.{key}")
    return value


def _texts(data: Mapping[str, Any], key: str, where: str) -> tuple[str, ...]:
    value = data.get(key)
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ScenarioError(f"BAD_TEXT_LIST:{where}.{key}")
    return tuple(cast(list[str], value))


def _exact_keys(data: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    extra = sorted(set(data) - allowed)
    if extra:
        raise ScenarioError(f"UNEXPECTED_FIELDS:{where}:{','.join(extra)}")
    missing = sorted(allowed - set(data))
    if missing:
        raise ScenarioError(f"MISSING_FIELDS:{where}:{','.join(missing)}")


def _object(data: Mapping[str, Any], key: str, where: str) -> Mapping[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ScenarioError(f"NOT_AN_OBJECT:{where}.{key}")
    return value


def _spec(data: Mapping[str, Any]) -> ScenarioSpec:
    _exact_keys(data, _SCENARIO_KEYS, "scenario")
    kind = data.get("kind")
    if kind not in ("fault", "normal"):
        raise ScenarioError("BAD_KIND")
    incident = _object(data, "incident", "scenario")
    _exact_keys(incident, _INCIDENT_KEYS, "scenario.incident")
    stimulus = _object(data, "stimulus", "scenario")
    _exact_keys(stimulus, _STIMULUS_KEYS, "scenario.stimulus")
    scenario_id = _text(data, "scenario_id", "scenario")
    return ScenarioSpec(
        scenario_id=scenario_id,
        version=_text(data, "version", "scenario"),
        kind=cast(ScenarioKind, kind),
        incident=IncidentScenario(
            scenario_id=scenario_id,
            feature_id=_text(incident, "feature_id", "incident"),
            acceptance_step=_text(incident, "acceptance_step", "incident"),
            kind=_text(incident, "kind", "incident"),
            subject_id=_text(incident, "subject_id", "incident"),
        ),
        stimulus=Stimulus(
            question=_text(stimulus, "question", "stimulus"),
            subject_id=_text(stimulus, "subject_id", "stimulus"),
            target_id=_text(stimulus, "target_id", "stimulus"),
        ),
    )


def _injected(data: Mapping[str, Any]) -> InjectedFault:
    _exact_keys(data, _INJECTED_KEYS, "answer.injected")
    return InjectedFault(
        service=_text(data, "service", "injected"),
        service_aliases=_texts(data, "service_aliases", "injected"),
        operation=_text(data, "operation", "injected"),
        operation_aliases=_texts(data, "operation_aliases", "injected"),
        flag=_text(data, "flag", "injected"),
        variant=_text(data, "variant", "injected"),
    )


def _signature(data: Mapping[str, Any]) -> SignatureRule:
    extra = sorted(set(data) - {"rule_id", "any_of", "min_matches"})
    if extra:
        # A misspelt threshold must not silently fall back to 1.
        raise ScenarioError(f"UNEXPECTED_FIELDS:answer.signature:{','.join(extra)}")
    matchers = data.get("any_of")
    if not isinstance(matchers, list) or not matchers:
        raise ScenarioError("EMPTY_SIGNATURE")
    any_of: list[SpanMatcher] = []
    for item in matchers:
        if not isinstance(item, dict):
            raise ScenarioError("BAD_SIGNATURE_MATCHER")
        _exact_keys(item, frozenset({"service", "operation"}), "signature.any_of")
        any_of.append(
            SpanMatcher(
                service=_text(item, "service", "matcher"),
                operation=_text(item, "operation", "matcher"),
            )
        )
    min_matches = data.get("min_matches", 1)
    if (
        not isinstance(min_matches, int)
        or isinstance(min_matches, bool)
        or min_matches < 1
    ):
        raise ScenarioError("BAD_MIN_MATCHES")
    return SignatureRule(
        rule_id=_text(data, "rule_id", "signature"),
        any_of=tuple(any_of),
        min_matches=min_matches,
    )


def _answer(data: Mapping[str, Any]) -> Answer:
    _exact_keys(data, _ANSWER_KEYS, "answer")
    expected = _texts(data, "expected_output", "answer")
    if not expected:
        raise ScenarioError("EMPTY_EXPECTED_OUTPUT")
    injected = data.get("injected")
    signature = data.get("signature")
    return Answer(
        scenario_id=_text(data, "scenario_id", "answer"),
        version=_text(data, "version", "answer"),
        expected_output=expected,
        injected=None
        if injected is None
        else _injected(_object(data, "injected", "answer")),
        signature=None
        if signature is None
        else _signature(_object(data, "signature", "answer")),
    )


def load_scenario(directory: Path) -> tuple[ScenarioSpec, Answer]:
    spec = _spec(_read(directory / "scenario.json"))
    answer = _answer(_read(directory / "answer.json"))
    if (spec.scenario_id, spec.version) != (answer.scenario_id, answer.version):
        raise ScenarioError("SCENARIO_ANSWER_MISMATCH")
    if spec.kind == "fault" and (answer.injected is None or answer.signature is None):
        raise ScenarioError("FAULT_REQUIRES_INJECTED_AND_SIGNATURE")
    if spec.kind == "normal" and (
        answer.injected is not None or answer.signature is not None
    ):
        raise ScenarioError("NORMAL_FORBIDS_INJECTED_AND_SIGNATURE")
    return spec, answer


def load_scenarios(root: Path) -> tuple[tuple[ScenarioSpec, Answer], ...]:
    loaded = [load_scenario(path) for path in sorted(root.iterdir()) if path.is_dir()]
    return tuple(sorted(loaded, key=lambda pair: pair[0].scenario_id))


def investigator_input(spec: ScenarioSpec) -> Mapping[str, str]:
    """Everything the investigated model may see; no answer, no scenario ID."""
    return {
        "question": spec.stimulus.question,
        "subject_id": spec.stimulus.subject_id,
        "target_id": spec.stimulus.target_id,
    }
