"""在隔离工作区测试诊断结果，绝不操作真实 daemon 或安装依赖。"""

import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "doctor", Path(__file__).resolve().parents[1] / "scripts/doctor.py"
)
doctor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(doctor)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / "uv.lock").write_text("")
    (tmp_path / ".venv/bin").mkdir(parents=True)
    (tmp_path / ".venv/bin/ruff").touch()
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "/tool/uv")
    return tmp_path


def responses(root, version=(3, 12, 0), missing=False, lab_ok=True):
    def run(argv, cwd):
        assert cwd == root
        if argv[0] == "git":
            return True, str(root)
        if argv[0] == "/tool/uv":
            return True, "uv test-version"
        if argv[0] == str(root / ".venv/bin/python"):
            if missing:
                return False, ""
            return True, json.dumps(
                {
                    "version": version,
                    "prefix": str(root / ".venv"),
                    "base_prefix": "/base",
                    "tools": {"pytest": "test-version", "ruff": "test-version"},
                }
            )
        assert argv in [
            ["docker", "--version"],
            ["docker", "compose", "version"],
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            ["kubectl", "version", "--client=true"],
            ["helm", "version", "--short"],
        ]
        return lab_ok, ""

    return run


@pytest.mark.parametrize("missing,version", [(True, (3, 12, 0)), (False, (3, 14, 0))])
def test_missing_or_wrong_python_blocks(workspace, monkeypatch, missing, version):
    monkeypatch.setattr(doctor, "probe", responses(workspace, version, missing))
    assert doctor.diagnose(workspace) == 1


def test_optional_lab_does_not_change_base_result(workspace, monkeypatch, capsys):
    monkeypatch.setattr(doctor, "probe", responses(workspace, lab_ok=False))
    assert doctor.diagnose(workspace) == 0
    assert doctor.diagnose(workspace, lab=True) == 2
    output = capsys.readouterr().out
    assert "基础开发环境：前提可用" in output
    assert "实验设施：未就绪" in output


def test_diagnostic_does_not_write_or_install(workspace, monkeypatch):
    before = {
        p.relative_to(workspace): p.read_bytes()
        for p in workspace.rglob("*")
        if p.is_file()
    }
    monkeypatch.setattr(doctor, "probe", responses(workspace))
    assert doctor.diagnose(workspace, lab=True) == 0
    after = {
        p.relative_to(workspace): p.read_bytes()
        for p in workspace.rglob("*")
        if p.is_file()
    }
    assert after == before


def test_real_cli_missing_environment_is_read_only(tmp_path):
    import subprocess
    import sys

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "scripts").mkdir()
    source = Path(__file__).resolve().parents[1] / "scripts/doctor.py"
    target = tmp_path / "scripts/doctor.py"
    target.write_bytes(source.read_bytes())
    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / "uv.lock").write_text("")
    before = {
        p.relative_to(tmp_path): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    result = subprocess.run(
        [sys.executable, "-B", str(target)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 1
    assert "基础开发环境：未就绪" in result.stdout
    after = {
        p.relative_to(tmp_path): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    assert after == before
    assert not (tmp_path / ".venv").exists()
