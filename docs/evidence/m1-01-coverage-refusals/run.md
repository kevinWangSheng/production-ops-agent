# M1-01 coverage refusals：OTel Demo 有界真实 Run

- 日期：2026-10-01 UTC 10:24–10:26（本机 PDT 03:24–03:26）。
- profile：`otel-demo`；目标：`m0-otel-20260909`；场景沿用 view-tokens-effect 的宽查询，要求 24 小时、15 秒步长、每服务 limit≥50 的 trace 搜索。
- 环境：共享 lab 锁已取得；OTel Demo health 通过；本 worktree PostgreSQL 55431 已启动。模型凭据由 `M0_ENV_FILE` 读取，未打印或写入证据。

Run 完成：1 个 Run，8 次模型请求，19 次工具调用，16 个已提交 evidence，报告 `completed/partial`。ledger 与报告见同目录 `ledger.json`、`report.json`，事件导出见 `events.jsonl`。

观察到 3 次 `traces_search (RESULT_TOO_LARGE)`，均为 error、没有交付视图。最终报告 gaps 明确点名了 frontend 24 小时、frontend 30 分钟和 currency 24 小时的拒绝及其覆盖影响。更正（lead，依独立审查）：本 Run 8 次请求的 `context.final` 均为 false，模型自行停止，覆盖摘要未发送；gaps 中的点名来自工具结果里的拒绝说明，不是摘要。该 Run 因此没有验证本项改动，而是成为「覆盖摘要在自行停止的 Run 中不生效」的证据。

真实 Run 结束后已停止 web/worker 与 OTel/PG lab；实验数据未删除。gitleaks：lead 已扫描，无发现。
