# `d1b6d15` post-lock query deadline 全新上下文独立复验

日期：2026-09-11  
受检 worktree：`/Users/shenghuikevin/dev/AI/production-ops-agent-m0-01`  
受检 HEAD：`d1b6d15f4b21e596cd30e464ea7dfbc00ff4fcf9`（`fix: enforce query deadline after lock wait`）

本审查以全新上下文从当前 HEAD 开始，只读复验 `guarded_send` 的 post-lock
`effective_query_deadline` 修复，并回看此前 timing-sidecar/source guard 与
较早 scope deadline 两项修复。没有修改生产代码、`feature_list.json`/passes、
`SPEC.md` 或历史 evidence；没有运行模型、trace、OTel/Holmes 服务或
PostgreSQL。受检 worktree 在开始时已有未跟踪的
`round-03-final-head-review.md`，本文件为本审查新增记录。

## 依据与范围

- `SPEC.md:72-82` 要求证据时间窗可检查，并要求 query/tool timeout、预算、
  取消和人工控制保持边界；`SPEC.md:117-128` 要求交付验证区分静态、集成和
  真实运行证据。
- `docs/design/technical-proposal-2026-09-07.md:201-213` 要求工具合同包含
  绝对查询窗口和 request deadline，并设置 SDK、网关、数据源超时及可终止
  子进程。
- `docs/testing/first-investigation-v4-2026-09-10.md:51-59` 规定来源自身更早
  的授权 deadline 优先；候选上限不是恢复旧 aggregate cap 的授权。
- 前一份全新上下文审查
  [`round-03-final-head-review.md`](round-03-final-head-review.md) 已记录
  `0a514a0` 在锁竞争下缺 post-lock recheck 和 deadline timeout 的 P2；本次
  复验检查该缺口是否由 `d1b6d15` 闭合。

## 代码复验

### 1. scope deadline 传播（此前修复 `0a514a0`）

`scripts/m0_environment/holmes_baseline.py:480-485` 从可信 scope 读取
`effective_query_deadline`，缺失时才回退 `PROFILE.deadline`，对非有限值
fail closed，并写回 `min(source_deadline, PROFILE.deadline)`。因此来源更早
期限不再被 profile 较晚期限覆盖；来源较晚期限仍被 profile 上界截断。

### 2. post-lock 修复（当前 `d1b6d15`）

当前 `guarded_send` 的查询路径为：

1. pre-lock guard 在 `:704-705` 拒绝已过期 scope deadline；
2. 在 `:707` 获取共享 `tool_io_lock`；
3. 取得锁后于 `:708-710` 重新取 `now` 并再次拒绝已过期
   `scope["effective_query_deadline"]`；
4. `:711-715` 将 `scope["effective_query_deadline"] - now` 与单工具、累计
   工具及 `run_stop` 一起取最小值；
5. `:717-721` 在剩余时间不超过清理余量时拒绝，否则把该 `remaining` 传给
   `bounded_send`。`bounded_send` 在 `:667` 将同一剩余时间减去 4 秒作为
   transport timeout，并在 `:672` 作为 child wall deadline。

这闭合了前审查指出的可达路径：并行 GET 在锁上等待跨过较早 deadline 时，
取得锁后会在发出数据源请求前拒绝；未跨过 deadline 时，实际请求的 wall
timeout 也受该较早 deadline 限制。`now` 只在持锁后计算一次并同时用于
`remaining`，避免重新取时钟造成不一致。只读 AST 检查确认上述 recheck、
最小值项和 `bounded_send(..., remaining, ...)` 均位于同一个 `with
tool_io_lock` 块内。

### 3. 此前 timing-sidecar/source guard（`da505dc`）

`holmes_baseline.py:637-643` 在读取 `initial_timings_file` 前调用
`validate_source_path()`，再解析 JSON；`initial_evidence.py:38-63` 的 guard
拒绝敏感 basename、敏感目录、符号链接和非普通文件。该修复的历史完整检查
`round-03-timing-guard-final-make-check.txt` 保留为 **723 passed / 44 skipped**，
且本次定向 timing/strict 回归继续通过。

## 可复现检查

本次在受检 worktree 执行，均未触发模型、trace、服务或 PG：

```text
.venv/bin/ruff check scripts/m0_environment/holmes_baseline.py scripts/m0_environment/round03.py tests/test_m0_holmes_round02.py tests/test_m0_question_paths.py tests/test_m0_question_scope_binding.py tests/test_m0_timing_echo.py tests/test_m0_outcomes_v4.py
All checks passed!

.venv/bin/ruff format --check scripts/m0_environment/holmes_baseline.py scripts/m0_environment/round03.py tests/test_m0_holmes_round02.py tests/test_m0_question_paths.py tests/test_m0_question_scope_binding.py tests/test_m0_timing_echo.py tests/test_m0_outcomes_v4.py
7 files already formatted

.venv/bin/python -m pytest tests/test_m0_holmes_round02.py tests/test_m0_question_paths.py tests/test_m0_question_scope_binding.py tests/test_m0_timing_echo.py tests/test_m0_outcomes_v4.py -q
190 passed in 1.89s

git diff --check d1b6d15^ d1b6d15 -- scripts/m0_environment/holmes_baseline.py
(no output; exit 0)
```

提交随附的 `round-03-query-deadline-lock-final-make-check.txt` 记录了完整
离线检查：Ruff/format 通过，pytest **723 passed / 44 skipped in 21.20s**；
44 个 skip 均为显式 PostgreSQL/合成预算 opt-in。本次没有复跑该完整检查，
以避免重复较大范围检查；该输出仍是提交内可审计证据。

## 证据边界与残余覆盖

- 当前代码对 post-lock deadline 的控制流和 timeout 传播通过静态复验，前一
  份 P2 的具体越界路径已被修复。
- 当前定向 190 项回归覆盖 sidecar、scope/path、报告与 strict 合同，但仓库
  没有直接调用嵌套 `guarded_send` 并人为让 `tool_io_lock` 等待跨过
  `effective_query_deadline` 的确定性测试；`round-03-query-deadline-lock-`
  `final-make-check.txt` 也只是完整套件输出，并未新增该专项用例。因此，
  “代码路径已闭合”不等于已取得锁竞争运行时/真实数据源证据；建议后续在不
  触发模型、服务或 PG 的隔离 harness 中补一条 regression test，再将该专项
  运行证据归档。此为覆盖建议，不将当前修复误报为运行时或产品验收通过。
- `source_deadline` 使用 `(int, float)` 检查，Python `bool` 因继承 `int` 会
  被视为有限数值；`False`/`True` 只会得到 0/1 秒的更早 deadline，属于
  fail-closed 行为，不构成放宽授权，但若未来收紧 schema 可增加显式拒绝
  bool 的回归。

## 独立结论

**通过（本次代码目标）**：`d1b6d15` 在共享工具 I/O 锁取得后重新检查较早
`effective_query_deadline`，并将其纳入 query timeout；此前 `0a514a0` 的
较早期限传播和 `da505dc` 的 timing-sidecar/source guard 仍保持。未发现新的
P1/P2，也未发现权限、数据出口、预算授权、feature passes 或 SPEC gate 被
放宽。

**边界**：专项锁竞争运行时回归尚未存在，本结论是源码/AST 与离线确定性套件
复验，不是模型、trace、服务、PostgreSQL 全链路或生产证明。M0 partial/unknown、
`SPEC gate` 的既有 `not cleared` 状态、feature passes 和 PR 用户审核/合并
边界保持不变。
