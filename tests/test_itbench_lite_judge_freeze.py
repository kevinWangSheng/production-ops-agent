"""#604 judging process, the shared Run freeze helpers and process stop.

Synthetic ground truth written by the test itself; no dataset answer file.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import run_freeze
from scripts.itbench_lite import answer, judge_trial, services

VOCAB = ["checkout", "payment"]
GT = """groups:
  - id: g-checkout
    kind: Service
    filter: ["^checkout$"]
    root_cause: true
aliases: []
"""


def _report(summary):
    return {
        "schema_version": "m0-report-v2",
        "assessment_status": "completed",
        "conclusion": "supported",
        "summary": summary,
        "claims": [
            {
                "kind": "fact",
                "text": summary,
                "evidence_ids": ["ev-1"],
                "target_refs": ["target-1"],
                "time_scope_ref": "window-1",
            }
        ],
        "gaps": [],
        "next_steps": [],
    }


def _trial(tmp_path: Path, report) -> tuple[Path, Path]:
    gt_root = tmp_path / "gt"
    (gt_root / "Scenario-7").mkdir(parents=True)
    (gt_root / "Scenario-7" / "ground_truth.yaml").write_text(GT)
    outputs = tmp_path / "outputs"
    answer.write_trial(outputs, "Scenario-7", 2, answer.convert(report, [], VOCAB))
    return gt_root, outputs


def _run(tmp_path, gt_root, outputs, *extra):
    out = tmp_path / "scores.json"
    code = judge_trial.main(
        [
            "--ground-truth", str(gt_root), "--scenario-dir", "Scenario-7",
            "--outputs", str(outputs), "--trial", "2",
            "--work-dir", str(tmp_path / "work"), "--out", str(out), *extra,
        ]
    )  # fmt: skip
    return code, (json.loads(out.read_text()) if out.is_file() else None)


def test_deterministic_score_without_judge(tmp_path):
    gt_root, outputs = _trial(tmp_path, _report("checkout is failing"))
    code, scores = _run(tmp_path, gt_root, outputs)
    assert code == 0
    assert scores["det"]["det_f1"] == 1.0 and scores["det"]["det_pass"] == 1.0
    assert scores["official"] is None and scores["judged"] is False
    # Numbers only; nothing answer-derived is written anywhere.
    assert all(isinstance(v, float) for v in scores["det"].values())
    assert "matched_to" not in json.dumps(scores)
    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    for path in written:
        if path.parent.name != "Scenario-7" or path.parent.parent.name != "gt":
            assert "g-checkout" not in path.read_text(errors="replace"), path


def test_reportless_trial_is_zero_and_never_sent_to_the_judge(tmp_path, monkeypatch):
    monkeypatch.delenv("ITBENCH_EVALUATIONS_DIR", raising=False)
    gt_root, outputs = _trial(tmp_path, None)
    code, scores = _run(tmp_path, gt_root, outputs, "--judge")
    assert code == 0
    assert scores["official"]["root_cause_entity_f1"] == 0.0
    assert scores["official"]["root_cause_entity_pass"] == 0.0
    assert scores["det"]["det_f1"] == 0.0
    assert not (tmp_path / "work").exists()


def test_judge_configuration_failure_exits_3(tmp_path, monkeypatch):
    monkeypatch.delenv("ITBENCH_EVALUATIONS_DIR", raising=False)
    gt_root, outputs = _trial(tmp_path, _report("payment broke"))
    code, scores = _run(tmp_path, gt_root, outputs, "--judge")
    assert code == 3
    # The deterministic score is still written; the official one is absent.
    assert scores["det"]["det_f1"] == 0.0 and scores["official"] is None
    assert scores["judged"] is False and scores["judge_error"] == "ConfigError"
    assert not (tmp_path / "work").exists()


def test_judge_work_directory_is_removed_after_reading_the_row(tmp_path, monkeypatch):
    from scripts.itbench_lite import official_eval

    gt_root, outputs = _trial(tmp_path, _report("checkout is failing"))

    def fake_run(directory, ground_truth, outputs_root, work_dir, config):
        work_dir.mkdir(parents=True)
        (work_dir / "evaluation_results.json").write_text("judge text g-checkout")
        scores = work_dir / "trial_scores.json"
        scores.write_text(json.dumps({"7": [{"root_cause_entity_f1": 1.0}]}))
        return scores

    monkeypatch.setattr(official_eval, "run", fake_run)
    monkeypatch.setenv("ITBENCH_EVALUATIONS_DIR", str(tmp_path))
    code, scores = _run(tmp_path, gt_root, outputs, "--judge")
    assert code == 0
    assert scores["official"]["root_cause_entity_pass"] == 1.0
    assert not (tmp_path / "work").exists()


def test_official_row_takes_the_one_trial(tmp_path):
    path = tmp_path / "trial_scores.json"
    path.write_text(json.dumps({"7": [{"root_cause_entity_f1": 1, "x": "y"}]}))
    assert judge_trial.official_row(path, "Scenario-7", 2) == {
        "root_cause_entity_f1": 1.0
    }


# -- run_freeze -----------------------------------------------------------------


@pytest.mark.parametrize(
    "report", ["plain text, not JSON", None, ["x"], {"claims": "x"}]
)
def test_cited_ids_of_a_non_object_report_are_empty(report):
    assert run_freeze.cited_evidence_ids(report) == []


def test_cited_ids_of_a_report():
    report = {"claims": [{"evidence_ids": ["b", "a"]}, {"evidence_ids": ["a"]}, "x"]}
    assert run_freeze.cited_evidence_ids(report) == ["a", "b"]


def test_worker_interrupted_while_settling_is_stopped(tmp_path, monkeypatch):
    import time as real_time

    spawned = []
    real_sleep = real_time.sleep
    calls = {"n": 0}

    def fake_spawn(argv, env, log_path, stdout_pipe=False):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        spawned.append(proc)
        return proc

    def interrupt_once(seconds):
        # Only the settle wait is interrupted; Popen.wait polls with sleep too.
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyboardInterrupt
        real_sleep(seconds)

    monkeypatch.setattr(services, "spawn", fake_spawn)
    monkeypatch.setattr(services.time, "sleep", interrupt_once)
    with pytest.raises(KeyboardInterrupt):
        services.start_worker({}, tmp_path / "w.log", settle_seconds=5)
    assert spawned and spawned[0].poll() is not None


def test_report_tokens_and_flat_evidence():
    steps = [
        {"response": {"kind": "tool_calls", "usage": {"prompt_tokens": 10, "completion_tokens": 2}}},
        {"response": {"kind": "conclusion", "usage": {"prompt_tokens": 5, "total_tokens": 9},
                      "conclusion": {"report_content": json.dumps({"summary": "s"})}},
         "sequence": 2},
    ]  # fmt: skip
    report, step = run_freeze.report_of(steps)
    assert report == {"summary": "s"} and step["sequence"] == 2
    usage = run_freeze.token_usage(steps)
    assert usage["prompt_tokens"] == 15 and usage["model_rounds_with_usage"] == 2
    assert run_freeze.report_of([{"response": {"kind": "tool_calls"}}]) == (None, None)
    block = {
        "observations": [{"evidence_id": "e", "tool": "t", "query": {"a": 1}, "x": 2}]
    }
    assert run_freeze.flat_evidence(block) == [
        {"evidence_id": "e", "tool": "t", "query": {"a": 1}}
    ]
    assert run_freeze.relabel({"a": [{"idempotency_key": 1}]}) == {
        "a": [{"idempotency_label": 1}]
    }


class _Client:
    def __init__(self, polls_before_found=1, fail=False):
        self.polls = polls_before_found
        self.fail = fail
        self.filters = []

    def read_project(self, project_name):
        if self.fail:
            raise ConnectionError("down")
        return SimpleNamespace(id="proj-1", trace_tier="longlived")

    def list_runs(self, **kwargs):
        self.filters.append(kwargs["filter"])
        if self.polls:
            self.polls -= 1
            return []
        return [SimpleNamespace(id="ls-1", start_time=None, end_time=None)]

    def _get_optional_tenant_id(self):
        return "tenant"


def test_trace_roots_polls_by_business_run_id():
    client = _Client()
    out = run_freeze.trace_roots(
        "opspilot-lab-x", "r-1", client_factory=lambda: client, sleep=lambda s: None
    )
    assert out["read_back"] == "found" and out["roots"][0]["langsmith_run_id"] == "ls-1"
    assert "/o/tenant/projects/p/proj-1/r/ls-1" in out["roots"][0]["url"]
    assert all('"run_id"' in f and '"r-1"' in f for f in client.filters)
    assert run_freeze.trace_roots(None, "r", client_factory=lambda: client) == {
        "project": None,
        "read_back": "not_exported",
        "roots": [],
    }
    failed = run_freeze.trace_roots(
        "opspilot-lab-x", "r", client_factory=lambda: _Client(fail=True)
    )
    assert failed["read_back"] == "error:ConnectionError"


def test_trace_roots_records_not_found_after_bounded_polls():
    client = _Client(polls_before_found=99)
    out = run_freeze.trace_roots(
        "opspilot-lab-x",
        "r",
        client_factory=lambda: client,
        attempts=3,
        sleep=lambda s: None,
    )
    assert out["read_back"] == "root_run_not_found" and len(client.filters) == 3


# -- process stop (L-D20) -----------------------------------------------------------


def test_stop_process_escalates_to_kill_of_the_same_pid(tmp_path):
    proc = subprocess.Popen(
        [sys.executable, "-c",
         "import signal,time,sys;signal.signal(signal.SIGTERM, signal.SIG_IGN);"
         "print('ready',flush=True);time.sleep(60)"],
        stdout=subprocess.PIPE,
    )  # fmt: skip
    try:
        assert proc.stdout.readline().strip() == b"ready"
        services.stop_process(proc, grace=0.5)
        assert proc.returncode == -9
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()


def test_started_stop_is_idempotent():
    calls = []
    item = services.Started("x", lambda: calls.append(1))
    item.stop()
    item.stop()
    assert calls == [1]


@pytest.mark.parametrize("bad", ["0.0.0.0", "192.168.1.5"])
def test_deliver_refuses_non_loopback(bad):
    from scripts.itbench_lite import driver

    with pytest.raises(ValueError):
        driver.deliver(f"http://{bad}:29180/intake/alertmanager", "t", {})
