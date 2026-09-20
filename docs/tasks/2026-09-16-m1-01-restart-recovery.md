# M1-01 重启/不兼容处理

- 目标：实现 worker 侧按 PostgreSQL 业务记录重建 Run、续跑与租约 fencing；不兼容版本进入 `blocked(INCOMPATIBLE_STATE)`。
- 范围：`opspilot/recovery.py`、`opspilot/worker.py`、`opspilot/persistence.py` 及确定性测试；不改 UI。持久化最小改动：沿用原有事务与 fence，并在 `claim()` 的 SQL 写入边界把 `lease_until` 封顶到 Run `deadline`。
- 依据：SPEC.md、PRODUCT-CONSTRAINTS.md、C3 第 7 节、M0 recovery evidence、PR #28 diff（已检查 persistence.py，未重复其人工控制逻辑）。
- 完成条件：确定性测试、M1 PG 故障注入/重启覆盖、`make check`、独立审查、PR。
- 进展：新增不可变（深拷贝隔离）`RecoveryPlan` 与绑定 lease 的 `RecoverySession`；`Worker.resume()` 先从 PG 一致快照重建，再通过 `DurableStore.claim()` 获取新 epoch，待执行工具来自已提交业务记录。当前恢复执行器失败会 best-effort 释放同一 lease 并原样抛出原异常；`claim()` 使用数据库时钟将租约封顶到 Run `deadline`。
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

## 追加（2026-09-17，历史记录）：租约续期接入 `RecoverySession`（lease-wire.md §7b）

- 依据：`/private/tmp/claude-501/-Users/shenghuikevin-dev-AI-production-ops-agent/a7d3abb1-8221-4812-bafb-316b8b11e6aa/scratchpad/reports/lease.md`（历史工作区路径，当前不作为验证入口）。该阶段记录的是 PR #35 尚未合并时的接线与临时合并验证；随后 PR #35 已合并到 `main`，因此本段关于「缺少 `renew_lease`」「行为不变」「需先合并 #35」的判断均为历史状态，不代表当前 tree。
- 历史做法：`RecoverySession.execute_pending` 在每次 `execute(item)` 之后、`commit_tool()` 之前调用 `_renew()`；当时通过可选能力兼容尚未合并的 `DurableStore.renew_lease`。历史单测覆盖无能力替身、`execute → renew → commit` 顺序和续租拒绝；历史 PG 临时合并验证覆盖短租约续期及人工 cancel 竞态。
- 当前树校准：`origin/main` 已包含 PR #35（merge commit `76293b0`，实现提交 `91451d9`），本分支基于该 main 合并结果，`opspilot.persistence.DurableStore.renew_lease` 已存在并由 `_renew()` 直接启用。`tests/integration/test_m1_lease_renewal_wiring_postgres.py` 仅在 `M1_DURABLE_POSTGRES!=1` 或能力确实不存在时跳过；因此在显式 PG 环境中它是可运行的当前验证，不应再写成恒跳过或未接线。
- 当前验证边界：本次修复会重新运行 wiring PG 测试并记录实际通过/跳过状态；没有真实模型调用、产品验收或 feature passes 结论。

## 追加（2026-09-20）：PR #30 P1——claim 后刷新恢复计划

- 依据：PR #30 最新 Code Review thread 指出，`Worker.resume()` 原先在 `claim()` 前生成 `RecoveryPlan`，claim 后只校验代际；同代际的另一 worker 可能先提交 pending tool 后失去 lease，导致旧计划重复外部查询或漏掉新状态。
- 修复：claim 成功后再次调用 `DurableStore.rebuild()`/`recover()`，再校验刷新计划的 `candidate`、`run_id` 与 lease 的 `control_generation`；任一不符即精确 `abandon()` 并返回 `CONTROL_DENIED`。通过 `RecoverySession` 绑定刷新后的计划，保留已有 fence 语义。
- 回归：`tests/test_worker_recovery.py::test_resume_refreshes_pending_plan_after_claim` 使用确定性双快照替身，断言 `rebuild → claim → rebuild` 顺序、claim 前后计划不同，以及 session 使用新 pending tool/run state。
- 工作区/版本：分支 `feature/m1-01-restart-recovery`，基线 `origin/main=a1eeadf88bc0e8c93e3f0ee223fae5bc6a339091`，修复开始前 HEAD `f9f936c4de61f2b0279b000a8f06085445254202`，修复代码提交 HEAD `e24b7325f304846499a7ad56aa54d8597e91c708`。
- 验证：定向 worker 单测 `10 passed`；`make check`（含 ruff、format、mypy）`1061 passed, 111 skipped, 2 xfailed`；`M1_DURABLE_POSTGRES=1 .venv/bin/python -m pytest tests/integration/test_m1_durable_state_postgres.py -q` `38 passed`。55431 已被其他 worktree 的 PostgreSQL 进程占用，本 worktree 未停止或接管该进程；定向 PG 测试在该现有实例上通过。无真实模型调用、无产品验收或 feature passes 结论。

## 追加（2026-09-20）：PR #30 最新 review findings 收尾

- **P1：迟到结果不得参与模型计划校验（`4057312360`）**：采纳并修复。`DurableStore.rebuild()` 对 `status='late_result'` 的不可变历史行跳过 `_tool_plan()` 校验；模型步骤仍保持原有 fail-closed 校验。新增 `test_rebuild_ignores_late_result_payload_shape`，覆盖 `{"tool_calls": 0}` 这类合法任意结果，避免后续重建被污染历史阻断。
- **P2：claim 后刷新失败必须释放租约（`4057312362`）**：采纳并修复。`Worker.resume()` 的第二次 `recover()` 抛出 `PersistenceError` 时，对同一 owner/epoch/generation 执行 best-effort `abandon()`，原始刷新错误原样抛出；释放失败不会覆盖原始错误。新增两个确定性单测，分别覆盖释放成功与释放自身失败。
- **P2：恢复被位移的人控 PG 覆盖（`4057312364`）**：采纳并修复。恢复原先被 worker 测试替换的 9 个 `DurableStore` 集成测试，覆盖取消后新 Run、迟到结果历史/命名空间、旧代际与幂等、并发 follow-up/cancel 等控制语义；另新增 1 个 late-result rebuild 回归。
- 工作区/验证：分支 `feature/m1-01-restart-recovery`，55431 由其他 worktree 持有，未停止或接管。`ruff check`、`ruff format --check`、`mypy` 全绿；worker 定向单测 `12 passed`；`M1_DURABLE_POSTGRES=1 .venv/bin/python -m pytest tests/integration/test_m1_durable_state_postgres.py -q` `48 passed`；最终 `make check` `1061 passed, 111 skipped, 2 xfailed`。无真实模型调用、无产品验收或 feature passes 结论。

## 追加（2026-09-20）：最终 review findings 收尾

- P1 executor 异常：`RecoverySession.execute_pending()` 在工具 callback 抛异常时 best-effort `abandon()` 当前 owner/epoch/generation lease，原始异常保持不变，释放失败不覆盖它；新增两条 worker 单测。
- P2 deadline：`DurableStore.claim()` 的初始 `lease_until` 使用数据库时钟与 Run deadline 的 `LEAST` 封顶，续租与初始领取保持同一绝对期限边界；更新 PG deadline 回归。
- P2 记录同步：当前 main 已含 PR #35，续租 wiring 在本分支可用；旧的“#35 未合并/能力缺失”文字保留为历史背景，不再作为当前状态。
- 本轮最终验证：worker 单测 `12 passed`；M1 DurableStore PG 定向 `48 passed`；全量 `make check` `1061 passed, 111 skipped, 2 xfailed`。55431 由其他 worktree 持有，未停止或接管。

## 追加（2026-09-20）：PR #30 P1/P2 最新 findings

- **P1：恢复工具 executor 异常的 lease 清理（`4057446902`）**：采纳并修复。`RecoverySession.execute_pending()` 只捕获 executor callback 的退出路径，best-effort 调用同一 `Lease` 的 `store.abandon()`，随后用裸 `raise` 保留原异常；abandon 失败也不覆盖原异常。未把外部异常伪装成成功或吞掉。新增 worker 单测覆盖释放成功及释放失败仍保留原异常，且断言没有 renew/commit。
- **P2：初始 claim 的 deadline 封顶（`4057446908`）**：采纳并修复。`DurableStore.claim()` 的持久 SQL 使用 PostgreSQL `LEAST(clock_timestamp()+lease_interval, deadline)`，避免应用时钟误差和 deadline 前的 420 秒超额租约；更新 PG 回归断言 claim 直接得到 `lease_until == deadline`，并保留 deadline 到期后的 `DEADLINE_EXCEEDED` 语义。
- **P2：续租任务记录状态（`4057446911`）**：采纳并修复。明确 `origin/main` 已含 PR #35（`76293b0`，实现 `91451d9`），当前 `renew_lease` 已存在且 wiring PG 测试在显式 `M1_DURABLE_POSTGRES=1` 下可运行；历史临时合并与恒跳过描述已标注为历史，不伪称当前 wiring 已运行。本次验证实际结果以命令输出为准。
- 本轮没有真实模型调用、产品验收或 feature passes 结论；55431 若由其他 worktree 占用，不停止或接管其 PostgreSQL 进程。
- 最终本地验证（当前工作树，2026-09-20）：`PYTHONPATH=. .venv/bin/pytest -q tests/test_worker_recovery.py` 为 `14 passed`；显式复用已由其他 worktree 持有的 55431 PostgreSQL（未停止/接管），`M1_DURABLE_POSTGRES=1 PYTHONPATH=. .venv/bin/pytest -q tests/integration/test_m1_durable_state_postgres.py tests/integration/test_m1_lease_renewal_postgres.py tests/integration/test_m1_lease_renewal_wiring_postgres.py` 为 `67 passed`，wiring 未跳过；`make check` 为 `1065 passed, 121 skipped, 2 xfailed`，其中默认未设置 PG opt-in，wiring 的 2 项按显式 PG 条件跳过。`ruff check`、`ruff format --check`、`mypy` 均由 `make check` 覆盖并通过。

## 追加（2026-09-20）：PR #30 最新 P2 持久化失败与工具结果校验

- **P2：executor 后续持久化失败释放租约（`4057494822`）**：采纳并修复。`RecoverySession.execute_pending()` 将续租和 `commit_tool()` 纳入同一 best-effort `abandon()` 清理路径；释放失败不会遮蔽原始 `TIMEOUT`/`RETRY`/`STORAGE_UNAVAILABLE` 等持久化错误。新增确定性 worker 回归，分别覆盖续租失败、提交失败及释放失败仍保留原异常。
- **P2：live step 的畸形 `tool_results` fail closed（`4057494824`）**：采纳并修复。`DurableStore.rebuild()` 在构造 `pending_tools` 前验证结果为 list、每项为 mapping、ordinal 为非 bool 的合法 int（范围内且无重复），并拒绝缺失/非 mapping result；`{}`、scalar、非 mapping 项统一抛出 `PersistenceError("INCONSISTENT_STATE")`。新增 PG 回归覆盖这些损坏形状，并保留现有工具计划/代际过滤语义。
- 当前验证：worker 定向 `16 passed`；复用其他 worktree 持有的 55431 PostgreSQL（未停止/接管），M1 durable + lease renewal + wiring 定向 `68 passed`；`make check` 为 `1067 passed, 122 skipped, 2 xfailed`，`ruff`、格式、`mypy` 均通过。无真实模型调用、产品验收或 feature passes 结论。

## 追加（2026-09-20）：当前 HEAD 新增 P2 findings

- **P2：续租/提交失败后的 lease 清理（`4057494822`）**：采纳并修复。executor 成功但 `_renew()` 或 `commit_tool()` 抛出持久化/控制异常时，`execute_pending()` 对同一 lease best-effort `abandon()`，原始异常原样抛出；新增 renew rejection 与 commit failure 回归断言，释放失败不覆盖原始错误。
- **P2：持久 `tool_results` fail-closed（`4057494824`）**：采纳并修复。`rebuild()` 对活模型步骤要求 `tool_results` 是列表，每项是带唯一、非负、范围内整数 ordinal 和 mapping result 的记录；malformed 值返回 `INCONSISTENT_STATE`，不把 falsey 值当空列表而重放外部查询。新增 PG 回归覆盖非列表、非法 ordinal、缺失/错误 result。
- 最新本地验证：worker 定向 `15 passed`；55431 复用的 M1 durable/renewal/wiring `68 passed`；`make check` `1066 passed, 122 skipped, 2 xfailed`；ruff/format/mypy 全绿。无真实模型调用、产品验收或 feature passes 结论。
