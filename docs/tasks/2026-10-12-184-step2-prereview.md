# #184 第 2 步合同预审（Codex，只读，HEAD cf84de2）

合同预审基于当前 worktree `feature/F6-184-self-heal-step2`、HEAD `cf84de2`；工作区干净。已核对 r6、F6/F13、C3、PRODUCT-CONSTRAINTS、现有投影、复盘生成、工作台和第 1 步实现。没有修改文件、启动进程或数据库。

### 决定表

| 编号 | 问题 | 选项与代价 | 推荐（依据） | 影响面 | 分类 |
|---|---|---|---|---|---|
| D2-01 | F6 投影如何读取新增 `authorization_source`，以及旧归档记录如何兼容？ | ① 缺失值按 `post_remediation` 读取；不改归档证据，兼容旧快照。② 缺失即 `unknown`；会使既有 F6 归档断言失败。③ 回写历史证据；破坏不可变证据。 | ①。C6 明确旧 `incident_recovery` 会话按 `post_remediation` 读取且“不改写归档证据”（任务记录:30）；当前 F6 投影只复制已提交会话/控制记录（`acceptance_recovery.py:15-34`）。 | F6 投影、归档重放、旧数据库升级 | B |
| D2-02 | 新增来源字段是否改变 F6 既有断言或 `passes`？ | ① 只增加来源字段和系统/人工分类，保留生命周期、采样、重放断言。② 修改 F6 `actions` 既有含义或重算逻辑；会回退已通过的 F6。 | ①。F6 `passes` 已为 true，不能回退（`feature_list.json:49-61`）；第 2 步范围只要求系统授权分类与来源展示（任务记录:54-56）。 | `RecoveryOutcome`、F6 测试、`passes` | A（验收判定受影响，需用户确认“只增字段、不改既有断言”） |
| D2-03 | `auto_authorize_observation` 是否进入 F6 `handling_audit/actions`？ | ① 保留在原始审计可观察记录，但投影为系统授权，不产生 `record_handling`、`human_takeover` 或其他人工动作。② 继续按任意 control 计入人工动作；会把系统行为冒充人工处置。 | ①。C7 明确该动作是 `actor=system` 且 F6/F13 投影“不计入 `human_actions`”（任务记录:29-31）；当前 `_actions()` 对所有 control 默认按人工动作解释（`acceptance_recovery.py:665-685`）。 | F6 `actions`、`permissions`、F13 `human_actions` | A |
| D2-04 | F6 的 `permissions` 是否因系统 control 行存在而包含 `human_control`？ | ① `human_control` 只由人工控制动作产生；自动授权不提升人工权限。② 沿用 `bool(controls)`；自动授权会错误显示人工控制能力。 | ①。当前实现直接以 `bool(controls)` 判定（`acceptance_recovery.py:928-930`），必须改为过滤 `actor/system + auto_authorize_observation`。 | F6 权限投影、只读/人工控制断言 | A |
| D2-05 | F13 复盘输入如何表达系统授权？ | ① 在 `recovery` 段加入 `authorization_source="automatic"`、系统授权者/时间；`human_actions` 排除该 control。② 把系统动作放入 `human_actions` 并标记 actor=system；违反“人工动作”口径。③ 只放审计行、不放恢复段；复盘无法解释来源。 | ①。F13 代码段由代码确定性组装，含 `human_actions` 与 `recovery`（`generation.py:538-585`）；F13 第一步要求“记录的人工动作和恢复观察”（`feature_list.json:141-145`）。 | prompt 输入、复盘页面、F13 外部投影 | A |
| D2-06 | 自动授权是否使已有草稿/审核中复盘变陈旧？ | ① 观察授权推进 `observation_generation`，沿用 `mark_stale_in(..., reason="observation_changed")`。② 因为不是人工动作而不标 stale；会让复盘水位落后。③ 仅由 worker 异步补标；存在提交窗口。 | ①。授权事务当前已调用 `mark_stale_in`（`observation/store.py:1255-1264`）；D18 要求改变水位的业务事务同步标 stale（`docs/tasks/2026-10-05-m1-03-postmortem.md:25,41-42`）。 | F13 stale 状态、复盘水位、并发一致性 | B |
| D2-07 | `human_actions` 的现有计数口径如何处理系统授权？ | ① 只计人工 control 和人工输入；系统授权保留在 recovery/audit。② 计入所有 `opspilot_controls`；计数和页面叙述会错误。 | ①。当前生成器把全部 controls 追加进 `human_actions`（`generation.py:484-511`），需按系统动作过滤；F13 测试已有人工动作断言（`tests/acceptance/test_f13_postmortem.py:452-454`）。 | F13 `human_actions`、timeline、模型输入 | A |
| D2-08 | 工作台“已恢复，未登记处置”在哪些状态显示？ | ① 仅 `authorization_source=automatic` 且独立观察已确认、生命周期 `resolved` 时显示；`authorized/observing_recovery` 显示观察中，未确认时不声称恢复。人工登记仍显示现有人工处置语义。② 自动授权一创建就显示；会把观察中当恢复。③ 所有 `resolved` 都显示该语句；会误标人工登记。 | ①。C3/SPEC 规定健康确认后显示该句、无法确认保持 `open`（技术方案:344）；页面当前分别展示 session state 和 replay verdict（`incident.html:44-75`）。 | 事故页状态文案、生命周期事实、因果表述 | A |
| D2-09 | 文案语言与来源如何展示？ | ① 在现有英文工作台提供明确英文等价文案，同时显示 `authorization_source=automatic/post_remediation`；② 直接只插入中文；与现有英文页面不一致。③ 只显示内部枚举；用户无法理解。 | 需用户裁决 exact UI 文案。合同固定中文展示语「已恢复，未登记处置」（任务记录:10、SPEC:18）；当前模板整体为英文（`incident.html:4-17,44-75`）。建议批准“英文页面采用等价英文，外部验收同时断言语义与来源”。 | 产品文案、验收 HTML 断言、国际化一致性 | A |
| D2-10 | 默认循环改为 30 秒后，是否追溯扫描已有事故？ | ① 按现有持久 C1 查询，升级后首次扫描可处理所有当前代际合格存量事故。② 仅处理默认开启后发布的结论；需要新增发布时间/启用水位。 | 这是未由 r6 明确锁定的合同选择。当前候选 SQL 没有启用时间或结论发布时间截断（`observation/store.py:1431-1459`），而 C2 只规定默认值从 0 改 30（任务记录:24）。建议用户明确：**推荐①**，因为持久状态是权威且重启/漏扫可恢复；若禁止回溯则属 A 类合同变更。 | 存量事故、首次部署行为、真实验收 | A |
| D2-11 | 自动授权是否需要写 subject event，使 SSE 事件流显示生命周期变化？ | ① 保持 C7：只写独立 control 审计，不新增 subject event；页面通过刷新后的持久快照显示。② 增加事件；需定义事件 kind、幂等键、事件与事务关系。 | ①。第 1 步试运行已显示自动授权没有 subject event；C7 只要求独立审计动作，任务第 2 步范围未包含事件流（任务记录:31、54-56）。现有 SSE 只监听固定事件并在 `reload` 类事件后刷新（`incident.html:329-346`）。 | 事件流、审计合同、SSE 行为 | B（若产品要求实时事件，则升级为 A） |
| D2-12 | 自动会话如何在 F6 `observation_authorization` / recovery 中表达版本和来源？ | ① `authorization_source=automatic`，`target.revision=null`/展示 unknown；人工会话保留 revision。② 伪造版本或沿用人工 revision；违反 C4。 | ①。C4 明确自动会话没有权威目标版本，记录 null、工作台 unknown（任务记录:26）；当前页面已有 null→unknown 逻辑（`service.py:2005-2022`、`incident.html:53-56`）。 | F6 投影、工作台、复盘 recovery 段 | B |
| D2-13 | 自动与人工会话混合时，F6 生命周期动作如何配对？ | ① 按 `authorization_source`/实际会话控制代际关联，不能再用“第 k 个 control 对第 k 个 session”的位置假设。② 保留现有位置算法；自动会话会错配人工登记和生命周期转移。 | ①。当前 `_lifecycle_before_registration` 明确按 session 序号配对（`acceptance_recovery.py:597-611`），而自动授权不属于 registration；必须改为来源感知。 | F6 replay、`advance_incident_lifecycle` 计数、混合历史归档 | B |
| D2-14 | 来源字段在页面、F6、F13 的命名是否允许内部名？ | ① 对外只用 `automatic` / `post_remediation` 与中性展示语；内部 `self_heal` 不出现在产品投影。② 暴露 `self_healing/self_healed`；违反 r6 中性来源约束。 | ①。任务记录明确内部表/函数名不出产品展示或 F13 投影（任务记录:39-43），C6 固定来源枚举（任务记录:30）。 | API 字段、HTML、复盘 prompt、审计展示 | B |
| D2-15 | 默认 30 秒的部署配置与关闭语义如何保持？ | ① 未设置环境变量时默认 30；显式 `0` 仍关闭；显式正数覆盖默认；非法值继续 fail-closed。② 删除 0 关闭或让默认环境变量覆盖代码默认；会改变部署合同。 | ①。当前解析保留 `0` 关闭和非负校验（`web/__main__.py:105-117`），web 仅在 interval>0 启线程（`web/__main__.py:211-219`）；C2 明确该语义（任务记录:24）。 | 部署、启动行为、运维配置 | B |
| D2-16 | 没有 HealthProfile、挂起、人工接管或代际变化时，投影是否显示自动来源？ | ① 只显示实际持久记录；未授权/跳过不显示“已恢复”，`skipped` 原因可审计，人工接管后以人工控制和新代际为准。② 页面按“有结论”推断自动恢复；会掩盖 fail-closed/竞态结果。 | ①。C8/C10/C11 要求跳过、人工优先、无 profile 保持现状（任务记录:33-37）；F6 外部入口只复制提交记录，主体错绑返回 unknown（`acceptance_recovery.py:15-34`）。 | 失败路径、人工优先、页面与投影一致性 | B |

### 独立验收测试作者应覆盖的外部断言

按公开接口 `Workbench.snapshot`、事故页 HTTP/SSE、`recovery_outcome(...)`、`postmortem_outcome(...)` 及数据库只读验收读取编写，不依赖内部调用顺序：

- 自动授权来源为 `automatic`；目标 revision 为 `null`，页面显示 unknown；人工登记来源仍为 `post_remediation`。
- 自动 control 的 actor 为 `system`、动作是 `auto_authorize_observation`、不推进 `control_generation`，但会推进 observation generation。
- `RecoveryOutcome` 保留自动授权和 session 记录；`actions`、`human_interaction`、`permissions` 不把自动授权算作人工处置或人工控制。
- F6 既有五步断言全部保持：健康窗口、缺失信号、持续故障、交接、离线重放和主体绑定不回退；`feature_list.json` 中 F6 `passes` 仍为 true。
- F13 `human_actions` 不含 `auto_authorize_observation`；`recovery` 含来源、授权时间、观察状态和结束原因。
- 自动授权推进 observation 水位后，已有 draft/under_review 复盘变为 `stale`，原因是 observation changed；自动 control 本身不作为人工动作计数。
- 自动观察持续健康并结束为 `resolved` 时，事故页显示“已恢复，未登记处置”的约定语义和 `automatic` 来源；观察中只显示观察中，未确认/到期保持 `open` 并带交接原因。
- 人工接管、登记处置、挂起与代际变化先提交者胜；自动会话停止/失效，旧自动记录不改变新人工决定。
- 无 HealthProfile、身份缺失、profile/target 不匹配、暂停状态均 fail-closed；不显示恢复确认。
- 同一 `(incident_id, control_generation)` 只产生一个自动授权；多副本/重复扫描不重复会话或重复审计。
- 循环配置：未设置时每约 30 秒运行；显式 `0` 不运行；显式正数覆盖；非法值启动失败。
- 升级后存量事故行为按用户裁决验证：若采纳推荐①，符合当前 C1 的已有事故会被首次扫描；若选择禁止回溯，则断言旧结论不被自动授权。
- SSE/事件流断言应只要求已约定的公开事件；若不新增 subject event，则通过重新读取快照看到生命周期与来源，不断言内部调用顺序或不存在的事件。

未核查到独立运行证据；本次仅完成静态合同预审。