"""结构约束的确定性检查。

这些断言覆盖的是跨文件的结构事实——依赖方向、状态词汇的单一来源——
lint 与类型检查都看不见它们（linter 只看单文件内的局部语法，mypy 只看
已写出的类型）。每条断言对应一个已经发生过的真实缺陷，不是预设的原则。
"""

import ast
import pathlib
import re
from typing import get_args

import pytest

import opspilot.domain as domain
from opspilot.domain import INCIDENT_LIFECYCLE, RUN_EXECUTION
from opspilot.domain.control import _ACTIONS, ControlAction

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PERSISTENCE = REPO_ROOT / "opspilot" / "persistence.py"
WORKER = REPO_ROOT / "opspilot" / "worker.py"


def _imported_modules(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


@pytest.mark.xfail(
    strict=True,
    reason=(
        "持久化层是否建立在 domain 状态机之上，是尚未做出的架构决定。"
        "该测试记录这笔欠债；决定做出并实施后删除此标记。"
    ),
)
def test_persistence_builds_on_domain() -> None:
    """依赖方向：持久化层应建立在领域层之上，而不是与之平行另建一套。"""
    modules = _imported_modules(PERSISTENCE)
    assert any(name.startswith("opspilot.domain") for name in modules), (
        f"opspilot/persistence.py 未依赖 opspilot.domain；实际 import：{sorted(modules)}"
    )


@pytest.mark.xfail(
    strict=True,
    reason="同上：状态判定改为经由 domain 状态机后，本测试应转为常规断言。",
)
def test_persistence_has_no_bare_domain_state_literals() -> None:
    """单一事实来源：领域状态名不应以裸字符串出现在持久化层。

    `paused` 曾因只存在于 SQL 字面量、未被纳入 claim/publish 的状态判定，
    导致人工暂停可被绕过。
    """
    known_states = RUN_EXECUTION.states | INCIDENT_LIFECYCLE.states
    tree = ast.parse(PERSISTENCE.read_text())
    offenders = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value in known_states
    }
    assert not offenders, (
        f"持久化层直接写入领域状态字面量 {sorted(offenders)}；"
        "状态判定应经由 opspilot.domain 的状态机"
    )


def test_literal_types_match_state_machine_keys() -> None:
    """同一状态集合被声明两次（Literal 与状态机键）时，两处必须一致。"""
    pairs = [
        ("RunExecution", domain.RunExecution, domain.RUN_EXECUTION),
        ("JobState", domain.JobState, domain.JOB),
        ("IncidentLifecycle", domain.IncidentLifecycle, domain.INCIDENT_LIFECYCLE),
        ("ReleaseStatus", domain.ReleaseStatus, domain.RELEASE_OBSERVATION),
        ("ToolOperationState", domain.ToolOperationState, domain.TOOL_OPERATION),
        ("EvidenceAdoption", domain.EvidenceAdoption, domain.EVIDENCE_ADOPTION),
        (
            "ObservationSessionState",
            domain.ObservationSessionState,
            domain.OBSERVATION_SESSION,
        ),
        ("PostmortemState", domain.PostmortemState, domain.POSTMORTEM),
        ("KnowledgeState", domain.KnowledgeState, domain.KNOWLEDGE_REVISION),
    ]
    for name, literal, machine in pairs:
        declared = frozenset(get_args(literal))
        assert declared == machine.states, (
            f"{name} 的 Literal 取值与状态机键不一致："
            f"仅在 Literal={sorted(declared - machine.states)}，"
            f"仅在状态机={sorted(machine.states - declared)}"
        )


def test_control_action_vocabulary_has_one_source() -> None:
    """`ControlAction` 与运行时校验用的 `_ACTIONS` 不得分叉。"""
    declared = frozenset(get_args(ControlAction))
    assert declared == _ACTIONS, f"两处定义不一致：{sorted(declared ^ _ACTIONS)}"


def test_persistence_sql_never_selects_a_qualified_star() -> None:
    """联表 SELECT 里的 `r.*` 会让同名列在结果集里出现两次。

    `psycopg.rows.dict_row` 静默保留最后一个，另一个在 dict 里不可达，而取到
    哪一个只取决于 select 列表的书写顺序。`claim()` 曾因此拿错
    `control_generation`（PR #19 修了那一处），三条写路径上同样的写法直到
    2026-09-15 复核才被发现——这是 SQL 字符串内部的事实，测试套件、mypy
    strict 与 ruff 都看不见它，只能在这里守。
    """
    tree = ast.parse(PERSISTENCE.read_text())
    offenders = sorted(
        {
            node.value.strip()
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and re.search(r"\bselect\b", node.value, re.IGNORECASE)
            and re.search(r"\b\w+\.\*", node.value)
        }
    )
    assert not offenders, (
        "持久化层的 SQL 使用了带表别名的 `*`，同名列会在结果集里重复并被静默"
        f"取到最后一个；改为显式列名：{offenders}"
    )


def _protected_by_lease_release(node: ast.AST) -> set[ast.AST]:
    """Calls whose enclosing ``try`` releases the lease on the way out."""
    protected: set[ast.AST] = set()
    for candidate in ast.walk(node):
        if not isinstance(candidate, ast.Try):
            continue
        releases = any(
            isinstance(inner, ast.Attribute) and inner.attr == "_abandon_best_effort"
            for handler in candidate.handlers
            for inner in ast.walk(handler)
        )
        if not releases:
            continue
        for statement in candidate.body:
            protected.update(ast.walk(statement))
    return protected


def test_recovery_session_releases_its_lease_when_a_store_call_fails() -> None:
    """持有租约期间的每个 store 调用，失败时都必须释放该租约。

    这条断言对应一次反复发生的真实缺陷：`RecoverySession` 里某个
    store 调用抛出瞬时存储错误后直接向上抛，租约留到期（最长
    `renew_seconds` 或 Run deadline），替补 worker 拿到 `LEASE_ACTIVE`，
    一次短暂的存储故障被放大成一次恢复停摆。executor、续租、提交、
    `publish()`、`rebuild()` 与 `lease_current()` 是分四轮逐个发现的，
    因此改为在结构上一次性约束，而不是继续逐个补。

    只覆盖直接的 `self.store.<方法>` 调用；`_renew()` 经 `getattr` 间接
    调用 `renew_lease`，由其调用点所在的 try 覆盖。
    """
    tree = ast.parse(WORKER.read_text())
    session = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "RecoverySession"
    )
    protected = _protected_by_lease_release(session)

    unprotected = []
    for method in session.body:
        if not isinstance(method, ast.FunctionDef):
            continue
        # The release helper itself is the terminal path; it swallows.
        if method.name == "_abandon_best_effort":
            continue
        for node in ast.walk(method):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "store"
                and node not in protected
            ):
                unprotected.append(f"{method.name}: self.store.{node.func.attr}()")

    assert not unprotected, (
        "以下 store 调用失败时不会释放本会话的租约："
        f"{sorted(unprotected)}；请置于调用 _abandon_best_effort() 的 try 内"
    )
