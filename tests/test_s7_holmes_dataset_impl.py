"""S7 HolmesGPT dataset runner: offline implementation-side checks (plan,
egress 503 injection, attempt projection, summary). No docker, kind or
model; the F14 raw outputs are used only when the git-ignored raw directory
exists locally."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
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

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "evals/datasets/dev-v2"
PROM = "http://f14-lab-relay:9090"
JAEGER = "http://f14-lab-relay:16686/jaeger/ui"
RAW = Path(
    os.environ.get(
        "F14_HOLMES_RAW_DIR",
        str(Path.home() / "dev/AI/production-ops-agent/tmp/f14-holmes"),
    )
)
UPSTREAM = {
    "kind": "hash",
    "value": "holmesgpt@x image=sha256:1 config=c",
    "reason": None,
}


def plans() -> dict[str, ds.HolmesPlan]:
    return {s.scenario_id: ds.plan(s, a) for s, a in load_scenarios_v2(DATASET)}


def answers() -> dict[str, Any]:
    return {s.scenario_id: (s, a) for s, a in load_scenarios_v2(DATASET)}


# --- plan


def test_alert_entry_scenarios_are_not_applicable_and_question_ones_are() -> None:
    for sid, (spec, answer) in answers().items():
        p = plans()[sid]
        assert p.applicable == (spec.entry == "question"), sid
        if spec.entry == "alertmanager":
            assert p.upstream == "no_upstream_entry"
        elif answer.category == "model_failure":
            assert p.upstream == "same_proxy_injection"
        else:
            assert p.upstream == "comparable"


def test_holmes_side_injectors_follow_the_answer() -> None:
    p = plans()
    assert p["dv2-c39f60"].model_mode == "always_503"
    assert p["dv2-c39f60"].egress_extra() == ["--inject-503", "always"]
    assert p["dv2-fa0e58"].egress_extra() == ["--inject-503", "first"]
    assert p["dv2-fcd9eb"].source == "jaeger"
    assert p["dv2-7261fe"].source == "prometheus"
    for sid in ("dv2-2b214b", "dv2-8e9455", "dv2-8b262a"):
        assert p[sid].source is None and p[sid].model_mode is None
        assert p[sid].egress_extra() == []
    assert p["dv2-bec37f"].lab_side == ("flagd", "otlp_span")


def test_unreachable_source_uses_opspilots_url_and_bypasses_the_egress_proxy() -> None:
    p = plans()
    jaeger, prom = p["dv2-fcd9eb"], p["dv2-7261fe"]
    assert jaeger.jaeger_url(JAEGER) == UNREACHABLE_URLS["jaeger"]
    assert jaeger.prometheus_url(PROM) == PROM
    assert prom.prometheus_url(PROM) == UNREACHABLE_URLS["prometheus"]
    assert prom.jaeger_url(JAEGER) == JAEGER
    assert jaeger.no_proxy(["f14-lab-relay"]) == "f14-lab-relay,127.0.0.1"
    assert p["dv2-2b214b"].no_proxy(["f14-lab-relay"]) == "f14-lab-relay"


def test_unreachable_config_keeps_the_same_two_toolsets() -> None:
    p = plans()["dv2-fcd9eb"]
    config = hconfig.holmes_config(p.prometheus_url(PROM), p.jaeger_url(JAEGER))
    enabled = {n for n, t in config["toolsets"].items() if t.get("enabled")}
    assert enabled == {"prometheus/metrics", "jaeger"}
    [endpoint] = config["toolsets"]["jaeger"]["config"]["endpoints"]
    assert endpoint["methods"] == ["GET"]
    assert endpoint["hosts"] == ["127.0.0.1:9"]


def test_budget_is_opspilots_frozen_values() -> None:
    budget = ds.budget()
    assert budget["max_steps"] == limits.MAX_MODEL_REQUESTS_PER_RUN
    assert budget["run_wall_seconds"] == limits.RUN_WALL_SECONDS
    assert budget["max_output_tokens"] == limits.MAX_OUTPUT_TOKENS
    assert budget["tool_timeout_seconds"] == MAX_REQUEST_TIMEOUT_SECONDS
    assert (
        int(hconfig.MODEL_ENV["OVERRIDE_MAX_CONTENT_SIZE"]) == limits.MAX_CONTEXT_TOKENS
    )
    assert float(hconfig.MODEL_ENV["LLM_REQUEST_TIMEOUT"]) == (
        limits.MODEL_REQUEST_TIMEOUT_SECONDS
    )


def test_prompt_is_the_scenario_question_and_the_window_before_submission() -> None:
    spec, _ = answers()["dv2-2b214b"]
    end = 1_793_000_000.0
    text = ds.prompt(spec.stimulus.question, end, 300)
    assert text.startswith(spec.stimulus.question + "\n")
    assert text.endswith(f"{ds.utc(end - 300)} to {ds.utc(end)}.")


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


def test_setup_passes_the_injection_to_the_egress_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.holmes_baseline import run as hrun

    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(hrun, "docker", lambda *a, **k: calls.append(a) or "")
    monkeypatch.setattr(hrun.time, "sleep", lambda _: None)
    hrun.setup(egress_extra=["--inject-503", "first"])
    [egress] = [c for c in calls if hrun.EGRESS in c and "run" in c]
    assert egress[-4:] == (
        "--allow",
        f"{hconfig.MODEL_HOST}:443",
        "--inject-503",
        "first",
    )


# --- attempt projection and summary


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
    upstream: dict[str, Any] = UPSTREAM,
) -> dict[str, Any]:
    spec, answer = answers()[sid]
    output = fake_output() if output is None else output
    summary = ds.attempt_summary(
        holmes_plan=ds.plan(spec, answer),
        attempt_id=attempt_id,
        round_id="r1",
        mode=mode,
        output=output,
        proxy_lines=["t holmes GET /api/v1/query_range?x 200 1 2 0.1"],
        egress=[],
        started=started,
        finished=started + 10,
        prompt_text="p",
        window_seconds=300,
        upstream=upstream,
        attempt_config={},
        raw_sha256=None,
        pre_run=None,
        balance=None,
    )
    directory = root / attempt_id
    directory.mkdir(parents=True)
    report = ds.report_text(output)
    if report is not None:
        (directory / ds.REPORT_FILE).write_text(report)
    (directory / ds.SUMMARY_FILE).write_text(json.dumps(summary))
    return summary


def test_attempt_summary_keeps_no_report_text(tmp_path: Path) -> None:
    summary = write_attempt(tmp_path, "dv2-2b214b", "a1", 100.0)
    text = json.dumps(summary)
    assert "payment Charge fails" not in text
    assert (
        summary["report"]["sha256"]
        == hashlib.sha256(b"## Report\npayment Charge fails").hexdigest()
    )
    assert summary["outcome"]["final_state"] == "completed"
    assert summary["outcome"]["proxied_queries_ok"] == 1
    assert summary["tool_errors"] == 1


def test_summary_slots_denominators_and_not_applicable(tmp_path: Path) -> None:
    write_attempt(tmp_path, "dv2-2b214b", "a2", 200.0)
    write_attempt(tmp_path, "dv2-2b214b", "a1", 100.0)
    write_attempt(
        tmp_path,
        "dv2-c39f60",
        "m1",
        300.0,
        output={"exit_code": 1, "timed_out": False, "error": "503"},
    )
    (tmp_path / "f14-holmes-run1").mkdir()  # F14 raw dirs share the root
    doc = ds.summarize(DATASET, tmp_path, n=3)
    assert doc["schema"] == ds.ROUND_SCHEMA
    assert doc["dataset"].startswith("dev-v2:")
    assert doc["upstream"] == UPSTREAM
    alert = [s for s in doc["slots"] if s["entry"] == "alertmanager"]
    assert alert and all(
        s["status"] == "not_applicable" and not s["in_denominator"] for s in alert
    )
    payment = [s for s in doc["slots"] if s["scenario_id"] == "dv2-2b214b"]
    assert [s["attempt_id"] for s in payment] == ["a1", "a2", None]
    assert payment[0]["report"]["path"] == "a1/report.md"
    assert payment[2]["status"] == "not_started"
    model = [s for s in doc["slots"] if s["scenario_id"] == "dv2-c39f60"][0]
    assert model["final_state"] == "failed" and model["final_state_expected"] is True
    cap, reg = doc["totals"]["capability"], doc["totals"]["regression"]
    question_cap = sum(
        1
        for s, a in answers().values()
        if a.category != "regression" and s.entry == "question"
    )
    assert cap["slots"] == question_cap * 3
    assert cap["not_applicable"] == (12 - question_cap) * 3
    assert reg == {
        "slots": 18,
        "not_applicable": 0,
        "started": 2,
        "completed": 2,
        "report_available": 2,
        "final_state_expected": 2,
    }


@pytest.mark.parametrize(
    "setup,code",
    [
        (
            lambda r: [
                write_attempt(r, "dv2-2b214b", f"a{i}", float(i)) for i in range(4)
            ],
            "REDRAW_DETECTED:dv2-2b214b",
        ),
        (
            lambda r: write_attempt(r, "dv2-30921c", "x", 1.0),
            "NOT_APPLICABLE_ATTEMPT:dv2-30921c",
        ),
        (
            lambda r: write_attempt(r, "dv2-2b214b", "x", 1.0, mode="capture_model"),
            "ATTEMPT_NOT_REAL:x",
        ),
        (
            lambda r: (
                write_attempt(r, "dv2-2b214b", "x", 1.0),
                write_attempt(
                    r, "dv2-455c6d", "y", 2.0, upstream={**UPSTREAM, "value": "other"}
                ),
            ),
            "MANIFEST_DRIFT:upstream",
        ),
        (
            lambda r: (
                write_attempt(r, "dv2-2b214b", "x", 1.0),
                (r / "x" / ds.REPORT_FILE).write_text("edited"),
            ),
            "REPORT_MISMATCH:x",
        ),
    ],
)
def test_summary_refusals(tmp_path: Path, setup: Any, code: str) -> None:
    setup(tmp_path)
    with pytest.raises(ds.DatasetError) as exc:
        ds.summarize(DATASET, tmp_path, n=3)
    assert exc.value.code == code


def test_cli_summarize_writes_once(tmp_path: Path) -> None:
    attempts = tmp_path / "attempts"
    write_attempt(attempts, "dv2-2b214b", "a1", 1.0)
    out = tmp_path / "out.json"
    argv = [
        "summarize",
        "--dataset",
        str(DATASET),
        "--attempts",
        str(attempts),
        "--out",
        str(out),
    ]
    assert ds.main(argv) == 0
    assert json.loads(out.read_text())["system"] == "holmesgpt"
    with pytest.raises(FileExistsError):
        ds.main(argv)


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
    assert summary["outcome"]["final_state"] == "completed"
    assert summary["report"]["available"] is True
    assert summary["outcome"]["tool_calls"] == len(raw["tool_calls"]) > 0
    names = {r["tool"] for r in summary["tool_calls"]}
    assert "jaeger_request" in names
    doc = ds.summarize(DATASET, tmp_path, n=3)
    [slot] = [s for s in doc["slots"] if s["attempt_id"] == "real"]
    assert slot["report"]["path"] == "real/report.md"
