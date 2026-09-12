# 工作包 2：真实协议补证结果

执行日期：2026-09-12。依据 [`round-06-protocol-contract.md`](round-06-protocol-contract.md)。共 5 个模型 HTTP，0 trace 上传；allocation 上界 6 CNY，已知成本上界 0.014244 CNY，首个中断请求 usage unknown 预留 2 CNY，未超上界。凭据只在受信任 probe 进程读取，结果工件仅含摘要/hash。

## 逐项结果

| 项目 | HTTP | 结果 | 证据路径 | 限定 |
|---|---:|---|---|---|
| 流式中断 | 2 | **部分通过（隔离 probe）** | [`stream-interrupt.json`](round-06-protocol-runs/stream-interrupt.json)；首请求 HTTP 200 后主动中断，未解析/采纳 partial；第二请求 HTTP 200 但 `finish_reason=length`。 | 当前产品 transport 合同固定 `stream=false`，未证明产品流式恢复/PG 审计；仅证明 provider 线路可被隔离 probe 中断。 |
| 工具错误后续接 | 2 | **部分通过（provider 协议）** | [`tool-error.json`](round-06-protocol-runs/tool-error.json)；首请求真实 tool call，第二请求带同 `tool_call_id` 与固定 404 错误消息返回 stop。 | 未接入 StepStore/PG，不能宣称产品 operation.status=`failed` 或审计持久化已通过；错误内容为无敏感固定文本。 |
| 上下文压缩后工具消息配对 | 1 | **证据不足/隔离 provider** | [`context-compression.json`](round-06-protocol-runs/context-compression.json)；手工构造 assistant/tool 成对视图 HTTP 200、stop。 | 仓库当前无 compressor；本次只验证 provider 接受手工配对视图，不证明压缩触发、淘汰策略或产品配对机制。 |

## 费用与原始输出

- 账本：[`round-06-protocol-ledger.json`](round-06-protocol-ledger.json)，5 HTTP、known cost upper 0.014244 CNY、unknown reservation 2 CNY、trace 0。
- 每个 JSON 的 SHA-256：`stream-interrupt` `f42fa2cd966afe236b62203456f39b626d2e10b526e19fc42fec3632fb05ef8b`；`tool-error` `8da0f90f37a3615aa96a59db5830a015cef049ddea6bb62f326a4d1b9d769c7f`；`context-compression` `dc2ac0997b10d21f856870024082b66c57a6cfa8cd947a00bef205f502a76636`。
- 失败/前置错误：首次直接脚本调用因缺 `PYTHONPATH` 未发 HTTP，原始错误摘要保留在 [`stream-interrupt-initial-failure.txt`](round-06-protocol-runs/stream-interrupt-initial-failure.txt)；修正为模块调用后才执行合同内请求。该脚本错误不计模型 HTTP。

结论：provider 真实协议与工具错误 continuation 已取得部分证据；产品 streaming/PG 审计和 context compressor 仍是明确缺口，不把本轮写成工作包 2 通过。
