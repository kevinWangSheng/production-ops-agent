"""Live kind/OTel scenario driver (eval PR-C): engineer entry point.

Interface: docs/tasks/2026-10-18-eval-pr-c-interface.md. Contract:
docs/tasks/2026-10-18-eval-pr-c-contract.md (r1, U1-U3, A1-A11).

    .venv/bin/python -m scripts.eval_live --mode live --round R \\
        --scenario ID [--scenario ID ...] | --scenario all --n N --parallel 1 \\
        --holder H --batch B --flagd-url URL --pg-port PORT [--dry-run]
    .venv/bin/python -m scripts.eval_live --recover --round R \\
        --holder H --batch B --flagd-url URL

One round runs its slots strictly in order on the shared lab (D3/D4): per
slot an attempt journal is written before any setup (P10), the fault is
injected under a persisted restore intent (U2), an independent observation
gates the Run (U1), the real product Run is recorded through the B2 hooks
(A5/A6), and the fault is restored and the lab washed whatever happened.
Every started attempt gets an ``opspilot-eval-summary-1`` summary (A8); the
raw ledger, recording and pre-observation stay in ignored private paths
(A9). Occupation is checked, never claimed or released (Q4).

Engineer script, not product code: it is the isolated harness that may
inject faults; the product Run it drives keeps its read-only tools.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import signal
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from opspilot.acceptance import IncidentOutcome
from opspilot.evaluation import (
    REQUIRED_DIMENSIONS,
    Answer,
    AnswerV2,
    ArtifactRef,
    AttemptJournal,
    AttemptRecord,
    DeliveredRecord,
    DimensionValue,
    EvaluationCapture,
    Manifest,
    PlannedSlot,
    ReportCapture,
    RoundCapture,
    ScenarioError,
    ScenarioSpec,
    ScenarioSpecV2,
    SignatureRule,
    StorageError,
    TraceRecord,
    Window,
    check_recording_binding,
    evidence_from_recording,
    load_recording,
    load_scenarios,
    load_scenarios_v2,
    manifest_hash,
    write_attempt,
    write_round,
    write_summary,
)
from opspilot.observation.release_policy import sha256
from opspilot.tools.executor import QueryScope
from opspilot.tools.otel_demo import OtelDemoConfig
from scripts.eval_live_inject import ModelTransportFault as ModelTransportFault
from scripts.eval_live_lab import (
    _STOP,
    NOISE_NOT_OBSERVED,
    SOURCE_QUERY,
    UNREACHABLE_PORT,
    V2_FAULT_FLAGS,
    WINDOW_SECONDS,
    AttemptLedger,
    Committed,
    FaultIntent,
    InjectionPlan,
    Interrupted,
    LabError,
    LiveDeps,
    MetricNoiseRule,
    PgInstance,
    PreObservation,
    Provenance,
    active_marker,
    append_run_start,
    await_fault,
    check_flagd_url,
    check_occupation,
    check_run_start,
    checkpoint,
    commit_fault,
    confirm_all_normal,
    fault_for,
    fault_gate,
    fault_guard,
    handle_signal,
    interrupt_scope,
    mark_injector,
    noise_gate,
    noise_rule,
    normal_window,
    observation_json,
    protected,
    read_state,
    request_stop,
    restore_fault,
    stop_requested,
    validate_injectors,
    write_private,
)
from scripts.eval_live_lab import (
    NOISE_RULES as NOISE_RULES,
)
from scripts.eval_live_lab import InjectorHarness as InjectorHarness

ROOT = Path(__file__).resolve().parents[1]
ROUND_PATTERN = r"[a-z0-9][a-z0-9.-]{0,62}"
FRESHNESS = timedelta(seconds=300)
PREFLIGHT_FLAG_SECONDS = 15.0
Status = Literal["ran", "setup_failed", "aborted", "not_started"]


# -- DTOs ----------------------------------------------------------------------


@dataclass(frozen=True)
class Slot:
    spec: ScenarioSpec | ScenarioSpecV2
    answer: Answer | AnswerV2
    repetition: int
    # v2 only (#431): the validated injectors; None for a v1 slot.
    plan: InjectionPlan | None = None

    @property
    def scenario_id(self) -> str:
        return self.spec.scenario_id

    @property
    def restore_intent_count(self) -> int:
        """v2: one shared restore intent per attempt; v1: one per fault."""
        if self.plan is not None:
            return self.plan.restore_intent_count
        return 1 if getattr(self.spec, "kind", None) == "fault" else 0


def is_v2(slot: Slot) -> bool:
    return slot.plan is not None


@dataclass(frozen=True)
class RoundConfig:
    round_id: str
    slots: tuple[Slot, ...]
    holder: str
    batch: str
    pg_port: int
    # None: built by ``run_round`` once the preflight passed (r5 provenance).
    manifest: Manifest | None
    command: tuple[str, ...] = ()
    n: int = 1
    # F1 closure R7: the systems that answer under this attempt's one
    # injection, in order; ``("opspilot",)`` is the unpaired driver.
    pair: tuple[str, ...] = ("opspilot",)
    # write ``runs/<attempt>/pair.json`` (every scheduled window, R7)
    pair_record: bool = False


@dataclass(frozen=True)
class InvestigationRequest:
    attempt: AttemptRecord
    spec: ScenarioSpec
    pg: PgInstance
    recording_dir: Path
    ledger_dir: Path
    round_id: str
    pre_observation: PreObservation
    manifest: Manifest
    # The Run must be submitted by then (U1 freshness, Q8); later is stale.
    submit_not_after: datetime | None = None
    # v2 (#431): the attempt's injectors (source/model/alert are applied by
    # the investigator, in-process) and the first alert episode's startsAt.
    plan: InjectionPlan | None = None
    alert_starts_at: datetime | None = None
    # v2 (#432 seams §2): the answer's canary, only to count its occurrences
    # in the attempt PG's model output; never sent to the Run.
    canary: str | None = None


@dataclass(frozen=True)
class InvestigationResult:
    product_run_id: str | None
    outcome: IncidentOutcome | None
    report: ReportCapture | None
    recording_ref: ArtifactRef | None
    recording_error: str | None
    traces: tuple[TraceRecord, ...]
    submitted_at: datetime | None
    started_at: datetime | None
    ended_at: datetime | None
    usage: Mapping[str, Any]
    error: str | None
    # Accepted for symmetry with the interface; the driver re-derives the
    # visible evidence from the verified recording and ignores these.
    delivered: Mapping[str, DeliveredRecord] = field(default_factory=dict)
    authorized: Mapping[str, Window] = field(default_factory=dict)
    delivery_records: Mapping[str, bytes] = field(default_factory=dict)
    # v2 (#431 S5): ``runs/<attempt_id>/intake.json`` as the investigator saw
    # the intake (deliveries and PG counts); None for v1.
    intake: Mapping[str, Any] | None = None
    # v2 (#432 seams §2): ``runs/<attempt_id>/projection.json``, the read-only
    # PG projection Q20 is judged from; None for v1.
    projection: Mapping[str, Any] | None = None


@dataclass
class SlotResult:
    scenario_id: str
    scenario_version: str
    repetition: int
    attempt_id: str | None = None
    status: Status = "not_started"
    stage: str | None = None
    reason: str | None = None
    closure_reason: str | None = None
    product_run_id: str | None = None
    summary: dict[str, Any] | None = None
    fault: dict[str, Any] | None = None
    pre_observation: dict[str, Any] | None = None
    timings: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RoundResult:
    round_id: str
    status: Literal["finished", "stopped", "interrupted", "refused", "recovered"]
    exit_code: int
    reason: str | None
    slots: tuple[SlotResult, ...]


class _Stop(Exception):
    """End the round after this attempt (restore/evidence failure)."""

    def __init__(self, exit_code: int, reason: str) -> None:
        super().__init__(reason)
        self.exit_code = exit_code
        self.reason = reason


# -- paths and refs ------------------------------------------------------------


def round_evidence(deps: LiveDeps, round_id: str) -> Path:
    return deps.evidence_root / round_id


def attempt_ledger_dir(deps: LiveDeps, round_id: str, attempt_id: str) -> Path:
    return attempt_root(deps) / round_id / attempt_id


def attempt_root(deps: LiveDeps) -> Path:
    """#432 seams §10 A1: the attempts' private ledgers; the coordination
    files stay in ``deps.ledger_root``."""
    root = getattr(deps, "attempt_root", None)
    return deps.ledger_root if root is None else root


def recording_dir(deps: LiveDeps, round_id: str, attempt_id: str) -> Path:
    return deps.recording_root / f"eval-live-{round_id}-{attempt_id}"


def _ref(path: str, raw: bytes, media_type: str) -> ArtifactRef:
    return ArtifactRef(path, sha256(raw), len(raw), media_type, "private")


def attempt_resolver(
    round_id: str, attempt_id: str, *, ledger_root: Path, recording_root: Path
) -> Callable[[ArtifactRef], bytes]:
    """``recording.json``/``blobs/*`` from the recording, the rest from the
    attempt's private ledger directory (interface section 10)."""
    ledger = ledger_root / round_id / attempt_id
    recording = recording_root / f"eval-live-{round_id}-{attempt_id}"

    def resolve(ref: ArtifactRef) -> bytes:
        base = (
            recording
            if ref.path == "recording.json" or ref.path.startswith("blobs/")
            else ledger
        )
        return (base / ref.path).read_bytes()

    return resolve


def _round_resolver(
    deps: LiveDeps, round_id: str, attempt_ids: Sequence[str]
) -> Callable[[ArtifactRef], bytes]:
    """One resolver for a whole round: refs are content-addressed, so the
    attempt holding a ``recording.json``/delivery is the one whose bytes match."""
    evidence = round_evidence(deps, round_id)
    resolvers = [
        attempt_resolver(
            round_id,
            attempt_id,
            ledger_root=attempt_root(deps),
            recording_root=deps.recording_root,
        )
        for attempt_id in attempt_ids
    ]

    def resolve(ref: ArtifactRef) -> bytes:
        if ref.path.startswith("runs/"):
            return (evidence / ref.path).read_bytes()
        for candidate in resolvers:
            try:
                raw = candidate(ref)
            except OSError:
                continue
            if sha256(raw) == ref.sha256:
                return raw
        raise FileNotFoundError(ref.path)

    return resolve


# -- manifest -----------------------------------------------------------------


def _file_hash(*paths: Path) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.read_bytes())
    return digest.hexdigest()


def build_manifest(
    round_id: str,
    slots: Sequence[Slot],
    n: int,
    command: Sequence[str],
    *,
    scenarios_root: Path,
    config: Any = None,
    provenance: Provenance | None = None,
) -> Manifest:
    """The round manifest, frozen before the first attempt (P4/P10, Q1).

    Pure apart from reading repository files: the code version comes in as
    ``provenance`` (r5), never from a process started here.
    """
    from opspilot.evaluation.live import registry_hash, scope_policy_hash
    from opspilot.investigation.limits import M1_FROZEN_LIMITS
    from opspilot.investigation.loop import ACCEPTED_RESPONSE_MODEL
    from opspilot.tools import otel_demo
    from opspilot.tools.registry import canonical_hash

    def hashed(value: str) -> DimensionValue:
        return DimensionValue("hash", value, None)

    def none(reason: str) -> DimensionValue:
        return DimensionValue("none", None, reason)

    commit = None if provenance is None else provenance.commit
    clean = provenance is not None and provenance.clean
    # v2 (#431 S2): the round's one binding class decides the registry
    # (an unreachable source changes the target) and the scope policy.
    entry, source = round_binding(slots)
    if source != "none":
        from scripts.eval_live_inject import unreachable_config

        config = unreachable_config(config or OtelDemoConfig(), source)
    registrations = otel_demo.otel_demo_registrations(config)
    versions = otel_demo.otel_demo_versions()
    specs = sorted({(s.spec.scenario_id, s.spec.version) for s in slots})
    scenario_files = [
        scenario_dir(scenarios_root, sid) / name
        for sid, _version in specs
        for name in ("scenario.json",)
    ]
    answer_files = [
        scenario_dir(scenarios_root, sid) / "answer.json" for sid, _version in specs
    ]
    dims: dict[str, DimensionValue] = {
        "code": DimensionValue("version", commit, None)
        if commit and clean
        else DimensionValue("unknown", None, "worktree not clean or commit unknown"),
        "upstream": none("no upstream code in the live driver"),
        "scenarios": hashed(_file_hash(*scenario_files)),
        "answers": hashed(_file_hash(*answer_files)),
        "evidence_rules": hashed(_file_hash(*answer_files)),
        "model_request": DimensionValue("version", ACCEPTED_RESPONSE_MODEL, None),
        "prompt": hashed(canonical_hash(versions)),
        "tools_schema": DimensionValue(
            "version", versions["tool_schema_revision"], None
        ),
        "projection": hashed(_file_hash(ROOT / "opspilot/tools/otel_demo.py")),
        "registry": hashed(registry_hash(registrations.tools, registrations.targets)),
        # The per-Run scope policy without Run identity (Q1); a claimed
        # scope whose policy differs fails check_recording_binding.
        "permissions": hashed(
            scope_policy_hash(template_scope(config, alert=entry == "alertmanager"))
        ),
        "profile": DimensionValue("version", "otel-demo", None),
        "adapter": hashed(_file_hash(ROOT / "scripts/eval_live.py")),
        "evaluator": hashed(
            _file_hash(*sorted((ROOT / "opspilot/evaluation").glob("*.py")))
        ),
        "judge_config": none("PR-C runs no judge"),
        "calibration_pack": none("PR-C runs no judge"),
        "limits": hashed(canonical_hash(M1_FROZEN_LIMITS.as_json())),
        "kind_environment": hashed(
            _file_hash(*sorted((ROOT / "scripts/kind_lab").glob("*.yaml")))
        ),
        "subject_initial_state": none("fresh PostgreSQL per attempt"),
        "session": none("one fresh Run per attempt"),
        "human_feedback": none("no human control in a live attempt"),
        "workload": DimensionValue("version", "otel-demo-loadgen", None),
        "knowledge_snapshot": none("no knowledge store in the live driver"),
        "replay_fixture": none("live mode records, it does not replay"),
        "n": DimensionValue("version", str(n), None),
        "seed": none("fixed scenario-major order, no randomness"),
        "command": hashed(canonical_hash(list(command))),
        "clock_policy": DimensionValue(
            "version",
            ALERT_CLOCK_POLICY
            if entry == "alertmanager"
            else "event-clock/frozen-end-real-monotonic",
            None,
        ),
        "runbook": none("no approved knowledge revision is sent into the Run"),
        "dataset": dataset_dimension(scenarios_root),
    }
    assert set(dims) == set(REQUIRED_DIMENSIONS)
    return Manifest(round_id, dims)


ALERT_CLOCK_POLICY = "alert-frame/received-at/event-clock"


def is_v2_root(scenarios_root: Path) -> bool:
    """A v2 dataset root: at least one group directory (``dataset_dimension``
    refuses a root with only one of them)."""
    from opspilot.evaluation.model import DATASET_GROUPS

    return any((scenarios_root / g).is_dir() for g in DATASET_GROUPS)


def scenario_dir(scenarios_root: Path, scenario_id: str) -> Path:
    """A scenario's directory: ``<root>/<id>`` (v1) or ``<root>/<group>/<id>``."""
    from opspilot.evaluation.model import DATASET_GROUPS

    for group in DATASET_GROUPS:
        candidate = scenarios_root / group / scenario_id
        if candidate.is_dir():
            return candidate
    return scenarios_root / scenario_id


def validate_round_bindings(slots: Sequence[Slot]) -> None:
    """Seams revision 2: ``LabError`` with ``.code == "ROUND_BINDING_MIXED"``
    when the slots span more than one binding class."""
    round_binding(slots)


def round_binding(slots: Sequence[Slot]) -> tuple[str, str]:
    """The round's binding class (#431 S2): (entry, unreachable source or
    "none"); v1 slots are ("question", "none"). Mixed: ``ROUND_BINDING_MIXED``."""
    classes = {
        ("question", "none") if slot.plan is None else slot.plan.binding_class
        for slot in slots
    }
    if len(classes) > 1:
        raise LabError("ROUND_BINDING_MIXED")
    return next(iter(classes), ("question", "none"))


def dataset_dimension(scenarios_root: Path) -> DimensionValue:
    """V2-R7 ``dataset`` for a round's scenario root.

    ``<dataset name>:<tree hash>`` (R8; the name is the root directory's,
    such as ``dev-v2``) only for a complete v2 dataset: both group
    directories present and ``load_scenarios_v2`` accepting the tree. A root
    with neither group is the v1 ``evals/scenarios`` set and records ``none``.
    A root with just one group, or one that fails to load, raises
    ``ScenarioError``: recording it as ``none`` would let the round pass as
    well-formed.
    """
    from opspilot.evaluation.model import (
        DATASET_GROUPS,
        ScenarioError,
        dataset_tree_hash,
        load_scenarios_v2,
    )

    present = [g for g in DATASET_GROUPS if (scenarios_root / g).is_dir()]
    if not present:
        return DimensionValue(
            "none", None, "v1 scenario set (evals/scenarios), not a versioned dataset"
        )
    if len(present) != len(DATASET_GROUPS):
        missing = sorted(set(DATASET_GROUPS) - set(present))
        raise ScenarioError(f"DATASET_INCOMPLETE:{','.join(missing)}")
    load_scenarios_v2(scenarios_root)
    return DimensionValue(
        "hash",
        f"{dataset_name(scenarios_root)}:{dataset_tree_hash(scenarios_root)}",
        None,
    )


#: R8: a dataset name is its root directory's name (``dev-v2``, a sealed
#: ``holdout-<id>``); anything else could not be told apart in a manifest.
DATASET_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


def dataset_name(scenarios_root: Path) -> str:
    """The ``dataset`` dimension's name part (F1 closure R8)."""
    from opspilot.evaluation.model import ScenarioError

    name = scenarios_root.resolve().name
    if not DATASET_NAME.fullmatch(name):
        raise ScenarioError("DATASET_NAME_INVALID")
    return name


def template_scope(
    config: OtelDemoConfig | None = None, *, alert: bool = False
) -> QueryScope:
    """A QueryScope as ``otel_demo_executor_factory`` issues one for a fresh
    (non-alert) Run -- or, with ``alert``, for an alert Run, whose policy
    states a default query window (#431 S2) -- with placeholder Run identity:
    only its policy part, which ``scope_policy_hash`` keeps, is meaningful
    (public, interface r5)."""
    from datetime import timezone

    from opspilot.tools import otel_demo
    from opspilot.tools.executor import QueryScope
    from opspilot.tools.outcomes import Window as ToolWindow
    from opspilot.tools.registry import TargetRegistry, ToolRegistry

    registrations = otel_demo.otel_demo_registrations(config)
    end = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return QueryScope(
        scope_id="template",
        subject_kind="incident",
        subject_id="template",
        run_id="template",
        control_generation=0,
        global_suspension_generation=0,
        target_suspension_generation=0,
        registry_revision=TargetRegistry(list(registrations.targets)).revision,
        tool_registry_revision=ToolRegistry(registrations.tools).revision,
        target_ids=frozenset({otel_demo.TARGET_ID}),
        tool_names=frozenset(item.name for item in registrations.tools),
        window=ToolWindow(end - timedelta(seconds=otel_demo.OBSERVATION_SECONDS), end),
        default_query_window=ToolWindow(end - timedelta(hours=1), end)
        if alert
        else None,
        deadline=end,
    )


# -- round --------------------------------------------------------------------


def fault_signatures(deps: LiveDeps) -> tuple[SignatureRule, ...]:
    """The distinct signatures of every fault scenario ("three classes")."""
    rules: dict[str, SignatureRule] = {}
    if is_v2_root(deps.scenarios_root):
        # v2 (#431): every non-null signature, whatever the category.
        for _spec, answer2 in load_scenarios_v2(deps.scenarios_root):
            if answer2.signature is not None:
                rules[answer2.signature.rule_id] = answer2.signature
        return tuple(rules[key] for key in sorted(rules))
    for spec, answer in load_scenarios(deps.scenarios_root):
        if spec.kind == "fault" and answer.signature is not None:
            rules[answer.signature.rule_id] = answer.signature
    return tuple(rules[key] for key in sorted(rules))


def _refuse(config: RoundConfig, reason: str) -> RoundResult:
    slots = tuple(
        SlotResult(s.spec.scenario_id, s.spec.version, s.repetition)
        for s in config.slots
    )
    if stop_requested():
        # A stop requested before or during the preflight (R10): 130, not 3.
        return RoundResult(config.round_id, "interrupted", 130, "INTERRUPTED", slots)
    return RoundResult(config.round_id, "refused", 3, reason, slots)


def preflight(config: RoundConfig, deps: LiveDeps) -> str | None:
    """A refusal code, or None; nothing here writes or mutates anything."""
    environment = deps.env.check()
    if environment is not None:
        return environment
    source = deps.investigator_source
    # No account for the Runs: refused before any lab call (#284).
    if source is not None and source.credential() == "absent":
        return "ENV_UNCONFIRMED"
    if round_evidence(deps, config.round_id).exists():
        return "ROUND_EXISTS"
    try:
        check_occupation(
            deps.ledger_root, config.holder, config.batch, deps.clock.now()
        )
        if read_state(deps.ledger_root) is not None:
            return "FAULT_RECOVERY_REQUIRED"
        with fault_guard(deps.ledger_root):
            pass
        failures = deps.identity.verify()
        if failures:
            return "LAB_IDENTITY_UNCONFIRMED"
        if source is not None:
            # One bounded read-only query with the Runs' own account, on
            # the lab whose identity was just confirmed (V2-R9).
            if source.probe() != 200:
                return "ENV_UNCONFIRMED"
        if deps.ports.in_use(config.pg_port):
            return "PG_PORT_IN_USE"
        v2 = any(is_v2(slot) for slot in config.slots)
        confirm_all_normal(
            deps,
            deps.flags.read(),
            budget_seconds=PREFLIGHT_FLAG_SECONDS,
            whitelist=V2_FAULT_FLAGS if v2 else None,
        )
        check_slots(config.slots)
        if any(s.plan is not None and s.plan.source for s in config.slots) and (
            deps.ports.in_use(UNREACHABLE_PORT)
        ):
            return "SOURCE_PORT_IN_USE"
        # Everything append_run_start checks, before begin_round's external
        # call: after it, a batch log failure is no longer a refusal.
        check_run_start(deps.ledger_root, config.round_id)
    except LabError as exc:
        return exc.code
    except OSError:
        # A guard/occupation file under the private ledger root cannot be
        # opened: nothing was touched, so this is a refusal (exit 3).
        return "LAB_LEDGER_UNAVAILABLE"
    return None


def check_slots(slots: Sequence[Slot]) -> None:
    """Every slot's injection is whitelisted (v1: its one flag; v2: the
    whole injector list) and the round has one binding class."""
    for slot in slots:
        if isinstance(slot.spec, ScenarioSpecV2):
            assert isinstance(slot.answer, AnswerV2)
            validate_injectors(slot.spec.entry, slot.answer.injectors)
        elif slot.spec.kind == "fault":
            injected = slot.answer.injected  # type: ignore[union-attr]
            assert injected is not None
            fault_for(injected.flag, injected.variant)
    round_binding(slots)


def _source_record(deps: LiveDeps) -> dict[str, Any] | None:
    """What ``lifecycle.json`` records of the passed source check (#284): a
    passed preflight means the probe was answered 200. No credential value."""
    source = deps.investigator_source
    if source is None:
        return None
    return {
        "credential": source.credential(),
        "query": SOURCE_QUERY,
        "http_status": 200,
        "result": "confirmed",
    }


def _retained(deps: LiveDeps, round_id: str) -> tuple[bool, bool]:
    """(a restore intent exists, it belongs to this round). An unreadable
    intent counts as both: the preflight saw none, and it must be recovered."""
    try:
        state = read_state(deps.ledger_root)
    except (LabError, OSError):
        return True, True
    if state is None:
        return False, False
    return True, state.get("round") == round_id


# Exit precedence (r13/r14): 4 > 5 > 130 > 1. A stop with 3 after the start
# (the reservation vanished) ranks with 130; equal ranks keep the first.
_RANK = {4: 4, 5: 3, 130: 2, 3: 2, 1: 1}


def _outranks(new: int, current: int | None) -> bool:
    return _RANK.get(new, 0) > _RANK.get(current or 0, 0)


def run_round(config: RoundConfig, deps: LiveDeps) -> RoundResult:
    with interrupt_scope():
        return _run_round(config, deps)


def _run_round(config: RoundConfig, deps: LiveDeps) -> RoundResult:
    refusal = preflight(config, deps)
    if refusal is not None:
        return _refuse(config, refusal)
    source = _source_record(deps)
    if config.manifest is None:
        try:
            provenance = None if deps.provenance is None else deps.provenance.read()
        except LabError as exc:
            return _refuse(config, exc.code)
        try:
            manifest = build_manifest(
                config.round_id,
                config.slots,
                config.n,
                config.command,
                scenarios_root=deps.scenarios_root,
                config=getattr(deps.investigator, "config", None),
                provenance=provenance,
            )
        except OSError:
            return _refuse(config, "SCENARIO_DIRECTORY_INVALID")
        config = replace(config, manifest=manifest)
    try:
        signatures = fault_signatures(deps)
    except (OSError, ScenarioError):
        return _refuse(config, "SCENARIO_DIRECTORY_INVALID")
    # First external setup of the round (the LangSmith project), only once
    # the preflight passed: a refused round makes no external call (r4).
    begin = getattr(deps.investigator, "begin_round", None)
    if begin is not None:
        checkpoint()  # before and after the project preparation (R1)
        try:
            begin(config.round_id)
        except LabError as exc:
            return _refuse(config, exc.code)
        checkpoint()
    results = [
        SlotResult(s.spec.scenario_id, s.spec.version, s.repetition)
        for s in config.slots
    ]
    try:
        append_run_start(
            deps.ledger_root,
            config.round_id,
            config.holder,
            config.batch,
            deps.clock.now(),
        )
    except (LabError, OSError) as exc:
        # The preflight already checked the batch log (check_run_start), so
        # this is a change since then; begin_round has run, so it is no
        # longer a refusal: the round stops before any attempt (exit 1).
        code = exc.code if isinstance(exc, LabError) else "LAB_LEDGER_UNAVAILABLE"
        for result in results:
            result.reason = "ROUND_STOPPED"
        return RoundResult(config.round_id, "stopped", 1, code, tuple(results))
    status: Literal["finished", "stopped", "interrupted"] = "finished"
    exit_code: int | None = None
    reason: str | None = None
    journals: list[tuple[str, ArtifactRef]] = []
    summaries: dict[str, ArtifactRef] = {}
    assert config.manifest is not None
    round_state = {"manifest": config.manifest}
    balances: list[dict[str, Any]] = []
    evidence_dir = round_evidence(deps, config.round_id)
    try:
        with active_marker(
            deps.ledger_root,
            config.holder,
            config.batch,
            config.round_id,
            deps.clock.now(),
        ):
            for index, slot in enumerate(config.slots):
                try:
                    checkpoint()  # before every slot
                    _run_slot(
                        config,
                        deps,
                        slot,
                        results[index],
                        signatures,
                        round_state,
                        journals,
                        summaries,
                        balances,
                    )
                except _Stop as stop:
                    status = "stopped" if stop.exit_code != 130 else "interrupted"
                    exit_code, reason = stop.exit_code, stop.reason
                    for later in results[index + 1 :]:
                        later.reason = "ROUND_STOPPED"
                    break
                except (Interrupted, KeyboardInterrupt):
                    # A checkpoint outside every attempt-level handler (e.g.
                    # before a slot): the round outputs are still written.
                    request_stop()
                    status, exit_code, reason = "interrupted", 130, "INTERRUPTED"
                    for later in results[index + 1 :]:
                        later.reason = "ROUND_STOPPED"
                    break
                except (OSError, StorageError):
                    # An evidence write outside the slot's own handlers (r11).
                    status, exit_code, reason = "stopped", 5, "EVIDENCE_WRITE_FAILED"
                    for later in results[index + 1 :]:
                        later.reason = "ROUND_STOPPED"
                    break
    except LabError as exc:
        # The reservation vanished or changed between the preflight and here.
        status, exit_code, reason = "stopped", 3, exc.code
    except (Interrupted, KeyboardInterrupt):
        # While the active marker was being taken (its lock wait): the round
        # started, so its outputs are still written.
        request_stop()
        if _outranks(130, exit_code):
            status, exit_code, reason = "interrupted", 130, "INTERRUPTED"
    except OSError:
        # The active marker could not be written or removed: the lab
        # coordination is incomplete, so the round never reports success.
        if _outranks(1, exit_code):
            status, exit_code, reason = "stopped", 1, "LAB_LEDGER_UNAVAILABLE"
    manifest = round_state["manifest"]
    # The round outputs are written in a protected section; a stop requested
    # meanwhile is read afterwards (r16), so the final JSON always comes out
    # and then the stop applies (r13: 4 > 5 > 130).
    with protected():
        try:
            aggregate_error = _write_round(
                config, deps, manifest, journals, summaries, evidence_dir
            )
        except (Interrupted, KeyboardInterrupt):
            request_stop()
            aggregate_error = None
        retained, own = _retained(deps, config.round_id)
    if own and _outranks(4, exit_code):
        # This round's intent is still there although no closure reported a
        # failure (a path cut short): it needs --recover, so exit 4 (r13).
        status, exit_code, reason = "stopped", 4, "FAULT_RECOVERY_REQUIRED"
    if stop_requested() and _outranks(130, exit_code):
        status, exit_code, reason = "interrupted", 130, "INTERRUPTED"
    # A missing aggregate outranks an interrupt and a stop with 1 (e.g.
    # PG_STOP_FAILED, kept in lifecycle), never a retained fault.
    if aggregate_error is not None and _outranks(5, exit_code):
        status, exit_code, reason = "stopped", 5, aggregate_error
    if exit_code is None:
        exit_code = 0 if all(r.status == "ran" for r in results) else 1
    with protected():
        try:
            _write_lifecycle(
                config,
                deps,
                manifest,
                results,
                status,
                reason,
                retained,
                balances,
                evidence_dir,
                source,
            )
        except OSError:
            # Like a journal/summary write failure (r11): exit 5 with the
            # final JSON still printed; a retained fault (4) keeps its code.
            if _outranks(5, exit_code):
                status, exit_code, reason = "stopped", 5, "EVIDENCE_WRITE_FAILED"
        except (Interrupted, KeyboardInterrupt):
            request_stop()
    if stop_requested() and _outranks(130, exit_code):
        status, exit_code, reason = "interrupted", 130, "INTERRUPTED"
    return RoundResult(config.round_id, status, exit_code, reason, tuple(results))


def _write_round(
    config: RoundConfig,
    deps: LiveDeps,
    manifest: Manifest,
    journals: list[tuple[str, ArtifactRef]],
    summaries: dict[str, ArtifactRef],
    evidence_dir: Path,
) -> str | None:
    if not journals:
        return None
    capture = RoundCapture(
        manifest=manifest,
        planned=tuple(
            PlannedSlot(s.spec.scenario_id, s.spec.version, "live", s.repetition)
            for s in config.slots
        ),
        journals=tuple(ref for _attempt, ref in journals),
        summaries=summaries,
    )
    try:
        write_round(
            evidence_dir / "aggregate.json",
            capture,
            resolve=_round_resolver(
                deps, config.round_id, [attempt for attempt, _ref in journals]
            ),
        )
    except StorageError as exc:
        return f"AGGREGATE_{exc.code}"
    except OSError:
        return "EVIDENCE_WRITE_FAILED"
    return None


def _write_lifecycle(
    config: RoundConfig,
    deps: LiveDeps,
    manifest: Manifest,
    results: Sequence[SlotResult],
    status: str,
    reason: str | None,
    retained: bool,
    balances: list[dict[str, Any]],
    evidence_dir: Path,
    investigator_source: Mapping[str, Any] | None = None,
) -> None:
    document = {
        "schema": "opspilot-eval-live-lifecycle-1",
        "round": config.round_id,
        "manifest_hash": manifest_hash(manifest),
        "command": list(config.command),
        "round_status": status,
        "reason": reason,
        "retained_fault": retained,
        "recovered_at": None,
        "balances": balances,
        "investigator_source": investigator_source,
        "slots": [
            {
                "scenario_id": r.scenario_id,
                "scenario_version": r.scenario_version,
                "repetition": r.repetition,
                "attempt_id": r.attempt_id,
                "status": r.status,
                "stage": r.stage,
                "reason": r.reason,
                "closure_reason": r.closure_reason,
                "product_run_id": r.product_run_id,
                "summary": r.summary,
                "fault": r.fault,
                "pre_observation": r.pre_observation,
                "timings": r.timings,
            }
            for r in results
        ],
    }
    evidence_dir.mkdir(parents=True, exist_ok=True)
    with (evidence_dir / "lifecycle.json").open("x") as stream:
        stream.write(json.dumps(document, indent=2, sort_keys=True) + "\n")


@dataclass
class _Attempt:
    """Mutable working state of one attempt while it runs."""

    record: AttemptRecord
    ledger: AttemptLedger
    started_at: datetime
    status: Status = "setup_failed"
    stage: str = "setup"
    reason: str | None = None
    investigating: bool = False
    result: InvestigationResult | None = None
    gate: PreObservation | None = None
    pg: PgInstance | None = None
    injected: bool = False
    stop: _Stop | None = None
    closure_reason: str | None = None
    interrupted: bool = False
    evidence_failed: bool = False  # a private evidence write raised (exit 5)
    marks: dict[str, float] = field(default_factory=dict)
    # F1 closure R7: one entry per system of a paired attempt (``pair.json``)
    pair: list[dict[str, Any]] = field(default_factory=list)
    baseline_p95_ms: float | None = None
    lab_restarts_before: str | None = None


def _balance(deps: LiveDeps) -> Decimal | None:
    try:
        return deps.balance.balance()
    except Exception:  # noqa: BLE001 - a balance read never fails an attempt
        return None


def _run_slot(
    config: RoundConfig,
    deps: LiveDeps,
    slot: Slot,
    out: SlotResult,
    signatures: tuple[SignatureRule, ...],
    round_state: dict[str, Manifest],
    journals: list[tuple[str, ArtifactRef]],
    summaries: dict[str, ArtifactRef],
    balances: list[dict[str, Any]],
) -> None:
    clock = deps.clock
    attempt_id = str(uuid4())
    manifest = round_state["manifest"]
    record = AttemptRecord(
        attempt_id=attempt_id,
        round_id=config.round_id,
        manifest_hash=manifest_hash(manifest),
        scenario_id=slot.spec.scenario_id,
        scenario_version=slot.spec.version,
        mode="live",
        repetition=slot.repetition,
        started=True,
        status="unknown",
        replay_miss=False,
        product_run_id=None,
    )
    run_dir = round_evidence(deps, config.round_id) / "runs" / attempt_id
    started_at = clock.now()
    # The journal, the private ledger and its first event: one protected
    # section, so a started attempt never lacks either (r16).
    with protected():
        try:
            run_dir.mkdir(parents=True, exist_ok=False)
            journal_ref = write_attempt(
                run_dir / "attempt.json",
                AttemptJournal(record, slot.spec, slot.answer, started_at=started_at),
            )
        except (OSError, StorageError):
            raise _Stop(5, "EVIDENCE_WRITE_FAILED") from None
        out.attempt_id = attempt_id
        journals.append(
            (
                attempt_id,
                replace(journal_ref, path=f"runs/{attempt_id}/attempt.json"),
            )
        )
        ledger = AttemptLedger(
            attempt_ledger_dir(deps, config.round_id, attempt_id), clock
        )
        state = _Attempt(record, ledger, started_at)
        state.marks["start"] = clock.monotonic()
        ledger.event("journal", "attempt_started", attempt_id=attempt_id)
    if stop_requested():
        state.interrupted = True
        _fail(state, "INTERRUPTED")
    if ledger.write_failed:
        # The private ledger cannot be written: nothing is touched without
        # it; the attempt still gets its summary and the round stops (5).
        _fail(state, "EVIDENCE_WRITE_FAILED")
    # From the journal on, every interrupt -- balance reads, setup outside the
    # guard, closure -- ends here, so the started attempt always gets its
    # summary and the round its outputs (independent review P2).
    before = after = None
    try:
        if state.reason is None:
            checkpoint()  # before and after every balance read (R1)
            before = _balance(deps)
            checkpoint()
            _attempt_body(config, deps, slot, state, signatures, round_state)
    except (Interrupted, KeyboardInterrupt):
        request_stop()
        state.interrupted = True
        _fail(state, "INTERRUPTED")
    except OSError:
        # Occupation or guard file outside the guarded body (its closure,
        # if any, already ran): a setup failure, never an escape.
        _fail(state, "LAB_LEDGER_UNAVAILABLE")
    except Exception:  # noqa: BLE001 - a driver bug still gets its summary
        _fail(state, "DRIVER_ERROR")
    try:
        checkpoint()
        after = _balance(deps)
        checkpoint()
    except (Interrupted, KeyboardInterrupt):
        request_stop()
        state.interrupted = True
    # After the closure everything is frozen and written in one protected
    # section: the cost, the slot fields, the timings and the summary (r16,
    # #273 item 4). A stop requested meanwhile is read once it is done.
    with protected():
        balances.append(_cost(attempt_id, before, after))
        out.status = state.status
        out.stage = state.stage
        out.reason = state.reason
        out.closure_reason = state.closure_reason
        out.product_run_id = state.record.product_run_id
        out.pre_observation = (
            None if state.gate is None else observation_json(state.gate)
        )
        out.timings = _timings(state, clock.now())
        try:
            summary_ref = _write_summary(
                config,
                deps,
                slot,
                state,
                round_state["manifest"],
                run_dir,
                balances[-1],
            )
        except (OSError, StorageError, ValueError):
            # Exit precedence 4 > 5 > 130 (r13): a retained fault outranks the
            # missing summary, which outranks an interrupt.
            out.status, out.stage = state.status, state.stage
            out.reason = state.reason
            out.fault = _fault_json(state)
            if state.stop is not None and state.stop.exit_code == 4:
                raise state.stop from None
            raise _Stop(5, "EVIDENCE_WRITE_FAILED") from None
        # Verifying the recording may have turned a ran attempt into aborted;
        # the public slot must show what the summary says.
        out.status, out.stage = state.status, state.stage
        out.reason = state.reason
        summaries[attempt_id] = replace(
            summary_ref, path=f"runs/{attempt_id}/summary.json"
        )
        intake = None if state.result is None else state.result.intake
        if intake is not None and not _write_intake(run_dir, intake):
            state.evidence_failed = True
        if is_v2(slot) and not _write_expectations(run_dir, slot, state):
            state.evidence_failed = True
        if config.pair_record and not _write_pair(run_dir, config, state, deps):
            state.evidence_failed = True
        out.summary = {
            "path": f"runs/{attempt_id}/summary.json",
            "sha256": summary_ref.sha256,
        }
        out.fault = _fault_json(state)
    if stop_requested():
        state.interrupted = True
    # Exit precedence 4 > 5 > 130 > 1 (_RANK): LAB_FAULT_FOREIGN and
    # PG_STOP_FAILED stop the round with 1 and stay in the slot's reasons.
    if state.stop is not None and state.stop.exit_code == 4:
        raise state.stop
    if state.ledger.write_failed or state.evidence_failed:
        raise _Stop(5, "EVIDENCE_WRITE_FAILED")
    if state.interrupted:
        raise _Stop(130, "INTERRUPTED")
    if state.stop is not None:
        raise state.stop


def _attempt_body(
    config: RoundConfig,
    deps: LiveDeps,
    slot: Slot,
    state: _Attempt,
    signatures: tuple[SignatureRule, ...],
    round_state: dict[str, Manifest],
) -> None:
    """Setup, investigation and closure, all under one held fault guard so
    nobody else can touch the lab between the injection and its restore."""
    clock = deps.clock
    try:
        state.stage = "occupation"
        check_occupation(deps.ledger_root, config.holder, config.batch, clock.now())
        state.stage = "guard"
        with fault_guard(deps.ledger_root):
            try:
                _guarded(config, deps, slot, state, signatures, round_state)
            except (Interrupted, KeyboardInterrupt):
                request_stop()
                state.interrupted = True
                _fail(state, "INTERRUPTED")
            except LabError as exc:
                _fail(state, exc.code)
            except OSError:
                # A private evidence/state write (pre-observation, flags,
                # recording directory): restored below, then exit 5 (r11).
                state.evidence_failed = True
                _fail(state, "EVIDENCE_WRITE_FAILED")
            except Exception:  # noqa: BLE001 - a driver bug still restores the lab
                _fail(state, "DRIVER_ERROR")
            finally:
                _closure(config, deps, slot, state, signatures)
    except LabError as exc:
        # The reservation or the guard was not ours: nothing was touched.
        _fail(state, exc.code)
        _closure(config, deps, slot, state, signatures)


def _fail(state: _Attempt, code: str) -> None:
    if state.result is not None:
        # The OpsPilot Run already finished (a paired attempt's later stage
        # failed): its status and reason stand, and the pair entries stay as
        # they are (an unrun one stays not_started, so the scheduler reads a
        # harness interruption); the code goes to the ledger only.
        state.ledger.event(state.stage, "failed", reason=code)
    else:
        if state.reason is None:
            state.reason = code
        state.status = "aborted" if state.investigating else "setup_failed"
        state.ledger.event(state.stage, "failed", reason=state.reason)
    if code == "LAB_FAULT_FOREIGN":
        # Another holder's fault is live: no further slot may start (exit 1;
        # exit 4 stays for this driver's own restore/wash failure).
        state.stop = _Stop(1, code)
    elif code == "PG_STOP_FAILED":
        # A failed start could not stop the server it launched.
        _pg_stop_failed(state, code)


def _pg_stop_failed(state: _Attempt, code: str) -> None:
    """A PG this attempt started may still listen: the closure is incomplete,
    so the attempt is not ``ran`` and the round stops with exit 1 (r14; exit 4
    stays for a retained fault, which this is not)."""
    state.ledger.event("pg", "stop_failed", reason=code)
    if state.status == "ran":
        state.status = "aborted"
    if state.closure_reason is None:
        state.closure_reason = code
    if state.reason is None:
        state.stage, state.reason = "closure", code
    if state.stop is None:
        state.stop = _Stop(1, code)


def _guarded(
    config: RoundConfig,
    deps: LiveDeps,
    slot: Slot,
    state: _Attempt,
    signatures: tuple[SignatureRule, ...],
    round_state: dict[str, Manifest],
) -> None:
    clock, ledger = deps.clock, state.ledger
    spec, answer = slot.spec, slot.answer
    checkpoint()
    state.stage = "identity"
    if deps.identity.verify():
        raise LabError("LAB_IDENTITY_UNCONFIRMED")
    if read_state(deps.ledger_root) is not None:
        # Appeared after the preflight: not ours, so touch nothing (r3/7).
        raise LabError("LAB_FAULT_FOREIGN")
    if config.pair_record:
        # R7 / r0.1-3: before anything is injected, the window's baseline
        # checkout p95 and the telemetry stores' restart counts.
        state.stage = "pair_baseline"
        state.baseline_p95_ms = _lab_value(deps, BASELINE_P95_QUERY)
        state.lab_restarts_before = _lab_restarts(deps)
    state.stage = "pg"
    if deps.ports.in_use(config.pg_port):
        raise LabError("PG_PORT_IN_USE")
    directory = deps.pg_root / config.round_id / state.record.attempt_id / "pg"
    if directory.exists() or directory.is_symlink():
        raise LabError("PG_DIRECTORY_EXISTS")
    state.pg = deps.pg.start(directory, config.pg_port)
    ledger.event("pg", "started", port=config.pg_port)
    submit_not_after: datetime | None
    alert_starts_at: datetime | None = None
    if slot.plan is not None:
        gate, submit_not_after, alert_starts_at = _setup_v2(
            config, deps, slot, state, signatures
        )
    elif spec.kind == "fault":  # type: ignore[union-attr]
        injected = answer.injected  # type: ignore[union-attr]
        assert injected is not None and answer.signature is not None
        state.stage = "inject"
        intent = FaultIntent(
            config.round_id,
            state.record.attempt_id,
            config.holder,
            config.batch,
            fault_for(injected.flag, injected.variant),
        )
        checkpoint()  # before the injection
        state.injected = True

        def committed(fault: Committed) -> None:
            # Recorded inside the commit's protected section, before the
            # propagation wait (R6, #273 item 3).
            state.marks["requested_at"] = fault.requested_at.timestamp()

        result = commit_fault(intent, deps=deps, ledger=ledger, record=committed)
        effective_at = await_fault(result, deps=deps, ledger=ledger)
        state.marks["effective_at"] = effective_at.timestamp()
        state.stage = "pre_observation"
        gate = fault_gate(answer.signature, confirmed_at=effective_at, deps=deps)
        state.gate = gate
        with protected():
            ledger.write("pre-observation.json", _raw_observation(gate))
        if not gate.passed:
            raise LabError(gate.reason or "FAULT_NOT_OBSERVED")
        newest = max(span.start for span in gate.matched)
        submit_not_after = newest + FRESHNESS
    else:
        state.stage = "flags_baseline"
        confirmed = confirm_all_normal(deps, deps.flags.read())
        state.stage = "pre_observation"
        gate = normal_window(signatures, not_before=confirmed, deps=deps)
        state.gate = gate
        with protected():
            ledger.write("pre-observation.json", _raw_observation(gate))
        if not gate.passed or gate.window is None:
            raise LabError(gate.reason or "NORMAL_WINDOW_UNCONFIRMED")
        submit_not_after = gate.window.end + FRESHNESS
    state.marks["setup_end"] = clock.monotonic()
    # §8: the recording's parent directory is created (700) when missing; the
    # package directory itself is left to ``record()`` (OUTPUT_EXISTS rule).
    deps.recording_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not config.pair_record:
        _investigate(
            config,
            deps,
            slot,
            state,
            round_state,
            gate,
            submit_not_after,
            alert_starts_at,
        )
        return
    _paired(
        config,
        deps,
        slot,
        state,
        signatures,
        round_state,
        gate,
        submit_not_after,
        alert_starts_at,
    )


def _investigate(
    config: RoundConfig,
    deps: LiveDeps,
    slot: Slot,
    state: _Attempt,
    round_state: dict[str, Manifest],
    gate: PreObservation,
    submit_not_after: datetime | None,
    alert_starts_at: datetime | None,
) -> None:
    """The OpsPilot Run of an attempt, after its setup (unchanged order)."""
    clock, ledger = deps.clock, state.ledger
    spec, answer = slot.spec, slot.answer
    state.stage = "investigate"
    state.investigating = True
    request = InvestigationRequest(
        attempt=state.record,
        spec=spec,
        pg=state.pg,
        recording_dir=recording_dir(deps, config.round_id, state.record.attempt_id),
        ledger_dir=ledger.directory,
        round_id=config.round_id,
        pre_observation=gate,
        manifest=round_state["manifest"],
        submit_not_after=submit_not_after,
        plan=slot.plan,
        alert_starts_at=alert_starts_at,
        canary=answer.expected_outcome.canary if isinstance(answer, AnswerV2) else None,
    )
    ledger.event("investigate", "started")
    checkpoint()
    try:
        result = deps.investigator.investigate(request)
    except (Interrupted, KeyboardInterrupt):
        raise
    except Exception:  # noqa: BLE001 - any investigator failure is an abort
        state.status, state.reason = "aborted", "INVESTIGATOR_FAILED"
        ledger.event("investigate", "failed", reason=state.reason)
        return
    state.result = result
    state.investigating = False
    state.marks["investigation_end"] = clock.monotonic()
    if result.product_run_id is not None:
        state.record = replace(state.record, product_run_id=result.product_run_id)
    stale = result.error == "PRE_OBSERVATION_STALE" or (
        result.submitted_at is not None
        and submit_not_after is not None
        and result.submitted_at > submit_not_after
    )
    if stale:
        # Still setup_failed after the call (r3/2); a Run that was already
        # created keeps its ID in the attempt and the private ledger.
        state.investigating = False
        state.status, state.reason = "setup_failed", "PRE_OBSERVATION_STALE"
        state.stage = "submit"
        ledger.event(
            "submit",
            "stale",
            product_run_id=result.product_run_id,
            submitted_at=None
            if result.submitted_at is None
            else result.submitted_at.isoformat(),
        )
    elif result.error is not None:
        state.status, state.reason = "aborted", result.error
    elif handed_off_without_run(result):
        # #431 S4: the intake handed the alert to a human without a Run, so
        # there is no model call and nothing to record.
        state.status = "ran"
    elif result.recording_ref is None or result.recording_error is not None:
        state.status = "aborted"
        state.reason = result.recording_error or "RECORDING_INCOMPLETE"
    else:
        state.status = "ran"
    ledger.event("investigate", "finished", status=state.status, reason=state.reason)


# -- F1 closure R7: paired attempts ---------------------------------------------

#: The systems a paired attempt may run, and the second system's order.
PAIR_ORDERS = {
    "opspilot-first": ("opspilot", "holmes"),
    "holmes-first": ("holmes", "opspilot"),
    # an alert-entry window (no HolmesGPT entry): OpsPilot alone, same record
    "opspilot-only": ("opspilot",),
}
PAIR_FILE = "pair.json"
PAIR_SCHEMA = "opspilot-eval-pair-1"
PROC_STAT = Path("/proc/stat")


@dataclass(frozen=True)
class BaselineRequest:
    """What the HolmesGPT side of a paired attempt gets: the same scenario,
    the same live injection, its own private output directory."""

    attempt_id: str
    round_id: str
    spec: ScenarioSpecV2
    answer: AnswerV2
    plan: InjectionPlan
    output_dir: Path
    position: int
    submit_not_after: datetime | None


@dataclass(frozen=True)
class BaselineResult:
    status: Literal["ran", "setup_failed", "aborted"]
    reason: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    #: a HolmesGPT investigation call was made (the setup was over)
    product_run_created: bool = False
    #: the run ended on a model provider 5xx/transport failure (not injected)
    provider_error: bool = False
    output_sha256: str | None = None
    #: HolmesGPT's own attempt id (never OpsPilot's) and its raw directory
    attempt_id: str | None = None
    output_path: str | None = None


def holmes_attempt_id(round_id: str, opspilot_attempt_id: str) -> str:
    """S7b's id for the HolmesGPT side of a paired window: deterministic,
    never the OpsPilot id, safe as a directory name (same formula as
    ``scripts.holmes_baseline.dataset_runner.holmes_attempt_id``)."""
    digest = hashlib.sha256(f"{round_id}\0{opspilot_attempt_id}".encode()).hexdigest()
    return f"holmes-{digest[:24]}"


def _proc_stat() -> str | None:
    try:
        return PROC_STAT.read_text(encoding="ascii")
    except OSError:
        return None


def _paired(
    config: RoundConfig,
    deps: LiveDeps,
    slot: Slot,
    state: _Attempt,
    signatures: tuple[SignatureRule, ...],
    round_state: dict[str, Manifest],
    gate: PreObservation,
    submit_not_after: datetime | None,
    alert_starts_at: datetime | None,
) -> None:
    """Both systems under the attempt's one injection, in ``config.pair``
    order (R7). Before the second system the fault (or the normal window) is
    re-confirmed and a synthetic span written again, so it gets the same
    fresh evidence the first one had; a failed re-confirmation fails that
    system's setup only. The attempt's own status stays OpsPilot's."""
    ledger = state.ledger
    state.pair = [
        {
            "system": system,
            "position": n,
            "status": "not_started",
            "reason": None,
            "started_at": None,
            "ended_at": None,
            "product_run_created": False,
            "provider_error": False,
            "host_noise": None,
            "attempt_id": state.record.attempt_id if system == "opspilot" else None,
            "output_path": None,
            "output_sha256": None,
        }
        for n, system in enumerate(config.pair, start=1)
    ]
    for entry in state.pair:
        with _holmes_turn(deps, state, entry["system"]) as waited:
            if entry["position"] > 1 or waited:
                state.stage = "pair_regate"
                checkpoint()
                try:
                    gate, submit_not_after = _regate(deps, slot, state, signatures)
                except LabError as exc:
                    entry.update(
                        status="setup_failed", reason=f"PAIR_REGATE:{exc.code}"
                    )
                    if entry["system"] == "opspilot":
                        state.status, state.reason = "setup_failed", entry["reason"]
                    ledger.event("pair_regate", "failed", reason=entry["reason"])
                    return
            _pair_slot(
                config,
                deps,
                slot,
                state,
                round_state,
                entry,
                gate,
                submit_not_after,
                alert_starts_at,
            )


#: HolmesGPT's helper containers have fixed names (S7b), so one HolmesGPT
#: slot at a time per host: labs on one host take turns through this lock.
HOLMES_LOCK = Path("/tmp/opspilot-holmes-baseline.lock")
HOLMES_LOCK_POLL_SECONDS = 5.0


def holmes_lock_enabled() -> bool:
    """OPSPILOT_HOLMES_HOST_LOCK=1/0 decides; unset, the lock is on until
    ``kind_lab`` names lab instances (``SUFFIX``), since S7b then suffixes
    its helper container names per lab and HolmesGPT slots may overlap."""
    import os

    from scripts import kind_lab

    configured = os.environ.get("OPSPILOT_HOLMES_HOST_LOCK")
    if configured in ("0", "1"):
        return configured == "1"
    return not hasattr(kind_lab, "SUFFIX")


@contextmanager
def _holmes_turn(deps: LiveDeps, state: _Attempt, system: str) -> Iterator[bool]:
    """Hold the host-wide HolmesGPT lock for a ``holmes`` slot; yields
    whether it had to wait (the gate is then re-confirmed)."""
    if system != "holmes" or not holmes_lock_enabled():
        yield False
        return
    import fcntl

    waited = False
    with HOLMES_LOCK.open("a") as handle:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not waited:
                    state.ledger.event("baseline", "waiting_for_host_lock")
                waited = True
                checkpoint()
                deps.clock.sleep(HOLMES_LOCK_POLL_SECONDS)
        try:
            yield waited
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _pair_slot(
    config: RoundConfig,
    deps: LiveDeps,
    slot: Slot,
    state: _Attempt,
    round_state: dict[str, Manifest],
    entry: dict[str, Any],
    gate: PreObservation,
    submit_not_after: datetime | None,
    alert_starts_at: datetime | None,
) -> None:
    from scripts.eval_pair_schedule import steal_percent

    clock = deps.clock
    before = _proc_stat()
    entry["started_at"] = clock.now().isoformat()
    if entry["system"] == "opspilot":
        _investigate(
            config,
            deps,
            slot,
            state,
            round_state,
            gate,
            submit_not_after,
            alert_starts_at,
        )
        entry.update(
            status=state.status,
            reason=state.reason,
            product_run_created=state.record.product_run_id is not None,
            provider_error=_recording_provider_error(
                recording_dir(deps, config.round_id, state.record.attempt_id)
            ),
        )
    else:
        _baseline(config, deps, slot, state, entry, submit_not_after)
    entry["ended_at"] = clock.now().isoformat()
    after = _proc_stat()
    steal = None if before is None or after is None else steal_percent(before, after)
    entry["host_noise"] = {"steal_percent": steal, "load1": _load1()}


def _regate(
    deps: LiveDeps,
    slot: Slot,
    state: _Attempt,
    signatures: tuple[SignatureRule, ...],
) -> tuple[PreObservation, datetime | None]:
    """The second system's fresh gate: the signature (or a clean normal
    window) in the last ``RECONFIRM_SECONDS``, each noise flag's own errors
    again, and the synthetic span written again; returns its deadline."""
    clock, ledger = deps.clock, state.ledger
    plan, answer = slot.plan, slot.answer
    assert plan is not None and isinstance(answer, AnswerV2)
    since = clock.now() - timedelta(seconds=RECONFIRM_SECONDS)
    effective = state.marks.get("effective_at")
    if effective is not None:
        since = max(since, datetime.fromtimestamp(effective, tz=since.tzinfo))
    if answer.signature is not None:
        gate = fault_gate(answer.signature, confirmed_at=since, deps=deps)
        reason = SIGNATURE_NOT_RECONFIRMED
    else:
        window_start = clock.now() - timedelta(seconds=WINDOW_SECONDS)
        gate = normal_window(signatures, not_before=window_start, deps=deps)
        reason = "NORMAL_WINDOW_UNCONFIRMED"
    with protected():
        ledger.write("pre-observation-pair.json", _raw_observation(gate))
    if not gate.passed:
        raise LabError(gate.reason or reason)
    if answer.signature is not None:
        deadline: datetime | None = max(span.start for span in gate.matched) + FRESHNESS
    else:
        assert gate.window is not None
        deadline = gate.window.end + FRESHNESS
    for flag, rule in _metric_last(
        noise_only_rules(plan, answer) + noise_rules(plan, answer)
    ):
        seen = noise_gate(rule, confirmed_at=since, deps=deps)
        with protected():
            ledger.write(
                f"pre-observation-pair-noise-{flag}.json", _raw_observation(seen)
            )
        if not seen.passed:
            raise LabError(NOISE_NOT_OBSERVED)
        noise_deadline = _noise_deadline(seen)
        deadline = noise_deadline if deadline is None else min(deadline, noise_deadline)
    if plan.span is not None:
        from scripts.eval_live_inject import OtlpSpanWriter, write_span

        state.stage = "pair_otlp_span"
        writer = getattr(deps, "span_writer", None) or OtlpSpanWriter()

        def planned(span: Any) -> None:
            mark_injector(
                deps.ledger_root,
                "otlp_span",
                phase="intent",
                trace_id=span.trace_id,
                span_end=span.end.isoformat(),
            )

        span = write_span(plan.span, writer=writer, clock=clock, on_planned=planned)
        mark_injector(deps.ledger_root, "otlp_span", phase="effective")
        ledger.event("pair_otlp_span", "visible", trace_id=span.trace_id)
    ledger.event("pair_regate", "passed")
    return gate, deadline


def _baseline(
    config: RoundConfig,
    deps: LiveDeps,
    slot: Slot,
    state: _Attempt,
    entry: dict[str, Any],
    submit_not_after: datetime | None,
) -> None:
    """The HolmesGPT side: never raises; its outcome lands in ``entry``."""
    runner = getattr(deps, "baseline", None)
    state.stage = "baseline"
    if runner is None:
        entry.update(status="setup_failed", reason="BASELINE_UNAVAILABLE")
        return
    assert slot.plan is not None and isinstance(slot.spec, ScenarioSpecV2)
    assert isinstance(slot.answer, AnswerV2)
    # HolmesGPT writes ``<output_dir>/<attempt_id>`` (S7b ``run_attempt``).
    output_dir = deps.recording_root / f"holmes-{config.round_id}"
    request = BaselineRequest(
        attempt_id=state.record.attempt_id,
        round_id=config.round_id,
        spec=slot.spec,
        answer=slot.answer,
        plan=slot.plan,
        output_dir=output_dir,
        position=int(entry["position"]),
        submit_not_after=submit_not_after,
    )
    state.ledger.event("baseline", "started")
    checkpoint()
    try:
        result = runner.run(request)
    except (Interrupted, KeyboardInterrupt):
        raise
    except Exception:  # noqa: BLE001 - a baseline failure is that slot's abort
        entry.update(status="aborted", reason="BASELINE_FAILED")
        state.ledger.event("baseline", "failed", reason="BASELINE_FAILED")
        return
    entry.update(
        status=result.status,
        reason=result.reason,
        product_run_created=result.product_run_created,
        provider_error=result.provider_error,
        output_sha256=result.output_sha256,
        attempt_id=result.attempt_id,
        output_path=result.output_path,
    )
    state.ledger.event(
        "baseline", "finished", status=result.status, reason=result.reason
    )


def _recording_provider_error(directory: Path) -> bool:
    """The Run's last model call failed as unavailable (provider 5xx or
    transport): the recording's ``MODEL_UNAVAILABLE`` on its last model
    event. Unreadable or absent recordings say False."""
    try:
        events = json.loads((directory / "recording.json").read_text("utf-8"))["events"]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    models = [e for e in events if isinstance(e, Mapping) and e.get("kind") == "model"]
    return bool(models) and models[-1].get("error") == "MODEL_UNAVAILABLE"


def _write_pair(
    run_dir: Path, config: RoundConfig, state: _Attempt, deps: LiveDeps
) -> bool:
    """``runs/<attempt>/pair.json``; an attempt whose shared setup failed
    before either system started shows both slots as that setup failure."""
    slots = state.pair or [
        {
            "system": system,
            "position": n,
            "status": "setup_failed",
            "reason": state.reason,
            "started_at": None,
            "ended_at": None,
            "product_run_created": False,
            "provider_error": False,
            "host_noise": None,
            "attempt_id": state.record.attempt_id if system == "opspilot" else None,
            "output_path": None,
            "output_sha256": None,
        }
        for n, system in enumerate(config.pair, start=1)
    ]
    # After the closure: the fault is restored and washed (or not).
    after = _lab_restarts(deps)
    before = state.lab_restarts_before
    document = {
        "schema": PAIR_SCHEMA,
        "attempt_id": state.record.attempt_id,
        "order": list(config.pair),
        "slots": slots,
        "baseline_p95_ms": state.baseline_p95_ms,
        "lab_restarts": {
            "before": before,
            "after": after,
            # unknown (None) when either read failed; a change voids (r3)
            "changed": None if before is None or after is None else before != after,
        },
    }
    return write_public_exclusive(run_dir, (PAIR_FILE,), document)


#: r0.1-3: the window's baseline checkout p95 (ms), read before injection.
BASELINE_P95_QUERY = (
    "histogram_quantile(0.95, sum by (le) (rate("
    'traces_span_metrics_duration_milliseconds_bucket{k8s_namespace_name="otel-demo",'
    'service_name="checkout",span_kind="SPAN_KIND_SERVER",'
    'span_name=~".*CheckoutService/PlaceOrder"}[5m])))'
)
#: The telemetry stores whose restart would wipe or gap the evidence the
#: Runs read (fault-visibility 2026-11-04: Jaeger OOMKilled under the flood).
STORE_CONTAINERS = ("jaeger", "prometheus-server")


def _lab_value(deps: LiveDeps, expr: str) -> float | None:
    """One instant query through the lab telemetry (None when it has no
    ``instant`` seam, the offline doubles, or the read fails)."""
    instant = getattr(deps.telemetry, "instant", None)
    if instant is None:
        return None
    try:
        value = instant(expr, deps.clock.now(), 5.0)
    except Exception:  # noqa: BLE001 - a record, never a gate
        return None
    return None if value is None else round(float(value), 3)


def _lab_restarts(deps: LiveDeps) -> str | None:
    """``pod/container=restarts`` of the telemetry stores, sorted; None when
    unreadable."""
    reader = getattr(deps, "lab_restarts", None)
    if reader is None:
        return None
    try:
        value = reader()
    except Exception:  # noqa: BLE001 - unknown, never a gate
        return None
    return None if value is None else str(value)


def _load1() -> float | None:
    try:
        return float(Path("/proc/loadavg").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def write_intake(path: Path, payload: Mapping[str, Any]) -> None:
    """Seams revision 2: write ``intake.json`` (section 5 shape) at ``path``,
    creating its parent directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(payload), indent=2, sort_keys=True) + "\n")


def validate_recorded_model_error(entry: Mapping[str, Any]) -> None:
    """Seams revision 2 / section 0: a recorded model error is whole -- a
    3-digit non-2xx ``status``, the complete ``response`` bytes, and the
    parent error ``MODEL_UNAVAILABLE`` -- else ``StorageError`` (a missing
    body is ``RECORDING_INCOMPLETE``, the rest ``SCHEMA_INVALID``)."""
    import re as _re

    status = entry.get("status")
    if not isinstance(status, str) or not _re.fullmatch(r"[1-5][0-9]{2}", status):
        raise StorageError("SCHEMA_INVALID")
    if 200 <= int(status) < 300:
        raise StorageError("SCHEMA_INVALID")
    response = entry.get("response")
    if response is None:
        raise StorageError("RECORDING_INCOMPLETE")
    if type(response) is not bytes:
        raise StorageError("SCHEMA_INVALID")
    if entry.get("error") != "MODEL_UNAVAILABLE":
        raise StorageError("SCHEMA_INVALID")


#: The namespace an ``unresolved_labels`` request uses when none is given.
UNRESOLVED_NAMESPACE = "otel-demo-unregistered"


def build_alertmanager_request(
    *,
    pattern: str | Mapping[str, Any],
    fingerprint: str,
    starts_at: datetime,
    token: str,
    namespace: str | None = None,
) -> dict[str, Any]:
    """Seams revision 2: the first firing delivery of ``pattern`` as
    ``{"path", "headers", "body"}``; headers are exactly Authorization and
    Content-Type, the body is the v4 webhook dict (it never holds the
    token)."""
    from scripts import eval_live_inject

    params: dict[str, Any] = (
        {"pattern": pattern} if isinstance(pattern, str) else dict(pattern)
    )
    if params["pattern"] == "unresolved_labels":
        params.setdefault("namespace", namespace or UNRESOLVED_NAMESPACE)
    body = eval_live_inject.alert_body(
        params,
        status="firing",
        fingerprint=fingerprint,
        starts_at=starts_at,
        ends_at=None,
    )
    return {
        "path": eval_live_inject.INTAKE_PATH,
        "headers": {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        "body": body,
    }


def _write_intake(run_dir: Path, intake: Mapping[str, Any]) -> bool:
    """``runs/<attempt_id>/intake.json`` (#431 S5), public, created once."""
    return write_public_exclusive(run_dir, ("intake.json",), intake)


def _write_expectations(run_dir: Path, slot: Slot, state: _Attempt) -> bool:
    """#432 seams §2: ``projection.json`` (when the investigator took one) and
    ``expectations.json`` (every started v2 attempt, setup failures included:
    decision D1), both public and created once."""
    from opspilot.evaluation.expectations import (
        check_expectations,
        expectations_document,
    )

    answer = slot.answer
    assert isinstance(answer, AnswerV2)
    result = state.result
    projection = None if result is None else result.projection
    intake = None if result is None else result.intake
    ok = True
    if projection is not None:
        ok = write_public_exclusive(run_dir, ("projection.json",), projection)
    checked = check_expectations(
        answer,
        attempt_status=state.status,
        attempt_reason=state.reason,
        projection=projection,
        intake=intake,
        target_id=slot.spec.stimulus.target_id,
    )
    document = expectations_document(answer, checked)
    return write_public_exclusive(run_dir, ("expectations.json",), document) and ok


def handed_off_without_run(result: InvestigationResult) -> bool:
    """#431 S4: the first delivery was ``handoff_created`` and no Run exists."""
    intake = result.intake
    if (
        result.product_run_id is not None
        or result.recording_ref is not None
        or not isinstance(intake, Mapping)
    ):
        return False
    deliveries = intake.get("deliveries") or []
    counts = intake.get("counts") or {}
    return (
        bool(deliveries)
        and deliveries[0].get("outcome") == "handoff_created"
        and counts.get("runs") == 0
    )


def _setup_v2(
    config: RoundConfig,
    deps: LiveDeps,
    slot: Slot,
    state: _Attempt,
    signatures: tuple[SignatureRule, ...],
) -> tuple[PreObservation, datetime | None, datetime | None]:
    """A v2 attempt's setup (#431): one restore intent for every injector,
    the flags in one write, the pre-observation, then the synthetic span.
    Returns the gate, the submission deadline and the first episode's
    startsAt (alert entry)."""
    plan = slot.plan
    assert plan is not None
    ledger, clock = state.ledger, deps.clock
    answer = slot.answer
    if not plan.injectors:
        # A v2 scenario without injectors: the v1 normal path, all four flags.
        state.stage = "flags_baseline"
        confirmed = confirm_all_normal(
            deps, deps.flags.read(), whitelist=V2_FAULT_FLAGS
        )
        state.stage = "pre_observation"
        gate = normal_window(signatures, not_before=confirmed, deps=deps)
        state.gate = gate
        with protected():
            ledger.write("pre-observation.json", _raw_observation(gate))
        if not gate.passed or gate.window is None:
            raise LabError(gate.reason or "NORMAL_WINDOW_UNCONFIRMED")
        return gate, gate.window.end + FRESHNESS, None
    state.stage = "inject"
    intent = FaultIntent(
        config.round_id,
        state.record.attempt_id,
        config.holder,
        config.batch,
        plan.flags[0] if plan.flags else None,
        also=plan.flags[1:],
        injectors=plan.injectors,
        whitelist=V2_FAULT_FLAGS,
    )
    checkpoint()  # before the injection
    state.injected = True

    def committed(fault: Committed) -> None:
        state.marks["requested_at"] = fault.requested_at.timestamp()

    result = commit_fault(intent, deps=deps, ledger=ledger, record=committed)
    effective_at = await_fault(result, deps=deps, ledger=ledger)
    state.marks["effective_at"] = effective_at.timestamp()
    if plan.flags:
        mark_injector(deps.ledger_root, "flagd", phase="effective")
    noise_only = noise_only_rules(plan, answer)
    noise_only_deadline: datetime | None = None
    # Span-gated flags first, metric ones last: a metric gate's deadline is
    # its window end, so a slow span wait after it would leave it stale.
    for flag, rule in _metric_last(noise_only):
        # #432 seams §12 A16: a noise-only scenario must carry its noise; wait
        # for the noise flag's own error first, then judge checkout clean.
        state.stage = "noise_observation"
        seen = noise_gate(rule, confirmed_at=effective_at, deps=deps)
        with protected():
            ledger.write(f"pre-observation-noise-{flag}.json", _raw_observation(seen))
        if not seen.passed:
            raise LabError(NOISE_NOT_OBSERVED)
        newest_noise = _noise_deadline(seen)
        if noise_only_deadline is None or newest_noise < noise_only_deadline:
            noise_only_deadline = newest_noise
    state.stage = "pre_observation"
    if answer.signature is not None:
        gate = fault_gate(answer.signature, confirmed_at=effective_at, deps=deps)
    elif noise_only:
        # The trailing window, so a long noise wait is not paid twice.
        since = max(effective_at, clock.now() - timedelta(seconds=WINDOW_SECONDS))
        gate = normal_window(signatures, not_before=since, deps=deps)
    else:
        gate = normal_window(signatures, not_before=effective_at, deps=deps)
    state.gate = gate
    with protected():
        ledger.write("pre-observation.json", _raw_observation(gate))
    if not gate.passed:
        raise LabError(
            gate.reason
            or (
                "FAULT_NOT_OBSERVED"
                if answer.signature
                else "NORMAL_WINDOW_UNCONFIRMED"
            )
        )
    if answer.signature is not None:
        newest = max(span.start for span in gate.matched)
        submit_not_after: datetime | None = newest + FRESHNESS
    else:
        assert gate.window is not None
        submit_not_after = gate.window.end + FRESHNESS
        if noise_only_deadline is not None:
            submit_not_after = min(submit_not_after, noise_only_deadline)
    if plan.entry == "alertmanager" and answer.signature is not None:
        # #432 seams §12 A14: an alert Run's frame ends at the delivery, so
        # wait for enough fault in it (probe d17df3ec went out 25 s after
        # one matched trace and its Run never read the fault).
        assert submit_not_after is not None
        state.stage = "alert_hold"
        gate = alert_hold(
            answer.signature,
            gate,
            effective_at=effective_at,
            submit_not_after=submit_not_after,
            deps=deps,
        )
        state.gate = gate
        with protected():
            ledger.write("pre-observation-alert-hold.json", _raw_observation(gate))
        submit_not_after = max(span.start for span in gate.matched) + FRESHNESS
    noise = noise_rules(plan, answer)
    noise_deadline: datetime | None = None
    for flag, rule in _metric_last(noise):
        # #432 seams §6: the noise flag's own errors must be in the window
        # the Run will read; its newest error also bounds the submission.
        state.stage = "noise_observation"
        seen = noise_gate(rule, confirmed_at=effective_at, deps=deps)
        with protected():
            ledger.write(f"pre-observation-noise-{flag}.json", _raw_observation(seen))
        if not seen.passed:
            raise LabError(NOISE_NOT_OBSERVED)
        newest_noise = _noise_deadline(seen)
        if noise_deadline is None or newest_noise < noise_deadline:
            noise_deadline = newest_noise
    if noise_deadline is not None:
        # #432 seams §12 A10: a long noise wait can outlast the signature's
        # freshness (probe ac449f38 went stale); re-confirm the signature in
        # the last RECONFIRM_SECONDS, then submit by the earlier deadline.
        assert answer.signature is not None
        state.stage = "signature_reconfirm"
        since = max(effective_at, clock.now() - timedelta(seconds=RECONFIRM_SECONDS))
        again = fault_gate(answer.signature, confirmed_at=since, deps=deps)
        with protected():
            ledger.write("pre-observation-reconfirm.json", _raw_observation(again))
        if not again.passed:
            raise LabError(SIGNATURE_NOT_RECONFIRMED)
        signature_deadline = max(span.start for span in again.matched) + FRESHNESS
        submit_not_after = min(signature_deadline, noise_deadline)
    if plan.span is not None:
        from scripts.eval_live_inject import OtlpSpanWriter, write_span

        state.stage = "otlp_span"
        writer = getattr(deps, "span_writer", None) or OtlpSpanWriter()

        def planned(span: Any) -> None:
            mark_injector(
                deps.ledger_root,
                "otlp_span",
                phase="intent",
                trace_id=span.trace_id,
                span_end=span.end.isoformat(),
            )

        span = write_span(plan.span, writer=writer, clock=clock, on_planned=planned)
        mark_injector(deps.ledger_root, "otlp_span", phase="effective")
        ledger.event("otlp_span", "visible", trace_id=span.trace_id)
    alert_starts_at = effective_at if plan.entry == "alertmanager" else None
    return gate, submit_not_after, alert_starts_at


#: #432 seams §12 A14: an alert-entry attempt delivers once the signature
#: gate has this many distinct matched traces, or this long after the first
#: match, whichever comes first; never later than the freshness deadline
#: minus the margin.
ALERT_HOLD_TRACES = 3
ALERT_HOLD_SECONDS = 60.0
ALERT_HOLD_MARGIN_SECONDS = 30.0
ALERT_HOLD_POLL_SECONDS = 5.0


def alert_hold(
    signature: SignatureRule,
    gate: PreObservation,
    *,
    effective_at: datetime,
    submit_not_after: datetime,
    deps: LiveDeps,
) -> PreObservation:
    """Hold an alert-entry attempt's first delivery (A14): return the latest
    passed signature observation once it has ``ALERT_HOLD_TRACES`` distinct
    traces, or ``ALERT_HOLD_SECONDS`` have passed since the first matched
    span, or the freshness deadline is ``ALERT_HOLD_MARGIN_SECONDS`` away --
    whichever comes first. A later observation that does not pass keeps the
    earlier one; the deadline always wins."""
    clock = deps.clock
    first = min(span.start for span in gate.matched)
    latest = gate
    while True:
        checkpoint()
        now = clock.now()
        traces = {span.trace_id for span in latest.matched}
        if len(traces) >= ALERT_HOLD_TRACES:
            return latest
        if now >= first + timedelta(seconds=ALERT_HOLD_SECONDS):
            return latest
        left = (submit_not_after - now).total_seconds() - ALERT_HOLD_MARGIN_SECONDS
        if left <= 0:
            return latest
        clock.sleep(min(ALERT_HOLD_POLL_SECONDS, left))
        left = (submit_not_after - clock.now()).total_seconds()
        left -= ALERT_HOLD_MARGIN_SECONDS
        if left <= 0:
            return latest
        again = fault_gate(
            signature, confirmed_at=effective_at, deps=deps, budget_seconds=left
        )
        if again.passed and len(again.matched) >= len(latest.matched):
            latest = again


#: #432 seams §12 A10: how far back the post-noise signature re-confirmation
#: looks (inside FRESHNESS, so a match leaves time to submit).
RECONFIRM_SECONDS = 240
SIGNATURE_NOT_RECONFIRMED = "SIGNATURE_NOT_RECONFIRMED"


def _noise_rules(
    plan: InjectionPlan,
) -> tuple[tuple[str, SignatureRule | MetricNoiseRule], ...]:
    return tuple(
        (flag.flag, rule)
        for flag in plan.flags
        if (rule := noise_rule(flag.flag)) is not None
    )


def noise_only_rules(
    plan: InjectionPlan, answer: Answer | AnswerV2
) -> tuple[tuple[str, SignatureRule | MetricNoiseRule], ...]:
    """#432 seams §12 A16: the noise gates of a noise-only v2 attempt -- every
    injected noise flag (``NOISE_RULES`` or ``METRIC_NOISE_RULES``) when the
    answer carries no signature (the noise negative); its noise must be seen
    before the Run."""
    if answer.signature is not None:
        return ()
    return _noise_rules(plan)


def noise_rules(
    plan: InjectionPlan, answer: Answer | AnswerV2
) -> tuple[tuple[str, SignatureRule | MetricNoiseRule], ...]:
    """#432 seams §6: the noise gates of a v2 attempt -- every injected noise
    flag (``NOISE_RULES`` or ``METRIC_NOISE_RULES``), only when the answer
    carries a signature (the noise sits beside a fault; a noise-only scenario
    keeps its normal window)."""
    if answer.signature is None:
        return ()
    return _noise_rules(plan)


def _metric_last(
    rules: tuple[tuple[str, SignatureRule | MetricNoiseRule], ...],
) -> tuple[tuple[str, SignatureRule | MetricNoiseRule], ...]:
    """The noise flags in gate order: error-span rules, then metric rules."""
    return tuple(sorted(rules, key=lambda item: isinstance(item[1], MetricNoiseRule)))


def _noise_deadline(seen: PreObservation) -> datetime:
    """A passed noise gate's submission bound: its newest error span, or for
    a metric gate (no span) the end of the window it read, plus FRESHNESS."""
    if seen.matched:
        return max(span.start for span in seen.matched) + FRESHNESS
    assert seen.window is not None
    return seen.window.end + FRESHNESS


def _closure(
    config: RoundConfig,
    deps: LiveDeps,
    slot: Slot,
    state: _Attempt,
    signatures: tuple[SignatureRule, ...],
) -> None:
    """Restore and wash (if this attempt wrote an intent), then stop its PG."""
    # The whole closure is protected (r16): the restore, the result
    # assignment and the PG stop with its retry finish whatever arrives.
    with protected():
        _close(config, deps, state, signatures)


def _close(
    config: RoundConfig,
    deps: LiveDeps,
    state: _Attempt,
    signatures: tuple[SignatureRule, ...],
) -> None:
    ledger = state.ledger
    state.marks["closure_start"] = deps.clock.monotonic()
    try:
        # Only this attempt's own intent; the caller still holds the guard.
        current = read_state(deps.ledger_root) if state.injected else None
        mine = current is not None and current.get("attempt_id") == (
            state.record.attempt_id
        )
        restored = (
            restore_fault(
                config.round_id,
                state.record.attempt_id,
                signatures,
                deps=deps,
                ledger=ledger,
            )
            if mine
            else None
        )
        if restored is not None:
            state.marks["restored_at"] = restored.normal_confirmed_at.timestamp()
            if restored.wash.window is not None:
                state.marks["washed_at"] = restored.wash.window.end.timestamp()
    except (Exception, Interrupted, KeyboardInterrupt) as exc:  # noqa: BLE001
        if isinstance(exc, LabError):
            code = exc.code
        elif isinstance(exc, (Interrupted, KeyboardInterrupt)):
            request_stop()
            code = "INTERRUPTED"
        else:
            code = "DRIVER_ERROR"
        ledger.event("restore", "failed", reason=code)
        if state.status == "ran":
            state.status = "aborted"
        # The first failure stays the attempt's reason; the closure failure
        # is recorded beside it and stops the round (the intent is retained).
        state.closure_reason = f"RESTORE_FAILED:{code}"
        if state.reason is None:
            state.stage, state.reason = "closure", state.closure_reason
        state.stop = _Stop(4, state.closure_reason)
    if state.pg is not None:
        try:
            deps.pg.stop(state.pg)
            ledger.event("pg", "stopped")
        except (Interrupted, KeyboardInterrupt):
            # Closure still finishes: stop again, still protected (P2).
            request_stop()
            state.interrupted = True
            ledger.event("pg", "stop_interrupted")
            try:
                deps.pg.stop(state.pg)
                ledger.event("pg", "stopped")
            except Exception:  # noqa: BLE001 - the round stops below
                _pg_stop_failed(state, "PG_STOP_FAILED")
        except Exception:  # noqa: BLE001 - lab recovery is unaffected
            _pg_stop_failed(state, "PG_STOP_FAILED")
    state.marks["closure_end"] = deps.clock.monotonic()


def _raw_observation(gate: PreObservation) -> dict[str, Any]:
    return {**observation_json(gate), "raw": [dict(item) for item in gate.raw]}


def _fault_json(state: _Attempt) -> dict[str, Any] | None:
    if not state.injected:
        return None
    directory = state.ledger.directory

    def digest(name: str) -> str | None:
        path = directory / name
        try:
            return sha256(str(json.loads(path.read_bytes())["raw"]).encode())
        except (OSError, ValueError, KeyError, TypeError):
            return None  # missing or unreadable: not shown, never raised

    def at(name: str) -> str | None:
        value = state.marks.get(name)
        if value is None:
            return None
        from datetime import timezone

        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()

    return {
        "original_sha256": digest("flags-original.json"),
        "injected_sha256": digest("flags-injected.json"),
        "effective_at": at("effective_at"),
        "normal_confirmed_at": at("restored_at"),
        "restored_sha256": digest("flags-original.json")
        if "restored_at" in state.marks
        else None,
        "washed_at": at("washed_at"),
    }


def _timings(state: _Attempt, ended: datetime) -> dict[str, Any]:
    marks = state.marks

    def span(a: str, b: str) -> float | None:
        if a in marks and b in marks:
            return round(marks[b] - marks[a], 3)
        return None

    return {
        "started_at": state.started_at.isoformat(),
        "ended_at": ended.isoformat(),
        "setup_s": span("start", "setup_end"),
        "investigation_s": span("setup_end", "investigation_end"),
        "closure_s": span("closure_start", "closure_end"),
    }


def _cost(
    attempt_id: str, before: Decimal | None, after: Decimal | None
) -> dict[str, Any]:
    measured = before is not None and after is not None
    return {
        "attempt_id": attempt_id,
        "balance_before_cny": None if before is None else str(before),
        "balance_after_cny": None if after is None else str(after),
        "delta_cny": str(before - after)
        if before is not None and after is not None
        else None,
        "status": "measured" if measured else "unknown",
        "shared_key_possible": True,
    }


def _write_summary(
    config: RoundConfig,
    deps: LiveDeps,
    slot: Slot,
    state: _Attempt,
    manifest: Manifest,
    run_dir: Path,
    cost: Mapping[str, Any],
) -> ArtifactRef:
    result = state.result
    outcome = report = None
    traces: tuple[TraceRecord, ...] = ()
    delivered: Mapping[str, DeliveredRecord] = {}
    authorized: Mapping[str, Window] = {}
    delivery_refs: dict[str, ArtifactRef] = {}
    recording_ref: ArtifactRef | None = None
    usage: Mapping[str, Any] | None = None
    if result is not None:
        outcome, report, traces, usage = (
            result.outcome,
            result.report,
            result.traces,
            dict(result.usage),
        )
        if state.status == "ran" and result.recording_ref is not None:
            try:
                live = _verified_evidence(config, deps, state, manifest)
            except (StorageError, OSError, KeyError, TypeError, ValueError) as exc:
                code = getattr(exc, "code", "RECORDING_UNVERIFIED")
                state.status, state.reason = "aborted", code
                state.ledger.event("freeze", "recording_rejected", reason=code)
            else:
                delivered, authorized = live.delivered, live.authorized
                # Only a verified package is linked: the loader re-resolves
                # the ref, and an unverified one would fail the whole round.
                recording_ref = result.recording_ref
                for evidence_id, raw in live.delivery_records.items():
                    name = f"deliveries/{sha256(raw)}.json"
                    path = state.ledger.directory / name
                    if not path.exists():
                        write_private(path, raw)
                    delivery_refs[evidence_id] = _ref(name, raw, "application/json")
    state.ledger.event("freeze", "summary", status=state.status, reason=state.reason)
    ledger_raw = state.ledger.freeze()
    # An incomplete private ledger is not linked (the round exits 5).
    ledger_ref = (
        None
        if state.ledger.write_failed
        else _ref("lifecycle.jsonl", ledger_raw, "application/x-ndjson")
    )
    attempt = replace(state.record, status=_attempt_status(state.status))
    capture = EvaluationCapture(
        manifest=manifest,
        spec=slot.spec,
        answer=slot.answer,
        attempt=attempt,
        outcome=outcome,
        report=report,
        delivered=delivered,
        authorized=authorized,
        traces=tuple(
            replace(t, attempt_id=attempt.attempt_id, evaluation_id=attempt.attempt_id)
            for t in traces
        ),
        judge=None,
        delivery_refs=delivery_refs,
        recording_ref=recording_ref,
        ledger_ref=ledger_ref,
        started_at=state.started_at,
        ended_at=deps.clock.now(),
        usage=usage,
        cost=dict(cost),
    )
    return write_summary(run_dir / "summary.json", capture)


def _attempt_status(status: Status) -> Literal["ran", "setup_failed", "aborted"]:
    assert status != "not_started"
    return status


def _verified_evidence(
    config: RoundConfig, deps: LiveDeps, state: _Attempt, manifest: Manifest
) -> Any:
    """Load the recording by its returned hash, bind it to this attempt (Q1)
    and derive the visible evidence from the verified package only."""
    result = state.result
    assert result is not None and result.recording_ref is not None
    directory = recording_dir(deps, config.round_id, state.record.attempt_id)
    recording = load_recording(
        directory / result.recording_ref.path,
        expected_hash=result.recording_ref.sha256,
    )
    if recording.context.synthetic:
        # A live attempt is backed by a real recording, never a fixture.
        raise StorageError("IDENTITY_MISMATCH")
    epoch = result.usage.get("epoch")
    if type(epoch) is not int:
        raise StorageError("IDENTITY_MISMATCH")
    check_recording_binding(
        recording,
        state.record,
        round_id=config.round_id,
        epoch=epoch,
        manifest=manifest,
    )
    if recording.outcome != result.outcome:
        raise StorageError("IDENTITY_MISMATCH")
    return evidence_from_recording(recording, outcome=result.outcome)


# -- recovery -----------------------------------------------------------------


def recover_round(
    round_id: str, holder: str, batch: str, deps: LiveDeps
) -> RoundResult:
    """Restore and wash a retained fault of this round (exit 0/3/4/5/130)."""
    with interrupt_scope():
        return _recover_round(round_id, holder, batch, deps)


def _refused(round_id: str, reason: str) -> RoundResult:
    if stop_requested():
        # A stop requested before the refusal (R10): 130, not 3.
        return RoundResult(round_id, "interrupted", 130, "INTERRUPTED", ())
    return RoundResult(round_id, "refused", 3, reason, ())


def _recover_round(
    round_id: str, holder: str, batch: str, deps: LiveDeps
) -> RoundResult:
    # The wash reads the lab Prometheus account; check it before anything.
    environment = deps.env.check()
    if environment is not None:
        return _refused(round_id, environment)
    try:
        state = read_state(deps.ledger_root)
    except LabError as exc:
        return _refused(round_id, exc.code)
    except OSError:
        return _refused(round_id, "LAB_LEDGER_UNAVAILABLE")
    if state is None or state.get("round") != round_id:
        return _refused(round_id, "NO_RETAINED_FAULT")
    attempt_id = str(state.get("attempt_id"))
    ledger: AttemptLedger | None = None
    interrupted = False
    failure: str | None = None
    restoring = False
    try:
        check_occupation(deps.ledger_root, holder, batch, deps.clock.now())
        signatures = fault_signatures(deps)
        checkpoint()
        ledger = AttemptLedger(
            attempt_ledger_dir(deps, round_id, attempt_id), deps.clock, frozen=True
        )
        with (
            active_marker(deps.ledger_root, holder, batch, round_id, deps.clock.now()),
            fault_guard(deps.ledger_root),
            protected(),  # the restore and its result (r16)
        ):
            restoring = True
            restored = restore_fault(
                round_id, attempt_id, signatures, deps=deps, ledger=ledger
            )
        if restored is None:
            # Another recovery cleared the state between our read and the
            # guard: nothing was restored here, so no record and no exit 0.
            return _refused(round_id, "NO_RETAINED_FAULT")
    except LabError as exc:
        if exc.code.startswith("LAB_") or exc.code == "FAULT_BELONGS_TO_ANOTHER_RUN":
            return _refused(round_id, exc.code)
        return RoundResult(round_id, "stopped", 4, exc.code, ())
    except (Interrupted, KeyboardInterrupt, Exception) as exc:
        # An interrupt or an unexpected failure (a file under the ledger
        # root, a driver bug) while reading, restoring or washing: the intent
        # decides. Still there -> not recovered, exit 4; gone after our
        # restore started -> it finished, so the record is written.
        if not isinstance(exc, Exception):
            request_stop()
        with protected():
            _retained_now, still_here = _retained(deps, round_id)
        interrupted = stop_requested()
        if isinstance(exc, Exception):
            failure = (
                "LAB_LEDGER_UNAVAILABLE" if isinstance(exc, OSError) else "DRIVER_ERROR"
            )
        if still_here:
            reason = failure or "INTERRUPTED"
            return RoundResult(round_id, "stopped", 4, f"RESTORE_FAILED:{reason}", ())
        if not restoring:
            # Gone before our restore began: another recovery did it.
            return _refused(round_id, "NO_RETAINED_FAULT")
    with protected():
        stamp = deps.clock.now()
        document = {
            "schema": "opspilot-eval-live-recovery-1",
            "round": round_id,
            "attempt_id": attempt_id,
            "recovered_at": stamp.isoformat(),
        }
        base = f"recovery-{stamp.strftime('%Y%m%dT%H%M%SZ')}"
        names = [f"{base}.json"] + [f"{base}-{n}.json" for n in range(2, 100)]
        written = write_public_exclusive(
            round_evidence(deps, round_id), names, document
        )
    interrupted = stop_requested() or interrupted
    if not written or (ledger is not None and ledger.write_failed):
        # The lab is restored; only the public record or the private
        # recovery.jsonl could not be written (5 outranks 130).
        return RoundResult(round_id, "stopped", 5, "EVIDENCE_WRITE_FAILED", ())
    if interrupted:
        return RoundResult(round_id, "interrupted", 130, "INTERRUPTED", ())
    if failure is not None:
        # Restored, but e.g. the active marker could not be removed: the
        # lab coordination is incomplete, so this is never reported as 0.
        return RoundResult(round_id, "stopped", 1, failure, ())
    return RoundResult(round_id, "recovered", 0, None, ())


def write_public_exclusive(
    directory: Path, names: Sequence[str], document: Mapping[str, Any]
) -> bool:
    """Exclusive-create the first free name; never overwrite, never raise."""
    raw = json.dumps(document, indent=2, sort_keys=True) + "\n"
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    for name in names:
        try:
            with (directory / name).open("x") as stream:
                stream.write(raw)
            return True
        except FileExistsError:
            continue
        except OSError:
            return False
    return False


# -- CLI ----------------------------------------------------------------------


class _Usage(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:  # pragma: no cover - argparse hook
        raise _Usage(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="scripts.eval_live", description=__doc__.split("\n\n")[0])
    parser.add_argument("--mode")
    parser.add_argument("--recover", action="store_true")
    parser.add_argument("--round", required=True)
    parser.add_argument("--scenario", action="append", default=[])
    parser.add_argument("--n", type=int)
    parser.add_argument("--parallel", type=int)
    # Required by every mode except --verify-replay (checked in ``main``).
    parser.add_argument("--holder")
    parser.add_argument("--batch")
    parser.add_argument("--flagd-url")
    parser.add_argument("--pg-port", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--verify-replay", action="store_true")
    parser.add_argument("--attempt")
    # #431: a v2 dataset root such as evals/datasets/dev-v2 (relative to the
    # repository); default the v1 evals/scenarios.
    # F1 closure R7: run HolmesGPT under the same injection, in this order.
    parser.add_argument("--pair", choices=sorted(PAIR_ORDERS))
    parser.add_argument("--scenario-root")
    # #432 seams §3 / §10 A1: an absolute directory outside the repository
    # for the evidence, the attempt PGs and the attempts' private ledgers
    # (probe Runs); recordings stay at <repo>/tmp/eval (B2 rule).
    parser.add_argument("--output-root")
    return parser


def output_root_arg(value: str | None) -> Path | None:
    """``--output-root``: absolute and outside the repository, or None."""
    if value is None:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise _Usage("--output-root must be an absolute path")
    resolved = path.resolve()
    if resolved == ROOT.resolve() or resolved.is_relative_to(ROOT.resolve()):
        raise _Usage("--output-root must be outside the repository")
    return resolved


def relocate(deps: LiveDeps, output_root: Path | None) -> LiveDeps:
    """``deps`` with evidence, PG and attempt ledgers under ``output_root``
    (recording_root and the coordination ``ledger_root`` unchanged)."""
    if output_root is None:
        return deps
    return replace(
        deps,
        evidence_root=output_root / "evidence",
        pg_root=output_root / "pg",
        attempt_root=output_root / "ledgers",
    )


def scenario_root_arg(value: str | None) -> Path:
    """``--scenario-root``: a directory under the repository's ``evals/``."""
    if value is None:
        return ROOT / "evals/scenarios"
    path = (ROOT / value).resolve()
    if not path.is_dir() or not path.is_relative_to((ROOT / "evals").resolve()):
        raise _Usage("--scenario-root must be a directory under evals/")
    return path


def plan_slots(scenarios_root: Path, wanted: Sequence[str], n: int) -> tuple[Slot, ...]:
    if is_v2_root(scenarios_root):
        return _plan_v2(scenarios_root, wanted, n)
    loaded = {
        spec.scenario_id: (spec, answer)
        for spec, answer in load_scenarios(scenarios_root)
    }
    if list(wanted) == ["all"]:
        ids = sorted(loaded)
    elif not wanted or "all" in wanted:
        raise _Usage("--scenario all cannot be mixed with IDs")
    else:
        ids = sorted(set(wanted))
        if len(ids) != len(wanted) or any(i not in loaded for i in ids):
            raise _Usage("unknown or repeated scenario")
    slots = []
    for scenario_id in ids:
        spec, answer = loaded[scenario_id]
        if spec.kind == "fault":
            injected = answer.injected
            assert injected is not None
            try:
                fault_for(injected.flag, injected.variant)
            except LabError:
                raise _Usage("FAULT_NOT_WHITELISTED") from None
        for repetition in range(1, n + 1):
            slots.append(Slot(spec, answer, repetition))
    return tuple(slots)


def _wanted(ids: Sequence[str], wanted: Sequence[str]) -> list[str]:
    if list(wanted) == ["all"]:
        return sorted(ids)
    if not wanted or "all" in wanted:
        raise _Usage("--scenario all cannot be mixed with IDs")
    chosen = sorted(set(wanted))
    if len(chosen) != len(wanted) or any(i not in ids for i in chosen):
        raise _Usage("unknown or repeated scenario")
    return chosen


def _plan_v2(scenarios_root: Path, wanted: Sequence[str], n: int) -> tuple[Slot, ...]:
    """v2 slots (#431): ``load_scenarios_v2`` (``DATASET_INCOMPLETE`` for a
    root with one group), each scenario's injectors validated, one binding
    class per round; a refusal is a usage error carrying the code."""
    from opspilot.evaluation.model import DATASET_GROUPS

    if not all((scenarios_root / g).is_dir() for g in DATASET_GROUPS):
        missing = sorted(g for g in DATASET_GROUPS if not (scenarios_root / g).is_dir())
        raise ScenarioError(f"DATASET_INCOMPLETE:{','.join(missing)}")
    loaded = {
        spec.scenario_id: (spec, answer)
        for spec, answer in load_scenarios_v2(scenarios_root)
    }
    slots = []
    for scenario_id in _wanted(list(loaded), wanted):
        spec, answer = loaded[scenario_id]
        try:
            plan = validate_injectors(spec.entry, answer.injectors)
        except LabError as exc:
            raise _Usage(exc.code) from None
        for repetition in range(1, n + 1):
            slots.append(Slot(spec, answer, repetition, plan))
    try:
        round_binding(slots)
    except LabError as exc:
        raise _Usage(exc.code) from None
    return tuple(slots)


def _dry_slot(slot: Slot) -> dict[str, Any]:
    if slot.plan is not None:
        return {
            "scenario_id": slot.spec.scenario_id,
            "repetition": slot.repetition,
            "entry": slot.plan.entry,
            "injectors": sorted({item["kind"] for item in slot.plan.injectors}),
        }
    injected = slot.answer.injected  # type: ignore[union-attr]
    return {
        "scenario_id": slot.spec.scenario_id,
        "repetition": slot.repetition,
        "kind": slot.spec.kind,  # type: ignore[union-attr]
        "flag": None if injected is None else injected.flag,
    }


def _final(result: RoundResult) -> int:
    print(
        json.dumps(
            {
                "round": result.round_id,
                "status": result.status,
                "exit_code": result.exit_code,
                "reason": result.reason,
                "slots": [
                    {
                        "scenario_id": s.scenario_id,
                        "repetition": s.repetition,
                        "attempt_id": s.attempt_id,
                        "status": s.status,
                    }
                    for s in result.slots
                ],
            },
            sort_keys=True,
        )
    )
    return result.exit_code


# Cooperative interrupts (r16) live in ``eval_live_lab``: the handler only
# records a stop request, checkpoints raise, protected sections never do.
# ``_DEFER`` and ``_deferred_interrupts`` are the r7 names of the same state
# and the same nestable protected depth (R3).
_DEFER = _STOP
_deferred_interrupts = protected


def _settled(result: RoundResult) -> RoundResult:
    """The final snapshot (R10): a stop requested so far merges by rank
    (4 > 5 > 130 = 3 after the start > 1); a later one changes nothing."""
    if stop_requested() and _outranks(130, result.exit_code):
        return replace(
            result, status="interrupted", exit_code=130, reason="INTERRUPTED"
        )
    return result


@contextmanager
def _signals() -> Iterator[None]:
    """One CLI call: a fresh stop state and the cooperative handler."""
    with interrupt_scope():
        previous = {
            sig: signal.signal(sig, handle_signal)
            for sig in (signal.SIGINT, signal.SIGTERM)
        }
        try:
            yield
        finally:
            for sig, old in previous.items():
                signal.signal(sig, old)


def _verify(
    round_id: str,
    attempt_id: str | None,
    deps: LiveDeps | None,
    output_root: Path | None = None,
) -> int:
    """``--verify-replay`` (r9): local files only, no seam is called."""
    from scripts.eval_live_verify import SCHEMA, verify_replay

    evidence_root = deps.evidence_root if deps else ROOT / "docs/evidence/eval"
    if deps is None and output_root is not None:
        evidence_root = output_root / "evidence"
    recording_root = deps.recording_root if deps else ROOT / "tmp/eval"
    # verify_replay takes the final snapshot itself, right before its record
    # is written, so a normal return is never merged again (R10): the record
    # and the final JSON always agree.
    frozen = True
    try:
        code, document = verify_replay(
            round_id,
            evidence_root=evidence_root,
            recording_root=recording_root,
            attempt_id=attempt_id,
        )
    except (Interrupted, KeyboardInterrupt, Exception) as exc:
        frozen = False
        _escape_note(exc)
        if not isinstance(exc, Exception):
            request_stop()
        code = 130 if not isinstance(exc, Exception) else 1
        reason = "INTERRUPTED" if code == 130 else "DRIVER_ERROR"
        document = {
            "schema": SCHEMA,
            "round": round_id,
            "status": "interrupted" if code == 130 else "failed",
            "exit_code": code,
            "reason": reason,
            "results": [],
        }
    # The final snapshot (R10), then the JSON: a later signal changes nothing.
    with protected():
        if not frozen and stop_requested() and _outranks(130, code):
            code = 130
            document = {**document, "status": "interrupted", "exit_code": 130}
        print(json.dumps(document, sort_keys=True))
    return code


def _escape_note(exc: BaseException) -> None:
    if isinstance(exc, Exception):
        # Type name only: a message may carry provider text or paths.
        print(f"eval_live: unexpected {type(exc).__name__}", file=sys.stderr)


def _escaped(round_id: str, deps: LiveDeps, exc: BaseException) -> RoundResult:
    """The backstop of every lab mode: whatever escaped a mode's own
    handling still yields the final JSON, mapped by r13 (4 > 5 > 130)."""
    _escape_note(exc)
    _any, own = _retained(deps, round_id)
    if own:
        return RoundResult(round_id, "stopped", 4, "FAULT_RECOVERY_REQUIRED", ())
    if isinstance(exc, (OSError, StorageError)):
        return RoundResult(round_id, "stopped", 5, "EVIDENCE_WRITE_FAILED", ())
    if not isinstance(exc, Exception):
        return RoundResult(round_id, "interrupted", 130, "INTERRUPTED", ())
    code = exc.code if isinstance(exc, LabError) else "DRIVER_ERROR"
    return RoundResult(round_id, "stopped", 1, code, ())


def _dispatch(round_id: str, deps: LiveDeps, run: Callable[[], RoundResult]) -> int:
    try:
        result = run()
    except (Interrupted, KeyboardInterrupt, Exception) as exc:
        if not isinstance(exc, Exception):
            request_stop()
        with protected():
            result = _escaped(round_id, deps, exc)
    # The final snapshot (R10), then the JSON and the same exit code: a
    # signal after the snapshot changes nothing.
    with protected():
        return _final(_settled(result))


def main(argv: Sequence[str] | None = None, *, deps: LiveDeps | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    with _signals():
        try:
            return _main(argv, deps)
        except (Interrupted, KeyboardInterrupt):
            # Only before a mode started (parsing, planning, dry-run): no
            # lab state was touched.
            print(json.dumps({"status": "interrupted", "exit_code": 130}))
            return 130


def _main(argv: list[str], deps: LiveDeps | None) -> int:
    import re

    try:
        args = _parser().parse_args(argv)
        if not re.fullmatch(ROUND_PATTERN, args.round):
            raise _Usage("invalid round")
        output_root = output_root_arg(args.output_root)
        if args.verify_replay:
            mixed = (
                args.mode,
                args.scenario,
                args.n,
                args.parallel,
                args.pg_port,
                args.probe,
                args.dry_run,
                args.recover,
            )
            if any(value not in (None, [], False) for value in mixed):
                raise _Usage("--verify-replay takes only --round [--attempt]")
            return _verify(args.round, args.attempt, deps, output_root)
        if args.attempt is not None:
            raise _Usage("--attempt only applies to --verify-replay")
        if not args.holder or not args.batch or not args.flagd_url:
            raise _Usage("--holder, --batch and --flagd-url are required")
        try:
            check_flagd_url(args.flagd_url)
        except LabError as exc:
            raise _Usage(exc.code) from None
        if args.recover:
            if (
                args.mode not in (None, "live")
                or args.scenario
                or args.dry_run
                or args.probe
            ):
                raise _Usage("--recover takes only round/holder/batch/flagd-url")
        else:
            if args.mode != "live":
                raise _Usage("--mode live is required")
            if args.parallel != 1:
                raise _Usage("PARALLEL_UNSUPPORTED")
            if args.n is None or args.n < 1:
                raise _Usage("--n must be >= 1")
            if args.probe and args.n != 1:
                raise _Usage("--probe takes --n 1")
            if not args.probe and (
                args.pg_port is None or not 1024 <= args.pg_port <= 65535
            ):
                raise _Usage("--pg-port is required")
            scenarios_root = (
                deps.scenarios_root
                if deps is not None
                else scenario_root_arg(args.scenario_root)
            )
            try:
                slots = plan_slots(scenarios_root, args.scenario, args.n)
            except (ScenarioError, OSError):
                raise _Usage("scenario directory invalid") from None
            if args.pair is not None:
                check_pair(slots, probe=args.probe, alone=args.pair == "opspilot-only")
        if args.recover and args.pair is not None:
            raise _Usage("--recover takes only round/holder/batch/flagd-url")
    except _Usage as exc:
        print(json.dumps({"status": "usage", "exit_code": 2, "reason": str(exc)}))
        return 2
    checkpoint()  # a stop requested while parsing: nothing touched, 130
    if not args.recover and args.dry_run:
        document = {
            "round": args.round,
            "status": "dry_run",
            "slots": [_dry_slot(s) for s in slots],
            "evidence": f"docs/evidence/eval/{args.round}/",
            "ledger": f"tmp/lab-ledgers/{args.round}/",
        }
        # R10: freeze before serialization/output; later signals change nothing.
        with protected():
            code = 130 if stop_requested() else 0
            if code == 130:
                # The same minimal document as a stop seen while parsing (r14).
                document = {"status": "interrupted", "exit_code": 130}
            print(json.dumps(document, sort_keys=True))
        return code
    if deps is None:
        try:
            # Recovery needs the lab seams only, never model credentials (r10).
            extra: dict[str, Any] = {}
            if output_root is not None:
                # Only when given: the r10 call shape stays unchanged.
                extra["output_root"] = output_root
            if args.scenario_root is not None:
                root = scenario_root_arg(args.scenario_root)
                extra.update(
                    scenarios_root=root,
                    v2_probe=args.probe and is_v2_root(root),
                )
            deps = real_deps(
                args.flagd_url,
                args.round,
                probe=args.probe or args.recover,
                **extra,
            )
        except (LabError, OSError) as exc:
            code = exc.code if isinstance(exc, LabError) else "ENV_UNCONFIRMED"
            # The final snapshot (R10): a stop requested before this refusal
            # (e.g. while the deps were built) is 130, not 3.
            with protected():
                return _final(_refused(args.round, code))
    # Idempotent: real_deps already relocated; injected deps (tests) too.
    live = relocate(deps, output_root)
    if args.recover:
        return _dispatch(
            args.round,
            live,
            lambda: recover_round(args.round, args.holder, args.batch, live),
        )
    if args.probe:
        from scripts.eval_live_probe import run_probe

        probe = RoundConfig(
            args.round,
            slots,
            args.holder,
            args.batch,
            args.pg_port or 0,
            None,
            tuple(argv),
            1,
        )
        return _dispatch(args.round, live, lambda: run_probe(probe, live))
    pair: tuple[str, ...] = ("opspilot",)
    if args.pair is not None:
        pair = PAIR_ORDERS[args.pair]
    if "holmes" in pair:
        if getattr(live, "baseline", None) is None:
            try:
                live = replace(live, baseline=baseline_runner(live))
            except (ImportError, LabError, OSError):
                with protected():
                    return _final(_refused(args.round, "BASELINE_UNAVAILABLE"))
    config = RoundConfig(
        args.round,
        slots,
        args.holder,
        args.batch,
        args.pg_port,
        None,
        tuple(argv),
        args.n,
        pair,
        args.pair is not None,
    )
    return _dispatch(args.round, live, lambda: run_round(config, live))


def check_pair(slots: Sequence[Slot], *, probe: bool, alone: bool = False) -> None:
    """A scheduled round (R7) is one v2 slot: the schedule makes one round
    per window; with HolmesGPT it is question entry, since HolmesGPT has no
    alert entry (V2-R12)."""
    if probe:
        raise _Usage("--pair does not take --probe")
    if len(slots) != 1:
        raise _Usage("PAIR_ONE_SLOT")
    plan = slots[0].plan
    if plan is None or (not alone and plan.entry != "question"):
        raise _Usage("PAIR_ENTRY_UNSUPPORTED")


def baseline_runner(deps: LiveDeps) -> Any:
    """The real HolmesGPT side: S7b ``scripts.holmes_baseline.dataset``."""
    from scripts.holmes_baseline.dataset import run_attempt

    return HolmesBaseline(run_attempt)


class HolmesBaseline:
    """Adapts S7b ``run_attempt`` (which never touches the lab) to a paired
    slot. HolmesGPT gets its own attempt id (``holmes_attempt_id``) and
    writes ``<output_dir>/<that id>``. No ``deadline``: it would cap
    HolmesGPT's wall clock, which stays the frozen budget aligned with
    OpsPilot's (R15); ``submit_not_after`` is only checked right before
    HolmesGPT starts (``SUBMIT_LATE`` is a setup failure)."""

    def __init__(self, run_attempt: Callable[..., Mapping[str, Any]]) -> None:
        self.run_attempt = run_attempt

    def run(self, request: BaselineRequest) -> BaselineResult:
        from scripts import kind_lab

        own = holmes_attempt_id(request.round_id, request.attempt_id)
        out = self.run_attempt(
            request.spec,
            request.answer,
            attempt_id=own,
            slot=None,
            raw_root=request.output_dir,
            round_id=request.round_id,
            kind_node=f"{kind_lab.CLUSTER}-control-plane",
            submit_not_after=None
            if request.submit_not_after is None
            else request.submit_not_after.timestamp(),
        )
        status = str(out.get("status"))
        reason = out.get("reason")
        if status == "aborted" and reason == "DEADLINE_PASSED":
            status = "setup_failed"
        if status not in ("ran", "setup_failed", "aborted"):
            status, reason = "aborted", f"BASELINE_STATUS:{status}"

        def moment(value: Any) -> datetime | None:
            try:
                return None if value is None else datetime.fromisoformat(str(value))
            except ValueError:
                return None

        return BaselineResult(
            status=status,  # type: ignore[arg-type]
            reason=None if reason is None else str(reason),
            started_at=moment(out.get("started_at")),
            ended_at=moment(out.get("ended_at")),
            product_run_created=status != "setup_failed",
            provider_error=bool(out.get("provider_error")),
            output_sha256=out.get("holmes_json_sha256"),
            attempt_id=own,
            output_path=str(request.output_dir / own),
        )


def real_deps(
    flagd_url: str,
    round_id: str,
    *,
    probe: bool = False,
    scenarios_root: Path | None = None,
    v2_probe: bool = False,
    output_root: Path | None = None,
) -> LiveDeps:
    """The only constructor of lab-touching adapters (never used offline).

    ``probe`` (r8) builds the lab seams only: no credential file is read and
    the model, PG and balance seats refuse any use.
    """
    from scripts import kind_lab

    # several labs on one host: refuse an unsupported instance or a
    # telemetry URL of another instance before any lab adapter exists
    try:
        kind_lab.check_instance()
    except SystemExit:
        raise LabError("LAB_INSTANCE_UNSUPPORTED") from None
    from scripts.eval_live_investigator import (
        InvestigatorSource,
        OtelDemoInvestigator,
        prepare_round,
    )
    from scripts.eval_live_lab import (
        DeepSeekBalance,
        GitProvenance,
        HttpFlagResolver,
        KindLabIdentity,
        KubectlFlagStore,
        LabEnvChecker,
        LabTelemetry,
        LocalPgLauncher,
        SocketPortProbe,
        SystemClock,
        store_restarts,
    )
    from scripts.f11_u2_harness import resolve_ledger_root

    clock = SystemClock()
    root = ROOT / "evals/scenarios" if scenarios_root is None else scenarios_root
    if probe:
        from scripts.eval_live_lab import ProbeEnvChecker, Unavailable

        return relocate(
            LiveDeps(
                clock=clock,
                flags=KubectlFlagStore(),
                flagd=HttpFlagResolver(flagd_url),
                telemetry=LabTelemetry(clock),
                identity=KindLabIdentity(),
                ports=SocketPortProbe(),
                # A v2 probe starts the attempt PG for alert_delivery (#431 §7).
                pg=LocalPgLauncher() if v2_probe else Unavailable(),
                balance=Unavailable(),
                investigator=Unavailable(),
                ledger_root=resolve_ledger_root(ROOT),
                evidence_root=ROOT / "docs/evidence/eval",
                recording_root=ROOT / "tmp/eval",
                pg_root=ROOT / "tmp/eval-live",
                scenarios_root=root,
                flagd_url=flagd_url,
                env=ProbeEnvChecker(),
            ),
            output_root,
        )
    session = prepare_round(round_id)
    return relocate(
        LiveDeps(
            clock=clock,
            flags=KubectlFlagStore(),
            flagd=HttpFlagResolver(flagd_url),
            telemetry=LabTelemetry(clock),
            identity=KindLabIdentity(),
            ports=SocketPortProbe(),
            pg=LocalPgLauncher(),
            balance=DeepSeekBalance(session.key),
            investigator=OtelDemoInvestigator(session, clock),
            lab_restarts=lambda: store_restarts(STORE_CONTAINERS),
            ledger_root=resolve_ledger_root(ROOT),
            evidence_root=ROOT / "docs/evidence/eval",
            recording_root=ROOT / "tmp/eval",
            pg_root=ROOT / "tmp/eval-live",
            scenarios_root=root,
            flagd_url=flagd_url,
            env=LabEnvChecker(),
            provenance=GitProvenance(ROOT),
            investigator_source=InvestigatorSource(session.config),
        ),
        output_root,
    )


if __name__ == "__main__":
    sys.exit(main())
