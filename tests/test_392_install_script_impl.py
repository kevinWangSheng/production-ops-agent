"""#392 install.sh without Docker: a stand-in ``docker`` on PATH records each
call, so the
secret files, their idempotence and the order of the install steps are
observable in PR CI. The real stack runs in the opt-in end-to-end test."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from opspilot.web.auth import token_digest, verify_password

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "deploy" / "compose" / "install.sh"

FAKE_DOCKER = f"""#!{sys.executable}
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a") as log:
    log.write(json.dumps({{"args": args, "env": {{k: os.environ.get(k) for k in (
        "OPSPILOT_ALLOWED_ORIGINS", "OPSPILOT_WEB_HOST_PORT", "OPSPILOT_SECRETS_DIR",
        "OPSPILOT_RUNTIME_UID", "OPSPILOT_AUTH_REVISION", "COMPOSE_PROFILES")}}}}) + "\\n")
"""

SECRET_FILES = (
    "postgres_password",
    "owner_pgpass",
    "web_pgpass",
    "worker_pgpass",
    "observer_pgpass",
    "retention_pgpass",
    "ui_password",
    "alertmanager_token",
    "web_ui_users",
    "web_event_tokens",
    "worker_env",
    "observer_env",
)


@pytest.fixture
def host(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    log = tmp_path / "docker.log"
    secrets = tmp_path / "home" / ".config" / "opspilot"

    def run(*args: str, **extra: str) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(tmp_path / "home"),
            "FAKE_DOCKER_LOG": str(log),
            **extra,
        }
        return subprocess.run(
            ["bash", str(INSTALL), *args],
            env=env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
        )

    def calls() -> list[dict]:
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    def reset() -> None:
        log.unlink(missing_ok=True)

    run.calls = calls  # type: ignore[attr-defined]
    run.reset = reset  # type: ignore[attr-defined]
    run.secrets = secrets  # type: ignore[attr-defined]
    return run


def _compose(calls: list[dict]) -> list[list[str]]:
    """The compose sub-command lines, after ``compose -p <p> -f <file>``."""
    return [c["args"][5:] for c in calls if c["args"][:1] == ["compose"]]


def _index(lines: list[list[str]], predicate) -> int:
    return next(i for i, line in enumerate(lines) if predicate(line))


def test_install_generates_private_secret_files_outside_the_repository(host):
    result = host()
    assert result.returncode == 0, result.stderr
    secrets: Path = host.secrets
    assert stat.S_IMODE(secrets.stat().st_mode) == 0o700
    for name in SECRET_FILES:
        path = secrets / name
        assert path.stat().st_size > 0, name
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, name
    password = (secrets / "ui_password").read_text().strip()
    name, _, encoded = (secrets / "web_ui_users").read_text().strip().partition("=")
    assert name == "operator" and verify_password(password, encoded)
    token = (secrets / "alertmanager_token").read_text().strip()
    assert (secrets / "web_event_tokens").read_text().strip() == (
        f"{token_digest(token)}=alertmanager"
    )
    for login in ("web", "worker", "observer", "retention"):
        line = (secrets / f"{login}_pgpass").read_text().strip()
        assert line.startswith(f"*:*:*:opspilot_{login}_app:")
    pg = (secrets / "postgres_password").read_text().strip()
    assert (
        secrets / "owner_pgpass"
    ).read_text().strip() == f"*:*:*:opspilot_owner:{pg}"


def test_no_secret_value_is_printed(host):
    result = host()
    assert result.returncode == 0, result.stderr
    output = result.stdout + result.stderr
    for name in ("postgres_password", "ui_password", "alertmanager_token"):
        assert (host.secrets / name).read_text().strip() not in output
    assert "operator" in output and str(host.secrets / "ui_password") in output


def test_reinstall_keeps_every_secret_unchanged(host):
    assert host().returncode == 0
    before = {name: (host.secrets / name).read_bytes() for name in SECRET_FILES}
    result = host("install")
    assert result.returncode == 0, result.stderr
    after = {name: (host.secrets / name).read_bytes() for name in SECRET_FILES}
    assert after == before
    assert "generated" not in result.stdout


def test_install_migrates_and_creates_roles_before_any_runtime_starts(host):
    assert host().returncode == 0
    lines = _compose(host.calls())
    stop = _index(lines, lambda line: line[0] == "stop")
    postgres = _index(lines, lambda line: line[0] == "up" and line[-1] == "postgres")
    migrate = _index(lines, lambda line: line[0] == "up" and line[-1] == "migrate")
    roles = _index(lines, lambda line: line[0] == "up" and line[-1] == "init-roles")
    runtimes = _index(lines, lambda line: line[0] == "up" and "web" in line)
    assert {"web", "worker", "observer"} <= set(lines[stop])
    assert stop < postgres < migrate < roles < runtimes
    assert "--no-deps" in lines[runtimes] and "--wait" in lines[runtimes]


def test_the_image_is_built_before_any_compose_step(host):
    assert host().returncode == 0
    calls = [c["args"] for c in host.calls()]
    assert calls[0][:1] == ["build"]
    assert "opspilot:local" in calls[0]


def test_the_workbench_port_is_passed_to_compose_and_remembered(host):
    assert host(OPSPILOT_WEB_HOST_PORT="18180").returncode == 0
    up = [c for c in host.calls() if c["args"][5:6] == ["up"]][-1]
    assert up["env"]["OPSPILOT_WEB_HOST_PORT"] == "18180"
    host.reset()
    assert host("upgrade").returncode == 0
    up = [c for c in host.calls() if c["args"][5:6] == ["up"]][-1]
    assert up["env"]["OPSPILOT_WEB_HOST_PORT"] == "18180"


def test_upgrade_stops_runtimes_then_migrates_then_starts(host):
    assert host().returncode == 0
    host.reset()
    result = host("upgrade")
    assert result.returncode == 0, result.stderr
    lines = _compose(host.calls())
    stop = _index(lines, lambda line: line[0] == "stop")
    migrate = _index(lines, lambda line: line[0] == "up" and line[-1] == "migrate")
    start = _index(lines, lambda line: line[0] == "up" and "web" in line)
    assert stop < migrate < start


def test_upgrade_without_an_install_is_refused(host):
    result = host("upgrade")
    assert result.returncode != 0
    assert not any(c["args"][-1:] == ["migrate"] for c in host.calls())


def test_uninstall_keeps_volumes_and_secrets(host):
    assert host().returncode == 0
    host.reset()
    result = host("uninstall")
    assert result.returncode == 0, result.stderr
    downs = [line for line in _compose(host.calls()) if line[0] == "down"]
    assert downs and all("--volumes" not in line and "-v" not in line for line in downs)
    assert all((host.secrets / name).exists() for name in SECRET_FILES)


def test_purge_without_confirmation_removes_nothing(host):
    assert host().returncode == 0
    host.reset()
    result = host("uninstall", "--purge-data")
    assert result.returncode != 0
    assert not any(line[0] == "down" for line in _compose(host.calls()))
    result = host("uninstall", "--purge-data", OPSPILOT_PURGE_CONFIRM="wrong")
    assert result.returncode != 0
    assert not any(line[0] == "down" for line in _compose(host.calls()))


def test_purge_with_the_project_name_removes_the_volume(host):
    assert host(OPSPILOT_PROJECT="opspilot-t").returncode == 0
    host.reset()
    result = host("uninstall", "--purge-data", OPSPILOT_PURGE_CONFIRM="opspilot-t")
    assert result.returncode == 0, result.stderr
    downs = [line for line in _compose(host.calls()) if line[0] == "down"]
    assert downs and "--volumes" in downs[0]
    assert (host.secrets / "ui_password").exists()


def test_a_secrets_dir_inside_the_repository_is_refused(host, tmp_path):
    inside = ROOT / "tmp" / f"secrets-{os.getpid()}"
    try:
        result = host(OPSPILOT_SECRETS_DIR=str(inside))
        assert result.returncode != 0
        assert "outside the repository" in result.stderr
        assert host.calls() == []
        assert not inside.exists()
    finally:
        if inside.exists():
            inside.rmdir()


def test_root_is_refused(host, monkeypatch):
    if os.getuid() == 0:
        pytest.skip("runs as root")
    # Stand-in `id` reporting uid 0.
    fake_id = Path(host.secrets).parent.parent.parent / "bin" / "id"
    fake_id.write_text("#!/bin/sh\necho 0\n")
    fake_id.chmod(0o755)
    result = host()
    assert result.returncode != 0
    assert "non-root" in result.stderr


def test_compose_passthrough_runs_with_the_installed_settings(host):
    assert (
        host(OPSPILOT_PROJECT="opspilot-t", OPSPILOT_WEB_HOST_PORT="18180").returncode
        == 0
    )
    host.reset()
    result = host("compose", "logs", "web")
    assert result.returncode == 0, result.stderr
    (call,) = host.calls()
    assert call["args"][:3] == ["compose", "-p", "opspilot-t"]
    assert call["args"][5:] == ["logs", "web"]
    assert call["env"]["OPSPILOT_SECRETS_DIR"] == str(host.secrets.resolve())
    assert call["env"]["OPSPILOT_WEB_HOST_PORT"] == "18180"


def test_the_compose_project_name_is_honoured_when_no_project_is_given(host):
    assert host(COMPOSE_PROJECT_NAME="from-compose-env").returncode == 0
    assert all(
        c["args"][:3] == ["compose", "-p", "from-compose-env"]
        for c in host.calls()
        if c["args"][:1] == ["compose"]
    )
