# #309 调查循环：压缩后重置去重、同轮工具有限并行

- 状态：合同 r1 已裁决，实施中
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
- 未做：两次真实 Run（Run A cap=2、Run B `--context-tokens` 强制压缩）由 lead 在审查后安排；独立验收测试在另一 worktree。

## 待决

无。
