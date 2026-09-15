"""L1 调查纪律单一来源模块的确定性检查。

这些断言覆盖的是 C3 第 5 节「指令分层与版本」在收敛后必须继续成立的事实：
字节冻结、变体身份、模板/实例分界、以及投影字段的同步。lint 与 mypy 都看不见
它们——收敛的风险恰恰在于「代码看起来对，但送进模型的字节变了」。

承重的那一条是 :func:`test_replay_candidate_reproduces_the_frozen_m0_prompt_hash`：
``prompt_sha256`` 已经冻结在 M0 证据里，收敛不允许改动它。期望值不写死在本文件，
而是从证据 JSON 读回，避免「改实现顺手改期望值」。
"""

import ast
import functools
import hashlib
import json
import pathlib
import re

import pytest

from opspilot.domain.base import DomainError
from opspilot.domain.control import ScopeVersions, check_scope_versions
from opspilot.instructions import discipline as d
from scripts.m0_environment.report_contract import (
    LEGACY_REPORT_VERSION,
    REPORT_VERSION,
    report_instruction,
)
from scripts.m0_lab.round07.candidate_runner import DISCIPLINE

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
HOLMES_BASELINE = REPO_ROOT / "scripts/m0_environment/holmes_baseline.py"
CANDIDATE_EVIDENCE = (
    REPO_ROOT
    / "docs/evidence/m0-real-investigation/round-07-upstream-runs"
    / "m004-normal-candidate-retry2/result-business.json"
)

LEGACY_REPORT_CONTRACT = report_instruction(version=LEGACY_REPORT_VERSION)
SERVICES = ("checkoutservice", "cartservice")


@functools.cache
def baseline_literals() -> dict[str, str]:
    """从 ``holmes_baseline.py`` 源码里取回三段 ``addition`` 与窗口/服务句。

    用 AST 而不是 import：该脚本运行时依赖固定版本的 HolmesGPT checkout
    （``tmp/`` 下，被 .gitignore 排除），CI 里不存在。读源码才能让漂移检查
    在没有上游的环境里照样执行。

    定位一律走**结构**不走内容——按节点类型与出现顺序取，不按句子里的词取。
    否则改动被检查的文字会让提取器自己找不到目标，漂移检查以「采集失败」
    而不是「字节不符」的形式收场，正好在它该转红的时候失去意义。
    """
    tree = ast.parse(HOLMES_BASELINE.read_text())
    found: dict[str, str] = {}
    appended: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "addition" for t in node.targets
        ):
            value = node.value
            if isinstance(value, ast.JoinedStr):
                found["multi_open"] = "".join(
                    part.value if isinstance(part, ast.Constant) else "{steps}"
                    for part in value.values
                )
            elif isinstance(value, ast.Constant):
                found["final_open"] = value.value
        elif (
            isinstance(node, ast.AugAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "addition"
            and isinstance(node.value, ast.Constant)
        ):
            appended.append((node.lineno, node.value.value))
        elif (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Add)
            and isinstance(node.left, ast.Constant)
            and isinstance(node.left.value, str)
            and isinstance(node.right, ast.Call)
            and isinstance(node.right.func, ast.Attribute)
            and node.right.func.attr == "join"
        ):
            # 唯一一处「字面量 + 分隔符.join(...)」，即窗口句加授权服务列表。
            found["window_scope"] = node.left.value
    for index, (_, text) in enumerate(sorted(appended)[:2]):
        found[("evidence", "projection")[index]] = text
    missing = {
        "multi_open",
        "final_open",
        "evidence",
        "projection",
        "window_scope",
    } - found.keys()
    assert not missing, (
        f"holmes_baseline.py 的结构变了，取不到这些历史字面量：{sorted(missing)}"
    )
    return found


def _historical_baseline_prompt(opening: str, report_contract: str) -> str:
    """复刻 ``holmes_baseline.py`` 第 1134–1150 行的拼装顺序。

    注意窗口句与服务列表拼在报告契约**之后**，而候选臂拼在**之前**；
    这个顺序差异是真的，模块的 segment 序列必须照样保留。
    """
    baseline = baseline_literals()
    addition = (
        opening + baseline["evidence"] + baseline["projection"] + " " + report_contract
    )
    return addition + baseline["window_scope"] + ", ".join(SERVICES)


# --- 硬约束：冻结的 prompt_sha256 -------------------------------------------


def test_replay_candidate_reproduces_the_frozen_m0_prompt_hash() -> None:
    """收敛后渲染出的字节必须与 M0 证据里记录的 ``prompt_sha256`` 一致。

    ``m004-normal-candidate-retry2`` 与 ``m004-fault-candidate`` 两个 Run 都以
    ``max_http=2`` 记录了这个哈希。文本改一个字节，这里立刻转红。
    """
    recorded = json.loads(CANDIDATE_EVIDENCE.read_text())
    rendered = d.render(
        "replay-candidate",
        model_requests=recorded["max_http"],
        report_contract=LEGACY_REPORT_CONTRACT,
    )
    assert hashlib.sha256(rendered.encode()).hexdigest() == recorded["prompt_sha256"]


def test_replay_candidate_matches_the_historical_assembly_byte_for_byte() -> None:
    """模块渲染结果与 ``candidate_runner.py`` 现存的拼装逐字节相同。"""
    rendered = d.render(
        "replay-candidate", model_requests=2, report_contract=LEGACY_REPORT_CONTRACT
    )
    assert rendered == DISCIPLINE.format(steps=2) + LEGACY_REPORT_CONTRACT


@pytest.mark.parametrize("report_version", [LEGACY_REPORT_VERSION, REPORT_VERSION])
def test_baseline_variants_match_the_historical_assembly_byte_for_byte(
    report_version: str,
) -> None:
    """两个 baseline 变体在两种报告契约版本下都逐字节重建成功。"""
    contract = report_instruction(version=report_version)
    assert d.render(
        "baseline-multi-step",
        model_requests=4,
        report_contract=contract,
        authorized_services=SERVICES,
    ) == _historical_baseline_prompt(
        baseline_literals()["multi_open"].format(steps=4), contract
    )
    assert d.render(
        "baseline-final-report",
        model_requests=1,
        report_contract=contract,
        authorized_services=SERVICES,
    ) == _historical_baseline_prompt(baseline_literals()["final_open"], contract)


def test_baseline_without_scope_drops_the_window_and_service_clause() -> None:
    """没有授权 scope 时窗口句整段不出现——历史行为，不是本次新增的分支。"""
    contract = report_instruction(version=REPORT_VERSION)
    rendered = d.render(
        "baseline-final-report", model_requests=1, report_contract=contract
    )
    baseline = baseline_literals()
    assert (
        rendered
        == baseline["final_open"]
        + baseline["evidence"]
        + baseline["projection"]
        + " "
        + contract
    )
    assert d.FIXED_WINDOW not in rendered


# --- 漂移检查：单一来源与历史脚本不得分叉 ------------------------------------


def test_module_constants_still_match_the_historical_script_literals() -> None:
    """收敛后两处仍是同一字节。

    历史实验脚本按 ROADMAP 的既定做法保留原值作为历史记录，不改成 import；
    因此需要这条断言承担「两处不得分叉」的职责。任一处被改动即转红。
    """
    baseline = baseline_literals()
    assert d.READ_ONLY_OPENING == baseline["multi_open"]
    assert d.FINAL_REPORT_OPENING == baseline["final_open"]
    assert d.EVIDENCE_DISCIPLINE == baseline["evidence"]
    assert d.PROJECTION_DISCIPLINE + d.MISSING_SERIES == baseline["projection"]
    assert d.FIXED_WINDOW + d.AUTHORIZED_SERVICES_PREFIX == baseline["window_scope"]
    assert (
        d.READ_ONLY_OPENING
        + d.EVIDENCE_DISCIPLINE
        + d.MISSING_SERIES
        + d.FIXED_WINDOW
        + " "
        == DISCIPLINE
    )


def test_sentence_census_matches_the_recorded_layer_analysis() -> None:
    """句数普查：两个 baseline 变体各 24 句、并集 30 句、候选臂 15 句。

    这些数字是分层分析的结论（任务记录 F1/P2-2/P2-4）。收敛只准搬运不准增删，
    所以普查在这里固定下来；有人顺手补一句纪律，这条会先转红。
    """

    def sentences(text: str) -> int:
        return len([s for s in re.split(r"(?<=\.)\s+", text.strip()) if s])

    def variant_sentences(variant_id: str) -> int:
        return sum(
            sentences(s.text)
            for s in d.VARIANTS[variant_id]
            if s.layer == d.LAYER_TEMPLATE
        )

    assert variant_sentences("baseline-multi-step") == 24
    assert variant_sentences("baseline-final-report") == 24
    assert variant_sentences("replay-candidate") == 15
    shared = variant_sentences("baseline-multi-step") - sentences(d.READ_ONLY_OPENING)
    assert shared == 18
    assert sentences(d.PROJECTION_DISCIPLINE) == 8
    union = {
        s.key
        for variant in ("baseline-multi-step", "baseline-final-report")
        for s in d.VARIANTS[variant]
        if s.layer == d.LAYER_TEMPLATE
    }
    assert sum(sentences(getattr(d, key.upper())) for key in union) == 30


# --- C3 第 5 节：模板 / 实例分界 --------------------------------------------


@pytest.mark.parametrize("variant_id", sorted(d.VARIANTS))
def test_instance_values_do_not_move_the_revision(variant_id: str) -> None:
    """仅预算轮次或授权服务列表不同的两个 Run，revision 必须相同。

    这条直接防住「仅因预算不同就 blocked(INCOMPATIBLE_STATE)」。
    """
    assert d.discipline_revision(variant_id) == d.discipline_revision(variant_id)
    one = d.prompt_revision(variant_id, report_contract=LEGACY_REPORT_CONTRACT)
    other = d.prompt_revision(variant_id, report_contract=LEGACY_REPORT_CONTRACT)
    assert one == other

    cheap = d.render(
        variant_id,
        model_requests=2,
        report_contract=LEGACY_REPORT_CONTRACT,
        authorized_services=("checkoutservice",),
    )
    rich = d.render(
        variant_id,
        model_requests=9,
        report_contract=LEGACY_REPORT_CONTRACT,
        authorized_services=SERVICES,
    )
    assert one == d.prompt_revision(variant_id, report_contract=LEGACY_REPORT_CONTRACT)
    if "{steps}" in "".join(
        s.text for s in d.VARIANTS[variant_id] if s.layer == d.LAYER_TEMPLATE
    ):
        assert cheap != rich, "实例值必须真的改变渲染字节，否则这条断言是空的"


def test_projection_carries_template_bytes_not_filled_values() -> None:
    """哈希投影必须落在模板字节上，占位符要原样留着。

    C3 第 5 节 revision 规则 2：运行时值不进 revision。把 ``{steps}`` 提前填掉
    （例如给它设个默认值再存进常量）会让两个仅预算轮次不同的 Run 得到不同的
    ``prompt_revision``，进而仅因预算不同就被判 ``INCOMPATIBLE_STATE``。
    :func:`prompt_revision` 的签名不收实例值，挡住的是一类写法；这条挡的是另一类。
    """
    projected = {
        entry["key"]: entry["text"]
        for entry in d.template_projection("replay-candidate")
    }
    assert projected["read_only_opening"] == d.READ_ONLY_OPENING
    assert "{steps}" in projected["read_only_opening"]
    filled = d.render(
        "replay-candidate", model_requests=7, report_contract=LEGACY_REPORT_CONTRACT
    )
    assert "{steps}" not in filled and " 7 model requests" in filled


def test_report_contract_version_moves_the_prompt_revision() -> None:
    """``prompt_revision`` 是 L1a 与 L2 的复合版本——L2 换版必须 bump。"""
    legacy = d.prompt_revision(
        "baseline-multi-step", report_contract=LEGACY_REPORT_CONTRACT
    )
    strict = d.prompt_revision(
        "baseline-multi-step",
        report_contract=report_instruction(version=REPORT_VERSION),
    )
    assert legacy != strict


def test_revision_identifies_the_variant() -> None:
    """C3：``discipline_revision`` 必须标识变体，否则两条路径共用版本号而内容不同。"""
    revisions = {v: d.discipline_revision(v) for v in d.VARIANTS}
    assert len(set(revisions.values())) == len(d.VARIANTS)
    for variant_id, revision in revisions.items():
        assert revision.startswith(f"l1a-{variant_id}-")


def test_template_byte_change_bumps_the_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """改一个词也要 bump。这里直接做一次变异，确认 revision 真的跟着动。"""
    before = d.discipline_revision("replay-candidate")
    mutated = tuple(
        s._replace(text=s.text.replace("read-only", "read only"))
        if s.key == "read_only_opening"
        else s
        for s in d.VARIANTS["replay-candidate"]
    )
    monkeypatch.setitem(d.VARIANTS, "replay-candidate", mutated)
    assert d.discipline_revision("replay-candidate") != before


def test_revision_projection_covers_every_template_field() -> None:
    """新增 ``Segment`` 字段必须同步进哈希投影，否则模板变了而 revision 不变。

    与 C3 第 8 节对工具注册表哈希的要求同理：静默丢弃新字段是已经发生过的缺陷类型。
    """
    projected = set(d.template_projection("baseline-multi-step")[0])
    declared = set(d.Segment._fields) - {"layer"}
    assert declared <= projected, (
        f"这些 Segment 字段没进哈希投影：{sorted(declared - projected)}"
    )


# --- C3 第 5 节：工具专有知识不进纪律层 --------------------------------------


def test_tool_specific_text_is_quarantined_and_absent_from_the_clean_variant() -> None:
    """投影字段语义按 C3 属 L3；它被标出来，且不出现在不含工具专有知识的变体里。"""
    tool_specific = {
        s.key for variant in d.VARIANTS.values() for s in variant if s.tool_specific
    }
    assert tool_specific == {"projection_discipline"}
    assert not any(s.tool_specific for s in d.VARIANTS["replay-candidate"])
    assert any(s.tool_specific for s in d.VARIANTS["baseline-multi-step"])


# --- PRODUCT-CONSTRAINTS：凭据不得进入 prompt --------------------------------

CREDENTIAL_SHAPES = (
    re.compile(r"\bsk-[A-Za-z0-9]{8,}"),
    re.compile(r"\bBearer\s+\S+"),
    re.compile(r"https?://[^\s/]*:[^\s/]*@"),
    re.compile(r"\b[A-Za-z0-9_]*(?:api[_-]?key|secret|token|password)\b\s*[=:]\s*\S+"),
)


@pytest.mark.parametrize("variant_id", sorted(d.VARIANTS))
def test_no_credential_shaped_text_reaches_the_prompt(variant_id: str) -> None:
    """凭据形态正则，不做关键词匹配。

    PRODUCT-CONSTRAINTS：``Credentials and secret-bearing raw inputs must not enter
    prompts or exported traces``。这里查的是形态，不是某个具体字符串。
    """
    rendered = d.render(
        variant_id,
        model_requests=4,
        report_contract=LEGACY_REPORT_CONTRACT,
        authorized_services=SERVICES,
    )
    for pattern in CREDENTIAL_SHAPES:
        assert not pattern.search(rendered), (
            f"{variant_id} 的渲染结果命中凭据形态 {pattern.pattern}"
        )


def test_unknown_variant_fails_closed() -> None:
    with pytest.raises(d.UnknownVariantError):
        d.render("does-not-exist", model_requests=1, report_contract="x")


# --- C3 第 5 节：四类变化各走各的机制 ----------------------------------------


def test_scope_tightening_does_not_travel_through_versions() -> None:
    """授权收紧走控制代际，不走 ``versions``。

    C3 第 5 节：``INCOMPATIBLE_STATE`` 指状态版本不兼容，恢复路径是显式迁移或新建
    Run；授权收回的恢复路径是解除后重新授权。用前者表达后者会把正常权限操作记成
    版本事故。这里断言两条通道确实分开——授权变化落在 ``CONTROL_CONFLICT``，
    且完全不触动 ``prompt_revision``。
    """
    before = ScopeVersions(
        subject_control_generation=1,
        global_suspension_generation=0,
        target_suspension_generation=0,
    )
    tightened = before.model_copy(update={"target_suspension_generation": 1})
    with pytest.raises(DomainError) as raised:
        check_scope_versions(before, tightened)
    assert raised.value.code == "CONTROL_CONFLICT"

    narrow = d.render(
        "baseline-multi-step",
        model_requests=4,
        report_contract=LEGACY_REPORT_CONTRACT,
        authorized_services=("checkoutservice",),
    )
    wide = d.render(
        "baseline-multi-step",
        model_requests=4,
        report_contract=LEGACY_REPORT_CONTRACT,
        authorized_services=SERVICES,
    )
    assert narrow != wide, "授权范围必须真的改变送进模型的字节"
    assert d.prompt_revision(
        "baseline-multi-step", report_contract=LEGACY_REPORT_CONTRACT
    ) == d.prompt_revision(
        "baseline-multi-step", report_contract=LEGACY_REPORT_CONTRACT
    )
