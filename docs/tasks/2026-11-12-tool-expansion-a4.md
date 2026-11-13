# 工具面拓展 A4：trace 按标签过滤、按 ID 取整条、列服务与操作

- 状态：实施中（实现已在本地分支，等独立合同测试合入与 CI）
- 更新日期：2026-11-12
- 依据：[工具面拓展任务记录](2026-11-12-tool-expansion.md)、[决定表 r3](2026-11-12-tool-expansion-contract.md)（r1 的 R15，r3 的 N3 结论与 A4-1 至 A4-12）、C3 第 8 节；上游 HolmesGPT `a0009195` 的 `grafana/toolset_grafana_tempo.py`；Jaeger `v1.53.0` 源码。issue #556。开工前预审在仓库外（A4、A5、B2 预审，2026-11-12），A4 没有需要用户裁决的项。
- 工作区：worktree `/Users/shenghuikevin/dev/AI/poa-a4-impl`，分支 `feature/tool-expansion-a4-trace-tags`（起点 `origin/main` `989940a8`）。

## 目标

调查能按标签和时长筛 trace、按 ID 取回一整条 trace、查 Jaeger 里有哪些服务和操作。四个新工具只进扩展档位 `otel-demo-ext`；现有 `traces_search` 与 `otel-demo` 档位不变。lab 的 Jaeger 不用改（匿名、直连查询端口）。

## 行为（公开接口）

- `traces_search_by_tags(service, tags?, operation?, min_duration?, max_duration?, limit?, start?, end?)`：在某个服务里按标签、操作名、时长筛 trace。必须给服务名（Jaeger 不支持不带服务搜索）。标签写成 `key="value" key2=value2`，写错在发请求前就拒绝。所有条件要落在**同一个 span** 上；时长比的是该服务某一个 span 的时长，不是整条 trace 的时长，描述写明。默认 20 条，返回满了就标不完整；超出时给的是最新的那些。
- `traces_query_trace_by_id(trace_id, start?, end?)`：按 ID 取回整条 trace 的全部 span，按时间排好，不抽样。不给时间时用整个授权时间框；框外的 span 丢掉并显示丢了几条。Jaeger 里没有这条时返回空结果，并说明「内存里只存最近 5000 条，查不到不能证明它不存在」。整条太大超过视图上限时整条拒绝，证据存下供回读。
- `traces_list_services()`、`traces_list_operations(service, span_kind?)`：列出 Jaeger 本次启动以来见过的服务、某服务的操作。清单没有时间范围；描述写明不在列表不说明不存在，Jaeger 重启会清空。
- 服务名不再限定为 otel-demo 写死的那组，空结果时描述提示先核对服务名（可用 `traces_list_services` 查）。
- 不提供标签名发现：lab 的 Jaeger 1.53.0 没有这个接口。

参数校验、投影、错误分类与模块位置以决定表 A4-1 至 A4-12 为准。

## 这个 PR 做什么

- 新模块 `opspilot/tools/jaeger_query.py`：四个工具的 schema、注册、传输；复用 `otel_demo.py` 里已有的时间窗、拒绝与 trace 投影函数，不改它们，所以不动 `otel-demo` 字节。扩展档位的 Jaeger 可用工具加这四个名字。
- 合同测试对着按 Jaeger 1.53.0 响应结构写的替身响应，由未参与实现的测试作者按本记录、决定表与 C3 编写，不看实现代码。

## 不做

TraceQL、Tempo 的 trace 指标工具、标签名发现、放宽 trace 投影白名单、Jaeger `/api/v3`、评测回放。

## 完成条件

1. 代码 PR：合同测试与 CI 通过，`otel-demo` 钉值不变。这是功能 PR。
2. 代码 PR 合并后 #556 关闭。真实证据归租机集中取证（#580）：租机后的真实 Run 里出现按标签搜索或按 ID 取整条的证据，报告引用；仓库里有 `summary.json` 与 LangSmith trace 链接。

## 没有真实实例时的已知风险

N3 的结论来自读 Jaeger v1.53.0 源码，下列各项未在真实实例取样，可能返工：

1. 标签值怎么变成字符串（布尔、整数），OTel 状态码类标签在 lab 里是哪种类型；模型写 `http.status_code="500"` 可能因为 lab 用的是新键名 `http.response.status_code` 或类型不同而查空。
2. 一条 checkout 整条 trace 的 span 数与投影后的 token 数；若常超 25,000 token，按 ID 取整条会常被整条拒绝，要回来定分页或精简行。
3. 404 响应体的形态、响应里 `errors` 字段会不会出现。
4. 5000 条内存在 lab 负载下能覆盖多久；不够时按 ID 查早期 trace 会落到查不到的路径。

## 实施记录

探针骨架提交 `a035c1da`；定向测试中只有 A2 合同测试「扩展档位工具集恰好等于 otel-demo 加两个 Kubernetes 工具」失败，与 A2-2「A3–A6 也进这个档位」冲突，已报 lead 裁定，未改该断言。

自行决定的可逆细节：

- 传输是 `otel_demo.OtelDemoTransport` 的子类 `JaegerQueryTransport`，只换分派、复用其 GET（无代理、不跟重定向、期限、限读）；`RoutedTransport(routes=...)` 按精确名路由。新模块不导入网络库、不开子进程。
- 四个 schema 插在 otel-demo 工具之后、Kubernetes 工具之前（A2 实现测试钉了「最后两个是 Kubernetes」）。
- 预检原因码（都归 `INVALID_PARAMS`）：`INVALID_SERVICE`、`INVALID_TAGS`、`INVALID_OPERATION`、`INVALID_DURATION`、`INVALID_LIMIT`、`INVALID_TRACE_ID`、`INVALID_SPAN_KIND`、`QUERY_OUT_OF_WINDOW`。`INVALID_TAGS` 按决定表；其余命名为本实现所定。
- logfmt：键为不含空白、`=`、`"`、`\` 的串；值为带 `\"`、`\\` 转义的双引号串或非空无引号串；重复键、控制字符（含制表）、超 2000 字符拒绝；空白串视为不带标签。发给 Jaeger 时用 `tags` JSON。
- 时长：Go 单位 ns/us/µs/ms/s/m/h，必须为正，换算成整数纳秒以 `NNNns` 发送，保证校验的就是发出的。
- 超出 limit：按每条 trace 最早 span 的开始时间取最新 N 条；视图 `backend_traces_returned` 记原始条数，记录里加 `backend_traces_dropped_over_limit`。
- 按 ID：只保留完全落在查询窗内（且有开始时间和时长）的 span，其余计入 `spans_outside_window` 与 `spans_omitted`；ID 转小写；不发时间参数。注册的 `max_result_bytes` 取注册表上限 2 MiB（读仍是 4 MiB），使超视图上限的整条能走「留证据供回读」路径。
- 404（按 ID）是成功的空视图，执行器照常给 `no_data`/`NO_DATA`（与 `traces_search` 空结果一致），视图带 `trace_found: false` 和 5000 条内存说明。其余工具的 404 记 `INVALID_PARAMS`。
- 400 只回显 Jaeger 错误包 `errors[].msg`（由执行器脱敏截 1024 字符）；响应体 `errors` 非空时标不完整并计数，不回显其文字。
- 服务名清单去重排序、脱敏、截 200 字符；操作清单按（名、span kind）去重排序，span kind 只认 Jaeger 五种，`span_kind` 参数同样只认这五种。

已知限制：按 ID 投影记录超过 2 MiB 时整条 `RESULT_TOO_LARGE` 且不留证据；上文「没有真实实例时的已知风险」四项均未取样。

## 依赖 A2 的假设

扩展档位与按工具名分派的传输路由由 A2 带出；评测回放只认 `otel-demo` 工具名，A4 不加回放分支。全量探针未跑：A2 合并后开草稿 PR 经 CI 补跑，预计只加扩展档位、0 失败。

## 下一步

A5 合并后由实现会话接手：开 worktree，派测试作者与实现者，按 AGENTS.md 的功能 PR 流程走。
