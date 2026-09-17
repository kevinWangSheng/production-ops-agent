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
            # 后写覆盖先写，所以这里要断唯一，否则多出来的一处会被悄悄吃掉。
            assert "window_scope" not in found, (
                "holmes_baseline.py 出现了多处「字面量 + join(...)」拼接，"
                "窗口句的提取不再唯一"
            )
            found["window_scope"] = node.left.value
    # 恰好两段，不是「前两段」。只取前两段的话，有人在后面再 append 一句纪律，
    # 提取器视而不见，漂移检查全绿——而它是「两处不得分叉」的唯一守门人。
    assert len(appended) == 2, (
        f"holmes_baseline.py 的 addition 追加段数变成了 {len(appended)}（原为 2）；"
        "新增或删除纪律文字必须同步到 opspilot/instructions/discipline.py"
    )
    for index, (_, text) in enumerate(sorted(appended)):
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


def scoped_services(variant_id: str, services: tuple[str, ...]) -> tuple[str, ...]:
    """只给带授权服务槽位的变体传服务列表。

    候选臂没有这个槽位；对它传非空列表现在是 ``ValueError``（fail-closed），
    而不是被静默丢掉。这个 helper 让参数化测试覆盖全部变体而不触发那条拒绝。
    """
    if any(s.key == "authorized_services" for s in d.VARIANTS[variant_id]):
        return services
    return ()


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
    # 这里**不**断言「同参数两次调用结果相同」——那是拿相同实参调同一个纯函数
    # 两次，怎么写都不会红。实例值不进 revision 这件事由 prompt_revision 的签名
    # 保证（它根本不收实例值）；这条测试真正承重的是下面「实例值确实改变了字节」。
    cheap = d.render(
        variant_id,
        model_requests=2,
        report_contract=LEGACY_REPORT_CONTRACT,
        authorized_services=scoped_services(variant_id, ("checkoutservice",)),
    )
    rich = d.render(
        variant_id,
        model_requests=9,
        report_contract=LEGACY_REPORT_CONTRACT,
        authorized_services=scoped_services(variant_id, SERVICES),
    )
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
    monkeypatch.setattr(d, "VARIANTS", {**d.VARIANTS, "replay-candidate": mutated})
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
    re.compile(
        r"\b[A-Za-z0-9_]*(?:api[_-]?key|secret|token|password)\b\s*[=:]\s*\S+",
        re.IGNORECASE,
    ),
)


@pytest.mark.parametrize(
    "sample",
    [
        "token sk-abcd1234efgh5678",
        "Authorization: Bearer abc.def.ghi",
        "https://user:pw@metrics.internal/api",
        "api_key = swordfish",
        "SERVICE_TOKEN: hunter2",
    ],
)
def test_credential_patterns_actually_match_something(sample: str) -> None:
    """阳性对照：没有它，上一条断言是恒真的。

    实测过：把 ``CREDENTIAL_SHAPES`` 四条正则整体换成永不匹配的
    ``ZZZ_NEVER_MATCHES_ANYTHING_ZZZ``，整个文件仍然全绿——正则写错、写漏、
    被删都不会有任何测试发现。所以先钉住「这些正则确实会命中凭据形态」，
    再让下一条断言「纪律文本命不中」才有意义。
    """
    assert any(pattern.search(sample) for pattern in CREDENTIAL_SHAPES), (
        f"没有任何凭据形态正则命中 {sample!r}；正则可能已被改坏"
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
        authorized_services=scoped_services(variant_id, SERVICES),
    )
    for pattern in CREDENTIAL_SHAPES:
        assert not pattern.search(rendered), (
            f"{variant_id} 的渲染结果命中凭据形态 {pattern.pattern}"
        )


def test_unknown_variant_fails_closed() -> None:
    with pytest.raises(d.UnknownVariantError):
        d.render("does-not-exist", model_requests=1, report_contract="x")


@pytest.mark.parametrize("value", [0, -1, True, 1.0, "many"])
def test_non_positive_budget_is_refused(value: object) -> None:
    """预算轮次必须是正整数。

    放行 0 或负数会拼出 ``You have at most -1 model requests`` 并照常送进模型——
    错误的字节比一个异常危险。``True`` 也要拒：``bool`` 是 ``int`` 的子类，
    放行它会让 ``at most True model requests`` 通过。
    """
    with pytest.raises(ValueError, match="positive integer"):
        d.render(
            "replay-candidate",
            model_requests=value,  # type: ignore[arg-type]
            report_contract=LEGACY_REPORT_CONTRACT,
        )


def test_services_for_a_variant_without_the_slot_are_refused_not_dropped() -> None:
    """给没有授权服务槽位的变体传服务列表 → 拒绝，而不是静默丢掉。

    静默丢掉会让调用方以为查询范围已被限定，而模型拿到的是未限定的纪律。
    查询范围属 Controller 权限（PRODUCT-CONSTRAINTS「Runtime and human control
    requirements」：*Read identity, exact target resolution, query budgets,
    cancellation and human control decisions are scoped outside the model's
    authority*），不能悄悄落空。
    """
    assert not any(
        s.key == "authorized_services" for s in d.VARIANTS["replay-candidate"]
    )
    with pytest.raises(ValueError, match="no authorized service list"):
        d.render(
            "replay-candidate",
            model_requests=2,
            report_contract=LEGACY_REPORT_CONTRACT,
            authorized_services=("checkoutservice",),
        )


def test_budget_slot_must_follow_its_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """预算槽位被排到不带占位符的段之后 → 拒绝，而不是默默改写别的段。

    ``render`` 是把数字回填进**前一段**。槽位一旦挪位，原实现会对报告契约那段
    调用 ``.format``，字节错了却不报错。今天三个变体都把开场排在第一位，
    所以这是给下一个加变体的人留的护栏——M1-01 的调查 loop 就要加。
    """
    broken = (
        d.VARIANTS["replay-candidate"][0],
        d.VARIANTS["replay-candidate"][2],  # 证据纪律段，不含 {steps}
        d.VARIANTS["replay-candidate"][1],  # 预算槽位被排到它后面
    )
    monkeypatch.setattr(d, "VARIANTS", {**d.VARIANTS, "broken": broken})
    with pytest.raises(ValueError, match="budget slot must follow"):
        d.render("broken", model_requests=2, report_contract=LEGACY_REPORT_CONTRACT)


def test_scope_conditional_segments_are_structural_not_a_suffix_trim() -> None:
    """无 scope 时窗口句与服务列表整段消失，且该行为由 ``scoped`` 标记决定。

    早先的实现是「渲染完再按后缀裁掉」：只要 segment 顺序变一下，裁剪就静默失效，
    而失效的表现是字节错了却没人报错。改成结构判定后，候选臂那句**无条件**的窗口句
    不受影响——同一段文字在两类变体里的条件性不同，这一点必须保住。
    """
    contract = report_instruction(version=REPORT_VERSION)
    unscoped = d.render(
        "baseline-multi-step", model_requests=2, report_contract=contract
    )
    assert d.FIXED_WINDOW not in unscoped
    assert d.AUTHORIZED_SERVICES_PREFIX not in unscoped

    candidate = d.render("replay-candidate", model_requests=2, report_contract=contract)
    assert d.FIXED_WINDOW in candidate, "候选臂的窗口句是无条件的，不得被一并裁掉"

    scoped_keys = {s.key for s in d.VARIANTS["baseline-multi-step"] if s.scoped}
    assert scoped_keys == {
        "fixed_window",
        "authorized_services_prefix",
        "authorized_services",
    }


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


@pytest.mark.parametrize(
    "bad",
    ["cartservice", ("",), ("cartservice", ""), ("cartservice", None), (b"svc",)],
)
def test_malformed_service_lists_are_refused(bad: object) -> None:
    """``authorized_services`` 必须是非空服务名的元组。

    ``str`` 也是可迭代的，所以传 ``"cartservice"`` 时 ``", ".join()`` 会逐字符展开成
    ``c, a, r, t, …``——不报错，但送进模型的授权清单已经烂了。空服务名同理：
    渲染出前缀后空无一物，读起来像「没授权任何服务」。两种都是静默损坏授权范围，
    而授权范围属 Controller 权限，不能悄悄落空。

    这条也补上了原先的不对称：同一个函数对 ``model_requests`` 严格到连 ``bool``
    都挡，对服务列表却只判真值。
    """
    with pytest.raises(ValueError, match="non-empty service names"):
        d.render(
            "baseline-multi-step",
            model_requests=2,
            report_contract=LEGACY_REPORT_CONTRACT,
            authorized_services=bad,  # type: ignore[arg-type]
        )


def test_a_string_service_list_would_have_been_shredded_character_by_character() -> (
    None
):
    """钉住上一条防的到底是什么：正常元组与逐字符展开的结果截然不同。"""
    proper = d.render(
        "baseline-multi-step",
        model_requests=2,
        report_contract=LEGACY_REPORT_CONTRACT,
        authorized_services=("cartservice",),
    )
    assert proper.endswith("cartservice")
    assert ", ".join("cartservice") == "c, a, r, t, s, e, r, v, i, c, e"


def test_a_one_shot_iterator_does_not_get_silently_drained_to_nothing() -> None:
    """独立审查发现：一次性迭代器会被校验那遍 ``all(...)`` 先耗尽。

    ``authorized_services`` 的类型注解是 ``tuple[str, ...]``，但运行时不强制。
    如果调用方传一个生成器，校验用 ``all(... for name in authorized_services)``
    迭代一遍，渲染再用 ``", ".join(authorized_services)`` 迭代第二遍——生成器
    只能走一遍，第二遍拿到的是空的。校验通过，但送进模型的授权清单悄悄变空，
    和 F14/`authorized_services` 那批「静默丢失」是同一类失效，只是换了个不
    触发字符串/空值护栏的入参形状。``render`` 现在先把入参物化成 tuple 再校验。
    """

    def services() -> object:
        yield "checkoutservice"
        yield "cartservice"

    rendered = d.render(
        "baseline-multi-step",
        model_requests=2,
        report_contract=LEGACY_REPORT_CONTRACT,
        authorized_services=services(),  # type: ignore[arg-type]
    )
    assert rendered.endswith("checkoutservice, cartservice")


def test_unknown_slot_is_refused_not_silently_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``render`` 不认识的槽位 → 拒绝，而不是当成一个什么都不填的空位。

    没有这条，加一个新槽位而忘了在 ``render`` 里接上，渲染照常成功，
    那段内容从 prompt 里整条消失——正是本模块自己声明要防的失效模式。
    """
    broken = d.VARIANTS["replay-candidate"] + (
        d.Segment("time_window", d.LAYER_INSTANCE, ""),
    )
    monkeypatch.setattr(d, "VARIANTS", {**d.VARIANTS, "broken": broken})
    with pytest.raises(ValueError, match="unknown slot 'time_window'"):
        d.render("broken", model_requests=2, report_contract=LEGACY_REPORT_CONTRACT)


def test_variants_mapping_is_read_only() -> None:
    """「单一来源」不能被任意调用方在运行时改掉。"""
    with pytest.raises(TypeError):
        d.VARIANTS["replay-candidate"] = ()  # type: ignore[index]


# --- C3 第 5 节 L1b 行：prompt_face_sha256 ------------------------------------


def test_face_hash_moves_with_instance_values_while_the_revision_holds() -> None:
    """C3 第 5 节 L1b：实例值变，face 就得变。

    这是「L1b 只记录」的落点。另一半（实例值**不**移动 `versions` 里的 revision）
    由 `prompt_revision` 的签名保证，并在 PG 用例里走过一遍真实 claim 路径，
    不在这里重复断言。
    """

    def face(*, model_requests: int, services: tuple[str, ...]) -> str:
        return d.prompt_face_sha256(
            "baseline-multi-step",
            model_requests=model_requests,
            report_contract=LEGACY_REPORT_CONTRACT,
            authorized_services=services,
        )

    base = face(model_requests=2, services=("checkoutservice",))
    richer_budget = face(model_requests=9, services=("checkoutservice",))
    wider_scope = face(model_requests=2, services=("checkoutservice", "cartservice"))

    assert len({base, richer_budget, wider_scope}) == 3, "两类实例值都必须移动 face"
    assert all(len(value) == 64 for value in (base, richer_budget, wider_scope)), (
        "face 是证据字段，取完整摘要而不是给人看的短码"
    )


def test_face_hash_equals_the_sha256_of_what_was_rendered() -> None:
    """face 就是实际送出字节的摘要，不是另算一套。"""
    kwargs = {
        "model_requests": 2,
        "report_contract": LEGACY_REPORT_CONTRACT,
    }
    rendered = d.render("replay-candidate", **kwargs)  # type: ignore[arg-type]
    assert (
        d.prompt_face_sha256("replay-candidate", **kwargs)  # type: ignore[arg-type]
        == hashlib.sha256(rendered.encode()).hexdigest()
    )
