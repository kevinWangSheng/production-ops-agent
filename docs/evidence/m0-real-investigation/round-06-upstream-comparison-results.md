# B6 上游 Holmes 与候选比较结果

执行日期：2026-09-12。依据 [`round-06-upstream-comparison-contract.md`](round-06-upstream-comparison-contract.md)。本轮没有上传新 trace；候选与上游使用独立记录，失败不重试补分。

## 结果

| 对象 | 状态 | HTTP | 结果 |
|---|---|---:|---|
| OpsPilot candidate | **部分通过** | 2 | 两次 DeepSeek HTTP 200；固定 fixture/PG 步骤重建与最终合同完成。见 `round-06-candidate-first.txt`、`round-06-candidate-second.txt`。 |
| Holmes upstream | **证据不足/阻塞** | 0 个确认成功请求 | 第一次缺 LiteLLM provider 前缀；第二次缺 `DEEPSEEK_API_KEY`；第三次空 Bearer header 在客户端/provider 前置校验失败。完整 stdout/stderr 保留。 |

候选 ledger [`m0-06-b6-ledger.json`](m0-06-b6-ledger.json) 的 2 次模型请求 token 分别为 403、453，总已知成本上界 0.004086 CNY；ledger SHA-256 为 `150c9d34d775e0b8950fd2aacd255301f166cf56e939a1e2674e7eafcdba46c9`。每次 reservation 2 CNY，未超过合同 8 CNY。没有足够的上游真实样本，不能计算同条件质量差异、非退化或收益结论。

## 差异与处置

- 候选使用已审核的 PG/fixture probe；上游 Holmes 使用固定 checkout，但当前上游环境没有可用 DeepSeek 认证变量，且未启动新的 OTel 环境。
- 缺失凭据不是工程 Agent 可以猜测或从其他来源复制的内容；不继续重试，不把空 Bearer 错误写成供应商 HTTP 失败。
- B6 的 LangSmith trace 出口已在 B4 三个 Run 中独立验证；本轮上游比较不新增 trace，避免在比较样本不足时扩大数据出口。

## 认证线路后续复验

在用户确认 session 费用授权后，另立 [`round-06-upstream-auth-followup-contract.md`](round-06-upstream-auth-followup-contract.md) 做了 1 次无工具认证/线路复验。固定 Holmes checkout 已返回模型内容，未再出现缺 provider 或空 Bearer 错误；但启动器没有持久化 wire HTTP 状态/usage，且模型输出了未执行的 shell tool-call，故仅记为“认证/线路部分通过”，不计入正式同条件质量样本。原始输出和 hash 见 [`round-06-upstream-auth-followup-results.md`](round-06-upstream-auth-followup-results.md)。

## 结论

B6 当前为**候选协议链路真实通过、上游认证线路部分通过但基线证据仍不足**。费用授权已明确，无需再次等待费用确认；正式质量比较仍需一个受控的上游最终报告 Run，并在完成后再做人工校准/盲测。不修改 feature passes，不打开 SPEC gate。
