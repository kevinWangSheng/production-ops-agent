# M1-01 6c：报告合同补齐与 metrics 回看语义（v4 包 0/4 后的产品修复）

- 状态：进行中
- 更新日期：2026-09-27
- 依据：[v4 验收记录「待决」](2026-09-26-m1-01-v4-acceptance.md)、[证据](../evidence/m1-01-v4-acceptance/run.md)、C3 §5（指令分层与 revision）与 §8（模型可见面）、[DeepSeek 写法参考](../design/deepseek-flash-prompt-tool-reference.md)、用户 2026-09-26 决定（见下）
- 工作区：`feature/m1-01-report-contract`，worktree `../production-ops-agent-report-contract`（基线 `main` `7172ea8`）

## 目标与范围

v4 包 2+2 真实 Run 0/4：模型两次都正确定位 checkout→payment `Charge`（gRPC 13），但三份报告被产品合同缺口拒绝。用户决定（2026-09-26）：

1. **规则不放松**：缺失/失败/陈旧数据不是合格服务事实。把规则写进模型可见的 L2 报告契约（fact / counter_evidence / rejected_hypothesis 只能引用 `status == ok` 的视图；`no_data`/失败视图只能支撑 gaps 或 unknown），每个视图显式标明是否可作事实证据，`operation_id` 标为不可引用、只引 `evidence_id`。按 C3 §5 bump 相关 revision；文字按 DeepSeek 参考写法。校验器不改。
2. **metrics 回看**：不硬拒（参照上游 Holmes），但描述如实写出窗口起点之前允许的最大回看，视图记录真实的最早源时间；超过允许回看的查询仍拒。
3. 顺带：把 `observe.py` 的故障谓词收紧为「授权 checkout 依赖服务的失败调用 + 父子关系」，可复用版本放 `scripts/`，已合并证据文件不动。

范围外：重跑（下一项）、loop/runner 除模型可见文字与视图字段外的改动、依赖、重构、`passes`。

## 前提与完成条件

- 前提：worktree 从 `origin/main` 建；PG 走 CI；真实 Run 需 OTel 实验环境（colima `m0-otel`，数据保留）与本 worktree 专属 55431 实例。
- 完成条件：新合同测试由全新上下文 Agent 依公开接口写出并先红后绿；三份已录交接报告（normal-2、fault-1、fault-2）在未改的校验器下仍被拒；`make check` 通过；一次有界真实 Run 显示报告发布或说明仍交接的原因；独立审查 P1/P2 处置；PR 至 `main`，CI 与机器人分诊一次，**不合并**。

## 决定与理由（执行者一行理由）

- **决定 (3) 与现有检查冲突，已报 lead**：`source_start_at` 若记为 `window_start − 范围选择器`，执行器 `WINDOW_OUT_OF_SCOPE`（`executor.py` `_validate`）会拒绝整条响应，`eligible_time_policies` 也会剥夺该视图的时间策略，等于对所有 `rate()` 查询硬拒，与「允许有界回看」相反。采用方案 A：`source_start_at` 保持「窗内首个样本」语义（继续受作用域与时间策略检查），新增视图字段 `lookback_seconds`（表达式中最大范围选择器长度，无则 0）与 `lookback_start_at`（`window_start − lookback_seconds`）如实记录窗前读取；描述写明最大回看 = 窗口长度（这是既有 PromQL 守卫 `promql_problem` 已经允许的最小一致上界，超过即拒）。lead 于 2026-09-27 批准方案 A。**方案 B（把 Run 的时间策略窗与执行器作用域窗按最大回看外扩，让 `source_start_at` 字面记 `window_start − 选择器`）已考虑并否决**：它扩大了每个 Run 的授权面（证据上下文与作用域），超出「模型可见文字与视图字段」范围。视图合同：`source_start_at` = 窗内首个返回样本（受作用域与时间策略检查），`lookback_seconds`/`lookback_start_at` = 窗前读取；`TransportResponse` docstring、metrics 工具描述与本记录三处一致。Prometheus 对瞬时选择器的 staleness 回看（服务端默认 5 分钟）不计入 `lookback_seconds`，描述亦未承诺——未确认其对首点取值的影响，列为残余项。
- 规则文字放 L2 而非 L1a：L1a 三个变体是字节冻结的历史模板（`discipline.py` 有冻结哈希断言），且「引用规则」按 C3 §5 表属 L2；`prompt_revision` 由内容哈希自动 bump。
- 视图字段 `citable_as_fact` 跨工具成立（属投影而非某个工具描述），故加在执行器投影并 bump `PROJECTION_REVISION` 至 `m1-01-tool-view-v5`；同时进入超限视图的 stub 保留字段（`context_policy_revision` 随之 bump）。
- `TransportResponse.lookback_seconds` 由适配器按源语义给出，与 `source_start_at` 同一信任级别。

## 执行进展与证据

- 提交 `75a2e91`（`fix: model-visible citation rule, citable_as_fact view flag and metrics lookback semantics [M1-01]`）。改动：`reports.py` L2 契约加三句引用规则；`executor.py` 视图加 `citable_as_fact`（= adopted 且 status ok；拒绝视图恒 false）、`lookback_seconds`/`lookback_start_at`，`TransportResponse.lookback_seconds` 校验非负整数；`otel_demo.py` 新增 `promql_lookback_seconds`、metrics 传输回填该值、两处描述改写（模型可见 `TOOL_SCHEMAS` 与注册表 `ToolDescription`）；`context.py` stub 保留字段加 `citable_as_fact`；`outcomes.py` `PROJECTION_REVISION` v5。`promql_problem` 与 `unsupported_citations` 未改。
- revision（均由内容哈希自动变化）：`prompt_revision` `prompt-replay-candidate-2c26fd0db1e0` → `04a9d1a1a80e`；`tool_schema_revision` `otel-demo-3936d7ae7edb` → `ec37260c67b4`；`context_policy_revision` `ctx-ctx-policy-v1-156004a224ba` → `26cf1129e6c7`；`PROJECTION_REVISION` v4 → v5。
- 合同测试由全新上下文 Agent 依公开接口与决定写出（`tests/test_m1_report_contract_citations.py`、`tests/test_m1_otel_demo_lookback.py`，43 用例）：实现前 31 红 / 12 绿；`git apply -R` 产品补丁复核 31 红，重新 apply 43 绿。12 条始终绿的是回放守卫：normal-2、fault-1、fault-2 三份已录报告在未改的 `unsupported_citations` 下仍被拒，并断言具体原因（前两者 fact 引用 `no_data` 视图；fault-2 引用的全部是 `operation_id`）。
- `make check`：ruff / format / mypy / pytest 全过（2148 passed，258 skipped，2 xfailed）。
- lead 追加的两条端到端测试（`rate(x[5m])` 视图仍可采纳、可引用；超窗选择器仍在请求前被拒）同样由该 Agent 写出，绿。
- 真实 Run（故障案 1 次）：**已发布**，12 claim 全部引用完整 `evidence_id`，fact 类只引 `ok` 视图，两条 `no_data` 视图只进 gaps；10 个 trace id 与独立观察一致；4 请求 0.13 CNY。详见[证据](../evidence/m1-01-report-contract/run.md)。
- 独立审查（全新上下文）：P1 0、P2 1（已处置：产生证据的描述改动随本 PR 提交）、P3 4（处置见证据 run.md「独立审查」）。

## 下一步与交接

- PR 至 `main`，CI，机器人分诊一次；**不合并**（功能 PR，用户门）。
- 残余项（交 lead/用户）：(a) metrics 描述与 `values_format` 按 DeepSeek 参考 §2.5 拆成一句一约束——会再 bump `tool_schema_revision`，建议放在 2+2 重跑之前一并做；(b) Prometheus 瞬时选择器的 staleness 回看不计入 `lookback_seconds`；(c) `PROJECTION_REVISION` 手工编号（既有）；(d) 发布后事故 `state=queued`（v4 包已记）。
- 资源：lab VM 已 `stop`（数据保留）；web/worker 已停；本 worktree 的 55431 实例仍在运行供审查回读，合并后 `postgres_lab stop`；`.env` 未改。
