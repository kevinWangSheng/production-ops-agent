# normal-2：`REPORT_INVALID` 交接的离线定位（执行者记录，非独立审查）

- Run `f771d566-5ef2-5311-b7ce-71f1878c1e23`，第 4 轮 `finish_reason=stop`，报告正文 13,059 字节，`parse_report` 离线复跑通过（schema `m0-report-v2`、`assessment_status=completed`、`conclusion=partial`、17 条 claim）。
- 交接原因来自 loop 的引用绑定检查（`opspilot/investigation/loop.py` `_parse_report` → `opspilot/investigation/reports.py` `unsupported_citations`）：fact/counter_evidence/rejected_hypothesis 只能引用 `status == "ok"` 的已交付视图。
- 离线复算：claim 1（`fact`，「A metrics query for `traces_span_metrics_calls_total{status_code="ERROR"}` restricted to checkout, payment, … returned no_data」）引用的证据 `504f8012-3b98-4ba9-ae0d-dea919d2bb6b-t2:…` 在 `opspilot_evidence` 中 `status = no_data`（`adopted = true`）。其余 16 条 claim 的证据 id、`target_refs`、`time_scope_ref` 均合法。
- 产品行为：按 ADR-0005 未发布，Run `waiting_human`、事故保持开放，事件 23 `run_handoff`（`published: false`、`reasons: ["REPORT_INVALID"]`）。这是合同定义的正确处置，但按 v4 判据 1「无法调查可有准确 handoff，但不能抵作正常/故障正向能力通过」，本 Run 不通过；不重跑替换。
