# #184 自愈故障的独立恢复确认

- 状态：合同 r1 已裁决（2026-10-12）；授权 PR 待用户合并；实现未开工
- 更新日期：2026-10-12
- 依据：[#184](https://github.com/kevinWangSheng/production-ops-agent/issues/184)；C3 §4「事故与发布观察分开建模」「人工控制与结果所有权」「全局与目标级暂停」、§10「观察主体与授权」「采样与提交」；PRODUCT-CONSTRAINTS；feature_list F6（`passes` 已为 true，不得回退）；M1-02 [任务记录](2026-10-03-m1-02-recovery-observation.md)；M1-04 决定 C（resolved 告警只记录）
- 工作区：授权 PR `chore/184-self-heal-gate`，`../poa-184-self-heal`；实施每步一个 worktree，写在执行节

## 目标与范围

恢复观察现在只能由人在工作台登记「已处理」开启（`register_remediation`）。对会自行恢复的故障，没人点击就永远停在 `open`，值班侧分不清「仍在故障」和「早已自愈」。本切片让系统在约定时机为有 HealthProfile 的目标自动授权一次独立恢复观察，持续健康则推进到 `resolved` 并标明来源为自愈；无法确认则保持 `open` 交人。

不在范围：放宽 Observer 数据库权限；由模型认证恢复；改变「人工关闭不等于恢复」；告警 resolved 触发观察（#184 方案 B，依赖改 M1-04 决定 C，用户未要求）。

## SPEC 有界开放

SPEC「2026-10-12 user decision」段与 C3 §10「自愈确认」段，随授权 PR 合并（用户门）。

## 前提与完成条件

- 前提：用户裁决下方决定表；C3 §10 与生命周期描述按裁决修订（C3 变更属用户门）。
- 完成条件（草案）：自愈场景在 kind 实验环境真实跑通——故障注入后自愈、无人点击，事故经独立观察推进到 `resolved` 且来源标为自愈；持续故障场景到期保持 `open` 并交接；人工登记路径、F6 验收投影不回退；独立审查与独立验收测试作者按 AGENTS.md 执行。

## 合同（r5）

用户 2026-10-12 按推荐裁决 A1–A11；B1–B5 按推荐作可逆默认。完整表见[合同决定表](2026-10-12-184-self-healing-recovery-contract.md)（Codex GPT-6 Astra，high，只读预审）。

- **A1** 纳入 M1，仅限有 HealthProfile 的 `incident_recovery`。
- **A2** 只在合格结论发布后自动开观察；交接不发布（ADR-0005），交接路径不触发。
- **A3** 由 Controller 侧可重放任务授权，调用与人工登记相同的事务原语；worker 不写观察表。
- **A4** 会话持久 `authorization_source = post_remediation | automatic`（下一个迁移；r4 改名）；r2：按 `purpose` 条件约束，release 会话为 NULL。
- **A5** 审计 actor 为 `system`，来源记 `automatic`（r4）；不新增 DB 角色，DB current_user 仍是 Controller/web 角色。
- **A6** 观察起点为授权事务的 DB 时钟；HealthProfile 参数不变，不延长 deadline。
- **A7** 事故行锁 + 代际决胜，先提交者胜；人工登记、接管、全局/目标挂起撤销自动会话（r4 收窄：暂停与取消结论发布后不可用，保持现状）。
- **A8** 无 HealthProfile 的目标保持现状，只能人工处理。
- **A9** 保留 M1-04 决定 C；resolved 告警只作工作台提示，不触发观察。
- **A10** 不改写旧证据；旧会话兼容读作 `post_remediation`，新会话必须显式来源。
- **A11** 复用现有 Controller/web 观察写权限，不给 worker 扩权。
- B1 幂等键 `incident_id + control_generation + trigger=published`；B2 参数全部取自已加载 HealthProfile；B3（r2）代际、挂起、生命周期等失效情形作废为无操作、只留历史，仅运行性失败有界重试、耗尽后写交接原因、事故保持 `open`；B4 实验用 harness 定时 `fault restore`，产品侧无人点击；B5 工作台/F13 展示来源。

### r3（第 1 步预审，用户 2026-10-12 按推荐裁决）

- **S1-D1** 自动授权循环跑在 web 进程内的后台循环，用 `opspilot_web` 角色；多副本靠唯一键与 `SKIP LOCKED` 防重；#197 拆出 Controller 时整体迁走。
- **S1-D2** 触发由扫描持久事故状态派生：`conclusion IS NOT NULL`（只有 `publish()` 写，`steps.py:356`；`new_run`/`controls.py:399` 清空）且 `lifecycle='open'`、`mode='automatic'`、无活动会话、该 `(incident_id, control_generation)` 尚无自动授权记录。worker 权限不变。
- **S1-D3** 审计写 `opspilot_controls`：`actor='system'`，`expected_generation = resulting_generation` = 当前值，**不递增** `control_generation`；只按现有原语递增 `observation_generation`。
- 可逆默认（lead）：自动会话的 `revision` 记固定值 `automatic:no-remediation-recorded`（r4 改名）（人工登记的 revision 是操作者填写的整改后版本，自愈没有）；会话 ID 用 `uuid5(intake_key:auto-observe:<control_generation>)`。

### 第 1 步公开接口（r3，lead 按可逆默认定）

- 迁移（下一个线性号）：`opspilot_observation_sessions.authorization_source text`，CHECK：`purpose='incident_recovery'` 时 ∈ {`post_remediation`,`automatic`}（r4）且非空，`release_observation` 时为 NULL；已有 `incident_recovery` 行回填 `post_remediation`。新表 `opspilot_self_heal_authorizations`：主键 `(incident_id, control_generation)`，`state` ∈ {`authorized`,`skipped`,`retrying`,`handed_off`}，`reason`、`attempts`、`next_attempt_at`、`session_id`、`created_at`、`updated_at`。`RUNTIME_GRANTS`：web 读写该表与新列，worker 与 Observer 无写权限。
- `ObservationStore.register_remediation` 写 `authorization_source='post_remediation'`；`authorize_session_in` 显式接收 `authorization_source`。
- `Workbench.self_heal_pass(*, limit: int = 20) -> list[SelfHealOutcome]`（`opspilot/web/service.py`），`SelfHealOutcome(incident_id, control_generation, state, reason, session_id)`。候选为该代际尚无记录，或 `retrying` 且 `next_attempt_at <= now()`（r4）。每个候选一个事务：行锁（`SKIP LOCKED`）后复核条件，解析目标身份与 HealthProfile 并在同一事务补齐空的目标登记行（r4，与人工登记共用同一补齐步骤与 `TARGET_MISMATCH`/`TARGET_IDENTITY_MISSING`/`HEALTH_PROFILE_TARGET_MISMATCH` 检查），调用同一授权原语（`actor='system'`，`authorization_source='automatic'`，参数全部取自 HealthProfile，观察起点为事务 DB 时钟），写审计行（独立动作 `auto_authorize_observation`，r4）与本表 `authorized` 行。
- 失效或不适用（r2 B3）：生命周期非 open、`human_owned`、事故已暂停/已取消、`handoff_only`、全局或目标挂起、已有活动会话、无身份或 HealthProfile 不匹配 → 本表 `skipped` + 原因，终态，不改事故、不写审计。挂起解除后不补开（C3 §4）。
- 运行性失败（DB 错误等）→ `retrying`，有界重试（最多 5 次、指数退避），耗尽 → `handed_off`，并写一条工作台可见的事故事件，事故保持 `open`。
- web 启动时起后台循环，间隔 `OPSPILOT_SELF_HEAL_INTERVAL_SECONDS`（默认 30，`0` 关闭），进程退出时停止。
- 第 1 步不含：F6/F13 投影与页面展示（第 2 步）、kind 真实运行（第 3 步）。

## 执行进展与证据

- 2026-10-12 建 worktree、任务记录、SPEC 草案；Codex 合同预审完成；用户按推荐裁决 r1，F11 排在本任务之后。
- 实验环境：与另一会话（production-ops-agent-00）协调，同一时间只跑一个真实故障；真实 Run 前先确认。
- 迁移：线性链，当前 main 末尾 0012；对方先合并则 rebase 并重编号。

## 下一步与交接

授权 PR #209 待用户合并；第 1 步按 r3 公开接口实施（worktree `../poa-184-step1`，分支 `feature/F6-184-self-heal-step1`）：实现 Agent 与独立测试作者（Codex）并行，PR 在 #209 合并后对 main 提交。

### r4（PR #209 机器人分诊，用户 2026-10-12 裁决前三项）

- **R4-1** PRODUCT-CONSTRAINTS「Product workflow」修订：人工处理之外，有 HealthProfile 的目标在结论发布后也可进入独立恢复观察（用户批准）。
- **R4-2** 不作因果判断：来源值由 `self_healing` 改为 `automatic`；界面与复盘显示「已恢复，未登记处置」，不称自愈（用户批准）。替换 r1 A4/A5/B5 中的 `self_healing`/`self_healed`。
- **R4-3** 收窄 A7：自动授权后的人工优先为接管、登记处置、全局/目标挂起；暂停与取消保持现状（结论发布后不可用，`controls.py:306-309`），不改控制路径（用户批准）。
- R4-4（lead）自动路径在同一事务补齐目标身份，与人工登记共用补齐步骤，首个观察不依赖先前人工登记。
- R4-5（lead）扫描同时选取到期的 `retrying` 行。
- R4-6（lead）审计动作 `auto_authorize_observation`；第 2 步 F13 投影把它归为系统授权，不计入 `human_actions`（`knowledge/generation.py:484-497`）。
- R4-7（lead）旧来源映射只适用 `incident_recovery` 会话。

### r5（lead，2026-10-12，实施中冒出，可逆默认）

- R5-1 触发额外要求当前 Run 为 `completed` 且 `runs.control_generation = incidents.control_generation`：人工登记推进代际后结论仍在，但 G+1 没有发布，不得再次自动授权（A2、B1「trigger=published」的本义）。
- R5-2 `authorization_source` 设 `DEFAULT 'post_remediation'`，CHECK 按 `purpose` 条件约束，迁移把 release 行置 NULL；产品原语始终显式传来源（A10）。理由：无默认值会让独立 F13 测试 D53 的共享夹具（在 0011 与 head 两个版本都插入会话）无法同时正确，不为此改独立测试。对 F11 的影响：发布会话必须显式插入 NULL。
