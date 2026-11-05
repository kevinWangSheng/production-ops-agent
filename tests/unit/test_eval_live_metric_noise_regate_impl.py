"""Paired regate with a metric noise flag, a finished Run kept after a later
pair failure, and the noise-only deadline after a slow span gate (found in
the F1 closure exam; harness only)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from opspilot.evaluation import load_scenarios_v2
from scripts import eval_live, eval_live_lab
from scripts.eval_live_lab import CallCount, MetricNoiseRule, ObservedSpan
from tests.unit import test_s7_paired_live_impl as s7

lab = s7.lab  # the paired offline lab fixture
FAULT = "dv2-2b214b"  # paymentFailure signature
NOISE_NEG = "dv2-eac1f4"  # noise-only (adFailure, an error-span rule)


def _add_injector(root: Path, sid: str, flag: str) -> None:
    (path,) = root.glob(f"*/{sid}/answer.json")
    answer = json.loads(path.read_text())
    answer["injectors"].append(
        {"kind": "flagd", "params": {"flag": flag, "variant": "on"}}
    )
    path.write_text(json.dumps(answer, indent=2) + "\n")
    load_scenarios_v2(root)


def _gc_metric(fake: Any) -> list[datetime]:
    """adManualGc's GC time is up once the flag is on."""
    calls: list[datetime] = []

    def metric(expr: str, start: datetime, end: datetime, timeout: float) -> CallCount:
        calls.append(end)
        before = fake.effective("adManualGc", end - timedelta(seconds=1))
        return CallCount(0.5 if before != "off" else 0.002, True)

    fake.metric = metric
    return calls


def test_paired_regate_gates_a_metric_noise_flag_through_metric_gate(lab, monkeypatch):
    _add_injector(lab["root"], FAULT, "adManualGc")
    _gc_metric(lab["lab"])
    gated: list[str] = []
    real = eval_live_lab.metric_gate

    def metric_gate(rule: MetricNoiseRule, **kwargs: Any) -> Any:
        gated.append(rule.rule_id)
        return real(rule, **kwargs)

    monkeypatch.setattr(eval_live_lab, "metric_gate", metric_gate)
    baseline = s7.FakeBaseline()
    result, evidence = s7._round(lab, FAULT, "opspilot-first", baseline)
    first, second = s7._pair(evidence, result)["slots"]
    # setup once, regate once for the second system: no AttributeError
    assert len(gated) == 2
    assert second["system"] == "holmes" and second["status"] == "ran"
    assert second["reason"] is None
    assert len(baseline.calls) == 1
    assert result.slots[0].reason != "DRIVER_ERROR"
    ledger = lab["deps"].ledger_root / "s7pair" / result.slots[0].attempt_id
    pair_noise = json.loads(
        (ledger / "pre-observation-pair-noise-adManualGc.json").read_text()
    )
    assert pair_noise["passed"] is True and pair_noise["matched_span_ids"] == []
    assert lab["lab"].raw == s7.dr.ORIGINAL_V2


def test_a_finished_run_is_not_relabeled_when_the_regate_crashes(lab, monkeypatch):
    finished: list[tuple[str, str | None]] = []
    real = eval_live._investigate

    def investigate(*args: Any, **kwargs: Any) -> None:
        real(*args, **kwargs)
        state = args[3]
        # the offline investigator records nothing; take it as a clean Run
        state.status, state.reason = "ran", None
        finished.append((state.status, state.reason))

    monkeypatch.setattr(eval_live, "_investigate", investigate)

    def crash(*args: Any, **kwargs: Any) -> Any:
        raise AttributeError("driver bug")

    monkeypatch.setattr(eval_live, "_regate", crash)
    baseline = s7.FakeBaseline()
    result, evidence = s7._round(lab, FAULT, "opspilot-first", baseline)
    first, second = s7._pair(evidence, result)["slots"]
    (expected,) = finished
    assert (result.slots[0].status, result.slots[0].reason) == expected
    assert (first["status"], first["reason"]) == expected
    assert result.slots[0].product_run_id is not None
    assert (second["status"], second["reason"]) == ("setup_failed", "PAIR:DRIVER_ERROR")
    assert baseline.calls == []
    assert lab["lab"].raw == s7.dr.ORIGINAL_V2


SLOW_SPAN_SECONDS = 400.0


def test_noise_only_deadline_is_taken_after_the_slow_span_gate(lab, monkeypatch):
    # adManualGc (metric) is listed before adFailure (error span, slow)
    (path,) = lab["root"].glob(f"*/{NOISE_NEG}/answer.json")
    answer = json.loads(path.read_text())
    answer["injectors"].insert(
        0, {"kind": "flagd", "params": {"flag": "adManualGc", "variant": "on"}}
    )
    path.write_text(json.dumps(answer, indent=2) + "\n")
    load_scenarios_v2(lab["root"])
    _gc_metric(lab["lab"])
    clock = lab["deps"].clock
    order: list[str] = []
    real = eval_live.noise_gate

    def noise_gate(rule: Any, *, confirmed_at: datetime, deps: Any) -> Any:
        if isinstance(rule, MetricNoiseRule):
            order.append("metric")
            return real(rule, confirmed_at=confirmed_at, deps=deps)
        order.append("span")
        clock.advance(SLOW_SPAN_SECONDS)  # the error span shows up late
        span = ObservedSpan(
            "t" * 32,
            "s" * 16,
            "ad",
            "oteldemo.AdService/GetAds",
            clock.now(),
            {"error": True},
        )
        return eval_live_lab.PreObservation(
            "fault", True, None, confirmed_at, None, (span,), 1.0, 1
        )

    monkeypatch.setattr(eval_live, "noise_gate", noise_gate)
    requested: list[datetime] = []
    lab["investigator"].action = lambda request: requested.append(
        clock.now() - timedelta(seconds=60)
    )
    result, _ = s7._round(lab, NOISE_NEG, "opspilot-only", s7.FakeBaseline())
    assert order == ["span", "metric"]
    (request,) = lab["investigator"].requests
    assert request.submit_not_after is not None
    assert request.submit_not_after > requested[0]
    assert result.slots[0].reason != "PRE_OBSERVATION_STALE"


@pytest.mark.parametrize(
    "flags", [("adManualGc", "adFailure"), ("adFailure", "adManualGc")]
)
def test_metric_last_orders_span_rules_first(flags):
    rules = tuple((f, eval_live_lab.noise_rule(f)) for f in flags)
    ordered = eval_live._metric_last(rules)  # type: ignore[arg-type]
    assert [f for f, _ in ordered] == ["adFailure", "adManualGc"]
