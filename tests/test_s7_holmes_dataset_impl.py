"""S7 HolmesGPT dataset runner: offline implementation-side checks (plan,
egress 503 injection, attempt projection, summary, judge inputs, the
in-process ``run_attempt`` with fake docker). No docker, kind or model; the
F14 raw outputs are used only when the git-ignored raw directory exists."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from opspilot.evaluation.model import load_scenarios_v2
from opspilot.investigation import limits
from opspilot.tools.registry import MAX_REQUEST_TIMEOUT_SECONDS
from scripts.eval_live_lab import UNREACHABLE_URLS
from scripts.holmes_baseline import config as hconfig
from scripts.holmes_baseline import dataset as ds
from scripts.holmes_baseline import relay
from scripts.holmes_baseline import run as hrun

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "evals/datasets/dev-v2"
RAW = Path(
    os.environ.get(
        "F14_HOLMES_RAW_DIR",
        str(Path.home() / "dev/AI/production-ops-agent/tmp/f14-holmes"),
    )
)
SUBMITTED = datetime(2026, 11, 10, 18, 30, tzinfo=UTC)


def loaded() -> dict[str, Any]:
    return {s.scenario_id: (s, a) for s, a in load_scenarios_v2(DATASET)}


def injections() -> dict[str, ds.InjectionPlan]:
    return {sid: ds.injection_plan(s, a) for sid, (s, a) in loaded().items()}


# --- plan


def test_lab_urls_are_the_harness_ones() -> None:
    assert ds.PROMETHEUS_URL == hrun.PROMETHEUS_URL
    assert ds.JAEGER_URL == hrun.JAEGER_URL
    assert ds.RELAY == hrun.RELAY and ds.CAPTURE == hrun.CAPTURE


def test_applicability_follows_v2_r12() -> None:
    for sid, (spec, answer) in loaded().items():
        p = injections()[sid]
        assert p.applicable == (spec.entry == "question"), sid
        if spec.entry == "alertmanager":
            assert p.upstream == "no_upstream_entry"
        elif answer.category == "model_failure":
            assert p.upstream == "same_proxy_injection"
        else:
            assert p.upstream == "comparable"


def test_holmes_side_injectors_follow_the_answer() -> None:
    p = injections()
    assert p["dv2-c39f60"].egress_extra() == ["--inject-503", "always"]
    assert p["dv2-fa0e58"].egress_extra() == ["--inject-503", "first"]
    assert p["dv2-fcd9eb"].source == "jaeger"
    assert p["dv2-7261fe"].source == "prometheus"
    for sid in ("dv2-2b214b", "dv2-8e9455", "dv2-8b262a"):
        assert p[sid].source is None and p[sid].egress_extra() == []
    assert p["dv2-bec37f"].lab_side == ("flagd", "otlp_span")


def test_unreachable_source_uses_opspilots_url_and_bypasses_the_egress_proxy() -> None:
    p = injections()
    assert p["dv2-fcd9eb"].jaeger_url() == UNREACHABLE_URLS["jaeger"]
    assert p["dv2-fcd9eb"].prometheus_url() == ds.PROMETHEUS_URL
    assert p["dv2-7261fe"].prometheus_url() == UNREACHABLE_URLS["prometheus"]
    assert p["dv2-fcd9eb"].no_proxy(["f14-lab-relay"]) == "f14-lab-relay,127.0.0.1"
    assert p["dv2-2b214b"].no_proxy(["f14-lab-relay"]) == "f14-lab-relay"
    spec, answer = loaded()["dv2-fcd9eb"]
    plan = ds.holmes_plan(spec, answer, submitted_at=SUBMITTED)
    assert plan["no_proxy"] == "f14-lab-relay,127.0.0.1"
    [endpoint] = plan["config"]["toolsets"]["jaeger"]["config"]["endpoints"]
    assert endpoint["hosts"] == ["127.0.0.1:9"]


def test_budget_is_opspilots_frozen_values() -> None:
    budget = ds.budget()
    assert budget["max_steps"] == limits.MAX_MODEL_REQUESTS_PER_RUN
    assert budget["run_wall_seconds"] == limits.RUN_WALL_SECONDS
    assert budget["max_output_tokens"] == limits.MAX_OUTPUT_TOKENS
    assert budget["tool_timeout_seconds"] == MAX_REQUEST_TIMEOUT_SECONDS
    assert int(hconfig.MODEL_ENV["OVERRIDE_MAX_CONTENT_SIZE"]) == (
        limits.MAX_CONTEXT_TOKENS
    )
    assert float(hconfig.MODEL_ENV["LLM_REQUEST_TIMEOUT"]) == (
        limits.MODEL_REQUEST_TIMEOUT_SECONDS
    )


def test_read_only_check() -> None:
    get = "t holmes GET /api/v1/query 200 1 2 0.1"
    other = "t opspilot POST /api/v1/query 200 1 2 0.1"
    assert ds.read_only({}, [get, other])
    assert not ds.read_only({}, ["t holmes PUT /x 403 1 2 0.1"])
    ok = {"tool_calls": [{"description": "HTTP GET http://x/api/services"}]}
    bad = {"tool_calls": [{"description": "HTTP POST http://x/api/services"}]}
    assert ds.read_only(ok, []) and not ds.read_only(bad, [])


# --- egress 503 injection


def test_inject503_modes() -> None:
    always, first, none = (relay.Inject503(m) for m in ("always", "first", None))
    assert [always.take() for _ in range(3)] == [True, True, True]
    assert [first.take() for _ in range(3)] == [True, False, False]
    assert [none.take() for _ in range(3)] == [False, False, False]
    with pytest.raises(ValueError):
        relay.Inject503("sometimes")


async def _connect(port: int, target: str) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    writer.close()
    return head.split(b"\r\n", 1)[0]


def test_egress_proxy_answers_503_then_tunnels_for_first(
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def scenario(mode: str) -> list[bytes]:
        async def upstream_handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            writer.close()

        upstream = await asyncio.start_server(upstream_handler, "127.0.0.1", 0)
        target = f"127.0.0.1:{upstream.sockets[0].getsockname()[1]}"
        inject = relay.Inject503(mode)
        proxy = await asyncio.start_server(
            lambda r, w: relay.handle_connect(r, w, frozenset({target}), inject),
            "127.0.0.1",
            0,
        )
        port = proxy.sockets[0].getsockname()[1]
        lines = [await _connect(port, target) for _ in range(2)]
        lines.append(await _connect(port, "api.example.com:443"))
        await asyncio.sleep(0.1)
        proxy.close()
        upstream.close()
        return lines

    assert asyncio.run(scenario("first")) == [
        b"HTTP/1.1 503 Service Unavailable",
        b"HTTP/1.1 200 Connection established",
        b"HTTP/1.1 403 Forbidden",
    ]
    logged = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert ds.egress_summary(logged)["injected_503"] == 1
    assert (
        asyncio.run(scenario("always"))[:2] == [b"HTTP/1.1 503 Service Unavailable"] * 2
    )
    summary = ds.egress_summary(
        [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    )
    assert summary["injected_503"] == 2
    assert summary["allowed"] == 0
    assert summary["denied"] == ["target api.example.com:443"]


def test_setup_passes_injection_and_kind_node(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(hrun, "docker", lambda *a, **k: calls.append(a) or "")
    monkeypatch.setattr(hrun.time, "sleep", lambda _: None)
    hrun.setup(egress_extra=["--inject-503", "first"], kind_node="lab2-control-plane")
    [egress] = [c for c in calls if hrun.EGRESS in c and "run" in c]
    assert egress[-4:] == (
        "--allow",
        f"{hconfig.MODEL_HOST}:443",
        "--inject-503",
        "first",
    )
    [relay_run] = [c for c in calls if hrun.RELAY in c and "run" in c]
    assert "9090=lab2-control-plane:30090" in relay_run


# --- attempt summary, summarize, judge inputs


def fake_output(**extra: Any) -> dict[str, Any]:
    return {
        "result": "## Report\npayment Charge fails",
        "num_llm_calls": 3,
        "tool_calls": [
            {"tool_name": "jaeger_request", "result": {"status": "success"}},
            {
                "tool_name": "execute_prometheus_range_query",
                "result": {"status": "error"},
            },
        ],
        "exit_code": 0,
        "timed_out": False,
        **extra,
    }


def write_attempt(
    root: Path,
    sid: str,
    attempt_id: str,
    started: float,
    *,
    output: dict[str, Any] | None = None,
    mode: str = "real_model",
    image_id: str = "sha256:img",
    lines: tuple[str, ...] = ("t holmes GET /api/v1/query_range?x 200 1 2 0.1",),
) -> dict[str, Any]:
    spec, answer = loaded()[sid]
    output = fake_output() if output is None else output
    plan = ds.holmes_plan(spec, answer, submitted_at=SUBMITTED)
    row = ds.result_row(
        spec,
        answer,
        attempt_id=attempt_id,
        plan=plan,
        holmes_output=output,
        proxy_log_lines=list(lines),
    )
    directory = root / attempt_id
    (directory / "out").mkdir(parents=True)
    raw = json.dumps(output).encode()
    (directory / ds.RAW_FILE).write_bytes(raw)
    summary = ds.attempt_summary(
        row=row,
        plan=plan,
        slot=None,
        round_id="r1",
        mode=mode,
        output=output,
        proxy_lines=list(lines),
        egress=[],
        started=started,
        finished=started + 10,
        image_id=image_id,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        pre_run=None,
        balance=None,
    )
    (directory / ds.SUMMARY_FILE).write_text(json.dumps(summary))
    return summary


def test_attempt_summary_keeps_no_report_text(tmp_path: Path) -> None:
    summary = write_attempt(tmp_path, "dv2-2b214b", "a1", 100.0)
    assert "payment Charge fails" not in json.dumps(summary)
    assert (
        summary["report_sha256"]
        == hashlib.sha256(b"## Report\npayment Charge fails").hexdigest()
    )
    assert summary["final_state"] == "completed"
    assert summary["checks"] == {"read_only": True, "completed": True}
    assert summary["outcome"]["proxied_queries_ok"] == 1
    assert summary["tool_errors"] == 1
    assert summary["window"] == {
        "start": "2026-11-10T18:25:00Z",
        "end": "2026-11-10T18:30:00Z",
    }


def test_summary_slots_denominators_and_judge_inputs(tmp_path: Path) -> None:
    attempts = tmp_path / "attempts"
    write_attempt(attempts, "dv2-2b214b", "a2", 200.0)
    write_attempt(attempts, "dv2-2b214b", "a1", 100.0)
    write_attempt(
        attempts,
        "dv2-c39f60",
        "m1",
        300.0,
        output={"exit_code": 1, "timed_out": False, "error": "503"},
        lines=("t holmes POST /api/v1/admin 403 1 2 0.1",),
    )
    (attempts / "f14-holmes-run1").mkdir()  # F14 raw dirs share the root
    doc = ds.summarize(DATASET, attempts, n=3)
    assert doc["schema"] == ds.ROUND_SCHEMA and doc["system"] == "holmes"
    assert doc["dataset"].startswith("dev-v2:")
    assert doc["upstream"] == ds.upstream_dimension()
    assert doc["image_id"] == "sha256:img"
    alert = [s for s in doc["slots"] if s["entry"] == "alertmanager"]
    assert alert and all(
        s["status"] == "not_applicable" and not s["in_denominator"] for s in alert
    )
    payment = [s for s in doc["slots"] if s["scenario_id"] == "dv2-2b214b"]
    assert [s["attempt_id"] for s in payment] == ["a1", "a2", None]
    assert payment[2]["status"] == "not_started"
    [model] = [
        s
        for s in doc["slots"]
        if s["scenario_id"] == "dv2-c39f60" and s["status"] == "ran"
    ]
    assert model["final_state"] == "failed" and model["final_state_expected"] is True
    question_cap = sum(
        1
        for s, a in loaded().values()
        if a.category != "regression" and s.entry == "question"
    )
    cap, reg = doc["totals"]["capability"], doc["totals"]["regression"]
    assert cap["slots"] == question_cap * 3
    assert cap["not_applicable"] == (12 - question_cap) * 3
    assert cap["read_only"] == 0 and cap["started"] == 1
    assert reg["slots"] == 18 and reg["started"] == 2 and reg["completed"] == 2

    out = tmp_path / "judge" / "holmes-inputs.json"
    inputs = ds.judge_inputs(doc, attempts, out)
    assert inputs["schema"] == "opspilot-holmes-judge-inputs-1"
    by_id = {e["attempt_id"]: e for e in inputs["attempts"]}
    assert set(by_id) == {"a1", "a2", "m1"}
    assert by_id["a1"]["slot"] == 1 and by_id["a2"]["slot"] == 2
    assert by_id["m1"]["read_only"] is False
    assert (out.parent / by_id["a1"]["holmes_json"]).resolve() == (
        attempts / "a1" / ds.RAW_FILE
    ).resolve()


def retarget(root: Path, sid: str) -> None:
    """An attempt summary claiming an alert-entry scenario (no plan exists)."""
    write_attempt(root, "dv2-2b214b", "x", 1.0)
    path = root / "x" / ds.SUMMARY_FILE
    path.write_text(json.dumps({**json.loads(path.read_text()), "scenario_id": sid}))


@pytest.mark.parametrize(
    "setup,code",
    [
        (
            lambda r: [
                write_attempt(r, "dv2-2b214b", f"a{i}", float(i)) for i in range(4)
            ],
            "REDRAW_DETECTED:dv2-2b214b",
        ),
        (lambda r: retarget(r, "dv2-30921c"), "NOT_APPLICABLE_ATTEMPT:dv2-30921c"),
        (
            lambda r: write_attempt(r, "dv2-2b214b", "x", 1.0, mode="capture_model"),
            "ATTEMPT_NOT_REAL:x",
        ),
        (
            lambda r: (
                write_attempt(r, "dv2-2b214b", "x", 1.0),
                write_attempt(r, "dv2-455c6d", "y", 2.0, image_id="sha256:arm"),
            ),
            "MANIFEST_DRIFT:image",
        ),
        (
            lambda r: (
                write_attempt(r, "dv2-2b214b", "x", 1.0),
                (r / "x" / ds.RAW_FILE).write_text("{}"),
            ),
            "RAW_MISMATCH:x",
        ),
    ],
)
def test_summary_refusals(tmp_path: Path, setup: Any, code: str) -> None:
    setup(tmp_path)
    with pytest.raises(ds.DatasetError) as exc:
        ds.summarize(DATASET, tmp_path, n=3)
    assert exc.value.code == code


def test_cli_summarize_writes_both_once(tmp_path: Path) -> None:
    attempts = tmp_path / "attempts"
    write_attempt(attempts, "dv2-2b214b", "a1", 1.0)
    out, inputs = tmp_path / "out.json", tmp_path / "inputs.json"
    argv = [
        "summarize",
        "--dataset",
        str(DATASET),
        "--attempts",
        str(attempts),
        "--out",
        str(out),
        "--judge-inputs",
        str(inputs),
    ]
    assert ds.main(argv) == 0
    assert json.loads(out.read_text())["system"] == "holmes"
    [entry] = json.loads(inputs.read_text())["attempts"]
    assert entry["holmes_json"] == "attempts/a1/out/holmes.json"
    with pytest.raises(FileExistsError):
        ds.main(argv)


# --- run_attempt with fake docker (S7c's in-process call)


class FakeLab:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, output: dict[str, Any]):
        self.setup_kwargs: dict[str, Any] = {}
        self.run_kwargs: dict[str, Any] = {}
        self.torn_down = 0
        monkeypatch.setattr(hrun, "frontend_span_rate", lambda: 1.0)
        monkeypatch.setattr(
            hrun,
            "clock_skew",
            lambda: {"host_vm": 0.0, "host_container": 0.1, "measure_seconds": 0.2},
        )
        monkeypatch.setattr(hrun, "model_key", lambda: "sk-test-not-a-key")
        monkeypatch.setattr(hrun, "balance", lambda key: {"ok": True})
        monkeypatch.setattr(hrun, "docker", lambda *a, **k: '[{"Id": "sha256:img"}]')
        monkeypatch.setattr(hrun, "setup", lambda **k: self.setup_kwargs.update(k))
        monkeypatch.setattr(hrun, "teardown", self.teardown)
        monkeypatch.setattr(hrun, "holmes_secret_env", lambda key: {})
        monkeypatch.setattr(hrun, "egress_log", lambda name: [])

        def run_holmes(
            directory: Path, config_path: Path, prompt: str, env: Any, **k: Any
        ) -> Any:
            self.run_kwargs = {"prompt": prompt, **k}
            raw = directory / "out" / "holmes.json"
            raw.parent.mkdir(parents=True, exist_ok=True)
            raw.write_text(json.dumps(output))
            return {
                "output": dict(output),
                "started": 1000.0,
                "finished": 1010.0,
                "raw_sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
            }

        monkeypatch.setattr(hrun, "run_holmes", run_holmes)

    def teardown(self) -> None:
        self.torn_down += 1


def test_run_attempt_ran(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lab = FakeLab(monkeypatch, fake_output())
    spec, answer = loaded()["dv2-fa0e58"]
    got = ds.run_attempt(
        spec,
        answer,
        attempt_id="att-1",
        slot=2,
        raw_root=tmp_path,
        kind_node="lab2-control-plane",
        proxy_log=lambda since: ["t holmes GET /api/v1/query 200 1 2 0.1"],
    )
    assert got["status"] == "ran" and got["reason"] is None
    assert got["checks"] == {"read_only": True, "completed": True}
    assert got["holmes_json"] == str(tmp_path / "att-1" / ds.RAW_FILE)
    assert lab.setup_kwargs["egress_extra"] == ["--inject-503", "first"]
    assert lab.setup_kwargs["kind_node"] == "lab2-control-plane"
    assert lab.run_kwargs["wall_seconds"] == hconfig.RUN_WALL_SECONDS
    assert spec.stimulus.question in lab.run_kwargs["prompt"]
    assert lab.torn_down == 1
    summary = json.loads(Path(got["summary"]).read_text())
    assert summary["slot"] == 2 and summary["versions"]["image_id"] == "sha256:img"


def test_run_attempt_deadline_caps_the_wall_and_aborts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lab = FakeLab(monkeypatch, {"exit_code": -9, "timed_out": True})
    spec, answer = loaded()["dv2-2b214b"]
    got = ds.run_attempt(
        spec,
        answer,
        attempt_id="att-1",
        slot=1,
        raw_root=tmp_path,
        deadline=ds.time.time() + 60,
        proxy_log=lambda since: [],
    )
    assert lab.run_kwargs["wall_seconds"] <= 60
    assert got["status"] == "aborted" and got["reason"] == "DEADLINE"
    past = ds.run_attempt(
        spec,
        answer,
        attempt_id="att-2",
        slot=1,
        raw_root=tmp_path,
        deadline=ds.time.time() - 1,
    )
    assert past["status"] == "aborted" and past["reason"] == "DEADLINE_PASSED"


def test_run_attempt_setup_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    FakeLab(monkeypatch, fake_output())
    spec, answer = loaded()["dv2-2b214b"]
    alert_spec, alert_answer = loaded()["dv2-30921c"]
    assert (
        ds.run_attempt(
            alert_spec, alert_answer, attempt_id="x", slot=1, raw_root=tmp_path
        )["reason"]
        == "NOT_APPLICABLE"
    )
    monkeypatch.setattr(hrun, "frontend_span_rate", lambda: 0.0)
    got = ds.run_attempt(spec, answer, attempt_id="y", slot=1, raw_root=tmp_path)
    assert got["status"] == "setup_failed" and got["reason"] == "SPAN_RATE_ZERO"
    assert not (tmp_path / "y").exists()

    def boom(**_: Any) -> None:
        raise SystemExit("docker network create failed")

    monkeypatch.setattr(hrun, "frontend_span_rate", lambda: 1.0)
    monkeypatch.setattr(hrun, "setup", boom)
    got = ds.run_attempt(spec, answer, attempt_id="z", slot=1, raw_root=tmp_path)
    assert got["status"] == "setup_failed" and got["reason"] == "SETUP:SystemExit"


# --- the F14 raw outputs (local only)

REAL_RUNS = [
    ("f14-holmes-run1", "dv2-2b214b"),
    ("f14s3-2356-jaeger-real-1", "dv2-fcd9eb"),
    ("f14s3-2356-prometheus-real-1", "dv2-7261fe"),
]


@pytest.mark.skipif(
    not (RAW / "f14-holmes-run1").is_dir(), reason="F14 raw output absent"
)
@pytest.mark.parametrize("run_id,sid", REAL_RUNS)
def test_f14_raw_outputs_project_and_summarize(
    tmp_path: Path, run_id: str, sid: str
) -> None:
    raw = json.loads((RAW / run_id / "out/holmes.json").read_text())
    raw.update(exit_code=0, timed_out=False)
    summary = write_attempt(tmp_path, sid, "real", 1.0, output=raw)
    assert summary["final_state"] == "completed"
    assert summary["checks"] == {"read_only": True, "completed": True}
    assert summary["outcome"]["tool_calls"] == len(raw["tool_calls"]) > 0
    assert "jaeger_request" in {r["tool"] for r in summary["tool_calls"]}
    doc = ds.summarize(DATASET, tmp_path, n=3)
    [slot] = [s for s in doc["slots"] if s["attempt_id"] == "real"]
    assert slot["raw"] == "real/out/holmes.json"


# --- review round 1: paired adapter, provider error, lab instances


def test_names_follow_the_lab_instance_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib

    from scripts import kind_lab

    assert ds.RELAY == hrun.RELAY and ds.PROMETHEUS_URL == hrun.PROMETHEUS_URL
    monkeypatch.setattr(kind_lab, "SUFFIX", "-i2", raising=False)
    try:
        run2 = importlib.reload(hrun)
        ds2 = importlib.reload(ds)
        assert run2.NETWORK == "f14-holmes-internal-i2"
        assert {run2.RELAY, run2.EGRESS, run2.HOLMES, run2.CAPTURE} == {
            "f14-lab-relay-i2",
            "f14-egress-i2",
            "f14-holmes-i2",
            "f14-capture-i2",
        }
        assert (
            ds2.PROMETHEUS_URL == run2.PROMETHEUS_URL == "http://f14-lab-relay-i2:9090"
        )
        spec, answer = loaded()["dv2-2b214b"]
        plan = ds2.holmes_plan(spec, answer, submitted_at=SUBMITTED)
        assert plan["no_proxy"] == "f14-lab-relay-i2"
        # the frozen dimension does not move with the instance
        assert plan["upstream"]["value"].endswith(
            f"config={ds2.canonical_sha256(hconfig.holmes_config('http://f14-lab-relay:9090', 'http://f14-lab-relay:16686/jaeger/ui'))}"
        )
    finally:
        monkeypatch.undo()
        importlib.reload(hrun)
        importlib.reload(ds)


MODEL = {"mode": "connect", "client": "c", "target": "api.deepseek.com:443"}
TAIL_503 = "Traceback ...\nInternalServerError: litellm.InternalServerError:\nDeepseekException - 503 Service Unavailable"


@pytest.mark.parametrize(
    "state,error,egress,want",
    [
        ("failed", TAIL_503, [{**MODEL, "allowed": True}], True),
        ("failed", TAIL_503, [{**MODEL, "allowed": False, "injected": True}], False),
        (
            "failed",
            TAIL_503,
            [{**MODEL, "allowed": False, "injected": True}, {**MODEL, "allowed": True}],
            True,
        ),
        (
            "failed",
            "x\nConnection error: 127.0.0.1:9\nKeyError: 'result'\n...\n.",
            [],
            False,
        ),
        ("completed", TAIL_503, [{**MODEL, "allowed": True}], False),
        ("budget_exhausted", TAIL_503, [], False),
        ("failed", None, [], False),
    ],
)
def test_provider_error(state: str, error: Any, egress: Any, want: bool) -> None:
    assert ds.provider_error(state, {"error": error}, egress) is want


def test_slot_carries_read_only_and_provider_error(tmp_path: Path) -> None:
    write_attempt(tmp_path, "dv2-2b214b", "a1", 1.0)
    doc = ds.summarize(DATASET, tmp_path, n=3)
    ran = [s for s in doc["slots"] if s["status"] == "ran"]
    assert ran and all(
        s["read_only"] is True and s["provider_error"] is False for s in ran
    )
    assert all(
        "read_only" not in s for s in doc["slots"] if s["status"] == "not_applicable"
    )


def test_run_attempt_submit_late_is_setup_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lab = FakeLab(monkeypatch, fake_output())
    spec, answer = loaded()["dv2-2b214b"]
    got = ds.run_attempt(
        spec,
        answer,
        attempt_id="late",
        slot=1,
        raw_root=tmp_path,
        submit_not_after=ds.time.time() - 1,
    )
    assert got["status"] == "setup_failed" and got["reason"] == "SUBMIT_LATE"
    assert lab.torn_down == 1 and lab.run_kwargs == {}


@dataclass(frozen=True)
class Request:
    attempt_id: str
    round_id: str
    spec: Any
    answer: Any
    plan: Any
    output_dir: Path
    position: int
    submit_not_after: datetime | None


def test_paired_runner_adapts_run_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from scripts.holmes_baseline import dataset_runner as dr

    seen: dict[str, Any] = {}

    def fake_run_attempt(spec: Any, answer: Any, **kw: Any) -> dict[str, Any]:
        seen.update(kw)
        return {
            "status": "ran",
            "reason": None,
            "attempt_dir": str(kw["raw_root"] / kw["attempt_id"]),
            "started_at": "2026-11-10T18:30:01Z",
            "ended_at": "2026-11-10T18:33:01Z",
            "holmes_json_sha256": "ab" * 32,
            "provider_error": True,
        }

    spec, answer = loaded()["dv2-2b214b"]
    not_after = datetime(2026, 11, 10, 18, 31, tzinfo=UTC)
    request = Request("ops-1", "r1", spec, answer, None, tmp_path, 2, not_after)
    runner = dr.PairedHolmes(fake_run_attempt, result_type=dr.HolmesBaselineResult)
    got = runner.run(request)
    hid = dr.holmes_attempt_id("r1", "ops-1")
    assert seen["attempt_id"] == hid != "ops-1" and hid == dr.holmes_attempt_id(
        "r1", "ops-1"
    )
    assert hid != dr.holmes_attempt_id("r2", "ops-1")
    assert seen["raw_root"] == tmp_path
    assert seen["submit_not_after"] == not_after.timestamp()
    assert "deadline" not in seen
    assert got.status == "ran" and got.product_run_created and got.provider_error
    assert got.output_path == str(tmp_path / hid)
    assert got.started_at == datetime(2026, 11, 10, 18, 30, 1, tzinfo=UTC)
    assert got.output_sha256 == "ab" * 32

    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("docker gone")

    failed = dr.PairedHolmes(boom, result_type=dr.HolmesBaselineResult).run(request)
    assert failed.status == "aborted" and failed.reason == "BASELINE_ERROR:RuntimeError"

    def setup_failed(*a: Any, **k: Any) -> Any:
        return {"status": "setup_failed", "reason": "SUBMIT_LATE"}

    late = dr.PairedHolmes(setup_failed, result_type=dr.HolmesBaselineResult).run(
        request
    )
    assert late.status == "setup_failed" and not late.product_run_created


def test_paired_runner_fills_only_known_result_fields(tmp_path: Path) -> None:
    from dataclasses import dataclass

    from scripts.holmes_baseline import dataset_runner as dr

    @dataclass(frozen=True)
    class Narrow:  # #474's BaselineResult has no output_path
        status: str
        reason: str | None = None
        provider_error: bool = False

    spec, answer = loaded()["dv2-2b214b"]
    request = Request("ops-1", "r1", spec, answer, None, tmp_path, 1, None)
    got = dr.PairedHolmes(
        lambda *a, **k: {"status": "ran", "provider_error": False}, result_type=Narrow
    ).run(request)
    assert got == Narrow(status="ran", reason=None, provider_error=False)
