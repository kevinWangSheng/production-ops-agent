# fault-2：`REPORT_INVALID` 交接的离线定位（执行者记录，非独立审查）

- Run `86fe860c-699f-5694-a752-65d9e671412a`，第 4 轮 `finish_reason=stop`，报告正文 13,541 字节，`parse_report` 离线复跑通过（`m0-report-v2`、14 条 claim）。摘要同样把 checkout 失败归到 payment 依赖（PlaceOrder gRPC 13，「Payment request failed. Invalid token」），并列出 8 条 trace id。
- 交接原因与前两次不同：14 条 claim 引用的 evidence id 全部是**截断形式** `<step_id>-tN`（例如 `12225891-2ac0-49cd-b065-ac1b3b83df80-t0`），而本 Run 交付的 id 形如 `<step_id>-tN:<uuid>`；`unsupported_citations` 第一条规则（`eid not in by_id`）即拒绝。14 条证据行中还有 `no_data` 视图，但本次没走到那一步。
- 产品行为：未发布，Run `waiting_human`，事故开放（ADR-0005）。按 v4 判据 1，本 Run 不通过；不重跑替换。
- 是模型引用错误还是模型可见面诱导截断（例如工具结果或报告合同中 id 的展示形式），由独立审查判断。
