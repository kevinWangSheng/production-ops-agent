# M1-01 调查 loop 长程执行边界改造

- 状态：**实现完成，独立实现审查的 2 个 P1 / 4 个 P2 已全部修复并复验；分支已推送，PR 待 #29 合并后 rebase 到 main 创建**
- 更新日期：2026-09-21
- 前序：[Flash 调查 loop 任务记录](2026-09-16-m1-01-investigation-loop.md)、PR #29（`9f3506f`，`CLEAN`，待用户审核合并）
- 依据：SPEC 有界开放 M1-01；PRODUCT-CONSTRAINTS；C3 §5/§7/§13；ADR-0002/0003/0004；
  v4 冻结包 B2 段；[上游对标调研](../research/upstream-agent-loop-benchmark-2026-09-21.md)；
  [设计草案（含独立审查与实施差异）](../design/investigation-loop-long-horizon-2026-09-21.md)
- 用户决定（2026-09-21）：PR 策略 A（合并 #29 后新 PR）；压缩「按参考的来」→ HolmesGPT 两段式（单结果 stub + LLM 摘要 compaction）
- 工作区：worktree `/Users/shenghuikevin/dev/AI/production-ops-agent-loop-long-horizon`，
  分支 `feature/m1-01-loop-long-horizon`，起点 #29 头 `9f3506f`；本任务专属 PG：`tmp/m1-lh/postgres`，端口 55432

## 目标与范围

把 PR #29 的单 Run 内存 loop 改造成：从 PostgreSQL 业务行重建模型上下文并继续、可测量的上下文预算与
Holmes 式压缩、模型/工具/活跃时间/上下文预算分离且重启不重置、显式 handoff 与跨 Run 续接上下文构造器。
范围外：UI、intake、observer、postmortem、自动创建新 Run、修改 11 个 `passes`、修改 v4 冻结值、引入框架。

## 实现（提交 `cf56d46..HEAD`）

- `opspilot/investigation/limits.py`：`RunLimits` / `M1_FROZEN_LIMITS`；loop 的上限从常量变为参数，产品边界（runner）拒绝超冻结值。
- `opspilot/persistence.py`：`opspilot_runs.input` 输入快照；预留表 `reserved_seconds/seconds`（活跃时间先预留后结算）；`run_usage()`；`block(lease)`；`accept/new_run` 可选 `input`。
- `opspilot/investigation/store.py`：接缝增 `usage()`、seconds 参数、`MemoryStepStore.snapshot()`；`DurableStepStore` 每次物理请求前续租。
- `opspilot/investigation/context.py`（新）：`InvestigationInput`、步骤键 `{segment}:round-{n}`、`rebuild_transcript()`、估计器 + 校准、`visible_view()` stub、`fold_digest()`/`compaction_message()`、`context_policy_revision()`、`continuation_context()`。
- `opspilot/investigation/loop.py`：`run()/resume()` 共用 `_drive()`；逻辑轮与物理请求分开计数；活跃时间跨 attempt；每轮前 `_manage_context()`（Holmes 阈值公式；压缩失败或仍超预算 fail-closed）；终态统一提交 `kind=conclusion` 步骤。
- `opspilot/investigation/runner.py`（新）：`Worker.resume` → `execute_pending` → `rebuild_transcript` → `loop.resume` → `publish`；已发布/控制拒绝/不兼容/malformed 各有明确结果，malformed 落库 `blocked`。
- `scripts/m1_compaction_smoke.py`（开发脚本）：真实 provider 折叠冒烟。

## 验证证据

| 项 | 命令 / 工件 | 结果 |
|---|---|---|
| 静态 + 单元 | `make check`（审查修复后 HEAD） | `ruff` 全过、`mypy` 31 文件无问题、`1640 passed, 163 skipped, 2 xfailed` |
| 确定性恢复 | `tests/test_m1_investigation_context.py`（20 条） | 超 4 逻辑轮、5 个断点重启、代际丢弃、撤权 stub、malformed fail-closed、活跃时间跨 attempt |
| 确定性压缩 | `tests/test_m1_investigation_compaction.py`（13 条） | 阈值触发、字节级重建、压缩后重启、摘要失败 handoff、不足两槽 `CONTEXT_EXHAUSTED`、摘要仍超预算、计费、stub、校准、策略哈希 |
| 跨 Run 续接 | `tests/test_m1_investigation_continuation.py`（3 条） | 后继 Run 引用前 Run 证据并完成、越权证据不携带、fail-closed |
| PG 集成 | `M1_DURABLE_POSTGRES=1`，DSN 改写到 55432：`test_m1_loop_resume_postgres.py`（15 条）+ 既有 4 个 M1 PG 套件 | `109 passed`（含既有 94 条无回归） |
| 真实 provider 冒烟 | `.venv/bin/python -m scripts.m1_compaction_smoke` | 2 次 HTTP 200、`deepseek-flash`、带 tools + thinking；`prompt 3422 / completion 2147` tokens；费用上界见 `docs/evidence/m1-01-loop-long-horizon/compaction-smoke.json`；估计 vs 实测校准因子已记录 |

不是产品验收：11 个 `passes` 未改；未做真实 Run 全链路；worker 进程级 kill 只在既有 `test_worker_subprocess_kill_then_resume_from_business_rows` 覆盖 claim 后一点，其余断点用进程内异常 + 租约过期等价模拟。

## 独立实现审查（2026-09-21）

全新上下文子代理审查 `9f3506f..HEAD`，结论「修复后可合并」；2 个 P1（压缩吞掉报告槽、已提交报告崩溃后被改写为预算耗尽）、
4 个 P2（被拒计划被 recovery 执行、旧代际 conclusion 永久拒绝 publish、rebuild 不比对哈希、字节级测试不真）全部采纳修复，
每项配先红后绿用例；处置表见设计文档第 13 节。

## 未完成 / 后续

- 审查 P3 后续项：`MODEL_REJECTED` 按超时上界计活跃时间（过保守）；`DEADLINE_EXCEEDED` 后无可落库终态；
  runner 不校验 `Worker.versions` 是否含 `context_policy_revision`；dropped 组只在内存 `Transcript`；
  `executor_factory` 合同未写明须从工具 ledger 回填 `tool_seconds_used`。
- #29 合并后：rebase 到 main、推送、开 PR（含 PR 描述：覆盖/未覆盖、权限/费用/兼容性）。
- 供应商余额差记账（2 次冒烟请求）。
- 跨 Run 自动接续、UI 展示 compaction/handoff、`opspilot_inputs` 追问通道接入 transcript 均不在本 PR。
