# M1-01 超时 Run 上的追问开新 Run：有界真实 Flash Run

- 日期：2026-09-25（账本时间戳为 UTC 2026-09-26 04:25）
- 授权：AGENTS.md「费用与真实调用」常设授权。
- 驱动：`scripts/m1_live_runner.py --model-requests 3 --deadline-seconds 8 --sweep --timeout-follow-up`（`InvestigationRunner` + 本 worktree 的 lab PostgreSQL 55431 + `DurableEventLog` + `DurableEvidenceStore`，fixture 工具，无真实 OTel）。
- 原始账本：`live-runs/ec939f2e-cd78-4247-8d11-e17a2307366c/ledger.json`（目录按首个 Run 命名；`current_run_id` 为续开的 Run `b2386590`）；`report.json` 为新 Run 发布的报告正文。不含凭据（写盘后用 key 全文比对确认）；`owner` 列只记「set/None」。
- 费用：1 个事故、2 个 Run，4 次 HTTP，0.047744 CNY 上界。

## 时间线（数据库时钟）

| 时刻 | 事件 |
|---|---|
| 04:25:26.8Z | accept：deadline = now + 8s；首次 `resume` 领到 Run `ec939f2e` 租约，1 轮真实 Flash 工具轮落库（事件 1–3） |
| +7.8s | 第 2 次 HTTP 返回时已过 deadline：提交被栅栏拒绝，loop 交接 `CONTROL_DENIED`，`hand_off` 同样被拒 → `control_denied`；行仍 `running` |
| 过期后再轮询 | 入口清扫停放 `waiting_human`，事件 4 `run_handoff`（`DEADLINE_EXCEEDED`） |
| follow_up | `control(follow_up, renew_run_id=b2386590, renew_deadline=now+12min, renew_input=<C3 continuation>)`：代际 0→1，旧 Run `cancelled`，新 Run `queued`，事故指向新 Run |
| 再轮询 | 新 Run 被 claim（事件 5）；1 轮工具 + 1 轮报告（事件 6–8）；报告 `m0-report-v2`、`completed`，引用 2 条证据（1 条为旧 Run 携带的视图，1 条为本 Run 新查询）；发布，事件 9 `run_completed` |

最终行：新 Run `completed`、结论已发布、代际 1；旧 Run `cancelled`。事件流中没有任何 `run_claim_refused`。

## 判定

1. 用户决定（2026-09-25）在真实驱动器上成立：超时停放的 Run 收到追问后不再重排队为无人能 claim 的行，而是在同一代际步内关闭旧 Run、开出新 Run；新 Run 被下一次轮询真实 claim 并调查到发布。
2. 新 Run 的输入按 C3 续接合同（`continuation_context`）构造：旧 Run 已提交的证据视图重绑到新 run id，报告引用它并被校验通过；追问文本经 `opspilot_inputs` 落库、由 `begin_round` 冻结进新 Run 的轮次。
3. 页面合同：旧 Run 的 `run_handoff` 保留为历史，新 Run 有自己的 `run_claimed … run_completed`；每次轮询不再产生 `run_claim_refused`。
4. 观察：`tool_operations_used` 两个 Run 均为 0（worker 未接 `DurableToolLedger`，ROADMAP 第 4 项，既有缺口）。
5. 本轮不是产品验收、不改 feature `passes`。
