# M0 C 独立审查

日期：2026-09-08。审查者：全新上下文 review_c，未参与实现。工作区 `/Users/shenghuikevin/dev/AI/production-ops-agent-m0-c`；审查提交 `719257535c8a1b4355e9d757e5f3df71fa6b892a`，相对 `chore/m0-batch-baseline`。开始时工作区干净；未修改实现。

当前结论：两个 P2 已在修复提交 `965c7433fd9b1ce18ec462ccdb261cb437d9c1a6` 修复，并于 HEAD `cb8c463f5e9696934ca61c58ce2fbc422cfe550c` 由原独立审查者实际复验关闭。限定本批公开合成合同的独立审查通过；不代表 M0 退出、F1/F6/F14 或产品验收。下文 C-R1/C-R2 及首次结果保留为历史证据，最终复验见末节。

## C-R1 / P2：无任何引用的假设仍可标记 supported

位置：`scripts/m0/outcomes.py:309–314`。supported 分支只要求 completed、sufficient 和非空 claims，未要求最小证据支持结构。以公开 recovered-incident 为输入，清空 outcome.evidence，换成一条无 evidence_ids 的 hypothesis，并将 conclusion 设为 supported，实际返回 `[]`。因此未提供任何支持引用的结果可以通过“受证据支持的结论”检查。该问题无需判断自然语言因果真伪；有无支持引用可以确定性判定。

最小修复：为 supported 定义并校验至少一条具有可解析、成功且身份正确证据引用的支持性 claim；不能以无引用的 hypothesis/recommendation 或仅 counter_evidence 非空替代。增加无引用 supported 的拒绝测试和合法 supported 正例；保留因果正确性由校准 rubric 评价的现有边界。

## C-R2 / P2：报告省略恢复证据绕过内容完整性检查

位置：`scripts/m0/outcomes.py:258–275` 与 `326–337`。独立健康路径消费 captured_evidence，但没有校验其内容 SHA256；唯一 hash 校验只遍历 outcome.evidence。以公开 normal-release 为输入，清空 outcome.claims/evidence，篡改 evaluator.captured_evidence[0].content 而保留原 hash，实际 `independent_health == healthy` 且 `check_outcome == []`。这不是要求报告重新声明恢复事实；是独立恢复依据本身内容与其登记 hash 不一致时仍认证健康，违背可重放证据完整性。

最小修复：独立健康判定使用的证据必须验证内容 hash，失败为 unknown，并由 outcome 检查报告可定位的完整性违反项。不要要求 evaluator 私有资料全部进入 AgentInput。增加“仅外部观察使用、未在报告引用”的损坏/缺失证据拒绝测试。

## 反例重放

在上述工作区使用 `.venv/bin/python` 执行以下公开开发数据操作：

```python
import json
from pathlib import Path
from scripts.m0.outcomes import (
    IncidentScenario,
    IncidentOutcome,
    check_outcome,
    independent_health,
)

root = Path("tests/fixtures/m0/outcomes")


def check(data):
    scenario = IncidentScenario.model_validate_json(json.dumps(data["scenario"]))
    outcome = IncidentOutcome.model_validate_json(json.dumps(data["outcome"]))
    print(independent_health(scenario), check_outcome(scenario, outcome))


data = json.loads((root / "recovered-incident.json").read_text())
data["outcome"]["conclusion"] = "supported"
data["outcome"]["evidence"] = []
data["outcome"]["claims"] = [
    {
        "kind": "hypothesis",
        "text": "No supporting observation exists",
        "evidence_ids": [],
    }
]
check(data)  # 审查提交实际输出：healthy []

data = json.loads((root / "normal-release.json").read_text())
data["outcome"]["claims"] = []
data["outcome"]["evidence"] = []
data["scenario"]["evaluator"]["captured_evidence"][0]["content"] = "tampered"
check(data)  # 审查提交实际输出：healthy []
```

## 实际核查与结果

- 完整 SPEC、AGENTS、ROADMAP、任务/共享合同、C3、M0 计划和 F1/F3/F6/F7/F11/F14 原验收已读；未改变 passes 或实施门槛。
- `.venv/bin/python -m pytest tests/test_m0_outcomes.py`：37 passed，0.08s。
- `make check`：doctor、offline lock check、Ruff lint/format 均通过，84 passed，0.94s。
- 额外 duplicate captured evidence 反例：独立函数仍计算 healthy，但总 outcome 检查返回 DUPLICATE_EVIDENCE；未记为入口绕过。
- 7 份本地上游/OTel 原文和许可证逐一 SHA256 对照 sources.json 均一致。通过无认证 urllib HTTPS 只读重新抓取清单中的 7 个固定 SHA raw URL，实际字节 hash 全部一致。GitHub 公开 tag API `https://api.github.com/repos/open-telemetry/opentelemetry-demo/git/ref/tags/2.0.2` 实际返回 type=commit、SHA=`63649d6d6a59de88fb421b88c3c3a6185b6d21ad`。
- 源码实际具备映射文档所列 checkout 名称/依赖、Collector 三类写出 endpoint、Prometheus 资源属性列表及 1h 留存；Holmes create_tool_executor 与 ToolCallResult/format_tool_result_data 引用存在。公开文档正确区分遥测写出与调查只读查询入口，并明确未运行 baseline/环境、未固定 digest、未证明标签/IAM/容量，未将 issue 升级为本地复现。
- 独立发布主体、正常发布不建 Incident、调查执行与确定性结论分离、人工 closed 不等于健康、AgentInput 投影排除 evaluator 及可见字段边界有实现/测试证据。文档明确投影不等于 OS 隔离；本批缺少真实运行与最终校准资料已披露，不凭缺口要求扩大本批范围。
- 未读取真实 .env、私有保留集、答案或供应商私有材料；未调用模型/trace、云或生产，未运行重型环境。仅写本审查文件；无后台进程。

## 修复后独立复验（2026-09-08）

复验基点：`cb8c463f5e9696934ca61c58ce2fbc422cfe550c`，包含实现修复 `965c7433fd9b1ce18ec462ccdb261cb437d9c1a6`。审查文件当时是本审查者已有未跟踪文件，其余工作区无变更；未修改实现。

- C-R1 原反例重放：`healthy ['UNSUPPORTED_CONCLUSION']`，关闭。supported 要求至少一条带引用 fact；既有逐引用校验继续验证可解析、可见、已捕获、目标、hash 及成功状态。额外悬空 fact 返回 UNRESOLVED_EVIDENCE；引用 failed/no_data 记录返回 FAILED_EVIDENCE_AS_FACT，不会仅靠 claim.kind 绕过。该结构仅是最低证据门槛，不把存在引用当作自然语言因果正确。
- C-R2 原反例重放：`unknown ['EVIDENCE_HASH_MISMATCH', 'HEALTH_MISMATCH', 'UNPROVEN_HEALTHY_STATE']`，关闭。独立健康使用的每条 required-signal 证据均检查 hash，重复 captured ID 也 unknown；总入口校验全部 captured evidence，不因报告省略失去完整性验证。外部健康证据无需强行投影到 AgentInput。
- 新增 8 个回归用例和既有 37 项一起运行：`.venv/bin/python -m pytest tests/test_m0_outcomes.py` → **45 passed in 0.08s**。合法 supported、无 Agent/report 拷贝的完整独立健康证据正例通过；缺失/重复/损坏记录拒绝。
- 另行清空 completed-uncertain 的 claims/evidence，分别保留 conclusion=partial/inconclusive：均返回 `unknown []`。合理不确定结果未被新 supported 门槛过度限制；人工 closed 区别由原回归继续验证。
- `make check` 实际 doctor、offline lock、Ruff lint/format 通过，**92 passed in 0.95s**。该结果在本节写入前取得；本节仅追加审查文字。

本轮没有新增必须修复发现。两个原始反例已确定性拒绝，首次审查的源码映射/隔离限制结论继续有效。审查完成不等于完整运行/权限/恢复/统计评测通过；原 SPEC 与产品验收门槛保持。
