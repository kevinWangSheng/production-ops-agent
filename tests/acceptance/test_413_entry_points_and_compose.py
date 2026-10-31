"""F7 enumeration and Compose acceptance checks for #413."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

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


def _normalized_port(entry):
    if isinstance(entry, dict):
        return entry.get("host_ip"), int(entry["published"]), int(entry["target"])
    parts = str(entry).split(":")
    if len(parts) == 3:
        host_ip, published, target = parts
        return host_ip, int(published), int(target)
    if len(parts) == 2:
        published, target = parts
        return None, int(published), int(target)
    raise AssertionError(f"unsupported Compose port syntax: {entry!r}")


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
    document = yaml.safe_load(LAB.read_text())
    services = document.get("services", {})
    expected = {
        "web": (19464, 9464),
        "worker": (19465, 9464),
        "observer": (19466, 9464),
    }

    published_services = {
        name for name, service in services.items() if service.get("ports")
    }
    assert published_services == set(expected)
    for name, (host_port, target_port) in expected.items():
        ports = services[name]["ports"]
        assert len(ports) == 1
        host_ip, published, target = _normalized_port(ports[0])
        assert host_ip == "127.0.0.1"
        assert published == host_port
        assert target == target_port

    install = INSTALL.read_text()
    settings = re.search(r"readonly SETTINGS=\((.*?)\)", install, re.S)
    assert settings
    assert set(re.findall(r"OPSPILOT_[A-Z0-9_]+", settings.group(1))) == {
        "OPSPILOT_PROJECT",
        "OPSPILOT_WEB_HOST_PORT",
        "OPSPILOT_IMAGE",
        "OPSPILOT_TOOL_PROFILE",
        "OPSPILOT_START_AGENT",
        # added by #393 (merged before #413); #413 adds no install.env key (F8-R11)
        "OPSPILOT_OBSERVER_PROMETHEUS_URL",
    }
