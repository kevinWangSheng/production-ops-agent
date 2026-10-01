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

## 合同修订 r2（2026-10-01，lead 依独立审查 P1/P2 决定；可逆实现细节，不需用户门）

审查发现：补写先于原 announcer 落地时出现两条 `parked:true`（runner 的 `announce_handoff` 用非幂等 `append`；清扫 key 含 `reasons`，与补写的空列表不互相包含）；按结论步骤推断原因会把超时停放标成 loop 交接；同一 `run_id` 可被重新排队后再次停放，按 `run_id` 去重会吞掉第二次停放。据此替换合同 1、2：

1'. 补写**不推断**：`execution` 恒为 `"unknown"`、`reasons` 恒为空列表、`report_sha256` 为 `null`、`evidence_ids` 为空列表，另带 `reconciled: true`。交接报告等内容仍由页面从权威行读取。
2'. 去重单位是「一次停放」= (`run_id`, 停放时的代际)。代际取停放发生时可确定、且 reconcile 事后也能从行读出的值（例如 Run/事故的 `control_generation`；实现者选定并写一行理由）。runner 停放、清扫停放、补写三方对同一次停放**共用同一个去重键**（`append_once` 的 key 只含 `run_id`、`parked: true` 与代际，不含 `reasons`），无论谁先到，同一次停放最多一条 `parked: true` 的 `run_handoff`；同一 `run_id` 在新代际再次停放时，可以（且缺失时应被补写）出现新的一条。
   - runner 的 `parked: false` 事件（被拒/崩溃尝试的历史）不受此键约束，行为不变。

需补的验收情形：补写先到、原 announcer（runner 与清扫各一）后到 → 仍只有一条；同一 Run 重新排队并在新代际再次停放且事件丢失 → 补写出第二条；存在当前代际的结论步骤（`handoff:false` 或 `true`）时补写仍为 `unknown`/空。

## 验收口径

在真实 PG（`M1_DURABLE_POSTGRES=1`，lab 端口 55431）上按公开入口制造：runner 交接后事件追加失败、清扫停放后事件追加失败、两种情况各自 reconcile 一次与两次、原 announcer 已写过后再 reconcile、非 waiting_human 的各状态。断言事件日志与页面可见结果，不断言内部调用顺序。

## 不做

不改 `hand_off` / 清扫的提交顺序；不新增"交接原因"列（若持久化记录没有原因，按合同 1 留空）；不处理 `run_completed` 以外其他事件的补写。

## 待决

无合同层取舍需要用户决定。功能 PR 涉及状态恢复，按 AGENTS.md 属用户门。

## 追加记录（2026-10-01）

- 改动行为：`reconcile()` 在当前 Run 为 `waiting_human` 且缺少 `parked:true` 事件时补写可见的 `run_handoff`，不修改 Run 行；去重使用事件日志 `append_once` 的 `run_id` + `parked:true` 键，以覆盖原 announcer 竞争。
- 来源选择：`execution`、`reasons`、`report_sha256`、`evidence_ids` 优先读取当前 Run 已提交结论步骤的持久化字段；清扫路径或字段不可确定时按合同写 `unknown`、空列表和空值，不猜交接原因。
- 状态：已完成。
- 验证：`M1_DURABLE_POSTGRES=1 ... pytest`（相关 5 个 integration 文件）41 passed；`make check` 2501 passed、277 skipped、2 xfailed。
- 2026-10-01：按合同修订 r2 修复三方 parked 事件共用含 `control_generation` 的 `append_once` 键；reconcile 固定写 unknown/空值并移除全量日志预检与结论推断；兼容无代际字段的历史 parked 事件。PG 指定集成 41 passed；`make check` 2501 passed、277 skipped、2 xfailed；提交 `fix: one run_handoff per park across announcers and reconcile [M1-01]`。
