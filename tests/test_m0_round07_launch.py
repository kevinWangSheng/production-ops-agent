import hashlib
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest

from scripts.m0_lab.round07 import candidate_runner, launch
from scripts.m0_lab.round07.candidate_runner import validated_tool_calls
from scripts.m0_lab.round07.launch import validate_max_http


@pytest.mark.parametrize("value", [0, -1, True, 1.0])
def test_round07_launch_rejects_non_positive_max_http(value):
    with pytest.raises(ValueError, match="positive integer"):
        validate_max_http(value)


def test_round07_launch_accepts_positive_max_http():
    assert validate_max_http(1) == 1
    assert validate_max_http(3) == 3


def test_round07_launch_resolves_env_file_from_explicit_override(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("DEEPSEEK_API_KEY=synthetic\n")
    assert launch.resolve_env_file(env_file) == env_file.resolve()


def test_round07_launch_missing_env_file_is_explicit(tmp_path):
    with pytest.raises(ValueError, match="credential file unavailable"):
        launch.resolve_env_file(tmp_path / "missing.env")


def test_round07_launch_resolves_executable_holmes_override(tmp_path, monkeypatch):
    holmes = tmp_path / "python"
    holmes.write_text("#!/bin/sh\n")
    holmes.chmod(0o700)
    monkeypatch.setattr(launch, "DEFAULT_HOLMES_PYTHON", holmes)
    monkeypatch.setattr(
        launch, "HOLMES_PYTHON_SHA256", hashlib.sha256(holmes.read_bytes()).hexdigest()
    )
    assert launch.resolve_holmes_python(holmes) == holmes.resolve()


def test_round07_launch_missing_holmes_is_explicit(tmp_path):
    with pytest.raises(ValueError, match="Holmes Python path is not pinned"):
        launch.resolve_holmes_python(tmp_path / "missing-python")


def test_round07_launch_missing_holmes_checkout_is_explicit(tmp_path):
    with pytest.raises(ValueError, match="Holmes checkout path is not pinned"):
        launch.resolve_holmes_root(tmp_path / "missing-checkout")


def test_round07_container_rejects_arbitrary_executable_without_reading_key(
    monkeypatch, tmp_path
):
    env_file = tmp_path / ".env"
    env_file.write_text("DEEPSEEK_API_KEY=synthetic\n")
    ledger = tmp_path / "ledger.json"
    ledger.write_text(
        '{"runs": [], "http_count": 0, "known_cost_upper_cny": 0.0, '
        '"unknown_reservation_cny": 0.0}\n'
    )
    monkeypatch.setattr(launch, "LEDGER", ledger)
    monkeypatch.setattr(
        launch, "read_key", lambda *_: pytest.fail("must not read credentials")
    )
    monkeypatch.setattr(
        launch.sys,
        "argv",
        [
            "launch.py",
            "--arm",
            "container",
            "--run-id",
            "run-test",
            "--scenario",
            "normal",
            "--max-http",
            "1",
            "--out",
            str(tmp_path / "out"),
            "--env-file",
            str(env_file),
            "--",
            "/bin/cat",
        ],
    )
    with pytest.raises(ValueError, match="not allowlisted"):
        launch.main()


def test_round07_container_accepts_only_pinned_docker_command(monkeypatch, tmp_path):
    docker_path = tmp_path / "docker"
    docker_path.write_bytes(b"trusted synthetic docker")
    docker_path.chmod(0o700)
    monkeypatch.setattr(launch, "DOCKER_PATH", docker_path)
    monkeypatch.setattr(
        launch, "DOCKER_SHA256", hashlib.sha256(docker_path.read_bytes()).hexdigest()
    )
    docker = str(docker_path)
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    out = tmp_path / "tmp" / "round07"
    command = launch.build_container_command(
        docker, scenario="normal", run_id="run-test", max_http=1, out=out
    )
    assert launch.validate_container_command(command) == command


def test_round07_launch_timeout_releases_reservation_and_redacts_output(
    monkeypatch, tmp_path
):
    env_file = tmp_path / ".env"
    env_file.write_text("DEEPSEEK_API_KEY=synthetic-secret\n")
    ledger = tmp_path / "ledger.json"
    monkeypatch.setattr(launch, "LEDGER", ledger)
    out = tmp_path / "out"
    packet = launch.PACKET_ROOT / "packet-m004-normal.json"
    monkeypatch.setattr(
        launch.sys,
        "argv",
        [
            "launch.py",
            "--arm",
            "candidate",
            "--run-id",
            "timeout-run",
            "--scenario",
            "normal",
            "--max-http",
            "1",
            "--out",
            str(out),
            "--env-file",
            str(env_file),
            "--packet",
            str(packet),
        ],
    )

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            args[0],
            kwargs["timeout"],
            output="synthetic-secret",
            stderr="synthetic-secret",
        )

    monkeypatch.setattr(launch.subprocess, "run", timeout)
    assert launch.main() == 1
    saved = json.loads(ledger.read_text())
    assert saved["reserved_http"] == 0
    run = saved["runs"][0]
    assert run["status"] == "failed"
    assert run["failure"] == "RUNNER_TIMEOUT"
    assert "synthetic-secret" not in run["stdout_tail"]
    assert "synthetic-secret" not in run["stderr_tail"]


@pytest.mark.parametrize("value", [{}, "", None, [{"id": "ok"}, "bad"]])
def test_round07_candidate_rejects_malformed_tool_calls(value):
    with pytest.raises(ValueError, match="TOOL_PAIRING_INVALID"):
        validated_tool_calls({"tool_calls": value})


def test_round07_candidate_rejects_non_mapping_function():
    with pytest.raises(ValueError, match="TOOL_PAIRING_INVALID"):
        validated_tool_calls({"tool_calls": [{"function": "not-a-mapping"}]})


def test_round07_candidate_records_function_shape_error(tmp_path, monkeypatch):
    packet = launch.PACKET_ROOT / "packet-m004-normal.json"
    raw = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [{"id": "call-1", "function": "bad"}],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {},
        }
    ).encode()
    monkeypatch.setattr(
        candidate_runner, "post", lambda *args, **kwargs: (200, raw, 0.01)
    )
    result = candidate_runner.run(packet, tmp_path, "run-shape", 1, "synthetic", False)
    assert result["status"] == "failed"
    assert result["failure"] == "TOOL_PAIRING_INVALID"
    assert json.loads((tmp_path / "result-business.json").read_text())["failure"] == (
        "TOOL_PAIRING_INVALID"
    )


def test_round07_candidate_accepts_missing_or_list_tool_calls():
    assert validated_tool_calls({}) == []
    assert validated_tool_calls({"tool_calls": [{"id": "ok"}]}) == [{"id": "ok"}]


def test_round07_ledger_reservation_serializes_concurrent_cap_checks(
    monkeypatch, tmp_path
):
    ledger = tmp_path / "ledger.json"
    monkeypatch.setattr(launch, "LEDGER", ledger)
    monkeypatch.setattr(launch, "TOTAL_HTTP", 1)
    entries = [
        {"run_id": "run-a", "max_http": 1, "attempts": [], "status": "launched"},
        {"run_id": "run-b", "max_http": 1, "attempts": [], "status": "launched"},
    ]

    def reserve(entry):
        try:
            launch.reserve_run(entry)
            return "reserved"
        except SystemExit as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(reserve, entries))
    assert outcomes.count("reserved") == 1
    assert outcomes.count("allocation HTTP cap would be exceeded; not launched") == 1
    saved = json.loads(ledger.read_text())
    assert saved["reserved_http"] == 1
    assert len(saved["runs"]) == 1
