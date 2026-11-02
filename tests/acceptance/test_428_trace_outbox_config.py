"""Static acceptance checks for #428 install and entrypoint wiring."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "deploy/compose/install.sh"
COMPOSE = ROOT / "deploy/compose/compose.yaml"
ENTRYPOINT = ROOT / "deploy/docker-entrypoint.sh"


def test_install_env_declares_trace_with_off_default_and_closed_allowlist():
    text = INSTALL.read_text()
    assert "OPSPILOT_TRACE" in text
    assert re.search(r"OPSPILOT_TRACE[^\n]*off", text)
    assert re.search(r"off\|lab\|outbox", text)


def test_compose_passes_trace_only_to_worker_and_defaults_off():
    services = yaml.safe_load(COMPOSE.read_text())["services"]
    worker_env = services["worker"].get("environment", {})
    assert str(worker_env.get("OPSPILOT_TRACE", "")).find("off") >= 0
    for name in ("web", "observer"):
        env = services[name].get("environment", {})
        assert "OPSPILOT_TRACE" not in env


def test_entrypoint_allowlist_exposes_project_and_endpoint_to_worker():
    text = ENTRYPOINT.read_text()
    assert "LANGSMITH_PROJECT" in text
    assert "LANGSMITH_ENDPOINT" in text
    # The two public routing values are allowlisted; the API key remains file-only.
    assert re.search(
        r"LANGSMITH_PROJECT.*LANGSMITH_ENDPOINT|LANGSMITH_ENDPOINT.*LANGSMITH_PROJECT",
        text,
        re.S,
    )
