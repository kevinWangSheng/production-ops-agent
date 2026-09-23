# B1–B3 loop follow-ups：有界真实 Flash Run

- 日期：2026-09-23
- 授权：AGENTS.md「费用与真实调用」——真实模型、trace 和有界真实软件环境调用已由用户常设授权。
- 驱动：`scripts/m1_live_flash_loop.py`（产品 `InvestigationLoop` + fixture 工具，无真实 OTel），
  与 2026-09-16 的 [`docs/evidence/m1-01-investigation-loop/`](../m1-01-investigation-loop/run.md) 同一脚本，
  单独跑一次并存到本目录，避免覆盖已提交的历史 Run 证据。
- 目的：`fix/m1-01-loop-followups` 分支（B1 恢复重放校验、B2 工具轮 reasoning_content 前置拒绝、
  B3 模型 4xx 分类）改动了 `opspilot/investigation/{runner,loop,client}.py`；AGENTS.md 要求触碰
  loop/恢复路径的 PR 附至少一次有界真实 Run 的 ledger 与结果。
- 原始账本：[`ledger.json`](ledger.json)；解析后的报告：[`report-parsed.json`](report-parsed.json)。

## 结果

| 项 | 值 |
|---|---|
| 请求名 / 回报名 | `deepseek-flash` / `deepseek-flash`（两轮均精确匹配） |
| HTTP | 2（第 1 轮 `tool_calls`，第 2 轮 `stop` + `json_object`） |
| wall | 约 11.2 s（09:25:25Z–09:25:37Z UTC） |
| tokens | prompt 2486（第 2 轮 cache hit 512）、completion 2391（含 reasoning 44+863） |
| 峰值上界费用 | 0.026389 CNY（input $0.3/1M、output $1.2/1M、7.3 CNY/USD） |
| 执行 | `completed`，`handoff=true`，原因 `INCOMPLETE_INVESTIGATION` |
| 报告 | `m0-report-v2`，`assessment_status=incomplete`，`conclusion=inconclusive`，6 条 claims（fact×3、counter_evidence、hypothesis×2） |
| 证据 | 1 条 `evidence_id` |
| 步骤提交 | 2（`round-1` 工具结果已提交，`round-2` 响应已提交） |

## 判定

1. 本分支改动的三条路径（`runner.py` 的恢复重放前置校验、`loop.py` 的工具轮
   reasoning_content 前置拒绝、`client.py` 的 4xx 分类）都不在这次在线单轮 Run 的
   触发条件内——本 Run 没有恢复、没有缺 `reasoning_content` 的工具轮、没有遇到
   非 2xx 状态码——所以这次真实调用只确认了改动后 `InvestigationLoop` 主链路
   （组装上下文 → 真实模型 → 提交步骤 → 执行只读工具并配对 → 最终 json 报告）
   仍然端到端走通，不构成对 B1/B2/B3 本身分支条件的真实触发证据。
2. B1/B2/B3 的行为证据来自对应单测与 `M1_DURABLE_POSTGRES=1` 下的 PostgreSQL
   集成测试（见任务提交记录），本 Run 是 AGENTS.md 要求的「触碰 loop/恢复路径的 PR
   附至少一次有界真实 Run」的合规证据，不是这三个缺陷本身的复现实验。
3. 产出的是 v4/`m0-report-v2` 结构化报告，不是散文；工具面是 fixture，报告正确地
   区分了「完成但不确定」与「完成」，未把单点样本冒充为确诊。
4. 本 Run 不是产品验收、不是 M0 通过、不改 feature `passes`。

## 未验证

多轮超过 2 HTTP、真实数据源、PG DurableStore 与本 loop 的集成（该集成已由
`M1_DURABLE_POSTGRES=1` 下的确定性 PostgreSQL 测试覆盖，不是本次真实模型 Run 的
范围）、流式续接、intake 接线、UI。
