# M1-02 独立恢复观察（F6）

- 状态：待开始（D1–D4 已决；门槛已合并 #73；排在 M1 基础设施准备之后）
- 更新日期：2026-10-05
- 依据：[feature_list.json](../../feature_list.json) F6；[PRODUCT-CONSTRAINTS](../../PRODUCT-CONSTRAINTS.md)「Recovery observations」；[C3](../design/technical-proposal-2026-09-07.md) §4「事故与发布观察分开建模」、§10「健康规则 / 观察主体与授权 / 采样与提交」、§13 观察预算；[ADR-0003](../adr/0003-business-state-recovery-authority.md)；ROADMAP「M1-01 剩余工作」行的下一步
- 工作区：门槛文档 `../production-ops-agent-m1-02-gate`，分支 `chore/m1-02-gate`；实施按子项另建 `feature/m1-02-*` worktree

## 目标与范围

人工在系统外处置事故后，在工作台登记处置，事故进入 `observing_recovery`。随后由确定性 Observer（不调用模型）按版本化 HealthProfile 采样，最终给出三种结果之一：

- 恢复：`resolved`
- 仍异常：保持 `observing_recovery` 有界继续采样，到期交接后回到 `open`
- 无法确认：交接给人

每个结果都能从已存的采样重建判定依据。

范围内：incident_recovery 一种观察主体；OTel Demo 实验环境上的一个 HealthProfile；F6 的 5 个验收步骤。

范围外：
- release_observation（F11，M2）
- 人工 close/reopen（M1-01 已决⑥移出）
- 复盘（F13）
- 任何恢复、回滚、发布门动作

## 前提与完成条件

- 前提：
  - SPEC 门槛 PR 合并（D1）。
  - M1 基础设施准备先完成（2026-10-05 用户决定）：[schema 迁移](2026-10-05-m1-prep-schema-migrations.md)、[trace 接入](2026-10-05-m1-prep-trace-langsmith.md)。
  - 计划 0 的 kind 环境就绪。旧 Compose 环境目录（本仓库 `tmp/m0-environment` 和 sibling worktree）都已不存在，`scripts/otel_demo_lab.py up` 当前无法启动；colima `m0-otel` VM 还在（Stopped）。
- 完成条件：
  - F6 五步在真实实验环境各至少执行一次，确定性断言通过，证据在 `docs/evidence/m1-02-*`。
  - 验收与合同测试由未参与实现的全新上下文 Agent 按 C3 §10 和 F6 步骤编写。
  - 各 PR 经独立审查。
  - 用户合并功能 PR 后，按证据决定是否翻 F6 `passes`。

## 已核查的现状

| 层 | 现状 | 证据 |
|---|---|---|
| 领域合同 | 已有纯函数层：`ObservationSession`、`HealthSample`、采纳判定 `evaluate_sample`（控制版本、generation、规则版本、水位、暂停、期限）、`confirms_health`（无 profile 或缺必要信号不算健康）；事故状态机 `open → observing_recovery → resolved/open`。M1-01 期间作为 F2 领域类型写成（`6efe24d`），未接入运行时 | `opspilot/domain/observation.py`、`opspilot/domain/subjects.py:24-40` |
| 持久化 | `opspilot_incidents.lifecycle` 列存在，但只写 `'open'`。没有观察会话表和采样表，也没有 HealthProfile 存储 | `opspilot/persistence.py:217,373,696,1626` |
| 采样数据源 | 产品只读 Prometheus/Jaeger profile（#54）。工程侧独立观察脚本（`scripts/otel_demo_observe.py`）只判调查验收前提，不是产品 Observer | `opspilot/tools/otel_demo.py` |
| 环境 | OTel Demo 2.0.2 跑在 colima + Docker Compose 上，**没有 Kubernetes**，没有 deployment 状态和 pod 健康信号 | `scripts/otel_demo_lab.py:1-20`；本机无 kind/k3d/kubectl |
| 上游 | HolmesGPT main `9e21560`（2026-10-04 核对）：只有 `holmes/checks/`，check 无状态、由 LLM 判 pass/fail/error、无观察窗口、`schedule` 字段注释为未来实现；没有「处置后观察」。可借鉴 pass/fail/error 三态输出；判定须按 C3 确定性实现，不照搬 LLM 判定 | [`holmes/checks/models.py:11-45` @ `9e21560`](https://github.com/HolmesGPT/holmesgpt/blob/9e21560c7ad1d02fdac29c9d9979999992bc3edb/holmes/checks/models.py#L11-L45) |

## 计划（每项一个 PR，按序合并，不做 stacked PR）

0. **kind 实验环境**
   - colima 上起 kind，装固定版本的 OTel Demo 官方 Helm chart 和 kube-state-metrics，Prometheus 抓取 deployment/pod 指标。
   - 工程脚本提供 up/health/stop/fault 四个动作，实验环境写权限只在工程侧。
   - 回归 M1-01 的只读工具 profile（服务标签、Jaeger 地址），跑一次真实故障调查确认没有退化。
1. **HealthProfile 与采样合同**
   - 版本化 profile 文件：必要信号、PromQL、最低样本与有效流量门槛、阈值、新鲜度、持续窗口、观察期限与次数。
   - 用 OTel Demo checkout 及其依赖写一份 profile。
   - 数值先按实验环境基线校准，在任务记录写明来源，候选评测前冻结（C3 §13）。
2. **PG 观察会话与原子采纳**
   - 建观察会话表和采样表。
   - 采纳、水位推进、生命周期转换、安排下一次采样在同一事务里完成，复用 `evaluate_sample`。
   - 观察表、采样表与 Observer 专用 PG 角色及最小授权（D3）都以 Alembic 增量迁移落地；迁移只建 `NOLOGIN` 角色与授权，Observer 的登录凭据在仓库外设置（PG 17.9 实测 `CREATE ROLE` 可在事务内执行，见 #75 thread）。
   - 每次采样保存查询、时间窗、来源、每个必要信号的实际返回值（原始结果经现有证据登记，带 hash）及判定，重放只读这些存储，不再查遥测（F6 第 5 步）。
3. **人工登记处置 → 开始观察**
   - 工作台动作，带 `expected_version`、幂等键和操作者审计。
   - 递增 `control_generation`，授权新观察会话。
   - 暂停、接管、取消在同一事务里撤销观察授权（C3 §10）。
4. **Observer 采样任务**
   - 独立 Observer 进程里的确定性任务，直接以自己的只读凭据查 Prometheus，不调用模型，不经调查 worker 或其工具网关（D3）。
   - 健康窗口满足 → `resolved`。
   - 缺测、陈旧、低流量 → unknown，按原期限有界继续，到期交接，事故回到 `open`。
   - 持续异常 → 保持 `observing_recovery` 有界继续，到期交接，事故回到 `open`（C3 §10）。
   - 每个会话最多一个活动采样任务，租约重试保持原逻辑序号；全局/目标暂停同样挡住 Observer 采样（C3 §4）。
   - 角色隔离（C3 §3，D3）：Observer 用独立 PG 角色，只授予观察会话/采样表读写和事故生命周期转换所需权限，不能写调查 Run、报告或证据结论；Prometheus 只读凭据与调查侧分开发放；网络上只配置 Prometheus 与 PG 端点。调查侧凭据与 DB 角色不能用于采纳采样，Observer 凭据不能调调查工具，两条各写负向测试。
   - 调查侧（Controller/Worker/Gateway）的 C3 §3 隔离不在本切片，沿用 M1-01 现状。
5. **工作台展示与重放**
   - 事故页把调查结论和恢复判定分开展示，恢复判定附采样依据。
   - 离线重放脚本只用已存采样重算判定，结果须与已存结果一致（F6 第 5 步）。
6. **F6 验收（真实环境）**
   - 四个场景：处置后恢复、撤流量、去掉必要遥测、持续异常；再加一次重放。
   - 每个场景记录 ledger 和独立观察结果。
   - 本项不调用模型，费用只有实验环境资源。

## 已决（2026-10-03 用户）

- **D1 实施门槛**：SPEC 原先只为 M1-01 开放实施，M0 遗留项是「更大 M1 范围」的入口条件，和 ROADMAP 的下一步冲突。用户决定**有界开放 M1-02**：只开放本记录范围；observer 授权在本切片内交付；其余 M0 遗留项仍挡候选评测和更大范围。SPEC 已加一段，随门槛 PR 合入。
- **D2 实验环境**：F6 第 1 步要求 deployment 状态和 pod 健康，Compose 给不出。用户决定**迁到 kind + OTel Demo Helm + kube-state-metrics**，产品仍只经只读 Prometheus 读取，不新增连接器。
- **D3 Observer 角色隔离**（2026-10-05 用户，起因：#73 机器人 P1 引用 C3 §3「隔离必须落实到凭据、数据库权限和网络可达性」）：原计划让 Observer 与调查共用 worker 进程和网关，属未经批准的合同偏离。用户决定**只隔离 Observer**：独立进程、独立 PG 角色、独立 Prometheus 只读凭据，见计划第 2、4 项；调查侧隔离不在本切片。网络可达性在本地实验环境能做到哪一层，以第 4 项的实测证据为准，做不到的如实列为限制。

- **D4 实验环境规格**（2026-10-05 用户）：本机运行精简版——Homebrew 安装 kind、kubectl、helm（第 0 步执行时安装），colima 约 8 GiB，关闭 checkout 故障路径用不到的 OTel Demo 服务；运行期间不同时跑其他重负载。依据：本机 16 GB 内存、10 核、磁盘余 34 GB；旧 Compose VM `m0-otel` 为 6 GiB。精简后的服务清单与实测内存写进第 0 步证据；若精简后仍无法稳定运行，停在决策边界报告，不擅自改用远程环境。

## 待决

- 无。HealthProfile 的具体数值属于可逆技术细节，由实施 Agent 校准并记录来源，候选评测前冻结。

## 下一步与交接

- 门槛 PR 已合并（#73）；子项 issue 见 #82–#88。
- 上游核对已完成（见上表）；M1 基础设施准备完成后从计划 0（kind 环境）开工。
- 当前没有运行中的服务或进程。

## 第 2 步执行（2026-10-07，观察会话与原子采纳，#84）

- 工作区：`../production-ops-agent-obs-store`，分支 `feature/m1-02-observation-store`，基于 main `0c97377`。与第 1 步并行，只按「接口约定」对齐，未 import 对方代码。
- 迁移 `0003_observation_store`（downgrade 完整撤销，含条件删角色）：三张表 + `opspilot_incidents.observation_generation`；表结构、CHECK 与角色授权见 [development.md](../development.md#数据库迁移alembic)。会话行同时承载唯一的活动采样任务（`active_sample_*` 列），「每个会话最多一个活动采样任务」由结构保证；租约重试保持 `job_id` 与逻辑序号、只递增 epoch。
- 代码：`opspilot/observation/store.py` 的 `ObservationStore`（继承 `persistence.base._StoreBase`，共用连接池与超时）。`authorize_session[_in]`（Controller 侧存储原语：锁事故行、`open → observing_recovery`、递增 `observation_generation`、建会话与第一个任务；人工动作语义留给第 3 步）、`revoke_sessions[_in]`、`claim_due_samples`（`FOR UPDATE SKIP LOCKED`，全局/目标挂起只跳过不租）、`submit_sample`、`sweep_expired_sessions`、`session_history`、`replay_session`。
- `submit_sample` 事务内顺序：锁事故行 → 锁会话行 → 读控制范围（不加锁；挂起路径会锁事故行，事故行锁即排序点，且 Observer 角色没有控制表的 UPDATE 权限、无法 `FOR SHARE`）→ 租约校验（job/owner/epoch/到期，失败即 `lease_revoked` 只作历史）→ `evaluate_sample`（事故当前控制代际与观察代际作为会话代际，旧样本判 stale）→ `adopt_sample` 推进水位 → `confirms_health` 与持续窗口折叠（任何非健康已采纳样本重置连续计数）→ 会话状态机 → 事故生命周期（`recovery_confirmed` 非法则整笔回滚；`observation_ended_unconfirmed` 对已是 `open` 的事故不改生命周期）→ 安排下一任务或清空任务槽 → 写采样行（含判定与判定时的条件：生命周期、挂起、期限、租约、两种代际）→ 写读数行。被拒样本不改水位、不改生命周期、不排新任务，只释放租约让同一任务重试。
- 领域层唯一改动：`SampleReason` 增加 `lease_revoked`（C3 §10 把 owner/epoch/lease 列为提交时原子校验项，`evaluate_sample` 的输入里没有租约，由存储层判定）。`evaluate_sample`/`adopt_sample`/`confirms_health` 未改。
- 位置说明（待用户决定）：模块放在 `opspilot/observation/` 而非 `opspilot/persistence/`，因为 `tests/test_architecture.py` 的 strict xfail 把「持久化层是否建立在 domain 状态机之上」记为未决（ADR-0007 明示另决）；本模块直接建立在 domain 之上，放进 `persistence/` 会让该 xfail 变成 XPASS 失败。是否把它并入 `persistence/` 并撤掉 xfail，属架构决定。
- 验证（PostgreSQL 17.9 Homebrew 临时实例，端口 55471，数据目录在 scratchpad，`OPSPILOT_LAB_DSN` 指向，未碰 55431 lab）：空库 `make migrate` → `schema upgraded: 0003_observation_store`；`downgrade 0002` 后 15 张表、列与角色均消失、无残留授权；再 `migrate` 后 dump 与 fresh head 一致。`M1_DURABLE_POSTGRES=1 M0_B_POSTGRES=1 M0_STEP_POSTGRES=1 pytest tests/integration`：325 passed、10 skipped（仅既有 opt-in/重启 lab 用例）。新 PG 测试 `tests/integration/test_m1_02_observation_store_postgres.py`（19 例）：Observer 登录角色跑通 claim/submit/sweep/replay，同时 20 条越权语句（写 runs/steps/evidence/controls/inputs、改事故 `conclusion`/`control_generation`/`state`、读 `conclusion`、改会话参数、插会话、删改采样）全部 `InsufficientPrivilege`；持续健康窗口 → `resolved`；no_data/stale/failed/缺必要信号可采纳但不确认且重置连续计数；无 profile revision 永不确认；profile/观察代际/控制代际过期只作历史；丢失租约的样本 `lease_revoked`、重领同序号 epoch+1；目标/全局挂起挡住 claim 与采纳；期限到（提交时与 sweep 两条路径）与次数耗尽 → 会话 expired、事故回 `open`；两线程同一租约并发提交只一个推进水位；篡改已存判定后 replay 报不一致。`make check` 结果见 PR。
- 独立审查处置（2026-10-07，PR #114，全部采纳）：
  - P1-1 暂停/恢复：租约记录领取时的 global/target scope generation，提交时不等 → `suspended` 只作历史（与 Run 的 `_lease_revoked` 同一规则）；健康连续期在「已采纳样本的 window.start 晚于上一个已采纳 window_end（有间隙）」或「连续期起点的 scope generation 与当前不同」时重新起算，会话行记 `healthy_since_{global,target}_generation`，采样行记当时的两种 generation 供重放。
  - P1-2 处置前数据：会话行记 `authorized_at`；window.start 早于它的样本可采纳（水位照常推进、留底）但 `confirms_health=false`，采样行 `health_basis='window_before_authorization'`（其余取值 confirmed / not_adopted / no_health_profile / outcome_not_healthy / required_signals_missing）。按 profile 形参（窗口 300 s、持续 600 s、间隔 60 s）的测试：授权后 60k 秒取样、窗口 [t-300, t]，k=1..4 只作观察，k=5 起健康连续期从授权时刻起算，k=10（授权后 600 s，窗口 [T+300, T+600]）确认——即连续健康覆盖 ≥ 600 s 的时刻，不是审查意见写的「window+sustained = 900 s」。lead 裁决（2026-10-07）：按此语义——持续健康窗口 = 授权后连续已采纳健康样本的覆盖跨度（首个样本的 window.start 到最新样本的 window.end）≥ `sustained_window_seconds`；[T, T+300] 已全是整改后数据，不另加一个窗口长度。
  - P2-1 被拒样本：可恢复原因（profile revision 不符、挂起、窗口倒退）释放租约并把 `active_sample_due_at` 推迟一个采样间隔；不可恢复原因（控制代际/观察代际过期、事故状态不可采纳）直接结束会话（state `revoked`、`ended_reason='binding_stale'`），不再排任务，生命周期不动。
  - P2-2 生命周期护栏：触发器只允许 Observer 角色（`current_user` 为该角色或其直接成员；嵌套成员不识别，部署时登录角色须直接 `IN ROLE`）做 `observing_recovery → resolved | open`，其余 `insufficient_privilege`；代码只用这两条边。
  - 原始返回：读数表加 `raw bytea`（CHECK ≤ 131072 字节且必须带 `raw_sha256`；取 M0 的 128 KiB 响应上限）；`SignalReading.raw` 给出时自动算 sha256、与给定值不符拒绝；`replay_session` 对存有 raw 的读数重算 sha256，并比对折叠出的会话终态与已存终态（sweep/撤销导致的结束按「无样本结束」接受）、已存判定蕴含的事故生命周期（resolved / open）与事故当前生命周期。
  - profile 内容留底（PR #113 复验补充）：新表 `opspilot_health_profiles`（主键 revision、profile_id、content_sha256、content text）；用 text 而不是 jsonb 存，因为 jsonb 会重排键、改写数字格式，哈希只能对原文算。`authorize_session[_in]` 新增 `health_profile`（规范化 JSON 文本），给了 revision 必须给内容，校验 `revision == <profile_id>@<sha256(content)[:12]>` 且已存同 revision 的完整 sha256 一致，否则 `HEALTH_PROFILE_REVISION_MISMATCH`，不写任何东西；按 revision 去重（一张表而非会话列：同一 revision 多次授权只存一份，重放按 revision 取回）。会话 `health_profile_revision` 外键指向它；`session_history` 带回内容；Observer 角色只读。
  - 复验第二轮（2026-10-07，全部采纳）：P2-A 提交时 `window.end > DB now + 30 s` 按 INVALID_INPUT 拒绝、不入判定（30 s：远大于 NTP 时钟偏差、远小于任何采样间隔；Observer 用自己的 now 截窗，不能因小偏差拒掉全部样本，但未来的窗口没有遥测可覆盖）；暂停区间不计入覆盖：`opspilot_scope_controls` 没有时间列，取 `opspilot_suspension_audit` 中「当前 generation 对应行」的 `created_at`（global 行 target_id 为 NULL），只看授权之后的变化（之前的数据已由 `window_before_authorization` 排除），存在采样行 `scope_changed_at`；窗口起点早于它的样本可采纳不确认，`health_basis='window_before_scope_change'`。Observer 角色为此增加 `opspilot_suspension_audit` 只读。P2-B 提交时样本携带的 sequence / subject_control_generation / observation_generation / health_profile_revision 必须等于租约印记，否则 INVALID_INPUT、不入库、不结束会话；`binding_stale` 只在印记与租约一致而事故侧已变化时发生。P2-C `replay_session` 不再比较事故当前生命周期，改为与会话最后一条样本记录的生命周期（其 `subject_lifecycle` 施加其 `transition`）比较；sweep 结束不再推导 expected lifecycle；旧会话过期→新会话授权、resolved 后人工 close 都不再误报。P2-2 触发器改用 `pg_has_role(current_user, 'opspilot_observer', 'MEMBER')` 并排除 superuser 与表 owner（两者 pg_has_role 恒真），嵌套成员的登录角色同样被拦。
  - 第 4 步须知：`within_deadline` 用提交时的数据库时钟 `now < deadline_at`，不看样本窗口。
- 未执行：真实实验环境采样（第 4 步）、本机 55431 lab 库升级到 0003（用户待办：`make migrate`）。
- 风险：`controls.control()` 的续开路径把生命周期写回 `open` 而不撤销会话，第 3 步须在同一事务撤销（本步对这种状态的处置：确认恢复整笔回滚、到期只结束会话）；`observation_generation` 列是本步新增的权威位置，第 3 步递增 `control_generation` 时须经 `authorize_session_in` 同步递增它。

