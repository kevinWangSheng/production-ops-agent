# M1-01 子任务「Flash 调查 loop」

- 状态：`facb64d` 推送后机器人先后共追加 7 条新发现，已逐条先红后绿修复并提交（`7eea577`/`bab8941`/`8f9688c`/`0c648fe`/`bad4ab6`/`b89d10d`/`929ee54`），三轮独立审查均确认全部「正确、最小」；`make check`/PG 定向全绿，已推送、CI 通过、30 条 review thread 全部回复处置并 resolve，`mergeStateStatus=CLEAN`；base 分支 PR #20 持续前进但未变 DIRTY/CONFLICTING，未执行合并；不手动触发 `@codex review`；等待用户审核合并（[PR #29](https://github.com/kevinWangSheng/production-ops-agent/pull/29)）
- 更新日期：2026-09-18
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
- 提交：三个逻辑变更分三次提交——`519c108`（`feat: ... prompt_revision`）、
  `4ed021f`（`fix: ... P3-4`）、`692cf3e`（`docs: ...`），已推送。对当前
  HEAD `workflow_dispatch` 触发 CI（非 main-base PR 不自动挂 checks），
  `checks`/`m0-postgres` 均通过（run 35255856858）。
- **机器人审查（第五轮，`chatgpt-codex-connector`，2026-09-17T16:47:31Z，
  锚定在改动前的 `814dd2d`）**：GraphQL 核查 reviewThreads 共 23 条，
  接手时 4 条未 resolve，均与本任务两项范围（`prompt_revision` 接线 /
  P3-4 白名单）无关，是本 PR 更早既有代码（`delivered_from_context` 的
  run_id/v4 类型绑定、`eligible_time_policies` 的目标覆盖与新鲜度区间
  判定、`_settle`/`_commit_step` 对 `CONTROL_DENIED` 的历史记录丢失）
  的独立发现。逐条判断为「看起来成立，但超出本次任务范围」，在 PR 上
  逐条回复拒绝理由并 resolve（4 条 comment id：4039957732、4039960100、
  4039962359、4039963491）；不在本任务里修——同时修 4 条无关且未经独立
  审查的逻辑改动会明显扩大本次变更的风险面。resolve 后 23 条全部
  已处理，`mergeStateStatus=CLEAN`、`mergeable=MERGEABLE`。
  **这 4 条对本 PR 仍是真实、未修的发现，需要一个新任务接手**（不是本
  任务的遗留，而是本 PR 累积的既有缺口，本次只是碰上并处置了流程）。
- 不自行合并；等待用户审核。

## 追加（2026-09-17 续）：处置上一轮 4 条机器人发现 + 模型请求 wall clamp

调度者用 `.../scratchpad/briefs/loop-followup.md` 明确授权修复上一轮判定为
「看起来成立但超范围」并 resolve 的 4 条机器人发现（comment id
4039957732、4039960100、4039962359、4039963491），并追加
`.../scratchpad/reports/lease-wire-33.md` 第 6 节指出的模型请求 wall clamp
缺口。要求：逐条先写确定性红测试、最小修复、方向只能更严不得放宽已有拒绝、
一条发现一个独立提交；若复现后判定不成立，写证据不修并在 PR thread 补说明。

### 逐条处置

1. **`delivered_from_context` 缺 run_id/v4 类型绑定（`82218ae`）**：复现确认——
   伪造/跨 Run 的 context 会被当作已交付证据接受。`delivered_from_context`
   新增 `run_id` 必填参数，`type`/`run_id` 任一不匹配即整体丢弃该 context
   （不做部分信任）。`loop.py` 调用点传入 `request.run_id`。受影响的既有
   测试夹具（`test_v4_opaque_target_refs_are_validated_via_catalog` 等 6 处）
   补齐 `run_id` 字段——这是让夹具符合它们本就隐含的契约，不是放宽任何断言。
2. **`eligible_time_policies` 对空 `target_refs` 判定过宽（`23c7158`）**：
   复现确认——`named and named.isdisjoint(...)` 对空/缺失 `target_refs`
   恒假，导致「显式不覆盖任何目标」的策略被当成「覆盖所有目标」。改为
   `all_authorized_targets` 非 True 时一律要求非空且相交的 `target_refs`。
   触发级联：`assemble()` 共享夹具与一处显式夹具的默认 time_policy 均缺
   `target_refs`/`all_authorized_targets`，靠旧 bug 覆盖到测试用的单一
   授权目标；两处均按其真实意图补 `all_authorized_targets: true`（这些
   测试从未打算测目标范围收窄，是关于 catalog 映射/工具执行流程的）。
3. **`current` 策略新鲜度判定未拒绝负值/未来时间戳（`faf80bc`，部分修复，
   已如实标注）**：复现确认负值子问题——`float(freshness_seconds) > max_age`
   对负值恒假，未来时间戳（负 age）被当成最新鲜。已修：改为
   `0 <= freshness_seconds <= max_age`。**未修并有证据**：发现另一半
   「应校验完整 source 区间」缺数据基础——`opspilot/tools/executor.py`
   的 `TransportResponse`/view 目前只有单点 `data_as_of`
   （`freshness_seconds = observed_at - data_as_of`），全仓搜不到任何
   `source_start_at`/`source_end_at` 字段。补这类字段是跨 PR 的 schema
   变更（`opspilot/tools/` 属 PR #20，需要它自己的设计评审），不是本任务
   能在 `reports.py` 内做的「最小修复」。已在提交信息与本记录写明，PR
   thread 需补一条对应说明（见下「后续动作」）。
4. **`_settle`/`commit_step` 丢弃围栏期回复（`871e628`）**：复现确认——
   `commit_step` 对 CONTROL_DENIED 直接 raise，模型回复的
   content/usage/response_id 完全不落盘，不同于 `publish()` 已有的
   late_result 语义。修复：`DurableStore.commit_step()`
   和 `MemoryStepStore.commit_step()` 在围栏时都先记 late_result
   （`MemoryStepStore.late_results` 列表 / PG `status='late_result'` 行）
   再报 `CONTROL_DENIED`。PG 侧关键坑：若在 `with self.transaction()` 块
   内部直接 raise，插入会随异常一起被回滚（这也是 `publish()` 用 `return
   False` 而不是 raise 的原因）——改为块内只置 `fenced=True`，块外再 raise，
   插入才能真正提交。用真实 PG 测试
   （`test_a_fenced_model_reply_is_retained_as_late_result_history`）
   验证过这个坑：先按「块内直接 raise」的写法跑，late_result 行数为 0，
   改成块外 raise 后变 1。
5. **模型请求 wall clamp（`ff8e17b`）**：`opspilot/investigation/client.py`
   的 `DeepSeekClient` 增加可注入 `clock`/`opener`（均可选，默认真实实现，
   现有唯一调用点 `scripts/m1_live_flash_loop.py` 不受影响，顺手接上已有
   `clock` 保持一致），`_read_capped` 按累计 wall-clock 时间（而非每次
   `read()` 各自的 socket 超时）在发起下一次 `read()` 前判定是否超出
   `call.timeout_seconds`（该值已是
   `min(360s 冻结上限, 剩余 deadline, 剩余 wall)`）。新增 `MonotonicClock`
   协议（只要求 `monotonic()`），比复用 `opspilot.tools.executor.Clock`
   窄——这个客户端不做任何 lease/deadline 判定，没有理由需要
   `now()`，而 `opspilot/` 产品代码的 `datetime.now()` 被 ruff `TID251`
   规则禁用（须用数据库时钟）。

### 独立审查发现并已修的残余缺口（`8b65c13`）

首次实现（`ff8e17b`）的 wall 检查只在发起下一次 `read()` 前拦截，独立审查
（全新上下文子代理）指出：单次已在飞行中的 `response.read(65536)` 调用
本身仍可能远超 `deadline`——`http.client.HTTPResponse.read()` 实际调用
`self.fp.read(amt)`，而 `io.BufferedReader.read(n)` 会在**内部**反复读
底层 socket 直到凑满 `n` 字节才返回，中途没有机会跑到我的判断分支。
审查用一个每次内部读只吐 1 字节、带 0.05s 延迟的合成 `BufferedReader`
实测：单次 `.read(65536)` 跨 201 次内部读耗时 11.3s，全程零次外层判断
机会——代码注释「deadline 下面的判断真正约束了总时长」原话确认属过度
声称。已核实并采纳：新增 `_tighten_socket_deadline`，在每次 `read()`
前（不管外层第几次迭代）把 `response.fp.raw._sock`（真实
`http.client.HTTPResponse.__init__` 里 `self.fp = sock.makefile("rb")`
的这条链路，已用 `socket.socketpair()` 实测核对属实）的 socket 超时收紧
到剩余预算，直接约束内层循环而不只是外层。够不到这条属性链（测试替身、
未来 stdlib 变化、非 socket 传输）时静默忽略，退化到仍然存在的外层
每次迭代判断，不崩溃。新增测试用带 `fp.raw._sock` 形状的假响应验证
`settimeout()` 调用序列严格递减；另加一条无该属性链的假响应回归测试，
确认退化路径仍能触发 wall clamp。

### 测试（均先红后绿，红态已用受控方式复现，非凭推断）

- 1/2/3：`tests/test_m1_investigation_loop.py` 新增/扩展的
  `eligible_time_policies`/`delivered_from_context` 直接单测（此前
  `eligible_time_policies` 全仓无任何直接测试，只被 loop 间接覆盖）。
- 4：内存态 `test_a_fenced_reply_after_settlement_is_retained_as_late_history`；
  PG 态 `test_a_fenced_model_reply_is_retained_as_late_result_history`
  （`tests/integration/test_m1_durable_state_postgres.py`）。
- 5：新文件 `tests/test_m1_investigation_client.py`，用「每次 `read()`
  推进一个 `FakeClock` 而不真的 sleep」的假传输验证 wall clamp；红态
  验证方式：临时删掉 `_read_capped` 里的时钟判断分支重跑该测试——0.69s
  内失败（`RESPONSE_TOO_LARGE` 而非预期的 `MODEL_UNAVAILABLE`，证实不是
  死循环, 只是判定错误)，随后从备份恢复原文件；loop 级端到端测试
  `test_a_slow_trickling_model_response_settles_as_unknown_through_the_loop`
  证明该失败最终走 `settle unknown` 路径。

### 独立审查结果与处置

全新上下文 general-purpose 子代理审查（未继承本会话讨论，给定五个提交、
待审 diff、原始证据 `loop-followup.md`/`lease-wire-33.md`，未以实现者
结论引导）。核实方式含逐条 `git show`/读码、自跑 `make check`、
在真实 PostgreSQL 上把第 4 条的 `persistence.py` hunk 临时还原重跑确认
真红后再复原、grep 全仓核对第 3 条「无 source 区间字段」与第 1 条
「其它 `view_bindings` 用例不受影响」的事实陈述、用合成 `BufferedReader`
实测第 5 条的内层阻塞问题。结论：

1–4 判定「正确」，无发现；5 判定「有效但存在遗漏」——已采纳并修复
（见上「独立审查发现并已修的残余缺口」）。审查明确指出 5 的修复方向
不违反 fail-closed 收紧原则（不放宽任何既有拒绝），只是完整性声称
过头；已采纳的修复同时更正了代码注释里的过度声称。

审查也确认：本次 4 条既有机器人发现的修复彼此独立、未互相依赖对方的
判定；`docs/tasks/...md` 的同步编辑（本记录）是审查开始前已存在的
未提交修改，审查未触碰。

### 验证证据

- 每条发现修复后单独跑过 `tests/test_m1_investigation_loop.py`（或对应
  文件）确认绿，再跑一次 `make check` 确认无级联破坏，逐条独立提交；
  独立审查处置后（`8b65c13`）复跑一遍确认仍全绿。
- 最终 `make check`（全部 6 个提交后）→ `uv lock --check` 通过；
  `ruff check` All checks passed；`ruff format --check` 无需改动；
  `mypy` Success: no issues found in 27 source files；`pytest`
  **1314 passed, 84 skipped, 2 xfailed**。
- PG 定向：本 worktree 专属 55431 端口本次全程空闲，
  `.venv/bin/python -m scripts.m0.postgres_lab start` →
  `M1_DURABLE_POSTGRES=1 .venv/bin/python -m pytest tests/integration -q`
  → **30 passed, 54 skipped**（含新增 `late_result` PG 测试；独立审查
  期间也独立跑过一次 `test_m1_durable_state_postgres.py` 全量 26 passed）；
  完成后 `postgres_lab stop`；未用 `M0_ENV_FILE`（不涉及真实 DeepSeek
  调用）。

## 追加（2026-09-17 三续）：合并 base 分支 PR #20 的新提交（解决 DIRTY/CONFLICTING）

调度者发现 PR #29 的 `mergeStateStatus` 变成 `DIRTY/CONFLICTING`——base 分支
`feature/m1-01-tool-executor`（PR #20）当天又追加了 9 个提交（工具预算
`charge_tool` 原子上限、执行顺序复查、`ToolDescription` 五字段结构化、
`source_start_at`/`source_end_at` 区间保留等，`f4f30fe`→`fb28026`）。
授权做法：`git fetch` 后 `git merge origin/feature/m1-01-tool-executor`
（普通 merge 提交，禁止 rebase/force），语义解冲突，保留双方测试。

- **唯一真实冲突**：`opspilot/persistence.py`。本分支在 `reserve_budget`
  之后插入了新方法 `settle_budget`（模型请求预算结算，#20 无这个概念）；
  同时 #20 把相邻的 `charge_tool`（工具预算，#29 不碰这个方法体）签名
  从 `(lease, operation_id, seconds)` 改成新增必填关键字
  `max_operations`（原子执行上限修复）。三路合并把两处改动都锚定在同一
  个 `def` 行邻近位置，误判为互斥。**解法**：保留 HEAD 完整的
  `settle_budget` 方法体，紧接着换用 origin 的新 `charge_tool` 签名，
  函数体其余部分本就已正确自动合并（未改动）。核对过 `charge_tool` 的
  全部调用方（`opspilot/tools/ledger.py`、
  `tests/integration/test_m1_tool_budget_postgres.py`）均已在 #20 侧带上
  `max_operations=`；`settle_budget` 的调用方（`opspilot/investigation/`
  全部文件）不受 #20 影响。
- **`tests/m1_tool_support.py` 未产生冲突**：#29 从未修改这个文件，#20
  给 `build()` 新增的 `ledger=None` 关键字参数是纯新增、向后兼容，
  `tests/m1_investigation_support.py` 里 `assemble()` 对它的调用
  （`build(clock=clock)`）与返回值解包（`executor, transport, sink, clock`）
  未受影响；已核对 `scripts/m1_live_flash_loop.py` 的 `build(...)` 调用同样
  兼容。
- **其余 11 个标记为冲突候选但实际无冲突的文件**（`opspilot/tools/*`、
  `tests/test_m1_tool_*`、`docs/tasks/2026-09-14-...md` 等）：均是 #20
  单方面新增/修改、#29 从未碰过，三路合并直接采纳无需人工介入。
- **意外发现（记录不处理）**：#20 的 `472a4e1` 已经给
  `opspilot/tools/executor.py` 的 `TransportResponse`/view 补上了
  `source_start_at`/`source_end_at` 字段——这正是上一轮任务里判定
  「发现 #3 另一半无数据基础、需 PR #20 先补字段」时缺的那份数据。
  merge 后这个前提已经成立，但**本任务范围是合并本身，不包含借机去
  `reports.py` 补那部分判定**，按范围口径未做，留给下一次任务决定是否
  接着做。

### 验证证据

- `make check`（merge 后）→ `uv lock --check` 通过；`ruff check` All
  checks passed；`ruff format --check` 无需改动；`mypy` Success: no
  issues found in 27 source files；`pytest` **1354 passed, 86 skipped,
  2 xfailed**（较合并前 1314/84 各自新增 40/2，均来自 #20 一侧新增的
  工具执行器测试，非本分支代码回归）。
- PG 定向：`.venv/bin/python -m scripts.m0.postgres_lab start` →
  `M1_DURABLE_POSTGRES=1 .venv/bin/python -m pytest tests/integration -q`
  → **32 passed, 54 skipped**（较合并前 30 passed 新增 2，为 #20 一侧
  工具预算原子上限的新 PG 用例）；完成后 `postgres_lab stop`。
- 推送与 CI：`10f32b6`（merge 提交，双亲 `26d0c70`/`fb28026`）已推送；
  对该 HEAD `workflow_dispatch` 触发，`checks`/`m0-postgres` 均通过
  （run 35284885782）。GraphQL 核查 reviewThreads 共 23 条，全部
  resolve，合并后未产生新 thread；`mergeStateStatus=CLEAN`，
  `mergeable=MERGEABLE`——DIRTY/CONFLICTING 已解决。

## 追加（2026-09-17 四续）：完成发现 #3 的另一半（完整 source 区间校验）

调度者在 `rebase-29.md` 里指出的前提（#20 已给 `TransportResponse`/view
补上 `source_start_at`/`source_end_at`）已随 `10f32b6` 合并成立，`srcrange.md`
的「#29 接线建议」给出了具体消费口径。任务书要求：字段缺失时的行为
与 #20 的字段语义一致（可选字段则按现有行为处理，写明依据），不能让
已保存的真实 Run 回放无故转红；复用 #20 的字段与 view 传播，不复制其
校验实现。

- **字段可选性核实**：`opspilot/tools/executor.py` 的 `TransportResponse`
  docstring 原话——「Both default to None when unknown」——确认两端皆缺
  是合法、常见的「覆盖范围未知」状态，不是错误。据此在
  `eligible_time_policies` 里：两端都缺失 → 完全走此前（`faf80bc` 之前）
  的既有逻辑，不新增任何限制；两端都存在且合法 → 在既有检查之上追加
  区间校验（historical 要求
  `policy.window.start <= source_start_at <= source_end_at <=
  policy.window.end`；current 要求区间落在查询窗口内、且以区间**较早**
  端相对 `reference_at`（取 view 的 `observed_at`，复用既有字段，非新引入
  时钟依赖）计算年龄，同时全局拒绝 `source_end_at` 晚于 `reference_at`）；
  只存在一端、无法解析、naive、倒置 → 判为「不一致」而非「未知」，直接
  对该 view 的**全部**策略返回空集（比 `srcrange.md` 建议的「只跳过该
  policy」更严格——因为当前仅有的两种模式都是区间相关的，一个自相矛盾的
  区间声明不该被任何策略部分采信；已在提交信息注明这不是逐字照抄 #20
  的建议）。
- **复用而非复制**：新增参数解析全部复用本文件既有的 `_aware()`（tz-aware
  校验、`ValueError` 兜底），没有重写 #20 在 `executor.py` 里已经做过的
  日期校验；`loop.py` 调用点只是把 view 里已有的三个字段
  （`source_start_at`/`source_end_at`/`observed_at`）透传进去，没有新建
  数据结构。
- **未破坏已保存的真实 Run**：`docs/evidence/m1-01-investigation-loop/`
  的证据来自 `scripts/m1_live_flash_loop.py` 的 fixture 工具，grep 确认
  该脚本构造的 `TransportResponse` 从未设置这两个新字段——回放这份证据
  会走「两端皆缺→既有逻辑」的分支，结果不变。`tests/acceptance/
  test_m1_live_flash_replay.py` 在本仓库/本 worktree 不存在（`find`
  确认），任务书对它的引用是过期状态；已核实但未强行假设其存在。

### 测试（先红后绿）

`tests/test_m1_investigation_loop.py` 新增 8 条：`eligible_time_policies`
直接单测覆盖「旧起点+新终点被拒」「未来终点使整条 view 的全部策略失效」
「current 越出查询窗口」「historical 越出策略窗口」「区间缺失回退到既有
行为（两种模式）」「单端区间被普遍拒绝」；另 2 条端到端跑真实
`InvestigationLoop`（经 `_run_tools` 真实工具交付路径），验证 wiring 生效。
两条端到端用例都需要显式 `all_authorized_targets: true`——若省略，会被
`23c7158`（上一轮已修的目标范围收紧）先行拒绝，掩盖本次要验证的区间
机制；已在调试中实际复现这个陷阱并修正。

### 独立审查（全新上下文 general-purpose 子代理）

未继承本会话讨论，给定待审提交 `facb64d`、`srcrange-29.md`/`srcrange.md`
原始依据，未以实现者结论引导。逐条核实：字段可选性判断与 #20 docstring
一致；不破坏已保存证据的结论经 grep 独立复核；「整条 view 全部策略失效」
的严格化判定为「合理但确属超出 #20 建议的设计选择，已在提交信息坦白
说明，不是掩盖」；historical 新增校验用具体场景验证非空操作（查询窗口
在策略窗口内但真实 source 区间越界）；current 年龄计算确认取区间较早端；
`view_start`/`view_end` 为 None 的分支在真实调用路径里是死代码（`QueryScope.
window`/`Window.as_json()` 保证非空），只对直接单测调用方有意义，已如实
指出但不算缺陷；`all_authorized_targets: true` 必要性通过移除后重跑实测
确认。结论：6 项全部「正确」，无发现，未要求任何修复。

### 验证证据

- `make check`（`facb64d` 上）→ `uv lock --check` 通过；`ruff check`
  All checks passed；`ruff format --check` 无需改动；`mypy` Success:
  no issues found in 27 source files；`pytest` **1362 passed, 86
  skipped, 2 xfailed**（较合并后 1354 passed 净增 8，即本次新测试）。
- PG 定向：本 worktree 专属 55431 端口空闲，`.venv/bin/python -m
  scripts.m0.postgres_lab start` → `M1_DURABLE_POSTGRES=1 .venv/bin/
  python -m pytest tests/integration -q` → **32 passed, 54 skipped**
  （与合并后持平，符合预期——本次改动不碰 `persistence.py`）；完成后
  `postgres_lab stop`。独立审查期间也独立起停过一次 PG lab，同样
  32 passed。

## 追加（srcrange-29 续：`facb64d` 推送后新增的 5 个机器人发现）

`facb64d`/`bcc466b` 推送后，`chatgpt-codex-connector` 在同一 PR 上追加了
5 个新 review thread（前 4 个于 2026-09-17T23:12:30Z 一批发出，第 5 个于
2026-09-18T06:51:08Z 单独发出，晚于前 4 个的修复提交）。按本项目「合并后
出现新 bot thread 也一并处置」的既定做法，逐条修复，先红后绿，一发现一
提交。

### 发现 1（P1，`reports.py:257`，comment 4042176048）—— 允许字段的值形状未校验

- **问题**：P3-4 的字段白名单只按 key 名过滤，未校验 value 的形状；
  `interfaces: [{"token": "..."}]`、对象值的 `namespace` 等畸形嵌套值会
  被原样拷贝，随 `InvestigationLoop.run()` 序列化进外部模型请求，等于
  「任意深度白名单」的说法名不副实。
- **修复**（`7eea577`）：把 `_VIEW_BINDING_FIELDS`/`_TIMING_FIELDS`/
  `_TARGET_CATALOG_ENTRY_FIELDS`/`_TIME_POLICY_FIELDS`/`_TIME_WINDOW_FIELDS`
  从 `frozenset[str]` 改为 `dict[str, Callable[[object], bool]]`，新增
  `_is_str`/`_is_str_or_none`/`_is_bool`/`_is_int_or_none`/`_is_list_of_str`
  校验器；`_project_fields()` 改为 key 存在**且** value 形状合法才拷贝；
  容器字段（`view_bindings`/`target_catalog`/`time_policies` 及嵌套
  `timing`/`window`）不再泛化拷贝，只在显式 `isinstance` 检查后逐条投影。
  副作用：发现并修掉了「条件覆盖遗留旧畸形容器」的次生 bug（旧代码对
  容器字段做条件覆盖时，形状不合法的旧值不会被清空）。
- **测试**：`test_evidence_context_projection_rejects_a_nested_object_
  under_a_scalar_field` 新增，构造 `interfaces`/`observed_services`/
  `target_refs` 三处形状不合法的嵌套值，断言全部被丢弃、合法同级字段
  保留。先红（旧 `_project_fields` 原样拷贝对象值）后绿。

### 发现 2（P1，`loop.py:456`，comment 4042176052）—— 陌生 Run 的 context 仍能授予新证据资格

- **问题**：更早一轮的 `82218ae` 只把 `type`/`run_id` 身份校验绑定到
  `delivered_from_context()`（预先提供的证据），`context_target_catalog()`/
  `eligible_time_policies()`（工具**新采集**证据走的路径）仍然照单全收
  陌生 `run_id` 的 context——陌生 context 的 target 别名和 time policy
  可以套用到本 Run 新采集的 view 上。
- **修复**（`bab8941`）：`evidence_context_projection()` 新增 `run_id`
  必填关键字参数，在任何字段投影之前先校验 `context.get("type") ==
  EVIDENCE_CONTEXT_TYPE and context.get("run_id") == run_id`，不匹配直接
  返回 `None`；`loop.py`（`run()` 顶部）调用点改为传入
  `run_id=request.run_id`。因为身份门禁现在在投影入口就短路，原来的
  `_CONTEXT_SCALAR_FIELDS`（用于旁路校验 type/run_id 两个标量字段）变成
  死代码，一并删除。
- **交付顺序**：因为发现 2 直接改了发现 1 刚加的函数签名和容器逻辑，为
  保持「一次提交一个逻辑变更」，采用「回退发现-2 专属改动→单独提交
  发现-1→在其上恢复发现-2→单独提交发现-2」的手工拆分，每一步都跑过
  全量测试确认绿（发现-1-only 1363 passed；恢复后 1364 passed，与两者
  合并态完全一致）。
- **测试**：`test_a_foreign_run_context_grants_no_eligibility_to_freshly_
  collected_evidence` 新增：陌生 `run_id` + 宽松 `current` 策略
  （`max_source_age_seconds: 7200`），断言即使工具真实调用成功
  （`transport.called is True`），最终仍 `execution == "failed"` 且
  `handoff_reasons == ("REPORT_INVALID",)`。级联影响：身份门禁导致约 5
  个既有测试的 fixture 因缺 `run_id` 转红，逐一在基础 fixture
  （`tests/m1_investigation_support.py`）、4 处独立 fixture、2 处
  `run_id` 不匹配的调用点补齐/修正后转绿。

### 发现 3（P1，`client.py:130`，comment 4042176056）—— 连接建立/收头阶段未受 wall clamp 约束

- **问题**：`urlopen` 的 `timeout` 只约束单次 socket 操作，服务端如果让
  每次 socket 读都恰好在 `timeout` 前完成（慢滴灌），`opener.open()`
  本身（连接建立、状态行、响应头）可以无限期阻塞在拿到 response 对象
  之前——此时既有的 `_read_capped`/`_tighten_socket_deadline` 都无从
  介入，因为它们只在拿到 response 对象之后才能生效。可能超过模型
  超时、Run wall 上限与授权 deadline，延误 pause/cancel。
- **修复**（`8f9688c`）：把 `opener.open()` + `_read_capped()` 整体包进
  `_fetch()`，提交到 `ThreadPoolExecutor(max_workers=1)`，用
  `future.result(timeout=budget)` 作为唯一的、真实时钟的兜底——无论
  `_fetch` 内部卡在哪个阶段，本线程最多等 `budget` 秒。`finally` 块用
  `pool.shutdown(wait=False)`（而非 `with` 语句的隐式 `shutdown(wait=
  True)`）避免反过来阻塞在被抛弃的慢线程上。验证
  `concurrent.futures.TimeoutError is TimeoutError`（本项目锁定
  Python 3.12 下为 `True`），既有的 `except (URLError, TimeoutError,
  OSError)` 不需要新增分支即可捕获 `future.result` 自身超时。
- **测试**：`test_a_stalled_connect_or_header_phase_is_also_bounded`
  新增（`tests/test_m1_investigation_client.py`）：因为该阶段完全在
  `opener.open()` 内部阻塞，无法像其余用例一样注入 `FakeClock`（Python
  无法把假时钟注入阻塞的 C 级 socket 调用），改用真实 `time.sleep(0.3)`
  模拟慢连接，断言真实耗时 `< 0.3s`（即被 `future.result(timeout=0.05
  的 budget)` 提前打断，而非等满 0.3s）。先红（旧代码耗时 ≥0.3s）后绿。

### 发现 4（P2，`loop.py:655`，comment 4042176058）—— 工具调用参数 JSON 的 RecursionError 未捕获

- **问题**：模型返回约一万层嵌套的 `function.arguments` 时，Python 3.12
  的 `json.loads()` 抛 `RecursionError` 而非 `ValueError`——`RecursionError`
  不是 `ValueError` 子类，`_parse_arguments()` 原有的 `except ValueError:`
  漏抓，异常从 `InvestigationLoop.run()` 逃逸，此时该轮 assistant step
  已经落库提交，调用方却收不到任何 `LoopOutcome`。
- **修复**（`0c648fe`）：`except ValueError:` 扩为 `except (ValueError,
  RecursionError):`，与 #20 在 `opspilot/tools/executor.py`
  `_result_rows`（提交 `46cbf3e`）解析工具**响应体**时遇到的同一个
  stdlib 陷阱采用完全相同的修法，是复用而非重复发明。核实过下游：
  `params=None` 会流入 `_accept_params`（`isinstance(params, Mapping)`
  为假）→ `(None, "INVALID_PARAMS")` → `_refuse(operation, "error",
  "INVALID_PARAMS")`，是既有的、已被良好测试覆盖的「参数不合法」拒绝
  路径，不需要在 `_parse_arguments` 之外新增任何处理。
- **测试**：`test_a_deeply_nested_tool_call_argument_does_not_crash_the_
  loop` 新增：`"[" * 20_000 + "]" * 20_000` 作为 `arguments`，断言
  `outcome.execution == "failed"` 且 `transport.called is False`（因为
  在 `_authorize` 阶段就被拒绝，根本到不了 transport）。先红（旧代码
  `RecursionError` 逃出 `loop.run()`）后绿。

### 发现 5（P1，`client.py:136`，comment 4044473894，晚于前 4 个发现单独出现）—— HTTP 错误响应体的排空读取未受 deadline 约束

- **问题**：`except HTTPError as exc:` 分支里直接调用
  `exc.read(MAX_HTTP_RESPONSE_BYTES + 1)` 排空错误响应体，这是在**调用
  线程**里的一次原始、无超时阻塞调用，没有走 `_read_capped()` 的逐块
  deadline 检查——与发现 3 修复的连接/收头阶段是同一类缺口，只是发生
  在 429/500/503 错误体的排空阶段：服务端慢滴灌错误体（每次读仍能
  完成，只是很慢）可以让这次调用无限期挂起，绕开模型超时和 Run wall
  上限，延误 pause/cancel。
- **修复**（`bad4ab6`）：复用发现 3 引入的同一套机制，而非另起一套——
  把排空读取提交到**同一个** `pool`（`max_workers=1`，此时 `_fetch`
  已跑完，worker 空闲），用 `future.result(timeout=remaining)` 兜底，
  `remaining = deadline - self._clock.monotonic()`（`deadline` 是本次
  物理请求一开始就算好的同一个绝对时间点，此处直接复用，非新增字段）；
  `remaining <= 0` 时直接跳过排空。验证 `TimeoutError` 是 `OSError` 的
  子类（`issubclass(TimeoutError, OSError) is True`），既有的
  `except OSError: pass` 不需要新增分支即可同时覆盖「原始读取自身的
  OSError」与「`future.result` 排空超时」两种情况。
- **测试**：`test_a_stalled_http_error_body_drain_is_also_bounded` 新增
  （`tests/test_m1_investigation_client.py`）：构造真实
  `urllib.error.HTTPError`，其 `fp` 参数是一个 `.read()` 会真实
  `time.sleep(0.3)` 的假对象（验证过 `HTTPError.read()` 通过
  `tempfile._TemporaryFileWrapper` 的属性代理确实会转发到 `fp.read()`），
  断言真实耗时 `< 0.3s`。先红（旧代码耗时 ≥0.3s）后绿。

### 测试与检查汇总（5 个发现合计）

- 新增测试 5 条（`tests/test_m1_investigation_loop.py` 4 条 + `tests/
  test_m1_investigation_client.py` 1 条，各发现一条），全部先红后绿。
- `make check`（`bad4ab6` 上）：`ruff check .`/`ruff format --check .`/
  `mypy` 全部干净；`pytest` **1367 passed, 86 skipped, 2 xfailed**。
  基数为 `facb64d`/`bcc466b`（srcrange 轮结束时）的 **1362 passed**，
  5 个发现各新增 1 条用例，1362+5=1367，与实测一致。
- `M1_DURABLE_POSTGRES=1 .venv/bin/python -m pytest tests/integration/
  test_m1_durable_state_postgres.py tests/integration/
  test_m1_tool_budget_postgres.py -q` → **32 passed**（每个发现修复后
  单独跑过一次，均为 32 passed，与合并后持平；本轮改动不碰
  `persistence.py`）。
- 真实 Run 回放：`tests/acceptance/test_m1_live_flash_replay.py` 在本
  仓库/本 worktree 不存在（上一轮 `find` 已确认，任务书引用已过期）；
  `docs/evidence/m1-01-investigation-loop/` 的 `ledger.json` 只记录
  token/耗时等汇总统计，不保留原始 `function.arguments` 字符串，且该
  Run 只有 1 次工具调用（`tool_call_count: 1`），DeepSeek 真实返回的
  正常 `{"expr": "..."}"` 参数不可能触发 `RecursionError`——5 个发现均
  不改变任何「合法输入」下的既有行为（发现 4 只多捕获一种此前会让
  进程崩溃的病态输入；发现 3/5 只在服务端行为异常慢时才会分支进新
  代码路径），回放这份证据不受影响。

### 独立审查（全新上下文 general-purpose 子代理）

未继承本会话讨论，给定 5 个待审提交（`7eea577`/`bab8941`/`8f9688c`/
`0c648fe`/`bad4ab6`）、各自对应的机器人原始评论文本、v4 schema 与
`opspilot/tools/executor.py` 作为独立核对依据，未以实现者结论引导。
逐条复核（含在独立沙箱脚本里重放红/绿、逐行核对控制流）：

1. 发现 1（字段形状校验）：对照 v4 schema 的每个 `$def` 逐字段核实
   校验器类型匹配，容器字段确认「只经校验重建、不再泛拷贝」，判定
   「正确、无遗漏」。
2. 发现 2（run_id 身份门禁）：核实 `loop.run()` 里投影调用在**所有**
   后续读取（含 `delivered_from_context`、`_run_tools` 内的
   `context_target_catalog`/`eligible_time_policies`）之前完成，判定
   「正确闭合了描述的缺口，无合法路径被误伤」。
3. 发现 3（连接/收头阶段 wall clamp）：独立验证
   `concurrent.futures.TimeoutError is TimeoutError` 为 `True`、
   `pool.shutdown(wait=False)` 不阻塞，沙箱重放确认先红后绿，判定
   「正确」；提出一条非阻塞性后续建议——`ThreadPoolExecutor` 的 worker
   线程受 CPython `concurrent.futures.thread._python_exit`（进程退出
   钩子）跟踪，如果对端持续无限慢滴灌（每次读都恰好在单次 socket
   超时前完成），被 `shutdown(wait=False)` 放弃的线程永远不会结束，
   进程正常退出时的 `join()` 可能被卡住——这是「放弃并继续」设计本身
   的既有取舍（本次改动的 P1 之前就已存在同构风险，不是新引入的
   缺陷，也不在 comment 4042176056 的范围内），不阻塞本次交付，记录
   为后续可选项（若需要保证进程在对抗性网络条件下干净退出，需要另开
   任务评估）。
4. 发现 4（RecursionError 捕获）：独立验证 `issubclass(RecursionError,
   ValueError)` 为 `False`（`RecursionError` 是 `RuntimeError` 子类）、
   沙箱重放确认先红后绿、`params=None` 到 `_refuse` 的下游路径核实
   无误，判定「正确、复用 #20 手法属实」。
5. 发现 5（错误体排空 wall clamp）：核实 `deadline` 在 `HTTPError` 可能
   抛出之前就已算好、复用同一个 `pool` 不会与 `finally` 里的
   `shutdown(wait=False)` 竞争（排空提交与 `.result()` 均在
   `except HTTPError` 块内先于 `finally` 完成）、`issubclass(TimeoutError,
   OSError)` 为 `True`，判定「正确」。

结论：5 项全部「正确、最小、按声称复用既有机制」，全量测试/ruff/mypy
均干净（`1367 passed, 86 skipped, 2 xfailed`，与本次实测一致）。
PG 定向测试在独立审查的沙箱环境里无本地 PostgreSQL，未能重跑（明确
标注为「未验证的声称，非失败」）；本任务在本 worktree 已用真实 PG lab
独立跑过（见上）。无需在合并前修复任何项；发现 3 的后续建议记入本节，
留待需要时另开任务处理，不阻塞本次 PR。

## 追加（2026-09-18 续：`b89d10d` 推送后新增第 6 条机器人发现）

`c496cea` 推送后，`chatgpt-codex-connector` 又追加 1 条新 review thread
（comment 4044987306，P1，`client.py:132`，2026-09-18T08:13:31Z）。

### 发现 6（P1，`client.py:132`）—— 不应把 sub-100ms 剩余预算延长

- **问题**：`budget = max(timeout, 0.1)` 会把已授权的 sub-100ms 剩余预算
  （Run deadline 或 wall 上限已接近耗尽）静默延长到 100ms。这个下限本身
  在本会话工作之前就已存在；但发现 3（`8f9688c`）把同一个 `budget` 变成
  了 `future.result(timeout=budget)` 的真实墙钟等待时间之后，这个下限就
  从「per-socket-operation timeout 上一个软性、基本无害的最小值」变成了
  「真实的 deadline/control 边界违规」——请求（及其后台 transport 线程）
  可能比 Run 实际授权多跑最多 5 倍时间。
- **修复**（`b89d10d`）：移除下限，`budget = timeout`；在 `_post()` 最前
  面新增 `if timeout <= 0: raise ModelError("MODEL_UNAVAILABLE")`，在任何
  dispatch 之前 fail-closed。核实过 `loop.py` 的 `_remaining_timeout()`
  本就保证送进本客户端的 `timeout_seconds` 严格为正（否则先以
  `DEADLINE_EXCEEDED`/`WALL_TIME_EXHAUSTED` halt），所以下限本不需要用来
  保证 `future.result(timeout=...)` 语义良好——只需如实传递真实剩余预算；
  新增的守卫是给其他潜在调用方（本客户端是通用组件，非仅被 loop 驱动）
  的防御边界，不是本次改动依赖的新逻辑。
- **测试**：2 条新用例（`tests/test_m1_investigation_client.py`）：
  `test_a_non_positive_timeout_fails_before_dispatch`（0/负数 timeout 断言
  从未触达 opener）；`test_a_sub_100ms_deadline_is_not_extended_past_the_
  authorized_budget`（20ms 授权预算 + 真实延迟 0.3s 的假 opener，断言真实
  耗时远低于旧下限 100ms）。先红后绿。
- **独立审查（全新上下文子代理）**：判定「正确、最小、按声称复用既有
  `_remaining_timeout()` 保证」。额外核实过 `ModelCall.timeout_seconds`
  是未做校验的裸 `float` 字段，理论上可能收到 NaN/inf/极小正数等退化值；
  但唯一真实调用方 `_remaining_timeout()` 结构上不可能产生这类值（来自
  真实 datetime/monotonic 减法），判定为「本 finding 范围外的既有非问题」；
  即便出现，直接验证过 `future.result(timeout=float('nan'))` 会抛既有
  `except (URLError, TimeoutError, OSError)` 链已捕获的 `TimeoutError`，
  不会新增未捕获异常路径。

### 验证证据

- `make check`（`b89d10d` 上）→ `ruff check`/`ruff format --check`/`mypy`
  全干净；`pytest` **1369 passed, 86 skipped, 2 xfailed**（较上一节
  1367 净增 2，即本条新用例）。
- PG 定向：本 worktree 专属 55431 端口曾被另一 worktree
  （`production-ops-agent-m1-control-completion`）的 PG lab 临时占用
  （2 分钟量级）——未强行处理，等待其自然释放后（`lsof` 确认端口转空）
  再 `postgres_lab start`；`M1_DURABLE_POSTGRES=1 pytest tests/integration/
  test_m1_durable_state_postgres.py tests/integration/
  test_m1_tool_budget_postgres.py -q` → **32 passed**；用完立即
  `postgres_lab stop`。
- 推送与 CI：`b89d10d` 已推送；`workflow_dispatch` 触发 run
  `35326233029`，`completed`/`success`。
- PR 状态：`gh pr view 29` → `mergeStateStatus=CLEAN`、
  `mergeable=MERGEABLE`。
- base 分支（PR #20）状态：推送前后两次核实 `origin/feature/m1-01-tool-
  executor` 又前进了若干提交（最终 HEAD `5f6cc44`），`git merge-tree
  --write-tree HEAD origin/feature/m1-01-tool-executor` 本地 dry-run 与
  GitHub 的 `mergeStateStatus` 均确认仍为 `CLEAN`，未变 `DIRTY/
  CONFLICTING`，按任务要求未执行合并。
- 机器人审查处置：GraphQL 核查 reviewThreads 共 29 条。本条新增发现
  已回复处置说明（引用修复提交、测试、独立审查结论）并
  `resolveReviewThread`；复查确认全部 29 条 resolve，无新增。

## 追加（2026-09-18 三续：`929ee54` 处置第 7 条机器人发现，空 v4 catalog fail-closed）

`1ba0e23` 推送后，`chatgpt-codex-connector` 又追加 1 条新 review thread
（comment 4045327020，P1，`reports.py:601`，2026-09-18T09:04:08Z）。

### 发现 7（P1，`reports.py:601`）—— 空 v4 catalog 未 fail-closed

- **问题**：`context_target_catalog()` 无论「context 完全没有
  `target_catalog`」还是「`target_catalog` 存在但为空字典 `{}`」都统一
  返回 `{}`；`unsupported_citations()` 的 `catalog = dict(target_catalog)
  if target_catalog else {}` + `if catalog:` 真值判断对两种情况都判定为
  假，无法区分。一个合法提供了 `target_catalog: {}`（零个不透明 key）
  的 v4 context 会被误当成「完全没有 catalog」，落回旧的「直接比对注册表
  `target_id`」路径——而这正是不透明 v4 catalog 存在的目的所要防止的事。
  新鲜工具调用产生的 `DeliveredView.target_ids` 仍会带上注册表原始
  target_id，所以一个 supported fact 可以直接引用它，绕过空 catalog
  本该造成的「零个合法 key、任何引用都不合法」的约束。
- **修复**（`929ee54`）：`context_target_catalog()` 返回类型改为
  `dict[str, str | None] | None`：`context` 不是 Mapping、或
  `target_catalog` 键不是 Mapping（即「完全未提供」）时返回 `None`；只要
  `target_catalog` 本身是 Mapping（哪怕过滤后为空）就返回实际的
  （可能为空的）dict。`unsupported_citations()` 把真值判断
  `if catalog:` 改为身份判断 `if target_catalog is not None:`，使
  「显式提供空 catalog」正确落入 v4 分支并对任何 `target_refs` 判定失败
  （因为空字典里不存在任何 key）。`loop.py` 里另一处不关心 None/空区分
  的调用点（`DeliveredView.target_ids` 的别名查找，用 `catalog.items()`）
  改用 `(context_target_catalog(...) or {})` 兼容，避免对 `None` 调用
  `.items()`。
- **测试**：`test_an_empty_v4_catalog_fails_closed_for_a_freshly_
  collected_view` 新增：`target_catalog: {}` + 真实工具调用产生的新鲜
  view，报告经 `report_from_transcript` 默认引用原始注册表 target_id
  `"checkout-prod"`，断言 `execution == "failed"`、
  `handoff_reasons == ("REPORT_INVALID",)`（旧代码会误判为
  `"completed"`）。先红后绿。
- **独立审查（全新上下文子代理）**：逐项核实：`None` 只在真正「未提供」
  时返回，一个 key 形状全部非法（如 `{123: {...}}`）的 catalog 过滤后
  仍正确返回 `{}` 而非 `None`；`unsupported_citations()` 新分支对「非
  fact-like claim 的空 `target_refs`」（`any()` 对空可迭代对象恒假，不会
  被误拒）与「fact-like claim 的非空 `target_refs`」（`ClaimV2.
  explicit_fact_scope` 强制非空，任何 ref 在空字典里都找不到，正确拒绝）
  两种情况分别验证；`loop.py` 调用点的 `or {}` 不改变该处既有行为；
  grep 全仓确认只有这两处调用点。判定「正确、最小、可安全维持」，无
  发现。

### 验证证据

- `make check`（`929ee54` 上）→ `ruff check`/`ruff format --check`/
  `mypy` 全干净；`pytest` **1370 passed, 86 skipped, 2 xfailed**
  （较上一节 1369 净增 1，即本条新用例）。
- PG 定向：55431 端口本次全程空闲（`lsof` 确认），直接
  `postgres_lab start` → `M1_DURABLE_POSTGRES=1 pytest tests/integration/
  test_m1_durable_state_postgres.py tests/integration/
  test_m1_tool_budget_postgres.py -q` → **32 passed**；用完立即
  `postgres_lab stop`（确认端口转空）。
- 推送与 CI：`929ee54` 已推送；`workflow_dispatch` 触发 run
  `35329891182`，`completed`/`success`。
- PR 状态：`gh pr view 29` → `mergeStateStatus=CLEAN`、
  `mergeable=MERGEABLE`。
- base 分支（PR #20）状态：推送前核实 `origin/feature/m1-01-tool-
  executor` 又前进（最终 HEAD `e60fc75`），本地 `git merge-tree` dry-run
  与 GitHub `mergeStateStatus` 均确认仍为 `CLEAN`，未执行合并。
- 机器人审查处置：**踩坑记录**——首次用 `gh api ... -f body="..."`
  （双引号内联字符串）回复该 thread 时，body 里的反引号被 shell 当作
  命令替换执行，导致回复正文里 `` `loop.py` `` 一段被吞掉（shell 报
  `command not found: loop.py`，但 API 调用仍返回了 200，正文已损坏且
  已发布）；发现后改用 `gh api ... -X PATCH -F body=@<文件路径>`（`-F`
  而非 `-f`，读取文件内容而非当字面量字符串）重新提交并核对正文完整，
  已更正。GraphQL 复查 reviewThreads 共 30 条，本条已回复（更正后的
  完整正文）并 `resolveReviewThread`；全部 30 条 resolve，无新增。用户
  已明确指示本轮处置完毕后不再手动触发 `@codex review`，未触发。
