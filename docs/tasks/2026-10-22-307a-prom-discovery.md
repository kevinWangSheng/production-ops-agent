# #307 A：Prometheus 指标发现工具

- 状态：实施中（合同 r1）
- 更新日期：2026-10-22
- 依据：#307 A 部分；C3 §5、§8；PRODUCT-CONSTRAINTS；F3 合同 U1-b / PR-3a D3；F7 合同；ADR-0006；上游 HolmesGPT `a0009195468ebc55a7dff7d602b61feabe3cda1b` `holmes/plugins/toolsets/prometheus/prometheus.py`、`prometheus_instructions.jinja2`
- 工作区：`/Users/shenghuikevin/dev/AI/poa-307a`，分支 `feature/F3-307-prom-discovery`（起点 main `a4eea42`）

## 目标与范围

调查模型目前只能用 `metrics_range_query` 和 `traces_search`，无法列出指标名和标签值，只能靠工具描述里写死的名单或猜测。本任务照搬上游 `get_metric_names` / `get_label_values` 的设计，新增有界的只读发现工具：过滤参数必填，结果条数与字节有上限，时间窗有默认值，超限时明确标记结果不完整。新工具纳入 F7 的只读校验、目标绑定、脱敏与审计。

不在范围：#307 B（Kubernetes 只读与日志工具）、新凭据或连接器、报告 schema 改版。

## 前提与完成条件

- 前提：lab 只读代理已放行发现端点（`scripts/kind_lab.py:328-365`），不需要新凭据；kind 与另一个会话共用，真实 Run 前确认没有观察窗在跑，并查 `vm.swapusage`。
- 完成条件：lab 真实 Run 中模型调用发现工具并拿到有界结果（附 LangSmith trace 与 `summary.json`）；F7 审计与脱敏覆盖新工具；全新上下文 Agent 编写的验收测试全部通过；Codex 独立审查的发现已处置；功能 PR，合并前给用户简报。

## 合同（r1）

完整决定表见 [2026-10-22-307a-prom-discovery-contract.md](2026-10-22-307a-prom-discovery-contract.md)。U1、U2 由用户 2026-10-22 裁决，U3、U4、R1–R12 按用户 2026-10-17 常设指示采纳推荐。

- U1-a：只做 Prometheus 发现；#307 审计评论扩出的 trace tag 发现和去掉写死枚举另开 issue。
- U2-a：保留 D3-a 的 KSM 名单，metrics 描述加一句先发现再查询的引导；名单去留等本 PR 合入 main 后由 F3 第 5 步评测决定。
- U3：两者都在 PR-3b 之前合并，后合并的一方 merge main 吸收对方；被钉住的合同测试由独立测试作者改。
- U4-a：lab Run 提问不点名工具；模型至少一次调用发现工具，结果 ok 或 no_data、≤100 条、有证据、不完整标记与 warnings 一致。
- R1–R12：两个工具照搬上游名；match、时间窗、label 校验、凭据样式 label 拒绝、limit=100、warnings 作不完整标记、exact bytes、错误分类扩到 Prometheus 类工具、auth 与 scope 从注册派生、引导写进工具描述、fixture 不改。

## 执行进展与证据

- 2026-10-22 合同预审：Codex 三个模型 5 轮均返回 at capacity，改由全新上下文 Claude Opus 5.5 只读子 Agent 完成；r1 写入。

## 下一步与交接

预审完成 → 按推荐裁决写入 r1 → Opus 5.5 子 Agent 实现 ∥ Codex gpt-6.1-sol 按合同写验收测试 → lab 真实 Run → Codex astra 独立审查 → PR。
