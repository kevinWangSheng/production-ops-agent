import importlib.util
import json
import tomllib
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "setup_docs_mcp", Path(__file__).parents[1] / "scripts/setup_docs_mcp.py"
)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


@pytest.fixture
def project(tmp_path):
    (tmp_path / "AGENTS.md").write_text("instructions")
    (tmp_path / "SPEC.md").write_text("scope")
    return tmp_path


def test_install_preserves_unrelated_settings_and_is_idempotent(project):
    (project / ".codex").mkdir()
    (project / ".codex/config.toml").write_text('model = "existing"\n')
    (project / ".claude").mkdir()
    settings = project / ".claude/settings.local.json"
    settings.write_text(json.dumps({"permissions": {"deny": ["Bash(rm *)"]}}))
    files = setup.install(project)
    snapshot = {p: p.read_bytes() for p in files}
    setup.install(project)
    assert snapshot == {p: p.read_bytes() for p in files}
    parsed = tomllib.loads((project / ".codex/config.toml").read_text())
    assert parsed["model"] == "existing"
    assert (
        "submit_feedback"
        not in parsed["mcp_servers"]["langchain-docs"]["enabled_tools"]
    )
    parsed = json.loads(settings.read_text())
    assert "Bash(rm *)" in parsed["permissions"]["deny"]
    assert "mcp__langchain-docs__submit_feedback" in parsed["permissions"]["deny"]
    assert not parsed.get("enableAllProjectMcpServers", False)


def test_conflict_causes_no_partial_config_write(project):
    mcp = project / ".mcp.json"
    before = json.dumps({"mcpServers": {"langchain-reference": {"url": "existing"}}})
    mcp.write_text(before)
    with pytest.raises(ValueError):
        setup.install(project)
    assert mcp.read_text() == before
    assert not (project / ".codex/config.toml").exists()
    assert not (project / ".claude").exists()


def test_refuses_symlink_directory(project, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (project / ".codex").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        setup.install(project)
    assert not list(outside.iterdir())


def test_inline_toml_table_is_rejected_without_writing(project):
    (project / ".codex").mkdir()
    config = project / ".codex/config.toml"
    before = "mcp_servers = {}\n"
    config.write_text(before)
    with pytest.raises(ValueError):
        setup.install(project)
    assert config.read_text() == before
    assert not (project / ".mcp.json").exists()
    assert not (project / ".claude").exists()


@pytest.mark.parametrize("parent", [".codex", ".claude"])
def test_regular_file_parent_leaves_no_registrations(project, parent):
    (project / parent).write_text("preserve")
    before = {p.relative_to(project): p.read_bytes() for p in project.rglob("*")}
    with pytest.raises(ValueError):
        setup.install(project)
    assert before == {
        p.relative_to(project): p.read_bytes() for p in project.rglob("*")
    }


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure", ["stage", "replace"])
def test_io_failure_restores_all_original_files(
    project, monkeypatch, existing, failure
):
    if existing:
        (project / ".codex").mkdir()
        (project / ".codex/config.toml").write_text('model = "keep"\n')
        (project / ".codex/config.toml").chmod(0o640)
        (project / ".mcp.json").write_text('{"other": true}')

    def snapshot():
        return {
            p.relative_to(project): (p.read_bytes(), p.stat().st_mode)
            for p in project.rglob("*")
            if p.is_file()
        }

    before = snapshot()
    original = setup.stage_file if failure == "stage" else setup.os.replace
    calls = 0

    def fail_once(*args):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("synthetic write failure")
        return original(*args)

    if failure == "stage":
        monkeypatch.setattr(setup, "stage_file", fail_once)
    else:
        monkeypatch.setattr(setup.os, "replace", fail_once)
    with pytest.raises(OSError):
        setup.install(project)
    assert snapshot() == before
    assert not list(project.rglob(".mcp-stage-*"))
    assert not (project / ".claude").exists()


def test_rollback_failure_retains_original_and_continues_other_restores(
    project, monkeypatch
):
    (project / ".codex").mkdir()
    config = project / ".codex/config.toml"
    config.write_text('model = "keep"\n')
    config.chmod(0o640)
    mcp = project / ".mcp.json"
    original_mcp = b'{"other": true}'
    mcp.write_bytes(original_mcp)
    mcp.chmod(0o640)
    replace = setup.os.replace
    calls = 0

    def fail_publish_and_one_restore(*args):
        nonlocal calls
        calls += 1
        if calls in (3, 4):
            raise OSError("synthetic I/O failure")
        return replace(*args)

    monkeypatch.setattr(setup.os, "replace", fail_publish_and_one_restore)
    with pytest.raises(setup.RollbackError, match="MCP_ROLLBACK_INCOMPLETE"):
        setup.install(project)
    assert config.read_text() == 'model = "keep"\n'
    assert config.stat().st_mode & 0o777 == 0o640
    retained = list(project.rglob(".mcp-stage-*"))
    assert len(retained) == 1
    assert retained[0].parent == mcp.parent
    assert retained[0].read_bytes() == original_mcp
    assert retained[0].stat().st_mode & 0o777 == 0o640
    assert not (project / ".claude").exists()
