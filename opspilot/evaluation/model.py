"""Types and scenario loading for the scenario evaluation suite (PR-A, contract r7).

The suite is a development baseline, not product runtime: nothing here is
imported by the investigation loop. ``scenario.json`` carries only what the
investigated model may see; ``answer.json`` stays on the evaluator side and is
never an input of :func:`investigator_input` (r7 P11).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
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
    unreadable = False
    data: Any = None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        unreadable = True
    if unreadable:
        raise ScenarioError(f"UNREADABLE:{path.name}")
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


def validate_scenario(spec: ScenarioSpec, answer: Answer) -> None:
    """Validate DTOs through the same parsers and constraints as scenario files."""
    # The DTO has a full IncidentScenario; the file format omits its derived ID.
    # JSON round-tripping maps DTO tuples to the file format's JSON arrays.
    scenario_data = json.loads(json.dumps(asdict(spec)))
    scenario_data["incident"].pop("scenario_id")
    answer_data = json.loads(json.dumps(asdict(answer)))
    if (_spec(scenario_data), _answer(answer_data)) != (spec, answer):
        raise ScenarioError("SCENARIO_DTO_MISMATCH")
    if (spec.scenario_id, spec.version) != (answer.scenario_id, answer.version):
        raise ScenarioError("SCENARIO_ANSWER_MISMATCH")
    if spec.kind == "fault" and (answer.injected is None or answer.signature is None):
        raise ScenarioError("FAULT_REQUIRES_INJECTED_AND_SIGNATURE")
    if spec.kind == "normal" and (
        answer.injected is not None or answer.signature is not None
    ):
        raise ScenarioError("NORMAL_FORBIDS_INJECTED_AND_SIGNATURE")


def load_scenario(directory: Path) -> tuple[ScenarioSpec, Answer]:
    spec = _spec(_read(directory / "scenario.json"))
    answer = _answer(_read(directory / "answer.json"))
    validate_scenario(spec, answer)
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


# -- scenario schema v2 (opspilot-eval-scenario-2, #430) ----------------------
#
# Appended after the v1 loader, which stays byte-identical. Same discipline as
# v1: stimulus and answer in separate files, exact keys on every object, and
# everything that names the category, polarity or injected fault lives only in
# ``answer.json`` (V2-R3).

SCENARIO_SCHEMA_V2 = "opspilot-eval-scenario-2"
ScenarioEntry = Literal["question", "alertmanager"]
ScenarioCategory = Literal[
    "regression",
    "noise",
    "missing_telemetry",
    "duplicate_event",
    "model_failure",
    "prompt_injection",
    "wrong_target",
]
ScenarioPolarity = Literal["positive", "negative"]
InjectorKind = Literal[
    "flagd", "source_unreachable", "model_transport", "otlp_span", "alert_delivery"
]
SCENARIO_ENTRIES: tuple[str, ...] = ("question", "alertmanager")
SCENARIO_CATEGORIES: tuple[str, ...] = (
    "regression",
    "noise",
    "missing_telemetry",
    "duplicate_event",
    "model_failure",
    "prompt_injection",
    "wrong_target",
)
SCENARIO_POLARITIES: tuple[str, ...] = ("positive", "negative")
INJECTOR_KINDS: tuple[str, ...] = (
    "flagd",
    "source_unreachable",
    "model_transport",
    "otlp_span",
    "alert_delivery",
)
DATASET_GROUPS: tuple[str, ...] = ("regression", "capability")


@dataclass(frozen=True)
class ScenarioSpecV2:
    scenario_id: str
    version: str
    entry: ScenarioEntry
    incident: IncidentScenario
    stimulus: Stimulus


@dataclass(frozen=True)
class Injector:
    kind: InjectorKind
    params: Mapping[str, Any]


@dataclass(frozen=True)
class ExpectedCounts:
    incidents: int | None
    runs: int | None
    episodes: int | None


@dataclass(frozen=True)
class ExpectedOutcome:
    final_states: tuple[str, ...]
    decision: str | None
    handoff_reason: str | None
    counts: ExpectedCounts
    forbidden_tool_requests: tuple[str, ...]
    canary: str | None


@dataclass(frozen=True)
class AnswerV2:
    scenario_id: str
    version: str
    category: ScenarioCategory
    polarity: ScenarioPolarity
    injectors: tuple[Injector, ...]
    expected_outcome: ExpectedOutcome
    expected_output: tuple[str, ...]
    signature: SignatureRule | None
    reference_report: str | None


_V2_VERSION = "2"
_V2_SCENARIO_KEYS = frozenset(
    {"scenario_id", "version", "entry", "incident", "stimulus"}
)
_V2_ANSWER_KEYS = frozenset(
    {
        "scenario_id",
        "version",
        "category",
        "polarity",
        "injectors",
        "expected_outcome",
        "expected_output",
        "signature",
        "reference_report",
    }
)
_V2_INJECTOR_KEYS = frozenset({"kind", "params"})
_V2_FLAGD_PARAM_KEYS = frozenset({"flag", "variant"})
_V2_OUTCOME_KEYS = frozenset(
    {
        "final_states",
        "decision",
        "handoff_reason",
        "counts",
        "forbidden_tool_requests",
        "canary",
    }
)
_V2_COUNT_KEYS = frozenset({"incidents", "runs", "episodes"})


def _v2_id(value: str, where: str) -> str:
    if (
        len(value) != 10
        or not value.startswith("dv2-")
        or any(ch not in "0123456789abcdef" for ch in value[4:])
    ):
        raise ScenarioError(f"BAD_SCENARIO_ID:{where}")
    return value


def _v2_version(data: Mapping[str, Any], where: str) -> str:
    if data.get("version") != _V2_VERSION:
        raise ScenarioError(f"BAD_VERSION:{where}")
    return _V2_VERSION


def _optional_text(data: Mapping[str, Any], key: str, where: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ScenarioError(f"BAD_OPTIONAL_TEXT:{where}.{key}")
    return value


def _string_list(data: Mapping[str, Any], key: str, where: str) -> tuple[str, ...]:
    """A possibly empty list of non-empty strings."""
    value = data.get(key)
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ScenarioError(f"BAD_TEXT_LIST:{where}.{key}")
    return tuple(cast(list[str], value))


def _spec_v2(data: Mapping[str, Any]) -> ScenarioSpecV2:
    _exact_keys(data, _V2_SCENARIO_KEYS, "scenario")
    scenario_id = _v2_id(_text(data, "scenario_id", "scenario"), "scenario")
    version = _v2_version(data, "scenario")
    entry = data.get("entry")
    if entry not in SCENARIO_ENTRIES:
        raise ScenarioError("BAD_ENTRY")
    incident = _object(data, "incident", "scenario")
    _exact_keys(incident, _INCIDENT_KEYS, "scenario.incident")
    stimulus = _object(data, "stimulus", "scenario")
    _exact_keys(stimulus, _STIMULUS_KEYS, "scenario.stimulus")
    return ScenarioSpecV2(
        scenario_id=scenario_id,
        version=version,
        entry=cast(ScenarioEntry, entry),
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


def _injector(data: Any) -> Injector:
    if not isinstance(data, dict):
        raise ScenarioError("NOT_AN_OBJECT:answer.injectors[]")
    _exact_keys(data, _V2_INJECTOR_KEYS, "answer.injectors[]")
    kind = data.get("kind")
    if kind not in INJECTOR_KINDS:
        raise ScenarioError("BAD_INJECTOR_KIND")
    params = _object(data, "params", "answer.injectors[]")
    if kind == "flagd":
        _exact_keys(params, _V2_FLAGD_PARAM_KEYS, "answer.injectors[].params")
        _text(params, "flag", "injector.params")
        _text(params, "variant", "injector.params")
    # A JSON round trip detaches the params from the parsed document.
    return Injector(
        kind=cast(InjectorKind, kind), params=json.loads(json.dumps(params))
    )


def _count(data: Mapping[str, Any], key: str) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ScenarioError(f"BAD_COUNT:{key}")
    return value


def _expected_outcome(data: Mapping[str, Any]) -> ExpectedOutcome:
    where = "answer.expected_outcome"
    _exact_keys(data, _V2_OUTCOME_KEYS, where)
    final_states = _string_list(data, "final_states", where)
    if not final_states:
        raise ScenarioError("EMPTY_FINAL_STATES")
    counts = _object(data, "counts", where)
    _exact_keys(counts, _V2_COUNT_KEYS, f"{where}.counts")
    return ExpectedOutcome(
        final_states=final_states,
        decision=_optional_text(data, "decision", where),
        handoff_reason=_optional_text(data, "handoff_reason", where),
        counts=ExpectedCounts(
            incidents=_count(counts, "incidents"),
            runs=_count(counts, "runs"),
            episodes=_count(counts, "episodes"),
        ),
        forbidden_tool_requests=_string_list(data, "forbidden_tool_requests", where),
        canary=_optional_text(data, "canary", where),
    )


def _reference_report(data: Mapping[str, Any]) -> str | None:
    value = _optional_text(data, "reference_report", "answer")
    if value is None:
        return None
    parts = value.split("/")
    if (
        value.startswith("/")
        or "\\" in value
        or any(p in ("", ".", "..") for p in parts)
    ):
        # Relative to the scenario directory and never outside it.
        raise ScenarioError("BAD_REFERENCE_REPORT")
    return value


def _answer_v2(data: Mapping[str, Any]) -> AnswerV2:
    _exact_keys(data, _V2_ANSWER_KEYS, "answer")
    scenario_id = _v2_id(_text(data, "scenario_id", "answer"), "answer")
    version = _v2_version(data, "answer")
    category = data.get("category")
    if category not in SCENARIO_CATEGORIES:
        raise ScenarioError("BAD_CATEGORY")
    polarity = data.get("polarity")
    if polarity not in SCENARIO_POLARITIES:
        raise ScenarioError("BAD_POLARITY")
    injectors = data.get("injectors")
    if not isinstance(injectors, list):
        raise ScenarioError("BAD_INJECTORS")
    expected = _texts(data, "expected_output", "answer")
    if not expected:
        raise ScenarioError("EMPTY_EXPECTED_OUTPUT")
    signature = data.get("signature")
    return AnswerV2(
        scenario_id=scenario_id,
        version=version,
        category=cast(ScenarioCategory, category),
        polarity=cast(ScenarioPolarity, polarity),
        injectors=tuple(_injector(item) for item in injectors),
        expected_outcome=_expected_outcome(_object(data, "expected_outcome", "answer")),
        expected_output=expected,
        signature=None
        if signature is None
        else _signature(_object(data, "signature", "answer")),
        reference_report=_reference_report(data),
    )


def stimulus_leaks(raw_scenario: str, answer: AnswerV2) -> tuple[str, ...]:
    """Answer-side words found in a scenario file's text (V2-R3/R4, H5).

    Case-insensitive: every category word (with ``_`` or a space), the
    polarity words and their v1 forms (``fault``/``normal``), and the flag
    names this scenario's injectors set. ``incident.kind`` is therefore a
    neutral value in v2; polarity lives only in the answer.
    """
    text = raw_scenario.lower()
    words: set[str] = set()
    for category in SCENARIO_CATEGORIES:
        words.update({category, category.replace("_", " ")})
    words.update(SCENARIO_POLARITIES)
    words.update({"fault", "normal"})
    for injector in answer.injectors:
        flag = injector.params.get("flag") if injector.kind == "flagd" else None
        if isinstance(flag, str):
            words.add(flag.lower())
    return tuple(sorted(word for word in words if word in text))


def _read_text(path: Path) -> str:
    unreadable = False
    raw = ""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        unreadable = True
    if unreadable:
        raise ScenarioError(f"UNREADABLE:{path.name}")
    return raw


def _parse_object(raw: str, name: str) -> dict[str, Any]:
    invalid = False
    data: Any = None
    try:
        data = json.loads(raw)
    except ValueError:
        invalid = True
    if invalid:
        raise ScenarioError(f"UNREADABLE:{name}")
    if not isinstance(data, dict):
        raise ScenarioError(f"NOT_AN_OBJECT:{name}")
    return data


def load_scenario_v2(directory: Path) -> tuple[ScenarioSpecV2, AnswerV2]:
    """One v2 scenario directory: ``scenario.json`` plus ``answer.json``."""
    raw_scenario = _read_text(directory / "scenario.json")
    spec = _spec_v2(_parse_object(raw_scenario, "scenario.json"))
    answer = _answer_v2(
        _parse_object(_read_text(directory / "answer.json"), "answer.json")
    )
    if spec.scenario_id != directory.name:
        raise ScenarioError("SCENARIO_ID_NOT_DIRECTORY")
    if (spec.scenario_id, spec.version) != (answer.scenario_id, answer.version):
        raise ScenarioError("SCENARIO_ANSWER_MISMATCH")
    leaks = stimulus_leaks(raw_scenario, answer)
    if leaks:
        raise ScenarioError(f"STIMULUS_LEAK:{','.join(leaks)}")
    return spec, answer


def validate_scenario_v2(spec: ScenarioSpecV2, answer: AnswerV2) -> None:
    """Validate v2 DTOs through the same parsers as the v2 files (#431 S1)."""
    if not isinstance(spec, ScenarioSpecV2) or not isinstance(answer, AnswerV2):
        raise ScenarioError("SCENARIO_DTO_MISMATCH")
    scenario_data = json.loads(json.dumps(asdict(spec)))
    scenario_data["incident"].pop("scenario_id")
    answer_data = json.loads(json.dumps(asdict(answer)))
    if (_spec_v2(scenario_data), _answer_v2(answer_data)) != (spec, answer):
        raise ScenarioError("SCENARIO_DTO_MISMATCH")
    if (spec.scenario_id, spec.version) != (answer.scenario_id, answer.version):
        raise ScenarioError("SCENARIO_ANSWER_MISMATCH")


#: Either schema's scenario and answer DTOs: the offline evaluator takes both
#: (#431 S1); everything it reads exists on both, except v1 ``kind`` and
#: ``injected`` (see ``is_fault_scenario`` / ``injected_fault``).
AnyScenarioSpec = ScenarioSpec | ScenarioSpecV2
AnyAnswer = Answer | AnswerV2


def is_fault_scenario(spec: AnyScenarioSpec, answer: AnyAnswer) -> bool:
    """Whether the deterministic gate requires the evidence signature: v1 by
    ``kind``, v2 by the answer carrying a signature (#431 S1)."""
    if isinstance(spec, ScenarioSpecV2):
        return answer.signature is not None
    return spec.kind == "fault"


def injected_fault(answer: AnyAnswer) -> InjectedFault | None:
    """The v1 injected fault (service/operation names the judge diagnosis is
    checked against); a v2 answer names its faults as injectors only, so it
    has none and a factual causal diagnosis cannot be recomputed as passing."""
    return answer.injected if isinstance(answer, Answer) else None


def _scenario_dirs(group: Path) -> list[Path]:
    if not group.is_dir():
        return []
    return [
        path
        for path in sorted(group.iterdir())
        if path.is_dir() and not path.name.startswith(("_", "."))
    ]


def load_scenarios_v2(root: Path) -> tuple[tuple[ScenarioSpecV2, AnswerV2], ...]:
    """Every scenario under ``root/regression`` and ``root/capability``.

    Directories starting with ``_`` (schema samples) are skipped. The
    regression group holds only ``regression`` answers and the capability
    group none; duplicate IDs across groups are refused.
    """
    loaded: list[tuple[ScenarioSpecV2, AnswerV2]] = []
    seen: set[str] = set()
    for group in DATASET_GROUPS:
        for directory in _scenario_dirs(root / group):
            spec, answer = load_scenario_v2(directory)
            if (answer.category == "regression") != (group == "regression"):
                raise ScenarioError(f"CATEGORY_GROUP_MISMATCH:{spec.scenario_id}")
            if spec.scenario_id in seen:
                raise ScenarioError(f"DUPLICATE_SCENARIO_ID:{spec.scenario_id}")
            seen.add(spec.scenario_id)
            loaded.append((spec, answer))
    return tuple(sorted(loaded, key=lambda pair: pair[0].scenario_id))


def dataset_tree_hash(root: Path) -> str:
    """sha256 over every file under ``root``: ``path\\0content\\0`` in POSIX
    relative-path order (V2-R7 ``dataset`` dimension)."""
    import hashlib

    if not root.is_dir():
        raise ScenarioError("DATASET_ROOT_MISSING")
    files = sorted(
        (path.relative_to(root).as_posix(), path)
        for path in root.rglob("*")
        if path.is_file()
    )
    digest = hashlib.sha256()
    for relative, path in files:
        unreadable = False
        content = b""
        try:
            content = path.read_bytes()
        except OSError:
            unreadable = True
        if unreadable:
            raise ScenarioError(f"UNREADABLE:{relative}")
        digest.update(relative.encode("utf-8") + b"\0" + content + b"\0")
    return digest.hexdigest()
