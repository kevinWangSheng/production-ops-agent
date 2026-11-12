"""CI fast-pass scope decision (#519): only unreferenced Markdown skips."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ci_scope", ROOT / "scripts/ci_scope.py")
assert spec and spec.loader
ci_scope = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci_scope)


@pytest.mark.parametrize(
    "path",
    ["NOTES-519-fixture.md", "docs/tasks/2026-01-01-fixture-519.md"],
)
def test_unreferenced_markdown_takes_the_fast_pass(path):
    assert ci_scope.blocking_reasons([path], references="unrelated text") == []


@pytest.mark.parametrize(
    "path",
    [
        "docs/evidence/fixture-519/README-519.md",
        "docs/fixture-519.json",
        "feature_list.json",
        "scripts/fixture_519.py",
        "deploy/compose/FIXTURE-519.md",
        "tests/FIXTURE-519.md",
    ],
)
def test_anything_else_keeps_the_full_run(path):
    assert ci_scope.blocking_reasons([path], references="")


def test_a_document_named_by_code_keeps_the_full_run():
    references = 'TEXT = (ROOT / "docs/tasks/2026-01-01-fixture-519.md").read_text()'
    reasons = ci_scope.blocking_reasons(
        ["docs/tasks/2026-01-01-fixture-519.md"], references
    )
    assert reasons


def test_one_blocking_path_among_documents_keeps_the_full_run():
    paths = ["docs/tasks/2026-01-01-fixture-519.md", "opspilot/fixture_519.py"]
    assert ci_scope.blocking_reasons(paths, references="")


def test_an_empty_diff_keeps_the_full_run():
    assert ci_scope.blocking_reasons([], references="")


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _merge_repo(tmp_path, changed):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "ci@example.invalid")
    _git(root, "config", "user.name", "ci")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "tests").mkdir()
    (root / "tests" / "test_reads.py").write_text('DOC = "docs/read-519.md"\n')
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "checkout", "-q", "-b", "pr")
    for name in changed:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("changed\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "pr")
    _git(root, "checkout", "-q", "main")
    (root / "base-only.txt").write_text("moved on\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "main moved")
    _git(root, "merge", "-q", "--no-ff", "-m", "merge", "pr")
    return root


@pytest.mark.parametrize(
    ("changed", "expected"),
    [
        (["docs/tasks/new-519.md"], "full=false"),
        (["docs/read-519.md"], "full=true"),
        (["docs/tasks/new-519.md", "scripts/x.py"], "full=true"),
    ],
)
def test_main_reads_the_merge_commit_and_writes_the_output(
    tmp_path, monkeypatch, changed, expected
):
    root = _merge_repo(tmp_path, changed)
    output = tmp_path / "github_output"
    monkeypatch.chdir(root)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert ci_scope.main() == 0
    assert output.read_text() == expected + "\n"


def test_a_head_that_is_not_a_merge_commit_keeps_the_full_run(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "NOTES.md").write_text("x\n")
    _git(root, "add", "-A")
    _git(
        root,
        "-c",
        "user.email=ci@example.invalid",
        "-c",
        "user.name=ci",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-q",
        "-m",
        "only",
    )
    output = tmp_path / "github_output"
    monkeypatch.chdir(root)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert ci_scope.main() == 0
    assert output.read_text() == "full=true\n"
