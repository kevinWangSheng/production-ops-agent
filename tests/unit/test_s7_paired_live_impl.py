"""S7c (F1 closure R7): a paired attempt over the PR-C offline lab doubles."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from opspilot.evaluation import load_scenarios_v2
from scripts import eval_live
from tests.acceptance import test_432_dev_round as dr

DEV_V2 = dr.DEV_V2


class FakeBaseline:
    def __init__(self, status: str = "ran", raises: bool = False) -> None:
        self.calls: list[eval_live.BaselineRequest] = []
        self.status = status
        self.raises = raises

    def run(self, request: eval_live.BaselineRequest) -> eval_live.BaselineResult:
        self.calls.append(request)
        if self.raises:
            raise RuntimeError("boom")
        return eval_live.BaselineResult(
            status=self.status,  # type: ignore[arg-type]
            product_run_created=self.status != "setup_failed",
            output_sha256="0" * 64,
        )


@pytest.fixture
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(eval_live, "HOLMES_LOCK", tmp_path / "holmes.lock")
    root = tmp_path / "dev-v2"
    shutil.copytree(DEV_V2 / "regression", root / "regression")
    shutil.copytree(DEV_V2 / "capability", root / "capability")
    load_scenarios_v2(root)
    env = dr.pr_c.make_env(tmp_path)
    env["lab"].raw = dr.ORIGINAL_V2
    env["deps"] = replace(
        env["deps"], scenarios_root=root, provenance=dr.Provenance("a" * 40)
    )
    env["root"] = root
    return env


def _round(lab, sid: str, order: str, baseline: Any) -> tuple[Any, Path]:
    deps = replace(lab["deps"], baseline=baseline)
    slots = eval_live.plan_slots(lab["root"], [sid], 1)
    config = eval_live.RoundConfig(
        "s7pair",
        slots,
        "lead",
        "b1",
        55999,
        None,
        ("t",),
        1,
        eval_live.PAIR_ORDERS[order],
        True,
    )
    result = eval_live.run_round(config, deps)
    return result, deps.evidence_root / "s7pair"


def _pair(evidence: Path, result: Any) -> dict[str, Any]:
    attempt = result.slots[0].attempt_id
    return json.loads((evidence / "runs" / attempt / "pair.json").read_text())


@pytest.mark.parametrize(
    ("order", "systems"),
    [
        ("opspilot-first", ["opspilot", "holmes"]),
        ("holmes-first", ["holmes", "opspilot"]),
    ],
)
def test_both_systems_run_under_one_injection_in_order(lab, order, systems):
    baseline = FakeBaseline()
    result, evidence = _round(lab, "dv2-2b214b", order, baseline)
    pair = _pair(evidence, result)
    assert pair["order"] == systems
    assert [s["system"] for s in pair["slots"]] == systems
    assert [s["position"] for s in pair["slots"]] == [1, 2]
    holmes = next(s for s in pair["slots"] if s["system"] == "holmes")
    opspilot = next(s for s in pair["slots"] if s["system"] == "opspilot")
    if opspilot["status"] == "not_started" or holmes["status"] == "not_started":
        # the second system's re-confirmation failed: recorded, never silent
        assert any(
            str(s["reason"]).startswith("PAIR_REGATE:") for s in pair["slots"]
        ), pair
    else:
        assert len(baseline.calls) == 1
        assert baseline.calls[0].position == systems.index("holmes") + 1
        assert holmes["status"] == "ran"
        assert opspilot["status"] == result.slots[0].status
    # one injection: the round restored exactly one fault
    assert result.slots[0].fault is not None


def test_baseline_exception_is_that_slots_abort(lab):
    result, evidence = _round(
        lab, "dv2-2b214b", "holmes-first", FakeBaseline(raises=True)
    )
    holmes = _pair(evidence, result)["slots"][0]
    assert holmes["status"] == "aborted" and holmes["reason"] == "BASELINE_FAILED"


def test_unpaired_round_writes_no_pair_file(lab):
    slots = eval_live.plan_slots(lab["root"], ["dv2-2b214b"], 1)
    config = eval_live.RoundConfig(
        "s7solo", slots, "lead", "b1", 55999, None, ("t",), 1
    )
    result = eval_live.run_round(config, lab["deps"])
    attempt = result.slots[0].attempt_id
    run = lab["deps"].evidence_root / "s7solo" / "runs" / attempt
    assert run.is_dir() and not (run / "pair.json").exists()


@pytest.mark.parametrize("sid", ["dv2-30921c"])
def test_pair_refuses_alert_entry(lab, sid):
    slots = eval_live.plan_slots(lab["root"], [sid], 1)
    with pytest.raises(eval_live._Usage, match="PAIR_ENTRY_UNSUPPORTED"):
        eval_live.check_pair(slots, probe=False)
    two = eval_live.plan_slots(lab["root"], ["dv2-2b214b"], 2)
    with pytest.raises(eval_live._Usage, match="PAIR_ONE_SLOT"):
        eval_live.check_pair(two, probe=False)


def test_shared_setup_failure_marks_both_slots(lab):
    # adFailure's own errors are not produced by the offline lab double
    result, evidence = _round(lab, "dv2-8b262a", "opspilot-first", FakeBaseline())
    slots = _pair(evidence, result)["slots"]
    assert [s["status"] for s in slots] == ["setup_failed", "setup_failed"]
    assert {s["reason"] for s in slots} == {result.slots[0].reason}
    assert not any(s["product_run_created"] for s in slots)


@pytest.mark.parametrize("order", ["opspilot-first", "holmes-first"])
def test_failed_regate_fails_only_the_second_system(lab, monkeypatch, order):
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise eval_live.LabError("SIGNATURE_NOT_RECONFIRMED")

    monkeypatch.setattr(eval_live, "_regate", refuse)
    baseline = FakeBaseline()
    result, evidence = _round(lab, "dv2-2b214b", order, baseline)
    first, second = _pair(evidence, result)["slots"]
    assert second["status"] == "setup_failed"
    assert second["reason"] == "PAIR_REGATE:SIGNATURE_NOT_RECONFIRMED"
    assert first["status"] in ("ran", "aborted")
    if order == "opspilot-first":
        assert baseline.calls == []
        assert result.slots[0].status == first["status"]
    else:
        assert len(baseline.calls) == 1
        assert result.slots[0].status == "setup_failed"
        assert result.slots[0].product_run_id is None


def test_opspilot_only_window_still_writes_its_record(lab):
    baseline = FakeBaseline()
    result, evidence = _round(lab, "dv2-30921c", "opspilot-only", baseline)
    (slot,) = _pair(evidence, result)["slots"]
    assert slot["system"] == "opspilot" and slot["position"] == 1
    assert slot["status"] == result.slots[0].status
    assert baseline.calls == []
    alert = eval_live.plan_slots(lab["root"], ["dv2-30921c"], 1)
    eval_live.check_pair(alert, probe=False, alone=True)


def test_waiting_for_the_host_holmes_lock_reconfirms_the_gate(lab, monkeypatch):
    import fcntl

    calls: list[str] = []
    real = eval_live._regate

    def regate(*args: Any, **kwargs: Any) -> Any:
        calls.append("regate")
        return real(*args, **kwargs)

    monkeypatch.setattr(eval_live, "_regate", regate)
    monkeypatch.setattr(eval_live, "HOLMES_LOCK_POLL_SECONDS", 0.01)
    holder = eval_live.HOLMES_LOCK.open("a")
    fcntl.flock(holder, fcntl.LOCK_EX)
    sleeps = {"n": 0}
    clock = lab["deps"].clock
    original = clock.sleep

    def sleep(seconds: float) -> None:
        if seconds == eval_live.HOLMES_LOCK_POLL_SECONDS:
            sleeps["n"] += 1
            fcntl.flock(holder, fcntl.LOCK_UN)  # another lab's slot ends
        original(seconds)

    monkeypatch.setattr(clock, "sleep", sleep)
    _round(lab, "dv2-2b214b", "holmes-first", FakeBaseline())
    holder.close()
    # holmes (position 1) waited, so it re-confirms; opspilot (2) always does
    assert sleeps["n"] >= 1
    assert calls == ["regate", "regate"]


def test_holmes_adapter_maps_s7b_results():
    from datetime import UTC, datetime

    seen: dict[str, Any] = {}

    def fake(spec, answer, **kw):
        seen.update(kw)
        return {
            "status": "aborted",
            "reason": "DEADLINE_PASSED",
            "started_at": None,
            "ended_at": None,
            "holmes_json_sha256": None,
        }

    request = eval_live.BaselineRequest(
        attempt_id="a1",
        round_id="r1",
        spec=None,  # type: ignore[arg-type]
        answer=None,  # type: ignore[arg-type]
        plan=None,  # type: ignore[arg-type]
        output_dir=Path("/x/holmes-r1"),
        position=1,
        submit_not_after=datetime.now(UTC),
    )
    out = eval_live.HolmesBaseline(fake).run(request)
    assert out.status == "setup_failed" and not out.product_run_created
    assert "deadline" not in seen  # the frozen wall budget stays (R15)
    own = eval_live.holmes_attempt_id("r1", "a1")
    assert own.startswith("holmes-") and own != "a1"
    assert seen["raw_root"] == Path("/x/holmes-r1") and seen["attempt_id"] == own
    assert seen["submit_not_after"] == request.submit_not_after.timestamp()
    assert out.attempt_id == own and out.output_path == f"/x/holmes-r1/{own}"

    ran = eval_live.HolmesBaseline(
        lambda spec, answer, **kw: {
            "status": "ran",
            "reason": None,
            "started_at": "2026-11-05T00:00:00+00:00",
            "ended_at": "2026-11-05T00:03:00+00:00",
            "holmes_json_sha256": "f" * 64,
        }
    ).run(request)
    assert ran.status == "ran" and ran.product_run_created
    assert ran.output_sha256 == "f" * 64
    assert not ran.provider_error
    late = eval_live.HolmesBaseline(
        lambda spec, answer, **kw: {"status": "setup_failed", "reason": "SUBMIT_LATE"}
    ).run(request)
    assert late.status == "setup_failed" and late.reason == "SUBMIT_LATE"
    outage = eval_live.HolmesBaseline(
        lambda spec, answer, **kw: {
            "status": "aborted",
            "reason": None,
            "provider_error": True,
        }
    ).run(request)
    assert outage.provider_error and outage.product_run_created


def test_pair_record_carries_ids_baseline_and_store_restarts(lab):
    reads = iter(["jaeger-x/jaeger=2", "jaeger-x/jaeger=3"])

    class Telemetry:
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def instant(self, expr, at, timeout):
            return 412.5

    lab["deps"] = replace(
        lab["deps"],
        lab_restarts=lambda: next(reads),
        telemetry=Telemetry(lab["deps"].telemetry),
    )
    result, evidence = _round(lab, "dv2-2b214b", "opspilot-first", FakeBaseline())
    pair = _pair(evidence, result)
    assert pair["baseline_p95_ms"] == 412.5
    assert pair["lab_restarts"] == {
        "before": "jaeger-x/jaeger=2",
        "after": "jaeger-x/jaeger=3",
        "changed": True,
    }
    ops = pair["slots"][0]
    assert ops["attempt_id"] == result.slots[0].attempt_id
    assert set(ops["host_noise"]) == {"steal_percent", "load1"}


@pytest.mark.parametrize(
    ("env", "suffix", "expected"),
    [
        (None, False, True),
        (None, True, False),
        ("0", False, False),
        ("1", True, True),
    ],
)
def test_holmes_host_lock_switch(monkeypatch, env, suffix, expected):
    from scripts import kind_lab

    if env is None:
        monkeypatch.delenv("OPSPILOT_HOLMES_HOST_LOCK", raising=False)
    else:
        monkeypatch.setenv("OPSPILOT_HOLMES_HOST_LOCK", env)
    if suffix:
        monkeypatch.setattr(kind_lab, "SUFFIX", "", raising=False)
    elif hasattr(kind_lab, "SUFFIX"):
        monkeypatch.delattr(kind_lab, "SUFFIX")
    assert eval_live.holmes_lock_enabled() is expected
