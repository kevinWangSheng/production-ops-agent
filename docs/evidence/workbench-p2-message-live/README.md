# 工作台重设计 P2 消息输入与接力：有界真实 Run（2026-11-17）

真实 DeepSeek Run（`deepseek-flash`），驱动 `scripts/m1_live_runner.py --message --reply-only-message`，全程走产品路径：事故经 workbench 的 `/intake/events` 进入，消息经 `POST /incidents/{id}/control action=message` 发出，web 进程的接力在同一请求里开续接 Run，`InvestigationRunner` 领取；关闭也走同一路由。PostgreSQL 17 为本 worktree 的一次性实例（端口 55444，schema head `0025_web_session_rebind`），工具是脚本自带的 fixture（固定返回 `http_errors_rate=0.042`）。合同：[任务记录](../../tasks/2026-11-17-workbench-redesign.md)（A1–A11、B1–B8、D 组、R1–R5、r8）。

## 重跑（主证据）

- [summary.json](dc3efd1b-3d65-5964-8a16-21a1ecba6924/summary.json)；[LangSmith trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/98c56dd8-ac1f-4247-8db1-8be275d09273/r/00000000-0000-0000-217a-8fd9119d7aad?poll=true)（项目 `opspilot-lab-workbench-p2`，根 run 已读回，`dropped_spans=0`）。事故 `72b54f8e-3cfe-5be2-b9ac-1c0a2103b595`。
- 首个 Run：发布结论（D3 的前提）。
- 消息「你好，这个结论现在还成立吗？」：接力开 `report_optional` 续接 Run（floor 0，代际 0）。第 1 轮查一次工具，第 2 轮返回 `m0-response-v1`：一条回复 `to_input_seq=1`，引用本 Run 两个证据编号（无 `unsupported`），同时附一份合格的修订报告并发布——结论 sha 由 `14817dfd…` 变为 `0a0d6f9e…`；发布这份结论的是续接 Run 的结论步骤（`conclusion:g0:ctx0:round-2`，代际 0）。代际、lifecycle（open）不变。
- 关闭：200，代际 0 → 1，lifecycle closed。
- 关闭后的消息「谢谢。关闭之后还有需要注意的吗？」：接力开 `reply_only` 续接 Run（floor 1），一次请求、0 个工具调用、0 次工具计费，结论步骤 `execution=answered`、一条回复 `to_input_seq=2`；结论（`0a0d6f9e…`）、lifecycle（closed）、代际（1）不变，Run 行 `completed`。
- 5 次 HTTP，已知费用上限 0.080614 CNY。时间 2026-11-17 19:06 PT，约 30 秒。

已用数据库行核对上述各项（事故、三个 Run、两条 `message` 输入、一条 `close` 审计、发布结论的步骤）。

## Run 1（失败，保留在仓库外）

- [LangSmith trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/98c56dd8-ac1f-4247-8db1-8be275d09273/r/00000000-0000-0000-ecf4-4d2a1591ee95?poll=true)；摘要与原始 ledger 在 `~/.agent/handoffs/attachments/workbench-p2-ledgers/`。
- 首个 Run 发布，「你好」续接 Run `answered`（结论不变）；关闭后的 `reply_only` 续接 Run 交接 `RESPONSE_INVALID`：唯一一轮是最终轮，请求里在回复说明之后又带了最终报告指令，模型按后者返回裸 `m0-report-v3`、没有回复。结论保留、lifecycle 仍为 closed。修复：`reply_only` 最终轮不发最终报告指令（合同 r8，提交 `90bf51bb`）。

## 运行

用户自己运行 `bash ~/.agent/p2run.sh`（命令见交接；`M0_ENV_FILE` 由用户设置，凭据只由进程读取、不打印）。原始 ledger 写到 `~/.agent/handoffs/attachments/workbench-p2-ledgers/`，摘要里只留它的 sha256。

## 没有覆盖的

- fixture 执行器不在 Run 行记工具次数（`tool_operations_used=0` 对调过一次工具的 `report_optional` 续接 Run 也是 0，P1 已记录的既有行为）；`reply_only` 不调工具由步骤行（0 个 tool_calls）和工具计费表（0 行）确认。
- 运行中发消息（D1）、暂停（D2）、人工接管（D8）、取消后的接力规则没有真实 Run，由 `tests/acceptance/test_workbench_conversation.py` 的脚本化模型场景覆盖。
