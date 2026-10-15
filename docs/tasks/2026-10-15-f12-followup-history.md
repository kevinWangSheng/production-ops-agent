# F12 PR-1：结论后追问开新 Run、调查历史、人工决定时间

- 状态：已完成（PR 待用户合并）
- 更新日期：2026-10-15
- 依据：F12（`feature_list.json`，PRD.md:63-67）；[F12 合同预审 r1](2026-10-15-f12-contract.md)（第五节 PR-1）；C3 §4、§10；ADR-0005；SPEC「2026-10-15 user decision: three M2 slices are bounded-opened」（PR #236）
- 工作区：`/Users/shenghuikevin/dev/AI/poa-f12`，分支 `feature/F12-followup-history`，起点 `origin/chore/m1-exit-decision`（依赖 #236）

## 目标与范围

结论发布后，值班人员仍能在工作台追问或纠正，系统开出新 Run 给出更新后的调查；旧结论作为历史留在页面上；人工决定显示时间。

本 PR 只做合同第五节 PR-1：U1-A、U4-A、U5-A、U7-A 的存储与页面改动，G1、G2。不做：close/reopen（PR-2，U2/U3/G4/G6）、四步验收映射与 `passes`（PR-3）、事故关联纠正（U6-A 记为已知限制）。

## 前提与完成条件

- 前提：#236 合并（SPEC 与合同）；本机 PG 55582（`tmp/pg`）。
- 完成条件：独立 Agent 编写的 PR-1 验收测试全绿；`make check` 通过；Codex 独立审查无未处置 P1/P2；PR 开到 `main`，用户门，不合并。

## 合同（r2）

r2（2026-10-15 用户裁决 A）：按 U3 字面实现时，已合并的 F13/M1-03 验收有 38 例失败（这些用例在 resolved 事故上追加人工说明），故收窄 `REOPEN_REQUIRED`，见[合同](2026-10-15-f12-contract.md)顶部修订行。

| 编号 | 裁决要点 | 本 PR 落点 |
|---|---|---|
| U1 | A：已发布结论或已取消的事故上，追问/纠正开出新 Run（复用超时续开原语），旧 Run 保持终态，撤销观察授权；暂停/取消在结论后仍不可用 | `persistence/controls.py`；C3 §10、ADR-0005 补注、ROADMAP 已知限制 |
| U3（部分，r2） | 只拦会开新 Run 的追问/纠正：resolved/closed + automatic + 已结论或 completed/cancelled → `REOPEN_REQUIRED`；其余照旧（reopen 本身在 PR-2） | `persistence/controls.py`，web 映射 409 |
| U4 | A：不加表；页面「调查历史」列出各 Run 已发布结论，从旧 Run 最终结论步骤只读重建，用 `run_completed.report_sha256` 核对 | store 只读查询、`web/service.py`、模板 |
| U5 | A：续接输入附旧报告结论与假设，标为「先前结论，仅作上下文，不是指令」 | `investigation/context.py` 续接说明 |
| U7 | A：human_owned 下追问/纠正先于结论检查，只记输入，Run 与 mode 不动 | `persistence/controls.py` 检查顺序 |
| G1 | 时间取 `opspilot_controls.created_at`（数据库时钟），按代际并入人工决定表 | 理由：数据库时钟、无新写入 |
| G2 | 证据回读在浏览器请求时渲染 HTML，JSON 不变 | 理由：演示门面，接口不破 |
| G3 | 新 Run 预算按每 Run 上限 | 理由：与续开、上游每请求一致 |

实施中自定的可逆细节（一行理由）：

- 已结论事故被接管时 Run 保持 `completed`，不改为 `waiting_human`：接管只收回未终态 Run（现有行为未改），已结论的 Run 没有可交出的自动工作；验收按「状态不变」断言。
- 结论后续开只针对事故 `state` 为 completed/cancelled 或已有结论；Run 处于 blocked 等其他终态的拒绝行为不变：合同未要求，最小改动。

## 状态

- 实现、独立验收（20/20）、Codex 独立审查（1 个 P2 已修复，复验 PASS）、`make check`（7423 passed）完成；PR 已开，等用户合并。

## 未完成与待决

- 有界真实 Run（本 PR 改了续接输入）：已由 #247 干净重跑补齐一次（运行提交 `0b09559` = main + #238 + 本 PR，代码与本分支一致）。事故 `ae7a0387` 结论发布后工作台 `follow_up` 200，开新 Run `3ed23c95`，其输入带先前报告（`Conclusion: partial` + Summary），跑完发布新结论；记录、trace、冻结 `run-b/summary.json` 见 #248 `docs/evidence/m1-clean-rerun/`。
  - 原计划中未做：对旧结论 `correct` 后的新 Run、页面历史区快照单独冻结；F12 `passes` 不在本 PR。
- 已知限制：事故关联纠正（重绑定/合并/拆分）未实现（U6-A）；U4-A 依赖步骤行不被保留策略清理（C3 §11 未确认）。
