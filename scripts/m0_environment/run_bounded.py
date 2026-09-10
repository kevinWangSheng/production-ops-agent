"""Trusted M0 subprocess-group supervisor; never reads model payloads."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.m0_environment.round02 import PROFILE  # noqa: E402


def supervise(command, timeout=1800, grace=2):
    child = subprocess.Popen(command, start_new_session=True)
    try:
        return child.wait(timeout=max(0.01, timeout - 2 * grace))
    except BaseException as original:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
        # A exited leader does not imply its worker children exited.
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait(timeout=grace)
        if isinstance(original, subprocess.TimeoutExpired):
            return 124
        raise
    finally:
        # Also clean descendants after normal leader exit; worker never setsid.
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", type=Path, required=True)
    args, rest = parser.parse_known_args()
    if not rest:
        parser.error("Holmes wrapper arguments required")
    timeout = min(PROFILE.run_seconds, PROFILE.deadline - time.time())
    if timeout <= 4:
        raise SystemExit("allocation deadline reached")
    raise SystemExit(
        supervise(
            [
                str(args.python),
                str(ROOT / "scripts/m0_environment/holmes_baseline.py"),
                *rest,
            ],
            timeout,
        )
    )


if __name__ == "__main__":
    main()
