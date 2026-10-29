"""#392 end to end against a real Docker engine: opt-in with
``OPSPILOT_COMPOSE_E2E=1``, never in PR CI.

Runs ``deploy/compose/install.sh`` on this host under a throwaway project
(``OPSPILOT_E2E_PROJECT``, default ``opspilot-e2e``) and workbench port
(``OPSPILOT_E2E_WEB_PORT``, default 18180), with secrets in a temporary
directory; uninstalls with ``--purge-data`` at the end. Builds the image from
this checkout (several minutes on first run).
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("OPSPILOT_COMPOSE_E2E") != "1",
    reason="explicit Docker opt-in required: OPSPILOT_COMPOSE_E2E=1",
)

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "deploy" / "compose" / "install.sh"
PROJECT = os.environ.get("OPSPILOT_E2E_PROJECT", "opspilot-e2e")
PORT = os.environ.get("OPSPILOT_E2E_WEB_PORT", "18180")


def _sh(*args: str, **kw) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False, **kw)


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    secrets = tmp_path_factory.mktemp("opspilot-secrets")
    env = {
        **os.environ,
        "OPSPILOT_SECRETS_DIR": str(secrets),
        # The install record stays with the test, not in the real ~/.local/state.
        "XDG_STATE_HOME": str(tmp_path_factory.mktemp("state")),
        "OPSPILOT_PROJECT": PROJECT,
        "OPSPILOT_WEB_HOST_PORT": PORT,
    }
    first = _sh("bash", str(INSTALL), "install", env=env, timeout=1800)
    try:
        yield env, secrets, first
    finally:
        _sh(
            "bash",
            str(INSTALL),
            "uninstall",
            "--purge-data",
            env={**env, "OPSPILOT_PURGE_CONFIRM": PROJECT},
            timeout=300,
        )


def _get(path: str, user: str | None = None, password: str | None = None) -> int:
    request = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}")
    if user is not None:
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        request.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _container(service: str) -> str:
    out = _sh("docker", "compose", "-p", PROJECT, "ps", "-aq", service).stdout.split()
    assert out, service
    return out[0]


def _inspect(ref: str) -> dict:
    return json.loads(_sh("docker", "inspect", ref).stdout)[0]


def test_fresh_install_migrates_before_the_workbench_starts(stack):
    _, _, first = stack
    assert first.returncode == 0, first.stdout[-4000:] + first.stderr[-4000:]
    log = first.stdout
    assert "schema upgraded:" in log + first.stderr
    for group in (
        "opspilot_web",
        "opspilot_worker",
        "opspilot_observer",
        "opspilot_retention",
    ):
        assert re.search(rf"{group}: login \w+ ok, schema at head \w+", log), group
    stamps = {
        step: stamp
        for stamp, step in re.findall(
            r"\[opspilot-install (\S+)\] (migrate: done|init-roles: done)", log
        )
    }
    for one_shot in ("migrate", "init-roles"):
        state = _inspect(_container(one_shot))["State"]
        assert (state["Status"], state["ExitCode"]) == ("exited", 0), one_shot
    started = _inspect(_container("web"))["State"]["StartedAt"]
    assert stamps["migrate: done"] <= stamps["init-roles: done"] <= started[:19] + "Z"


def test_the_workbench_login_works_with_the_generated_password_only(stack):
    _, secrets, _ = stack
    password = (secrets / "ui_password").read_text().strip()
    assert _get("/", "operator", password) == 200
    assert _get("/") == 401
    assert _get("/", "operator", password + "x") == 401


def test_the_workbench_is_published_on_loopback_only(stack):
    ports = _inspect(_container("web"))["NetworkSettings"]["Ports"]["8080/tcp"]
    assert [(p["HostIp"], p["HostPort"]) for p in ports] == [("127.0.0.1", PORT)]
    assert _inspect(_container("postgres"))["NetworkSettings"]["Ports"] in (
        {},
        {"5432/tcp": None},
    )


def test_no_secret_is_in_the_container_configuration(stack):
    _, secrets, _ = stack
    values = [
        (secrets / name).read_text().strip()
        for name in ("postgres_password", "ui_password", "alertmanager_token")
    ] + [(secrets / "web_pgpass").read_text().strip().rsplit(":", 1)[1]]
    for service in ("postgres", "web"):
        config = json.dumps(_inspect(_container(service))["Config"])
        assert not any(value in config for value in values), service


def test_the_image_runs_non_root_and_carries_no_env_tmp_evidence_or_vcs(stack):
    image = os.environ.get("OPSPILOT_IMAGE", "opspilot:local")
    assert _inspect(image)["Config"]["User"] not in ("", "0", "root", "0:0")
    uid = _sh("docker", "exec", _container("web"), "id", "-u").stdout.strip()
    assert uid and uid != "0"
    listing = _sh(
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "sh",
        image,
        "-c",
        "find /app -maxdepth 3 \\( -name '.env*' -o -name tmp -o -name evidence -o -name .git \\) -print; ls -A /app",
    ).stdout.split()
    assert sorted(listing) == [".venv", "opspilot"]


def test_reinstall_keeps_passwords_and_uninstall_keeps_the_volume(stack):
    env, secrets, _ = stack
    names = sorted(p.name for p in secrets.iterdir())
    before = {name: (secrets / name).read_bytes() for name in names}
    again = _sh("bash", str(INSTALL), "install", env=env, timeout=1800)
    assert again.returncode == 0, again.stdout[-4000:] + again.stderr[-4000:]
    assert {name: (secrets / name).read_bytes() for name in names} == before
    password = (secrets / "ui_password").read_text().strip()
    assert _get("/", "operator", password) == 200
    down = _sh("bash", str(INSTALL), "uninstall", env=env, timeout=300)
    assert down.returncode == 0, down.stderr
    volumes = _sh(
        "docker",
        "volume",
        "ls",
        "-q",
        "--filter",
        f"label=com.docker.compose.project={PROJECT}",
    ).stdout.split()
    assert volumes == [f"{PROJECT}_pgdata"]
