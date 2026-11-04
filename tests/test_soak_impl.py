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


def _items(kind: str) -> list[dict[str, Any]]:
    return [i for i in soak_plan.build_plan()["items"] if i["kind"] == kind]


def test_plan_episodes() -> None:
    episodes = _items("episode")
    assert len(episodes) == 24
    starts = [e["at_s"] for e in episodes]
    assert all(b - a == 3 * 3600 for a, b in zip(starts, starts[1:], strict=False))
    long = {e["id"] for e in episodes if e["duration_s"] == 75 * 60}
    assert long == {"ep08", "ep16"}
    assert all(e["duration_s"] == 15 * 60 for e in episodes if e["id"] not in long)
    assert all(e["flag"] == "paymentFailure" for e in episodes)
    # the last episode's 90 min cycle ends inside 72 h
    assert starts[-1] + 90 * 60 < 72 * 3600


def test_plan_noise_and_agent_faults() -> None:
    noise = _items("noise")
    assert sorted(n["flag"] for n in noise) == [
        "adManualGc",
        "adManualGc",
        "loadGeneratorFloodHomepage",
        "loadGeneratorFloodHomepage",
    ]
    busy = [(e["at_s"], e["at_s"] + 90 * 60) for e in _items("episode")]
    for n in noise:
        assert not any(
            a < n["at_s"] + n["duration_s"] and n["at_s"] < b for a, b in busy
        )
    agent = _items("agent_fault")
    assert [a["id"] for a in agent] == ["A1", "A2", "A3", "A4", "A5"]
    times = [a["at_s"] for a in agent]
    assert all(12 * 3600 <= t <= 60 * 3600 for t in times)
    assert all(b - a >= 2 * 3600 for a, b in zip(times, times[1:], strict=False))
    by_id = {a["id"]: a for a in agent}
    episodes = {e["id"]: e for e in _items("episode")}
    a3, ep = by_id["A3"], episodes[by_id["A3"]["overlaps"]]
    assert ep["at_s"] <= a3["at_s"] < ep["at_s"] + ep["duration_s"]
    assert a3["duration_s"] == 20 * 60 and by_id["A4"]["duration_s"] == 10 * 60
    assert by_id["A1"]["steps"][0]["condition"] == "run_running"
    assert by_id["A2"]["steps"][0]["condition"] == "idle"


def test_plan_steps_well_formed() -> None:
    plan = soak_plan.build_plan()
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
    sha = soak_plan.freeze(path)
    assert soak_plan.freeze(path) == sha
    assert soak_plan.load(path)[1] == sha
    path.write_text(path.read_text().replace('"ep01"', '"epXX"'))
    with pytest.raises(SystemExit):
        soak_plan.freeze(path)


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
    return {"schema": soak_plan.SCHEMA, "duration_s": 72 * 3600, "items": list(items)}


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
        r.t0()  # T0 is the first tick
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
    return [e for e in read_jsonl(r.events_path) if e["kind"] == "step"]


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


def test_runner_is_restart_safe(fake: Any) -> None:
    r = fake.make(_plan(EPISODE))
    r.now.advance(700)
    r.plan_tick()
    t0 = r.t0()
    again = fake.make(_plan(EPISODE))
    again.now.advance(800)
    again.plan_tick()
    assert again.t0() == t0
    assert len(fake.calls) == 1  # ep01:on is not repeated
    other = runner.Runner(
        config=r.config, data=r.data, plan=r.plan, plan_sha256="other"
    )
    with pytest.raises(SystemExit):
        other.t0()


def test_runner_retries_then_records_failure(fake: Any) -> None:
    r = fake.make(_plan(EPISODE))
    fake.results.extend([Proc(1, "", "boom")] * 3)
    r.now.advance(700)
    r.plan_tick()
    statuses = [s["status"] for s in _steps(r)]
    assert statuses == ["retry", "retry", "failed"]
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
    event = [e for e in read_jsonl(r.events_path) if e["kind"] == "operator"][0]
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


def _write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_summary_of_a_tiny_soak(tmp_path: Path) -> None:
    plan = _plan(EPISODE)
    (tmp_path / "plan.json").write_bytes(soak_plan.canonical(plan))
    sha = soak_plan.load(tmp_path / "plan.json")[1]
    on, off = T0 + timedelta(seconds=600), T0 + timedelta(seconds=1500)
    end = T0 + timedelta(hours=72, minutes=1)
    _write(
        tmp_path / "events.jsonl",
        [
            {"at": iso(T0), "kind": "start", "t0": iso(T0), "plan_sha256": sha},
            {"at": iso(on), "kind": "step", "step": "ep01:on", "status": "ok"},
            {"at": iso(off), "kind": "step", "step": "ep01:off", "status": "ok"},
            {"at": iso(end), "kind": "end", "t0": iso(T0)},
        ],
    )
    _write(
        tmp_path / "probe.jsonl",
        [
            {"at": iso(T0), "target": "web", "status": 200},
            {
                "at": iso(T0),
                "target": "tunnel",
                "status": 200,
                "opspilot_up": {"172.18.0.1:29464": "1"},
            },
        ],
    )
    _write(tmp_path / "lab.jsonl", [{"at": iso(T0), "deployments": {"checkout": 1}}])
    _write(
        tmp_path / "resources-da-server.jsonl",
        [
            {
                "at": iso(T0),
                "host": "da-server",
                "mem_available_kib": 100,
                "load1": 0.5,
                "disk_free_bytes": 10,
                "containers": [
                    {"name": "opspilot-web-1", "cpu_pct": 1.0, "mem_bytes": 5}
                ],
            },
        ],
    )
    fired = on + timedelta(minutes=4)
    (tmp_path / "pg.json").write_text(
        json.dumps(
            {
                "exported_at": iso(end + timedelta(minutes=31)),
                "runs": [
                    {
                        "run_id": "r1",
                        "state": "completed",
                        "prompt_tokens": 1000,
                        "completion_tokens": 100,
                        "prompt_cache_hit_tokens": 600,
                        "prompt_cache_miss_tokens": 400,
                        "model_steps": 3,
                    }
                ],
                "deliveries": [
                    {
                        "status": "firing",
                        "alertname": summarize.ALERTNAME,
                        "outcome": "created",
                        "starts_at": iso(fired),
                        "received_at": iso(fired + timedelta(seconds=20)),
                        "incident_id": "i1",
                    }
                ],
                "sessions": [],
                "postmortem_tokens": [],
            }
        )
    )
    (tmp_path / "cost.json").write_text(
        json.dumps(
            {
                "deepseek_cny_per_mtok": {
                    "input_cache_hit": 1,
                    "input_cache_miss": 2,
                    "output": 3,
                },
                "price_source": "test",
            }
        )
    )
    out = summarize.summarize(tmp_path)
    assert out["plan_matches_start"] is True
    assert out["window"]["coverage_ok"] is True
    assert out["faults"][0]["status"] == "executed"
    assert out["availability"]["end_to_end"][0]["within_10_min"] is True
    assert out["stuck_work"]["injections"][0]["destination"] == "run"
    assert out["stuck_work"]["export_settled"] is True
    assert out["cost"]["deepseek_cny_estimate"] == pytest.approx(
        (600 * 1 + 400 * 2 + 100 * 3) / 1e6
    )
    assert out["step4"]["achieved"] is True, out["step4"]
    assert out["step4"]["passes_flipped"] is False
    assert set(out["inputs"]) >= {"plan.json", "events.jsonl", "pg.json"}


def test_interruptions_exclude_the_planned_tunnel_stop() -> None:
    stop, start = T0 + timedelta(hours=40), T0 + timedelta(hours=40, minutes=10)
    events = [
        {"kind": "step", "step": "A4:stop", "status": "ok", "at": iso(stop)},
        {"kind": "step", "step": "A4:start", "status": "ok", "at": iso(start)},
        {
            "kind": "interruption",
            "start": iso(T0),
            "end": iso(T0 + timedelta(hours=1)),
            "reason": "rental reboot",
        },
    ]
    probes = [
        {"at": iso(stop + timedelta(seconds=30)), "target": "tunnel", "status": None},
        {"at": iso(start + timedelta(seconds=60)), "target": "tunnel", "status": 200},
        {"at": iso(T0 + timedelta(hours=50)), "target": "tunnel", "status": None},
        {
            "at": iso(T0 + timedelta(hours=50, minutes=5)),
            "target": "tunnel",
            "status": 200,
        },
    ]
    assert summarize.interruption_seconds(events, probes) == pytest.approx(3600 + 300)


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
    assert sorted(summarize.INSTANCE_ROLES) == listen_ports[1:]
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
