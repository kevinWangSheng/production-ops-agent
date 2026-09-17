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

## 追加（2026-09-17）：租约续期接入 `RecoverySession`（lease-wire.md §7b）

- 依据：`/private/tmp/claude-501/-Users-shenghuikevin-dev-AI-production-ops-agent/a7d3abb1-8221-4812-bafb-316b8b11e6aa/scratchpad/reports/lease.md`
  （PR #35 `fix/lease-renewal`，新增 `DurableStore.renew_lease(lease, extend_seconds) -> datetime`，语义见其 §1/§2，
  接线建议见其 §7b）。**PR #35 尚未合并**，本分支的 `DurableStore` 没有 `renew_lease`；未复制其实现。
- 做法：`RecoverySession.execute_pending` 在每次 `execute(item)` 之后、`self.store.commit_tool(...)` 之前调用新增的
  `_renew()`，通过 `getattr(self.store, "renew_lease", None)` 以「可选能力」接入——store 没有该方法时是 no-op，
  行为与改动前完全一致；一旦 #35 合并，自动启用。拒绝语义与本模块其它写路径一致：原样抛 `PersistenceError`，
  不吞、不改包装类型。`Worker.resume(..., renew_seconds=420)`、`RecoverySession.renew_seconds=420`。
- 提交：`9d23b9f`（接入 + 内存替身单测 + 自跳过的 PG 集成测试文件）、`711c67d`（格式修正）、
  `1d55e9f`（见下方独立审查发现 1 的修复）。
- 单测（`tests/test_worker_recovery.py`，内存 `_RecordingStore` 双测替身，不需要 PG）：
  store 无 `renew_lease` 时行为不变；有该能力时验证调用顺序恰为 `execute → renew → commit`；续期被拒
  （`PersistenceError("CONTROL_DENIED")`）时该工具结果不提交、异常原样上抛。
- PG 集成测试（`tests/integration/test_m1_lease_renewal_wiring_postgres.py`，跳过条件
  `M1_DURABLE_POSTGRES!=1 or not hasattr(DurableStore,"renew_lease")`，本分支上恒跳过）：用
  `git checkout -b tmp/lease-wire-30-verify`（本地临时分支，`feature/m1-01-restart-recovery` +
  `origin/fix/lease-renewal`，三方 merge 无冲突）验证过两次（初版与修复后各一次）针对真实 PG 通过：
  1) 短初始租约 + 两个 pending 工具，仅靠续期让第二个 `commit_tool` 不因租约到期被拒；
  2) 工具执行期间人工 `cancel`，续期本身被拒、结果确未提交。临时分支跑完即删除（`git branch -D`），
  未提交、未推送该合并；仅用于本地验证。
- 独立审查（全新上下文只读子代理，未见实现者结论，只给背景与合同）：
  - **发现 1（已修复，`1d55e9f`）**：续期发生在 `execute()` 之后，对循环里**第一个**（或唯一一个）pending
    工具，其自身执行期间只受初始 `claim()` 的 `lease_seconds` 保护，续期帮不上——若该工具执行时长接近初始
    租约（改动前默认 30s），`renew_lease` 本身会因租约已过期被拒，接线对这唯一工具等价于没有收益。
    修复：把 `Worker.claim`/`Worker.resume` 的 `lease_seconds` 默认值与 `renew_seconds` 默认值统一为同一常量
    `DEFAULT_LEASE_SECONDS=420`（`opspilot/worker.py`），使初始 claim 本身也能覆盖第一个工具的执行；
    并加回归测试锁定四处默认值一致（`test_the_initial_claim_defaults_to_the_same_length_as_renewal`）。
    本分支上无任何直接调用方依赖旧默认值 30（已 grep 确认），改动风险低。
  - **发现 2（有依据的疑点，未改代码，仅记录）**：`420` 借自 lease.md §2「`MODEL_REQUEST_TIMEOUT_SECONDS`
    (360s) + 60s 余量」的推导，但那是给会调模型的循环用的；`RecoverySession` 本身不调模型，只重放已提交的
    工具计划，本分支代码里也找不到任何强制「单工具 ≤30s」的常量或校验（该说法只出现在
    `docs/evidence/m0-*` 的 M0 实验报告里）。420s 对这条路径偏保守但不算错，已在 `opspilot/worker.py` 的
    `DEFAULT_LEASE_SECONDS` 注释里如实标注「未独立针对工具执行时长重新推导」，留给后续复核。
  - 无阻塞发现：拒绝语义一致、三个新单测非同义反复、PG 测试跳过条件符合预期、未改动
    `persistence.py`/`recovery.py`/`_assert_current`/`publish`/`claim` 的既有语义、未超出任务范围。
- 验证：本地 `ruff check`/`ruff format --check`/`mypy` 全绿；`.venv/bin/python -m pytest -q` →
  `1060 passed, 92 skipped, 2 xfailed`（含新增 4 个确定性单测，PG 相关 2 个自跳过）。真实 PG（临时合并分支，
  本 worktree 专属 lab，端口 55431，跑完已 `stop`）：`test_m1_durable_state_postgres.py` +
  `test_m1_lease_renewal_postgres.py`（#35 自带）+ `test_m1_lease_renewal_wiring_postgres.py` 共
  `55 passed`，两轮（初版、修复后）均通过，无回归。费用：0（无模型调用）。
- 依赖顺序：**#35 需先合并**才能让本分支的 `renew_lease` 真正生效（当前是可选能力，缺失时无操作）；#35
  合并前，本改动本身对生产行为无影响（`getattr` 找不到方法）。合并授权：本任务未授权合并，PR #30 状态
  为「实现中，待推送与 CI」。
