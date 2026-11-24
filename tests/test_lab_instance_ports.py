"""Engineer lab scripts follow OPSPILOT_LAB_INSTANCE (#483).

The ports are fixed at import time, so each instance is read in a fresh
interpreter. Instance 0 must keep today's 19090/16686/18080.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

PROBE = """
import json
from scripts import otel_demo_lab, otel_demo_observe
print(json.dumps([
    otel_demo_lab.PROMETHEUS,
    otel_demo_lab.JAEGER,
    otel_demo_lab.FRONTEND,
    otel_demo_observe.PROM,
    otel_demo_observe.JAEGER,
]))
"""


def _urls(instance: int) -> list[str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("OPSPILOT_OTEL_PROMETHEUS_URL", "OPSPILOT_OTEL_JAEGER_URL")
    }
    env["OPSPILOT_LAB_INSTANCE"] = str(instance)
    out = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


@pytest.mark.parametrize(
    ("instance", "prom", "jaeger", "frontend"),
    [(0, 19090, 16686, 18080), (2, 19290, 16886, 18280)],
)
def test_lab_scripts_follow_instance(
    instance: int, prom: int, jaeger: int, frontend: int
) -> None:
    jaeger_ui = f"http://127.0.0.1:{jaeger}/jaeger/ui"
    assert _urls(instance) == [
        f"http://127.0.0.1:{prom}",
        jaeger_ui,
        f"http://127.0.0.1:{frontend}/",
        f"http://127.0.0.1:{prom}",
        jaeger_ui,
    ]


def test_no_hardcoded_lab_ports_left() -> None:
    for name in ("otel_demo_lab", "otel_demo_observe", "r358_live", "f11_pr5c_lab"):
        text = (ROOT / "scripts" / f"{name}.py").read_text()
        for port in ("19090", "16686", "18080"):
            assert port not in text, (name, port)
