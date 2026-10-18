"""Implementer tests for the cooperative interrupts of the live driver (r16).

Interface: docs/tasks/2026-10-18-eval-pr-c-interface.md section 15 and the
r16 rulings R1-R10; issue #273 lists the five windows covered below. Every
test delivers real signals to the process (``signal.raise_signal``) while the
driver's own handler is installed (``main`` or ``eval_live._signals``), in the
middle of the section under test, and checks the observable result: exit
code, final JSON, evidence files and the lab state. Offline: the fake lab of
the driver tests, no kubectl, network, PostgreSQL or model call.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from scripts import eval_live, eval_live_lab
from scripts.eval_live_lab import Interrupted, LabError
from tests.unit import test_eval_c_driver_impl as d
from tests.unit import test_eval_c_probe_impl as p

CART, NORMAL = "eval-cart-failure", "eval-normal-1"


@pytest.fixture
def env(tmp_path: Path):
    return d.make_env(tmp_path)


def _signal(sig: signal.Signals = signal.SIGTERM) -> None:
    signal.raise_signal(sig)


def _run(env, capsys, *scenarios: str) -> tuple[int, dict]:
    argv = [*d.LIVE_ARGV, *p._scenarios(*scenarios)]
    code = eval_live.main(argv, deps=env["deps"])
    return code, d._last_json(capsys)


def _probe(deps, capsys, *scenarios: str) -> tuple[int, dict]:
    code = eval_live.main([*p.ARGV, *p._scenarios(*scenarios)], deps=deps)
    return code, d._last_json(capsys)


def _ledger_events(deps, round_id: str = "r1") -> list[tuple[str, str]]:
    rows = []
    for path in (deps.ledger_root / round_id).rglob("lifecycle.jsonl"):
        rows += [json.loads(line) for line in path.read_text().splitlines()]
    return [(row["stage"], row["event"]) for row in rows]


# -- #273 item 1: PG start interrupted, its cleanup cannot stop the server -----


def test_w1_pg_start_cleanup_finishes_and_reports_stop_failed(tmp_path, monkeypatch):
    """A signal during ``pg_ctl start`` and a second one during the cleanup
    stop: the cleanup is protected, so the stop runs to its end and its
    failure (PG_STOP_FAILED) is reported; the stop request stays set."""
    data = tmp_path / "pg"
    calls: list[str] = []

    def run(command, **kwargs):
        action = command[-1]
        calls.append(action)
        if Path(command[0]).name == "initdb":
            data.mkdir()
        elif action == "start":
            (data / "postmaster.pid").write_text(f"4242\n{data}\n0\n55998\n")
            _signal()  # the operator stops while the server starts
        elif action == "stop":
            _signal(signal.SIGINT)  # a second signal inside the cleanup
            calls.append("stop_returned")
            raise subprocess.CalledProcessError(1, command)
        return d._Completed(0)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(eval_live_lab, "_pg_binary", lambda name: f"/bin/{name}")
    with eval_live._signals():
        with pytest.raises(LabError, match="PG_STOP_FAILED"):
            eval_live_lab.LocalPgLauncher().start(data, 55998)
        assert eval_live._DEFER["pending"] is True
    assert calls[-2:] == ["stop", "stop_returned"]
    assert "--encoding=UTF8" in calls  # initdb ran; nothing after the start


def test_w1_pg_stop_failed_and_interrupt_are_both_kept(env, capsys):
    """R9: closure_reason PG_STOP_FAILED, later slots stopped, exit 130."""
    deps = env["deps"]

    def start(directory, port):
        _signal()
        raise LabError("PG_STOP_FAILED")  # the start's cleanup could not stop

    env["pg"].start = start
    code, final = _run(env, capsys, CART, NORMAL)
    assert (code, final["exit_code"], final["status"]) == (130, 130, "interrupted")
    first, later = d.lifecycle(deps)["slots"]
    assert first["closure_reason"] == "PG_STOP_FAILED"
    assert first["reason"] == "PG_STOP_FAILED" and first["summary"] is not None
    assert later["status"] == "not_started" and later["reason"] == "ROUND_STOPPED"
    assert env["lab"].writes == []


# -- #273 item 2: interrupted after the intent was unlinked -------------------


def _signal_after_unlink(monkeypatch, deps, sigs=(signal.SIGTERM, signal.SIGINT)):
    """Both signals right after ``fault-active.json`` is removed: the next
    step of the restore is its ``wash passed`` ledger event."""
    real = eval_live_lab.AttemptLedger.event
    fired: list[bool] = []

    def event(self, stage, name, **detail):
        if (stage, name) == ("wash", "passed") and not fired:
            fired.append(True)
            assert not (deps.ledger_root / "fault-active.json").exists()
            for sig in sigs:
                _signal(sig)
        return real(self, stage, name, **detail)

    monkeypatch.setattr(eval_live_lab.AttemptLedger, "event", event)
    return fired


def test_w2_run_interrupted_after_unlink_is_restored_not_a_false_4(
    env, capsys, monkeypatch
):
    deps = env["deps"]
    fired = _signal_after_unlink(monkeypatch, deps)
    code, final = _run(env, capsys, CART, NORMAL)
    assert fired
    assert (code, final["exit_code"], final["status"]) == (130, 130, "interrupted")
    assert not (deps.ledger_root / "fault-active.json").exists()
    assert env["lab"].raw == d.ORIGINAL
    first, later = d.lifecycle(deps)["slots"]
    assert first["closure_reason"] is None
    assert first["fault"]["normal_confirmed_at"] is not None
    assert first["fault"]["washed_at"] is not None
    assert later["status"] == "not_started"
    assert env["pg"].stopped == env["pg"].started
    assert ("wash", "passed") in _ledger_events(deps)


def test_w2_probe_interrupted_after_unlink_is_restored_not_a_false_4(
    tmp_path, capsys, monkeypatch
):
    env, deps = p._probe_env(tmp_path)
    fired = _signal_after_unlink(monkeypatch, deps)
    code, final = _probe(deps, capsys, CART, NORMAL)
    assert fired
    assert (code, final["exit_code"], final["status"]) == (130, 130, "interrupted")
    report = json.loads((deps.evidence_root / "r1/probe.json").read_text())
    entry = report["scenarios"][0]
    assert entry["closure_reason"] is None
    assert entry["fault"]["restored"] is True and entry["fault"]["washed"] is True
    assert report["retained_fault"] is False
    assert len(report["scenarios"]) == 1  # the next slot never started
    assert not (deps.ledger_root / "fault-active.json").exists()


# -- #273 item 3: interrupted during the propagation wait ---------------------


def test_w3_probe_fault_record_exists_before_the_propagation_wait(tmp_path, capsys):
    env, deps = p._probe_env(tmp_path)
    lab = env["lab"]
    real = lab.resolve
    fired: list[bool] = []

    def resolve(flag, method, timeout):
        if lab.writes and not fired:  # the injection is committed
            fired.append(True)
            _signal()
        return real(flag, method, timeout)

    lab.resolve = resolve  # type: ignore[method-assign]
    code, final = _probe(deps, capsys, CART)
    assert fired
    assert (code, final["exit_code"], final["status"]) == (130, 130, "interrupted")
    entry = json.loads((deps.evidence_root / "r1/probe.json").read_text())["scenarios"][
        0
    ]
    fault = entry["fault"]
    assert fault is not None
    assert fault["original_sha256"] and fault["injected_sha256"]
    assert fault["effective_at"] is None  # propagation never confirmed (R6)
    assert fault["restored"] is True and fault["washed"] is True
    assert entry["interrupted"] is True and entry["closure_reason"] is None
    assert lab.raw == d.ORIGINAL
    assert not (deps.ledger_root / "fault-active.json").exists()


# -- #273 item 4: a signal between the closure and the evidence ---------------


def test_w4_signal_after_the_closure_still_freezes_and_writes_the_attempt(
    env, capsys, monkeypatch
):
    deps = env["deps"]
    real = eval_live._timings
    fired: list[bool] = []

    def timings(state, ended):
        fired.append(True)
        _signal()  # after the closure, while the evidence is prepared
        return real(state, ended)

    monkeypatch.setattr(eval_live, "_timings", timings)
    code, final = _run(env, capsys, CART, NORMAL)
    assert fired == [True]
    assert (code, final["exit_code"], final["status"]) == (130, 130, "interrupted")
    first, later = d.lifecycle(deps)["slots"]
    assert first["attempt_id"] and first["summary"] is not None
    stored = d.summary(deps, first["attempt_id"])["capture"]["attempt"]
    assert stored["status"] == first["status"]
    assert first["timings"]["closure_s"] is not None
    assert later["status"] == "not_started"
    assert (deps.evidence_root / "r1/aggregate.json").exists()
    assert not (deps.ledger_root / "fault-active.json").exists()


# -- #273 item 5: the active marker teardown ----------------------------------


def test_w5_signal_while_the_active_marker_is_removed(env, capsys, monkeypatch):
    deps = env["deps"]
    real = Path.unlink
    fired: list[bool] = []

    def unlink(self, missing_ok=False):
        if self.name.startswith(".active-") and not fired:
            fired.append(True)
            _signal()
        return real(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    code, final = _run(env, capsys, NORMAL)
    assert fired
    assert (code, final["exit_code"], final["status"]) == (130, 130, "interrupted")
    assert not list(deps.ledger_root.glob(".active-*.json"))
    assert (deps.evidence_root / "r1/aggregate.json").exists()
    assert d.lifecycle(deps)["round_status"] == "interrupted"


# -- R2: the second signal ----------------------------------------------------


def test_a_second_signal_outside_protected_sections_forces_the_interrupt(env, capsys):
    """The first signal only records the stop; the second raises at once,
    then the closure and the evidence still run (R2)."""
    deps, lab = env["deps"], env["lab"]
    reached: list[str] = []

    def twice(request):
        _signal()
        reached.append("after_first")
        _signal(signal.SIGINT)
        reached.append("after_second")

    env["investigator"].action = twice
    code, final = _run(env, capsys, CART, NORMAL)
    assert reached == ["after_first"]
    assert (code, final["exit_code"], final["status"]) == (130, 130, "interrupted")
    first, later = d.lifecycle(deps)["slots"]
    assert (first["status"], first["reason"]) == ("aborted", "INTERRUPTED")
    assert first["summary"] is not None and later["status"] == "not_started"
    assert lab.raw == d.ORIGINAL
    assert not (deps.ledger_root / "fault-active.json").exists()
    assert env["pg"].stopped == env["pg"].started


# -- R10: the final snapshot --------------------------------------------------


@pytest.mark.parametrize("when", ["after_snapshot", "during_preflight"])
def test_the_final_code_is_a_snapshot(env, capsys, monkeypatch, when):
    deps = env["deps"]
    if when == "after_snapshot":
        real = eval_live._final

        def final_with_signals(result):
            _signal()
            _signal(signal.SIGINT)  # protected: no forced raise either
            return real(result)

        monkeypatch.setattr(eval_live, "_final", final_with_signals)
    else:

        def check():
            _signal()
            return "ENV_UNCONFIRMED"

        env["env_checker"].check = check  # type: ignore[method-assign]
    code, final = _run(env, capsys, NORMAL)
    if when == "after_snapshot":
        # The fake investigator returns no recording: aborted, exit 1, and a
        # signal after the snapshot does not turn it into 130.
        assert (code, final["exit_code"], final["status"]) == (1, 1, "finished")
        assert d.lifecycle(deps)["round_status"] == "finished"
    else:
        # A stop requested before the refusal: 130, not 3; nothing started.
        assert (code, final["exit_code"], final["status"]) == (130, 130, "interrupted")
        assert not deps.evidence_root.exists()


# -- R5 / R8: interruptible sleep and checkpointed calls ----------------------


def test_the_system_clock_sleep_returns_soon_after_a_stop_request():
    clock = eval_live_lab.SystemClock()
    with eval_live._signals():
        timer = threading.Timer(0.2, os.kill, (os.getpid(), signal.SIGTERM))
        timer.start()
        started = time.monotonic()
        try:
            clock.sleep(5.0)
        finally:
            timer.join()
        assert time.monotonic() - started < 1.5
        with pytest.raises(Interrupted):
            eval_live_lab.checkpoint()
        with pytest.raises(Interrupted):  # the stop is never consumed (R3)
            eval_live_lab.checkpoint()
        with eval_live_lab.protected():
            started = time.monotonic()
            clock.sleep(0.6)  # protected: the full duration
            assert time.monotonic() - started >= 0.6
            eval_live_lab.checkpoint()  # a no-op inside a protected section


def test_a_completed_model_or_tool_call_is_recorded_before_the_checkpoint():
    from scripts.eval_live_investigator import _Checkpointed

    recorded: list[int] = []

    class Proxy:
        scope = "scope"

        def call(self, value: int) -> int:
            recorded.append(value)
            if value == 2:
                eval_live_lab.request_stop()  # arrives during the call
            return value

    wrapped = _Checkpointed(Proxy())
    with eval_live_lab.interrupt_scope():
        assert wrapped.scope == "scope" and wrapped.call(1) == 1
        with pytest.raises(Interrupted):
            wrapped.call(2)
        with pytest.raises(Interrupted):
            wrapped.call(3)  # never reaches the recording proxy
    assert recorded == [1, 2]


# -- astra P1: external commands run in their own session ----------------------


def test_every_external_command_runs_in_its_own_session(tmp_path, monkeypatch):
    """A terminal Ctrl-C goes to the whole foreground process group; every
    kubectl/git/initdb/pg_ctl call must be outside it (its own session)."""
    import psycopg

    data = tmp_path / "pg"
    seen: list[tuple[str, object]] = []

    def run(command, **kwargs):
        seen.append((command[-1], kwargs.get("start_new_session")))
        if Path(command[0]).name == "initdb":
            data.mkdir()
        elif command[-1] == "start":
            (data / "postmaster.pid").write_text(f"4242\n{data}\n0\n55998\n")
        elif command[-1] == "stop":
            (data / "postmaster.pid").unlink()
        return d._Completed(0, b"{}")

    def refuse(*args, **kwargs):
        raise OSError("no server")  # after pg_ctl start: the cleanup stops it

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(eval_live_lab, "_pg_binary", lambda name: f"/bin/{name}")
    monkeypatch.setattr(psycopg, "connect", refuse)
    eval_live_lab._external(["kubectl", "get"], timeout=1)
    eval_live_lab._patch(["kubectl", "patch"], timeout=1)
    eval_live_lab.GitProvenance(tmp_path)._git("rev-parse", "HEAD")
    with pytest.raises(LabError):
        eval_live_lab.LocalPgLauncher().start(data, 55998)
    actions = [action for action, _new in seen]
    assert actions == ["get", "patch", "HEAD", "--encoding=UTF8", "start", "stop"]
    assert all(new is True for _action, new in seen), seen


def test_no_subprocess_call_of_the_driver_shares_the_process_group():
    """Every ``subprocess`` call site in the driver modules passes
    ``start_new_session=True`` (covers call sites added later too)."""
    import ast

    root = Path(__file__).resolve().parents[2] / "scripts"
    sites = []
    for name in (
        "eval_live.py",
        "eval_live_lab.py",
        "eval_live_probe.py",
        "eval_live_investigator.py",
        "eval_live_verify.py",
    ):
        for node in ast.walk(ast.parse((root / name).read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"
            ):
                keywords = {k.arg: k.value for k in node.keywords}
                new = keywords.get("start_new_session")
                sites.append(
                    (name, node.lineno, isinstance(new, ast.Constant) and new.value)
                )
    assert len(sites) >= 6
    assert all(ok is True for _name, _line, ok in sites), sites


_HARNESS = """
import sys
from scripts import eval_live, eval_live_lab
pidfile = sys.argv[1]
with eval_live._signals():
    try:
        eval_live_lab._external(
            ["/bin/sh", "-c", f"echo $$ > {pidfile}; sleep 1"], timeout=10
        )
        result = "completed"
    except eval_live_lab.LabError as exc:
        result = exc.code
    print(result, eval_live_lab.stop_requested())
"""


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
def test_a_terminal_sigint_reaches_the_driver_but_not_its_command(tmp_path):
    """Real processes: the driver runs alone in a new session (like a shell
    job); SIGINT to its process group, as Ctrl-C would send, while a command
    runs. The command finishes; the driver only records the stop."""
    pidfile = tmp_path / "child.pid"
    root = Path(__file__).resolve().parents[2]
    driver = subprocess.Popen(
        [sys.executable, "-c", _HARNESS, str(pidfile)],
        cwd=root,
        stdout=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 20
        while not (pidfile.exists() and pidfile.read_text().strip()):
            assert time.monotonic() < deadline and driver.poll() is None
            time.sleep(0.05)
        os.killpg(driver.pid, signal.SIGINT)  # the driver's group only
        out, _ = driver.communicate(timeout=20)
    finally:
        if driver.poll() is None:
            driver.kill()
            driver.wait()
    assert driver.returncode == 0
    assert out.decode().split() == ["completed", "True"]


# -- astra P2: a stop while the real deps are built ---------------------------


def test_a_stop_while_the_deps_are_built_exits_130_not_3(capsys, monkeypatch):
    def failing(flagd_url, round_id, *, probe=False):
        _signal()  # the first signal arrives during the initialisation
        raise LabError("ENV_UNCONFIRMED")

    monkeypatch.setattr(eval_live, "real_deps", failing)
    code = eval_live.main([*d.LIVE_ARGV, *p._scenarios(NORMAL)])
    final = d._last_json(capsys)
    assert (code, final["exit_code"], final["status"]) == (130, 130, "interrupted")


def test_a_deps_failure_without_a_stop_is_still_refused_with_3(capsys, monkeypatch):
    def failing(flagd_url, round_id, *, probe=False):
        raise LabError("ENV_UNCONFIRMED")

    monkeypatch.setattr(eval_live, "real_deps", failing)
    code = eval_live.main([*d.LIVE_ARGV, *p._scenarios(NORMAL)])
    final = d._last_json(capsys)
    assert (code, final["exit_code"], final["reason"]) == (3, 3, "ENV_UNCONFIRMED")
