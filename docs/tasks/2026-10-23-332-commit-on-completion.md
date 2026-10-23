# #332 并行工具结果完成即提交（F2 断点③ 修复）

- 状态：实现完成，等待独立验收测试合入与断点③ 真实复跑
- 更新日期：2026-10-23
- 依据：issue #332；F2 断点③ FAIL（用户 2026-10-22 裁决不改 U7 判据）；[合同（预审全文与裁决）](2026-10-23-332-commit-on-completion-contract.md)；[#309 合同 r2](2026-10-22-309-loop-dedupe-parallel-contract.md)（末尾 r2 节）
- 工作区：实现 `/Users/shenghuikevin/dev/AI/poa-332`（`fix/332-commit-on-completion`，起点 main `200172c`）；独立验收测试 `fix/332-commit-on-completion-tests`

## 目标

`tool_parallelism=2` 时，一轮里 t1 先于 t0 完成，t1 的结果只在内存里等 t0；此时 worker 被杀，resume 会把 t1 重派一次（断点③ 的「重复外部查询 2 次」）。修复后，主线程一看到某条调用完成就持久提交它，kill 时已提交的调用不再重派。

## 合同（r1，用户 2026-10-23 裁决）

- U-1 A：并行路径里结果一完成就在主线程 `commit_tool`（含 evidence pin 与 `tool_committed` 事件）；`seen_tool_calls`、`delivered`/`evidence_ids`、tool 消息、`capture_delivery` 仍按 index。
- U-2：#309 合同改写为「持久提交按完成顺序，模型可见面按 index」，含 (a) 事件/late_result 可能乱序、(b) 异常时更高 index 可能已提交、(c) fence 后第一次 `CONTROL_DENIED` 可能在任意 index；已追加为 #309 r2。
- U-3：断点③ 的 harness kill 条件加「同一 step 已结算的兄弟调用都已出现在 `tool_results` 且稳定 1 s」，`committed_ops` 读实际 ordinal；毫秒窗口如实写入证据。
- R-1–R-8 可逆默认照做（合同第 2 节）。不改 context/messages/worker/runner/executor/progress/迁移/C3，不 bump revision。

## 执行进展与证据

- 实现：`_run_tools_parallel` 每次等待前后提交「已完成、未抛异常、index 高于当前轮、未提交」的结果（等待前也提交，因为等待集合是快照，快照之前完成的调用不会唤醒主线程——这个竞态由自测发现）；当前轮的结果已提前提交时只做模型可见面交付。`_deliver` 拆为 `_commit_durable` 与 `_deliver_visible`。`steps.py` 与 `MemoryStepStore` 写入时按 ordinal 排序。`docs/development.md` 的并行说明同步改写。
- 自测：`tests/test_332_commit_on_completion_impl.py` 4 例（兄弟在途时已提交且模型可见面与 cap=1 相同；提前提交被 `CONTROL_DENIED` 拒绝即停、无行；低 index 抛异常时高 index 已提交；每个 ordinal 只提交一次且按完成顺序）、`tests/test_332_commit_on_completion_postgres_impl.py` 1 例（PG 上 t0 在途时 ordinal 1 已落库，行排序为 `[0,1]`）。用 `git apply -R` 去掉 loop.py 修复后 5 例全部失败，恢复后通过。
- 调整：`tests/test_309_loop_parallel_impl.py` 的异常用例按 r2 (b) 允许 `[0]` 或 `[0,2]`；`tests/test_309_parallel_postgres_impl.py` 只改 docstring。
- 独立验收测试（`fix/332-commit-on-completion-tests` `86f30e8`）已 merge：`tests/acceptance/test_332_commit_on_completion.py` 4 例、`tests/integration/test_332_commit_on_completion_postgres.py` 1 例、#309 验收两处断言改写，未改断言即通过；去掉 loop.py 修复时内存版 3/4、PG 版 1/1 失败。
- 断点③ harness 改动与真实复跑未做（见待决）。

## 验证

- 临时 PG 17（56201，本 worktree `tmp/pg`，自建自停），`M1_DURABLE_POSTGRES=1 M0_STEP_POSTGRES=1`。merge 验收分支后：`tests/test_309_*`、`tests/test_332_*`、`tests/acceptance/test_309_*`、`tests/acceptance/test_332_*`、`tests/test_m1_loop*`、`tests/test_m1_investigation*`、`tests/test_m0_step_store_*`、`tests/integration/` 下 `test_309_parallel_crash_resume_postgres.py`、`test_332_commit_on_completion_postgres.py`、`test_m1_loop_resume_postgres.py`、`test_m1_durable_state_postgres.py`、`test_m0_step_store_postgres.py`：403 passed、0 skipped。ruff check/format、mypy 通过。未跑全量 make check。

## 待决

- U-3 harness（`scripts/f2_kill_resume.py`）在 PR #333，未进 main；#333 合并后 merge main 再改。
