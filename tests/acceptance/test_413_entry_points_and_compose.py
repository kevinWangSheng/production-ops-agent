"""F7 enumeration and Compose acceptance checks for #413."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from opspilot.web import create_app
from tests.acceptance.test_f7_step1_entry_points import (
    EXPECTED_ROUTES,
    PROCESS_ENTRIES,
    _auth,
    _routes,
    _Untouchable,
)

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "deploy/compose/compose.yaml"
LAB = REPO / "deploy/compose/compose.lab.yaml"
INSTALL = REPO / "deploy/compose/install.sh"


def test_f7_classifies_health_as_a_process_entry_and_keeps_web_routes_unchanged():
    assert "opspilot.health" in PROCESS_ENTRIES
    assert (REPO / "opspilot/health.py").is_file()
    app = create_app(_Untouchable("workbench"), _auth(), _Untouchable("clock"))
    assert _routes(app) == set(EXPECTED_ROUTES)


def test_health_module_entry_surface_is_get_only_and_has_three_paths():
    tree = ast.parse((REPO / "opspilot/health.py").read_text())
    route_literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value in {"/healthz", "/readyz", "/metrics"}
    }
    assert route_literals == {"/healthz", "/readyz", "/metrics"}


def test_default_compose_publishes_only_web_and_all_runtime_healthchecks_are_loopback_readyz():
    text = COMPOSE.read_text()
    assert "ports:" in text
    assert text.count("published:") == 1
    assert "target: 8080" in text
    for service in ("web:", "worker:", "observer:"):
        start = text.index(f"\n  {service}")
        boundary = re.search(r"\n  [a-z][a-z0-9-]*:", text[start + 3 :])
        end = start + 3 + boundary.start() if boundary else len(text)
        block = text[start : end if end != -1 else None]
        assert "healthcheck:" in block
        assert "127.0.0.1" in block and "/readyz" in block


def test_lab_override_publishes_only_loopback_health_ports_and_install_keys_are_unchanged():
    assert LAB.is_file()
    text = LAB.read_text()
    published = set(re.findall(r"published:\s*['\"]?(\d+)", text))
    published.update(re.findall(r"127\.0\.0\.1:(1946[456])", text))
    assert published == {"19464", "19465", "19466"}
    assert all(re.search(rf"127\.0\.0\.1.*{port}|{port}.*127\.0\.0\.1", text) for port in ("19464", "19465", "19466"))
    install = INSTALL.read_text()
    settings = re.search(r"readonly SETTINGS=\((.*?)\)", install, re.S)
    assert settings
    assert set(re.findall(r"OPSPILOT_[A-Z0-9_]+", settings.group(1))) == {
        "OPSPILOT_PROJECT",
        "OPSPILOT_WEB_HOST_PORT",
        "OPSPILOT_IMAGE",
        "OPSPILOT_TOOL_PROFILE",
        "OPSPILOT_START_AGENT",
    }

