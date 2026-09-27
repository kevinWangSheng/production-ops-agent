# fault-1：`REPORT_INVALID` 交接的离线定位（执行者记录，非独立审查）

- Run `017adaaf-ab1a-5f1f-846a-06cb1dc26a7b`，第 4 轮 `finish_reason=stop`，报告正文 12,621 字节，`parse_report` 离线复跑通过（`m0-report-v2`、`assessment_status=completed`）。摘要把 checkout 错误归到 checkout→payment `PaymentService/Charge` 调用（gRPC 13，「Payment request failed. Invalid token」），并列出 4 条错误 trace id。
- 交接原因与 normal-2 同一条规则（`reports.py` `unsupported_citations`：fact 类 claim 只能引用 `status == "ok"` 视图）：claim 7（`fact`）引用了两条 `status = no_data` 的证据 `fbc3c8eb-121…` 与 `070aa409-698…`。其余 claim 的引用、目标与时间策略合法。
- 产品行为：未发布，Run `waiting_human`，事故开放（ADR-0005）。按 v4 判据 1，本 Run 不通过；不重跑替换。
- 这是本包第二次因同一规则交接（normal-2、fault-1）：模型看到的工具结果对 `no_data` 视图标 `adopted: true` 且带 evidence_id，而 `REPORT_CONTRACT` 未告知 fact 类 claim 不得引用非 `ok` 视图（normal-2 审查 P3-1）。
