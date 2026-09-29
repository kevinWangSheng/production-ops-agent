# M1-01 后续项 3：发布后事故状态显示 `queued`

- 状态：进行中（用户 2026-09-29 选定 A；合同已写，实现待第二阶段后半）
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

## 用户决定（2026-09-29）：选 A

事故级 `state` 只是人工控制的镜像；不迁移数据、不改写库里的值、不改现有测试；只规定页面投影。B（派生状态）与 C（删列）不做，改变条件：要对外 API/通知暴露「事故当前状态」时重开 B。

## 合同

**原文位置：** C3 §4 在 `事故生命周期：…` 一行（technical-proposal-2026-09-07.md:84）之后新增一段（:86），只补定义，不改既有语义；该段已写入本分支。

**定义。** 事故行 `state` 的语义是「人工控制标记」：`paused`（人工暂停，或全局/目标挂起）、`cancelled`（当前 Run 被取消）、其余一律视为「进行中」。库里的值 `queued`、`running`（含 resume/follow_up/correct 写入的 `running`）与任何其他值都属「进行中」，界面不解释、不显示。它不表达执行进度；执行进度只看当前 Run 的状态，结论只看是否已发布，生命周期只看 lifecycle。`cancelled` 不等于事故关闭，lifecycle 仍为 `open`。

**公开接口。** 只有 HTML：`GET /` 与 `GET /incidents/{id}`（`opspilot/web/app.py:260,266`），没有 GET 的 JSON 投影（`POST .../control` 的 JSON 不含 state，不变）。断言用 HTTP 页面文本，`Accept: text/html`。

**详情页投影**（`templates/incident.html`）：
- 始终显示：`id="run-state"`（Run 行状态，原样）、lifecycle 徽标、`id="concluded"`、`id="control-generation"`。
- 新元素 `id="incident-control"`：仅当标记为 `paused` 或 `cancelled` 时出现，文本恰为 `paused` 或 `cancelled`（前缀 `control`）。进行中时**整个元素不出现**。
- `id="incident-state"` 元素及其「State」标签不再出现；`queued`/`running` 不得作为事故级徽标出现（Run 徽标 `run-state` 可为 `queued`/`running`）。

**列表页**（`templates/index.html`）：「State」列改名「Control」，单元格为 `paused`/`cancelled`，进行中为空（不写 `queued`/`running`）；Lifecycle、Generation、Concluded 列不变。列表不显示 Run 状态（本次不扩）。

**逐场景（事故标记 / Run 行 / 详情页应见与不应见）：**
| 场景 | 库中 `state` / Run | 应见 | 不应见 |
|---|---|---|---|
| 发布后 | queued 或 running / completed，有 conclusion | `run-state`=completed，`concluded`=concluded，lifecycle open | `incident-control`、`incident-state`、事故级 `queued` |
| 交接或超时清扫后 | queued 或 running / waiting_human | `run-state`=waiting_human，`concluded`=no conclusion，lifecycle open，`handoff-report` 或 `report-missing` 沿用 | `incident-control` |
| cancel 后 | cancelled / cancelled | `incident-control`=cancelled，`run-state`=cancelled，lifecycle **open** | `incident-state`；lifecycle 不得为 closed |
| 人工 pause 后 | paused / paused | `incident-control`=paused，`run-state`=paused | 事故级 `queued`/`running` |
| 暂停尚未被领取的 queued Run | paused / **queued 或 paused**（以库行为准） | `incident-control`=paused；`run-state` 显示库中 Run 状态，可为 `queued`（不改写路径，属预期，lead 裁定） | 事故级 `queued`/`running` |
| 全局或目标挂起后（结论未发布） | paused / paused | 同 pause（页面不区分来源） | 同上 |
| 挂起时已发布的事故 | 不被挂起写入（挂起只改无结论事故）/ completed | 同「发布后」 | `incident-control` |
| 解除挂起后 | 仍 paused / 仍 paused | `incident-control`=paused（要人工 resume） | 自动变回进行中 |
| resume / follow_up / correct 后 | running / queued | `run-state`=queued，无 `incident-control` | 事故级 `running`（Run 尚未被领取） |
| claim 后 | queued / running | `run-state`=running，无 `incident-control` | 事故级 `queued` |

**并发与恢复不变：** 本次不改任何写路径、租约、代际、清扫或人工控制优先；投影是读时纯函数，值取自同一次 `find_incident`/`rebuild` 快照，不得为它新增写入或 join。刷新页面不改变库行（`snapshot` 已有的 reconcile 清扫行为不变）。

**验收口径（IncidentScenario -> IncidentOutcome）：** 测试在 PG 上按上表制造各场景（accept、claim、publish、hand_off/过期清扫、control 各动作、全局/目标挂起与解除），只用公开入口（HTTP 页面 + `POST /incidents/{id}/control`）读最终页面，断言上表的应见/不应见及库行不因页面渲染而变化；库中值由既有 store 合同测试守护，不在本合同内重复。

## 不做

- 不迁移数据、不改 `opspilot_incidents.state` 的任何写路径，不消除 `resume/follow_up` 写 `running` 这一副作用。
- 不删 `'completed'` 死分支（:482,769,1162,1538,1801）：删除不是 A 所必需，仅在此记录。
- 不新增 JSON 投影、不区分暂停来源、不显示挂起横幅、不给列表页加 Run 状态、不改 lifecycle 语义（观察属 M2）。
- 不改现有测试断言；`test_m1_web_workbench.py` 若断言了 `State` 标签或事故级徽标，按合同变更处理并报告（当前仅见 `run-state`、`conclusion` 断言，未见事故级断言，未运行）。

库值 `completed` 属「其他值」，按进行中处理（lead 裁定）。控制标记元素文本恰为 `paused`/`cancelled`，摘要区不写含 "state" 的词。
