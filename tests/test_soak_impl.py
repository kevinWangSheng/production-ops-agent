"""Implementation-side tests of the 72h soak scripts (scripts/soak/).

The summarizer's acceptance/contract tests come from an independent author;
these pin the implementation's own invariants: the frozen plan (S-R6), the
scripted operator's close rule (S-R3), the runner's step bookkeeping and
restart safety, the fault key's command whitelist (S-R2), the tunnel wiring
(S-R1) and log rotation (S-R14).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from scripts import kind_lab
from scripts.soak import lab_fault, pg, runner, summarize
from scripts.soak import plan as soak_plan
from scripts.soak.common import extension_s, iso, read_jsonl

ROOT = Path(kind_lab.ROOT)
SOAK = ROOT / "scripts/soak"
T0 = datetime(2026, 11, 10, 0, 0, tzinfo=timezone.utc)


# --- plan (S-R6) ---------------------------------------------------------------


def _full_plan() -> dict[str, Any]:
    return soak_plan.build_plan(
        T0,
        price_input=2.0,
        price_output=3.0,
        price_source="test",
        expected_deployments=["payment", "checkout"],
    )


def _items(kind: str) -> list[dict[str, Any]]:
    return [i for i in _full_plan()["injections"] if i["category"] == kind]


def test_plan_has_the_raw_directory_shape() -> None:
    plan = _full_plan()
    assert plan["product_alertname"] == "CheckoutPlaceOrderErrorRatioHigh"
    assert plan["expected_deployments"] == ["checkout", "payment"]
    assert plan["model_price_cny_per_mtok"] == {"input": 2.0, "output": 3.0}
    assert plan["t0"] == iso(T0)
    for item in plan["injections"]:
        assert item["category"] in {"episode", "noise", "agent_fault"}
        assert item["planned_start"] == iso(T0 + timedelta(seconds=item["at_s"]))
        assert isinstance(item["planned_duration_s"], int) and item["name"]


def test_plan_episodes() -> None:
    episodes = _items("episode")
    assert len(episodes) == 24
    starts = [e["at_s"] for e in episodes]
    assert all(b - a == 3 * 3600 for a, b in zip(starts, starts[1:], strict=False))
    long = {e["id"] for e in episodes if e["planned_duration_s"] == 75 * 60}
    assert long == {"ep08", "ep16"}
    assert all(
        e["planned_duration_s"] == 15 * 60 for e in episodes if e["id"] not in long
    )
    assert all(e["name"] == "paymentFailure" for e in episodes)
    # the last episode's 90 min cycle ends inside 72 h
    assert starts[-1] + 90 * 60 < 72 * 3600


def test_plan_noise_and_agent_faults() -> None:
    noise = _items("noise")
    assert sorted(n["name"] for n in noise) == [
        "adManualGc",
        "adManualGc",
        "loadGeneratorFloodHomepage",
        "loadGeneratorFloodHomepage",
    ]
    busy = [(e["at_s"], e["at_s"] + 90 * 60) for e in _items("episode")]
    for n in noise:
        assert not any(
            a < n["at_s"] + n["planned_duration_s"] and n["at_s"] < b for a, b in busy
        )
    agent = _items("agent_fault")
    assert [a["id"] for a in agent] == ["A1", "A2", "A3", "A4", "A5"]
    times = [a["at_s"] for a in agent]
    assert all(12 * 3600 <= t <= 60 * 3600 for t in times)
    assert all(b - a >= 2 * 3600 for a, b in zip(times, times[1:], strict=False))
    by_id = {a["id"]: a for a in agent}
    episodes = {e["id"]: e for e in _items("episode")}
    a3, ep = by_id["A3"], episodes[by_id["A3"]["overlaps"]]
    assert ep["at_s"] <= a3["at_s"] < ep["at_s"] + ep["planned_duration_s"]
    assert a3["planned_duration_s"] == 20 * 60
    assert by_id["A4"]["planned_duration_s"] == 10 * 60
    assert by_id["A1"]["steps"][0]["condition"] == "run_running"
    assert by_id["A2"]["steps"][0]["condition"] == "idle"


def test_plan_steps_well_formed() -> None:
    plan = _full_plan()
    steps = soak_plan.steps(plan)
    ids = [s["id"] for s in steps]
    assert len(ids) == len(set(ids))
    for step in steps:
        assert ("at_s" in step) != ("after" in step)
        if "after" in step:
            assert step["after"] in ids
        assert step["action"] in {
            "flag_on",
            "flag_off",
            "worker_kill",
            "worker_up",
            "pg_restart",
            "egress_cut",
            "egress_restore",
            "tunnel_stop",
            "tunnel_start",
            "web_restart",
        }
    assert {s["flag"] for s in steps if "flag" in s} == soak_plan.FLAGS


def test_freeze_is_stable_and_refuses_another_plan(tmp_path: Path) -> None:
    path = tmp_path / "plan.json"
    sha = soak_plan.freeze(path, _full_plan())
    assert soak_plan.freeze(path, _full_plan()) == sha
    assert soak_plan.load(path)[1] == sha
    path.write_text(path.read_text().replace('"ep01"', '"epXX"'))
    with pytest.raises(SystemExit):
        soak_plan.freeze(path, _full_plan())


# --- the scripted operator (S-R3) ------------------------------------------------


@pytest.mark.parametrize(
    ("row", "faults_active", "expected"),
    [
        ({"lifecycle": "resolved"}, True, "resolved"),
        (
            {"lifecycle": "open", "last_transition": pg.UNCONFIRMED, "active_runs": 0},
            False,
            "observation_ended_unconfirmed_fault_cleared",
        ),
        (
            {"lifecycle": "open", "last_transition": pg.UNCONFIRMED, "active_runs": 0},
            True,
            None,
        ),
        (
            {"lifecycle": "open", "last_transition": pg.UNCONFIRMED, "active_runs": 1},
            False,
            None,
        ),
        (
            {
                "lifecycle": "open",
                "last_transition": "recovery_confirmed",
                "active_runs": 0,
            },
            False,
            None,
        ),
        ({"lifecycle": "open", "last_transition": None, "active_runs": 0}, False, None),
        ({"lifecycle": "observing_recovery"}, False, None),
        ({"lifecycle": "closed"}, False, None),
    ],
)
def test_should_close(
    row: dict[str, Any], faults_active: bool, expected: str | None
) -> None:
    assert pg.should_close(row, faults_active=faults_active) == expected


def test_queries_are_reads_scoped_to_t0() -> None:
    sqls = [
        pg.close_candidates_sql(T0),
        pg.run_states_sql(T0),
        *pg.export_queries(T0).values(),
    ]
    for sql in sqls:
        assert sql.lstrip().upper().startswith("SELECT")
        assert not re.search(
            r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE)\b", sql.upper()
        )
    for sql in sqls[:2]:
        assert "'2026-11-10T00:00:00Z'::timestamptz" in sql


# --- runner bookkeeping -----------------------------------------------------------


@dataclass
class Proc:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _config(tmp_path: Path) -> runner.Config:
    creds = tmp_path / "operator"
    creds.write_text("soak-operator:pw\n")
    return runner.Config(
        compose=("compose",),
        compose_egress=("-f", "egress.yaml"),
        workbench="http://127.0.0.1:18080",
        operator_credentials_file=creds,
        health={"web": "http://127.0.0.1:19464/readyz"},
        prometheus="http://127.0.0.1:19090",
        prometheus_env_file=None,
        fault_ssh=("ssh", "soakfault@rental"),
    )


def _plan(*items: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": soak_plan.SCHEMA,
        "t0": iso(T0),
        "duration_s": 72 * 3600,
        "injections": list(items),
    }


EPISODE = {
    "id": "ep01",
    "kind": "episode",
    "flag": "paymentFailure",
    "at_s": 600,
    "duration_s": 900,
    "steps": [
        {"id": "ep01:on", "action": "flag_on", "at_s": 600, "flag": "paymentFailure"},
        {
            "id": "ep01:off",
            "action": "flag_off",
            "after": "ep01:on",
            "delay_s": 900,
            "flag": "paymentFailure",
        },
    ],
}
KILL = {
    "id": "A1",
    "kind": "agent_fault",
    "at_s": 1000,
    "steps": [
        {
            "id": "A1:kill",
            "action": "worker_kill",
            "at_s": 1000,
            "condition": "run_running",
            "max_wait_s": 1800,
        },
        {"id": "A1:recover", "action": "worker_up", "after": "A1:kill", "delay_s": 30},
    ],
}


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    calls: list[list[str]] = []
    results: list[Proc] = []
    states: dict[str, str] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> Proc:
        calls.append(list(cmd))
        return results.pop(0) if results else Proc()

    monkeypatch.setattr(runner, "run", fake_run)

    def make(plan: dict[str, Any]) -> runner.Runner:
        clock = Clock(T0)
        r = runner.Runner(
            config=_config(tmp_path),
            data=tmp_path,
            plan=plan,
            plan_sha256="sha",
            now=clock,
            sleep=lambda s: clock.advance(s),
        )
        r.run_states = lambda: dict(states)  # type: ignore[method-assign]
        r.record_open_incidents = lambda step_id: None  # type: ignore[method-assign]
        r.begin()  # the clock is at T0: no wait
        return r

    @dataclass
    class Fake:
        calls: list[list[str]]
        results: list[Proc]
        states: dict[str, str]

    fake_obj = Fake(calls, results, states)
    fake_obj.make = make  # type: ignore[attr-defined]
    yield fake_obj


def _steps(r: runner.Runner) -> list[dict[str, Any]]:
    return [e for e in read_jsonl(r.events_path) if e["type"] == "step"]


def _marks(r: runner.Runner) -> list[tuple[str, str]]:
    return [
        (e["type"], e["injection_id"])
        for e in read_jsonl(r.events_path)
        if e["type"].startswith("injection_")
    ]


def test_runner_times_followers_from_the_actual_step(fake: Any) -> None:
    r = fake.make(_plan(EPISODE))
    r.plan_tick()
    assert fake.calls == []  # not due before T0+600
    r.now.advance(700)
    r.plan_tick()
    assert fake.calls[-1] == ["ssh", "soakfault@rental", "flag paymentFailure on ep01"]
    r.now.advance(899)
    r.plan_tick()
    assert len(fake.calls) == 1
    r.now.advance(1)
    r.plan_tick()
    assert fake.calls[-1][-1] == "flag paymentFailure off ep01"
    assert [s["step"] for s in _steps(r)] == ["ep01:on", "ep01:off"]
    assert _marks(r) == [("injection_on", "ep01"), ("injection_off", "ep01")]


def test_runner_is_restart_safe(fake: Any) -> None:
    r = fake.make(_plan(EPISODE))
    r.now.advance(700)
    r.plan_tick()
    again = fake.make(_plan(EPISODE))
    again.now.advance(800)
    again.plan_tick()
    assert len(fake.calls) == 1  # ep01:on is not repeated
    starts = [e for e in read_jsonl(r.events_path) if e["type"] == "soak_start"]
    assert len(starts) == 1 and starts[0]["ts"] == iso(T0)
    other = runner.Runner(
        config=r.config, data=r.data, plan=r.plan, plan_sha256="other"
    )
    with pytest.raises(SystemExit):
        other.begin()


def test_runner_refuses_a_start_after_t0(tmp_path: Path) -> None:
    late = Clock(T0 + timedelta(seconds=runner.LATE_START_S + 1))
    r = runner.Runner(
        config=_config(tmp_path),
        data=tmp_path,
        plan=_plan(EPISODE),
        plan_sha256="sha",
        now=late,
    )
    with pytest.raises(SystemExit):
        r.begin()


def test_runner_waits_for_t0(tmp_path: Path) -> None:
    early = Clock(T0 - timedelta(seconds=60))
    r = runner.Runner(
        config=_config(tmp_path),
        data=tmp_path,
        plan=_plan(EPISODE),
        plan_sha256="sha",
        now=early,
        sleep=early.advance,
    )
    r.begin()
    start = [e for e in read_jsonl(r.events_path) if e["type"] == "soak_start"]
    assert start[0]["ts"] == iso(T0)


def test_runner_retries_then_records_failure(fake: Any) -> None:
    r = fake.make(_plan(EPISODE))
    fake.results.extend([Proc(1, "", "boom")] * 3)
    r.now.advance(700)
    r.plan_tick()
    statuses = [s["status"] for s in _steps(r)]
    assert statuses == ["retry", "retry", "failed"]
    skipped = [e for e in read_jsonl(r.events_path) if e["type"] == "injection_skipped"]
    assert skipped[0]["injection_id"] == "ep01" and "failed" in skipped[0]["reason"]
    # the off step still runs after a failed on (restoring steps always run)
    r.now.advance(900)
    r.plan_tick()
    assert _steps(r)[-1]["step"] == "ep01:off"


def test_runner_skips_a_missed_injection_but_still_restores(fake: Any) -> None:
    r = fake.make(_plan(EPISODE))
    r.now.advance(600 + runner.MISSED_AFTER_S + 60)
    r.plan_tick()
    steps = _steps(r)
    assert steps[0]["step"] == "ep01:on" and steps[0]["status"] == "skipped"
    assert steps[0]["reason"] == "missed"
    assert _marks(r) == [("injection_skipped", "ep01")]
    r.now.advance(900)
    r.plan_tick()
    assert _steps(r)[-1]["step"] == "ep01:off" and _steps(r)[-1]["status"] == "ok"


def test_conditional_step_waits_then_runs_anyway(fake: Any) -> None:
    r = fake.make(_plan(KILL))
    r.now.advance(1000)
    r.plan_tick()
    assert fake.calls == [] and _steps(r)[-1]["status"] == "waiting"
    r.now.advance(600)
    r.plan_tick()
    assert fake.calls == []
    fake.states["run-1"] = "running"
    r.now.advance(5)
    r.plan_tick()
    assert fake.calls[-1] == ["compose", "kill", "worker"]
    kill = _steps(r)[-1]
    assert kill["status"] == "ok" and kill["condition_met"] is True
    r.now.advance(30)
    r.plan_tick()
    assert fake.calls[-1] == ["compose", "up", "-d", "--no-deps", "worker"]

    late = fake.make(
        _plan({**KILL, "id": "A9", "steps": [{**KILL["steps"][0], "id": "A9:kill"}]})
    )
    fake.states.clear()
    late.now.advance(1000)
    late.plan_tick()
    late.now.advance(1800)
    late.plan_tick()
    record = [s for s in _steps(late) if s["step"] == "A9:kill"][-1]
    assert record["status"] == "ok" and record["condition_met"] is False


def test_egress_cut_adds_the_drill_file(fake: Any) -> None:
    r = fake.make(_plan())
    r.action({"action": "egress_cut"})
    r.action({"action": "egress_restore"})
    assert fake.calls[0] == [
        "compose",
        "-f",
        "egress.yaml",
        "up",
        "-d",
        "--no-deps",
        "worker",
    ]
    assert fake.calls[1] == [
        "compose",
        "up",
        "-d",
        "--no-deps",
        "--force-recreate",
        "worker",
    ]


def test_close_posts_the_workbench_form(
    fake: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake_http(url: str, **kwargs: Any) -> Any:
        seen.update(url=url, **kwargs)
        return SimpleNamespace(status=200, error=None, body=b"{}")

    monkeypatch.setattr(runner, "http", fake_http)
    r = fake.make(_plan())
    r.close({"incident_id": "abc", "control_generation": 4}, "resolved")
    assert seen["url"] == "http://127.0.0.1:18080/incidents/abc/control"
    assert seen["method"] == "POST"
    assert seen["headers"]["Origin"] == "http://127.0.0.1:18080"
    assert seen["headers"]["Authorization"].startswith("Basic ")
    form = seen["data"].decode()
    assert "action=close" in form and "expected_generation=4" in form
    assert "idempotency_key=soak-close-abc-4" in form
    event = [e for e in read_jsonl(r.events_path) if e["type"] == "operator"][0]
    assert event["scripted"] is True and event["actor"] == "soak-operator"
    assert "pw" not in json.dumps(event)


def test_flags_on_tracks_open_injections() -> None:
    steps = soak_plan.steps(_plan(EPISODE))
    assert runner.flags_on(steps, {}) == []
    on = {"ep01:on": {"status": "ok", "at": iso(T0)}}
    assert runner.flags_on(steps, on) == ["ep01"]
    assert runner.flags_on(steps, {**on, "ep01:off": {"status": "ok"}}) == []


def test_deployment_pods_join() -> None:
    def s(**labels: str) -> dict[str, Any]:
        return {"metric": labels, "value": [0, "1"]}

    series = {
        "pod_owner": [
            s(pod="a-1", owner_name="a-rs"),
            s(pod="a-2", owner_name="a-rs"),
            s(pod="b-1", owner_name="b-rs"),
        ],
        "replicaset_owner": [
            s(replicaset="a-rs", owner_name="a"),
            s(replicaset="b-rs", owner_name="b"),
            s(replicaset="c-rs", owner_name="c"),
        ],
        "container_info": [s(pod="a-1"), s(pod="b-1")],
    }
    assert runner.deployment_pods(series) == {"a": 1, "b": 1, "c": 0}
    assert runner.deployment_pods({**series, "pod_owner": None}) is None


def test_soak_end_extension() -> None:
    assert extension_s(4 * 3600) == 0
    assert extension_s(5 * 3600) == 5 * 3600
    assert extension_s(20 * 3600) == 12 * 3600
    assert runner.soak_end(T0, 0) == T0 + timedelta(hours=72)


# --- summarizer smoke (contract tests are separate) --------------------------------


def test_interruption_seconds_pairs_and_open_spans() -> None:
    events = [
        {"ts": iso(T0 + timedelta(hours=1)), "type": "env_interruption_start"},
        {"ts": iso(T0 + timedelta(hours=2)), "type": "env_interruption_end"},
        {"ts": iso(T0 + timedelta(hours=10)), "type": "env_interruption_start"},
    ]
    now = T0 + timedelta(hours=10, minutes=30)
    assert runner.interruption_seconds(events, now) == pytest.approx(5400)


def test_unplanned_tunnel_outage_is_an_interruption(fake: Any) -> None:
    r = fake.make(_plan())
    r.tunnel_transition(False)
    r.tunnel_transition(False)
    r.now.advance(300)
    r.tunnel_transition(True)
    kinds = [e["type"] for e in read_jsonl(r.events_path)][1:]
    assert kinds == [
        "tunnel_down",
        "env_interruption_start",
        "tunnel_up",
        "env_interruption_end",
    ]


def test_planned_tunnel_stop_is_not_an_interruption(fake: Any) -> None:
    r = fake.make(_plan())
    r.event("step", step="A4:stop", status="ok")
    r.tunnel_transition(False)
    r.now.advance(600)
    r.event("step", step="A4:start", status="ok")
    r.now.advance(30)
    r.tunnel_transition(True)
    kinds = [e["type"] for e in read_jsonl(r.events_path)]
    assert "tunnel_down" in kinds and "tunnel_up" in kinds
    assert "env_interruption_start" not in kinds


def test_alerts_tick_records_each_firing_once(fake: Any) -> None:
    alert = {
        "fingerprint": "abc",
        "startsAt": "2026-11-10T01:03:04.123456789Z",
        "labels": {"alertname": "CheckoutPlaceOrderErrorRatioHigh"},
    }
    fake.results.extend([Proc(0, json.dumps([alert])), Proc(0, json.dumps([alert]))])
    r = fake.make(_plan())
    r.alerts_tick()
    r.alerts_tick()
    assert fake.calls[-1] == ["ssh", "soakfault@rental", "alerts"]
    rows = list(read_jsonl(r.data / "alerts.jsonl"))
    assert len(rows) == 1
    assert rows[0]["starts_at"] == "2026-11-10T01:03:04.123456Z"
    assert rows[0]["alertname"] == "CheckoutPlaceOrderErrorRatioHigh"


def test_probe_writes_readyz_and_prom_up(
    fake: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runner,
        "http",
        lambda url, **kw: SimpleNamespace(status=None, error="x", body=b""),
    )
    r = fake.make(_plan())
    result = [
        {"metric": {"instance": "172.18.0.1:29464"}, "value": [0, "1"]},
        {"metric": {"instance": "172.18.0.1:29465"}, "value": [0, "0"]},
    ]
    r.prom = SimpleNamespace(  # type: ignore[assignment]
        query=lambda expr, timeout=0: (SimpleNamespace(status=200), result)
    )
    r.probe_tick()
    readyz = (r.data / "readyz.csv").read_text().splitlines()
    assert readyz[0] == "ts,role,status" and readyz[1].endswith(",web,0")
    up = (r.data / "prom_up.csv").read_text().splitlines()
    assert up[1:] == [
        f"{iso(T0)},observer,0,prometheus",
        f"{iso(T0)},web,1,prometheus",
        f"{iso(T0)},worker,0,prometheus",
    ]


def test_raw_runs_derives_times() -> None:
    from scripts.soak.__main__ import raw_runs

    rows = [
        {
            "run_id": "r1",
            "incident_id": "i1",
            "state": "completed",
            "deadline": iso(T0 + timedelta(minutes=40)),
            "intake_created_at": iso(T0),
            "first_step_at": iso(T0 + timedelta(seconds=90)),
            "last_step_at": iso(T0 + timedelta(minutes=9)),
            "prompt_tokens": "1200",
            "completion_tokens": 30,
        }
    ]
    log = [
        {"ts": iso(T0 + timedelta(seconds=60)), "run_id": "r1", "state": "running"},
        {"ts": iso(T0 + timedelta(minutes=10)), "run_id": "r1", "state": "completed"},
    ]
    (run,) = raw_runs(rows, log)
    assert run["created_at"] == iso(T0)
    assert run["started_at"] == iso(T0 + timedelta(seconds=60))
    assert run["finished_at"] == iso(T0 + timedelta(minutes=10))
    assert run["wall_clock_limit_s"] == 39 * 60
    assert (run["input_tokens"], run["output_tokens"]) == (1200, 30)


def test_signal_status_mapping() -> None:
    assert pg.signal_status("healthy", True) == "ok"
    assert pg.signal_status("degraded", True) == "ok"
    assert pg.signal_status("stale", True) == "stale"
    assert pg.signal_status("no_data", True) == "missing"
    assert pg.signal_status("healthy", False) == "missing"


def test_runner_output_feeds_the_summarizer(fake: Any) -> None:
    """The runner's own events and CSVs are a raw directory the summarizer
    accepts: a simulated whole plan executes every injection."""
    plan = _full_plan()
    r = fake.make(plan)
    (r.data / "plan.json").write_bytes(soak_plan.canonical(plan))
    for _ in range(72 * 60):
        r.plan_tick()
        r.now.advance(60)
    r.event("soak_end")
    (r.data / "meta.json").write_text(json.dumps({"snapshot_at": iso(r.now())}))
    summary = summarize.summarize(r.data)
    assert all(item["executed"] for item in summary["injections"])
    assert summary["window"]["coverage_ok"] is True
    unmet = summary["step4"]["unmet"]
    # no alerts or PG files in this simulation: episodes are unaccounted and
    # the sections that need those files are unpublished, nothing else
    assert all(
        u.startswith(("injection_unaccounted:", "section_unpublished:")) for u in unmet
    )
    assert not any(u.startswith("injection_unexplained") for u in unmet)
    # #486: no Run is ever running here, so A1 ran with its condition unmet;
    # A2 waits for idle, which holds
    items = {item["id"]: item for item in summary["injections"]}
    assert [c["step"] for c in items["A1"]["unmet_conditions"]] == ["A1:kill"]
    assert items["A1"]["unmet_conditions"][0]["condition"] == "run_running"
    assert items["A2"]["unmet_conditions"] == []
    assert not any(u.startswith("condition") for u in unmet)


# --- fault key (S-R2) ---------------------------------------------------------------


def test_soak_flags_match_the_plan() -> None:
    assert set(lab_fault.SOAK_FLAGS) == soak_plan.FLAGS
    assert (
        lab_fault.SOAK_FLAGS["paymentFailure"] == kind_lab.FAULT_FLAGS["paymentFailure"]
    )


def _flags(**defaults: str) -> bytes:
    variants = {
        "paymentFailure": {"off": 0, "100%": 1},
        "adManualGc": {"off": False, "on": True},
        "loadGeneratorFloodHomepage": {"off": 0, "on": 100},
    }
    return json.dumps(
        {
            "flags": {
                name: {"defaultVariant": defaults.get(name, "off"), "variants": v}
                for name, v in variants.items()
            }
        }
    ).encode()


def test_flag_move_is_idempotent_and_refuses_a_third_state() -> None:
    moved = lab_fault.moved(_flags(), "adManualGc", "on")
    assert moved is not None
    assert json.loads(moved)["flags"]["adManualGc"]["defaultVariant"] == "on"
    assert lab_fault.moved(moved, "adManualGc", "on") is None
    assert lab_fault.moved(_flags(), "paymentFailure", "off") is None
    with pytest.raises(SystemExit):
        lab_fault.moved(_flags(paymentFailure="50%"), "paymentFailure", "off")
    with pytest.raises(SystemExit):
        lab_fault.moved(_flags(), "cartFailure", "on")


@pytest.fixture
def fault_repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    repo = tmp_path / "repo"
    pkg = repo / "scripts/soak"
    pkg.mkdir(parents=True)
    (pkg / "lab_fault.py").write_text("import sys\nprint('ARGS', *sys.argv[1:])\n")
    env_file = tmp_path / "fault.env"
    env_file.write_text(f"REPO={repo}\n")
    env = {**os.environ, "OPSPILOT_SOAK_FAULT_ENV": str(env_file)}
    return repo, env


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("flag paymentFailure on ep01", "ARGS flag paymentFailure on ep01"),
        ("flag adManualGc off nz1", "ARGS flag adManualGc off nz1"),
        ("collector-restart", "ARGS collector-restart"),
        ("ping", "pong"),
    ],
)
def test_fault_sh_accepts(fault_repo: Any, command: str, expected: str) -> None:
    _, env = fault_repo
    proc = subprocess.run(
        ["bash", str(SOAK / "fault.sh")],
        capture_output=True,
        text=True,
        env={**env, "SSH_ORIGINAL_COMMAND": command},
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == expected


@pytest.mark.parametrize(
    "command",
    [
        "",
        "flag cartFailure on ep01",
        "flag paymentFailure maybe ep01",
        "flag paymentFailure on ep01;id",
        "flag paymentFailure on ../x",
        "flag paymentFailure on ep01 extra",
        "bash -c id",
        "clock now",
        "flag paymentFailure on $(id)",
        "flag * on ep01",
    ],
)
def test_fault_sh_refuses(fault_repo: Any, command: str) -> None:
    _, env = fault_repo
    proc = subprocess.run(
        ["bash", str(SOAK / "fault.sh")],
        capture_output=True,
        text=True,
        env={**env, "SSH_ORIGINAL_COMMAND": command},
        timeout=30,
    )
    assert proc.returncode == 64, (command, proc.stdout, proc.stderr)
    assert "ARGS" not in proc.stdout


def test_fault_clock_prints_epoch(fault_repo: Any) -> None:
    _, env = fault_repo
    proc = subprocess.run(
        ["bash", str(SOAK / "fault.sh"), "clock"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0
    assert re.match(r"epoch=\d+", proc.stdout)


# --- wiring (S-R1, S-R14) ---------------------------------------------------------


def _soak_compose() -> dict[str, Any]:
    return yaml.safe_load((SOAK / "compose.soak.yaml").read_text())


def test_every_product_service_rotates_logs() -> None:
    product = yaml.safe_load((ROOT / "deploy/compose/compose.yaml").read_text())
    soak = _soak_compose()["services"]
    for name in [*product["services"], "soak-tunnel"]:
        logging = soak[name]["logging"]
        assert logging["driver"] == "json-file"
        assert logging["options"] == {"max-size": "100m", "max-file": "10"}


def test_tunnel_forwards_match_the_lab_ports_and_the_rental_keys() -> None:
    tunnel = _soak_compose()["services"]["soak-tunnel"]
    command = tunnel["command"]
    remote = [
        re.sub(r"\$\{[A-Z_]+:-([^}]*)\}", r"\1", command[i + 1])
        for i, word in enumerate(command)
        if word == "-R"
    ]
    assert all(r.startswith("172.18.0.1:") for r in remote)
    local = [command[i + 1] for i, word in enumerate(command) if word == "-L"]
    listen_ports = [r.split(":")[1] for r in remote]
    assert listen_ports == ["28080", "29464", "29465", "29466"]
    assert [r.split(":", 2)[2] for r in remote] == [
        "web:8080",
        "web:9464",
        "worker:9464",
        "observer:9464",
    ]
    assert local == ["0.0.0.0:19090:127.0.0.1:19090", "0.0.0.0:16686:127.0.0.1:16686"]
    assert {p["host_ip"] for p in tunnel["ports"]} == {"127.0.0.1"}
    assert set(tunnel["networks"]["default"]["aliases"]) == {
        "lab-prometheus",
        "lab-jaeger",
    }
    # summarizer role mapping and the rental's permitlisten/ufw ports agree
    assert sorted(runner.INSTANCE_ROLES) == listen_ports[1:]
    setup = (SOAK / "rental_setup.sh").read_text()
    assert "WEBHOOK_PORT=28080" in setup and "HEALTH_PORTS=(29464 29465 29466)" in setup
    assert 'permitopen="127.0.0.1:19090",permitopen="127.0.0.1:16686"' in setup


def test_lab_port_override_matches_the_tunnel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPSPILOT_LAB_HOST_ADDRESS", "172.18.0.1")
    monkeypatch.setenv(
        "OPSPILOT_LAB_HOST_PORTS", "webhook=28080,web=29464,worker=29465,observer=29466"
    )
    endpoints = kind_lab.lab_endpoints()
    assert endpoints is not None
    assert sorted(e.split(":")[1] for e in endpoints.values()) == [
        "28080",
        "29464",
        "29465",
        "29466",
    ]


def test_shell_scripts_parse() -> None:
    for name in ("fault.sh", "rental_setup.sh"):
        proc = subprocess.run(
            ["bash", "-n", str(SOAK / name)], capture_output=True, text=True, timeout=30
        )
        assert proc.returncode == 0, proc.stderr


# --- #477 round-1 fixes ------------------------------------------------------------


STALLED = {
    "lifecycle": "open",
    "last_transition": None,
    "active_runs": 0,
    "current_run_state": "waiting_human",
    "observations_in_flight": 0,
}


@pytest.mark.parametrize(
    ("row", "faults_active", "soon", "expected"),
    [
        (STALLED, False, True, "before_next_injection_run_waiting_human"),
        (
            {**STALLED, "current_run_state": "failed"},
            False,
            True,
            "before_next_injection_run_failed",
        ),
        (
            {**STALLED, "current_run_state": "blocked"},
            False,
            True,
            "before_next_injection_run_blocked",
        ),
        (
            {**STALLED, "current_run_state": "budget_exhausted"},
            False,
            True,
            "before_next_injection_run_budget_exhausted",
        ),
        (STALLED, False, False, None),  # not before an injection
        (STALLED, True, True, None),  # a fault is still on
        ({**STALLED, "observations_in_flight": 1}, False, True, None),
        ({**STALLED, "current_run_state": "running"}, False, True, None),
        ({**STALLED, "current_run_state": "completed"}, False, True, None),
        ({**STALLED, "lifecycle": "observing_recovery"}, False, True, None),
    ],
)
def test_b1_closes_stalled_incidents_before_the_next_injection(
    row: dict[str, Any], faults_active: bool, soon: bool, expected: str | None
) -> None:
    assert (
        pg.should_close(row, faults_active=faults_active, injection_soon=soon)
        == expected
    )


def test_b1_injection_soon_window(fake: Any) -> None:
    r = fake.make(_plan(EPISODE))  # ep01:on due at T0+600
    assert r.injection_soon({}) is True
    r.now.advance(-1)  # 601 s before
    assert r.injection_soon({}) is False
    r.now.advance(1 + 601)  # due passed
    assert r.injection_soon({}) is False


def test_b1_close_candidates_carry_run_state_and_observations() -> None:
    sql = pg.close_candidates_sql(T0)
    assert "current_run_state" in sql and "observations_in_flight" in sql


def test_b2_failed_off_keeps_the_flag_on_and_holds_injections(fake: Any) -> None:
    second = {
        **EPISODE,
        "id": "ep02",
        "at_s": 2000,
        "steps": [
            {**EPISODE["steps"][0], "id": "ep02:on", "at_s": 2000},
            {**EPISODE["steps"][1], "id": "ep02:off", "after": "ep02:on"},
        ],
    }
    r = fake.make(_plan(EPISODE, second))
    r.now.advance(600)
    r.plan_tick()  # ep01:on ok
    r.now.advance(900)
    fake.results.extend([Proc(1, "", "ssh down")] * 3)
    r.plan_tick()  # ep01:off fails three times
    steps = _steps(r)
    assert steps[-1]["step"] == "ep01:off" and steps[-1]["status"] == "restore_failed"
    assert "ep01:off" not in runner.finished_steps(list(read_jsonl(r.events_path)))
    assert runner.flags_on(r.steps, runner.finished_steps(_steps(r))) == ["ep01"]
    calls = len(fake.calls)
    r.now.advance(10)
    r.plan_tick()  # within the retry back-off: nothing
    assert len(fake.calls) == calls
    r.now.advance(runner.RESTORE_RETRY_S)
    fake.results.extend([Proc(1, "", "ssh down")] * 3)
    r.now.advance(2000 - (600 + 900 + 10 + runner.RESTORE_RETRY_S) - 60)
    r.plan_tick()
    # ep02:on is due but held while ep01 is still on; only ep01:off was tried
    assert all(c[-1] == "flag paymentFailure off ep01" for c in fake.calls[calls:])
    r.now.advance(runner.RESTORE_RETRY_S + 60)
    r.plan_tick()  # ep01:off now succeeds, then ep02:on runs
    assert fake.calls[-2][-1] == "flag paymentFailure off ep01"
    assert fake.calls[-1][-1] == "flag paymentFailure on ep02"
    assert ("injection_off", "ep01") in _marks(r)


def test_b2_held_injection_is_skipped_with_a_reason(fake: Any) -> None:
    second = {
        **EPISODE,
        "id": "ep02",
        "at_s": 2000,
        "steps": [
            {**EPISODE["steps"][0], "id": "ep02:on", "at_s": 2000},
            {**EPISODE["steps"][1], "id": "ep02:off", "after": "ep02:on"},
        ],
    }
    r = fake.make(_plan(EPISODE, second))
    r.now.advance(600)
    r.plan_tick()
    fake.results.extend([Proc(1, "", "x")] * 100)
    r.now.advance(2000 - 600 + runner.MISSED_AFTER_S + 1)
    r.plan_tick()
    skipped = [e for e in read_jsonl(r.events_path) if e["type"] == "injection_skipped"]
    assert skipped and skipped[0]["injection_id"] == "ep02"
    assert "still on (ep01)" in skipped[0]["reason"]


def test_b2_summary_lists_injections_left_on(tmp_path: Path) -> None:
    plan = _plan(EPISODE)
    plan.update(product_alertname="X", expected_deployments=[])
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "plan.json").write_bytes(soak_plan.canonical(plan))
    (raw / "meta.json").write_text("{}")
    rows = [
        {"ts": iso(T0), "type": "soak_start"},
        {
            "ts": iso(T0 + timedelta(seconds=600)),
            "type": "injection_on",
            "injection_id": "ep01",
        },
        {"ts": iso(T0 + timedelta(hours=72)), "type": "soak_end"},
    ]
    (raw / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    summary = summarize.summarize(raw)
    assert summary["stuck_work"]["injections_left_on"] == {"count": 1, "ids": ["ep01"]}
    assert "injection_left_on:ep01" in summary["step4"]["unmet"]


def test_m1_failed_prometheus_query_writes_down_rows(
    fake: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runner,
        "http",
        lambda url, **kw: SimpleNamespace(status=200, error=None, body=b""),
    )
    r = fake.make(_plan())
    r.prom = SimpleNamespace(  # type: ignore[assignment]
        query=lambda expr, timeout=0: (SimpleNamespace(status=None), None)
    )
    r.probe_tick()
    rows = (r.data / "prom_up.csv").read_text().splitlines()
    assert rows[0] == "ts,role,up,source"
    assert rows[1:] == [
        f"{iso(T0)},{role},0,query_failed" for role in ("observer", "web", "worker")
    ]


def test_m2_outage_past_the_planned_window_is_an_interruption(fake: Any) -> None:
    r = fake.make(_plan())
    r.event("step", step="A4:stop", status="ok")
    r.tunnel_transition(False)  # planned: no interruption
    r.now.advance(runner.TUNNEL_STOP_S + runner.TUNNEL_RECONNECT_GRACE_S - 1)
    r.tunnel_transition(False)
    assert "env_interruption_start" not in [
        e["type"] for e in read_jsonl(r.events_path)
    ]
    r.now.advance(2)  # no A4:start record (runner restarted), window over
    r.tunnel_transition(False)
    r.tunnel_transition(False)
    kinds = [e["type"] for e in read_jsonl(r.events_path)]
    assert kinds.count("env_interruption_start") == 1
    r.tunnel_transition(True)
    assert [e["type"] for e in read_jsonl(r.events_path)][-1] == "env_interruption_end"


def test_l1_l2_rental_setup_rules() -> None:
    setup = (SOAK / "rental_setup.sh").read_text()
    assert "exactly one line" in setup
    assert "AllowStreamLocalForwarding no" in setup
    assert "PermitOpen 127.0.0.1:19090 127.0.0.1:16686" in setup
    assert "PermitListen $permit_listen" in setup


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-11-05T01:02:03.123456789Z", "2026-11-05T01:02:03.123456Z"),
        ("2026-11-05T01:02:03.123456789+00:00", "2026-11-05T01:02:03.123456Z"),
        ("2026-11-05T01:02:03.5-08:00", "2026-11-05T09:02:03.500000Z"),
        ("2026-11-05T01:02:03Z", "2026-11-05T01:02:03Z"),
    ],
)
def test_normalize_ts_keeps_the_offset(raw: str, expected: str) -> None:
    assert runner.normalize_ts(raw) == expected


def _raw(tmp_path: Path, plan: dict[str, Any], rows: list[dict[str, Any]]) -> Path:
    plan = {**plan, "product_alertname": "X", "expected_deployments": []}
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "plan.json").write_bytes(soak_plan.canonical(plan))
    (raw / "meta.json").write_text("{}")
    (raw / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return raw


def test_486_summary_shows_steps_run_with_their_condition_unmet(
    tmp_path: Path,
) -> None:
    """#486: a conditional step that ran after its maximum wait is listed on
    its injection, a met one is not, and step 4 is unchanged."""
    met = {**KILL, "id": "A9", "steps": [{**KILL["steps"][0], "id": "A9:kill"}]}
    pg = {
        "id": "A2",
        "kind": "agent_fault",
        "at_s": 5000,
        "steps": [
            {
                "id": "A2:restart",
                "action": "pg_restart",
                "at_s": 5000,
                "condition": "idle",
            }
        ],
    }
    plan = _plan(KILL, met, pg)

    def at(seconds: int) -> str:
        return iso(T0 + timedelta(seconds=seconds))

    def step(seconds: int, step_id: str, status: str, **extra: Any) -> dict[str, Any]:
        return {
            "ts": at(seconds),
            "type": "step",
            "step": step_id,
            "status": status,
            **extra,
        }

    rows = [
        {"ts": at(0), "type": "soak_start"},
        step(1000, "A1:kill", "waiting", condition="run_running"),
        step(2800, "A1:kill", "retry", condition="run_running", condition_met=False),
        step(2810, "A1:kill", "ok", condition="run_running", condition_met=False),
        {"ts": at(2810), "type": "injection_on", "injection_id": "A1"},
        step(2840, "A1:recover", "ok"),
        {"ts": at(2840), "type": "injection_off", "injection_id": "A1"},
        step(1000, "A9:kill", "ok", condition="run_running", condition_met=True),
        {"ts": at(1000), "type": "injection_on", "injection_id": "A9"},
        step(6800, "A2:restart", "failed", condition="idle", condition_met=False),
        {
            "ts": at(6800),
            "type": "injection_skipped",
            "injection_id": "A2",
            "reason": "x",
        },
        {"ts": at(72 * 3600), "type": "soak_end"},
    ]
    summary = summarize.summarize(_raw(tmp_path, plan, rows))
    items = {item["id"]: item for item in summary["injections"]}
    assert items["A1"]["unmet_conditions"] == [
        {"step": "A1:kill", "condition": "run_running", "ts": at(2800), "status": "ok"}
    ]
    assert items["A9"]["unmet_conditions"] == []
    # a failed step whose condition was unmet is still reported
    assert items["A2"]["unmet_conditions"] == [
        {"step": "A2:restart", "condition": "idle", "ts": at(6800), "status": "failed"}
    ]
    # reported, not a step-4 code
    assert not any("A1" in u or "A9" in u for u in summary["step4"]["unmet"])


NOISE_ITEM = {
    "id": "nz1",
    "kind": "noise",
    "flag": "adManualGc",
    "at_s": 7200,
    "duration_s": 900,
    "steps": [
        {"id": "nz1:on", "action": "flag_on", "at_s": 7200, "flag": "adManualGc"},
        {
            "id": "nz1:off",
            "action": "flag_off",
            "after": "nz1:on",
            "delay_s": 900,
            "flag": "adManualGc",
        },
    ],
}


def test_487_summary_lists_work_opened_inside_a_noise_window(tmp_path: Path) -> None:
    """#487: a delivery that opened a Run or a handoff between a noise
    toggle's on and its off plus the slack is listed on the noise injection
    and under ``stuck_work``; outside the window, ``attached`` and null
    outcomes are not; step 4 is unchanged."""
    plan = _plan(
        {**EPISODE, "category": "episode"}, {**NOISE_ITEM, "category": "noise"}
    )
    plan.update(product_alertname="X", expected_deployments=[])
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "plan.json").write_bytes(soak_plan.canonical(plan))
    (raw / "meta.json").write_text("{}")

    def at(seconds: float) -> str:
        return iso(T0 + timedelta(seconds=seconds))

    on, off = 7200, 8100
    slack = summarize.NOISE_SLACK_S
    events = [
        {"ts": at(0), "type": "soak_start"},
        {"ts": at(600), "type": "injection_on", "injection_id": "ep01"},
        {"ts": at(1500), "type": "injection_off", "injection_id": "ep01"},
        {"ts": at(on), "type": "injection_on", "injection_id": "nz1"},
        {"ts": at(off), "type": "injection_off", "injection_id": "nz1"},
        {"ts": at(72 * 3600), "type": "soak_end"},
    ]
    (raw / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in events))

    def delivery(name: str, seconds: float, outcome: str | None) -> dict[str, Any]:
        return {
            "delivery_id": name,
            "fingerprint": f"fp-{name}",
            "received_at": at(seconds),
            "outcome": outcome,
            "outcome_at": at(seconds) if outcome else None,
            "run_id": f"run-{name}" if outcome == "run" else None,
            "incident_id": f"inc-{name}" if outcome else None,
        }

    deliveries = [
        delivery("before", on - 1, "run"),
        delivery("run_in", on + 60, "run"),
        delivery("attached_in", on + 120, "attached"),
        delivery("null_in", on + 180, None),
        delivery("handoff_slack", off + slack, "handoff"),
        delivery("after", off + slack + 1, "run"),
    ]
    (raw / "deliveries.jsonl").write_text(
        "".join(json.dumps(d) + "\n" for d in deliveries)
    )
    summary = summarize.summarize(raw)
    items = {item["id"]: item for item in summary["injections"]}
    opened = items["nz1"]["opened_work"]
    assert [o["delivery_id"] for o in opened] == ["run_in", "handoff_slack"]
    assert opened[0] == {
        "delivery_id": "run_in",
        "received_at": at(on + 60),
        "outcome": "run",
        "run_id": "run-run_in",
        "incident_id": "inc-run_in",
        "episode": None,
    }
    assert items["ep01"]["opened_work"] is None
    assert summary["stuck_work"]["noise_opened_work"] == {
        "count": 2,
        "ids": ["handoff_slack", "run_in"],
    }
    assert not any("nz1" in u or "noise" in u for u in summary["step4"]["unmet"])


def test_487_noise_never_turned_off_has_an_open_window(tmp_path: Path) -> None:
    plan = _plan({**NOISE_ITEM, "category": "noise"})
    plan.update(product_alertname="X", expected_deployments=[])
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "plan.json").write_bytes(soak_plan.canonical(plan))
    (raw / "meta.json").write_text("{}")
    late = iso(T0 + timedelta(hours=72, seconds=601))
    events = [
        {"ts": iso(T0), "type": "soak_start"},
        {
            "ts": iso(T0 + timedelta(seconds=7200)),
            "type": "injection_on",
            "injection_id": "nz1",
        },
        {"ts": iso(T0 + timedelta(hours=72)), "type": "soak_end"},
    ]
    (raw / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in events))
    (raw / "deliveries.jsonl").write_text(
        json.dumps(
            {"delivery_id": "D1", "received_at": late, "outcome": "run", "run_id": "R1"}
        )
        + "\n"
    )
    summary = summarize.summarize(raw)
    assert summary["stuck_work"]["noise_opened_work"] == {"count": 1, "ids": ["D1"]}
    assert "injection_left_on:nz1" in summary["step4"]["unmet"]


def test_487_noise_delivery_linked_to_an_episode_names_it(tmp_path: Path) -> None:
    plan = _plan(
        {**EPISODE, "category": "episode"}, {**NOISE_ITEM, "category": "noise"}
    )
    plan.update(product_alertname="X", expected_deployments=[])
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "plan.json").write_bytes(soak_plan.canonical(plan))
    (raw / "meta.json").write_text("{}")

    def at(seconds: float) -> str:
        return iso(T0 + timedelta(seconds=seconds))

    events = [
        {"ts": at(0), "type": "soak_start"},
        {"ts": at(600), "type": "injection_on", "injection_id": "ep01"},
        {"ts": at(7200), "type": "injection_on", "injection_id": "nz1"},
        {"ts": at(8100), "type": "injection_off", "injection_id": "nz1"},
        {"ts": at(72 * 3600), "type": "soak_end"},
    ]
    (raw / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in events))
    (raw / "alerts.jsonl").write_text(
        json.dumps({"alertname": "X", "fingerprint": "f", "starts_at": at(7300)}) + "\n"
    )
    (raw / "deliveries.jsonl").write_text(
        json.dumps(
            {
                "delivery_id": "D1",
                "fingerprint": "f",
                "received_at": at(7310),
                "outcome": "run",
                "outcome_at": at(7310),
                "run_id": "R1",
                "incident_id": "I1",
            }
        )
        + "\n"
    )
    summary = summarize.summarize(raw)
    items = {item["id"]: item for item in summary["injections"]}
    assert items["ep01"]["disposition"] == "run"
    assert items["nz1"]["opened_work"][0]["episode"] == "ep01"
