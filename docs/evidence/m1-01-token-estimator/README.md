# M1-01 第一步：真实 Run 对账

本次完成两种请求体来源：

- **A 离线重建**：按 `docs/evidence/m1-01-e-class-attribution/scripts/rebuild_offline.py` README 的用法，从 `m1-01-alignment-c-effect` 的五个 ledger 形态重建 6 条请求。6 条的第 1 轮输入快照哈希均校验通过，但**整条请求哈希全部不匹配**（`hash_match=false`）：导出 ledger 把历史 `reasoning_content` 替换为占位符，被替换的早期推理共 1,799–5,704 tokens，占对应 `prompt_tokens` 的 0.8%–2.9%。因此 A 是带占位符的重建，不是供应商请求的逐字节复现；对比值的影响见下文「结论」。（lead 更正：执行者初稿称 normal-2、fault-2 精确匹配，与 `offline-results.json` 不符。）原始结果见 [`offline-results.json`](offline-results.json)。
- **B 真实 DeepSeek**：使用项目既有 `M0_ENV_FILE` 读取方式和 `deepseek-flash`，`max_tokens=1`，共 9 次（其中 3 次来自本轮中断前已完成的结果，未重复调用；新增 6 次）。请求材料来自 A 的重建消息，带 tools 3 次、不带 tools 6 次，覆盖约 1k、10k、100k、300k、600k、900k token。原始请求哈希、usage、计数见 [`live-results.json`](live-results.json)；汇总见 [`reconciliation.json`](reconciliation.json)。

旧估算为 `estimate_tokens`（规范 JSON 字节 × 0.25，每消息加 4；tools 数组按字节 × 0.25）。tokenizer 计数按 `opspilot/tools/tokens.py` 的 vendored tokenizer，对每条消息逐条计数，另计规范化 tools 数组；`delta_pct=(tokenizer_content-prompt_tokens)/prompt_tokens`。

| 来源 | 条数 | prompt_tokens 范围 | 旧估算/prompt | tokenizer/prompt | 说明 |
|---|---:|---:|---:|---:|---|
| A 离线重建（真实 Run 请求，推理占位符） | 6 | 74,703–251,805 | 0.805–0.859 | 1.122–1.164 | 6 条请求哈希均不匹配，见上 |
| B 真实调用（合成填充） | 9 | 2,328–902,379 | 0.838–1.230 | 1.043–1.261 | 填充为重复的规范 JSON，形态不代表真实 Run |

（lead 2026-10-01 按 `offline-results.json`、`live-results.json` 原始字段复算并更正此表；执行者初稿把 A 的 tokenizer 偏差误写为 +1.8%–+6.8%。）

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

**结论（2026-10-01，用户决定不改代码）：**

- 「估算约少算 1.6 倍」不成立。真实 Run 请求的重建（A）上旧估算是供应商计数的 0.81–0.86；把被占位符替换的早期推理按 1.25 倍 token 计回估算（偏保守的上界），比值最多上移约 0.03 至 0.84–0.89。即旧估算少算约 11–20%；该值来自 2026-09-29 的推断（用 2.46 MB 对 975k tokens 的请求字节比反推），未直接测。
- 旧估算的少算由 `Calibration` 在每次响应后按 `usage.prompt_tokens` 上调（只升不降），首个响应后即补齐；风险只在一个 Run 的第一轮内，且第一轮上下文小。
- 直接用 vendored tokenizer 数规范 JSON 会多算 12–26%（含 JSON 键与转义），拟合出的每消息开销为负，说明要对齐供应商的消息序列化才能用；收益小于改动成本（每轮约 1 秒计数、`context_policy_revision` 变化挡旧 Run）。
- 处置：不改 `estimate_tokens`；本目录作为对账证据保留。会改变结论的证据：真实 Run 出现提供方拒绝超长请求，或首轮估算与 `prompt_tokens` 比值低于 0.75。

费用/调用：本轮共 9 次真实模型调用，prompt 合计 **3,023,328**，completion 合计 **9**；未做余额前后快照，未写凭据。
