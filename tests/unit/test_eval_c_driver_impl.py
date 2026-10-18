"""Implementer tests for the eval PR-C live driver orchestration (offline).

Every lab collaborator is a fake (``scripts.eval_live_lab`` seams): a CAS
ConfigMap with propagation delay on a fake flagd, telemetry that shows error
spans exactly while a fault is effective, a fake PostgreSQL and investigator.
No kubectl, network, PostgreSQL or model call happens here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from opspilot.evaluation import load_attempt, load_scenarios
from scripts import eval_live, eval_live_lab
from scripts.eval_live_lab import (
    CallCount,
    FlagConfig,
    Interrupted,
    LabError,
    LiveDeps,
    ObservedSpan,
    PgInstance,
    SpanQuery,
)

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = ROOT / "evals/scenarios"
T0 = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
FLAGS = {
    "flags": {
        "paymentFailure": {
            "state": "ENABLED",
            "variants": {"100%": 1, "90%": 0.95, "off": 0},
            "defaultVariant": "off",
        },
        "productCatalogFailure": {
            "state": "ENABLED",
            "variants": {"on": True, "off": False},
            "defaultVariant": "off",
        },
        "cartFailure": {
            "state": "ENABLED",
            "variants": {"on": True, "off": False},
            "defaultVariant": "off",
        },
    }
}
ORIGINAL = (json.dumps(FLAGS, indent=4) + "\n").encode()


class Clock:
    def __init__(self) -> None:
        self.t = T0
        self.mono = 1000.0

    def now(self) -> datetime:
        return self.t

    def monotonic(self) -> float:
        return self.mono

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)
        self.mono += seconds


def _matchers() -> dict[str, list[tuple[str, str]]]:
    out: dict[str, list[tuple[str, str]]] = {}
    for spec, answer in load_scenarios(SCENARIOS):
        if spec.kind == "fault":
            assert answer.injected is not None and answer.signature is not None
            out[answer.injected.flag] = [
                (m.service, m.operation) for m in answer.signature.any_of
            ]
    return out


@dataclass
class Lab:
    """ConfigMap + flagd + telemetry, all driven by one fake clock."""

    clock: Clock
    raw: bytes = ORIGINAL
    uid: str = "uid-1"
    rv: int = 1
    delay: float = 20.0
    # flag -> list of (effective_at, variant) transitions as flagd sees them
    history: dict[str, list[tuple[datetime, str]]] = field(default_factory=dict)
    writes: list[bytes] = field(default_factory=list)
    lose_write_response: bool = False
    reject_next: bool = False
    fail_reads_after_write: bool = False
    errors_always: bool = False
    complete: bool = True
    resolve_calls: int = 0
    fence: str | None = None
    fences: list[str | None] = field(default_factory=list)

    def _variants(self, raw: bytes) -> dict[str, str]:
        return {
            name: entry["defaultVariant"]
            for name, entry in json.loads(raw)["flags"].items()
        }

    # FlagStore
    def read(self) -> FlagConfig:
        if self.fail_reads_after_write and self.writes:
            raise LabError("FLAG_READ_FAILED")
        return FlagConfig(self.raw, self.uid, str(self.rv), self.fence)

    def replace(
        self, current: FlagConfig, new_raw: bytes, *, fence: str | None
    ) -> FlagConfig:
        if (current.uid, current.resource_version, current.raw) != (
            self.uid,
            str(self.rv),
            self.raw,
        ):
            raise LabError("FLAG_CAS_CONFLICT")
        if self.reject_next:
            self.reject_next = False
            raise LabError("FLAG_CAS_CONFLICT")
        self.fences.append(fence)
        self.set(new_raw)
        if fence is not None:
            self.fence = fence
        if self.lose_write_response:
            self.lose_write_response = False  # one lost response only
            raise LabError("FLAG_WRITE_UNKNOWN")
        return FlagConfig(self.raw, self.uid, str(self.rv), self.fence)

    def set(self, new_raw: bytes) -> None:
        before, after = self._variants(self.raw), self._variants(new_raw)
        for flag, variant in after.items():
            if before.get(flag) != variant:
                self.history.setdefault(flag, []).append(
                    (self.clock.now() + timedelta(seconds=self.delay), variant)
                )
        self.raw = new_raw
        self.rv += 1
        self.writes.append(new_raw)

    def effective(self, flag: str, at: datetime) -> str:
        variant = "off"
        for when, value in self.history.get(flag, []):
            if when <= at:
                variant = value
        return variant

    # FlagResolver
    def resolve(self, flag: str, method: str, timeout: float) -> str | None:
        self.resolve_calls += 1
        return self.effective(flag, self.clock.now())

    # TelemetryProbe
    def spans(
        self, service: str, start: datetime, end: datetime, timeout: float
    ) -> SpanQuery:
        rows: list[ObservedSpan] = []
        t = start
        index = 0
        while t <= end:
            for flag, matchers in _matchers().items():
                if self.errors_always or self.effective(flag, t) != "off":
                    for svc, op in matchers:
                        if svc == service:
                            rows.append(
                                ObservedSpan(
                                    f"trace-{index}",
                                    f"{flag}-{index}-{svc}",
                                    svc,
                                    op,
                                    t,
                                    {"otel.status_code": "ERROR"},
                                )
                            )
            rows.append(
                ObservedSpan(f"trace-{index}", f"ok-{index}", service, "ok-op", t, {})
            )
            t += timedelta(seconds=10)
            index += 1
        return SpanQuery(tuple(rows), self.complete)

    def checkout_calls(
        self, start: datetime, end: datetime, timeout: float
    ) -> CallCount:
        return CallCount(42.0 if end > start else 0.0, self.complete)


class Identity:
    def __init__(self) -> None:
        self.failures: tuple[str, ...] = ()

    def verify(self) -> tuple[str, ...]:
        return self.failures


class Ports:
    def __init__(self) -> None:
        self.busy: set[int] = set()

    def in_use(self, port: int) -> bool:
        return port in self.busy


class Pg:
    def __init__(self, deps_ref: dict[str, Any]) -> None:
        self.started: list[Path] = []
        self.stopped: list[Path] = []
        self.deps_ref = deps_ref
        self.journal_seen: list[bool] = []

    def start(self, directory: Path, port: int) -> PgInstance:
        directory.mkdir(parents=True)
        self.started.append(directory)
        # P10: the attempt journal exists before any setup step.
        runs = self.deps_ref["deps"].evidence_root
        self.journal_seen.append(bool(list(runs.rglob("attempt.json"))))
        return PgInstance(directory, port, "owner", "runtime")

    def stop(self, instance: PgInstance) -> None:
        self.stopped.append(instance.directory)


class Balance:
    def balance(self):
        from decimal import Decimal

        return Decimal("100.00")


class Investigator:
    def __init__(self, lab: Lab, clock: Clock) -> None:
        self.lab = lab
        self.clock = clock
        self.requests: list[Any] = []
        self.action = None

    def investigate(self, request: Any) -> Any:
        self.requests.append(request)
        self.clock.advance(60)
        if self.action is not None:
            self.action(request)
        return eval_live.InvestigationResult(
            product_run_id="run-" + request.attempt.attempt_id[:8],
            outcome=None,
            report=None,
            recording_ref=None,
            recording_error=None,
            traces=(),
            submitted_at=self.clock.now() - timedelta(seconds=59),
            started_at=self.clock.now() - timedelta(seconds=59),
            ended_at=self.clock.now(),
            usage={"epoch": 1},
            error=None,
        )


@pytest.fixture
def env(tmp_path: Path):
    return make_env(tmp_path)


def make_env(tmp_path: Path) -> dict[str, Any]:
    clock = Clock()
    lab = Lab(clock)
    ref: dict[str, Any] = {}
    identity, ports, pg = Identity(), Ports(), Pg(ref)
    investigator = Investigator(lab, clock)
    env_checker = EnvOk()
    deps = LiveDeps(
        clock=clock,
        flags=lab,
        flagd=lab,
        telemetry=lab,
        identity=identity,
        ports=ports,
        pg=pg,
        balance=Balance(),
        investigator=investigator,
        ledger_root=tmp_path / "ledgers",
        evidence_root=tmp_path / "evidence",
        recording_root=tmp_path / "recordings",
        pg_root=tmp_path / "pg",
        scenarios_root=SCENARIOS,
        flagd_url="http://127.0.0.1:18013",
        env=env_checker,
    )
    ref["deps"] = deps
    reserve(deps)
    return dict(
        deps=deps,
        clock=clock,
        lab=lab,
        identity=identity,
        ports=ports,
        pg=pg,
        investigator=investigator,
        env_checker=env_checker,
    )


class EnvOk:
    def __init__(self) -> None:
        self.result: str | None = None

    def check(self) -> str | None:
        return self.result


def reserve(deps: LiveDeps, holder: str = "lead", batch: str = "b1", hours: int = 4):
    deps.ledger_root.mkdir(parents=True, exist_ok=True)
    (deps.ledger_root / "occupation.json").write_text(
        json.dumps(
            dict(
                holder=holder,
                batch=batch,
                started_at=T0.isoformat(),
                expected_end=(T0 + timedelta(hours=hours)).isoformat(),
            )
        )
    )


def config(deps: LiveDeps, scenarios: list[str], n: int = 1, round_id: str = "r1"):
    slots = eval_live.plan_slots(SCENARIOS, scenarios, n)
    manifest = eval_live.build_manifest(
        round_id, slots, n, ["test"], scenarios_root=SCENARIOS
    )
    return eval_live.RoundConfig(
        round_id, slots, "lead", "b1", 55999, manifest, ("test",)
    )


def lifecycle(deps: LiveDeps, round_id: str = "r1") -> dict[str, Any]:
    return json.loads((deps.evidence_root / round_id / "lifecycle.json").read_text())


def summary(deps: LiveDeps, attempt_id: str, round_id: str = "r1") -> dict[str, Any]:
    path = deps.evidence_root / round_id / "runs" / attempt_id / "summary.json"
    return json.loads(path.read_text())


# -- preflight refusals (exit 3, nothing started) --------------------------------


@pytest.mark.parametrize(
    "case,code",
    [
        ("no_reservation", "LAB_RESERVATION_REQUIRED"),
        ("foreign", "LAB_OCCUPIED"),
        ("expired", "LAB_RESERVATION_EXPIRED"),
        ("fault_active", "FAULT_RECOVERY_REQUIRED"),
        ("identity", "LAB_IDENTITY_UNCONFIRMED"),
        ("port", "PG_PORT_IN_USE"),
        ("flag_on", "FLAGS_NOT_NORMAL"),
        ("round_exists", "ROUND_EXISTS"),
        ("env", "ENV_UNCONFIRMED"),
    ],
)
def test_preflight_refuses_without_starting_any_attempt(env, case, code):
    deps = env["deps"]
    if case == "no_reservation":
        (deps.ledger_root / "occupation.json").unlink()
    elif case == "foreign":
        reserve(deps, holder="other")
    elif case == "expired":
        reserve(deps, hours=-1)
    elif case == "fault_active":
        (deps.ledger_root / "fault-active.json").write_text("{}")
    elif case == "identity":
        env["identity"].failures = ("JAEGER_IDENTITY_UNCONFIRMED",)
    elif case == "port":
        env["ports"].busy.add(55999)
    elif case == "flag_on":
        data = json.loads(ORIGINAL)
        data["flags"]["cartFailure"]["defaultVariant"] = "on"
        env["lab"].raw = json.dumps(data).encode()
    elif case == "round_exists":
        (deps.evidence_root / "r1").mkdir(parents=True)
    elif case == "env":
        env["env_checker"].result = "ENV_UNCONFIRMED"
    occupation_before = (
        (deps.ledger_root / "occupation.json").read_bytes()
        if (deps.ledger_root / "occupation.json").exists()
        else None
    )
    result = eval_live.run_round(config(deps, ["eval-cart-failure"]), deps)
    assert (result.exit_code, result.reason, result.status) == (3, code, "refused")
    assert not list(deps.evidence_root.rglob("attempt.json"))
    assert env["lab"].writes == [] and env["investigator"].requests == []
    if occupation_before is not None:
        assert (deps.ledger_root / "occupation.json").read_bytes() == occupation_before


# -- a fault attempt end to end --------------------------------------------------


def test_fault_attempt_injects_gates_restores_and_washes(env):
    deps, lab = env["deps"], env["lab"]
    result = eval_live.run_round(config(deps, ["eval-cart-failure"]), deps)
    # The fake investigator returns no recording, so the attempt is aborted
    # (never ran), but the lab is restored and washed and the round finished.
    assert result.status == "finished" and result.exit_code == 1
    slot = result.slots[0]
    assert (slot.status, slot.reason) == ("aborted", "RECORDING_INCOMPLETE")
    assert (
        lab.raw == ORIGINAL
        and lab.effective("cartFailure", env["clock"].now()) == "off"
    )
    assert not (deps.ledger_root / "fault-active.json").exists()
    assert env["pg"].journal_seen == [True]
    assert env["pg"].stopped == env["pg"].started
    request = env["investigator"].requests[0]
    gate = request.pre_observation
    assert gate.passed and gate.matched
    assert all(span.start >= gate.flag_confirmed_at for span in gate.matched)
    assert request.submit_not_after == max(s.start for s in gate.matched) + timedelta(
        seconds=300
    )
    doc = lifecycle(deps)
    assert doc["round_status"] == "finished" and doc["retained_fault"] is False
    entry = doc["slots"][0]
    assert entry["status"] == "aborted" and entry["fault"]["washed_at"]
    assert entry["fault"]["restored_sha256"] == entry["fault"]["original_sha256"]
    stored = summary(deps, slot.attempt_id)
    assert stored["schema"] == "opspilot-eval-summary-1"
    assert stored["capture"]["attempt"]["status"] == "aborted"
    assert stored["capture"]["attempt"]["product_run_id"] == slot.product_run_id
    assert "SETUP_FAILED" not in stored["evaluation"]["failure_reasons"]
    journal = load_attempt(
        deps.evidence_root / "r1/runs" / slot.attempt_id / "attempt.json"
    )
    assert journal.attempt.status == "unknown" and journal.attempt.started
    assert (deps.evidence_root / "r1/aggregate.json").exists()
    # Occupation untouched; the .active marker is gone.
    assert (deps.ledger_root / "occupation.json").exists()
    assert not list(deps.ledger_root.glob(".active-*.json"))


def test_restore_intent_is_written_before_the_config_write_and_is_f11_compatible(env):
    deps, lab = env["deps"], env["lab"]
    seen: dict[str, Any] = {}
    original_replace = lab.replace

    def spy(current, new_raw, *, fence):
        if not seen:
            seen.update(
                json.loads((deps.ledger_root / "fault-active.json").read_text())
            )
        return original_replace(current, new_raw, fence=fence)

    lab.replace = spy  # type: ignore[method-assign]
    eval_live.run_round(config(deps, ["eval-cart-failure"]), deps)
    for key in (
        "ledger",
        "flag",
        "normal",
        "variant",
        "original",
        "injected",
        "planned_at",
    ):
        assert key in seen
    assert seen["phase"] == "intent" and seen["flag"] == "cartFailure"
    assert seen["original"].encode() == ORIGINAL
    assert seen["injected_sha256"] == eval_live_lab.sha256(seen["injected"].encode())


def test_a_fault_never_observed_is_setup_failed_and_still_restored(env):
    deps, lab = env["deps"], env["lab"]
    lab.complete = False  # every telemetry read is truncated
    result = eval_live.run_round(config(deps, ["eval-cart-failure"]), deps)
    slot = result.slots[0]
    assert (slot.status, slot.reason) == ("setup_failed", "FAULT_NOT_OBSERVED")
    assert env["investigator"].requests == []
    # Restore succeeded (config + flagd); the wash cannot pass on truncated data.
    assert lab.raw == ORIGINAL
    assert slot.closure_reason == "RESTORE_FAILED:NORMAL_WINDOW_UNCONFIRMED"
    assert result.exit_code == 4 and (deps.ledger_root / "fault-active.json").exists()


def test_write_succeeded_but_response_lost_is_restored_from_the_intent(env):
    deps, lab = env["deps"], env["lab"]
    lab.lose_write_response = True
    result = eval_live.run_round(config(deps, ["eval-cart-failure"]), deps)
    slot = result.slots[0]
    assert (slot.status, slot.reason) == ("setup_failed", "FLAG_WRITE_UNKNOWN")
    # restore used the recorded intent: injected bytes went back to the original
    assert lab.raw == ORIGINAL
    assert not (deps.ledger_root / "fault-active.json").exists()
    assert result.exit_code == 1


def test_a_refused_injection_is_fenced_before_the_intent_is_cleared(env):
    deps, lab = env["deps"], env["lab"]
    lab.reject_next = True
    result = eval_live.run_round(config(deps, ["eval-cart-failure"]), deps)
    slot = result.slots[0]
    assert (slot.status, slot.reason) == ("setup_failed", "FLAG_CAS_CONFLICT")
    # live == original: one fence write with unchanged bytes, then cleared
    assert lab.writes == [ORIGINAL]
    assert not (deps.ledger_root / "fault-active.json").exists()
    assert result.exit_code == 1


def test_round_aggregate_reloads_through_the_round_resolver(env):
    from opspilot.evaluation import load_round, load_summary

    deps = env["deps"]
    cfg = config(deps, ["eval-cart-failure", "eval-normal-1"])
    result = eval_live.run_round(cfg, deps)
    ids = [s.attempt_id for s in result.slots]
    resolve = eval_live._round_resolver(deps, "r1", ids)
    aggregate = load_round(deps.evidence_root / "r1/aggregate.json", resolve=resolve)
    assert {row.scenario_id: row.attempted for row in aggregate.rows} == {
        "eval-cart-failure": 1,
        "eval-normal-1": 1,
    }
    for attempt_id in ids:
        evaluation = load_summary(
            deps.evidence_root / "r1/runs" / attempt_id / "summary.json",
            manifest=cfg.manifest,
            resolve=eval_live.attempt_resolver(
                "r1",
                attempt_id,
                ledger_root=deps.ledger_root,
                recording_root=deps.recording_root,
            ),
        )
        assert evaluation.overall is False


def test_a_foreign_flag_change_is_never_overwritten_and_stops_the_round(env):
    deps, lab = env["deps"], env["lab"]

    def tamper(request):
        data = json.loads(lab.raw)
        data["flags"]["paymentFailure"]["defaultVariant"] = "90%"
        lab.set(json.dumps(data).encode())

    env["investigator"].action = tamper
    cfg = config(deps, ["eval-cart-failure", "eval-normal-1"])
    result = eval_live.run_round(cfg, deps)
    assert result.exit_code == 4 and result.status == "stopped"
    first, later = result.slots
    assert first.status == "aborted" and first.reason == "RECORDING_INCOMPLETE"
    assert first.closure_reason == "RESTORE_FAILED:FLAGS_CHANGED_REFUSE_RESTORE"
    assert result.reason == first.closure_reason
    assert later.status == "not_started" and later.reason == "ROUND_STOPPED"
    tampered = lab.raw
    assert json.loads(tampered)["flags"]["paymentFailure"]["defaultVariant"] == "90%"
    assert (deps.ledger_root / "fault-active.json").exists()
    doc = lifecycle(deps)
    assert doc["retained_fault"] is True and doc["slots"][1]["attempt_id"] is None
    assert (deps.ledger_root / "occupation.json").exists()


def test_recover_restores_a_retained_fault_of_its_round(env):
    deps, lab = env["deps"], env["lab"]
    lab.complete = False
    eval_live.run_round(config(deps, ["eval-cart-failure"]), deps)
    assert (deps.ledger_root / "fault-active.json").exists()
    lab.complete = True
    assert eval_live.recover_round("other", "lead", "b1", deps).exit_code == 3
    result = eval_live.recover_round("r1", "lead", "b1", deps)
    assert (result.exit_code, result.status) == (0, "recovered")
    assert not (deps.ledger_root / "fault-active.json").exists()
    assert list((deps.evidence_root / "r1").glob("recovery-*.json"))


def test_interrupt_during_investigation_restores_then_exits_130(env):
    deps, lab = env["deps"], env["lab"]

    def interrupt(request):
        raise Interrupted()

    env["investigator"].action = interrupt
    cfg = config(deps, ["eval-cart-failure", "eval-normal-1"])
    result = eval_live.run_round(cfg, deps)
    assert (result.exit_code, result.status) == (130, "interrupted")
    assert (
        result.slots[0].status == "aborted" and result.slots[0].reason == "INTERRUPTED"
    )
    assert result.slots[1].status == "not_started"
    assert lab.raw == ORIGINAL and not (deps.ledger_root / "fault-active.json").exists()
    assert summary(deps, result.slots[0].attempt_id)["capture"]["attempt"][
        "status"
    ] == ("aborted")


def test_a_stale_submission_is_setup_failed(env):
    deps = env["deps"]
    investigator = env["investigator"]

    def late(request):
        env["clock"].advance(600)

    investigator.action = late
    original = investigator.investigate

    def investigate(request):
        result = original(request)
        return replace(
            result, submitted_at=request.submit_not_after + timedelta(seconds=1)
        )

    investigator.investigate = investigate  # type: ignore[method-assign]
    result = eval_live.run_round(config(deps, ["eval-cart-failure"]), deps)
    slot = result.slots[0]
    assert (slot.status, slot.reason) == ("setup_failed", "PRE_OBSERVATION_STALE")
    # The Run already created keeps its ID (attempt + private ledger).
    assert slot.product_run_id is not None
    stored = summary(deps, slot.attempt_id)
    assert stored["capture"]["attempt"]["product_run_id"] == slot.product_run_id
    assert stored["capture"]["attempt"]["status"] == "setup_failed"
    events = [
        json.loads(line)
        for line in (deps.ledger_root / "r1" / slot.attempt_id / "lifecycle.jsonl")
        .read_text()
        .splitlines()
    ]
    stale = next(e for e in events if e["event"] == "stale")
    assert stale["detail"]["product_run_id"] == slot.product_run_id


def test_a_foreign_fault_appearing_during_setup_is_never_touched(env):
    deps, lab = env["deps"], env["lab"]
    foreign = json.dumps({"ledger": "/elsewhere", "flag": "adFailure"})
    real_verify = env["identity"].verify

    def verify():
        # After the preflight, another holder writes its restore intent.
        if not (deps.ledger_root / "fault-active.json").exists() and list(
            deps.evidence_root.rglob("attempt.json")
        ):
            (deps.ledger_root / "fault-active.json").write_text(foreign)
        return real_verify()

    env["identity"].verify = verify
    cfg = config(deps, ["eval-cart-failure", "eval-normal-1"])
    result = eval_live.run_round(cfg, deps)
    first, later = result.slots
    assert (first.status, first.reason) == ("setup_failed", "LAB_FAULT_FOREIGN")
    assert later.status == "not_started" and later.reason == "ROUND_STOPPED"
    assert (result.exit_code, result.status) == (1, "stopped")
    assert lab.writes == [] and env["investigator"].requests == []
    assert (deps.ledger_root / "fault-active.json").read_text() == foreign


def test_final_statuses_are_never_unknown(env):
    deps = env["deps"]
    env["lab"].complete = False
    eval_live.run_round(config(deps, ["eval-normal-1"]), deps)
    for path in deps.evidence_root.rglob("summary.json"):
        status = json.loads(path.read_text())["capture"]["attempt"]["status"]
        assert status != "unknown"
    assert all(s["status"] != "unknown" for s in lifecycle(deps)["slots"])


# -- normal attempts --------------------------------------------------------------


def test_normal_attempt_needs_a_clean_fresh_window(env):
    deps, lab = env["deps"], env["lab"]
    result = eval_live.run_round(config(deps, ["eval-normal-1"]), deps)
    slot = result.slots[0]
    assert slot.status == "aborted"  # no recording from the fake
    gate = env["investigator"].requests[0].pre_observation
    assert gate.passed and gate.kind == "normal" and not gate.matched
    assert (gate.window.end - gate.window.start) == timedelta(seconds=300)
    assert gate.window.start >= gate.flag_confirmed_at
    assert lab.writes == []


def test_normal_attempt_with_signature_errors_is_setup_failed(env):
    deps, lab = env["deps"], env["lab"]
    lab.errors_always = True
    result = eval_live.run_round(config(deps, ["eval-normal-2"]), deps)
    slot = result.slots[0]
    assert (slot.status, slot.reason) == ("setup_failed", "NORMAL_WINDOW_UNCONFIRMED")
    assert env["investigator"].requests == []
    assert result.exit_code == 1


def test_a_busy_fault_guard_is_setup_failed_without_lab_writes(env):
    deps, lab = env["deps"], env["lab"]
    cfg = config(deps, ["eval-cart-failure"])
    original = eval_live.check_occupation
    calls = {"n": 0}

    def hold_guard_on_attempt(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    import fcntl

    with (deps.ledger_root / ".fault.guard").open("a") as other:
        # Preflight would see it too; take it after the preflight passes.
        real_preflight = eval_live.preflight

        def preflight(config, deps):
            code = real_preflight(config, deps)
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return code

        eval_live.preflight = preflight  # type: ignore[assignment]
        try:
            result = eval_live.run_round(cfg, deps)
        finally:
            eval_live.preflight = real_preflight  # type: ignore[assignment]
            fcntl.flock(other, fcntl.LOCK_UN)
    slot = result.slots[0]
    assert (slot.status, slot.reason) == ("setup_failed", "LAB_OPERATION_IN_PROGRESS")
    assert lab.writes == []


# -- CLI ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["--mode", "replay"],
        ["--parallel", "2"],
        ["--n", "0"],
        ["--scenario", "nope"],
        ["--scenario", "all", "--scenario", "eval-normal-1"],
        ["--flagd-url", "http://10.0.0.1:1"],
        ["--round", "Bad_Round"],
    ],
)
def test_cli_usage_errors_exit_2_and_write_nothing(env, argv, capsys):
    deps = env["deps"]
    base = {
        "--mode": "live",
        "--round": "r1",
        "--n": "1",
        "--parallel": "1",
        "--holder": "lead",
        "--batch": "b1",
        "--flagd-url": "http://127.0.0.1:18013",
        "--pg-port": "55999",
    }
    scenario = ["--scenario", "eval-normal-1"]
    for i in range(0, len(argv), 2):
        if argv[i] == "--scenario":
            scenario = []
    overrides = dict(zip(argv[::2], argv[1::2], strict=True))
    args = [
        item
        for key, value in {**base, **overrides}.items()
        if key != "--scenario"
        for item in (key, value)
    ]
    args += scenario + [
        a
        for i in range(0, len(argv), 2)
        if argv[i] == "--scenario"
        for a in argv[i : i + 2]
    ]
    assert eval_live.main(args, deps=deps) == 2
    assert not deps.evidence_root.exists()
    assert (
        json.loads(capsys.readouterr().out.strip().splitlines()[-1])["exit_code"] == 2
    )


def test_cli_dry_run_touches_nothing(env, capsys):
    deps = env["deps"]
    argv = [
        "--mode", "live", "--round", "r1", "--scenario", "all", "--n", "2",
        "--parallel", "1", "--holder", "lead", "--batch", "b1",
        "--flagd-url", "http://127.0.0.1:18013", "--pg-port", "55999", "--dry-run",
    ]  # fmt: skip
    assert eval_live.main(argv, deps=deps) == 0
    plan = json.loads(capsys.readouterr().out)
    assert len(plan["slots"]) == 12
    ids = [s["scenario_id"] for s in plan["slots"]]
    assert ids == sorted(ids)
    assert env["lab"].resolve_calls == 0 and not deps.evidence_root.exists()


def test_fault_flag_table_matches_the_kind_lab_whitelist():
    assert {f.flag: f.fault_variant for f in eval_live_lab.FAULT_FLAGS.values()} == {
        "paymentFailure": "100%",
        "productCatalogFailure": "on",
        "cartFailure": "on",
    }
    raw = ORIGINAL
    assert eval_live_lab.flagd_method(raw, "paymentFailure") == "Float"
    assert eval_live_lab.flagd_method(raw, "cartFailure") == "Boolean"
    with pytest.raises(LabError, match="FAULT_NOT_WHITELISTED"):
        eval_live_lab.fault_for("adFailure", "on")


def test_a_driver_bug_after_injection_still_restores_under_the_guard(env, monkeypatch):
    deps, lab = env["deps"], env["lab"]

    def broken(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(eval_live, "fault_gate", broken)
    result = eval_live.run_round(config(deps, ["eval-cart-failure"]), deps)
    slot = result.slots[0]
    assert (slot.status, slot.reason) == ("setup_failed", "DRIVER_ERROR")
    assert lab.raw == ORIGINAL and not (deps.ledger_root / "fault-active.json").exists()


# -- real adapters, with their transport stubbed ---------------------------------


def test_kubectl_store_patches_by_cas_and_fences_with_the_annotation(monkeypatch):
    calls: list[list[str]] = []
    document = {
        "metadata": {"uid": "u1", "resourceVersion": "7", "annotations": {"a": "b"}},
        "data": {"demo.flagd.json": ORIGINAL.decode()},
    }

    def external(command, *, timeout, data=None):
        calls.append(command)
        if "patch" in command:
            patch = json.loads(command[command.index("-p") + 1])
            written = json.loads(json.dumps(document))
            written["metadata"]["resourceVersion"] = "8"
            for op in patch:
                if op["path"] == "/metadata/annotations":
                    written["metadata"]["annotations"] = op["value"]
            return json.dumps(written).encode()
        return json.dumps(document).encode()

    monkeypatch.setattr(eval_live_lab, "_external", external)
    monkeypatch.setattr(
        eval_live_lab,
        "_patch",
        lambda command, *, timeout: external(command, timeout=timeout),
    )
    store = eval_live_lab.KubectlFlagStore()
    current = store.read()
    assert (current.uid, current.resource_version) == ("u1", "7")
    store.replace(current, ORIGINAL, fence="f1")
    patch = json.loads(calls[-1][calls[-1].index("-p") + 1])
    tests = {op["path"]: op["value"] for op in patch if op["op"] == "test"}
    assert tests["/metadata/uid"] == "u1" and tests["/metadata/resourceVersion"] == "7"
    assert tests["/data/demo.flagd.json"] == ORIGINAL.decode()
    fence = next(op for op in patch if op["path"] == "/metadata/annotations")
    assert fence["value"] == {"a": "b", eval_live_lab.FENCE_ANNOTATION: "f1"}
    assert "--context" in calls[-1]


def test_lab_telemetry_parses_jaeger_and_marks_a_full_page_incomplete(monkeypatch):
    trace = {
        "traceID": "t1",
        "processes": {"p1": {"serviceName": "checkout"}},
        "spans": [
            {
                "spanID": "s1",
                "processID": "p1",
                "operationName": "oteldemo.CartService/EmptyCart",
                "startTime": int(T0.timestamp() * 1_000_000),
                "tags": [{"key": "error", "value": True}],
            }
        ],
    }
    pages = {"n": 1}
    monkeypatch.setattr(
        eval_live_lab,
        "_get_json_sized",
        lambda url, timeout: ({"data": [trace] * pages["n"]}, 100),
    )
    probe = eval_live_lab.LabTelemetry()
    query = probe.spans("checkout", T0, T0 + timedelta(seconds=5), 5)
    assert query.complete and query.spans[0].start == T0
    assert eval_live_lab.span_error(query.spans[0].tags)
    pages["n"] = eval_live_lab.TRACE_LIMIT  # always full: splitting cannot finish
    assert not probe.spans("checkout", T0, T0, 5).complete
    monkeypatch.setattr(
        eval_live_lab, "_get_json_sized", lambda url, timeout: (None, 0)
    )
    assert not probe.spans("checkout", T0, T0, 5).complete


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:1",
        "http://example.com:1",
        "http://127.0.0.1",
        "http://u:p@127.0.0.1:1",
        "http://127.0.0.1:1/x",
        "http://127.0.0.1:1?q=1",
    ],
)
def test_flagd_url_must_be_a_bare_loopback_endpoint(url):
    with pytest.raises(LabError, match="LOCAL_FLAGD_URL_REQUIRED"):
        eval_live_lab.check_flagd_url(url)


# -- r4: a refused round makes no external call ---------------------------------


class _Counting:
    """Wraps a seam and counts every method call made through it."""

    def __init__(self, inner: Any, calls: list[str], name: str) -> None:
        self._inner, self._calls, self._name = inner, calls, name

    def __getattr__(self, attr: str) -> Any:
        value = getattr(self._inner, attr)
        if not callable(value):
            return value

        def call(*args: Any, **kwargs: Any) -> Any:
            self._calls.append(f"{self._name}.{attr}")
            return value(*args, **kwargs)

        return call


class _BeginningInvestigator(Investigator):
    def __init__(self, lab: Lab, clock: Clock, deps_ref: dict[str, Any]) -> None:
        super().__init__(lab, clock)
        self.begun: list[tuple[str, bool]] = []
        self.deps_ref = deps_ref
        self.fail: str | None = None

    def begin_round(self, round_id: str) -> None:
        journals = list(self.deps_ref["deps"].evidence_root.rglob("attempt.json"))
        self.begun.append((round_id, bool(journals)))
        if self.fail is not None:
            raise LabError(self.fail)


def _counted(env: dict[str, Any]) -> tuple[LiveDeps, list[str], _BeginningInvestigator]:
    deps, calls = env["deps"], []
    investigator = _BeginningInvestigator(env["lab"], env["clock"], {"deps": deps})
    counted = replace(
        deps,
        flags=_Counting(deps.flags, calls, "flags"),
        flagd=_Counting(deps.flagd, calls, "flagd"),
        telemetry=_Counting(deps.telemetry, calls, "telemetry"),
        identity=_Counting(deps.identity, calls, "identity"),
        ports=_Counting(deps.ports, calls, "ports"),
        pg=_Counting(deps.pg, calls, "pg"),
        balance=_Counting(deps.balance, calls, "balance"),
        investigator=_Counting(investigator, calls, "investigator"),
    )
    investigator.deps_ref["deps"] = counted
    return counted, calls, investigator


def test_a_round_refused_before_any_lab_read_makes_zero_external_calls(env):
    deps, calls, investigator = _counted(env)
    (deps.ledger_root / "occupation.json").unlink()
    result = eval_live.run_round(config(deps, ["eval-cart-failure"]), deps)
    assert (result.exit_code, result.reason) == (3, "LAB_RESERVATION_REQUIRED")
    assert calls == [] and investigator.begun == []


def test_a_round_refused_at_the_flag_check_never_begins_or_spends(env):
    deps, calls, investigator = _counted(env)
    data = json.loads(ORIGINAL)
    data["flags"]["cartFailure"]["defaultVariant"] = "on"
    env["lab"].raw = json.dumps(data).encode()
    result = eval_live.run_round(config(deps, ["eval-cart-failure"]), deps)
    assert (result.exit_code, result.reason) == (3, "FLAGS_NOT_NORMAL")
    assert investigator.begun == []
    forbidden = ("pg.", "balance.", "investigator.", "telemetry.")
    assert not [c for c in calls if c.startswith(forbidden)]
    assert not [c for c in calls if c == "flags.replace"]


def test_begin_round_runs_once_after_the_preflight_and_before_any_journal(env):
    deps, calls, investigator = _counted(env)
    eval_live.run_round(config(deps, ["eval-normal-1"], n=2), deps)
    assert investigator.begun == [("r1", False)]
    assert calls.index("investigator.begin_round") < calls.index("pg.start")


def test_a_failed_round_begin_is_a_refusal_without_journal(env):
    deps, calls, investigator = _counted(env)
    investigator.fail = "TRACE_PROJECT_UNAVAILABLE"
    result = eval_live.run_round(config(deps, ["eval-normal-1"]), deps)
    assert (result.exit_code, result.reason) == (3, "TRACE_PROJECT_UNAVAILABLE")
    assert not list(deps.evidence_root.rglob("attempt.json"))
    assert not [c for c in calls if c.startswith(("pg.", "balance."))]
    assert not (deps.ledger_root / "run-starts.jsonl").exists()


def test_real_deps_build_makes_no_langsmith_call(tmp_path, monkeypatch):
    from scripts import eval_live_investigator, lab_evidence

    langsmith: list[str] = []

    def no_project(*args: Any, **kwargs: Any) -> Any:
        langsmith.append("project")
        return {"project": "p", "project_id": "id", "trace_tier": "longlived"}

    def no_client(*args: Any, **kwargs: Any) -> Any:
        langsmith.append("client")
        raise AssertionError("no LangSmith client before the preflight")

    for module in (eval_live_investigator, lab_evidence):
        monkeypatch.setattr(module, "prepare_lab_project", no_project, raising=False)
        monkeypatch.setattr(module, "langsmith_client", no_client, raising=False)
    monkeypatch.setattr(eval_live_investigator.tracing, "configure", lambda env: None)
    env_file = tmp_path / "env"
    env_file.write_text("DEEPSEEK_API_KEY=synthetic\nLANGSMITH_API_KEY=synthetic\n")
    monkeypatch.setenv("M0_ENV_FILE", str(env_file))
    monkeypatch.setenv("OPSPILOT_TRACE", "lab")
    monkeypatch.setenv("OPSPILOT_TOOL_PROFILE", "otel-demo")
    for name in ("LANGSMITH_API_KEY", "LANGSMITH_PROJECT", "LANGSMITH_ENDPOINT"):
        # setenv first so monkeypatch restores the variables the code sets.
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    deps = eval_live.real_deps("http://127.0.0.1:18013", "r1")
    assert langsmith == []
    assert deps.env.check() is None
    deps.investigator.begin_round("r1")
    assert langsmith == ["project"]


# -- r5: provenance seam, verification-consistent slots, inclusive wash budget ----


class _Provenance:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def read(self):
        self.calls.append("provenance.read")
        return eval_live_lab.Provenance("a" * 40, True)


def test_provenance_is_read_once_after_preflight_and_never_on_refusal(env, monkeypatch):
    import subprocess

    def forbidden(*args, **kwargs):
        raise AssertionError("the driver core must start no process")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    calls: list[str] = []
    deps = replace(env["deps"], provenance=_Provenance(calls))
    argv = [
        "--mode", "live", "--round", "r1", "--scenario", "eval-normal-1", "--n", "1",
        "--parallel", "1", "--holder", "lead", "--batch", "b1",
        "--flagd-url", "http://127.0.0.1:18013", "--pg-port", "55999",
    ]  # fmt: skip
    (deps.ledger_root / "occupation.json").rename(deps.ledger_root / "held.json")
    assert eval_live.main(argv, deps=deps) == 3
    assert calls == []
    (deps.ledger_root / "held.json").rename(deps.ledger_root / "occupation.json")
    assert eval_live.main(argv, deps=deps) == 1  # fake investigator: no recording
    assert calls == ["provenance.read"]
    stored = next(deps.evidence_root.rglob("summary.json"))
    code = json.loads(stored.read_text())["capture"]["manifest"]["dimensions"]["code"]
    assert code == {"kind": "version", "value": "a" * 40, "reason": None}


def test_an_unverifiable_recording_aborts_consistently_and_is_not_linked(env):
    from opspilot.evaluation import ArtifactRef

    deps = env["deps"]
    original = env["investigator"].investigate

    def investigate(request):
        result = original(request)
        bogus = ArtifactRef(
            "recording.json", "0" * 64, 1, "application/json", "private"
        )
        return replace(result, recording_ref=bogus)

    env["investigator"].investigate = investigate  # type: ignore[method-assign]
    result = eval_live.run_round(config(deps, ["eval-normal-1"]), deps)
    slot = result.slots[0]
    assert slot.status == "aborted" and result.exit_code == 1
    assert lifecycle(deps)["slots"][0]["status"] == "aborted"
    capture = summary(deps, slot.attempt_id)["capture"]
    assert capture["attempt"]["status"] == "aborted"
    assert capture["recording_ref"] is None
    assert (deps.evidence_root / "r1/aggregate.json").exists()


def test_a_fresh_window_closing_exactly_at_the_budget_is_read(env):
    deps, lab = env["deps"], env["lab"]
    start = env["clock"].now()
    original = lab.spans

    def first_window_dirty(service, begin, end, timeout):
        query = original(service, begin, end, timeout)
        if begin == start and service == "checkout":
            span = ObservedSpan(
                "t", "s", "checkout", "oteldemo.CartService/EmptyCart", begin,
                {"otel.status_code": "ERROR"},
            )  # fmt: skip
            return SpanQuery((*query.spans, span), True)
        return query

    lab.spans = first_window_dirty  # type: ignore[method-assign]
    sigs = eval_live.fault_signatures(deps)
    result = eval_live_lab.normal_window(sigs, not_before=start, deps=deps)
    assert result.passed and result.window.start == start + timedelta(seconds=300)
    assert env["clock"].mono - 1000.0 == 600


def test_public_template_scope_separates_policy_from_run_identity():
    from opspilot.evaluation.live import scope_policy_hash
    from opspilot.tools import otel_demo

    template = eval_live.template_scope()
    run = replace(
        template,
        scope_id="otel-demo:run-9:3",
        subject_id="incident-9",
        run_id="run-9",
        control_generation=4,
        global_suspension_generation=5,
        target_suspension_generation=6,
        window=replace(
            template.window,
            start=template.window.start + timedelta(days=30),
            end=template.window.end + timedelta(days=30),
        ),
        deadline=template.deadline + timedelta(days=31),
    )
    assert scope_policy_hash(run) == scope_policy_hash(template)
    shorter = replace(
        template,
        window=replace(template.window, start=template.window.end - timedelta(hours=1)),
    )
    assert scope_policy_hash(shorter) != scope_policy_hash(template)
    assert template.target_ids == frozenset({otel_demo.TARGET_ID})
    assert template.default_query_window is None


# -- r6: independent review P2 findings ---------------------------------------


class _Completed:
    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


@pytest.mark.parametrize(
    "stderr,code",
    [
        (
            b"The request is invalid: the server rejected our request due to an "
            b"error in our request: testing value /metadata/resourceVersion failed",
            "FLAG_CAS_CONFLICT",
        ),
        (
            b"Error from server (Conflict): Operation cannot be fulfilled on "
            b'configmaps "flagd-config": the object has been modified',
            "FLAG_CAS_CONFLICT",
        ),
        (b"Precondition failed: UID in precondition: u1", "FLAG_CAS_CONFLICT"),
        (
            b"Unable to connect to the server: dial tcp: i/o timeout",
            "FLAG_WRITE_UNKNOWN",
        ),
    ],
)
def test_kubectl_patch_separates_definitive_refusal_from_unknown(
    monkeypatch, stderr, code
):
    """P2-1: a server-side CAS refusal is never adjudicated by re-read."""
    import subprocess

    def run(command, **kwargs):
        if "patch" in command:
            return _Completed(1, stderr=stderr)
        raise AssertionError("unexpected call")

    monkeypatch.setattr(subprocess, "run", run)
    store = eval_live_lab.KubectlFlagStore()
    current = FlagConfig(ORIGINAL, "u1", "10")
    with pytest.raises(LabError) as raised:
        store.replace(current, ORIGINAL, fence=None)
    assert raised.value.code == code


def test_kubectl_patch_timeout_is_unknown(monkeypatch):
    import subprocess

    def run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 60)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(LabError) as raised:
        eval_live_lab.KubectlFlagStore().replace(
            FlagConfig(ORIGINAL, "u1", "10"), ORIGINAL, fence=None
        )
    assert raised.value.code == "FLAG_WRITE_UNKNOWN"


def test_an_unknown_fence_write_needs_our_fence_on_read_back(env):
    """P2-1: another writer's version bump is not our fence; keep the intent."""
    deps, lab = env["deps"], env["lab"]
    lab.complete = False  # first round leaves a retained fault (wash fails)
    eval_live.run_round(config(deps, ["eval-cart-failure"]), deps)
    state_path = deps.ledger_root / "fault-active.json"
    assert state_path.exists() and lab.raw == ORIGINAL
    lab.complete = True

    def foreign_bump(current, new_raw, *, fence):
        # Our fence never lands; someone else bumps the version meanwhile.
        lab.rv += 1
        raise LabError("FLAG_WRITE_UNKNOWN")

    lab.replace = foreign_bump  # type: ignore[method-assign]
    result = eval_live.recover_round("r1", "lead", "b1", deps)
    assert (result.exit_code, result.reason) == (4, "FLAG_RESTORE_UNVERIFIED")
    assert state_path.exists()


def test_pg_start_failing_after_launch_stops_the_instance_it_started(
    tmp_path, monkeypatch
):
    """P2-2: a migration failure after ``pg_ctl start`` leaves no server."""
    import subprocess

    from opspilot import schema

    data = tmp_path / "pg"
    calls: list[str] = []

    def run(command, **kwargs):
        action = command[-1]
        calls.append(action)
        if Path(command[0]).name == "initdb":
            data.mkdir()
        elif action == "start":
            (data / "postmaster.pid").write_text(f"4242\n{data}\n0\n55998\n")
        elif action == "stop":
            (data / "postmaster.pid").unlink()
        return _Completed(0)

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, *args, **kwargs):
            return None

    def migrate(*args, **kwargs):
        raise RuntimeError("migration failed")

    import psycopg

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(eval_live_lab, "_pg_binary", lambda name: f"/bin/{name}")
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: Conn())
    monkeypatch.setattr(schema, "migrate", migrate)
    with pytest.raises(LabError, match="PG_START_FAILED"):
        eval_live_lab.LocalPgLauncher().start(data, 55998)
    assert calls[-1] == "stop"
    assert not (data / "postmaster.pid").exists()


class _InterruptingBalance:
    def __init__(self, at: int) -> None:
        self.at, self.calls = at, 0

    def balance(self):
        self.calls += 1
        if self.calls == self.at:
            raise Interrupted()
        return None


@pytest.mark.parametrize("at", [1, 2])
def test_a_signal_during_a_balance_read_still_writes_every_output(env, at):
    """P2-3: before (1) or after (2) the attempt body, exit 130 with outputs."""
    deps = replace(env["deps"], balance=_InterruptingBalance(at))
    cfg = config(deps, ["eval-cart-failure", "eval-normal-1"])
    result = eval_live.run_round(cfg, deps)
    assert (result.exit_code, result.status) == (130, "interrupted")
    first, later = result.slots
    assert later.status == "not_started" and later.reason == "ROUND_STOPPED"
    assert first.summary is not None
    if at == 1:
        assert (first.status, first.reason) == ("setup_failed", "INTERRUPTED")
        assert env["lab"].writes == []
    assert lifecycle(deps)["round_status"] == "interrupted"
    assert (deps.evidence_root / "r1/aggregate.json").exists()
    assert env["lab"].raw == ORIGINAL
    assert not (deps.ledger_root / "fault-active.json").exists()


# -- r7: independent re-verification P2 findings ------------------------------


def test_every_configmap_write_of_the_driver_carries_its_own_fence(env):
    deps, lab = env["deps"], env["lab"]
    eval_live.run_round(config(deps, ["eval-cart-failure"], n=2), deps)
    assert len(lab.fences) == 4  # inject + restore, twice
    assert all(isinstance(fence, str) and fence for fence in lab.fences)
    assert len(set(lab.fences)) == len(lab.fences)


def _retained_after_injection(env):
    """Run one cart attempt whose restore is refused, leaving live==injected."""
    deps, lab = env["deps"], env["lab"]
    real = lab.replace
    calls = {"n": 0}

    def refuse_restore(current, new_raw, *, fence):
        calls["n"] += 1
        if calls["n"] == 2:  # the restore write
            raise LabError("FLAG_CAS_CONFLICT")
        return real(current, new_raw, fence=fence)

    lab.replace = refuse_restore  # type: ignore[method-assign]
    eval_live.run_round(config(deps, ["eval-cart-failure"]), deps)
    lab.replace = real  # type: ignore[method-assign]
    state = json.loads((deps.ledger_root / "fault-active.json").read_text())
    assert lab.raw == state["injected"].encode()
    return real


def test_an_unknown_restore_from_injected_needs_our_fence(env):
    """P2-1: another writer restores the original while our write is lost."""
    deps, lab = env["deps"], env["lab"]
    _retained_after_injection(env)

    def foreign_restore(current, new_raw, *, fence):
        lab.set(new_raw)  # someone else wrote the original bytes, no fence
        raise LabError("FLAG_WRITE_UNKNOWN")

    lab.replace = foreign_restore  # type: ignore[method-assign]
    result = eval_live.recover_round("r1", "lead", "b1", deps)
    assert (result.exit_code, result.reason) == (4, "FLAG_RESTORE_UNVERIFIED")
    assert (deps.ledger_root / "fault-active.json").exists()


def test_our_own_unknown_restore_from_injected_is_verified_by_fence(env):
    deps, lab = env["deps"], env["lab"]
    _retained_after_injection(env)
    lab.lose_write_response = True  # our fenced write lands, response lost
    result = eval_live.recover_round("r1", "lead", "b1", deps)
    assert (result.exit_code, result.status) == (0, "recovered")
    assert not (deps.ledger_root / "fault-active.json").exists()


class _InterruptingPg(Pg):
    def __init__(self, deps_ref: dict[str, Any]) -> None:
        super().__init__(deps_ref)
        self.stop_calls = 0

    def stop(self, instance: PgInstance) -> None:
        self.stop_calls += 1
        if self.stop_calls == 1:
            raise Interrupted()
        super().stop(instance)


@pytest.mark.parametrize("restore_fails", [False, True])
def test_an_interrupt_during_pg_stop_still_closes_and_writes_the_attempt(
    env, restore_fails
):
    """P2-2: closure finishes, summary/lifecycle match the journal, 4 > 130."""
    pg = _InterruptingPg({"deps": env["deps"]})
    deps = replace(env["deps"], pg=pg)
    pg.deps_ref["deps"] = deps
    if restore_fails:
        env["lab"].complete = False  # wash fails -> restore failure
    cfg = config(deps, ["eval-cart-failure", "eval-normal-1"])
    result = eval_live.run_round(cfg, deps)
    first, later = result.slots
    assert pg.stop_calls == 2 and pg.stopped == pg.started
    assert first.attempt_id is not None and first.summary is not None
    stored = summary(deps, first.attempt_id)
    assert stored["capture"]["attempt"]["status"] == first.status
    assert first.status in ("aborted", "setup_failed")
    assert lifecycle(deps)["slots"][0]["status"] == first.status
    assert later.status == "not_started"
    if restore_fails:
        assert (result.exit_code, result.status) == (4, "stopped")
    else:
        assert (result.exit_code, result.status) == (130, "interrupted")


def test_fault_gate_reports_each_span_once_across_service_queries(env):
    """Live A11 probe: whole traces come back from both signature services."""
    deps, clock = env["deps"], env["clock"]
    start = clock.now()
    rule = next(
        answer.signature
        for spec, answer in load_scenarios(SCENARIOS)
        if spec.scenario_id == "eval-cart-failure"
    )
    whole_trace = tuple(
        ObservedSpan(
            "trace-1",
            f"span-{index}",
            matcher.service,
            matcher.operation,
            start + timedelta(seconds=1),
            {"otel.status_code": "ERROR"},
        )
        for index, matcher in enumerate(rule.any_of)
    )
    services = {m.service for m in rule.any_of}
    assert len(services) == 2

    def spans(service, begin, end, timeout):
        return SpanQuery(whole_trace if service in services else (), True)

    env["lab"].spans = spans  # type: ignore[method-assign]
    clock.advance(5)
    gate = eval_live_lab.fault_gate(rule, confirmed_at=start, deps=deps)
    assert gate.passed
    ids = [span.span_id for span in gate.matched]
    assert ids == ["span-0", "span-1"]
    assert eval_live_lab.observation_json(gate)["matched_span_ids"] == ids


# -- r11: PR #271 bot findings --------------------------------------------------


class _JaegerOpener:
    """A Jaeger search that, like the real one, honours a ``tags`` filter."""

    def __init__(self, body: dict[str, Any] | bytes) -> None:
        self.body, self.urls = body, []

    def open(self, request, timeout=None):
        import io
        from urllib.parse import parse_qs, urlsplit

        url = request.full_url if hasattr(request, "full_url") else str(request)
        self.urls.append(url)
        body = self.body
        query = parse_qs(urlsplit(url).query)
        if isinstance(body, dict) and "tags" in query:
            wanted = json.loads(query["tags"][0])
            body = {
                "data": [
                    trace
                    for trace in body["data"]
                    if any(
                        {t["key"]: str(t["value"]).lower() for t in span["tags"]}.get(k)
                        == v
                        for span in trace["spans"]
                        for k, v in wanted.items()
                    )
                ]
            }
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()

        class Response(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return Response(raw)


def _trace(trace_id: str, tags: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "traceID": trace_id,
        "processes": {"p1": {"serviceName": "checkout"}},
        "spans": [
            {
                "spanID": f"{trace_id}-s",
                "processID": "p1",
                "operationName": "oteldemo.CartService/EmptyCart",
                "startTime": int(T0.timestamp() * 1_000_000),
                "tags": tags,
            }
        ],
    }


def test_lab_telemetry_finds_errors_that_carry_no_error_tag(monkeypatch):
    """P1: status-code, gRPC and HTTP 5xx errors are decided locally."""
    body = {
        "data": [
            _trace("t-status", [{"key": "otel.status_code", "value": "ERROR"}]),
            _trace("t-grpc", [{"key": "rpc.grpc.status_code", "value": 13}]),
            _trace("t-http", [{"key": "http.status_code", "value": 503}]),
            _trace("t-ok", [{"key": "rpc.grpc.status_code", "value": 0}]),
        ]
    }
    opener = _JaegerOpener(body)
    monkeypatch.setattr(eval_live_lab.kind_lab, "OPENER", opener)
    query = eval_live_lab.LabTelemetry().spans("checkout", T0, T0, 5)
    assert query.complete
    errors = {s.trace_id for s in query.spans if eval_live_lab.span_error(s.tags)}
    assert errors == {"t-status", "t-grpc", "t-http"}
    assert all("tags=" not in url for url in opener.urls)


@pytest.mark.parametrize("defect", ["truncated", "oversize", "full_page"])
def test_lab_telemetry_marks_partial_answers_incomplete(monkeypatch, defect):
    """P1: a cut or capped search is incomplete, never 'no errors'."""
    trace = _trace("t", [{"key": "otel.status_code", "value": "ERROR"}])
    if defect == "truncated":
        body: Any = json.dumps({"data": [trace]}).encode()[:-10]
    elif defect == "oversize":
        body = {"data": [trace]}
        monkeypatch.setattr(eval_live_lab, "MAX_TELEMETRY_BYTES", 16)
    else:
        body = {"data": [trace]}
        monkeypatch.setattr(eval_live_lab, "TRACE_LIMIT", 1)
    monkeypatch.setattr(eval_live_lab.kind_lab, "OPENER", _JaegerOpener(body))
    assert not eval_live_lab.LabTelemetry().spans("checkout", T0, T0, 5).complete


CREDENTIAL_MARKERS = (
    "DEEPSEEK",
    "LANGSMITH",
    "PROMETHEUS",
    "PASSWORD",
    "TOKEN",
    "KEY",
    "SECRET",
    "PGPASS",
)


def test_pg_launcher_uses_scram_passwords_and_a_sanitized_environment(
    tmp_path, monkeypatch
):
    """P2 security: no trust auth, no credential variable reaches postgres."""
    import subprocess

    import psycopg

    from opspilot import schema

    for name in (
        "DEEPSEEK_API_KEY",
        "LANGSMITH_API_KEY",
        "OPSPILOT_OTEL_PROMETHEUS_PASSWORD",
        "OPSPILOT_LAB_PROMETHEUS_USERNAME",
        "PGPASSWORD",
        "GITHUB_TOKEN",
    ):
        monkeypatch.setenv(name, "CANARY-SECRET")
    monkeypatch.setenv("LC_ALL", "C")
    data = tmp_path / "attempt" / "pg"
    runs: list[dict[str, Any]] = []

    def run(command, **kwargs):
        record = {"command": command, "env": kwargs.get("env")}
        if Path(command[0]).name == "initdb":
            pwfile = Path(
                next(a for a in command if a.startswith("--pwfile=")).split("=", 1)[1]
            )
            record["pwfile_mode"] = pwfile.stat().st_mode & 0o777
            record["password"] = pwfile.read_text().strip()
            data.mkdir()
        elif command[-1] == "start":
            (data / "postmaster.pid").write_text(f"4242\n{data}\n0\n55997\n")
        runs.append(record)
        return _Completed(0)

    connects: list[str] = []
    statements: list[Any] = []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, statement, *args):
            statements.append(statement)

    def connect(conninfo, **kwargs):
        connects.append(conninfo)
        return Conn()

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(eval_live_lab, "_pg_binary", lambda name: f"/bin/{name}")
    monkeypatch.setattr(psycopg, "connect", connect)
    monkeypatch.setattr(schema, "migrate", lambda *a, **k: None)
    instance = eval_live_lab.LocalPgLauncher().start(data, 55997)

    initdb = runs[0]
    assert "--auth=scram-sha-256" in initdb["command"]
    assert not any("trust" in arg for arg in initdb["command"])
    assert initdb["pwfile_mode"] == 0o600 and len(initdb["password"]) >= 32
    assert initdb["password"] not in " ".join(initdb["command"])
    assert not list((tmp_path / "attempt").glob(".pwfile-*"))  # removed
    for record in runs:
        env = record["env"]
        assert env is not None, record["command"]
        assert "LC_ALL" in env
        assert not [k for k in env if any(m in k.upper() for m in CREDENTIAL_MARKERS)]
        assert "CANARY-SECRET" not in env.values()
    for dsn in (*connects, instance.owner_dsn, instance.runtime_dsn, instance.web_dsn):
        assert "password=" in dsn
    passwords = {
        part.split("=", 1)[1]
        for dsn in (instance.owner_dsn, instance.runtime_dsn, instance.web_dsn)
        for part in dsn.split()
        if part.startswith("password=")
    }
    assert len(passwords) == 3 and initdb["password"] in passwords
    created = [s for s in statements if "CREATE ROLE" in str(s)]
    assert len(created) == 2


# -- r12: adaptive split under a live-sized load --------------------------------


class _LoadedJaeger:
    """10,000 traces over 300 s; a page returns at most ``limit`` of the
    traces whose queried-service span starts in [start, end] (Jaeger rule)."""

    def __init__(self, traces: int = 10_000, seconds: int = 300) -> None:
        import bisect

        self.bisect = bisect
        self.requests: list[dict[str, str]] = []
        base = int(T0.timestamp() * 1_000_000)
        self.traces = []
        self.expected: dict[str, set[tuple[str, str]]] = {
            "checkout": set(),
            "payment": set(),
        }
        for index in range(traces):
            at = base + (index * seconds * 1_000_000) // traces
            checkout_tags: list[dict[str, Any]] = []
            payment_tags: list[dict[str, Any]] = [
                {"key": "rpc.grpc.status_code", "value": 0}
            ]
            if index % 97 == 0:
                checkout_tags = [{"key": "otel.status_code", "value": "ERROR"}]
            elif index % 89 == 0:
                payment_tags = [{"key": "rpc.grpc.status_code", "value": 13}]
            elif index % 83 == 0:
                checkout_tags = [{"key": "http.status_code", "value": 503}]
            elif index % 79 == 0:
                payment_tags = [{"key": "error", "value": True}]
            trace_id = f"t{index:05d}"
            if checkout_tags:
                self.expected["checkout"].add((trace_id, "c"))
            if (
                payment_tags[0]["key"] != "rpc.grpc.status_code"
                or payment_tags[0]["value"]
            ):
                self.expected["payment"].add((trace_id, "p"))
            self.traces.append(
                (
                    at,
                    {
                        "traceID": trace_id,
                        "processes": {
                            "p1": {"serviceName": "checkout"},
                            "p2": {"serviceName": "payment"},
                        },
                        "spans": [
                            {
                                "spanID": "c",
                                "processID": "p1",
                                "operationName": "oteldemo.PaymentService/Charge",
                                "startTime": at,
                                "tags": checkout_tags,
                            },
                            {
                                "spanID": "p",
                                "processID": "p2",
                                "operationName": "grpc.oteldemo.PaymentService/Charge",
                                "startTime": at + 10,
                                "tags": payment_tags,
                            },
                        ],
                    },
                )
            )
        self.starts = [at for at, _ in self.traces]

    def open(self, request, timeout=None):
        import io
        from urllib.parse import parse_qs, urlsplit

        query = {k: v[0] for k, v in parse_qs(urlsplit(request.full_url).query).items()}
        self.requests.append(query)
        low, high, limit = int(query["start"]), int(query["end"]), int(query["limit"])
        lo = self.bisect.bisect_left(self.starts, low - 10)
        hi = self.bisect.bisect_right(self.starts, high)
        page = [trace for _, trace in self.traces[lo:hi]][:limit]
        raw = json.dumps({"data": page}).encode()

        class Response(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return Response(raw)


@pytest.mark.parametrize("service", ["checkout", "payment"])
def test_adaptive_split_completes_a_live_sized_window_and_finds_every_error(
    monkeypatch, service
):
    jaeger = _LoadedJaeger()
    monkeypatch.setattr(eval_live_lab.kind_lab, "OPENER", jaeger)
    end = T0 + timedelta(seconds=300)
    query = eval_live_lab.LabTelemetry().spans(service, T0, end, 5)
    assert query.complete
    found = {(span.trace_id, span.span_id) for span in query.spans}
    assert found == jaeger.expected[service] and found
    assert all(span.service == service for span in query.spans)
    assert len(found) == len(query.spans)  # deduplicated
    # 10,000 traces: 300 s full -> 4 x 75 s full -> 16 x 18.75 s (625 each).
    assert len(jaeger.requests) == 21
    assert all("tags" not in request for request in jaeger.requests)
    forms = {next(iter(span.tags)) for span in query.spans}
    expected_forms = (
        {"otel.status_code", "http.status_code"}
        if service == "checkout"
        else {"rpc.grpc.status_code", "error"}
    )
    assert forms == expected_forms


def test_adaptive_split_reports_incomplete_when_a_cap_is_reached(monkeypatch):
    jaeger = _LoadedJaeger()
    monkeypatch.setattr(eval_live_lab.kind_lab, "OPENER", jaeger)
    monkeypatch.setattr(eval_live_lab, "MAX_SUBREQUESTS", 5)
    query = eval_live_lab.LabTelemetry().spans(
        "checkout", T0, T0 + timedelta(seconds=300), 5
    )
    assert not query.complete and len(jaeger.requests) == 5
    monkeypatch.setattr(eval_live_lab, "MAX_SUBREQUESTS", 64)
    monkeypatch.setattr(eval_live_lab, "MAX_SEARCH_BYTES", 1024)
    assert (
        not eval_live_lab.LabTelemetry()
        .spans("checkout", T0, T0 + timedelta(seconds=300), 5)
        .complete
    )


# -- r13: stage deadline and exit-code precedence -------------------------------


def _slow(env, seconds: float):
    """Each telemetry span query takes ``seconds`` of the fake clock."""
    lab, clock = env["lab"], env["clock"]
    real = lab.spans

    def spans(service, start, end, timeout):
        clock.advance(seconds)
        return real(service, start, end, timeout)

    lab.spans = spans  # type: ignore[method-assign]


def test_normal_window_judged_after_the_deadline_is_never_clean(env):
    deps = env["deps"]
    _slow(env, 100)  # 4 services x 100 s after the 300 s wait -> 700 s > 600 s
    sigs = eval_live.fault_signatures(deps)
    result = eval_live_lab.normal_window(sigs, not_before=env["clock"].now(), deps=deps)
    assert not result.passed and result.reason == "NORMAL_WINDOW_UNCONFIRMED"


def test_fault_gate_never_passes_after_the_deadline(env):
    deps, lab, clock = env["deps"], env["lab"], env["clock"]
    lab.set(d_inject_cart(lab.raw))
    clock.advance(lab.delay + 1)
    confirmed = clock.now()
    clock.advance(5)
    _slow(env, 400)  # two signature services -> 800 s on the first poll
    rule = next(
        a.signature
        for s, a in load_scenarios(SCENARIOS)
        if s.scenario_id == "eval-cart-failure"
    )
    result = eval_live_lab.fault_gate(rule, confirmed_at=confirmed, deps=deps)
    assert not result.passed and result.reason == "FAULT_NOT_OBSERVED"


def d_inject_cart(raw: bytes) -> bytes:
    data = json.loads(raw)
    data["flags"]["cartFailure"]["defaultVariant"] = "on"
    return json.dumps(data).encode()


def test_lab_telemetry_stops_sub_requests_at_the_stage_deadline(monkeypatch):
    clock = Clock()
    jaeger = _LoadedJaeger()
    timeouts: list[float] = []
    real_open = jaeger.open

    def slow_open(request, timeout=None):
        timeouts.append(timeout)
        clock.advance(4)  # each Jaeger request takes 4 s
        return real_open(request, timeout)

    jaeger.open = slow_open  # type: ignore[method-assign]
    monkeypatch.setattr(eval_live_lab.kind_lab, "OPENER", jaeger)
    probe = eval_live_lab.LabTelemetry(clock)
    deadline = clock.monotonic() + 10  # room for 2.5 requests
    query = probe.spans_within(
        "checkout",
        T0,
        T0 + timedelta(seconds=300),
        request_timeout=5,
        deadline=deadline,
    )
    assert not query.complete
    assert len(timeouts) == 3 and timeouts[0] == 5 and timeouts[-1] == pytest.approx(2)
