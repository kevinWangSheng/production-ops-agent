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

## 下一步与交接

- 派 PR-1 实现与独立测试作者。预审未确认项 1–7（事件投影白名单、审计投影版本、F11 消费路径指针跟随、`_alert_rebuild` 取值来源等）由实现者核对，属可逆细节的写一行理由。
