"""#830 contract: SREGym p12's read-only Valkey account is made before the fault
by a ``before_fault_injection`` hook (R6; prereview A 830-U1 = a, 830-U2,
830-1..830-7).

Rules under test:

* the hook is one stand-alone, standard-library module with a top-level name
  of its own; the registration names it as ``hooks``; the driver copies it to
  the round directory and that directory alone is ``main.py``'s
  ``PYTHONPATH``; it imports beside a foreign top-level ``scripts`` package;
* it acts only in p12 (problem id and the job's seq 12), through
  ``kubectl exec -i deploy/valkey-cart -- valkey-cli`` with the command on
  stdin (never in argv), a 10 s timeout, and never raises;
* the driver writes the pass's password mode 600 in the trial directory for
  p12 only, fresh per pass, and removes it after the pass;
* the entry reads the hook's result: missing -> ``HOOK_RESULT_MISSING``, a
  failed hook -> ``HOOK_FAILED:<code>``, both disclosed as
  ``VALKEY_READONLY_ACCOUNT_FAILED`` with the pass going on; success is
  recorded with ``created_before_fault_injection``;
* against a real Valkey (local container, skipped without one): the account
  made while ``default`` needs no password still authenticates and passes the
  tool's own check after a password is set.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from opspilot.tools.redis_inspect import account_problem
from tests.sregym_eval_support import mod
from tests.test_sregym_eval_b_entry_contract import _record, _run, _setup
from tests.test_sregym_eval_b_round_contract import PID as P03
from tests.test_sregym_eval_kafka_contract import ASTRONOMY, _round, _summary_of
from tests.test_sregym_eval_valkey_809_contract import P12, _hook_files

HOOKS = mod("opspilot_sregym_hooks")
PASSWORD = "c0ffee" * 8


def _job(tmp_path: Path, seq: int = 12) -> tuple[Path, Path]:
    work = tmp_path / "work"
    work.mkdir()
    job = tmp_path / "job.json"
    job.write_text(
        json.dumps(
            {"seq": seq, "work_dir": str(work), "engineer_kubeconfig": "/k/eng.yaml"}
        )
    )
    return job, work


def _ctx(problem: str = P12, namespace: str | None = "astronomy-shop") -> Any:  # noqa: ANN401
    return types.SimpleNamespace(problem_id=problem, namespace=namespace)


class Run:
    def __init__(self, result: Any = None, raises: BaseException | None = None) -> None:  # noqa: ANN401
        self.calls: list[dict[str, Any]] = []
        self.result = result or subprocess.CompletedProcess([], 0, "OK\n", "")
        self.raises = raises

    def __call__(self, argv: list[str], **kwargs: Any) -> Any:  # noqa: ANN401
        self.calls.append({"argv": argv, **kwargs})
        if self.raises is not None:
            raise self.raises
        return self.result


@pytest.fixture
def hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    job, work = _job(tmp_path)
    monkeypatch.setenv("SREGYM_EVAL_JOB", str(job))
    (work / HOOKS.SECRET_FILE).write_text(PASSWORD + "\n")
    run = Run()
    monkeypatch.setattr(HOOKS.subprocess, "run", run)
    return types.SimpleNamespace(work=work, run=run, job=job)


def _result(work: Path) -> dict[str, Any]:
    return json.loads((work / HOOKS.RESULT_FILE).read_text())


# --- the hook -------------------------------------------------------------------------------------


def test_hook_creates_the_account_over_kubectl_exec_with_the_command_on_stdin(
    hook: Any,  # noqa: ANN401
) -> None:
    HOOKS.HOOKS.before_fault_injection(_ctx())
    (call,) = hook.run.calls
    assert call["argv"] == [
        "kubectl", "--kubeconfig", "/k/eng.yaml", "-n", "astronomy-shop",
        "exec", "-i", "deploy/valkey-cart", "--", "valkey-cli",
    ]  # fmt: skip
    assert PASSWORD not in " ".join(call["argv"])
    assert call["input"] == HOOKS.setuser_line(PASSWORD) + "\n"
    assert call["input"].startswith(f"ACL SETUSER opspilot_ro reset on >{PASSWORD} ")
    assert call["timeout"] == 10.0
    result = _result(hook.work)
    assert result["status"] == "created" and result["code"] is None
    assert result["before_fault_injection"] is True
    assert stat.S_IMODE((hook.work / HOOKS.RESULT_FILE).stat().st_mode) == 0o600
    assert PASSWORD not in (hook.work / HOOKS.RESULT_FILE).read_text()


@pytest.mark.parametrize(("problem", "seq"), [(P03, 12), (P12, 3)])
def test_hook_does_nothing_outside_p12(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, problem: str, seq: int
) -> None:
    job, work = _job(tmp_path, seq)
    monkeypatch.setenv("SREGYM_EVAL_JOB", str(job))
    run = Run()
    monkeypatch.setattr(HOOKS.subprocess, "run", run)
    HOOKS.before_fault_injection(_ctx(problem))
    assert run.calls == []
    assert not (work / HOOKS.RESULT_FILE).exists()


@pytest.mark.parametrize(
    ("run", "code"),
    [
        (Run(raises=subprocess.TimeoutExpired("kubectl", 10)), "EXEC_TIMEOUT"),
        (Run(raises=FileNotFoundError("kubectl")), "EXEC_FAILED:FileNotFoundError"),
        (Run(subprocess.CompletedProcess([], 0, "ERR nope\n", "")), "SETUSER_NOT_OK"),
        (
            Run(subprocess.CompletedProcess([], 1, "", f"error >{PASSWORD}\n")),
            "EXEC_EXIT_NONZERO",
        ),
    ],
)
def test_hook_failures_are_recorded_never_raised(
    hook: Any,
    monkeypatch: pytest.MonkeyPatch,
    run: Run,
    code: str,  # noqa: ANN401
) -> None:
    monkeypatch.setattr(HOOKS.subprocess, "run", run)
    HOOKS.before_fault_injection(_ctx())
    result = _result(hook.work)
    assert result["status"] == "failed" and result["code"] == code
    assert PASSWORD not in json.dumps(result)


def test_hook_without_secret_or_namespace_records_why(hook: Any) -> None:  # noqa: ANN401
    HOOKS.before_fault_injection(_ctx(namespace=None))
    assert _result(hook.work)["code"] == "NAMESPACE_MISSING"
    (hook.work / HOOKS.SECRET_FILE).unlink()
    HOOKS.before_fault_injection(_ctx())
    assert _result(hook.work)["code"] == "SECRET_MISSING"
    assert hook.run.calls == []


def test_hook_never_raises_without_a_job(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SREGYM_EVAL_JOB", raising=False)
    HOOKS.before_fault_injection(_ctx())
    monkeypatch.setenv("SREGYM_EVAL_JOB", "/nonexistent/job.json")
    HOOKS.before_fault_injection(_ctx())


def test_hook_module_imports_alone_beside_a_foreign_scripts_package(
    tmp_path: Path,
) -> None:
    """As SREGym's ``load_hooks`` would, from its checkout (which has its own
    top-level ``scripts``), with only the round's hook directory on the path
    and no site-packages (``-S``: standard library only)."""
    hooks_dir = mod("driver").install_hooks(tmp_path / "round")
    upstream = tmp_path / "sregym"
    (upstream / "scripts").mkdir(parents=True)
    (upstream / "scripts" / "__init__.py").write_text("")
    spec = mod("agent_registry").HOOKS_SPEC
    code = (
        "import importlib; m, a = %r.split(':'); "
        "h = getattr(importlib.import_module(m), a); "
        "assert callable(h.before_fault_injection); print('ok')" % spec
    )
    done = subprocess.run(
        [sys.executable, "-S", "-c", code],
        cwd=upstream,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(hooks_dir)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "ok"


# --- the driver -----------------------------------------------------------------------------------


def test_round_installs_the_hook_and_main_env_points_at_it_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = _round(tmp_path, monkeypatch, "127.0.0.1 kafka\n")
    r.prepare()
    d = mod("driver")
    hooks_dir = r.cfg.round_dir / d.HOOKS_DIR
    copied = hooks_dir / "opspilot_sregym_hooks.py"
    assert copied.read_bytes() == Path(HOOKS.__file__).read_bytes()
    env = d.main_env(r.cfg, "judge", tmp_path / "job.json")
    assert env["PYTHONPATH"] == str(hooks_dir)
    registry = (r.cfg.round_dir / "agents.yaml").read_text()
    assert "opspilot_sregym_hooks:HOOKS" in registry


def test_p12_password_file_lives_only_for_the_pass_and_is_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = _round(tmp_path, monkeypatch, "127.0.0.1 kafka\n")
    seen: list[tuple[int, str, str]] = []
    original = r.fake.run_main

    def run_main(argv, cwd, env, log_path, timeout):  # noqa: ANN001, ANN202
        work = Path(json.loads(Path(env["SREGYM_EVAL_JOB"]).read_text())["work_dir"])
        secret = work / HOOKS.SECRET_FILE
        if secret.exists():
            seen.append(
                (
                    stat.S_IMODE(secret.stat().st_mode),
                    secret.read_text().strip(),
                    str(work),
                )  # fmt: skip
            )
        return original(argv, cwd, env, log_path, timeout)

    r.fake.run_main = run_main  # type: ignore[method-assign]
    r.host = r.fake.as_host()
    r.prepare()
    d = mod("driver")
    d.run_trial(r.cfg, r.host, P12, 1)
    d.run_trial(r.cfg, r.host, P12, 2)
    d.run_trial(r.cfg, r.host, P03, 1)
    assert len(seen) == 2, seen
    (mode_a, pw_a, work_a), (mode_b, pw_b, work_b) = seen
    assert mode_a == mode_b == 0o600
    assert pw_a != pw_b and len(pw_a) >= 32
    for work in (work_a, work_b):
        assert not (Path(work) / HOOKS.SECRET_FILE).exists()
    assert "VALKEY_READONLY_ACCOUNT_BY_HARNESS" in _summary_of(r, 12)["disclosures"]


# --- the entry ------------------------------------------------------------------------------------


def _entry_p12(tmp_path: Path, files: str) -> Any:  # noqa: ANN401
    s = _setup(
        tmp_path, app=ASTRONOMY, seq=12,
        problem_digest=mod("boundary").problem_id_digest(P12),
    )  # fmt: skip
    if files == "created":
        _hook_files(s.work_dir, PASSWORD)
    elif files == "failed":
        _hook_files(s.work_dir, PASSWORD, status="failed")
    elif files == "no-result":
        (s.work_dir / HOOKS.SECRET_FILE).write_text(PASSWORD)
    checks: list[Any] = []
    s.harness.valkey_check = lambda **kw: checks.append(kw)  # type: ignore[attr-defined]
    try:
        s.harness.fake.advance("diagnosis")
        assert _run(s) == 0
    finally:
        s.harness.server.shutdown()
        s.harness.server.server_close()
    return s, checks


@pytest.mark.parametrize(
    ("files", "code"),
    [
        ("no-result", "HOOK_RESULT_MISSING"),
        ("failed", "HOOK_FAILED:EXEC_TIMEOUT"),
        ("none", "SECRET_MISSING"),
    ],
)
def test_entry_discloses_a_missing_or_failed_hook_and_goes_on(
    tmp_path: Path, files: str, code: str
) -> None:
    s, checks = _entry_p12(tmp_path, files)
    record = _record(s)
    assert checks == []
    assert record["valkey_account"]["code"] == code
    assert record["valkey_account"]["status"] == "failed"
    assert record["valkey_account"]["created_before_fault_injection"] is False
    assert record["disclosures"].count("VALKEY_READONLY_ACCOUNT_FAILED") == 1
    assert "intake" in s.harness.events
    (worker,) = s.harness.worker_envs
    assert worker["OPSPILOT_REDIS_PASSWORD"]
    assert PASSWORD not in json.dumps(record)


# --- a real Valkey: open when the hook runs, password-protected when the entry checks ---------

VALKEY_IMAGE = "valkey/valkey:8.1-alpine"


def _docker_image_present() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        done = subprocess.run(
            ["docker", "image", "inspect", VALKEY_IMAGE],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.skipif(
    not _docker_image_present(), reason=f"docker with {VALKEY_IMAGE} not present"
)
def test_real_valkey_account_made_before_auth_survives_a_password_being_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = f"opspilot-830-{os.getpid()}"
    port = _free_port()
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "--memory=64m",
         "-p", f"127.0.0.1:{port}:6379", VALKEY_IMAGE],
        check=True, capture_output=True, timeout=60,
    )  # fmt: skip
    try:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        kubectl = bin_dir / "kubectl"
        # Stands in for ``kubectl exec -i deploy/valkey-cart -- valkey-cli``.
        kubectl.write_text(f"#!/bin/sh\nexec docker exec -i {name} valkey-cli\n")
        kubectl.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
        job, work = _job(tmp_path)
        monkeypatch.setenv("SREGYM_EVAL_JOB", str(job))
        (work / HOOKS.SECRET_FILE).write_text(PASSWORD + "\n")
        for _ in range(50):
            ping = subprocess.run(
                ["docker", "exec", name, "valkey-cli", "PING"],
                capture_output=True, text=True, timeout=10, check=False,
            )  # fmt: skip
            if ping.stdout.strip() == "PONG":
                break
        HOOKS.before_fault_injection(_ctx())
        assert _result(work)["status"] == "created", _result(work)
        # The state the entry met on 11-24 (evidence swing/run.md): ``default``
        # needs a password.
        subprocess.run(
            ["docker", "exec", name, "valkey-cli", "CONFIG", "SET", "requirepass",
             "admin-830"],
            check=True, capture_output=True, timeout=10,
        )  # fmt: skip
        noauth = subprocess.run(
            ["docker", "exec", name, "valkey-cli", "PING"],
            capture_output=True, text=True, timeout=10, check=False,
        )  # fmt: skip
        assert "NOAUTH" in noauth.stdout + noauth.stderr
        entry = mod("agent_entry")
        reply = entry.valkey_check(
            port=port, username=HOOKS.VALKEY_USER, password=PASSWORD
        )
        assert account_problem(reply) is None
    finally:
        subprocess.run(
            ["docker", "rm", "-f", name], capture_output=True, timeout=30, check=False
        )
