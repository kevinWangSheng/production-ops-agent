# M1-01 Flash 调查 loop 有界真实 Run

- 日期：2026-09-16
- 授权：用户对本任务明确授权少量真实 DeepSeek 调用，走现有合同/ledger 记账。
- 驱动：`scripts/m1_live_flash_loop.py`（产品 `InvestigationLoop` + fixture 工具，无真实 OTel）。
- 原始账本：[`ledger.json`](ledger.json)；解析后的报告：[`report-parsed.json`](report-parsed.json)。

## 结果

| 项 | 值 |
|---|---|
| 请求名 / 回报名 | `deepseek-flash` / `deepseek-flash`（两轮均精确匹配） |
| HTTP | 2（第 1 轮 `tool_calls`，第 2 轮 `stop` + `json_object`） |
| wall | 约 11.2 s（14:45:31Z–14:45:42Z） |
| tokens | prompt 2225（第 2 轮 cache hit 512）、completion 2254（含 reasoning 118+637） |
| 峰值上界费用 | 0.024617 CNY（input $0.3/1M、output $1.2/1M、7.3 CNY/USD） |
| 执行 | `completed`，`handoff=true`，原因 `INCOMPLETE_INVESTIGATION` |
| 报告 | `m0-report-v2`，`assessment_status=incomplete`，`conclusion=inconclusive` |
| 证据 | 1 条 `evidence_id`，claims 含 fact / counter_evidence / hypothesis / rejected_hypothesis / recommendation |
| 步骤提交 | 2（`round-1` 工具结果已提交，`round-2` 响应已提交） |
| ledger `run_id` | 本文件记录的是账本 UUID，不是 loop 的 `request.run_id`（当时为 fixture `run-9`）。脚本已改为写入 `request.run_id`，本 Run 未重放。 |

第 2 轮出现 `prompt_cache_hit_tokens=512`，与 DeepSeek 前缀缓存规则一致：稳定纪律/报告契约在前，本 Run 问题与工具结果在后。

## 判定

1. 产品 loop 端到端走通：组装上下文 → 真实模型 → 提交步骤 → 执行只读工具并配对 → 最终 json 报告。
2. 产出的是 v4/`m0-report-v2` 结构化报告，不是散文。incomplete + gaps + handoff 符合 v4「完成但不确定与未完成区分；不得把预算/连接失败冒充完成」。
3. 工具面是 fixture，故报告正确地拒绝把单点 `0.042` 升级为「错误升高」——这是调查质量，不是 loop 失败。
4. 本 Run 不是产品验收、不是 M0 通过、不改 feature `passes`。

## 未验证

多轮超过 2 HTTP、真实数据源、PG DurableStore 与本 loop 的集成、流式续接、intake 接线、UI。
