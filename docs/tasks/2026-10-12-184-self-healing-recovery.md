# #184 结论发布后自动开启恢复观察（未登记处置时）

- 状态：合同 r6（2026-10-12）；#209 授权、#213 第 1 步、#215 第 2 步已合并；第 3 步实施中（`../poa-184-step3`）
- 更新日期：2026-10-12
- 依据：[#184](https://github.com/kevinWangSheng/production-ops-agent/issues/184)；PRODUCT-CONSTRAINTS「Product workflow」；SPEC 2026-10-12 段；C3 §4、§10「结论发布后自动开观察」段；feature_list F6（`passes` 已为 true，不得回退）；M1-02 [任务记录](2026-10-03-m1-02-recovery-observation.md)；M1-04 决定 C
- 工作区：授权 PR `chore/184-self-heal-gate`（`../poa-184-self-heal`）；第 1 步 `feature/F6-184-self-heal-step1`（`../poa-184-step1`，lab PG 55471，测试作者 PG 55472）；第 2 步 `feature/F6-184-self-heal-step2`（已合并）；第 3 步 `feature/F6-184-self-heal-step3`（`../poa-184-step3`，lab PG 55497，数据在主仓库 `tmp/184-step3/pg`）

## 目标与范围

恢复观察现在只能由人在工作台登记「已处理」开启。故障自行恢复或被人在系统外修好、但没人点击时，事故永远停在 `open`。本切片让系统在合格结论发布后，为有 HealthProfile 的目标自动授权一次独立恢复观察：持续健康则推进到 `resolved`，显示「已恢复，未登记处置」；无法确认则保持 `open` 交人。记录不作因果判断（不称「自愈」）。

不在范围：放宽 Observer 或 worker 权限；由模型认证恢复；改变「人工关闭不等于恢复」；告警 resolved 触发观察（M1-04 决定 C 不变）；结论发布后的暂停与取消。

## 完成条件

kind 实验环境真实跑通两个场景：故障由 harness 恢复、产品侧无人点击 → 自动观察推进到 `resolved`，来源 `automatic`；故障持续 → 到期保持 `open` 并写交接原因。人工登记路径与 F6 验收投影、`passes` 不回退；独立审查与独立验收测试作者按 AGENTS.md 执行。

## 合同（r6，当前有效）

r1 由用户 2026-10-12 按预审推荐裁决；r3、r4 前三项为用户裁决；其余为 lead 可逆默认。[合同决定表](2026-10-12-184-self-healing-recovery-contract.md)是预审原文与修订历史，**与本节冲突时以本节为准**。

触发与授权
- C1 候选（不满足则不写任何记录）：`incidents.conclusion IS NOT NULL`（只有 `publish()` 写，`steps.py:356`）且当前 Run `completed` 且 `runs.control_generation = incidents.control_generation`（人工登记推进代际后不再是候选）、`lifecycle` ∈ {`open`,`observing_recovery`}，且该 `(incident_id, control_generation)` 尚无记录或为到期的 `retrying`。交接不发布、`handoff_only` 无 Run，都不是候选。其余条件在行锁下复核，见 C8。
- C2 执行者：web 进程内后台循环（`opspilot_web` 角色），间隔 `OPSPILOT_SELF_HEAL_INTERVAL_SECONDS`（未设置时默认 30；`0` 关闭；非法值拒绝启动。第 1 步默认 `0`，第 2 步与 F6/F13 投影一起改为默认 30，避免投影未就绪时自动会话被计为人工处置）；多副本靠主键与 `SKIP LOCKED` 防重；#197 拆出 Controller 时整体迁走。worker 与 Observer 权限不变。
- C3 授权：与人工登记共用同一事务原语、暂停与代际栅栏。目标身份只取自配置的目标身份注册表（`OPSPILOT_TARGET_IDENTITIES`，按已登记 `resource_uid` 绑定），与人工路径共用补齐步骤与 `TARGET_MISMATCH`/`TARGET_IDENTITY_MISSING`/`HEALTH_PROFILE_TARGET_MISMATCH` 检查；不接受告警或模型提供的身份，缺失即 fail-closed。会话参数全部取自 HealthProfile，观察起点为授权事务的 DB 时钟，不延长 deadline。
- C4 版本：自动会话没有权威的目标版本，`target.revision` 记 `null`，工作台显示为未知；不写伪造的版本串。
- C5 幂等：`(incident_id, control_generation)` 至多一次；会话 ID `uuid5(intake_key:auto-observe:<control_generation>)`。

来源与审计
- C6 `opspilot_observation_sessions.authorization_source`：`incident_recovery` ∈ {`post_remediation`,`automatic`} 非空，`release_observation` 为 NULL（CHECK 按 `purpose`）。列默认 `post_remediation` 仅为旧写入方与跨版本夹具兼容（r5-2：独立 F13 测试 D53 夹具在 0011 与 head 都插入会话）；产品原语的 `authorization_source` 为必填参数、无默认值，测试断言自动会话从不落到默认值。例外（lead 可逆默认）：仅测试调用的便捷包装 `ObservationStore.authorize_session` 保留默认 `post_remediation`，产品与脚本无调用方（grep 核对），避免改动约 15 处既有测试调用。迁移回填旧 `incident_recovery` 行，release 行置 NULL；不改写归档证据。
- C7 审计：`opspilot_controls` 写独立动作 `auto_authorize_observation`，actor `system`，`expected_generation = resulting_generation`，不推进 `control_generation`；只按现有原语推进 `observation_generation`。第 2 步 F13 投影把它归为系统授权，不计入 `human_actions`。

失败与竞态
- C8 候选在行锁下复核，不满足 → 记录 `skipped` + 原因（终态，不改事故、不写审计）：`mode='human_owned'`、事故已暂停/取消、已有活动会话或 `lifecycle` 非 `open`、全局或目标挂起、无身份或 profile 不匹配、复核时已不再是 C1 候选（`NOT_PUBLISHED_AT_GENERATION`）。挂起解除后不补开。
- C9 运行性失败 → `retrying`，最多 5 次、指数退避 30×2^(n−1) 秒（扫描选取到期的 `retrying` 行）；第 5 次 → `handed_off`，与一条工作台可见的 `auto_observation_handed_off` 事故事件在同一事务提交，事故保持 `open`。记录失败时按 scope → incident → 决定行加锁并复核代际（等锁，受 4 s `lock_timeout` 约束，超时留给下一轮）：代际已变 → `skipped`/`CONTROL_GENERATION_CHANGED`，不计次、不交接、不写事件。已过期代际的 `retrying` 行（无论是否到期）在下一轮同样退役为 `skipped`。（第 1 步实施中补充：PR #209 机器人与独立代码审查 P1。）
- C10 人工优先：接管（停止自动观察）、登记处置推进代际并撤销自动会话，先提交者胜；全局/目标挂起沿用 M1-02 现有行为，自动与人工会话一致：挂起期间不领取采样（`claim_due_samples` 排除挂起 scope，`store.py:1509`），挂起后的采样结果作废，解除后会话因 scope 代际不符失效（`_scope_blocks`），不在挂起事务中逐个撤销（挂起路径本身不属本切片）。暂停与取消在结论发布后不可用，保持现状（`controls.py:306-309`）。
- C11 无 HealthProfile 的目标保持现状，只能人工处理。

第 1 步公开接口
- 迁移（下一个线性号）：上述列与新表 `opspilot_self_heal_authorizations`（主键 `(incident_id, control_generation)`；`state` ∈ `authorized/skipped/retrying/handed_off`；`reason`、`attempts`、`next_attempt_at`、`session_id`、时间戳）；`RUNTIME_GRANTS` web 读写，worker、Observer 无写。
- `Workbench.self_heal_pass(*, limit=20) -> list[SelfHealOutcome(incident_id, control_generation, state, reason, session_id)]`；`authorize_session_in(..., authorization_source)` 必填；`register_remediation` 传 `post_remediation`。
- `self_heal` 这类表名与函数名是内部名，不出现在产品展示、审计动作或 F13 投影中。
- 不含：F6/F13 投影与页面（第 2 步）、kind 真实运行（第 3 步）。

## 第 2 步决定（预审 D2-01–D2-16）

预审原文：[第 2 步预审](2026-10-12-184-step2-prereview.md)（Codex，只读）。用户 2026-10-12 授权：r6 范围内的推荐由 lead 采纳，只有改 PRODUCT-CONSTRAINTS、权限或验收步骤要求的项才停下裁决（选项 b）。本步全部按推荐采纳，无停下项。

- 预审标为 A 的 D2-02/03/04/05/07/08 已由 r6 C7 与 SPEC 2026-10-12 段确定：自动授权投影为系统授权，不进 F6 `actions`/`human_interaction`、不使 `permissions` 含 `human_control`、不进 F13 `human_actions`；F13 `recovery` 段带 `authorization_source`；F6 只增字段，既有断言与 `passes` 不变；「已恢复，未登记处置」只在 `automatic` 会话经独立观察确认、事故 `resolved` 时显示，观察中不声称恢复，人工登记保持现有语义。
- D2-09 文案：工作台为英文，采用 SPEC 原文 "recovered, no remediation recorded"，同时显示来源 `automatic` / `post_remediation`。
- D2-10 存量：采纳推荐①，按 C1 持久条件，升级开启后的首次扫描也会处理符合条件的存量事故（C1 无时间截断，持久状态为权威）。在 PR 简报中列为提示项，用户合并前可改为只处理开启后发布的结论。
- D2-11 不新增 subject event；页面靠刷新快照看到生命周期与来源。D2-13 F6 生命周期配对改为按来源/代际关联，不再按位置。D2-01/06/12/14/15/16 按推荐。
- 实施中 lead 定的公开字段名（合同未固定，可逆）：F6 `RecoveryOutcome` 新增 `authorization_sources`（与 `observation_sessions` 按序对应）、`authorization_source`、`observation_authorization["authorization_source"]`、`system_authorizations`，不往 `observation_sessions[*]` 加键（保留 `tests/test_f6_recovery_outcome.py:183` 的既有精确键断言，D2-02）；`Workbench.snapshot()` 新增顶层 `authorization_source`、`recovery_status`、`recovery_statement`；自动授权不在 F6 `actions` 中出现，open→observing_recovery 只经 `system_authorizations` 与会话可见。第 1 步独立测试中断言默认间隔 0 的一条按 C2 改为 30，由测试作者修改。
- 实施发现：F13 输入的凭据键脱敏规则（匹配 "auth"）此前把 `authorized_by`/`authorized_at` 静默删掉；现在三项来源字段在脱敏后写入（值仍经脱敏），人工路径的复盘输入因此也带授权者与时间（D2-05）。

## 第 3 步决定（预审 D3-01–D3-15）

预审原文：[第 3 步预审](2026-10-12-184-step3-prereview.md)（Codex，只读）。按用户选项 b 全部采纳推荐，无 A 类停下项。

- 入口与身份（D3-01/02）：两场景都由真实 Alertmanager webhook 进入；身份文件沿用 M1-04 的 `m0-otel-20260909` 条目，`cluster_uid` 与 kind 集群 `kube-system` UID 核对一致。
- 前提（D3-03/04）：真实 worker 发布结论满足 C1；每场景一次真实尝试，交接或未发布照实记为未达前提，不刷到成功。
- S1（D3-05/06）：看到 `automatic` 会话后立即由 harness restore；用随产品发布的 HealthProfile，不缩短健康窗。
- S2（D3-07/09/10）：只把 `session.deadline_seconds` 改为 1200 的实验 profile（[`otel-demo-checkout-deadline-1200.json`](../evidence/m1-184-self-heal-live/otel-demo-checkout-deadline-1200.json)），保证期限先于 40 次采样耗尽；经已有 `OPSPILOT_HEALTH_PROFILE` 接口加载，不改 shipped 配置。通过 = 会话 `expired`/`deadline_expired`、事故 `open`、F6 交接原因含期限到期；捕获证据后再 restore。
- 证据（D3-11/12/13）：每场景一份 `summary.json`（`lab_evidence.freeze`，原始 ledger 不入库），F6 `recovery_outcome` 与离线重放为外部断言源，各链接自己的调查 Run trace。
- 工具（D3-14）：新增取证脚本 `docs/evidence/m1-184-self-heal-live/collect.py`（lead）；通过判据 `verify.py` 及其单元测试由独立 Codex 按合同与公开接口编写。

## 执行进展与证据

- 2026-10-12 预审 → 用户裁决 r1；第 1 步预审 → r3；PR #209 机器人与独立复审 → r4–r6（用户裁决 PRODUCT-CONSTRAINTS 修订、中性来源 `automatic`、收窄人工优先）；#209 合并为 `1752639`。F11 排在本任务之后。
- 第 1 步（`feature/F6-184-self-heal-step1`）：迁移 `0013_self_heal_authorization`；`Workbench.self_heal_pass` 与 web 进程后台循环（默认关闭）；人工与自动路径共用身份补齐与授权原语；`Target.revision` 可空（仅 `automatic` 来源可为 null）；事件写入步骤移到 `opspilot/persistence/events.py` 以便与交接同事务。
- 验证（PG 17.9 本机临时实例，测试作者用 55472、实现用 55471）：独立测试作者（Codex GPT-6 Astra high，仅按合同与公开接口编写）`test_self_heal_authorization_postgres.py` 27 passed（单独运行与完整集成套件内均通过）；实现者测试 24 + 10 passed；完整集成 714 passed、54 skipped；非集成 3994 passed、2 xfailed；ruff、mypy 通过。独立代码审查（Codex GPT-6 Astra high，全新上下文）发现 1 个 P1（失败记录未复核代际），修复后同一审查者复验关闭，含真实 PG 锁超时复现（约 4.00 s 返回 TIMEOUT、不写决定）。
- 独立测试修正记录：作者初版基于 r3，更新到 r6；两处失败为测试夹具违反 `job_check`、一处断言代际写错（`generation - 1` → `generation`，语义更严）、一处共享库候选顺序依赖（显式大 `limit`），均由作者本人修改；实现者未改该文件。
- 第 2 步（`feature/F6-184-self-heal-step2`）：F6 投影把自动授权归为系统授权并暴露来源字段；F13 `human_actions` 排除自动授权、`recovery` 段带来源；工作台显示来源与 "recovered, no remediation recorded"（仅 automatic 且独立确认且 `resolved`）；循环默认 30 s。验证（PG 17.9 本机临时实例，测试作者 55482、实现 55483）：独立测试作者（Codex，仅按合同与公开接口）`test_self_heal_projection_postgres.py` 14 passed；第 1 步独立测试中默认间隔一条按 C2 由测试作者改为 30；完整集成 729 passed、54 skipped；`make check` 3850 passed、2 xfailed；mypy 通过。未覆盖：D2-06（自动授权使已有复盘变 stale）无端到端测试——真实路径需要完整人工观察周期加新 Run 才能同时满足「已有复盘」与「当前代际新结论」；代码路径为与人工登记共用的授权原语（第 1 步未改的 `mark_stale_in` 调用）。
- 第 3 步（`feature/F6-184-self-heal-step3`，2026-10-13 05:46–06:45Z）：kind 真实运行两场景，与 production-ops-agent-00 确认实验环境空闲后执行，[运行记录](../evidence/m1-184-self-heal-live/run.md)。S1：真实告警 → 真实调查发布（结论 supported）→ web 循环约 20 s 后自动授权（system、`automatic`、revision null）→ harness restore → 11 个连续健康样本后 `resolved`，事故页显示 "recovered, no remediation recorded"；S2：故障持续 → 19 个 degraded 样本 → 1200 s 期限到期 `expired`/`deadline_expired`，事故回 `open`，F6 交接原因含 `DEADLINE_EXPIRED`。两场景均无任何人工 control。独立判据（Codex 按合同与公开接口编写的 `verify.py`）S1 11/11、S2 18/18 通过（S2-10/11 为 PR 机器人发现后由判据作者补的故障时序与采样覆盖判据），离线重放一致；各附 LangSmith trace（read_back found）与冻结 `summary.json`。费用合计 1.08 CNY（两次调查 + 两份自动复盘草稿）。独立审查（Codex，全新上下文）报 4 个 P2 + 1 个 P3（时间线原文入摘要、缺脚本哈希、恢复后复核无原始记录、费用归属漏了复盘调用、restore 时间），均已修正并在 restore 后重新 verify 与冻结。未覆盖：自动授权的 C9 重试/交接、C8 跳过路径与人工优先竞态只有第 1 步集成测试，没有真实运行。

## 下一步与交接

第 1 步（#213）、第 2 步（#215）已合并；第 3 步真实运行完成，PR 合并后完成条件满足，#184 可关闭（关闭 issue 待用户确认）。F11 发布后观察排在其后（[任务记录](2026-10-12-f11-post-release.md)）。
