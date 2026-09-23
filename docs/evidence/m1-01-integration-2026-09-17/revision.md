# 报告：`prompt_revision` 接线 + P3-4 白名单投影

- 任务书：`.../scratchpad/briefs/revision.md`
- 工作区：`/Users/shenghuikevin/dev/AI/production-ops-agent-m1-investigation-loop`，分支 `feature/m1-01-investigation-loop`，PR #29。
- 接手时状态：`git status`/`git diff` 为空。此前一个 Codex agent 因模型容量错误未能开工，未留下任何改动，无内容可沿用。
- 最终 HEAD：`4c406b0`（已推送）。

## 结论

两项要求均已实现、测试、独立审查处置、推送，PR #29 恢复到「就绪待用户审核合并」状态，本任务未合并。另发现并处置了 4 条与本任务无关但仍未修的既有机器人审查发现（见下）。

## 事实核查（先于实现，与任务书背景对照）

1. **`prompt_revision` 来源已是确定性计算，但与 PR #27 当前 HEAD 不一致。** ledger.json 的 `prompt-replay-candidate-017c81744c26` 确认由本分支 `opspilot/instructions/discipline.py`（2026-09-16 从 `origin/chore/instruction-contract-impl` 整体复制的快照）的 `prompt_revision()` 算出，不是人工编号。但实际执行 `git show origin/chore/instruction-contract-impl:opspilot/instructions/discipline.py` 取 #27 **当前** HEAD 重算同一变体，得到 `prompt-replay-candidate-2c26fd0db1e0`——不同（两次计算均已实际跑过，非推断）。原因：#27 已把 `template_projection()` 的覆盖面从「只投影 `LAYER_TEMPLATE` 段」改成「投影全部 segment」，修复一个真实缺口——C3 §5 明文要求「调整顺序」也要 bump revision，旧覆盖面漏了这类改动。**未修**：本分支未把该修复搬进本地 `discipline.py`（AGENTS.md 工作区约定「#27 尚未合并时不要复制实现」；这份差异正是当初整体复制 #27 实现的后果，再次搬运其后续修复只是重复同一问题）。已在 `prompt_revision_versions()` docstring 与任务记录中显式记录为已知缺口，合并顺序仍为 #27 → #29，届时 `prompt_revision` 会再变一次（预期的版本升级，非本任务遗留 bug）。
2. **红线 P3-4 引用的过滤代码在本分支（#29 单独）不存在。** 审计对象是 `integration/m1-01-full`（#29 × #31 × #33 合并分支）；grep 全仓确认「顶层键 `password/secret/token/authorization` 过滤」的代码在本分支 `opspilot/investigation/`、`opspilot/tools/registry.py` 均不存在（来自 #31/#33 叠加的 `opspilot_inputs` 通道）。本分支当前唯一会把调用方结构化 Mapping 整体序列化进模型 prompt 的路径是 `InvestigationRequest.evidence_context`，此前全程不过滤。按任务书要求实现的是这条路径上的白名单投影，为将来接上 `opspilot_inputs`/`append_input` 预置防线，而不是「修复」一段本分支并不存在的代码。

## 实现

- `opspilot/investigation/loop.py`：新增 `prompt_revision_versions(variant_id=DISCIPLINE_VARIANT, *, report_contract=REPORT_CONTRACT) -> dict[str, str]`，唯一来源是 `discipline.prompt_revision`；`LoopOutcome.prompt_revision` 改经此函数计算（值不变，路径统一）。`run()` 开头用 `dataclasses.replace` 把 `request.evidence_context` 换成 `evidence_context_projection(...)` 的投影结果，之后所有读取（prompt 消息、`_round`/`_run_tools` 里的引用校验）都只看投影后的值。
  刻意不做：不构造完整 `ModelProfile`（`provider`/`model`/`endpoint_mode`/`adapter_revision` 本分支无真实计算来源）；不计算 `tool_schema_revision`（C3 §5 `versions` 比对的另一半，属工具注册表/PR #20）；不接线产品代码里真正创建 Run 的 `DurableStore.accept`/`claim` 调用点（worker/service 层尚未实现，任务记录既有范围声明为范围外）。
- `opspilot/investigation/reports.py`：新增 `evidence_context_projection()` 及字段白名单常量，字段集合对齐冻结的 `docs/evidence/m0-real-investigation/IncidentScenario.v4.schema.json`（初版只抄了当前读取函数用到的字段，被独立审查指出窄于 schema，已扩到 schema 全部必填字段，见下）。
- `opspilot/investigation/__init__.py`：导出 `prompt_revision_versions`。

## 测试（先红后绿，均已实际执行）

- 单元/loop：`test_loop_outcome_prompt_revision_ignores_the_run_instance_budget`（真实跑两次 loop，`model_requests=1`/`4`，revision 相同、face 不同）；`test_prompt_revision_versions_moves_with_the_l2_report_contract_text`（真实编辑 L2 文本使 revision 改变）；`test_evidence_context_projection_strips_nested_unlisted_keys`（三处嵌套注入的敏感键均被剥离）；`test_evidence_context_projection_preserves_every_schema_required_field`（对着 schema 全部必填字段的合规 context 做投影后原样返回的回归断言）；`test_loop_never_sends_nested_secret_bearing_keys_to_the_model`（端到端）。
- PG（`tests/integration/test_m1_durable_state_postgres.py`）：`test_prompt_revision_content_change_blocks_an_in_flight_run`（编辑 L2 文本 → `store.claim()` 抛 `INCOMPATIBLE_STATE` → `rebuild()` 确认 `blocked`）；`test_prompt_revision_ignores_instance_values_so_reclaim_is_not_blocked`（同一 revision 下，租约到期后第二次 `claim()` 正常拿到新 epoch）。

## 验证证据

- `make check`（最终 HEAD）：`uv lock --check` 通过；`ruff check` All checks passed；`ruff format --check` 无需改动；`mypy` Success: no issues found in 27 source files；`pytest` **1299 passed, 83 skipped, 2 xfailed**。
- PG 定向：本 worktree 专属 55431 端口一度被另一并行 worktree（`production-ops-agent-flake`）占用，等待释放后 `.venv/bin/python -m scripts.m0.postgres_lab start` → `M1_DURABLE_POSTGRES=1 pytest tests/integration/test_m1_durable_state_postgres.py -q` → **25 passed**（2 条新测试单独按名验证通过）；`tests/integration -q` 同参数 → 29 passed, 54 skipped（其余 PG 用例走不同环境变量，跳过属既有行为）；完成后 `postgres_lab stop`；未用 `M0_ENV_FILE`（本次不涉及真实 DeepSeek 调用）。
- CI：对 HEAD `692cf3e`（其后仅 docs 提交，无代码变化）`workflow_dispatch` 触发，`checks`、`m0-postgres` 均通过（run 35255856858）。

## 独立审查（全新上下文 general-purpose 子代理）

未继承本会话讨论，给定目标/约束/待审 diff 路径/原始证据，未以实现者结论引导；核实方式含自行读码、自跑 `make check`、`git show` 对比 #27 当前 HEAD、grep 全仓确认 P3-4 代码不存在。

- `prompt_revision` 接线：判定「正确，测试到位」，无发现。
- P3-4 投影：判定「机制正确，但初版白名单窄于冻结 v4 schema」——**已采纳并修复**。指出遗漏 `run_id`、`ViewBinding.view_hash`/`timing`、`TimePolicy.revision`/`integration_id`/`reference_rule`/`scope_revision`、`target_catalog` 的 `ComposeTarget`/`IntegrationTarget` 两个变体。已用 `Read` 核对 schema 原文，重写白名单为 schema 字段并集，新增回归测试防止再次收窄。
- discipline.py 已知缺口：判定「暂缓修复的决定站得住脚，但应更明确写出」——已采纳，在 `prompt_revision_versions()` docstring 补充「Known gap, not fixed here」段。
- 未自跑 PG 测试（时间限制，只读代码确认模式一致）；本任务已自行跑通，见上。

## PR 与机器人审查处置

- 提交（三个逻辑变更）：`519c108`（feat，prompt_revision）、`4ed021f`（fix，P3-4）、`692cf3e`（docs）、`4c406b0`（docs，本节收尾），均已推送。
- PR #29 描述已更新（追加 2026-09-17 节）。`mergeStateStatus=CLEAN`，`mergeable=MERGEABLE`。
- **发现一轮既有、范围外的机器人审查（`chatgpt-codex-connector`，2026-09-17T16:47:31Z，锚定在改动前的 `814dd2d`，非本次改动触发）**：GraphQL 核查 reviewThreads 共 23 条，接手时 4 条未 resolve，内容与本任务两项范围均无关（`delivered_from_context` 的 `run_id`/v4 类型绑定校验缺失；`eligible_time_policies` 的目标覆盖判定对空 `target_refs` 过宽；`eligible_time_policies` 的 current 策略新鲜度判定未校验完整 source 区间/未来时间戳；`_settle`/`_commit_step` 对 `CONTROL_DENIED` 丢弃围栏期回复而未落盘历史）。逐条判断为「看起来成立，但超出本次任务范围」，在 PR 上逐条回复拒绝理由（comment id 4039957732、4039960100、4039962359、4039963491）并 resolve；resolve 后 23 条全部处理完。**这 4 条对本 PR 是真实、未修的发现，不属于本任务遗留，需要用户决定是否派发新任务修复**——本次未修的理由是：都不在任务书范围内，且同时修复 4 条未经独立审查、彼此无关的逻辑改动会明显扩大本次变更的风险面。

## 未完成 / 已知限制

1. `discipline.py` 的 `template_projection` 仍停在 #27 合并前的旧覆盖面（只投影 `LAYER_TEMPLATE`），合并 #27 时需整体替换而非合并，届时 `prompt_revision` 值会再变一次。
2. `prompt_revision_versions()` 只覆盖 C3 §5 `versions` 比对里 `prompt_revision` 这一半；`tool_schema_revision` 半边、完整 `ModelProfile` 构造、以及产品代码里真正调用 `DurableStore.accept`/`claim` 创建 Run 的调用点均不在本任务范围（worker/service 层尚未实现）。
3. 上述 4 条既有机器人审查发现未修，见上。
4. 未改 11 个 feature `passes`、验收步骤、SPEC 门槛陈述；未扩产品权限；未合并 PR（等待用户审核）。

## 任务记录

`docs/tasks/2026-09-16-m1-01-investigation-loop.md`「追加（2026-09-17）」节，含事实核查、实现、测试、独立审查处置、机器人审查处置全部细节。

---

/private/tmp/claude-501/-Users-shenghuikevin-dev-AI-production-ops-agent/a7d3abb1-8221-4812-bafb-316b8b11e6aa/scratchpad/reports/revision.md
