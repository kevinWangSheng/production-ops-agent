# M1-01 第一步：真实 Run 对账

本次完成两种请求体来源：

- **A 离线重建**：按 `docs/evidence/m1-01-e-class-attribution/scripts/rebuild_offline.py` README 的用法，从 `m1-01-alignment-c-effect` 的五个 ledger 形态重建 6 条请求。`normal-2`、`fault-2` 的重建 SHA-256 与 ledger 记录一致；另外 4 条因导出 ledger 将历史 `reasoning_content` 替换为占位符而不一致，仍与对应 `usage.prompt_tokens` 配对，均已标注 `hash_match=false`。原始结果见 [`offline-results.json`](offline-results.json)。
- **B 真实 DeepSeek**：使用项目既有 `M0_ENV_FILE` 读取方式和 `deepseek-flash`，`max_tokens=1`，共 9 次（其中 3 次来自本轮中断前已完成的结果，未重复调用；新增 6 次）。请求材料来自 A 的重建消息，带 tools 3 次、不带 tools 6 次，覆盖约 1k、10k、100k、300k、600k、900k token。原始请求哈希、usage、计数见 [`live-results.json`](live-results.json)；汇总见 [`reconciliation.json`](reconciliation.json)。

旧估算为 `estimate_tokens`（规范 JSON 字节 × 0.25，每消息加 4；tools 数组按字节 × 0.25）。tokenizer 计数按 `opspilot/tools/tokens.py` 的 vendored tokenizer，对每条消息逐条计数，另计规范化 tools 数组；`delta_pct=(tokenizer_content-prompt_tokens)/prompt_tokens`。

| 来源 | 条数 | prompt_tokens 范围 | tokenizer/prompt 偏差范围 | 说明 |
|---|---:|---:|---:|---|
| A 离线重建 | 6 | 74,703–251,805 | +1.8%–+6.8% | 2 条 hash exact；4 条占位符重建 |
| B 真实调用 | 9 | 2,328–902,379 | +4.3%–+26.1% | 其中 3 条中断前结果约 366k，未重复 |

新增长度梯度（B）明细：

| case | tools | old estimate | tokenizer content | prompt_tokens | tokenizer/prompt | fitted message overhead |
|---|---:|---:|---:|---:|---:|---:|
| live-01 | 否 | 2,863 | 2,463 | 2,328 | 1.058 | -45.0 |
| live-02 | 是 | 14,803 | 12,934 | 12,399 | 1.043 | -178.3 |
| live-03 | 否 | 124,465 | 108,889 | 101,388 | 1.074 | -2,500.3 |
| live-04 | 是 | 370,993 | 324,670 | 302,559 | 1.073 | -7,370.3 |
| live-05 | 否 | 738,223 | 646,047 | 601,368 | 1.074 | -14,893.0 |
| live-06 | 是 | 1,107,311 | 969,092 | 902,379 | 1.074 | -22,237.7 |

拟合采用 `prompt_tokens = tokenizer_content + α × message_count`，并将 tools 数组 tokenizer 值计入 `tokenizer_content`。全体 15 条的中位 α 为 **-1,913.8 token/消息**；负值表明当前“规范 JSON tokenizer 计数”系统性高于供应商计数，不能把它解释成正的协议开销。B 的原始 tokenizer 偏差为 **+4.3% 至 +26.1%**，超过 ±10%；A 的占位符重建结果不能消除该结论。

**结论：待决。** tokenizer 计数与供应商 `prompt_tokens` 未在 ±10% 内（B 的大请求约 +7.4%，但中断前 3 条约 +26% 且整体范围越界），按合同停在第一步，不进入产品代码或合同测试。需要先决定供应商计数与 vendored tokenizer 的消息序列化/特殊 token 对齐方式，再重新拟合每消息开销及 tools 固定开销。

费用/调用：本轮共 9 次真实模型调用，prompt 合计 **3,023,328**，completion 合计 **9**；未做余额前后快照，未写凭据。
