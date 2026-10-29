"""#399 live gate: the isolation probe against the real container in the kind
lab (colima m1-kind). Opt-in with ``OPSPILOT_HOLMES_ISOLATION_LIVE=1``, run
inside the kind-lab lock; never in PR CI. Rewrites isolation-probe.json."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scripts.holmes_baseline import isolation

pytestmark = pytest.mark.skipif(
    os.environ.get("OPSPILOT_HOLMES_ISOLATION_LIVE") != "1",
    reason="needs colima m1-kind and the kind lab (OPSPILOT_HOLMES_ISOLATION_LIVE=1)",
)


def test_live_probe_passes_every_check(tmp_path: Path) -> None:
    code = isolation.main(["probe", "--out", str(tmp_path)])
    evidence = json.loads((isolation.EVIDENCE / "isolation-probe.json").read_text())
    assert evidence["observed"]["probe"]["valid"] is True
    assert [c["id"] for c in evidence["result"]["checks"]] == list(isolation.CHECK_IDS)
    assert evidence["result"]["passed"] is True, evidence["result"]
    assert code == 0
