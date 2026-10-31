"""Installer preflight and upgrade-order contracts for issue #393 (B4, B7)."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INSTALL = REPO / "deploy/compose/install.sh"
DIGEST_IMAGE = "example.invalid/opspilot@sha256:" + "a" * 64

DOCKER_STUB = """#!/bin/sh
set -eu
printf '%s\\n' "$*" >> "$OPSPILOT_393_DOCKER_LOG"
exit 0
"""


def _base(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(DOCKER_STUB)
    docker.chmod(0o755)
    log = tmp_path / "docker.log"
    secrets = tmp_path / "secrets"
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("OPSPILOT_", "COMPOSE_", "DOCKER_", "XDG_"))
    }
    env.update(
        {
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "HOME": str(tmp_path / "home"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "OPSPILOT_SECRETS_DIR": str(secrets),
            "OPSPILOT_PROJECT": "opspilot_acceptance_393",
            "OPSPILOT_IMAGE": DIGEST_IMAGE,
            "OPSPILOT_393_DOCKER_LOG": str(log),
        }
    )
    return env, secrets, log


def _run(
    env: dict[str, str], command: str = "install"
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(INSTALL), command],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


@pytest.mark.parametrize(
    ("worker_contents", "observer_url", "needle"),
    [
        (None, "http://prometheus.invalid", "worker_env"),
        ("DEEPSEEK_API_KEY=\n", "http://prometheus.invalid", "DEEPSEEK_API_KEY"),
        (
            "DEEPSEEK_API_KEY=sentinel-model-key\n",
            "",
            "OPSPILOT_OBSERVER_PROMETHEUS_URL",
        ),
        (
            "DEEPSEEK_API_KEY=sentinel-model-key\n",
            "ftp://prometheus.invalid",
            "OPSPILOT_OBSERVER_PROMETHEUS_URL",
        ),
    ],
)
def test_agent_preflight_refuses_bad_inputs_before_any_docker_call(
    tmp_path: Path,
    worker_contents: str | None,
    observer_url: str,
    needle: str,
) -> None:
    env, secrets, log = _base(tmp_path)
    env.update(
        {"OPSPILOT_START_AGENT": "1", "OPSPILOT_OBSERVER_PROMETHEUS_URL": observer_url}
    )
    secrets.mkdir(mode=0o700)
    if worker_contents is not None:
        (secrets / "worker_env").write_text(worker_contents)
    result = _run(env)
    assert result.returncode != 0, result.stdout + result.stderr
    assert needle in result.stderr
    assert "sentinel-model-key" not in result.stdout + result.stderr
    assert not log.exists() or not log.read_text().strip(), (
        log.read_text() if log.exists() else ""
    )


def test_start_agent_zero_preserves_existing_non_agent_flow(tmp_path: Path) -> None:
    env, secrets, log = _base(tmp_path)
    env.update(
        {
            "OPSPILOT_START_AGENT": "0",
            "OPSPILOT_OBSERVER_PROMETHEUS_URL": "ftp://invalid",
        }
    )
    secrets.mkdir(mode=0o700)
    (secrets / "worker_env").write_text("DEEPSEEK_API_KEY=\n")
    result = _run(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert log.exists() and log.read_text().strip(), "normal mode should reach Docker"
    assert "OPSPILOT_OBSERVER_PROMETHEUS_URL" not in result.stderr


def test_upgrade_stops_all_runtimes_then_migrates_initializes_and_starts(
    tmp_path: Path,
) -> None:
    env, secrets, log = _base(tmp_path)
    env.update(
        {
            "OPSPILOT_START_AGENT": "1",
            "OPSPILOT_OBSERVER_PROMETHEUS_URL": "http://prometheus.invalid",
        }
    )
    secrets.mkdir(mode=0o700)
    (secrets / "postgres_password").write_text("existing")
    (secrets / "worker_env").write_text("DEEPSEEK_API_KEY=sentinel-model-key\n")
    result = _run(env, "upgrade")
    assert result.returncode == 0, result.stdout + result.stderr
    calls = log.read_text().splitlines()
    stop = next(
        i
        for i, line in enumerate(calls)
        if re.search(r"compose .*stop .*web .*worker .*observer", line)
    )
    migrate = next(
        i for i, line in enumerate(calls) if re.search(r"compose .*up .*migrate", line)
    )
    roles = next(
        i
        for i, line in enumerate(calls)
        if re.search(r"compose .*up .*init-roles", line)
    )
    start = next(
        i
        for i, line in enumerate(calls)
        if re.search(r"compose .*up .*web .*worker .*observer", line)
    )
    assert stop < migrate < roles < start
