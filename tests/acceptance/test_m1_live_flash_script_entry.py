"""Execute ``scripts/m1_live_flash_loop.py`` as ``__main__`` with no provider.

The replay tests import the module, so a name that is only bound *after* the
``if __name__ == "__main__"`` guard is invisible to them but breaks the real
command (``NameError``, PR #32 review). This test runs the file the way the
operator does; the provider client is stubbed, so no request and no spend.
"""

import hashlib
import json
import runpy
import sys
from pathlib import Path

import pytest

import opspilot.investigation.client as client_module
from opspilot.investigation.loop import ModelError

SCRIPT = Path(__file__).parents[2] / "scripts/m1_live_flash_loop.py"


class RefusingClient:
    """Accepts the live constructor shape and rejects every call."""

    def __init__(self, api_key, *, clock=None):
        assert api_key == "not-a-real-key"

    def complete(self, call):
        raise ModelError("MODEL_REJECTED")


def test_script_runs_as_main_past_build_run_without_a_provider(tmp_path, monkeypatch):
    env_file = tmp_path / "env"
    env_file.write_text("DEEPSEEK_API_KEY=not-a-real-key\n")
    out_root = tmp_path / "out"
    ledger_root = tmp_path / "ledgers"
    monkeypatch.setenv("M0_ENV_FILE", str(env_file))
    monkeypatch.setenv("M1_ACCEPTANCE_OUT", str(out_root))
    monkeypatch.setenv("OPSPILOT_LEDGER_DIR", str(ledger_root))
    monkeypatch.delenv("OPSPILOT_TRACE", raising=False)
    monkeypatch.setattr(client_module, "DeepSeekClient", RefusingClient)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT)])

    with pytest.raises(SystemExit) as raised:
        runpy.run_path(str(SCRIPT), run_name="__main__")

    # The stubbed provider fails the Run; the script must still reach the end
    # of main() and freeze the evidence for that failed Run (ADR-0006: only
    # summary.json in the evidence tree, the raw ledger outside it, hashed).
    assert raised.value.code == 1
    (summary_path,) = out_root.glob("*/summary.json")
    assert [p.name for p in summary_path.parent.iterdir()] == ["summary.json"]
    summary = json.loads(summary_path.read_text())
    assert summary["verdicts"]["status"] == "failed"
    assert summary["counts"]["http_count"] >= 1
    assert summary["verdicts"]["acceptance_outcome"]["final_state"]
    assert summary["trace"]["mode"] == "off"
    (ledger_path,) = ledger_root.glob("*/*/ledger.json")
    assert (
        summary["raw_ledger"]["sha256"]
        == hashlib.sha256(ledger_path.read_bytes()).hexdigest()
    )
    assert json.loads(ledger_path.read_text())["execution"] == "failed"
