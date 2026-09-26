"""红证明：PR 新增或修改的测试，放到 merge-base 的代码上至少要有一个失败。

测试在「目标行为还不存在」的代码上也能通过，就证明不了这次改动——它可能 mock 掉了
被测对象、断言了现有（含 bug）的输出，或者根本没有触及改动。PR 正文里自述的
「红→绿」无法复核，这里把它变成可重放的检查：

1. 找出 PR 相对 merge-base 新增或改动过的测试函数（按 AST 比较，忽略格式与注释）。
2. 导出 merge-base 的代码快照，再用 HEAD 的 ``tests/`` 覆盖，即「新测试 + 旧实现」。
3. 逐文件在快照里只跑这些测试。任何一个失败或报错，即有红证明。

结果分四类：断言失败（强证据）、报错（含收集失败；为全新模块写的测试在 base 上
因 ImportError 失败属于这一类，证明力弱，单独标出）、通过、跳过。跳过通常是缺少
环境开关（例如 PostgreSQL 集成测试），不算红。

只比较已提交的内容；本地使用前先提交。试行期在 CI 中用 ``--report-only``：
结论写入 step summary 与 warning 注解，退出码恒为 0，不影响合并状态。

用法::

    .venv/bin/python scripts/check_red_proof.py --base origin/main
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import io
import os
import pathlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ET

TEST_ROOT = "tests"
PER_FILE_TIMEOUT_SECONDS = 900

RED_ASSERT = "断言失败"
RED_ERROR = "报错"
RED_COLLECT = "收集失败"
GREEN = "通过"
SKIPPED = "跳过"
NOT_RUN = "未运行"
RED_KINDS = (RED_ASSERT, RED_ERROR, RED_COLLECT)


@dataclasses.dataclass(frozen=True)
class Outcome:
    node_id: str
    kind: str
    detail: str = ""


def git(repo: pathlib.Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def git_bytes(repo: pathlib.Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True
    ).stdout


def test_items(source: str) -> dict[str, str]:
    """返回 {限定名: AST dump}；限定名为 ``func`` 或 ``Class::method``。"""
    items: dict[str, str] = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if node.name.startswith("test"):
                items[node.name] = ast.dump(node)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for child in node.body:
                if isinstance(
                    child, ast.FunctionDef | ast.AsyncFunctionDef
                ) and child.name.startswith("test"):
                    items[f"{node.name}::{child.name}"] = ast.dump(child)
    return items


def changed_items(base_source: str | None, head_source: str) -> list[str]:
    base = test_items(base_source) if base_source is not None else {}
    head = test_items(head_source)
    return [name for name, dump in head.items() if base.get(name) != dump]


def is_test_file(path: str) -> bool:
    name = pathlib.PurePosixPath(path).name
    return (
        path.startswith(f"{TEST_ROOT}/")
        and name.endswith(".py")
        and (name.startswith("test_") or name.endswith("_test.py"))
    )


def selected_tests(repo: pathlib.Path, base: str, head: str) -> dict[str, list[str]]:
    """{测试文件: [限定名]}，只含新增或改动过的测试。"""
    names = git(
        repo, "diff", "--name-only", "--diff-filter=AMR", base, head, "--", TEST_ROOT
    ).split()
    selected: dict[str, list[str]] = {}
    for path in names:
        if not is_test_file(path):
            continue
        head_source = git(repo, "show", f"{head}:{path}")
        try:
            base_source: str | None = git(repo, "show", f"{base}:{path}")
        except subprocess.CalledProcessError:
            base_source = None
        items = changed_items(base_source, head_source)
        if items:
            selected[path] = items
    return selected


def extract(repo: pathlib.Path, ref: str, dest: pathlib.Path, *paths: str) -> None:
    archive = git_bytes(repo, "archive", "--format=tar", ref, *paths)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(dest, filter="data")


def build_snapshot(repo: pathlib.Path, base: str, head: str) -> pathlib.Path:
    """merge-base 的代码 + HEAD 的 tests/。"""
    snapshot = pathlib.Path(tempfile.mkdtemp(prefix="red-proof-"))
    extract(repo, base, snapshot)
    shutil.rmtree(snapshot / TEST_ROOT, ignore_errors=True)
    extract(repo, head, snapshot, TEST_ROOT)
    return snapshot


def assert_imports_from(snapshot: pathlib.Path, module: str) -> None:
    """被测包必须从快照解析，否则跑的其实是 HEAD 的实现。"""
    probe = (
        "import importlib.util, sys; "
        f"spec = importlib.util.find_spec({module!r}); "
        "print(spec.origin if spec and spec.origin else "
        "(list(spec.submodule_search_locations)[0] if spec else ''))"
    )
    origin = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=snapshot,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    resolved = pathlib.Path(origin).resolve() if origin else None
    if resolved is None or not resolved.is_relative_to(snapshot.resolve()):
        raise SystemExit(
            f"红证明无效：{module} 解析到 {origin or '无'}，不在 base 快照 {snapshot} 内"
        )


def run_file(
    snapshot: pathlib.Path, path: str, items: list[str], junit: pathlib.Path
) -> list[Outcome]:
    node_ids = [f"{path}::{item}" for item in items]
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            f"--junitxml={junit}",
            *node_ids,
        ],
        cwd=snapshot,
        capture_output=True,
        text=True,
        timeout=PER_FILE_TIMEOUT_SECONDS,
    )
    if not junit.exists():
        tail = (completed.stdout + completed.stderr).strip().splitlines()[-3:]
        return [Outcome(n, NOT_RUN, " / ".join(tail)) for n in node_ids]
    return classify(path, items, junit)


def classify(path: str, items: list[str], junit: pathlib.Path) -> list[Outcome]:
    module = path.removesuffix(".py").replace("/", ".")
    cases = list(ET.parse(junit).iter("testcase"))
    for case in cases:
        if case.get("classname") == "" and case.get("name") == module:
            error = case.find("error")
            detail = error.get("message", "") if error is not None else ""
            return [
                Outcome(f"{path}::{i}", RED_COLLECT, first_line(detail)) for i in items
            ]

    outcomes = []
    for item in items:
        cls, _, func = item.rpartition("::")
        classname = f"{module}.{cls}" if cls else module
        kinds = []
        detail = ""
        for case in cases:
            if case.get("classname") != classname:
                continue
            if (case.get("name") or "").split("[", 1)[0] != func:
                continue
            if (failure := case.find("failure")) is not None:
                kinds.append(RED_ASSERT)
                detail = detail or failure.get("message", "")
            elif (error := case.find("error")) is not None:
                kinds.append(RED_ERROR)
                detail = detail or error.get("message", "")
            elif (skip := case.find("skipped")) is not None:
                kinds.append(SKIPPED)
                detail = detail or skip.get("message", "")
            else:
                kinds.append(GREEN)
        kind = next(
            (k for k in (RED_ASSERT, RED_ERROR, GREEN, SKIPPED) if k in kinds),
            NOT_RUN,
        )
        outcomes.append(Outcome(f"{path}::{item}", kind, first_line(detail)))
    return outcomes


def first_line(text: str) -> str:
    return text.strip().splitlines()[0] if text.strip() else ""


def verdict(outcomes: list[Outcome]) -> tuple[bool, str]:
    kinds = {o.kind for o in outcomes}
    if RED_ASSERT in kinds or RED_ERROR in kinds:
        return True, "有红证明：至少一个新增/改动的测试在 base 上断言失败或报错。"
    if RED_COLLECT in kinds:
        return True, (
            "弱红证明：只有收集失败（多为测试导入了 base 上不存在的模块），"
            "没有测试在 base 上走到断言。"
        )
    return False, (
        "无红证明：新增/改动的测试在 base 上没有一个失败，"
        "它们区分不出这次改动（跳过的测试不计）。"
    )


def render(base: str, outcomes: list[Outcome], ok: bool, summary: str) -> str:
    lines = [
        "## 红证明",
        "",
        f"base：`{base[:12]}`；检查 {len(outcomes)} 个新增/改动的测试。",
        "",
        f"**{summary}**",
        "",
        "| 测试 | base 上的结果 | 说明 |",
        "|---|---|---|",
    ]
    for o in outcomes:
        detail = o.detail.replace("|", "\\|")[:160]
        lines.append(f"| `{o.node_id}` | {o.kind} | {detail} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--base", default="origin/main", help="对比的目标分支")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--repo", default=".", type=pathlib.Path)
    parser.add_argument("--module", default="opspilot", help="用于核对快照解析的被测包")
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="试行模式：只报告，退出码恒为 0",
    )
    args = parser.parse_args(argv)

    repo = args.repo.resolve()
    base = git(repo, "merge-base", args.base, args.head).strip()
    head = git(repo, "rev-parse", args.head).strip()
    selection = selected_tests(repo, base, head)
    if not selection:
        print("红证明：本次没有新增或改动的测试函数，不适用。")
        return 0

    snapshot = build_snapshot(repo, base, head)
    try:
        if (snapshot / args.module).exists():
            assert_imports_from(snapshot, args.module)
        outcomes: list[Outcome] = []
        for index, (path, items) in enumerate(sorted(selection.items())):
            junit = snapshot / f".red-proof-{index}.xml"
            outcomes.extend(run_file(snapshot, path, items, junit))
    finally:
        shutil.rmtree(snapshot, ignore_errors=True)

    ok, summary = verdict(outcomes)
    report = render(base, outcomes, ok, summary)
    print(report)
    if step_summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write(report)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        level = "notice" if ok else "warning"
        print(f"::{level} title=红证明::{summary}")
    if args.report_only:
        return 0
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
