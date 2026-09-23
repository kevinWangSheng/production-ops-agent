"""Execute ``scripts/m1_live_flash_loop.py`` as ``__main__`` with no provider.

The replay tests import the module, so a name that is only bound *after* the
``if __name__ == "__main__"`` guard is invisible to them but breaks the real
command (``NameError``, PR #32 review). This test runs the file the way the
operator does; the provider client is stubbed, so no request and no spend.
"""

import json
import runpy
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
    monkeypatch.setenv("M0_ENV_FILE", str(env_file))
    monkeypatch.setenv("M1_ACCEPTANCE_OUT", str(out_root))
    monkeypatch.setattr(client_module, "DeepSeekClient", RefusingClient)

    with pytest.raises(SystemExit) as raised:
        runpy.run_path(str(SCRIPT), run_name="__main__")

    # The stubbed provider fails the Run; the script must still reach the end
    # of main() and write the ledger for that failed Run.
    assert raised.value.code == 1
    (ledger_path,) = out_root.glob("*/ledger.json")
    ledger = json.loads(ledger_path.read_text())
    assert ledger["execution"] == "failed"
    assert ledger["http_count"] >= 1
    assert (ledger_path.parent / "acceptance-outcome.json").exists()
