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


def test_probe_stops_on_a_foreign_fault_without_touching_it(tmp_path, capsys):
    """r10/section 5: foreign state after the preflight stops the probe round."""
    env, deps = _probe_env(tmp_path)
    foreign = json.dumps({"ledger": "/elsewhere", "flag": "adFailure"})
    calls = {"n": 0}

    def verify():
        calls["n"] += 1
        if calls["n"] == 2:  # first per-scenario identity check, after preflight
            (deps.ledger_root / "fault-active.json").write_text(foreign)
        return ()

    env["identity"].verify = verify
    code = eval_live.main(
        [*ARGV, *_scenarios("eval-cart-failure", "eval-payment-failure-1")], deps=deps
    )
    final = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert (code, final["status"], final["reason"]) == (
        1,
        "stopped",
        "LAB_FAULT_FOREIGN",
    )
    assert final["slots"][1]["status"] == "not_started"
    assert env["lab"].writes == []
    assert (deps.ledger_root / "fault-active.json").read_text() == foreign


def test_recover_builds_lab_only_deps_without_model_credentials(
    tmp_path, capsys, monkeypatch
):
    """r10: a probe's retained fault is recoverable without M0_ENV_FILE."""
    seen = {}

    def real_deps(flagd_url, round_id, *, probe=False):
        seen["probe"] = probe
        raise LabError("STOP_HERE")

    monkeypatch.setattr(eval_live, "real_deps", real_deps)
    monkeypatch.delenv("M0_ENV_FILE", raising=False)
    argv = [
        "--recover", "--round", "r1", "--holder", "lead", "--batch", "b1",
        "--flagd-url", "http://127.0.0.1:18013",
    ]  # fmt: skip
    assert eval_live.main(argv) == 3
    assert seen == {"probe": True}


def test_recover_refuses_without_the_lab_account(tmp_path, capsys):
    env, deps = _probe_env(tmp_path)
    env["lab"].complete = False
    eval_live.main([*ARGV, *_scenarios("eval-cart-failure")], deps=deps)
    capsys.readouterr()
    state = (deps.ledger_root / "fault-active.json").read_bytes()
    env["env_checker"].result = "ENV_UNCONFIRMED"
    result = eval_live.recover_round("r1", "lead", "b1", deps)
    assert (result.exit_code, result.reason) == (3, "ENV_UNCONFIRMED")
    assert (deps.ledger_root / "fault-active.json").read_bytes() == state


# -- r11: evidence write failures are exit 5 with the final JSON ----------------


def test_round_lifecycle_write_failure_is_exit_5_with_final_json(
    tmp_path, capsys, monkeypatch
):
    env = d.make_env(tmp_path)

    def broken(*args, **kwargs):
        raise PermissionError("read-only evidence")

    monkeypatch.setattr(eval_live, "_write_lifecycle", broken)
    argv = [
        "--mode", "live", "--round", "r1", "--scenario", "eval-normal-1", "--n", "1",
        "--parallel", "1", "--holder", "lead", "--batch", "b1",
        "--flagd-url", "http://127.0.0.1:18013", "--pg-port", "55999",
    ]  # fmt: skip
    assert eval_live.main(argv, deps=env["deps"]) == 5
    final = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert (final["exit_code"], final["reason"]) == (5, "EVIDENCE_WRITE_FAILED")


def test_probe_report_write_failure_is_exit_5_with_final_json(tmp_path, capsys):
    env, deps = _probe_env(tmp_path)
    real_verify = env["identity"].verify

    def verify():
        # After the preflight: the round's evidence path becomes a file.
        target = deps.evidence_root / "r1"
        if not target.exists():
            deps.evidence_root.mkdir(parents=True, exist_ok=True)
            target.write_text("not a directory")
        return real_verify()

    calls = {"n": 0}

    def gated():
        calls["n"] += 1
        return real_verify() if calls["n"] == 1 else verify()

    env["identity"].verify = gated
    code = eval_live.main([*ARGV, *_scenarios("eval-normal-1")], deps=deps)
    final = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert (code, final["reason"]) == (5, "EVIDENCE_WRITE_FAILED")


def test_recovery_record_write_failure_and_same_second_names(tmp_path, capsys):
    env, deps = _probe_env(tmp_path)
    env["lab"].complete = False
    eval_live.main([*ARGV, *_scenarios("eval-cart-failure")], deps=deps)
    capsys.readouterr()
    env["lab"].complete = True
    result = eval_live.recover_round("r1", "lead", "b1", deps)
    assert result.exit_code == 0
    # Two records in the same second never overwrite each other.
    evidence = deps.evidence_root / "r1"
    names = ["recovery-X.json", "recovery-X-2.json"]
    assert eval_live.write_public_exclusive(evidence, names, {"n": 1})
    assert eval_live.write_public_exclusive(evidence, names, {"n": 2})
    assert json.loads((evidence / "recovery-X.json").read_text()) == {"n": 1}
    assert json.loads((evidence / "recovery-X-2.json").read_text()) == {"n": 2}
    assert not eval_live.write_public_exclusive(evidence, names, {"n": 3})
    # A second retained fault, then an unwritable evidence path.
    env["lab"].complete = False
    eval_live.main(
        [*ARGV[:4], "r2", *ARGV[5:], *_scenarios("eval-cart-failure")], deps=deps
    )
    capsys.readouterr()
    env["lab"].complete = True
    (deps.evidence_root / "r2").rename(deps.evidence_root / "r2-moved")
    (deps.evidence_root / "r2").write_text("not a directory")
    result = eval_live.recover_round("r2", "lead", "b1", deps)
    assert (result.exit_code, result.reason) == (5, "EVIDENCE_WRITE_FAILED")
    assert not (deps.ledger_root / "fault-active.json").exists()


@pytest.mark.parametrize("first", ["retained_fault", "interrupt"])
def test_probe_exit_precedence_with_a_failed_report(
    tmp_path, capsys, monkeypatch, first
):
    """r13: 4 > 5 > 130 for the probe report too."""
    env, deps = _probe_env(tmp_path)
    if first == "retained_fault":
        env["lab"].complete = False
    else:
        real = env["identity"].verify
        calls = {"n": 0}

        def verify():
            calls["n"] += 1
            if calls["n"] == 2:  # inside the first scenario, after preflight
                from scripts.eval_live_lab import Interrupted

                raise Interrupted()
            return real()

        env["identity"].verify = verify
    monkeypatch.setattr(eval_live, "write_public_exclusive", lambda *a, **k: False)
    code = eval_live.main(
        [*ARGV, *_scenarios("eval-cart-failure", "eval-normal-1")], deps=deps
    )
    final = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == (4 if first == "retained_fault" else 5)
    assert final["exit_code"] == code


# -- PR #271 round-2 bot findings -------------------------------------------------


@pytest.mark.parametrize("step", ["aggregate", "lifecycle"])
def test_an_interrupt_while_writing_round_outputs_still_prints_and_exits_130(
    tmp_path, capsys, monkeypatch, step
):
    from scripts.eval_live_lab import Interrupted

    env = d.make_env(tmp_path)
    real = {
        "aggregate": eval_live._write_round,
        "lifecycle": eval_live._write_lifecycle,
    }
    target = "_write_round" if step == "aggregate" else "_write_lifecycle"

    def interrupted(*args, **kwargs):
        real[step](*args, **kwargs)  # the output is written, then the signal
        raise Interrupted()

    monkeypatch.setattr(eval_live, target, interrupted)
    argv = [
        "--mode", "live", "--round", "r1", "--scenario", "eval-cart-failure", "--n", "1",
        "--parallel", "1", "--holder", "lead", "--batch", "b1",
        "--flagd-url", "http://127.0.0.1:18013", "--pg-port", "55999",
    ]  # fmt: skip
    code = eval_live.main(argv, deps=env["deps"])
    final = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert (code, final["exit_code"], final["status"]) == (130, 130, "interrupted")
    assert (env["deps"].evidence_root / "r1/lifecycle.json").exists()
    assert not (env["deps"].ledger_root / "fault-active.json").exists()


def test_a_second_concurrent_recovery_restores_nothing_and_is_refused(
    tmp_path, capsys, monkeypatch
):
    env, deps = _probe_env(tmp_path)
    env["lab"].complete = False
    eval_live.main([*ARGV, *_scenarios("eval-cart-failure")], deps=deps)
    capsys.readouterr()
    env["lab"].complete = True
    state = deps.ledger_root / "fault-active.json"
    assert state.exists()
    real_check = eval_live.check_occupation

    def first_recovery_finishes_meanwhile(*args, **kwargs):
        # The other recovery restored and cleared the state after our read.
        state.unlink(missing_ok=True)
        return real_check(*args, **kwargs)

    monkeypatch.setattr(
        eval_live, "check_occupation", first_recovery_finishes_meanwhile
    )
    writes_before = len(env["lab"].writes)
    result = eval_live.recover_round("r1", "lead", "b1", deps)
    assert (result.exit_code, result.reason) == (3, "NO_RETAINED_FAULT")
    assert not list((deps.evidence_root / "r1").glob("recovery-*.json"))
    assert len(env["lab"].writes) == writes_before
