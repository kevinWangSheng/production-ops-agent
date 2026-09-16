# M1-01 子任务「Flash 调查 loop」

- 状态：PR 已就绪，待用户审核合并（[PR #29](https://github.com/kevinWangSheng/production-ops-agent/pull/29)）
- 更新日期：2026-09-16
- PR：https://github.com/kevinWangSheng/production-ops-agent/pull/29
  （stacked，base = `feature/m1-01-tool-executor` / PR #20）
- 依据：[M1-01 拆分](../evidence/m0-real-investigation/m0-exit-matrix.md)「Flash 调查 loop」；
  [C3 第 5 节](../design/technical-proposal-2026-09-07.md)「调查循环」「指令分层与版本」「上下文」；
  [v4 验收包](../testing/first-investigation-v4-2026-09-10.md)；
  [PRODUCT-CONSTRAINTS.md](../../PRODUCT-CONSTRAINTS.md)；
  [SPEC.md](../../SPEC.md) 有界开放 M1-01；
  B2 冻结上限见 ROADMAP 与 v4 校准段；
  纪律单一来源 [PR #27](https://github.com/kevinWangSheng/production-ops-agent/pull/27)
  `opspilot/instructions/discipline.py`；
  工具执行器 [PR #20](https://github.com/kevinWangSheng/production-ops-agent/pull/20)；
  DurableStore `opspilot/persistence.py`。
  相关验收：F3（证据可区分性），`passes` 保持 false。
- 工作区：分支 `feature/m1-01-investigation-loop`，
  worktree `/Users/shenghuikevin/dev/AI/production-ops-agent-m1-investigation-loop`，
  起点 `origin/feature/m1-01-tool-executor`（PR #20）。

## 目标与范围

实现 C3 第 5 节定义的调查 loop：v4 输入/输出绑定、模型工具调用与
`ToolOperation` 结果的消息配对、handoff/最终报告、冻结的每 Run 资源上限，
以及与 DurableStore 的 `reserve_budget` / `commit_step` / `commit_tool` 提交。

范围内：

- `opspilot/instructions/`：复用 PR #27 纪律模块，不另写拷贝。
- `opspilot/investigation/`：loop、L2 报告契约、配对、冻结上限、步骤提交接缝。
- 确定性单元/合同测试：消息配对、预算/deadline 拒绝、handoff。
- 一次有界真实 DeepSeek Run（ledger 记账），证明 loop 产出 v4 结构化报告。

范围外：

- UI、intake 接线（intake 合同只读参考 PR #21）。
- 不改 11 个 feature `passes`、验收步骤、SPEC 门槛陈述。
- 不扩展产品权限（只读、人工优先）。
- 不 `import scripts/`（产品代码）；M0 adapters/live 仅作参考。

## 前提与完成条件

- 前提：SPEC 已有界开放 M1-01；本 worktree 基于 PR #20；
  `make setup`、本地 PG 集成、少量真实 DeepSeek 调用（走 scripts/m0 合同/ledger）已获用户授权。
- 完成条件：
  1. 确定性测试覆盖消息配对、预算/deadline 拒绝、handoff。
  2. 至少一次有界真实 DeepSeek Run 产出 v4 结构化报告，有 ledger 记账。
  3. `make check` 通过并保留实际输出。
  4. 独立审查完成并处置发现。
  5. 提交、推送、按 stacked PR 规则开 PR（base 为 PR #20 分支，若已进 main 则 rebase 到 main）。

## 必要上下文

- C3 循环：`组装上下文 → 调用模型 → 提交完整响应和工具计划 → 执行并提交工具观察 → 下一轮`。
- 指令分层：L1a `discipline.py`；L2 本任务按
  [DeepSeek 参考](../design/deepseek-flash-prompt-tool-reference.md) 撰写；L3 工具面由调用方提供实例快照。
- 冻结上限（2026-09-13）：每 Run 4 HTTP / 20 工具；输出 16,384 tokens；
  HTTP 512KiB/2MiB；模型 360s / Run 1800s；单工具 30s / 累计 240s。
- `opspilot/tools/`：`ToolRequest` / `ToolOutcome` / `ReadOnlyToolExecutor`。
- `opspilot/persistence.py`：`DurableStore.reserve_budget/commit_step/commit_tool`。

## 执行进展与证据

- 2026-09-16：确认 worktree 与分支，阅读 SPEC 门槛 / Operating constraints /
  Verification and delivery、PRODUCT-CONSTRAINTS 全文、C3 第 5 节、v4 包与拆分表。
  工作区干净，HEAD 与 `origin/feature/m1-01-tool-executor` 一致。
- 复用 `origin/chore/instruction-contract-impl` 的 `opspilot/instructions/`
  与 `tests/test_instruction_discipline.py`，未另写 L1 拷贝。
- 实现 `opspilot/investigation/`：loop、L2 `m0-report-v2` 契约、配对、
  B2 冻结上限、Memory/Durable 步骤提交接缝、DeepSeek 薄客户端。
- 确定性测试：配对、预算/deadline 拒绝、handoff、v4 引用绑定
  （invented evidence_id / target_ref / time_scope）、truncated tool plan 不执行。
- `make check`（P2 修复后复跑）：见本记录验证节。
- 有界真实 Run：`scripts/m1_live_flash_loop.py`，证据
  [`docs/evidence/m1-01-investigation-loop/run.md`](../evidence/m1-01-investigation-loop/run.md)。
  2 HTTP、回报名 `deepseek-flash`、`m0-report-v2` incomplete/inconclusive + handoff，
  峰值上界 0.024617 CNY。fixture 工具，MemoryStepStore，不是 PG 集成。
- 独立审查（全新 explore 子代理）：P1 无；P2 三条已修复
  （v4 目标/时间窗绑定、client 对非 mapping tool_calls fail-closed、
  `finish_reason!=tool_calls` 不执行工具）。P3 ledger `run_id` 脚本已改，
  已记录的那次 Run 未重放。

## 验证证据

- 检查对象与版本：Python 3.12.13、pytest 9.1.1、ruff 0.16.6、mypy 2.3.1。
- 命令（worktree 根目录）：`make check`
- 结果：`uv lock --check` 通过；`ruff check` All checks passed；
  `ruff format --check` formatted；`mypy` Success: no issues found in 26 source files；
  `pytest` **1286 passed, 75 skipped, 2 xfailed**（第四轮机器人审查修复后）。
- 真实 Run 命令：`PYTHONPATH=. M0_ENV_FILE=<main .env> .venv/bin/python scripts/m1_live_flash_loop.py`
  stdout：`{"status": "completed", "handoff": true, "http_count": 2, "known_cost_cny_upper": 0.024617, "report_schema_version": "m0-report-v2", "handoff_reasons": ["INCOMPLETE_INVESTIGATION"]}`

## 最终汇报

- PR：https://github.com/kevinWangSheng/production-ops-agent/pull/29
- 本地 `make check`：1286 passed / 75 skipped / 2 xfailed。
- 真实 Run：2 HTTP，`m0-report-v2` incomplete + handoff，上界 0.024617 CNY。
- 独立审查 P2 三条已修；第一轮机器人审查 4 P1 + 1 P2：采纳 4 条并修于 `122862d`，拒绝「崩溃后续跑」一条（属重启子任务）。
- 第二轮机器人审查（覆盖 `fe30cd4`）：3 P1 + 4 P2，**7 条全部采纳**，修于 `ce9837a`：
  1. P1 每次物理重试重新 `reserve_budget`（控制/deadline 再核）
  2. P1 非 final 重试不得占用最后一格；剩余 1 次物理请求强制 final-report
  3. P1 步骤提交写入 `request_sha256` 与 `response_id`
  4. P2 非 list 的 `tool_calls`（`{}`/`""`/0/false）fail-closed
  5. P2 请求体积按实际 POST 字节（含 thinking / response_format / tool_choice）
  6. P2 live ledger 记录失败 HTTP（usage 为 unknown）
  7. P2 live 脚本生成新 `run_id` 并写入 scope（历史 ledger 不改写）
- 第三轮机器人审查（覆盖 `6dcf0dd`）：2 P1 + 1 P2，**3 条全部采纳**，修于 `2e9f5d3`：
  1. P1 v4 `target_refs` 按 `target_catalog` 的不透明键校验，不再用 registry id 顶替
  2. P1 每条 cited view 必须自带 `time_scope_refs`，fact 不得借用 context 里其它 policy
  3. P2 `request.run_id` 必须等于授权 `scope.run_id` 才预留预算
- 第四轮机器人审查（覆盖 `7f47d55`）：3 P1 + 1 P2，**4 条全部采纳**，修于 `d4e846d`：
  1. P1 step store 租约 `authorized_run_id` 必须等于请求 run
  2. P1 无 `target_id` 的 canonical catalog 在唯一授权目标时映射到该目标
  3. P1 新 view 的时间策略由资格推导（historical window / current freshness），不再因「只有一条 policy」整表贴上
  4. P2 无字符串 `id` 的 2xx 响应拒绝
- CI workflow 不对非 main base 的 PR 自动挂 checks；第四轮修复后对当前 HEAD `workflow_dispatch`。
- **此后停止扩范围，等待用户审核合并。**
- 未完成：intake/UI、PG DurableStore 集成、流式续接、tokenizer 上下文计数。11 个 `passes` 未改。
- 合并由用户审核后执行，本任务不自动合并。

## 下一步与交接

用户审核合并 PR #29。第四轮审查已闭环；不再主动扩范围。若 PR #20 先合进 main，再 rebase 到 main。

## 接手记录（2026-09-16，Codex）

执行者更换为 Codex。接手时工作区为 `/Users/shenghuikevin/dev/AI/production-ops-agent-m1-investigation-loop`，分支 `feature/m1-01-investigation-loop`，当前 HEAD `4551378c8f6655b897e39588fe2b7226c91b2a94`。PR #29 base 仍为 `feature/m1-01-tool-executor`，状态为开放；GraphQL 核查 reviewThreads 共 19 条，全部已 resolve，未发现最后一次推送后新增的机器人 thread。保持现有范围，等待用户审核合并；若基分支先合并 main，后续按约定 rebase、retarget base、复核 CI 与新 review threads。
