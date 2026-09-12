# B6 上游 Holmes 与候选同条件比较合同

状态：**session 内已授权，准备执行；总技术上限 4 个模型 HTTP，费用记录上界 8 CNY。** 这是独立于 B4 的新 allocation，不改历史账本。

## 范围

- 固定 HolmesGPT `5e983c17f30e93099c7d775167266d4cd1d586c4`、OpsPilot 当前候选代码、`deepseek-v4-flash`、thinking/high、相同公开 fixture/问题、相同只读工具范围、相同 max 2 steps、相同 Run deadline。
- 上游与候选各执行 1 个 Run；每个最多 2 个模型 HTTP，单 Run 费用记录上界 2 CNY，总上界 8 CNY（含失败/unknown 预留）。
- 不上传新 trace；使用 B4 已验证的 trace 白名单协议作为出口基线。凭据只在可信客户端认证渠道使用，不进入 prompt、报告或工件。
- 失败、timeout、工具缺失、依赖缺失均保留原始 stdout/stderr 与 ledger，不重跑补分。

## 判据

候选与上游分别记录：HTTP/工具次数、finish 状态、可观察报告/交接、步骤数、错误类型、耗时和 token usage。不可匹配的模型参数、工具面、数据窗口或输出协议逐项披露，不计算混合质量分数。该合同只验证比较方法可执行，不打开 SPEC gate、不宣称泛化或生产能力。

## 停止条件

达到 4 HTTP、8 CNY、Run deadline、数据出口不匹配、或任何凭据/私有字段可能出站时立即停止，并将状态记为 failed/unknown。
