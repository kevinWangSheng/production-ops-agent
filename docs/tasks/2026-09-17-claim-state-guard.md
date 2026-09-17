# DurableStore.claim：人工控制状态先于版本判定（红线审计 P2-2）

- 状态：进行中（PR 已开，待 CI/code review）
- 更新日期：2026-09-17
- 依据：对 `integration/m1-01-full`@e42d7b7 的跨 PR 只读红线审计第 2 节 P2-2；
  [C3 第 5 节](../design/technical-proposal-2026-09-07.md)「授权变化不得经由 versions 触发
  INCOMPATIBLE_STATE」与「四类变化走四套机制，不得互相顶替」；
  [ADR-0003](../adr/0003-business-state-recovery-authority.md)；
  [PRODUCT-CONSTRAINTS](../../PRODUCT-CONSTRAINTS.md)「Runtime and human control requirements」。
- 工作区：分支 `fix/claim-state-guard`（自 `origin/main` b483a12 分出），
  worktree `/Users/shenghuikevin/dev/AI/production-ops-agent-claim-guard`。

## 目标与范围

`opspilot/persistence.py` `claim()` 在 `versions` 不符时无状态守卫地把 run 行改成 `blocked`
并抛 `INCOMPATIBLE_STATE`，且该判定先于 `paused/cancelled/completed` 与不可领取 run 状态的
拒绝分支。部署换版本后任何一次 claim 都会把人工暂停/取消/已发布的 Run 改写成 `blocked`：
人工 `resume` 得 `ILLEGAL_TRANSITION`，人工取消被投影成版本事故。

本任务只调整 `claim()` 的判定顺序并补一条 PG 集成测试；不改冻结上限、验收步骤或其他写路径。

归属判定：违规块 `persistence.py` 587-592（集成分支行号）`git blame` 为 `68c423a9`
（2026-09-14，已在 main），故修复自 `origin/main` 开分支，PR 到 main。
同一修复已在集成分支 `integration/m1-01-full-store` 以 `3b855fe` 验证
（那里的 `claim()` 含 PR #26/#31 的 `_lock_scope` 与 `run_state` 别名，语义一致）。

## 前提与完成条件

- 前提：本地 PostgreSQL lab（`scripts.m0.postgres_lab`），`M1_DURABLE_POSTGRES=1`。
- 完成条件：新增用例修复前红、修复后绿；`make check` 通过；独立审查发现逐条处置；
  PR 到 main 并等待 CI 与 code review。

## 执行进展与证据

- 修复（提交 `ade112a`）：状态守卫（incident `completed/cancelled/paused`；run 不在
  `queued/running/blocked`）先于 `versions` 判定；`UPDATE ... SET state='blocked'`
  加 `WHERE state IN ('queued','running')`；已 `blocked` 的 Run 保持原答复
  （版本仍不符 → `INCOMPATIBLE_STATE`；版本已对上 → `CONTROL_DENIED`，不静默恢复）。
- 用例：`tests/integration/test_m1_durable_state_postgres.py`
  `::test_version_change_cannot_override_a_paused_cancelled_or_completed_run`
  覆盖 paused（含解除暂停后再领取才 blocked）、cancelled、completed 三种。
  修复前：`1 failed`（实际 `INCOMPATIBLE_STATE`，期望 `CONTROL_DENIED`）；
  修复后本文件 `22 passed`（用 `git apply -R` 还原补丁复核红，再重新应用）。
- `make check`（本 worktree）：`1050 passed, 76 skipped, 2 xfailed`，ruff/format/mypy 通过。
- 已知合并顺序影响：PR #26 也改写了 `claim()` 周边（`_lock_scope`、`run_state` 别名），
  本 PR 与 #26 后合并者会在该函数出现可手工解决的冲突；集成分支上两者已并存并通过测试。

- 独立审查（全新上下文只读子代理，model sonnet，2026-09-17）：对本修复无 P1/P2；
  P3-6「已 blocked 的 Run 版本对回来后再领取 → CONTROL_DENIED 无测试」→ 采纳，
  提交 `08cf97b` 在同一用例补断言。审查另确认：新守卫顺序对 `waiting_human/failed/
  budget_exhausted` 同样不再改写成 blocked（同一机制，非扩范围）；`WHERE state IN`
  为冗余防御；两处移植语义一致。

## 下一步与交接

- 已推送并创建 PR，等待 CI 与 code review；结果与 thread 处置回写本记录。
- PG lab 进程属集成 worktree 会话（`tmp/m0-b/postgres`，端口 55431），任务结束时停止。
