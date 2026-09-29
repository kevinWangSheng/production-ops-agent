# M1-01 后续项 3：发布后事故状态显示 `queued`

- 状态：进行中（第一阶段：定性与合同，停在用户门）
- 更新日期：2026-09-29
- 依据：[收口记录](2026-09-28-m1-01-closure.md) 后续项 3；C3 §4；[ADR-0005](../adr/0005-handoff-and-deadline-terminal.md)
- 工作区：分支 `feature/m1-01-incident-state`，worktree `/Users/shenghuikevin/dev/AI/production-ops-agent-incident-state`

## 结论（静态读码，未运行 PG）

**性质为 (b) 合同空白，并带实现漂移；不是单纯 (a) 或 (c)。** 权威来源没有任何一处定义 `opspilot_incidents.state`：

- C3 §4（technical-proposal-2026-09-07.md:72）Incident 只有「事故生命周期、人工控制版本、当前调查 Run」；:84 生命周期 `open → observing_recovery → resolved → closed`；:92 Run 状态 `queued / running / waiting_human / paused / blocked / completed / failed / cancelled / budget_exhausted`，「调查状态与调查结果分开」。领域代码同：`opspilot/domain/subjects.py:19,24` 只有 `IncidentLifecycle`，`opspilot/domain/runs.py:23,37` 只有 `RUN_EXECUTION`。两处都没有事故级 `state`。
- ADR-0003/0005、PRD、feature_list、SPEC 均无事故 `state`。feature_list.json:131「respects the incident state」指生命周期/人工交接，未定义该列。
- C3:96「取消 Run 后……取消 Run 不等于关闭 Incident」；:92「执行完成不自动代表原因找对，也不代表已经恢复」。

因此 (a)「合同写明该推进」不成立；C3 与 ADR 对**事故 lifecycle 与 Run 状态**在 claim、发布、交接、取消、超时后的要求，现有实现都满足（见下表），没有违约。问题在于该列是无合同的第三套状态，且被页面当状态展示。

## 实现事实

写者（`opspilot/persistence.py`，全部）：`accept` 写 `queued`(:373)；全局/目标挂起写 `paused`(:405,482,551)；`new_run` 写 `queued`/`paused`(:696)；`control()` 写 `cancelled` / `paused` / **`running`**(:1579)。`claim`、`publish`、`hand_off`、`block`、`sweep_expired_runs`、租约过期**从不写它**；没有任何路径写 `completed`。

| 动作 | Run 行（权威） | 事故 `state` 列 | 事故 `lifecycle` |
|---|---|---|---|
| accept | queued | queued | open |
| claim | running | queued（不变） | open |
| publish | completed，写 conclusion | queued/running（不变） | open |
| hand_off / 过期清扫 | waiting_human | 不变 | open |
| control cancel | cancelled | cancelled | open（合同要求，符合 C3:96） |
| control resume / follow_up / correct | queued | **running**（Run 尚未被领取） | open |
| pause / 全局或目标挂起 | paused | paused | open |
| 解除挂起 | 不变（仍 paused） | 不变（仍 paused） | open |

漂移证据：`running` 与 Run `queued` 同时出现（:1577 状态映射 vs :1625,1630）；`'completed'` 在 :482,769,1162,1538,1801 被当作排除项，但无写者（死分支）；`lifecycle` 只被写为 `open`（观察属 M2）。

读者：`control()`/`new_run()` 用它判 `cancelled`/`paused`（:1538,1548,672），`claim`/`publish`/`claimable_incidents` 用它挡 `paused/cancelled`（:769,1162,1801；`publish` 注释 :1795 自述为纵深防御）；`rebuild` 与 `control_state` 原样返回（:619,1916）。工作台 `templates/incident.html:8` 把它标为 `State`，并列显示 lifecycle 与 Run 状态；`web/store.py:451,499` 直接取列。既有测试锁定行为：`tests/integration/test_m1_durable_state_postgres.py:1594`、`test_m1_otel_demo_contract_postgres.py:166,181`、`test_m1_upstream_alignment_b_contract.py:166`、`test_m1_otel_demo_contract.py:201`。

实际上该列是**人工控制的镜像**（paused/cancelled 由人工或挂起写入，与 Run 同事务），`queued/running` 只是未被覆写的初值或 control 的副产物，不反映执行。已有记录（web-worker 证据第 5 条、v4-acceptance:38、otel-tools:36）都写「Run 行与 conclusion 才是权威」，与此一致。

上游对照（引自 ADR-0005 对 HolmesGPT `5e983c17` 的引用，本机未找到上游源码，**未复核**）：会话只有一个状态字段 `ConversationStatus`（`COMPLETED/FAILED/TIMEOUT` 等），无「会话状态 + 执行状态」两层。

## 需要用户决定：事故 `state` 的语义（状态语义属用户门）

- **A. 承认为「人工控制镜像」，页面不再当状态展示（推荐）。** 合同写一句：事故级只有 lifecycle；`state` 仅是人工控制在事故行上的副本，取值语义为 active（初值，含 `queued`/`running`）/`paused`/`cancelled`，不表达执行进度；页面以 Run 状态 + lifecycle 为准，把该列显示为「control: paused/cancelled」，`active` 时不显示。不迁移数据，现有测试仍成立；顺带可删除 `completed` 死分支（可选）。代价：列名仍叫 state，靠合同文字约束。
- **B. 让它成为真正的派生状态**：定义为 = 当前 Run 执行状态（paused/cancelled 来自人工控制），在 claim/publish/hand_off/block/清扫/租约过期/各控制路径同事务写。代价：约 10 条写路径与并发语义（人工控制优先、代际栅栏）要逐条改，重复保存 Run 状态，与 C3「执行状态与结果分开」及上游单字段做法相比多一层冗余；收益：列自身正确。
- **C. 删列**：控制判定改为 join 当前 Run + 代际，页面只显示 Run 与 lifecycle。最干净，但要迁移与改所有读者/测试，超出 M1-01 收口体量。

推荐 A：B、C 的收益只是列名好看，代价触及并发与恢复关键路径；A 只改合同文字与展示。改变推荐的条件：若后续要对外 API/通知暴露「事故当前状态」，则选 B。

## 若选 B 的合同要点（供合同测试作者，先选后写）

验收口径 `IncidentScenario -> IncidentOutcome`，PG 上断言最终可观察的（事故 state，Run state，代际）：accept→(queued,queued)；claim→(running,running)；publish→(completed，conclusion 写入，事故仍开放)；hand_off/清扫→waiting_human；cancel→cancelled 且 lifecycle 仍 open；resume/follow_up/correct→(queued，未领取时不得显示 running)；挂起→paused，解除挂起不自动恢复；人工控制优先：晚到的 publish/claim/清扫不得覆盖 paused/cancelled（租约、代际栅栏不变）。选 A 则合同测试只需断言页面不再把 `queued/running` 作为状态展示、control 镜像取值与 Run 状态各自符合上表。
