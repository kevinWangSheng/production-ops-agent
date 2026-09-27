# M1-01 指标视图只返回窗内计算点

## 目标

6d 重跑（[证据](../evidence/m1-01-v4-rerun/run.md)）4 个 Run 中 3 个的 P2 来自同一工具缺陷：`metrics_range_query` 从授权窗口起点开始求值，`rate()/increase()` 等范围选择器在前几个点读取窗口前的样本，模型把这些点报成窗内值。本项只修这一处，按上游 HolmesGPT 的做法把聚合交给 PromQL，不增加报告数值校验（用户 2026-09-27 决定：内容质量由验收审查衡量，不做自由文本校验；结构化数值合同方案撤回，分支 `feature/m1-01-numeric-contract` 仅本地保留）。

- worktree：`production-ops-agent-window-points`，分支 `feature/m1-01-window-points`，起点 4431456（PR #57 HEAD）；#57 合并后 rebase 到 `main` 再开 PR。

## 行为合同

1. Prometheus `query_range` 的 `start` = 窗口起点 + L，L = 表达式中最长的范围或子查询选择器秒数（现有 `promql_lookback_seconds`，已保证 ≤ 窗口长度）；`end` = 窗口终点；`step` 规则不变。因此每个返回点的范围选择器只读 `[窗口起点, 窗口终点]` 内的样本。
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
