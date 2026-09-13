# Round 05 文档与脚本独立复审

日期：2026-09-12。复审对象为 D1–D3、B4–B7 文档/脚本；全新上下文、只读，未运行模型、trace、m0-otel 或付费实验。

## 结论

- `m0-exit-matrix.md` 已按工作包和复合要求拆分状态，明确 partial/证据不足不能汇总为 M0 通过；关键相对链接可回读。
- B4 合同写明三项各最多 2 HTTP、各 2 CNY、总 6 CNY、trace 白名单、停机保全和用户批准门，状态待批准未执行。
- B5 将 PG 写拒绝、K8s RBAC 和 Holmes OS 隔离分别标为未执行/环境缺测/候选方案，不采购。
- B6 有差异披露表、6 份已见独立审查样本和待人工校准 rubric，不冒称盲测。
- B7 实际运行 0 model/tool HTTP、`LANGGRAPH_EXTRA_UNAVAILABLE`、结论“推迟”；脚本后续已改为即使依赖可用也不伪造 `compared`，除非加入真实图实现。

## 处置与限制

初审曾指出 B7 可用依赖分支会复用现有 loop 并错误标记 `compared`；已在后续修复中改为 `LANGGRAPH_COMPARISON_NOT_IMPLEMENTED` fail-closed，当前结果仍为 blocked_dependency。B5 SQL 只提供预期输出，未创建临时角色；实际拒绝证据需用户批准设施/凭据边界后执行。该复审不证明真实 PG/K8s/OS 权限、模型兼容性、trace、账单或产品运行。
