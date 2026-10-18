"""Implementer tests for the A11 lifecycle probe (``--probe``, interface r8).

Offline: the same fake lab as the driver tests. A probe must exercise the lab
lifecycle and must never touch the product Run, model, PG or balance seats.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from scripts import eval_live
from scripts.eval_live_lab import LabError, Unavailable
from tests.unit import test_eval_c_driver_impl as d

ARGV = [
    "--mode", "live", "--probe", "--round", "r1", "--n", "1", "--parallel", "1",
    "--holder", "lead", "--batch", "b1", "--flagd-url", "http://127.0.0.1:18013",
]  # fmt: skip


def _probe_env(tmp_path: Path):
    env = d.make_env(tmp_path)
    deps = replace(
        env["deps"], investigator=Unavailable(), pg=Unavailable(), balance=Unavailable()
    )
    return env, deps


def _scenarios(*ids: str) -> list[str]:
    return [item for sid in ids for item in ("--scenario", sid)]


def test_probe_runs_each_fault_lifecycle_without_run_model_or_pg(tmp_path, capsys):
    env, deps = _probe_env(tmp_path)
    faults = [
        "eval-cart-failure",
        "eval-payment-failure-1",
        "eval-product-catalog-failure",
    ]
    code = eval_live.main([*ARGV, *_scenarios(*faults)], deps=deps)
    final = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert (code, final["status"]) == (0, "finished")
    assert [s["status"] for s in final["slots"]] == ["ran"] * 3
    lab = env["lab"]
    assert lab.raw == d.ORIGINAL and len(lab.writes) == 6  # inject+restore each
    assert all(isinstance(fence, str) for fence in lab.fences)
    assert not (deps.ledger_root / "fault-active.json").exists()
    assert not list(deps.evidence_root.rglob("attempt.json"))
    assert not list(deps.evidence_root.rglob("summary.json"))
    probe = json.loads((deps.evidence_root / "r1/probe.json").read_text())
    assert probe["schema"] == "opspilot-eval-live-probe-1"
    assert [s["flag"] for s in probe["scenarios"]] == [
        "cartFailure",
        "paymentFailure",
        "productCatalogFailure",
    ]
    for item in probe["scenarios"]:
        assert item["gate"]["passed"] and item["gate"]["matched_span_ids"]
        assert item["fault"]["restored"] and item["fault"]["washed"]
    for sid in faults:
        assert (deps.ledger_root / "r1" / f"probe-{sid}" / "lifecycle.jsonl").exists()


def test_probe_normal_scenario_writes_no_configmap(tmp_path, capsys):
    env, deps = _probe_env(tmp_path)
    assert eval_live.main([*ARGV, *_scenarios("eval-normal-1")], deps=deps) == 0
    assert env["lab"].writes == []


def test_probe_restore_failure_retains_the_fault_and_recover_clears_it(
    tmp_path, capsys
):
    env, deps = _probe_env(tmp_path)
    env["lab"].complete = False  # gate and wash cannot pass
    code = eval_live.main(
        [*ARGV, *_scenarios("eval-cart-failure", "eval-payment-failure-1")], deps=deps
    )
    final = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 4
    assert final["slots"][1]["status"] == "not_started"
    assert (deps.ledger_root / "fault-active.json").exists()
    env["lab"].complete = True
    recovered = eval_live.recover_round("r1", "lead", "b1", deps)
    assert recovered.exit_code == 0
    assert not (deps.ledger_root / "fault-active.json").exists()


def test_probe_refusal_touches_nothing(tmp_path, capsys):
    env, deps = _probe_env(tmp_path)
    (deps.ledger_root / "occupation.json").unlink()
    assert eval_live.main([*ARGV, *_scenarios("eval-cart-failure")], deps=deps) == 3
    assert env["lab"].writes == [] and not deps.evidence_root.exists()


def test_probe_requires_n_one_and_no_pg_port(tmp_path, capsys):
    _env, deps = _probe_env(tmp_path)
    argv = [*ARGV, *_scenarios("eval-cart-failure")]
    argv[argv.index("--n") + 1] = "2"
    assert eval_live.main(argv, deps=deps) == 2


def test_unavailable_seat_refuses_use():
    with pytest.raises(LabError, match="PROBE_SEAM_UNAVAILABLE"):
        Unavailable().investigate(None)
