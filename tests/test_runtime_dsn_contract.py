"""#169 r1 U3 / P3: fail closed before any PG/model connection."""

import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    ("module", "variable", "args"),
    [
        ("opspilot.web", "OPSPILOT_WEB_DSN", ["serve"]),
        ("opspilot.worker_main", "OPSPILOT_WORKER_DSN", []),
    ],
)
@pytest.mark.parametrize("value", [None, ""], ids=["missing", "empty"])
@pytest.mark.parametrize("legacy", [False, True], ids=["no-owner-dsn", "owner-dsn-set"])
def test_runtime_dsn_required_before_connections(module, variable, args, value, legacy):
    env = dict(os.environ)
    for name in ("OPSPILOT_DSN", "OPSPILOT_WEB_DSN", "OPSPILOT_WORKER_DSN"):
        env.pop(name, None)
    if value is not None:
        env[variable] = value
    if legacy:
        env["OPSPILOT_DSN"] = "owner-dsn-must-never-be-used"
    # The subprocess must fail before any long-running server/model call.
    entry = (
        "from opspilot.web.__main__ import main; raise SystemExit(main(['opspilot.web', 'serve']))"
        if args
        else "from opspilot.worker_main import main; raise SystemExit(main([]))"
    )
    source = "exec(" + repr(entry) + ")"
    result = subprocess.run(
        [sys.executable, "-c", source],
        env=env,
        capture_output=True,
        text=True,
        timeout=3,
    )
    assert result.returncode != 0
    assert variable in result.stderr, result.stderr
