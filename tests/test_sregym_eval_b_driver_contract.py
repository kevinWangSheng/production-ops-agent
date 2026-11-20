"""#667 PR-B contract tests: the round driver's ``main.py`` command, result
discovery and the standalone filter proxy script (pre-review group 4).

Written from docs/tasks/2026-11-19-sregym-667.md "PR-B 公开接口", without
reading the PR-B implementation. Upstream ``main.py`` flag names were checked
against the pinned clone (argument parser only).
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from tests.sregym_eval_support import LITE_17, REPO, mod

PID = LITE_17[2]

# --- group 4: main.py command line ---------------------------------------------------------------

FORBIDDEN_FLAGS = (
    "--suite",
    "--profile",
    "--noise",
    "--baseline",
    "--propagation",
    "--use-external-harness",
    "--resume",
    "--agent-timeout",
)


def _flags(argv: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    rest = argv[3:]
    i = 0
    while i < len(rest):
        flag = rest[i]
        assert flag.startswith("--"), argv
        if "=" in flag:
            k, v = flag.split("=", 1)
            out[k] = v
            i += 1
        else:
            out[flag] = rest[i + 1]
            i += 2
    return out


@pytest.mark.parametrize("pid", LITE_17)
def test_main_command_runs_one_diagnosis_attempt_for_the_problem(pid: str) -> None:
    argv = mod("driver").main_command(pid)
    assert argv[:3] == ["uv", "run", "main.py"]
    assert _flags(argv) == {
        "--problem": pid,
        "--n-attempts": "1",
        "--stages": "diagnosis",
        "--agent": "opspilot",
        "--internet-access": "open",
        "--judge-model": "openai/glm-5.3-flashx",
    }


def test_main_command_has_no_tuning_knobs() -> None:
    argv = mod("driver").main_command(PID)
    for flag in FORBIDDEN_FLAGS:
        assert not any(a == flag or a.startswith(flag + "=") for a in argv), flag


# --- result discovery: only this pass's new run directory ---------------------------------------


def _csv(root: Path, run_dir: str, pid: str, mtime: float) -> Path:
    path = root / run_dir / "opspilot" / pid / f"{pid}_opspilot_results.csv"
    path.parent.mkdir(parents=True)
    path.write_text("attempt\n1\n")
    os.utime(path, (mtime, mtime))
    return path


def test_new_result_csv_ignores_run_dirs_that_existed_before(tmp_path: Path) -> None:
    root = tmp_path / "results"
    old = _csv(
        root, "20261119_0100", PID, mtime=2_000_000_000
    )  # newer mtime, but old dir
    before = frozenset(root.iterdir())
    new = _csv(root, "20261119_0200", PID, mtime=1_900_000_000)
    found = mod("driver").new_result_csv(root, PID, before)
    assert found is not None and Path(found).resolve() == new.resolve()
    assert Path(found).resolve() != old.resolve()


def test_new_result_csv_is_none_when_no_new_run_dir_has_one(tmp_path: Path) -> None:
    root = tmp_path / "results"
    _csv(root, "20261119_0100", PID, mtime=2_000_000_000)
    before = frozenset(root.iterdir())
    (root / "20261119_0200" / "opspilot").mkdir(parents=True)  # new dir, no CSV
    assert mod("driver").new_result_csv(root, PID, before) is None


def test_new_result_csv_ignores_another_problem(tmp_path: Path) -> None:
    root = tmp_path / "results"
    root.mkdir()
    before = frozenset(root.iterdir())
    _csv(root, "20261119_0200", LITE_17[0], mtime=1_900_000_000)
    assert mod("driver").new_result_csv(root, PID, before) is None


def test_new_result_csv_when_results_root_was_empty(tmp_path: Path) -> None:
    root = tmp_path / "results"
    root.mkdir()
    new = _csv(root, "20261119_0300", PID, mtime=1_900_000_000)
    found = mod("driver").new_result_csv(root, PID, frozenset())
    assert found is not None and Path(found).resolve() == new.resolve()


# --- the standalone filter proxy -------------------------------------------------------------------


FILTER_PROXY = REPO / "scripts" / "sregym_eval" / "filter_proxy.py"


def _imports(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
        elif isinstance(node, ast.ImportFrom) and node.level:
            names.add("." * node.level + (node.module or ""))
    return names


def test_filter_proxy_does_not_import_this_repository() -> None:
    tree = ast.parse(FILTER_PROXY.read_text())
    for name in _imports(tree):
        assert not name.startswith((".", "scripts", "opspilot", "tests")), name


def test_filter_proxy_reuses_the_upstream_proxy_class_and_takes_port_and_out_dir() -> (
    None
):
    text = FILTER_PROXY.read_text()
    tree = ast.parse(text)
    assert any(n.startswith("sregym.service.k8s_proxy") for n in _imports(tree)) or (
        "sregym.service.k8s_proxy" in text
    )
    assert "KubernetesAPIProxy" in text
    assert "generate_agent_kubeconfig" in text
    assert '"--port"' in text or "'--port'" in text
    assert '"--out-dir"' in text or "'--out-dir'" in text
    assert "127.0.0.1" in text
    assert "SIGTERM" in text
