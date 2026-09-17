# M1-01 子任务「Flash 调查 loop」

- 状态：新增提交待推送 + CI/机器人审查，之后回到「PR 已就绪，待用户审核合并」（[PR #29](https://github.com/kevinWangSheng/production-ops-agent/pull/29)）
- 更新日期：2026-09-17
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

## 追加（2026-09-17）：模型请求预留的结算（ui-e2e 交接项 1 → storefix2 B）

- 判定：C3 §13「预算在 PostgreSQL 原子预留和结算，未知费用保持占用」；M0 lab `scripts/m0/budget.py` 已有 reserved/settled/unknown 三态先例；
  产品表 `opspilot_runs` 建了 `budget_reserved/spent/unknown` 三列但无任何结算路径（完成的 Run 停在 reserved=N/spent=0）。
  结论：合同要求结算但未实现。执行语义原本正确（上限判断求和三列，每请求预留 1），缺的是真实记账。未引入费用换算，只记次数；token 用量已在 step 载荷 `usage`。
- 修复（`b133cc0`，自集成分支 `c92feca` 移植，`settle_budget` 按 main 的内联租约栅栏写法）：
  `DurableStore.settle_budget(lease, reservation_id, outcome)`（spent/unknown；不释放；同结果重放 no-op、异结果 `IDENTITY_CONFLICT`；租约栅栏）；
  `StepCommitter.settle_budget`、`MemoryStepStore`（含 `budget_unknown`）、`DurableStepStore`；loop 每个物理请求在返回后 settle `spent`、抛 `ModelError` 后 settle `unknown`，
  `_settle` 只吞 `CONTROL_DENIED`（预留保持占用，由下一次被栅栏的写入记录迟到历史并停机）。
- 审查 P1（`4f0648a`）：本分支没有 PR #31 的 `g{gen}:e{epoch}:` 轮次键前缀，新 attempt 重新领取同一 Run 时 `round-1#a1` 的预留 id 与死掉的 attempt 相同，
  `unknown` 后再 settle `spent` 会被判 `IDENTITY_CONFLICT`，使 C3 §7「有界重试」失败。修复：`DurableStepStore` 按租约 epoch 派生预留 id；
  死掉 attempt 的 `unknown` 预留保持占用。PG 用例 `test_a_reclaimed_run_settles_its_own_reservations_without_conflict`。
- 测试：PG `test_budget_reservations_settle_to_spent_or_unknown_and_never_release`；loop 单元 `test_every_physical_request_is_settled_as_spent_or_unknown`、
  `test_a_fenced_settlement_leaves_the_reservation_occupied_and_records_history`。
- 验证：`make check` → `All checks passed!` / `Success: no issues found in 26 source files` / `1288 passed, 77 skipped, 2 xfailed`；PG durable_state → `23 passed`。
- web 侧 `_EmittingCommitter` 的转发在 PR #33（`eec8dca`）。

## 追加（2026-09-17）：`prompt_revision` 接线 + P3-4 白名单投影

- 接手：执行者换为 Claude，通过 Herdr 面板接受调度者派发的修订任务书。接手时
  `git status`/`git diff` 为空——此前一个 Codex agent 因模型容量错误未能开工，
  未留下任何改动可沿用。
- 前置核查：读 AGENTS.md「项目目标与权限」「接手与执行」「验证与汇报」
  「独立审查与上下文交接」「变更、Git 与交接」、PRODUCT-CONSTRAINTS.md 全文、
  SPEC.md 门槛段/Operating constraints/Verification and delivery、
  C3 第 5 节「指令分层与版本」、红线审计
  （`.../scratchpad/reports/redline.md` 第 4 节 P3-4）。

### 事实核查（先于实现）

1. **`prompt_revision` 来源已是确定性计算，但与 #27 当前 HEAD 不一致。**
   ledger.json 的 `prompt-replay-candidate-017c81744c26` 确认由本分支
   `opspilot/instructions/discipline.py`（2026-09-16 从
   `origin/chore/instruction-contract-impl` 整体复制）的
   `prompt_revision("replay-candidate", report_contract=REPORT_CONTRACT)`
   算出，不是人工编号。但用 `git show
   origin/chore/instruction-contract-impl:opspilot/instructions/discipline.py`
   取 #27 当前 HEAD 重算同一变体，得到 `prompt-replay-candidate-2c26fd0db1e0`
   ——不同。原因：#27 已把 `template_projection()` 的覆盖面从「只投影
   `LAYER_TEMPLATE` 段」改成「投影全部 segment（L1a/L1b/L2 文字恒为空）」，
   修复一个真实缺口——C3 §5 明文要求「调整顺序」也要 bump revision，
   而只投影 L1a 时，挪动一个 L1b/L2 槽位的相对位置不会改变 revision，
   即便渲染字节已经变了。本分支这份**尚未合并**的复制件仍停在旧覆盖面。
   **未修：** 本分支未把该修复搬进本地 `discipline.py`。理由是 AGENTS.md
   工作区约定「#27 尚未合并，需要它的符号时不要复制实现」——现在这份差异
   正是当初整体复制 #27 实现的后果，再次搬运其后续修复只是重复同一问题。
   `replay-candidate` 变体当前没有实际发生过槽位重排，因此这个缺口暂不影响
   已产出的报告字节，但一旦 #27 合并，本分支的 `prompt_revision` 计算方式
   必须整体替换为 #27 版本（而不是逐行合并），届时该值会再变一次——这是
   预期的版本升级，不是本任务遗留的 bug。合并顺序仍为 #27 → #29。
2. **红线 P3-4 引用的过滤代码在本分支（#29 单独）不存在。** 审计对象是
   `integration/m1-01-full`（#29 × #31 × #33 的合并分支）；那条「顶层键
   `password/secret/token/authorization` 过滤、嵌套值不过滤」的代码经核实
   来自 #31/#33 叠加的 `opspilot_inputs`/`append_input` 通道，本分支
   `opspilot/investigation/`、`opspilot/tools/registry.py` 里都搜不到这段
   代码（`RESERVED_PARAMETERS` 是工具参数名保留清单，用途不同，不是同一
   机制）。本分支当前唯一会把**调用方提供的、结构未经产品校验**的 Mapping
   整体序列化进模型 prompt 的路径，是 `InvestigationRequest.evidence_context`
   （`loop.py` `run()` 里 `canonical(request.evidence_context)`），此前
   全程不过滤。按调度者要求实现的是这条路径上的白名单投影，为将来接上
   `opspilot_inputs`/`append_input` 预置防线，而不是「修复」一段本分支并不
   存在的代码。

### 实现

- `opspilot/investigation/loop.py`：新增 `prompt_revision_versions(variant_id=DISCIPLINE_VARIANT, *, report_contract=REPORT_CONTRACT) -> dict[str, str]`，
  唯一来源是 `discipline.prompt_revision`；`LoopOutcome.prompt_revision`
  改经此函数计算（值不变，路径统一）。`run()` 开头用
  `dataclasses.replace` 把 `request.evidence_context` 换成
  `evidence_context_projection(...)` 的投影结果，之后所有读取
  （prompt 消息、`_round`/`_run_tools` 里的引用校验）都只看投影后的值。
  刻意不做的事：不构造完整 `ModelProfile`（`provider`/`model`/
  `endpoint_mode`/`adapter_revision` 本分支没有真实计算来源，编造值比不建
  更差）；不计算 `tool_schema_revision`（C3 §5 表格里 `versions` 比对的
  另一半，属工具注册表/PR #20，不属本任务）。`opspilot/persistence.py`
  的 `accept`/`claim` 目前仍只被测试/M0 脚本调用，产品代码里没有真正创建
  Run 的调用点——那是 worker/service 层的接线，本任务范围之外
  （任务记录既有范围声明：「不改 UI、intake 接线」）。
- `opspilot/investigation/reports.py`：新增
  `evidence_context_projection()` 及配套字段白名单常量
  （`_CONTEXT_FIELDS`/`_VIEW_BINDING_FIELDS`/`_TARGET_CATALOG_ENTRY_FIELDS`/
  `_TIME_POLICY_FIELDS`/`_TIME_WINDOW_FIELDS`），字段集合逐一核对本文件
  既有读取函数（`context_target_catalog`/`delivered_from_context`/
  `eligible_time_policies`/`context_time_policy_ids`）与
  `opspilot/domain/intake.py` 的 `Target` DTO 得出，不是猜测。
- `opspilot/investigation/__init__.py`：导出 `prompt_revision_versions`
  （其余 reports.py 内部读取函数按既有约定不导出）。

### 测试

- `tests/test_m1_investigation_loop.py`：
  `test_loop_outcome_prompt_revision_ignores_the_run_instance_budget`
  （真实跑两次 loop，`model_requests=1` 与 `4`，`prompt_revision` 相同、
  `prompt_face_sha256` 不同）；
  `test_prompt_revision_versions_moves_with_the_l2_report_contract_text`
  （真实编辑 L2 文本使 revision 改变）；
  `test_evidence_context_projection_strips_nested_unlisted_keys`
  （`view_bindings`/`target_catalog`/`time_policies[].window` 三处嵌套
  注入的敏感键均被剥离，合法字段原样保留）；
  `test_evidence_context_projection_preserves_every_schema_required_field`
  （独立审查发现后新增，见下）；
  `test_loop_never_sends_nested_secret_bearing_keys_to_the_model`
  （端到端：真实跑 loop，断言送进 `ModelClient` 的字节里不含注入串）。
- `tests/integration/test_m1_durable_state_postgres.py`（PG，先红后绿）：
  `test_prompt_revision_content_change_blocks_an_in_flight_run`
  （编辑 L2 文本 → `store.claim()` 抛 `INCOMPATIBLE_STATE` →
  `rebuild()` 确认 run 变 `blocked`）；
  `test_prompt_revision_ignores_instance_values_so_reclaim_is_not_blocked`
  （同一 `prompt_revision_versions()` 结果下，租约到期后第二次 `claim()`
  正常拿到新 epoch，不被判不兼容）。

### 独立审查结果与处置

全新上下文 general-purpose 子代理审查（未继承本会话讨论，给定目标/约束/
待审 diff 路径/原始证据，未以实现者结论引导），核实方式含自行读码、自跑
`make check`、`git show` 取 #27 当前 HEAD 对比、grep 全仓确认 P3-4 过滤代码
在本分支不存在。结论与处置：

1. **`prompt_revision` 接线：判定「正确，测试到位」，无发现。**
2. **P3-4 投影：判定「机制正确，但白名单窄于本项目冻结的 v4 schema」——
   已采纳并修复。** 审查指出 `docs/evidence/m0-real-investigation/
   IncidentScenario.v4.schema.json`（`$defs.EvidenceContext`，
   `additionalProperties: false`）是权威契约，而实现时的白名单只抄了
   `reports.py` 现有读取函数实际用到的字段，遗漏了 schema 要求但当前代码
   还没人读的字段：顶层 `run_id`；`ViewBinding.view_hash`/`timing`；
   `TimePolicy.revision`/`integration_id`/`reference_rule`/
   `scope_revision`；以及 `target_catalog` 的 `ComposeTarget`/
   `IntegrationTarget` 两个变体（原实现只覆盖了
   `opspilot.domain.intake.Target` 对应的 Kubernetes 形状）。方向仍安全
   （丢字段不是漏字段），但会把一个完全合规的 v4 context 悄悄截断。
   已用 `Read` 直接核对 schema 原文确认字段清单，重写
   `opspilot/investigation/reports.py` 的五个白名单常量为 schema 三个
   Target 变体字段并集 + `run_id`/`view_hash`/`timing`/`revision`/
   `integration_id`/`reference_rule`/`scope_revision`，新增
   `_project_view_binding`/`_TIMING_FIELDS` 投影 `ViewBinding.timing`，
   并加测试 `test_evidence_context_projection_preserves_every_schema_required_field`
   对着 schema 全部必填字段构造一份合规 context 做“投影后应原样返回”的
   回归断言。`target_id`（登记表 wrapper）与 view_bindings 的 `target_id`
   兜底字段不在 schema 里，为兼容既有代码路径/测试保留。
3. **discipline.py 陈旧 `template_projection` 缺口：判定「暂缓修复的决定
   站得住脚，但应该更明确地写出来，不能只靠代码注释隐含」——已采纳，
   补充文档。** 在 `prompt_revision_versions()` 的 docstring 里新增
   「Known gap, not fixed here」段，显式点出缺口内容、为什么不在本分支
   修（避免复制 #27 尚未合并的实现）、以及当前无变体触发它。
4. 审查未自跑 PG 测试（时间限制，只读代码确认与既有
   `test_incompatible_versions_block_without_silent_resume` 模式一致）；
   本任务已自行用 `M1_DURABLE_POSTGRES=1` 跑过，见下方验证证据。
   `prompt_revision` 分叉审查用静态读码确认（两版
   `template_projection` 结构不同即可推出摘要必然不同），本任务另外
   实际执行两版计算得到 `017c81744c26` vs `2c26fd0db1e0` 两个不同值，
   证据更强，结论一致。

### 验证证据

- 检查对象：Python 3.12.13、pytest 9.1.1、ruff 0.16.6、mypy 2.3.1（同前）。
- `make check`（独立审查处置后复跑）→ `uv lock --check` 通过；
  `ruff check` All checks passed；`ruff format --check` 无需改动；
  `mypy` Success: no issues found in 27 source files；
  `pytest` **1299 passed, 83 skipped, 2 xfailed**。
- PG 定向测试（在扩白名单/补文档之前跑；这两处后续改动都不碰
  `prompt_revision_versions`/`DurableStore` 的行为，PG 用例覆盖的正是
  这两者，结论仍适用于最终代码）：本 worktree 专属 55431 端口一度被
  另一并行 worktree（`production-ops-agent-flake`）占用，等待其释放后
  `.venv/bin/python -m scripts.m0.postgres_lab start` →
  `M1_DURABLE_POSTGRES=1 .venv/bin/python -m pytest
  tests/integration/test_m1_durable_state_postgres.py -q` → 25 passed
  （含 2 条新测试单独验证通过）；`tests/integration -q` 同参数 → 29 passed,
  54 skipped（其余 PG 用例走不同环境变量，跳过属既有行为非本次改动引入）；
  完成后 `postgres_lab stop`，未使用 `M0_ENV_FILE`（本次不涉及真实
  DeepSeek 调用，无需读密钥）。

### 最终汇报

- 改动范围：`opspilot/investigation/{loop,reports,__init__}.py`、
  `tests/test_m1_investigation_loop.py`、
  `tests/integration/test_m1_durable_state_postgres.py`、本任务记录；
  未改 11 个 feature `passes`、验收步骤、SPEC 门槛陈述、依赖/锁文件。
- 未完成/已知限制（均已在上文写明，非本次遗漏）：
  1. `discipline.py` 的 `template_projection` 仍停在 #27 合并前的旧覆盖面
     （只投影 `LAYER_TEMPLATE`），合并 #27 时需整体替换而非合并，届时
     `prompt_revision` 会再变一次；
  2. `prompt_revision_versions()` 只覆盖 C3 §5 `versions` 比对里
     `prompt_revision` 这一半，`tool_schema_revision` 半边、完整
     `ModelProfile` 构造、以及产品代码里真正调用
     `DurableStore.accept`/`claim` 创建 Run 的调用点，均不在本任务范围
     （worker/service 层尚未实现，此前任务记录已声明范围外）；
  3. `evidence_context_projection` 的白名单已扩到匹配冻结 v4 schema 的
     每个必填字段，但 schema 本身未来若再演进，需要人工同步更新。
- 提交计划：两个逻辑变更分两次提交（`feat: ... prompt_revision`、
  `fix: ... P3-4`），推送后按 stacked PR 规则更新 PR #29 描述、
  对当前 HEAD 触发 `workflow_dispatch`（CI 不自动挂非 main-base PR）、
  等待 CI 与已有机器人审查通道；不自行合并。
