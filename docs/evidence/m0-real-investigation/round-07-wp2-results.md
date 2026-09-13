# 工作包 2：真实协议与 compressor 接续结果

执行日期：2026-09-12。沿用既有 round-06 隔离 probe 合同，不修改主 transport；本轮新增 1 个 compressor 真实 HTTP，0 trace。stream/工具错误的隔离 provider 证据仍见 `round-06-protocol-results.md`。

## Context compressor 真实 Run

- `m0-compressor-v1` 对 14 条消息（6 个 assistant/tool group）折叠 4 组，输出 7 条消息；`pairing_count=2`，tool_call/evidence id 保持成对，provider reasoning 未进入压缩摘要。
- 压缩前估算 664 tokens，压缩后 365 tokens；阈值 332、`keep_groups=1` 导致 `over_threshold=true`，因此这是“超阈值触发并保持配对、provider 接受”的部分证据，不是压缩后必低于阈值的保证。
- 真实 DeepSeek HTTP 200，reported model `deepseek-flash`，`finish_reason=stop`，usage 321/447/768；trace uploads 0。
- 原始摘要/哈希：[`round-07-compressor-real-run.json`](round-07-compressor-real-run.json)，stdout/stderr 同名文件；ledger [`round-07-wp2-ledger.json`](round-07-wp2-ledger.json)。

## 结论

工作包 2 目前为**部分**：隔离 provider probe 已证明 stream 中断、工具错误续接和 compressor paired transcript 的协议可达；但 stream/工具错误尚未写入产品 StepStore/PG 审计，compressor 的 threshold floor 仍可能保持 over-threshold，未形成完整产品兼容证明。SPEC gate 与 feature passes 不变。
