# 有界真实 Run（2026-09-23，PR #31 `feature/m1-01-control-completion`）

AGENTS.md「验证与汇报」要求触及调查 loop / 恢复路径的 PR 附至少一次有界真实 Run 的账本与结果。本目录是
`.venv/bin/python -m scripts.m1_live_flash_loop` 在本分支（含 P1 修复 `e61216b` 及其后提交）上执行一次的产物；
脚本本身把输出写到固定目录 `docs/evidence/m1-01-investigation-loop/`（已提交的 2026-09-16 证据），本次运行后把新
文件复制到此处，再用 `git restore` 复原原目录的跟踪文件（`git diff --quiet` 通过）。凭据来自私有 env 文件，未打印、
未写入任何工件（已对本目录 grep 凭据形态，无命中）。

| 项目 | 值 |
|---|---|
| run_id | `b556ef98-6959-4227-864d-2de3937c374c` |
| 时间（UTC） | 2026-09-23T11:30:49 → 11:31:00（约 11.3 s） |
| 模型 / 物理请求数 | `deepseek-flash` / **2 HTTP**（`model_requests=2`，`budget_limit=4`） |
| 第 1 次请求 | `finish_reason=tool_calls`，1 个工具调用，0.987 s，prompt 1125 / completion 105（reasoning 59） |
| 第 2 次请求 | `finish_reason=stop`，json_mode，10.325 s，prompt 1341 / completion 2131（reasoning 957） |
| tokens 合计 | prompt 2466 / completion 2236 |
| 费用上界 | **0.024988 CNY**（按脚本内 0.3 / 1.2 USD per M、7.3 CNY/USD 估算） |
| execution / handoff | `completed` / `false`，`handoff_reasons=[]` |
| 报告 | `m0-report-v2`（`report.json` 原文、`report-parsed.json` 解析结果），1 个 evidence_id，`steps_committed=2` |
| prompt_revision | `prompt-replay-candidate-2c26fd0db1e0` |

## 这次 Run 证明与不证明的事

- 证明：本分支上 loop 主路径（组装上下文 → 模型 → 提交步骤与工具计划 → 执行并提交工具观测 → 最终报告）在真实
  DeepSeek 调用下按预算与 deadline 完成，账本、报告结构与 evidence 引用与合并前证据（2026-09-16，2 HTTP，
  0.024617 CNY）同量级。
- **不证明**：本 Run 没有任何 follow_up / correct / event 输入——脚本用 `MemoryStepStore` 且未预置 `inputs`，
  `begin_round()` 每轮返回空列表，`investigation_inputs` 尾随消息未出现，步骤行 `context.input_watermark=0`。
  因此它不覆盖 P1 修复的输入回放/重建路径；该路径的证据是确定性单测（`tests/test_m1_investigation_context.py`
  三条、`tests/test_m1_investigation_compaction.py` 一条）与 CI `m0-postgres` 中的
  `tests/integration/test_m1_loop_resume_postgres.py::test_a_follow_up_between_rounds_then_a_restart_resumes_and_publishes`。
- 本 Run 是内存 store 上的单次尝试，不涉及 PostgreSQL 租约、重启恢复或人工控制；不是产品验收，也不改变任何
  feature `passes` 或 SPEC 门槛。
