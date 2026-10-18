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
import signal
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import FrameType
from typing import Any, Literal
from uuid import uuid4

from opspilot.acceptance import IncidentOutcome
from opspilot.evaluation import (
    REQUIRED_DIMENSIONS,
    Answer,
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
    SignatureRule,
    StorageError,
    TraceRecord,
    Window,
    check_recording_binding,
    evidence_from_recording,
    load_recording,
    load_scenarios,
    manifest_hash,
    write_attempt,
    write_round,
    write_summary,
)
from opspilot.observation.release_policy import sha256
from scripts.eval_live_lab import (
    AttemptLedger,
    FaultIntent,
    Interrupted,
    LabError,
    LiveDeps,
    PgInstance,
    PreObservation,
    active_marker,
    append_run_start,
    check_flagd_url,
    check_occupation,
    confirm_all_normal,
    fault_for,
    fault_gate,
    fault_guard,
    inject_fault,
    normal_window,
    observation_json,
    read_state,
    restore_fault,
    write_private,
)

ROOT = Path(__file__).resolve().parents[1]
ROUND_PATTERN = r"[a-z0-9][a-z0-9.-]{0,62}"
FRESHNESS = timedelta(seconds=300)
PREFLIGHT_FLAG_SECONDS = 15.0
Status = Literal["ran", "setup_failed", "aborted", "not_started"]


# -- DTOs ----------------------------------------------------------------------


@dataclass(frozen=True)
class Slot:
    spec: ScenarioSpec
    answer: Answer
    repetition: int


@dataclass(frozen=True)
class RoundConfig:
    round_id: str
    slots: tuple[Slot, ...]
    holder: str
    batch: str
    pg_port: int
    manifest: Manifest
    command: tuple[str, ...] = ()


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
    return deps.ledger_root / round_id / attempt_id


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
            ledger_root=deps.ledger_root,
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


def _git(*args: str) -> str | None:
    import subprocess

    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), *args],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.decode().strip() if result.returncode == 0 else None


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
) -> Manifest:
    """The round manifest, frozen before the first attempt (P4/P10, Q1)."""
    from opspilot.evaluation.live import registry_hash, scope_policy_hash
    from opspilot.investigation.limits import M1_FROZEN_LIMITS
    from opspilot.investigation.loop import ACCEPTED_RESPONSE_MODEL
    from opspilot.tools import otel_demo
    from opspilot.tools.registry import canonical_hash

    def hashed(value: str) -> DimensionValue:
        return DimensionValue("hash", value, None)

    def none(reason: str) -> DimensionValue:
        return DimensionValue("none", None, reason)

    commit = _git("rev-parse", "HEAD")
    dirty = _git("status", "--porcelain", "--untracked-files=no")
    registrations = otel_demo.otel_demo_registrations(config)
    versions = otel_demo.otel_demo_versions()
    specs = sorted({(s.spec.scenario_id, s.spec.version) for s in slots})
    scenario_files = [
        scenarios_root / sid / name
        for sid, _version in specs
        for name in ("scenario.json",)
    ]
    answer_files = [scenarios_root / sid / "answer.json" for sid, _version in specs]
    dims: dict[str, DimensionValue] = {
        "code": DimensionValue("version", commit, None)
        if commit and dirty == ""
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
        "permissions": hashed(scope_policy_hash(template_scope(config))),
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
            "version", "event-clock/frozen-end-real-monotonic", None
        ),
    }
    assert set(dims) == set(REQUIRED_DIMENSIONS)
    return Manifest(round_id, dims)


def template_scope(config: Any = None) -> Any:
    """A QueryScope as ``otel_demo_executor_factory`` issues one for a fresh
    (non-alert) Run, with placeholder Run identity: only its policy part,
    which ``scope_policy_hash`` keeps, is meaningful."""
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
        tool_names=frozenset({otel_demo.METRICS_TOOL, otel_demo.TRACES_TOOL}),
        window=ToolWindow(end - timedelta(seconds=otel_demo.OBSERVATION_SECONDS), end),
        default_query_window=None,
        deadline=end,
    )


# -- round --------------------------------------------------------------------


def fault_signatures(deps: LiveDeps) -> tuple[SignatureRule, ...]:
    """The distinct signatures of every fault scenario ("three classes")."""
    rules: dict[str, SignatureRule] = {}
    for spec, answer in load_scenarios(deps.scenarios_root):
        if spec.kind == "fault" and answer.signature is not None:
            rules[answer.signature.rule_id] = answer.signature
    return tuple(rules[key] for key in sorted(rules))


def _refuse(config: RoundConfig, reason: str) -> RoundResult:
    return RoundResult(
        config.round_id,
        "refused",
        3,
        reason,
        tuple(
            SlotResult(s.spec.scenario_id, s.spec.version, s.repetition)
            for s in config.slots
        ),
    )


def preflight(config: RoundConfig, deps: LiveDeps) -> str | None:
    """A refusal code, or None; nothing here writes or mutates anything."""
    environment = deps.env.check()
    if environment is not None:
        return environment
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
        if deps.ports.in_use(config.pg_port):
            return "PG_PORT_IN_USE"
        confirm_all_normal(
            deps, deps.flags.read(), budget_seconds=PREFLIGHT_FLAG_SECONDS
        )
        for slot in config.slots:
            if slot.spec.kind == "fault":
                injected = slot.answer.injected
                assert injected is not None
                fault_for(injected.flag, injected.variant)
    except LabError as exc:
        return exc.code
    return None


def run_round(config: RoundConfig, deps: LiveDeps) -> RoundResult:
    refusal = preflight(config, deps)
    if refusal is not None:
        return _refuse(config, refusal)
    try:
        append_run_start(
            deps.ledger_root,
            config.round_id,
            config.holder,
            config.batch,
            deps.clock.now(),
        )
    except LabError as exc:
        return _refuse(config, exc.code)
    signatures = fault_signatures(deps)
    results = [
        SlotResult(s.spec.scenario_id, s.spec.version, s.repetition)
        for s in config.slots
    ]
    status: Literal["finished", "stopped", "interrupted"] = "finished"
    exit_code: int | None = None
    reason: str | None = None
    journals: list[tuple[str, ArtifactRef]] = []
    summaries: dict[str, ArtifactRef] = {}
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
    except LabError as exc:
        # The reservation vanished or changed between the preflight and here.
        status, exit_code, reason = "stopped", 3, exc.code
    manifest = round_state["manifest"]
    aggregate_error = _write_round(
        config, deps, manifest, journals, summaries, evidence_dir
    )
    if aggregate_error is not None and exit_code is None:
        status, exit_code, reason = "stopped", 5, aggregate_error
    if exit_code is None:
        exit_code = 0 if all(r.status == "ran" for r in results) else 1
    retained = read_state(_safe(deps)) is not None
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
    )
    return RoundResult(config.round_id, status, exit_code, reason, tuple(results))


def _safe(deps: LiveDeps) -> Path:
    return deps.ledger_root


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
    marks: dict[str, float] = field(default_factory=dict)


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
    ledger = AttemptLedger(attempt_ledger_dir(deps, config.round_id, attempt_id), clock)
    state = _Attempt(record, ledger, started_at)
    state.marks["start"] = clock.monotonic()
    ledger.event("journal", "attempt_started", attempt_id=attempt_id)
    before = _balance(deps)
    _attempt_body(config, deps, slot, state, signatures, round_state)
    after = _balance(deps)
    balances.append(_cost(attempt_id, before, after))
    out.status = state.status
    out.stage = state.stage
    out.reason = state.reason
    out.closure_reason = state.closure_reason
    out.product_run_id = state.record.product_run_id
    out.pre_observation = None if state.gate is None else observation_json(state.gate)
    out.timings = _timings(state, clock.now())
    try:
        summary_ref = _write_summary(
            config, deps, slot, state, round_state["manifest"], run_dir, balances[-1]
        )
    except (OSError, StorageError, ValueError):
        raise _Stop(5, "EVIDENCE_WRITE_FAILED") from None
    summaries[attempt_id] = replace(summary_ref, path=f"runs/{attempt_id}/summary.json")
    out.summary = {
        "path": f"runs/{attempt_id}/summary.json",
        "sha256": summary_ref.sha256,
    }
    out.fault = _fault_json(state)
    if state.stop is not None:
        raise state.stop
    if state.interrupted:
        raise _Stop(130, "INTERRUPTED")


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
                state.interrupted = True
                _fail(state, "INTERRUPTED")
            except LabError as exc:
                _fail(state, exc.code)
            except Exception:  # noqa: BLE001 - a driver bug still restores the lab
                _fail(state, "DRIVER_ERROR")
            finally:
                _closure(config, deps, slot, state, signatures)
    except LabError as exc:
        # The reservation or the guard was not ours: nothing was touched.
        _fail(state, exc.code)
        _closure(config, deps, slot, state, signatures)


def _fail(state: _Attempt, code: str) -> None:
    if state.reason is None:
        state.reason = code
    state.status = "aborted" if state.investigating else "setup_failed"
    state.ledger.event(state.stage, "failed", reason=state.reason)
    if code == "LAB_FAULT_FOREIGN":
        # Another holder's fault is live: no further slot may start (exit 1;
        # exit 4 stays for this driver's own restore/wash failure).
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
    state.stage = "identity"
    if deps.identity.verify():
        raise LabError("LAB_IDENTITY_UNCONFIRMED")
    if read_state(deps.ledger_root) is not None:
        # Appeared after the preflight: not ours, so touch nothing (r3/7).
        raise LabError("LAB_FAULT_FOREIGN")
    state.stage = "pg"
    if deps.ports.in_use(config.pg_port):
        raise LabError("PG_PORT_IN_USE")
    directory = deps.pg_root / config.round_id / state.record.attempt_id / "pg"
    if directory.exists() or directory.is_symlink():
        raise LabError("PG_DIRECTORY_EXISTS")
    state.pg = deps.pg.start(directory, config.pg_port)
    ledger.event("pg", "started", port=config.pg_port)
    submit_not_after: datetime | None
    if spec.kind == "fault":
        injected = answer.injected
        assert injected is not None and answer.signature is not None
        state.stage = "inject"
        intent = FaultIntent(
            config.round_id,
            state.record.attempt_id,
            config.holder,
            config.batch,
            fault_for(injected.flag, injected.variant),
        )
        state.injected = True
        result = inject_fault(intent, deps=deps, ledger=ledger)
        state.marks["effective_at"] = result.effective_at.timestamp()
        state.stage = "pre_observation"
        gate = fault_gate(answer.signature, confirmed_at=result.effective_at, deps=deps)
        state.gate = gate
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
        ledger.write("pre-observation.json", _raw_observation(gate))
        if not gate.passed or gate.window is None:
            raise LabError(gate.reason or "NORMAL_WINDOW_UNCONFIRMED")
        submit_not_after = gate.window.end + FRESHNESS
    state.marks["setup_end"] = clock.monotonic()
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
    )
    ledger.event("investigate", "started")
    try:
        result = deps.investigator.investigate(request)
    except (Interrupted, KeyboardInterrupt):
        raise
    except Exception:  # noqa: BLE001 - any investigator failure is an abort
        state.status, state.reason = "aborted", "INVESTIGATOR_FAILED"
        ledger.event("investigate", "failed", reason=state.reason)
        return
    state.result = result
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
    elif result.recording_ref is None or result.recording_error is not None:
        state.status = "aborted"
        state.reason = result.recording_error or "RECORDING_INCOMPLETE"
    else:
        state.status = "ran"
    ledger.event("investigate", "finished", status=state.status, reason=state.reason)


def _closure(
    config: RoundConfig,
    deps: LiveDeps,
    slot: Slot,
    state: _Attempt,
    signatures: tuple[SignatureRule, ...],
) -> None:
    """Restore and wash (if this attempt wrote an intent), then stop its PG."""
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
        except Exception as exc:  # noqa: BLE001 - logged, lab recovery decides
            ledger.event(
                "pg", "stop_failed", reason=getattr(exc, "code", "PG_STOP_FAILED")
            )
    state.marks["closure_end"] = deps.clock.monotonic()


def _raw_observation(gate: PreObservation) -> dict[str, Any]:
    return {**observation_json(gate), "raw": [dict(item) for item in gate.raw]}


def _fault_json(state: _Attempt) -> dict[str, Any] | None:
    if not state.injected:
        return None
    directory = state.ledger.directory

    def digest(name: str) -> str | None:
        path = directory / name
        if not path.exists():
            return None
        return sha256(str(json.loads(path.read_bytes())["raw"]).encode())

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
        if state.status == "ran":
            try:
                live = _verified_evidence(config, deps, state, manifest)
            except (StorageError, OSError, KeyError, TypeError, ValueError) as exc:
                code = getattr(exc, "code", "RECORDING_UNVERIFIED")
                state.status, state.reason = "aborted", code
                state.ledger.event("freeze", "recording_rejected", reason=code)
            else:
                delivered, authorized = live.delivered, live.authorized
                for evidence_id, raw in live.delivery_records.items():
                    name = f"deliveries/{sha256(raw)}.json"
                    path = state.ledger.directory / name
                    if not path.exists():
                        write_private(path, raw)
                    delivery_refs[evidence_id] = _ref(name, raw, "application/json")
        # Linked whatever the status: an unverified package is still evidence
        # of what happened, and the loader re-checks its bytes.
        recording_ref = result.recording_ref
    state.ledger.event("freeze", "summary", status=state.status, reason=state.reason)
    ledger_raw = state.ledger.freeze()
    ledger_ref = _ref("lifecycle.jsonl", ledger_raw, "application/x-ndjson")
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
    """Restore and wash a retained fault of this round (exit 0/3/4)."""
    try:
        state = read_state(deps.ledger_root)
    except LabError as exc:
        return RoundResult(round_id, "refused", 3, exc.code, ())
    if state is None or state.get("round") != round_id:
        return RoundResult(round_id, "refused", 3, "NO_RETAINED_FAULT", ())
    attempt_id = str(state.get("attempt_id"))
    try:
        check_occupation(deps.ledger_root, holder, batch, deps.clock.now())
        signatures = fault_signatures(deps)
        ledger = AttemptLedger(
            attempt_ledger_dir(deps, round_id, attempt_id), deps.clock, frozen=True
        )
        with (
            active_marker(deps.ledger_root, holder, batch, round_id, deps.clock.now()),
            fault_guard(deps.ledger_root),
        ):
            restore_fault(round_id, attempt_id, signatures, deps=deps, ledger=ledger)
    except LabError as exc:
        code = (
            3
            if exc.code.startswith("LAB_") or exc.code == "FAULT_BELONGS_TO_ANOTHER_RUN"
            else 4
        )
        return RoundResult(
            round_id, "stopped" if code == 4 else "refused", code, exc.code, ()
        )
    evidence = round_evidence(deps, round_id)
    evidence.mkdir(parents=True, exist_ok=True)
    stamp = deps.clock.now()
    with (evidence / f"recovery-{stamp.strftime('%Y%m%dT%H%M%SZ')}.json").open(
        "x"
    ) as f:
        f.write(
            json.dumps(
                {
                    "schema": "opspilot-eval-live-recovery-1",
                    "round": round_id,
                    "attempt_id": attempt_id,
                    "recovered_at": stamp.isoformat(),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
    return RoundResult(round_id, "recovered", 0, None, ())


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
    parser.add_argument("--holder", required=True)
    parser.add_argument("--batch", required=True)
    parser.add_argument("--flagd-url", required=True)
    parser.add_argument("--pg-port", type=int)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def plan_slots(scenarios_root: Path, wanted: Sequence[str], n: int) -> tuple[Slot, ...]:
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


@contextmanager
def _signals() -> Iterator[None]:
    def handler(signum: int, frame: FrameType | None) -> None:
        raise Interrupted()

    previous = {
        sig: signal.signal(sig, handler) for sig in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        yield
    finally:
        for sig, old in previous.items():
            signal.signal(sig, old)


def main(argv: Sequence[str] | None = None, *, deps: LiveDeps | None = None) -> int:
    import re

    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args = _parser().parse_args(argv)
        if not re.fullmatch(ROUND_PATTERN, args.round):
            raise _Usage("invalid round")
        try:
            check_flagd_url(args.flagd_url)
        except LabError as exc:
            raise _Usage(exc.code) from None
        if args.recover:
            if args.mode not in (None, "live") or args.scenario or args.dry_run:
                raise _Usage("--recover takes only round/holder/batch/flagd-url")
        else:
            if args.mode != "live":
                raise _Usage("--mode live is required")
            if args.parallel != 1:
                raise _Usage("PARALLEL_UNSUPPORTED")
            if args.n is None or args.n < 1:
                raise _Usage("--n must be >= 1")
            if args.pg_port is None or not 1024 <= args.pg_port <= 65535:
                raise _Usage("--pg-port is required")
            scenarios_root = (
                deps.scenarios_root if deps is not None else ROOT / "evals/scenarios"
            )
            try:
                slots = plan_slots(scenarios_root, args.scenario, args.n)
            except ScenarioError:
                raise _Usage("scenario directory invalid") from None
    except _Usage as exc:
        print(json.dumps({"status": "usage", "exit_code": 2, "reason": str(exc)}))
        return 2
    if not args.recover and args.dry_run:
        print(
            json.dumps(
                {
                    "round": args.round,
                    "status": "dry_run",
                    "slots": [
                        {
                            "scenario_id": s.spec.scenario_id,
                            "repetition": s.repetition,
                            "kind": s.spec.kind,
                            "flag": None
                            if s.answer.injected is None
                            else s.answer.injected.flag,
                        }
                        for s in slots
                    ],
                    "evidence": f"docs/evidence/eval/{args.round}/",
                    "ledger": f"tmp/lab-ledgers/{args.round}/",
                },
                sort_keys=True,
            )
        )
        return 0
    if deps is None:
        try:
            deps = real_deps(args.flagd_url, args.round)
        except LabError as exc:
            return _final(RoundResult(args.round, "refused", 3, exc.code, ()))
    with _signals():
        if args.recover:
            return _final(recover_round(args.round, args.holder, args.batch, deps))
        manifest = build_manifest(
            args.round,
            slots,
            args.n,
            argv,
            scenarios_root=deps.scenarios_root,
            config=getattr(deps.investigator, "config", None),
        )
        config = RoundConfig(
            args.round,
            slots,
            args.holder,
            args.batch,
            args.pg_port,
            manifest,
            tuple(argv),
        )
        return _final(run_round(config, deps))


def real_deps(flagd_url: str, round_id: str) -> LiveDeps:
    """The only constructor of lab-touching adapters (never used offline)."""
    from scripts.eval_live_investigator import OtelDemoInvestigator, prepare_round
    from scripts.eval_live_lab import (
        DeepSeekBalance,
        HttpFlagResolver,
        KindLabIdentity,
        KubectlFlagStore,
        LabEnvChecker,
        LabTelemetry,
        LocalPgLauncher,
        SocketPortProbe,
        SystemClock,
    )
    from scripts.f11_u2_harness import resolve_ledger_root

    session = prepare_round(round_id)
    clock = SystemClock()
    return LiveDeps(
        clock=clock,
        flags=KubectlFlagStore(),
        flagd=HttpFlagResolver(flagd_url),
        telemetry=LabTelemetry(),
        identity=KindLabIdentity(),
        ports=SocketPortProbe(),
        pg=LocalPgLauncher(),
        balance=DeepSeekBalance(session.key),
        investigator=OtelDemoInvestigator(session, clock),
        ledger_root=resolve_ledger_root(ROOT),
        evidence_root=ROOT / "docs/evidence/eval",
        recording_root=ROOT / "tmp/eval",
        pg_root=ROOT / "tmp/eval-live",
        scenarios_root=ROOT / "evals/scenarios",
        flagd_url=flagd_url,
        env=LabEnvChecker(),
    )


if __name__ == "__main__":
    sys.exit(main())
