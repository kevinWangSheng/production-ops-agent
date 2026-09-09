"""Pinned Gitleaks check: Git history and tracked files, never private .env files."""

import argparse
import json
import os
import secrets
import stat
import subprocess
import tempfile
from pathlib import Path

VERSION = "8.30.1"


class ScanError(Exception):
    pass


def command(args, *, cwd, timeout=60):
    # CLI subprocesses do not inherit service credentials or scanner override config.
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "TMPDIR")}
    return subprocess.run(
        args, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout
    )


def tracked_snapshot(root: Path, destination: Path):
    listing = command(["git", "ls-files", "-z"], cwd=root)
    if listing.returncode:
        raise ScanError("GIT_LIST_FAILED")
    count = 0
    for name in listing.stdout.split("\0"):
        if not name:
            continue
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ScanError("TRACKED_PATH_INVALID")
        # No copying/reading a real .env, even if accidentally staged.
        if any(
            part == ".env" or part.startswith(".env.") and part != ".env.example"
            for part in relative.parts
        ):
            raise ScanError("PRIVATE_CONFIG_TRACKED")
        source = root / relative
        if not source.exists() and not source.is_symlink():
            continue  # Tracked deletion; history scanner still covers its old content.
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ScanError("TRACKED_SYMLINK_DENIED")
        if not stat.S_ISREG(source.stat().st_mode):
            raise ScanError("TRACKED_FILE_INVALID")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        count += 1
    if count == 0:
        raise ScanError("EMPTY_SCAN")


def scan(binary: Path, mode: str, target: Path, scratch: Path):
    report = scratch / "report.json"
    report.unlink(missing_ok=True)
    config = scratch / "scanner.toml"
    # Two reviewed non-credential source digests in one historical evidence manifest.
    # Rule + exact manifest suffix + exact value all must match; no directory exclusion.
    config.write_text(
        """[extend]
useDefault = true
[[rules]]
id = "generic-api-key"
[[rules.allowlists]]
description = "Reviewed source SHA256 values in the M0 integration manifest"
condition = "AND"
paths = ['(^|/)docs/evidence/m0-integration/verification\\.json$']
regexTarget = "secret"
regexes = [
  '^b39dadc086f7d83346d9b5ff6ece87b956365ec9fde8c33912772bd0ec128ac1$',
  '^b3497adb93bd314032e83d974893e9d62adea82c24ad534dd223883cd794b67f$'
]
"""
    )
    args = [
        str(binary),
        mode,
        "--no-banner",
        "--redact=100",
        "--ignore-gitleaks-allow",
        f"--config={config}",
        f"--gitleaks-ignore-path={scratch / 'no-ignore-file'}",
        "--report-format=json",
        f"--report-path={report}",
    ]
    if mode == "git":
        args.append("--log-opts=--all --no-ext-diff --no-textconv")
    result = command([*args, str(target)], cwd=scratch)
    if result.returncode not in (0, 1) or not report.is_file():
        raise ScanError("SCANNER_FAILED")
    findings = json.loads(report.read_text())
    if not isinstance(findings, list):
        raise ScanError("SCANNER_FAILED")
    if bool(findings) != (result.returncode == 1):
        raise ScanError("SCANNER_FAILED")
    return len(findings)


def check(root: Path, binary: Path):
    binary = binary.resolve(strict=True)
    root = root.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="m0-secret-scan-") as tmp:
        scratch = Path(tmp)
        version = command([str(binary), "version"], cwd=scratch)
        if version.returncode or version.stdout.strip() != VERSION:
            raise ScanError("SCANNER_VERSION_INVALID")
        probe = scratch / "probe"
        probe.mkdir()
        # Invalid locally generated canary, never a credential or model input.
        (probe / "canary.txt").write_text('api_key = "' + secrets.token_hex(24) + '"\n')
        if scan(binary, "dir", probe, scratch) == 0:
            raise ScanError("SCANNER_SELFTEST_FAILED")
        (probe / "canary.txt").write_text("public non-secret fixture\n")
        if scan(binary, "dir", probe, scratch):
            raise ScanError("SCANNER_SELFTEST_FAILED")
        precise = probe / "docs/evidence/m0-integration/verification.json"
        precise.parent.mkdir(parents=True)
        known_digest = (
            "b39dadc086f7d83346d9b5ff6ece87b956365ec9fde8c33912772bd0ec128ac1"
        )
        precise.write_text(json.dumps({"scripts/check_secrets.py": known_digest}))
        if scan(binary, "dir", probe, scratch):
            raise ScanError("SCANNER_SELFTEST_FAILED")
        precise.write_text('api_key = "' + secrets.token_hex(24) + '"\n')
        if scan(binary, "dir", probe, scratch) == 0:
            raise ScanError("SCANNER_SELFTEST_FAILED")
        precise.unlink()
        (probe / "canary.txt").write_text('api_key = "' + known_digest + '"\n')
        if scan(binary, "dir", probe, scratch) == 0:
            raise ScanError("SCANNER_SELFTEST_FAILED")
        snapshot = scratch / "tracked"
        snapshot.mkdir()
        tracked_snapshot(root, snapshot)
        # Avoid repository-owned suppressions: no config supplied; run outside repo.
        if scan(binary, "dir", snapshot, scratch) or scan(binary, "git", root, scratch):
            raise ScanError("SECRET_DETECTED")
    return "SECRET_SCAN_PASSED"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--binary", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(check(args.root, args.binary))
    except ScanError as error:
        print(str(error))
        raise SystemExit(1) from None
    except Exception:
        print("SECRET_SCAN_FAILED")
        raise SystemExit(2) from None
