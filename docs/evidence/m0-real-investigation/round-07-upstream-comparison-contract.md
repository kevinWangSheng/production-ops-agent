# B6 round-07 上游与候选同工具面比较合同

状态：**已执行；每 Run 最多 2 个模型 HTTP，本项实际 8 HTTP / known upper 0.228585 CNY；不上传 trace；未启动 m0-otel。**
日期：2026-09-12。工作区 `production-ops-agent-m0-07-wp5`（分支 `chore/m0-07-wp5`，基线 `c618651`）。

## 问题与根因

round-06 两次上游复验（[`round-06-upstream-comparison.md`](round-06-upstream-comparison.md)）中，固定 HolmesGPT CLI 只暴露上游默认 toolset（bash/kubectl/curl 等），模型生成未执行的 shell tool-call，没有最终报告。上游与候选工具面不同，不能比较。本轮把两方置于**同一只读工具面**：候选 strict proxy 的四个 evidence view 查询（`otel_services`、`otel_metrics`、`otel_logs`、`otel_traces`）以 Holmes `Tool` 形式提供给上游循环，禁用全部默认 toolset、skills、shell/curl/kubectl。

## 固定条件（两方相同）

| 维度 | 值 |
|---|---|
| 模型 | `deepseek-v4-flash`（OpenAI 兼容端点 `https://api.deepseek.com/v1`），`thinking.type=enabled`，`reasoning_effort=high`，`max_tokens=8192`，`temperature` 由各自默认（上游 0.00000001，候选不设） |
| 输入视图 | 已提交的 M004 developer observations：[`m004-normal-observation.json`](../m0-real-environment/m004-normal-observation.json)、[`m004-fault-observation.json`](../m0-real-environment/m004-fault-observation.json) 及其 raw-evidence（7/7 hash 已独立复核）。由 `scripts/m0_lab/round07/build_packet.py` 投影为 replay packet，视图 hash 记入 packet |
| 时间政策 | 固定窗口 normal `1789147272..1789147572`、fault `1789147639..1789147939`（与 M004 相同）；工具不接受 start/end 参数 |
| 工具面 | 4 个只读 replay 工具，只返回 packet 内冻结视图；不在 replay 集内的查询返回错误并列出可用查询；无 shell、无网络、无注入器 |
| 预算 | 每 Run 最多 2 个模型 HTTP（第 2 个为无工具最终请求）、最多 20 次工具调用、每 HTTP 360 s、Run 900 s、每 HTTP 1 CNY 预留 |
| 出口 | 仅允许 `POST https://api.deepseek.com/v1/chat/completions`；其它 HTTP 拒绝；trace 上传 0 |
| 凭据 | 受信任启动器从主工作区私有 `.env` 读取 `DEEPSEEK_API_KEY`，仅存于进程内存/子进程 stdin；不打印、不落盘、不进入 prompt/报告/工件 |
| 场景 | M004 normal 与 M004 fault，各方各 1 Run，共 4 Run |

## 已知差异（逐项披露，不计混合分数）

| 维度 | Holmes 上游 | OpsPilot 候选 | 处置 |
|---|---|---|---|
| 循环 | 固定 checkout `5e983c17` 的 `ToolCallingLLM.call_stream`，LiteLLM 1.89.0 | 直接 HTTP（stdlib `urllib`），单进程有界循环 | 只比可观察报告；不比步骤实现 |
| system prompt | 上游 `build_system_prompt` 默认模板 + 本 toolset 描述 | 候选调查纪律文本 + 报告协议 | prompt 文本与 hash 均记录 |
| 报告协议 | 上游自由 Markdown | `m0-report-v1` JSON（`schema_version/assessment_status/conclusion/claims[evidence_ids]/gaps/next_steps`）；未使用 M004 的 v2 strict context（需 registry/time policy 绑定），差异披露 | judge 按 claim 对照，不比格式 |
| 上下文管理 | 上游 token 计数/compaction 关闭 | 无 | 相同视图，字节相同 |
| M004 原候选 Run | — | M004 原报告由 strict v2 harness 在 3 HTTP/9–12 工具下产生 | 本轮候选是 2 HTTP 重跑，不是 M004 原报告的复制 |

## 判据与停止条件

- 记录每 Run：HTTP 次数、状态码、usage、finish_reason、工具调用次数与名称、最终报告或失败原因、耗时、原始响应 hash。
- 失败（无最终报告、协议错误、超预算、DSML 文本工具调用）进入分母，不重跑补分。
- 结果只做逐 claim 并列表与差异披露；**不得出候选优于/劣于上游的结论**，不打开 SPEC gate，不改 feature passes。
- 达到 8 HTTP、任一 Run 超 2 HTTP、任何凭据/私有字段可能出站、或 deadline 时立即停止并记 failed/unknown。
