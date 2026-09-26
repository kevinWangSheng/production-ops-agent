# M1-01 超时清扫：有界真实 Flash Run 跑进 deadline 后被清扫

- 日期：2026-09-24
- 授权：AGENTS.md「费用与真实调用」常设授权。
- 驱动：`scripts/m1_live_runner.py --model-requests 3 --deadline-seconds 8 --sweep`（`InvestigationRunner` + 本 worktree 的 lab PostgreSQL 55431 + `DurableEventLog` + `DurableEvidenceStore`，fixture 工具，无真实 OTel）。
- 原始账本：`live-runs/19cf2b62-4a2f-43dc-9daf-2c3b133b1e25/ledger.json`（HTTP 尝试、tokens、费用上界、`run_usage`、每次尝试后的行快照、事件流）。不含凭据（脚本从 `M0_ENV_FILE` 读 key 后即 `del`，写盘前已用 key 全文比对确认不在账本中）；`owner` 列只记「set/None」。
- 费用：1 次 Run，3 次 HTTP，0.012809 CNY 上界（input $0.3/1M、output $1.2/1M、7.3 CNY/USD）。

## 时间线（数据库时钟）

| 时刻 | 事件 |
|---|---|
| 20:30:30.21Z | accept：deadline = now + 8s；runner 首次 `resume` 领到租约（epoch 1），`run_claimed` |
| +1.5s / +3.0s | 两轮真实 Flash 请求各带一次 tool_call，行与事件：`ctx0:round-1`、`ctx0:round-2` 均 `tool_result_committed`，事件 2–5 |
| +7.8s（第 3 次 HTTP 返回时已过 deadline） | loop 提交结论步骤被 deadline 栅栏拒绝，落为 `late_result:step:conclusion:g0:ctx0:round-2:e1`（status `late_result`）；loop 交接原因 `DEADLINE_EXCEEDED`；runner `_hand_off` 调 `hand_off(lease)` 同样被栅栏拒绝 → 首次 `resume` 返回 `control_denied` / `CONTROL_DENIED`。此时 run 行仍 `running`、owner 已设、lease 未清 —— 即 ADR-0005 描述的「无人能收尾」状态 |
| 20:30:38.30Z | 脚本等到 deadline 过后再 `resume` 一次：runner 入口的清扫在事务内确认 `running` 且已过期，停放为 `waiting_human`、owner/lease 清空；返回 `handed_off` / `DEADLINE_EXCEEDED`；追加事件 6 `run_handoff`（`parked: true`，`reasons: ["DEADLINE_EXCEEDED"]`，`published: false`） |

最终行：run `waiting_human`，owner `None`，lease `None`，事故 `conclusion` 为空、代际 0；步骤 3 行（2 个工具轮 + 1 个 late_result 结论）。`run_usage.model_requests_used = 3`。

## 判定

1. ADR-0005 第 2 条在真实驱动器上成立：跑过 deadline 的 Run 在没有任何人工动作的情况下，被下一次轮询清扫为超时交接，事故保持开放、不发布结论；迟到的结论步骤保留为 `late_result` 历史，不成为结论。
2. 清扫写路径与 #44 的 `hand_off` 是同一条 `running -> waiting_human` 语句（`DurableStore._park`），栅栏不同：无租约，改为事务内行锁下的 state + deadline 复核。
3. 事件流与工作台合同一致：每个已结算的尝试恰一个终态事件；被栅栏拒绝的首次尝试不发事件（与 #44 runner 行为相同），清扫发唯一的 `run_handoff`。
4. 观察：`tool_operations_used` 仍为 0（worker 组合层未接 `DurableToolLedger`，ROADMAP 第 4 项，既有缺口）。
5. 本轮不是产品验收、不是 M0/M1 通过、不改 feature `passes`。
