"""Independent r16 acceptance through CLI, injected protocols and filesystem I/O.

No private interrupt state, decorators, or driver implementation are inspected.
See EVAL_C_R16_AMBIGUITIES.md for boundaries the public interface cannot locate.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import eval_live
from scripts import eval_live_lab as lab
from tests.acceptance import eval_c_support as s


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("r16 acceptance attempted network, model or subprocess I/O")

    for name in ("connect", "connect_ex", "sendto", "sendall"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    for name in ("create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, name, forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(eval_live, "real_deps", forbidden)
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    yield
    for sig, handler in previous.items():
        signal.signal(sig, handler)


@pytest.fixture
def h(tmp_path):
    return s.Harness(tmp_path)


def send(sig=signal.SIGINT):
    """Call the installed handler, never pytest's default SIGINT handler."""
    handler = signal.getsignal(sig)
    assert callable(handler), "CLI has not installed a callable signal handler"
    assert handler is not signal.default_int_handler, "CLI left default SIGINT active"
    handler(sig, None)


def finish(h, capsys, argv=None):
    code = eval_live.main(h.argv(n=2) if argv is None else argv, deps=h.deps)
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1, "final stdout must be exactly one JSON line"
    result = json.loads(lines[0])
    assert result["exit_code"] == code
    return code, result


def closed(h, result, *, code=130, retained=False, summaries=1):
    assert result["exit_code"] == code
    assert h.flags.current.raw == s.flags_raw()
    assert (h.deps.ledger_root / "fault-active.json").exists() is retained
    assert not list(h.deps.ledger_root.glob(".active-*.json"))
    assert h.pg.stopped == h.pg.started
    assert h.reservation.exists()
    written_summaries = h.summaries()
    assert len(written_summaries) == summaries
    assert all(
        summary["capture"]["attempt"]["status"] != "unknown"
        for summary in written_summaries
    )
    assert list(h.public().glob("runs/*/attempt.json"))
    for name in ("aggregate.json", "lifecycle.json"):
        assert isinstance(json.loads((h.public() / name).read_bytes()), dict)
    assert all(slot["status"] == "not_started" for slot in result["slots"][summaries:])


@pytest.mark.parametrize("phase", ["inject", "restore", "wash", "pg-stop"])
@pytest.mark.parametrize("signals", [(signal.SIGINT,), (signal.SIGINT, signal.SIGTERM)])
def test_protected_protocol_calls_finish_after_signals(h, capsys, phase, signals):
    reached = []

    def deliver():
        if reached:
            return
        reached.append(phase)
        for sig in signals:
            send(sig)
        # Reaching here proves neither first nor mixed second signal raised.
        reached.append("returned")

    if phase in {"inject", "restore"}:

        def on_replace(intent, current, raw, fence):
            restoring = raw == s.flags_raw()
            if restoring == (phase == "restore"):
                deliver()

        h.flags.on_replace = on_replace
    elif phase == "wash":
        spans = h.telemetry.spans

        def washing(service, start, end, timeout):
            if len(h.flags.writes) >= 2:
                deliver()
            return spans(service, start, end, timeout)

        h.telemetry.spans = washing
    else:
        stop = h.pg.stop

        def stopping(instance):
            deliver()
            stop(instance)

        h.pg.stop = stopping
    code, result = finish(h, capsys)
    assert reached == [phase, "returned"]
    assert code == 130
    closed(h, result)


@pytest.mark.parametrize("target", ["intent", "active-marker"])
@pytest.mark.parametrize("count", [1, 2])
def test_unlink_finishes_protected_transition(h, capsys, monkeypatch, target, count):
    unlink = Path.unlink
    reached = []

    def observed(path, *args, **kwargs):
        value = unlink(path, *args, **kwargs)
        match = (
            path == h.deps.ledger_root / "fault-active.json"
            if target == "intent"
            else path.parent == h.deps.ledger_root and path.name.startswith(".active-")
        )
        if match and not reached:
            reached.append(str(path))
            for _ in range(count):
                send()
            reached.append("returned")
        return value

    monkeypatch.setattr(Path, "unlink", observed)
    code, result = finish(h, capsys)
    assert len(reached) == 2
    assert code == 130
    closed(h, result, summaries=2 if target == "active-marker" else 1)


@pytest.mark.parametrize("name", ["summary.json", "aggregate.json", "lifecycle.json"])
@pytest.mark.parametrize("count", [1, 2])
def test_evidence_file_io_is_protected(h, capsys, monkeypatch, name, count):
    opened = Path.open
    reached = []

    def deliver(path):
        if Path(path).name == name and not reached:
            reached.append(str(path))
            for _ in range(count):
                send()
            reached.append("returned")

    def observed(path, mode="r", *args, **kwargs):
        stream = opened(path, mode, *args, **kwargs)
        if any(flag in mode for flag in "wax+"):
            try:
                deliver(path)
            except BaseException:
                stream.close()
                raise
        return stream

    def publishing(operation):
        def observed_publish(source, destination, *args, **kwargs):
            value = operation(source, destination, *args, **kwargs)
            deliver(destination)
            return value

        return observed_publish

    monkeypatch.setattr(Path, "open", observed)
    # Observe the public destination of atomic publication too. No temporary
    # filename, serialization helper, or private writer name is assumed.
    for operation in ("replace", "rename", "link"):
        monkeypatch.setattr(os, operation, publishing(getattr(os, operation)))
    code, result = finish(h, capsys, h.argv())
    assert len(reached) == 2, (
        "published-file I/O hook was not reached (see ambiguities)"
    )
    assert code == 130
    closed(h, result)


@pytest.mark.parametrize("seam", ["balance", "http", "investigation"])
def test_first_signal_returns_then_next_checkpoint_stops_slots(h, capsys, seam):
    returned = []

    def interrupt_then_return():
        send()
        returned.append(True)

    if seam == "balance":
        balance = h.deps.balance.balance

        def read_balance():
            if not returned:
                interrupt_then_return()
            return balance()

        h.deps.balance.balance = read_balance
    elif seam == "http":
        spans = h.telemetry.spans

        def query(*args):
            if not returned:
                interrupt_then_return()
            return spans(*args)

        h.telemetry.spans = query
    else:
        h.investigator.callback = lambda request: interrupt_then_return()
    code, result = finish(h, capsys)
    assert returned == [True]
    assert code == 130
    assert result["slots"][1]["status"] == "not_started"
    assert not list(h.deps.ledger_root.glob(".active-*.json"))
    assert not (h.deps.ledger_root / "fault-active.json").exists()
    if seam != "investigation":
        assert not h.investigator.requests


@pytest.mark.parametrize("second", [signal.SIGINT, signal.SIGTERM])
def test_second_signal_forces_nonprotected_investigator_into_closure(h, capsys, second):
    events = []

    def blocked(request):
        send()
        events.append("first-returned")
        send(second)
        events.append("second-returned")

    h.investigator.callback = blocked
    code, result = finish(h, capsys)
    assert events == ["first-returned"]
    assert code == 130
    closed(h, result)


@pytest.mark.parametrize("mode", ["run", "probe"])
@pytest.mark.parametrize("failure", ["signal", "error"])
def test_propagation_failure_preserves_committed_fault_without_effective_at(
    h, capsys, mode, failure
):
    resolve = h.flagd.resolve
    reached = []

    def propagation(flag, method, timeout):
        if h.flags.writes and h.flags.current.raw != s.flags_raw() and not reached:
            reached.append(True)
            if failure == "signal":
                send()
            else:
                raise lab.LabError("FLAG_RESOLVE_FAILED")
        return resolve(flag, method, timeout)

    h.flagd.resolve = propagation
    argv = h.argv()
    if mode == "probe":
        argv = h.argv() + ["--probe"]
        index = argv.index("--pg-port")
        del argv[index : index + 2]
    code, result = finish(h, capsys, argv)
    assert reached == [True]
    assert code == (130 if failure == "signal" else 1)
    if mode == "run":
        slot = json.loads((h.public() / "lifecycle.json").read_bytes())["slots"][0]
    else:
        probe = json.loads((h.public() / "probe.json").read_bytes())

        # The interface does not specify the probe scenario container; find the
        # public scenario record by its documented scenario_id, not an index.
        def records(value):
            if isinstance(value, dict):
                if value.get("scenario_id") == s.FAULT and "fault" in value:
                    yield value
                for child in value.values():
                    yield from records(child)
            elif isinstance(value, list):
                for child in value:
                    yield from records(child)

        matches = list(records(probe))
        assert len(matches) == 1
        slot = matches[0]
    assert slot["fault"] is not None
    assert slot["fault"]["original_sha256"] == s.sha(s.flags_raw())
    assert slot["fault"]["injected_sha256"]
    assert slot["fault"]["effective_at"] is None
    assert h.flags.current.raw == s.flags_raw()
    assert not (h.deps.ledger_root / "fault-active.json").exists()
    assert not list(h.deps.ledger_root.glob(".active-*.json"))
    assert not h.investigator.requests
    assert result["exit_code"] == code


@pytest.mark.parametrize("count", [1, 2])
def test_pg_stop_failure_and_signal_preserve_both_outcomes(h, capsys, count):
    stops = []

    def failed_stop(instance):
        stops.append(instance)
        if len(stops) == 1:
            for _ in range(count):
                send()
        raise lab.LabError("PG_STOP_FAILED")

    h.pg.stop = failed_stop
    code, result = finish(h, capsys)
    assert stops
    assert code == 130
    lifecycle = json.loads((h.public() / "lifecycle.json").read_bytes())
    assert lifecycle["slots"][0]["closure_reason"] == "PG_STOP_FAILED"
    assert result["slots"][1]["status"] == "not_started"
    assert h.summaries()
    assert (h.public() / "aggregate.json").is_file()
    assert h.flags.current.raw == s.flags_raw()
    assert not (h.deps.ledger_root / "fault-active.json").exists()
    assert not list(h.deps.ledger_root.glob(".active-*.json"))


@pytest.mark.parametrize("exception", [lab.Interrupted, KeyboardInterrupt])
def test_direct_interrupt_exception_remains_supported_and_sticky(h, capsys, exception):
    def interrupted(request):
        raise exception()

    h.investigator.callback = interrupted
    code, result = finish(h, capsys)
    assert code == 130
    closed(h, result)


def test_restore_failure_outranks_signal(h, capsys):
    def interrupt(request):
        h.telemetry.bad_wash = True
        send()

    h.investigator.callback = interrupt
    code, result = finish(h, capsys)
    assert code == 4
    closed(h, result, code=4, retained=True)


def test_late_signal_at_stdout_does_not_change_snapshot_or_return_code(
    h, capsys, monkeypatch
):
    captured = []
    delivered = []
    stdout = sys.stdout

    def capture_handler(request):
        captured.append(signal.getsignal(signal.SIGINT))

    h.investigator.callback = capture_handler

    class Output:
        def write(self, text):
            if text.lstrip().startswith("{") and not delivered:
                payload = json.loads(text)
                delivered.append(payload["exit_code"])
                assert callable(captured[0])
                captured[0](signal.SIGINT, None)
            return stdout.write(text)

        def __getattr__(self, name):
            return getattr(stdout, name)

    monkeypatch.setattr(sys, "stdout", Output())
    code, result = finish(h, capsys, h.argv())
    assert captured
    assert delivered == [code]
    assert code == 0
    assert result["exit_code"] == code


@pytest.mark.parametrize("count", [1, 2])
def test_protected_wash_keeps_full_fake_clock_deadline(h, capsys, count):
    sleep = h.clock.sleep
    observed = []
    wash_start = []

    def washing_sleep(seconds):
        if len(h.flags.writes) >= 2:
            if not wash_start:
                wash_start.append(h.clock.monotonic())
                for _ in range(count):
                    send()
                observed.append("handler-returned")
        sleep(seconds)

    h.clock.sleep = washing_sleep
    code, result = finish(h, capsys)
    assert observed == ["handler-returned"]
    assert h.clock.monotonic() - wash_start[0] >= 300
    assert code == 130
    closed(h, result)


@pytest.mark.parametrize("mode", ["probe", "recover"])
@pytest.mark.parametrize("count", [1, 2])
def test_lab_only_modes_finish_protected_restore_and_emit_evidence(
    h, capsys, mode, count
):
    if mode == "recover":
        h.investigator.callback = lambda request: setattr(h.telemetry, "bad_wash", True)
        code, _ = finish(h, capsys, h.argv())
        assert code == 4
        h.telemetry.bad_wash = False
        argv = [
            "--recover",
            "--round",
            s.ROUND,
            "--holder",
            s.HOLDER,
            "--batch",
            s.BATCH,
            "--flagd-url",
            h.deps.flagd_url,
        ]
    else:
        argv = h.argv() + ["--probe"]
        index = argv.index("--pg-port")
        del argv[index : index + 2]
    spans = h.telemetry.spans
    reached = []

    def washing(*args):
        if h.flags.current.raw == s.flags_raw() and not reached:
            # For probe, wait until injection and restoration have happened.
            if mode == "recover" or len(h.flags.writes) >= 2:
                reached.append("wash")
                for _ in range(count):
                    send()
                reached.append("returned")
        return spans(*args)

    h.telemetry.spans = washing
    code, result = finish(h, capsys, argv)
    assert reached == ["wash", "returned"]
    assert code == result["exit_code"] == 130
    assert not (h.deps.ledger_root / "fault-active.json").exists()
    assert not list(h.deps.ledger_root.glob(".active-*.json"))
    assert h.flags.current.raw == s.flags_raw()
    if mode == "recover":
        records = list(h.public().glob("recovery-*.json"))
        assert len(records) == 1
        assert json.loads(records[0].read_bytes())["recovered_at"]
    else:
        assert isinstance(json.loads((h.public() / "probe.json").read_bytes()), dict)
        assert not h.pg.started
        assert not h.investigator.requests


def test_evidence_failure_outranks_signal(h, capsys, monkeypatch):
    stop = h.pg.stop

    def stopping(instance):
        send()
        stop(instance)

    h.pg.stop = stopping
    opened = Path.open
    failed = []

    def cannot_publish(path, mode="r", *args, **kwargs):
        if path == h.public() / "lifecycle.json" and any(c in mode for c in "wax+"):
            failed.append(True)
            raise OSError("offline evidence write failure")
        return opened(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", cannot_publish)
    code, result = finish(h, capsys)
    assert failed
    assert code == result["exit_code"] == 5
    assert h.flags.current.raw == s.flags_raw()
    assert not (h.deps.ledger_root / "fault-active.json").exists()
    assert not list(h.deps.ledger_root.glob(".active-*.json"))


@pytest.mark.parametrize("interrupted", [False, True])
def test_preflight_rejection_uses_stop_snapshot_before_refusal(h, capsys, interrupted):
    def refused():
        if interrupted:
            send()
        return "ENV_UNCONFIRMED"

    h.env.check = refused
    code, result = finish(h, capsys)
    assert code == result["exit_code"] == (130 if interrupted else 3)
    assert not h.flags.writes
    assert not h.pg.started
    assert not h.investigator.requests
    assert h.deps.balance.calls == 0
