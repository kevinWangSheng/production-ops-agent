import subprocess
from pathlib import Path

import pytest

from scripts.check_secrets import ScanError, tracked_snapshot


def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


def stage(root, name):
    subprocess.run(["git", "add", "--", name], cwd=root, check=True)


def test_tracked_snapshot_omits_untracked_private_material(tmp_path):
    root = repo(tmp_path)
    (root / "public.py").write_text("pass\n")
    (root / ".env").write_text("synthetic-private-material")
    stage(root, "public.py")
    out = tmp_path / "out"
    out.mkdir()
    tracked_snapshot(root, out)
    assert [p.name for p in out.iterdir()] == ["public.py"]


def test_accidentally_staged_private_config_denied_before_read(tmp_path, monkeypatch):
    root = repo(tmp_path)
    (root / ".env").write_text("synthetic-only")
    stage(root, ".env")
    monkeypatch.setattr(Path, "read_bytes", lambda self: pytest.fail("must not read"))
    with pytest.raises(ScanError, match="PRIVATE_CONFIG_TRACKED"):
        tracked_snapshot(root, tmp_path / "out")


def test_tracked_symlink_cannot_escape_snapshot(tmp_path):
    root = repo(tmp_path)
    private = tmp_path / "synthetic-private"
    private.write_text("not for scanning")
    (root / "public.py").symlink_to(private)
    stage(root, "public.py")
    with pytest.raises(ScanError, match="TRACKED_SYMLINK_DENIED"):
        tracked_snapshot(root, tmp_path / "out")


def test_missing_scanner_fails_closed_with_fixed_output(tmp_path):
    result = subprocess.run(
        [
            ".venv/bin/python",
            "scripts/check_secrets.py",
            "--binary",
            str(tmp_path / "missing"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert result.stdout.strip() == "SECRET_SCAN_FAILED"
    assert result.stderr == ""
