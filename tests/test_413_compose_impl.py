"""#413 implementer tests: Compose healthchecks hit each runtime's /readyz,
and only the lab override publishes the health ports (F8-R7, F8-R11)."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_DIR = ROOT / "deploy" / "compose"
DEFAULT = yaml.safe_load((COMPOSE_DIR / "compose.yaml").read_text())["services"]
LAB = yaml.safe_load((COMPOSE_DIR / "compose.lab.yaml").read_text())["services"]
READYZ = "http://127.0.0.1:9464/readyz"


def test_every_runtime_healthcheck_requests_its_own_readyz():
    for name in ("web", "worker", "observer"):
        test = DEFAULT[name]["healthcheck"]["test"]
        assert test[:3] == ["CMD", "python", "-c"], name
        assert READYZ in test[3], name


def test_the_web_healthcheck_still_requires_the_app_port():
    assert "('127.0.0.1', 8080)" in DEFAULT["web"]["healthcheck"]["test"][3]


def test_the_default_file_publishes_only_the_workbench():
    published = {
        name: [entry["target"] for entry in service.get("ports", ())]
        for name, service in DEFAULT.items()
    }
    assert {name: ports for name, ports in published.items() if ports} == {
        "web": [8080]
    }


def test_the_lab_override_publishes_the_health_ports_on_loopback_only():
    assert LAB == {
        name: {
            "ports": [
                {
                    "target": 9464,
                    "host_ip": "127.0.0.1",
                    "published": port,
                    "protocol": "tcp",
                }
            ]
        }
        for name, port in (("web", "19464"), ("worker", "19465"), ("observer", "19466"))
    }


def test_the_installer_never_uses_the_lab_override():
    assert "compose.lab.yaml" not in (COMPOSE_DIR / "install.sh").read_text()


def test_the_image_binds_the_listener_on_the_container_network():
    dockerfile = (ROOT / "Dockerfile").read_text()
    runtime = dockerfile[dockerfile.index("AS runtime") :]
    assert "OPSPILOT_HEALTH_BIND=0.0.0.0:9464" in runtime
    assert "ENV OPSPILOT_REVISION=${OPSPILOT_REVISION}" in runtime
