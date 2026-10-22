# #309 调查循环：压缩后重置去重、同轮工具有限并行

- 状态：独立验收与审查通过；两次真实 Run 因 DeepSeek 余额为负未跑（draft PR）
- 更新日期：2026-10-22
- 依据：issue #309；C3 §5、§13（工具并行数 2）；`docs/research/upstream-design-audit-2026-10-22.md` 第二、三节；[合同 r1](2026-10-22-309-loop-dedupe-parallel-contract.md)
- 工作区：实现 `/Users/shenghuikevin/dev/AI/poa-309`（`feature/309-loop-dedupe-parallel`）；独立测试 `/Users/shenghuikevin/dev/AI/poa-309-tests`（`feature/309-loop-dedupe-parallel-tests`）

## 目标

压缩被接受后清空同工具同参去重集合；同一轮多个工具调用有限并行（默认 2，`OPSPILOT_TOOL_PARALLELISM`），按 index 提交，模型可见内容不变，cap=1 与改动前逐字节等价。

## 合同

预审：全新上下文 Claude Opus 5.5 子 Agent，只读，基线 `0b4b820`。U-1–U-5 与 R-1–R11 按推荐采纳（用户 2026-10-17 常设指示；默认 2 与 C3 §13 一致，不改 C3）。

## 执行进展与证据

- 实现（`feature/309-loop-dedupe-parallel`，本地提交，未推送）：`loop.py` 在被接受的压缩末尾清空 `seen_tool_calls`（U-5/R-1）；`tool_parallelism`（默认 1，1–16）>1 且一轮 ≥2 个调用时走 `_run_tools_parallel`：只有 `executor.execute` 进线程池，续租/fence 每次派发前一次、`commit_tool` 与 `delivered`/消息/`capture_delivery` 在主线程按 index（U-3/R-5）；同批重复按 R-2（首个先派发，后续到自己轮次按串行规则）；fence 失败停派发、提交前缀后交接；非预期异常提交更小 index 后原样抛出（R-8）。cap=1 与单调用不建线程池，串行路径只抽出帮助函数（R-4）。`executor.py` 计数加锁（R-6，秒数仍为总和，U-2）；`tracing.py` 增加 `carry()` 把 run span 带进工作线程、序号加锁（R-7）；`runner.py` 透传；`worker_main.py` 读 `OPSPILOT_TOOL_PARALLELISM`（默认 2，非法 `SystemExit`）；`scripts/m1_live_runner.py` 增加 `--tool-parallelism`（默认 1）与 `--context-tokens`（须在 (output_tokens, 冻结上限]），两值写入 ledger 与 summary。未改 `CONTEXT_POLICY`/`prompt_revision`/tool schema（R-10）。
- 实现者自测（非独立验收）：`tests/test_309_loop_parallel_impl.py` 18 项（重叠用 Barrier 证明、cap=1 不建池、cap1/cap2 提交内容与请求数相同、同批重复与纯拒绝重复、fence 中断、异常、压缩后重查真实派发——去掉清空语句时该项失败）；`tests/test_309_parallel_postgres_impl.py` 1 项（PG + otel-demo profile，两工具真实重叠，span 挂在 run 下，ordinal 0,1）。既有回归：28 个定向文件 1082 passed；PG 回归 5 个文件 118 passed；`ruff check`、`ruff format --check`、`mypy` 通过。
- 独立验收（Codex gpt-6-sol 起、容量不足后同会话续到 gpt-5.6-sol，未接触实现）：`tests/acceptance/test_309_loop_dedupe_parallel.py`、`tests/integration/test_309_parallel_crash_resume_postgres.py`。合入后按 lead 反馈改过四轮：比较两侧用同类 transport；fence 场景按新增 R-12 改为「续租失败后不再派发」并让调用真实在途；批内重复参数化 cap=1/2；补真实 PG 崩溃恢复（原为无条件 skip）。lead 两处脚手架修复（读 ordinal 取行、ruff format），断言未改。
- 合同补充 R-12（lead 2026-10-22）：cap>1 时第 i+1 个调用的续租可早于第 i 个的提交；续租或 fence 失败后不再派发，提交按 CONTROL_DENIED 停止。
- 独立审查（Codex，gpt-6-sol 起、续到 gpt-6.1-sol）：首审 3 条（R-8 异常后仍派发更高 index → `c685a18`；fence 测试无在途调用且有恒真断言、批内重复只测 cap=1 → 测试作者修）；复验发现纯拒绝重复未决时先派发更高 index（[0,2,3,1]）→ `3853c9e` 改为严格按 index 派发；二次复验无发现，可合并。
- 最终：#309 独立验收 + 实现自测 33 passed，PG 2 passed，定向回归 416 passed；全仓 `ruff check`、`ruff format --check` 通过。
- 未做：两次真实 Run（Run A cap=2 otel-demo、Run B `--context-tokens` 强制压缩）。2026-10-22T14:13Z DeepSeek 余额 −0.01 CNY，等用户充值。

## 待决

无。
