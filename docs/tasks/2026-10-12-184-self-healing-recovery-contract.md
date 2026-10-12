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

   推荐：下一个迁移新增非空枚举字段，例如 `authorization_source = post_remediation | self_healing`；升级时旧 incident recovery 会话填 `post_remediation`，release observation 不受影响。迁移必须在线性链下一个号，按 C3 §11 在 web、worker、Observer 全停时升级（C3 `:408-410`）。

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
| A1 | 是否纳入 M1 | 纳入：修改 C3 §10、生命周期说明并实现验收；不纳入：保持人工登记，问题继续存在 | 纳入有 HealthProfile 的 incident recovery | SPEC、C3、实现范围、验收 | |
| A2 | 自动开观察时机 | 仅合格结论发布后；发布后和交接后都开；仅交接后开。交接路径需新增持久触发语义 | 仅在合格结论已发布后；当前 `publish()` 有明确持久标记，ADR-0005 明确交接不发布（`steps.py:300-363`；ADR-0005:13-16） | Controller 调度、事故生命周期 | |
| A3 | 谁来触发自动授权 | worker 直接写；Controller 发布后同步写；Controller 通过可重放待处理任务写。worker 直写会违反 C3 §3 | Controller 侧可重放任务，调用与人工登记相同的事务原语 | 角色、迁移、崩溃恢复 | |
| A4 | 是否区分自愈与人工整改 | 只靠 actor；显式 provenance 字段；仅 UI 推导。只靠 actor 无法稳定表达旧数据和未来来源 | 会话持久 `authorization_source`，审计 actor 值为 `system`，DB current_user 仍为 Controller/web | schema、F6/F13 投影、审计 | |
| A5 | `system` actor 的精确定义 | actor=`system`；actor=`system:self_healing`；仅 payload 标记。数据库当前只要求非空文本 | actor 字段使用 `system`，payload/source 使用 `self_healing`；不新增 DB 角色 | 审计查询、兼容读取 | |
| A6 | 观察时间锚点与 deadline | 从结论发布时刻；从 Controller 授权时刻；从首次采样时刻。发布时刻当前未单独持久化，首次采样会放宽“整改后新数据” | 从授权事务 DB 时钟起算；profile 参数不变、不延长 | HealthProfile、实时性、验收 | |
| A7 | 自动会话与人工操作竞态 | 自动优先；人工优先；按 incident 行锁和 control_generation 决胜。自动优先会压过接管/登记 | 行锁 + expected generation；人工登记/接管/暂停/取消先提交者胜，人工动作撤销自动会话 | 并发、审计、lease | |
| A8 | 无 HealthProfile 目标 | 只交接；用通用探针；使用 Alertmanager resolved。通用探针无法满足 C3 必要信号，resolved 方案违反 M1-04 C | 保持现状：不能自动确认恢复，继续人工关闭/处理 | 目标范围、验收 | |
| A9 | 与 M1-04 决定 C 的关系 | 改 C 让 resolved 触发；保留 C，resolved 仅提示；两条入口并行 | 保留 C；resolved 只能作为工作台提示，不触发观察 | Alert intake、#167、#184 | |
| A10 | 旧会话与归档来源字段 | 回填并重写旧证据；旧字段缺失即 unknown；兼容读取默认 legacy/post-remediation。重写会破坏归档真实性 | 不改原始证据；旧 incident recovery 会话映射为 post-remediation/legacy，新增会话必须显式来源 | F6 replay、F13 复盘 | |
| A11 | 迁移和权限合同 | 给 worker 观察写权限；复用 web/Controller 写权限；新增专门 Controller 角色。worker 扩权与 C3 冲突 | 复用当前 Controller/web 观察写权限；worker 只发布结论和写待处理标记 | 0011、下一个迁移、部署停机 | |

## 决定表 B：可逆默认

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| B1 | 自动授权幂等键 | 随机 session；incident + control_generation；incident + conclusion hash。随机键无法安全重试 | `incident_id + control_generation + trigger=published`，session_id 确定性派生 | 重试、崩溃恢复 | 可逆默认：与现有 `register_remediation` 的代际和幂等语义一致 |
| B2 | 自动会话参数来源 | 新配置；调用方传参；HealthProfile 固定值 | 完全复制已加载 HealthProfile 的 session 参数，前端和 worker 不得覆盖 | 预算、窗口、replay | 可逆默认：现有 `authorize_session_in` 已要求调用方提供冻结参数 |
| B3 | 自动授权失败处理 | 重试无限；标记失败并交接；静默忽略 | 有界重试，失败写可见 handoff reason，incident 保持 `open` | 运维、值班可见性 | 可逆默认：符合 C3 unknown/handoff 和 ADR-0005 |
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