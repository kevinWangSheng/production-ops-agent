# storefix 报告：红线审计 P2-2（DurableStore.claim）与 P2-3（工具预算跨 attempt）

- 执行者：storefix-claude（worktree `production-ops-agent-integration-full-store`，分支 `integration/m1-01-full-store`，起点 e42d7b7）
- 日期：2026-09-17
- 边界遵守：未碰 main、未合并 PR、未改验收步骤/passes、未改冻结上限、未新增依赖、未 force-push、无模型费用。

## 1. 归属判定

| 发现 | 证据 | 判定 | 落地 |
|---|---|---|---|
| P2-2 `claim()` 版本判定先于状态守卫 | `git blame -L 587,592 e42d7b7 -- opspilot/persistence.py` → `68c423a9`（2026-09-14）；`git branch -r --contains 68c423a9` 含 `origin/main`；`git show origin/main:opspilot/persistence.py` 211-220 行同一块 | 违规代码已在 main | 新分支 `fix/claim-state-guard`（自 `origin/main` b483a12），worktree `../production-ops-agent-claim-guard`，PR 到 main |
| P2-3 执行器工具次数/秒数只在实例内存 | `git diff --stat origin/main...origin/feature/m1-01-tool-executor` 只新增文件（main 无 `opspilot/tools/`）；`_operations_used`/`_tool_seconds_used` 仅在 `opspilot/tools/executor.py` | 违规代码只在 PR #20 分支；持久化侧为新增代码，与执行器同一交付才有测试落点 | 独立提交 cherry-pick 到 `feature/m1-01-tool-executor`（PR #20 → main） |

审计要求先确认的事实：产品里没有把执行器接进 `Workbench.run_once`/`Worker` 的组合代码。核实：`grep -rn "ReadOnlyToolExecutor(" opspilot scripts tests` 仅命中 `tests/m1_tool_support.py::build`（测试夹具）与 `tests/test_m1_tool_registry_binding.py`；`opspilot/web/service.py::run_once` 只接收注入的 `Investigator`，`opspilot/worker.py::RecoverySession.execute_pending` 只接收 `execute` 回调。因此 P2-3 修在接口层（执行器 + 持久化 + 适配器），组合层接入属各 PR 范围（见「未完成项」）。

## 2. 修复概要

### P2-2（集成分支 `3b855fe`；fix 分支 `ade112a`）

`opspilot/persistence.py` `claim()`：
- 顺序改为：`LEASE_ACTIVE` → `DEADLINE_EXCEEDED` → suspension（集成分支才有）→ **incident `completed/cancelled/paused` → `CONTROL_DENIED`** → **run 不在 `queued/running/blocked` → `CONTROL_DENIED`** → `versions` 不符 → `UPDATE ... SET state='blocked' WHERE run_id=%s AND state IN ('queued','running')` + `INCOMPATIBLE_STATE` → 已 `blocked` 且版本已对上 → `CONTROL_DENIED`（不静默恢复）→ 发租约。
- 已 `blocked` 的 Run 保持修复前的答复（版本仍不符 → `INCOMPATIBLE_STATE`；已对上 → `CONTROL_DENIED`），`Worker.resume` 的 `plan.candidate` 守卫不受影响。
- 两处内容差异仅为 main 上 `row["state"]` 与集成分支 `row["run_state"]` 的列别名，以及集成分支多出的 `_lock_scope`/suspension 前置判定（均为 PR #26/#31 已有代码）。

### P2-3（集成分支 `7c3ff53`；PR #20 分支 `28f0b2b`）

- `opspilot/tools/executor.py`：新增 `ToolUsage`（校验非负/有限）、`ToolUsageLedger` Protocol（`usage()`/`charge(operation_id, seconds)`）；构造函数新增必填 `ledger`（不合规 → `ToolContractError("INVALID_LEDGER")`）；起点取 `ledger.usage()`；`_run` 派发前 `charge(op, 0.0)` 记次数（进程中途死掉也算已用），派发后 `charge(op, elapsed)` 结算秒数；charge 失败 → 派发前拒绝 `denied/CONTROL_UNAVAILABLE`（不出网），派发后拒绝且不采纳、不登记证据（与 `EVIDENCE_NOT_COMMITTED` 同一 fail-closed 规则）；异常文本不外泄。
- `opspilot/persistence.py`：`opspilot_runs` 新列 `tool_operations_used integer`、`tool_seconds_used double precision`（`ADD COLUMN IF NOT EXISTS`，沿用 `install()` 既有演进方式，无迁移框架）；新表 `opspilot_tool_charges(run_id, epoch, operation_id, seconds)`；`charge_tool(lease, operation_id, seconds)`：首次记次数+秒数，同键再次调用只按正差额补秒数，租约栅栏与同文件其它写路径一致（集成分支用 `_lock_scope`+`_lease_revoked`；#20 分支按 main 的 `reserve_budget` 内联栅栏重写）。
- `opspilot/tools/ledger.py`：`DurableToolLedger(store, lease)`，`usage()` 从 `rebuild()["run"]` 读累计值。
- `opspilot/tools/__init__.py` 导出；`tests/m1_tool_support.py` 新增 `RecordingLedger` 并作为 `build()` 默认 ledger（`ScriptedInvestigator`、`scripts/m1_live_flash_loop.py` 经 `build()` 行为不变）；`tests/test_m1_tool_registry_binding.py` 直接构造处补 `ledger=`。
- 冻结上限 `MAX_OPERATIONS_PER_RUN=20`/`MAX_TOOL_SECONDS_PER_RUN=240.0` 未改。

## 3. 复现与修复测试

### P2-2
- 用例：`tests/integration/test_m1_durable_state_postgres.py::test_version_change_cannot_override_a_paused_cancelled_or_completed_run`（paused：claim 新版本 → `CONTROL_DENIED`、run 仍 `paused`、`resume` 返回代际 2、解除暂停后再领取才 `blocked`；cancelled：仍 `cancelled`；completed：`conclusion` 保留、run 仍 `completed`）。
- 修复前（集成分支）：`1 failed`，断言输出 `Actual message: 'INCOMPATIBLE_STATE'`（期望 `CONTROL_DENIED`）。
- 修复后：集成分支该文件 `44 passed in 6.10s`；fix 分支该文件 `22 passed in 7.56s`。红/绿切换用 `git diff > 补丁` / `git apply -R` / `git apply`，两边均复核。

### P2-3
- 修复前（旧执行器接口，脚本，同一 Run 两个执行器实例、FakeTransport 每次 12 s）：
  `attempt 1: operations_used=20, tool_seconds_used=240.0, 最后拒绝 OPERATION_BUDGET_EXHAUSTED`；
  `attempt 2（新实例）: operations_used=20, tool_seconds_used=240.0` —— 同一 Run 共派发 40 次 / 480 s。
  （新接口要求 `ledger`，因此提交的用例在旧代码上以 `TypeError` 红而非行为红；行为红以此脚本输出为证。）
- 修复后：
  - `tests/integration/test_m1_tool_budget_postgres.py`（3 条）：第二 attempt（新 epoch）起点 `(3, 18.0)`，整 Run 派发到 20 次后 `OPERATION_BUDGET_EXHAUSTED`，PG 行 `tool_operations_used=20`、`tool_seconds_used=120.0`；`charge_tool` 同键幂等/只升不降/输入校验；cancel 后 `charge_tool` → `CONTROL_DENIED` 且计数不变。
  - `tests/test_m1_tool_boundaries.py` 末尾 6 条：从 ledger 起点续计（19 → 第 2 次拒绝）、秒数起点续计（239 s → timeout 绑 1 s → 之后 `TIME_BUDGET_EXHAUSTED`）、未出网的拒绝不计费、派发前后各一次 charge、ledger 不可用两种路径、非法 ledger/usage 的合同错误。

## 4. 检查结论行

| 位置 | 命令 | 结论行 |
|---|---|---|
| 集成分支 `integration/m1-01-full-store`@7c3ff53 | `make check` | `All checks passed!`（ruff）/ `Success: no issues found in 39 source files`（mypy）/ `1481 passed, 106 skipped, 2 xfailed in 31.13s` |
| 同上 | `M1_DURABLE_POSTGRES=1 .venv/bin/python -m pytest tests/integration -q` | `52 passed, 54 skipped in 13.12s`（skip 为 M0 专属开关的用例） |
| PR #20 分支 `feature/m1-01-tool-executor`@28f0b2b | `make check` | `All checks passed!` / `Success: no issues found in 18 source files` / `1216 passed, 78 skipped, 2 xfailed in 26.11s` |
| 同上 | `M1_DURABLE_POSTGRES=1 pytest tests/integration/test_m1_tool_budget_postgres.py tests/integration/test_m1_durable_state_postgres.py` | `24 passed in 3.43s` |
| fix 分支 `fix/claim-state-guard`@ade112a | `make check` | `All checks passed!` / `Success: no issues found in 13 source files` / `1050 passed, 76 skipped, 2 xfailed in 32.97s` |
| 同上 | `M1_DURABLE_POSTGRES=1 pytest tests/integration/test_m1_durable_state_postgres.py` | `22 passed in 7.56s` |

PG lab：初始复用了 `integration-full-ui` worktree 会话启动的 lab（端口 55431），该会话中途将其停止导致一次全红（`44 failed in 1.56s`，连接失败），随后在本 worktree 自启 lab（`tmp/m0-b/postgres`）重跑，以上结论均来自自启 lab。

## 5. 落地位置

| 发现 | 分支 | 提交 | PR / 状态 |
|---|---|---|---|
| P2-2 验证 | `integration/m1-01-full-store`（未推送，集成验证用） | `3b855fe` 修复+用例；`e94177a` 审查补断言 | — |
| P2-3 验证 | 同上 | `7c3ff53` 修复+用例；`dd7fe04` 审查修复 | — |
| P2-2 交付 | `fix/claim-state-guard`（自 `origin/main` b483a12；worktree `../production-ops-agent-claim-guard`） | `ade112a` 修复；`4560bba` 任务记录；`08cf97b` 审查补断言（cherry-pick -x）；`e25c5a5` 记录审查处置 | **PR #34** → main，CI `checks` pass / `m0-postgres` pass，`CLEAN`/`MERGEABLE`；Codex Code/Security Review 于 15:46Z 触发，结果见第 9 节 |
| P2-3 交付 | `feature/m1-01-tool-executor`（worktree `../production-ops-agent-m1-tool-executor`） | `28f0b2b` 修复（cherry-pick，`charge_tool` 按 main 栅栏重写）；`63b3cd7` 任务记录；`988ec90` 审查修复（cherry-pick -x）；`ffd3161` 栅栏口径注释；`f4f30fe` 审查处置记录 | **PR #20**（已 `gh pr edit --body-file` 追加说明），head `f4f30fe`，CI `checks` pass / `m0-postgres` pass，`CLEAN`/`MERGEABLE` |

集成分支自审查修复后复验：`make check` → `All checks passed!` / `Success: no issues found in 39 source files` / `1481 passed, 107 skipped, 2 xfailed in 31.83s`；PG 定向（budget + durable_state 两文件）`48 passed`。
PR #20 分支复验（`ffd3161`）：`make check` → `1216 passed, 79 skipped, 2 xfailed`；PG 定向 `25 passed`。
fix 分支复验（`08cf97b`）：PG 定向 `22 passed`；ruff/format 通过（`make check` 全量在 `ade112a` 时已通过，之后只增测试断言与文档）。

## 6. 独立审查处置表

审查者：Agent 工具派出的全新上下文只读子代理（model sonnet），审 `3b855fe`/`7c3ff53`/`ade112a`/`28f0b2b`，本地跑非 PG 单元测试；PG 用例由其静态追踪。结论：无 P1，2 项 P2，4 项 P3。

| # | 级别 | 发现 | 处置 | 提交 |
|---|---|---|---|---|
| 1 | P2 | PR #20 分支 `charge_tool` 把 `lease_until IS NULL` 判为撤销，同文件另三条写路径（main 旧写法）容忍 NULL，口径不一 | 采纳为记录：保留更严口径（与 #26 收敛后的 `_lease_revoked` 一致，方向只更严），加注释说明；回改另三条属 #26 范围 | `ffd3161` |
| 2 | P2 | 同 operation_id 跨 epoch 重派发会再计一次，且该情形无测试 | 采纳：确认为有意设计（C3 §7 未提交结果可能需有界重复，是真实第二次查询；累计只增不减），docstring 写明，新增 PG 用例 `test_a_re_dispatched_operation_in_a_new_epoch_is_counted_again` | `dd7fe04`/`988ec90` |
| 3 | P3 | `DurableToolLedger.usage()` 异常在执行器构造时未捕获 | 采纳：构造时 `ledger.usage()` 异常 → `ToolContractError("LEDGER_UNAVAILABLE")`，单元测试补 | 同上 |
| 4 | P3 | 本地 `_operations_used` 在派发前 charge 确认之前自增 | 采纳：自增移到 charge 成功之后，单元测试补断言 | 同上 |
| 5 | P3 | 派发后结算失败时持久秒数停留在 0 s | 记录不改：窗口极窄、方向保守（结果不采纳、次数已计），注释写明 | 同上（注释） |
| 6 | P3 | blocked 后版本对回来再领取 → `CONTROL_DENIED` 无测试 | 采纳：同一用例补断言 | `e94177a`/`08cf97b` |

审查确认无误的项：P2-2 顺序无回归（并附带保护 `waiting_human/failed/budget_exhausted` 不被改写，同一机制）；两处移植语义一致；双 charge 协议同 epoch 幂等；fail-closed 不外泄异常文本、不登记证据；锁顺序与 `install()` 演进方式一致；冻结上限未改；无范围外改动。

## 7. 未完成项

- **组合层未接入 `DurableToolLedger`**：`Workbench.run_once`（#33）、`Worker`（#30）、loop 的组合（#29）目前没有产品代码构造执行器；测试夹具 `build()` 默认内存 ledger，行为不变。接入时须用 `DurableToolLedger(store, lease)`，否则 P2-3 在产品路径上仍未闭环。属各自 PR 范围，本任务未动。
- **PR #34 的 Codex code review 结果**：触发于 15:46Z，本报告写作时仍在运行；结果与 thread 处置见第 9 节（后台等待中，若本节无更新即表示报告结束前未返回）。
- **PR #20 未对新提交重新触发机器人审查**：AGENTS.md 要求覆盖当前 HEAD 的复审；此前该 PR 的 `@codex review` 两次因额度失败。我未擅自再触发（触发即外部动作、且额度不足）。PR #20 既有 3 条 inline thread 已 resolved，无新 thread。
- **合并顺序冲突**：PR #34 与 PR #26 在 `claim()` 同一函数、PR #20 与 #26 在 `install()` 处会有可手工解决的冲突；集成分支已证明两者并存通过。
- 集成分支 `integration/m1-01-full-store` 的 4 个验证提交未推送（调度者建的本地分支）。
- `tests/integration` 全量 PG 跑有 54 项 skip，属 M0 专属开关（`M0_*`）用例，非本任务范围。

## 8. 需用户裁定事项

1. **PR #34、PR #20 的合并**：均 CI 绿、`CLEAN`/`MERGEABLE`、独立审查发现已处置；按 AGENTS.md 由用户审核合并。建议顺序：#34 与 #26 二选一先合，后合者手工解冲突。
2. **是否对 PR #20 再触发 `@codex review`** 以获得覆盖 `f4f30fe` 的机器人复审（此前两次因额度失败）。
3. **跨 epoch 重派发再计次**（审查 #2）是本任务作出的口径判定：C3 §13 未对工具次数明说，我按 §7「未提交结果可能需要有界重复」取保守（只增不减）解释。若用户希望「同一逻辑操作只计一次」，需改键为 `(run, operation_id)` 并接受重派发不计的放松。
4. PR #20 分支 `charge_tool` 的 NULL 租约口径比同文件另三条更严（审查 #1）：保持现状等 #26 统一，或指示回改。

## 9. 环境与进程

- PG lab：本 worktree 自启（`tmp/m0-b/postgres`，端口 55431），任务结束已 `postgres_lab stop`，端口已释放。之前共用的 `integration-full-ui` 会话的 lab 被其自行停止，未由我操作。
- 新建 worktree `../production-ops-agent-claim-guard`（分支 `fix/claim-state-guard`）保留至 PR #34 合并后清理；PR #20 worktree 原有，未改其它文件。
- 无未提交改动（三处 `git status` 干净）。

## 10. PR #34 机器人审查结果（补记）

- Codex Code Review：2026-09-17T15:49:17Z **Completed**，覆盖 `e25c5a5`，无 inline thread、无 review 意见。
- Codex Security Review：**Failed**（运行失败，非「无发现」；按 AGENTS.md 不是交付门槛）。
- 状态更新：PR #34 已推送 `docs` 提交记录 CI/审查结果（head 变为该 docs 提交，仅文档，不改代码）；PR 状态「PR 已就绪，待用户审核合并」。
- PR #20：head `f4f30fe`，CI 绿、`CLEAN`/`MERGEABLE`，机器人未对新提交复审（见第 7、8 节）。

---

# 追加任务 A/B（storefix2）：`rebuild()["pending_tools"]` 恒空；`budget_spent` 从未结算

- 执行者/工作区同上；集成分支 `integration/m1-01-full-store` 在上一任务 4 个提交之上继续（未推送）。
- 边界遵守同上：未碰 main、未合并、未改验收/passes/冻结上限、无 force-push、无费用。

## A. `rebuild()["pending_tools"]` 恒空

### 归属判定
| 证据 | 结论 |
|---|---|
| `git blame` 集成分支 `persistence.py` pending_tools 块：`.get("tool_calls")` 三处基线为 `68c423a9`（main）；校验块 `2f316553` 与 `operation_id`/`tool_call` 字段 `05b48721`（2026-09-16）只在 `feature/m1-01-restart-recovery`（PR #30）及其下游分支，不在 main、不在 `chore/durable-store-hardening`（#26） | 当前 pending_tools 读取块是 #30 的重写；`Worker.resume`/`RecoverySession`（`opspilot/worker.py`、`recovery.py`）也只在 #30（main 无 `opspilot/worker.py`） |
| loop 提交形状 `{"assistant": {...tool_calls}, finish_reason, usage, request_sha256, response_id}`：`loop.py` `_commit_step` 为 `580ffcce`（#29） | 写方是 #29 |
| C3 §4「`ModelStep`：稳定步骤 ID、输入快照、**完整模型响应及工具计划**」；§7「完整模型响应与工具计划提交后，才能执行工具」；#33 的 web 层已按 `response["assistant"]["tool_calls"]` 消费（`service.py:137,729`） | loop 的形状符合合同（完整响应 + 计划）；错的是 `rebuild()` 的读取路径 |

判定：修 `rebuild()` 读取路径。落地到 **PR #30**（当前块的所有者，且 `Worker.resume` 级测试只能在这里落地）；main 上原始读取器的同一缺陷被 #30 的重写覆盖，不另开 fix 分支（避免与 #30 同函数冲突）。这是一处判断，写入报告供裁定。

### 修复（集成分支 `2c43896`；PR #30 分支 `4273a38`，cherry-pick -x，冲突为 #31 的 `status` 过滤行与测试文件上下文，手工解）
- 模块级 `_tool_plan(response)`：`assistant.tool_calls` 优先，否则顶层 `tool_calls`（兼容 M0 harness 与既有测试的形状）；`rebuild()` 三处（形状校验、`tool_call`、ordinal 范围）改用它。`test_rebuild_rejects_malformed_persisted_tool_calls` 仍通过。

### 复现/修复测试
- `test_rebuild_lists_pending_tools_from_a_loop_shaped_step`：loop 形状 step 提交两个 tool_calls、ordinal 0 已提交 → `pending_tools == [(step, 1)]`，含 `operation_id`/`tool_call`；旧顶层形状同时可读。修复前：`assert [] == [(step, 1)]` 红。
- `test_worker_resume_executes_only_the_pending_tools_of_a_loop_step`（`Worker.resume` 层）：租约到期后 `Worker.create(...).resume()` 得新 epoch，`execute_pending` 只执行 ordinal 1（记录回调收到 `logs.search`），提交后 `pending_tools == []`、两条结果齐全，旧租约再写 → `CONTROL_DENIED`。修复前：`execute_pending` 返回 0 红。

## B. `budget_spent` 从未结算

### 判定
- C3 §13：「请求还必须受剩余总预算约束。**重试计入次数和费用。预算在 PostgreSQL 原子预留和结算，未知费用保持占用**，重启不能重置预算。」PRODUCT-CONSTRAINTS「Runtime and human control requirements」要求 budgets；M0 lab `scripts/m0/budget.py` 已有同一三态（reserved/settled/unknown：`settle()`/`retain_unknown()`）的权威先例，产品表 `opspilot_runs` 也建了 `budget_reserved/spent/unknown` 三列。
- `origin/main` 的 `budget_spent`/`budget_unknown` 只出现在建表与 `reserve_budget` 的求和判断，无任何结算路径（核实同调度者）。
- 结论：**合同要求结算但未实现**。补充事实：M1-01 的预算单位是模型请求次数，每次请求预留恰为 1、且求和已含三列，因此「执行」本就正确；缺的是预留→spent/unknown 的真实记账（web 页面 `budget {{ run.budget_spent }} spent / ...` 一直显示 0）。未引入费用换算：token 用量已在 step 载荷 `usage` 中，仓库权威的 CNY 换算只在 M0 脚本，产品层只记次数。

### 修复（集成分支 `c92feca`；按归属拆到 PR #29 `b133cc0` 与 PR #33 `eec8dca`）
- `DurableStore.settle_budget(lease, reservation_id, outcome)`：`spent`（供应商已应答）/`unknown`（超时、传输失败、请求被拒等结果未知）；预留额转入对应列、**从不释放**；同结果重放为 no-op、不同结果 `IDENTITY_CONFLICT`；未知预留 `UNKNOWN_IDENTITY`；租约栅栏与其它写路径一致（被栅栏的尝试留在 `reserved`，仍计入上限）。#29 分支按 main 的内联栅栏写法。
- 提交器 seam：`StepCommitter.settle_budget`；`MemoryStepStore`（新增 `budget_unknown` 并纳入上限求和）；`DurableStepStore` 转发；web `_EmittingCommitter` 转发（#33 分支用该文件既有的 `getattr` 转发写法，因其 base 的协议尚无该方法，mypy 通过）；测试替身 `_MemoryCommitter` 镜像。
- loop `_call_model`：每个物理请求在 `model.complete` 返回后 settle `spent`、抛 `ModelError` 后 settle `unknown`（重试 `#a2` 各自结算）；`_settle` 只吞 `CONTROL_DENIED`（飞行中被栅栏：预留保持占用，由下一次被栅栏的写入把迟到响应记为历史并停机——否则 `test_a_pause_during_the_run_hands_off_and_publishes_nothing` 的 late_result 历史会丢），其它码停机。
- 执行语义不变：上限判断原本就是 reserved+spent+unknown，每次预留 1。

### 测试
- PG：`test_budget_reservations_settle_to_spent_or_unknown_and_never_release`：三笔预留 → spent/unknown 各一，`(1,1,1)`；重放 no-op；冲突/未知/非法结果三种拒绝；结算不释放（再预留仍 `BUDGET_EXHAUSTED`）；cancel 后结算 `CONTROL_DENIED` 且预留保持占用。
- loop 单元：`test_every_physical_request_is_settled_as_spent_or_unknown`（`MODEL_UNAVAILABLE` 重试后 `(0,1,1)`）；`test_a_fenced_settlement_leaves_the_reservation_occupied_and_records_history`。
- 修复前红：这些用例在旧代码上因方法不存在（AttributeError）而红；行为红以「完成 Run 停在 reserved=2/spent=0」（ui-e2e 报告已复现）为证。

## 检查结论行

| 位置 | `make check` | PG 定向 |
|---|---|---|
| 集成分支 @`c92feca` | `All checks passed!` / `Success: no issues found in 39 source files` / `1483 passed, 110 skipped, 2 xfailed in 28.55s` | `pytest tests/integration` → `56 passed, 54 skipped in 10.57s` |
| PR #30 `feature/m1-01-restart-recovery` @`4273a38` | `All checks passed!` / `Success: no issues found in 15 source files` / `1056 passed, 89 skipped, 2 xfailed in 29.04s` | durable_state + worker_recovery + architecture → `43 passed, 2 xfailed` |
| PR #29 `feature/m1-01-investigation-loop` @`b133cc0` | `All checks passed!` / `Success: no issues found in 26 source files` / `1288 passed, 76 skipped, 2 xfailed in 27.12s` | durable_state → `22 passed` |
| PR #33 `feature/m1-01-progress-ui` @`eec8dca` | `All checks passed!` / `Success: no issues found in 37 source files` / `1467 passed, 97 skipped, 2 xfailed in 29.79s` | web PG → `2 passed`；web workbench 单元 `23 passed` |

PG lab：本 worktree 自启（端口 55431），任务结束停止。

## 独立审查处置表（storefix2）

审查者：Agent 工具派出的全新上下文只读子代理（model sonnet），审 `2c43896`（A）与 `c92feca`（B），本地跑非 PG 全量（1483 passed）。结论：1 项 P1、1 项 P3、1 项范围外备注。

| # | 级别 | 发现 | 处置 | 提交 |
|---|---|---|---|---|
| 1 | P1 | 新 attempt 重新领取同一 Run 时，`round-1#a1` 的预留 id 与死掉的 attempt 相同；死掉的 attempt 已 settle `unknown`，新 attempt 再 settle `spent` → `IDENTITY_CONFLICT` → Run 失败，违背 C3 §7 首行「有界重试」 | **成立于 PR #29 单独分支**：集成分支上 loop 用的是 `begin_round` 返回的键，`DurableStepStore.begin_round`（PR #31）已加 `g{gen}:e{epoch}:` 前缀，不会碰撞；但 #29 的 base 没有 #31，移植后碰撞真实存在（移植时的首版 PG 用例在 #29 上因无 `begin_round` 而暴露）。采纳：`DurableStepStore` 按租约 epoch 派生 reserve/settle 的预留 id（两边一致，集成分支上是双重命名空间的防御），新增 PG 用例 `test_a_reclaimed_run_settles_its_own_reservations_without_conflict`（不依赖 `begin_round`），死掉 attempt 的 `unknown` 预留保持占用 | 集成 `248e4a3`→`81f8f15`；#29 `4f0648a` |
| 2 | P3 | `_tool_plan` 在 `assistant` 存在但非 dict 且无顶层 `tool_calls` 时静默返回 `[]`，与 fail-closed 校验不对称；loop 形状的损坏计划无测试 | 采纳：非 dict `assistant` 返回非列表使 `rebuild()` 抛 `INCONSISTENT_STATE`；新增 `test_rebuild_rejects_a_malformed_loop_shaped_step`（两种损坏形状） | 集成 `845522a`；#30 `2fc706f` |
| 3 | 备注（先于本次、范围外） | `pending_tools[].operation_id` 为 `step:ordinal`，执行器 `ToolRequest.operation_id` 为 `step-t{index}`；`Worker/RecoverySession` 当前无产品调用方 | 记录不改：写入 #30 任务记录与 PR 说明，接入真实执行器前统一 | — |
| 4 | 措辞 | A 的提交信息「死掉的 attempt 被重新领取并从头跑」对当前产品路径（web `run_once` 不用 `Worker.resume`）有所夸大 | 采纳为记录：#30 任务记录与 PR 说明写明现阶段可见效果是事故页「未提交工具操作数」与恢复接口本身；已 cherry-pick 的提交信息不改写 | — |

审查确认无误的项：修读方而非写方正确；Worker 测试非同义反复；spent/unknown 分类与 §13 一致；结算前置于身份校验、锁顺序与栅栏同其它写路径；`_settle` 吞 `CONTROL_DENIED` 不丢预留（求和仍计）；`MemoryStepStore` 上限项与 PG 一致；所有 `reserve_budget` 实现都有 `settle_budget`；无范围外改动、无产品约束问题。

## 落地位置（storefix2）

| 项 | 分支 | 提交 | PR / 状态 |
|---|---|---|---|
| 集成验证 | `integration/m1-01-full-store`（未推送） | `2c43896` A；`c92feca` B；`845522a` A 审查修复；`248e4a3`+`81f8f15` B 审查修复 | 复验 `make check` → `All checks passed!` / `Success: no issues found in 39 source files` / `1483 passed, 112 skipped, 2 xfailed in 29.76s`；`pytest tests/integration` → `58 passed, 54 skipped` |
| A | `feature/m1-01-restart-recovery` | `4273a38` 修复；`2fc706f` 审查 P3；`bd47a9f` 任务记录 | **PR #30**，body 已追加；复验 `make check` → `1056 passed, 90 skipped, 2 xfailed`；PG durable_state `36 passed` |
| B（持久层 + loop） | `feature/m1-01-investigation-loop` | `b133cc0` 修复；`4f0648a` 审查 P1；`814dd2d` 任务记录 | **PR #29**，body 已追加；复验 `make check` → `1288 passed, 77 skipped, 2 xfailed`；PG durable_state `23 passed` |
| B（web 转发） | `feature/m1-01-progress-ui` | `eec8dca` 转发；`da3537d` 任务记录 | **PR #33**，body 已追加；`make check` → `1467 passed, 97 skipped, 2 xfailed` |

CI 与 merge state 见下节「CI 补记」。

## 未完成项（storefix2）

- 三个 PR 的机器人 code review 未对新提交复审（同上一任务：额度问题，未擅自触发）。
- `pending_tools[].operation_id` 与执行器 `operation_id` 格式不一致（审查备注 3），先于本次，接入真实执行器时统一。
- 产品 web 路径 `run_once` 仍不使用 `Worker.resume`（ui-e2e 交接项 2，worker 组合层归属待用户决定）；A 修好了恢复接口，但生产路径尚未接。
- 集成分支 9 个验证提交未推送。
- 合并顺序：#29/#30 都在 `persistence.py` 与 durable_state 测试文件末尾追加内容，与 #26/#31 会有可手工解决的冲突；集成分支已证明全部并存通过。

## 需用户裁定事项（storefix2）

1. **A 的落地判断**：我把 `rebuild()` 读取路径的修复落在 PR #30（当前块与 `Worker` 的所有者），未为 main 上的原始读取器另开 `fix/` 分支（会与 #30 同函数冲突）。若用户要求严格按「main 代码走 fix 分支」，需另开分支并接受与 #30 的冲突。
2. **B 的判定**：按 C3 §13 判为「合同要求结算但未实现」并实现最小结算（次数级、不释放、不换算费用）。若用户认为结算不在 M1-01 范围，可只保留 `settle_budget` 与 seam 而不在 loop 调用——但那样 `budget_spent` 仍为 0。
3. **PR #29/#30/#33 的合并**：待 CI 结果与用户审核；#33 的转发提交是在 ui-claude 空闲、worktree 干净时追加的，请其 owner 知悉。

## CI 补记（storefix2）

- 仓库 CI 只在 PR 目标为 `main`/`chore/m0-*` 或 push main 时自动触发；#29/#30/#33 的目标是其它分支，此前的运行都是 `workflow_dispatch`。本次同样用 `gh workflow run ci.yml --ref <branch>` 触发（无费用、仓库内动作）。
- PR #29 推送后 `mergeStateStatus` 一度为 `DIRTY/CONFLICTING`：其 base `feature/m1-01-tool-executor`（PR #20）在上一任务新增了 `charge_tool`，与本次 `settle_budget` 落在 `persistence.py` 同一位置。已在 #29 上合并 base（普通 merge 提交 `c64be4b`，保留两侧方法，无历史改写），复验 `make check` → `All checks passed!` / `Success: no issues found in 27 source files` / `1294 passed, 81 skipped, 2 xfailed`；`pytest tests/integration` → `27 passed, 54 skipped`。
- 结果：

| PR | head | CI（workflow_dispatch） | merge state |
|---|---|---|---|
| #29 | `c64be4b` | run 35248915839 **success** | `CLEAN` / `MERGEABLE` |
| #30 | `bd47a9f` | run 35248688844 **success** | `CLEAN` / `MERGEABLE` |
| #33 | `da3537d` | run 35248692615 **success** | `CLEAN` / `MERGEABLE` |

- 环境：两个 PG lab（本 worktree 与 #29 worktree 各自的 `tmp/m0-b/postgres`）均已 `stop`，端口 55431 已释放；四个 worktree 无未提交改动；集成分支 9 个验证提交未推送。
