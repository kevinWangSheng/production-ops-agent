# 批次 2 工作台体验（#652 #689 #704 #715 #744）：开工前合同预审（2026-11-22）

- 预审者：独立 Agent，没参与实现，只读，没改仓库。对照 `origin/main` `7ebb04fa`（已 `git fetch`）。
- 读过：共用任务书 `opspilot-next-batches-brief-2026-11-22.md`；AGENTS.md；PRODUCT-CONSTRAINTS.md（第 30、46 行）；`docs/tasks/README.md`「合同预审」；工作台重设计合同 `docs/tasks/2026-11-17-workbench-redesign-contract.md`（U 组、测试计划、P6 r11 L1–L10）；设计稿 `docs/design/workbench-redesign-2026-11-17.md` §5–§6；`docs/tasks/2026-11-15-itbench-lite-root-cause.md`（R1–R9）；`docs/design/web-ux-spec-2026-11-16.md:59,179`；C3 L2（`technical-proposal:170-180`）；五个 issue 的正文与评论（全部没有评论）；代码见各行引用。
- AWS DevOps Agent 原文（2026-11-22 重新抓取 `production-operations-autonomous-incident-response.html`）：Summary 标签页包括 "The current cause hypothesis or confirmed root cause" 和 "Expandable findings and supporting evidence, including links to relevant resources"；反馈表单有 "Verdict (required) – Whether the root cause was correct or incorrect" 与 "Actual root cause"。没有讲列表页显示根因，也没有讲复盘或事故报告页（这一页没有讲，其他页【未确认】）。
- 标注：【核实】读过代码或跑过；【推断】从代码推出，没跑；【未确认】没核对到。
- **全量探针：没跑。** `sysctl vm.swapusage` 显示已用 16373.56M / 17408M（大于任务书规定的 10 GB），按任务书跳过 pytest。本机也没有可用的 PG（没有 `psql`/`pg_isready`）。开工后由草稿 PR 的 CI 跑 `tests/acceptance` 和 `-m pg`。下面「钉值」一节列出了我用 grep 找到、预计会受影响的断言【推断】。

## 一、结论

- 需用户裁决：**2 项**，都属于 #652（新能力，事故负责人看得见的行为）。其余 4 个 issue 只改显示或修缺陷，**可逆默认 18 项**。
- 预审中新发现的 3 个事实（实现时会踩到）：
  1. **复盘生成器里的报告白名单实际上没起作用（不在本批，建议另开 issue）。** `knowledge/generation.py:160` 的 `_REPORT_FIELDS` 含 `"conclusion"`，`_report()`（`:686-700`）把事故的 `conclusion` 列当成报告本身来读。但这一列存的是终止步骤 `{"kind":"conclusion","conclusion":{report_content, execution, replies, prompt_revision, calibration, …}}`（`investigation/loop.py:1642-1677`，F13 测试夹具也是这个形状，`tests/test_f13_step4_acceptance.py:168-186`）。所以复盘模型收到的是 `{"conclusion": <整个步骤体>}`：报告是一段**原始 JSON 字符串**（里面已经带着 v3 的 `root_cause`），另外还有回复、提示词版本号和校准数据。`summary`/`claims`/`gaps` 没有按白名单拆出来。实测：`_report(<真实形状>, [])` 返回 `{'conclusion': {'execution': …, 'report_content': '…', …}}`【核实，本地跑了 `.venv/bin/python`】。没有测试断言 `investigation_report` 的内容（grep 无结果）。修它会改复盘模型的输入（要升 `POSTMORTEM_INPUT_POLICY_VERSION`，`contract.py:58`，并在 `tests/safety` 加一个变异），所以不放进本批。这件事也影响 B2-U2 选项 b 的代价。
  2. **#744 第 1 项不能直接把标题改名。** C1 合同 C1-10 定的卡片标题是「Investigation plan」，独立作者写的测试钉了这个标题：`tests/integration/test_tool_expansion_c1_todo_postgres.py:436` 断言标题存在，`:483` 断言标题不存在。改成「Latest plan」后，`:436` 会失败；`:483` 是反向断言，改名后会不再检查任何东西却照样通过（等于悄悄削弱了测试）。默认做法见 B2-R14：保留标题，加一个限定词。
  3. **#744 第 4 项要改调查循环。** 任务清单的追踪 span 由 `investigation/loop.py:1826` 的 `tracer().tool_call(...)` 打开，`tracing.py:972` 在这个调用里给 `parent.tool_calls` 加一。不改 loop.py 就没法把任务清单排除在计数之外，而 AGENTS.md 要求改 loop 的 PR 附一次有界真实 Run 的证据。建议移到批次 4，跟租机一起做（见第六节）。

## 二、需用户裁决（2 项）

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| B2-U1 | **事故页怎么显示报告里的根因**（#652，新能力）。现在：v3 报告可以带一个 `root_cause` 对象（kind、namespace、name、证据编号，`investigation/reports.py:158-191`），但页面完全不读它：`categorize_report` 只拆 claims（`web/service.py:5020-5050`），结论区只有结论等级、摘要和事实数（`_incident_regions.html:212-233`）。设计稿 §5 原本写的是「有 root_cause 时显示，标『Confirmed』」（`workbench-redesign:277`）。但校验器对根因只检查证据编号是不是本 Run 交付过的，不做事实级检查（R6，`reports.py:437-446`），PRODUCT-CONSTRAINTS 第 46 行也写着「the investigator does not certify its own correctness」 | **a. 照 AWS 的「hypothesis 或 root cause」两档，不写 Confirmed**：在结论摘要第一句下面加一行。结论是 supported 时写「Root cause」，partial 时写「Likely root cause」，后面跟 `Deployment otel-demo/payment`（集群级对象写 `Node worker-1`），再跟证据链接，点开就是那条查询和结果（和 Key evidence 同一套链接）。根因为 null 或结论是 inconclusive 时不显示这一行。列表页不显示。代价：一个模板块加 `categorize_report` 多一个键。**b. 照设计稿**：supported 时标「Confirmed」，其余情况显示第一条假设、标「Hypothesis」。代价：「Confirmed」让模型给自己的结论盖章，和 PC 第 46 行冲突；页面下面本来就有 Hypotheses 区，会重复。**c. 再加列表页**：列表行写「Root cause: Deployment payment」。代价：AWS 列表没有这个，列表 SQL 要多拆一个字段，违背「只保主路径」 | **a** | `web/service.py` `categorize_report`；`web/incident_view.py` `_report_block`（`:419-453`）；`_incident_regions.html` `results()`；不迁移、不改授权、不改模型输入 | |
| B2-U2 | **复盘里怎么显示根因**（#652 的后半）。现在：复盘页由代码写的章节（incident、timeline、runs、human_actions、recovery，`knowledge/contract.py:84`）和模型写的章节组成，都不含根因。复盘页上已经有一个先例：页面渲染时读取**当前**的反馈并显示「Actual root cause」，这段内容不进复盘文档，也不发给复盘模型（`web/app.py:714-715`、`postmortem.html:451`） | **a. 只在页面上显示**：复盘页渲染时读取事故当前已发布的报告，显示「Investigation root cause」一行，带证据链接。不写进复盘版本、不进导出、不发给复盘模型。如果事故的当前 Run 已经不是这一版草稿水位里的 Run（`Watermark.last_run_id`，`knowledge/store.py:103-113`），加一句「the report has changed since this draft」。代价：版本页显示的根因不随版本冻结（反馈也是这样）。**b. 写进代码章节**：在 `runs` 章节里加上根因。代价：存下来的复盘文档会变、复盘模型的输入会变（要升输入策略版本号、在 `tests/safety` 加变异），还会和上面「发现 1」的白名单缺陷缠在一起。**c. 复盘不显示**，只在事故页显示 | **a** | `web/review.py` `version_page`（`:226-310`）或 `app.py` 的复盘路由；`postmortem.html` 一个块；不改知识库存储与生成 | |

## 三、必答类别（docs/tasks/README.md 第 1–9 类）

| 类 | 状态 |
|---|---|
| 1 时间 | #652、#689、#715、#744 不涉及新的时间来源。#704 显示的读数时间窗取存储行的 `window_start`/`window_end`（`service.py:3724-3725`），按页面现有的 UTC 规则显示（`view.clock`）。#744 的「updated at step N」时间取那一步的 `observed_at`，规则和日志现有的时间一致 |
| 2 计数与口径 | #715：日志里「merged into this」的条目数等于实际并入的事故数，不再停在 20；Linked incidents 区仍然最多显示 20 个，多出时注明（P6 L1 不变）。#744：步骤摘要里的查询数不算任务清单（C1-9 已定）；trace 属性 `opspilot.run.tool_calls` 的口径移到批次 4（第六节）。#689：搜索字段的口径见 B2-R12 |
| 3 状态转移 | 都只是读取。根因跟着页面上显示的那份报告走（A8/A9：已发布的报告，否则是最新的不完整报告，`incident_view.py:1539-1560`），报告修订后，页面靠现有的 `/live` 刷新跟着变，不新增事件类型 |
| 4 主体与权限 | 不改授权。web 角色本来就能读事故的 `conclusion` 列、观察读数、合并审计和投递记录（P4/P6 已经在读）。复盘页按 U2=a 做时，读的是 web 已经读过的 `conclusion` 列【推断：`incident_postmortem`/`page_view` 已经在用这一列】 |
| 5 数据合同 | **不迁移**。`categorize_report` 的输出多一个 `root_cause` 键（只增不删，dict 或 None）。#689 只改 SQL 的 WHERE 条件（PG 17.9，`deploy/compose/compose.yaml:36-37`，支持 `IS JSON` 谓词）。web-ux-spec §3.7 第 179 行（「匹配 `ld.alert_json`」）在同一个 PR 里改成解码后的字段 |
| 6 旧数据兼容 | v2 报告没有 `root_cause`，不显示根因行；解析不了的报告照旧显示 `report-unparseable`。投递记录里 `alert_json` 可能为 NULL（保留期清理，迁移 0023），也可能被截断成不是 JSON 的文本（`store.py:1435-1444`）：这两种情况下只是告警字段匹配不上，不报错。观察读数里 `raw_verified` 为 None（原始数据包不存在或已被清理）的行显示「no bundle」，不标成不一致 |
| 7 并发与水位 | 没有新的写入。#715 改成一次不设上限的查询；合并只能由人发起，数量自然有界【推断】 |
| 8 外部调用 | 没有模型调用，也没有数据源调用（U2 选 b 才会改复盘模型的输入，推荐的是 a） |
| 9 验收投影与 `passes` | 不翻任何 `passes`。F13 第 1 步（复盘草稿内容）不受 U2=a 影响。F6 第 5 步（重放存储的观察、重建恢复依据）：#704 在页面上补回逐条读数的依据，这一步的支撑变得更完整，`passes` 不变（P4 去掉这些字段时也没有翻）。`feature_list.json` 里没有根因显示的步骤 |

## 四、可逆默认（实现者自定，各一行理由）

| 编号 | issue | 默认 | 理由 / 依据 |
|---|---|---|---|
| B2-R1 | #652 | `categorize_report` 加键 `root_cause`：`{kind, namespace, name, evidence_ids}` 或 None；`namespace` 为空字符串时当作 None（与 PR-A 的规则一致） | 撤销 R9「本轮忽略」正是 #652 的目的；只增键，JSON 读取方不受影响 |
| B2-R2 | #652 | 照报告原文显示：等宽字体，`Kind namespace/name`，没有 namespace 时写 `Kind name`；kind 不做归一（评测侧的 `normalise_kind` 不搬进产品）；Jinja 自动转义 | kind 是自由文本（R4）；页面显示的就是报告写的内容 |
| B2-R3 | #652 | 证据链接复用 `_report_block` 的 `with_calls`（`incident_view.py:425-430`）：能对上本页查询的显示为「tool · step N」，对不上的链接到 `/incidents/{id}/evidence/{eid}`。链接文字不显示完整证据编号（真实编号像 `6638448a-…-t0:5013c954-…`，见 F3 的 `summary.json`），在 400px 宽度下也不撑破版面 | 和 Key evidence 一样；PC 第 30 行要求链接能解析到真实证据 |
| B2-R4 | #652 | 不完整报告（A8）：结论必定是 inconclusive（`reports.py` `INCOMPLETE_REPORT_CONTRACT`），按 U1=a 不显示根因行 | R5：inconclusive 时忽略根因 |
| B2-R5 | #652 | 反馈卡片（「Rate the root cause」）的文字不改，表单也不预填报告的根因 | 反馈是人的判断，预填会诱导人直接照抄 |
| B2-R6 | #652 | U2=a 时，复盘页这个块放在页头的摘要附近，标注「from the investigation report · not part of this draft」；不进导出，不进知识条目 | 照搬反馈块的先例（`app.py:714`） |
| B2-R7 | #652 | `incident_handoff.html`（交接页）的报告区如果也渲染结论，同样加根因行；没有结论就不加【未确认：没逐行读交接页】 | 同一份报告在不同页面显示要一致 |
| B2-R8 | #704 | 展开 Recovery watch records 后，每个样本下面逐条列出读数：signal、stored → replayed（status / value / count，值为 None 时写「–」）、verdict 和 reason、query、时间窗、source、raw sha256，以及「verified / hash mismatch / no bundle」。直接照搬 P4 之前的写法（`git show 09c1a68b^:opspilot/web/templates/incident.html` 第 591-597 行） | 字段都已经由 `_recovery_view` 提供（`service.py:3719-3733`）；照搬旧写法最快 |
| B2-R9 | #704 | 有问题的读数单独标在自己那一行，用图标加文字：`✗ hash mismatch`（`raw_verified is false`）、`✗ replay differs`（`replayed` 不为 None 且与 stored 不同，即 `ReplayedReading.matches` 为假，`observer/replay.py:127-129`）；`no bundle` 和「raw data cleaned」用灰色，不算不一致 | issue 要求不能只靠颜色区分；数据被清理不等于被篡改（`replay.py:96-97`） |
| B2-R10 | #704 | 400px 宽度下用现有的 `.tblwrap { overflow-x: auto }`（`_incident_page_css.html:90`）让表格在自己的容器里横向滚动；sha256 单元格允许换行 | 已有样式 |
| B2-R11 | #715 | `linked_incidents` 改成可以不设上限（`limit=None` 时 SQL 写 `LIMIT ALL`）。service 读一次全部合并记录：Linked incidents 区在 Python 里取前 20 个并带上截断标志，日志用全部记录。排序不变 | 一次查询同时供两处使用；告警重发事件（refires）本来也不设上限（`service.py:3577-3593`） |
| B2-R12 | #689 | SQL 只匹配解码后的 `labels.alertname` 和 `annotations.summary`：`CASE WHEN alert_json IS JSON OBJECT THEN (alert_json::jsonb) #>> '{labels,alertname}' END`，summary 同理。问题原文、service 和短 id 前缀照旧。不再匹配整段 JSON 文本（键名、其他 label 都不再命中）。和内存实现 `_matches` 一致（`incident_list.py:187-197`），也和占位符「Search title, alert or question」一致（`web-ux-spec:59`） | issue 要求；用内存实现作为参照 |
| B2-R13 | #689 | 告警原文里含 `\u0000` 转义时，PG 的 `::jsonb` 会报错，整页搜索都会失败【未确认，没有 PG 环境验证】。加一个守卫，含这种转义的记录在告警字段上视为匹配不上（和内存实现在这种极端输入上有一点差异，写进 spec）。按原值匹配，不按折叠空白后的标题匹配 | 不可信输入不能让列表页报 500 |
| B2-R14 | #744-1 | 保留标题「Investigation plan」，后面加「latest · updated at step N, HH:MM UTC」 | 保住 C1-10 的措辞和 `test_tool_expansion_c1_todo_postgres.py:436/483`，同时说清楚这是最新的一份计划 |
| B2-R15 | #744-2 | 一个步骤既有查询又更新了计划时，折叠的摘要写「1 query · plan updated」（`_incident_regions.html:314` 那个条件分支） | 不展开也能看到 |
| B2-R16 | #744-3 | 实时合并时，`data-key` 以 `plan-` 开头的条目原地更新，不计入「N new ↓」，也不作为 `firstUnseen`（`incident.html:606-638`） | 计划卡片在 Run 的顶部，算作「新条目」会把读者拉回顶部 |
| B2-R17 | 全部 | 测试：不新增场景函数（合同测试计划的上限已经用到 12 个），也不新增浏览器路径。每个 PR 只给 1–3 句定向断言，由独立的 Opus 测试作者写，放进现有文件（#652 → `tests/acceptance/test_workbench_conversation.py` 不加函数，改放在页面渲染的单元测试里；#689/#715 → 带 `pg` 标记的集成测试，确认 CI 的 `-m pg` 能选中它，`ci.yml:264`）。#689 加一句「内存实现与 SQL 实现对同一组输入（标题含引号或反斜杠、只出现在 JSON 键名里的词、截断的记录）结果一致」 | 「测试少而有用」；CI 过去出现过 PG 测试漏选 |
| B2-R18 | 全部 | 截图放在仓库外的 `~/.agent/diagrams/opspilot-batch2-shots/<issue>/`，1440 和 400 两个宽度加深色模式；只给人看，不进自动断言（U2 的规则） | P3–P7 的惯例（`docs/tasks/2026-11-17-workbench-redesign.md:62-65`） |

## 五、PR 拆分、顺序、证据与合并类别

一个 issue 一个 PR，都以 `main` 为目标，不 stacked。三个前端 PR 都改 `_incident_regions.html`，所以按顺序合并，后开的 PR 先 `git merge origin/main` 再开。

| 顺序 | 分支 | 内容 | 合并类别 | 真实 Run 证据 | 截图（Opus 对照截图审查） |
|---|---|---|---|---|---|
| 1 | `chore/689-list-search-decoded`（提交类型 `fix`） | B2-R12/R13；web-ux-spec §3.7 第 179、453 行同步修改 | 缺陷修复，不属于用户门；独立审查后由 lead 合并 | 不需要（不碰调查循环、报告校验、恢复路径） | 不需要；证据是 PG 测试和一次本地搜索的前后对比（标题带引号的事故复制标题能搜到；搜 `"labels"` 搜不到） |
| 2 | `chore/715-merge-log-uncapped`（`fix`） | B2-R11 | 同上 | 不需要 | 1 张：并入 21 个以上事故的主事故，日志里能看到第 21 条「merged into this」，Linked 区写着「and N more」（1440） |
| 3 | `chore/704-recovery-readings`（`fix`） | B2-R8/R9/R10 | 同上（只改显示，没有红线，issue 原文也这么写） | 不需要：只改显示，读的是已有的 `_recovery_view`；观察器和重放都不改 | 用本地 PG 夹具造一个观察会话，3 种读数各一行：已验证、`hash mismatch`、`no bundle`/raw data cleaned，再加一行 replay differs；1440、400（看表格在容器内横向滚动、页面本身不横向滚动）、深色 |
| 4 | `chore/744-plan-card-polish`（`fix`） | 第 1–3 项（B2-R14/R15/R16）；第 4 项移出 | 同上 | 不需要（只改 web 模板和 JS） | 用脚本化模型客户端跑一个带 TodoWrite 的 Run：计划卡片标题带「latest · updated at step N」；折叠的步骤摘要显示「1 query · plan updated」；实时视图下计划第一次出现时**不弹出**「new ↓」（录一张实时视图截图，或者在 PR 正文写明浏览器里的操作步骤和结果） |
| 5 | `feature/652-root-cause-display` | U1、U2 + B2-R1–R7 | **用户门（功能 PR，新的用户能力）**；简报列出 U1/U2 的落地结果 | 不需要：不碰调查循环和报告校验（只读已经发布的报告）；U2 选 b 时也不需要，但要在 `tests/safety` 加变异 | 事故页：① supported + 带 namespace 的根因（用 F3 真实报告 `docs/evidence/f3-acceptance-live/prompt-8688f6e52275/run-1/summary.json` 的报告内容做夹具，`Deployment otel-demo/payment`，3 个长证据编号）；② partial（同目录 run-2）显示「Likely root cause」；③ 集群级对象（namespace 为空）；④ supported 但根因为 null；⑤ v2 旧报告；⑥ 不完整报告。①②两张各拍 1440、400 和深色。复盘页：根因块，加一张「the report has changed since this draft」的状态 |

审查：每个 PR 由一个没参与实现的全新 Opus 代理，对照截图和本表审一轮（AGENTS.md：前端 PR 用 Opus 看截图审）。只有违反约束、可复现的错误或 CI 失败才阻塞合并。

## 六、移出本批或不做

- **#744 第 4 项（trace 属性 `opspilot.run.tool_calls` 不计任务清单）→ 移到批次 4。** 要改 `loop.py:1826`（例如传 `counted=False`，`tracing.py:972` 据此不累加），按 AGENTS.md 要附一次有界真实 Run 的 trace。批次 4 本来就要租机跑全量，到时顺带用一个真实 trace 核对这个属性。默认口径：只计数据源工具，和 `summary.json`（C1-9）一致；另加 `opspilot.run.task_list_calls` 属性【推断，没有读者依赖这个属性：grep 只命中 `tracing.py` 和 `tests/test_tracing.py:339`】。
- **新缺陷（发现 1）：复盘生成器的报告白名单没起作用 → 另开 issue，不放在本批。** 修它会改复盘模型的输入（数据流 + 模型输入通道 → 要在 `tests/safety` 加变异、升输入策略版本号），属于用户门。建议放进批次 1（加固）或单独做。issue 正文只写现象和行号，不写凭据。
- 设计稿 §5「Root cause … 标 Confirmed」这一行，U1 选 a 后作废。`feature/652` 的 PR 在设计稿开头的状态说明里补一句。
