# F12 PR-2：关闭与重新打开事故

- 状态：进行中
- 更新日期：2026-10-16
- 依据：F12 第 3 步（`feature_list.json`，PRD.md:63-67）；[F12 合同预审 r2](2026-10-15-f12-contract.md)（第五节 PR-2：U2-A、U3-A、G4、G5、G6）；C3 §4、§10；PRODUCT-CONSTRAINTS「Recovery」；[PR-1 记录](2026-10-15-f12-followup-history.md)（#246 已合并）
- 工作区：`/Users/shenghuikevin/dev/AI/poa-f12-close`，分支 `feature/F12-close-reopen`，起点 main `d19e78a`；本机 PG 55591（`tmp/pg`）

## 目标与范围

值班人员能在工作台关闭事故（停下自动工作与观察）和重新打开事故（回到 open，不自动开 Run、不恢复旧观察），页面只显示当前可用的控制按钮并显示生命周期徽章。

本 PR 只做合同第五节第 2 条。零迁移、零授权变更（web 角色已有 `opspilot_incidents.lifecycle` 等列的 UPDATE 与 `opspilot_controls` INSERT；Observer 生命周期守卫只约束 Observer 角色）。不做：四步验收映射与 `passes`（PR-3）、真实运行。

## 前提与完成条件

- 前提：#246 已合并。
- 完成条件：独立 Agent 编写的 F12 PG 验收测试中 close/reopen 相关断言通过；`make check` 通过；Codex 独立审查无未处置 P1/P2；PR 开到 `main`，用户门，不合并。

## 合同（r2）

| 编号 | 裁决要点 | 本 PR 落点 |
|---|---|---|
| U2 | A：close 从 open/observing_recovery/resolved → closed，同一事务代际+1、取消未终态 Run、撤销观察授权、审计行；human_owned 可关 | `persistence/controls.py` |
| U3 | A：reopen 从 resolved/closed → open，代际+1，不开 Run、不恢复旧观察；resolved/closed 上会开新 Run 的追问返回 `REOPEN_REQUIRED`（r2，PR-1 已实现） | `persistence/controls.py` |
| G4 | 只显示当前可用动作，服务端仍是唯一判定 | `web/service.py`、模板 |
| G5 | close 撤销观察沿用现有观察结束逻辑，不新增条件；实测是否触发复盘生成 | 本记录 |
| G6 | 动作名 `close`/`reopen`，照搬领域模型 | 理由：与 `domain/control.py` 一致 |

实施中自定的可逆细节（一行理由）：

- C1 close 在已是 closed 时拒绝（`ILLEGAL_TRANSITION`）；reopen 在 open/observing_recovery 时拒绝：领域状态机 `INCIDENT_LIFECYCLE` 没有这些边。
- C2 close 取消了未终态 Run 时，事故控制镜像 `state` 写为 `cancelled`（与 cancel 一致）；当前 Run 已终态时 `state` 不变；结论保留。理由：镜像与 Run 一致，reopen 后追问按「已结论/已取消」续开新 Run。
- C3 close 不带文字、不带 revision；全局/目标挂起不阻止 close（停下自动工作不需要放行）；reopen 同样不受挂起影响（不开 Run）。
- C4 closed 事故上 `new_run` 与 `register_remediation` 返回 `REOPEN_REQUIRED`：与 r2 对追问的处理同向，关闭后开新自动工作须先重新打开；此前没有任何路径能产生 closed，不改变既有行为。resolved 上的这两个动作保持现状。
- C5 closed 上的 pause/resume/cancel 沿用现有判定（Run 已终态 → `ILLEGAL_TRANSITION`）；takeover 照旧允许；human_owned 下的追问/纠正照旧只记输入（r2）。
- C6 close/reopen 与其他动作一样调用 `mark_stale_in(reason="control_generation_changed")`：代际变了，复盘草稿按现有 D18 规则标记陈旧。

## 状态

- 实现（Codex gpt-6.1-sol）、独立验收测试（全新上下文 Claude 子 Agent）、独立审查（Codex gpt-6-astra，PASS，无 P1/P2）完成；PR 用户门，不合并。

## 证据

- 独立验收（PR-3 用，文件暂放本地分支 `test/F12-acceptance`）：`tests/integration/test_f12_acceptance_postgres.py` 13 passed（PG 55591）。红灯对照：撤下本 PR 产品改动后 5 failed / 8 passed，失败的 5 例均为步骤 3 的 close/reopen 场景，其余 8 例是 PR-1 已合并的行为。映射见 `docs/testing/f12-acceptance-tests.md`。
- 实现测试：`test_f12_close_reopen_postgres.py`、`test_f12_available_actions.py` 及控制/页面回归 87 passed；按关键字筛选回归 713 passed、14 skipped。
- G5 实测：observing_recovery 事故 close 后，会话以 `authority_revoked` 结束（结束记录前后 lifecycle 均为 observing_recovery），随后 `GenerationStore.candidates` 把该事故列为复盘生成候选；即关闭会触发复盘草稿生成（沿用现有「观察结束」条件，未新增条件）。未实际调用模型。reopen 不恢复旧观察。
- 既有测试调整两处：恢复页表单数改按 G4 的 `available_actions(snapshot)`；登记处置在 closed 上的预期按 C4 改为 `REOPEN_REQUIRED`，resolved 与其余断言不变。
- close 对 `waiting_human` Run（接管后）同样取消：`waiting_human` 属控制层开放态（`_CONTROL_OPEN_RUN_STATES`），与现有 cancel 一致；ADR-0005 的交接终态是另一状态，不受影响。用户 2026-10-16 裁决 A（取消，即本 PR 现行为），验收测试按此收紧断言（PR-3）。
- `make check`：ruff、format、mypy 通过；pytest 7605 passed、17 failed、54 skipped——17 例均为迁移类测试，首因是环境未把 `pg_dump` 放进 PATH（`OPSPILOT_PG_DUMP` 未设）；补上 PG 17 bin 后这 17 例单独重跑 17 passed。

- PR-3 真实运行（2026-10-16）：四步可观察行为通过，见 [f12-live 记录](../evidence/f12-live/run.md)；用户 2026-10-16 接受两处偏离（4② 手工重投、等价 HTTP 表单提交）为实际通过；续开后复盘重复生成见 #255。

## 未完成与待决

- 真实运行属 PR-3，本 PR 不做。
- 无 Run 的 handoff_only 事故保留既有 `HANDOFF_ONLY` 拒绝与无按钮，未放开 close/reopen。
