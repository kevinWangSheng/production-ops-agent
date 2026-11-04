"""S7c (F1 closure R7/R8): paired schedule, preregistered rules, dataset name."""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from opspilot.evaluation import ScenarioError, dataset_tree_hash
from scripts import eval_schedule as es
from scripts.eval_dev_round import aggregate
from scripts.eval_live import dataset_dimension

ROOT = Path(__file__).resolve().parents[2]
DEV_V2 = ROOT / "evals/datasets/dev-v2"
T0 = datetime(2026, 11, 5, tzinfo=UTC)


@pytest.fixture(scope="module")
def rules() -> es.Rules:
    return es.load_rules()


@pytest.fixture(scope="module")
def scenarios() -> tuple[es.Scenario, ...]:
    return es.scenarios_from_dataset(DEV_V2)


# -- rules ----------------------------------------------------------------------


def test_registered_rules_load(rules):
    assert rules.repetitions == 3
    assert rules.same_scenario_gap == timedelta(minutes=60)
    assert rules.wash_min == timedelta(seconds=600)
    assert rules.max_reruns == 1
    assert rules.void_classes == es.VOID_CLASSES
    assert rules.sha256 is not None and len(rules.sha256) == 64


@pytest.mark.parametrize(
    ("path", "value", "code"),
    [
        (("pairing", "order"), "random", "RULES_UNSUPPORTED"),
        (("spacing", "same_scenario_gap_scope"), "global", "RULES_UNSUPPORTED"),
        (("void_rerun", "rerun_unit"), "slot", "RULES_UNSUPPORTED"),
        (("schema",), "other", "RULES_SCHEMA"),
        (("repetitions",), 0, "RULES_INVALID"),
    ],
)
def test_rules_the_code_does_not_implement_are_refused(tmp_path, path, value, code):
    data = json.loads(es.RULES_PATH.read_text())
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    changed = tmp_path / "rules.json"
    changed.write_text(json.dumps(data))
    with pytest.raises(es.ScheduleError) as exc:
        es.load_rules(changed)
    assert exc.value.code == code


# -- windows ----------------------------------------------------------------------


def test_every_scenario_gets_k_windows_and_alternating_order(rules, scenarios):
    windows = es.windows(scenarios, rules)
    assert len(windows) == 3 * len(scenarios)
    for i, scenario in enumerate(scenarios):
        own = [w for w in windows if w.scenario_id == scenario.scenario_id]
        assert [w.repetition for w in own] == [1, 2, 3]
        if scenario.entry == "alertmanager":
            assert all(w.systems == ("opspilot",) for w in own)
            continue
        firsts = [w.systems[0] for w in own]
        assert firsts[0] != firsts[1] and firsts[1] != firsts[2]
        assert sorted(own[0].systems) == ["holmes", "opspilot"]
    paired = [w for w in windows if len(w.systems) == 2]
    opspilot_first = sum(w.systems[0] == "opspilot" for w in paired)
    assert abs(opspilot_first - (len(paired) - opspilot_first)) <= len(paired) // 6 + 1


# -- planning and checking ----------------------------------------------------------


def _log(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {"schema": es.LOG_SCHEMA, "windows": records}


@pytest.mark.parametrize("labs", [("l0",), ("l0", "l1"), ("l0", "l1", "l2", "l3")])
def test_plan_meets_every_spacing_rule(rules, scenarios, labs):
    plan = es.plan(
        scenarios, labs, rules, window_length=timedelta(minutes=27), start=T0
    )
    assert len(plan["simulated"]) == len(plan["windows"])
    records = es.simulate(
        es.windows(scenarios, rules),
        labs,
        rules,
        window_length=timedelta(minutes=27),
        start=T0,
    )
    result = es.check(plan, _log(records), rules)
    assert result["violations"] == []
    assert all(counts["opspilot"] == 3 for counts in result["started_slots"].values())
    used = {r["lab"] for r in records}
    assert used == set(labs)


def test_more_labs_shorten_the_plan(rules, scenarios):
    hours = [
        es.plan(
            scenarios,
            tuple(f"l{i}" for i in range(n)),
            rules,
            window_length=timedelta(minutes=27),
            start=T0,
        )["estimated_hours"]
        for n in (1, 2, 3)
    ]
    assert hours[0] > hours[1] > hours[2]
    # 54 windows of 27 min plus the 10 min wash after each, on one lab
    assert hours[0] == pytest.approx(54 * 37 / 60 - 10 / 60, abs=0.5)


def test_one_lab_waits_out_the_same_scenario_gap(rules):
    lone = (es.Scenario("dv2-aaaaaa", "question"),)
    records = es.simulate(
        es.windows(lone, rules),
        ("l0",),
        rules,
        window_length=timedelta(minutes=5),
        start=T0,
    )
    injected = [es._moment(r["injected_at"]) for r in records]
    restored = [es._moment(r["restored_at"]) for r in records]
    for k in (1, 2):
        assert injected[k] - restored[k - 1] >= timedelta(minutes=60)
    assert records[1]["predecessor"]["scenario_id"] == "dv2-aaaaaa"


def _record(wid, sid, lab, start, minutes=20, order=("opspilot", "holmes"), **extra):
    return {
        "window_id": wid,
        "scenario_id": sid,
        "lab": lab,
        "started_at": start.isoformat(),
        "injected_at": start.isoformat(),
        "restored_at": (start + timedelta(minutes=minutes)).isoformat(),
        "ended_at": (start + timedelta(minutes=minutes)).isoformat(),
        "slots": [
            {"system": s, "position": n + 1, "status": "ran"}
            for n, s in enumerate(order)
        ],
        "void": None,
        **extra,
    }


def _plan_json(*windows):
    return {
        "windows": [
            {"window_id": w, "scenario_id": s, "systems": list(o), "repetition": 1}
            for w, s, o in windows
        ]
    }


def test_check_reports_each_broken_rule(rules):
    both = ("opspilot", "holmes")
    plan = _plan_json(
        ("a-r1", "a", both),
        ("a-r2", "a", both),
        ("b-r1", "b", both),
        ("c-r1", "c", both),
        ("d-r1", "d", both),
    )
    records = [
        _record("a-r1", "a", "l0", T0),
        # wash 5 min and same scenario 5 min after the restore
        _record("a-r2", "a", "l0", T0 + timedelta(minutes=25)),
        # overlaps a-r2 on l0
        _record("b-r1", "b", "l0", T0 + timedelta(minutes=30)),
        # order swapped
        _record("c-r1", "c", "l1", T0, order=("holmes", "opspilot")),
        # a rerun of a window that was not voided
        _record(
            "c-r1-rerun",
            "c",
            "l1",
            T0 + timedelta(hours=2),
            order=("holmes", "opspilot"),
        ),
        _record("zz", "z", "l2", T0),
    ]
    found = es.check(plan, _log(records), rules)["violations"]
    assert "WASH_SHORT:a-r2" in found
    assert "GAP_SHORT:a-r2" in found
    assert "OVERLAP:l0" in found
    assert "ORDER_MISMATCH:c-r1" in found
    assert "RERUN_UNJUSTIFIED:c-r1-rerun" in found
    assert "UNPLANNED:zz" in found
    assert "MISSING:d-r1" in found


def test_check_counts_the_rerun_instead_of_the_voided_window(rules):
    both = ("opspilot", "holmes")
    plan = _plan_json(("a-r1", "a", both))
    voided = _record("a-r1", "a", "l0", T0, void="ENV_SETUP_FAILED")
    voided["slots"][1]["status"] = "setup_failed"
    rerun = _record("a-r1-rerun", "a", "l0", T0 + timedelta(minutes=80))
    result = es.check(plan, _log([voided, rerun]), rules)
    assert result["violations"] == []
    assert result["voided"] == [
        {"window_id": "a-r1", "void": "ENV_SETUP_FAILED", "rerun": "a-r1-rerun"}
    ]
    assert result["started_slots"]["a"] == {"opspilot": 1, "holmes": 1}


# -- void classes ------------------------------------------------------------------


def _slots(*items: dict[str, Any]) -> dict[str, Any]:
    return {"slots": list(items)}


@pytest.mark.parametrize(
    ("record", "code"),
    [
        (_slots({"status": "ran"}, {"status": "ran"}), None),
        (
            _slots({"status": "ran"}, {"status": "setup_failed", "run_created": False}),
            "ENV_SETUP_FAILED",
        ),
        # a Run existed: the setup failure is the system's own
        (_slots({"status": "setup_failed", "run_created": True}), None),
        (_slots({"status": "aborted"}), None),
        (_slots({"status": "ran", "provider_error": True}), "PROVIDER_5XX_STORM"),
        (
            {**_slots({"status": "ran", "provider_error": True}), "model_fault": True},
            None,
        ),
        (_slots({"status": "ran", "host_noise": {"steal_percent": 9.0}}), None),
        (
            {
                **_slots({"status": "ran", "host_noise": {"steal_percent": 9.0}}),
                "shared_host": True,
            },
            "HOST_NOISE",
        ),
        (
            {
                **_slots({"status": "ran", "host_noise": {"baseline_p95_ratio": 2.5}}),
                "shared_host": True,
            },
            "HOST_NOISE",
        ),
        ({**_slots({"status": "ran"}), "interrupted": True}, "HARNESS_INTERRUPTED"),
        (_slots({"status": "ran"}, {"status": "not_started"}), "HARNESS_INTERRUPTED"),
    ],
)
def test_void_classes(rules, record, code):
    assert es.classify_void(record, rules) == code


def test_a_voided_window_is_rerun_once(rules):
    window = es.Window("a-r1", "a", 1, ("opspilot", "holmes"))
    dispatcher = es.Dispatcher([window], ["l0"], rules)
    failed = _slots({"system": "opspilot", "position": 1, "status": "setup_failed"})
    taken, _ = dispatcher.take("l0", T0)
    assert taken == window
    assert (
        dispatcher.finish("l0", window, {**failed, "restored_at": T0.isoformat()})
        == "ENV_SETUP_FAILED"
    )
    assert [w.window_id for w in dispatcher.pending] == ["a-r1-rerun"]
    # the wash and the same-scenario gap still hold for the rerun
    assert dispatcher.take("l0", T0 + timedelta(minutes=5)) == (
        None,
        T0 + timedelta(minutes=60),
    )
    rerun, _ = dispatcher.take("l0", T0 + timedelta(minutes=60))
    assert rerun is not None and rerun.attempt == 1
    dispatcher.finish("l0", rerun, {**failed, "restored_at": T0.isoformat()})
    assert dispatcher.pending == [] and dispatcher.done()
    assert dispatcher.records[-1]["void"] == "ENV_SETUP_FAILED"
    assert "rerun_window_id" not in dispatcher.records[-1]


def test_steal_percent():
    before = "cpu  100 0 100 700 0 0 0 100 0 0\ncpu0 1 2 3\n"
    after = "cpu  200 0 200 1400 0 0 0 200 0 0\n"
    assert es.steal_percent(before, after) == pytest.approx(10.0)
    assert es.steal_percent("garbage", after) is None
    assert es.steal_percent(after, before) is None


# -- runner ------------------------------------------------------------------------


class FakeRunner:
    def __init__(self, fail_once: str | None = None) -> None:
        self.active: dict[str, int] = {}
        self.max_active: dict[str, int] = {}
        self.fail_once = fail_once
        self.calls: list[tuple[str, str]] = []

    def run(self, lab: str, window: es.Window) -> dict[str, Any]:
        self.calls.append((lab, window.window_id))
        self.active[lab] = self.active.get(lab, 0) + 1
        self.max_active[lab] = max(self.max_active.get(lab, 0), self.active[lab])
        start = datetime.now(UTC)
        time.sleep(0.02)
        end = datetime.now(UTC)
        self.active[lab] -= 1
        status = "ran"
        if window.window_id == self.fail_once:
            status = "setup_failed"
        return {
            "started_at": start.isoformat(),
            "injected_at": start.isoformat(),
            "restored_at": end.isoformat(),
            "ended_at": end.isoformat(),
            "slots": [
                {"system": s, "position": n + 1, "status": status, "run_created": False}
                for n, s in enumerate(window.systems)
            ],
        }


def test_labs_run_in_parallel_one_window_each(rules, scenarios):
    fast = replace(
        rules,
        same_scenario_gap=timedelta(seconds=0.15),
        wash_min=timedelta(seconds=0.01),
    )
    picked = scenarios[:6]
    windows = es.windows(picked, fast)
    runner = FakeRunner(fail_once=windows[0].window_id)
    seen: list[dict[str, Any]] = []
    records = es.run_schedule(
        windows,
        ("l0", "l1", "l2"),
        fast,
        runner,
        on_record=seen.append,
        poll_seconds=0.01,
    )
    assert len(records) == len(windows) + 1 == len(seen)
    assert set(runner.max_active) == {"l0", "l1", "l2"}
    assert all(n == 1 for n in runner.max_active.values())
    plan = {"windows": [w.as_json() for w in windows]}
    result = es.check(plan, _log(records), fast)
    assert result["violations"] == []
    assert [v["window_id"] for v in result["voided"]] == [windows[0].window_id]


def test_runner_exception_is_booked_and_stops_that_lab(rules):
    class Broken:
        def run(self, lab, window):
            raise RuntimeError("ssh lost")

    window = es.Window("a-r1", "a", 1, ("opspilot", "holmes"))
    records = es.run_schedule([window], ("l0",), rules, Broken(), poll_seconds=0.01)
    assert records[0]["interrupted"] is True
    assert records[0]["void"] == "HARNESS_INTERRUPTED"


# -- R8: dataset dimension name ------------------------------------------------------


def test_dataset_dimension_names_the_dataset(tmp_path):
    holdout = tmp_path / "holdout-7f3a"
    shutil.copytree(DEV_V2, holdout)
    value = dataset_dimension(holdout).value
    assert value == f"holdout-7f3a:{dataset_tree_hash(holdout)}"
    assert dataset_dimension(DEV_V2).value.startswith("dev-v2:")
    bad = tmp_path / "Holdout Set"
    shutil.copytree(DEV_V2, bad)
    with pytest.raises(ScenarioError, match="DATASET_NAME_INVALID"):
        dataset_dimension(bad)


def test_dev_round_summary_uses_the_dataset_name(tmp_path):
    holdout = tmp_path / "holdout-7f3a"
    shutil.copytree(DEV_V2, holdout)
    summary = aggregate([], holdout)
    assert summary["dataset"] == f"holdout-7f3a:{dataset_tree_hash(holdout)}"


def test_plan_cli_writes_a_plan(tmp_path):
    labs = tmp_path / "labs.json"
    labs.write_text(
        json.dumps({"schema": es.LABS_SCHEMA, "labs": [{"name": "a"}, {"name": "b"}]})
    )
    out = tmp_path / "plan.json"
    assert (
        es.main(
            ["plan", "--dataset", str(DEV_V2), "--labs", str(labs), "--out", str(out)]
        )
        == 0
    )
    plan = json.loads(out.read_text())
    assert plan["schema"] == es.PLAN_SCHEMA
    assert plan["dataset"].startswith("dev-v2:")
    assert plan["rules_sha256"] == es.load_rules().sha256
    assert plan["labs"] == ["a", "b"]
