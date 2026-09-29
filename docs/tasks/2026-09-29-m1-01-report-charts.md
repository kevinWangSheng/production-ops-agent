# M1-01 后续项 2：报告证据图表

- 状态：进行中（实现完成，待独立审查；合同测试 `tests/test_m1_report_charts_contract.py` 为独立作者所写）
- 更新日期：2026-09-29
- 依据：[收口记录「后续项」第 2 条](2026-09-28-m1-01-closure.md)；[PRODUCT-CONSTRAINTS](../../PRODUCT-CONSTRAINTS.md)「Evidence and context requirements」（报告链接须解析到已捕获的证据，模型文字不是权威）与「Data flow contract」；[C3](../design/technical-proposal-2026-09-07.md) §2（页面为 Jinja、少量 JavaScript、SSE）
- 工作区：`feature/m1-01-report-charts`，worktree `../production-ops-agent-report-charts`（基线 `main` `f0038dd`）

## 目标与范围

事故页面的报告目前只有文字与证据链接。目标：被报告引用的 metrics 证据，在事故页面上按 `evidence_id` 渲染成时间序列图，让人不用点进 JSON 就能看到曲线形状。

范围内：事故页面（`incident.html`）服务端内联 SVG；数据只读自已落库并通过哈希校验的证据视图。范围外：trace 图、发布观察页、模型侧任何改动（提示词、报告 schema、工具描述）、新依赖、新路由、`feature_list.json` 与 `passes`（本项不在 PRD 验收步骤内）。

## 上游对照（HolmesGPT，固定 `5e983c17`，与 [源码审计](../research/holmes-source-audit-2026-09-07.md) 同版本）

事实（已读源码，路径均在上游仓库）：

- **模型侧嵌入标记**：`holmes/plugins/toolsets/prometheus/prometheus_instructions.jinja2:20-21` 要求模型在回答里最多嵌 2 张图，写成 `<< {"type": "promql", "tool_name": "execute_prometheus_range_query", "tool_call_id": "…"} >>`，并说明「工具不给你数据，事后处理会重跑查询并渲染图」；`:35` 要求截断时不得据截断数据下结论，宁可给图让用户自己看。range 工具描述 `prometheus.py:1760`（"Generates a graph and Execute a PromQL range query"），`output_type` 参数（Plain/Bytes/Percentage/CPUUsage，`:863`、`:1797-1803`）只影响图的取值解释。`tool_calls_return_data`（`prometheus.py:165`，默认 true）为 false 时只回摘要不回数据。
- **图的数据来自工具结果，不来自模型**：结果 `data` 是 Prometheus 原始 `{resultType, result:[{metric, values:[[ts,"v"],…]}]}`。
- **开源参考渲染器**：`experimental/ag-ui/server-agui.py:223-240` 在 Prometheus 工具结果事件上调用前端工具 `graph_timeseries_data`，`:334-339` 只对 range/instant 两个工具生效，`:342-` `_parse_timeseries_data` 把结果整理成 `{title, query, data, metadata}`；前端 `experimental/ag-ui/front-end/src/components/GraphVisualization.tsx`（`GraphData` 类型 `:32-48`，每个 series 映射为 Chart.js 折线 `:126-142`，标签取自 metric 标签），`ChatAssistant.tsx:37,200,917` 接线。`server.py:598` 的 "Graphs" 追问提示词让模型嵌 `<< >>`。
- 上游**没有**任何 trace/Jaeger 图表机制（Tempo 等工具为文本；`grep` 固定版本仅 Prometheus 有此路径，未逐一读完所有 toolset，此条为**未完全确认**）。

看不到的部分（**未确认**）：`<< {"type":"promql",…} >>` 标记由谁解析并渲染——`holmes/utils/tags.py:54` 的 `<<…>>` 解析只处理**用户输入**里的标签，不处理回答；ag-ui 前端也不解析该标记（`grep "<<"` 无命中）。推断为闭源 Robusta UI 按 `tool_call_id` 找到工具结果并重跑/渲染，源码不在上游仓库。

对照结论：上游是「模型选图（标记）+ 工具结果里的原始数据 + 客户端 Chart.js 渲染」；本方案保留「数据来自工具结果」与「每系列一条折线、标签取自 metric 标签」，其余按本项目约束调整（见决定）。

## 本项目现状（已读代码）

- 事故页 `GET /incidents/{incident_id}`（`web/app.py:266`）渲染 `incident.html`；报告经 `service._report_view`/`categorize_report`（`web/service.py:1125-1176`）拆成 facts/hypotheses/counter_evidence/rejected_hypotheses/recommendations，每条 claim 带 `evidence_ids`，模板已为每个 id 输出 `/incidents/{id}/evidence/{eid}` 链接（`incident.html:71-75`）。`m0-report-v2` 校验器保证已发布报告的引用解析到本事故的 `ok` 视图（[report-contract](2026-09-27-m1-01-report-contract.md)）。
- 证据读回：`Workbench.evidence_for(incident_id, evidence_id)`（`service.py:776`）只返回本事故 Run 的证据；`StoredEvidence`（`web/evidence.py:26`）含 `raw`、`view`、`hashes_verified`。
- metrics 视图：`evidence.view["content"]` 是 `data.result` 的行列表（`otel_demo.py:489` `result_path`），每行 `{"metric": {label: value}, "values": [[unix_ts, "value_string"], …]}`；视图另有 `tool`（`metrics_range_query`）、`status`、`adopted`、`query.expr`、`query.step_seconds`、`window.{start,end}`、`truncated`、`omitted_rows`（`executor.py:1386-1449`、`1816-1830`）。视图超 100 KiB 时整行（整条 series）被丢，点不会被单独截断。窗口长度选择器的聚合查询只有 1 个点。夹具工具 `tools/fixture.py` 的 content 不是 `values` 形状（`{"metric": str, "value": float}`），必须容错。
- 约束核对：无新数据出口（页面渲染的是同一 Run 已落库的视图，读权限与现有证据页相同）；无模型输入变化；只读；CSP 仅 `frame-ancestors`（`app.py:52`），内联 SVG 无需放宽。

## 方案

事故页在「Report」段之后新增「Evidence charts」段：按页面上 claim 出现顺序（Facts → Hypotheses → Counter-evidence → Rejected → Recommendations）收集被引用的 `evidence_id`，去重，凡对应证据是 `metrics_range_query` 的，服务端渲染一张内联 SVG 折线图。不加 JS、不加依赖、不加路由。

## 合同（供写验收与合同测试）

### 可观察行为

以 `GET /incidents/{id}`（Basic 认证，同现有页面）为准，事故已有可解析报告（已发布结论，或「未发布交接报告」，二者规则相同）：

1. 页面含 `<section id="evidence-charts">`。被报告任一 claim 引用、`tool == "metrics_range_query"`、`status == "ok"`、`adopted` 为真、`hashes_verified` 为真、且能解析出至少一条可绘 series 的证据，各有一个 `<figure class="evidence-chart" data-evidence-id="<evidence_id>">`，内含一个内联 `<svg role="img">`（含 `<title>`）。同一 `evidence_id` 被多条 claim 引用只出一张。
2. 每条可绘 series 一个 `<g class="series" data-series-label="<标签>" data-point-count="<n>">`：标签由 metric 标签拼成 `k="v"` 列表（无标签时为 `{}`），`n` 为实际绘出的点数。一个连续段用一个 `<polyline>`，孤立单点用一个 `<circle>`，因此窗口长度选择器的单点聚合仍可见，且图注显示该点数值。
3. `<svg>` 带 `data-x-start`、`data-x-end`（= 视图 `window.start/end`）与 `data-y-min`、`data-y-max`（坐标轴范围，绘出的每个值都落在其中，两者不等）；轴上可见起止时间（UTC）与 y 最小/最大值文字。时间戳落在窗口外的点不绘制，并计入图注的「out of window」提示。
4. `<figcaption>` 含：`evidence_id`（链接到 `/incidents/{id}/evidence/{eid}`）、`query.expr`、窗口、`step_seconds`（有则显示）、引用它的 claim 类别（facts、hypotheses…）。
5. 图上没有的东西必须如实标出，不得画成 0 或线性插值：
   - 相邻点时间差大于 `1.5 × query.step_seconds` 时折线断开（缺点）；`step_seconds` 缺失则不判断断点。
   - 值为 `NaN`/`±Inf`/非数字串的点不绘制，图注写明跳过个数。
   - 视图 `truncated` 为真时图注写明「series truncated, omitted_rows=N」；因图上 series 数超上限而未绘制的，写明未绘个数。
   - 某条 series `values` 为空或全部点被跳过：该 series 不出现在图中（无 `g.series`），图注写「N series without plottable points」。
6. 无法出图时不出空图，出 `<p class="chart-unavailable" data-evidence-id="…" data-reason="…">`，`data-reason` 取：`not_ok`（status 非 ok 或未 adopted）、`no_series`（无任何可绘 series，含 content 为空/为 null）、`unrecognized_shape`（content 行不符合 `{metric: 对象, values: [[数, 字符串], …]}`，如夹具形状）、`hash_mismatch`（`hashes_verified` 为假）。这些情形都不得让页面出错（状态码仍 200，其余报告内容照常渲染）。
7. 被引用但不是 `metrics_range_query` 的证据（如 `traces_search`）、`evidence_for` 返回 `None` 的 id（不属于本事故或不存在）：不出图也不出占位（链接行为不变）。
8. 上限：最多 6 张图，超出部分不绘，`#evidence-charts` 内写「N more cited metrics evidence not charted」；每图最多 10 条 series，超出写在图注。
9. 无报告、报告不可解析（`report-unparseable`）、或无被引用的 metrics 证据：页面不含任何 `figure.evidence-chart` 或 `chart-unavailable`；现有元素 id 与证据链接不变。

### 合同裁定（测试作者发现的含糊，lead 2026-09-29 确认）

- 证据链接 `/incidents/{evidence.subject_id}/evidence/{eid}`：已核实 `subject_id` 即事故 id（`tools/fixture.py:250`、`otel_demo.py:1495` 取 `lease.incident_id`）。id 按 URL 转义，冒号保留。
- 断线只看「已画出的点」是否相邻：夹在中间的 NaN 或窗外点使线断开；间隔恰好 1.5×step 不断。
- 窗口边界为闭区间。
- 10 条 series 上限只计可画的 series；6 图上限不计 `chart-unavailable` 占位。
- 值越大越靠上；全部值非负时 y 轴含 0，否则取实际最小值；全常数时上界补 1，保证 `data-y-min < data-y-max`。
- series 标签为 `k="v", …`（无标签为 `{}`），超 80 字符截断。

### 输入与安全边界

- 数据源仅为 `Workbench.evidence_for(...)` 返回的 `StoredEvidence.view`（已落库投影，与模型看到的同一份）。不读 `raw`、不读报告文字里的数字、不发起任何 Prometheus 请求。
- 模型不产生图数据：报告 schema、L2 提示词、工具描述、`PROJECTION_REVISION` 与各 revision **均不变**。
- metric 标签值、`query.expr` 是不可信证据：输出必须 HTML/SVG 转义，标签文本截断到 80 字符；页面不得出现 `<script>`、外部 URL、`href`/`xlink:href` 指向外域、事件处理属性。
- 只读：不新增 POST、不改变任何持久状态；渲染不得让 `snapshot` 变成写路径。

### 公开接口

- HTTP：`GET /incidents/{incident_id}`（上述 DOM 合同）；`GET /incidents/{incident_id}/evidence/{evidence_id}` 不变。
- Python：新增 `opspilot.web.charts.render_evidence_chart(evidence: StoredEvidence, cited_by: Sequence[str]) -> str | None`，返回上述 `<figure>` 或 `<p class="chart-unavailable">` 的 HTML 片段；非 `metrics_range_query` 返回 `None`。`cited_by` 为 claim 类别名。纯函数、无 I/O，可直接以 `StoredEvidence` 构造输入做合同测试。测试可用 `MemoryEvidenceStore.register/commit` 与已发布报告经 `build_workbench` 走 HTTP。

### 不做

trace/span 图；发布观察页；交互（缩放、悬停、JS）；模型选图/嵌入标记；`output_type` 单位换算（Bytes/Percentage/CPU）；对 series 做聚合、降采样、平滑或推断；从 `raw` 补全被截断的 series；PNG 导出；新依赖、新路由、新持久化。

## 决定与理由（执行者一行理由）

- **按引用推导，不加嵌入标记**（偏离上游）：上游靠模型在回答里写 `<< >>` 选图；本项目报告是 `m0-report-v2` 结构化 JSON，加标记要改报告 schema 与 L2 提示词并 bump revision、重跑真实 Run，且再次让模型输出决定「页面显示什么」。按 claim 引用推导让「图 = 报告实际引用的证据」，与 PRODUCT-CONSTRAINTS「报告链接解析到实际证据」一致，且不触碰调查 loop。
- **服务端内联 SVG，不用 Chart.js**：C3 §2 指定 Jinja/少量 JS；上游 ag-ui 用 Chart.js 属实验前端。无新依赖与 CDN，CSP 不变。
- **图数据取视图不取 raw**：视图是模型所见且已哈希固定的投影，体量有界（100 KiB）；`raw` 可达 1 MiB。
- 断点阈值 `1.5 × step`、6 图/10 系列上限是可逆的实现细节，写入合同以便测试；改动按合同变更处理。

## 需要用户决定的点

无必须项（不涉及新依赖、合同层变更或不可逆项）。请知悉并可推翻的偏离：(1) 上游用模型嵌入标记选图，本方案改为按引用推导——如希望模型选图，需另开合同变更（报告 schema + L2 revision + 真实 Run 证据）；(2) 「不出 trace 图」。本 PR 属功能 PR，按 AGENTS.md 走用户门，不预授权合并。

## 完成条件与下一步

- 完成条件：合同测试（全新上下文 Agent 依本节写出，先红后绿）；`make check`；一次有界真实 Run 或对已录真实证据（如 `docs/evidence/m1-01-window-points-rerun/*/ledger.json` 的 metrics 视图）渲染出的页面截图/HTML 作为可观察证据；独立审查处置；PR 至 `main`，CI 与机器人分诊一次，**不合并**。
- 下一步：lead 派测试 Agent 按合同写测试 → 通知实现。实现落点预计：新增 `opspilot/web/charts.py`，`service.snapshot` 收集被引用证据，`incident.html` 加一段。
- 未验证：Robusta UI 侧标记解析（闭源，未确认）；真实 Run 的 metrics 视图在页面上的观感（实现后以真实证据渲染核对）。

## 实现与证据（第二阶段）

- 实现：`opspilot/web/charts.py`（纯函数 `render_evidence_chart`、`evidence_chart`）、`service.py` `_charts`（按 claim 引用收集、6 图上限）、`incident.html` 新增 `#evidence-charts` 段。
- 合同测试：72 用例全绿。其中页面顺序用例原断言 7 张图，与 6 图上限冲突，已报 lead，由测试作者在 `c48e1d5` 改为 6 个证据；实现未因此改动。
- 真实证据离线渲染：用 [alignment-c 故障 Run 的 ledger](../evidence/m1-01-alignment-c-effect/) 中记录的报告与 metrics 视图，经 `docs/evidence/m1-01-report-charts/render_offline.py` 渲染事故页（`pc-fault.html` 5 图、`fault-2.html` 6 图、0 占位），截图 `pc-fault-charts.png`。ledger 只存 raw 的 SHA-256 不存字节，脚本先按 `view_sha256` 校验视图再渲染；页头的事故元数据来自内存测试夹具，不是那次 Run 的真实元数据。截图显示：单点聚合以圆点显示并在图注给数值，31 个 series 只画 10 条并写明，图注如实写「2 non-finite point(s) skipped」。
- 未验证：`make check` 见提交说明；未新跑真实调查（本项不改调查 loop）。
