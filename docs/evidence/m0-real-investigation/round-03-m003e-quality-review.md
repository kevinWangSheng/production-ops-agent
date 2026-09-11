# M0-03 m003e 真实 Run 质量核对

本文件记录协调者对新窗口 real Run 的结构和证据边界核对；不把模型自述当作独立认证。

## m003e-fault-05

- `investigation_returned`，4 HTTP / 14 工具，`finish_reason=stop`，`assessment_status=completed`，`conclusion=supported`。
- trusted policy `m003e-fault01-window` 已送入模型上下文，fact/counter/rejected claim 均带该 `time_scope_ref`。
- 结论支持 checkout→payment `Charge` code 2、checkout `PlaceOrder` code 13，错误文本来自可见 span；错误增量为窗口估算，不当作唯一请求数。
- 报告明确：没有直接显示 POST `/api/checkout` 的 HTTP 500 行，payment 日志不可用，样本截断且没有 baseline/SLO；不作健康/恢复认证。
- 原先同窗口无 time policy 的 `m003e-fault-04` 保留为失败；未覆盖或改写。

## m003e-normal-02

- `investigation_returned`，3 HTTP / 11 工具，`finish_reason=stop`，`assessment_status=completed`，`conclusion=supported`。
- trusted policy `m003e-normal01-window` 已送入模型上下文，关键 checkout error series 和 PlaceOrder/Charge error rate 均为零且 series 存在。
- 报告明确：trace/log 截断、checkout 日志缺失、部分服务 error series unknown、无 baseline/SLO；只表达查询信号内未发现失败，不作健康/恢复认证。
- 该 Run 避开了历史 normal-01 的 9 vs 14/10 span 计数错误。

## 结论

本组把正常和故障正向样本各推进一例，且修复了“缺 time policy 导致可信运行器拒绝 fact scope”的执行入口问题。完整 M0 仍保留真实日志覆盖、HTTP 直接映射、baseline/SLO 和独立最终质量复核边界；旧失败和旧质量 P2 全部保留。
