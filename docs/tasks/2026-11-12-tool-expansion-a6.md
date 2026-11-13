# 工具面拓展 A6：按列查询集群对象

- 状态：实施中（代码与实现者单元测试已提交；等独立合同测试合入后对齐，未开 PR）
- 更新日期：2026-11-13
- 依据：[工具面拓展任务记录](2026-11-12-tool-expansion.md)、[决定表 r3](2026-11-12-tool-expansion-contract.md)（r1 的 R16，r2 的 A2-1 至 A2-15，r3 的 A6-1 至 A6-6）、C3 第 8 节；上游 HolmesGPT `a0009195` 的 `kubernetes.yaml` 里 `kubernetes_tabular_query`。issue #558。开工前预审在仓库外（A3、A6 预审，2026-11-12），没有需要用户裁决的项。
- 工作区：worktree `/Users/shenghuikevin/dev/AI/poa-a6-impl`，分支 `feature/tool-expansion-a6-tabular-query`（起点 `origin/main` `b717e1ef`）；探针骨架 `6469127c`。

## 目标

调查能用「列名:字段路径」的写法，一次把某类集群对象列成一张表（例如某 namespace 的 Pod 名、状态、镜像三列），不用写 jq。工具只进扩展档位 `otel-demo-ext`，`otel-demo` 档位不变。

## 行为（公开接口）

- `kubernetes_tabular_query(kind, columns, namespace?, label_selector?, field_selector?, filter_pattern?)`：列出某类资源，每个对象按 `columns` 抽出一行，返回表头与各行单元格。
- 取数与 A2 完全一致：同一张资源表、`namespace` 不传就是整个集群、问 Secret 直接拒绝、事件只留授权时间框内的、结果超过读取上限整条拒绝。
- 列写法支持上游文档与例子用到的子集：字段名、数组下标、`[*]` 展开（多个值用逗号拼接）。缺字段显示 `<none>`。不支持过滤表达式、递归、切片、带引号的键；带点的 label 键查不了，描述里指向 `kubernetes_jq_query`。写错的列报参数错误并回显出错的那一列。
- `filter_pattern` 是区分大小写的正则（同上游 `grep -E`），匹配整行文本；病态正则在请求期限内被硬停。
- 不设条数上限；表太大超过视图 token 上限时整条拒绝，证据存下供回读。
- 对象是查询那一刻的状态，描述写明不能证明事故发生时的状态；去掉上游描述里「只传请求的字段」这句。

列语法的确切范围、返回形态与计数以决定表 A6-1 至 A6-6 为准。

## 这个 PR 做什么

- 工具登记（五段模型可见描述），列解析与求值器（在 Python 里求值，不起 jq）；取数复用 A2 的「取一类资源的全部对象（已筛事件）」那一步，正则复用 A3 的子进程。
- 合同测试对着替身 API 响应写，由未参与实现的测试作者按本记录、决定表与 C3 编写，不看实现代码：缺字段、`[*]`、下标越界、集群级资源带 namespace、事件越框、Secret 拒绝、超读取上限、非法列。

## 不做

kubectl、服务端表格打印、完整 JSONPath、带点 label 键、评测回放。

## 完成条件

1. 代码 PR：合同测试与 CI 通过，`otel-demo` 钉值不变。这是功能 PR。
2. #558 的完成标志「列出某 namespace 的 Pod 名、状态、镜像三列」用替身响应在合同测试里就能证明；阶段表原计划写的是无真实 Run。A6 不碰调查循环与报告校验，按 ADR-0006 不强制附 trace。lead 定：代码 PR 凭合同测试合并；代码 PR 合并后 #558 关闭；租机时在真实集群上取样调用一次、核对输出形态，归租机集中取证（#580），不要求单独的真实 Run。

## 没有真集群时的已知风险

1. 不带 namespace 的全集群 Pod 列表是否常碰 4 MiB 读取上限（按列查询的典型用法正是不带 namespace，碰到概率比 jq 工具高，未确认）。
2. 其余同 [A2 开工说明](2026-11-12-tool-expansion-a2.md) 的风险清单。

## 实施记录

新模块 `opspilot/tools/kubernetes_tabular.py`，由 `KubernetesTransport` 按工具名分派（与 A3 同法），扩展档位的 schema、注册、`data_sources`、routes 与 `kubernetes_core._registrations` 都加了这一项；`otel-demo` 字节不变。`INVALID_COLUMNS` 是适配器固定码，外层原因归 `INVALID_PARAMS`（同 A3 的 `POD_NOT_FOUND`），不进共享原因词表。

自行决定的可逆细节：

- 取数直接调 A2 的 `KubernetesClient.list_objects`；给它加了一个默认 `"jq ran"` 的 `consumer` 参数，只改事件备注的措辞（A6 写「before the columns were read」），A2 输出不变。
- 列写法另接受可选的开头点（`metadata.name`），与上游打印器的宽松写法一致；列前后空白去掉。下标只收非负整数，负下标、切片、过滤、带引号的键、`..` 一律 `INVALID_COLUMNS`。列名按 `,` 切、每列按 `:` 切且必须恰好两段（同上游），所以带 `:` 的切片报的是「不是 HEADER:PATH」。
- 单元格：字符串原样，其他值用规范 JSON（`true`、`3`、`{"app":"x"}`；上游打印 Go 的 map 写法，模型更好读 JSON）；`null` 与路径没走到都算 `<none>`；多个值用逗号拼。
- `filter_pattern` 匹配单元格用三个空格拼成的行（上游表格列间至少三个空格），不含表头；没有行时不起子进程，`filter_mode` 为空；编不过的模式沿用 A3 子进程的退化子串匹配并写进 notes（上游 `grep -E` 会报错得零行）。
- 返回体 `{header, rows, count, scope, notes}`；由于执行器只把 `result_path`（`rows`）当 content 给模型，表头、计数、范围、备注同时放进视图字段，另加 `total_items_processed`、`rows_removed_by_filter`、`filter_mode`、`pages_read`、`cannot_prove`。`scope` 在 A2 的键上加了 `label_selector`、`field_selector`（上游 scope 的键）。
- 事件的来源时间取过滤后保留行对应事件的首尾（A6-6 的「保留事件」按最终返回的行算，更窄）。
- 错误码表：A2 的表去掉 jq 专用码，加 `INVALID_COLUMNS`、`INVALID_FILTER`（都归参数错误）、`FILTER_FAILED`（来源错误，同 A3）。校验顺序 kind（Secret 先）→ namespace → 选择器 → 列 → 正则。

偏离：开工说明要求回显出错列；执行器对未发出的请求不向模型传详情（`executor.py` `_source_error`），与 A2/A4 一致，回显只在拒绝记录（拒绝体的 `column`、`problem`；单列写错回显那一列，超列数或超长回显整串，超 4000 字符截断并标 `column_truncated`），模型只见 `INVALID_PARAMS`（lead 2026-11-13 裁决不改执行器）。

行只在视图 `content` 里放一份，不在视图字段里重复 `rows`（lead 2026-11-13 裁决）。

## 依赖 A2 与 A3 的假设

A6 假设 A2 把资源表、分页、读取上限、事件时间筛选与错误映射做成可复用函数；若 A2 写成两个工具专用的一体实现，A6 要先抽函数。正则子进程与超时结果类来自 A3（A3 照抄 A2 的 jq 子进程）。`view_fields` 新键名要在 A2 落地后对照通用视图字段再核一次。全量探针已跑（探针骨架 `6469127c`，草稿 PR #599，已关闭）：全量 CI 0 failed / 12226 passed / 2550 skipped，只加扩展档位。

## 下一步

A3 合并后由实现会话接手：开 worktree，派测试作者与实现者，按 AGENTS.md 的功能 PR 流程走。实现顺序排在最后（A2 → A5 → A4 → A3 → A6）。
