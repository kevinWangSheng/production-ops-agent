# #184 自愈故障独立恢复确认合同预审

依据：git HEAD `0edf0e6d7ea710b692c2ea11f7c3c24451e6c208`；`gh issue 184`；任务记录、SPEC 2026-10-12 draft、C3、PRODUCT-CONSTRAINTS、M1-02/M1-04 合同及当前实现。工作树已有用户修改；本次未修改文件、未写 Git、未跑迁移或注入故障。

## 九类结论

1. **时间**

   已定：健康窗从第一个健康采样的评估窗口末尾起算，不能使用授权前数据；非健康采样或采样缺口重置窗口（C3 §10，`technical-proposal-2026-09-07.md:356-362`；`store.py:1693-1708`）。当前 profile 为 300 秒评估窗、60 秒间隔、600 秒持续窗、3600 秒 deadline（`observer/profiles/otel-demo-checkout.json:11-18`）。

   待决：自愈场景 deadline 是否从发布时刻还是自动授权时刻起算。推荐以 Controller 实际授权事务的 DB 时钟为起点；否则调度延迟会消耗观察预算，而发布时刻当前没有独立持久字段（`0001_baseline.py:35-37`）。

2. **计数与口径**

   已定：继续使用 HealthProfile 的 `max_samples`、间隔、有效流量、缺测和持续窗口；no-data/stale/failed 只能形成 unknown，不得算健康（C3 §10；`store.py:1693-1771`；F6 合同 `f6-acceptance-tests.md:16-20`）。

   推荐：自愈与人工登记共用同一计数与 replay 口径，不新增“自愈宽限次数”。

3. **状态转移**

   已定：只能 `open → observing_recovery → resolved`；到期或无法确认回 `open`；人工关闭不是恢复确认（C3 §4、§10；`domain/subjects.py:24-39`；0003 观察触发器 `0003_observation_store.py:383-467`）。

   推荐：自愈来源只影响审计和展示，不增加新的生命周期状态。

4. **主体与权限**

   已确认：`register_remediation` 在一个事务中锁 scope 和 incident，检查代际与挂起，递增 `control_generation`，撤销旧会话，再调用 `authorize_session_in`，最后写控制审计（`observation/store.py:1263-1356`）。`authorize_session_in` 会检查暂停状态、目标、profile subject、旧目标身份、唯一活动会话，并递增 `observation_generation`（`store.py:1065-1220`）。

   已确认：0003 的 Observer 只能写采样、读观察表和沿两条证据绑定生命周期边；不能授权会话（`0003_observation_store.py:359-467`）。0011 的 `opspilot_worker` 对观察表只有 `SELECT`，对事故只有 `UPDATE(conclusion)`；`opspilot_web` 才有观察会话插入权限和事故生命周期/代际更新权限（`0011_runtime_roles.py:55-117`、`:167-208`）。

   结论：worker 不能直接自动授权；由 worker 扩权会违反 C3 §3“Worker 的业务变更通过 Controller 提交”（C3 `technical-proposal-2026-09-07.md:50-64`）。

5. **数据合同与迁移/部署顺序**

   待决：需要新增会话来源字段或等价持久合同；当前会话只有 `authorized_by`，没有 provenance（0003 `:214-265`）。

   推荐：下一个迁移新增枚举字段 `authorization_source = post_remediation | self_healing`；升级时旧 incident recovery 会话填 `post_remediation`，release observation 不受影响。（r2 补充：该字段按 `purpose` 条件约束——`incident_recovery` 必填，`release_observation` 必须为 NULL，发布来源由 F11 另定；PR #209 机器人 P2。）迁移必须在线性链下一个号，按 C3 §11 在 web、worker、Observer 全停时升级（C3 `:408-410`）。

6. **旧数据与已归档证据兼容**

   已定：F6 投影只复制持久会话、采样、结束记录和权限测量，不重算或补造（`f6-acceptance-tests.md:24-46`）。F6 `passes` 已为 true，不得回退（`feature_list.json:49-61`）。

   推荐：新增来源字段只增投影字段；旧归档记录缺字段时按 legacy/post-remediation 兼容读取，不重写原始 ledger、样本或已归档 summary。F13 复盘的恢复段继续读取结束原因、transition 和读数；只在人工动作/来源展示中追加自愈标识（`f13-acceptance-tests.md:14-35`；`acceptance_recovery.py:890-934`）。

7. **并发与水位**

   已定：事故行锁、控制代际、观察代际、scope generation、单活动任务和 lease 是现有栅栏；暂停/接管/取消/新 Run 会撤销观察授权（`store.py:1494-1531`；`store.py:1632-1771`；`persistence/controls.py:231-273`；`persistence/runs.py:105-138`）。

   待决：发布后自动授权若在单独事务执行，发布提交与授权之间可能崩溃。推荐增加可重放的 Controller 侧待处理标记/扫描键，使用 incident_id + 当前 control_generation 幂等；不得依靠 worker 直接插入观察表。

8. **外部调用**

   已定：授权本身不调用模型或目标系统；Observer 仍只读 Prometheus，任何健康确认必须来自采样记录。resolved Alert 目前只记录、不创建 Run/观察（M1-04 决定 C，`2026-10-10-m1-04-alert-intake.md:14-19`）。

   实验依据：`scripts/kind_lab.py` 使用 OTel Demo 2.0.2、固定 Helm chart，并通过 `flagd-config` 的 `paymentFailure` 在 `off` 与 `100%` 间切换（`scripts/kind_lab.py:1-31`、`:867-982`）。当前 `kind-opspilot-m1` 可见 OTel Demo、Prometheus、kube-state-metrics 和 Alertmanager；Prometheus NodePort 返回 Unauthorized，实验 credential 文件当前不可用，因此实时 Prometheus 数值**未确认**。

9. **验收投影与 passes 影响**

   待决：是否把自愈纳入 F6 的新增验收场景，以及 F13 页面/投影的来源字段名称。

   推荐：新增自愈正向和持续故障负向场景；保留全部既有 F6 五步和 `passes: true`。F13 只增加确定性来源/人工处理展示，不把自愈当人工动作，不改变 F13 `passes`（当前 F13 仍 false，`feature_list.json:136-147`）。

## 决定表 A：需用户裁决

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| A1 | 是否纳入 M1 | 纳入：修改 C3 §10、生命周期说明并实现验收；不纳入：保持人工登记，问题继续存在 | 纳入有 HealthProfile 的 incident recovery | SPEC、C3、实现范围、验收 | 采纳推荐（用户 2026-10-12，r1） |
| A2 | 自动开观察时机 | 仅合格结论发布后；发布后和交接后都开；仅交接后开。交接路径需新增持久触发语义 | 仅在合格结论已发布后；当前 `publish()` 有明确持久标记，ADR-0005 明确交接不发布（`steps.py:300-363`；ADR-0005:13-16） | Controller 调度、事故生命周期 | 采纳推荐（用户 2026-10-12，r1） |
| A3 | 谁来触发自动授权 | worker 直接写；Controller 发布后同步写；Controller 通过可重放待处理任务写。worker 直写会违反 C3 §3 | Controller 侧可重放任务，调用与人工登记相同的事务原语 | 角色、迁移、崩溃恢复 | 采纳推荐（用户 2026-10-12，r1） |
| A4 | 是否区分自愈与人工整改 | 只靠 actor；显式 provenance 字段；仅 UI 推导。只靠 actor 无法稳定表达旧数据和未来来源 | 会话持久 `authorization_source`，审计 actor 值为 `system`，DB current_user 仍为 Controller/web | schema、F6/F13 投影、审计 | 采纳推荐（用户 2026-10-12，r1） |
| A5 | `system` actor 的精确定义 | actor=`system`；actor=`system:self_healing`；仅 payload 标记。数据库当前只要求非空文本 | actor 字段使用 `system`，payload/source 使用 `self_healing`；不新增 DB 角色 | 审计查询、兼容读取 | 采纳推荐（用户 2026-10-12，r1） |
| A6 | 观察时间锚点与 deadline | 从结论发布时刻；从 Controller 授权时刻；从首次采样时刻。发布时刻当前未单独持久化，首次采样会放宽“整改后新数据” | 从授权事务 DB 时钟起算；profile 参数不变、不延长 | HealthProfile、实时性、验收 | 采纳推荐（用户 2026-10-12，r1） |
| A7 | 自动会话与人工操作竞态 | 自动优先；人工优先；按 incident 行锁和 control_generation 决胜。自动优先会压过接管/登记 | 行锁 + expected generation；人工登记/接管/暂停/取消先提交者胜，人工动作撤销自动会话 | 并发、审计、lease | 采纳推荐（用户 2026-10-12，r1） |
| A8 | 无 HealthProfile 目标 | 只交接；用通用探针；使用 Alertmanager resolved。通用探针无法满足 C3 必要信号，resolved 方案违反 M1-04 C | 保持现状：不能自动确认恢复，继续人工关闭/处理 | 目标范围、验收 | 采纳推荐（用户 2026-10-12，r1） |
| A9 | 与 M1-04 决定 C 的关系 | 改 C 让 resolved 触发；保留 C，resolved 仅提示；两条入口并行 | 保留 C；resolved 只能作为工作台提示，不触发观察 | Alert intake、#167、#184 | 采纳推荐（用户 2026-10-12，r1） |
| A10 | 旧会话与归档来源字段 | 回填并重写旧证据；旧字段缺失即 unknown；兼容读取默认 legacy/post-remediation。重写会破坏归档真实性 | 不改原始证据；旧 incident recovery 会话映射为 post-remediation/legacy，新增会话必须显式来源 | F6 replay、F13 复盘 | 采纳推荐（用户 2026-10-12，r1） |
| A11 | 迁移和权限合同 | 给 worker 观察写权限；复用 web/Controller 写权限；新增专门 Controller 角色。worker 扩权与 C3 冲突 | 复用当前 Controller/web 观察写权限；worker 只发布结论和写待处理标记 | 0011、下一个迁移、部署停机 | 采纳推荐（用户 2026-10-12，r1） |

## 决定表 B：可逆默认

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| B1 | 自动授权幂等键 | 随机 session；incident + control_generation；incident + conclusion hash。随机键无法安全重试 | `incident_id + control_generation + trigger=published`，session_id 确定性派生 | 重试、崩溃恢复 | 可逆默认：与现有 `register_remediation` 的代际和幂等语义一致 |
| B2 | 自动会话参数来源 | 新配置；调用方传参；HealthProfile 固定值 | 完全复制已加载 HealthProfile 的 session 参数，前端和 worker 不得覆盖 | 预算、窗口、replay | 可逆默认：现有 `authorize_session_in` 已要求调用方提供冻结参数 |
| B3 | 自动授权失败处理 | 重试无限；标记失败并交接；静默忽略 | 分两类（r2）：代际、挂起、生命周期、已有活动会话或 HealthProfile 不再生效等**失效**情形，任务作废为无操作，只留历史，不改事故、不写交接；只有运行性失败（DB 不可用等）有界重试，耗尽后写可见交接原因，事故保持 `open` | 运维、值班可见性 | 可逆默认：失效结果只保留历史（C3 §10），人工先提交者胜（A7）；PR #209 机器人 P1 |
| B4 | 自愈实验构造 | 产品自动恢复；工程 harness 定时恢复；告警 resolved 触发 | 工程 harness 定时执行 `fault inject` 后 `restore`，产品无人点击登记 | kind、F6 真实验收 | 可逆默认：`scripts/kind_lab.py:895-984` 已提供受校验的 flagd 注入/恢复 |
| B5 | 展示命名 | 仅显示 resolved；显示 self-healed；显示授权来源和恢复来源两处 | F6 显示 `authorization_source=self_healing`，F13 recovery 段显示 `recovery_source=self_healed` | 工作台、F13、审计 | 可逆默认：不改变生命周期和既有恢复 verdict |

## 建议的实施拆步

1. **合同与 schema PR（用户门）**  
   修订 SPEC、C3 §10、生命周期说明和本合同；增加会话来源字段、必要审计 payload/检查；迁移使用下一个线性编号。迁移前后按 C3 §11 停止 web、worker、Observer。需要迁移。

2. **Controller 授权/重放 PR（用户门）**  
   抽取“自动授权”到与 `register_remediation` 相同的事务路径；增加发布后待处理标记或可重放扫描；保持 scope 锁、incident 锁、control_generation、observation_generation 和撤销逻辑。不得给 worker 增加观察写权限。需要迁移后的合同测试。

3. **竞态与 replay 合同测试 PR（功能 PR）**  
   覆盖自动授权与人工登记、takeover、pause/cancel、new_run、scope suspension、过期 lease、重复扫描和崩溃重试；覆盖 `system` actor、来源字段、旧行兼容和 F6 投影。

4. **F6/F13 投影与页面 PR（功能 PR）**  
   自愈会话显示来源；人工整改仍显示人工处理；F13 recovery 段保留结束原因、transition、读数和水位，新增来源字段；不修改既有 F6 断言或 `passes`。

5. **kind 真实实验 PR/证据包**  
   在 `kind-opspilot-m1` 上先确认 profile、Prometheus Observer credential、workbench、worker、Observer 和 PG 角色；用 `scripts/kind_lab.py fault inject --experiment-id <id>` 置 `paymentFailure=100%`，由工程 harness 延迟调用 `fault restore --experiment-id <id>`，产品无人登记；另跑持续故障不 restore。需真实实验环境运行，不得把工程恢复动作计为产品权限。

6. **独立审查与验收 PR**  
   由未参与实现的 Agent 按 C3、F6 五步、F13 第 1–3 步编写合同测试和审查；最终附自愈/持续故障真实 Run、冻结 `summary.json`、投影对照和失败分母。

## 风险与未确认项

- 当前没有独立的 `conclusion_published_at`；若用户坚持以发布时刻为 deadline 起点，必须新增持久字段和迁移。
- 当前 worker 的 `OPSPILOT_WORKER_DSN` 明确使用 `opspilot_worker`，观察表只有读取权限；直接在 worker 调用 `register_remediation` 会被 PostgreSQL 拒绝，也不应通过扩权绕过 C3。
- `system` 作为列值在当前 schema 中可写：`authorized_by`、`opspilot_controls.actor` 都是非空文本，没有禁止 `system` 的 CHECK（`0001_baseline.py:87-100`；`0003_observation_store.py:214-225`）。这不等于允许 worker 角色写入；实际 DB current_user 仍应是 Controller/web。
- 当前 web action 集合没有 `close/reopen`（`web/service.py:110-140`），领域模型有对应生命周期边（`domain/subjects.py:24-39`）。若本任务要覆盖 close/reopen 竞态，必须先确认实际产品入口或将其列为未确认，不得假定已有实现。
- `authorize_session_in` 只允许从 `open` 开始进入 `observing_recovery`；已 `resolved/closed` 事故不能被旧自动任务重开。人工 reopen/new Run 必须递增代际并重新授权（`domain/subjects.py:27-39`；`persistence/runs.py:105-138`）。
- 当前 kind 集群只做了只读 `kubectl get/describe` 核查：OTel Demo、Prometheus、kube-state-metrics、Alertmanager 均在运行；Prometheus HTTP GET 未能认证，因为本地实验 credential 文件不可用，实时指标状态未确认。
- `pytest --collect-only` 未执行成功：工作树没有 `.venv/bin/python`，系统也没有 `python` 命令；本次没有测试通过结论。
- F6 当前归档合同明确要求 42 个 PG opt-in 场景、权限测量、重放一致性和 `passes: true` 保持（`docs/testing/f6-acceptance-tests.md:10-20`）。任何来源字段变更都必须做旧归档 replay，不能仅凭静态检查宣称兼容。

## r2 追加（lead，2026-10-12，可逆默认细化，不改用户裁决）

- B3：失效情形无操作，仅运行性失败耗尽后交接（PR #209 机器人 P1）。
- A4 实现细节：`authorization_source` 按 `purpose` 条件约束，release 会话为 NULL（PR #209 机器人 P2）。


---

# 第 1 步按步预审（Codex GPT-6 Astra high，只读，2026-10-12，基于 r1）

已核对当前 worktree、r1、SPEC/C3、迁移、运行入口和持久化代码。本次未修改文件、未写 Git、未跑迁移、未接触 kind。

已确认的事实：

- `web` 只有请求驱动逻辑；`web/__main__.py:118-171` 启动 Uvicorn，SSE 的 `reconcile` 仅在已有浏览器连接时运行，见 `web/app.py:761-805`。没有独立后台 Controller 循环。
- `worker` 有调查循环和 postmortem 循环，见 `worker_main.py:267-305`；postmortem 有独立 jobs 表和 lease，见 `knowledge/jobs.py:24-25,86-128`。
- `opspilot_worker` 对观察会话只有 `SELECT`，对事故只有 `UPDATE(conclusion)`，见 `0011_runtime_roles.py:190-208`；`opspilot_web` 才有观察会话插入和事故控制更新权限，见 `0011_runtime_roles.py:97-140`。
- `publish()` 只把结论写入事故并将 Run 置为 `completed`，见 `persistence/steps.py:300-363`。真正的“合格结论”判据是 `conclusion_publishable()`，见 `investigation/context.py:1228-1259`；Runner 只有通过该判据才调用 `publish()`，见 `investigation/runner.py:377-430`。
- `run_completed` 事件带 `published=true`，但它是通知投影，不能作为崩溃恢复的唯一权威，见 `investigation/progress.py:80-95`。
- `authorize_session_in()` 当前会锁事故、检查暂停、目标、HealthProfile subject、目标身份、活动会话，并递增 `observation_generation`，见 `observation/store.py:1126-1220`。
- 人工 `register_remediation` 会递增 `control_generation`、撤销旧会话、授权新会话并写控制审计，见 `observation/store.py:1263-1356`。接管也会递增代际并撤销观察，见 `persistence/controls.py:231-250`。

## A：需用户裁决

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| A1 | 谁扫描“待授权”任务、用哪个角色 | ① worker 直接写观察会话：被 `0011` 权限和 C3 §3 禁止；② worker 写专用待授权表，Controller 扫描并调用 web/Controller 观察写原语：增加一张任务表和 worker 的最小 INSERT/UPDATE 权限；③ 新增独立 Controller 进程：边界清晰，但 #197 尚无进程、部署或凭据合同 | 采用②。Controller 近期落在 web 进程内的后台循环，使用 `opspilot_web`；worker 只写非观察任务标记。需要单任务 lease，避免多 web 副本重复执行。 | 新表、0011 角色授权、web 启动/停止生命周期、部署运行合同 | r1 已定“Controller 侧可重放、worker 不写观察表”；具体是 web 后台循环还是独立进程仍**未确认**。若不接受 web 承载，需在 #197 先定义 Controller 进程。 |
| A2 | “合格结论已发布”的精确读取点和触发语义 | ① 只看 `run_completed` 事件：事件丢失时无法恢复；② 只看 `incidents.conclusion`：需复用已有合格判据；③ 重新调用 `publish()`：会混淆幂等发布和触发 | 以持久状态为权威：读取当前 Run、`opspilot_incidents.conclusion` 和当前代际，按 `conclusion_publishable()` 的完整规则判断；事件只作加速提示。交接/失败结论不触发。见 `context.py:1228-1259`、`steps.py:343-363`。 | 触发正确性、崩溃恢复、重复发布、验收场景 | r1 已定“只在合格结论发布后”。需要确认实现必须采用上述持久判据，而不是事件存在性。 |
| A3 | 授权时条件不满足时的分类 | 全部静默跳过会丢失交接；全部无限重试会在人工接管或暂停后重新开观察 | 推荐固定分类：`lifecycle != open`、结论/代际已陈旧、`human_owned`、无 HealthProfile、已有人工活动会话 → 终态跳过并写可见原因；全局/目标挂起、数据库冲突 → 有界重试；重试耗尽写 handoff reason、事故保持 `open`。人工会话先提交则自动任务标为已处理。依据 `authorize_session_in` 的拒绝条件 `store.py:1126-1180`、接管撤销逻辑 `controls.py:231-250`。 | 事故状态、值班可见性、暂停恢复后的行为、B3 验收 | r1 只规定“有界重试、失败写交接原因”；上述永久/暂时失败分类仍**未确认**。 |
| A4 | 迁移、触发器和运行角色的具体合同 | 修改历史 `0003/0011`；或新增线性迁移并补授权 | 在会话表 `opspilot_observation_sessions` 增加 `authorization_source text NOT NULL DEFAULT 'post_remediation'`，CHECK 仅允许 `post_remediation`/`self_healing`；`register_remediation` 固定写 `post_remediation`，自动原语固定写 `self_healing`。不要改写历史 `0003/0011`；新增迁移补 `opspilot_web` 对新列的 INSERT 权限，并为待授权表授予 worker 最小权限。0003 的 Observer 生命周期证据触发器无需改变，因为它只约束 `observing_recovery -> resolved/open`，见 `0003_observation_store.py:383-467`。 | 旧数据默认值、升级顺序、角色隔离、Observer 边界 | r1 已定字段和值域；“历史迁移不改、由下一个线性迁移补 grant”是推荐实现，需作为第 1 步合同确认。 |
| A5 | 自动审计是否进入 `opspilot_controls`，以及是否推进 `control_generation` | ① 新建审计表：查询面增加；② 复用 `opspilot_controls` 并递增代际：会使旧 Run/任务失效；③ 复用该表但同代审计：需允许 `expected_generation == resulting_generation` | 复用 `opspilot_controls`，`actor='system'`，payload 至少含 `authorization_source='self_healing'`、task key、session/profile、结果或失败原因；`expected_generation` 与 `resulting_generation` 都写当前值，**自动授权不递增 `control_generation`**。只递增 `observation_generation`，这是现有授权原语的语义，见 `store.py:1181-1207`。 | 审计查询、并发栅栏、旧 Run/lease、F13 水位 | r1 已定 actor 和来源；是否允许 controls 中出现同代系统审计行、动作名称取值仍**未确认**。 |

## B：可逆默认

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| B1 | 幂等与重复发布 | 随机 session 会在崩溃重试时重复开会话；只按 incident 会阻止新 Run | 使用 `incident_id + control_generation + trigger=published`，并确定性派生 `session_id`/任务 key。相同 Run 重复 `publish()`、重复扫描或恢复扫描只产生一次授权；新 Run 或追问产生新 `control_generation`，才允许新的自动授权。`new_run` 会清除结论并推进代际，见 `runs.py:82-113`。 | 崩溃恢复、重复发布、新 Run、追问 |
| B2 | HealthProfile 和模式读取 | 从调用方传参可能漂移；读取默认 profile 可能覆盖目标实际 profile | Controller 在同一事务锁下重新读取目标绑定的 HealthProfile，完整复制 `max_samples`、间隔、持续窗口、deadline 和 revision；不允许 worker/UI 覆盖。`authorize_session_in()` 已将这些参数冻结到会话，见 `store.py:1178-1217`。只接受 `mode='automatic'`；`human_owned` 直接停止自动观察，符合 `controls.py:231-250` 和 `runs.py:169-179`。 | 参数冻结、接管语义、replay |
| B3 | 待授权任务表的最小结构 | 仅存 incident_id 无法证明代际和触发来源；存完整 profile 会复制配置 | 建议字段：`task_id`、`incident_id`、`control_generation`、`trigger`、`state`、`attempts`、`next_attempt_at`、`lease_owner/lease_until`、`last_error`、`created_at/updated_at`；不复制 HealthProfile 内容。任务只保存水位，授权时重新读取 profile。可参考 `knowledge/jobs.py:131-155,210-257` 的 lease/backoff 结构。 | 重放、并发扫描、有界重试 |
| B4 | `register_remediation` 接线 | 让人工路径隐式依赖默认 source；低层原语允许任意 actor/source 组合 | 低层 `authorize_session_in()` 显式接收 source；人工包装器固定 `post_remediation`，自动包装器固定 `self_healing`；现有直接调用者更新为显式值。旧行由迁移默认回填，归档证据不改。当前会话表没有 provenance，见 `0003_observation_store.py:214-228`。 | API 合同、旧调用兼容、归档 replay |
| B5 | 第 1 步必须有的确定性测试 | 只测 schema 会漏掉并发、权限和 crash replay | 至少覆盖：迁移默认值与 CHECK；旧会话兼容读取；worker 对观察表仍无 INSERT/UPDATE；web 可插入 source；合格结论才生成任务；handoff/不完整报告不生成；同代重复扫描只授权一次；新 Run 生成新 key；人工登记、takeover、pause、cancel、scope suspension 的先提交者胜负；自动审计 actor/source；自动授权不推进 `control_generation`；profile 参数和 DB 时钟冻结；lease 过期后可重放且达到上限后写 handoff reason。权限基线见 `grants.py:49-62,124-140`，Observer 证据边界见 `0003_observation_store.py:369-380,415-467`。 | 第 1 步的合同测试和角色测试 | 可直接作为 PR 的完成门槛；不需要真实模型、迁移到共享库或 kind。 |

第 1 步建议限定为一个 PR：新增线性迁移和角色授权、增加 `authorization_source` 与兼容读取、增加待授权任务及 lease/replay 原语、在 Controller 侧接入发布后的扫描触发、接入同一事务授权路径、补上述确定性合同测试。F6/F13 投影、页面展示和 kind 真实运行应留到后续 PR；任务记录本身也把 Controller 授权/重放和投影分开列出，见 `docs/tasks/2026-10-12-184-self-healing-recovery.md:89-107`。

主要风险是当前没有既有 Controller 后台循环，web 的 SSE `reconcile` 不能替代无人值守扫描；若 web 多副本没有任务 lease，会产生重复竞争。另一个风险是 `publish()` 本身只持久化结论，不保存独立的“发布时刻”，所以必须以授权事务 DB 时钟作为观察起点，并以持久结论和代际重建触发，而不能依赖通知事件。

## lead 核对（2026-10-12）

- `opspilot_incidents.conclusion` 只由 `publish()` 写入（`persistence/steps.py:356`），`new_run`（`runs.py:112`）与 `controls.py:399` 清空；因此「`conclusion IS NOT NULL` 且 `lifecycle=open`」即当前 Run 已发布合格结论，Controller 可直接扫描事故表派生触发，不需要 worker 写待授权标记。
- 预审 A3 的失败分类与 r2 B3 一致：失效情形作废为无操作，原因只记在自动授权记录上，不写事故。
- 待用户裁决：S1-D1 Controller 承载位置、S1-D2 触发来源、S1-D3 审计与代际。


## r4 追加（2026-10-12）

见任务记录「r4」节：PRODUCT-CONSTRAINTS 修订、来源值改为 `automatic` 不作因果判断、A7 收窄（用户批准）；身份补齐、retrying 扫描、独立审计动作、旧映射限定（lead）。
