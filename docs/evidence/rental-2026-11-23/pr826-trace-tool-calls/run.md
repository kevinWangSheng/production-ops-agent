# PR #826（#744）trace 的 tool_calls 只计数据源工具：真实 Run

代码同 [pr827](../pr827-k8s-empty-list/run.md)，即合并分支 `47419bca`；web 与 worker 都设了 `OPSPILOT_TODO_WRITE=1`。

- 问题：分 4 步对 otel-demo 做健康巡检，并要求维护任务清单（`q3-audit.txt`）。
- Run `f65b6206-94fa-5412-a71c-a1bdfdb3b08b`：completed，17 次模型请求；prompt 1,086,338（命中 1,007,360），completion 25,697。trace：https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/d9043906-3b89-466d-bd20-a0ceca5d77cf/r/00000000-0000-0000-f958-e510f0d94e15。
- 根 span `opspilot.run` 的 metadata（用 `cp1_trace.py` 读回）：`opspilot.run.tool_calls` 34，`opspilot.run.task_list_calls` 3，`opspilot.run.model_calls` 17。工具 span 有 `tool:TodoWrite` 3 个，数据源工具 span 共 34 个（kubernetes_jq_query 16、fetch_pod_logs 11、execute_prometheus_instant_query 3、kubernetes_tabular_query 2、traces_list_services 1、get_label_values 1）。
- summary.json 的 `run.tool_requests` 也是 34 条，按工具分的计数与 trace 一致，不含 TodoWrite。
- 判定：通过。tool_calls 等于 34，等于 summary 的查询数；任务清单调用只计入 task_list_calls。
