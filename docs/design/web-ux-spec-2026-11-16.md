# 工作台 UX 规格（2026-11-16）

状态：草案。第 2、3 节（外壳/导航、事故列表）已定稿，可以进入合同预审。第 4 节（其余页面）只有要点，标了 TODO。

视觉基线沿用 [#572 设计说明](web-ui-design-spec-2026-11-15.md)：黑白灰、系统字体、英文界面、不用多色状态标记。

阶段划分（产品负责人 2026-11-16 裁定）：

| 阶段 | 范围 |
|---|---|
| 第一阶段 | 只改展示：每页显示什么、怎么读、数据多了怎么撑住。现有表单的字段与行为不变，只能挪位置或折叠 |
| 第二阶段 | 一切提交相关的改动，见第 5 节，只列要点。登录由另一条线负责，本规格不涉及 |

标记：
- **[a]**：只改模板或纯展示的视图函数。
- **[b]**：改 `app.py` 处理器、store 查询或 URL 参数，不迁移，不改认证。
- **[c]**：迁移、新存储能力、合同或验收断言变更。

「参照」列写这个做法取自哪个成熟产品。依据是对公开产品界面的了解，没有逐条对照当前官方文档，标为**未核实**；实现只照搬交互形态，不照搬视觉。

## 1. 根因（为什么 #572 没解决问题）

| 原因 | 证据 |
|---|---|
| #572 的合同不改路由、表单、授权；其余五页只套了外壳 | `docs/tasks/2026-11-15-web-ui-redesign.md:10` |
| 样稿和截图核验只用了 8 行列表数据 | 同文件 `:76` |
| 完成条件只检查页面不横向滚动 | 同文件 `:45` |
| 列表固定取最新 50 条，已关闭的事故也占名额；计数只数这 50 条；排在 50 名之外的待人工事故直接消失 | `opspilot/web/store.py:838-846`、`opspilot/web/incident_list.py:285`、`opspilot/web/app.py:366-370`（不接收查询参数）、`index.html:153` |

本规格的做法：
- 每页写明首屏内容和数据规模场景。
- 验收写成用户任务检查，并配大数据夹具。
- 交互照搬成熟产品，不自创。

## 2. 外壳与导航（`base.html`、`_ui.html`）

| 项 | 规格 | 参照 | 标记 |
|---|---|---|---|
| 顶栏 | 依次为 `Incidents · Version changes · Knowledge · Suspension`，右侧是只读声明和 `Signed in as alice`（去掉现在的 `· ui_basic`，`base.html:125`）。Knowledge 链到新的 `/knowledge` 列表（D-L2，随 PR 3 上线；在那之前不显示这一项） | PagerDuty、incident.io 顶部一级导航 | [a] |
| 高亮 | 每个模板都显式设置 `nav`。事故、证据、复盘页高亮 Incidents，知识页高亮 Knowledge。现在知识页默认高亮的是 Incidents（`base.html:113`） | — | [a] |
| 面包屑 | `_ui.html` 新增 `crumbs(items)` 宏。格式 `Incidents / <事故标题前 40 字> / Postmortem v3`；证据页为 `… / Evidence R4 · <tool>`；知识页为 `Knowledge / <条目名>`；版本变更页为 `Version changes / <service> <version>` | Grafana IRM 面包屑 | [a] |
| 时间 | 服务端输出 `<time datetime="<isoformat>" title="2026-11-16 14:02:11 UTC">2026-11-16 14:02 UTC</time>`，内联脚本把可见文字改成 `2 h ago`，`title` 里保留绝对时间。服务端不计算相对时间，原因见 §3.6 的 D13 | PagerDuty、Datadog 的相对时间 + 悬停显示绝对时间 | [a] |
| 长文本 | `.clamp2`：`-webkit-line-clamp: 2`，完整文字放在 `title` 里，最多 500 字 | — | [a] |
| 页面 GET 的 HTML 错误页 | 只在 `Accept: text/html` 的 GET 上生效：404、403、413、400、503，以及框架自带的 404/405，改为渲染 `error.html`。页面结构：一句标题、一句说明、返回链接，原始 code 放在 `<details>Details</details>` 里。状态码保持不变。不带该 Accept 的请求仍返回 JSON；401 不在此列，由登录线负责 | — | [b] |
| 外壳约束（保持不变） | 顶栏里不出现 `<h2>`、`<th>`、`<form>`，不出现逐次请求变化的文字，不出现 state、queued、running、closed 这几个词。依据是 `tests/test_m1_incident_control_contract.py:29-32,89-100` 读取第一个 `<h2>` 之前的文字，以及 `tests/test_m1_02_recovery_page.py:142` 统计 `<form` 数量 | — | — |

## 3. 事故列表 `/`（`index.html`、`incident_list.py`、`store.py`）

用户是值班者，要完成三件事：(1) 找到等我处理的事故；(2) 按服务或文字找到某一个事故；(3) 没有告警时手动发起一次调查。

### 3.1 页面结构（从上到下）

| 区块 | 规格 | 参照 |
|---|---|---|
| 标题行 | 左侧 `Incidents`。右侧 `<details id="ask">`，默认收起，摘要做成按钮 `Ask OpsPilot`，展开后是右上角的浮层，窄屏下全宽。浮层第一行是标题 `Ask OpsPilot to investigate`，`tests/test_572_incident_list_page.py:277` 按这段文字断言。浮层里是原来那个表单，`method`、`action="/intake/ui"`、字段 `target_id`/`question`/`idempotency_key`、Target 下拉和幂等键脚本全部不变（`index.html:226-246,289-305`）。DOM 顺序上，这个表单排在筛选表单之前，这样 `page.forms[0]` 仍然是它（`:297`）。删去 `#ask-jump` 和 `.ask-open` 两个跳转链接。这取代 #572 的 U6「放在列表下方」，属于负责人 2026-11-16 的裁定 | incident.io「Declare incident」、Rootly「New incident」右上角主按钮 |
| 状态页签 | `Needs a human (30) · In progress (12) · Resolved (3) · Closed (1,958) · All (2,003)`。每个页签是链接 `?view=…`，当前页签带 `aria-current`。计数覆盖全库，并且受当前筛选影响 | PagerDuty 事故列表的 Open/Triggered/Acknowledged/Resolved/Any 页签 |
| 筛选条 | `<form method="get">`：Service（目标）下拉，第一项是 `All services`，取值见 §3.7 Q3；文字搜索 `q`，占位符 `Search title, alert or question`；`Apply` 按钮。切换页签时保留筛选条件，页码回到 1 | PagerDuty 的 Services 筛选，Datadog 的搜索框 |
| 表格 | 可见 4 列：`Title`、`Status`、`Service`、`Age`。另外四个原值列 `Run`、`Control`、`Lifecycle`、`Concluded` 的 `<th>` 和 `<td>` 继续留在 DOM 里，内容与现在一致（`index.html:159,195-198`），用 `class="rawc"` 默认隐藏（`display:none`），页面不提供开关；保留它们是因为测试助手要用（§3.6）。只有 thead 一行 `<th>`，每个事故一行 `<tr>`，不再有分组行 `tr.gh` | PagerDuty、incident.io 的少列分页表 |
| 分页 | 每页 25 行。表格下方显示 `1–25 of 1,958 · ‹ Prev · Next ›`；第一页的 Prev 和最后一页的 Next 显示为禁用文字 | PagerDuty 分页表 |
| 状态说明 | 现有的「What each status means」保留，默认折叠，放在分页下方。删除现有的「Other controls」侧栏，因为它和顶栏重复 | — |

### 3.2 行内容

| 列 | 内容 |
|---|---|
| Title | 链接到 `/incidents/{id}`，每个事故只有这一个链接（`a.svc`）。文字按优先级取：告警的 `annotations.summary`（解析最新一次投递，和详情页 `hd.summary` 同源）→ `alertname` → 问题原文 → `Incident <短 id>`。用 `.clamp2` 限两行。下面一行小字是 `<短 id> · <alertname>`；如果是关闭后重开的事故，再接 `· after <短 id>`。不可信文本一律转义 |
| Status | 待办药丸（`ui.pill(todo=true)`，与现在的 `status_of` 相同）加上人话状态（`status.words`）。交接事故后面接 `<code>TARGET_AMBIGUOUS</code>` 这类原因码；重复触发时接 `alert re-fired after resolved` 标记。复盘待审时药丸链接到复盘页 |
| Service | `namespace/workload`。没有这两项时显示目标的 `resource_uid`，都没有则用 `ui.missing()` |
| Age | 创建时间，格式见 §2 的时间规则 |

从列表移走的内容（移到详情页，不删数据）：
- `Agent conclusion` 列和 `Recovery` 列。详情页首屏有这两项。
- 四个原值列在列表上不再可见，只保留在 DOM 里。要看原值，到详情页的 `Stored record`，那里已有 `run-state`、`incident-control`、`incident-lifecycle`、`concluded` 这几个 id（`incident.html:51-56`）。这取代 #572 的 U5「原值与人话并列显示」，同样是负责人的裁定。

### 3.3 页签成员规则

现有的 D11 映射在 `incident_list.py:58-126`，按顺序命中第一条：

| 页签 | 成员 |
|---|---|
| Needs a human（默认） | D11 的 `needs_human`：已关闭但复盘待审、仅交接、已暂停、卡住、已接管、等待人工、已恢复未关闭、已取消、报告已发布、开放无 Run |
| In progress | D11 的 `in_progress`：恢复观察中，或 Run 为 queued/running |
| Resolved | `lifecycle = 'resolved'`。这些事故同时在 Needs a human 里，因为它们还等人关闭 |
| Closed | `lifecycle = 'closed'`。其中复盘待审的事故同时在 Needs a human 里 |
| All | 全部 |

Resolved 和 Closed 是按生命周期看的视图，所以与「Needs a human」这个待办视图有交集，页签旁的说明里写明这一点。之所以不做互斥划分，是因为互斥会改到一条验收断言，见 D-L1。

### 3.4 URL 状态

| 参数 | 取值 | 非法值的处理 |
|---|---|---|
| `view` | `needs_human`（默认）、`in_progress`、`resolved`、`closed`、`all` | 按默认处理，不返回 400 |
| `service` | 下拉里的某个值 | 不在下拉值里时忽略 |
| `q` | 去掉首尾空白，最多 200 字 | 超出部分截断 |
| `page` | 正整数 | 超过最后一页时显示最后一页 |

不带参数的 `/` 就是默认视图。连续两次请求必须逐字节相同（D13）。

### 3.5 空、错误与规模

| 场景 | 显示 |
|---|---|
| 库里没有事故 | 保留现有空态 `No incidents yet.`，加一个 `Ask OpsPilot` 入口 |
| 当前页签为空，且没有筛选 | Needs a human：`Nothing needs a human right now.`；其他页签：`No incidents here.` |
| 有筛选但没有结果 | `No incidents match these filters.`，后面接 `Clear filters` 链接到 `/?view=<当前>` |
| 存储不可用 | 列表 PR 里维持现状（返回 503 JSON）；HTML 错误页随 PR 2 上线 |
| 规模：2000 个事故，30 个等人处理（最旧的排在第 1500 位），1958 个已关闭 | 默认页签分两页显示全部 30 个；计数正确；HTML 不超过 120 KB；SQL 共 3 条，不随行数增长 |
| 规模：40 行 | 每页 25 行，表格只有 4 列，标题限两行，页面上看不到展开的长文本 |

### 3.6 固定列表行为的测试

验收测试（`tests/acceptance/`）：以下 4 处在本设计下都能原样通过，不需要改断言。

| 位置 | 断言 | 本设计如何满足 |
|---|---|---|
| `test_f2_alert_correlation.py:569-595` | 把事故设为 `resolved` 后，`GET /` 里 `href="/incidents/{id}"` 恰好出现 1 次；用 `rindex("<tr")` 取出这一行；重复触发前这一行没有 `re-fired`，触发后有 | resolved 事故留在默认页签（§3.3）；行仍是 `<tr>`；只有一个详情链接；标记写在行内 |
| `test_m1_04_alert_intake.py:330-334` | 一个非法告警进来后，`GET /` 的结果与之前逐字节相同 | 服务端不输出相对时间，Service 下拉的值只取自数据 |
| `test_m1_04_alert_intake.py:357-362` | `/` 里含交接事故的 id 和原因码 | 交接事故在默认页签，原因码以 `<code>` 留在 Status 列 |
| `test_f11_pr5a_release_pages.py:258-261` | `/` 里含 `href="/version-changes"` | 顶栏有这个链接 |

普通测试：下面这些由未参与实现的测试作者按新页面改写。按 AGENTS.md，这不属于用户门。

| 文件与位置 | 会失效的点 | 改写方向 |
|---|---|---|
| `tests/test_572_incident_list_page.py:93-101`（解析器在 `tr.gh` 上记分组名）、`:160-173`（`assert "in progress" in _row_group(...)`）、`:176-189`（`"need" in group and "human" in group`） | 分组布局：页面不再有 `tr.gh` 分组行，Needs a human / In progress / Closed 也不再是同一页上的分组标题，而是页签 | 改为断言事故落在哪个页签：取 `/?view=in_progress` 和 `/?view=needs_human` 的行，或者读当前页签的 `aria-current` |
| `tests/test_572_incident_list_page.py:192-238` | `question[:48]` 应出现在 `Incident` 列；`Agent conclusion` 列的文字（`:225-238`） | 问题文字改为断言在 `Title` 列；结论文字改到详情页上断言 |
| `tests/test_572_incident_list_page.py:170,186,212` | 按表头名取 `Run`、`Control`、`Concluded` 的值 | 原值单元格保留在 DOM 里，这几处不用改 |
| `tests/test_572_incident_list_page.py:261`（`len(page.forms) == 1`） | 新增 GET 筛选表单后，页面上有 2 个表单 | 改为按 `action="/intake/ui"` 定位表单；`:263-276` 的字段集合与空幂等键断言不变 |
| `tests/test_572_incident_list_page.py:277,297` | 页面文字含 `Ask OpsPilot to investigate`；`page.forms[0]` 是提问表单 | 按 §3.1 的做法即可通过，可以不改 |
| `tests/test_572_incident_list_page.py:315-323` | 只有一个 `<th>` 行，且在 `<thead>` 里 | 新表头只有一行，原值 `<th>` 在同一行里，不用改 |
| `tests/test_572_incident_list_page.py:244-253` | D13 | 保持不变，并加上带参数的情形 |
| `tests/m1_incident_control_support.py:157-174`（`list_row`），被 `tests/test_m1_incident_control_contract.py:103-109,205,353-374`（8 处）、`tests/integration/test_m1_incident_control_postgres.py:80`、`tests/integration/test_m1_list_run_state_postgres.py:160-173,291`（5 处）使用 | 表头和单元格都还在，`list_row` 本身不受影响。会失效的是夹具：queued 或 running 的事故在 `In progress` 页签，不在默认页签 | 测试改用 `/?view=all` 查找；夹具行数控制在 25 以内 |

PRD 和 `feature_list.json` 都没有提到列表的列（grep 结果为空）。

### 3.7 数据与查询改动 [b]，无迁移

**接口**：`Workbench.incident_list(view, service, q, page)` 返回：
- `rows`：当前页最多 25 行
- `counts`：五个页签的计数
- `services`：Service 下拉的取值
- `page`、`pages`
- `targets`：已有字段

store 新增 `list_page(...)`，旧的 `list_incidents()` 和 `list_incident_items()` 保留不动：很多测试用 `list_incidents()[0]` 取最新事故（例如 `tests/test_m1_web_workbench.py:148`），而且 `incident_item` 也在用它们。

**SQL**：三条查询都在同一个 `transaction(snapshot=True)` 里执行。公共 CTE `base` 由 `_LIST_FROM` 加上三部分组成：
- 一个只取 `pm.state` 的 LATERAL；
- `wl`（问题原文）；
- `ld`（最新一次投递的 `alert_json`）。

页签归属用 SQL 的 `CASE` 算出，顺序和 `status_of` 完全一致：

```sql
CASE
 WHEN i.lifecycle='closed' THEN CASE WHEN pm.state='under_review' AND pm.postmortem_id IS NOT NULL THEN 'needs_human' ELSE 'closed' END
 WHEN <handoff_reason 表达式> IS NOT NULL THEN 'needs_human'
 WHEN i.state='paused' OR r.state='paused' THEN 'needs_human'
 WHEN r.state='blocked' OR i.mode='human_owned' OR r.state='waiting_human' OR i.lifecycle='resolved' THEN 'needs_human'
 WHEN i.lifecycle='observing_recovery' OR r.state IN ('queued','running') THEN 'in_progress'
 ELSE 'needs_human' END AS grp
```

`i.state='paused'` 对应的是 `IncidentSummary.control` 的含义（`store.py:72-78`）。

Service 的表达式：`COALESCE(a.namespace||'/'||a.workload, t.resource_uid)`。

| 查询 | 内容 |
|---|---|
| Q1 计数 | `SELECT count(*) FILTER (WHERE grp='needs_human'), … FILTER (WHERE grp='in_progress'), … FILTER (WHERE lifecycle='resolved'), … FILTER (WHERE lifecycle='closed'), count(*) FROM base WHERE <筛选>` |
| Q2 当前页 | `SELECT {_LIST_COLUMNS},{_ITEM_COLUMNS} FROM {_LIST_FROM} {_ITEM_JOINS} WHERE i.incident_id IN (SELECT incident_id FROM base WHERE <view 谓词> AND <筛选> ORDER BY created_at DESC, incident_id LIMIT 25 OFFSET %s) ORDER BY i.created_at DESC, i.incident_id` |
| Q3 下拉值 | `SELECT DISTINCT service FROM base WHERE service IS NOT NULL ORDER BY 1 LIMIT 200` |

筛选条件：
- `service = %s`
- `q`：先转义 `\`、`%`、`_`，然后用 `ILIKE '%'||%s||'%' ESCAPE '\'` 匹配问题原文、`ld.alert_json`（含 alertname 和 summary；这段文字已脱敏、有长度上限，且只来自最新一次投递）、service；另外用 `i.incident_id::text LIKE %s||'%'` 匹配短 id 前缀。

**分页**：用 OFFSET，不用 keyset。按每页 25 行和数千事故的规模，OFFSET 足够；而且 `?page=N` 可读、可分享。只有事故达到十万级才需要改成 keyset。

**一致性守卫**：新增合同测试，用夹具覆盖全部 13 个状态，逐行断言 SQL 算出的 `grp` 等于 `status_of(item)["group"]`。以后改 D11 必须两处一起改。

**索引**：不需要。现在 `opspilot_incidents` 上只有 `target_id` 索引（`opspilot/migrations/versions/0001_baseline.py:45`）。在 2000 行规模下，三条查询走顺序扫描加 LATERAL 也是毫秒级，这是推断，PG 上未实测。真到十万级，再考虑 `(created_at DESC, incident_id)` 索引和 `pg_trgm`，那时属于 [c]。

**内存 store**：`tests/m1_web_support.py:786-791` 的内存实现要镜像同样的过滤和分页逻辑，Protocol 定义在 `store.py:381`。

**视图**：`incident_list.list_page()` 不再分组，改为输出 `rows`、`tabs`（标签、计数、href、是否当前）、`pager` 和 `filters`。新增 `title_of(item)`，按 §3.2 的取值顺序；为此要在 `_item()` 里多解析一个 `annotations.summary`，与 `_alert_name` 同一处（`store.py:1034-1043`）。

## 4. 其余页面（第一阶段，只改展示）：TODO，待补全细节

| 页面 | 首屏 | 折叠与上限 | 参照 |
|---|---|---|---|
| 事故详情 `incident.html` | 头部：标题、人话状态、Age。摘要：结论等级加 1–2 句、恢复观察一行、最后一次人工决定一行。右侧属性栏：Service、Target、Opened、Run、Recovery。`Stored record` 原样移进 `<details>Stored values</details>`，仍放在第一个 `<h2>` 之前，id 不变，并在这里加上从列表移过来的四个原值。动作表单原样收进 `Actions (N)` 面板，不改字段和数量 | 每组陈述显示 5 条；轮次显示首 3 轮和末 5 轮；每轮工具调用显示 5 个；历史 Run 每个一行；恢复样本显示最近 20 个；告警投递显示最近 10 次；决策显示最近 10 条；事件折叠。去掉重复的 run_id、恢复、结论。窄屏顺序：头部、摘要、Actions、依据、其余 | incident.io、Rootly：头部状态、摘要、时间线、右侧属性栏 |
| 交接页 `incident_handoff.html` | `OpsPilot did not investigate this alert`，后接原因的人话说明，`#handoff-reason` 保留 | 投递显示最近 10 次 | — |
| 复盘 `postmortem.html` | 状态句和计数条；被标记的陈述排在前面；宽屏时决定区吸顶，表单不变 | 各节可折叠；每条陈述引用超过 3 条时折叠；生成尝试、事件、哈希收进 `History and integrity`（`postmortem-attempts` 等表保留原格式） | incident.io、Rootly 的 retrospective |
| 证据 `evidence.html` | 键值：Tool、Query、Window、Status、Used by agent、Fingerprint matches | view 超过 200 行折叠；raw 只显示前 64 KiB，完整内容放在 `?raw=full`（[b]） | Datadog 证据侧栏 |
| 知识条目 `knowledge.html` | 标题用条目名；显示生效修订的正文；`#form-revoke` 折叠 | 修订显示最近 10 个，审计显示最近 20 条，都分页（[b]） | — |
| 知识列表 `/knowledge`（D-L2） | Name、Tags、State、Source incident、Updated，每页 25 条 | — | — |
| 版本变更列表 | Service、Change、Verdict（人话）、Changed、Samples、Incident；id 显示短形式 | 每页 25 条，可按服务和判定筛选（[b]） | PagerDuty Change Events |
| 版本变更详情 | 标题 `<service> v1 → v2`；判定；控制按钮首字母大写，移到第一屏 | 会话、对比、审计、投递各显示最近 10 条；store 读加 `LIMIT 200`（[b]，`observation/releases.py:733-757`） | — |
| 暂停控制 | 全局状态用大字显示；目标表格 | 目标超过 20 个时出现过滤框 | — |

所有页面的措辞规则：人话标签在前，原始值放进 `raw` 折叠或作为行尾的小号 `<code>`，例如 generation、epoch、logical_key、sha256、watermark、conclusion_key、supersede、citable_as_fact、原因码。验收依赖的 id 和原始文字一律保留在 HTML 里，完整清单留待各页预审时复核。

## 5. 第二阶段（只列要点，另行出规格）

- 详情页动作合并：按状态给出 1–2 个主动作。
- 不可逆动作的页内确认，做法是在同一个 `<form>` 里加必勾选框。
- 成功提示：303 的 `Location` 被测试逐字比较，所以改用一次性 cookie 传递。
- 表单出错后重渲染，保留用户输入。
- 防重复提交，加 `no-store`。
- 实时更新不再整页刷新，草稿存入 `sessionStorage`。
- 独立的 Ask 页面，新路由需要改白名单。
- 逐句异议：A 方案是写进退回理由，B 方案是存储人工异议，后者是新能力。
- 合并事故时用候选下拉选择。

## 6. 需用户裁决（第一阶段）：无

依据用户 2026-11-16 的说明：只有改 PRODUCT-CONSTRAINTS、扩大权限、不可逆操作，或者删除、放宽验收断言，才需要停下来问用户。下面两项都是可逆的，由 Agent 按推荐决定，并在任务记录里写明理由。

| 编号 | 决定 | 理由 | 备选及其代价 |
|---|---|---|---|
| D-L1 | Resolved 和 Closed 两个页签与 Needs a human 允许重叠（§3.3） | 已恢复但未关闭的事故确实还在等人处理；这样做不需要改任何验收断言 | 改成互斥（PagerDuty 的做法）：要把 `tests/acceptance/test_f2_alert_correlation.py:588,593` 里的 `GET /` 改成 `GET /?view=resolved`，这算修改验收断言，届时再问用户 |
| D-L2 | 新增 `GET /knowledge` 列表页，顶栏加 Knowledge 入口 | 只读页面，不含 `MUTATION_WORDS`；按测试自身的设计，在 `tests/acceptance/test_f7_step1_entry_points.py:68-91` 登记 `read: knowledge list` 即可，由独立测试作者完成 | 不加：知识页只能从复盘页进入 |

第一阶段不删除也不放宽任何验收断言。§3.6 列出的普通测试，由独立测试作者按新页面改写。U5、U6 被取代，这一点已由产品负责人裁定。

## 7. PR 拆分（第一阶段）

每个 PR 都目标 `main`，不做 stacked PR，最多两条线并行。每个 PR 先做合同预审，测试由未参与实现的 Agent 编写，再做独立审查。

| PR | 内容 | 依赖 |
|---|---|---|
| 1 `feature/ux-list` | 只含列表需要的部分：§3 全部内容；§2 中的 `time`、`.clamp2`；顶栏去掉 `ui_basic`。不依赖任何共享框架 PR，可以单独合并 | — |
| 2 `feature/ux-incident-detail` | §4 的事故详情和交接页；§2 的其余部分：各页 `nav` 高亮、面包屑宏、页面 GET 的 HTML 错误页 | 1 |
| 3 `feature/ux-postmortem-pages` | §4 的复盘、证据、知识（含 D-L2）、版本变更、暂停控制 | 1，可以和 2 并行 |

**PR 1 验收**（全部是用户任务；夹具用内存 store，PG 部分由 CI 的 m0-postgres 跑）：
1. 夹具：2003 个事故，其中 30 个等人处理，最旧的一个排在第 1500 位。打开 `/`：默认页签显示 `Needs a human (30)`，第 2 页能看到那个第 1500 位的事故；各页签计数与夹具一致；HTML 不超过 120 KB；SQL 查询 3 条。
2. 选 Service `shop/checkout`，搜索 `latency`：结果和计数一致；刷新以及复制 URL 到新标签页，看到的内容相同；切到 Closed 页签时筛选条件保留、页码回到 1。
3. 筛选没有结果：显示 `No incidents match these filters.`，点 `Clear filters` 回到全量。
4. 40 行数据，1440 和 400 两种宽度：表格只有 4 列，标题最多两行，悬停能看到全文，Age 显示相对时间、悬停显示绝对时间，页面不横向滚动。
5. 点右上角 `Ask OpsPilot`：表单展开，选目标、填问题、提交，跳到新事故页；表单字段与之前一致。
6. `?view=bogus&page=999`：显示默认页签的最后一页，不报错。
7. 隐藏的原值列：浏览器里看不到 `Run`、`Control`、`Lifecycle`、`Concluded` 这四列，但 `list_row` 助手仍能读到。
8. 新增的 SQL 与 `status_of` 一致性测试通过；`tests/acceptance/` 全部原样通过；D13 测试通过。

## 8. 未确认

- 第 4 节没有逐页核对锚点。上一轮子代理整理过的 id 清单，在 PR 2、3 预审时复核。
- 「参照」列的产品细节，比如 PagerDuty 页签的名称和默认每页行数，没有对照当前官方文档。
- 查询在 PG 上的实际耗时没有实测，是推断。
- `opspilot_incidents.updated_at`（`0001_baseline.py:56`）是否会随状态变化而更新，没有核实，所以 Age 用的是 `created_at`。
- `ld.alert_json` 只是最新一次投递的记录，搜索范围因此不包括更早的投递。
