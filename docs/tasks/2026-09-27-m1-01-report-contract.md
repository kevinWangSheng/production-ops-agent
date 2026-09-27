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

- **决定 (3) 与现有检查冲突，已报 lead**：`source_start_at` 若记为 `window_start − 范围选择器`，执行器 `WINDOW_OUT_OF_SCOPE`（`executor.py` `_validate`）会拒绝整条响应，`eligible_time_policies` 也会剥夺该视图的时间策略，等于对所有 `rate()` 查询硬拒，与「允许有界回看」相反。采用方案 A：`source_start_at` 保持「窗内首个样本」语义（继续受作用域与时间策略检查），新增视图字段 `lookback_seconds`（表达式中最大范围选择器长度，无则 0）与 `lookback_start_at`（`window_start − lookback_seconds`）如实记录窗前读取；描述写明最大回看 = 窗口长度（这是既有 PromQL 守卫 `promql_problem` 已经允许的最小一致上界，超过即拒）。
- 规则文字放 L2 而非 L1a：L1a 三个变体是字节冻结的历史模板（`discipline.py` 有冻结哈希断言），且「引用规则」按 C3 §5 表属 L2；`prompt_revision` 由内容哈希自动 bump。
- 视图字段 `citable_as_fact` 跨工具成立（属投影而非某个工具描述），故加在执行器投影并 bump `PROJECTION_REVISION` 至 `m1-01-tool-view-v5`；同时进入超限视图的 stub 保留字段（`context_policy_revision` 随之 bump）。
- `TransportResponse.lookback_seconds` 由适配器按源语义给出，与 `source_start_at` 同一信任级别。

## 执行进展与证据

- 待补。

## 下一步与交接

- 待补。
