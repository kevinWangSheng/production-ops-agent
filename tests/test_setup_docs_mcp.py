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
