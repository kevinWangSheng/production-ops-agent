"""Version-source and release-version checker contracts for issue #394."""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]
CHECK_VERSION = REPO / "scripts" / "release" / "check_version.py"


def _load_checker() -> ModuleType:
    assert CHECK_VERSION.is_file(), (
        f"missing release checker: {CHECK_VERSION.relative_to(REPO)}"
    )
    spec = importlib.util.spec_from_file_location(
        "opspilot_check_version_394", CHECK_VERSION
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_repo(
    root: Path,
    *,
    version: str,
    runtime: str | None = None,
    lock: str | None = None,
) -> None:
    (root / "opspilot").mkdir(parents=True)
    for relative in ("pyproject.toml", "uv.lock", "opspilot/__init__.py"):
        shutil.copy2(REPO / relative, root / relative)

    pyproject = (root / "pyproject.toml").read_text()
    pyproject, count = re.subn(
        r"(?m)^version\s*=\s*(['\"])[^'\"]+\1",
        f'version = "{version}"',
        pyproject,
        count=1,
    )
    assert count == 1, "the copied pyproject must have one project version"
    (root / "pyproject.toml").write_text(pyproject)

    lock_version = version if lock is None else lock
    blocks = (root / "uv.lock").read_text().split("[[package]]")
    rewritten = 0
    for index, block in enumerate(blocks):
        if re.search(r'(?m)^name\s*=\s*"opspilot"\s*$', block):
            blocks[index], count = re.subn(
                r'(?m)^version\s*=\s*"[^"]*"',
                f'version = "{lock_version}"',
                block,
                count=1,
            )
            rewritten += count
    assert rewritten == 1, "the copied lockfile must have one opspilot package version"
    (root / "uv.lock").write_text("[[package]]".join(blocks))

    init = (root / "opspilot" / "__init__.py").read_text()
    runtime_version = version if runtime is None else runtime
    init, count = re.subn(
        r"(?m)^__version__\s*=\s*['\"][^'\"]+['\"]\s*$",
        f"__version__ = {runtime_version!r}",
        init,
        count=1,
    )
    if count == 0:
        init += f"\n__version__ = {runtime_version!r}\n"
    (root / "opspilot" / "__init__.py").write_text(init)


def test_pyproject_uv_lock_and_runtime_version_agree() -> None:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    version = pyproject["project"]["version"]
    assert version == "0.1.0"

    lock = tomllib.loads((REPO / "uv.lock").read_text())
    matches = [
        entry for entry in lock.get("package", []) if entry.get("name") == "opspilot"
    ]
    assert matches, "uv.lock has no opspilot package entry"
    assert {entry.get("version") for entry in matches} == {version}

    import opspilot

    assert opspilot.__version__ == version


def test_check_accepts_matching_version_and_tag(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_repo(tmp_path, version="1.2.3")
    assert checker.check(tmp_path) == []
    assert checker.check(tmp_path, "v1.2.3") == []


@pytest.mark.parametrize(
    ("kwargs", "tag"),
    [
        pytest.param({"version": "1.2.3"}, "v1.2.4", id="tag-differs"),
        pytest.param({"version": "1.2.3"}, "1.2.3", id="tag-without-v"),
        pytest.param({"version": "1.2"}, None, id="not-semver-two-parts"),
        pytest.param({"version": "1.2.3.4"}, None, id="not-semver-four-parts"),
        pytest.param(
            {"version": "1.2.3", "runtime": "1.2.4"}, None, id="runtime-differs"
        ),
        pytest.param({"version": "1.2.3", "lock": "1.2.4"}, None, id="lock-differs"),
    ],
)
def test_check_reports_tag_semver_runtime_and_lock_mismatches(
    tmp_path: Path, kwargs: dict[str, str], tag: str | None
) -> None:
    checker = _load_checker()
    _write_repo(tmp_path, **kwargs)
    problems = checker.check(tmp_path, tag)
    assert isinstance(problems, list) and problems, problems
    assert all(isinstance(problem, str) and problem.strip() for problem in problems), (
        problems
    )


def _stdlib_python() -> str:
    # "Runnable as python3 -I without the venv": the base interpreter has no
    # project dependencies, so this proves the script is stdlib-only.
    base = Path(sys.base_prefix) / "bin" / "python3"
    return str(base) if base.exists() else sys.executable


@pytest.mark.parametrize(
    "args", [[], ["--tag", "v0.1.0"]], ids=["no-tag", "matching-tag"]
)
def test_check_version_cli_exits_zero_on_the_repository(args: list[str]) -> None:
    assert CHECK_VERSION.is_file(), (
        f"missing release checker: {CHECK_VERSION.relative_to(REPO)}"
    )
    good = subprocess.run(
        [_stdlib_python(), "-I", str(CHECK_VERSION), *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert good.returncode == 0, good.stdout + good.stderr


def test_check_version_cli_exits_one_with_problems_on_stderr() -> None:
    assert CHECK_VERSION.is_file(), (
        f"missing release checker: {CHECK_VERSION.relative_to(REPO)}"
    )
    bad = subprocess.run(
        [_stdlib_python(), "-I", str(CHECK_VERSION), "--tag", "v0.1.1"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad.returncode == 1, bad.stdout + bad.stderr
    assert bad.stderr.strip()
