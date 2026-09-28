# M1-01 对齐上游（第二批）：工具体量、时间窗、校验重试、防循环

## 目标与来源

限制类规则独立审计（Opus，2026-09-28；Codex 因容量三次失败未产出）发现：除已处理的循环预算外，工具层上限、固定 300 秒观测窗、校验失败即整份交接、缺少重复调用检测都偏离上游且无记录理由。用户 2026-09-28 决定：trace/视图上限对齐上游；时间窗对齐上游由模型自选；校验失败把错误反馈给模型重试。lead 按"对齐上游"原则直接纳入：压缩阈值 0.95、重复调用检测。

依赖：先完成 `2026-09-28-m1-01-loop-limits.md`（含 L1a、L3a）并完成其真实效果测量，再实施本批，以便分别度量。

## 行为合同

B1. **trace 与视图体量对齐上游**：trace 搜索不再设条数上限（`limit` 参数可选，默认值与上游一致，查不到则取 20 并记录理由），不再按 20 行裁剪展示 span；视图字节上限放宽到上游单条工具结果量级（约 25k tokens / 约 100 KB，以上游 `holmes/common/env_vars.py:137,142` 为准并记录换算）。超出时仍截断，但视图必须显式给出省略的行数与字节数（沿用现有 omitted/truncated 字段与第二轮单位化计数）。metrics 视图同理放宽。
B2. **时间窗由模型在授权外框内自选（对齐上游）**：工具新增可选时间参数（与上游 `holmes/plugins/toolsets/utils.py:111-113` 同语义：start/end 可省略，默认回看 1 小时至提交时刻）。runner 仍决定一个授权外框（默认提交时刻前 24 小时至提交时刻），模型参数超出外框即拒绝并说明外框——满足 PRODUCT-CONSTRAINTS「Query scope … constrained」，不改该文件。视图记录实际查询窗；lookback 语义按窗内规则（第一批 window-points）继续适用。报告中的时间范围引用按实际视图窗口。
B3. **报告校验失败反馈重试**：报告未通过解析或引用校验时，把规范化的失败原因（哪条 claim、哪个 evidence_id、何种错误，不含原始视图内容）作为一条固定模板 user 消息回给模型，允许重写 1 次（占用请求额度）；仍失败再按现有路径交接。与 loop-limits L1a 的"格式修复一次"合并为同一机制，总计最多 1 次修复重试。
B4. **压缩阈值对齐上游**：0.8 → 0.95（上游 `holmes/core/llm.py:159`）；单条工具结果占上下文比例上限按上游设置核对并记录。
B5. **重复调用检测（对齐上游 `holmes/core/safeguards.py`）**：同一 Run 内完全相同的工具调用（同工具、同参数）再次出现时不重复执行，返回一条说明"与第 N 次调用相同，结果见 evidence_id X"的工具结果；行为与上游默认一致并记录差异。

B6. **上下文取保守值**（lead 2026-09-28 补充）：`MAX_CONTEXT_TOKENS` 由 1_048_576 改为 1_000_000。DeepSeek 官方页面只写 "1M"，按二进制换算的 1_048_576 在接近满窗口时可能被供应商拒绝；输出 65536 不变，请求字节上限相应核对。

实现决定（lead 2026-09-28）：B2 的模型自选时间窗采用"工具参数"方式——start/end 作为可选参数进入 `request.params`，由 `_metrics`/`_traces` 在 runner 授权外框内校验与使用（与上游工具参数语义一致）；otel_demo 各注册的 `max_window_seconds` 相应提高以容纳 24 小时外框。

## 验收

- 全新上下文测试作者先写 `tests/test_m1_upstream_alignment_b_contract.py` 并确认红；实现者转绿不改断言；既有测试因合同变化的修改逐条记录。
- 独立审查。
- 真实效果测量：同 loop-limits 的 6 个 Run 口径（2 正常 + 2 故障 + 2 留出），与第一批结果并列对比。

## 状态

- 2026-09-28：合同写定；待第一批完成后实施。
- 2026-09-28：实现完成（分支 `feature/m1-01-window-points`，worktree
  `production-ops-agent-window-points`）。B1-B6 逐条落地于
  `opspilot/tools/otel_demo.py`（B1 trace/view 体量、B2 时间窗）、
  `opspilot/investigation/loop.py`（B3 重试反馈、B5 去重）、
  `opspilot/investigation/reports.py`（B3 模板）、
  `opspilot/investigation/context.py`（B4 压缩阈值）、
  `opspilot/investigation/limits.py`（B6）、`opspilot/instructions/discipline.py`
  （B3 反馈模板并入 `prompt_revision` 哈希）。本地已提交，未 push。

  **合同测试结果**（`tests/test_m1_upstream_alignment_b_contract.py`，3d2d74c，
  未改断言）：14 项中 11 项转绿。3 项仍红，判定为测试文件自身的既有缺陷，
  与本批实现无关（已用独立脚本核对 `outcome.model_view` 真实结构，附证据）：
  - `test_b1_traces_view_no_longer_caps_sampled_spans_at_20`：测试自身的
    "test invariant: stays small" 前置断言用 60 个 `_small_span` 算出
    22201 字节，超过它自己断言的 16 KiB 门槛（与本批改动无关，是该测试
    夹具早于任何实现就已算错的常量）。
  - `test_b1_traces_view_byte_cap_matches_the_upstream_tool_result_scale`、
    `test_b1_metrics_view_byte_cap_also_widened`：都断言
    `outcome.model_view["data"][...]`，但 `ReadOnlyToolExecutor._record()`
    从未把行内容放在 `view["data"]` 下——一直是 `view["content"]`（同一测试
    文件既有的 `test_m1_otel_demo_contract.py:1203/1228/1239/1286` 等多处直接
    断言 `view["content"]`，且用真实 executor 跑一次可复现）。`view["data"]`
    这个键从不存在，与截断/行数上限无关。
  这 3 处按规则"不得改其断言"未动；已用真实 executor 验证本批实现在
  `view["content"]`/`omitted_rows`/`truncated` 等真实字段上行为正确（见下）。

  **既有测试冲突**（合同直接后果，逐条更新，未削弱其余断言）：
  - `tests/test_m1_otel_demo_contract.py`：`test_profile_constants_match_the_contract`
    （`OBSERVATION_SECONDS` 300→86400）、
    `test_tool_schemas_are_two_function_schemas_named_as_registered`
    （schema 新增 start/end）、
    `test_face_evidence_context_is_the_300s_window_ending_at_the_clock`
    → 改名 `..._24h_frame_ending_at_the_clock`（B2 授权外框变化）、
    `test_scope_window_is_the_policy_window_of_the_input` 及
    `test_factory_scope_is_issued_from_the_lease_the_run_row_and_the_input`
    （共享 `_input()` 改为绕过 `otel_demo_face` 直接构造 `window=WINDOW` 的
    evidence_context，理由同批次 B 合同测试自己的 `_otel_executor` 注释：
    `otel_demo_face` 默认窗口不再是 300s，直接依赖它会偷偷绑定明天的 24h
    帧而非本文件真实录制的夹具窗口）、
    `test_project_traces_records_the_recorded_checkout_search`、
    `test_project_traces_payment_search_puts_payment_spans_first`、
    `test_traces_call_through_the_executor_returns_sampled_spans`
    （B1 撤销 20 span 采样上限，真实计数从 20 改为夹具真实值 90/98）、
    `test_transport_traces_limit_defaults_to_ten` → 改名
    `..._to_twenty`（B1 默认值 10→20，理由：上游 Grafana Tempo trace 搜索
    `params.get("limit") or 20`，无独立上游 Jaeger 值可对齐）、
    `test_transport_traces_refuses_a_bad_limit_without_sending`（parametrize
    去掉 21，改用仍非法的值组合）、
    `test_traces_bad_limit_is_an_error_before_any_request`
    （limit 从 21 改为 -1，21 不再非法）。
  - `tests/test_m1_view_explicit_contract.py`：
    `test_b_complete_search_reports_backend_and_shown_counts`（B1 行数
    20→90，omitted 70→0，与实际字节数一致）、
    `test_b_byte_truncation_leaves_spans_omitted_positive_and_consistent`
    （lead 已预告：16 KiB→100 KiB 后原 20 条重 span 不再触发截断，改用
    40 条并用真实 executor 跑一次量出 `shown=29 omitted=11`，保留"仍触发
    截断"的测试意图）、`test_c_two_incomplete_views_are_listed_in_delivery_order`
    （B5 去重：两个默认参数相同的 tool_call 现在会被判定为重复调用，改用
    不同 `arguments` 恢复"两条独立证据"的原意图）。
  - `tests/test_m1_investigation_context.py`、
    `tests/test_m1_investigation_compaction.py`、
    `tests/test_m1_investigation_continuation.py`、
    `tests/test_m1_investigation_loop.py`
    （`test_the_lease_is_renewed_before_every_live_tool_dispatch`）、
    `tests/integration/test_m1_loop_resume_postgres.py`
    （`test_kill_after_the_tool_result_commits_before_the_next_call_then_resume`）：
    各自的本地 `_rounds`/`_tool_rounds` 夹具用默认 `tool_call()` 复用同一组
    参数模拟"新一轮"，B5 去重后这些轮次被正确判定为重复调用而不再真实派发，
    改为每轮传不同 `arguments`，恢复"每轮都是独立派发"的原意图。
  - `tests/integration/test_m1_otel_demo_contract_postgres.py`：
    `test_a_workbench_run_under_the_otel_profile_records_real_shaped_evidence`
    （B2：真实 workbench 记录的授权窗口从 `WINDOW` 改为
    `Window(WINDOW_END-24h, WINDOW_END)`）。

  **实现判断点**（可逆技术细节，独立决定，一行理由）：
  - B1 视图字节上限统一取 `100 * 1024`（100 KiB，非合同文本给出的十进制
    100_000）：贴合本仓库既有 KiB 幂次常量惯例（24*1024/16*1024/1024*1024），
    换算回约 25,600 tokens，仍与上游 25,000-token 量级同一数量级。
  - trace 注册的 `max_result_bytes` 从 256 KiB 提到 1 MiB（与 metrics 一致）：
    B1 撤销行数上限后，未截断投影记录的原始字节可能超过旧上限；未截断，仅
    放宽，保守选择。
  - B2 省略 start/end 时默认窗口做了"钳制"：`max(frame.start, frame.end-1h)`
    而非直接拒绝更窄的授权外框——保证已有 300s 夹具测试在不传参数时行为
    不变，也符合"不足 1 小时的外框不该无理由拒绝"的直觉；未在合同文本中
    显式要求，记录为实现细节。
  - B2 用 `_query_window()` 在 `_metrics`/`_traces` 内部完成校验/裁剪，未改
    `ReadOnlyToolExecutor._authorize()` 或 `loop.py` 的顶层窗口语义——按 lead
    指示的落点，且合同测试自己的注释里明确排除了在 loop 收窄
    `ToolRequest.window` 这条路径。
  - B3 的模板反馈文本（`REPORT_RETRY_TEMPLATE`）并入了 `prompt_revision` 哈希
    （新增 `report_retry_template` 参数，默认空串不影响未传参调用方）：这是
    新增的模型可见文本，按本仓库既有惯例（`report_contract`/
    `run_coverage_template` 同样参与）理应参与版本哈希，避免一次跨部署的
    resume 在未变版本号的情况下悄悄改变模型看到的文字。核对过仓库内三处
    硬编码 `prompt_revision`/`context_policy_revision` 字符串均为
    `!=`（变更检测）用法，不受影响。
  - B3 诊断详情只对"引用了未交付的 evidence_id"这一种失败原因给出具体值
    （`unsupported_evidence_ids`），其余引用失败原因（target_ref 不在授权
    范围、time_scope_ref 不匹配、非 fact 视图被当作 fact 引用）只给固定原因
    码不给细节：这些情况没有"可以安全复述、不含原始视图内容"的具体值可给。
  - B5 的去重范围仅限当前 attempt 的内存态（`_State.seen_tool_calls`），不在
    `resume()`/压缩折叠后重建：跨 attempt/跨压缩重建需要从已提交行反推原始
    调用参数，folded 摘要（`fold_digest`）只保留参数的 SHA-256 而非原始值，
    重建会既复杂又可能出错；本批 6 个合同测试均不覆盖跨 resume 的去重，界定
    为已知限制而非实现缺口，记录于此以便后续任务评估是否需要补上。
  - B5 的去重结果视图携带原调用的 `evidence_id`（而非只给
    `duplicate_of_evidence_id`）：多处既有共享测试夹具（如
    `report_from_transcript`）假定"每条 tool 消息都有 evidence_id"，携带
    原值既修复了这一假设，也让模型能在报告里直接引用它——真实证据仍只在
    `state.delivered` 里记一次（`adopted` 故意不设 `True`），不会产生重复
    引用。
  - B4：只改了 `compaction_pct`（0.8→0.95），未动 `single_tool_pct`（0.25）
    ——B1 的视图字节上限（≈25k tokens）已经先于这个机制生效，同一份视图永远
    到不了 25% 这个更松的旧上限，核对结果记在
    `opspilot/investigation/context.py` 的 `ContextPolicy` 注释里，不在本文件
    重复。

  **验证**：
  - `.venv/bin/python -m pytest tests/ --ignore=tests/integration -q`：2273
    passed, 3 known test-bugs (above), 2 xfailed（架构欠债标记，与本批无关）。
  - `.venv/bin/ruff check .`/`ruff format --check .`/`.venv/bin/mypy`：本批
    改动的 14 个文件全部通过；`make check` 整体失败仅因
    `docs/evidence/m1-01-replay-ablation/scripts/{heldout_fault,replay}.py`
    两处与本批无关的既有 lint 债务（用 `git stash` 核对：改动前后表现一致）。
  - `M1_DURABLE_POSTGRES=1 .venv/bin/python -m pytest tests/integration -q`
    （本 worktree 55431 PG lab，用完已 `postgres_lab stop`）：204 passed，54
    skipped（未 opt-in 的 M0 测试）。
  - 未执行：6-Run 真实效果测量（本文件"验收"第 3 项）与独立审查——按 lead
    交给我的范围（实现 + `make check`/PG）未覆盖，留给下一个判断点。
