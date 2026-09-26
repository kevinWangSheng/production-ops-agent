# M1-01 3b：网页提交 → 常驻 worker → 真实 runner，两个事故的有界真实 Run

- 日期：2026-09-26（worker 日志为本机时区 01:40–01:44）
- 授权：AGENTS.md「费用与真实调用」常设授权。
- 进程：本 worktree 的 lab PostgreSQL 55431；`python -m opspilot.web serve`（真实 uvicorn，`OPSPILOT_UI_USERS` 一个演示账号）；`python -m opspilot.worker_main`（`OPSPILOT_ENV_FILE` 读 key，`OPSPILOT_WORKER_POLL_SECONDS=1`）。提交与追问经 `curl` 走真实 HTTP（`/intake/ui`、`/incidents/{id}/control`），事件流经 `curl -N /incidents/{id}/events`。模型 DeepSeek Flash，工具为产品 fixture profile（canned Prometheus 视图，无真实 OTel）。
- 原始材料（本目录）：`ledger.json`（两事故的行、步骤、`run_usage`、全部事件、已发布报告正文）；`sse-a.txt`、`sse-b-1.txt`、`sse-b-2.txt`（页面事件流原文）；`worker.log`、`web*.log`；`incident-a.html`、`incident-b.html`（最终页面）；`intake-*.json`、`control-b.json`（HTTP 响应）。写盘后用 key 全文比对全部文件，无凭据；`owner` 只记 set/None。
- 费用：3 个 Run、7 次模型请求（`run_usage.model_requests_used` 3 + 2 + 2）；token 与金额未进账本（worker 不带 RecordingClient），按供应商余额差对账。

## 事故 A：正常发布

| 时刻 | 事件 |
|---|---|
| 01:40:0x | `POST /intake/ui` → 201，`run_id 5084378c`，行带调查输入（`input_recorded: true`） |
| 01:40:0x–34 | worker 轮询领到：`run_claimed` → 工具轮 ×2（`step_committed`/`tool_committed`，`metrics_range_query` 对 `checkout-prod`，adopted）→ 报告轮 → 发布，`run_completed`（事件 1–8，见 `sse-a.txt`） |

终态：Run `completed`、结论已发布、`model_requests_used 3`、**`tool_operations_used 2`**（DurableToolLedger 落库；#47 记录里同类 Run 为 0）。页面 `incident-a.html` 显示已解析报告。

## 事故 B：8 秒 wall 超时 → worker 清扫 → HTTP 追问 → 续开的新 Run 被 worker 领取并发布

web 以 `OPSPILOT_RUN_SECONDS=8` 重启后提交（默认 wall 下无法在有界时间内演示超时）。

| 时刻 | 事件 |
|---|---|
| 01:41:2x | `POST /intake/ui` → 201，`run_id d0b7b6d7`，deadline = now + 8s |
| 01:41:2x–35 | worker 领到，1 轮真实工具轮落库（事件 2–4）；第 2 次请求返回时已过 deadline，提交被栅栏拒绝 → `control_denied`（worker 日志 01:41:35） |
| 01:41:36 | 下一次轮询先清扫：行停放 `waiting_human`，事件 5 `run_handoff`（`DEADLINE_EXCEEDED`，`parked: true`） |
| 01:42:xx | web 以默认 wall 重启；`POST /incidents/{id}/control follow_up` → 200，代际 0→1，事件 6 `control_applied` 带新 `run_id 02de1518`；旧 Run `cancelled`，新 Run `queued`，输入为 C3 续接（`input_recorded: true`，问题含 `Continuation of investigation Run d0b7b6d7`） |
| 01:42:xx–43 | worker 领到新 Run：`run_claimed` → 工具轮（2 次 tool_committed）→ 报告 → 发布，`run_completed`（事件 7–12，见 `sse-b-2.txt`） |

终态：旧 Run `cancelled`（`model_requests_used 2`、`tool_operations_used 1`），新 Run `completed`、结论已发布（`model_requests_used 2`、`tool_operations_used 2`），代际 1。事件流里没有 `run_claim_refused`。

## worker 停止

`kill -TERM` 后日志：`signal=15: stop claiming, finishing the in-flight attempt` → `worker stopped`，进程退出（当时无在跑的尝试，3 ms）。

## 判定

1. 网页提交的事故被常驻 worker 真实调查到发布：工作台写入的输入快照被 runner 接受（`INPUT_MISSING` 缺口关闭），页面事件流完整显示 #44 事件。
2. 工具次数经 DurableToolLedger 持久（三个 Run 均非 0）。
3. 超时 Run 由 worker 的轮询清扫收尾；HTTP 追问经工作台续开新 Run（续接输入由工作台构造，不再依赖脚本），worker 领取并发布。
4. 未演示项：多实例并发与被杀 worker 的回放只在 PG 集成用例中证明（`tests/integration/test_m1_web_worker_postgres.py`），本次真实 Run 未做。
5. 观察（既有行为，非本 PR 引入）：发布后 `opspilot_incidents.state` 仍为 `queued`/`running`，`conclusion` 与 Run 行才是终态权威；worker 与页面均按 `conclusion` 判断。
6. 干扰记录：01:43–01:45 另一 worktree 的 PG 用例跑在同一 55431 实例上，本 worker 曾对其中 3 个事故 `LEASE_ACTIVE` 拒绝、对 1 个版本不符的 Run 记 `blocked`（`worker.log`）；这是 worker 的正确行为，与本次两事故无关。
7. 本轮不是产品验收、不改 feature `passes`。
