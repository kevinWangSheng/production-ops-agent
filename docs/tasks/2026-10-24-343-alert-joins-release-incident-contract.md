# #343 合同预审（r1）：发布异常先开事故后，告警如何关联（反向顺序）

- 预审者：全新上下文 Claude（Opus 5.5），只读，未参与 #336 / PR-C 实现。基线 main `1904345`（含 F11 PR-4b）。未跑测试、服务、模型或 kind。
- 依据：issue #343；F2 合同 U1-A（`docs/tasks/2026-10-17-f2-contract.md:44`）；F2 PR-C 合同 C3、R-C2–R-C4、§6（`docs/tasks/2026-10-21-f2-c-contract.md:88,96-98,151-169`）；F11 PR-4 合同 P4-04、P4-07、R2、§0、§8（`docs/tasks/2026-10-21-f11-pr4-contract.md:23,88,91,98,192`）；F11 A6、U8（`docs/tasks/2026-10-12-f11-post-release.md:33,48`）；#305 合同 W3-A（`docs/tasks/2026-10-24-305-successor-window-contract.md:36`）；证据 `docs/evidence/f11-pr4b-live/run.md:45-58,67`。

## 0. 现状（已核对）

- 告警候选集只有「同 fingerprint、同 resource_uid、非交接的告警 identity 所开事故」：`opspilot/persistence/alerts.py:195-198`（SQL JOIN `opspilot_alert_identities`），再交 `choose_correlation`（`opspilot/persistence/correlation.py:40-53`）。发布开出的事故没有 identity 行，永远不是告警候选。
- 发布侧候选是同 `target_id` 的**任意**事故（`opspilot/observation/release_commands.py:908-912`），所以「告警先开 → 发布追加」已成立（S4 PASS，`run.md:67`）。
- 两侧目标身份可直接对齐：告警 `_alert_target` 按 `resource_uid` 落到 `opspilot_targets` 行（`alerts.py:213-235`，`resource_uid` 唯一），发布事故 `incidents.target_id` 是同表 UUID（`release_commands.py:984`）；S4 中发布确实附加到了告警事故，说明真实环境两者同一行。注意 `opspilot_alert_identities.target_id` 是 text，存的是 resource_uid（`0010_alert_intake.py:71`，`alerts.py:197`），而 `opspilot_incidents.target_id` 是 UUID。
- 实测时序：anomalous 11:02:35、对比 11:03:43、告警投递 11:04:41（`run.md:48,56`）；S4 靠把 web pass 间隔临时设 0 才得到「告警先开」。默认 30 s 间隔下会先建发布事故与 Run，再建告警事故与第二个 Run——**推断，未运行**（`run.md:58`）。
- 合同现状：PR-C §6 第 5 条明确写「发布或手工开的同目标事故：不参与告警附加」（`f2-c-contract.md:163`）——即现行行为是已批准合同，不是遗漏；但仓库内没有测试断言它（grep `tests/acceptance/test_f2_alert_correlation.py`、`tests/integration/test_f2_c_impl_correlation_postgres.py` 未见发布/手工事故用例）。F11 PR-4 合同 §0/§8 把「告警先开」写成按 U2 推断（`f11-pr4-contract.md:23,192`），实测推翻。
- 权限：告警接入在 web（`opspilot_web`）执行；web 已有 `opspilot_release_incident_links` SELECT（`opspilot/persistence/grants.py:214-218`）、`opspilot_alert_deliveries` INSERT、inputs INSERT。`attached` 已在 outcome CHECK 中（0018）。投递行 `incident_id` 只是 FK 到 incidents，没有触发器要求被附加事故有 identity（`0018_alert_correlation.py` 只有两个 CHECK）。
- 读侧：`DurableWebStore.alert_records` 在没有 identity 时返回 None（`opspilot/web/store.py:837-840`），所以发布事故页不会出现告警区；`alert_intake_outcome` 对无 identity 抛 `NOT_ALERT_INTAKE`（`opspilot/acceptance_alert.py:240-242`）。附加事件作为 `kind='event'` 输入本身会进事故事件流与 Run 输入。`_REFIRED` 标记只看 attached 投递，与来源无关（`web/store.py:883-889`）。
- #305 实现中（`/Users/shenghuikevin/dev/AI/poa-305` 未提交 diff）：`_attached_reframe` 只对带告警锚点的 Run 生效，注释写「a Run without an alert anchor (submitted, release) never moves (W3-A)」。所以告警附加到发布事故不会触发换框。
- 上游 HolmesGPT：CLI 对每条 Alertmanager issue 逐条独立调查（`docs/research/holmes-source-audit-2026-09-07.md:15-17`），发布 trigger 生成独立 HealthCheck（:87）；审计里没有跨来源关联做法——**未确认**有可照搬的方案。

## 1. 九类必答

1. **时间**：已定——不设时间窗，「未 closed」即边界（F2 合同 :28、U1-A）；A6 禁止按时间相近合并（`f11-post-release.md:33`）；DB 时钟。本项不引入新时间口径。待决：无。
2. **计数与口径**：已定——每个附加 episode 首个 firing 只追加 1 条事件（R-C3）；附加不开 Run（U1-A）；发布异常至多 1 个事故/Run 或 1 条事件（F11 R1）。新增：一个发布事故至多被**一个** alert fingerprint 认领（U1-A 推荐的认领规则），保证「不同 fingerprint 各自成事故」。待决：U1。
3. **状态转移**：已定——附加不改 state/lifecycle/mode/control_generation/conclusion，只可能提高优先级（B1 U-B1-2）；closed 不重开（C3 §4:105）。待决：发布事故是否成为告警候选（U1）；closed 发布事故能否作 `related_incident_id`（U2）。
4. **主体与权限**：已定——只在 web 执行，所需 SELECT/INSERT 均已授（见 §0）。**不新增任何角色权限**，worker/Observer 不变。
5. **数据合同**：无迁移。附加事实仍是 `opspilot_alert_deliveries`（outcome=`attached`，`incident_id`=发布事故）+ 1 条 event 输入（C1-A 不变）。输入版本、`tool_schema_revision`、`prompt_revision` 不变（事件 content 键集合不变，只多一个文本模板，R3）。部署：只发 web。
6. **旧数据与已归档证据**：既有分开的发布事故与告警事故不合并、不回填；`f11-pr4b-live`、`f2-alert-correlation-live` 按当时行为解读不改写。上线后，库中已开放的发布事故开始接收同目标告警——合同意图。
7. **并发与水位**：已定——R-C2 锁序（fp advisory → scope → 挂起行 → 候选事故 FOR UPDATE，id 序）；发布消费与告警首投并发时可能多建一个事故，F11 R2 已按 A6 接受（`f11-pr4-contract.md:98`）。新增：两个不同 fingerprint 并发争同一未认领发布事故——必须在锁住候选行**之后**用新语句复查认领（READ COMMITTED 下新语句取新快照，`persistence/base.py:139` 默认隔离），见 R2。水位：附加事件按输入水位在安全边界纳入（C3 §4:109）。
8. **外部调用**：接入不发外部调用；结果是少一个 Run（少一次模型/工具预算消耗）。
9. **验收投影与 `passes`**：不改 F2/F11 步骤文字，不翻 `passes`。F2 第 1 步「declared correlation policy joins related signals」：现行声明下，默认时序的真实会话会出现同因两事故两 Run，诚实判定只能是部分通过或需人为控序；F11 第 3 步文字（延迟回归被定时跟进观察、不无休止重查健康发布）不直接被违反，但其真实运行在告警开启时会产生二次调查，与 U8「不开第二个调查」意图冲突。见 U3。

## 2. 决定表

### 需用户裁决（改已批准的 PR-C §6 第 5 条、`related_incident_id` 语义、验收证据形态）

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 |
|---|---|---|---|---|
| U1 | 告警到达时，是否、按什么判据附加到同目标上由发布异常新建的开放事故 | **A（认领）**：告警候选 = 现有同 fingerprint 告警事故 ∪ 同目标（`incidents.target_id` = 本次 resource_uid 解析出的 `opspilot_targets.target_id`）上由发布开出的事故（link 表 `disposition IN ('created','created_related')`），但排除已被**其他 fingerprint** 附加过的发布事故（存在 `outcome='attached' AND fingerprint<>本次` 的投递）；并集交给 `choose_correlation`。结果：发布先开时首个告警 fingerprint 附加（无新 Run），第二个不同 fingerprint 各自成事故，与告警先开时的结果对称（告警先开：X 开事故、发布追加、Y 另开）。代价：多一次 SQL；PR-C §6 第 5 条改为「手工开的不参与，发布开的按认领参与」。**B（任意）**：同目标任何未关闭发布事故吸收任何告警，与发布侧「同目标任意事故」完全对称。代价：发布事故开放期间同目标两个不同 fingerprint 会合进一个事故，违背 U1-A「不同 fingerprint 各自成事故」与 F2 步骤 1 后半句。**C（维持）**：只改文档，把单向关联写进声明策略；真实验收靠控序（像 S4 关 pass）。代价：零代码；默认时序同因两 Run，F2 步骤 1 只能部分证明，F11 真实运行需人为控序，近似削弱验收。**D（时间窗）**：发布事故 N 分钟内接收告警——A6 明确排除，不可选。 | **A** | `persistence/alerts.py` `_correlate`、PR-C 合同 §6.5、F2 步骤 1、F11 第 3 步真实运行 |
| U2 | 同目标发布事故已 closed 时，新告警事故的 `related_incident_id` 是否可以指向它 | **A**：按 U1-A 的候选集原样交 `choose_correlation`：没有开放候选时，关联最新的 closed 候选（可能是发布事故）。与发布侧「closed 告警事故 → `created_related`」对称，符合 A6「跨来源只建关联」，共享函数不改。代价：一个 closed 的老发布事故（无时间窗）可能成为新告警事故的关联对象；同 fingerprint 只有 closed 告警事故时，若发布事故更新，关联对象从告警事故变为发布事故。**B**：closed 发布事故不进候选，关联只按同 fingerprint。代价：候选集按 lifecycle 分来源，规则不再是一个函数调用；跨来源关联只在一个方向存在。 | **A** | `related_incident_id` 语义、页面关联链接 |
| U3 | 排期与验收证据 | **A**：#343 在 F2 PR-E 之前合并（也在 F11 PR-5 真实运行之前）；合并门只要求 PG 确定性测试（本 PR 不触碰调查 loop、报告校验、恢复路径，ADR-0006 不强制 trace）；真实证据并入 PR-E kind 串行会话：一轮延迟回归、告警开启、web pass 保持默认 30 s、不控序，断言「发布 `created` + 1 个 Run → 告警 `attached` 到同一事故、该事故 Run 仍 1 个、全库因该故障只有 1 个 Run」，附发布 Run 的 LangSmith trace（事件出现在输入）与 `summary.json`；若当轮实际顺序是告警先开，如实记录并重跑一轮。PR-E 独立验收作者在 F2 步骤 1 测试中加入反向顺序的确定性用例。**B**：PR-E 先做，#343 后补，PR-E 真实会话照 S4 控序。代价：F2 步骤 1 证据靠人为控序，`passes` 翻转后默认部署仍会二次调查。**C**：单独开 kind 窗口取证。代价：多占一个窗口，kind 资源紧（swap）。 | **A** | PR-E 排期、F2 步骤 1、F11 PR-5 |

三类停止情况检查：推荐**不**扩大产品权限（无新授权、无新写路径，见 §1.4）；**不**削弱或改写验收步骤文字（F2/F11 步骤原文不动，只收紧 PR-E 测试覆盖）；**不**违背 PRODUCT-CONSTRAINTS（只读不变；附加不改人工控制字段；「abnormality linked to a new/existing Incident」`PRODUCT-CONSTRAINTS.md:22` 允许；不按时间合并，A6 满足）。选项 C 接近「为迁就实现而以控序取证」，不推荐。

### 可逆默认（Agent 自定，每项一行理由）

| 编号 | 内容 | 理由 |
|---|---|---|
| R1 | 候选读取改为一条语句：`opspilot_incidents i` 上 `(EXISTS 同 fp 非交接 identity) OR (i.target_id=<解析 target UUID> AND EXISTS link created/created_related)`，`ORDER BY i.incident_id FOR UPDATE OF i`；目标行不存在时只走原 identity 条件。锁序完全沿用 R-C2。 | 不新增锁类型，与发布消费的 incidents id 序一致，无死锁新面。 |
| R2 | 认领排除在候选行锁住**之后**单独一条语句查 `opspilot_alert_deliveries WHERE incident_id=ANY(候选发布事故) AND outcome='attached' AND fingerprint<>%s`；被排除者不进 `choose_correlation`。 | READ COMMITTED 下新语句能看到已提交的并发附加，避免两个 fingerprint 同时认领。 |
| R3 | 附加到发布事故的事件用单独英文模板（如 "Alertmanager alert {fp} fired with startsAt {ts} (status firing) on this incident's target; attached to this release-anomaly incident without a new Run."），content 键集合与 R-C4 相同；同 fingerprint 附加的原模板逐字不变。 | 原文 "fired again" 对首次附加不真；键不变则输入版本与读侧不变。 |
| R4 | 优先级：附加到发布事故也按 B1 `_escalate_priority` 只升不降（发布事故默认 1，`release_commands.py:43`）。 | 与告警附加一致；F11 r3 的「发布追加不改优先级」只约束发布侧。 |
| R5 | 发布消费与告警首投真正并发（两边都未见对方）时可能两个事故，沿用 F11 R2 接受，不加 target 级 advisory 锁。 | 实测两者相隔约 1 分钟；加锁需改发布路径，超出本项。 |
| R6 | 告警附加到发布事故不触发 #305 换框；告警注解/alert_context 不进发布 Run（R-C4）；发布事故续开仍按 `_release_rebuild`。**2026-10-26 修订（[#347](https://github.com/kevinWangSheng/production-ops-agent/issues/347)）：首句作废，附加告警 episode 晚于前一框终点时后继按 #305 W1-B 换框，锚点仍为 `released_at`，发布事实不变；后两句不变。** | 与 W3-A 镜像；#305 实现已按「无告警锚点不动」处理。 |
| R7 | 页面不加新区块：发布事故的 `alert_records` 仍为 None，附加通过事件流可见，`_REFIRED` 照常。 | 最小改动；独立的告警投递视图可另开 issue（可选）。 |
| R8 | 手工/提交事故、交接事故（target NULL，含 `RELEASE_TARGET_UNRESOLVED`）、他目标事故仍不是告警候选；告警自身解析为交接时不关联（不变）。 | 保留 PR-C §6.5 中仍成立的部分。 |
| R9 | 文档：`alerts.py` 模块与 `_correlate` docstring、`correlation.py` 模块 docstring、F2 任务记录 #343 节、PR-C 合同修订行（§6.5 改写）、F11 PR-4 合同 §8 注明实测为发布先开；`docs/development.md:53` 一句补「或同目标由发布开出、未被其他 fingerprint 认领的事故」。 | 合同与代码同 PR 落地。 |

## 3. 允许修改的文件、迁移

- 允许：`opspilot/persistence/alerts.py`（`_correlate`、`_append_attached_event` 模板分支）、`opspilot/persistence/correlation.py`（仅 docstring）、`docs/development.md:53`、F2 任务记录与 PR-C 合同修订行、F11 PR-4 合同 §8 一句、新增实现测试。
- 不改：`observation/release_commands.py`、迁移、`grants.py`、`web/service.py`、`web/store.py`、`investigation/*`、worker、Observer、`feature_list.json`、现有 PR-C / F11 独立验收测试断言。
- 迁移：**不需要**。与 #305（改 `inputs.py`、`web/service.py`）无文件重叠，可并行。
- 实现者须 grep 是否存在「同目标先有发布事故、再投告警期望 `created`」的既有测试（本次粗查 `tests/` 未见）；如有，按合同变更交独立测试作者改。

## 4. 给独立测试作者的确定性行为（公开接口 `/intake/alertmanager` + web 消费 pass + PG）

1. 发布先开：发布消费 `created`（事故 R + Run）→ 同目标告警 X 首投：`attached`，`incident_id=R`，`run_id` 空；R 的 Run 集合仍 1 个；R 恰好多 1 条 `kind='event'` 输入（channel alertmanager，模型投影只含 channel/text）；无新 identity 行；R 的 state/lifecycle/mode/control_generation/conclusion 不变；priority = min(原值, X 的映射值)。
2. X 同 `startsAt` 重投（含注解变化）→ `attached`，事件仍 1 条；X resolved → `resolved_attached` 到 R，无事件。
3. X 新 `startsAt`、R 未 closed → `attached` 到 R，事件 +1。
4. X 已认领 R 后，同目标告警 Y → `created`（新事故 + Run），R 无变化，`related_incident_id` 空。
5. 未认领的 R 上 X、Y 并发首投 → 恰好一个 `attached` 到 R，另一个 `created`。
6. R 为 observing_recovery、resolved、human_owned、已有结论 → 都只附加，各项不变。
7. R closed（U2-A）：X → `created`，`related_incident_id=R`，R 仍 closed；R 被 Y 认领且 closed 时 X → 不关联到 R。
8. 不参与：手工/提交事故、交接事故（告警交接与 `RELEASE_TARGET_UNRESOLVED`）、他目标发布事故。
9. 同时存在开放的同 fp 告警事故与可认领的发布事故 → 附加到 created_at 最新者（同时取 incident_id 较大）。
10. 告警先开路径不变（发布追加到告警事故，S4 行为）；现有 PR-C、B1、F11 PR-4b 测试全部原样通过。
11. 附加与人工 close R 并发：只有「先附加」或「close 先提交后新建关联事故」两种结果。
12. 全局/目标挂起期间照常附加；挂起解除前无查询、无模型调用。
13. R 附加告警后 new_run / 结论后追问：后继 Run 时间框与无附加时相同（R6），附加事件出现在后继输入中。
14. alembic head、`RUNTIME_GRANTS` 与实际 ACL 不变；输入版本串、`tool_schema_revision`、`prompt_revision` 不变。

## 5. 最小真实证据

可并入 F2 PR-E 的 kind 串行会话（U3-A），不单独占窗口：延迟回归一轮，告警开启，web pass 默认 30 s，不控序；断言见 U3。观察窗内不跑 `make check`，注入前查 swap（kind 记忆条目）。本 PR 合并门只要 PG 确定性测试与独立审查。

## 6. 未确认

- 默认时序下二次 Run 是推断，未运行（`run.md:58`）。
- PR-E kind 会话中发布与告警的实际先后：按 U2 时间线大概率发布先，但未保证。
- 发布事故 Run 只看到告警 fingerprint 哈希、看不到 alertname（R-C4 白名单）；是否把 alertname 加进事件属输入合同变更，本项不做，可另开 issue（可选）。
- F11 PR-5 的步骤 3 真实运行设计未读（`prereview-f11-pr5` 另有预审），只建议其默认顺序运行在 #343 之后。
- 认领规则对「同一发布事故上告警 X 的 resolved 后又出现 Y」仍视为被 X 认领（Y 另开）；是否按 X 已 resolved 释放认领未讨论，推荐不释放（简单、与告警先开对称）。

## 裁决（2026-10-24，lead 按 Q1）

U1 = A（认领）、U2 = A、U3 = A；可逆默认照做。不属三类停止情况。改写 PR-C §6 第 5 条（手工开的不参与，发布开的按认领参与），在 F2 任务记录追加合同行。实现 worktree `/Users/shenghuikevin/dev/AI/poa-343`（`fix/343-alert-joins-release-incident`），独立测试 worktree `/Users/shenghuikevin/dev/AI/poa-343-tests`（`fix/343-alert-joins-release-incident-tests`）。
