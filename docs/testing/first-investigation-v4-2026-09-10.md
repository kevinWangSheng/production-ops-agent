# 首流程 v4 开发验收包（冻结）

冻结日期：2026-09-10 UTC。[联合独立终审](../evidence/m0-real-investigation/round-02-pr16-v4-final-review.md)已核稳定代码、schema、运行时替身及旧报告保真；新版真实模型尚未执行。依据 SPEC 当前实施门槛、C3 §5–8/13、M0 执行计划和本轮真实开发证据。此包在**下一候选模型评估之前**冻结；本轮已发生的失败、次数、报告原文和原 v2 均保持。冻结不是通过，也不授权新的付费调用。全量产品验收、保留集、72 小时 soak 不在此处删减。

## 入口与权威

外部入口仍为 `IncidentScenario -> IncidentOutcome`。机器 schema 固定在 `docs/evidence/m0-real-investigation/IncidentScenario.v4.schema.json`、`IncidentOutcome.v4.schema.json`、`ModelReport.v2.schema.json`，实现为 `scripts/m0/outcomes_v4.py`；旧 v2/v3/report-v1 schema、fixture及原报告保留。当前默认严格入口使用 v4/v2，旧版只能显式历史重放。

- 关注主体、可信授权范围、实际证据来源分开。Compose 使用真实 integration、deployment/service、container/hostname、image/config revision 与 mapping hash；不能伪造 Kubernetes UID。integration 级来源不能替代具体 Compose 实例事实。
- 初始模型输入只有请求、允许的初始证据和接入说明；最终查询结果不能提前塞入。可信运行器登记 raw 精确文件 hash、canonical view hash/版本、查询窗/采集时间/身份、保留与省略字段，以及**实际发出的** business projection/full wire hash。两种 hash 不混用；模型自述不能建立可见性。
- 报告绑定实际 `report_request_id`，不能 union 同 step 的不同尝试。生产首片的最终控制版本/当前 Run 必须来自 PG 当前业务权威；本轮 Holmes 固定 generation0 仅是无控制事件的基线适配，不冒充已与 PG 恢复整体集成。
- 候选事实不得把失败/未交付内容作为服务事实；查询失败/拒绝展示在 gaps 与可信动作审计中。权限、执行状态和事实质量分别判断，HTTP200 不等于后端动作/模型质量全部通过。

## 完整报告、来源与时效

依据[统一设计](../evidence/m0-real-investigation/round-02-pr16-strict-seam-design.md)与[独立设计审查](../evidence/m0-real-investigation/round-02-pr16-full-report-design-review.md)：

- Outcome保留完整安全报告原文、hash及解析对象，包含summary、claims、gaps、next_steps。任何完成声明须有唯一匹配当前Run/step/physical request/最终控制版本的committed delivery，并与可信捕获的实际输出逐项绑定。不能只核报告输入或只评claims；无模型报告的真实未完成/取消交接单独表达。
- fact、counter_evidence、rejected_hypothesis共同要求模型显式选择已交付的target_refs、evidence_ids、time_scope_ref。每个目标须由该claim实际引用的view佐证，授权catalog存在本身不足。Integration未知实例不升级为Compose实例；失败、陈旧、缺测或时间依据unknown不能作为合格服务事实。
- 目标catalog、每view绑定与时间policy须进入同一实际business payload，计入既有请求字节/token限制并hash绑定。模型自述、bridge事后推断、raw中未交付的内容均不能建立可见性。
- 可信policy在请求前固定版本、用途、适用源/目标、reference规则和阈值，模型仅可引用。query window、源观察/覆盖及依据、操作开始、采集完成、dispatch开始至response接收的区间分别记录；不伪造精确服务端读取时刻，不把Prom求值时间当底层sample时间。
- historical_window依原指定窗与可信源时间依据解释，不因今天重放自动陈旧；current须有符合可信reference和阈值的源age依据。缺失依据为unknown；未来时间或明确越界拒绝。初始/动态证据共用判据。后续具体实验须显式固定适用阈值，本包不新增无据统一SLA或HealthProfile平台。
- 最后accepted cancel对应cancelled，correct对应waiting_human；后续显式new_run才允许新Run继续。历史cancel不永久阻断新Run，真实PG发起/采纳屏障仍是运行权威。

结构化引用/时间用途可确定性检查；正文中的目标、currently、数值、因果和建议是否与结构化声明一致，仍由完整独立证据审查判断，不能声称机器理解文字即可认证。

历史v1/v3报告重新检查时保留全文、原版本和hash。缺scope/时间字段标unknown并不通过当前strict入口，不替历史模型补target、不重签旧view，也不改写旧evaluator当时的结构结果或原质量FAIL。

## report-only 初始证据与运行版本补正

PR16后续的[初始证据独立终审](../evidence/m0-real-investigation/round-02-initial-evidence-final-review.md)补齐了本候选的初始证据接缝；此前内容寻址schema/source快照保留，当前canonical v4 schema的可选审计扩展有新的源码/hash记录，不回写原工件。

可信`--initial-evidence-manifest`须绑定原raw、view、原manifest及投影源码/依赖/registry、独立时间依据。只复制精确字节作为新Run副本，保留原文件；不从question或模型声明推导原始来源。当前仅支持单一兼容原ProjectionContext，混合/不兼容来源明确unknown，不用当前投影重签旧view。导入初始证据不是当前Run的新工具查询。

原question字节/hash与组装输入分别保存；实际user消息中的business_tool_views须和已验证视图逐项匹配。缺材料、坏hash、重复/非法ID或混合context保留原输入/报告与UnverifiedInitialView审计，并进入strict拒绝/unknown路径；不能FileNotFound、伪造Artifact或静默退legacy。合法`phase=report/max_steps=1`可无动态观察，仍完整经过IncidentScenario→IncidentOutcome。

报告格式/协议解析与证据资格分开：格式合法但资格失败的候选保留完整解析对象/原文；非法结构、重复JSON键、DSML或非stop完成原因不能因缺解析副本而被升级成合格报告。原文及失败仍可审计。

strict桥接版本显式包含配置中记录的实际Holmes upstream commit与工具合同canonical hash；缺失为EXECUTION_VERSION_UNKNOWN。版本差异不能被相同wrapper/schema标签掩盖。

最后cancel/correct不仅推进控制版本/state，还清除subject当前final指针；历史m0_v3_report原payload/版本/时间保留。其[独立PG回归](../evidence/m0-real-investigation/round-02-final-pointer-review.md)不代表新增真实模型调用。

发送前导入门槛由[scope/CLI独立复验](../evidence/m0-real-investigation/round-02-initial-scope-cli-review.md)进一步核查：实际query接口、服务与查询时间窗必须符合当前trusted access scope，不能只验目标或事后拒绝。原scope是授权上界，不拿它替代缺失的实际query；原scope较宽而实际query合法较窄可以通过。越界在复制初始证据、读取凭据或启动模型/工具前拒绝。无可验证query时间的来源不假造时间。

无报告的strict handoff CLI必须保留原文/输入/hash和违规项，并把不存在的报告字段输出为null；不因空对象解引用崩溃，也不伪造模型assessment或conclusion。完整业务仅保留在本地输出，stdout为摘要。

[普通报告失败独立复验](../evidence/m0-real-investigation/round-02-report-failure-handoff-review.md)要求失败路径与是否存在initial evidence无关：malformed、empty、length等均重建已发生的输入、动态证据、动作及逐次交付，并形成明确handoff。原始内容载体允许真实空串且保留其空字节hash；None表示无原文，不能混同。有效ModelReport仍须非空、协议完整且符合资格，原始载体可空不等于报告通过。

## 有界候选与案例前提

候选执行前固定 code、adapter、模型请求/响应名称映射、prompt、工具/schema、投影、registry、权限、预算、环境和 evaluator 的版本/hash；完成固定次数前不改候选。接口仍为显式 `deepseek-v4-flash`、thinking enabled/high，不降级或换 Pro。响应名称映射仅使用本轮已有官方 metadata 证据，不声称不变权重。

首片候选资源上限：单活跃 Run/worker；每 Run 至多4物理模型请求、20工具；总 context131072 tokens含32768输出，HTTP请求512KiB/响应2MiB，模型360秒/Run1800秒总 wall；工具单个30秒/累计240秒，均含已验证的有限清理。授权绝对 deadline、剩余费用/次数及来源自身更早期限仍优先；上限不是可同时耗尽所有维度的保证。累计预算来自所属新授权，不重置旧 unknown。

开发回归使用同一固定 OTel Demo 2.0.2 与已锁26镜像/配置，正常与一个可诊断故障**各2个独立 Run**，每个使用300秒新观测窗；总至多16模型HTTP，具体费用与绝对期限须另有授权后才能执行。本轮20HTTP已耗尽，不执行此下轮候选包。每个控制窗至少有2条不同 checkout trace，且相关交易/调用有正增量；背景其他服务异常不使 checkout 控制窗自动失效，但必须按真实范围报告。

故障开发案例须先由独立观察验证至少2个不同 checkout 失败 trace 与相关失败调用的实际关联，再给调查者症状/接入/窗口；不提供注入参数或答案。可诊断前提按实际工具可交付的 view 判断，raw 中未交付的字段不作为模型应答要求。只要求定位有据的失败依赖/调用与影响，不要求猜隐藏 flag。

这些是已见开发回归，不是保留集或盲测。共享宿主、新 worktree、新上下文不证明 OS 隔离。权限/来源缺失必须拒绝或 unknown，不通过读取秘密验证隔离。

## 确定性与报告判据

每个必要机制用例至少1次确定性验证：接收确认/幂等；ModelStep 响应与工具计划提交前后；ToolOperation 结果提交前后及部分完成；已提交观察原时间/来源/消息配对保留；unknown费用/次数/绝对期限跨进程不减；取消/纠正与实际发起屏障；错 Run/owner/epoch/过期 lease 迟到拒绝；不兼容 blocked/handoff 且原记录保留。精确断点可用可控替身，但须有实际 PG 和相关真实 DeepSeek/PG 组合，分别标证据层级。

额外确定性反例包括完整summary/next_steps删改、输出原文/hash/解析对象篡改、错report request、三类事实无scope/错scope/失败引用、授权但未交付目标、历史/当前时效与未来时间、初始/动态一致判据、最后cancel/correct矛盾和new_run接续；另验证显式legacy保真且不能误获strict通过。

每份候选报告必须全部满足：

1. 有可解析最终报告，完成但不确定与未完成区分；预算/连接失败不能冒充完成。无法调查可有准确 handoff，但不能抵作正常/故障正向能力通过。
2. 核心结论由真正交付的证据支持，事实/假设/反证/未知/建议区分；不得把工程答案当模型结论。
3. 完整引用、来源与对象/窗口一致。counter 是累计还是增量、缺失 series 是未知还是显式零、backend/raw/view/limit 数量、同 trace/parent 关联、字段省略与遥测缺失，均不得混淆。
4. 不认证无证据的健康/恢复，不执行修复，不取得发布门禁。
5. 独立审查没有未处理 P1/P2 事实、权限或状态错误。P1 为权限/人控/预算/状态恢复或核心结论受破坏；P2 为可观察的数值、时间、来源、可见范围或因果/反证错误。仅不改变含义的表达问题可作为 P3 留存。

正常/故障各2次均满足，才记此有界开发包通过；4次全过也不证明泛化或生产可靠性。保留每次失败和未完成，不从分母移除；次数/窗口/数据不可比时不计算混合成功率。非退化规则为不新增 P1/P2、不以速度/费用抵消事实或控制退化，既有确定性回归全部通过。报告质量由独立证据审查，非模型自评；安全/最终状态仍用确定性断言。

## 本轮状态

本轮取得主动正常/故障 JSON 报告、有限核心结论、真实 PG 跨进程组合及多项确定性机制证据；但 fault/normal03 的独立完整报告质量均 FAIL，最新 view 修复仅离线；v4/v2新协议也尚无真实模型复验。因此本轮不满足上述入口，不写 feature passes=true、不启动 M1。下一项只做报告事实与实际可见证据的有界复验，保留本轮失败；新模型执行仍需新的明确次数/费用授权。

严格入口必须具备原始操作员输入、实际送模输入及各自精确hash；缺失/空/损坏来源记录不得跳过验证。缺失事实可保留在审计packet中，但不能获得合同一致结论，直接checker及investigator_input导出同样执行该约束；旧schema和历史工件不改写。

公共交付真实性不依赖最终报告是否可解析：每条已有Delivery均绑定actual input；已有ReportCapture须绑定身份、原文/hash、唯一已提交Delivery和响应时钟。没有请求/采集的合法handoff不补造记录；报告解析、声明和时效资格仍按其适用前提检查。

初始证据的采集时钟逐字段与原raw精确一致，原值缺失/null不能由bundle补齐，已知原值不能擦为None；unknown来源时间不得带无依据边界。CLI完整审计输出以原子独占方式创建为0600，保留已有文件及符号链接，不依赖调用者默认umask。

new_run边界：接收/新建/领取都在事务前验证非空版本映射，非法快照不得写入或推进控制版本；最后接受事件为new_run时，cancelled/waiting_human须有后续cancel/correct来源，不能继承旧Run人控状态。合法运行终态仍按各自证据检查。
