import pytest

from scripts.m0_lab.round07 import launch
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


def test_round07_launch_resolves_executable_holmes_override(tmp_path):
    holmes = tmp_path / "python"
    holmes.write_text("#!/bin/sh\n")
    holmes.chmod(0o700)
    assert launch.resolve_holmes_python(holmes) == holmes.resolve()


def test_round07_launch_missing_holmes_is_explicit(tmp_path):
    with pytest.raises(ValueError, match="Holmes Python unavailable"):
        launch.resolve_holmes_python(tmp_path / "missing-python")


def test_round07_launch_missing_holmes_checkout_is_explicit(tmp_path):
    with pytest.raises(ValueError, match="Holmes checkout unavailable"):
        launch.resolve_holmes_root(tmp_path / "missing-checkout")


def test_round07_container_rejects_arbitrary_executable_without_reading_key(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(launch.shutil, "which", lambda name: "/usr/bin/docker")
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
    docker = "/usr/bin/docker"
    monkeypatch.setattr(launch.shutil, "which", lambda name: docker)
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    out = tmp_path / "tmp" / "round07"
    command = launch.build_container_command(
        docker, scenario="normal", run_id="run-test", max_http=1, out=out
    )
    assert launch.validate_container_command(command) == command


@pytest.mark.parametrize("value", [{}, "", None, [{"id": "ok"}, "bad"]])
def test_round07_candidate_rejects_malformed_tool_calls(value):
    with pytest.raises(ValueError, match="TOOL_PAIRING_INVALID"):
        validated_tool_calls({"tool_calls": value})


def test_round07_candidate_accepts_missing_or_list_tool_calls():
    assert validated_tool_calls({}) == []
    assert validated_tool_calls({"tool_calls": [{"id": "ok"}]}) == [{"id": "ok"}]
