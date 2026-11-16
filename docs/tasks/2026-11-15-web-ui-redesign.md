# 网页工作台 UI 重做

- 状态：设计定稿，合同 r2（r1 已裁决），待实现
- 更新日期：2026-11-16
- 依据：GitHub issue #572；[设计说明](../design/web-ui-design-spec-2026-11-15.md)；[合同决定表 r2](2026-11-15-web-ui-redesign-contract.md)
- 工作区：设计说明 PR #636、#637；本记录与决定表在分支 `chore/ui-redo-contract`。静态稿保存在仓库外 `~/.agent/diagrams/`。实现各 PR 的分支与 worktree 开工时填在「实现计划」

## 目标与范围

把工作台换成设计说明里的版式，用于面试演示。实现范围：公共外壳（`base.html`，新增 `_ui.html` 宏）加主路径三页（事故列表、事故详情含 `incident_handoff.html` 与 `_alert.html`、复盘审核）。其余五页（证据、知识、版本变更列表与详情、暂停控制）只继承外壳的字体、间距与基础样式，不单独重做、不新增内容。不改路由、表单字段、授权与迁移。

## 已裁决（用户，2026-11-14 至 2026-11-16）

- 视觉方向用事故页设计稿第二版；不装官方 `frontend-design` skill，不用画布工具，不引入 Tailwind 或组件库。读者是没见过产品的资深工程师。设计稿数据与真实 Run 逐项核对，不编数据。
- 目的与标准（11-16）：UI 是面试演示用，做到「相对不错」即可，不追细节，设计稿不再细化。
- 设计基线（11-16）：黑白灰分组版——事故列表 `opspilot-ui-index-2026-11-15-r2.html`、事故详情 `opspilot-ui-incident-2026-11-15-c4.html`、复盘审核 `opspilot-ui-postmortem-2026-11-15-r2.html`。五色状态标记版（列表 r3、详情 c5、复盘 r3）作废；「全部八张稿连通」一轮在叫停指令送达前已做完，产物不作实现依据，其中核实到的代码事实保留使用。
- 实现范围（11-16）：外壳加三页，见上。
- 列表页人工提问表单保留能力，呈现改为「Ask OpsPilot to investigate」、默认收起、放在列表下方；Target 改下拉（注册表为空回退文本框），幂等键由浏览器端脚本生成、服务端 HTML 不变（合同 U6）。
- 模型安排：UI 相关的出稿、实现、评审都用 Opus 5.5。
- 字体只用系统后备字体；深色主题不做；界面语言保持英文，版本变更详情页的中文固定提示原样保留（设计说明「已定」）。

## 合同（r2）

r2：独立审查后由协调者补充，未改用户裁决。完整决定表（选项、依据、逐页对照、受影响测试、实现约束）见 [决定表](2026-11-15-web-ui-redesign-contract.md)。U1–U5 用户未逐项答复，按推荐（协调者依用户 2026-11-16 表态与常设授权代定）。

| 编号 | 裁决要点 | 出处 |
|---|---|---|
| U1 | 稿子新增的 7 处内容只保留落在三页与外壳内的项（7 处均在内，全做），按基线稿画法；属于其余五页的不做 | 决定表第 1 节；预审 U1 |
| U2 | 只做模板能出的，以及库里已有、改查询或视图即可的；要迁移、改授权、页面内报错重显的都不做，按稿里写好的退路显示 | 决定表第 1、3 节 |
| U3 | 保留 `tests/acceptance/` 断言用到的 id、class 与固定措辞，验收测试一行不改；普通测试由独立测试作者按新页面改 | 决定表第 5 节 |
| U4 | 评审暴露的 6 处后端现有行为都不改、不开 issue，页面如实显示 | 决定表第 1 节 |
| U5 | 列表合同语义保留（Run、Control、Lifecycle、Concluded 存储原词各成一列）；列顺序、表头清单、第一格内容随设计变化；三处按固定表头或列序取值的测试改为按表头名取值；Generation 不单独成列 | 决定表第 1 节；[列表合同](2026-10-01-m1-01-list-run-state.md) |
| U6 | 人工提问表单：接口与字段不变，改名、默认收起、置于列表下方并在标题右侧留次要按钮；Target 下拉取注册表 key，空则回退文本框；幂等键由页面内联脚本在提交前生成（服务端不生成，列表页 HTML 两次请求间须逐字相同）；并入候选、revision 候选、复制按钮、页内目录不做 | 决定表第 1 节 |
| D1–D20 | 可逆默认，Agent 自定（D5、D14 只涉及其余五页，不在范围） | 决定表第 2 节 |
| r2-M1 | 验收锚点补全（含复盘页 f13 表格与水位、CI 跳过的 `test_393_live`、re-fired 计数、列表页转人工原因码）；稿子与验收断言冲突时按断言做、PR 列出偏离，不回头问 | 决定表第 5.1、6 节 |
| r2-M2 | 列表页与外壳不放每次请求都变的内容，列表稿「Loaded …」不做；详情与复盘页可显示加载时间（D13 改写） | 决定表 D13 |
| r2-M3 | 列表 Recovery 列与「Review postmortem」待办都做，同一条列表查询里 LATERAL 取 | 决定表 3.2、D11 |
| r2-M4 | 列表新字段同一条查询取，结论原值在视图层用 `_report_view` 解析，不按行查询 | 决定表 U1、第 6 节 |
| r2-M5 | 基线稿新增块分档：时间线（最近 50 条事件）、摘要带四格、Postmortem events 做；hypothesis / counter-evidence 原因词不做 | 决定表 3.1、3.3 |
| r2-M6 | 幂等键页面加载时仅空时生成一次，`crypto.getRandomValues`，先填后隐藏，无脚本时可见 | 决定表 U6 |

## 前提与完成条件

- 前提：合同 r1 已入库；外壳 PR 先合，其余三个 PR 以它为起点。
- 完成条件：四个 PR 合并；三页在真实 Run 数据下于 1440、1100、400 三个宽度截图，页面本身不横向滚动；`tests/acceptance/` 全部原样通过；普通测试由未参与实现的测试作者改写并通过；每个功能 PR 经未参与实现的全新上下文 Agent 审查。
- 不翻转任何 `passes`；验收入口 `IncidentScenario -> IncidentOutcome` 不读 HTML。

## 实现计划

按 PR 顺序，PR 目标均为 `main`，不做 stacked PR。估时取自预审，单位工作日，依次为实现 + 测试作者 + 审查。分支：外壳 `chore/572-ui-shell`，三页 `feature/572-ui-<页名>`；worktree 为仓库同级 `../poa-572-<页名>`，实际路径开工时填。

| 顺序 | PR | 范围 | 类别 | 估时 | 分支 / worktree |
|---|---|---|---|---|---|
| 1 | 公共外壳 | `base.html` 取值、顶栏、导航加 Suspension、只读声明、系统字体；`_ui.html` 宏（来源记号、药丸、缺失斜纹、键值表、折叠）。其他页套新外壳、保留旧正文；不改后端。约束（r2）：不加 `<h2>`/`<th>`，不放每次请求都变的内容，页眉文字不含 state、queued、running、closed | `chore` | 0.5 + 0.2 + 0.2 | `chore/572-ui-shell` / `../poa-572-shell` |
| 2 | 事故详情 | `incident.html`、`incident_handoff.html`、`_alert.html`；`_step_view` 加 `observed_at`；`_stored_alert` 加 alertname；决策日志合并系统行；`charts.py` 中性配色；抽屉与筛选 JS | `feat`，功能 PR | 3 + 0.75 + 0.75 | 待填 |
| 3 | 事故列表 | `index.html`；列表查询加目标、告警名或问题、结论等级与首句；Recovery 列与复盘待办（LATERAL，r2）；状态映射表（D11）；人工提问表单（U6，路由加注册表 key，幂等键浏览器端生成）；三处列表测试按 U5 改写 | `feat`，功能 PR | 1 + 0.5 + 0.5 | `feature/572-ui-index` / `../poa-572-index` |
| 4 | 复盘审核 | `postmortem.html`；`version_page` 加事故上下文、版本表、生成尝试、Postmortem events（r2） | `feat`，功能 PR | 1.5 + 0.5 + 0.5 | `feature/572-ui-postmortem` / `../poa-572-postmortem` |

合计约 6 个实现日。PR 1 合并后 2–4 可并行，本机内存紧，最多两条线。PR 2 只换展示、不改报告校验；是否附真实 Run trace 由协调者判断。

## 进展

- 2026-11-15：设计说明成稿（PR #636），独立评审后修订（PR #637）。现有页面清单完成。事故页第二版与真实 Run 核对（[F9 第 2、4 步证据](../evidence/f9-steps-2-4-2026-11-10/README.md)）后修订。
- 2026-11-15：八张静态稿全部出稿并经独立设计评审与数据核对，最后一轮均无必须改项（PR #640）。
- 2026-11-15：用户看稿认为列表与复盘「看不清、定位不到状态、不知道哪些要处理」。独立可扫视性评审判定主要是信息缺失与结构问题；按评审出列表与复盘 r2、事故详情 c4，复验判列表可交用户，复盘余 2 处小项。设计说明修订草稿同时成稿。
- 2026-11-15：另出一版五色状态标记（列表 r3、详情 c5、复盘 r3），用户看后认为不如前一版，作废。
- 2026-11-15：「全部八张稿连通」一轮在叫停指令送达前已做完，产出带彩色状态标记的三张主稿就地修订、其余五张稿的 r2 和一张入口页（均在仓库外）；因用户选定黑白灰基线，不作为实现依据，其中核实到的代码事实（如列表页表单的目标注册与幂等键，见决定表 U6）保留使用。
- 2026-11-15：合同预审完成（未参与出稿的只读 Agent）：4 项需裁决、20 项可逆默认、逐页对照、受影响测试清单与拆步。
- 2026-11-16：PR 1 公共外壳实现（`base.html` 取值、顶栏、导航加 Suspension、只读声明；新增 `_ui.html` 宏）。本机 swap 紧，验收探针未在本地跑 PG 部分，依赖 CI；web 定向普通测试 320 项与不需 PG 的页面验收 32 项本地通过。
- 2026-11-16：用户裁决目的、基线、范围、表单呈现与模型安排；U1–U5 按推荐代定。合同 r1、设计说明修订入库（PR #654）。
- 2026-11-16：独立审查（全新上下文，只读）提出必须处理 M1–M6，均只改文档；协调者按「验收测试不改、够用就行、不为边角加后端」定，合同升 r2。
- 2026-11-16：PR 3 事故列表实现：列表一条查询加取问题（按 `intake_key` 联 `opspilot_web_ledger`）、最新投递的告警原文（Python 里取 `labels.alertname`，截断记录不是 JSON）、结论原值、最新恢复会话与待审复盘版本（LATERAL）；视图层 `opspilot/web/incident_list.py` 分组并用 `_report_view` 解析结论；人工提问表单按 U6。本机 swap 余量不足 1.5G，未起 PG，查询数与 SQL 依赖 CI 的 m0-postgres；截图用仿真实记录的离线渲染（1440、1100、400 页面不横向滚动）。
- 2026-11-16：PR 4 复盘审核实现：页面按基线 r2 重排（任务句、三格摘要带、待核清单、按节陈述、右栏决定与审核历史）；`version_page` 加版本表（最多 20 个）、生成尝试与调度（复用 `incident_postmortem`，store 上限 10 条）、Postmortem events（取事故最近 200 条事件按 `EVENT_KINDS` 与本复盘过滤，最多显示 20 条）；事故上下文（告警名、服务、问题、目标）由 Workbench 新方法按列表页同一查询取单行。偏离基线：水位保持验收句式收进折叠块；结论行只写例外（citations invalid / disputed / not citable as fact）；争议与引用失败列表保留（验收锚点）。本机 swap 余量约 1.0G，验收探针与 PG 测试未在本地跑，依赖 CI；截图用按真实记录（run e6058448）构造的离线渲染，1440、1100、400 页面不横向滚动。

### 列表状态映射（D11，自上而下第一条命中）

| 条件 | 分组 | 待办药丸 | 状态词 |
|---|---|---|---|
| lifecycle `closed`，有 `under_review` 复盘版本 | Needs a human | Review postmortem（链到复盘页） | Closed · postmortem version N waits for review |
| lifecycle `closed` | Closed | — | Closed（合并时加 merged into …） |
| 有 `handoff_reason`（无 Run） | Needs a human | Handle outside OpsPilot | Handed to a human, no Run · 原因码 |
| control `paused` 或 Run `paused` | Needs a human | Resume | Paused |
| Run `blocked` | Needs a human | Cancel or take over | Blocked |
| mode `human_owned` | Needs a human | Register or close | Taken over |
| Run `waiting_human` | Needs a human | Ask or take over | Waiting for a human |
| lifecycle `resolved` | Needs a human | Close or reopen | Recovered（`refired_after_resolved` 时加 re-fired 标记） |
| lifecycle `observing_recovery` | In progress | — | Watching recovery |
| Run `queued`/`running` | In progress | — | Investigating |
| control `cancelled` | Needs a human | New Run or close | Open, Run cancelled |
| 已有结论 | Needs a human | Close or register | Report published |
| 其余 | Needs a human | Take over or close | Open |

恢复列：无会话 Not started；`authorized` Watching + 采纳样本数；`recovery_confirmed` 结束 Confirmed + 结束时刻；其他结束 Ended, not confirmed + 结束原因码。结论列：未结论 No conclusion（Run 进行中为 No conclusion yet）；能解析 Supported / Partial / Inconclusive 加固定半句与摘要首句；不能解析 Concluded · level not in the record。

## 已知限制

- 复盘审核基线稿的草稿正文是缺失占位，另有真实正文示例状态（`?state=realtext`）。
- 本机无 IBM Plex，版式按后备系统字体验证，实现也只用后备字体。
- 全量验收探针仍未跑，以 grep 清单代替（r2 按审查补了漏项，仍可能有遗漏）。每个实现 PR 开工后第一件事是在该 worktree 跑一次 `tests/acceptance`（本机内存允许时）或依赖 CI，清单外的失败追加进决定表。`test_393_live` 在 CI 里被跳过，实现 PR 用 grep 自查。
- 预审的逐页对照对着更早的稿（详情 c3、列表与复盘首版）做；r2 补了审查发现的四块，基线稿各状态未逐一通读；其余未确认项见决定表第 7 节。

## 下一步

1. 开 PR 1（公共外壳），开工前在本记录填分支与 worktree。
2. PR 1 合并后按顺序开 PR 2–4；每个功能 PR 由独立测试作者按决定表改普通测试、独立 Agent 审查。

## 待决

无。实施中冒出的新问题按[合同预审](README.md#合同预审)的修订规则处理：可逆项实现者写一行理由，需用户裁决的停下，追加后递增修订号。
