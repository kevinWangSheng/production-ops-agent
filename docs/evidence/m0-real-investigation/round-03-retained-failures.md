# M0-03 合同失败保留（分母）

审查者为任务协调者亲自核对；计划中的独立子代理因宿主 402 未完成，不把本文件标成独立 Agent 审查。只读 `result-business.json` 与 `m0-03-request-ledger.json`。未读 `.env` / `private-protocol/`。不改写原结果。

账本 `tmp/m0-environment/m0-03-request-ledger.json`：allocation `m0-03-20260911-report-facts`，fault phase HTTP 限额 8，实际 8 次 HTTP 200 后停止。trace 0。

| Run | HTTP | 工具 | 机器状态 | 原因 | 处置 |
|---|---|---|---|---|---|
| m003-fault-01 | 4 | 20 | incomplete | `FACT_SCOPE_REQUIRED`：fact/counter/rejected 的 `time_scope_ref` 为 null（16 项校验错误） | 失败保留 |
| m003-fault-02 | 4 | 17 | incomplete | `FACT_TIME_SCOPE_NOT_DELIVERED` | 失败保留 |
| m003-fault-03 | 0 | 0 | failed | `phase HTTP budget`；`error_type=InternalServerError` 是包装，边界码是预算拒绝 | 失败保留 |

前两份仍产出了 `m0-report-v2` JSON 正文（stop），但严格入口拒绝，不得记 `investigation_returned` 或质量候选。第三份无正文，属于同一账本 fault 8/8 用尽后的正确拒绝，不是模型能力证据。

这三份计入 M0-03 分母，不删除、不重跑、不回写成通过。后续 `m003b` / `m003c` 是新账本，不抵消本表。
