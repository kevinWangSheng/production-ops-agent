# M1-01 交接不发布 + 真实驱动实时进度：有界真实 Flash Run

- 日期：2026-09-24
- 授权：AGENTS.md「费用与真实调用」常设授权。
- 驱动：`scripts/m1_live_runner.py`（`InvestigationRunner` + 本 worktree 的 lab PostgreSQL + `DurableEventLog` + `DurableEvidenceStore`，fixture 工具，无真实 OTel）。这是本仓库首次经真实驱动器（而非内存 loop）跑真实模型。
- 原始账本：各 `live-runs/<run_id>/ledger.json`（HTTP 尝试、tokens、费用上界、`run_usage`、每次尝试后的行快照、事件流）；`report.json` 为模型最后一份报告正文。不含凭据；`owner` 列只记「set/None」。
- 费用：4 次 Run 合计 0.080403 CNY 上界（input $0.3/1M、output $1.2/1M、7.3 CNY/USD）。

## 结果

| Run | 上限 | HTTP | 结果 | run 行 | 结论 | 事件流 |
|---|---|---|---|---|---|---|
| `3a7dee13` | 2 | 2（tool_calls → stop/json） | `published`，报告 `m0-report-v2` `completed/inconclusive`，1 条证据 | `completed` | 已发布 | `run_claimed, step_committed, tool_committed, step_committed, run_completed` |
| `325642ca` | 3 | 2 | `published`，`completed/partial`，1 条证据 | `completed` | 已发布 | 同上 |
| `b127b23a` | 1，`--follow-up` | 1（模型直接回报告，未用工具） | 第一次 `handed_off` `INCOMPLETE_INVESTIGATION`；`control(follow_up)` 0→1 被接受，run 回 `queued`；再次 resume 被 claim，预算已尽 → `handed_off` `BUDGET_EXHAUSTED`（无 HTTP） | 两次均 `waiting_human`，owner/lease 为空 | 始终未发布，事故保持开放 | `run_claimed, step_committed, run_handoff, run_claimed, run_handoff` |
| `f987b4b6` | 2 | 2 | `handed_off` `REPORT_INVALID` | `waiting_human` | 未发布 | `run_claimed, step_committed, tool_committed, step_committed, run_handoff` |

## 判定

1. ADR-0005 第 1 条在真实驱动器上成立：合格报告（`3a7dee13`、`325642ca`）发布并把 Run 落为 `completed`；交接（`b127b23a`、`f987b4b6`）不发布，Run 落 `waiting_human`、租约释放、事故 `conclusion` 为空，结论步骤 `conclusion:g0:...` 仍在行里可读。
2. `b127b23a` 证明交接后人工控制可用：`control(follow_up)` 被接受、代际 0→1、Run 重新排队并被下一次 resume 真实 claim；继续尝试因预算已尽再次交接（第二条 `conclusion:g1:...` 步骤），仍不发布。cancel + new_run 由 PG 用例覆盖，本轮真实 Run 未做。
3. 事件流与工作台合同一致：每次结算的尝试恰一个终态事件（`run_completed` 或 `run_handoff`），`step_committed`/`tool_committed` 与已提交行一一对应。
4. `f987b4b6` 的 `REPORT_INVALID` 由本脚本首版把 fixture scope 的 `control_generation` 覆盖成租约代际所致：fixture 工具网关按自己固定的控制快照校验，查询被拒（`CONTROL_GENERATION_CHANGED`，`tool_operations_used=0`），模型只能报「无遥测」，报告校验失败。这是证据脚本的配置错误，不是产品缺陷；脚本已修正并注明，该目录保留作为「工具被拒时 Run 仍以交接收尾且不发布」的记录。
5. 观察（既有缺口，不在本 PR 范围）：`run_usage.tool_operations_used` 在真实工具轮后仍为 0——worker 组合层未接 `DurableToolLedger`（ROADMAP「M1-01 剩余工作」第 4 项）。
6. 本轮不是产品验收、不是 M0/M1 通过、不改 feature `passes`。
