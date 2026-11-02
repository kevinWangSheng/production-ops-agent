"""Independent acceptance tests for #432: the offline dev-round summary (seams §5).

The round directories are produced by the public driver (``eval_live.run_round``)
over the PR-C offline lab doubles, exactly as a real round writes them, then
``scripts.eval_dev_round.aggregate`` reads them back.  Offline attempts end
without a verifiable recording, so every started slot is a Q20 failure (D1);
the tests assert counts, refusals and missing-slot handling, not a pass.
"""

from __future__ import annotations

import importlib
import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from opspilot.evaluation import dataset_tree_hash, load_scenarios_v2
from scripts import eval_live, eval_live_lab
from tests.unit import test_eval_c_driver_impl as pr_c

# The #432 module; an ImportError here is confined to this file.
aggregate = importlib.import_module("scripts.eval_dev_round").aggregate

ROOT = Path(__file__).resolve().parents[2]
DEV_V2 = ROOT / "evals" / "datasets" / "dev-v2"
_FLAGS = json.loads(pr_c.ORIGINAL)
_FLAGS["flags"]["adFailure"] = {
    "state": "ENABLED",
    "variants": {"on": True, "off": False},
    "defaultVariant": "off",
}
ORIGINAL_V2 = (json.dumps(_FLAGS, indent=4) + "\n").encode()
PAYMENT = {"kind": "flagd", "params": {"flag": "paymentFailure", "variant": "100%"}}
AD = {"kind": "flagd", "params": {"flag": "adFailure", "variant": "on"}}
SIGNATURE = json.loads((DEV_V2 / "regression/dv2-2b214b/answer.json").read_text())[
    "signature"
]
NOISE_NEGATIVE = "dv2-b0b001"
MODEL_NEGATIVE = "dv2-b0b002"
CAPABILITY = (NOISE_NEGATIVE, MODEL_NEGATIVE)
REGRESSION = tuple(sorted(p.name for p in (DEV_V2 / "regression").iterdir()))


class Provenance:
    def __init__(self, commit: str) -> None:
        self.commit = commit

    def read(self):
        return eval_live_lab.Provenance(commit=self.commit, clean=True)


def _scenario(root: Path, sid: str, injectors, signature, category, polarity):
    directory = root / "capability" / sid
    directory.mkdir(parents=True)
    scenario = json.loads((DEV_V2 / "regression/dv2-2b214b/scenario.json").read_text())
    scenario.update(scenario_id=sid, entry="question")
    answer = json.loads((DEV_V2 / "regression/dv2-2b214b/answer.json").read_text())
    answer.update(
        scenario_id=sid,
        category=category,
        polarity=polarity,
        injectors=injectors,
        signature=signature,
    )
    (directory / "scenario.json").write_text(json.dumps(scenario))
    (directory / "answer.json").write_text(json.dumps(answer))


@pytest.fixture
def lab(tmp_path: Path) -> dict[str, Any]:
    root = tmp_path / "dev-v2"
    shutil.copytree(DEV_V2 / "regression", root / "regression")
    (root / "capability").mkdir()
    _scenario(root, NOISE_NEGATIVE, [AD], None, "noise", "negative")
    _scenario(
        root,
        MODEL_NEGATIVE,
        [PAYMENT, {"kind": "model_transport", "params": {"mode": "first_503"}}],
        SIGNATURE,
        "model_failure",
        "negative",
    )
    load_scenarios_v2(root)
    env = pr_c.make_env(tmp_path)
    env["lab"].raw = ORIGINAL_V2
    env["deps"] = replace(
        env["deps"], scenarios_root=root, provenance=Provenance("a" * 40)
    )
    env["root"] = root
    return env


def run_round(env: dict[str, Any], round_id: str, ids, n: int, commit: str = "a" * 40):
    deps = replace(env["deps"], provenance=Provenance(commit))
    slots = eval_live.plan_slots(env["root"], list(ids), n)
    config = eval_live.RoundConfig(
        round_id, slots, "lead", "b1", 55999, None, ("t",), n
    )
    result = eval_live.run_round(config, deps)
    assert result.status in ("finished", "stopped"), result
    assert all(slot.status != "not_started" for slot in result.slots), result
    return deps.evidence_root / round_id


def refusal(call) -> str:
    with pytest.raises(Exception) as exc:
        call()
    return str(getattr(exc.value, "code", None) or exc.value)


def by_slot(summary: dict[str, Any]) -> dict[tuple[str, int], dict[str, Any]]:
    return {(row["scenario_id"], row["slot"]): row for row in summary["slots"]}


def test_complete_rounds_summarise_every_slot_by_group(lab):
    dirs = [
        run_round(lab, "dv2-r00001", REGRESSION, 2),
        run_round(lab, "dv2-r00002", CAPABILITY, 2),
    ]
    summary = aggregate(dirs, lab["root"])
    assert summary["schema"] == "opspilot-eval-dev-round-1"
    assert summary["dataset"] == f"dev-v2:{dataset_tree_hash(lab['root'])}"
    assert {r["round"] for r in summary["rounds"]} == {"dv2-r00001", "dv2-r00002"}
    for row in summary["rounds"]:
        assert row["binding_class"] is not None
    slots = by_slot(summary)
    assert set(slots) == {(sid, k) for sid in REGRESSION + CAPABILITY for k in (1, 2)}
    for sid in REGRESSION:
        for k in (1, 2):
            row = slots[(sid, k)]
            assert row["group"] == "regression"
            assert row["category"] == "regression"
            assert row["entry"] == "question"
            assert row["upstream"] == "comparable"
            assert row["round"] == "dv2-r00001"
            assert row["attempt_id"]
            assert row["status"] != "not_started"
    assert slots[(NOISE_NEGATIVE, 1)]["category"] == "noise"
    assert slots[(NOISE_NEGATIVE, 1)]["polarity"] == "negative"
    assert slots[(NOISE_NEGATIVE, 1)]["upstream"] == "comparable"
    assert slots[(MODEL_NEGATIVE, 2)]["category"] == "model_failure"
    assert slots[(MODEL_NEGATIVE, 2)]["upstream"] == "same_proxy_injection"
    assert all(
        slots[(sid, k)]["group"] == "capability" for sid in CAPABILITY for k in (1, 2)
    )
    totals = summary["totals"]
    assert totals["regression"]["slots"] == 2 * len(REGRESSION)
    assert totals["regression"]["started"] == 2 * len(REGRESSION)
    assert totals["capability"]["slots"] == 2 * len(CAPABILITY)
    assert totals["capability"]["started"] == 2 * len(CAPABILITY)
    # Offline attempts never produce a verifiable Run, so none passes Q20 and
    # F1 step 2 is not achieved (D1: a started attempt counts).
    assert totals["regression"]["q20_passed"] == 0
    assert totals["capability"]["q20_passed"] == 0
    assert summary["f1_step2"]["achieved"] is False
    for row in summary["slots"]:
        assert row["expectations"]["passed"] is False


def test_missing_slots_are_not_started_and_fail_step_two(lab):
    dirs = [
        run_round(lab, "dv2-r00011", REGRESSION, 2),
        run_round(lab, "dv2-r00012", CAPABILITY, 1),
    ]
    summary = aggregate(dirs, lab["root"])
    slots = by_slot(summary)
    for sid in CAPABILITY:
        assert slots[(sid, 1)]["status"] != "not_started"
        assert slots[(sid, 2)]["status"] == "not_started"
    totals = summary["totals"]
    assert totals["capability"]["slots"] == 2 * len(CAPABILITY)
    assert totals["capability"]["started"] == len(CAPABILITY)
    assert totals["regression"]["started"] == 2 * len(REGRESSION)
    assert summary["f1_step2"]["achieved"] is False
    failed = {
        (row["scenario_id"], row["slot"]) for row in summary["f1_step2"]["failed_slots"]
    }
    assert {(sid, 2) for sid in CAPABILITY} <= failed


def test_a_resumed_round_fills_the_missing_slot(lab):
    dirs = [
        run_round(lab, "dv2-r00021", REGRESSION, 2),
        run_round(lab, "dv2-r00022", CAPABILITY, 1),
        run_round(lab, "dv2-r00023", CAPABILITY, 1),
    ]
    summary = aggregate(dirs, lab["root"])
    slots = by_slot(summary)
    for sid in CAPABILITY:
        assert slots[(sid, 1)]["round"] == "dv2-r00022"
        assert slots[(sid, 2)]["round"] == "dv2-r00023"
    assert summary["totals"]["capability"]["started"] == 2 * len(CAPABILITY)


def test_more_than_two_started_attempts_is_a_redraw(lab):
    dirs = [
        run_round(lab, "dv2-r00031", REGRESSION, 2),
        run_round(lab, "dv2-r00032", CAPABILITY, 2),
        run_round(lab, "dv2-r00033", [NOISE_NEGATIVE], 1),
    ]
    assert "REDRAW_DETECTED" in refusal(lambda: aggregate(dirs, lab["root"]))


def test_rounds_from_different_code_are_manifest_drift(lab):
    dirs = [
        run_round(lab, "dv2-r00041", REGRESSION, 2, commit="a" * 40),
        run_round(lab, "dv2-r00042", CAPABILITY, 2, commit="b" * 40),
    ]
    assert "MANIFEST_DRIFT" in refusal(lambda: aggregate(dirs, lab["root"]))
