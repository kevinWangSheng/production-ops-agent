# M1-01 后续项：交接停放后缺失 `run_handoff` 事件的补写

- 状态：进行中（独立合同测试已提交；PG lab 新测试一次运行 2 红、6 绿；红项为 runner 与清扫路径的缺失 `run_handoff` 补写，其余测试通过；测试文件与提交号见下方追加记录）
- 2026-10-01：`tests/integration/test_m1_handoff_backfill_postgres.py`；测试提交号 `472d370`；PG 一次运行 2 failed、6 passed（失败为 runner/清扫缺失 `run_handoff` 补写，绿项覆盖幂等、已有 announcer、非 waiting 状态、页面可见性、行不变与写失败保护）。
- 更新日期：2026-10-01
- 依据：[#44 任务记录](2026-09-24-m1-01-handoff-runner.md) 第 51 行（机器人审查 PR #44 P2，lead 裁定单列）；`opspilot/investigation/progress.py` `announce_deadline_exceeded` 文档串；ADR-0003（行是权威、事件是投影）；[ADR-0005](../adr/0005-handoff-and-deadline-terminal.md)
- 工作区：`feature/m1-01-handoff-backfill`，`../production-ops-agent-handoff-backfill`

## 问题

Run 被停放到 `waiting_human` 的两条路径——runner 交接（`service.py` 的 `hand_off` 后 `announce_handoff`）与超时清扫（`sweep_expired` 后 `announce_deadline_exceeded`）——都是先提交行、再追加事件。进程死在两者之间时，Run 行已是 `waiting_human`，事件日志却没有这个 Run 的 `run_handoff`。`reconcile()` 目前只补 `run_completed`，于是事故页缺这次交接（页面不显示交接、SSE 不触发重载）。

## 合同

1. `reconcile(incident_id)` 在当前 Run 行状态为 `waiting_human`、且事件日志中**没有**该 `run_id` 的 `parked: true` 的 `run_handoff` 时，追加一条 `run_handoff`：`run_id`、`published: false`、`handoff: true`、`parked: true`、`reconciled: true`、`execution` 与 `reasons` 取自持久化记录中能确定的值；记录无法确定原因时 `reasons` 为空列表（不猜交接还是超时），`execution` 为 `"unknown"`。`report_sha256`、`evidence_ids` 能从持久化记录得到则填，否则 `null` / 空列表。
2. 幂等：重复调用 `reconcile()`、或与原 announcer 竞争，同一 `run_id` 最多出现一条 `parked: true` 的 `run_handoff`（原 announcer 已写过则不补）。
3. 不补的情形：Run 不在 `waiting_human`（queued、running、completed、cancelled 等）；已有 `parked: false` 的 `run_handoff` 但行不在 `waiting_human`；事故不存在。已有 `parked: false` 事件而行确实停在 `waiting_human` 时照常补一条 `parked: true`。
4. 补写后页面（公开 HTTP 入口：事故页与快照）显示该 Run 处于交接等待人工，与原 announcer 写入时的显示一致（`reconciled` 字段可不显示）。
5. 行不因 reconcile 改变：补写只追加事件（及必要的 ledger 标记），不改 Run/事故行。
6. 写失败不让页面加载失败（同既有 sweep 的处理）。

## 验收口径

在真实 PG（`M1_DURABLE_POSTGRES=1`，lab 端口 55431）上按公开入口制造：runner 交接后事件追加失败、清扫停放后事件追加失败、两种情况各自 reconcile 一次与两次、原 announcer 已写过后再 reconcile、非 waiting_human 的各状态。断言事件日志与页面可见结果，不断言内部调用顺序。

## 不做

不改 `hand_off` / 清扫的提交顺序；不新增"交接原因"列（若持久化记录没有原因，按合同 1 留空）；不处理 `run_completed` 以外其他事件的补写。

## 待决

无合同层取舍需要用户决定。功能 PR 涉及状态恢复，按 AGENTS.md 属用户门。
