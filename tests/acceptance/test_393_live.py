"""Opt-in installed-stack acceptance for #393; never runs in PR CI by default."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INSTALL = REPO / "deploy/compose/install.sh"
ACTIVE_RUN_STATES = {
    "queued",
    "running",
    "pending",
    "paused",
    "waiting",
    "in_progress",
    "active",
}

pytestmark = pytest.mark.skipif(
    os.environ.get("OPSPILOT_393_LIVE") != "1",
    reason="set OPSPILOT_393_LIVE=1 with Docker, DeepSeek, and Prometheus to run",
)


def test_fixture_profile_real_alert_reaches_terminal_workbench() -> None:
    """The operator supplies keys/URL; the test triggers one Alertmanager alert."""
    model_key = os.environ.get("DEEPSEEK_API_KEY")
    prometheus = os.environ.get("OPSPILOT_OBSERVER_PROMETHEUS_URL")
    if not model_key or not prometheus:
        pytest.skip(
            "DEEPSEEK_API_KEY and OPSPILOT_OBSERVER_PROMETHEUS_URL are required"
        )
    secrets = Path(
        os.environ.get("OPSPILOT_393_SECRETS_DIR", "/tmp/opspilot-393-secrets")
    )
    secrets.mkdir(mode=0o700, parents=True, exist_ok=True)
    existing = sorted(path.name for path in secrets.iterdir())
    if existing:
        pytest.fail(
            f"refusing to run against a non-empty secrets directory: {secrets}"
        )
    identities = secrets / "target_identities.json"
    identities.write_text(
        json.dumps(
            {
                "checkout-prod": {
                    "integration_id": "fixture-prometheus",
                    "cluster_uid": "fixture-cluster",
                    "namespace": "otel-demo",
                    "workload": "checkout",
                    "health_profile_id": "otel-demo-checkout",
                    "match": {
                        "version": 1,
                        "labels": {"alertname": "OpsPilot393"},
                    },
                }
            }
        )
        + "\n"
    )
    identities.chmod(0o600)
    worker_env = secrets / "worker_env"
    worker_env.write_text(f"DEEPSEEK_API_KEY={model_key}\n")
    worker_env.chmod(0o600)
    observer_env = secrets / "observer_env"
    observer_env.write_text("")
    observer_env.chmod(0o600)
    env = os.environ.copy()
    env.update(
        {
            "OPSPILOT_START_AGENT": "1",
            "OPSPILOT_TOOL_PROFILE": "fixture",
            "OPSPILOT_SECRETS_DIR": str(secrets),
            "OPSPILOT_OBSERVER_PROMETHEUS_URL": prometheus,
            "OPSPILOT_TARGET_IDENTITIES": str(identities),
        }
    )
    installed = subprocess.run(
        ["bash", str(INSTALL), "install"],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        timeout=900,
    )
    assert installed.returncode == 0, "installed stack failed to start"
    network = os.environ.get("OPSPILOT_393_DOCKER_NETWORK")
    if network:
        project = env.get("OPSPILOT_PROJECT", "opspilot")
        listed = subprocess.run(
            [
                "docker",
                "ps",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--filter",
                "label=com.docker.compose.service=observer",
                "--format",
                "{{.ID}}",
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        assert listed.returncode == 0 and listed.stdout.strip(), (
            "OPSPILOT_393_DOCKER_NETWORK was set but no observer container was found"
        )
        for container in listed.stdout.splitlines():
            connected = subprocess.run(
                ["docker", "network", "connect", network, container],
                text=True,
                capture_output=True,
                check=False,
                timeout=30,
            )
            assert connected.returncode == 0, "could not connect observer to network"
    token = (secrets / "alertmanager_token").read_text().strip()
    port = env.get("OPSPILOT_WEB_HOST_PORT", "18080")
    payload = {
        "version": "4",
        "groupKey": "{alertname=OpsPilot393}",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "OpsPilot393", "instance": "fixture"},
                "annotations": {"summary": "#393 installed acceptance"},
                "startsAt": "2026-10-31T00:00:00Z",
                "endsAt": "0001-01-01T00:00:00Z",
                "fingerprint": "opspilot-393-live",
                "generatorURL": "http://alertmanager.invalid/393",
            }
        ],
    }
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/intake/alertmanager",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        assert response.status == 200
        result = json.loads(response.read())
    alert_result = result["results"][0]
    incident_id = alert_result.get("incident_id")
    run_id = alert_result.get("run_id")
    assert incident_id and run_id, alert_result
    ui_password = (secrets / "ui_password").read_text().strip()
    credentials = base64.b64encode(f"operator:{ui_password}".encode()).decode()
    page_request = urllib.request.Request(
        f"http://127.0.0.1:{port}/incidents/{incident_id}",
        headers={"Authorization": f"Basic {credentials}"},
    )
    deadline = time.monotonic() + 900
    page = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(page_request, timeout=30) as response:
                if response.status == 200:
                    page = response.read().decode()
        except (urllib.error.HTTPError, urllib.error.URLError):
            pass
        if (
            'id="run-state"' in page
            and 'id="report-status"' in page
            and not any(
                f'id="run-state">{state}<' in page for state in ACTIVE_RUN_STATES
            )
        ):
            break
        time.sleep(5)
    assert 'id="run-state"' in page, "incident page never exposed a Run state"
    assert f"<code>{run_id}</code>" in page, "workbench showed a different Run"
    assert not any(
        f'id="run-state">{state}<' in page for state in ACTIVE_RUN_STATES
    ), "Run did not reach a terminal state"
    assert 'id="report-status"' in page, "investigation report was not visible"
