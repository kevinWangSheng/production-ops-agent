"""Independent acceptance tests for issue #429's Compose and upgrade contract."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "deploy" / "compose" / "compose.yaml"
UPGRADE = ROOT / "deploy" / "compose" / "UPGRADE.md"


def _yaml(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text())
    assert isinstance(document, dict)
    return document


def test_worker_and_observer_have_forty_second_grace_period() -> None:
    services = _yaml(COMPOSE)["services"]
    assert services["worker"]["stop_grace_period"] == "40s"
    assert services["observer"]["stop_grace_period"] == "40s"


def test_only_worker_and_observer_set_a_stop_grace_period() -> None:
    # #429's scope in Compose is exactly the two grace-period keys. The
    # original check diffed against origin/main, which only held while #429
    # was unmerged; once merged it pinned compose.yaml for every later PR.
    services = _yaml(COMPOSE)["services"]
    with_grace = {
        name for name, spec in services.items() if "stop_grace_period" in spec
    }
    assert with_grace == {"worker", "observer"}


def test_upgrade_document_describes_rolling_back_in_required_order() -> None:
    text = UPGRADE.read_text()
    lowered = text.lower()
    assert "no down-migrations" not in lowered
    assert "older image" in lowered
    assert "rolling back" in lowered
    assert "refused" in lowered and "fix forward" in lowered and "backup" in lowered

    marker = "## Rolling back"
    assert marker in text
    section = text.split(marker, 1)[1]
    section = section.split("\n## ", 1)[0]
    lowered_section = section.lower()
    steps = re.findall(r"^\d+\.\s+([A-Za-z]+)\b", section, re.MULTILINE)
    assert steps == ["Stop", "Downgrade", "Upgrade"]
    assert "deploy/compose/install.sh compose stop web worker observer" in section
    assert "new release's image" in lowered_section
    assert "deploy/compose/install.sh compose run --rm --no-deps migrate" in section
    assert "python -m opspilot.schema downgrade --to <old head>" in section
    assert "old release's checkout" in section
    assert "deploy/compose/install.sh upgrade" in section
