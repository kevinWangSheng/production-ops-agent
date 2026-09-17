# M1-01 重启/不兼容处理

- 目标：实现 worker 侧按 PostgreSQL 业务记录重建 Run、续跑与租约 fencing；不兼容版本进入 `blocked(INCOMPATIBLE_STATE)`。
- 范围：`opspilot/recovery.py`、`opspilot/worker.py` 及确定性测试；不改 UI。持久化最小改动：无（沿用 `DurableStore.rebuild()`、`claim()` 的现有事务与 fence）。
- 依据：SPEC.md、PRODUCT-CONSTRAINTS.md、C3 第 7 节、M0 recovery evidence、PR #28 diff（已检查 persistence.py，未重复其人工控制逻辑）。
- 完成条件：确定性测试、M1 PG 故障注入/重启覆盖、`make check`、独立审查、PR。
- 进展：新增不可变（深拷贝隔离）`RecoveryPlan` 与绑定 lease 的 `RecoverySession`；`Worker.resume()` 先从 PG 一致快照重建，再通过 `DurableStore.claim()` 获取新 epoch，待执行工具来自已提交业务记录。持久化改动：无；沿用 `rebuild()`、`claim()`、`commit_tool()`、`publish()` 的既有 fence。
- 验证证据：`make setup` 成功；定向单元 3 passed；`make check` 通过（1054 passed, 84 skipped, 2 xfailed）；启动本 worktree 专属 PG 后 `M1_DURABLE_POSTGRES=1 ...test_m1_durable_state_postgres.py -q` 通过 31 passed；新增子进程真实 `Popen`/kill/restart 场景通过，确认 epoch 由 1 续为 2；PG 已由所属脚本停止。静态 `ruff`、`mypy` 与 `git diff --check` 通过。
- 独立审查：全新上下文第二轮已完成。审查指出恢复前代际重校验、畸形 tool call fail-closed、深层不可变三项问题；均已修复并补回归测试。原提交信息中的 `[ #M1-01 ]` 属拆分子任务标识，后续提交不再使用伪 feature ID，历史不改写。
- CI/审查收尾：修复 HEAD `2f31655` 的 workflow_dispatch run [35146834206](https://github.com/kevinWangSheng/production-ops-agent/actions/runs/35146834206) 已 success，`checks` 与 `m0-postgres` 均 success。三个机器人 thread 已逐条采纳修复并 resolve。当前 PR 仍以未合并的 #26 为 base；#26 合并后需 retarget `main`。本记录追加提交后需再以最终 HEAD 重跑 CI。
- 未运行真实模型调用；不声称产品验收或 feature passes 通过。
- 新一轮机器人 thread：lease epoch/owner/expiry 在工具 dispatch 前校验，并在代际不匹配时调用精确 lease relinquish；新增 PG fencing 回归。修复后本地 `ruff`、`mypy` 通过，M1 PG 集成 33 passed。待最终 HEAD CI。
- 最终收尾：HEAD `193539d` 的 workflow_dispatch run [35150133043](https://github.com/kevinWangSheng/production-ops-agent/actions/runs/35150133043) 中 `checks`、`m0-postgres` 均 success；本轮 2 个 thread 已逐条回复、修复并 resolve，当前 GraphQL 查询无 `isResolved=false` thread。PR 等待用户合并。

## 追加（2026-09-17）：`rebuild()["pending_tools"]` 恒空（ui-e2e 交接项 1/4 → storefix2 A）

- 缺陷：`rebuild()` 只从顶层 `response["tool_calls"]` 读工具计划，而 PR #29 的 loop 按 C3 §4/§7 提交完整模型响应
  `{"assistant": {...tool_calls}, finish_reason, usage, ...}`，于是产品 Run 的 `pending_tools` 恒空，
  `Worker.resume()/RecoverySession.execute_pending` 永远没有可续接的计划。
- 归属：当前 pending_tools 块（校验 `2f316553`、`operation_id`/`tool_call` `05b48721`）与 `Worker` 都在本 PR；
  读取路径与合同不符（#33 的 web 层已按 `assistant.tool_calls` 消费），故修读方、落本 PR；main 上的原始读取器被本 PR 的重写覆盖，不另开分支。
- 修复：`4273a38`（`_tool_plan()`：`assistant.tool_calls` 优先，否则顶层，兼容 M0 harness 形状）；
  `2fc706f`（审查 P3：`assistant` 非 dict 时返回非列表，`rebuild()` 校验 fail closed）。
- 测试（`tests/integration/test_m1_durable_state_postgres.py`）：`test_rebuild_lists_pending_tools_from_a_loop_shaped_step`
  （修复前 `[] == [(step, 1)]` 红）、`test_worker_resume_executes_only_the_pending_tools_of_a_loop_step`
  （修复前 `execute_pending` 返回 0 红；修复后只执行 ordinal 1、旧租约写入 `CONTROL_DENIED`）、
  `test_rebuild_rejects_a_malformed_loop_shaped_step`。
- 验证：`make check` → `All checks passed!` / `Success: no issues found in 15 source files` / `1056 passed, 90 skipped, 2 xfailed`；
  PG 定向 durable_state → `36 passed`。集成分支（main + 10 open PR）同一修复 `make check` → `1483 passed, 112 skipped, 2 xfailed`。
- 独立审查（全新上下文只读子代理）：修读方正确、Worker 测试非同义反复；P3「`assistant` 非 dict 静默为空」已采纳；
  另指出（范围外、先于本修复）`pending_tools[].operation_id` 为 `step:ordinal`，执行器 `ToolRequest.operation_id` 为 `step-t{index}`，
  接入真实执行器前需统一；以及当前产品 web 路径未使用 `Worker.resume`，本修复现阶段的可见效果是事故页「未提交工具操作数」与恢复接口本身。
