# F11 发布后观察与调查

- 状态：PR-1 已合并（#218）；PR-2a 已合并（#223）；PR-2b 已合并（#227）；#225 加固已合并（#228）；重放部分证据缺陷已合并（#231）；U2 校准进行中
- 更新日期：2026-10-14
- 依据：[#207](https://github.com/kevinWangSheng/production-ops-agent/issues/207)；feature_list F11 四步；C3 §1（第二个入口）、§4「事故与发布观察分开建模」「人工控制与结果所有权」、§6、§9（发布观察页面与操作）、§10「观察主体与授权」「采样与提交」中发布观察条款、§12「实验环境」（发布实验变更镜像或配置，不重建 chart）；PRODUCT-CONSTRAINTS（不批准、不阻断、不执行发布）；ROADMAP M2
- 工作区：PR-1 至 #231 均已合并并清理；U2 见其任务记录

## 目标与范围

第二个入口：观察到一次发布（滚动更新）后，为该发布建立独立的 `ReleaseObservation`（不需要先有事故），由确定性 Observer 在固定期限内按 HealthProfile 采样，覆盖「先健康后变坏」的延迟回归。结论为 healthy / anomalous / unknown（或 cancelled）。异常时幂等创建或关联事故，并以发布前后版本、新旧实例证据启动独立调查。结果只作建议，不改变任何 CI 检查、部署门、rollout 或流量策略。

C3 §14 把 F11 排在 M2；本切片是提前开放。F11 `passes` 只在四步验收全部实际通过后翻转。

## SPEC 有界开放

按 r1 定稿为 `SPEC.md`「2026-10-12 user decision: F11 post-release observation」段，随授权 PR 合并（用户门）。

## 前提与完成条件

- 前提：用户裁决决定表（发布来源、版本身份、观察参数、对比口径、拆步）；实验环境能做「改镜像或配置」的真实发布。
- 完成条件（草案）：F11 四步在 kind 实验环境真实跑通——健康发布、带无关并发故障的发布、延迟回归；输出区分 healthy / degraded / unknown；零发布门或流量策略写入；独立审查与独立验收测试作者按 AGENTS.md 执行；四步都有证据后翻 `passes`。

## 合同（r1）

用户 2026-10-12 按推荐裁决 A1–A11；B1–B4 按推荐作可逆默认；用户决定先做 #184，F11 在其后（两者都改观察会话 schema，F11 rebase 到 #184 之上）。完整表见[合同决定表](2026-10-12-f11-post-release-contract.md)（Codex GPT-6 Astra，high，只读预审）。

- **A1** 首版只接外部部署 webhook（event-token 通道）；集群内 rollout 探测另立合同。
- **A2** 来源必须提供不可变 source release ID；Deployment UID + ReplicaSet UID/hash 作辅助身份。
- **A3** 版本 = image digest + 完整 pod-template/config digest；hash/generation 仅作辅助证据。
- **A4** 复用 HealthProfile 的频率、健康窗、deadline、次数，新增冻结 `released_at` 与 `min_tracking_seconds`；以 DB 时钟采纳。
- **A5** Observer 只确定性判健康或退化，因果交给事故 Run；版本/流量证据不足输出 `unknown`，不称发布回归。
- **A6** 发布异常用独立幂等键，不按时间相近自动并入告警事故；跨来源只建关联；已关闭事故走新事故规则。
- **A7** 新增独立发布观察表；会话显式主体类型与 ID，不用空 Incident 代替。
- **A8** 首版产品不持 K8s 凭据；Observer 仍只有 PG + Prometheus。
- **A9** 异常后建 Incident 与事故 Run，输入快照带发布身份、前后版本、新旧实例证据；不新增 `release` Run。
- **A10** 存储保留 `anomalous`，UI/验收投影映射为 `degraded`。
- **A11** 零发布门写入三层证明：代码面、权限负向测试、真实发布前后核对 CI/门禁/rollout/流量策略不变；四步证据齐全才翻 `passes`。

**r1 未定项**（A1/A2/A4/A5/A6/A11 与 B2 只定了方向，以下取值未裁决；在首个需要它的实施步骤合同预审中由用户裁决，裁决前该步不开工）：
- U1（A4，PR-1 预审）：`released_at` 的权威来源（webhook 事件时间或接收时的 DB 时钟）、deadline 起算点，以及延迟或重放投递时的处理。
- U2（A4，PR-1 预审）：`min_tracking_seconds` 的取值与校准依据。
- U3（A5，PR-2/PR-4 中较早者的预审）：before/after 窗口、old/new 实例的确定性比较字段、判定「证据足够」的谓词，以及无关并发故障下整体服务查询与按版本/实例切片的取舍。
- U4（A1，PR-3 预审）：发布 webhook 的认证主体绑定。现有 `event_channel` 接受 `OPSPILOT_EVENT_TOKENS` 里的任一 token（`opspilot/web/app.py:194-198`）；推荐方向是该路由只接受专用 actor（同 M1-04 E13 的 `alertmanager` 专用 actor 做法），Alertmanager token 不能提交发布。
- U5（A11，PR-3/PR-5 预审）：零发布门写入证明如何与 harness 自身的发布变更分开。harness 改镜像或配置必然改变 rollout，简单的前后对比会因此失败；推荐方向是在 harness 变更之后取基线，并以 API 审计或 actor 证据区分产品发起的写入。
- U6（A1/A2，PR-1/PR-3 预审）：webhook 载荷到不可变已登记目标的确定性解析。推荐方向同 M1-04（`docs/tasks/2026-10-10-m1-04-alert-intake.md:49`）：在 `OPSPILOT_TARGET_IDENTITIES` 中恰好命中一个才建 `ReleaseObservation`，缺失、多义或不符一律 fail-closed，不发查询、不建事故。
- U7（B2，PR-2 预审）：发布观察的采样锁顺序。B2 写的「沿用 incident-first」只适用于恢复会话；发布会话没有事故行，推荐方向是 release observation → session，恢复会话保持现有顺序。
- U8（A6，PR-4 预审）：发布异常的目标已有开放事故时，关联该事故（以事件追加发布证据、不开第二个调查、不覆盖其人工控制，C3 `technical-proposal-2026-09-07.md:374`）还是新建发布事故并只建关系。PRODUCT-CONSTRAINTS「Product workflow」允许关联新的或已有事故；A6 只排除按时间相近自动合并。用户 2026-10-12 决定留待 PR-4 预审裁决。
  - 已裁决（用户 2026-10-17，随 F2 U2）：目标已有开放事故时追加发布证据事件并写关联，不开第二个调查，不覆盖人工控制。见 [F2 合同 r1](2026-10-17-f2-contract.md) U2。

r1 只定方向。其余实现者需要猜的语义点，按 AGENTS.md「合同预审」在各步开工前的预审中列表裁决，不在本授权 PR 内穷举。

## PR-1 合同（r2）

预审与完整决定表：[F11 PR-1 合同](2026-10-12-f11-pr1-contract.md)（Codex 只读预审；用户 2026-10-12 对 P1-01–P1-09 全部采纳推荐；P1-10–P1-13 为 lead 采纳的可逆默认；公开接口 r2 由 lead 定稿）。

- P1-01（U1）`released_at` 取部署源实际变更时间，`received_at` 取 DB 时钟；deadline = `released_at` + profile `deadline_seconds`；未来时间零容差拒绝；到达时已过期 → 记录 `unknown`/`late_delivery`，不观察；重放不重置时间。
- P1-02（U2）`min_tracking_seconds` 显式传入、无默认；1200 s 仅为开发候选，正式值在 PR-2 首次授权发布观察前校准冻结。
- P1-03 唯一键 `(source, source_release_id)`；同键内容不同 → `RELEASE_IDENTITY_CONFLICT`，不覆盖；successor 不在本步。
- P1-04 前后版本 = 各容器 image digest + 完整 pod-template/config digest；Deployment/ReplicaSet 身份仅辅助；只存摘要。
- P1-05（U6）本步只要求绑定配置登记的目标并逐字段核对；载荷解析留 PR-3。
- P1-06 冻结 profile revision 与参数；缺失或错配拒绝并审计，不套默认 profile。
- P1-07 web 只有新表 SELECT 与列级 INSERT，初始状态由 DB 守卫限定；worker、Observer 无权限。
- P1-08 每次投递追加一条审计（脱敏，限 16 KiB）。
- P1-09 新表非空时拒绝降级，不级联删除。
- 执行分工（用户 2026-10-12）：后端实现由 Codex gpt-6.1-sol 完成，限定在 r2 范围；独立验收测试由全新上下文 Claude 子 Agent 只按合同与接口编写；独立审查另开 Codex 会话。

## PR-2 合同（r1）

完整合同：[F11 PR-2 合同](2026-10-13-f11-pr2-contract.md)。要点：
- 发布观察由 web 授权，授权只认冻结的校准 policy 清单 `opspilot/observation/policies/release-calibrations.json`，初始为空 → 授权返回 `RELEASE_POLICY_UNCALIBRATED`；U2 正式值由单独的 kind 校准证据任务产出、用户批准后写入（改变 r2「PR-2 预审前先校准」的顺序）。
- 暂停/恢复/取消的存储原语在 PR-2，页面在 PR-5；异常只持久化唯一 ending（`anomaly_key`），事故关联由 PR-4 消费（修改领域「异常必须已有事故」）。
- 版本切片：span `service_instance_id` = Pod UID → `kube_pod_owner` ReplicaSet（`<workload>-<after_pod_template_hash>`）与 `kube_pod_container_info` image digest 完整等于 `after.images`；映射、流量、覆盖不足一律 unknown；整体服务信号只作上下文。
- 最短跟踪按授权后实际连续覆盖计算；采样次数按发布主体累计；最早健康严格早于 deadline。
- 锁：发布采样 release→session；授权/控制 scope→release→session；恢复采样保持 incident-first。
- 拆为 PR-2a（存储、判定、守卫、重放）与 PR-2b（Prometheus 多序列读取、Observer 接线）。

## PR-3 合同（r1）

预审与完整决定表：[F11 PR-3 合同](2026-10-21-f11-pr3-contract.md)（Codex gpt-6-astra 起、gpt-6.1-sol 续，只读，基于 `91a011c`）。全部按推荐采纳（用户 2026-10-17 常设指示「决策先按推荐走」）；M 值用户 2026-10-21 选 1500。

- U4 专用 `release-deployer` actor，沿用 `OPSPILOT_EVENT_TOKENS`；其他 actor → 403 `RELEASE_ACTOR_REQUIRED`。
- U6 在 `OPSPILOT_TARGET_IDENTITIES` 中恰好命中一个；缺失、多义、不符 → 422，不建主体、不发查询。
- U5 harness 发布完成后取基线，以 actor 证据区分实验写入；再核对产品路径未写 CI／门禁／rollout／流量策略。
- P3-01 载荷必须有非空 `source_release_id` 与带时区 `released_at`，不推导、不以接收时间替代。
- P3-02 webhook 只登记不授权；授权沿用 PR-2 原语，清单为空时照实记录 `RELEASE_POLICY_UNCALIBRATED`。
- P3-03 harness 经真实 HTTP `POST /intake/version-changes` 发送（路径见 r3），记录请求／响应哈希与状态。
- D1–D6 可逆默认：业务字段 `extra=forbid`、未知字段只进脱敏限长审计；raw／semantic 双哈希；HTTP 错误映射见合同；独立 128 KiB body 上限；解析器独立模块；`publish()` 拿到 after 身份后才发送。
- 一个请求一个发布事件；harness 仅对 503／网络失败重试。
- r2（lead，2026-10-21，测试作者歧义 A3）：`resource_uid` 必填（缺失 400）；uid 未登记 → `TARGET_UNRESOLVED`；uid 命中 A 而 (cluster_uid, namespace, workload) 等于另一条 B → `TARGET_AMBIGUOUS`；uid 命中但五字段有差异 → `TARGET_MISMATCH`。M 取常量 1500，须与校准清单一致（authorize 要求相等，`release_commands.py:645`）；嵌套未知键 400，顶层未知键只入脱敏审计；空白 `source_release_id` 与 UTC 归一化溢出的 `released_at` → 400（独立审查 P2）；harness 不跟随重定向。
- r3（lead，2026-10-21，`make check` 发现）：F7 验收 `MUTATION_WORDS` 禁止路由路径含 release/deploy/rollout/target，路由改名为 `POST /intake/version-changes`，F7 路由清单由独立测试作者按「新路由须分类」流程登记；不改 F7 断言。

## PR-4 合同（r1）

预审与完整决定表：[F11 PR-4 合同](2026-10-21-f11-pr4-contract.md)（全新上下文 Claude 子 Agent，只读，基于 `91a011c`；Codex at capacity 后替代）。用户 2026-10-21 裁决 P4-01、P4-02/P4-03、P4-05 选推荐；P4-04 由 F2 PR-C 合同 C3 定（共享判据）；P4-06、P4-07 与 R1–R12 按推荐（用户 2026-10-17 常设指示）。

- P4-01 Observer 在 anomaly ending 后做一次有界确定性对比采集（before 窗整体服务、before 窗旧实例切片、after 窗整体服务、新版本切片），写入新对比记录；PR-4 可拆 4a/4b。
- P4-02 由 web 后台 pass（`opspilot_web`，与 `self_heal_pass` 同线程）消费 ending、建或关联事故，每个 ending 一个事务；不新增角色、不扩 worker 权限。
- P4-03 新表 `opspilot_release_incident_links`；守卫只放行「anomalous 且 incident_id 为空 → 非空、同事务有 link 行」。
- P4-04 采用 F2 PR-C C3 共享判据与 `choose_correlation`；锁顺序对齐 PR-C r3（advisory → scope → 挂起行 → 候选事故）。
- P4-05 调查输入 v6，`release_observation` 事实经新边界常量作为不可信数据消息；接受所有 Run 的 `prompt_revision` 变化，worker 先于或与 web 同时升级。
- P4-06 PR-4 在 PR-C 之后合并；迁移号取开工时 main head + 1。
- P4-07 真实运行等正式 policy；kind 中健康发布、健康发布 + adFailure、延迟回归（静默告警，新建路径）、延迟回归（告警开启，追加路径）。

## 执行进展与证据

- 2026-10-12 建 worktree、任务记录、SPEC 草案、issue；派发 Codex 合同预审。
- 实验环境：与另一会话（production-ops-agent-00）及 #184 协调，同一时间只跑一个真实故障。
- 迁移：线性链，#184 第 1 步合并后 main 末尾为 `0013_self_heal_authorization`；PR-1 为 `0014_release_observations`。
- 2026-10-12/13 PR-1（`feature/F11-pr1-release-persistence`）：Codex gpt-6.1-sol 实现（迁移 0014、`opspilot/observation/releases.py`；最小提取 `observation/bindings.py` 与中立 `opspilot/redaction.py`，旧入口保留）；独立验收测试由全新上下文 Claude 子 Agent 只按合同编写（`tests/integration/test_f11_release_store_postgres.py`，在 `feature/F11-pr1-tests` 提交后拣入）。r3 来自测试作者歧义清单与实现者合同问题：identity 可空、late 守卫须 DB 确认已过期、序号用 advisory lock（web 无 UPDATE 不能行锁）、profile 拒绝码、脱敏不得从调查侧导入。独立审查（Codex gpt-6-astra，全新会话）2 个 P2：指纹含调用参数致同一目标两种传参误判冲突（已修，`9fa9e35`）；验收未经公开写入口覆盖存储故障（测试作者补锁超时/权限拒绝，`6543dce`）。PR #218 机器人分诊采纳 2 项（DTO 拒绝 NUL/不成对代理字符，`696df78`，测试作者补 4 个结构非法用例 `1673a4e`；审计正文中的 NUL 替换为 U+FFFD、`raw_sha256` 仍覆盖原字节，`25c8537`，lead 自核并做红证明）。检查（PG 17.9 本机临时实例 55501，代码为 `25c8537`）：独立验收 75 passed；完整集成 861 passed、38 skipped；`make check` 4949 passed、2 xfailed。无模型、无 kind 调用。
- 收尾四个数（PR-1）：实施期间新冒出的合同问题 6（r3），另机器人分诊 2 项缺陷；因合同变更重写的测试 0；环境重跑 0；用户裁决轮数 1。
- 2026-10-13/14 PR-2a（[#223](https://github.com/kevinWangSheng/production-ops-agent/pull/223)，`e0d39f9`）：Codex gpt-6.1-sol 实现；独立验收测试由全新上下文 Claude 子 Agent 只按合同编写；首跑失败经独立分诊，形成合同 r2–r3 补充，3 个旧测试按 P2-03/P2-08/P2-09 更新。独立审查（Codex gpt-6-astra，含非 owner 角色裸 SQL 故障注入）1 P1 + 4 P2 已修复复验；机器人 7 轮，采纳 12、拒绝 1、转 [#225](https://github.com/kevinWangSheng/production-ops-agent/issues/225) 2，逐轮处置见 #223 thread。r4：零查询证据按合法 unknown 计次（用户 2026-10-14 确认）。检查：独立验收 74＋110 passed；`make check`（含 PG 集成）5982 passed。无模型、无 kind 调用。
- 收尾四个数（PR-2a）：实施期间新冒出的合同问题 6（r2 两项、r3 四项）；因合同变更重写的测试 3（旧测试按 P2 裁决）；环境重跑 0；用户裁决轮数 1。
- 2026-10-14 PR-2b（`feature/F11-pr2b-telemetry`）：开工预审（Codex gpt-6-astra，只读）无合同层空白，四项可逆细节写入合同 r5。Codex gpt-6.1-sol 实现 `instant_vector`、`take_release_sample` 与 Observer 接线；独立验收测试由全新上下文 Claude 子 Agent 只按合同编写（55 条），首跑 2 条失败经 lead 分诊均为测试错误（计划中 coverage／freshness 共用查询文本、会话 purpose 取值为 `incident_recovery`），修正后 55 passed。独立审查（Codex gpt-6-astra，全新会话，含真实 HTTP 慢响应注入）无 P1/P2；四个测试文件 121 passed。实现者 `make check`（含 PG）6048 passed。未接真实 Prometheus／kind，校准清单仍为空，无模型调用。
- 收尾四个数（PR-2b）：新冒出的合同问题 0（r5 为开工预审）；因合同变更重写的测试 0；环境重跑 2（uv 缓存权限、同库并发竞争）；用户裁决轮数 0。
- 2026-10-14 #225 加固（`feature/F11-release-hardening`）：迁移 0016 替换到期证据守卫（downgrade 恢复 0015 函数），重放对每种 ending reason 校验完整边及真实终结事实。lead 裁决：issue 措辞过窄，以合同 §9 为准——无 authorized session 时允许只写审计（含暂停或 scope 撤销后的 observing），有 authorized session 时须同事务 deadline ending 并结束该 session。Codex gpt-6.1-sol 实现；独立验收测试由全新上下文 Claude 子 Agent 只按合同编写（26 条，修复前 18 红，修复后全绿）；独立审查（Codex gpt-6-astra，PG 故障注入）1 P2（终结 ending 未与真实主体终态核对）已修复并经同一审查者复验。4 个旧测试只同步迁移头版本号。实现者 `make check`（含 PG）6098 passed。
- PR #228 机器人分诊采纳 1 项 P2：有 session 的到期分支补齐同事务 applied 到期审计，并逐项匹配 session／ending、状态、代际与 paused（合法清扫原本就写完整审计）；实现测试补缺审计与引用错配负例。实现者 `make check` 6110 passed。
- PR #228 机器人第二轮采纳 3 项 P2（均为篡改归档后重放仍判一致）：控制驱动的 authority_revoked ending 须恰被一条匹配 applied pause／resume 审计引用；采样终结 receipt 的 ending_id 须等于 ending；所有 ending 须 authorized_at ≤ recorded_at。实现测试补对应篡改（修复前红）。实现者 `make check` 6126 passed。
- PR #228 第三轮（r6）：重放把 ending 原因与产生它的审计／采样依据双向绑定；无采样会话的 scope／binding 原因互换、以及暂停审计未记录旧会话时的归因，归档无区分证据，列为已知限制（不影响发布状态与结论，用户 2026-10-14 确认选 A）。实现者 `make check` 6200 passed。
- PR #228 第四轮（最后一轮，之后不再触发机器人）：0016 要求 deadline ending 无 sample，与重放一致；重放把样本回执与审计 result 快照中重复保存的引用与结果字段全部与权威记录比对。lead 决定：写入检查接受手写的、缺 job／result 字段或结束时间不精确的合法形状事务，与完整归档重放的要求存在差异，保留——守卫按合同只防代码／SQL 错误，完整性由重放负责，产品代码总写齐字段。实现者 `make check` 6417 passed。
- 收尾四个数（#225）：新冒出的合同问题 3（issue 与合同 §9 口径；r6 旧归档证据边界；写入检查与重放的最小证据差异，lead 保留）；因合同变更重写的测试 0；环境重跑 2（uv 缓存权限；审查提示措辞触发内容过滤）；用户裁决轮数 1（r6 选 A）；机器人 4 轮，采纳 8。
- 2026-10-14 重放部分证据缺陷（`feature/F11-replay-partial-evidence`，来自 #228 最终独立复核第 1 项；其余 5 类转 [#230](https://github.com/kevinWangSheng/production-ops-agent/issues/230)）：采样中途取消被重放误报 `QUERY_MISMATCH`。存储与重放改为共用同一判定函数，重放按存储顺序重算处置并比较（合同 r7）。Codex gpt-6.1-sol 实现，修复前 5／35 查询后取消为红；独立审查（Codex gpt-6-astra）无 P1/P2，存储端 8192 组条件组合与基线一致，回归 749 passed；实现者 `make check` 6786 passed。
- 2026-10-22 PR-3 kind 真实运行（`68221a2`，轮次 `f11-pr3-live-1`，新建临时 PG 55651，工作台为本分支）：harness 发布→watch→真实 HTTP webhook 201 created，同载荷重放 200 replayed（主体仍为 1 条），授权返回 `RELEASE_POLICY_UNCALIBRATED`（清单为空；本分支无 HTTP 授权路由，以 web 角色调用存储原语）；alertmanager actor 403 `RELEASE_ACTOR_REQUIRED`，未登记目标 422，均不写主体。U5：harness 发布后取基线，到授权结束，308 个策略面对象零差异；harness 写入由 managedFields `kubectl-patch`@R 区分。kind 未开审计日志；集群外 CI 只有静态证据。证据见 [run.md](../evidence/f11-pr3-release-intake-live/run.md)、[summary.json](../evidence/f11-pr3-release-intake-live/summary.json)。无模型调用，费用 0。
- r4（2026-10-22，lead 采纳 PR #298 机器人 P2，依据 PR-3 合同验收第 10 项「无半个主体」）：发布投递不再由 web 层先在独立事务登记目标；`ReleaseStore.record_release` 增加 `resource_uid` 入口，未登记目标在发布事务的拒绝回滚点内登记（同告警接入 `ON CONFLICT` 复用并发首次登记），拒绝（未来时间、身份冲突、profile 不符）与存储失败不留 `opspilot_targets`／`opspilot_target_suspensions` 行；不新增权限与迁移（web 角色 0011 已有两表 SELECT 与键列 INSERT）。新增 `tests/integration/test_f11_pr3_impl_target_txn_postgres.py` 5 项，修复前 4 红（含并发首次登记）；本机临时 PG 55791：PR-3 单元＋PG、发布存储 PR-1/PR-2 集成共 341 passed。
- r5（2026-10-22，lead 采纳 PR #298 机器人安全 P2，依据 U4 与「合同外权限扩张不在授权范围」）：release-deployer token 与其他事件 token 同在 `OPSPILOT_EVENT_TOKENS`，原先可经 `/intake/events` 建事故与 Run、经 `/intake/alertmanager` 投告警。两路由改为认证后、读 body 前拒绝该 actor：`403 RELEASE_ACTOR_FORBIDDEN`，不写任何行；其他 actor 不变，无权限与迁移改动。新增 `tests/integration/test_f11_pr3_impl_generic_intake_postgres.py` 4 项，修复前 3 红（`/intake/events` 实际 201 建出事故）；临时 PG 55793：F7 入口、M1 接入/认证、M1-04 告警、PR-3 全部相关测试 489 passed。
- r6（2026-10-22，lead 采纳 PR #298 机器人 P1，kind-298 实跑可复现）：`kind_lab up` 原把 release-deployer 哈希写进第二个 `workbench-release-deployer.env`，按文档只 source `workbench-alertmanager.env` 启动的工作台对 harness webhook 全部 401。改为两条 `<sha256>=actor` 并入标准文件的同一个 `OPSPILOT_EVENT_TOKENS`（600、只含哈希），不再写第二个文件；选并入而非改文档，因为既有启动流程与证据都只 source 一个文件。`docs/development.md` 告警段补一句。新增单测：source 该文件后经 `_pairs`→`Authenticator` 两个 actor 都认证通过、保留已有 token；kind_lab/harness/PR-3 单测 183 passed、1 skipped（nginx 不可用）。`up` 本身的接线未在 kind 上复跑。
  - 独立审查（全新上下文 Claude Opus，c96308d）PASS：失败路径零残留、与告警接入并发登记同一 uid 无死锁路径（读码推断）、无新权限。已知：从未登记的目标首次投递被拒时，审计行 semantic_sha256 用随机暂定 target_id 计算，同一载荷两次被拒的值不同；该字段只用于成功投递的核对，不违反合同 D2／验收第 13 项，lead 决定不改。
- 2026-10-22 PR-4a 对比采集（`feature/F11-pr4a-release-comparison`，worktree `poa-f11-pr4a`）：Claude Opus 5.5 实现。新表 `opspilot_release_comparisons`／`_comparison_queries`（r3 加 `_comparison_attempts`；迁移 `0019_release_comparisons` 接 0018；与 B1 #321 的 0019 撞号——若 B1 先合并，本迁移改为 `0020` 接 `0019_incident_priority`）。Observer 每轮在采样后取 1 个待对比的 anomaly ending，在 `released_at` 复用 PR-2 映射／判定求 before_service 与 before_old（49 查询／次，最多 3 次，仅失败或超时重试，每次 120 s 预算），after 两个 scope 取自 anomaly 样本；存储重算并幂等（先写者为准，r3 起须持有已领取的尝试）；挂起时不取、不查，采集中被挂起则不提交。重放新增 `COMPARISON_MISMATCH`。可逆默认：原因码写成 `CODE:<scope>[:<signal>]`；值只在证据新鲜且充足时给出；对比行不进 `release_history`（PR-2 固定形状），由 `release_comparisons()` 与 `session_history` 提供；Observer 对两表有 SELECT（幂等读与待办查询需要）。
- S3 澄清（lead 2026-10-22）：前后 pod-template-hash 相同即判 `VERSION_SLICE_NOT_DISTINGUISHABLE`，不再要求镜像也相同；镜像相同但 hash 不同照常切片。实现已按此（`before_context` 在 hash 相同时两侧都取 before 版本），单测覆盖 hash 相同而镜像不同。
- 检查（PG 17 临时实例 56021）：实现测试 19 passed；F11／迁移／角色／M1-02／#184／F2 PR-C 相关单测与 PG 1508 passed；ruff、mypy 通过。同步旧测试只改迁移头与 0014 降级授权枚举。未做 kind 真实运行。
- PR-4a r3（独立审查 Codex gpt-6-luna FAIL，3 P1 + 1 P2，均采纳）：① 生产 `ObserverLoop.run` 把对比放到独立线程，采样线程用 `poll_once(compare=False)`，慢 Prometheus 下到期采样不等待对比（`poll_once()` 默认仍同步做对比，供测试与单次调用）；② 新表 `opspilot_release_comparison_attempts`（追加式，Observer 只有 SELECT／列级 INSERT）：每次尝试在发查询前先插一行作为领取，主键加守卫保证每个 ending 至多 3 行，且其他 Observer 只能在前一次 150 s 租约过期后领下一次；三次都用完仍无记录时，不再查询，直接记 unavailable。总查询 ≤ 3×49，与 Observer 数量和崩溃次数无关。取代「重试不持久化」「两 Observer 可各采一次」两条可逆默认；③ 0019 守卫在库内用 PL/pgSQL SHA-1 重算 `uuid5(observation_id,'comparison:'||ending_id)`（不引入扩展），并核对 before 窗起点 = `released_at` − 冻结 profile 的 `evaluation_window_seconds`，以及 after 窗、`decision_at` 与已领取的尝试行；④ complete 还要求 anomaly 样本证据充足、判为 degraded、after 信号全部有值，否则加 `AFTER_EVIDENCE_INSUFFICIENT`。`pending_comparisons` 不列出持有有效租约的 ending。
- PR-4a r4（r3 复验 Codex gpt-6-sol，修复引入的 2 条 P1，采纳）：① 停机：对比在每个查询前、领取下一次尝试前、提交前检查 stop，停机即放弃当前尝试，不提交（尝试行仍计次，按租约规则由后续接管）；`run` 等待对比线程最多 `comparison_join_seconds`=30 s（与 Observer 默认停机宽限 `OPSPILOT_OBSERVER_GRACE_SECONDS` 一致；在途查询只能靠它自身的超时结束），超时记日志后退出；② 租约 fencing：对比记录新增 `owner` 列。守卫与存储（`COMPARISON_LEASE_LOST`）都要求提交者持有该 comparison 的最新尝试，且按 DB 时钟仍在租约内；唯一例外是第 3 次尝试租约已过期时，只能记 unavailable（exhausted 路径）。
- PR-4a r5（r4 复验 Codex gpt-6-luna，采纳 P1-1、P1-2；第三条待顾问裁决，未做）：① 领取（插尝试行）与提交（插对比记录）都先取同一把按 comparison 区分的事务级 advisory 锁，键为 `hashtextextended('release-comparison:'||comparison_id,0)`，沿用仓库 `release-audit:` 的写法；两个守卫内也取这把锁，直接写 SQL 也绕不过去。用两连接分步交错的测试复现原竞态：A 在未提交事务中写记录，租约过期，B 领取第 2 次尝试，此时 B 被阻塞；A 提交后 B 被拒。去掉守卫里的锁后该测试变红。② exhausted 只在「最新尝试是第 3 次，且其 lease_until ≤ DB 时钟」时成立，与 owner 无关；任何 owner 持有有效租约的 ending 都不可领取（自身续领只走 `claim_next_attempt`）；伪造的 exhausted 提交返回 `COMPARISON_LEASE_LOST`。③ r4 复验第 3 条「join 超时后残留线程仍可提交」：顾问裁决 B，拒绝，不改代码。理由：残留线程提交的是已完成采集的证据；`submit_comparison` 仍在同一事务内原子校验有效租约、最新尝试和挂起状态与代际，不会写入不完整的数据，也不会绕过挂起；停机只要求主循环有界退出，这一点已满足。
- 迁移改号（2026-10-22，B1 #321 先合入，main 迁移头为 `0019_incident_priority`）：按 P4-06 改为 `0020_release_comparisons`，接 `0019_incident_priority`。合并 origin/main 时同步迁移头常量；0014 降级授权枚举取两侧并集（B1 的 priority 列加本 PR 的三张表）。

## 下一步与交接

PR-1 已合并（#218，`8299970`）。PR-2a 已合并（#223，`e0d39f9`）；PR-2b 已合并（#227，`92920f1`）；#225 加固已合并（#228，`147115d`）；重放部分证据缺陷已合并（#231，`50cc62a`）；#230 待排；U2 校准证据任务进行中，见 [U2 任务记录](2026-10-14-f11-u2-calibration.md)（实验工具 #233 已合并，待开跑）。

- 来自 #184 r5-2：`opspilot_observation_sessions.authorization_source` 默认值为 `post_remediation`，发布观察会话必须显式插入 NULL。
