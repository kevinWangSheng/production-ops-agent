"""M0 rehearsal and explicitly approved one-shot live experiment."""

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
from pathlib import Path

from .config import ConfigError, load_config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("offline", "check-config", "live"))
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--env-file", type=Path)
    source.add_argument("--process-env", action="store_true")
    parser.add_argument("--approval-file", type=Path)
    args = parser.parse_args(argv)
    # No SDK import, config read, or network activity on the denied live path.
    if (
        args.mode == "live"
        and args.approval_file is not None
        and args.env_file is not None
    ):
        from .live import run_cli

        return run_cli(args.env_file, args.approval_file)
    if args.mode == "live":
        print(
            json.dumps(
                {"status": "denied", "reason": "LIVE_NOT_ENABLED", "network_calls": 0}
            )
        )
        return 3
    if args.approval_file is not None:
        print('{"status":"denied","reason":"APPROVAL_ONLY_FOR_LIVE"}')
        return 2
    if args.mode == "check-config":
        if args.env_file is None and not args.process_env:
            print('{"status":"denied","reason":"CONFIG_SOURCE_REQUIRED"}')
            return 2
        try:
            result = load_config(args.env_file).readiness()
        except ConfigError as exc:
            print(json.dumps({"status": "denied", "reason": str(exc)}))
            return 2
        print(json.dumps(result, sort_keys=True))
        return 2  # Prerequisites/authorization are not complete, even if fields are filled.
    if args.env_file is not None or args.process_env:
        print('{"status":"denied","reason":"OFFLINE_USES_SYNTHETIC_CONFIG_ONLY"}')
        return 2
    # Do not import SDKs with ambient service configuration. Offline child process only.
    for key in list(os.environ):
        if key.startswith(
            ("LANGSMITH_", "LANGCHAIN_", "OPENAI_", "DEEPSEEK_", "OTEL_")
        ):
            del os.environ[key]
    from .protocol import ROOT, no_network, rehearse

    try:
        with no_network():
            result = asyncio.run(rehearse())
        paths = [
            ROOT / "pyproject.toml",
            ROOT / "uv.lock",
            *sorted((ROOT / "scripts/m0").glob("*.py")),
            ROOT / "tests/fixtures/m0/protocol-v1.json",
            ROOT / "tests/test_m0.py",
        ]
        result = {
            "status": "offline_pass",
            "evidence_kind": "synthetic_sdk_wire",
            "external_calls": 0,
            "cost_cny": 0,
            "live_verified": False,
            "summary": result,
            "python": platform.python_version(),
            "platform": platform.system(),
            "architecture": platform.machine(),
            "versions": {
                k: importlib.metadata.version(k)
                for k in ("openai", "langsmith", "httpx2")
            },
            "sha256": {
                str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in paths
            },
        }
        result["base_commit"] = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.strip()
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except Exception:
        # SDK errors may contain full requests/responses: never render them here.
        print(
            '{"status":"failed","reason":"OFFLINE_REHEARSAL_FAILED","external_calls":0}'
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
