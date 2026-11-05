"""A stopped lab rejoins after ``eval_live --recover``, and a usage refusal
(exit 2, nothing injected) does not stop its lab (F1 closure exam; rules
f1-closure-paired-r3 ``lab_state.restore_unconfirmed``: "no later window
starts there ... until an engineer ran eval_live --recover")."""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts import eval_pair_schedule as es
from tests.unit.test_s7_pair_schedule_impl import FakeRunner, _lab, _labs_file

T0 = datetime(2026, 11, 5, tzinfo=UTC)


@pytest.fixture
def rules() -> es.Rules:
    return es.load_rules()


def _stop(dispatcher: es.Dispatcher, lab: str, window: es.Window) -> datetime:
    taken, _ = dispatcher.take(lab, T0)
    assert taken == window
    ended = T0 + timedelta(minutes=21)
    dispatcher.finish(
        lab,
        window,
        {
            "slots": [{"system": "opspilot", "position": 1, "status": "ran"}],
            "injected_at": T0.isoformat(),
            "started_at": T0.isoformat(),
            "ended_at": ended.isoformat(),
            "interrupted": True,
        },
    )
    assert dispatcher.stopped(lab)
    return ended


def test_a_recovered_lab_rejoins_and_counts_wash_and_gap_from_the_recovery(rules):
    a = es.Window("a-r1", "a", 1, ("opspilot",))
    b = es.Window("b-r1", "b", 1, ("opspilot",))
    a2 = es.Window("a-r2", "a", 2, ("opspilot",))
    dispatcher = es.Dispatcher([a, b, a2], ["l0"], rules)
    ended = _stop(dispatcher, "l0", a)
    # still stopped: a recovery from before the stop does not count
    assert dispatcher.rejoin("l0", ended - timedelta(seconds=1)) is False
    assert dispatcher.take("l0", T0 + timedelta(hours=5)) == (None, None)
    recovered = ended + timedelta(minutes=5)
    assert dispatcher.rejoin("l0", recovered) is True
    assert not dispatcher.stopped("l0")
    # the wash counts from the recovery
    window, ready = dispatcher.take("l0", recovered + timedelta(seconds=1))
    assert window is None and ready == recovered + rules.wash_min
    window, _ = dispatcher.take("l0", recovered + rules.wash_min)
    assert window == b  # a-r2 waits out the same-scenario gap from the recovery
    start = recovered + rules.wash_min
    record = {
        "slots": [{"system": "opspilot", "position": 1, "status": "ran"}],
        "injected_at": start.isoformat(),
        "started_at": start.isoformat(),
        "restored_at": (start + timedelta(minutes=20)).isoformat(),
        "ended_at": (start + timedelta(minutes=21)).isoformat(),
    }
    dispatcher.finish("l0", b, record)
    booked = dispatcher.records[-1]
    assert booked["rejoined"] == {
        "after": "a-r1",
        "stop": "INTERRUPTED",
        "recovered_at": recovered.isoformat(),
    }
    plan = {"windows": [w.as_json() for w in (a, b, a2)]}
    log = {"schema": es.LOG_SCHEMA, "windows": dispatcher.records}
    found = es.check(plan, log, rules)["violations"]
    assert "AFTER_UNCONFIRMED_RESTORE:b-r1" not in found
    assert "WASH_SHORT:b-r1" not in found
    assert "INTERRUPTED:a-r1" in found  # the stop itself stays reported


def test_check_still_flags_a_window_after_a_stop_without_rejoin(rules):
    a = es.Window("a-r1", "a", 1, ("opspilot",))
    b = es.Window("b-r1", "b", 1, ("opspilot",))
    dispatcher = es.Dispatcher([a, b], ["l0"], rules)
    ended = _stop(dispatcher, "l0", a)
    start = ended + timedelta(hours=2)
    record = {
        "window_id": "b-r1",
        "scenario_id": "b",
        "lab": "l0",
        "systems": ["opspilot"],
        "slots": [{"system": "opspilot", "position": 1, "status": "ran"}],
        "injected_at": start.isoformat(),
        "started_at": start.isoformat(),
        "restored_at": (start + timedelta(minutes=20)).isoformat(),
        # a rejoin that predates the stop is no rejoin
        "rejoined": {"after": "a-r1", "recovered_at": T0.isoformat()},
    }
    plan = {"windows": [w.as_json() for w in (a, b)]}
    log = {"schema": es.LOG_SCHEMA, "windows": [*dispatcher.records, record]}
    found = es.check(plan, log, rules)["violations"]
    assert "AFTER_UNCONFIRMED_RESTORE:b-r1" in found


def test_run_schedule_waits_for_a_rejoin_then_uses_the_lab_again(rules):
    fast = replace(
        rules,
        same_scenario_gap=timedelta(seconds=0.01),
        wash_min=timedelta(seconds=0.01),
    )
    windows = [es.Window(f"s{i}-r1", f"s{i}", 1, ("opspilot",)) for i in range(8)]
    recovered: dict[str, datetime] = {}
    stopped_once = threading.Event()

    class StopL0Once(FakeRunner):
        def run(self, lab: str, window: es.Window) -> dict[str, Any]:
            record = super().run(lab, window)
            if lab == "l0" and not stopped_once.is_set():
                stopped_once.set()
                record["exit_code"] = 4
            return record

    def rejoin(lab: str) -> datetime | None:
        if lab == "l0" and stopped_once.is_set() and lab not in recovered:
            recovered[lab] = datetime.now(UTC)  # the engineer ran --recover
        return recovered.get(lab)

    runner = StopL0Once()
    records = es.run_schedule(
        windows, ("l0", "l1"), fast, runner, poll_seconds=0.01, rejoin=rejoin
    )
    on_l0 = [r for r in records if r["lab"] == "l0"]
    assert on_l0[0]["lab_stopped"] == "EXIT_4_RETAINED_FAULT"
    assert len(on_l0) >= 2  # back in the rotation
    assert on_l0[1]["rejoined"]["after"] == on_l0[0]["window_id"]
    assert len(records) == len(windows)


def test_without_rejoin_a_stopped_lab_still_ends(rules):
    class Exit4(FakeRunner):
        def run(self, lab: str, window: es.Window) -> dict[str, Any]:
            record = super().run(lab, window)
            if lab == "l0":
                record["exit_code"] = 4
            return record

    fast = replace(
        rules,
        same_scenario_gap=timedelta(seconds=0.01),
        wash_min=timedelta(seconds=0.01),
    )
    windows = [es.Window(f"s{i}-r1", f"s{i}", 1, ("opspilot",)) for i in range(4)]
    runner = Exit4()
    es.run_schedule(windows, ("l0", "l1"), fast, runner, poll_seconds=0.01)
    assert [lab for lab, _ in runner.calls].count("l0") == 1


def test_a_usage_refusal_does_not_stop_the_lab_and_reruns_the_pair(tmp_path, rules):
    specs = es.load_lab_specs(_labs_file(tmp_path, [_lab("a", 0, 55001)]))
    runner = es.CommandWindowRunner(
        specs,
        {"x": "r", "y": "r"},
        round_prefix="p",
        holder="h",
        batch="b",
        execute=lambda spec, argv: (2, ""),
    )
    window = es.Window("x-r1", "x", 1, ("opspilot", "holmes"))
    record = runner.run("a", window)
    assert record["interrupted"] is False
    assert record["injected"] is False
    assert record["refused"] == "USAGE"
    assert es.restore_unconfirmed(record) is None
    decision = es.decide(record, rules)
    assert (decision["void"], decision["action"]) == (
        "HARNESS_INTERRUPTED",
        "rerun_pair",
    )
    other = es.Window("y-r1", "y", 1, ("opspilot",))
    dispatcher = es.Dispatcher([window, other], ["a"], rules)
    dispatcher.take("a", T0)
    dispatcher.finish("a", window, record)
    assert not dispatcher.stopped("a")
    assert "lab_stopped" not in dispatcher.records[-1]
    taken, _ = dispatcher.take("a", T0)
    assert taken is not None


def test_rejoin_cli_leaves_the_recovery_for_the_running_schedule(tmp_path: Path):
    log = tmp_path / "log.json"
    at = "2026-11-05T17:00:00+00:00"
    assert (
        es.main(["rejoin", "--log", str(log), "--lab", "lab-2", "--recovered-at", at])
        == 0
    )
    marker = json.loads((tmp_path / "log.json.rejoin" / "lab-2.json").read_text())
    assert marker["recovered_at"] == at
    read = es.read_rejoin(log)
    assert read("lab-2") == datetime.fromisoformat(at)
    assert read("lab-0") is None
    assert es.main(["rejoin", "--log", str(log), "--lab", "../x"]) == 2
