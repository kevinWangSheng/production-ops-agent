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
from dataclasses import replace
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
pidfile, donefile = sys.argv[1], sys.argv[2]
script = f"echo $$ > {pidfile}; sleep 1; echo done > {donefile}"
with eval_live._signals():
    try:
        eval_live_lab._external(["/bin/sh", "-c", script], timeout=10)
        result = "completed"
    except eval_live_lab.LabError as exc:
        result = exc.code
    except eval_live_lab.Interrupted:
        result = "interrupted_after"  # the checkpoint after the command
    print(result, eval_live_lab.stop_requested())
"""


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
def test_a_terminal_sigint_reaches_the_driver_but_not_its_command(tmp_path):
    """Real processes: the driver runs alone in a new session (like a shell
    job); SIGINT to its process group, as Ctrl-C would send, while a command
    runs. The command finishes; the driver records the stop and honours it
    at the checkpoint after the command."""
    pidfile, donefile = tmp_path / "child.pid", tmp_path / "child.done"
    root = Path(__file__).resolve().parents[2]
    driver = subprocess.Popen(
        [sys.executable, "-c", _HARNESS, str(pidfile), str(donefile)],
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
    assert donefile.read_text().strip() == "done"  # the command survived
    assert out.decode().split() == ["interrupted_after", "True"]


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


# -- PR #276 bot P1: the recording seal is protected --------------------------


def _signals_while_sealing(monkeypatch) -> list[str]:
    """Two real signals while ``record()`` writes ``recording.json``, i.e.
    after the driver returned: the second one is a forced stop."""
    from opspilot.evaluation import recording

    real = recording._publish
    fired: list[str] = []

    def publish(path, *args, **kwargs):
        if path.name == "recording.json" and not fired:
            fired.append(path.name)
            _signal()
            _signal(signal.SIGINT)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(recording, "_publish", publish)
    return fired


@pytest.mark.parametrize("sealed", [False, True])
def test_a_forced_stop_while_sealing_cannot_abandon_the_recording(
    tmp_path, monkeypatch, sealed
):
    """``sealed=False`` is plain ``record()``: the forced stop abandons the
    seal (the hazard). The driver's ``_sealed_record`` finishes it."""
    from opspilot.evaluation import load_recording, record
    from scripts.eval_live_investigator import _sealed_record
    from tests.acceptance import eval_b2_support as b2

    fired = _signals_while_sealing(monkeypatch)
    destination = tmp_path / "recording"
    seal = _sealed_record if sealed else record
    with eval_live._signals():
        if not sealed:
            with pytest.raises(Interrupted):
                seal(b2.source_driver(), destination=destination, context=b2.context())
            assert fired and (destination / "INCOMPLETE").exists()
            return
        ref = seal(b2.source_driver(), destination=destination, context=b2.context())
        assert fired
        assert eval_live._DEFER["pending"] is True  # recorded, not raised
        assert eval_live._DEFER["depth"] == 0
        with pytest.raises(Interrupted):
            eval_live_lab.checkpoint()  # honoured at the next checkpoint
    assert not (destination / "INCOMPLETE").exists()
    loaded = load_recording(destination / ref.path, expected_hash=ref.sha256)
    assert loaded.outcome is not None


@pytest.mark.parametrize("failure", ["drive", "interrupted_drive", "seal"])
def test_the_seal_protection_never_leaks_depth(tmp_path, monkeypatch, failure):
    from opspilot.evaluation import StorageError, recording
    from scripts.eval_live_investigator import _sealed_record
    from tests.acceptance import eval_b2_support as b2

    drive = b2.source_driver()
    expected: type[BaseException] = StorageError
    if failure == "drive":

        def drive(hooks):  # record() turns it into RECORDING_INCOMPLETE
            raise RuntimeError("the Run failed")

    elif failure == "interrupted_drive":

        def drive(hooks):
            raise Interrupted()

        expected = Interrupted
    else:

        def save(*args, **kwargs):
            raise StorageError("EVIDENCE_UNAVAILABLE")

        monkeypatch.setattr(recording, "_save", save)
    with eval_live_lab.interrupt_scope():
        with pytest.raises(expected):
            _sealed_record(drive, destination=tmp_path / "rec", context=b2.context())
        assert eval_live_lab._STOP["depth"] == 0


# -- PR #276 bot round 2: investigator checkpoints (R1) -----------------------


class _Store:
    """``DurableStore`` and its seats, offline: records every DB operation."""

    calls: list[str] = []
    stop_in: str | None = None

    def __init__(self, *args, **kwargs) -> None:
        self.name = type(self).__name__

    def install(self) -> None:
        _Store.calls.append(f"{self.name}.install")
        if _Store.stop_in == f"{self.name}.install":
            _signal()  # the first signal arrives during this DB operation

    def accept(self, *args, **kwargs) -> None:
        _Store.calls.append("accept")


class _Web(_Store):
    pass


class _Log(_Store):
    pass


class _Evidence(_Store):
    pass


@pytest.mark.parametrize("stop_in", ["_Web.install", "_Evidence.install"])
def test_a_stop_during_an_intake_db_operation_never_submits_the_run(
    env, capsys, monkeypatch, stop_in
):
    from opspilot.tools import otel_demo
    from scripts import eval_live_investigator as inv

    stores = iter([_Web, _Store])
    monkeypatch.setattr(inv, "DurableStore", lambda dsn: next(stores)())
    monkeypatch.setattr(inv, "DurableEventLog", _Log)
    monkeypatch.setattr(inv, "DurableEvidenceStore", _Evidence)
    monkeypatch.setattr(_Store, "calls", [])
    monkeypatch.setattr(_Store, "stop_in", stop_in)
    built: list[str] = []
    session = inv.RoundSession("synthetic-key", None, otel_demo.OtelDemoConfig())
    investigator = inv.OtelDemoInvestigator(
        session, env["clock"], client_factory=lambda *a: built.append("client")
    )
    deps = replace(env["deps"], investigator=investigator)
    env["pg"].deps_ref["deps"] = deps
    code = eval_live.main([*d.LIVE_ARGV, *p._scenarios(CART, NORMAL)], deps=deps)
    final = d._last_json(capsys)
    assert (code, final["exit_code"], final["status"]) == (130, 130, "interrupted")
    assert stop_in in _Store.calls
    assert "accept" not in _Store.calls and built == []  # no Run, no model
    first, later = d.lifecycle(deps)["slots"]
    assert (first["status"], first["reason"]) == ("aborted", "INTERRUPTED")
    assert first["summary"] is not None and later["status"] == "not_started"
    assert env["lab"].raw == d.ORIGINAL
    assert not (deps.ledger_root / "fault-active.json").exists()
    assert env["pg"].stopped == env["pg"].started


def test_a_stop_during_the_trace_flush_skips_the_read_back_honestly(
    env, capsys, monkeypatch
):
    from opspilot import tracing
    from scripts import eval_live_investigator as inv

    steps: list[str] = []

    def shutdown():
        steps.append("shutdown")
        _signal()  # the first signal arrives while the spans are flushed

    def read_back(*args, **kwargs):
        steps.append("read_back")
        return {"read_back": "found", "langsmith_run_id": "lsr-1"}

    monkeypatch.setattr(tracing, "shutdown", shutdown)
    monkeypatch.setattr(tracing, "configure", lambda env: steps.append("configure"))
    monkeypatch.setattr(inv, "trace_evidence", read_back)
    session = inv.RoundSession("synthetic-key", None, None)  # type: ignore[arg-type]
    real = env["investigator"].investigate

    def investigate(request):
        result = real(request)  # the Run itself completed
        traces = inv._traces(
            session, request.attempt.attempt_id, "run-1", "otel-1", env["clock"]
        )
        return replace(result, traces=traces)

    env["investigator"].investigate = investigate  # type: ignore[method-assign]
    code, final = _run(env, capsys, CART, NORMAL)
    assert steps == ["shutdown", "configure"]  # no read-back after the flush
    assert (code, final["exit_code"], final["status"]) == (130, 130, "interrupted")
    first, later = d.lifecycle(deps := env["deps"])["slots"]
    assert first["summary"] is not None and later["status"] == "not_started"
    capture = d.summary(deps, first["attempt_id"])["capture"]
    (trace,) = capture["traces"]
    assert trace["entry"]["read_back"] == "error"  # never "found"
    assert json.loads(trace["raw_read_back"])["read_back"] == "interrupted"
    assert env["lab"].raw == d.ORIGINAL
    assert not (deps.ledger_root / "fault-active.json").exists()


def test_a_completed_read_back_is_kept_when_the_stop_arrives_during_it(monkeypatch):
    from opspilot import tracing
    from scripts import eval_live_investigator as inv

    def read_back(*args, **kwargs):
        eval_live_lab.request_stop()  # arrives during the remote read-back
        return {"read_back": "found", "langsmith_run_id": "lsr-1"}

    monkeypatch.setattr(tracing, "shutdown", lambda: None)
    monkeypatch.setattr(tracing, "configure", lambda env: None)
    monkeypatch.setattr(inv, "trace_evidence", read_back)
    session = inv.RoundSession("synthetic-key", None, None)  # type: ignore[arg-type]
    with eval_live_lab.interrupt_scope():
        (trace,) = inv._traces(session, "a1", "run-1", "otel-1", d.Clock())
        assert eval_live_lab.stop_requested()  # left for the driver
    assert trace.entry.read_back == "found" and trace.entry.run_id == "lsr-1"


# -- PR #276 bot round 3 ------------------------------------------------------


def test_a_stop_during_the_lock_wait_never_writes_run_started(tmp_path, monkeypatch):
    """The batch-journal lock is busy; the stop arrives during the retry
    sleep and the lock is free right after: no permanent run_started row."""
    import fcntl

    root = tmp_path / "ledgers"
    root.mkdir()
    holder = (root / ".batch-journal.guard").open("a")
    fcntl.flock(holder, fcntl.LOCK_EX)  # another process holds the lock
    real_sleep = time.sleep

    def sleep(seconds):
        _signal()  # the stop arrives while waiting ...
        fcntl.flock(holder, fcntl.LOCK_UN)  # ... and the lock is then free
        real_sleep(0)

    monkeypatch.setattr(eval_live_lab.time, "sleep", sleep)
    try:
        with eval_live._signals():
            with pytest.raises(Interrupted):
                eval_live_lab.append_run_start(root, "r1", "lead", "b1", d.T0)
    finally:
        holder.close()
    log = root / "run-starts.jsonl"
    assert not log.exists() or log.read_text() == ""


@pytest.mark.parametrize("stop_at", ["kubectl", "prometheus_anonymous"])
def test_a_stop_inside_the_identity_check_skips_its_remaining_requests(
    monkeypatch, stop_at
):
    from scripts import kind_lab

    requests: list[str] = []

    def run(command, **kwargs):
        requests.append("kubectl")
        if stop_at == "kubectl":
            _signal()
        return d._Completed(0, f"namespace/{kind_lab.NAMESPACE}\n".encode())

    def get_json(url, **kwargs):
        requests.append(url.rsplit("/", 2)[-2])
        if stop_at == "prometheus_anonymous" and len(requests) == 2:
            _signal()
        return 200, {}

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(kind_lab, "get_json", get_json)
    monkeypatch.setattr(kind_lab, "lab_authorization", lambda: "Basic x")
    with eval_live._signals():
        with pytest.raises(Interrupted):
            eval_live_lab.KindLabIdentity().verify()
    expected = 1 if stop_at == "kubectl" else 2
    assert len(requests) == expected  # nothing after the stop was requested


@pytest.fixture
def ran_round(tmp_path, capsys):
    from tests.acceptance import eval_c_support as s

    harness = s.Harness(tmp_path)
    assert eval_live.main(harness.argv(s.FAULT), deps=harness.deps) == 0
    capsys.readouterr()
    return harness


@pytest.mark.parametrize("when", ["last_recording", "record_write"])
def test_the_structural_replay_record_agrees_with_the_final_json(
    ran_round, capsys, monkeypatch, when
):
    from scripts import eval_live_verify
    from tests.acceptance import eval_c_support as s

    if when == "last_recording":
        real = eval_live_verify._verify_one

        def verify_one(*args, **kwargs):
            item = real(*args, **kwargs)
            _signal()  # during the load/replay of the last recording
            return item

        monkeypatch.setattr(eval_live_verify, "_verify_one", verify_one)
    else:
        real_write = eval_live_verify._write_exclusive

        def write(directory, document, now):
            _signal()  # after the final snapshot, while the record is written
            return real_write(directory, document, now)

        monkeypatch.setattr(eval_live_verify, "_write_exclusive", write)
    code = eval_live.main(["--verify-replay", "--round", s.ROUND], deps=ran_round.deps)
    final = d._last_json(capsys)
    assert final["exit_code"] == code
    records = sorted((ran_round.deps.evidence_root / s.ROUND).glob("structural-*"))
    if when == "last_recording":
        assert (code, final["status"]) == (130, "interrupted")
        # Never a frozen "verified/0" next to a final 130.
        for path in records:
            stored = json.loads(path.read_text())
            assert (stored["status"], stored["exit_code"]) == ("interrupted", 130)
    else:
        (path,) = records
        stored = json.loads(path.read_text())
        assert (code, final["status"]) == (0, "verified")
        assert (stored["status"], stored["exit_code"]) == ("verified", 0)


# -- PR #276 bot round 4: the read-back polling -------------------------------


def test_a_stop_during_the_first_read_back_poll_ends_the_polling(
    env, capsys, monkeypatch
):
    """``find_root_run`` polls LangSmith up to 30 times, 2 s apart; a stop
    during the first poll ends it there: no further ``list_runs``, the trace
    is recorded as interrupted (never found), and the round exits 130."""
    from opspilot import tracing
    from scripts import eval_live_investigator as inv

    polls: list[str] = []

    class Client:
        def list_runs(self, **kwargs):
            polls.append(kwargs["project_name"])
            if len(polls) == 1:
                _signal()  # the first signal arrives during the first poll
            return []  # the root is not visible yet: it would poll again

    monkeypatch.setattr(inv, "langsmith_client", Client)
    monkeypatch.setattr(tracing, "shutdown", lambda: None)
    monkeypatch.setattr(tracing, "configure", lambda env: None)
    project = {"project": "eval-r1", "project_id": "pid-1", "trace_tier": "x"}
    session = inv.RoundSession("synthetic-key", project, None)  # type: ignore[arg-type]
    real = env["investigator"].investigate

    def investigate(request):
        result = real(request)  # the Run itself completed
        traces = inv._traces(
            session, request.attempt.attempt_id, "run-1", "otel-1", env["clock"]
        )
        return replace(result, traces=traces)

    env["investigator"].investigate = investigate  # type: ignore[method-assign]
    started = time.monotonic()
    code, final = _run(env, capsys, CART, NORMAL)
    assert time.monotonic() - started < 10  # no 2 s waits between 30 polls
    assert polls == ["eval-r1"]
    assert (code, final["exit_code"], final["status"]) == (130, 130, "interrupted")
    deps = env["deps"]
    first, later = d.lifecycle(deps)["slots"]
    assert first["summary"] is not None and later["status"] == "not_started"
    (trace,) = d.summary(deps, first["attempt_id"])["capture"]["traces"]
    assert trace["entry"]["read_back"] == "error"
    assert json.loads(trace["raw_read_back"])["read_back"] == "interrupted"
    assert env["lab"].raw == d.ORIGINAL
    assert not (deps.ledger_root / "fault-active.json").exists()
