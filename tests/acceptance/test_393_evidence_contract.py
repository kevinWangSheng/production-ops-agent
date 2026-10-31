"""Evidence shape contract for the #393 installed investigation."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
EVIDENCE = REPO / "docs/evidence/f9-393-installed-investigation/summary.json"
TERMINAL_EXCLUSIONS = {"queued", "pending", "running", "in_progress"}


def _walk(value: Any) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str):
                found.append((str(key), item))
            found.extend(_walk(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_walk(item))
    return found


@pytest.mark.skipif(
    not EVIDENCE.is_file(), reason="installed #393 evidence is not present yet"
)
def test_installed_evidence_summary_has_the_contractual_fields_and_no_secrets() -> None:
    summary = json.loads(EVIDENCE.read_text())
    assert isinstance(summary, dict)
    assert summary.get("profile") == "fixture"
    assert summary.get("image_id") and summary.get("version")
    assert summary.get("incident_id") and summary.get("run_id")
    assert summary.get("run_final_state")
    assert str(summary["run_final_state"]).lower() not in TERMINAL_EXCLUSIONS
    assert summary.get("report_visible_in_workbench") is True
    assert summary.get("observer_prometheus") in {"kind-proxy", "other"}
    services = summary.get("services")
    assert isinstance(services, dict)
    for name in ("web", "worker", "observer"):
        assert services.get(name) == {"started": True, "verify_head": True}
    assert summary.get("recorded_at")

    serialized = json.dumps(summary, ensure_ascii=False)
    assert not re.search(
        r"(?:DEEPSEEK_API_KEY|PASSWORD|TOKEN|SECRET)\s*[:=]", serialized, re.I
    )
    for key, value in _walk(summary):
        assert not re.search(r"(?:password|token|secret|api[_-]?key)", key, re.I)
        assert len(value) < 4096
