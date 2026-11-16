# 网页工作台 UI 重做：合同决定表（r2）

- 修订：r2（2026-11-16）。r1 为用户裁决入库；r2：独立审查后由协调者补充，未改用户裁决（审查报告 `~/.agent/handoffs/opspilot-ui-redo-contract-review-2026-11-16.md`，必须处理 M1–M6，审查者全新上下文、只读，基线 `main` d61e267e；r2 各条在本文标「r2」）。
- 任务记录：[2026-11-15-web-ui-redesign.md](2026-11-15-web-ui-redesign.md)；GitHub issue #572
- 来源：合同预审 `~/.agent/handoffs/opspilot-prereview-ui-redo-2026-11-15.md`（预审者未参与出稿、不参与实现，只读，基线 `main` e974a744）；可扫视性评审与 r2 复验 `~/.agent/handoffs/opspilot-ui-scan-review-2026-11-15.md`；设计说明修订草稿 `~/.agent/handoffs/opspilot-ui-spec-revision-draft-2026-11-15.md` 的「实现约束」一节。
- 行号核对：r1 对照 e974a744、r2 对照 d61e267e（两者之间只改文档）抽查，对不上的已改正并在该处注明「已改正」；未逐行核的保留预审原注「未确认」或「子 Agent 列出」。

## 0. 范围（用户裁决，2026-11-15 至 11-16）

- 目的：面试演示用，做到「相对不错」即可，不追细节，设计稿不再细化。
- 设计基线（黑白灰分组版，均在 `~/.agent/diagrams/`）：事故列表 `opspilot-ui-index-2026-11-15-r2.html`、事故详情 `opspilot-ui-incident-2026-11-15-c4.html`、复盘审核 `opspilot-ui-postmortem-2026-11-15-r2.html`。五色状态标记版（列表 r3、详情 c5、复盘 r3）作废；「全部八张稿连通」一轮在叫停指令送达前已做完，产物不作实现依据，其中核实到的代码事实保留使用。
- 实现范围：公共外壳（`base.html` 与新增 `_ui.html`）加主路径三页（事故列表、事故详情含转人工与告警片段、复盘审核）。其余五页（证据、知识、版本变更列表与详情、暂停控制）只继承外壳的字体、间距与基础样式，不单独重做，不新增内容。
- 注意：预审的逐页对照（第 3 节）是对着更早的稿（事故详情 c3、列表与复盘的首版）做的；与基线稿 c4、r2、r2 之间的差异预审未覆盖，实现时以基线稿为准，冒出的新项按任务记录「修订」规则追加。

## 1. 需用户裁决（已裁决）

U1–U4 与 U5 用户未逐项答复，按推荐（协调者依用户 2026-11-16 表态与常设授权代定）。

| 编号 | 问题 | 裁决 | 影响面 | 依据（已抽查） |
|---|---|---|---|---|
| U1 | 稿子新增、产品没有的 7 处内容：① 事故标题用告警 summary 注解，无告警退回 `Incident <id>`；② 顶栏只读声明；③ 导航加 Suspension 入口；④ 每个控制按钮旁的后果说明；⑤ Agent 能做与不能做的事；⑥ 每轮查询时间；⑦ 列表加目标服务与结论摘要 | 只保留落在三页与外壳内的项，按基线稿所画实现；属于其余五页的项不做。①–⑦ 都在外壳或三页内，全部做。④ 文案照稿（稿已逐句对过 `persistence/controls.py`）；⑤ 只写通用权限（D10）；⑦ 只做目标、告警名或问题、结论等级加首句，稿里其余列表字段不做（U2） | 外壳、三页；`store.py` 列表查询；`service.py` 步骤视图 | ① 注解已在页面数据：`opspilot/web/service.py:2953-2976` `_stored_alert`、`service.py:2499` `_alert_view`（已改正：原注 2953-2978 含两者，实际 `_alert_view` 在 2499）。③ `/controls` 已存在 `app.py:371`，导航缺 `base.html:28`。⑥ `opspilot_steps.observed_at`（列默认 `clock_timestamp()`：`opspilot/migrations/versions/0001_baseline.py:82,86`；`persistence/steps.py:156` 是 late_result 那条显式写入，r2 已改正），`_step_view` 未带（`service.py:3253-3293`）。⑦ 列表查询已 join `opspilot_targets`（`store.py:829-835`）。结论（r2 改正）：查询现在只取 `i.conclusion IS NOT NULL AS concluded`（`store.py:832`），列内存的是提交时的模型回复（`persistence/steps.py:441-444`），要经 `_report_view`（`service.py:3526` 起）解析才得到等级与 summary；取法见第 6 节 r2 条 |
| U2 | 设计备注里其余「需后端新增」的小项 | 只做模板能出的和库里已有、改查询或视图即可的；库里没有的、要迁移或改授权的、页面内报错重显的都不做，按稿里写好的退路显示 | 三页；不碰迁移与授权 | 暂停审计表网页角色只有 INSERT：`opspilot/persistence/grants.py` 的 `RUNTIME_GRANTS[WEB_ROLE]`（预审导入实测）；暂停原因无列：`opspilot/migrations/versions/0001_baseline.py:59-63`（已改正路径）；控制被拒统一返回 JSON：`app.py:223-232` |
| U3 | 验收测试锚点 | 新页面保留 `tests/acceptance/` 断言用到的 id、class 与固定措辞，验收测试一行不改；非验收目录的普通测试由独立测试作者按新页面改 | 全部模板（外壳改动波及其余五页，锚点同样要保留）；第 5 节清单 | `tests/acceptance/test_245_rebind_target.py:339-344`、`test_f13_postmortem.py:271-277`、`test_f11_step4_verdicts_no_gate.py:161,170`、`test_f2_alert_correlation.py:586-596` |
| U4 | 评审暴露的 6 处后端现有行为：① 运行中仍给 resume；② 归人工后仍给 takeover；③ 被全局或目标暂停压住与人工暂停都显示 paused；④ 恢复观察中接管后生命周期仍显示 observing_recovery；⑤ 运行中登记处置后 Run 仍可发布结论；⑥ resume 后上一代际未完成的工具调用不重放 | 都不改、不开 issue，页面如实显示；后果说明照实写 | 事故详情控制区文案；不改后端 | ① `service.py:268-319`、`persistence/controls.py:703-707`；② `persistence/controls.py:500-516`；⑤ `opspilot/observation/store.py:1266-1279`；⑥ `persistence/incidents.py:249-253`。③④ 按稿注引用，未确认 |
| U5 | 事故列表合同（[2026-10-01 列表合同](2026-10-01-m1-01-list-run-state.md)「合同」1–4） | 语义保留：每行仍显示 Run、Control、Lifecycle、Concluded 的存储原词；列顺序、表头清单与第一格内容随新设计变化。三处按固定表头清单、固定列序、第一格含完整事故编号取值的测试改为按表头名取值，由独立测试作者改：`tests/test_m1_incident_control_contract.py:106-116`（已改正：表头逐字断言在 106-113，114-116 是 `list_row` 取行）、`tests/integration/test_m1_list_run_state_postgres.py:161`、`tests/m1_incident_control_support.py:129-134` 的 `list_row`。Generation 不再单独成列（三个测试文件没有读它的行值，只在 `test_m1_incident_control_contract.py:110` 的表头清单里） | 事故列表；三处普通测试 | 基线稿 r2 已含这四列（表头 Status、Incident、Agent conclusion、Recovery、Run、Control、Lifecycle、Concluded、Opened） |
| U6 | 列表页人工提问表单（现名 Submit an incident） | 保留能力，接口 `POST /intake/ui` 与字段名 `target_id`、`question`、`idempotency_key` 不变。呈现：改名「Ask OpsPilot to investigate」，默认收起，放在列表下方，页面标题右侧一个次要按钮跳到并展开；说明写「事故通常由告警自动进来，这里用于没有告警时主动提问」。**Target 改成下拉**，候选取目标注册表的 key；注册表为空时回退为文本框。**幂等键**不由服务端生成：列表页 HTML 在两次请求之间必须逐字相同（验收测试 `tests/acceptance/test_m1_04_alert_intake.py:330-334` 比较整页文本，按 U3 不改），所以不能照暂停控制页的 `form_nonce` 做法。改为页面内一小段内联脚本生成（r2 补全时机）：① 页面加载时仅在字段为空时生成一次，之后提交与重试都沿用，不在每次提交时重新生成——服务端按 `intake:{key}` 去重（`service.py:432-447`），每次提交换键会让双击或慢网重复提交建出两个事故与两个 Run；② 用 `crypto.getRandomValues` 生成十六进制串，不用 `crypto.randomUUID`（后者只在 HTTPS 或 localhost 安全上下文可用，经代理走纯 HTTP 会抛错）；③ 先填值再隐藏字段；脚本失败或无脚本时字段保持可见的必填文本框、由人填写。字段名不变。Target 下拉属「代码里已有、改视图即可」。不做（人工介入的边角，按范围裁决）：并入事故的候选列表、登记处置 revision 的候选、编号复制按钮、页内目录 | 事故列表；`app.py` 列表页路由、`index.html` | [技术方案](../design/technical-proposal-2026-09-07.md)第 9–12 行批准的「人工请求」入口；现表单 `templates/index.html:8-14`。受理时未登记的目标不拒绝而是新建：`opspilot/web/service.py:470` 调 `register_target`，`opspilot/persistence/incidents.py:154-181` 已存在返回旧行、否则插入（`tests/integration/test_m1_otel_demo_target_fence_postgres.py:128-139`），所以文本框手误会静默新建一个空目标，这是改下拉的理由；之后调查对这种目标会怎样未确认（`service.py:459-461` 注释：工具网关按自己的注册表鉴权）。已知目标列表：`_registry_keys()`（`service.py:1144-1149`，来自 `OPSPILOT_TARGET_IDENTITIES`），目前只在详情页作 `rebind_targets`（`service.py:2455-2457`）；列表页路由只传 incidents（`app.py:365-369`）；暂停控制页的 `form_nonce`（`app.py:380`）每次请求都变，列表页不能用。并入规则备查：`opspilot/persistence/controls.py:780-793`、`service.py:283-290`。r2 审查已核对：下拉候选与 `target_id` 是同一种取值（`MappingTargetRegistry.keys()` 返回 `OPSPILOT_TARGET_IDENTITIES` 的键，`store.py:149-180`；加载时键与 `resource_uid` 不一致即拒，`opspilot/schema.py:189` 起；受理按同一字符串 `register_target`）；列表路由经 Workbench 新增的只读公开方法取键（`_registry_keys` 是私有方法，可逆细节）；内联脚本与现有 CSP 不冲突（`app.py:70-77` 只设 `frame-ancestors`，注释写明模板用内联脚本；`incident.html:449-469`、`postmortem.html:153-161` 已有内联脚本）；脚本与下拉选项每次请求相同，不破坏整页比较 |

## 2. 可逆默认（Agent 自定）

只列与外壳和三页相关的项；只涉及其余五页的 D5、D14 标「不在范围」。

| 编号 | 问题 | 推荐（已采纳） | 理由与依据 |
|---|---|---|---|
| D1 | 字体加载 | 只用系统后备字体；`font-family` 首位仍写 `"IBM Plex Sans"`，本机装了就用，不发外部请求、不随镜像带字体 | 面试演示够用；应用没有静态文件路由（`app.py` 无 StaticFiles） |
| D2 | 深色主题 | 不做，去掉稿子顶栏的切换钮 | 设计说明只定义浅色取值 |
| D3 | 界面语言 | 英文；版本变更详情页的中文固定提示逐字保留 | 提示在 `service.py:136`、渲染 `version_change.html:14`；验收断言子串在 `tests/acceptance/f11_steps_support.py:139` |
| D4 | 暂停要不要确认步骤 | 不加；按钮旁后果说明即提醒 | 两种暂停都可撤销，且不碰被调查系统 |
| D5 | 证据原文非文本分支 | 不在范围（证据页不重做）。预审判定当前不可达，严重度低：`app.py:940-946`、`opspilot/redaction.py:514-519` | — |
| D6 | 追问与补充更正并成一个输入框 | 保持每个动作一个表单，视觉上紧挨 | 幂等键模板 `incident.html:56`；`tests/test_m1_02_recovery_page.py:142` 断言 `<form` 数等于可用动作数 |
| D7 | 按钮文字改白话 | 改文字，隐藏字段 `action` 的 `value` 不变 | 是否有测试钉按钮文字未确认，实现前 grep |
| D8 | 证据抽屉字段 | 只用快照已有字段（工具、状态、是否采用、原因、来源错误、证据 id，加「打开完整证据」链接），几行原生 JS | `_step_view` 字段 `service.py:3253-3293` |
| D9 | 关键证据图配色 | 只换中性配色，不按语义标红 | `charts.py:48-51` 的 `_COLORS`（已改正：原注 47-50，47 行是边距常量）；图表测试只钉结构（子 Agent 列出） |
| D10 | Agent 权限清单写法 | 通用文字，措辞取自 PRODUCT-CONSTRAINTS；数据源复用快照的 coverage 来源与 `not_integrated` | 静态工具名会与部署注册的工具不符 |
| D11 | 列表「状态」人话 | 人话状态与待办药丸加在存储原词前面，不替换（U5）；四个字段组合到一句话的映射表由实现者写进任务记录，实现与独立测试都按表。r2：映射表含「Review postmortem」一档（取最新复盘版本 state，见 3.2） | `index.html:24-28`；字段与 `control` 属性 `store.py:34-78`（r2 改正：原注 34-63 不含 69-78 的 `control`） |
| D12 | 「x 条查询里 y 条被结论引用」口径 | 分母：当前 Run 步骤里所有带 `evidence_id` 的工具结果，`late_result`（仅作历史）不计；分子：其中出现在已发布报告任一结论 `evidence_ids` 里的条数 | 与覆盖率口径一致 |
| D13 | 加载时间（r2 改写） | 列表页与公共外壳不放任何每次请求都变的内容（加载时间、nonce、随机值）；基线列表稿右上「Loaded …」一行不实现，固定写「times in UTC · reload for current state」。事故详情与复盘审核页可以显示加载时间（取 `clock.now()`，显示 UTC） | 验收 harness 用 `DurableClock(store)` 建应用（`tests/acceptance/test_m1_04_alert_intake.py:144,161`），`DurableClock.now()` 每次执行 `SELECT clock_timestamp()`（`opspilot/web/store.py:526-538`），会查库且每次前进（r1「不查库」不成立）；`test_m1_04_alert_intake.py:330-334` 与 `tests/test_m1_04_alert_intake_contract.py:96-101,127-131` 比较两次 `GET /` 的整页文本，精确到秒的时间跨秒即失败。grep `tests/` 的整页相等断言只有这三处、都是 `GET /`，详情与复盘页没有，所以可以保留 |
| D14 | 版本变更 `ended_at` 时区与持续健康窗口 | 不在范围（版本变更页不重做）。事实备查：`ended_at` 已是 UTC（`service.py:2979-2985`），`sustained_window_seconds` 已在页面数据（`service.py:1863`，已改正：原注 1859 是 `ended_at`） | — |
| D15 | 控制被拒时浏览器看到 JSON | 维持 JSON；复盘审核现有的带错误重显保留 | `app.py:223-232`；复盘审核路由 `app.py:486` 起 |
| D16 | 样式放哪 | 内联在 `base.html` 的 `<style>`，公共部件写成 Jinja 宏放新的 `_ui.html` | CSP 只有 `frame-ancestors`（`app.py:74-77`） |
| D17 | 列表结构 | 保持 `<table>`；每行只放一个 `href="/incidents/{id}"`，告警名作同一链接里的文字 | `tests/acceptance/test_f2_alert_correlation.py:586-596` |
| D18 | 实时进度（SSE） | 只移位置；`EventSource` 脚本、重载事件种类、`id="progress"` 不变 | `incident.html:443-469` |
| D19 | 现有页面有、稿子没画的小信息 | 保留为一行安静说明：resolved 通知表、重复调用跳过数、尚未提交的计划工具数、图表溢出提示、登录通道 | `_alert.html:40-51`、`incident.html:355,396` |
| D20 | PR 分类 | 见任务记录「实现计划」：外壳 `chore`，三页 `feat`（功能 PR） | AGENTS.md「PR 与合并」 |

## 3. 逐页对照（三页）

分档：**模板** = 页面数据已有；**视图** = 库里已有，改查询或视图；**派生** = 由已有数据算出；**无** = 要迁移或新写入路径。结论按 U2。量为执行 Agent 工时。

### 3.1 事故详情（预审对照稿 c3；基线 c4）

| 稿上内容 | 对应现有数据 | 档 | 结论 | 量 |
|---|---|---|---|---|
| 顶栏：导航、只读声明、登录身份 | `base.html:27-30` | 模板 | 做；主题钮不做（D2） | 0.5h |
| 标题 = 告警 summary 注解，无告警退回 id | `_stored_alert`（`service.py:2953-2976`） | 模板 | 做 | 1h |
| 副标题告警名 | 标签在 `alert_json`，`_stored_alert` 只取注解与 `endsAt` | 视图 | 做（视图加 `labels.alertname`） | 1h |
| 结论句与摘要带结论格 | `snapshot.report.summary` | 派生 | 做；按词边界截断 | 1h |
| 结论等级、assessment、不完整与截断 | `incident.html:279-287` | 模板 | 做 | — |
| 「Why the agent believes this」 | `incident.html:289-321` | 模板 | 做 | 3h |
| 结论连到查询（引用编号） | claims `evidence_ids` 对 steps `tool.evidence_id` | 派生 | 做（D12） | 2h |
| 关键证据图 | `snapshot.charts`、`charts.py` | 模板 + 改配色 | 做（D9） | 2h |
| 「How the agent investigated」与筛选 | `snapshot.steps`（`_step_view`） | 模板 + 派生 | 做；筛选几行 JS | 3h |
| 每轮时间 | `opspilot_steps.observed_at` | 视图 | 做（U1 ⑥） | 0.5h |
| 证据抽屉 | `_step_view` | 模板 | 做（D8） | 2h |
| 「What the agent could and could not see」 | `coverage`、`not_integrated`（`incident.html:324-357,400-402`） | 模板 | 做 | 1h |
| 恢复观察侧栏、恢复条、观察记录 | `incident.html:75-158` | 模板 | 做 | 4h |
| 人工控制与后果说明 | `available_actions`（`service.py:268-319`）、`incident.html:47-73` | 模板 | 做（U1 ④、D6） | 4h |
| 决策日志含系统授权行 | 人工决定 `service.py:2265-2292` `_controls`；系统行在 `opspilot_controls`，网页角色有 SELECT | 视图 | 做（合并两处来源，按代际排序） | 2h |
| 复盘侧栏 | `review.incident_view`（`review.py:186`） | 模板 | 做 | 1h |
| Runs 与知识输入、更早的 Run | `incident.html:246-259,404-428` | 模板 | 做 | 1h |
| Raw records | `_alert.html`、question、run、budget、events | 模板 | 做 | 2h |
| 头部关系行（合并、改绑、重开、版本观察关联） | `incident.html:18-37` | 模板 | 做，保留锚点（U3） | 1h |
| 运行中实时进度放在结论位置 | SSE `incident.html:443-469` | 模板 | 做（D18） | 1h |
| 被暂停控制压住时单独标注 | 快照无此字段 | 无 | 不做（U4） | — |
| 转人工（无 Run） | `incident_handoff.html` | 模板 | 做 | 1h |
| r2：「What happened」时间线（告警到达、调查起止、报告发布、观察授权、恢复确认、复盘版本、关闭） | 投递 `received_at`、`snapshot.events[].recorded_at`（`service.py:3438-3446`）、`controls[].decided_at`、恢复会话时间、复盘版本 `created_at` | 派生 | 做，按现有数据画。`snapshot.events` 只有最近 50 条（`service.py:2442` 的 `events[-50:]`）：被截断时注明「earlier events not shown」，不补猜 | 2h |
| r2：摘要带除结论格外的四格（Grounds 计数、Blind spots、Recovery watch、Human control） | report 分类计数、`coverage`、`recovery`、`available_actions` | 派生 | 做 | 1h |

### 3.2 事故列表（预审对照首版；基线 r2）

| 稿上内容 | 现有数据 | 档 | 结论 |
|---|---|---|---|
| 事故 id、创建时间、生命周期、Run、控制、模式、重开、再次触发 | `IncidentSummary`（`store.py:34-63`）、`store.py:829-835` | 模板 | 做；四项存储原词保留为独立列（U5），人话状态加在前面（D11） |
| 告警名或问题文本、namespace/service | 目标已 join；告警名在 `opspilot_alert_identities` 或最新投递；问题文本在网页台账 intake 信封 | 视图 | 做（U1 ⑦），约 3h |
| 结论等级与摘要首句 | `i.conclusion` 原值（模型回复），视图层用 `_report_view` 解析 | 视图 + 派生 | 做，约 2h；同一条列表查询取原值，不新增查询（第 6 节 r2 条） |
| r2：Recovery 列（最新观察会话的 state、adopted_count、结束时间） | `opspilot_observation_sessions`、`opspilot_observation_endings`，网页角色有 SELECT（`opspilot/persistence/grants.py` 的 `RUNTIME_GRANTS[WEB_ROLE]`，r2 导入核实） | 改查询 | 做：同一条列表查询里用 LATERAL 取每个事故最新会话，约 2h |
| r2：「Review postmortem」待办分组 | `opspilot_postmortems`、`opspilot_postmortem_versions`，网页角色有 SELECT（同上） | 改查询 | 做：同一条列表查询里用 LATERAL 取最新复盘版本 state，进 D11 映射表，约 1h |
| 转人工原因码 | `IncidentSummary.handoff_reason` 已在列表数据 | 模板 | 做；验收要求列表页显示 `TARGET_UNRESOLVED`/`TARGET_AMBIGUOUS` 原码（5.1） |
| 观察采样明细、最近一次人工动作 | 观察样本、`opspilot_controls` | 视图 | 不做（边角） |
| 加载时间 | — | — | 不做（D13 r2） |
| 人工提问表单 | `index.html:8-14`；注册表 `_registry_keys()`（`service.py:1144-1149`） | 视图 | 做，按 U6（Target 下拉、幂等键由浏览器端生成） |

### 3.3 复盘审核（预审对照首版；基线 r2）

| 稿上内容 | 现有数据 | 档 | 结论 |
|---|---|---|---|
| 版本状态、代际、水位、哈希、生成记录、各节结论、争议、引用失败、提案、已发布、审核历史 | `postmortem.html`，数据 `review.version_page`（`review.py:215`） | 模板 | 做 |
| 头部事故上下文（告警名、服务） | `version_page` 只给 `incident_id` | 视图 | 做，约 2h |
| 本页版本表 | `version_page` 读的快照已含 `versions`（`review.py:218-219` 调 `_find_version`，`review.py:488-492` 遍历 `snapshot["versions"]`；r2 已核实） | 视图 | 做，约 1h |
| 生成尝试表 | `incident_view` 读了，`version_page` 没读 | 视图 | 做，复用，约 1h |
| 「What it was generated from」 | 各节规范 JSON | 派生 | 做；Run 证据数与恢复结果不做 |
| 每节计数、行内「引用有效 / 未争议」、按钮后果说明 | — | 模板 | 做 |
| 每条已发布知识旁直接撤销 | 要条目代际与重定向 | 视图 + 路由 | 不做；保留指向知识页的链接 |
| 「尚无版本」状态 | 无版本时路由 404 | 新路由 | 不做（留在事故页侧栏） |
| SSE 重载后保留已填理由 | — | 模板脚本 | 不做 |
| 冲突报错显示当前代际 | `error.current_generation`（`postmortem.html:24`） | 模板 | 做 |
| r2：「Postmortem events」列表 | `version_page` 只返回 `latest_sequence`（`review.py:276`）；事件在 `opspilot_subject_events`，`EventLog.read_after`（`opspilot/web/events.py:71`）可读，按 `EVENT_KINDS`（`review.py:51-57`）过滤 | 视图 | 做：复用 `EventLog` 读取，一次查询，约 1h |
| r2：按 hypothesis / counter-evidence 显示原因词 | 结论行只存 certainty（稿注已核） | 无 | 不做；退路是 uncertain 加小节名 |
| r2：水位「Identifiers and integrity」 | `version.watermark` | 模板 | 做，但保持验收句式，不画成稿上的键值表（第 6 节） |
| r2：结论行「citations valid / not disputed」 | `citations_valid`、`dispute_state` | 模板 | 偏离稿子：只在出问题时写 `citations invalid` / `disputed` / `not citable as fact`（第 6 节） |

其余五页（预审 2.4–2.7：证据、知识、暂停控制、版本变更列表与详情）不在范围，原表不搬。

## 4. 现有能力不丢（三页与外壳）

| 模板 | 操作或能力 | 处理 |
|---|---|---|
| base | 导航 Incidents、Version changes；登录身份与通道 | 保留，加 Suspension；通道一行小字 |
| index | 提交表单三字段；暂停控制与版本变更链接 | 保留（U6） |
| index | 列表行：Run、控制、生命周期、是否已结论、创建时间、转人工原因、再次触发 | 保留；代际不成列（U5） |
| incident | 12 种人工控制（close、reopen、takeover、merge_into、rebind_target、register_remediation、new_run、cancel、resume、pause、follow_up、correct）及各自输入 | 全部保留；rebind_target 与 new_run 按稿注位置补上 |
| incident | 代际条件更新说明；无 HealthProfile 时登记处置被拒的说明 | 保留 |
| incident | 恢复观察会话表、判定、离线回放一致性、样本与读数 | 保留 |
| incident | 复盘状态、版本表、生成尝试、模型调用明细、重试与停止说明、已发布知识 | 保留，放侧栏折叠块；r2：`postmortem-attempts`、`postmortem-versions` 两张表原样搬入，列数与逐格文本不变（5.1） |
| incident | 知识提示、报告异常态（解析失败、转人工未发布报告、不完整、截断） | 保留 |
| incident | 覆盖缺口、重复调用跳过数、图表及溢出提示、步骤表各列、未提交计划工具数、未接入数据源、调查历史、人工决定表、SSE | 保留；稿上缺的几行按 D19 补 |
| incident、_alert | 告警指纹、开始时间、原始值、受影响服务、注解、清理状态、截断、resolved 通知、投递表与再次触发 | 保留 |
| incident_handoff | 三种转人工原因文案与告警片段 | 保留 |
| postmortem | approve、supersede（保留 `entry_generation:{id}` 隐藏字段）、return 与 reject（必填理由）；不可审核说明；争议；引用失败；被替换知识；生成记录；审核历史；SSE | 保留 |

稿子里的表单都是静态示意，没有 `name` 与隐藏字段；实现时所有表单字段从现有模板原样搬。

## 5. 受影响的测试与钉值

做法：未跑全量验收探针，用 grep 清单代替；清单由预审的只读子 Agent 生成，预审抽查 10 处、r1 再抽查约 15 处（标「已抽查」）；r2 按独立审查补入漏项（标「r2」），审查者抽查 70 处、协调线 r2 复核补入各行。每个实现 PR 开工后第一件事是在该 worktree 跑一次 `.venv/bin/python -m pytest tests/acceptance -q`（本机内存允许时）或依赖 CI 的 PG 任务，清单外的失败追加进本表。

### 5.1 `tests/acceptance/` 钉页面的测试（按 U3 原样通过）

| 文件 | 钉住的锚点 | 复核 |
|---|---|---|
| `test_f11_step1_rollout_trigger.py` | 版本详情页含「不批准、不阻断、不执行发布」（:143，常量 `f11_steps_support.py:139`）等 | 已抽查 |
| `test_f11_step2_compare_under_policy.py:71-84` | `calibrated_policy_revision` 与四个 scope 名 | 子 Agent 列出 |
| `test_f11_step4_verdicts_no_gate.py:161,170` | 判定标签；unknown 时原结束原因代码 | 已抽查 |
| `test_f11_pr5a_release_pages.py:261` | 首页含 `href="/version-changes"`（外壳导航须保留） | 子 Agent 列出 |
| `test_f13_postmortem.py:271-277`（经 `tests/f13_acceptance_support.py`） | `postmortem-status`、`postmortem-generation`、`postmortem-latest` 恰为 `version N`、`postmortem-schedule`、`control-generation` | 已抽查 |
| `test_f2_alert_correlation.py:586-596` | 每个事故的 `href` 整页恰好 1 次；行内「re-fired」 | 已抽查 |
| `test_245_rebind_target.py:339-344` | `id="current-target"`、`<p class="rebind">`、「Rebound from」、`name="to_target"` | 已抽查 |
| `test_245_merge_into.py:365,367` | 「Merged into」「Merged from」 | 子 Agent 列出 |
| `test_343_alert_after_release_incident.py:745-746` | `related-incident` 不出现 | 子 Agent 列出 |
| `test_m1_04_alert_intake.py` | `id="alert-affected-service">otel-demo/checkout`、原因文案、XSS 转义；`/` 前后整页相等（:334） | 子 Agent 列出 |
| `test_m1_04_alert_window.py:259` | 正则 `name="expected_generation" value="(\d+)"`，属性顺序敏感 | 子 Agent 列出 |
| `test_f7_step4_suspension_postgres.py:673-685` | 暂停页 text/html 且含幂等键；HTML 提交返回 303 | 子 Agent 列出 |
| `test_319_escaped_userinfo_postgres.py:124-125` | 页面含 REDACTED_CREDENTIAL、不含明文 | 子 Agent 列出 |
| r2：`test_f13_postmortem.py:285-298` | 事故页 `postmortem-attempts` 表：行数等于尝试数，每行恰好 7 格，逐格文本相等（尝试 id、状态、错误码、`version` 加 ` revises N`、`x / y`、`started → finished`、逐次模型调用串） | 已抽查 |
| r2：`test_f13_postmortem.py:299-307` | 事故页 `postmortem-versions` 表：行数等于版本数；第 0、1、2、4、6 格为版本号、`state stale_reason`、revises、争议数、`created_at by X (kind)` | 已抽查 |
| r2：`test_f13_postmortem.py:308-318` | 复盘页 `version-state` 恰为存储状态、`postmortem-generation` 恰为代际；归一化全文含 `control generation N`、`observation generation N`、`runs N`、`input W`、`evidence snapshot S`（小写标签、一个空格、值） | 已抽查 |
| r2：`test_f13_postmortem.py:319-344` | 复盘页 `conclusion-{key}` 含 body、author、certainty；`citations invalid`、`disputed`、`not citable as fact` 三个子串各自当且仅当对应状态成立；证据链接 `/incidents/{id}/evidence/{eid}`；`id="disputes"` 含争议五字段 | 已抽查 |
| r2：`test_393_live.py:175-188` | `id="run-state">{state}<`（id 为该标签最后一个属性，标签后紧接状态词）；`<code>{run_id}</code>`（code 不带属性）；`id="report-status"`。**CI 里被 skip（`:28-30`，需 `OPSPILOT_393_LIVE=1`），改坏了 CI 不报**，实现 PR 用 grep 自查 | 已抽查 |
| r2：`test_f2_alert_correlation.py:545-565` | 事故页含 T2 的 startsAt 原串；`re-fired episode` 恰好出现 1 次；`related-incident` 要么在带 href 的 `<a>` 上，要么所在元素之后紧跟 `<a href>`（正则 :555-559） | 已抽查 |
| r2：`test_343_alert_after_release_incident.py:476-481` | 同上的 related-incident 正则 | 已抽查 |
| r2：`test_m1_04_alert_intake.py:241-242,261-263,357-359,415` | 事故页有指纹、`otel-demo`、`checkout`；注解转义后显示；**列表页**与事故页都显示转人工原因码和完整事故 id；事故页有 `resolved` | 已抽查 |
| r2：`test_f11_step1_rollout_trigger.py:229` | 事故页含版本变更详情链接 | 已抽查 |
| r2：`test_245_merge_into.py:366,368` | 两向的 `/incidents/{id}` 链接 | 已抽查 |
| PR2 探针：`test_f3_step3_coverage.py:124` | 覆盖区的图例与 not-integrated 行须放在表格之前，否则其文本串进最后一行的状态判定（按 `<tr>`/`<li>` 切到区块末尾） | CI 实测（#659） |

### 5.2 文件名含 acceptance、不在验收目录（按普通测试，独立测试作者可改）

- `tests/test_f3_pr1_acceptance.py:383,411`：按 `<h2>Steps</h2>`、`<h2>Investigation history</h2>` 切片（已抽查）。
- `tests/integration/test_f3_acceptance_postgres.py:724`：`id="coverage-gaps"`。
- `tests/integration/test_f2_acceptance_postgres.py:1233`：「source may have received the request」。
- `tests/integration/test_f13_step4_acceptance_postgres.py:556`：`<section id="knowledge-hints">` 下的 `<li>` 与 `<a>`。
- `tests/integration/test_f12_pr1_acceptance_postgres.py:550-577`：证据 JSON 键集合；证据页不重做，不受影响。

### 5.3 普通测试中钉标记最多的（独立测试作者按新页面改）

| 文件 | 钉住的内容 |
|---|---|
| `tests/integration/test_m1_03_workbench_review_postgres.py` | 约 15 个 id 与带标签边界的片段 |
| `tests/test_m1_web_workbench.py` | 区块计数标题（:538-544）；`conclusion <span class="badge">inconclusive`；从 `name="idempotency_key" value="` 切幂等键（:688） |
| `tests/test_m1_02_recovery_page.py` | 恢复判定在报告之前（:108-115）；`<form` 数等于可用动作数（:142，已抽查） |
| `tests/m1_incident_control_support.py` | 每个 `<th>` 都算列头（:75-79，已抽查）；`summary_words` 取第一个 `<h2>` 之前的文字（:106-117，已抽查）；`list_headers`、`list_row`（:125-134，已抽查）。语义按 U5 保留 |
| `tests/test_m1_incident_control_contract.py` | 表头逐字（:106-113）；行值不得恰为 `queued`/`running`（:116，`IN_PROGRESS_WORDS` 在 :40）；按表头名取 Control、Concluded、Lifecycle（:115,140,171,210,374-377，已抽查） |
| `tests/integration/test_m1_list_run_state_postgres.py` | Run 在第 2 列（:161）；按表头名取 Run、Control（:105,171,273-274，已抽查） |
| `tests/test_m1_184_projection_impl.py:389,408` | `<span class="badge authorization-source">automatic</span>` |
| r2：`tests/integration/test_f2_c_impl_correlation_postgres.py:274` | 整页找 `alert re-fired after resolved`；列表稿状态说明写成「alert re-fired after recovery」，说明区也不能出现徽标原文 |
| r2：`tests/integration/test_m1_04_alert_intake_impl_postgres.py:328`、`tests/test_m1_web_workbench.py:573`、`tests/integration/test_m1_incident_control_postgres.py:126-130` | 列表页含 `TARGET_AMBIGUOUS`；列表含两个事故 id；按表头取 Control |
| 其余约 20 个 | 单个 id 或单句文案，见预审文件第 5.3 节（未逐一复核） |

### 5.4 `docs/evidence/` 的钉值

没有 pytest 读取归档 HTML。离线脚本 `docs/evidence/m1-184-self-heal-live/verify.py`（按 `<section id="recovery-verdict">`）与 `docs/evidence/f2-live/collect.py`（「re-fired episode」）只在重跑对应真实验收时用，保留 U3 锚点后照样可用。

## 6. 实现约束（来自现有合同与测试，不进设计说明）

**r2 总则：基线稿与验收断言冲突时，一律以验收断言为准、偏离稿子。实现者遇到这类冲突不必回头问，按断言做，并在 PR 里列出偏离处。**

- r2 复盘页水位保持断言要求的句式 `control generation N · observation generation N · runs N · input W · evidence snapshot S`（小写标签、一个空格、值），可以收进折叠块，不改成稿上的 `Control generation` / `Input watermark` 键值表（`test_f13_postmortem.py:308-318`）。
- r2 复盘页结论行里 `disputed`、`citations invalid`、`not citable as fact` 当且仅当对应状态成立时出现；没出问题时不写「not disputed」「undisputed」「citations valid」这类含这些子串或通过措辞的词，可写「no dispute」或不写（`test_f13_postmortem.py:323-329`）。
- r2 事故页 `run-state` 维持 `<span … id="run-state">值</span>`，id 放最后、紧接状态词；Run id 用不带属性的 `<code>`（`test_393_live.py:175-188`，CI 不跑）。
- r2 事故页侧栏的 `postmortem-attempts`、`postmortem-versions` 两张表原样搬进折叠块，列数与逐格文本不变。
- r2 列表新字段在同一条列表查询里取，不按事故逐条查询（列表合同第 3 条，`tests/integration/test_m1_list_run_state_postgres.py:217-253` 统计整个 GET 的查询数）：问题文本按 `intake_key` JOIN `opspilot_web_ledger`（ledger 写入为 namespace `intake`、key `intake:{idempotency_key}`，`service.py:432-447`；JOIN 条件实现时核实）；告警名从最新投递的 `alert_json` 取，namespace/workload 来自 `opspilot_alert_identities`；结论取 `i.conclusion` 原值，在视图层对同一批行用 `_report_view` 解析得到等级与首句，不新增查询，解析失败显示稿上的「Concluded · level not in the record」；Recovery 与复盘状态用 LATERAL 取（3.2）。不得按行调用 `self.ledger.get("intake", …)`（参照 `service.py:1151` 起的 `_target_uid`）或 `_alert_view`（`service.py:2504-2505` 按事故读 `alert_records`）。
- r2 列表页与公共外壳不放任何每次请求都变的内容（D13）。
- r2 公共外壳不加 `<h2>`、`<th>`；页眉文字不含 `state`、`queued`、`running`、`closed`（外壳页眉落在 `summary_words` 读取的第一个 `<h2>` 之前范围内，`test_m1_incident_control_contract.py:92,100`，取消场景断言 `closed` 不出现；`:137` 等处还要求这段范围里出现 lifecycle 原词如 `open`）。事故详情稿 c4 部分状态在第一个 `<h2>` 之前画了「state as of …」，不照抄。

- 列表四项存储原词各为独立列，表头逐字为 `Run`、`Control`、`Lifecycle`、`Concluded`；单元格只放原词，空值单元格文本为空（短横线用 CSS 画，不进文本）。
- 测试把页面上每个 `<th>` 都当列头：分组标题行、说明行用 `<td colspan>`；「Stored values」这类归组标注用 CSS 生成，不新增 `<th>`。
- 每行只有一个指向事故页的地址，属性值也算：行上不放 `data-href="/incidents/…"`，指向事故页操作的待办药丸不做成第二个链接。
- 「re-fired」只用于 `refired_after_resolved`（`test_f2_alert_correlation.py:590,596`）；相关事故提示写 “opened after … was closed”。
- 列表行的人话状态单元格不要恰好等于 `queued` 或 `running`（`test_m1_incident_control_contract.py:116`，测试改写前仍生效）。
- 事故页第一个 `<h2>` 之前的文字被 `summary_words` 读取，不得出现 `state`、`queued`、`running` 这类事故级状态词（`test_m1_incident_control_contract.py:92,100`）；测试改写时保留语义。

## 7. 未确认

r2 关闭：整页比较与加载时间（D13）、复盘页版本表数据来源（3.3）、U6 下拉取值是否同一种（U6）。

- 全量验收探针仍未跑；M1 的漏项来自 grep 加人工阅读，可能还有遗漏（见第 5 节开工首件事）。
- 按钮文字是否被测试钉住（D7）。
- U4 的 ③ ④ 行号。
- 决策日志的「系统授权行」：`observation/store.py:1741` 往 `opspilot_controls` 写行，未逐条追到自愈授权路径。
- U6：文本框回退时手误新建的空目标之后调查会怎样。
- 预审对照稿与基线稿的差异：r2 按审查补了 M5 四块；审查者也没有逐状态通读 c4 的 8 个状态（Why、How、Recovery records、Raw 只看了标题）。
- 子 Agent 清单覆盖面：约 60 个文件未逐个读完，5.3「其余约 20 个」未逐一复核。
