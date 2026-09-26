# M1-01 6a：`otel-demo` profile 的真实冒烟 Run（一次正常事故）

- 日期：2026-09-26（UTC 14:36–14:52；日志时间为本机时区 07:36–07:52）
- 授权：AGENTS.md「费用与真实调用」常设授权；本地 PostgreSQL 55431 由 lead 授权本 worktree 独占。
- 这是冒烟，不是 v4 包的 2+2 验收（6b 另开 PR）；不改 feature `passes`。
- 进程：本 worktree 的 lab PostgreSQL 55431；`python -m opspilot.web serve` 与 `python -m opspilot.worker_main` 都以 `OPSPILOT_TOOL_PROFILE=otel-demo` 启动（worker 日志首行 `profile=otel-demo`，`tool_schema_revision otel-demo-3936d7ae7edb`）；模型 DeepSeek Flash（key 经 `OPSPILOT_ENV_FILE` 读取）；工具后端是运行中的 OTel Demo 2.0.2 实验环境（colima `m0-otel`，`lab-health.json`：Prometheus 200、Jaeger 17 个服务、frontend 200）。提交经 `curl` 走 `/intake/ui`，事件流经 `curl -N /incidents/{id}/events`。
- **权威的一次是 Run 3**（`1f6636d4`，代码 `dd6e85e`，55431 上没有其他进程）。Run 1 被并发 PG 用例撤销、Run 2 跑在审计修复之前的代码上（两者只作历史保留，见文末）。
- 原始材料（本目录）：`ledger.json`（三个事故的 incidents/runs/steps/events/tool_charges 行、全部证据行的 view 与 raw 摘要、已发布结论；模型响应里的 `reasoning_content` 按 C3 §12 置换为固定占位，raw 字节只记 sha256 与长度）；`sse.txt`（Run 3 事件流原文）、`sse-run2.txt`、`sse-revoked.txt`；`intake.json`、`intake-run2.json`；`incident.html`（Run 3 最终页面）；`web.log`、`worker.log`（Run 3）与 `*-run1-run2.log`；`lab-health.json`；`pg-contract-output.txt`。写盘后以 key 全文、`Bearer`/`Authorization`、演示口令扫描全部文件：0 命中。

## Run 3（`1f6636d4`，权威）：提交 → worker 领取 → 3 轮真实工具 → 报告 → 发布

| 时刻（UTC） | 事件 |
|---|---|
| 14:50:34 | `POST /intake/ui` → 201，`run_id 19f7f6a5`，观测窗 = 提交时刻往前 300 秒（报告正文引用 `14:45:34Z–14:50:34Z`） |
| 14:50:3x–14:51:27 | `run_claimed`（epoch 1）→ 3 轮工具（16 次）→ 第 4 轮报告 → `run_completed`（`published: true`，`handoff: false`），事件 1–23 见 `sse.txt` |

工具操作（全部 `ok`、`adopted`，目标 `m0-otel-20260909`）：

| 工具 | 次数 | 查询 | 行数（返回/保留） | 新鲜度 |
|---|---|---|---|---|
| `traces_search` | 7 | checkout、payment、product-catalog、cart、shipping、email、currency（`limit` 10–20） | 20/20 采样 span | 8–169 s |
| `metrics_range_query` | 9 | `traces_span_metrics_calls_total` 按 service/span/status（rate 与 increase）；`rpc_client_duration…count` 按 gRPC 状态；`histogram_quantile(0.95, …)` 三式；`http_server_request_duration_seconds_count` | 1–103 series | 3–13 s |

其中第一条 metrics 查询返回 103 个 series、视图保留 81 个、`truncated: true`——`max_view_bytes`（24 KiB）截断路径在真实数据上触发；其余 15 次未截断。

终态（`ledger.json` → `run3_authoritative.run`）：`state completed`、`epoch 1`、**`tool_operations_used 16`、`tool_seconds_used 0.54`**，`opspilot_tool_charges` 16 行（`DurableToolLedger` 落库）。结论：`model_requests_used 4`、`model_seconds_used 50.8`、`rounds 4`、`handoff_reasons []`。报告 `m0-report-v2`，`assessment_status completed`，`conclusion partial`：14 条 claim（9 fact、2 hypothesis、1 counter_evidence、1 rejected_hypothesis、1 recommendation），引用的 15 个证据 id 全部属于本 Run（事后核对 ⊆ `evidence_ids`）；16 条证据行 `view_sha256` 事后重算一致、全部 `committed`。摘要：窗口内 checkout 及 payment/product-catalog/cart/currency/shipping/email 的采样 span 全部无错误状态，唯一非零 ERROR series 属 recommendation 的 flagd EventStream（不在依赖集内）；checkout 的 ERROR series 未返回，故错误率记为 unknown 而非零；没有更早基线，「elevated」不能确立也不能否定 → partial。页面 `incident.html` 显示已解析报告。

## 判定

1. `otel-demo` profile 端到端可用：工作台记录的输入快照（含 300 秒窗）被 worker 读回，16 次真实 Prometheus/Jaeger 查询经执行器登记为证据（raw sha256 / view sha256），模型引用它们发布报告。
2. 两个工具都被模型实际使用；视图截断路径真实触发一次；traces 的 raw 是投影记录（带 Jaeger 原始响应的 sha256 与字节数），metrics 的 raw 是 Prometheus 原始字节。
3. 凭据未出现在任何证据、页面、日志或事件中（后端无认证，`credential_ref otel-demo-ro` 只在审计字段出现）。
4. 未演示：故障案、暂停/挂起下执行器自身的控制拒绝（PG 合同用例覆盖，`pg-contract-output.txt` 6 passed）、派发前拒绝的审计路径（`tests/test_m1_tool_refusal_audit.py`）、多实例。
5. 费用：三个 Run 各 4 次模型 HTTP 请求，共 12 次；token/金额未进账本，按供应商余额差对账。
6. 实验环境未停止（colima `m0-otel` 与 26 个容器仍在运行，供 6b 使用）；PostgreSQL 55431 在 Run 3 之后停止，数据保留。

## 历史：Run 1（`83316ae6`）与 Run 2（`31f5db80`）

- Run 1：14:37:09 提交，3 轮 16 次真实工具操作全部 ok（`sse-revoked.txt`）后 14:38:18 被 `CONTROL_DENIED`，Run `paused`、`lease_until NULL`。原因（`ledger.json.suspension_audit_global`）：14:37:52Z 合同测试作者按本人指示在同一实例重跑 PG 合同用例，其两次全局挂起/解除（全局代际 3→6）撤销了在途租约——租约栅栏按设计生效，但冒烟被污染。
- Run 2：14:43:30 提交，实例已独占，4 次请求、14 次工具操作、40 秒发布 `partial`（`sse-run2.txt`）；跑在派发前拒绝审计修复（`dd6e85e`）之前的代码上，且该 Run 没有走到拒绝分支。为让证据与最终代码一致，另跑 Run 3 作为权威。
