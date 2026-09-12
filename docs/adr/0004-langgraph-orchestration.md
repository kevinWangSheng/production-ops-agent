# ADR-0004：LangGraph 编排比较（草案）

状态：**待用户决定**。本 ADR 只记录 M0 离线准备，不改变已批准的共享调查 loop，也不向 `uv.lock` 主依赖组添加 LangGraph。

## 背景

M0 计划要求验证 LangGraph 对当前 PostgreSQL 业务恢复、取消和步骤持久点的净收益。比较必须使用同一固定替身工具序列，不能用模型质量或隐藏推理替代外部状态。

## 离线比较

脚本 [`scripts/m0_lab/langgraph_compare/compare.py`](../../scripts/m0_lab/langgraph_compare/compare.py) 固定 `otel_services → otel_traces → otel_metrics`，在第 2 步取消，统计现有 loop 的步骤重建、取消状态和持久点数量；执行结果见 [`round-05-langgraph-compare.json`](../evidence/m0-real-investigation/round-05-langgraph-compare.json)。当前环境未安装 `langgraph`，脚本返回 `LANGGRAPH_EXTRA_UNAVAILABLE`；即使未来安装 extra，在补入真实图实现前也会返回 `LANGGRAPH_COMPARISON_NOT_IMPLEMENTED`，不伪造候选成绩；模型/工具 HTTP 均为 0。

## 决定

**推荐推迟**（待用户决定）：当前离线数据为现有 loop 2 个步骤、2 个持久点、在第 2 步取消；LangGraph 候选为 `null`，原因 `LANGGRAPH_EXTRA_UNAVAILABLE`，因此没有可量化的步骤/持久点收益。即使未来依赖可导入，脚本在真实图实现前也返回 `LANGGRAPH_COMPARISON_NOT_IMPLEMENTED`，不会制造伪比较。没有可复现的同条件基准和明确收益前不增加依赖；若用户批准继续，使用隔离 extra（例如 `uv run --with langgraph`）实现真实候选后再比较，最终采用与否仍待确认。

## 后果

- 正面：保持主依赖、锁文件和运行时边界稳定，避免为比较建设第二套平台。
- 负面：本 ADR 暂无 LangGraph 真实实现成绩，不能声称编排收益已验证。
- 安全/费用：脚本不读凭据、不访问模型/工具、不采购；后续 extra 安装和任何实验仍需独立审查。
