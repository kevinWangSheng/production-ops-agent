# M1-01 后续项：交接停放后缺失 `run_handoff` 事件的补写

- 状态：进行中（r3 实现完成，五轮独立审查处置完毕，待 PR 与用户合并）；原状态行：进行中（独立合同测试已提交；PG lab 新测试一次运行 2 红、6 绿；红项为 runner 与清扫路径的缺失 `run_handoff` 补写，其余测试通过；测试文件与提交号见下方追加记录）
- 2026-10-01：按 r2 补入代际推进后 deadline 清扫页面顺序、清扫停放后补写与后到 announcer 共键两条独立 PG 测试；PG lab 指定文件一次运行 2 failed、12 passed（两条新增均按预期暴露实现缺陷），ruff check 与 format --check 通过。
- 状态追加（2026-10-01）：独立测试已补代际 1 经 resume 再次停放的连续三次页面加载及补写先到、runner announcer 后到；PG lab 指定文件一次运行 2 failed、10 passed（前者总计 4 条而期望 2 条，后者总计 3 条而期望 2 条，均违反 r2 同次停放最多一条）；r2 未规定事件代际字段名，内容断言只约束合同字段、允许附加代际字段。原始日志：`<SCRATCH>/handoff-generation-pytest.log`；lab 已停止并释放锁，ruff check / format --check 通过；按通用约束执行一次 `make check`：2501 passed、281 skipped、2 xfailed（PG 未 opt-in，不能替代上述红证明），日志 ` <SCRATCH>/handoff-generation-make-check.log`；实现修复待执行。
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

## 合同修订 r3（2026-10-01，用户决定「真实事件优先」）

第 4 轮独立审查：清扫先整批停放再逐个追加事件、runner 先 hand_off 再追加，这段窗口里任何页面加载都会先补写 `unknown`，原 announcer 随后被同一去重键挡掉，真实原因（如 `DEADLINE_EXCEEDED`）永久丢失；不需要崩溃。替换 r2 的 2'：

- r3-A：同一次停放（`run_id`，事故 `control_generation`）——原 announcer（runner 停放、清扫停放）写的事件标 `reconciled: false`，announcer 之间按该停放去重、最多一条；**补写不挡原 announcer**：补写已存在时原 announcer 仍追加自己的一条。补写（`reconciled: true`）在该停放已有任一 `parked: true` 事件时不写，最多一条。一次停放最多两条 `parked: true`（至多 1 条原 announcer + 至多 1 条补写，补写只可能早于原 announcer）。
- r3-B：读取方（事故页 `snapshot` 的结果/交接报告、SSE 重载、`opspilot/acceptance.py` 的原因读取）对同一停放**优先取原 announcer 的事件**，仅当没有原 announcer 事件时才用补写事件。页面每次停放只显示一次交接。
- r3-C：旧格式事件（无 `reconciled`/代际字段）按原 announcer 事件对待，既有 gen0 兼容不变。

需补的验收情形：补写先到、清扫事件后到 → 页面与 acceptance 读到 `DEADLINE_EXCEEDED`，事件恰好 2 条，页面只显示一次交接；runner 路径同理；announcer 先到 → 补写不写（1 条）；重复 reconcile 不增加；同一停放的原 announcer 重复写 → 仍 1 条原 announcer 事件。

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
- 2026-10-01：按合同修订 r2 更新独立测试：原 announcer 改用公开 `announce_handoff` / `announce_deadline_exceeded` 并传入公开签名要求的代际；新增补写先到后 announcer、同一 Run 新代际再次停放、当前代际结论步骤仍写 `unknown`/空的验收情形。该行说明原合同 1、2 已被 r2 替换。PG lab 一次运行 12 项：9 passed、3 failed；失败为两条先补写后 announcer 场景及新代际第二条补写，均为实现缺陷，未迁就测试。ruff check 与 format --check 通过。
- 2026-10-01：修复旧格式 parked 事件的代际兼容匹配仅限代际 0，避免新代际补写被首代事件吞掉；PG 指定集成集合 45 passed；`make check` 2501 passed、281 skipped、2 xfailed。
- 2026-10-01：按第二轮审查补写事件携带 `control_generation`，通用 `append_once` 恢复严格包含匹配；真实 DurableStore 清扫在停放事务内直接返回代际，旧测试适配器仍仅有二元清扫接口故保留 0 代际兼容退回；既有独立测试仍无参调用 `announce_deadline_exceeded`，故保留默认 0。待重新执行指定验证。
- 2026-10-01：按第三轮审查，补写改用 rebuilt 顶层事故 `control_generation`，与 runner/清扫共用同一停放代际；移除清扫旧适配器回退和超时 announcer 默认代际，测试替身改用三元清扫接口；旧事件无代际字段且属于 gen≥1 停放时，升级后首次 reconcile 会补一条（当前无生产数据，可接受）。
- 状态追加（2026-10-01）：第三轮修复已实现；PG 指定集合一次运行 55 passed、1 failed，失败为既有“先无事件停放、再 resume/pause、再超时清扫”场景断言两条事件，当前按同一事故代际去重为一条；`make check` 一次运行在格式检查处失败（随后已格式化），gitleaks 11 commits 未发现泄漏。待独立审查确认该验收断言与 r2 去重合同的取舍。
- 2026-10-01：PG 指定集合 58 passed、1 skipped；`make check` 2501 passed、281 skipped、2 xfailed；gitleaks 扫描 9 commits、未发现泄漏。实现与验证完成。
- 2026-10-01（lead）：`test_page_reconcile_preserves_deadline_reason_after_parked_run_generation_advances` 的计数 2 → 1。原期望来自 lead 给测试作者的任务书笔误：gen0 停放的事件丢失后 Run 已被 resume 离开 `waiting_human`，按合同 1 不再补写，只剩超时停放一条；核心断言（最新事件为 `DEADLINE_EXCEEDED`）不变。
- 2026-10-01：按合同修订 r3-A/B，更新 runner 与清扫“补写先到、announcer 后到”为恰好两条 `parked:true` 事件，并断言页面只显示一次交接且页面/`opspilot.acceptance` 读取原 announcer 的真实原因。
- 2026-10-01：按合同修订 r3-A，补充 announcer 先到时 reconcile 不补写、重复 reconcile 不增加，以及同一停放原 announcer 重复写仍保持一条原 announcer 事件的验收断言。
- 状态追加（2026-10-01）：r3 独立测试已更新；PG lab 指定文件一次运行 11 passed、3 failed（失败为当前实现尚未满足 r3 的 runner/清扫原 announcer 优先与两条事件断言）；lab 已停止并释放锁；ruff 与 `make check` 通过（2501 passed、283 skipped、2 xfailed）。
- 状态追加（2026-10-01）：按 r3 实现原 announcer `reconciled:false` 与补写 `reconciled:true` 的分层去重，并让 snapshot/acceptance 优先原 announcer；指定 PG 集合一次运行 74 passed、3 skipped、1 failed。失败为既有 `test_sweeper_generation_is_shared_by_reconcile_backfill_and_late_announcer` 仍断言补写后晚到清扫 announcer 只有 1 条，与 r3-A 明确要求同次停放可保留 1 条补写 + 1 条原 announcer（共 2 条）冲突；未改测试断言，make check/gitleaks/commit 按约束暂停，待决为更新该既有断言或确认保留旧合同。
- 2026-10-01（lead）：`test_sweeper_generation_is_shared_by_reconcile_backfill_and_late_announcer` 按 r3-A 改为 2 条（补写 + 晚到清扫各一，清扫重复写仍去重），并断言最新为 `DEADLINE_EXCEEDED`；测试作者漏改的 r2 断言，属合同变更。
- 2026-10-01（lead）：修 r3 实现两处——页面结果选择丢了「run_completed 优先」导致已发布 Run 显示为交接（既有 `test_a_completion_confirmed_after_a_lost_marker_announces_one_run_completed` 失败），且按 Run 行代际副本判定同一停放（应为事故代际，见第 3 轮审查）；另修 2 处 mypy。`make check` 2501 passed；PG 全量 integration 229 passed、54 skipped。
- 2026-10-01（lead）：第 5 轮独立审查（Opus）确认 r3 主体无 P1；两处 P2 回归已修——acceptance 读取 blocked 原因不再按代际过滤（`parked:false` 事件不带代际），reconcile 的 `rebuild` 失败时与 main 一样直接返回。页面「Last attempt」每次停放只显示一次；Live progress 是原始事件流，补写与原 announcer 两条都会列出（r3-A 允许两条的必然结果）。可选项未做：blocked + 代际≥1 的 acceptance 用例、代际 1 只有补写时的页面断言、snapshot 双 rebuild 合并。`make check` 2501 passed；PG integration 229 passed、54 skipped。

## 审查轮次小结

五轮 Opus 独立审查依次发现：补写与 announcer 竞争重复（r1）→ 补写事件缺代际（r2 后）→ 代际来源分叉（run 副本 vs 事故）→ 补写抢先吞掉真实原因（用户定 r3「真实事件优先」）→ r3 读取侧两处小回归。执行者（Codex Sol）多轮交付存在未跑通检查、漏改冲突断言、读取侧回归，均由 lead 复验后修正。
