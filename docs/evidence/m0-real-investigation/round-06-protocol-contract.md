# 工作包 2：真实协议补证合同

状态：**session 内已授权，执行前记录；总上界 6 个模型 HTTP、6 CNY 预留；不上传 trace。**

## 固定边界

- 固定模型 `deepseek-v4-flash`、thinking/high 和 DeepSeek endpoint；凭据只由受信任 probe 进程从私有 `.env` 读取，不进入 prompt、stdout、报告或 trace。
- 三个独立 probe 各 1 个 Run：流式中断（最多 2 HTTP）、工具 4xx 后续接（最多 2 HTTP）、上下文压缩配对（最多 2 HTTP）。失败、超时、协议不匹配进入分母，不重跑补分。
- probe 只保存状态、usage、hash、finish reason、tool-call id 等脱敏摘要；原始 provider bytes 不写入提交树。任何凭据/私有 reasoning 出站立即停止。
- 这是工作包 2 的真实 provider 协议实验，不改产品 transport 合同、不修改 feature passes/SPEC gate；当前生产 transport 明确固定 `stream=false`，流式 probe 与产品路径隔离。

## 分项判据

1. **流式中断**：首个非空 SSE chunk 后主动关闭连接；partial 不进入 accepted response；重试使用新 request identity，计入预算；记录 `complete=false`、审计摘要和第二次响应状态。若 provider 不返回可解析 SSE，标证据不足。
2. **工具错误后续接**：第一请求返回真实 tool call；固定只读工具返回 4xx 原文（不含凭据）；第二请求带完整 `assistant.tool_calls` 与 `tool` 错误消息续接；核对 tool_call_id 配对与错误摘要 hash。
3. **上下文压缩配对**：隔离 probe 对固定历史生成 deterministic 压缩副本，保留 assistant/tool 成对消息并记录 compressor hash、前后大小；真实请求只验证 provider 接受该配对视图。当前产品无 compressor 时不得写成产品机制通过。

费用上界：每项 2 CNY 预留，合计 6 CNY；真实账单仍 unknown。执行结束后保留 ledger/结果，停止任何临时服务。
