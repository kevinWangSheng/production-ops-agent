"""Implementer unit tests for #667 PR-B (the contract tests are separate)."""

from __future__ import annotations

import csv
import json
import os
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.holmes_evals.intake import SubmitResult
from scripts.itbench_lite.driver import RoundAbort
from scripts.itbench_lite.services import ServiceError, Started
from scripts.sregym_eval import agent_entry, boundary, driver, identity
from scripts.sregym_eval.agent_registry import registry_document
from scripts.sregym_eval.boundary import (
    CAN_I_CHECKS,
    check_no_problem_id_digest,
    check_product_env,
    check_rbac_manifest,
    problem_id_digest,
)
from scripts.sregym_eval.fake_conductor import FakeConductor, serve
from scripts.sregym_eval.identity import K8sTarget
from scripts.sregym_eval.job import Job, read_job, write_job
from scripts.sregym_eval.ports import Ports
from scripts.sregym_eval.question import AnswerLeak
from scripts.sregym_eval.upstream import LITE_PROBLEMS

PID = LITE_PROBLEMS[2]
APP = {
    "app_name": "Hotel Reservation",
    "namespace": "hotel-reservation",
    "namespaces": ["hotel-reservation"],
    "descriptions": "A hotel booking demo.",
}


def test_rbac_manifest_real_passes_and_mutants_fail() -> None:
    text = identity.RBAC_MANIFEST.read_text()
    assert check_rbac_manifest(text) == []
    assert "VERB:watch" in check_rbac_manifest(
        text.replace("verbs: [get, list]", "verbs: [get, list, watch]", 1)
    )
    assert "RESOURCE:secrets" in check_rbac_manifest(
        text.replace("- configmaps", "- secrets", 1)
    )
    assert "RESOURCE:pods/exec" in check_rbac_manifest(
        text.replace("- pods/log", "- pods/exec", 1)
    )
    assert check_rbac_manifest("kind: Namespace\n") == ["NO_ROLE"]


def test_can_i_verdict() -> None:
    good = {c: c.expect for c in CAN_I_CHECKS}
    assert boundary.can_i_verdict(good) == []
    bad = dict(good)
    bad[CAN_I_CHECKS[0]] = "yes"
    assert boundary.can_i_verdict(bad) == ["CAN_I:create:deployments.apps:yes"]
    worse = {c: v for c, v in good.items() if c.label != "list pods"}
    assert boundary.can_i_verdict(worse) == ["CAN_I:list:pods:missing"]
    noisy = dict(good)
    noisy[CAN_I_CHECKS[-1]] = "error: forbidden"
    assert boundary.can_i_verdict(noisy) == ["CAN_I:list:events:invalid"]
    exec_check = next(c for c in CAN_I_CHECKS if c.resource == "pods/exec")
    assert boundary.can_i_argv(exec_check)[3:6] == [
        "create",
        "pods",
        "--subresource=exec",
    ]


def test_product_env_isolation() -> None:
    check_product_env({"PATH": "/bin"}, role="web", forbidden_values=["s3cret-token"])
    for env, code in (
        ({"KUBECONFIG": "/x"}, "ENV_ISOLATION:worker:KUBECONFIG"),
        ({"JUDGE_API_KEY": "k"}, "ENV_ISOLATION:worker:JUDGE_API_KEY"),
        ({"X": "has s3cret-token"}, "ENV_ISOLATION:worker:X"),
        (
            {"OPSPILOT_LOKI_URL": "http://127.0.0.1:29531"},
            "ENV_ISOLATION:worker:OPSPILOT_LOKI_URL",
        ),
    ):
        with pytest.raises(boundary.BoundaryError, match=code):
            check_product_env(env, role="worker", forbidden_values=["s3cret-token"],
                              backend_addresses=["127.0.0.1:29531"])  # fmt: skip
    with pytest.raises(boundary.BoundaryError, match="web:OPSPILOT_LOKI_PASSWORD"):
        check_product_env(
            {"OPSPILOT_LOKI_PASSWORD": "p"}, role="web", forbidden_values=[]
        )


def test_digest_leak_check() -> None:
    digest = problem_id_digest(PID)
    check_no_problem_id_digest(digest, {"q": "Social Network network policy"})
    with pytest.raises(AnswerLeak, match="PROBLEM_ID_IN:q"):
        check_no_problem_id_digest(digest, {"q": "x Network-Policy_Block y"})


def test_main_command_and_env(tmp_path: Path) -> None:
    argv = driver.main_command(PID)
    assert argv[:3] == ["uv", "run", "main.py"] and "--stages" in argv
    assert not set(argv) & set(driver.FORBIDDEN_MAIN_FLAGS)
    cfg = driver.Config(
        round="r1", upstream=tmp_path, kubeconfig=tmp_path / "kc", tmp_root=tmp_path
    )
    env = driver.main_env(cfg, "zai-key", tmp_path / "job.json")
    assert (
        env["JUDGE_API_BASE"] == driver.JUDGE_API_BASE
        and env["KIND_CLUSTER_NAME"] == "sregym"
    )
    assert "DEEPSEEK_API_KEY" not in env
    doc = registry_document("/py/python", "/repo")
    assert doc["agents"][0]["container_isolation"] is False


def test_job_round_trip(tmp_path: Path) -> None:
    job = _job(tmp_path)
    write_job(tmp_path / "job.json", job)
    assert oct(os.stat(tmp_path / "job.json").st_mode & 0o777) == "0o600"
    assert read_job(tmp_path / "job.json") == job
    assert PID not in (tmp_path / "job.json").read_text()


def _job(tmp_path: Path, **kw: Any) -> Job:
    base: dict[str, Any] = dict(
        round="r1", seq=3, trial=1, work_dir=str(tmp_path / "work"),
        cluster_path="direct_readonly", k8s_target_file=str(tmp_path / "t.json"),
        engineer_kubeconfig=str(tmp_path / "kc"), secrets_file=str(tmp_path / "s.env"),
        loki_env_file=str(tmp_path / "loki.env"), problem_digest=problem_id_digest(PID),
        diagnosis_wait_s=5.0, submit_margin_s=0.0, done_wait_s=5.0,
    )  # fmt: skip
    base.update(kw)
    return Job(**base)


class _Pg(SimpleNamespace):
    pass


def _entry_host(
    conductor_url: str, calls: list[str], *, forward_fails: bool = False
) -> agent_entry.EntryHost:
    def started(name: str) -> Started:
        return Started(name, lambda: calls.append(f"stop:{name}"))

    def forward(spec: dict[str, Any], env: dict[str, str], log: Path) -> Started:
        assert env["KUBECONFIG"].endswith("kc")
        if forward_fails and spec["service"] == "jaeger-out":
            raise ServiceError("PORT_FORWARD_FAILED")
        return started(f"pf:{spec['service']}")

    return agent_entry.EntryHost(
        conductor=lambda _url: agent_entry.ConductorClient(conductor_url),
        start_port_forward=forward,
        start_pg=lambda d, p: _Pg(owner_dsn="o", web_dsn="w", runtime_dsn="r"),
        stop_pg=lambda pg: calls.append("stop:pg"),
        start_web=lambda env, port, log: (calls.append("web:" + json.dumps(env)), started("web"))[1],
        start_worker=lambda env, log, settle_seconds: (calls.append("worker:" + json.dumps(env)), started("worker"))[1],
        submit=lambda url, account, **kw: (calls.append("q:" + kw["question"]), SubmitResult(201, "i", "run-1", False, None))[1],
        run_state=lambda dsn, run_id: "succeeded",
        collect=lambda dsn, run_id: ({"report": None, "outcome": {"report_available": False}}, {"rows": []}),
        sleep=lambda s: None,
    )  # fmt: skip


def _files(tmp_path: Path) -> None:
    (tmp_path / "kc").write_text(
        "users:\n- user:\n    token: engineer-admin-token-xyz\n"
    )
    identity.write_target(
        tmp_path / "t.json",
        K8sTarget("https://127.0.0.1:26444", str(tmp_path / "ca"), "product-token-abc"),
    )
    (tmp_path / "s.env").write_text("DEEPSEEK_API_KEY=fake\n")
    (tmp_path / "loki.env").write_text(
        "OPSPILOT_LOKI_USERNAME=reader\nOPSPILOT_LOKI_PASSWORD=fake-pass\n"
    )


def test_entry_no_report_submits_empty_once(tmp_path: Path) -> None:
    _files(tmp_path)
    fake = FakeConductor(APP)
    fake.advance("diagnosis")
    server = serve(fake)
    calls: list[str] = []
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        job = _job(tmp_path)
        write_job(tmp_path / "job.json", job)
        threading.Timer(0.5, lambda: fake.advance("done")).start()
        environ = {"SREGYM_EVAL_JOB": str(tmp_path / "job.json"), "KUBECONFIG": "/sregym/agent-kc",
                   "JUDGE_API_KEY": "judge-secret-key", "API_HOSTNAME": "0.0.0.0"}  # fmt: skip
        code = agent_entry.main(environ, _entry_host(url, calls, forward_fails=True))
    finally:
        server.shutdown()
    assert code == 0
    posts = [r for r in fake.requests if r.method == "POST"]
    assert [r.body for r in posts] == [{"stage": "diagnosis", "solution": ""}]
    record = json.loads((tmp_path / "work" / "entry-record.json").read_text())
    assert (
        record["submission"]["submitted"] is True
        and record["submission"]["empty"] is True
    )
    assert (
        record["observability"]["jaeger"] == "missing" and record["leak_check"] == "ok"
    )
    web = json.loads(next(c for c in calls if c.startswith("web:"))[4:])
    worker = json.loads(next(c for c in calls if c.startswith("worker:"))[7:])
    assert (
        "OPSPILOT_LOKI_PASSWORD" not in web
        and worker["OPSPILOT_LOKI_PASSWORD"] == "fake-pass"
    )
    assert worker["OPSPILOT_OTEL_JAEGER_URL"].endswith(":29599")
    assert worker["OPSPILOT_K8S_NAMESPACE"] == "hotel-reservation"
    blob = json.dumps(calls) + (tmp_path / "work" / "entry-record.json").read_text()
    for secret in ("engineer-admin-token-xyz", "judge-secret-key", "/sregym/agent-kc"):
        assert secret not in blob
    assert "stop:worker" in calls and "stop:pg" in calls


def test_entry_refused_submission_is_not_submitted(tmp_path: Path) -> None:
    _files(tmp_path)
    fake = FakeConductor(APP)
    fake.advance("diagnosis")
    server = serve(fake)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        write_job(tmp_path / "job.json", _job(tmp_path, done_wait_s=0.0))
        host = _entry_host(url, [])
        original = host.collect

        def collect(dsn: str, run_id: str) -> Any:
            fake.fail_next(503)
            return original(dsn, run_id)

        host.collect = collect
        code = agent_entry.main({"SREGYM_EVAL_JOB": str(tmp_path / "job.json")}, host)
    finally:
        server.shutdown()
    assert code == agent_entry.EXIT_SUBMIT
    record = json.loads((tmp_path / "work" / "entry-record.json").read_text())
    assert (
        record["submission"]["submitted"] is False
        and record["submission"]["error_code"] == "SUBMIT_UNAVAILABLE"
    )


def test_entry_bad_job() -> None:
    assert agent_entry.main({}, agent_entry.EntryHost()) == agent_entry.EXIT_BAD_JOB


# -- driver end to end (fake SREGym) ----------------------------------------------------------------


class _Kubectl:
    def __init__(self, can_i: dict[str, str]) -> None:
        self.can_i = can_i

    def __call__(self, argv: list[str], env: dict[str, str], timeout: float) -> Any:
        joined = " ".join(argv)
        out = ""
        if "current-context" in joined:
            out = "kind-sregym\n"
        elif "config view" in joined:
            out = json.dumps({"clusters": [{"cluster": {"server": "https://127.0.0.1:41234",
                                                        "certificate-authority-data": "Q0E="}}]})  # fmt: skip
        elif "create token" in joined:
            out = "readonly-sa-token-999\n"
        elif "auth can-i" in joined:
            label = f"{argv[3]} {argv[4]}" + (
                f"/{argv[5].split('=')[1]}"
                if argv[5].startswith("--subresource")
                else ""
            )
            out = self.can_i.get(label, "no") + "\n"
        return subprocess.CompletedProcess(argv, 0, out, "")


def _fake_gateway(
    backend: str, port: int, upstream: str, state: Path, out: Path
) -> Any:
    assert upstream == "127.0.0.1:29531" and port == 29532
    out.write_text(
        "OPSPILOT_LOKI_URL=http://127.0.0.1:29532\nOPSPILOT_LOKI_USERNAME=reader\nOPSPILOT_LOKI_PASSWORD=fake-pass\n"
    )
    return Started("gw", lambda: None), {}


def _driver_host(tmp_path: Path, can_i: dict[str, str]) -> driver.Host:
    calls: list[str] = []

    def run_main(
        argv: list[str], cwd: Path, env: dict[str, str], log: Path, timeout: float
    ) -> int:
        assert "DEEPSEEK_API_KEY" not in env
        fake = FakeConductor(APP)
        fake.advance("diagnosis")
        server = serve(fake, port=Ports().conductor)
        try:
            threading.Timer(0.5, lambda: fake.advance("done")).start()
            entry_env = {**env, "API_HOSTNAME": "0.0.0.0"}
            agent_entry.main(
                entry_env, _entry_host(f"http://127.0.0.1:{Ports().conductor}", calls)
            )
        finally:
            server.shutdown()
            server.server_close()
        run_dir = cwd / "results" / "1119_1200" / "opspilot" / PID
        run_dir.mkdir(parents=True)
        with (run_dir / f"{PID}_opspilot_results.csv").open("w", newline="") as handle:
            w = csv.DictWriter(
                handle,
                fieldnames=["attempt", "Diagnosis.success", "Diagnosis.reasoning"],
            )
            w.writeheader()
            w.writerow(
                {
                    "attempt": "1",
                    "Diagnosis.success": "False",
                    "Diagnosis.reasoning": "ROOT-CAUSE-CANARY",
                }
            )
        return 0

    return driver.Host(
        kubectl=_Kubectl(can_i),
        run_main=run_main,
        start_gateway=_fake_gateway,
        port_in_use=lambda port: False,
        http_probe=lambda method, url, token, ca, body=None: (
            200 if method == "GET" else 403
        ),
        trace_roots=lambda project, run_id, env: {"project": None},
    )


def _cfg(tmp_path: Path) -> driver.Config:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    (tmp_path / "kc").write_text("token: engineer-admin-token-xyz\n")
    (tmp_path / "env").write_text("DEEPSEEK_API_KEY=fake\nZAI_API_KEY=fake\n")
    return driver.Config(
        round="r1", upstream=upstream, kubeconfig=tmp_path / "kc", env_file=tmp_path / "env",
        problems=(PID,), cluster_path="direct_readonly", tmp_root=tmp_path / "tmp",
        evidence_root=tmp_path / "ev", baseline=tmp_path / "no-baseline", allow_dirty=True,
    )  # fmt: skip


def test_driver_trial_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(driver, "verify_upstream", lambda root: None)
    monkeypatch.setenv("OPSPILOT_LEDGER_DIR", str(tmp_path / "ledger"))
    cfg = _cfg(tmp_path)
    host = _driver_host(tmp_path, {c.label: c.expect for c in CAN_I_CHECKS})
    outcomes = driver.run_round(cfg, host, (1,))
    assert [o.status for o in outcomes] == ["completed"]  # a Run without a report
    summary = json.loads(cfg.summary_path(PID, 1).read_text())
    block = summary["sregym_eval"]
    assert block["category"] == "no_report" and block["submission"]["submitted"] is True
    text = cfg.summary_path(PID, 1).read_text()
    assert "ROOT-CAUSE-CANARY" not in text and "readonly-sa-token-999" not in text
    assert not (cfg.results_root / driver.LOCK_NAME).exists()
    assert driver.run_round(cfg, host, (1,))[0].status == "skipped"


def test_driver_can_i_failure_aborts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(driver, "verify_upstream", lambda root: None)
    cfg = _cfg(tmp_path)
    answers = {c.label: c.expect for c in CAN_I_CHECKS}
    answers["patch pods"] = "yes"
    with pytest.raises(RoundAbort, match="CAN_I_FAILED"):
        driver.run_round(cfg, _driver_host(tmp_path, answers), (1,))
    assert not cfg.summary_path(PID, 1).exists()


def test_results_lock_and_new_csv(tmp_path: Path) -> None:
    with driver.ResultsLock(tmp_path):
        with pytest.raises(RoundAbort, match="RESULTS_ROOT_LOCKED"):
            driver.ResultsLock(tmp_path)
    old = tmp_path / "old" / "opspilot" / PID
    old.mkdir(parents=True)
    (old / f"{PID}_opspilot_results.csv").write_text("attempt\n1\n")
    before = driver.run_dirs(tmp_path)
    assert driver.new_result_csv(tmp_path, PID, before) is None


def test_sigterm_after_submission_keeps_record_and_stops_product(
    tmp_path: Path,
) -> None:
    import signal

    _files(tmp_path)
    fake = FakeConductor(APP)
    fake.advance("diagnosis")
    server = serve(fake)
    calls: list[str] = []
    before = signal.getsignal(signal.SIGTERM)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        write_job(tmp_path / "job.json", _job(tmp_path, done_wait_s=60.0))
        host = _entry_host(url, calls)
        # SREGym's deadline: SIGTERM while the entry waits for ``done``.
        host.sleep = lambda s: os.kill(os.getpid(), signal.SIGTERM)
        code = agent_entry.main({"SREGYM_EVAL_JOB": str(tmp_path / "job.json")}, host)
    finally:
        server.shutdown()
    assert code == agent_entry.EXIT_TERMINATED
    assert signal.getsignal(signal.SIGTERM) == before
    record = json.loads((tmp_path / "work" / "entry-record.json").read_text())
    assert record["submission"]["submitted"] is True
    assert record["terminated"] == "SystemExit" and record["exit_code"] == 7
    for name in ("stop:worker", "stop:web", "stop:pg", "stop:pf:prometheus-server"):
        assert name in calls


def test_sigterm_before_submission_still_stops_product(tmp_path: Path) -> None:
    import signal

    _files(tmp_path)
    fake = FakeConductor(APP)
    fake.advance("diagnosis")
    server = serve(fake)
    calls: list[str] = []
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        write_job(tmp_path / "job.json", _job(tmp_path))
        host = _entry_host(url, calls)
        host.run_state = lambda dsn, run_id: os.kill(os.getpid(), signal.SIGTERM)  # type: ignore[assignment,func-returns-value]
        code = agent_entry.main({"SREGYM_EVAL_JOB": str(tmp_path / "job.json")}, host)
    finally:
        server.shutdown()
    assert code == agent_entry.EXIT_TERMINATED
    assert not [r for r in fake.requests if r.method == "POST"]
    record = json.loads((tmp_path / "work" / "entry-record.json").read_text())
    assert record["submission"] is None and record["terminated"] == "SystemExit"
    assert "stop:worker" in calls and "stop:pg" in calls


def test_missing_entry_record_with_judged_csv_counts_by_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(driver, "verify_upstream", lambda root: None)
    monkeypatch.setenv("OPSPILOT_LEDGER_DIR", str(tmp_path / "ledger"))
    cfg = _cfg(tmp_path)
    host = _driver_host(tmp_path, {c.label: c.expect for c in CAN_I_CHECKS})

    def run_main(
        argv: list[str], cwd: Path, env: dict[str, str], log: Path, timeout: float
    ) -> int:
        # The entry was SIGKILLed before writing anything; SREGym judged it.
        run_dir = cwd / "results" / "1120_0100" / "opspilot" / PID
        run_dir.mkdir(parents=True)
        with (run_dir / f"{PID}_opspilot_results.csv").open("w", newline="") as handle:
            w = csv.DictWriter(handle, fieldnames=["attempt", "Diagnosis.success"])
            w.writeheader()
            w.writerow({"attempt": "1", "Diagnosis.success": "True"})
        return 0

    host.run_main = run_main
    driver.run_round(cfg, host, (1,))
    block = json.loads(cfg.summary_path(PID, 1).read_text())["sregym_eval"]
    assert block["category"] == "ok" and block["passed"] is True
    assert block["submission"]["error_code"] == "ENTRY_RECORD_MISSING"
    assert "ENTRY_RECORD_MISSING" in block["disclosures"]


_IGNORES_TERM = (
    "import signal, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "print('ready', flush=True)\n"
    "time.sleep(600)\n"
)


def _stubborn_child() -> subprocess.Popen[str]:
    import sys

    proc = subprocess.Popen(
        [sys.executable, "-c", _IGNORES_TERM], stdout=subprocess.PIPE, text=True
    )
    assert proc.stdout is not None and proc.stdout.readline().strip() == "ready"
    return proc


def test_terminate_all_kills_a_process_ignoring_sigterm() -> None:
    import time

    proc = _stubborn_child()
    try:
        target = agent_entry.ProcTarget("worker", proc.pid, lambda: proc.poll() is None)
        began = time.monotonic()
        states = agent_entry.terminate_all([target], wait_s=0.5)
        elapsed = time.monotonic() - began
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(5)
    assert states == {"worker": "killed"}
    assert proc.returncode == -9
    assert elapsed < 0.5 + agent_entry.KILL_WAIT_S


def test_sigterm_path_stops_stubborn_worker_within_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import signal
    import time

    monkeypatch.setattr(agent_entry, "TERM_WAIT_S", 0.5)
    _files(tmp_path)
    fake = FakeConductor(APP)
    fake.advance("diagnosis")
    server = serve(fake)
    proc = _stubborn_child()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        write_job(tmp_path / "job.json", _job(tmp_path))
        host = _entry_host(url, [])
        host.start_worker = lambda env, log, settle_seconds: Started(  # type: ignore[assignment]
            "worker",
            lambda: None,
            info={"pid": proc.pid},
            alive=lambda: proc.poll() is None,
        )
        host.run_state = lambda dsn, run_id: os.kill(os.getpid(), signal.SIGTERM)  # type: ignore[assignment,func-returns-value]
        began = time.monotonic()
        code = agent_entry.main({"SREGYM_EVAL_JOB": str(tmp_path / "job.json")}, host)
        elapsed = time.monotonic() - began
    finally:
        server.shutdown()
        if proc.poll() is None:
            proc.kill()
        proc.wait(5)
    assert code == agent_entry.EXIT_TERMINATED
    assert proc.returncode == -9
    assert elapsed < agent_entry.CLEANUP_BUDGET_S
    record = json.loads((tmp_path / "work" / "entry-record.json").read_text())
    assert record["termination"]["processes"]["worker"] == "killed"
    assert record["termination"]["stop_callbacks"] == "done"


def test_port_busy_names_port_and_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(driver, "verify_upstream", lambda root: None)
    cfg = _cfg(tmp_path)
    host = _driver_host(tmp_path, {c.label: c.expect for c in CAN_I_CHECKS})
    host.port_in_use = lambda port: port == cfg.ports.web
    host.port_owner = lambda port: "pid=4242 cmd=python"
    with pytest.raises(RoundAbort) as info:
        driver.run_round(cfg, host, (1,))
    assert info.value.code == "PORT_BUSY"
    assert info.value.detail == "web:29580(pid=4242 cmd=python)"


def test_port_owner_reads_lsof() -> None:
    import shutil
    import socket

    if shutil.which("lsof") is None:
        pytest.skip("lsof not installed")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert driver.port_owner(port).startswith(f"pid={os.getpid()}")
    assert driver.port_owner(port) == "pid=unknown"
