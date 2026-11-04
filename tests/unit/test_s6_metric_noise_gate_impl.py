"""Implementation tests for F1 S6: the holdout flags on the v2 whitelist and
the metric noise gate for the noise flags that raise no error span
(prereview §1 A, §2.2 S6, R3)."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from opspilot.evaluation import SignatureRule
from scripts import eval_live, eval_live_lab
from scripts.eval_live_lab import (
    METRIC_NOISE_RULES,
    NOISE_NOT_OBSERVED,
    CallCount,
    LabError,
    MetricNoiseRule,
    MetricSignal,
    metric_gate,
    noise_rule,
    validate_injectors,
)
from tests.unit import test_eval_c_driver_impl as pr_c
from tests.unit.test_431_injectors_impl import ORIGINAL_V2

ROOT = Path(__file__).resolve().parents[2]
DEV_V2 = ROOT / "evals/datasets/dev-v2"
NOISE_NEG = "dv2-eac1f4"
NEW = ("paymentUnreachable", "adManualGc", "loadGeneratorFloodHomepage")


def _flagd(flag: str, variant: str = "on") -> SimpleNamespace:
    return SimpleNamespace(kind="flagd", params={"flag": flag, "variant": variant})


# -- whitelist -------------------------------------------------------------------


@pytest.mark.parametrize("flag", NEW)
def test_new_flags_are_whitelisted_alone_and_beside_a_signature_fault(flag):
    assert validate_injectors("question", [_flagd(flag)]).flags[0].flag == flag
    plan = validate_injectors(
        "question", [_flagd("paymentFailure", "100%"), _flagd(flag)]
    )
    assert [f.flag for f in plan.flags] == ["paymentFailure", flag]


@pytest.mark.parametrize("flag", NEW)
def test_new_flags_accept_only_their_on_variant(flag):
    with pytest.raises(LabError, match="FAULT_NOT_WHITELISTED"):
        validate_injectors("question", [_flagd(flag, "100")])


@pytest.mark.parametrize("flag", ["kafkaQueueProblems", "adHighCpu", "imageSlowLoad"])
def test_unselected_flags_stay_off_the_whitelist(flag):
    with pytest.raises(LabError, match="FAULT_NOT_WHITELISTED"):
        validate_injectors("question", [_flagd(flag)])


def test_new_flags_mutate_like_adfailure():
    for flag in NEW:
        fault = eval_live_lab.V2_FAULT_FLAGS[flag]
        data = json.loads(eval_live_lab.mutate(ORIGINAL_V2, fault))
        assert data["flags"][flag]["defaultVariant"] == "on"
        others = {k: v for k, v in data["flags"].items() if k != flag}
        assert others == {
            k: v for k, v in json.loads(ORIGINAL_V2)["flags"].items() if k != flag
        }


def test_noise_rule_lookup_by_flag_name():
    assert isinstance(noise_rule("adFailure"), SignatureRule)
    assert isinstance(noise_rule("adManualGc"), MetricNoiseRule)
    assert isinstance(noise_rule("loadGeneratorFloodHomepage"), MetricNoiseRule)
    # A base fault under the payment signature: no noise gate (U2).
    assert noise_rule("paymentUnreachable") is None
    assert noise_rule("paymentFailure") is None
    for rule in METRIC_NOISE_RULES.values():
        for signal in rule.signals:
            assert signal.expr.count("{window}") == 1


# -- metric gate -----------------------------------------------------------------

RULE = MetricNoiseRule(
    "test-rule", (MetricSignal("s", 'sum(rate(x{a="b"}[{window}]))', 5.0, 1.0),)
)


class Metrics:
    """A metric read: ``base`` before ``on_at``, ``high`` after ``high_after``
    seconds of the fault window."""

    def __init__(
        self,
        clock: Any,
        on_at: datetime,
        *,
        base: float = 1.0,
        high: float = 10.0,
        high_after: float = 0.0,
        complete: bool = True,
    ) -> None:
        self.clock, self.on_at = clock, on_at
        self.base, self.high, self.high_after = base, high, high_after
        self.complete = complete
        self.calls: list[tuple[str, datetime, datetime]] = []

    def metric(
        self, expr: str, start: datetime, end: datetime, timeout: float
    ) -> CallCount:
        self.calls.append((expr, start, end))
        at = end
        if at <= self.on_at:
            return CallCount(self.base, self.complete)
        elapsed = (at - self.on_at).total_seconds()
        value = self.high if elapsed >= self.high_after else self.base
        return CallCount(value, self.complete)


def _deps(tmp_path: Path, telemetry: Any) -> tuple[Any, Any]:
    env = pr_c.make_env(tmp_path)
    return replace(env["deps"], telemetry=telemetry), env["clock"]


def test_metric_gate_compares_equal_windows_before_and_after_the_flag(tmp_path):
    env = pr_c.make_env(tmp_path)
    clock = env["clock"]
    on_at = clock.now()
    probe = Metrics(clock, on_at)
    deps = replace(env["deps"], telemetry=probe)
    seen = metric_gate(RULE, confirmed_at=on_at, deps=deps)
    assert seen.passed and seen.matched == ()
    assert seen.window is not None and seen.window.start == on_at
    seconds = int((seen.window.end - on_at).total_seconds())
    assert seconds >= eval_live_lab.METRIC_MIN_WINDOW_SECONDS
    (baseline, observed) = probe.calls
    span = timedelta(seconds=seconds)
    assert baseline == ('sum(rate(x{a="b"}[{window}]))', on_at - span, on_at)
    assert observed == (baseline[0], on_at, seen.window.end)
    (row,) = seen.raw
    assert (row["baseline"], row["observed"], row["passed"]) == (1.0, 10.0, True)


def test_metric_gate_waits_for_the_signal_then_passes(tmp_path):
    env = pr_c.make_env(tmp_path)
    on_at = env["clock"].now()
    probe = Metrics(env["clock"], on_at, high_after=300)
    deps = replace(env["deps"], telemetry=probe)
    seen = metric_gate(RULE, confirmed_at=on_at, deps=deps)
    assert seen.passed
    assert seen.window is not None
    assert (seen.window.end - on_at).total_seconds() >= 300
    assert [row["passed"] for row in seen.raw][-1] is True
    assert not any(row["passed"] for row in seen.raw[:-1])


@pytest.mark.parametrize(
    "kwargs",
    [
        {"high": 4.9},  # below ratio x baseline
        {"base": 0.0, "high": 0.5},  # below the floor
        {"complete": False},  # an incomplete read never passes
        {"high_after": 10_000},  # never inside the budget
    ],
)
def test_metric_gate_without_the_signal_is_noise_not_observed(tmp_path, kwargs):
    env = pr_c.make_env(tmp_path)
    on_at = env["clock"].now()
    deps = replace(env["deps"], telemetry=Metrics(env["clock"], on_at, **kwargs))
    seen = metric_gate(RULE, confirmed_at=on_at, deps=deps, budget_seconds=600)
    assert not seen.passed and seen.reason == NOISE_NOT_OBSERVED


def test_metric_gate_without_a_metric_read_never_passes(tmp_path):
    env = pr_c.make_env(tmp_path)
    on_at = env["clock"].now()
    deps = replace(env["deps"], telemetry=SimpleNamespace())
    seen = metric_gate(RULE, confirmed_at=on_at, deps=deps)
    assert not seen.passed and seen.reason == "METRIC_PROBE_UNAVAILABLE"


def test_lab_telemetry_metric_reads_one_instant_value(monkeypatch):
    seen: dict[str, Any] = {}

    def get_json(url: str, *, timeout: float, authorization: Any) -> Any:
        seen["url"] = url
        return 200, {"status": "success", "data": {"result": [{"value": [1, "7.5"]}]}}

    monkeypatch.setattr(eval_live_lab.kind_lab, "get_json", get_json)
    monkeypatch.setattr(eval_live_lab.kind_lab, "lab_authorization", lambda: None)
    at = datetime(2026, 11, 4, 12, 0, 0, tzinfo=pr_c.T0.tzinfo)
    start = at - timedelta(seconds=120)
    value = eval_live_lab.LabTelemetry().metric(
        "sum(rate(up[{window}]))", start, at, 1.0
    )
    assert value == CallCount(7.5, True)
    assert "query=sum%28rate%28up%5B120s%5D%29%29" in seen["url"]
    assert f"time={at.timestamp():.3f}" in seen["url"]

    monkeypatch.setattr(eval_live_lab.kind_lab, "get_json", lambda *a, **k: (503, None))
    assert eval_live_lab.LabTelemetry().metric("up", start, at, 1.0) == CallCount(
        None, False
    )


# -- through a v2 round ----------------------------------------------------------


@pytest.fixture
def gc_lab(tmp_path: Path):
    """The dev-v2 noise negative with its noise flag swapped to adManualGc."""
    root = tmp_path / "dev-v2"
    shutil.copytree(DEV_V2, root)
    answer_path = root / "capability" / NOISE_NEG / "answer.json"
    answer = json.loads(answer_path.read_text())
    answer["injectors"] = [
        {"kind": "flagd", "params": {"flag": "adManualGc", "variant": "on"}}
    ]
    answer_path.write_text(json.dumps(answer, indent=2) + "\n")
    env = pr_c.make_env(tmp_path)
    env["lab"].raw = ORIGINAL_V2
    env["deps"] = replace(env["deps"], scenarios_root=root)
    env["root"] = root
    return env


def _round(env: dict[str, Any]) -> Any:
    slots = eval_live.plan_slots(env["root"], [NOISE_NEG], 1)
    manifest = eval_live.build_manifest(
        "r1", slots, 1, ["t"], scenarios_root=env["root"]
    )
    config = eval_live.RoundConfig("r1", slots, "lead", "b1", 55999, manifest, ("t",))
    return eval_live.run_round(config, env["deps"])


def _gc_metric(lab: Any, *, high: float) -> None:
    def metric(expr: str, start: datetime, end: datetime, timeout: float) -> CallCount:
        before = lab.effective("adManualGc", end - timedelta(seconds=1))
        return CallCount(high if before != "off" else 0.002, True)

    lab.metric = metric


def test_metric_noise_negative_without_the_signal_is_a_setup_failure(gc_lab):
    _gc_metric(gc_lab["lab"], high=0.002)
    (slot,) = _round(gc_lab).slots
    assert (slot.status, slot.reason) == ("setup_failed", NOISE_NOT_OBSERVED)
    assert gc_lab["investigator"].requests == []
    assert gc_lab["lab"].raw == ORIGINAL_V2


def test_metric_noise_negative_with_the_signal_submits_and_restores(gc_lab):
    _gc_metric(gc_lab["lab"], high=0.5)
    (slot,) = _round(gc_lab).slots
    assert slot.reason != NOISE_NOT_OBSERVED
    (request,) = gc_lab["investigator"].requests
    assert request.submit_not_after is not None
    ledger = gc_lab["deps"].ledger_root / "r1" / slot.attempt_id
    noise = json.loads((ledger / "pre-observation-noise-adManualGc.json").read_text())
    assert noise["passed"] is True and noise["matched_span_ids"] == []
    assert noise["raw"][-1]["signal_id"] == "ad-jvm-gc-duration"
    assert gc_lab["lab"].raw == ORIGINAL_V2
