### 直接调工具取样（#580）

- 取样时刻：2026-11-14T20:38:50+00:00；窗口：2026-11-14T19:38:50+00:00 -> 2026-11-14T20:38:50+00:00
- 档位：`otel-demo-ext`，`tool_schema_revision=otel-demo-ext-d467c4b436b0`
- 记账操作数：6；凭据原文命中：0
- 主判定 all_requested_passed：True（允许跳过：无）；lab 闸门 check_lab_target 全部通过

| 项 | 工具 | status / reason | 判定 | 关键观察 |
|---|---|---|---|---|
| tabular | `kubernetes_tabular_query` | ok / None | pass | status == ok: "ok"; view has header/content(rows)/count/scope/notes: ["header", "content", "count", "scope", "notes"]; rows is a list of lists: "list"; count == len(rows): {"count": 22, "rows": 22}; header == requested column headers: {"header": ["NAME", "STATUS", "NODE", "IMAGE"], "requested": ["NAME", "STATUS", "NODE", "IMAGE"]}; every row has one cell per column: [4]; scope is an object and notes a list: {"scope": "dict", "notes": "list"} |
| logs | `fetch_pod_logs` | ok / None | pass | status == ok: "ok"; incomplete is True: true; lines_omitted > 0: 404; lines_shown == len(rows) <= limit: {"lines_shown": 100, "rows": 100}; matching lines > shown lines: {"backend_lines_returned": 504, "lines_shown": 100}; lines_omitted == backend_lines_returned - lines_shown: {"lines_omitted": 404, "backend_lines_returned": 504, "lines_shown": 100} |
| secret | `kubernetes_jq_query` | error / RESOURCE_NOT_ALLOWED | pass | reason == RESOURCE_NOT_ALLOWED: {"status": "error", "reason": "RESOURCE_NOT_ALLOWED"}; source_contact == none: "none"; operation.dispatched is False: false; reserved and every reservation settled at 0 s: [{"settled": true, "seconds": 0.0}]; no evidence recorded: null |
| traces | `traces_search_by_tags` | ok / None | pass | status == ok: "ok"; result_count >= 1: 66; source_contact == confirmed: "confirmed"; evidence adopted: true; request carried the tag condition: {"requested": "rpc.grpc.status_code=0", "recorded": "rpc.grpc.status_code=0"} |
| prom_instant | `execute_prometheus_instant_query` | ok / None | pass | status == ok: "ok"; result_count >= 1: 1; source_contact == confirmed: "confirmed"; evidence adopted: true |
| rules | `list_prometheus_rules` | ok / None | pass | status == ok: "ok"; result_count >= 1: 2; source_contact == confirmed: "confirmed"; evidence adopted: true |
