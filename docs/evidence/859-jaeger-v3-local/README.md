# #859 本地 Jaeger 2.23 证据（v3 查询接口）

- 日期：2026-11-25（PT 凌晨），执行者：#859 实现 Agent。
- 对象：`jaegertracing/jaeger:2.23.0` 单容器、内存存储，本机 colima；合成 OTLP span（`send_synthetic_spans.py`），不含任何场景数据。
- 性质：本地集成运行，不是生产证明，也不是 ITBench 端到端。调查 loop、报告校验、恢复路径未改，所以不附 LangSmith trace。

## 未确认点的实测结论

| 预审未确认点 | 实测 |
|---|---|
| v1 搜索是否 404 | `/api/services`、`/api/operations`、`/api/traces?service=` 都是 404，体是纯文本 `404 page not found`；`/api/traces/{id}` 仍 200 |
| 配置 base path 时 | `base_path: /jaeger` 时 `/jaeger/api/services` 404、`/jaeger/api/v3/services` 200；**base 写错时非 API 路径（如 `/jaeger/api/services` 落在未配 base path 的实例上）返回 200 HTML 页面**，所以探测要求 200 的体是 v1 envelope 才判 v1 |
| id 编码 | `traceId`/`spanId` 是小写 hex；短 id（`abc123`）按 id 查询可用（服务端左补零） |
| 时间戳 | `startTimeUnixNano`/`endTimeUnixNano`/`timeUnixNano` 是十进制字符串 |
| status.code | 整数（ERROR = `2`，OK = `1`），未设置时 `status` 是 `{}` |
| int 属性值 | `intValue` 是字符串（`"500"`）；`query.attributes={"http.status_code":"500"}` 在内存存储上能匹配到 int 属性（ClickHouse 未测） |
| 空结果与错误体 | 搜索无结果和按 id 查不到都是 404 `{"error":{"httpCode":404,"message":"No traces found"}}`；参数错误 400，`error.message` 带 Go 解析错误 |
| 结果分组 | 多 trace 结果按 (trace, resource) 拆成多个 `resourceSpans`，需要按 traceId 重新分组 |

## 经产品执行器的工具结果（`run_trace_tools.py`，`tool_results.json`）

`otel-demo-ext` profile，`select_profile` 构造的 worker 执行器，真实 HTTP 到 `http://127.0.0.1:16686/jaeger`，`PROJECTION_REVISION=m1-01-tool-view-v20`：

| 工具 | 状态 | 计数 |
|---|---|---|
| traces_list_services | ok | 2 个服务，1 条 v3 note |
| traces_list_operations(cart) | ok | 2 个操作 |
| traces_search(cart) | ok | 1 trace / 6 span，4 个错误 span，6 个错误详情字段，4 个 CHILD_OF 引用 |
| traces_search_by_tags(cart, tag + min_duration) | ok | 同上 |
| traces_search_by_tags(不存在的 tag) | no_data | 0（v3 404 当空结果，不是 error） |
| traces_query_trace_by_id(存在) | ok | 6 span，trace_found true |
| traces_query_trace_by_id(不存在) | no_data | trace_found false，1 条 v3 note |

6 span 是因为合成脚本对同一 trace 发了两次（span id 相同）。7 条存证记录的 `query.jaeger_api` 都是 `v3`。容器用完已按名字停止。
