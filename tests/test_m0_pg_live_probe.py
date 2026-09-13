"""Offline subprocess transport boundary tests; never read credentials or send HTTP."""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from scripts import m0_pg_live_probe as probe
from scripts.m0.contracts import RunContext
from scripts.m0.step_store import Fence


def fence():
    return Fence(
        uuid4(),
        RunContext(
            uuid4(),
            uuid4(),
            "deepseek",
            datetime.now(timezone.utc) + timedelta(minutes=5),
        ),
        0,
        uuid4(),
        1,
    )


def body():
    return json.dumps(
        {
            "model": "deepseek-v4-flash",
            "max_tokens": 32768,
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
            "messages": [],
        }
    ).encode()


def test_actual_signal_and_private_response_pipe(monkeypatch):
    original = subprocess.Popen
    children = []

    def fake(command, **kw):
        command = [
            sys.executable,
            "-c",
            'import os,sys; sys.stdin.buffer.read(); os.write(int(sys.argv[1]),b"1"); sys.stdout.buffer.write(b\'200\\n{"private":"SYNTHETIC"}\'); sys.stdout.buffer.flush()',
            command[-3],
        ]
        child = original(command, **kw)
        children.append(child)
        return child

    monkeypatch.setattr(probe.subprocess, "Popen", fake)
    monkeypatch.setattr(probe, "reserve_global", lambda *args: None)
    child = probe.start_transport(
        body(), uuid4(), uuid4(), time.time() + 5, fence=fence()
    )
    # No real wait needs renewal; no PG/network is touched.
    status, response = probe.wait_transport(child, time.time() + 5, None, None)
    assert status == 200 and json.loads(response) == {"private": "SYNTHETIC"}
    assert children[0].poll() == 0


def test_no_signal_child_is_reaped_before_start_raises(monkeypatch):
    original = subprocess.Popen
    children = []

    def fake(command, **kw):
        child = original([sys.executable, "-c", "import time; time.sleep(30)"], **kw)
        children.append(child)
        return child

    monkeypatch.setattr(probe.subprocess, "Popen", fake)
    monkeypatch.setattr(probe, "reserve_global", lambda *args: None)
    with pytest.raises(TimeoutError):
        probe.start_transport(
            body(),
            uuid4(),
            uuid4(),
            time.time() + 5,
            fence=fence(),
            handshake_seconds=0.15,
        )
    assert children and children[0].poll() is not None


def test_private_worker_invalid_body_has_no_export_or_config_read():
    read_fd, write_fd = os.pipe()
    try:
        child = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.m0_pg_private_transport",
                str(write_fd),
                str(time.time() + 5),
            ],
            input=b"{}",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(write_fd,),
            timeout=5,
        )
        assert child.returncode == 2
        assert child.stdout == child.stderr == b""
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.parametrize("reported", ["deepseek-v4-pro", None])
def test_wrong_model_preserves_wire_and_never_commits(monkeypatch, reported):
    from contextlib import nullcontext
    from types import SimpleNamespace

    captured = []
    unknown = []

    class Connection:
        def execute(self, sql, params):
            captured.append((sql, params))

    ledger = SimpleNamespace(
        _transaction=lambda: nullcontext(Connection()),
        retain_unknown=lambda request: unknown.append(request),
        settle=lambda *args: pytest.fail("wrong model settled"),
    )
    store = SimpleNamespace(
        ledger=ledger,
        prepare_request=lambda *args: uuid4(),
        commit_response=lambda *args, **kwargs: pytest.fail("wrong model committed"),
    )
    payload = json.dumps({"model": reported, "private": "SYNTHETIC_PRIVATE"}).encode()
    monkeypatch.setattr(probe, "start_transport", lambda *args, **kwargs: object())
    monkeypatch.setattr(probe, "wait_transport", lambda *args: (200, payload))
    monkeypatch.setattr(probe, "finish_global", lambda *args: None)
    monkeypatch.setattr(probe, "reap", lambda *args: None)
    with pytest.raises(ValueError, match="RESPONSE_MODEL_MISMATCH"):
        probe.request(store, fence(), 0, json.loads(body()), time.time() + 5)
    assert any(
        "INSERT INTO m0_pg_wire_response" in sql and params[2] == payload
        for sql, params in captured
    )
    assert any("RESPONSE_MODEL_MISMATCH" in params for _, params in captured)
    assert len(unknown) == 1


def test_finish_ledger_lock_has_deadline(monkeypatch, tmp_path):
    import fcntl

    path = tmp_path / "ledger.json"
    monkeypatch.setattr(probe, "LEDGER", path)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with pytest.raises(TimeoutError):
            probe.finish_global(uuid4(), 200, None, time.time() + 0.1)


def test_non200_thinking_rejection_is_preserved_and_classified(monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace

    captured = []

    class Connection:
        def execute(self, sql, params):
            captured.append((sql, params))

    ledger = SimpleNamespace(
        _transaction=lambda: nullcontext(Connection()),
        retain_unknown=lambda request: None,
    )
    store = SimpleNamespace(
        ledger=ledger,
        prepare_request=lambda *args: uuid4(),
        commit_response=lambda *args, **kwargs: pytest.fail("HTTP400 committed"),
    )
    payload = json.dumps(
        {"error": {"message": "Thinking mode does not support this tool_choice"}}
    ).encode()
    monkeypatch.setattr(probe, "start_transport", lambda *args, **kwargs: object())
    monkeypatch.setattr(probe, "wait_transport", lambda *args: (400, payload))
    monkeypatch.setattr(probe, "finish_global", lambda *args: None)
    monkeypatch.setattr(probe, "reap", lambda *args: None)
    with pytest.raises(
        probe.ProbeFailure, match="THINKING_TOOL_CHOICE_UNSUPPORTED"
    ) as error:
        probe.request(store, fence(), 0, json.loads(body()), time.time() + 5)
    assert error.value.http_status == 400
    assert any(
        "INSERT INTO m0_pg_wire_response" in sql and params[2] == payload
        for sql, params in captured
    )
    assert any("THINKING_TOOL_CHOICE_UNSUPPORTED" in params for _, params in captured)
    assert not any("RESPONSE_MODEL_MISMATCH" in params for _, params in captured)


def test_wire_null_content_normalization_preserves_private_and_stored_group():
    import copy

    stored = [
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "SYNTHETIC_PRIVATE_SENTINEL",
            "tool_calls": [
                {
                    "id": "call1",
                    "type": "function",
                    "function": {"name": "read_fixture", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call1", "content": "synthetic evidence"},
    ]
    original = copy.deepcopy(stored)
    wire, normalized = probe.wire_history(stored)
    assert normalized is True and stored == original
    assert wire[0]["content"] == ""
    assert wire[0]["reasoning_content"] == stored[0]["reasoning_content"]
    wire[0]["content"] = None
    assert wire == stored
    unchanged, normalized = probe.wire_history(
        [{"role": "assistant", "content": "done", "reasoning_content": "SYNTHETIC"}]
    )
    assert normalized is False and unchanged[0]["content"] == "done"


def test_probe_import_without_external_experiment_worktree():
    """A fresh checkout can collect offline checks with no preserved external lab."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    code = r"""
import importlib.machinery
import sys
class UnavailableLab:
    def find_spec(self, name, path=None, target=None):
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec and spec.origin and "production-ops-agent-m0-environment/" in spec.origin:
            raise ModuleNotFoundError("preserved lab unavailable in fresh checkout")
        return None
sys.meta_path.insert(0, UnavailableLab())
from scripts import m0_pg_live_probe
assert callable(m0_pg_live_probe.wire_history)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_shared_authorization_http_limit_survives_new_runs_and_reload(
    tmp_path, monkeypatch
):
    import fcntl

    from scripts.m0_environment import round02

    monkeypatch.setattr(round02.time, "time", lambda: round02.PROFILE.deadline - 60)
    path = tmp_path / "isolated-count-ledger.json"
    usage = {
        key: 0
        for key in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "prompt_cache_hit_tokens",
            "prompt_cache_miss_tokens",
        )
    }
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for index in range(20):
            budget = round02.Budget(path)
            if index == 0:
                # Isolated synthetic phase headroom exposes the independent
                # authorization-wide ceiling; never edits the real ledger.
                budget.data["phase_limits"]["normal"]["http"] = 20
            entry = budget.reserve(f"new-run-{index // 4}", "normal", 1)
            budget.finish(entry, 200, usage)
        reloaded = round02.Budget(path)
        with pytest.raises(ValueError, match="total HTTP budget"):
            reloaded.reserve("another-new-run", "normal", 1)
        assert len(reloaded.data["attempts"]) == 20
        assert reloaded.data["previous_allocation_reserved_cny"] == 24
