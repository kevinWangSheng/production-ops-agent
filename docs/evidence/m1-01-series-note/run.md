# M1-01 `series_note`：有界真实故障 Run

- 日期：2026-10-01 10:42–10:47 UTC（本机 03:42–03:47 PDT）。代码：`feature/m1-01-series-note`，投影修订 `m1-01-tool-view-v7`。
- 场景：OTel Demo `paymentFailure` 开发故障钩子注入后提交 checkout 故障问题；web 与 worker 均使用 `OPSPILOT_TOOL_PROFILE=otel-demo`，目标 `m0-otel-20260909`。运行前后已恢复故障并停止 OTel lab、PostgreSQL；共享 lab 锁已释放。
- 运行边界：1 个事故 / Run，模型请求 6 次，工具调用 17 次，Run 状态 `completed` / `published`。原始 ledger 只保留在 PostgreSQL；导出为 [fault-1/ledger.json](fault-1/ledger.json)，报告为 [fault-1/report.json](fault-1/report.json)，事件为 [fault-1/events.jsonl](fault-1/events.jsonl)。
- 视图证据：ledger 中有 3 个包含 `traces_span_metrics_calls_total` 的成功 metrics 视图，均带逐字 `series_note`；拒绝/错误/其他指标视图不带。字段随 `view_sha256` 写入证据。
- 报告观察：模型报告明确把 `STATUS_CODE_UNSET` 作为唯一返回状态标签并指出 metric/trace disagreement、错误率未知；没有把 UNSET 计为成功数（观察通过，不是合同通过条件）。报告同时从 trace 证据识别 checkout 与 payment 的 `Invalid token` ERROR spans。
- 安全：ledger 导出使用 `idempotency_label` 约定，未写入禁止的幂等键字段形式；本地未安装 `tmp/gitleaks/gitleaks`，故未执行 gitleaks，已记录为未扫。
