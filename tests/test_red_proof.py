"""红证明检查的端到端行为：在临时 git 仓库里构造 base / PR 两个提交，跑真实 pytest。

承重的是 :func:`test_new_test_failing_on_base_is_red_proof`：它只有在测试真的
跑在 base 实现上时才会失败——如果快照错拿了 HEAD 的实现，这条用例会变成无红证明。
"""

import os
import pathlib
import subprocess

import pytest

from scripts import check_red_proof as rp

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "red-proof-test",
    "GIT_AUTHOR_EMAIL": "red-proof@example.invalid",
    "GIT_COMMITTER_NAME": "red-proof-test",
    "GIT_COMMITTER_EMAIL": "red-proof@example.invalid",
}

BUGGY_CALC = "def add(a, b):\n    return a - b\n"
FIXED_CALC = "def add(a, b):\n    return a + b\n"


def _git(repo: pathlib.Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, check=True, env=GIT_ENV, capture_output=True
    )


def _write(repo: pathlib.Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


@pytest.fixture
def repo(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _write(
        root,
        {
            "pyproject.toml": (
                "[tool.pytest.ini_options]\n"
                'testpaths = ["tests"]\n'
                'addopts = "-p no:langsmith_plugin"\n'
            ),
            "app/__init__.py": "",
            "app/calc.py": BUGGY_CALC,
            "tests/test_existing.py": "def test_existing():\n    assert True\n",
        },
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "checkout", "-q", "-b", "pr")
    return root


def _pr(repo: pathlib.Path, files: dict[str, str]) -> None:
    _write(repo, files)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "pr")


def _check(repo: pathlib.Path, *extra: str) -> int:
    return rp.main(["--repo", str(repo), "--base", "main", "--module", "app", *extra])


def test_selection_ignores_formatting_and_flags_changed_bodies() -> None:
    base = "def test_a():\n    assert 1\n\ndef test_b():\n    assert 1\n"
    head = (
        "# 只改注释和空行\ndef test_a():\n\n    assert 1  # 同一断言\n\n"
        "def test_b():\n    assert 2\n\n"
        "class TestNew:\n    def test_c(self):\n        assert 1\n"
    )
    assert rp.changed_items(base, head) == ["test_b", "TestNew::test_c"]
    assert rp.changed_items(None, base) == ["test_a", "test_b"]


def test_new_test_failing_on_base_is_red_proof(repo: pathlib.Path, capsys) -> None:
    _pr(
        repo,
        {
            "app/calc.py": FIXED_CALC,
            "tests/test_calc.py": (
                "from app.calc import add\n\n"
                "def test_add():\n    assert add(2, 3) == 5\n"
            ),
        },
    )
    assert _check(repo) == 0
    out = capsys.readouterr().out
    assert "有红证明" in out
    assert f"`tests/test_calc.py::test_add` | {rp.RED_ASSERT}" in out
    assert "test_existing" not in out


def test_test_green_on_base_has_no_red_proof(repo: pathlib.Path, capsys) -> None:
    # add(0, 0) 在错误实现上同样为 0：这条测试区分不出修复。
    _pr(
        repo,
        {
            "app/calc.py": FIXED_CALC,
            "tests/test_calc.py": (
                "from app.calc import add\n\n"
                "def test_add_zero():\n    assert add(0, 0) == 0\n"
            ),
        },
    )
    assert _check(repo) == 1
    assert "无红证明" in capsys.readouterr().out
    assert _check(repo, "--report-only") == 0


def test_test_for_new_module_is_only_weak_red(repo: pathlib.Path, capsys) -> None:
    _pr(
        repo,
        {
            "app/greet.py": "def hello():\n    return 'hi'\n",
            "tests/test_greet.py": (
                "from app.greet import hello\n\n"
                "def test_hello():\n    assert hello() == 'hi'\n"
            ),
        },
    )
    assert _check(repo) == 0
    out = capsys.readouterr().out
    assert "弱红证明" in out
    assert rp.RED_COLLECT in out


def test_collection_failure_does_not_hide_other_files(
    repo: pathlib.Path, capsys
) -> None:
    _pr(
        repo,
        {
            "app/calc.py": FIXED_CALC,
            "app/greet.py": "def hello():\n    return 'hi'\n",
            "tests/test_greet.py": (
                "from app.greet import hello\n\n"
                "def test_hello():\n    assert hello() == 'hi'\n"
            ),
            "tests/test_calc.py": (
                "from app.calc import add\n\n"
                "def test_add():\n    assert add(2, 3) == 5\n"
            ),
        },
    )
    assert _check(repo) == 0
    out = capsys.readouterr().out
    assert "有红证明" in out
    assert f"`tests/test_calc.py::test_add` | {rp.RED_ASSERT}" in out
    assert f"`tests/test_greet.py::test_hello` | {rp.RED_COLLECT}" in out


def test_source_only_change_is_not_applicable(repo: pathlib.Path, capsys) -> None:
    _pr(repo, {"app/calc.py": FIXED_CALC})
    assert _check(repo) == 0
    assert "不适用" in capsys.readouterr().out


def test_package_resolving_outside_snapshot_is_rejected(tmp_path: pathlib.Path) -> None:
    # 标准库的 json 不在快照里：等同于被测包错从 HEAD 或 site-packages 解析。
    with pytest.raises(rp.RedProofError, match="红证明无效"):
        rp.assert_imports_from(tmp_path, "json")


def test_support_only_change_needs_manual_confirmation(
    repo: pathlib.Path, capsys
) -> None:
    # 断言强化只落在共享支持模块时，不能给出确定性的「不适用」。
    _pr(repo, {"tests/calc_support.py": "EXPECTED = 5\n"})
    assert _check(repo) == 1
    out = capsys.readouterr().out
    assert "需人工确认" in out
    assert "tests/calc_support.py" in out
    assert _check(repo, "--report-only") == 0


def test_timeout_is_reported_not_raised(
    repo: pathlib.Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rp, "PER_FILE_TIMEOUT_SECONDS", 1)
    _pr(
        repo,
        {
            "tests/test_slow.py": (
                "import time\n\ndef test_slow():\n    time.sleep(5)\n"
            )
        },
    )
    assert _check(repo, "--report-only") == 0
    out = capsys.readouterr().out
    assert f"`tests/test_slow.py::test_slow` | {rp.NOT_RUN}" in out
    assert "无红证明" in out


def test_internal_error_is_reported_and_report_only_exits_zero(
    repo: pathlib.Path, capsys
) -> None:
    code = rp.main(["--repo", str(repo), "--base", "no-such-ref", "--report-only"])
    assert code == 0
    assert "内部错误" in capsys.readouterr().out
    assert rp.main(["--repo", str(repo), "--base", "no-such-ref"]) == 2
