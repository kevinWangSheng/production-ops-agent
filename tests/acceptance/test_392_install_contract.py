"""Contract checks for the operator-facing Compose installer in #392."""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
INSTALL = REPO / "deploy" / "compose" / "install.sh"


def _script() -> str:
    assert INSTALL.is_file(), (
        f"required #392 seam is missing: {INSTALL.relative_to(REPO)}"
    )
    return INSTALL.read_text()


def _fake_docker_environment(tmp_path: Path) -> tuple[dict[str, str], Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "docker.log"
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "printf '%s\\n' \"$*\" >> \"$OPSPILOT_392_DOCKER_LOG\"\n"
        "case \" $* \" in\n"
        "  *' compose version '*) printf 'Docker Compose version v2\\n' ;;\n"
        "esac\n"
    )
    docker.chmod(0o755)
    return (
        {
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "OPSPILOT_392_DOCKER_LOG": str(log),
            "OPSPILOT_SECRETS_DIR": str(tmp_path / "secrets"),
            "COMPOSE_PROJECT_NAME": "opspilot_acceptance_392",
            "CI": "1",
        },
        log,
    )


def _run_install(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(INSTALL), *args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_install_script_is_executable_and_valid_shell() -> None:
    assert INSTALL.is_file(), (
        f"required #392 seam is missing: {INSTALL.relative_to(REPO)}"
    )
    assert stat.S_IMODE(INSTALL.stat().st_mode) & 0o111, "install.sh must be executable"
    result = subprocess.run(
        ["bash", "-n", str(INSTALL)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_install_script_exposes_install_upgrade_and_safe_uninstall_modes() -> None:
    text = _script()
    for command in ("install", "upgrade", "uninstall"):
        assert re.search(rf"\b{re.escape(command)}\b", text), command
    assert "--purge-data" in text
    assert "OPSPILOT_SECRETS_DIR" in text

    # The default uninstall path keeps named volumes.  A destructive volume
    # removal may occur only inside the explicit purge branch.
    purge = text.find("--purge-data")
    assert purge >= 0
    for match in re.finditer(r"\bdown\s+-v\b", text):
        assert match.start() >= purge, "volume deletion must be purge-only"


def test_install_script_uses_a_repo_external_default_secret_directory() -> None:
    text = _script()
    assignment = re.search(
        r"OPSPILOT_SECRETS_DIR[^\n]*:?[-=]\s*([^\s;]+)", text
    )
    assert assignment, "installer must define a default secrets directory"
    default = assignment.group(1).strip("'\"()")
    assert default not in {".", "./secrets", "${PWD}/secrets", "$PWD/secrets"}
    assert "OPSPILOT_SECRETS_DIR" in text


def test_install_and_upgrade_observe_migration_before_runtime_start(tmp_path: Path) -> None:
    assert INSTALL.is_file(), (
        f"required #392 seam is missing: {INSTALL.relative_to(REPO)}"
    )
    env, log = _fake_docker_environment(tmp_path)

    def run(label: str, *args: str) -> list[str]:
        with log.open("a") as stream:
            stream.write(f"BEGIN {label}\n")
        result = _run_install(env, *args)
        assert result.returncode == 0, result.stdout + result.stderr
        return log.read_text().splitlines()

    install_log = run("install", "install")
    upgrade_log = run("upgrade", "upgrade")

    def events(lines: list[str], label: str) -> list[str]:
        active = False
        result: list[str] = []
        for line in lines:
            if line == f"BEGIN {label}":
                active = True
                continue
            if active and line.startswith("BEGIN "):
                break
            if not active:
                continue
            lowered = line.lower()
            if re.search(r"\b(?:up|run)\b.*\bmigrate\b", lowered):
                result.append("migrate")
            elif re.search(r"\b(?:up|run)\b.*\binit-roles\b", lowered):
                result.append("init-roles")
            elif re.search(r"\bstop\b.*\b(?:web|worker|observer)\b", lowered):
                result.append("stop-runtimes")
            elif re.search(r"\bup\b", lowered) and re.search(
                r"\b(?:web|worker|observer)\b", lowered
            ):
                result.append("start-runtimes")
        return result

    install_events = events(install_log, "install")
    upgrade_events = events(upgrade_log, "upgrade")
    assert install_events.index("migrate") < install_events.index("start-runtimes")
    assert install_events.index("init-roles") < install_events.index("start-runtimes")
    assert upgrade_events.index("stop-runtimes") < upgrade_events.index("migrate")
    assert upgrade_events.index("migrate") < upgrade_events.index("start-runtimes")


def test_install_is_idempotent_for_secret_bytes_and_modes(tmp_path: Path) -> None:
    assert INSTALL.is_file(), (
        f"required #392 seam is missing: {INSTALL.relative_to(REPO)}"
    )
    env, _ = _fake_docker_environment(tmp_path)
    first = _run_install(env, "install")
    assert first.returncode == 0, first.stdout + first.stderr
    secrets = Path(env["OPSPILOT_SECRETS_DIR"])
    assert secrets.is_dir()
    snapshot = {
        path.relative_to(secrets): (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        for path in secrets.rglob("*")
        if path.is_file()
    }
    assert snapshot, "install must materialize its secret files"

    second = _run_install(env, "install")
    assert second.returncode == 0, second.stdout + second.stderr
    current = {
        path.relative_to(secrets): (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        for path in secrets.rglob("*")
        if path.is_file()
    }
    assert current == snapshot
    assert all(mode == 0o600 for _, mode in current.values())

