"""#604 driver orchestration with stand-in launchers; synthetic data only.

Every host-touching step (PostgreSQL, docker, subprocesses, the intake, the
database reads, LangSmith, the judging process) is replaced through
``driver.Backends``; the stand-ins record start/stop order so the tests can
assert that a failure anywhere stops everything already started.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts.itbench_lite import driver
from scripts.itbench_lite.services import ServiceError, Started

RUN_ID = "11111111-2222-5333-8444-555555555555"
METRICS_HEADER = (
    "timestamp\tmetric_name\tvalue\ttags\tnamespace\tpod_name\tservice_name\n"
)
OBJ_HEADER = "Timestamp\tLogAttributes\tBody\n"


def _alert(name, active, **labels):
    return {
        "labels": {"alertname": name, "severity": "warning", **labels},
        "annotations": {},
        "state": "firing",
        "activeAt": active,
    }


def make_scenario(
    dataset: Path, name: str = "Scenario-16", *, labels=None, answer_mode=0o000
) -> Path:
    root = dataset / name
    (root / "alerts").mkdir(parents=True)
    (root / "metrics").mkdir()
    labels = {"service_name": "checkout"} if labels is None else labels
    entries = [
        _alert("Watchdog", "2025-12-15T17:00:00Z"),
        _alert("RequestErrorRate", "2025-12-15T17:20:00Z", **labels),
    ]
    (root / "alerts" / "alerts_alerting_state_1.json").write_text(
        json.dumps({"status": "success", "data": {"alerts": entries}})
    )
    real = dataset / "elsewhere.tsv"
    real.write_text(
        METRICS_HEADER
        + "2025-12-15 17:30:00.000\tcpu\t1.5\t{'container': 'app'}\totel-demo\tx-1\t\n"
    )
    (root / "metrics" / "pod_x.tsv").symlink_to(real)
    ns = {
        "kind": "Namespace",
        "apiVersion": "v1",
        "metadata": {"name": "kube-system", "uid": "uid-ks-1"},
    }
    row = (
        "2025-12-15 17:25:00.000\t{'k8s.resource.name': 'namespaces'}\t\""
        + json.dumps(ns).replace('"', '""')
        + '"\n'
    )
    (root / "k8s_objects_raw.tsv").write_text(OBJ_HEADER + row)
    # Answer files: unreadable, so any attempt to open them fails loudly.
    for answer_name in ("ground_truth.yaml", "status.json"):
        path = root / answer_name
        path.write_text("secret: answer\n")
        path.chmod(answer_mode)
    (root / "notes.txt").write_text("not whitelisted")
    return root


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 11, 13, tzinfo=UTC)
        self.lock = threading.Lock()

    def now(self) -> datetime:
        with self.lock:
            return self.t

    def sleep(self, seconds: float) -> None:
        with self.lock:
            self.t += timedelta(seconds=seconds)


@dataclass
class FakePg:
    owner_dsn: str = "owner"
    runtime_dsn: str = "worker"
    web_dsn: str = "web"


def valid_report():
    return {
        "schema_version": "m0-report-v2",
        "assessment_status": "completed",
        "conclusion": "supported",
        "summary": "checkout fails.",
        "claims": [
            {
                "kind": "fact",
                "text": "checkout error rate rose",
                "evidence_ids": ["ev-1"],
                "target_refs": ["target-1"],
                "time_scope_ref": "window-1",
            }
        ],
        "gaps": [],
        "next_steps": [],
    }


class Harness:
    """Stand-in backends; ``fail`` names the step that raises."""

    def __init__(self, tmp: Path, *, fail: str | None = None, load_seconds=30):
        self.clock = Clock()
        self.events: list[tuple[str, str]] = []
        self.fail = fail
        self.load_seconds = load_seconds
        self.report: Any = valid_report()
        self.reply: Any = {
            "results": [{"outcome": "created", "run_id": RUN_ID, "reason": None}]
        }
        self.states = ["queued", "running", "completed"]
        self.delivered: list[dict] = []
        self.busy: set[int] = set()
        self.envs: dict[str, dict] = {}
        self.judge_calls: list[dict] = []
        self.judge_status = 0

    def _started(self, name: str) -> Started:
        if self.fail == name:
            raise ServiceError("NOT_READY", name)
        self.events.append(("start", name))
        self.clock.sleep(self.load_seconds / 4)
        return Started(
            name,
            lambda: self.events.append(("stop", name)),
            info={"api_url": "https://127.0.0.1:29143", "ca_file": "/x/ca.pem"},
        )

    def backends(self) -> driver.Backends:
        def start_pg(directory, port):
            if self.fail == "pg":
                raise RuntimeError("boom")
            self.events.append(("start", "pg"))
            return FakePg()

        def start_fake_k8s(snapshot, **kwargs):
            assert kwargs["offset_seconds"] > 0
            return self._started("fake_k8s")

        def start_web(env, port, log):
            self.envs["web"] = dict(env)
            return self._started("web")

        def start_worker(env, log, settle_seconds):
            self.envs["worker"] = dict(env)
            return self._started("worker")

        def deliver(url, token, body):
            self.delivered.append(body)
            if self.fail == "deliver":
                raise OSError("refused")
            return 200, self.reply

        def collect(dsn, run_id):
            block = {
                "run_id": run_id,
                "report": self.report,
                "report_present": self.report is not None,
                "observations": [
                    {
                        "evidence_id": "ev-1",
                        "tool": "m",
                        "query": {"service": "checkout"},
                    }
                ],
                "versions": {"prompt_revision": "p", "tool_schema_revision": "t"},
            }
            return block, {"raw": True}

        def judge(job):
            self.judge_calls.append(dict(job))
            if self.judge_status in (0, 3):  # the child writes det either way
                Path(job["out"]).write_text(
                    json.dumps(
                        {
                            "judged": job["judge"] and self.judge_status == 0,
                            "det": {"det_f1": 1.0, "det_pass": 1.0, "note": "x"},
                            "official": None,
                        }
                    )
                )
            return self.judge_status

        return driver.Backends(
            start_pg=start_pg,
            stop_pg=lambda pg: self.events.append(("stop", "pg")),
            start_prometheus=lambda name, om, work, port: self._started("prometheus"),
            start_jaeger=lambda name, q, o: self._started("jaeger"),
            post_json=lambda url, body: 200,
            start_fake_k8s=start_fake_k8s,
            start_web=start_web,
            start_worker=start_worker,
            deliver=deliver,
            run_state=lambda dsn, rid: (
                self.states.pop(0) if len(self.states) > 1 else self.states[0]
            ),
            collect=collect,
            trace_roots=lambda project, rid, env: {
                "project": project,
                "read_back": "not_exported",
                "roots": [],
            },
            judge=judge,
            port_in_use=lambda port: port in self.busy,
            memory_reading=lambda: None,
            now=self.clock.now,
            sleep=self.clock.sleep,
        )


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPSPILOT_LEDGER_DIR", str(tmp_path / "ledgers"))
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    make_scenario(dataset)
    vocab = tmp_path / "S16.txt"
    vocab.write_text("checkout\npayment\n")
    cfg = driver.Config(
        round="t604",
        dataset=dataset,
        ground_truth=dataset,
        tmp_root=tmp_path / "tmp",
        evidence_root=tmp_path / "evidence",
        load_budget=timedelta(seconds=60),
        trace="off",
        poll_seconds=1,
        git={"commit": "abc", "dirty": False, "allow_dirty": False},
        secrets_env={"DEEPSEEK_API_KEY": "synthetic-low-entropy"},
    )
    return cfg, vocab


def _summary(cfg, trial=1):
    path = cfg.evidence_dir / "S16" / f"trial-{trial}" / "summary.json"
    return json.loads(path.read_text())


STACK = ["pg", "prometheus", "jaeger", "fake_k8s", "web", "worker"]


def test_one_attempt_end_to_end(env, tmp_path):
    cfg, vocab = env
    h = Harness(tmp_path)
    out = driver.run_scenario(cfg, h.backends(), "Scenario-16", vocab, [1])
    record = out["trials"][1]
    assert record["status"] == "completed" and record["run_id"] == RUN_ID
    summary = _summary(cfg)
    block = summary["itbench_lite"]
    assert summary["status"] == "completed"
    assert block["intake"]["outcome"] == "created"
    assert 0 <= block["gap_seconds"] <= 120
    assert block["target_fallback"] is False
    assert block["entry_alert"]["match_labels"] == {"service_name": "checkout"}
    assert block["run_wait"] == {
        **block["run_wait"],
        "terminal_state": "completed",
        "timed_out": False,
    }
    assert summary["run"]["versions"]["prompt_revision"] == "p"
    # Every start has a stop; the worker first (spans flush), then reverse order.
    starts = [n for kind, n in h.events if kind == "start"]
    stops = [n for kind, n in h.events if kind == "stop"]
    assert set(starts) == set(STACK) and sorted(stops) == sorted(STACK)
    assert stops[0] == "worker" and stops[-1] == "pg"
    # The webhook carries the entry alert, shifted so the data ends before it.
    alert = h.delivered[0]["alerts"][0]
    assert alert["labels"]["alertname"] == "RequestErrorRate"
    # Conversion output for the evaluator, outside the evidence directory.
    trial_dir = cfg.round_dir / "S16" / "trial-1"
    agent = json.loads(
        (
            trial_dir
            / "outputs"
            / "Scenario-16"
            / "1"
            / "outputs"
            / "agent_output.json"
        ).read_text()
    )
    assert agent["entities"][0]["name"] == "otel-demo/Service/checkout"
    # Judge got the answer root and this attempt's outputs, scores are numeric only.
    job = h.judge_calls[0]
    assert (
        job["ground_truth"] == cfg.dataset and job["outputs"] == trial_dir / "outputs"
    )
    assert record["judge"]["det"] == {"det_f1": 1.0, "det_pass": 1.0}
    # Environment wiring.
    web, worker = h.envs["web"], h.envs["worker"]
    assert (
        web["OPSPILOT_TOOL_PROFILE"]
        == worker["OPSPILOT_TOOL_PROFILE"]
        == "otel-demo-ext"
    )
    assert web["OPSPILOT_HEALTH_BIND"] == worker["OPSPILOT_HEALTH_BIND"] == ""
    assert web["OPSPILOT_SELF_HEAL_INTERVAL_SECONDS"] == "0"
    assert worker["OPSPILOT_K8S_API_URL"].startswith("https://127.0.0.1:")
    assert "DEEPSEEK_API_KEY" not in web and "OPSPILOT_WORKER_DSN" not in web
    identities = json.loads(
        (trial_dir / "chance-1" / "target_identities.json").read_text()
    )
    entry = identities["m0-otel-20260909"]
    assert entry["cluster_uid"] == "uid-ks-1"
    assert entry["match"] == {"version": 1, "labels": {"service_name": "checkout"}}


def test_clean_copy_takes_whitelisted_names_only_and_never_opens_answers(env, tmp_path):
    cfg, _ = env
    scenario = driver.prepare_scenario(cfg.dataset, "Scenario-16", cfg.round_dir)
    names = sorted(
        str(p.relative_to(scenario.copy))
        for p in scenario.copy.rglob("*")
        if p.is_file()
    )
    assert names == [
        "alerts/alerts_alerting_state_1.json",
        "k8s_objects_raw.tsv",
        "metrics/pod_x.tsv",
    ]
    assert not (scenario.copy / "metrics" / "pod_x.tsv").is_symlink()
    # Answer files are mode 000: reaching here means they were never opened.
    assert scenario.copy_stats["files"] == 3
    again = driver.prepare_scenario(cfg.dataset, "Scenario-16", cfg.round_dir)
    assert again.copy_stats == {"reused": True}


def test_symlink_to_an_answer_file_is_refused(tmp_path):
    src = tmp_path / "s"
    src.mkdir()
    (tmp_path / "ground_truth.yaml").write_text("x")
    (src / "k8s_objects_raw.tsv").symlink_to(tmp_path / "ground_truth.yaml")
    with pytest.raises(driver.DeterministicError) as err:
        driver.copy_whitelisted(src, tmp_path / "copy")
    assert err.value.code == "ANSWER_FILE_PRESENT"
    assert not (tmp_path / "copy").exists()


@pytest.mark.parametrize(
    "step", ["pg", "prometheus", "jaeger", "fake_k8s", "web", "worker", "deliver"]
)
def test_any_failing_step_fails_the_attempt_and_cleans_up(env, tmp_path, step):
    cfg, vocab = env
    h = Harness(tmp_path, fail=step)
    out = driver.run_scenario(cfg, h.backends(), "Scenario-16", vocab, [1])
    assert out["trials"][1]["status"] == "missing"
    summary = _summary(cfg)
    assert summary["status"] == "missing" and summary["reason"] == "CHANCES_EXHAUSTED"
    history = summary["itbench_lite"]["history"]
    assert len(history) == driver.CHANCES
    assert all(item["kind"] == "infra" for item in history)
    starts = [n for kind, n in h.events if kind == "start"]
    stops = [n for kind, n in h.events if kind == "stop"]
    assert sorted(starts) == sorted(stops)  # nothing left running
    if step != "pg":
        assert "pg" in stops
    assert h.judge_calls == []


def test_late_delivery_is_void_before_any_post_and_retried(env, tmp_path):
    cfg, vocab = env
    h = Harness(tmp_path)
    original = h.backends()
    calls = {"n": 0}

    def slow_worker(env_, log, settle_seconds):
        calls["n"] += 1
        item = h._started("worker")
        if calls["n"] == 1:
            h.clock.sleep(200)  # worker start after loading crosses the line
        return item

    original.start_worker = slow_worker
    out = driver.run_scenario(cfg, original, "Scenario-16", vocab, [1])
    assert out["trials"][1]["status"] == "completed"
    summary = _summary(cfg)
    history = summary["itbench_lite"]["history"]
    assert [(i["kind"], i["code"]) for i in history] == [
        ("void", "DELIVERY_GAP_EXCEEDED")
    ]
    assert summary["itbench_lite"]["chance"] == 2
    assert len(h.delivered) == 1


def test_delivery_follows_loading_at_once_whatever_the_budget(env, tmp_path):
    cfg, vocab = env
    cfg.load_budget = timedelta(seconds=3600)  # a ceiling, never a wait
    h = Harness(tmp_path)
    driver.run_scenario(cfg, h.backends(), "Scenario-16", vocab, [1])
    block = _summary(cfg)["itbench_lite"]
    assert block["history"] == []
    # Only the worker start (7.5 s in the stand-in) separates load and POST.
    assert block["gap_seconds"] == 7.5
    assert "planned_delivery_at" not in block
    # Offset anchored at the start: data ends a minute before the start.
    assert block["data_end_to_delivery_seconds"] == pytest.approx(
        60 + block["load_seconds"] + 7.5, abs=1
    )


def test_loading_over_the_budget_is_an_infra_failure_without_delivery(env, tmp_path):
    cfg, vocab = env
    cfg.load_budget = timedelta(seconds=10)  # the stand-in loads take 30 s
    h = Harness(tmp_path)
    driver.run_scenario(cfg, h.backends(), "Scenario-16", vocab, [1])
    summary = _summary(cfg)
    assert summary["status"] == "missing"
    assert {i["code"] for i in summary["itbench_lite"]["history"]} == {
        "LOAD_BUDGET_EXCEEDED"
    }
    assert h.delivered == []


def test_busy_port_is_an_infra_failure_with_nothing_started(env, tmp_path):
    cfg, vocab = env
    h = Harness(tmp_path)
    h.busy = {cfg.ports.web}
    driver.run_scenario(cfg, h.backends(), "Scenario-16", vocab, [1])
    summary = _summary(cfg)
    codes = {i["code"] for i in summary["itbench_lite"]["history"]}
    assert codes == {"PORT_BUSY"}
    assert h.events == []


def test_deterministic_error_is_not_retried_and_marks_all_trials(env, tmp_path):
    cfg, vocab = env
    root = cfg.dataset / "Scenario-16"
    (root / "alerts" / "alerts_alerting_state_1.json").write_text(
        json.dumps([_alert("Watchdog", "2025-12-15T17:00:00Z")])
    )
    h = Harness(tmp_path)
    out = driver.run_scenario(cfg, h.backends(), "Scenario-16", vocab, [1, 2, 3])
    assert {t: r["status"] for t, r in out["trials"].items()} == {
        1: "missing",
        2: "missing",
        3: "missing",
    }
    for trial in (1, 2, 3):
        assert _summary(cfg, trial)["reason"] == "NO_ENTRY_ALERT"
    assert h.events == []


def test_unresolved_target_aborts_the_round_after_cleanup(env, tmp_path):
    cfg, vocab = env
    h = Harness(tmp_path)
    h.reply = {
        "results": [
            {"outcome": "handed_off", "run_id": None, "reason": "TARGET_UNRESOLVED"}
        ]
    }
    with pytest.raises(driver.RoundAbort) as err:
        driver.run_scenario(cfg, h.backends(), "Scenario-16", vocab, [1])
    assert err.value.code == "TARGET_UNRESOLVED"
    stops = [n for kind, n in h.events if kind == "stop"]
    assert sorted(stops) == sorted(STACK)


@pytest.mark.parametrize(
    ("report", "reason"),
    [(None, None), ({"summary": "checkout"}, "REPORT_INVALID_FOR_CONVERSION")],
)
def test_reportless_run_writes_the_empty_result(env, tmp_path, report, reason):
    cfg, vocab = env
    h = Harness(tmp_path)
    h.report = report
    driver.run_scenario(cfg, h.backends(), "Scenario-16", vocab, [1])
    block = _summary(cfg)["itbench_lite"]
    assert block["conversion"]["report_present"] is False
    assert block["conversion"]["reason"] == reason
    meta = json.loads(
        (
            cfg.round_dir
            / "S16"
            / "trial-1"
            / "outputs"
            / "Scenario-16"
            / "1"
            / "conversion_meta.json"
        ).read_text()
    )
    assert meta["report_present"] is False


def test_run_wait_times_out_and_still_freezes(env, tmp_path):
    cfg, vocab = env
    cfg.terminal_wait = timedelta(seconds=10)
    h = Harness(tmp_path)
    h.states = ["running"]
    driver.run_scenario(cfg, h.backends(), "Scenario-16", vocab, [1])
    wait = _summary(cfg)["itbench_lite"]["run_wait"]
    assert wait["timed_out"] is True and wait["terminal_state"] == "running"


def test_entry_alert_without_service_name_uses_the_fallback(tmp_path):
    dataset = tmp_path / "d"
    dataset.mkdir()
    make_scenario(dataset, labels={"namespace": "otel-demo"}, answer_mode=0o600)
    scenario = driver.prepare_scenario(dataset, "Scenario-16", tmp_path / "r")
    assert scenario.plan == driver.EntryPlan(
        "RequestErrorRate", {"alertname": "RequestErrorRate"}, "otel-demo", True
    )


def test_resume_skips_only_completed_or_terminal_missing_trials(env, tmp_path):
    cfg, vocab = env
    h = Harness(tmp_path)
    driver.run_scenario(cfg, h.backends(), "Scenario-16", vocab, [1])
    assert (cfg.round_dir / "S16" / "trial-1" / driver.MARKER).is_file()
    h2 = Harness(tmp_path)
    out = driver.run_scenario(cfg, h2.backends(), "Scenario-16", vocab, [1])
    assert out == {"scenario": "16", "skipped": [1]}
    assert h2.events == [] and h2.judge_calls == []


def test_resume_finishes_judging_without_a_new_run(env, tmp_path):
    cfg, vocab = env
    h = Harness(tmp_path)
    driver.run_scenario(cfg, h.backends(), "Scenario-16", vocab, [1])
    trial_dir = cfg.round_dir / "S16" / "trial-1"
    (trial_dir / driver.MARKER).unlink()  # crash after freeze, before marker
    (trial_dir / "scores.json").unlink()
    h2 = Harness(tmp_path)
    out = driver.run_scenario(cfg, h2.backends(), "Scenario-16", vocab, [1])
    assert out["trials"][1]["resumed"] == "judge"
    assert h2.events == [] and len(h2.judge_calls) == 1
    assert (trial_dir / driver.MARKER).is_file()


def test_resume_reruns_a_summary_without_outputs_and_keeps_the_old_one(env, tmp_path):
    cfg, vocab = env
    h = Harness(tmp_path)
    driver.run_scenario(cfg, h.backends(), "Scenario-16", vocab, [1])
    trial_dir = cfg.round_dir / "S16" / "trial-1"
    import shutil

    shutil.rmtree(trial_dir / "outputs")
    (trial_dir / driver.MARKER).unlink()
    h2 = Harness(tmp_path)
    out = driver.run_scenario(cfg, h2.backends(), "Scenario-16", vocab, [1])
    assert out["trials"][1]["status"] == "completed"
    assert ("start", "pg") in h2.events  # a new Run
    assert len(list((trial_dir / "stale").iterdir())) == 1  # set aside, not deleted
    assert _summary(cfg)["status"] == "completed"


@pytest.mark.parametrize(
    ("summary", "state"),
    [
        ({"status": "missing", "reason": "NO_ENTRY_ALERT"}, "missing"),
        ({"status": "missing"}, "rerun"),
        ({"status": "completed"}, "rerun"),
        ("not an object", "rerun"),
    ],
)
def test_trial_state_needs_explicit_terminal_records(env, summary, state):
    cfg, _ = env
    path = cfg.evidence_dir / "S16" / "trial-1"
    path.mkdir(parents=True)
    (path / "summary.json").write_text(json.dumps(summary))
    assert driver.trial_state(cfg, "S16", "Scenario-16", 1) == state


def test_judge_failures_are_retried_then_recorded(env, tmp_path):
    cfg, vocab = env
    h = Harness(tmp_path)
    h.judge_status = 3
    out = driver.run_scenario(cfg, h.backends(), "Scenario-16", vocab, [1])
    assert out["trials"][1]["judge"]["status"] == "failed"
    assert len(h.judge_calls) == driver.JUDGE_TRIES
    assert len({c["work_dir"] for c in h.judge_calls}) == driver.JUDGE_TRIES
    # The deterministic score exists, so the trial is complete; the official
    # score is re-judged later by ``driver judge``.
    assert driver.trial_state(cfg, "S16", "Scenario-16", 1) == "done"


def test_scores_aggregate_for_coverage(env, tmp_path):
    cfg, vocab = env
    h = Harness(tmp_path)
    driver.run_scenario(cfg, h.backends(), "Scenario-16", vocab, [1, 2])
    table = driver.aggregate_scores(cfg)
    assert table == {"16": [{"det_f1": 1.0, "det_pass": 1.0}] * 2}


def test_vocabulary_missing_is_deterministic(env, tmp_path):
    cfg, _ = env
    h = Harness(tmp_path)
    driver.run_scenario(cfg, h.backends(), "Scenario-16", tmp_path / "none.txt", [1])
    assert _summary(cfg)["reason"] == "VOCABULARY_MISSING"


def test_cleanup_failure_aborts_the_round(env, tmp_path):
    cfg, vocab = env
    h = Harness(tmp_path)
    backends = h.backends()

    def bad_stop(pg):
        raise RuntimeError("PG_STOP_FAILED")

    backends.stop_pg = bad_stop
    with pytest.raises(driver.RoundAbort) as err:
        driver.run_scenario(cfg, backends, "Scenario-16", vocab, [1])
    assert err.value.code == "CLEANUP_FAILED"
    assert "worker" in [n for k, n in h.events if k == "stop"]


def test_answer_files_stay_unopened_mode(env):
    cfg, _ = env
    assert (
        os.stat(cfg.dataset / "Scenario-16" / "ground_truth.yaml").st_mode & 0o777 == 0
    )


def test_real_fake_k8s_launcher_starts_ready_and_stops(env, tmp_path):
    import shutil
    import socket

    from scripts.itbench_lite import services

    if shutil.which("openssl") is None:
        pytest.skip("openssl not installed")
    cfg, _ = env
    scenario = driver.prepare_scenario(cfg.dataset, "Scenario-16", cfg.round_dir)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    item = services.start_fake_k8s(
        scenario.copy,
        offset_seconds=3600,
        token="synthetic-token-604",
        work_dir=tmp_path / "fk",
        port=port,
        load_timeout=60,
    )
    try:
        assert item.info["api_url"] == f"https://127.0.0.1:{port}"
        assert item.alive()
        assert (tmp_path / "fk" / "token").stat().st_mode & 0o777 == 0o600
    finally:
        item.stop()
    assert not item.alive()
    assert not services.port_in_use(port)


def test_cli_round_then_scores(env, tmp_path, monkeypatch, capsys):
    cfg, vocab = env
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-low-entropy")
    make_scenario(cfg.dataset, "Scenario-7", answer_mode=0o600)
    vocab_dir = tmp_path / "vocab"
    vocab_dir.mkdir()
    for key in ("7", "16"):
        (vocab_dir / f"S{key}.txt").write_text("checkout\n")
    h = Harness(tmp_path)
    common = ["--round", "t604", "--tmp-root", str(cfg.tmp_root)]
    code = driver.main(
        [
            "round", *common, "--dataset", str(cfg.dataset),
            "--vocabulary-dir", str(vocab_dir), "--trials", "1",
            "--trace", "off", "--allow-dirty", "--load-budget-seconds", "60",
            "--evidence-root", str(cfg.evidence_root), "--no-judge",
        ],
        backends=h.backends(),
    )  # fmt: skip
    assert code == 0
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert [x["scenario"] for x in lines] == ["7", "16"]  # sorted by id
    assert all(c["judge"] is False for c in h.judge_calls)
    out = tmp_path / "scores.json"
    assert driver.main(["scores", *common, "--out", str(out)]) == 0
    assert set(json.loads(out.read_text())) == {"7", "16"}
    first = json.loads(capsys.readouterr().out)
    assert first["scenarios"] == 2


def test_cli_refuses_without_model_key(env, monkeypatch, capsys):
    cfg, vocab = env
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    code = driver.main(
        [
            "attempt", "--round", "t604", "--dataset", str(cfg.dataset),
            "--scenario", "Scenario-16", "--trial", "1", "--vocabulary", str(vocab),
            "--trace", "off", "--allow-dirty", "--tmp-root", str(cfg.tmp_root),
            "--evidence-root", str(cfg.evidence_root),
        ],
        backends=Harness(cfg.tmp_root).backends(),
    )  # fmt: skip
    assert code == 2
    assert json.loads(capsys.readouterr().out)["code"] == "DEEPSEEK_API_KEY_MISSING"


# -- review round 1 (#606) ---------------------------------------------------------


def test_directory_symlink_anywhere_in_the_source_is_refused(env, tmp_path):
    cfg, _ = env
    root = cfg.dataset / "Scenario-16"
    other = tmp_path / "other"
    other.mkdir()
    (root / "deep" / "er").mkdir(parents=True)
    (root / "deep" / "er" / "link").symlink_to(other, target_is_directory=True)
    with pytest.raises(driver.DeterministicError) as err:
        driver.copy_whitelisted(root, tmp_path / "copy")
    assert err.value.code == "UNSAFE_SNAPSHOT"
    assert not (tmp_path / "copy").exists()


def test_symlinked_scenario_root_is_refused(env, tmp_path):
    cfg, _ = env
    link = tmp_path / "linked"
    link.symlink_to(cfg.dataset / "Scenario-16", target_is_directory=True)
    with pytest.raises(driver.DeterministicError) as err:
        driver.copy_whitelisted(link, tmp_path / "copy")
    assert err.value.code == "UNSAFE_SNAPSHOT"


def test_unlistable_subdirectory_fails_closed(env, tmp_path):
    cfg, _ = env
    locked = cfg.dataset / "Scenario-16" / "locked"
    locked.mkdir()
    locked.chmod(0o000)
    try:
        with pytest.raises(driver.DeterministicError) as err:
            driver.copy_whitelisted(cfg.dataset / "Scenario-16", tmp_path / "copy")
        assert err.value.code == "UNSAFE_SNAPSHOT"
    finally:
        locked.chmod(0o700)


def test_answer_names_elsewhere_in_the_tree_are_never_selected(env, tmp_path):
    cfg, _ = env
    nested = cfg.dataset / "Scenario-16" / "metrics" / "assertion.json"
    nested.write_text("{}")
    nested.chmod(0o000)
    stats = driver.copy_whitelisted(cfg.dataset / "Scenario-16", tmp_path / "copy")
    assert stats["files"] == 3


def test_reused_copy_is_verified_again(env, tmp_path):
    cfg, _ = env
    copy = tmp_path / "copy"
    driver.copy_whitelisted(cfg.dataset / "Scenario-16", copy)
    (copy / "extra.txt").write_text("x")
    with pytest.raises(driver.DeterministicError):
        driver.copy_whitelisted(cfg.dataset / "Scenario-16", copy)
    (copy / "extra.txt").unlink()
    (copy / "status.json").write_text("x")
    with pytest.raises(driver.DeterministicError) as err:
        driver.copy_whitelisted(cfg.dataset / "Scenario-16", copy)
    assert err.value.code == "ANSWER_FILE_PRESENT"


def test_non_json_report_text_counts_as_invalid(env, tmp_path):
    cfg, vocab = env
    h = Harness(tmp_path)
    h.report = "The checkout service failed."  # parse_report keeps the text
    driver.run_scenario(cfg, h.backends(), "Scenario-16", vocab, [1])
    block = _summary(cfg)["itbench_lite"]
    assert block["conversion"]["report_present"] is False
    assert block["conversion"]["reason"] == "REPORT_INVALID_FOR_CONVERSION"


@pytest.mark.parametrize("name", ["ground_truth.yaml", "status.json", "Assertion.JSON"])
def test_answer_named_inputs_are_refused(env, tmp_path, name):
    cfg, vocab = env
    target = tmp_path / name
    target.write_text("x")
    with pytest.raises(driver.RoundAbort) as err:
        driver.check_input_file(target, "env-file")
    assert err.value.code == "ANSWER_FILE_AS_INPUT"
    link = tmp_path / "vocab-link.txt"
    link.symlink_to(target)
    with pytest.raises(driver.RoundAbort):
        driver.run_scenario(cfg, Harness(tmp_path).backends(), "Scenario-16", link, [1])
    cfg.env_file = target
    with pytest.raises(driver.RoundAbort):
        driver.prepare_round(cfg, Harness(tmp_path).backends())


def test_judge_environment_is_narrowed():
    env = {
        "PATH": "/bin",
        "HOME": "/h",
        "ITBENCH_EVALUATIONS_DIR": "/e",
        "ZAI_API_KEY": "synthetic-zai",
        "OPENAI_API_KEY": "synthetic-openai",
        "DEEPSEEK_API_KEY": "synthetic-deepseek",
        "LANGSMITH_API_KEY": "synthetic-ls",
        "OPSPILOT_WEB_DSN": "dsn",
    }
    child = driver.judge_env(env)
    assert child == {
        "PATH": "/bin",
        "HOME": "/h",
        "ITBENCH_EVALUATIONS_DIR": "/e",
        "ZAI_API_KEY": "synthetic-zai",
        "PYTHON_DOTENV_DISABLED": "1",
    }
    child = driver.judge_env({**env, "ITBENCH_JUDGE_PROVIDER": "deepseek"})
    assert "DEEPSEEK_API_KEY" in child and "ZAI_API_KEY" not in child


def test_reused_copy_that_is_a_symlink_is_refused(env, tmp_path):
    cfg, _ = env
    outside = tmp_path / "outside"
    driver.copy_whitelisted(cfg.dataset / "Scenario-16", outside)  # a valid copy
    link = tmp_path / "copy"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(driver.DeterministicError) as err:
        driver.copy_whitelisted(cfg.dataset / "Scenario-16", link)
    assert err.value.code == "UNSAFE_SNAPSHOT"
