# M1-01 6a：`otel-demo` profile 的真实冒烟 Run（一次正常事故）

- 日期：2026-09-26（UTC 14:36–14:45；日志时间为本机时区 07:36–07:45）
- 授权：AGENTS.md「费用与真实调用」常设授权；本地 PostgreSQL 55431 由 lead 授权本 worktree 独占。
- 这是冒烟，不是 v4 包的 2+2 验收（6b 另开 PR）；不改 feature `passes`。
- 进程：本 worktree 的 lab PostgreSQL 55431；`python -m opspilot.web serve` 与 `python -m opspilot.worker_main` 都以 `OPSPILOT_TOOL_PROFILE=otel-demo` 启动（worker 日志首行记 `profile=otel-demo`，`tool_schema_revision otel-demo-3936d7ae7edb`）；模型 DeepSeek Flash（key 经 `OPSPILOT_ENV_FILE` 读取）；工具后端是运行中的 OTel Demo 2.0.2 实验环境（colima `m0-otel`，`lab-health.json`：Prometheus 200、Jaeger 17 个服务、frontend 200）。提交经 `curl` 走 `/intake/ui`，事件流经 `curl -N /incidents/{id}/events`。
- 原始材料（本目录）：`ledger.json`（两个事故的 incidents/runs/steps 行、全部 14 条证据行的 view 与 raw 摘要、已发布结论；模型响应里的 `reasoning_content` 已按 C3 §12 置换为固定占位，raw 字节只记 sha256 与长度）；`sse.txt` / `sse-revoked.txt`（事件流原文）；`intake*.json`（HTTP 响应）；`incident.html`（最终页面）；`web.log`、`worker.log`；`lab-health.json`。写盘后以 key 全文与 `Bearer`/`Authorization` 扫描全部文件：0 命中。

## 事故 2（`31f5db80`，干净的一次）：提交 → worker 领取 → 3 轮真实工具 → 报告 → 发布

| 时刻（UTC） | 事件 |
|---|---|
| 14:43:30 | `POST /intake/ui` → 201，`run_id 7581aa19`，观测窗 = 提交时刻往前 300 秒（报告正文引用 `14:38:30Z–14:43:30Z`） |
| 14:43:3x–14:44:11 | `run_claimed`（epoch 1）→ 第 1 轮 4 次工具 → 第 2 轮 4 次 → 第 3 轮 6 次 → 第 4 轮报告 → `run_completed`（`published: true`，`handoff: false`），事件 1–21 见 `sse.txt` |

工具操作（全部 `ok`、`adopted`、`truncated: false`，目标 `m0-otel-20260909`）：

| 工具 | 次数 | 查询 | 行数（返回/保留） | 新鲜度 |
|---|---|---|---|---|
| `traces_search` | 6 | checkout、payment、cart、email、product-catalog、frontend，各 `limit 20` | 20/20 采样 span | 23–50 s |
| `metrics_range_query` | 8 | `increase(traces_span_metrics_calls_total…[5m])` 按 service/span/status；`rpc_client_duration…count` 按 rpc 状态；`histogram_quantile(0.95, …)` 两式；`app_payment_transactions_total`；`http_server_request_duration_seconds_count` | 1–36 series | 5–21 s |

终态（`ledger.json` → `clean.run`）：`state completed`、`epoch 1`、**`tool_operations_used 14`、`tool_seconds_used 0.42`**（`DurableToolLedger` 落库）；结论 `model_requests_used 4`、`model_seconds_used 39.2`、`rounds 4`、`handoff_reasons []`。报告 `m0-report-v2`，`assessment_status completed`，`conclusion partial`：13 条 claim（7 fact、1 hypothesis、2 counter_evidence、2 rejected_hypothesis、1 recommendation），全部引用本 Run 的证据 id；摘要称采样的 checkout 及依赖 span 均为成功状态、错误 series 近零，但没有更早基线所以「elevated latency」既不能确立也不能否定，标为 partial。页面 `incident.html` 显示已解析报告。

## 事故 1（`83316ae6`）：被并发的 PG 用例撤销租约（保留为栅栏证据）

14:37:09 提交 → 领取 → 3 轮 **16 次真实工具操作**全部 ok（`sse-revoked.txt` 事件 1–20）→ 14:38:18 worker 记 `status=control_denied reason=CONTROL_DENIED`，Run `paused`、`lease_until NULL`、`tool_operations_used 16`，无终态事件。原因（`ledger.json` 之外的 `opspilot_suspension_audit`）：14:37:52Z 同一实例上另一进程做了两次全局挂起/解除（全局代际 3→6）——合同测试作者按本人指示重跑了 PG 合同用例。租约栅栏按设计撤销了在途 Run（C3 §4）；这次不算冒烟结果，改为实例独占后重提交事故 2。

## 判定

1. `otel-demo` profile 端到端可用：工作台记录的输入快照（含 300 秒窗）被 worker 读回，14 次真实 Prometheus/Jaeger 查询经执行器登记为证据（raw sha256 / view sha256），模型引用它们发布报告。
2. 两个工具都被模型实际使用，视图都在 `max_view_bytes` 内（无截断）；traces 的 raw 是投影记录（带 Jaeger 原始响应的 sha256 与字节数），metrics 的 raw 是 Prometheus 原始字节。
3. 凭据未出现在任何证据、页面、日志或事件中（后端无认证，`credential_ref otel-demo-ro` 只在审计字段出现）。
4. 未演示：故障案、暂停/挂起下执行器自身的控制拒绝（PG 合同用例覆盖）、工具截断路径、多实例。
5. 费用：1 个干净 Run 4 次请求 + 1 个被撤销 Run 4 次请求 = 8 次模型 HTTP 请求；token/金额未进账本，按供应商余额差对账。
6. 实验环境未停止（colima `m0-otel` 与 26 个容器仍在运行，供 6b 使用）；PostgreSQL 55431 在冒烟后停止，数据保留。
