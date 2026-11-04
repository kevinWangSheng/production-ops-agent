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
from scripts import eval_pair_schedule as es
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
        (("void_rerun", "classes", 3, "action"), "rerun_pair", "RULES_UNSUPPORTED"),
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


def test_check_counts_the_rerun_instead_of_the_voided_slot(rules):
    both = ("opspilot", "holmes")
    plan = _plan_json(("a-r1", "a", both), ("b-r1", "b", both))
    voided = _record(
        "a-r1",
        "a",
        "l0",
        T0,
        void="ENV_SETUP_FAILED",
        decision={"action": "rerun_slot", "systems": ["holmes"], "note": "x"},
    )
    voided["slots"][1]["status"] = "setup_failed"
    rerun = _record(
        "a-r1-rerun", "a", "l0", T0 + timedelta(minutes=80), order=("holmes",)
    )
    noisy = _record(
        "b-r1",
        "b",
        "l1",
        T0,
        void="HOST_NOISE",
        decision={"action": "rerun_pair", "systems": list(both), "note": "y"},
    )
    pair_rerun = _record("b-r1-rerun", "b", "l1", T0 + timedelta(minutes=80))
    result = es.check(plan, _log([voided, rerun, noisy, pair_rerun]), rules)
    assert result["violations"] == []
    assert result["voided"] == [
        {
            "window_id": "a-r1",
            "void": "ENV_SETUP_FAILED",
            "rerun": "a-r1-rerun",
            "systems": ["holmes"],
        },
        {
            "window_id": "b-r1",
            "void": "HOST_NOISE",
            "rerun": "b-r1-rerun",
            "systems": ["holmes", "opspilot"],
        },
    ]
    assert result["started_slots"]["a"] == {"opspilot": 1, "holmes": 1}
    # a slot rerun that also repeats the healthy system breaks the plan
    bad = _record("a-r1-rerun", "a", "l0", T0 + timedelta(minutes=80))
    assert (
        "ORDER_MISMATCH:a-r1-rerun"
        in es.check(plan, _log([voided, bad]), rules)["violations"]
    )


# -- void classes ------------------------------------------------------------------


def _slots(*items: dict[str, Any]) -> dict[str, Any]:
    return {"slots": list(items)}


@pytest.mark.parametrize(
    ("record", "code"),
    [
        (_slots({"status": "ran"}, {"status": "ran"}), None),
        (
            _slots(
                {"status": "ran"},
                {"status": "setup_failed", "product_run_created": False},
            ),
            "ENV_SETUP_FAILED",
        ),
        # a Run existed: the setup failure is the system's own
        (_slots({"status": "setup_failed", "product_run_created": True}), None),
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


def test_a_voided_slot_is_rerun_once_alone(rules):
    window = es.Window("a-r1", "a", 1, ("opspilot", "holmes"))
    dispatcher = es.Dispatcher([window], ["l0"], rules)
    taken, _ = dispatcher.take("l0", T0)
    assert taken == window
    record = {
        "slots": [
            {"system": "opspilot", "position": 1, "status": "ran"},
            {"system": "holmes", "position": 2, "status": "setup_failed"},
        ],
        "restored_at": T0.isoformat(),
    }
    assert dispatcher.finish("l0", window, record) == "ENV_SETUP_FAILED"
    (rerun_window,) = dispatcher.pending
    assert rerun_window.window_id == "a-r1-rerun"
    assert rerun_window.systems == ("holmes",)
    assert rerun_window.rerun_of == "a-r1"
    # the wash and the same-scenario gap still hold for the rerun
    assert dispatcher.take("l0", T0 + timedelta(minutes=5)) == (
        None,
        T0 + timedelta(minutes=60),
    )
    rerun, _ = dispatcher.take("l0", T0 + timedelta(minutes=60))
    assert rerun is not None and rerun.attempt == 1
    failed = {
        "slots": [{"system": "holmes", "position": 1, "status": "setup_failed"}],
        "restored_at": T0.isoformat(),
    }
    assert dispatcher.finish("l0", rerun, failed) is None  # never rerun again
    assert dispatcher.pending == [] and dispatcher.done()
    assert dispatcher.records[-1]["decision"]["action"] == "keep"


def test_host_noise_reruns_the_whole_window(rules):
    window = es.Window("a-r1", "a", 1, ("holmes", "opspilot"))
    dispatcher = es.Dispatcher([window], ["l0"], rules)
    dispatcher.take("l0", T0)
    record = {
        "shared_host": True,
        "slots": [
            {
                "system": "holmes",
                "position": 1,
                "status": "ran",
                "host_noise": {"steal_percent": 7.0},
            },
            {"system": "opspilot", "position": 2, "status": "ran"},
        ],
        "restored_at": T0.isoformat(),
    }
    assert dispatcher.finish("l0", window, record) == "HOST_NOISE"
    assert dispatcher.pending[0].systems == ("holmes", "opspilot")


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
                {
                    "system": s,
                    "position": n + 1,
                    "status": status,
                    "product_run_created": False,
                }
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


# -- live window runner ----------------------------------------------------------------


def _labs_file(tmp_path, labs):
    path = tmp_path / "labs.json"
    path.write_text(json.dumps({"schema": es.LABS_SCHEMA, "labs": labs}))
    return path


def _lab(name, instance, port, **extra):
    return {
        "name": name,
        "host": "opspilot-f9-1",
        "repo": "/root/poa",
        "pg_port": port,
        "flagd_url": f"http://127.0.0.1:{18080 + 100 * instance}/flagservice/",
        "output_root": f"/root/out/{name}",
        "env": {
            "OPSPILOT_LAB_INSTANCE": str(instance),
            "OPSPILOT_LAB_LEDGERS": f"/root/ledgers/{name}",
        },
        **extra,
    }


def test_lab_specs_refuse_secrets_and_shared_resources(tmp_path):
    good = es.load_lab_specs(
        _labs_file(tmp_path, [_lab("a", 0, 55001), _lab("b", 1, 55002)])
    )
    assert set(good) == {"a", "b"}
    leaky = _lab("a", 0, 55001)
    leaky["env"]["DEEPSEEK_API_KEY"] = "x"
    with pytest.raises(es.ScheduleError, match="LABS_SECRET_IN_ENV"):
        es.load_lab_specs(_labs_file(tmp_path, [leaky]))
    with pytest.raises(es.ScheduleError, match="LABS_SHARED_RESOURCE"):
        es.load_lab_specs(
            _labs_file(tmp_path, [_lab("a", 0, 55001), _lab("b", 0, 55002)])
        )
    with pytest.raises(es.ScheduleError, match="LABS_SHARED_RESOURCE"):
        es.load_lab_specs(
            _labs_file(tmp_path, [_lab("a", 0, 55001), _lab("b", 1, 55001)])
        )


def test_command_runner_drives_one_round_per_window(tmp_path):
    specs = es.load_lab_specs(_labs_file(tmp_path, [_lab("b", 1, 55002)]))
    window = es.Window("dv2-2b214b-r2", "dv2-2b214b", 2, ("holmes", "opspilot"))
    evidence = "/root/out/b/evidence/s10-dv2-2b214b-r2"
    lifecycle = {
        "slots": [
            {
                "attempt_id": "att1",
                "timings": {
                    "started_at": "2026-11-05T00:00:00+00:00",
                    "ended_at": "2026-11-05T00:30:00+00:00",
                },
                "fault": {
                    "effective_at": "2026-11-05T00:01:00+00:00",
                    "normal_confirmed_at": "2026-11-05T00:20:00+00:00",
                },
            }
        ]
    }
    pair = {
        "slots": [
            {"system": "holmes", "position": 1, "status": "ran"},
            {"system": "opspilot", "position": 2, "status": "ran"},
        ]
    }
    files = {
        f"{evidence}/lifecycle.json": lifecycle,
        f"{evidence}/runs/att1/pair.json": pair,
    }
    calls = []

    def execute(spec, argv):
        calls.append((spec.name, list(argv)))
        if argv[0] == "cat":
            found = files.get(argv[1])
            return (0, json.dumps(found)) if found is not None else (1, "")
        return 0, "{}"

    runner = es.CommandWindowRunner(
        specs,
        {"dv2-2b214b": "evals/datasets/dev-v2", "dv2-30921c": "evals/datasets/dev-v2"},
        round_prefix="s10",
        holder="lead",
        batch="b1",
        execute=execute,
    )
    record = runner.run("b", window)
    argv = calls[0][1]
    assert argv[argv.index("--pair") + 1] == "holmes-first"
    assert argv[argv.index("--round") + 1] == "s10-dv2-2b214b-r2"
    assert argv[argv.index("--pg-port") + 1] == "55002"
    assert argv[argv.index("--output-root") + 1] == "/root/out/b"
    assert record["injected_at"] == "2026-11-05T00:01:00+00:00"
    assert record["restored_at"] == "2026-11-05T00:20:00+00:00"
    assert [s["system"] for s in record["slots"]] == ["holmes", "opspilot"]
    assert record["interrupted"] is False
    shell = es._shell(specs["b"], argv)
    assert shell[:3] == ["ssh", "-o", "BatchMode=yes"]
    assert "OPSPILOT_LAB_INSTANCE=1" in shell[-1] and shell[-1].startswith(
        "cd /root/poa && env "
    )
    alone = es.Window("dv2-30921c-r1", "dv2-30921c", 1, ("opspilot",))
    assert "opspilot-only" in runner.argv("b", alone)


def test_command_runner_books_a_round_without_evidence_as_interrupted(tmp_path):
    specs = es.load_lab_specs(_labs_file(tmp_path, [_lab("a", 0, 55001)]))
    runner = es.CommandWindowRunner(
        specs,
        {"x": "r"},
        round_prefix="p",
        holder="h",
        batch="b",
        execute=lambda spec, argv: (2, ""),
    )
    record = runner.run("a", es.Window("x-r1", "x", 1, ("opspilot", "holmes")))
    assert record["interrupted"] is True
    assert es.classify_void(record, es.load_rules()) == "HARNESS_INTERRUPTED"


# -- round 1 review fixes ------------------------------------------------------------


def test_plan_then_run_end_to_end(tmp_path):
    labs = _labs_file(tmp_path, [_lab("a", 0, 55001), _lab("b", 1, 55002)])
    out = tmp_path / "plan.json"
    assert (
        es.main(
            ["plan", "--dataset", str(DEV_V2), "--labs", str(labs), "--out", str(out)]
        )
        == 0
    )
    plan = json.loads(out.read_text())
    assert all(v["root"] == str(DEV_V2) for v in plan["scenarios"].values())
    rules = es.load_rules()
    fast = replace(
        rules,
        same_scenario_gap=timedelta(seconds=0.05),
        wash_min=timedelta(seconds=0.01),
    )
    seen_roots: set[str] = set()

    class Runner(FakeRunner):
        def run(self, lab, window):
            record = super().run(lab, window)
            record["injected"] = True
            return record

    real = es.CommandWindowRunner(
        es.load_lab_specs(labs),
        {sid: v["root"] for sid, v in plan["scenarios"].items()},
        round_prefix="e2e",
        holder="h",
        batch="b",
        execute=lambda spec, argv: (0, ""),
    )
    for window in es.windows(es.scenarios_from_dataset(DEV_V2), rules)[:3]:
        argv = real.argv("a", window)
        seen_roots.add(argv[argv.index("--scenario-root") + 1])
    assert seen_roots == {str(DEV_V2)}
    result = es.execute_plan(
        {**plan, "rules_sha256": fast.sha256},
        es.load_lab_specs(labs),
        fast,
        tmp_path / "log.json",
        round_prefix="e2e",
        holder="h",
        batch="b",
        runner=Runner(),
        poll_seconds=0.01,
    )
    assert result["violations"] == []
    assert all(
        v == {"opspilot": 0, "holmes": 0} for v in result["missing_slots"].values()
    )
    log = json.loads((tmp_path / "log.json").read_text())
    assert len(log["windows"]) == len(plan["windows"])


def test_plan_without_roots_is_refused(tmp_path):
    rules = es.load_rules()
    plan = {
        "rules_sha256": rules.sha256,
        "scenarios": {"a": {"entry": "question", "model_fault": False}},
        "windows": [],
    }
    with pytest.raises(es.ScheduleError, match="PLAN_ROOT_MISSING"):
        es.execute_plan(
            plan,
            {},
            rules,
            tmp_path / "log.json",
            round_prefix="p",
            holder="h",
            batch="b",
        )


@pytest.mark.parametrize(
    ("extra", "stop"),
    [
        ({"exit_code": 4}, "EXIT_4_RETAINED_FAULT"),
        ({"interrupted": True}, "INTERRUPTED"),
        ({"restored_at": None, "injected": True}, "RESTORE_UNCONFIRMED"),
    ],
)
def test_unconfirmed_restore_stops_the_lab(rules, extra, stop):
    a = es.Window("a-r1", "a", 1, ("opspilot",))
    b = es.Window("b-r1", "b", 1, ("opspilot",))
    dispatcher = es.Dispatcher([a, b], ["l0", "l1"], rules)
    taken, _ = dispatcher.take("l0", T0)
    record = {
        "slots": [{"system": "opspilot", "position": 1, "status": "ran"}],
        "restored_at": (T0 + timedelta(minutes=20)).isoformat(),
        "ended_at": (T0 + timedelta(minutes=21)).isoformat(),
        **extra,
    }
    dispatcher.finish("l0", taken, record)
    assert dispatcher.stopped("l0")
    assert dispatcher.records[-1]["lab_stopped"] == stop
    # wash and gap never advance from it; the lab takes nothing more
    assert dispatcher.labs["l0"].last_restored is None
    assert dispatcher.take("l0", T0 + timedelta(hours=5)) == (None, None)
    # its pending windows go to the other lab
    other, _ = dispatcher.take("l1", T0)
    assert other is not None


def test_a_window_without_injection_needs_no_restore(rules):
    window = es.Window("n-r1", "n", 1, ("opspilot",))
    dispatcher = es.Dispatcher([window], ["l0"], rules)
    dispatcher.take("l0", T0)
    end = T0 + timedelta(minutes=9)
    dispatcher.finish(
        "l0",
        window,
        {
            "injected": False,
            "ended_at": end.isoformat(),
            "slots": [{"system": "opspilot", "position": 1, "status": "ran"}],
        },
    )
    assert not dispatcher.stopped("l0")
    assert dispatcher.labs["l0"].last_restored == end


def test_check_does_not_fall_back_to_the_end(rules):
    plan = _plan_json(("a-r1", "a", ("opspilot",)), ("b-r1", "b", ("opspilot",)))
    first = _record("a-r1", "a", "l0", T0, order=("opspilot",), restored_at=None)
    later = _record("b-r1", "b", "l0", T0 + timedelta(hours=2), order=("opspilot",))
    found = es.check(plan, _log([first, later]), rules)["violations"]
    assert "RESTORE_UNCONFIRMED:a-r1" in found
    assert "AFTER_UNCONFIRMED_RESTORE:b-r1" in found


def test_runner_exit_4_stops_its_lab_in_run_schedule(rules):
    class Exit4(FakeRunner):
        def run(self, lab, window):
            record = super().run(lab, window)
            if lab == "l0":
                record["exit_code"] = 4
            return record

    fast = replace(
        rules,
        same_scenario_gap=timedelta(seconds=0.05),
        wash_min=timedelta(seconds=0.01),
    )
    windows = [es.Window(f"s{i}-r1", f"s{i}", 1, ("opspilot",)) for i in range(6)]
    runner = Exit4()
    records = es.run_schedule(windows, ("l0", "l1"), fast, runner, poll_seconds=0.01)
    on_l0 = [lab for lab, _ in runner.calls if lab == "l0"]
    assert len(on_l0) == 1
    # the l0 window is interrupted-by-rule (HARNESS) only if flagged; here it
    # ran, so every other window ran on l1
    assert len(records) == 6


def test_lab_restart_reruns_the_pair(rules):
    record = {
        "slots": [
            {"system": "opspilot", "status": "ran"},
            {"system": "holmes", "status": "ran"},
        ],
        "lab_restarts": {
            "before": "j/jaeger=2",
            "after": "j/jaeger=3",
            "changed": True,
        },
    }
    out = es.decide(record, rules)
    assert out["void"] == "LAB_RESTARTED" and out["action"] == "rerun_pair"
    record["lab_restarts"]["changed"] = None  # unknown: recorded, not voided
    assert es.decide(record, rules)["void"] is None


def test_probe_baseline_comes_from_the_record_on_a_shared_host(rules):
    record = {
        "shared_host": True,
        "baseline_p95_ms": 700.0,
        "probe_baseline_p95_ms": 300.0,
        "slots": [{"system": "opspilot", "status": "ran"}],
    }
    assert es.decide(record, rules)["void"] == "HOST_NOISE"


def test_check_lists_voided_attempts_of_both_systems(rules, tmp_path):
    both = ("opspilot", "holmes")
    plan = _plan_json(("a-r1", "a", both), ("b-r1", "b", both))
    voided = _record(
        "a-r1",
        "a",
        "l0",
        T0,
        void="LAB_RESTARTED",
        decision={
            "action": "rerun_pair",
            "systems": list(both),
            "note": "lab store restarted, rerun per rule",
        },
    )
    voided["slots"][0]["attempt_id"] = "ops-1"
    voided["slots"][1]["attempt_id"] = "holmes-1"
    rerun = _record("a-r1-rerun", "a", "l0", T0 + timedelta(minutes=80))
    rerun["slots"][0]["attempt_id"] = "ops-2"
    rerun["slots"][1]["attempt_id"] = "holmes-2"
    slot_void = _record(
        "b-r1",
        "b",
        "l1",
        T0,
        void="ENV_SETUP_FAILED",
        decision={
            "action": "rerun_slot",
            "systems": ["holmes"],
            "note": "environment not ready, rerun per rule",
        },
    )
    slot_void["slots"][1].update(status="setup_failed", attempt_id="holmes-3")
    slot_rerun = _record(
        "b-r1-rerun", "b", "l1", T0 + timedelta(minutes=80), order=("holmes",)
    )
    slot_rerun["slots"][0]["attempt_id"] = "holmes-4"
    result = es.check(plan, _log([voided, rerun, slot_void, slot_rerun]), rules)
    assert result["violations"] == []
    rows = {(a["system"], a["voided_attempt_id"]): a for a in result["voided_attempts"]}
    assert set(rows) == {
        ("opspilot", "ops-1"),
        ("holmes", "holmes-1"),
        ("holmes", "holmes-3"),
    }
    assert rows[("opspilot", "ops-1")]["rule"] == "LAB_RESTARTED"
    assert rows[("holmes", "holmes-3")]["rerun_attempt_id"] == "holmes-4"
    registry = es.dev_round_reruns(result)
    assert registry["reruns"] == [
        {
            "voided_attempt_id": "ops-1",
            "rerun_attempt_id": "ops-2",
            "note": "lab store restarted, rerun per rule",
            "rule": "LAB_RESTARTED",
        }
    ]


def test_dev_round_accepts_a_schedule_void_of_a_run_that_ran():
    from scripts.eval_dev_round import DevRoundError, _registered_reruns

    started = {
        "ops-1": {
            "status": "ran",
            "product_run_id": "run-1",
            "scenario_id": "a",
            "fault": {
                "started_at": "2026-11-05T00:00:00+00:00",
                "ended_at": "2026-11-05T00:20:00+00:00",
            },
        },
        "ops-2": {
            "status": "ran",
            "product_run_id": "run-2",
            "scenario_id": "a",
            "fault": {
                "started_at": "2026-11-05T02:00:00+00:00",
                "ended_at": "2026-11-05T02:20:00+00:00",
            },
        },
    }
    entry = {
        "voided_attempt_id": "ops-1",
        "rerun_attempt_id": "ops-2",
        "note": "n",
        "rule": "HOST_NOISE",
    }
    registry = {"schema": "opspilot-eval-dev-reruns-1", "reruns": [entry]}
    assert _registered_reruns(registry, started)["ops-1"]["rule"] == "HOST_NOISE"
    # without a rule (#432) or with ENV_SETUP_FAILED, a Run that ran is not eligible
    for rule in (None, "ENV_SETUP_FAILED"):
        registry["reruns"] = [{**entry, "rule": rule}]
        with pytest.raises(DevRoundError, match="RERUN_NOT_ELIGIBLE"):
            _registered_reruns(registry, started)
    registry["reruns"] = [{**entry, "rule": "WHATEVER"}]
    with pytest.raises(DevRoundError, match="RERUNS_INVALID"):
        _registered_reruns(registry, started)


def test_dev_round_cli_defaults_to_three_slots():
    from scripts import eval_dev_round

    assert eval_dev_round.CLI_SLOTS_PER_SCENARIO == 3
