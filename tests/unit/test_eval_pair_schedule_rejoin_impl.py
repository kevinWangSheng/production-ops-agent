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


NOW = T0 + timedelta(hours=3)
STOP_END = T0 + timedelta(minutes=21)


def _stopped_log(tmp_path: Path, **stop: Any) -> Path:
    log = tmp_path / "log.json"
    record = {
        "window_id": "x-r1",
        "lab": "a",
        "round_id": "p-x-r1",
        "attempt_id": "att-1",
        "ended_at": STOP_END.isoformat(),
        "lab_stopped": "EXIT_4_RETAINED_FAULT",
        **stop,
    }
    log.write_text(json.dumps({"schema": es.LOG_SCHEMA, "windows": [record]}))
    return log


def _recovery(at: datetime, **extra: Any) -> str:
    return json.dumps(
        {
            "schema": es.RECOVERY_SCHEMA,
            "round": "p-x-r1",
            "attempt_id": "att-1",
            "recovered_at": at.isoformat(),
            **extra,
        }
    )


def _spec(tmp_path: Path) -> es.LabSpec:
    return es.load_lab_specs(_labs_file(tmp_path, [_lab("a", 0, 55001)]))["a"]


PATH = "/root/out/a/evidence/p-x-r1/recovery-20261105T030000Z.json"


def test_rejoin_needs_the_recover_record_of_the_stopping_round(tmp_path):
    log, spec = _stopped_log(tmp_path), _spec(tmp_path)
    at = STOP_END + timedelta(minutes=30)
    read: list[list[str]] = []

    def execute(lab: es.LabSpec, argv: Any) -> tuple[int, str]:
        read.append(list(argv))
        return 0, _recovery(at)

    got = es.confirm_recovery(log, "a", spec, "p", PATH, now=NOW, execute=execute)
    assert got == at
    assert read == [["cat", PATH]]


@pytest.mark.parametrize(
    ("record", "path", "code"),
    [
        # recovered_at later than now
        (_recovery(NOW + timedelta(seconds=1)), PATH, "RECOVERED_AT_IN_FUTURE"),
        # a recovery from before the stopping window ended
        (_recovery(STOP_END), PATH, "RECOVERY_BEFORE_STOP"),
        # another round's or attempt's recovery, or no recovery record at all
        (_recovery(NOW, round="p-y-r1"), PATH, "RECOVERY_INVALID"),
        (_recovery(NOW, attempt_id="att-2"), PATH, "RECOVERY_INVALID"),
        (_recovery(NOW, schema="other"), PATH, "RECOVERY_INVALID"),
        (None, PATH, "RECOVERY_UNREADABLE"),
        # a file outside the stopping round's evidence
        (_recovery(NOW), "/root/out/a/evidence/p-y-r1/recovery-20261105T030000Z.json",
         "RECOVERY_PATH_INVALID"),
        (_recovery(NOW), "/root/out/a/evidence/p-x-r1/lifecycle.json",
         "RECOVERY_PATH_INVALID"),
    ],
)  # fmt: skip
def test_rejoin_refuses_without_a_matching_recovery(tmp_path, record, path, code):
    log, spec = _stopped_log(tmp_path), _spec(tmp_path)

    def execute(lab: es.LabSpec, argv: Any) -> tuple[int, str]:
        return (1, "") if record is None else (0, record)

    with pytest.raises(es.ScheduleError) as raised:
        es.confirm_recovery(log, "a", spec, "p", path, now=NOW, execute=execute)
    assert raised.value.code == code


def test_rejoin_refuses_a_lab_that_is_not_stopped(tmp_path):
    log = _stopped_log(tmp_path, lab_stopped=None)
    with pytest.raises(es.ScheduleError) as raised:
        es.confirm_recovery(
            log, "a", _spec(tmp_path), "p", PATH, now=NOW, execute=lambda *a: (0, "")
        )
    assert raised.value.code == "LAB_NOT_STOPPED"


def test_a_runner_exception_stop_finds_its_round_by_the_prefix(tmp_path):
    # the run_schedule exception record has no round_id or attempt_id
    log = _stopped_log(tmp_path, round_id=None, attempt_id=None)
    at = STOP_END + timedelta(minutes=5)
    got = es.confirm_recovery(
        log, "a", _spec(tmp_path), "p", PATH, now=NOW,
        execute=lambda *a: (0, _recovery(at, attempt_id="whatever")),
    )  # fmt: skip
    assert got == at


def test_rejoin_cli_reads_the_recovery_on_the_lab_and_leaves_the_marker(tmp_path):
    out = tmp_path / "out"
    labs = tmp_path / "labs.json"
    lab = _lab("a", 0, 55001, host=None, repo=str(tmp_path), output_root=str(out))
    labs.write_text(json.dumps({"schema": es.LABS_SCHEMA, "labs": [lab]}))
    log = _stopped_log(tmp_path)
    at = datetime.now(UTC).replace(microsecond=0)
    evidence = out / "evidence" / "p-x-r1"
    evidence.mkdir(parents=True)
    path = evidence / "recovery-20261105T030000Z.json"
    argv = ["rejoin", "--log", str(log), "--labs", str(labs), "--lab", "a",
            "--round-prefix", "p", "--recovery", str(path)]  # fmt: skip
    assert es.main(argv) == 2  # no recovery record yet: refused, no marker
    assert es.read_rejoin(log)("a") is None
    path.write_text(_recovery(at))
    assert es.main(argv) == 0
    assert es.read_rejoin(log)("a") == at
    assert es.read_rejoin(log)("b") is None
    bad = [*argv[:6], "../x", *argv[7:]]
    assert es.main(bad) == 2


def test_run_ends_when_every_lab_is_stopped(rules):
    """With a rejoin reader that never reports a recovery, every lab stopped
    ends ``run`` (bounded: the test fails instead of hanging)."""

    class AllExit4(FakeRunner):
        def run(self, lab: str, window: es.Window) -> dict[str, Any]:
            record = super().run(lab, window)
            record["exit_code"] = 4
            return record

    fast = replace(
        rules,
        same_scenario_gap=timedelta(seconds=0.01),
        wash_min=timedelta(seconds=0.01),
    )
    windows = [es.Window(f"s{i}-r1", f"s{i}", 1, ("opspilot",)) for i in range(6)]
    done: list[list[dict[str, Any]]] = []
    thread = threading.Thread(
        target=lambda: done.append(
            es.run_schedule(
                windows,
                ("l0", "l1"),
                fast,
                AllExit4(),
                poll_seconds=0.01,
                rejoin=lambda lab: None,
            )
        ),
        daemon=True,
    )
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive(), "run_schedule kept waiting with every lab stopped"
    (records,) = done
    assert sorted(r["lab"] for r in records) == ["l0", "l1"]
    assert all(r["lab_stopped"] == "EXIT_4_RETAINED_FAULT" for r in records)
    plan = {"windows": [w.as_json() for w in windows]}
    result = es.check(plan, {"schema": es.LOG_SCHEMA, "windows": records}, fast)
    missing = [v for v in result["violations"] if v.startswith("MISSING:")]
    assert len(missing) == len(windows) - 2  # disclosed, never dropped
