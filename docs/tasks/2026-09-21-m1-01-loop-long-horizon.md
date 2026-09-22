# M1-01 调查 loop 长程执行边界改造

- 状态：**实现完成；独立审查 2 P1 / 4 P2、机器人第一轮 4 条 P1、第二轮 1 P1 / 2 P2 全部采纳修复并复验；PR #29 待最新提交 CI 通过后即「PR 已就绪，待用户审核合并」**
- 更新日期：2026-09-22
- 前序：[Flash 调查 loop 任务记录](2026-09-16-m1-01-investigation-loop.md)、PR #29（`9f3506f`，`CLEAN`，待用户审核合并）
- 依据：SPEC 有界开放 M1-01；PRODUCT-CONSTRAINTS；C3 §5/§7/§13；ADR-0002/0003/0004；
  v4 冻结包 B2 段；[上游对标调研](../research/upstream-agent-loop-benchmark-2026-09-21.md)；
  [设计草案（含独立审查与实施差异）](../design/investigation-loop-long-horizon-2026-09-21.md)
- 用户决定（2026-09-21）：改造直接算在 #29 内（一个 PR，整体完成后合并；此前的 A 方案作废）；压缩「按参考的来」→ HolmesGPT 两段式（单结果 stub + LLM 摘要 compaction）
- 工作区：worktree `/Users/shenghuikevin/dev/AI/production-ops-agent-loop-long-horizon`，
  实现在 `feature/m1-01-loop-long-horizon`（起点 #29 头 `9f3506f`）完成后快进到 `feature/m1-01-investigation-loop`；本任务专属 PG：`../production-ops-agent-loop-long-horizon/tmp/m1-lh/postgres`，端口 55432

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
| 静态 + 单元 | `make check`（机器人第二轮修复后 HEAD） | `ruff` 全过、`mypy` 31 文件无问题、`1646 passed, 165 skipped, 2 xfailed`，另 1 条 `tests/test_m0_adapters.py::test_physical_timeout_retains_unknown_and_no_tools`（M0 适配器 10ms 物理超时竞态）在本机 main `b483a12` 上同样失败，与本改动无关，以 CI 为准 |
| 确定性恢复 | `tests/test_m1_investigation_context.py`（24 条） | 超 4 逻辑轮、5 个断点重启、代际丢弃、撤权 stub、malformed fail-closed、活跃时间跨 attempt、截断回复/被拒计划在 resume 上与在线路径同判 |
| 确定性压缩 | `tests/test_m1_investigation_compaction.py`（13 条） | 阈值触发、字节级重建、压缩后重启、摘要失败 handoff、不足两槽 `CONTEXT_EXHAUSTED`、摘要仍超预算、计费、stub、校准、策略哈希 |
| 跨 Run 续接 | `tests/test_m1_investigation_continuation.py`（4 条） | 后继 Run 引用前 Run 证据并完成、越权证据不携带、二次交接保留继承证据、fail-closed |
| PG 集成 | `M1_DURABLE_POSTGRES=1`，DSN 改写到 55432：`test_m1_loop_resume_postgres.py`（16 条）+ 既有 4 个 M1 PG 套件 | `111 passed`（含既有 94 条无回归；新增迟到结果按 epoch 分键 1 条） |
| 真实 provider 冒烟 | `.venv/bin/python -m scripts.m1_compaction_smoke` | 2 次 HTTP 200、`deepseek-flash`、带 tools + thinking；`prompt 3422 / completion 2147` tokens；费用上界见 `docs/evidence/m1-01-loop-long-horizon/compaction-smoke.json`；估计 vs 实测校准因子已记录 |

不是产品验收：11 个 `passes` 未改；未做真实 Run 全链路；worker 进程级 kill 只在既有 `test_worker_subprocess_kill_then_resume_from_business_rows` 覆盖 claim 后一点，其余断点用进程内异常 + 租约过期等价模拟。

## 独立实现审查（2026-09-21）

全新上下文子代理审查 `9f3506f..HEAD`，结论「修复后可合并」；2 个 P1（压缩吞掉报告槽、已提交报告崩溃后被改写为预算耗尽）、
4 个 P2（被拒计划被 recovery 执行、旧代际 conclusion 永久拒绝 publish、rebuild 不比对哈希、字节级测试不真）全部采纳修复，
每项配先红后绿用例；处置表见设计文档第 13 节。

## 机器人分诊（2026-09-22，`@codex review` 一次）

4 条 P1 全部采纳修复，一条发现一个提交，各配先红后绿用例，thread 已回复并 resolve：
`1075b04` runner 在 claim 后重读快照并校验 run_id；`5d25bcb` conclusion 键含控制代际、被取代的旧报告不再自动重新完成；
`f5c0328` 畸形字段的时间策略整条丢弃（F5 必填字段裁定不变）；`e0cde69` 超时后主动 shutdown HTTPS 连接。
修复后 `make check` `1643 passed, 164 skipped, 2 xfailed`；PG 全套 `110 passed`。

## 机器人分诊第二轮（2026-09-22，机器人对 `3e2a4ca` 自动复审）

1 P1 + 2 P2 全部采纳修复，一条发现一个提交，各配先红后绿用例，thread 已回复并 resolve：

| 发现 | 核实 | 处置 |
|---|---|---|
| P1 `loop.py:455` resume 用硬编码 `finish_reason="stop"` 解析已提交的尾轮文本，`length` 截断但恰好是合法 JSON 的回复会在重启后被接受 | 属实：行里持久化了 `finish_reason`，但 `Transcript` 未带出；同类还有被拒计划行（`rejected_plan`）在崩溃后会被当作新一轮 | `c345451`：`Transcript` 新增 `last_finish_reason / last_final / last_rejection`；resume 先按已提交裁决收尾（接受、末槽按解析原因失败、或按记录的拒绝原因失败），不再合成 finish reason |
| P2 `context.py:875` `continuation_context` 只从本 Run 工具结果行重建 bindings，二次交接丢失继承证据 | 属实 | `346ed5e`：先从投影后的前 Run context 以同一授权过滤播种继承 bindings，再合并新采集视图 |
| P2 `persistence.py:936` `late_result:step:<key>` 无 epoch，`ON CONFLICT DO NOTHING` 吞掉第二个被围栏尝试的物理回复 | 属实；tool / publish 的迟到键同类 | `5703f51`：三类迟到键追加 `:e{lease.epoch}`；同一尝试重放仍合并为一行 |

修复后 `make check` `1646 passed, 165 skipped, 2 xfailed`（另 1 条 M0 适配器超时竞态本机环境失败，见验证表）；PG 全套 `111 passed`。

## 未完成 / 后续

- 审查 P3 后续项：`MODEL_REJECTED` 按超时上界计活跃时间（过保守）；`DEADLINE_EXCEEDED` 后无可落库终态；
  runner 不校验 `Worker.versions` 是否含 `context_policy_revision`；dropped 组只在内存 `Transcript`；
  `executor_factory` 合同未写明须从工具 ledger 回填 `tool_seconds_used`。
- PR #29 描述已重写；两轮机器人分诊已逐项处置；等待最新提交 CI；合并仍走用户门。main 分支保护开启 required conversation resolution，未 resolve 的 thread 会使 `mergeStateStatus=BLOCKED`。
- 供应商余额差记账（2 次冒烟请求）。
- 跨 Run 自动接续、UI 展示 compaction/handoff、`opspilot_inputs` 追问通道接入 transcript 均不在本 PR。
