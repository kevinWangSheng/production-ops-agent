"""#394 single version source: pyproject ``version`` is the source; the
package, uv.lock and a release tag must agree with it
(scripts/release/check_version.py)."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import opspilot

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release" / "check_version.py"


def _load_check():
    spec = importlib.util.spec_from_file_location("check_version", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.check


def _pyproject_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A copy of the three files the check reads."""
    (tmp_path / "opspilot").mkdir()
    shutil.copy(ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    shutil.copy(ROOT / "uv.lock", tmp_path / "uv.lock")
    shutil.copy(
        ROOT / "opspilot" / "__init__.py", tmp_path / "opspilot" / "__init__.py"
    )
    return tmp_path


def _replace(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new, 1))


def test_runtime_version_is_the_pyproject_version() -> None:
    assert _pyproject_version() == "0.1.0"
    assert opspilot.__version__ == _pyproject_version()


def test_repository_passes_the_check_with_and_without_its_tag() -> None:
    check = _load_check()
    assert check(ROOT) == []
    assert check(ROOT, tag="v0.1.0") == []


@pytest.mark.parametrize("tag", ["0.1.0", "v0.1.1", "v0.1.0-rc1", "v0.1", ""])
def test_tag_must_be_v_plus_version(tag: str) -> None:
    problems = _load_check()(ROOT, tag=tag)
    assert len(problems) == 1 and "tag" in problems[0], problems


def test_package_version_mismatch_is_reported(repo: Path) -> None:
    _replace(
        repo / "opspilot" / "__init__.py",
        '__version__ = "0.1.0"',
        '__version__ = "0.2.0"',
    )
    problems = _load_check()(repo)
    assert len(problems) == 1 and "__version__" in problems[0], problems


def test_missing_package_version_is_reported(repo: Path) -> None:
    _replace(repo / "opspilot" / "__init__.py", '__version__ = "0.1.0"', "")
    problems = _load_check()(repo)
    assert len(problems) == 1 and "__version__" in problems[0], problems


def test_lock_version_mismatch_is_reported(repo: Path) -> None:
    _replace(
        repo / "uv.lock",
        'name = "opspilot"\nversion = "0.1.0"',
        'name = "opspilot"\nversion = "0.0.0"',
    )
    problems = _load_check()(repo)
    assert len(problems) == 1 and "uv.lock" in problems[0], problems


@pytest.mark.parametrize(
    "version", ["0.1", "v0.1.0", "0.1.0rc1", "01.1.0", "0.1.0-rc.1"]
)
def test_pyproject_version_must_be_plain_semver(repo: Path, version: str) -> None:
    _replace(repo / "pyproject.toml", 'version = "0.1.0"', f'version = "{version}"')
    problems = _load_check()(repo)
    assert any("SemVer" in p for p in problems), problems


def test_cli_runs_without_the_venv_and_reports_on_stderr() -> None:
    ok = subprocess.run(
        ["python3", "-I", str(SCRIPT), "--tag", "v0.1.0"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert ok.returncode == 0, ok.stderr
    bad = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--tag", "v9.9.9"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad.returncode == 1
    assert "v9.9.9" in bad.stderr and bad.stdout == ""
