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


@pytest.mark.parametrize("working", ["clean", "deleted"])
def test_index_snapshot_preserves_staged_bytes_after_worktree_change(tmp_path, working):
    root = repo(tmp_path)
    source = root / "config.txt"
    staged = b"synthetic staged credential candidate\x00\xff\n"
    source.write_bytes(staged)
    stage(root, source.name)
    if working == "deleted":
        source.unlink()
    else:
        source.write_text("clean working copy\n")
    out = tmp_path / "index"
    tracked_snapshot(root, out)
    assert (out / source.name).read_bytes() == staged


def test_worktree_snapshot_still_includes_unstaged_changes(tmp_path):
    root = repo(tmp_path)
    source = root / "config.txt"
    source.write_text("clean staged copy\n")
    stage(root, source.name)
    source.write_text("synthetic unstaged credential candidate\n")
    out = tmp_path / "working"
    tracked_snapshot(root, out, index=False)
    assert (out / source.name).read_bytes() == source.read_bytes()


def test_private_index_entry_is_rejected_before_any_blob_read(tmp_path, monkeypatch):
    import scripts.check_secrets as scanner

    root = repo(tmp_path)
    (root / ".env").write_text("synthetic only")
    stage(root, ".env")
    original = scanner.command

    def guarded(args, **kwargs):
        assert "cat-file" not in args
        return original(args, **kwargs)

    monkeypatch.setattr(scanner, "command", guarded)
    with pytest.raises(ScanError, match="PRIVATE_CONFIG_TRACKED"):
        tracked_snapshot(root, tmp_path / "out")


def test_range_scan_uses_ci_rules_on_only_that_range(tmp_path, monkeypatch):
    import scripts.check_secrets as scanner

    calls = []

    def fake(args, *, cwd, timeout=60, text=True):
        calls.append(list(args))
        if args[-1] == "version":
            return subprocess.CompletedProcess(args, 0, scanner.VERSION + "\n", "")
        report = next(a for a in args if a.startswith("--report-path=")).split("=", 1)[
            1
        ]
        config = next(a for a in args if a.startswith("--config=")).split("=", 1)[1]
        calls.append(Path(config).read_text())
        Path(report).write_text("[]")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(scanner, "command", fake)
    binary = tmp_path / "gitleaks"
    binary.write_text("")
    result = scanner.check_range(tmp_path, binary, "origin/main..HEAD")
    assert result == "BRANCH_SECRET_SCAN_PASSED"
    scan_args = calls[1]
    assert scan_args[1] == "git"
    assert "--log-opts=origin/main..HEAD --no-ext-diff --no-textconv" in scan_args
    assert "tests/test_311_source_errors_impl" in calls[2]  # CI exceptions travel along


def test_range_scan_rejects_option_shaped_range(tmp_path):
    import scripts.check_secrets as scanner

    binary = tmp_path / "gitleaks"
    binary.write_text("")
    with pytest.raises(ScanError, match="RANGE_INVALID"):
        scanner.check_range(tmp_path, binary, "--all")
