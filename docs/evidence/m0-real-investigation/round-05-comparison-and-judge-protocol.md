# M0-06/B6 上游比较与人工 Judge 校准协议（待批准）

状态：**协议草案，待人工校准；未执行新的模型/trace。** 目标是比较固定 Holmes 上游与候选实现的可观察结果，不比较思维链，也不让 judge 替代权限、恢复和最终状态断言。

## 同条件比较协议

每个案例固定 `IncidentScenario`、OTel/数据窗口、target/scope/权限、tool schema、prompt、knowledge revision、model alias、请求/工具/时间/费用预算、代码和 evaluator hash。上游与候选各运行相同案例与重复次数；任何无法匹配的项必须进入差异表并降低结论等级。失败、放弃、timeout、handoff 进入分母，不从结果集中删除。

### 差异披露表

| 维度 | Holmes 上游 | OpsPilot 候选 | 影响/处置 |
|---|---|---|---|
| loop/状态 | 上游 `ToolCallingLLM` 内存循环 | PG 持久 ModelStep/ToolOperation + v4 bridge | 不能把步骤数直接当质量；需分别报告重建/取消状态。 |
| 工具面 | 固定 Holmes toolsets 与受限 proxy | `otel_*` allow-list + target/scope guard | 接口不同时按 evidence coverage 分层，不称同权限。 |
| 报告协议 | 上游原始报告格式 | `ModelReportV2`/`IncidentOutcome` strict seam | 质量 judge 只评分可观察报告，不评分私有 reasoning。 |
| 证据投影 | 上游 formatter/view | v4 raw/view/hash/context/time policy | 记录实际 model-visible view，缺失字段计 unknown。 |
| 持久恢复 | 上游无本项目 PG 业务断点 | PostgreSQL cross-process recovery | 恢复能力单独比较，不能用上游无该能力判劣。 |
| 模型/参数 | 固定 provider/model/profile | 相同 alias/profile；若不兼容则明示 | 不匹配时只作机制对照，不计算质量非退化。 |
| trace/平台 | 上游原生配置 | LangSmith 白名单出口 | trace 缺失不改写业务结果；平台成本单列。 |

## 已有六份独立审查作为人工标注样本

仅使用已提交的独立审查文本作 rubric 设计样本，不把它们当新的盲测集：

1. [`round-03-independent-review-m003c-normal-02.md`](round-03-independent-review-m003c-normal-02.md) — normal 正向/证据绑定。
2. [`round-03-independent-review-m003e-normal-02.md`](round-03-independent-review-m003e-normal-02.md) — Envoy 字段误读 P2。
3. [`round-03-independent-review-m003d-fault-02.md`](round-03-independent-review-m003d-fault-02.md) — 跨 trace/投影缺口 P2。
4. [`round-03-independent-review-m003e-fault-05.md`](round-03-independent-review-m003e-fault-05.md) — fault 方向与 HTTP 映射 unknown。
5. [`round-04-m004-independent-review.md`](round-04-m004-independent-review.md) — raw evidence 补交前后 hash 可复核性。
6. [`round-02-final-delivery-gate-review.md`](round-02-final-delivery-gate-review.md) — strict seam/报告质量边界。

## Judge rubric 草案（待人工校准）

每份报告由两名人工 reviewer 独立标注，再讨论分歧；不使用模型自评覆盖安全/状态断言。

| 维度 | 0 分 | 1 分 | 2 分 | 证据要求 |
|---|---|---|---|---|
| 事实准确性 | 与 raw/view 冲突 | 方向正确但有可见范围/数值错误 | 每个事实与实际 delivered view 一致 | evidence_id、view/raw/hash、target、window |
| 因果/依赖 | 无依据或把推测写事实 | 方向合理且明确 unknown | 仅在直接证据支持时声明因果 | parent edge、共享 trace、反证 |
| 不确定性 | 虚构健康/恢复或隐藏缺口 | 部分披露缺口 | 明确 unknown/gap/反证/建议 | gaps、SLO/coverage、失败分母 |
| 交接完整性 | 失败被写成 completed | 有 handoff 但缺关键原因 | 状态、控制版本、下一步可回读 | IncidentOutcome、control/audit |
| 证据可复核性 | 链接/hash 无法回读 | 有摘要但缺部分原件 | raw/view/hash 全部可重算 | artifact path/hash、独立复核 |

建议门槛：核心事实准确性与交接完整性不得为 0；总分及非退化规则在人工校准后冻结。此次六样本仅用于校准，不产生正式候选成绩。

## 盲测与污染边界

现有样本已被实现/审查者看到，转为开发 rubric 样本；正式保留集必须由独立 evaluator/OS 身份持有。未完成隔离前不宣称 held-out、blind 或泛化通过。
