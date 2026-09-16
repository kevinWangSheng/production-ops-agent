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
