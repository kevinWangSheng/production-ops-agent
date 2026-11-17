# 工作台 UX 规格（2026-11-16）

状态：草案 r2。第 2、3 节（外壳/导航、事故列表）已定稿，列表已随 PR #688 上线；§3.8 与第 4 节（事故详情页，布局 B）已经过合同预审（2026-11-16，对照 `main` d9af804d 的代码与测试），可以实施。§4.10 其余页面只有要点。

视觉基线沿用 [#572 设计说明](web-ui-design-spec-2026-11-15.md)：黑白灰、系统字体、英文界面、不用多色状态标记。

阶段划分（产品负责人 2026-11-16 裁定）：

| 阶段 | 范围 |
|---|---|
| 第一阶段 | 只改展示：每页显示什么、怎么读、数据多了怎么撑住。现有表单的字段与行为不变，只能挪位置、折叠或合并到同一个表单（详情页的 follow_up 与 correct，§4.2） |
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
| 状态页签 | `Needs a human (30) · In progress (12) · Resolved (3) · Closed (1,958) · All (2,003)`。每个页签是链接 `?tab=…`，当前页签带 `aria-current`。计数覆盖全库，并且受当前筛选影响 | PagerDuty 事故列表的 Open/Triggered/Acknowledged/Resolved/Any 页签 |
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
| `tab` | `needs_human`（默认）、`in_progress`、`resolved`、`closed`、`all` | 按默认处理，不返回 400 |
| `service` | 下拉里的某个值 | 不在下拉值里时忽略 |
| `q` | 去掉首尾空白，最多 200 字 | 超出部分截断 |
| `page` | 正整数 | 超过最后一页时显示最后一页 |

不带参数的 `/` 就是默认视图。连续两次请求必须逐字节相同（D13）。

### 3.5 空、错误与规模

| 场景 | 显示 |
|---|---|
| 库里没有事故 | 保留现有空态 `No incidents yet.`，加一个 `Ask OpsPilot` 入口 |
| 当前页签为空，且没有筛选 | Needs a human：`Nothing needs a human right now.`；其他页签：`No incidents here.` |
| 有筛选但没有结果 | `No incidents match these filters.`，后面接 `Clear filters` 链接到 `/?tab=<当前>` |
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
| `tests/test_572_incident_list_page.py:93-101`（解析器在 `tr.gh` 上记分组名）、`:160-173`（`assert "in progress" in _row_group(...)`）、`:176-189`（`"need" in group and "human" in group`） | 分组布局：页面不再有 `tr.gh` 分组行，Needs a human / In progress / Closed 也不再是同一页上的分组标题，而是页签 | 改为断言事故落在哪个页签：取 `/?tab=in_progress` 和 `/?tab=needs_human` 的行，或者读当前页签的 `aria-current` |
| `tests/test_572_incident_list_page.py:192-238` | `question[:48]` 应出现在 `Incident` 列；`Agent conclusion` 列的文字（`:225-238`） | 问题文字改为断言在 `Title` 列；结论文字改到详情页上断言 |
| `tests/test_572_incident_list_page.py:170,186,212` | 按表头名取 `Run`、`Control`、`Concluded` 的值 | 原值单元格保留在 DOM 里，这几处不用改 |
| `tests/test_572_incident_list_page.py:261`（`len(page.forms) == 1`） | 新增 GET 筛选表单后，页面上有 2 个表单 | 改为按 `action="/intake/ui"` 定位表单；`:263-276` 的字段集合与空幂等键断言不变 |
| `tests/test_572_incident_list_page.py:277,297` | 页面文字含 `Ask OpsPilot to investigate`；`page.forms[0]` 是提问表单 | 按 §3.1 的做法即可通过，可以不改 |
| `tests/test_572_incident_list_page.py:315-323` | 只有一个 `<th>` 行，且在 `<thead>` 里 | 新表头只有一行，原值 `<th>` 在同一行里，不用改 |
| `tests/test_572_incident_list_page.py:244-253` | D13 | 保持不变，并加上带参数的情形 |
| `tests/m1_incident_control_support.py:157-174`（`list_row`），被 `tests/test_m1_incident_control_contract.py:103-109,205,353-374`（8 处）、`tests/integration/test_m1_incident_control_postgres.py:80`、`tests/integration/test_m1_list_run_state_postgres.py:160-173,291`（5 处）使用 | 表头和单元格都还在，`list_row` 本身不受影响。会失效的是夹具：queued 或 running 的事故在 `In progress` 页签，不在默认页签 | 测试改用 `/?tab=all` 查找；夹具行数控制在 25 以内 |

PRD 和 `feature_list.json` 都没有提到列表的列（grep 结果为空）。

### 3.7 数据与查询改动 [b]，无迁移

**接口**：`Workbench.incident_list(tab, service, q, page)` 返回：
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
| Q2 当前页 | `SELECT {_LIST_COLUMNS},{_ITEM_COLUMNS} FROM {_LIST_FROM} {_ITEM_JOINS} WHERE i.incident_id IN (SELECT incident_id FROM base WHERE <tab 谓词> AND <筛选> ORDER BY created_at DESC, incident_id LIMIT 25 OFFSET %s) ORDER BY i.created_at DESC, i.incident_id` |
| Q3 下拉值 | `SELECT DISTINCT service FROM base WHERE service IS NOT NULL ORDER BY 1 LIMIT 200` |

筛选条件：
- `service = %s`
- `q`：先转义 `\`、`%`、`_`，然后用 `ILIKE '%'||%s||'%' ESCAPE '\'` 匹配问题原文、`ld.alert_json`（含 alertname 和 summary；这段文字已脱敏、有长度上限，且只来自最新一次投递）、service；另外用 `i.incident_id::text LIKE %s||'%'` 匹配短 id 前缀。

**分页**：用 OFFSET，不用 keyset。按每页 25 行和数千事故的规模，OFFSET 足够；而且 `?page=N` 可读、可分享。只有事故达到十万级才需要改成 keyset。

**一致性守卫**：新增合同测试，用夹具覆盖全部 13 个状态，逐行断言 SQL 算出的 `grp` 等于 `status_of(item)["group"]`。以后改 D11 必须两处一起改。

**索引**：不需要。现在 `opspilot_incidents` 上只有 `target_id` 索引（`opspilot/migrations/versions/0001_baseline.py:45`）。在 2000 行规模下，三条查询走顺序扫描加 LATERAL 也是毫秒级，这是推断，PG 上未实测。真到十万级，再考虑 `(created_at DESC, incident_id)` 索引和 `pg_trgm`，那时属于 [c]。

**内存 store**：`tests/m1_web_support.py:786-791` 的内存实现要镜像同样的过滤和分页逻辑，Protocol 定义在 `store.py:381`。

**视图**：`incident_list.list_page()` 不再分组，改为输出 `rows`、`tabs`（标签、计数、href、是否当前）、`pager` 和 `filters`。新增 `title_of(item)`，按 §3.2 的取值顺序；为此要在 `_item()` 里多解析一个 `annotations.summary`，与 `_alert_name` 同一处（`store.py:1034-1043`）。

### 3.8 列表增量（布局 B，2026-11-16 负责人选定）[a]

列表的页签、筛选、搜索、分页（PR #688）不变，只改下面两处。

| 项 | 规格 | 测试影响 |
|---|---|---|
| 发起调查 | 标题行右侧按钮文字改为 `New investigation`。仍是 `<details class="askd" id="ask">`，但展开后不再是浮层，而是标题行下方的整行内嵌框：Target 下拉、Question 文本框、`Cancel`（收起 details）与 `Start`（提交）。删掉框里的说明段落和可见的幂等键字段：幂等键沿用 r2-M6 的浏览器端生成，有脚本时字段始终隐藏，无脚本时才显示（否则无法提交）。表单的 `method`、`action`、三个字段名、DOM 中排第一位都不变。不加「默认查最近 1 小时」之类的提示：只有 `metrics_range_query` 有这个默认（`opspilot/tools/otel_demo.py:15-22`），问题类调查没有统一的时间窗 | `tests/test_684_incident_list_query.py:356-369` 原样通过；`tests/test_572_incident_list_page.py:279`（`Ask OpsPilot to investigate`）与 `tests/test_684_incident_list_query.py:328`（空态含 `Ask OpsPilot`）由独立测试作者改为新文字，属普通测试 |
| 状态单元格 | 第一行是人话状态 `status.words`（加原因码、重复触发标记，同现状），第二行小字：有 `todo` 时显示 `<span class="pill todo">…</span>`（CSS 前缀 `Next: ` 不变），没有时显示 `status.detail`。只调换现在两行的上下顺序，不新增数据 | `tests/test_684_incident_list_query.py:557-561` 原样通过 |

不做：样稿里 In progress 行的 `Run 2 · step 5 · 3m` 需要每行再查当前 Run 的步数，列表查询现在没有这一列，留作以后的 [b]；`Updated` 列仍用 `Age`（`updated_at` 是否随状态更新见 §8）。

## 4. 事故详情页（布局 B）

用户在这一页要完成：(1) 看懂 Agent 现在的结论和依据；(2) 看它正在做什么；(3) 用一句话追问、补充或纠正；(4) 暂停、接管、关闭等控制。负责人 2026-11-16 选定样稿 B（`~/.agent/diagrams/opspilot-agent-ux-b-panel-2026-11-16.html`，仓库外）：左侧结构化主栏，右侧吸顶的 Chat 面板。否决的备选是纯对话线程，理由是纯对话展示不了一次完整的调查。说明性段落一律删去。

本节只改展示与表单的摆放；POST 的路由、字段、语义都不变，后端行为不变，不迁移。

### 4.1 页面结构（DOM 顺序即窄屏阅读顺序）

| # | 区块 | 内容 | 数据来源 |
|---|---|---|---|
| 1 | 面包屑与标题 | `Incidents / <短 id>`；`<h1>` 标题取值同列表 `title_of`，限两行；下一行小字 `Alertmanager · <alertname>` 或 `Asked by <actor>`，接 `· <service> · opened <time>` | `view.header`；提问者取自 `intake_accepted` 事件的 `actor_id`，或 intake 账本信封的 `principal`（[b]，§4.3） |
| 2 | 头部动作 | 标题右侧的按钮行，规则见 §4.4 | `available_actions(snapshot)` |
| 3 | 状态行 | 一行：`<status.words> · Run N`，后接恢复观察一段，例如 `Watching recovery · 18 of 40 samples healthy · since 10:14` / `Recovered 07:51 · no handling recorded` / `Recovery confirmed`；没有观察就不显示这段 | 人话状态复用 `incident_list.status_of`（用一个从 snapshot 组装的适配对象，列表和详情页的说法因此一致）；恢复段取 `view.recovery` |
| 4 | `Stored values` | `<details>`，默认收起：现有 `Stored record` 那一行原样放进来（`incident-control`、`incident-lifecycle`、`incident-mode`、`concluded`、`control-generation`、`run-state` 以及 `Run <code>{run_id}</code>`），另加 related/merged/rebind/target request 那几行（`.rel`，原样） | 现状 |
| 5 | 进行中的 Run（仅 `live` 时） | `<h2>` `Run N`，副标题 `Round k · 3m 10s elapsed`（暂停时为 `Paused at round k`）。下面是本 Run 的轮次时间线，见 §4.5 | `snapshot.steps` + SSE |
| 6 | 结论 | `<h2 id="investigation-report">`：没有 Run 在跑时标题是 `Conclusion`，有 Run 在跑时是 `Previous conclusion`，并注明 `stays until Run N publishes`。正文见 §4.6 | `snapshot.report`；Run 在跑时取 `snapshot.history` 里最后一个已完成且可解析的报告 |
| 7 | 依据与盲区 | Key evidence、Hypotheses、Ruled out、Unknown、Could not see、Next steps，见 §4.6 | 报告 + `snapshot.coverage` |
| 8 | 本 Run 的轮次（非 live 时） | `<details class="how" id="how">`，默认收起，摘要为 `Run N · k rounds · m queries · 3 with a problem` | 同 #5 |
| 9 | 人工备注（仅 human_owned） | `Notes (n) · recorded, no Run`：接管之后的 follow_up/correct 文字 | `snapshot.controls` 中代际大于 takeover 那行的记录 |
| 10 | `Investigation history` | `<h2>Investigation history</h2>`，每个历史 Run 一行 `<details>`：`Run k · <run_id 短码> · concluded 09:52 / cancelled / handed off`；被后来的 Run 取代的结论标 `superseded by Run N`。展开后是该 Run 的结论全文与全部陈述（现有 `investigation-history` 的内容，搬进折叠） | `snapshot.history` |
| 11 | 恢复观察 | `<h2>Recovery watch</h2>`，一句结论加样本条带；会话表、样本、读数都收进现有的 `#recovery-verdict` 与 `#recwatch` 折叠，id 不变 | 现状 |
| 12 | 复盘 | 有复盘时显示 `<h2>Postmortem</h2>`，第一行是 `Review postmortem` 链接（待审时）；`#postmortem` 内的 id 与文字原样，放进折叠 | 现状 |
| 13 | `Raw records` | 告警原文（`_alert.html`）、问题原文、标识与完整性、事件日志（非 live 时的 `#progress`）、知识提示 `#knowledge-hints`、`Human control` 小节（见下）。全部折叠 | 现状 |
| 14 | Chat 面板 | `<aside>`，`<h2>Chat</h2>`。DOM 上排在主栏之后，宽屏时 CSS grid 把它放到右侧并吸顶。见 §4.2、§4.3 | §4.2 |

`Human control` 小节放在 #13 里：`<h3>Human control</h3>`，下面是 `Human decisions and system rows` 表（现有 `table.dlog` 原样，行仍以 `<tr>` 开头）。这几处字面文字和 `Investigation history` 都被 F12 验收按标题定位（§4.8），不能改名。

宽度规则：大于等于 1100px 时两栏，主栏可伸缩，Chat 固定 380px，`position: sticky; top: env(safe-area-inset-top)`，高度 `100vh` 减去顶栏；日志在面板内部滚动，输入框固定在面板底部。小于 1100px 时单栏，Chat 排在主栏之后；有脚本时页面顶部出现 `Investigation · Chat` 两个页签（样稿的移动端做法），没有脚本时两部分上下排列。

### 4.2 Chat 面板

日志在上，输入框在下。日志按时间升序，最新的在底部；加载时滚到底，用户往上翻时新内容不把视图拉回底部。

日志条目，全部在服务端由 snapshot 已有的数据组装（`incident_view.chat_log()`，取代现有的 `_timeline()` 与「What happened」区块）：

| 条目 | 样式 | 来源 |
|---|---|---|
| `Alert received from Alertmanager` | 系统行 | 第一次投递的 `received_at`（`_timeline` 现有逻辑） |
| `Alert fired again` / `Alert resolved (recorded only)` | 系统行 | 后续投递、`alert.resolved`。**不得**出现 `re-fired episode` 字样，见 §4.8 |
| 提问 | 人的消息（提问者、时间、原文），标签 `started Run 1` | `snapshot.question` + 提问者 |
| `Run N started` | 系统行 | Run 的 `started_at`（第一步的时间，`opspilot/persistence/runs.py:18`）；当前 Run 取 `steps[0].observed_at` |
| `Run N concluded` | OpsPilot 消息：结论首句（`opening(summary, 220)`）、结论等级、`View` 链接到 #6 或 #10 中该 Run 的锚点 | `history[].concluded_at/report`；当前 Run 取 `run_completed` 事件时间 |
| `Run N handed to a human` / `Run N cancelled` | 系统行 | `run_handoff`（`parked` 不为 false）事件；cancel 的决策行 |
| 人的追问或纠正 | 人的消息：actor、时间、原文（转义；超过 6 行折叠，可展开）；`correct` 带 `Correction` 标签；续开了新 Run 的带 `started Run N` 标签 | `snapshot.controls`（账本，含 text），`run_id` 见 §4.3 |
| 其他控制动作 | 系统行，`<actor> paused` / `took over` / `closed` / `reopened` / `merged into <短 id>` / `retargeted to <uid>` / `registered remediation <revision>` | `decision_log`，措辞沿用 `DONE` |
| `Recovery watch started` / `Recovery confirmed` / 观察结束 | 系统行 | `observation_sessions`、`recovery[].samples` 的 transition（`_timeline` 现有逻辑） |
| `Postmortem vN drafted` | 系统行，链接到复盘页 | `postmortem.versions` |

上限：日志最多渲染最新 200 条，更早的条目折叠成一行 `N earlier entries`（无脚本时是一个指向 `#raw` 的链接）；告警投递只列最近 10 次，其余并入这一行。`decision_log` 本身最多 50 行（`service.py:3424`），超出时在日志顶部注明 `older decisions in Raw records`。

**输入框**：一个 `<form method="post" action="/incidents/{id}/control">`，取代现在分开的 follow_up 和 correct 两个表单。字段：
- `<textarea name="text" maxlength="8192" required>`，占位符 `Ask, add context, or correct the conclusion`（human_owned 时为 `Add a note`）；
- 两个单选 `name="action"`：`value="follow_up"`（`Ask or add context`，默认选中）与 `value="correct"`（`Correct the conclusion`）；
- 隐藏的 `expected_generation`（与其他表单的写法相同，`name` 在前 `value` 在后）；
- 隐藏的 `idempotency_key`，值为 `msg-{generation}-{form_nonce}`，由服务端每次渲染生成，页面上不显示；
- 发送按钮 `Send`。

输入框上方一行状态句，按 §4.4 的表格取值。只有 `follow_up` 在 `available_actions` 里时输入框才可用；不可用时禁用文本框，状态句说明原因，若 `reopen` 可用，旁边放一个 `Reopen` 按钮。这个按钮用 HTML 的 `form` 属性提交头部那个 reopen 表单（`<button form="act-reopen">`），不单独新建表单。

### 4.3 数据与视图改动 [b]，无迁移

| 改动 | 位置 | 理由 |
|---|---|---|
| `controls` 每行带上 `run_id`：取自账本 `control` 结果里已有的 `run_id`（`service.py:979`，续开时非空） | `Workbench._controls` | 用来给消息打 `started Run N` 标签，不必按时间猜 |
| snapshot 加 `question_actor`：取 intake 账本信封的 `principal.actor_id`（`_envelope_from_json` 已经解析出来） | `Workbench.snapshot` | 事件列表只保留最新 50 条，`intake_accepted` 可能已滚出 |
| Run 序号：`history` 加当前 Run，按 `investigation_history` 现有的排序（`started_at NULLS LAST, control_generation, run_id`）从 1 开始编号 | `incident_view` | 页面上的 Run 编号只用于展示；原始 `run_id` 仍在 Stored values 和历史行里 |
| `chat_log()`、`header_actions()`、`composer()` 三个纯函数 | `incident_view` | 取代 `_timeline`、band、`_copy` 的段落文字 |

不新增路由（`tests/acceptance/test_f7_step1_entry_points.py:68-100` 不用改），不新增 store 方法。人工以外的 `event` 输入（`/intake/events` 附加、合并带来的事件）目前不在 snapshot 里；把它们放进日志需要一次新的有界读取，本步不做，见 §5。

### 4.4 头部动作与各状态下的输入框

头部按钮 = `available_actions` ∩ {`resume`, `pause`, `cancel`, `new_run`, `takeover`, `reopen`, `close`}，按 `ORDER` 排列，主按钮最多 4 个。`register_remediation`、`rebind_target`、`merge_into` 需要额外输入，放进按钮行末尾的 `<details>More</details>` 下拉，字段与现在相同（`name="revision"`、`name="to_target"`、`name="into_incident_id"`、可选 `text`）。每个动作仍是一个独立的 `<form>`，带 `id="act-<action>"`；`follow_up` 与 `correct` 合成 §4.2 的一个表单。

页内确认：`cancel`、`close`、`takeover`、`merge_into`、`rebind_target` 点击后，按钮行原地换成一句确认加两个按钮，例如 `Cancel Run 2? Steps so far are kept.` `[Cancel Run]` `[Keep running]`；`Close this incident? Unfinished Runs are cancelled.`；`Take over? OpsPilot will not start again on this incident.`（接管不可撤回，代码里没有写回 `automatic` 的路径）；`Merge into <id>? This cannot be undone.`。没有脚本时直接提交，与现在一样。确认文字由脚本生成，不出现在服务端 HTML 里。

输入框状态句（与 `controls.py` 的实际分支一致，措辞取自现有 `_copy`，`incident_view.py:528-560`）：

| 事故状态 | 状态句 | 实际行为 |
|---|---|---|
| Run queued/running/waiting_human，未过期 | `Sent to Run N. OpsPilot reads it at its next round.` | 同一个 Run 重新排队：持有它的 worker 失去租约，正在进行的那一轮不再提交，Run 被重新领取后在下一轮读到这条输入（`controls.py:708-712`） |
| 同上但 Run 已过 deadline | 同上句，再加 `If the Run has run out of time, a new Run starts instead.` | 续开新 Run（`controls.py:250-262`）；页面不预判 deadline，与 `_copy` 一致 |
| 已暂停（事故或范围挂起） | `Recorded. OpsPilot reads it when the Run is resumed.` | 只记录，保持暂停（`controls.py:592-600, 624-628`） |
| 已有结论或 Run 已取消（open/observing_recovery） | `Starts Run N+1.`；有恢复观察时加 `Ends the current recovery watch.` | 续开：旧结论从事故上清除，观察授权撤销，生命周期回到 open（`controls.py:667-700`）。页面上旧结论作为 `Previous conclusion` 保留 |
| human_owned（含 resolved/closed） | `Recorded as a note. No Run starts.` | 只记录（`controls.py:639-664`）。`available_actions` 对 human_owned 事故总是给出文字动作，所以已关闭的 human_owned 事故也能记备注，照后端行为显示 |
| resolved/closed 且已有结论（automatic） | 输入框禁用：`Resolved. Reopen to send messages.` / `Closed. …`，旁边是 `Reopen` | 后端返回 `REOPEN_REQUIRED`（`controls.py:569-580`），`available_actions` 不给文字动作 |
| 仅交接事故 | 交接页没有输入框 | 不变 |

样稿里的 `Queued until the current step finishes` 与后端行为不符：后端不会等当前这一步做完，而是让这一轮作废、重新领取。所以不用这句。

### 4.5 实时更新

保留现有机制：`EventSource` 订阅 `/incidents/{id}/events`，在 `run_completed`、`run_handoff`、`control_applied`、`reload` 和复盘事件上整页刷新（`incident.html:648-667`）。`#progress` 的 id 与脚本行为不变（D18），非 live 时放在 Raw records 的事件日志里，live 时放在 #5 下方，默认折叠。

新增的三点，都只在浏览器端：
1. **轮次时间线**：live 时 #5 渲染的就是 `<details class="how" id="how" open>`，每轮一项：`R3 · 10:21:04 · 2 of 2 tools`，下面每个工具一行：`<code>tool</code> → status [reason]`，有 evidence 时是一个证据链接。`step_committed` 到来时追加一个新的轮次项，`tool_committed` 到来时在对应 `step_id` 的轮次项下追加工具行，文字都用 `textContent` 写入。末尾一行 `Waiting for the model…` 带计时。时间线只用事件 payload 里已有的字段（`progress.py:276-288, 473-480`）；样稿里「Queried checkout p99 latency」这类描述数据里不存在，不做。
2. **草稿保留**：输入框内容在 `input` 时写入 `sessionStorage["draft:" + incident_id]`，加载时恢复（包括单选值），`submit` 时清除。刷新前不需要额外保存，因为每次输入都已写入。
3. **计时**：`elapsed` 每秒在浏览器端更新。服务端只输出 `<time datetime>`。

日志面板不在浏览器端追加条目：会改变日志的事件都会触发整页刷新。

### 4.6 结论与依据

| 区块 | 内容 | 来源 |
|---|---|---|
| 结论一句话 | `opening(summary, 480)`，超出部分放进 `Full summary` 折叠（现状） | `report.summary` |
| 元信息行 | `<等级> · Run N · 09:52 UTC`，被修订时加 `· revises Run k`。等级用报告自己的值并写成人话：`supported` → `Supported by evidence`，`partial` → `Partly supported`，`inconclusive` → `Inconclusive`；`incomplete` 时加 `Incomplete investigation`。不叫「confidence」，报告里没有置信度字段（`reports.py:177`） | `report.conclusion`、`report.incomplete` |
| `#report-status` | 现有的 Assessment、conclusion 徽章与 sha256 收进元信息行下的小号 `<code>`，id 保留 | 现状 |
| Key evidence | `facts` 与 `counter_evidence`，编号排列，每条是一个 `<details id="claim-<id>">`：摘要是陈述原文和来源标签（`R3 · metrics.range_query`）；展开后有脚本时用 `fetch` 取现有的 `GET /incidents/{id}/evidence/{eid}` JSON（不带 `Accept: text/html` 时返回 JSON，`app.py:1073-1133`），显示 `view.query`（有就显示）、状态和 `view` 的前 4 KiB 或前 60 行，接 `Open full evidence`；没有脚本时只显示这个链接。每条陈述引用多个证据时逐个列出。已有的指标图（`#evidence-charts`）放在列表上方 | 报告陈述 + 证据路由 |
| Hypotheses | 有就显示，标 `Not proven` | 报告 |
| Ruled out | `rejected_hypotheses`，每条右侧小字写引用的证据标签 | 报告 |
| Unknown | `report.unknown`，id `claim-G<n>` 不变 | 报告 |
| Could not see | 由代码统计的盲区：`#coverage-gaps` 小节，先是一行一个盲区源（`not_integrated`、`failed`、`partial_failure`、`not_queried`、`not_authorized`），完整的逐源表格（`tr.coverage-source[data-source][data-state]`）收进折叠；`#not-integrated`、`#coverage-duplicates`、`#collection-truncated` 原样 | `snapshot.coverage`（不是模型写的） |
| Next steps | `next_steps` 与 `recommendations` 合并，标题后小字 `advisory, OpsPilot will not act` | 报告 |

没有结论时，#6 只有一句：`No conclusion yet.`，或者 `Run N was cancelled.` / `Run N handed this incident to a human.` / `Run N stopped when <actor> took over.`。未发布的交接报告以 `#handoff-report` 显示在这一句下方，标 `Unpublished handoff report, not the conclusion`。

### 4.7 规模

| 场景 | 要求 |
|---|---|
| 一个 Run 80 轮、每轮 5 个工具 | `#how` 里完整渲染全部轮次（§4.8 的测试要在其中找到每一行）；默认显示前 3 轮与最后 5 轮，中间折叠为 `Show 72 more rounds`。这里用 CSS 类隐藏，脚本负责展开；不能用嵌套的 `<details>`，因为测试按 `#how` 后的第一个 `</details>` 截取。HTML 不超过 400 KB |
| 20 个 Run、每个都有结论 | Investigation history 每个 Run 一行，折叠；当前结论只出现一次。每个 Run 的结论全文与陈述在折叠内，留在 DOM 里 |
| 200 条人工消息 | 日志显示最新 200 条，更早的折叠为一行；单条最长 8192 字，超过 6 行折叠 |
| 单条证据 1 MB | 页面上只有标签；展开时最多取前 4 KiB 或 60 行，完整内容在证据页 |
| 30 条 Key evidence | 先显示 8 条，其余放在 `Show 22 more` 后面 |
| 400px 宽 | 单栏，不横向滚动；头部按钮换行；Chat 在主栏之后，有脚本时用顶部页签切换 |

### 4.8 测试约束：必须保留的字面与结构

`tests/acceptance/` 和其他按验收步骤写的测试（F3、F12）依赖下面这些。本设计全部保留，因此**不需要放宽任何验收断言**。

| 依赖 | 位置 | 本设计如何满足 |
|---|---|---|
| `id="run-state">{state}<` 与 `<code>{run_id}</code>` | `tests/acceptance/test_393_live.py:175-188` | Stored values 原样 |
| `id="<X>">值<`，X 为 incident-control、incident-lifecycle、incident-mode、control-generation、run-state、concluded | `tests/integration/test_f12_acceptance_postgres.py:97-99` | 同上 |
| `name="action" value="…"` 的集合等于可用动作（`reopen`/`close` 的有无） | 同文件 `:100,635-739` | 每个动作一个表单；composer 的两个单选恰好就是 `follow_up`/`correct`；Reopen 按钮借用 reopen 表单，不重复写 action |
| 第一个 `name="expected_generation" value="(\d+)"` 是当前代际 | `tests/acceptance/test_m1_04_alert_window.py:259` | 所有表单使用同一代际 |
| `Human decisions` 到下一个 `<h2` 之间有 `<tr>` 行，含 `<code>{action}</code>`、`→ {generation}<`、`<code>{actor}</code>` 和时间 | F12 `:383-394,569-571,827`，`test_f12_pr1_acceptance_postgres.py:518` | `table.dlog` 原样放在 Raw records 的 `Human control` 小节 |
| `Human control` 字样存在，到下一个 `<h2` 之间不含未发布的结论 | F12 `:692` | 同上。**注意**：`_section` 取第一次出现的位置，且不区分大小写，所以头部和 Chat 里不能出现 `human control` 字样 |
| `Investigation history` 到下一个 `<h2` 之间有旧 run_id、旧结论首句、旧假设原文 | F12 `:476-477,565-568`，`test_f12_pr1…:251`，`test_f12_followup_impl_postgres.py:82` | #10 的标题就是这几个字；每个历史 Run 的折叠里用 `<code>` 写完整 run_id，结论与全部陈述也在里面，中间不出现 `<h2` |
| `history only` | F12 `:693,854` | 轮次行保留 |
| `<details class="how" id="how">` 到第一个 `</details>` 之间：每个工具行出现一次工具名，含状态与原因码，恰好每个 ok/no_data 证据各一个 `/evidence/` 链接 | `tests/test_f3_pr1_acceptance.py:378-415`，`tests/integration/test_311_source_errors_postgres.py:171-173` | §4.5、§4.7：live 与非 live 共用这一元素；内部不嵌套 `<details>`；Key evidence 里的证据链接放在 `#how` 之外 |
| `#coverage-gaps`、`data-source="…" data-state="…"`、`#collection-truncated`、`#not-integrated`、duplicate 计数 | `tests/acceptance/test_f3_step3_coverage.py:18-31`，`test_f3_pr2_coverage_impl_postgres.py:179-181` | Could not see 区块 |
| `re-fired episode` 恰好出现 1 次；`T2`；`id="related-incident"` 链接 | `tests/acceptance/test_f2_alert_correlation.py:545-556` | 投递表在 `_alert.html` 里，原样；Chat 用 `Alert fired again`；`.rel` 在 Stored values 里 |
| `Merged into`；`<p class="rebind">`、`Rebound from`、`id="current-target"`、`name="to_target"` | `test_245_merge_into.py:365`，`test_245_rebind_target.py:339-344` | `.rel` 原样；`current-target` 放在标题下的小字行；rebind 表单在 More 下拉里（DOM 中存在） |
| 告警 fingerprint、`otel-demo`、`checkout`、`value changed` 出现在页面上；`<script>` 被转义 | `test_m1_04_alert_intake.py:241-263` | Raw records 中的 `_alert.html` |
| `postmortem-status`、`postmortem-generation`、`postmortem-latest`、`consecutive failures N`、`generating now`、`regeneration pending` | `tests/acceptance/test_f13_postmortem.py:271-284` | #12 原样 |
| 版本变更链接 | `test_f11_step1_rollout_trigger.py:228-229` | `.rel` 原样 |
| 第一个 `<h2>` 之前没有 `state`、`queued`、`running` 字样，除 `run-state` 外没有这些徽章 | `tests/test_m1_incident_control_contract.py:89-100`，`tests/m1_incident_control_support.py:134-151` | #1–#4 都在第一个 `<h2>` 之前，状态句用 `Investigating`、`Paused` 这类词；Chat 和输入框状态句排在主栏之后 |

普通测试（不是用户门，由未参与实现的测试作者改写）：
- `tests/test_m1_02_recovery_page.py:142`：`text.count("<form") == len(available_actions(snapshot))`。follow_up 和 correct 合成一个表单后，数量变成 `len(actions) - 1`（两者都可用时）。改为「表单数 = 可用动作数，follow_up 与 correct 算一个」。
- 其他按旧区块文字或顺序断言的普通测试：本次只 grep 了 `What happened`、band、`Show controls` 这些字样，都没有命中；完整清单**未确认**，由测试作者在实现分支上跑全量测试时列出。

### 4.9 无脚本时

服务端渲染完整页面。头部表单直接提交，没有页内确认；输入框的单选可用；Key evidence 只有链接；没有实时更新，需要手动刷新；没有移动端页签，两部分上下排列；列表的幂等键字段在无脚本时可见（r2-M6）。

POST 结果：仍然 303 到 `/incidents/{id}`，`Location` 不加锚点（测试逐字比较 `Location`）。提交后页面加载时，脚本若发现 `sessionStorage` 里有刚刚提交的标记，就把 Chat 日志滚到底并聚焦输入框。被拒绝的 POST（409 `CONTROL_CONFLICT` 等）仍返回现在的 JSON 错误，改进放在 §5。

### 4.10 其余页面（只改展示）

| 页面 | 首屏 | 折叠与上限 | 参照 |
|---|---|---|---|
| 交接页 `incident_handoff.html` | 标题、`OpsPilot did not investigate this alert`，后接原因的人话说明，`#handoff-reason` 保留。两栏外壳与详情页相同，Chat 只有告警条目，没有输入框 | 投递显示最近 10 次 | — |
| 复盘 `postmortem.html` | 状态句和计数条；被标记的陈述排在前面；宽屏时决定区吸顶，表单不变 | 各节可折叠；每条陈述引用超过 3 条时折叠；生成尝试、事件、哈希收进 `History and integrity`（`postmortem-attempts` 等表保留原格式） | incident.io、Rootly 的 retrospective |
| 证据 `evidence.html` | 键值：Tool、Query、Window、Status、Used by agent、Fingerprint matches | view 超过 200 行折叠；raw 只显示前 64 KiB，完整内容放在 `?raw=full`（[b]） | Datadog 证据侧栏 |
| 知识条目 `knowledge.html` | 标题用条目名；显示生效修订的正文；`#form-revoke` 折叠 | 修订显示最近 10 个，审计显示最近 20 条，都分页（[b]） | — |
| 知识列表 `/knowledge`（D-L2） | Name、Tags、State、Source incident、Updated，每页 25 条 | — | — |
| 版本变更列表 | Service、Change、Verdict（人话）、Changed、Samples、Incident；id 显示短形式 | 每页 25 条，可按服务和判定筛选（[b]） | PagerDuty Change Events |
| 版本变更详情 | 标题 `<service> v1 → v2`；判定；控制按钮首字母大写，移到第一屏 | 会话、对比、审计、投递各显示最近 10 条；store 读加 `LIMIT 200`（[b]，`observation/releases.py:733-757`） | — |
| 暂停控制 | 全局状态用大字显示；目标表格 | 目标超过 20 个时出现过滤框 | — |

所有页面的措辞规则：人话标签在前，原始值放进 `raw` 折叠或作为行尾的小号 `<code>`，例如 generation、epoch、logical_key、sha256、watermark、conclusion_key、supersede、citable_as_fact、原因码。验收依赖的 id 和原始文字一律保留在 HTML 里；详情页的清单见 §4.8，其余页面在各自预审时复核。

## 5. 第二阶段（只列要点，另行出规格）

布局 B 已经覆盖了原清单中的三项：按状态给主动作（§4.4）、页内确认（§4.4，用脚本实现）、实时更新时保留草稿（§4.5）。剩下的：

- 被拒绝的 POST 在页内显示：现在 409/400 返回 JSON，浏览器里看到的是原始 JSON。可以改为按 `Accept: text/html` 渲染错误页并保留输入，或者用 `fetch` 提交；两种都要改 POST 的响应合同。
- 成功提示：303 的 `Location` 被测试逐字比较，所以改用一次性 cookie 传递。
- 防重复提交，加 `no-store`。
- 无脚本时的不可逆动作确认：在同一个 `<form>` 里加必勾选框。
- Chat 日志加入 `event` 输入（`/intake/events` 附加、合并带来的事件）：需要给 store 加一次有界的 `opspilot_inputs` 读取。
- 日志里标出 Agent 读到消息的那一轮（`read at round k`）：需要轮次的输入水位，数据是否已有**未确认**。
- 早期 Run 的轮次：`rebuild` 只返回当前 Run 的步骤，历史 Run 的轮次要新增读取。
- 列表 In progress 行显示当前轮次与耗时。
- 独立的 Ask 页面，新路由需要改白名单。
- 逐句异议：A 方案是写进退回理由，B 方案是存储人工异议，后者是新能力。
- 合并事故时用候选下拉选择。

## 6. 决定表

依据用户 2026-11-16 的说明：只有改 PRODUCT-CONSTRAINTS、扩大权限、不可逆操作，或者删除、放宽验收断言，才需要停下来问用户。下表没有一项属于这几类，全部按推荐执行，在任务记录里写明理由。如果实现时发现 §4.8 有哪一项做不到、必须改验收测试，那一项才变成用户门。

| 编号 | 问题 | 决定（推荐） | 理由 | 备选及其代价 |
|---|---|---|---|---|
| D-L1 | Resolved/Closed 页签与 Needs a human 是否重叠 | 重叠（§3.3） | 已恢复但未关闭的事故确实还在等人；不改验收 | 互斥：要改 `test_f2_alert_correlation.py:588,593`，属用户门 |
| D-L2 | 是否加 `GET /knowledge` 列表 | 加，登记白名单 `read: knowledge list` | 只读页面 | 不加：知识页只能从复盘页进入 |
| D-B1 | Chat 日志的数据从哪来 | 服务端由 snapshot 已有的数据组装（§4.2），不迁移、不新建事件表 | 告警投递、Run、人工决策（含文字）、观察会话、复盘版本都已经在 snapshot 里 | 新建统一的事件流：要迁移，而且与现有审计重复 |
| D-B2 | 怎么知道哪条消息开了新 Run | `controls` 行带上账本结果里已有的 `run_id`（§4.3） | 数据已存，只是没有传到视图 | 按时间猜：Run 的 `control_generation` 后续会变，不可靠 |
| D-B3 | 一个输入框里怎么区分 follow_up 和 correct | 两个单选 `name="action"`，默认 follow_up；后端两个动作都保留 | 两种输入在模型那边是不同的 `kind`（`context.py:1323-1327`）；单选不依赖脚本，提交的字段与现在相同 | 只留 follow_up：correct 就无法从页面发出；按内容自动判断：等于替用户做决定 |
| D-B4 | Run 在跑时发消息，状态句怎么写 | 按后端实际：`Sent to Run N. OpsPilot reads it at its next round.`（§4.4） | 后端是让当前这一轮作废、重新领取，不是等这一步做完 | 照抄样稿的 `Queued until the current step finishes`：与事实不符 |
| D-B5 | 已有结论又有恢复观察时，状态句是否提到观察会结束 | 提，`Ends the current recovery watch.` | 续开会撤销观察授权（`controls.py:653-657`），用户应在发送前知道 | 不提：用户会以为只是追问 |
| D-B6 | 已关闭的 human_owned 事故能否写备注 | 能，按 `available_actions` | 后端接受，页面照实显示 | 照样稿禁用：页面与后端不一致 |
| D-B7 | 实时更新与草稿 | 保留 SSE 加整页刷新；草稿写入 `sessionStorage`；轮次时间线在浏览器端追加 | 刷新路径已被测试和使用多次验证过；草稿问题用存储就能解决 | 局部替换 DOM：要重写多处渲染，草稿以外的状态（展开、滚动）更容易出错 |
| D-B8 | POST 之后怎么显示结果 | 303 不变，脚本在加载后滚动到 Chat 底部 | `Location` 被逐字比较；无脚本也能用 | `fetch` 提交：属于第二阶段（§5） |
| D-B9 | 哪些动作需要页内确认 | cancel、close、takeover、merge_into、rebind_target；只用脚本实现 | 这几项会丢弃进度，或者无法撤回 | 全部确认：常用的 Pause/Resume 多一步；无脚本确认：要改表单，属第二阶段 |
| D-B10 | 头部放哪些按钮 | 不需要额外输入的动作放头部（最多 4 个），需要输入的三个放进 More | 符合样稿；字段不变 | 全部平铺：Run 在跑时会有 7 个按钮 |
| D-B11 | Run 怎么编号 | 按 `investigation_history` 的现有排序从 1 开始 | 这个排序已用于历史区块；编号只用于展示 | 加 `created_at` 列：要迁移 |
| D-B12 | 「置信度」 | 用报告的 `conclusion` 等级，写成人话；不叫 confidence | 报告里没有置信度字段 | 造一个置信度：没有数据支撑 |
| D-B13 | Key evidence 展开后的原始结果从哪来 | 用 `fetch` 取现有证据路由的 JSON，截取前 4 KiB 或 60 行 | 不新增路由，页面体积不随证据大小增长 | 服务端内联：每个被引用的证据要多读一次，页面变大 |
| D-B14 | 各类陈述放在哪 | Key evidence = facts + counter_evidence；Hypotheses 单列并标 Not proven；Ruled out = rejected；Next steps = next_steps + recommendations；Could not see 来自 coverage（代码），Unknown 来自报告 | 分清「模型说的」和「代码统计的」 | 照样稿只列 4 组：假设和反证就没有位置 |
| D-B15 | Run 在跑时，上一个结论是否显示 | 显示为 `Previous conclusion`，注明在新结论发布前保留 | 后端已从事故上清除结论，但历史里还在；标成 previous 是如实描述 | 不显示：用户在 Run 跑完前看不到任何结论 |
| D-B16 | 列表的发起调查框是否保留 `<details id="ask">` | 保留元素，只改文字和摆放 | 现有表单测试原样通过，且不依赖脚本 | 改成脚本开关的 div：多改几处测试，无脚本时打不开 |
| D-B17 | 幂等键「从不显示」与 r2-M6「无脚本时可见」 | 详情页的 key 由服务端渲染为隐藏字段；列表沿用 r2-M6 | 详情页本来就是每次渲染一个 nonce；列表要保证两次 GET 逐字节相同（D13） | 列表也由服务端生成：违反 D13 |

## 7. PR 拆分

每个 PR 都目标 `main`，不做 stacked PR，最多两条线并行。每个 PR 先做合同预审，测试由未参与实现的 Agent 编写，再做独立审查。PR #688（列表）已合并；§2 的外壳项大多已在 `main` 上（`crumbs` 宏、各页 `nav`、错误页），剩余项在 PR-D2 预审时对照代码复核。

| PR | 内容 | 依赖 |
|---|---|---|
| D1 `feature/ux-list-b` | §3.8：New investigation 内嵌框、状态单元格两行。只改模板，可以单独合并 | — |
| D2 `feature/ux-incident-b-main` | §4.1 的 #1–#13、§4.3（`run_id`、`question_actor`、Run 序号）、§4.4 的头部按钮、More 与页内确认、§4.5 的轮次时间线、§4.6、§4.7。follow_up 与 correct 两个表单此时原样放在右侧占位栏里，所以 `<form` 数量测试还不用改 | — |
| D3 `feature/ux-incident-b-chat` | §4.2 的 Chat 面板与 `chat_log()`、§4.4 的输入框状态句、§4.5 的草稿保留、§4.9；去掉「What happened」和占位栏；改写 `test_m1_02_recovery_page.py:142` | D2 合并后开工 |
| D4 `feature/ux-postmortem-pages` | §4.10 的复盘、证据、知识（含 D-L2）、版本变更、暂停控制；交接页的外壳 | 可以和 D2/D3 并行 |

建议顺序：D1 与 D2 并行；D2 合并后做 D3，同时做 D4。

**D2 + D3 验收**（用户任务；夹具用内存 store，PG 部分由 CI 的 m0-postgres 跑）：
1. 一个 Run 正在跑：页面顶部是标题、Pause/Take over/Cancel、状态行；Run 区块随 `step_committed`/`tool_committed` 增加轮次，不整页刷新；`run_completed` 时整页刷新，结论出现，正在输入的草稿还在。
2. 已有结论的事故：输入框提示 `Starts Run 2.`；发一条消息后，页面回到同一个 URL，Chat 底部出现这条消息，标签为 `started Run 2`；主栏显示 `Previous conclusion`，并注明 Run 2 发布前保留。
3. 选 `Correct the conclusion` 再发送：审计里这一行的 action 是 `correct`。
4. human_owned：状态句是 `Recorded as a note. No Run starts.`；发送后 Notes 区块多一条，没有新 Run。
5. resolved 且已有结论：输入框禁用，点 `Reopen` 后事故回到 open，输入框可用。
6. Cancel 和 Close 弹出页内确认；选 `Keep running` 什么也不提交；关闭脚本后直接提交。
7. 夹具：80 轮 × 5 个工具、20 个已结论的 Run、200 条消息、一个 1 MB 的证据：HTML 不超过 400 KB；`#how` 默认显示 8 轮；Investigation history 有 19 行；展开证据只取前 4 KiB。
8. 400px 与 1440px 两种宽度：不横向滚动；窄屏有 `Investigation · Chat` 页签；宽屏时 Chat 吸顶，日志在面板内滚动。
9. §4.8 列出的测试全部原样通过；`tests/acceptance/` 原样通过。

**D1 验收**：点 `New investigation`，内嵌框展开，选目标、填问题、点 Start，跳到新事故页；Cancel 收起框；页面上看不到幂等键；状态单元格第一行是状态，第二行是 `Next: …` 或说明。

## 8. 未确认

- §4.8 只覆盖了 grep 到的依赖。其他按旧区块写的普通测试，要在实现分支上跑全量测试才能列全。
- 样稿是在浏览器外读的脚本源码（`renderInv`、`renderChat`、`actionsFor`），没有截图核对。
- 「参照」列的产品细节，比如 PagerDuty 页签的名称和默认每页行数，没有对照当前官方文档。
- 查询在 PG 上的实际耗时没有实测，是推断。
- `opspilot_incidents.updated_at`（`0001_baseline.py:56`）是否会随状态变化而更新，没有核实，所以 Age 用的是 `created_at`。
- `ld.alert_json` 只是最新一次投递的记录，搜索范围因此不包括更早的投递。
- 证据的 `view` 里是否都有 `query` 字段：`otel_demo` 的部分工具有（`otel_demo.py:1606,2045`），其余工具没有逐个核对；没有时展开区只显示状态和 view。
- 被 follow_up 打断的那一轮里，已经提交的工具结果是否作为证据保留（`loop.py` 的 `_RoundAborted` 路径），没有逐行核对；§4.4 只写了「这一轮不再提交」。
