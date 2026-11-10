"""#531: ``kind_lab.py control-secrets`` prepares install.sh's secrets
directory for a lab on the same host, and ``install_linux.sh --user`` serves
a root-only image. The F9 step 1 clean-host run needed four manual steps;
these pin what replaces them: the lab's Alertmanager token as the
installer's, the otel-demo endpoints and role accounts, the lab target, and
a workbench port clear of the lab frontend."""

from __future__ import annotations

import argparse
import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from opspilot.web.auth import token_digest
from scripts import kind_lab

INSTALLER = Path(kind_lab.ROOT) / "scripts/kind_lab/install_linux.sh"


@pytest.fixture
def lab(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    lab_dir = tmp_path / "lab"
    monkeypatch.setattr(kind_lab, "WEBHOOK_TOKEN_FILE", lab_dir / "token")
    monkeypatch.setattr(kind_lab, "AUTH_FILE", lab_dir / "auth.json")
    monkeypatch.delenv("OPSPILOT_SECRETS_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return lab_dir


def _run(secrets_dir: Path | None = None) -> int:
    args = argparse.Namespace(secrets_dir=str(secrets_dir) if secrets_dir else None)
    return kind_lab.cmd_control_secrets(args)


def _env(path: Path) -> dict[str, str]:
    pairs = (
        line.split("=", 1)
        for line in path.read_text().splitlines()
        if line and not line.startswith("#")
    )
    return dict(pairs)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_fresh_directory_gets_the_lab_wiring(
    lab: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run() == 0
    secrets = tmp_path / "config/opspilot"
    assert _mode(secrets) == 0o700
    token = kind_lab.WEBHOOK_TOKEN_FILE.read_text().strip()
    assert (secrets / "alertmanager_token").read_text().strip() == token
    accounts = json.loads(kind_lab.AUTH_FILE.read_text())
    assert _env(secrets / "worker_env") == {
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://opspilot-m1-control-plane:30090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://opspilot-m1-control-plane:30686/jaeger/ui",
        "OPSPILOT_OTEL_PROMETHEUS_USERNAME": "investigator",
        "OPSPILOT_OTEL_PROMETHEUS_PASSWORD": accounts["investigator"],
    }
    assert _env(secrets / "observer_env") == {
        "OPSPILOT_OBSERVER_PROMETHEUS_USERNAME": "observer",
        "OPSPILOT_OBSERVER_PROMETHEUS_PASSWORD": accounts["observer"],
    }
    targets = json.loads((secrets / "target_identities.json").read_text())
    assert targets["m0-otel-20260909"]["match"] == {
        "version": 1,
        "labels": {"namespace": "otel-demo", "service": "checkout"},
    }
    assert targets["m0-otel-20260909"]["cluster_uid"] == "opspilot-m1"
    assert _env(secrets / "install.env") == {
        "OPSPILOT_WEB_HOST_PORT": "18090",
        "OPSPILOT_TOOL_PROFILE": "otel-demo",
        "OPSPILOT_OBSERVER_PROMETHEUS_URL": "http://opspilot-m1-control-plane:30090",
    }
    for path in secrets.iterdir():
        assert _mode(path) == 0o600, path
    out = capsys.readouterr().out
    for secret in (token, *accounts.values()):
        assert secret not in out


def test_web_port_clears_every_lab_instance_port() -> None:
    lab_ports = {
        base + n * kind_lab.PORT_STEP
        for base in (18080, 19090, 16686)
        for n in range(kind_lab.MAX_INSTANCE + 1)
    }
    assert kind_lab.CONTROL_WEB_PORT not in lab_ports


def test_installer_placeholders_are_completed_and_kept(
    lab: Path, tmp_path: Path
) -> None:
    secrets = tmp_path / "s"
    secrets.mkdir()
    worker_env = secrets / "worker_env"
    worker_env.write_text("# worker_env: placeholder\nDEEPSEEK_API_KEY=k\n")
    observer_env = secrets / "observer_env"
    observer_env.write_text("OPSPILOT_OBSERVER_PROMETHEUS_USERNAME=mine")
    (secrets / "target_identities.json").write_text("{}\n")
    assert _run(secrets) == 0
    worker = _env(worker_env)
    assert worker["DEEPSEEK_API_KEY"] == "k"
    assert worker["OPSPILOT_OTEL_PROMETHEUS_USERNAME"] == "investigator"
    assert worker_env.read_text().startswith("# worker_env: placeholder\n")
    observer = _env(observer_env)
    assert observer["OPSPILOT_OBSERVER_PROMETHEUS_USERNAME"] == "mine"
    assert "OPSPILOT_OBSERVER_PROMETHEUS_PASSWORD" in observer
    assert list(json.loads((secrets / "target_identities.json").read_text())) == [
        "m0-otel-20260909"
    ]


def test_rerun_changes_nothing(
    lab: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secrets = tmp_path / "s"
    assert _run(secrets) == 0
    before = {p.name: p.read_bytes() for p in secrets.iterdir()}
    capsys.readouterr()
    assert _run(secrets) == 0
    assert {p.name: p.read_bytes() for p in secrets.iterdir()} == before
    report = json.loads(capsys.readouterr().out.splitlines()[0])
    assert set(report["files"].values()) == {"kept"}


def test_an_installer_generated_token_is_refused(
    lab: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # the web's event token hash was derived from the installer's token, so
    # replacing it would 401 every alert; nothing else is written either
    secrets = tmp_path / "s"
    secrets.mkdir()
    (secrets / "alertmanager_token").write_text("installer-token\n")
    assert _run(secrets) == 1
    assert sorted(p.name for p in secrets.iterdir()) == ["alertmanager_token"]
    assert (secrets / "alertmanager_token").read_text() == "installer-token\n"
    err = capsys.readouterr().err
    assert "installer-token" not in err
    assert str(kind_lab.WEBHOOK_TOKEN_FILE) in err


def test_secrets_inside_the_repository_are_refused(lab: Path) -> None:
    with pytest.raises(SystemExit):
        _run(Path(kind_lab.ROOT) / "tmp/secrets")


def test_command_is_registered() -> None:
    proc = subprocess.run(
        ["python3", "scripts/kind_lab.py", "control-secrets", "--help"],
        cwd=kind_lab.ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0
    assert "--secrets-dir" in proc.stdout


# --- install_linux.sh on a root-only image


def test_installer_accepts_a_lab_user() -> None:
    usage = subprocess.run(
        ["bash", str(INSTALLER), "--help"], capture_output=True, text=True, timeout=30
    )
    assert usage.returncode == 0
    assert "--user <name>" in usage.stdout
    missing = subprocess.run(
        ["bash", str(INSTALLER), "--user"], capture_output=True, text=True, timeout=30
    )
    assert missing.returncode == 2


def test_installer_checks_the_lab_user_before_installing() -> None:
    text = INSTALLER.read_text()
    check = text.index('runuser -u "$LAB_USER" -- test -w "$REPO_ROOT"')
    assert check < text.index("apt-get update")
    # codex P1 (#532): a root-only minimal image may have no sudo at all
    assert "sudo -u" not in text
    assert 'LAB_USER="${SUDO_USER:-root}"' in text


def test_install_differing_settings_are_named(
    lab: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secrets = tmp_path / "s"
    secrets.mkdir()
    (secrets / "install.env").write_text(
        "OPSPILOT_WEB_HOST_PORT=18080\nOPSPILOT_TOOL_PROFILE=fixture\n"
    )
    assert _run(secrets) == 0
    out = capsys.readouterr().out
    assert "OPSPILOT_WEB_HOST_PORT=18090" in out
    assert "OPSPILOT_TOOL_PROFILE=otel-demo" in out
    assert "OPSPILOT_OBSERVER_PROMETHEUS_URL=" in out


# --- the documented sequence through install.sh (stand-in docker, as #392)

FAKE_DOCKER = f"""#!{sys.executable}
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a") as log:
    log.write(json.dumps({{"args": args, "env": {{k: os.environ.get(k) for k in (
        "OPSPILOT_WEB_HOST_PORT", "OPSPILOT_TOOL_PROFILE",
        "OPSPILOT_OBSERVER_PROMETHEUS_URL", "COMPOSE_PROFILES")}}}}) + "\\n")
"""


def test_install_after_control_secrets_uses_the_lab_wiring(
    lab: Path, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    secrets = home / ".config/opspilot"
    assert _run(secrets) == 0
    with (secrets / "worker_env").open("a") as handle:
        handle.write("DEEPSEEK_API_KEY=test-key\n")
    prepared = {p.name: p.read_bytes() for p in secrets.iterdir()}
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(FAKE_DOCKER)
    (bin_dir / "docker").chmod(0o755)
    log = tmp_path / "docker.log"
    result = subprocess.run(
        ["bash", str(Path(kind_lab.ROOT) / "deploy/compose/install.sh"), "install"],
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(home),
            "FAKE_DOCKER_LOG": str(log),
            "OPSPILOT_START_AGENT": "1",
        },
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    up = [c for c in calls if c["args"][5:6] == ["up"]][-1]
    assert {"web", "worker", "observer"} <= set(up["args"])
    assert up["env"] == {
        "OPSPILOT_WEB_HOST_PORT": "18090",
        "OPSPILOT_TOOL_PROFILE": "otel-demo",
        "OPSPILOT_OBSERVER_PROMETHEUS_URL": "http://opspilot-m1-control-plane:30090",
        "COMPOSE_PROFILES": "agent",
    }
    for name, content in prepared.items():
        if name != "install.env":
            assert (secrets / name).read_bytes() == content, name
    lab_token = kind_lab.WEBHOOK_TOKEN_FILE.read_text().strip()
    assert (secrets / "web_event_tokens").read_text().strip() == (
        f"{token_digest(lab_token)}=alertmanager"
    )
