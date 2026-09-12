# B4/B5/B7 真实证据独立复核

日期：2026-09-12。全新上下文只读复核 B4/B5/B7 结果、费用/HTTP ledger、PG 权限输出、LangGraph 结果与环境停机记录；未新增模型、trace、PG 或 OTel 操作。

- B4 三项结果均为有限机制证据：恢复、取消迟到拒绝、不兼容 handoff；费用与 HTTP 上界未超，失败和 unknown 费用保留。
- B4 三次 LangSmith 白名单回读均 `TRACE_VERIFIED`，但不扩展为完整产品调查或 M0 通过。
- B5 仅 PG 只读拒绝已执行；K8s/OS 隔离仍缺测/未采购。
- B7 隔离 LangGraph 1.2.11 比较可复现：与现有 loop 均 2 步、2 个持久点、第二步取消，因此推荐推迟；不加入主依赖。

未发现把局部机制证据写成 M0 通过的越界声明；SPEC gate 与 feature passes 保持不变。
