"""#393 end to end on a real Docker engine with a real model: opt-in with
``OPSPILOT_393_LIVE=1``, never in PR CI (it spends model credit).

Installs from this checkout under a throwaway project, fills the worker's
model key and the Observer's read-only Prometheus account, starts web +
worker + Observer with ``OPSPILOT_START_AGENT=1 install.sh upgrade``, sends
one Alertmanager webhook with synthetic labels and waits for the Run to
reach a terminal state with its report on the workbench page. Uninstalls
with ``--purge-data`` at the end.

* ``OPSPILOT_393_MODEL_ENV_FILE``  private ``KEY=value`` file with
  ``DEEPSEEK_API_KEY`` (only that line is copied)
* ``OPSPILOT_393_OBSERVER_ENV_FILE`` the Observer's own Prometheus account
  (``OPSPILOT_OBSERVER_PROMETHEUS_USERNAME`` / ``_PASSWORD`` or ``_TOKEN``)
* ``OPSPILOT_393_PROMETHEUS_URL``  that Prometheus, as seen from the
  Observer container
* ``OPSPILOT_393_OBSERVER_NETWORK`` optional Docker network the Observer
  container is also attached to so it can reach that URL (``kind`` for the
  local kind lab)
* ``OPSPILOT_393_PROJECT`` / ``OPSPILOT_393_WEB_PORT`` (default
  ``opspilot-393live`` / 18180)

The secrets directory is created under ``$HOME`` (colima only mounts the
home directory into the VM) and removed at the end.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("OPSPILOT_393_LIVE") != "1",
    reason="explicit live opt-in required: OPSPILOT_393_LIVE=1",
)

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "deploy" / "compose" / "install.sh"
PROJECT = os.environ.get("OPSPILOT_393_PROJECT", "opspilot-393live")
PORT = os.environ.get("OPSPILOT_393_WEB_PORT", "18180")
TERMINAL = {"completed", "cancelled", "failed", "blocked", "budget_exhausted"}
LABELS = {"alertname": "OpsPilot393Synthetic", "opspilot_393": "live-test"}
TARGET = {
    "checkout-prod": {
        "integration_id": "fixture",
        "cluster_uid": "fixture-cluster",
        "namespace": "otel-demo",
        "workload": "checkout",
        "health_profile_id": "otel-demo-checkout",
        "match": {"version": 1, "labels": LABELS},
    }
}


def _sh(*args: str, **kw) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False, **kw)


def _model_key_line(path: str) -> str:
    for line in Path(path).read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key.strip() == "DEEPSEEK_API_KEY" and value.strip():
            return f"DEEPSEEK_API_KEY={value.strip().strip(chr(39) + chr(34))}\n"
    pytest.fail("OPSPILOT_393_MODEL_ENV_FILE has no DEEPSEEK_API_KEY")


def _private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        out.write(text)


@pytest.fixture(scope="module")
def stack():
    required = (
        "OPSPILOT_393_MODEL_ENV_FILE",
        "OPSPILOT_393_OBSERVER_ENV_FILE",
        "OPSPILOT_393_PROMETHEUS_URL",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        pytest.fail(f"OPSPILOT_393_LIVE=1 also needs {missing}")
    base = Path(tempfile.mkdtemp(prefix="opspilot-393-", dir=Path.home()))
    secrets = base / "secrets"
    env = {
        **os.environ,
        "OPSPILOT_SECRETS_DIR": str(secrets),
        "XDG_STATE_HOME": str(base / "state"),
        "OPSPILOT_PROJECT": PROJECT,
        "OPSPILOT_WEB_HOST_PORT": PORT,
    }
    try:
        first = _sh("bash", str(INSTALL), "install", env=env, timeout=1800)
        assert first.returncode == 0, first.stderr[-2000:]
        _private(
            secrets / "worker_env",
            _model_key_line(os.environ["OPSPILOT_393_MODEL_ENV_FILE"]),
        )
        _private(
            secrets / "observer_env",
            Path(os.environ["OPSPILOT_393_OBSERVER_ENV_FILE"]).read_text(),
        )
        _private(secrets / "target_identities.json", json.dumps(TARGET))
        agent = {
            **env,
            "OPSPILOT_START_AGENT": "1",
            "OPSPILOT_OBSERVER_PROMETHEUS_URL": os.environ[
                "OPSPILOT_393_PROMETHEUS_URL"
            ],
        }
        upgrade = _sh("bash", str(INSTALL), "upgrade", env=agent, timeout=1800)
        assert upgrade.returncode == 0, upgrade.stderr[-2000:]
        network = os.environ.get("OPSPILOT_393_OBSERVER_NETWORK")
        if network:
            joined = _sh(
                "docker", "network", "connect", network, f"{PROJECT}-observer-1"
            )
            assert joined.returncode == 0, joined.stderr
        yield env, secrets, upgrade
    finally:
        _sh(
            "bash",
            str(INSTALL),
            "uninstall",
            "--purge-data",
            env={**env, "OPSPILOT_PURGE_CONFIRM": PROJECT},
            timeout=300,
        )
        shutil.rmtree(base, ignore_errors=True)


def _logs(env, service: str) -> str:
    result = _sh("bash", str(INSTALL), "compose", "logs", service, env=env, timeout=60)
    return result.stdout + result.stderr


def _get(secrets: Path, path: str) -> tuple[int, str]:
    password = (secrets / "ui_password").read_text().strip()
    basic = base64.b64encode(f"operator:{password}".encode()).decode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}", headers={"Authorization": f"Basic {basic}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def test_upgrade_with_the_agent_stops_runtimes_before_migrating(stack):
    _env, _secrets, upgrade = stack
    out = upgrade.stdout
    assert (
        out.index("stopping runtimes")
        < out.index("migrate: done")
        < out.index("init-roles: done")
        < out.index("starting runtimes: web worker observer")
    )


def test_worker_and_observer_start_after_verifying_the_schema_head(stack):
    env, _secrets, _upgrade = stack
    assert "worker started" in _logs(env, "worker")
    assert "observer started" in _logs(env, "observer")


def test_the_observer_reads_prometheus_with_its_own_account(stack):
    probe = (
        "import base64,os,urllib.request;"
        "from opspilot.observer.__main__ import _basic_auth,_token;"
        "e=os.environ;u=e['OPSPILOT_OBSERVER_PROMETHEUS_URL'].rstrip('/');"
        "b=_basic_auth(e);t=_token(e);h={};"
        "h.update({'Authorization':'Basic '+base64.b64encode(':'.join(b).encode()).decode()} if b else {});"
        "h.update({'Authorization':'Bearer '+t} if t else {});"
        "r=urllib.request.urlopen(urllib.request.Request(u+'/api/v1/query?query=vector(1)',headers=h),timeout=10);"
        "print(r.status)"
    )
    result = _sh(
        "docker", "exec", f"{PROJECT}-observer-1", "python", "-c", probe, timeout=60
    )
    assert result.stdout.strip() == "200", result.stderr[-500:]


def test_an_alertmanager_webhook_runs_to_a_terminal_state_with_a_report(stack):
    _env, secrets, _upgrade = stack
    token = (secrets / "alertmanager_token").read_text().strip()
    now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    payload = {
        "version": "4",
        "status": "firing",
        "receiver": "opspilot",
        "alerts": [
            {
                "status": "firing",
                "fingerprint": f"393live{int(time.time())}",
                "startsAt": now,
                "labels": {**LABELS, "severity": "warning"},
                "annotations": {"summary": "Synthetic #393 live-test alert"},
            }
        ],
    }
    request = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/intake/alertmanager",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        (result,) = json.loads(response.read())["results"]
    assert result["outcome"] == "created" and result["run_id"], result
    deadline = time.monotonic() + 900
    state = None
    while time.monotonic() < deadline:
        status, page = _get(secrets, f"/incidents/{result['incident_id']}")
        assert status == 200
        match = re.search(r'id="run-state">([a-z_]+)<', page)
        state = match and match.group(1)
        if state in TERMINAL:
            break
        time.sleep(10)
    assert state in TERMINAL, state
    assert result["run_id"] in page
    assert 'id="report-status"' in page
