"""Installer product-image contract for issue #394 (B9, seams).

install.sh runs against a stub ``docker`` on PATH that records every call;
no real Docker, registry or network is used. A non-local OPSPILOT_IMAGE must
be pinned by digest (``...@sha256:<64 hex>``) and is refused before anything
is created; the local ``opspilot:local`` build passes the pyproject version.
"""

from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INSTALL = REPO / "deploy" / "compose" / "install.sh"
DIGEST = "sha256:" + "0123456789abcdef" * 4
STUB = """#!/bin/sh
for arg in "$@"; do printf '%s\\t' "$arg"; done >> "$OPSPILOT_394_DOCKER_LOG"
printf '\\n' >> "$OPSPILOT_394_DOCKER_LOG"
exit 0
"""


def _run(
    tmp_path: Path, command: str, image: str | None
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    assert INSTALL.is_file(), f"missing installer: {INSTALL.relative_to(REPO)}"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    (fake_bin / "docker").write_text(STUB)
    (fake_bin / "docker").chmod(0o755)
    log = tmp_path / "docker.log"
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("OPSPILOT_", "COMPOSE_", "DOCKER_"))
    }
    env.update(
        {
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "HOME": str(tmp_path / "home"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "OPSPILOT_SECRETS_DIR": str(tmp_path / "secrets"),
            "OPSPILOT_PROJECT": "opspilot_acceptance_394",
            "OPSPILOT_394_DOCKER_LOG": str(log),
        }
    )
    if image is not None:
        env["OPSPILOT_IMAGE"] = image
    result = subprocess.run(
        ["bash", str(INSTALL), command],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    calls = (
        [line.rstrip("\t").split("\t") for line in log.read_text().splitlines()]
        if log.exists()
        else []
    )
    return result, calls


def _creates_or_starts(call: list[str]) -> bool:
    if call[:1] in (["build"], ["pull"], ["run"], ["create"]):
        return True
    return call[:1] == ["compose"] and any(
        verb in call for verb in ("up", "run", "create", "start")
    )


UNPINNED = [
    "ghcr.io/kevinwangsheng/opspilot:0.1.0",
    "ghcr.io/kevinwangsheng/opspilot",
    "ghcr.io/kevinwangsheng/opspilot@sha256:" + "0" * 63,
]


@pytest.mark.parametrize("image", UNPINNED)
def test_install_refuses_an_unpinned_non_local_image_before_creating_anything(
    tmp_path: Path, image: str
) -> None:
    result, calls = _run(tmp_path, "install", image)
    assert result.returncode != 0, result.stdout + result.stderr
    assert result.stderr.strip(), "the refusal must say why"
    assert not (tmp_path / "secrets").exists(), (
        "refusal must precede the secrets directory"
    )
    assert not (tmp_path / "state").exists(), "refusal must precede install records"
    assert not (tmp_path / "config").exists(), (
        "refusal must precede any default secrets directory"
    )
    assert not any(_creates_or_starts(call) for call in calls), calls


def test_upgrade_refuses_an_unpinned_non_local_image_without_touching_the_install(
    tmp_path: Path,
) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    (secrets / "postgres_password").write_text("existing\n")
    (secrets / "install.env").write_text("OPSPILOT_IMAGE=opspilot:local\n")
    before = {path.name: path.read_text() for path in secrets.iterdir()}

    result, calls = _run(tmp_path, "upgrade", "ghcr.io/kevinwangsheng/opspilot:0.1.0")
    assert result.returncode != 0, result.stdout + result.stderr
    assert {path.name: path.read_text() for path in secrets.iterdir()} == before
    assert not any(_creates_or_starts(call) for call in calls), calls


@pytest.mark.parametrize(
    "image",
    [
        f"ghcr.io/kevinwangsheng/opspilot@{DIGEST}",
        f"ghcr.io/kevinwangsheng/opspilot:0.1.0@{DIGEST}",
    ],
)
def test_install_accepts_a_digest_pinned_image_and_pulls_it(
    tmp_path: Path, image: str
) -> None:
    result, calls = _run(tmp_path, "install", image)
    assert result.returncode == 0, result.stdout + result.stderr
    assert ["pull", image] in calls, calls
    assert not any(call[:1] == ["build"] for call in calls), calls


def test_local_build_passes_the_pyproject_version_as_a_build_arg(
    tmp_path: Path,
) -> None:
    version = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]
    result, calls = _run(tmp_path, "install", None)
    assert result.returncode == 0, result.stdout + result.stderr
    builds = [call for call in calls if call[:1] == ["build"]]
    assert builds, calls
    arg = f"OPSPILOT_VERSION={version}"
    for call in builds:
        assert "opspilot:local" in call, call
        pairs = list(zip(call, call[1:]))
        assert ("--build-arg", arg) in pairs or f"--build-arg={arg}" in call, call
