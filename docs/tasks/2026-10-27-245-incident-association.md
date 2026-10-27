# #245 事故关联纠正：改目标与合并

- 状态：进行中（合同 r1 已定，待实施 PR-1）
- 更新日期：2026-10-27
- 依据：[issue #245](https://github.com/kevinWangSheng/production-ops-agent/issues/245) Agent Brief；C3 §4（核心句「重绑定/合并/拆分」）、§7（撤权后旧证据不得进入新输入）、§10（关联已有事故只增加事件）；[F12 合同](2026-10-15-f12-contract.md) U6；ROADMAP F12 行已知限制。功能 F12。
- 裁决：`~/.agent/handoffs/opspilot-rulings-2026-10-27.md` 第 1、3 节（下称「裁决」）；预审：`~/.agent/handoffs/opspilot-prereview-245-2026-10-27.md`（Fable 5.1，基线 `17bc865e`，下称「预审」）。
- 工作区：PR-1 `/Users/shenghuikevin/dev/AI/poa-245-impl`，分支 `feature/F12-245-rebind-target`；PR-2 开工时填写。

## 目标与范围

F12 只做了「假设纠正」，事故挂错目标或两条告警其实是同一事故时无法纠正。本轮交付两个人工控制动作：**改目标（重绑定）**与**合并**。拆分延后到 #373。不改 F12 四步文字，`passes` 保持 true。

## 前提与完成条件

- 前提：`main` `17bc865e`；迁移 head `0021`。
- 完成条件：PR-1、PR-2 合并，各自的 PG 确定性场景（独立测试作者编写）通过、独立审查无未处置发现；PR-1 附一次有界真实 Run 的 LangSmith trace 与 `summary.json`（ADR-0006）；PR-3 文档合并后关闭 #245。

## 合同（r1）

| 编号 | 裁决要点 | 出处 |
|---|---|---|
| U1 | 本轮只做改目标与合并；拆分另开 #373。C3 §4 偏离写进 PR-3，合并前交用户确认 | 裁决 §1、§3；预审 §2 U1 |
| U2 | 改目标仅 open / observing_recovery 可做，resolved/closed 返回 `REOPEN_REQUIRED`；同事务代际 +1、撤销授权观察会话、取消全部未终态 Run（含 waiting_human）、lifecycle 回 open、改 `target_id`、写审计、`mark_stale_in`；不自动开 Run | 裁决 §1；预审 §2 U2 |
| U3 | 合并：保留方只追加一条事件输入（固定模板，不复制结论正文），不推进代际、不动在途 Run、不撤销观察 | 裁决 §1；预审 §2 U3；C3 §10、#343 先例 |
| U4 | 合并要求两事故同一目标，否则 `TARGET_MISMATCH`；跨目标先改目标再合并 | 预审 §2 U4 |
| U5 | 迟到告警：关联候选须事故当前 `target_id` 等于告警目标；落到被并入事故时沿指针挂到保留事故，保留事故已 closed 则开新事故并 `related_incident_id` 指向保留事故 | 裁决 §1；预审 §2 U5 |
| U6 | 迁移 0022 授予 `opspilot_web` 对 `opspilot_incidents` 的 `UPDATE(merged_into_incident_id, target_id)`；worker/Observer 不变；不加守卫触发器。权限变更，PR-1 用户门 | 裁决 §1；预审 §2 U6 |
| U7 | F12 四步文字不改、`passes` 保持 true；步骤 2 新增改目标、合并两组 PG 确定性场景；PR-1 附有界真实 Run 证据 | 裁决 §1；预审 §2 U7 |
| G1–G13 | 可逆默认照预审：动作名 `rebind_target`/`merge_into`（G1）；observing_recovery 改目标后回 open（G2）；被并入 = closed + 指针，不加生命周期值（G3）；被并入事故拒绝 reopen/改目标/再合并（G4）；合并前置条件与 handoff_only 排除（G5）；工作台身份与后继授权目标改读事故行（G6）；目标变更后的后继走 fresh 输入（G7）；页面（G8）；无守卫触发器（G9）；锁序（G10）；审计投影动作集合（G11）；迁移名 `0022_incident_association`、downgrade 有非空指针时拒绝（G12）；两 PR 各自审查（G13） | 预审 §2 可逆默认 |
| 编号 | 迁移编号约定：#245 用 0022，#244 用 0023；#244 PR-1 等本项 PR-1 合并后再合 | 编排交接 2026-10-27 |

## 实施切分（均目标 main，不 stacked）

1. PR-1 `feature/F12-245-rebind-target`（用户门：迁移 + 权限）：迁移 0022、`rebind_target`、G6/G7、U5 ①、页面、G11、钉值更新。`tests/acceptance/test_f7_step1_entry_points.py` 的动作集合钉值由独立测试作者按合同变更修改。
2. PR-2 `feature/F12-245-merge-into`（用户门：功能）：`merge_into`、U5 ②、页面。不需 kind。
3. PR-3 文档：F12 测试映射步骤 2、ROADMAP F12 行删已知限制、C3 §4 补句（被并入事故以 closed + 指针表示；拆分延后 #373）。C3 偏离部分交用户确认后合并。

## 执行进展与证据

- 2026-10-27：合同 r1 写入；#373（拆分）已开；#245 已贴 Agent Brief。
- 2026-10-27：PR-1 实现（迁移 0022、`rebind_target`、G6/G7、U5 ①、页面、G11）。实现者自定的可逆细节：
  - 事件投影白名单（未确认 1）：PR-1 不写事件输入，不涉及；留给 PR-2。
  - 审计投影版本（未确认 2）：`acceptance_audit` 没有版本钉值（ROADMAP「投影 v11」是脱敏投影），加动作不递增。另把挂起时间线的目标集合扩为「当前目标 ∪ 改目标审计行的 `from_target`」，否则改目标前旧目标的挂起记录会从时间线消失。
  - `_alert_rebuild` 取值（未确认 4）：改目标后的 fresh 输入只沿用其告警锚点与告警上下文；`affected_service` 取新目标登记行（无 workload 则不写），不取告警身份行（旧目标）；发布事实描述旧目标的发布，不沿用。
  - 错误码：非登记键 `UNKNOWN_TARGET`（404，沿用挂起入口）、相同目标 `TARGET_UNCHANGED`（409）、缺目标 `TARGET_REQUIRED` / 其他动作带目标 `TARGET_NOT_ALLOWED`（400）。表单字段 `to_target`，`text` 可选。
  - `merge_into` 不进动作集合（PR-2）。
- 2026-10-27：PR-1 有界真实 Run（kind，[run.md](../evidence/f12-245-rebind-live/run.md)，[summary.json](../evidence/f12-245-rebind-live/summary.json) 14/14 通过）：A=`checkout-misbound` → 改目标到 B=`m0-otel-20260909` → 追问 → 新 Run 输入绑定 B、是 fresh 输入，对 B 的 21 次只读查询（17 ok / 4 no_data）后发布结论；旧 Run cancelled，会话 authority_revoked。[trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/7037e7fc-a4f9-466c-9f3b-612b77c10e7d/r/00000000-0000-0000-a998-c462d3287004?poll=true)。费用 0.10 CNY。限制：otel-demo 网关只登记一个真实目标，A 没有后端，A 上的 Run 在 worker 启动前就被取消。

- 独立验收测试（PR-1）：Codex gpt-6.1-sol（容量中断两次后由 gpt-6-astra 续完），`tests/acceptance/test_245_rebind_target.py` 15 例，另按合同变更改 F7 第 1 步动作集合与 #347 head/授权指纹钉值；main 上 14 红 1 绿，本分支全绿。lead 按 #378 公开接口裁定表单字段 `to_target`、审计在 `opspilot_controls.payload`、页面标记 `#current-target` / `.rebind`；修一处测试构造错误（观察会话改由 `ObservationStore` 读取）。
- 独立审查（PR-1）：Codex gpt-6-astra。首轮 3 阻塞：拒绝路径先提交了目标登记、幂等重放排在同目标检查之后、observing_recovery 起点的结束记录未记 `observation_ended_unconfirmed`。前两条以先红后绿修复（c166b14b）；第三条 lead 裁决不改：`ending_consistent` 要求撤销类结束记录 transition 为 NULL、前后 lifecycle 相同，close 与续开先例同写法，G2 的领域边指事故行 observing_recovery→open。复验两轴 0 发现。
- lead 接受的超字面改动：暂停审计时间线的目标集合 = 当前目标 + 改目标审计行的 `from_target`（G11 字面只要求动作集合），避免改目标后旧目标的暂停记录脱出时间线；审查未提异议。
- 2026-10-27：PR-2 实现（`merge_into`、U5 ②、页面、G11），工作区 `/Users/shenghuikevin/dev/AI/poa-245-merge-impl`，分支 `feature/F12-245-merge-into`。实现者自定的可逆细节：
  - 事件模板（未确认 1）：投影白名单不改。保留方事件 `channel="merge"`，`text` 里写源事故 id、告警 fingerprint/startsAt（开事故身份 + 附加 episode，最多 16 条）、发布观察 id（最多 16）、合并时 lifecycle、是否有结论（不含结论正文）；同样的字段以结构化键留在输入行供人读，模型只看到 `text`/`channel`。输入 id 由 (源, 保留方) 派生，actor 为操作员。
  - 错误码：保留方不存在 `UNKNOWN_IDENTITY`（404）；合并到自身、保留方 closed 或已被并入、被并入事故再 reopen/改目标/合并 → `ILLEGAL_TRANSITION`；任一方无 Run（handoff）→ `HANDOFF_ONLY`；跨目标 `TARGET_MISMATCH`。表单字段 `into_incident_id`（缺 → `INTO_INCIDENT_REQUIRED`，其他动作带 → `INTO_INCIDENT_NOT_ALLOWED`，非 UUID → `INVALID_INPUT`），`text` 可选。源事故任何 lifecycle 都可被并入（brief 未限制）。
  - 指针跟随（U5 ②）：`_correlate` 对每个候选沿指针走到保留方（最多 16 跳，逐行 FOR UPDATE），保留方已改绑到别的目标则不作候选；去重后交 `choose_correlation`，所以保留方 closed 时按「全 closed」规则取最新的 closed 候选作 `related_incident_id`。
  - F11 发布消费（未确认 3）：不改。它的候选是同目标的全部事故，保留方本就在内，被并入事故已 closed，不需要跟指针。

## 下一步与交接

- 派 PR-1 实现与独立测试作者。预审未确认项 1–7（事件投影白名单、审计投影版本、F11 消费路径指针跟随、`_alert_rebuild` 取值来源等）由实现者核对，属可逆细节的写一行理由。
