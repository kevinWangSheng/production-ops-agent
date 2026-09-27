# M1-01 指标视图只返回窗内计算点

## 目标

6d 重跑（[证据](../evidence/m1-01-v4-rerun/run.md)）4 个 Run 中 3 个的 P2 来自同一工具缺陷：`metrics_range_query` 从授权窗口起点开始求值，`rate()/increase()` 等范围选择器在前几个点读取窗口前的样本，模型把这些点报成窗内值。本项只修这一处，按上游 HolmesGPT 的做法把聚合交给 PromQL，不增加报告数值校验（用户 2026-09-27 决定：内容质量由验收审查衡量，不做自由文本校验；结构化数值合同方案撤回，分支 `feature/m1-01-numeric-contract` 仅本地保留）。

- worktree：`production-ops-agent-window-points`，分支 `feature/m1-01-window-points`，起点 4431456（PR #57 HEAD）；#57 合并后 rebase 到 `main` 再开 PR。

## 行为合同

1. Prometheus `query_range` 的 `start` = 窗口起点 + L，L = 表达式在一个求值点向前读取的总秒数：普通范围选择器取其长度；子查询 `(<inner>)[a:b]` 取 a + inner 的 L（递归，嵌套相加）；多个并列选择器取最大值。L 超过窗口长度则拒绝（`QUERY_OUT_OF_WINDOW`，与现有超窗规则一致）。例：`max_over_time(rate(x[1m])[4m:30s])` 的 L = 300；`[5m:30s]` 同内层 `[1m]` 的 L = 360，在 300 s 窗口下拒绝（2026-09-27 独立审查 P2 补充）。`end` = 窗口终点；`step` 规则不变。因此每个返回点的范围选择器只读 `[窗口起点, 窗口终点]` 内的样本。
2. L = 窗口长度时 `start == end`，只在窗口终点求值一次，得到整窗聚合值（如 `increase(x[300s])` 为整窗增量，`max_over_time(x[300s])` 为整窗最大值）。
3. 视图字段：`lookback_seconds` 仍为 L；`lookback_start_at` 表示查询读取的最早样本时刻，因此等于窗口起点；第一个返回点时间 ≥ 窗口起点 + L；`source_start_at` 语义不变（第一个返回样本时刻）。
4. 工具描述：删除"首点可能反映窗口前样本"的句子；说明每个点覆盖 `[t − range, t]` 且全部在窗内；照上游写法说明整窗总量、最大、最小值用对应 PromQL 在一次求值中算出，不要自己从点列计算。保持一句一约束的现有风格。
5. 拒绝规则不变：范围选择器超过窗口、`offset`、`@`、step 越界。
6. 不处理：L = 0 时即时选择器的 staleness 回看（Prometheus 默认值，查询不显式声明；上游同样不处理）。
7. 描述变化使 `tool_schema_revision` 变化；fixture profile 不受影响。

## 验收

- 独立合同测试（全新上下文作者，不看实现 diff）：`tests/test_m1_window_points_contract.py`。
- 实现后独立审查 + 一次 `@codex review` 分诊。
- 冻结后重跑 v4 2+2（与 6d 同流程），结果无论通过与否如实记录，M1-01 随之收尾。

## 状态

- 2026-09-27：合同写定，派发测试作者与实现者。
- 2026-09-27：实现落地（`opspilot/tools/otel_demo.py` 的 `_metrics` start = 窗口起点 + L，工具描述与 `values_format`/`cannot_prove` 改写；`opspilot/tools/executor.py` 的 `lookback_start_at` = 窗口起点）。`ruff check`、`ruff format --check`、`mypy` 通过；非 PostgreSQL 测试 2195 passed / 2 xfailed。因合同变化更新的既有测试：
  - `tests/test_m1_otel_demo_contract.py:test_transport_metrics_request_and_response` → 合同 1：`GOOD_EXPR` 含 `[5m]`，请求 `start` 从窗口起点改为窗口起点 + 300 s。
  - `tests/test_m1_otel_demo_lookback.py:test_view_carries_lookback_seconds_and_lookback_start_at` → 合同 3：`lookback_start_at` 从「窗口起点 − L」改为窗口起点。
  - `tests/test_m1_otel_demo_lookback.py:test_window_length_selector_view_is_adopted_and_citable_end_to_end` → 合同 3：同上（`[5m]` 端到端）。
  - `tests/test_m1_6d_prefreeze.py:test_lookback_equal_to_the_requested_window_is_accepted` → 合同 3：L = 窗口长度时 `lookback_start_at` 仍为窗口起点；执行器上限校验不变。
  - `tests/test_m1_otel_demo_lookback.py` 模块 docstring 改为记录语义变迁；`test_metrics_description_states_the_lookback_and_the_refusals`（要求描述含 "before"）未改，新句 "No point reads samples from before the window" 仍满足。
  - 未动 `tool_schema_revision` 钉值测试：合同 7 只要求与旧值不同，现有断言即为 `!=`。
- 2026-09-27：独立合同测试 `fd5c256`（18 项）。红绿对照：`git apply -R` 撤回 d8b6e39 的 opspilot/ 改动后 6 failed / 12 passed（失败均为新合同项：start 偏移 ×3、lookback_start_at、描述句、revision），恢复后 18 passed。
- 2026-09-27：独立审查（Codex 两次容量不足未产出，改由全新上下文 Claude Agent）：主修复正确，P2 嵌套子查询回看漏算 → 合同第 1 条修订 310701b，实现 3145325，独立测试 9cd4941（4 项）。红绿对照：撤回 3145325 的 opspilot/ 改动后 3 failed / 19 passed（并列取最大一项在新旧实现下均成立），恢复后全绿。`make check` 通过；非 PG 测试 2217 passed / 2 xfailed。
- 2026-09-27：**6e 重跑已执行**（候选冻结 `b352230`，`tool_schema_revision` `otel-demo-585e5ef89c39`，与 6d 同流程/同判据，[证据](../evidence/m1-01-window-points-rerun/run.md)）：4/4 发布；**v4 口径 1/4 通过（fault-1 五条全过，其余三次判据 3 失败：P2 合计 6，均为模型对缺失 vs 显式、视图 vs 后端计数的可见范围混淆，无产品归因）；上游口径 4/4**（两正常窗正确判定无故障，两故障窗正确定位 checkout→payment `Charge`，trace id 与独立观察逐一吻合）。6d 的「回看首点」类 P2（3/4 Run）本包为 0，但模型四次都只用 `[300s]`，多点路径未在真实 Run 中触发。费用 0.49 CNY（48.50 → 48.01）。环境已 stop（colima `m0-otel`、55431 均保留数据）。M1-01 完成定义的处置待用户决定。
- 2026-09-27：**第二轮实现落地**（A：`project_traces` 行加 `status_state`；B：`TransportResponse` 新增 `row_unit`/`backend_rows_returned`/`view_fields`，执行器 `_record` 据此写 `backend_spans_returned`/`spans_shown`/`spans_omitted`，`_traces` 提供 `traces_requested`/`backend_traces_returned`/`incomplete_reason`，`_unit_fields_problem` 拒绝与通用字段冲突的键；C：`reports.RUN_COVERAGE_TEMPLATE` + `run_coverage_message`，`DeliveredView` 加 `incomplete`/`truncated`/`inherited`，`loop._final_messages` 与 `context._check_snapshot_hash` 用同一函数，`discipline.prompt_revision` 新增 `run_coverage_template` 参数计入哈希）。新值：`prompt_revision` `prompt-replay-candidate-177e529f603d`，`tool_schema_revision` `otel-demo-44ed0d63b0d3`。`TRACE_PROJECTION` 保持 `otel-demo-traces-v1`（C3 §8 常量测试钉死，行加字段已由 `tool_schema_revision` 反映）。
  - **D 未改（待决）**：`actual_visible_span_count`/`display_max_spans` 只出现在 `PROJECTION_DISCIPLINE`，而它只进两个历史 baseline 变体（`replay-candidate` 不含），且 `tests/test_instruction_discipline.py:182` 按 `holmes_baseline.py` 源码逐字节漂移检查；改句即改变本 Run 变体之外的行为并转红该测试，与 D 的约束自相矛盾，未动。
  - **合同测试 2 项无法在不改断言下转绿（待决）**：`test_sanity_a_tool_round_then_final_round_reaches_the_model_twice` 钉死「最后一条消息 = FINAL_REPORT_INSTRUCTION」的第二轮前形态，实现 C 后必然失败；`test_c_the_final_round_message_is_byte_identical_live_and_rebuilt` 以 `model_requests=2` 中断最终请求后 `resume`，被中断的槽位不释放（`tests/test_m1_investigation_context.py:143` 已钉「in-flight slot stays occupied」），恢复后剩余 0 → `budget_exhausted`，与「completed」断言冲突。逐字节相同另以完整 Run 的已提交最终步 `input_snapshot_hash` 重建 + `run_coverage_message(transcript.delivered)` 直接比对验证为相同（scratch 脚本，非仓库测试）。其余 18 项通过。
  - 因合同 C 更新的既有测试：`tests/test_m1_investigation_loop.py:test_exploration_retry_does_not_consume_the_final_slot` 与 `:test_last_request_is_reserved_for_the_report_and_sends_no_tools` → `messages[-2]` 为 FINAL_REPORT_INSTRUCTION、`messages[-1]` 以 "Run coverage summary" 开头；`tests/test_m1_investigation_compaction.py:test_a_compaction_never_takes_the_reserved_final_report_slot` → 同上；`tests/test_m1_investigation_compaction.py:test_a_rebuild_reproduces_the_compacted_context_byte_for_byte` → 剥离尾部两条消息而非一条；`tests/test_m1_investigation_pairing.py:test_loop_pairs_executor_views_into_the_next_model_request` → 角色尾部多一条 user，tool/assistant 索引各后移一位。

## 第二轮：视图显式表达"未知"与单位（2026-09-27 用户决定）

6e（[证据](../evidence/m1-01-window-points-rerun/run.md)）剩余 6 个 P2 均为模型把"缺失/空/计数"误述。两份独立调研（Codex 多方对比、Claude 上下文与规则调研）结论一致：提示词不能消除，主路径是工具视图显式携带存在性与单位，跨视图计数由运行时算好。用户决定先做下列 A/B/C，不加新规则、不加报告校验器；"按服务分组的缺失序列显式化"待下一次重跑证据再定。本轮继续在本分支，重跑证据另建目录。

### 行为合同

A. **trace 行的状态存在性**：每个 trace 视图行新增 `status_state`，取值 `recorded`（`status_tags` 至少含一个状态键）或 `not_recorded`（不含任何状态键）；`status_tags` 保持原样。工具描述一句说明：`not_recorded` 表示该 span 没有状态标签，按 OTel 语义是 Unset，不等于状态码 0 或 OK。
B. **trace 视图计数带单位**：trace 视图顶层新增 `traces_requested`（= 请求的 limit）、`backend_traces_returned`（后端实际返回的 trace 数）、`backend_spans_returned`（后端返回的 span 总数）、`spans_shown`（视图展示的 span 行数）、`spans_omitted`（后端返回但未展示的 span 数）、`incomplete_reason`（`incomplete` 为 true 时为一句原因，如 "backend returned as many traces as requested; more may exist"，否则为 null）。数值取自投影记录中已有字段，不新增后端请求。既有通用字段（`result_count`、`returned_count`、`incomplete`、`query.limit`）保留不变。工具描述按一句一约束说明各字段单位。
C. **运行级覆盖摘要**：进入最终报告请求时，在 `FINAL_REPORT_INSTRUCTION` 之后追加一条固定模板的 user 消息，列出本 Run 已采纳视图中：`incomplete` 为 true 的 evidence_id、`truncated` 为 true 的 evidence_id、`status` 不为 `ok` 的 evidence_id（附 status），以及视图总数；按交付顺序排列；某类为空写 none。只读取视图的结构化字段，不解析任何文本。实时路径与重启后重建路径产生逐字节相同的消息；继承视图不计入。模板变化计入 `prompt_revision`。
D. **清理**：`opspilot/instructions/discipline.py` 中引用模型视图不存在字段（`actual_visible_span_count`、`display_max_spans`）的句子改为引用 B 中的新字段或删除；不得改变本 Run 实际使用的 prompt 变体之外的行为。

### 验收

- 全新上下文测试作者先按本合同写 `tests/test_m1_view_explicit_contract.py` 并在未实现代码上确认红；之后实现者实现并转绿，不改该文件断言。
- 独立审查 + 冻结后重跑 v4 2+2（6f），与 6e 对比；按服务分组缺失序列类 P2 是否仍出现作为第 2 条的决策依据。
- 2026-09-27：合同 D 不执行（lead 决定）：两处旧字段只出现在 `PROJECTION_DISCIPLINE`，该段只进入两个复刻上游 Holmes 的历史 baseline 变体（本 Run 使用的 replay-candidate 不含），且 `tests/test_instruction_discipline.py:182` 按 `holmes_baseline.py` 逐字节校验；修改会改变实际变体之外的行为，与 D 自身约束冲突。保持原样，记为已知。
- 2026-09-27：第二轮独立审查（全新上下文 Claude Agent）无 P1/P2：A/B 取值与投影同源、对 metrics 与 fixture 零影响；C 实时与重建共用 `run_coverage_message`，重建时对最终步做快照哈希核对，人为改模板后重建报 `INCOMPATIBLE_STATE`（失败而非静默）。可选项：视图溢出为 stub 时不含 B 的计数字段（trace 视图 ≤16 KiB，通常不溢出），记为已知。make check 非 PG 2237 passed；撤回实现后新合同测试 18 红。候选冻结于本提交，重跑证据 `docs/evidence/m1-01-explicit-view-rerun/`。
