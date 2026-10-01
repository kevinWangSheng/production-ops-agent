# M1-01 第一步：真实 Run 对账

运行命令：

```text
python3 docs/evidence/m1-01-token-estimator/reconcile.py
```

脚本只接受同一证据对象同时包含完整 `messages`（每项至少有 `role`、`content`）、完整 `tools` 数组和整数 `usage.prompt_tokens` 的记录。对合格记录会计算：

* 旧值：`opspilot.investigation.context.estimate_tokens(messages, tools)`，即规范 JSON 字节 × 0.25，加每消息 4 token；
* vendored tokenizer：对每条消息的规范 JSON 和规范化 tools 数组分别调用 `opspilot/tools/tokens.py`；
* 两个比值：旧值 / 供应商值、tokenizer 内容值 / 供应商值；
* 固定每消息开销拟合：`median((prompt_tokens - tokenizer_content_tokens) / message_count)`。

脚本不联网、不调用模型；原始扫描结果在 [`reconcile.json`](reconcile.json)。本次运行结果为 **0 条合格记录、1,211 条只有 usage/哈希/字节数或摘要的记录**。因此没有可诚实计算的三列数值、比值或每消息开销；README 不把请求字节数当作请求体，也不从哈希重建消息。

## 可用性抽样

下表是扫描到的真实供应商 usage 记录，展示小、中、大上下文覆盖；三列对账值和两个比值均为 `N/A`，因为完整请求体未保存。

| 来源（对象路径） | 供应商 `prompt_tokens` | 请求字节 | 旧估算 | tokenizer 计数 | 旧/供应商 | tokenizer/供应商 |
|---|---:|---:|---:|---:|---:|---:|
| `m0-06-b6-ledger.json $.attempts[0]` | 331 | 537 | N/A | N/A | N/A | N/A |
| `m0-06-b6-ledger.json $.attempts[1]` | 272 | 1,210 | N/A | N/A | N/A | N/A |
| `m004-fault-candidate/result-business.json $.attempts[0]` | 1,416 | 5,655 | N/A | N/A | N/A | N/A |
| `m004-fault-candidate/result-business.json $.attempts[1]` | 8,395 | 25,540 | N/A | N/A | N/A | N/A |
| `m004-fault-upstream/attempts.json $[1]` | 10,372 | 33,407 | N/A | N/A | N/A | N/A |
| `m004-normal-upstream/attempts.json $[1]` | 10,074 | 32,432 | N/A | N/A | N/A | N/A |
| `m1-01-e-class-attribution/ledger.jsonl`（样本） | 72,584 | 252,143 | N/A | N/A | N/A | N/A |
| `m1-01-e-class-attribution/ledger.jsonl`（样本） | 163,022 | 524,869 | N/A | N/A | N/A | N/A |
| `m1-01-e-class-attribution/ledger.jsonl`（样本） | 251,805 | 839,273 | N/A | N/A | N/A | N/A |
| `m1-01-view-bytes-timeout/ledger.json $.requests[5]` | 661,798 | 1,667,666 | N/A | N/A | N/A | N/A |
| `m1-01-view-bytes-timeout/decode-64k-at-975k.json $.requests[1]` | 975,182 | 2,457,471 | N/A | N/A | N/A | N/A |

大上下文记录确实存在（`661,798` 和 `975,182` prompt tokens），但这些对象只有 usage/request_bytes；`m1-01-e-class-attribution` 另有 `request_sha256`、`message_count` 和 placeholder reasoning 说明，仍没有 messages/tools 正文。唯一带 `messages` 字段的 `m1-01-loop-long-horizon/compaction-smoke.json` 只保存角色名数组，并明确写明 prompt/response 文本未保留，不能作为完整请求。

## 结论与待决

本步骤无法从仓库现有证据估计 tokenizer 与供应商 `prompt_tokens` 的偏差范围，也无法判断是否在 ±10% 内；每消息开销没有可拟合值。按合同要求在此停下，不能进入第二步。待决是补存至少 10 个真实请求的完整 `messages` 与 `tools`（连同同一请求的 `usage.prompt_tokens`），覆盖小/中/大上下文后再运行脚本；在此之前不应改 `estimate_tokens`。
