# M0 退出矩阵与 M1-01 拆分

日期：2026-09-11。该矩阵是 [M0 八个工作包](../../plans/m0-validation-plan-2026-09-07.md) 的索引，不是 gate 通过声明，也不修改 `feature_list.json` 的 passes。状态只反映当前提交中可回读的证据；真实模型、替身、离线合同和环境缺测分开记录。

## 工作包矩阵

| 工作包 | 状态 | 证据层级 | 当前依据 | 仍缺什么 |
|---|---|---|---|---|
| 1. 实验合同与证据 | 部分 | 真实 + 替身 | M0-01/M0-02 合同、v4 严格报告、M003/M004 observation 与独立复审（[`first-investigation-v4`](../../testing/first-investigation-v4-2026-09-10.md)、[`round-04 review`](round-04-m004-independent-review.md)） | 八包逐项退出判据此前没有单一矩阵；失败/证据不足仍在各原始记录中，不能合并成通过。 |
| 2. 模型与框架兼容 | 部分 | 真实 Flash + 替身 | Flash 多轮工具/JSON/PG/trace 回读见 [`trace-diagnosis`](../m0-01-live/trace-diagnosis.md)；malformed tool-call 合同见 `tests/test_m0_step_store_protocol.py`；持久配对：无可单列证据 | 流式中断、工具错误续接的真实组合证据不足；LangGraph 净收益尚未验证（见 B7）。 |
| 3. 持久恢复、人工控制与升级 | 部分 | 真实 PG 子集 + 替身 | PG 断点/取消/迟到拒绝见 [`test_m0_step_store_postgres.py`](../../../tests/integration/test_m0_step_store_postgres.py)；v4 控制审查见 [`round-02-final-pointer-review`](round-02-final-pointer-review.md) | DB 短故障、发布→事故竞争、no-data/stale 交接、HealthProfile 乱序、全局/目标暂停和单独观察并发尚未形成完整矩阵。 |
| 4. 真实环境与权限 | 部分 | 真实 OTel + 离线权限合同 | OTel 2.0.2 正常/故障/恢复 observations 与 26 镜像冻结证据；工具 scope/只读边界回归见 [`round-03-quality-summary`](round-03-quality-summary.md) | DB 写拒绝、Kubernetes RBAC 拒绝的实际输出缺失；Holmes 宿主 OS 隔离明确未测，不能以容器探针替代。 |
| 5. 上游基线与 eval 校准 | 部分 | 真实 Holmes + 离线 rubric | Holmes 固定 checkout 与 M004 独立报告复审；报告质量由新上下文独立审查，正常/故障各 2/2 候选 | 同条件上游比较、人工 judge 校准和保留集盲测尚未就绪；当前样本仍是开发集，不代表泛化。 |
| 6. Trace、审计与平台 | 部分 | 真实单链路 + 审计回归 | M0-01 LangSmith 白名单上传/回读 `TRACE_VERIFIED`（[`trace-diagnosis`](../m0-01-live/trace-diagnosis.md)）；v4 raw/view/hash 与业务报告摘要已提交 | Holmes M0-03 真实调查 trace 为 0；平台不可用恢复导出、完整白名单回读和跨 Run 关联尚未闭环。 |
| 7. 资源、费用与运行 | 部分 | 真实用量/时序账本 | [`round-02-final-usage`](round-02-final-usage.json)、[`round-02-activity-timing`](round-02-activity-timing.json)、M004 3 HTTP/9–12 工具记录 | 工具总 wall-time 与子任务清理上界未测；供应商账单未对账，实际费用保持 unknown（B8）。 |
| 8. 退出与实施交接 | 证据不足 | 真实 partial + 离线交接 | M004 normal/fault raw evidence 各 7/7 hash 独立匹配，报告仍为 partial 并保留五条 unknown；质量复核前半段（raw 尚未补交）是历史，不作为当前通过依据；[`round-04 quality review`](round-04-m004-quality-review.md) 记录补交后复核；SPEC gate 仍 not cleared | 未取得用户对 gate 的最终决定；B4 真实组合、B5 权限设施、B6 比较/盲测、B7 LangGraph、B8 账单仍是后续入口。 |

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

## 逐项状态拆分

以下把计划中的复合要求拆成可独立标记的条目；“部分”仍不等于工作包通过。

### 工作包 2：模型与框架兼容

- [部分/真实] DeepSeek Flash JSON/tool-call 基本协议：`docs/evidence/m0-01-live/execution.md`。
- [部分/真实] PostgreSQL 组合与 LangSmith 回读：`docs/evidence/m0-01-live/trace-diagnosis.md`。
- [部分/替身] malformed tool-call 容器 fail-closed：`tests/test_m0_step_store_protocol.py`。
- [无] 流式中断后的真实 provider 续接。
- [无] 工具错误后的真实 provider 续接。
- [无] LangGraph 净收益对比（B7）。

### 工作包 3：恢复、人控与升级

- [部分/真实 PG] ModelStep 响应提交前后重建：`tests/integration/test_m0_step_store_postgres.py`。
- [部分/真实 PG] ToolOperation 部分完成重建：同上。
- [部分/真实 PG] 取消/纠正、迟到结果拒绝：同上。
- [部分/替身] no-data/stale 交接与缺 profile→unknown：`tests/test_m0_outcomes.py`。
- [无] 发布异常转事故与既有事故接管竞争。
- [无] HealthProfile 变更与乱序采样。
- [无] 全局/目标暂停与 resume。
- [无] 单独观察授权并发。

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
