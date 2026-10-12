# #184 结论发布后自动开启恢复观察（未登记处置时）

- 状态：合同 r6（2026-10-12）；授权 PR #209 待合并；第 1 步实施中（`../poa-184-step1`）
- 更新日期：2026-10-12
- 依据：[#184](https://github.com/kevinWangSheng/production-ops-agent/issues/184)；PRODUCT-CONSTRAINTS「Product workflow」；SPEC 2026-10-12 段；C3 §4、§10「结论发布后自动开观察」段；feature_list F6（`passes` 已为 true，不得回退）；M1-02 [任务记录](2026-10-03-m1-02-recovery-observation.md)；M1-04 决定 C
- 工作区：授权 PR `chore/184-self-heal-gate`（`../poa-184-self-heal`）；第 1 步 `feature/F6-184-self-heal-step1`（`../poa-184-step1`，lab PG 55471，测试作者 PG 55472）

## 目标与范围

恢复观察现在只能由人在工作台登记「已处理」开启。故障自行恢复或被人在系统外修好、但没人点击时，事故永远停在 `open`。本切片让系统在合格结论发布后，为有 HealthProfile 的目标自动授权一次独立恢复观察：持续健康则推进到 `resolved`，显示「已恢复，未登记处置」；无法确认则保持 `open` 交人。记录不作因果判断（不称「自愈」）。

不在范围：放宽 Observer 或 worker 权限；由模型认证恢复；改变「人工关闭不等于恢复」；告警 resolved 触发观察（M1-04 决定 C 不变）；结论发布后的暂停与取消。

## 完成条件

kind 实验环境真实跑通两个场景：故障由 harness 恢复、产品侧无人点击 → 自动观察推进到 `resolved`，来源 `automatic`；故障持续 → 到期保持 `open` 并写交接原因。人工登记路径与 F6 验收投影、`passes` 不回退；独立审查与独立验收测试作者按 AGENTS.md 执行。

## 合同（r6，当前有效）

r1 由用户 2026-10-12 按预审推荐裁决；r3、r4 前三项为用户裁决；其余为 lead 可逆默认。[合同决定表](2026-10-12-184-self-healing-recovery-contract.md)是预审原文与修订历史，**与本节冲突时以本节为准**。

触发与授权
- C1 触发：`incidents.conclusion IS NOT NULL`（只有 `publish()` 写，`steps.py:356`）且当前 Run `completed` 且 `runs.control_generation = incidents.control_generation`（r5：人工登记推进代际后不再触发）、`lifecycle='open'`、`mode='automatic'`、`handoff_only` 为假、无活动会话、目标有生效的 HealthProfile。交接不发布，不触发。
- C2 执行者：web 进程内后台循环（`opspilot_web` 角色），间隔 `OPSPILOT_SELF_HEAL_INTERVAL_SECONDS`（`0` 关闭；第 1 步默认 `0`，第 2 步与 F6/F13 投影一起改为默认 30，避免投影未就绪时自动会话被计为人工处置）；多副本靠主键与 `SKIP LOCKED` 防重；#197 拆出 Controller 时整体迁走。worker 与 Observer 权限不变。
- C3 授权：与人工登记共用同一事务原语、暂停与代际栅栏。目标身份只取自配置的目标身份注册表（`OPSPILOT_TARGET_IDENTITIES`，按已登记 `resource_uid` 绑定），与人工路径共用补齐步骤与 `TARGET_MISMATCH`/`TARGET_IDENTITY_MISSING`/`HEALTH_PROFILE_TARGET_MISMATCH` 检查；不接受告警或模型提供的身份，缺失即 fail-closed。会话参数全部取自 HealthProfile，观察起点为授权事务的 DB 时钟，不延长 deadline。
- C4 版本：自动会话没有权威的目标版本，`target.revision` 记 `null`，工作台显示为未知；不写伪造的版本串。
- C5 幂等：`(incident_id, control_generation)` 至多一次；会话 ID `uuid5(intake_key:auto-observe:<control_generation>)`。

来源与审计
- C6 `opspilot_observation_sessions.authorization_source`：`incident_recovery` ∈ {`post_remediation`,`automatic`} 非空，`release_observation` 为 NULL（CHECK 按 `purpose`）。列默认 `post_remediation` 仅为旧写入方与跨版本夹具兼容（r5-2：独立 F13 测试 D53 夹具在 0011 与 head 都插入会话）；产品原语的 `authorization_source` 为必填参数、无默认值，测试断言自动会话从不落到默认值。迁移回填旧 `incident_recovery` 行，release 行置 NULL；不改写归档证据。
- C7 审计：`opspilot_controls` 写独立动作 `auto_authorize_observation`，actor `system`，`expected_generation = resulting_generation`，不推进 `control_generation`；只按现有原语推进 `observation_generation`。第 2 步 F13 投影把它归为系统授权，不计入 `human_actions`。

失败与竞态
- C8 失效或不适用（生命周期非 open、`human_owned`、事故已暂停/取消、`handoff_only`、全局或目标挂起、已有活动会话、无身份或 profile 不匹配）→ 记录 `skipped` + 原因，终态，不改事故、不写审计；挂起解除后不补开。
- C9 运行性失败 → `retrying`，最多 5 次、指数退避（扫描选取到期的 `retrying` 行），耗尽 → `handed_off` 并写一条工作台可见的事故事件，事故保持 `open`。
- C10 人工优先：接管（停止自动观察）、登记处置、全局/目标挂起，先提交者胜并撤销自动会话；暂停与取消在结论发布后不可用，保持现状（`controls.py:306-309`）。
- C11 无 HealthProfile 的目标保持现状，只能人工处理。

第 1 步公开接口
- 迁移（下一个线性号）：上述列与新表 `opspilot_self_heal_authorizations`（主键 `(incident_id, control_generation)`；`state` ∈ `authorized/skipped/retrying/handed_off`；`reason`、`attempts`、`next_attempt_at`、`session_id`、时间戳）；`RUNTIME_GRANTS` web 读写，worker、Observer 无写。
- `Workbench.self_heal_pass(*, limit=20) -> list[SelfHealOutcome(incident_id, control_generation, state, reason, session_id)]`；`authorize_session_in(..., authorization_source)` 必填；`register_remediation` 传 `post_remediation`。
- `self_heal` 这类表名与函数名是内部名，不出现在产品展示、审计动作或 F13 投影中。
- 不含：F6/F13 投影与页面（第 2 步）、kind 真实运行（第 3 步）。

## 执行进展与证据

- 2026-10-12 预审 → 用户裁决 r1；第 1 步预审 → r3；PR #209 机器人与独立复审 → r4–r6（用户裁决 PRODUCT-CONSTRAINTS 修订、中性来源 `automatic`、收窄人工优先）。F11 排在本任务之后。
- 实验环境：与另一会话（production-ops-agent-00）协调，同一时间只跑一个真实故障；真实 Run 前先确认。
- 迁移：线性链，当前 main 末尾 0012；对方先合并则 rebase 并重编号。

## 下一步与交接

授权 PR #209 待用户合并；第 1 步按 r6 公开接口实施（worktree `../poa-184-step1`，分支 `feature/F6-184-self-heal-step1`）：实现 Agent 与独立测试作者（Codex）并行，PR 在 #209 合并后对 main 提交。

