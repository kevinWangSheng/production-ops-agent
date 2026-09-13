# ADR-0004：LangGraph 编排比较（草案）

状态：**待用户决定**。本 ADR 只记录 M0 离线准备，不改变已批准的共享调查 loop，也不向 `uv.lock` 主依赖组添加 LangGraph。

## 背景

M0 计划要求验证 LangGraph 对当前 PostgreSQL 业务恢复、取消和步骤持久点的净收益。比较必须使用同一固定替身工具序列，不能用模型质量或隐藏推理替代外部状态。

## 离线比较

脚本 [`scripts/m0_lab/langgraph_compare/compare.py`](../../scripts/m0_lab/langgraph_compare/compare.py) 固定 `otel_services → otel_traces → otel_metrics`，在第 2 步取消，统计步骤、持久点和取消状态；使用 `uv run --with langgraph` 隔离安装 `langgraph 1.2.11` 后，现有 loop 与真实最小 StateGraph 均为 2 步、2 个持久点、同样取消。结果见 [`round-05-langgraph-compare.json`](../evidence/m0-real-investigation/round-05-langgraph-compare.json)；模型/工具 HTTP 为 0，未改 `uv.lock`。

## 决定

**推荐推迟**（待用户决定）：隔离 `langgraph 1.2.11` 的最小 StateGraph 与现有 loop 在固定序列上均为 2 个步骤、2 个持久点、同样在第 2 步取消，未观察到编排收益。该比较只覆盖离线状态模型，不证明真实 provider、PG checkpoint 或生产性能。建议暂不加入主依赖；若用户需要扩大比较，再另立性能/恢复合同，最终采用与否仍待确认。

## 后果

- 正面：保持主依赖、锁文件和运行时边界稳定，避免为比较建设第二套平台。
- 负面：本 ADR 只有最小离线图实现，不能声称真实 provider/恢复性能收益已验证。
- 安全/费用：脚本不读凭据、不访问模型/工具、不采购；后续 extra 安装和任何实验仍需独立审查。
