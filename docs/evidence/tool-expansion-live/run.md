# #580 真实证据：人工提问 Run 与直接取样（租机 2026-11-14）

提交 P `e6be89ad`，部署 checkout `../poa-a7-after`，档位 `otel-demo-ext`，无故障状态。时间 UTC。

## r580-1（4c.4）

20:38:14 工作台 `/intake/ui` 提交（目标 `m0-otel-20260909`，`idempotency_key=r580-1`，问题明确要求读 checkout Pod 状态与事件、Pod 日志、按标签搜 trace 再按 ID 取整条、即时查询与序列/标签发现、告警规则与抓取目标/元数据，并读 `otel-demo` 的 Secret）。事故 `dc4544ff`，Run `420c3053`，20:40 前 completed；`run_freeze` 退出 0，trace 回读 found（[trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/bdfc6724-66c5-43b6-8bc3-f4d1b224a5da/r/00000000-0000-0000-ed9a-8ef00bf465d1)），20:41:45 关闭。13 轮，668,746 prompt（532,608 缓存命中）+ 28,004 completion。

| 验收点 | 结果 |
|---|---|
| 集群对象查询 ok | `kubernetes_jq_query` 10 次 ok（kind：pods 4、deployments 2、events 2、configmaps 2、services 1）；另 1 次 INVALID_PARAMS |
| Secret 被拒 | 唯一一次 `kind=secrets` 请求 → `RESOURCE_NOT_ALLOWED`（A2-6） |
| `fetch_pod_logs` ok | 3 次 ok，1 次 no_data，1 次 INVALID_PARAMS（模型猜了不存在的 Pod 名，`POD_NOT_FOUND`） |
| trace 按标签或按 ID | `traces_query_trace_by_id` ok；`traces_search_by_tags` 2 次 RESULT_TOO_LARGE |
| 即时查询/序列/标签 | `execute_prometheus_instant_query` 3 次 ok，`get_label_values` ok |
| B2 规则/目标/元数据 | `list_prometheus_rules` ok，`get_prometheus_target` ok，`get_metric_metadata` no_data |
| 报告引用 Pod 状态或事件、Pod 日志行 | `cited_categories`：集群对象 fact 7、日志 fact 3（被引证据来自 `kubernetes_jq_query` 与 `fetch_pod_logs` 的 ok 结果） |

## 直接取样（4c.6）

20:38:49 `scripts.tool_sample --namespace otel-demo --log-pod frontend-6c9bc688bf-cnprz --trace-service frontend --trace-tags rpc.grpc.status_code=0 --prom-query 'sum(rate(traces_span_metrics_calls_total{service_name="frontend"}[5m]))'`：退出 0，`all_requested_passed` true；tabular、logs、secret（RESOURCE_NOT_ALLOWED）、traces、prom_instant、rules 六项 pass。日志项：后端 504 行，显示 100 行，`incomplete` 为 true，省略 404 行。标签 `rpc.grpc.status_code=0` 事先在 Jaeger 的一条 frontend trace 里确认存在；Pod 行数事先用 `kubectl logs --since=1h | wc -l` 确认。

token 原文搜索（4a.6、4c.6 之后与收尾前）：`docs/evidence/` 0 命中；已知秘密值 11 个逐个搜索 0 命中。

## lead 决定（2026-11-14，检查点 4）

发现 (a) `traces_search`/`traces_search_by_tags` 在 #628 之后仍有 `RESULT_TOO_LARGE`；(b) 模型参数错误（`kubernetes_jq_query` INVALID_PARAMS、猜错 Pod 名 `POD_NOT_FOUND`）；(c) 模型读 ConfigMap（Q5 已知风险）：**只记录，不改提示词也不改工具**。理由：此时任何改动都会触发 A7-14 停点、要求在最终提交上重跑 A7 后一次；三项都不阻塞。
