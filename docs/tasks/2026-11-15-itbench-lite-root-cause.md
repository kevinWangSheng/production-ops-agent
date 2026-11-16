# ITBench-Lite 根因改为结构化输出（#646、#647）

- 状态：进行中
- 更新日期：2026-11-15
- 依据：issue #646（根因改结构化）、#647（词表排除命名空间名）；[ITBench-Lite 适配任务记录](2026-11-12-itbench-lite-adapter.md)；C3 L2 报告合同（[技术方案](../design/technical-proposal-2026-09-07.md) 第 173–177 行）；开工前合同预审（仓库外，lead 会话 scratchpad `prereview-646.md`，决定表 R1–R9、C1–C5、B1–B6）；用户裁决 2026-11-15。
- 工作区：`/Users/shenghuikevin/dev/AI/poa-646-report`，分支 `feature/646-report-root-cause`，起于 `origin/main` `e974a744`。

## 目标与范围

全量 `lite-full-1114` 里 72/105 份答案的根因为空，来自评测侧 L2 启发式（总结里第一个词表名，命中否决类论断即置空）。本任务让产品报告直接给出一个结构化根因，换算侧读字段，不再猜。

**此改动由评测触发**：产品报告合同（C3 L2）为 ITBench-Lite 评测而改。产品其他消费方（web 事故页、复盘知识、`categorize_report`）本轮不读新字段、照常工作；web 显示另开 issue（见「下一步」）。

分两个 PR，一个合同条款一个，不 stacked：

- PR-A 产品：报告 `m0-report-v3` 加 `root_cause`，L2 文字与示例、校验与重试反馈、v2/v3 并存解析、eval 冻结 summary 加载器兼容旧键集。用户门。
- PR-B 评测侧：`answer.py` 读字段（C1–C3）、kind 归一与 C4 旁路、#647 词表排除、集成批选题命令。PR-A 合并后再开。

不做：#308、#498、HolmesGPT 对照；web/知识显示根因。

## 合同（r1）

用户裁决（2026-11-15）：

- R1：推翻 2026-11-12「报告格式不改」（[适配记录](2026-11-12-itbench-lite-adapter.md) 第 28 行）。L2 报告合同加根因字段；web/知识暂不显示（R9=a），PR 写明 `categorize_report` 等消费方忽略该字段。
- R2+R3+R4：单个 `root_cause` 对象或 null；`kind`、`name` 必填，`namespace` 可空；`kind` 自由文本，换算侧归一。
- R5+R6：键可省略（默认 null）；`inconclusive` 时由换算忽略，不判报告无效；`root_cause` 必须带非空 `evidence_ids`，只校验都是本 Run 交付过的 id（`unknown_evidence_id`），不做 fact 全套检查，不要 `target_refs`；重试反馈词表补对应原因。
- R7：升 `m0-report-v3`，解析同时接受 v2 与 v3；web、知识验收对旧报告照常；eval 冻结 summary 加载器按保存时的键集比对，`docs/evidence/eval` 下 100 份旧 summary 必须仍能加载，带测试。
- C3：报告原文有 `root_cause` 键（含 null）只读字段、不用 L2 兜底；原文无该键（旧轮次）走 L2；`conversion_meta` 记 `method=structured|heuristic`，分数表注明。
- 集成批与门：见下节。提示词在集成批上最多修订 2 次，仍不过就停下报 lead。

Agent 自定（按预审推荐，各一行理由）：

- C1 = a：根因实体单独生成，从 L1 实体里删同名（不区分大小写），其余 L1 实体一律 `contributing_factor: false`。理由：按全名去重会在 kind 不同时留下一真一假两个同名实体。
- C2 = a：根因名不在词表照写，`conversion_meta` 记 `root_cause_in_vocabulary`；namespace 为空时集群级 kind 留空段，其余用 `DEFAULT_NAMESPACE`。理由：词表只服务文本匹配，不是合法性判据。
- C4 = a：词表加「名字 → 快照中出现过的 kind 集合」旁路文件，唯一 kind 直接用，多个再走现有规则。理由：快照属性开跑前已知、不碰答案；与 #647 同批重生成 35 份词表。
- C5 = a（#647 一并做）：收 Namespace 对象名 ∪ 所有对象的 `metadata.namespace` 值，从词表按字符串排除，不论类型；换算侧排除保留兜底；显式根因字段不受此排除。理由：只收 Namespace 对象会漏掉快照里无 Namespace 对象但有其下对象的情况。新词表只用于下一轮。
- R8 = a：`root_cause` 结构错误按现有机制判整份 `REPORT_INVALID`。理由：「报告要么合法要么拒收」是现有合同，R4–R6 的宽松选项已把新增失败面压到键名写错、缺 kind/name、引用未交付 id。
- 实现细节（PR-A）：v2 报告带 `root_cause` 键不额外拒收（不新增 `REPORT_INVALID` 来源）；eval 冻结 summary 的 `report_parsed` 在原文无 `root_cause` 键时不输出该键，旧 summary 字节不变。

## lead 的三条判断

1. 防编造门（门 ④）：结构字段可能诱导模型硬填根因、少写 `inconclusive`。集成批记录 `inconclusive` 比例与各结论分布，与全量对比；`inconclusive` 明显下降而分数不升 → 判不过，停下看 trace。
2. 改动由评测触发（见上「目标与范围」），对外照实写。
3. 提示词版本变化后，旧产品证据（钉 `prompt_revision` 的真实 Run 证据）不直接可比；新旧结果分开报。

## 集成批（跑前写死）

- 选题：按 [tiers.json](../evidence/itbench-lite-sre-lite-full-1114/tiers.json) 两个覆盖档各 4 题，每档按 `sha256("646:" + 题号)` 十六进制升序取前 4；题号即 tiers.json 的键（如 `S12`）。盐值 `646:`。
  - 指标+trace：S12、S5、S14、S34
  - 故障窗无 trace：S2、S8、S105、S22
- 每题 2 次，共 16 Run；判分照跑，分数只记录不作门。
- 门：① `REPORT_INVALID`（含产品拒收）≤ 1/16；② 产品接受且结论非 `inconclusive` 的报告里 ≥ 80% 根因字段非 null；③ 换算出的根因无命名空间名，kind 归一或原样保留，换算 `reason` 全空；④ 防编造：见上。
- 门 ④ 对照基线（冻结 summary 的 `run.report.conclusion`，105 份）：supported 70、partial 35、inconclusive 0。基线 `inconclusive` 为 0，「下降」不会发生；门 ④ 实际看的是结论分布是否明显移向 supported 而分数不升。
- 全量时这 8 题单列，另报其余 27 题。
- 主机由用户在 Vultr 开；执行者不自行连主机。

## 执行进展与证据

- 2026-11-15：任务记录建立；worktree 起于 `e974a744`。

## 下一步与交接

- PR-A 实现；合同/验收测试由独立 Agent 编写，lead cherry-pick 进本分支，实现者不改其断言。
- 开 issue：web 事故页显示结构化根因（R9 后续）。
