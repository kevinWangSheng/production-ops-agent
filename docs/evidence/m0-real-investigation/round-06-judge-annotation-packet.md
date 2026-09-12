# M0 Judge 人工校准标注包（待人工确认）

用途：把已经看过的 6 份独立审查文本整理为 rubric 校准样本。它们不是 held-out 集，也不产生正式候选成绩；人工 reviewer 应逐条确认/修改临时分数，安全、权限、恢复和最终状态仍由确定性断言负责。

评分维度来自 [`round-05-comparison-and-judge-protocol.md`](round-05-comparison-and-judge-protocol.md)：事实准确性、因果/依赖、不确定性、交接完整性、证据可复核性，各 0–2 分。核心事实准确性与交接完整性不得为 0。

| 样本 | 临时事实 | 临时因果 | 临时不确定性 | 临时交接 | 临时可复核 | 临时总分 | 预填理由 | 人工确认/改分 |
|---|---:|---:|---:|---:|---:|---:|---|---|
| `round-03-independent-review-m003c-normal-02.md` | 2 | 1 | 2 | 2 | 2 | 9/10 | 正常报告与可见 evidence 对齐；因果仅作有限方向判断，保留 coverage/SLO gaps。 | ☐ |
| `round-03-independent-review-m003e-normal-02.md` | 1 | 1 | 2 | 2 | 2 | 8/10 | Envoy 字段误读导致一个可见数值错误；unknown、交接和 hash 仍披露。 | ☐ |
| `round-03-independent-review-m003d-fault-02.md` | 1 | 1 | 1 | 2 | 1 | 6/10 | 故障方向部分合理，但跨 trace 关联与投影省略未被直接观察；缺口披露不完整。 | ☐ |
| `round-03-independent-review-m003e-fault-05.md` | 2 | 1 | 2 | 2 | 2 | 9/10 | Charge/PlaceOrder 方向有可见证据；直接 parent edge 与内部触发原因仍明确 unknown。 | ☐ |
| `round-04-m004-independent-review.md` | 2 | 2 | 2 | 2 | 2 | 10/10 | raw evidence 补交后 7/7 hash 可重算；结论保持 partial，未越界为健康认证。 | ☐ |
| `round-02-final-delivery-gate-review.md` | 2 | 1 | 2 | 2 | 2 | 9/10 | strict seam/报告边界和历史失败披露清楚；真实生产因果与完整恢复明确不在证据内。 | ☐ |

## 人工确认步骤

1. 两名 reviewer 独立填写“人工确认/改分”和一句理由，再讨论差异；不得根据同一分数反向修改候选。
2. 将争议项保留在附注，不用平均分隐藏分歧；记录 reviewer、日期、样本 hash。
3. 校准完成前 rubric、门槛和非退化规则不冻结；本包不能替代正式保留集盲测。
