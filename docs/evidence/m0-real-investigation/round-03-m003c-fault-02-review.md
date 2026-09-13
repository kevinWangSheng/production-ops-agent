# M0-03 m003c-fault-02 证据审查

协调者按合同只读核对。独立子代理 402 失败。未读凭据、私有协议或注入答案。

## 交付真实性

- `investigation_returned`，4 HTTP / 20 工具，`m003c-fault-02-http-10`，stop。
- 报告 SHA-256 `78313ea0eb9b039db7ba43c0277d23dbc8019b89e1cb6bba50aaf3fdd99f070c` 与 capture 一致。
- 20/20 hash 通过。policy `m003c-fault02-window`。
- 窗 1789111761–1789112061（07:29:21Z–07:34:21Z）= `m003-fault-02-observation.json`。

## 可诊断故障前提

**本窗不成立。** 独立观察 Charge code2 的 **5m increase=0**（序列存在）。e6/e7 返回 traces 7、spans 358，可见 14/15，**所有服务 error_spans=0**，`raw_has_listed_error_details=false`。e10 increase 中 Charge code2=0、其余 checkout 客户端 code0≈7.49。没有 ≥2 条本窗失败 checkout trace。

## 逐项关键核对

| 项 | 判定 |
|---|---|
| 1 fact | 成立：e20 未变换计数 `rpc_client_duration_milliseconds_count{status!="0"}`，Charge code2 **累计=1**。 |
| 2 fact | **P2**：引用 e4，查询是 `rate(rpc_server_duration_milliseconds_count[5m])`。PlaceOrder code13 **rate=0**，code0 rate≈0.02495。报告写成 code13「nonzero cumulative」且 code0「~0 rate」，数值与语义都反了。累计非零的是 e20 的 Charge，不是 e4 的 PlaceOrder。 |
| 3 counter | 成立：e10 increase Charge code2=0，成功路径非零；明确累计≠本窗事件。这正是 M0-02 要修的混淆，本条做对了。 |
| 4 counter | 成立：e2 本窗 ERROR rate checkout/payment/frontend-proxy=0。 |
| 11 hypothesis | 有界：写明累计路径「真实但与本窗关联未证明」。不把工程注入当结论。 |

可见 span 写成截断样本，未把 query limit 20 当成 14。e11/e12 offset 查询 HTTP 400 保留为缺口/建议，未当成功数据。

## 分层结论

- 结构通过。
- 累计 Charge code2=1 有据；**本窗失败链路无据**。
- **完整质量 FAIL**：P2 一条（claim2 把 e4 的 rate0 写成累计非零 PlaceOrder INTERNAL）。
- **不能计入 v4 故障正向样本**（前提 + 质量）。无 P1。
