"""Paired regate of a metric noise flag after a long first Run (F1 closure
exam, second leg: the HolmesGPT slot failed PAIR_REGATE:NOISE_NOT_OBSERVED).

The first gate compares the noise window with the equally long window before
the flag became effective. The regate reads the noise in the last
``RECONFIRM_SECONDS``; its baseline must still end at the flag's effective
time, not at the start of that trailing window (which lies inside the noise
once the first Run took longer than ``RECONFIRM_SECONDS``). Harness only."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from scripts import eval_live_lab
from scripts.eval_live_lab import CallCount
from tests.unit import test_s7_paired_live_impl as s7
from tests.unit.test_eval_live_metric_noise_regate_impl import FAULT, _add_injector

lab = s7.lab  # the paired offline lab fixture
FIRST_RUN_SECONDS = 600.0  # longer than RECONFIRM_SECONDS + the gate's span
GC_ON, GC_OFF = 0.05, 0.0


def _gc_rate(fake: Any) -> list[tuple[datetime, datetime]]:
    """adManualGc's GC time as a rate over ``[start, end]``: the share of the
    window the flag was on, sampled per second (like PromQL ``rate``)."""
    reads: list[tuple[datetime, datetime]] = []

    def metric(expr: str, start: datetime, end: datetime, timeout: float) -> CallCount:
        reads.append((start, end))
        seconds = max(int((end - start).total_seconds()), 1)
        on = sum(
            fake.effective("adManualGc", start + timedelta(seconds=i)) != "off"
            for i in range(seconds)
        )
        return CallCount(GC_OFF + (GC_ON - GC_OFF) * on / seconds, True)

    fake.metric = metric
    return reads


def test_regate_after_a_long_first_run_compares_with_the_pre_injection_baseline(
    lab,
):
    _add_injector(lab["root"], FAULT, "adManualGc")
    _gc_rate(lab["lab"])
    clock = lab["deps"].clock
    lab["investigator"].action = lambda request: clock.advance(FIRST_RUN_SECONDS)
    baseline = s7.FakeBaseline()
    result, evidence = s7._round(lab, FAULT, "opspilot-first", baseline)
    first, second = s7._pair(evidence, result)["slots"]
    ledger = lab["deps"].ledger_root / "s7pair" / result.slots[0].attempt_id
    pair_noise = json.loads(
        (ledger / "pre-observation-pair-noise-adManualGc.json").read_text()
    )
    rows = [row for row in pair_noise["raw"] if "baseline" in row]
    assert pair_noise["passed"] is True, rows[:2]
    # the baseline is the flag-off window: the noise is still up, not merely
    # as high as a window that was already noisy
    assert rows[-1]["baseline"] == GC_OFF
    assert rows[-1]["observed"] >= rows[-1]["floor"]
    assert len(rows) == 1  # first poll, no 600 s wait
    assert second["system"] == "holmes" and second["status"] == "ran"
    assert second["reason"] is None
    assert len(baseline.calls) == 1
    assert lab["lab"].raw == s7.dr.ORIGINAL_V2


def _windows(reads: list[tuple[datetime, datetime]]) -> tuple[Any, Any]:
    (b_start, b_end), (o_start, o_end) = reads[-2], reads[-1]
    assert b_end - b_start == o_end - o_start  # equally long windows
    return b_end, o_start


def test_metric_gate_baseline_end_defaults_to_confirmed_at(lab):
    reads = _gc_rate(lab["lab"])
    deps = lab["deps"]
    now = deps.clock.now()
    rule = eval_live_lab.METRIC_NOISE_RULES["adManualGc"]
    since = now - timedelta(seconds=240)
    earlier = now - timedelta(seconds=900)
    eval_live_lab.metric_gate(rule, confirmed_at=since, deps=deps, budget_seconds=1)
    assert _windows(reads) == (since, since)
    eval_live_lab.metric_gate(
        rule, confirmed_at=since, deps=deps, budget_seconds=1, baseline_end=earlier
    )
    assert _windows(reads) == (earlier, since)
