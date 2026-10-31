"""Opt-in installed-stack acceptance for #393; never runs in PR CI by default."""

from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INSTALL = REPO / "deploy/compose/install.sh"
EVIDENCE = REPO / "docs/evidence/f9-393-installed-investigation/summary.json"

pytestmark = pytest.mark.skipif(
    os.environ.get("OPSPILOT_393_LIVE") != "1",
    reason="set OPSPILOT_393_LIVE=1 with Docker, DeepSeek, and Prometheus to run",
)


def test_fixture_profile_real_alert_reaches_terminal_evidence() -> None:
    """The operator supplies keys/URL; the test triggers one Alertmanager alert."""
    import subprocess

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
    (secrets / "worker_env").write_text(f"DEEPSEEK_API_KEY={model_key}\n")
    (secrets / "observer_env").write_text("")
    env = os.environ.copy()
    env.update(
        {
            "OPSPILOT_START_AGENT": "1",
            "OPSPILOT_TOOL_PROFILE": "fixture",
            "OPSPILOT_SECRETS_DIR": str(secrets),
            "OPSPILOT_OBSERVER_PROMETHEUS_URL": prometheus,
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
    assert installed.returncode == 0, installed.stdout + installed.stderr
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
        assert response.status in {200, 503}
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline and not EVIDENCE.is_file():
        time.sleep(5)
    assert EVIDENCE.is_file(), f"missing {EVIDENCE.relative_to(REPO)}"
