# M0-03 m003b-fault-01 证据审查

协调者按合同只读核对。独立子代理 402 失败。未读凭据、私有协议或注入答案。

## 交付真实性

- `investigation_returned`，4 HTTP / 18 工具，`m003b-fault-01-http-4`，stop。
- 报告 SHA-256 `63cc6321494eba41e3d69601556e90a891525466ae9ae688eaafebd4a49df4cb` 与 capture 一致。
- 18/18 hash 通过；引用 ID 均已交付。policy `m003-historical-window`。
- 窗 1789109827–1789110127（06:57:07Z–07:02:07Z）= 独立观察 `m003-fault-observation.json`。

## 可诊断故障前提

**不成立。** 独立工程 `checkout-rpc` 仅 grpc 0（含 Charge）；calls 中 checkout 无 ERROR 增量。模型可见 e2：6 traces / 282 spans / 14 visible / 268 omitted，**全部服务 `error_spans_by_listed_status_tags=0`**。e4 proxy 19 显示行无 POST /api/checkout 5xx。

v4 要求故障案先有 ≥2 条不同失败 checkout trace 再给调查者症状。本窗没有该前提，不能记「可诊断故障正向通过」，即使报告内部自洽。

## 报告与可见证据

报告结论是本窗**未显示** checkout HTTP 500 / 失败依赖，recommendation EventStream increase≈1.249 为唯一非零 ERROR 增量；14 条可见 checkout 路径 span 为 grpc 0 / HTTP 200。这与独立观察和 e2/e4 一致。visible 写成 14 而非 20。assessment=completed、conclusion=partial，未认证恢复。

不把「没看到故障」升级成「故障已定位」。本 Run 实质是一次贴了故障标签的正常窗观察。

## 分层结论

- 结构通过。
- 核心「本窗无可见 checkout 失败」有据。
- **不能计入 v4 故障 2 次**：可诊断前提失败。
- 报告质量就「所见」而言未发现新的 20/349 或累计/窗口混淆；因前提失败，整包故障侧仍 FAIL。无 P1。
