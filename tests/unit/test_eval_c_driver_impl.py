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

    def _variants(self, raw: bytes) -> dict[str, str]:
        return {
            name: entry["defaultVariant"]
            for name, entry in json.loads(raw)["flags"].items()
        }

    # FlagStore
    def read(self) -> FlagConfig:
        if self.fail_reads_after_write and self.writes:
            raise LabError("FLAG_READ_FAILED")
        return FlagConfig(self.raw, self.uid, str(self.rv))

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
        self.set(new_raw)
        if self.lose_write_response:
            self.lose_write_response = False  # one lost response only
            raise LabError("FLAG_WRITE_UNKNOWN")
        return FlagConfig(self.raw, self.uid, str(self.rv))

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
        "_get_json",
        lambda url, timeout: {"data": [trace] * pages["n"]},
    )
    probe = eval_live_lab.LabTelemetry()
    query = probe.spans("checkout", T0, T0 + timedelta(seconds=5), 5)
    assert query.complete and query.spans[0].start == T0
    assert eval_live_lab.span_error(query.spans[0].tags)
    pages["n"] = eval_live_lab.TRACE_LIMIT
    assert not probe.spans("checkout", T0, T0, 5).complete
    monkeypatch.setattr(eval_live_lab, "_get_json", lambda url, timeout: None)
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
