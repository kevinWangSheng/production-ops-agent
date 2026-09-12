# M0 退出矩阵与 M1-01 拆分

日期：2026-09-11。该矩阵是 [M0 八个工作包](../../plans/m0-validation-plan-2026-09-07.md) 的索引，不是 gate 通过声明，也不修改 `feature_list.json` 的 passes。状态只反映当前提交中可回读的证据；真实模型、替身、离线合同和环境缺测分开记录。

## 工作包矩阵

| 工作包 | 状态 | 证据层级 | 当前依据 | 仍缺什么 |
|---|---|---|---|---|
| 1. 实验合同与证据 | 部分 | 真实 + 替身 | M0-01/M0-02 合同、v4 严格报告、M003/M004 observation 与独立复审（[`first-investigation-v4`](../../testing/first-investigation-v4-2026-09-10.md)、[`round-04 review`](round-04-m004-independent-review.md)） | 八包逐项退出判据此前没有单一矩阵；失败/证据不足仍在各原始记录中，不能合并成通过。 |
| 2. 模型与框架兼容 | 部分 | 真实 Flash + 隔离真实 provider probe + 替身 | Flash 多轮工具/JSON/PG/trace 回读见 [`trace-diagnosis`](../m0-01-live/trace-diagnosis.md)；工作包 2 真实协议摘要见 [`round-06-protocol-results`](round-06-protocol-results.md)；malformed tool-call 合同见 `tests/test_m0_step_store_protocol.py`；B7 固定序列离线比较见 [`ADR-0004`](../../adr/0004-langgraph-orchestration.md) | streaming/工具错误尚未接入产品 StepStore/PG 审计；context compressor 尚不存在；LangGraph 只验证了最小离线步骤，不证明生产 checkpoint/性能收益。 |
| 3. 持久恢复、人工控制与升级 | 部分 | 真实 PG 子集 + 替身 | PG 断点/取消/迟到拒绝见 [`test_m0_step_store_postgres.py`](../../../tests/integration/test_m0_step_store_postgres.py)；B4 三项真实组合见 [`round-05-recovery-results`](round-05-recovery-results.md)；工作包 3 新控制合同见 [`round-06-control-contracts`](round-06-control-contracts.md)；v4 控制审查见 [`round-02-final-pointer-review`](round-02-final-pointer-review.md) | DB 短故障全链路、HealthProfile 持久乱序重放、全局/目标暂停状态机、独立 observer 授权并发仍未形成完整矩阵。 |
| 4. 真实环境与权限 | 部分 | 真实 OTel + 真实 PG + 离线权限合同 | OTel 2.0.2 正常/故障/恢复 observations 与 26 镜像冻结证据；B5 PG 只读角色实际拒绝见 [`round-05-isolation-plan`](round-05-isolation-plan.md)；工具 scope/只读边界回归见 [`round-03-quality-summary`](round-03-quality-summary.md) | Kubernetes RBAC 仍当前环境缺测；Holmes 宿主 OS 隔离明确未测，不能以容器探针替代。 |
| 5. 上游基线与 eval 校准 | 部分 | 真实 Holmes + 离线 rubric | Holmes 固定 checkout 与 M004 独立报告复审；B6 认证线路后续复验返回模型内容见 [`round-06-upstream-auth-followup-results`](round-06-upstream-auth-followup-results.md)；报告质量由新上下文独立审查，正常/故障各 2/2 候选 | 尚无可比的上游最终报告 Run；人工 judge 校准和保留集盲测尚未就绪，当前样本仍是开发集，不代表泛化。 |
| 6. Trace、审计与平台 | 部分 | 真实单链路 + 审计回归 | M0-01 LangSmith 白名单上传/回读 `TRACE_VERIFIED`（[`trace-diagnosis`](../m0-01-live/trace-diagnosis.md)）；v4 raw/view/hash 与业务报告摘要已提交 | Holmes M0-03 真实调查 trace 为 0；平台不可用恢复导出、完整白名单回读和跨 Run 关联尚未闭环。 |
| 7. 资源、费用与运行 | 部分 | 真实用量/时序账本 | [`round-02-final-usage`](round-02-final-usage.json)、[`round-02-activity-timing`](round-02-activity-timing.json)、M004 3 HTTP/9–12 工具记录；工作包 2 新 ledger 5 HTTP/unknown 预留见 [`round-06-protocol-ledger`](round-06-protocol-ledger.json) | 工具总 wall-time 与子任务清理上界未测；供应商账单未对账，实际费用保持 unknown（B8）。 |
| 8. 退出与实施交接 | 证据不足 | 真实 partial + 离线交接 | M004 normal/fault raw evidence 各 7/7 hash 独立匹配，报告仍为 partial 并保留五条 unknown；B4/B5 PG/B7 已有可回读工件；质量复核前半段（raw 尚未补交）是历史，不作为当前通过依据；[`round-04 quality review`](round-04-m004-quality-review.md) 记录补交后复核；SPEC gate 仍 not cleared | 未取得用户对 gate 的最终决定；B6 正式上游比较/盲测、B5 K8s/OS 设施、B8 账单仍缺。 |

状态含义：**真实**=真实软件/模型/PG 运行；**替身**=确定性或 fake transport；**部分**=同一工作包已有可回读证据但仍有明确缺口；**无**=尚未执行；**失败**=执行并保留了失败样例。任何“部分”或“证据不足”不能向上汇总为 M0 通过。

## M1-01 任务拆分与投入估算（待 gate 决定）

以下只做实施准备，不授权实现或新增模型调用。估算为一名工程师的有效工时，未包含用户审核、CI 排队和真实环境/付费等待。

| 子任务 | 交付物 | 估算 |
|---|---|---:|
| 入口与认证提交 | Jinja/SSE 入口、幂等接收、认证边界合同 | 3 h |
| PG 业务状态与断点 | incident/run/step/预算/控制版本持久化与重建 | 5 h |
| 只读工具执行器 | scope、目标解析、查询超时、证据 raw/view/hash 登记 | 4 h |
| Flash 调查 loop | v4 输入/输出绑定、工具消息配对、handoff | 4 h |
| 进度与证据 UI | SSE 游标、报告事实/假设/反证/unknown 展示 | 3 h |
| 追问/纠正/取消 | 人控版本、迟到结果拒绝、历史保留 | 3 h |
| 重启/不兼容处理 | worker 重启、blocked(INCOMPATIBLE_STATE)、恢复测试 | 3 h |
| 安全与验收 | 外部 `IncidentScenario -> IncidentOutcome`、秘密扫描、集成回归 | 3 h |
| **合计（范围）** | — | **28 h** |

投入估算会在 B4–B8 证据和用户 gate 决定后重新校准；不能把该估算当作实施已开始或产品完成。

## 2026-09-12 B4/B5/B7 实际更新

- **工作包 3：部分**：B4-1/B4-2/B4-3 已各执行 1 次真实模型+PG/PG-only 机制验证；三项结果与局限见 [`round-05-recovery-results.md`](round-05-recovery-results.md)。这不是完整恢复、产品调查或 M1 通过。
- **工作包 4：部分**：B5 专属 PG 只读角色 `opspilot_probe_20260912` 的 SELECT 成功、INSERT/UPDATE/DELETE/DDL 实际拒绝；K8s RBAC 仍环境缺测，Holmes OS 隔离仍未执行。见 [`round-05-isolation-plan.md`](round-05-isolation-plan.md)。
- **工作包 6：部分**：B4 三个 Run 的 LangSmith 白名单 DTO POST/同 Run GET 均 `TRACE_VERIFIED`；M0-03 历史调查 trace 仍为 0，不能推广为全链路 trace 通过。
- **工作包 7：部分**：B4 新 ledger 记录 3 模型 HTTP、known cost 0.004293 CNY、1.0 CNY unknown reservation；账单仍未核对。
- **工作包 8：证据不足**：B4/B5/B7 产生了可回读证据，但 B6 同条件比较、B8 账单及完整 M0 gate 条件仍缺，SPEC gate 保持 not cleared。
- **工作包 2 / B7：部分**：隔离 `langgraph 1.2.11` 最小 StateGraph 与现有 loop 在固定替身序列上均为 2 步、2 个持久点、第二步取消；未观察到收益，真实 provider/PG checkpoint/性能仍未测。ADR-0004 推荐推迟。
- **工作包 5 / B6：部分**：候选真实 fixture/PG 链路 2 HTTP 完成；上游认证后续复验已返回模型内容，但未形成可比较最终报告，不能计算同条件质量差异。见 [`round-06-upstream-comparison-results.md`](round-06-upstream-comparison-results.md) 与 [`round-06-upstream-auth-followup-results.md`](round-06-upstream-auth-followup-results.md)。

## 逐项状态拆分

以下把计划中的复合要求拆成可独立标记的条目；“部分”仍不等于工作包通过。

### 工作包 2：模型与框架兼容

- [部分/真实] DeepSeek Flash JSON/tool-call 基本协议：`docs/evidence/m0-01-live/execution.md`。
- [部分/真实] PostgreSQL 组合与 LangSmith 回读：`docs/evidence/m0-01-live/trace-diagnosis.md`。
- [部分/替身] malformed tool-call 容器 fail-closed：`tests/test_m0_step_store_protocol.py`。
- [部分/隔离真实 provider] 流式中断后的真实 provider 续接：`stream-interrupt.json` 首 chunk 中断后新请求返回 HTTP 200；未接入产品 StepStore/PG 审计，第二次为 `length`。
- [部分/隔离真实 provider] 工具错误后的真实 provider 续接：`tool-error.json` 首次真实 tool call、固定 404 消息后第二次 stop；未接入产品 StepStore/PG operation 持久化。
- [证据不足/隔离真实 provider] 上下文压缩后的工具消息配对：`context-compression.json` 只验证手工配对视图被 provider 接受；仓库无 compressor。
- [部分/替身] LangGraph 净收益最小离线对比（B7）：`scripts/m0_lab/langgraph_compare/compare.py`；真实 provider/PG checkpoint/性能仍未测。

### 工作包 3：恢复、人控与升级

- [部分/真实 PG] ModelStep 响应提交前后重建：`tests/integration/test_m0_step_store_postgres.py`。
- [部分/真实 PG] ToolOperation 部分完成重建：同上。
- [部分/真实 PG] 取消/纠正、迟到结果拒绝：同上。
- [部分/替身] no-data/stale 交接与缺 profile→unknown：`tests/test_m0_outcomes.py`。
- [部分/真实 PG] 发布异常转事故与既有事故接管竞争：`test_release_failure_then_takeover_generation_race_keeps_first_control`；旧 generation 被 `CONTROL_CONFLICT` 拒绝。
- [部分/真实 PG + 固定时钟] HealthProfile 变更与乱序采样：`test_health_profile_revision_and_capture_order_are_unknown_with_fixed_clock`；schema/旧版本保真已测，持久观察流乱序仍缺。
- [证据不足/真实 PG fail-closed] 全局/目标暂停与 resume：`test_global_pause_and_resume_are_fail_closed_until_control_contract_exists`；当前 pause/resume API 尚不存在，未把 fail-closed 输入拒绝写成状态机通过。
- [部分/真实 PG] 单独观察授权并发：`test_observer_authorization_cannot_borrow_investigation_identity`；investigation identity 不可借用，独立 observer 授权 API 仍缺。

### 工作包 4：真实环境与权限

- [部分/真实 OTel] 固定 OTel Demo 2.0.2、正常/故障/恢复观察。
- [部分/替身] 目标/scope/只读边界拒绝合同。
- [无] DB 写权限拒绝的实际输出。
- [无] Kubernetes RBAC 拒绝（当前环境缺测）。
- [无] Holmes 宿主 OS 隔离拒绝。

### 工作包 5：上游基线与评测

- [部分/真实] 固定 Holmes checkout 与报告质量独立复核。
- [无] Holmes 与候选的同条件运行比较。
- [部分/替身] 开发 rubric/失败分母/unknown 语义。
- [待人工校准] judge rubric，使用现有 6 份独立审查样本。
- [无] 保留集盲测与污染检测。
