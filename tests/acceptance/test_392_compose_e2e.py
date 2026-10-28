"""Opt-in Docker acceptance for the #392 Compose installation path.

The test is skipped in normal PR runs.  Set ``OPSPILOT_COMPOSE_E2E=1`` on a
Linux host with Docker and credentials prepared for the fixture profile.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INSTALL = REPO / "deploy" / "compose" / "install.sh"
COMPOSE = REPO / "deploy" / "compose" / "compose.yaml"


pytestmark = pytest.mark.skipif(
    os.environ.get("OPSPILOT_COMPOSE_E2E") != "1",
    reason="set OPSPILOT_COMPOSE_E2E=1 to run the Docker Compose acceptance gate",
)


def _compose(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE),
            "-p",
            env["COMPOSE_PROJECT_NAME"],
            *args,
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _records(output: str) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for line in output.splitlines():
        if line.strip():
            value = json.loads(line)
            if isinstance(value, dict):
                records.append(value)
    return records


def test_fresh_install_reaches_a_migrated_loopback_workbench() -> None:
    assert INSTALL.is_file() and COMPOSE.is_file()
    project = f"opspilot_acceptance_392_{os.getpid()}"
    secrets = REPO / ".tmp-acceptance-392-secrets"
    env = os.environ.copy()
    env.update(
        {
            "COMPOSE_PROJECT_NAME": project,
            "OPSPILOT_SECRETS_DIR": str(secrets),
            "OPSPILOT_WEB_HOST_PORT": env.get("OPSPILOT_WEB_HOST_PORT", "18080"),
        }
    )

    try:
        installed = subprocess.run(
            ["bash", str(INSTALL), "install"],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert installed.returncode == 0, installed.stdout + installed.stderr

        ps = _compose("ps", "--all", "--format", "json", env=env)
        assert ps.returncode == 0, ps.stdout + ps.stderr
        records = _records(ps.stdout)
        by_service = {str(record.get("Service")): record for record in records}
        for service in ("migrate", "init-roles"):
            assert service in by_service, by_service
            assert "exited (0)" in str(by_service[service].get("State", "")).lower()
        assert "web" in by_service, by_service
        assert str(by_service["web"].get("State", "")).lower() in {"running", "healthy"}

        port = env["OPSPILOT_WEB_HOST_PORT"]
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/", timeout=10
            ) as response:
                assert response.status < 500
        except urllib.error.HTTPError as response:
            # Authentication is expected at the public seam; a reachable 4xx
            # still proves the loopback listener is serving the workbench.
            assert response.code < 500
    finally:
        # This is an opt-in fixture-owned cleanup.  The explicit purge flag
        # makes the destructive action visible and leaves ordinary uninstall
        # semantics covered by the static contract tests.
        subprocess.run(
            ["bash", str(INSTALL), "uninstall", "--purge-data"],
            cwd=REPO,
            env=env,
            input="yes\n",
            capture_output=True,
            text=True,
            check=False,
        )
