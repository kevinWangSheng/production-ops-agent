# `6fe6274` 最终 HEAD 全新上下文独立审查：query deadline

日期：2026-09-11  
受检 worktree：`/Users/shenghuikevin/dev/AI/production-ops-agent-m0-01`  
受检 HEAD：`6fe6274430d72ad26785c6705fce2293b039b4de`（`docs: align final task tip`）  
受检修复：`0a514a06860a5136b31902eb3292b37cbaed6bc6`（`fix: honor earlier query authorization deadlines`）

本审查从当前 HEAD 的全新上下文开始，仅检查较早 `scope.effective_query_deadline` 的保留及其与现有查询 guard、授权/期限文档的相容性。不修改代码、`feature_list.json`/passes、`SPEC.md` 或历史 evidence；没有运行模型、trace、OTel/Holmes 服务或 PostgreSQL。

## 依据和变更核对

- `SPEC.md:72-82` 要求证据来源/时间窗可检查，且工具/query timeout、预算、取消和人工控制受边界约束；`SPEC.md:201-213` 要求工具合同含绝对 query window/request deadline，并设置网关/数据源超时。
- `docs/testing/first-investigation-v4-2026-09-10.md:51-53` 明确授权绝对 deadline 及来源自身更早期限优先；这不是恢复历史 aggregate cap 的授权。
- `docs/design/technical-proposal-2026-09-07.md:203-213` 要求绝对查询窗口、request deadline、大小/错误边界和有限可终止调用。
- `docs/tasks/2026-09-09-m0-real-investigation.md:432` 将 `0a514a0` 记录为“scope 较早 `effective_query_deadline` 被覆盖”的修复，并保留 `SPEC gate not cleared`。

修复本身在 `scripts/m0_environment/holmes_baseline.py:480-485` 做了以下改变：

1. 从可信 scope 读取 `effective_query_deadline`，缺失时才回退 `PROFILE.deadline`。
2. 对值做 `(int, float)` 与 `math.isfinite` 校验，非法值在启动后续调查前拒绝。
3. 将有效值写回 `min(source_deadline, PROFILE.deadline)`，因此来源更早期限不会被 profile 较晚期限覆盖，而来源较晚期限仍被全局 profile 上界截断。

该值随后进入 `trusted_access_scope`/`configuration.json`（`holmes_baseline.py:832-835, 1120-1133`），GET 查询 guard 在 `holmes_baseline.py:694-705` 使用同一字段。就“传播并保留较早期限”这一修复目标，静态检查结论为通过。

## 可复现检查

以下均在受检 worktree 执行，未触发模型、trace、服务或 PG：

```text
.venv/bin/ruff check scripts/m0_environment/holmes_baseline.py scripts/m0_environment/round03.py tests/test_m0_holmes_round02.py tests/test_m0_question_paths.py tests/test_m0_question_scope_binding.py
All checks passed!

.venv/bin/ruff format --check scripts/m0_environment/holmes_baseline.py scripts/m0_environment/round03.py tests/test_m0_holmes_round02.py tests/test_m0_question_paths.py tests/test_m0_question_scope_binding.py
5 files already formatted

.venv/bin/python -m pytest tests/test_m0_holmes_round02.py tests/test_m0_question_paths.py tests/test_m0_question_scope_binding.py -q
81 passed in 1.85s

git diff --check 0a514a0^ 6fe6274 -- scripts/m0_environment/holmes_baseline.py ROADMAP.md docs/tasks/2026-09-09-m0-real-investigation.md
（无输出，退出码 0）
```

另以只读 AST/源码检查确认 `source_deadline` 的 finite 校验和 `min(source_deadline, PROFILE.deadline)` 位于 scope 校验路径，并记录了查询 guard/锁的实际行号：source 480、guard 704、`tool_io_lock` 707、`bounded_send` 717。

## 发现：较早期限在锁竞争下仍可能被越过（P2）

`guarded_send` 先在 `holmes_baseline.py:704-705` 检查当前时间，再在 `:707` 获取 `tool_io_lock`。获取锁后 `:708-712` 的 `remaining` 只取 `PROFILE.tool_seconds`、累计工具上限和 `run_stop`，没有再次检查 `scope["effective_query_deadline"]`，也没有把该期限纳入 timeout。于是：

1. 第一个 GET 持有 `tool_io_lock` 时，第二个 GET 可在期限前通过 `:704`；
2. 第二个调用等待锁期间若跨过较早的 scope deadline，取得锁后仍可计算正的 `remaining` 并进入 `bounded_send`（`:715-717`），在授权期满后发起 GET；
3. 即使没有锁等待，已在期限前开始的 GET 的 wall timeout 也只受 `run_stop` 限制，不能保证在来源更早的期限前结束。

这不是假设不存在并发：锁本身表明出站查询会竞争，而当前 pinned HolmesGPT `5e983c17f30e93099c7d775167266d4cd1d586c4` 的 `holmes/core/tool_calling_llm.py:1368-1395` 使用 `ThreadPoolExecutor(max_workers=16)` 并行提交同轮工具调用。项目技术方案也保留工具并行数 2（`docs/design/technical-proposal-2026-09-07.md:348-358`）。因此该路径是可达的授权边界缺口。

建议后续有界修复在取得 `tool_io_lock` 后立即再次检查较早 scope deadline，并将其作为 `remaining` 的最小值；本审查未实施该修改，也未把它改记为通过。

## 独立结论与边界

- **通过（局部目标）**：`0a514a0` 已保留并截断 scope 的较早 `effective_query_deadline`，不再无条件写成 `PROFILE.deadline`；非法非有限值 fail closed；现有 pre-lock 查询 guard 读取修复后的字段。
- **未通过（完整期限语义）**：在并行工具的 `tool_io_lock` 竞争下缺少 post-lock deadline recheck，且 query timeout 未受较早 scope deadline 限制；该 P2 需后续代码/回归测试处置后才能称为完整实现。
- 本记录仅代表当前 HEAD 的静态检查、源码交叉核对和 81 项离线定向回归；不证明真实模型/trace/OTel/服务/PostgreSQL 全链路或产品验收，`SPEC gate`、feature passes 和 M0/M1 决策保持原状态。
