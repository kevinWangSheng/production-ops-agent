# M0-03 m003c-normal-01 证据审查

协调者按 [质量审查合同](round-03-quality-review-contract.md) 只读核对。独立子代理 402 失败，本文件不是全新上下文独立 Agent 结论。未读凭据与私有协议。全文留 tmp。

## 交付真实性

- 状态 `investigation_returned`，3 HTTP / 13 工具，`report_request_id=m003c-normal-01-http-3`，finish=stop。
- 报告 SHA-256 `9c8e4c0dca7bd03a1b0aa3469de9fe275cd3a91a38c109cbb198614d6f0457ff` 与 `report-capture-business.json` 一致。
- 13/13 manifest 的 raw/view canonical 与文件 hash 通过；claims 引用的 evidence_id 均已交付。未引用 e1（services 目录）。
- `time-policies` 含 `m003c-normal-window`；fact 的 `time_scope_ref` 均指向该策略。
- 窗 1789109059–1789109359（2026-09-11T06:44:19Z–06:49:19Z）与独立工程观察 `m003-normal-observation.json` 一致。

## 逐 claim

| 项 | 判定 |
|---|---|
| 1 fact | 成立：e2 checkout UNSET increase=114.806…；e9 仅 load-generator/frontend/payment/frontend-proxy 的 ERROR 系列且值为 0，无 checkout ERROR 系列；e5 为 checkout 无 status 分解的 114.81。 |
| 2 fact | 成立：e3 checkout server code0=8.735；e10 `status!="0"` 结果 0 行；e12 七条 checkout 客户端调用均为 code0（Charge/GetQuote/GetCart/Convert/EmptyCart/ShipOrder/GetProduct）。 |
| 3 fact | **P2 可见 span 计数**：e6 为 10 traces / 496 spans / **14 visible** / 482 omitted，backend 各服务 `error_spans_by_listed_status_tags=0`（checkout 132）。sampled 14 条均为 checkout 路径：PlaceOrder **10**、prepare 2、HTTP POST 2，grpc 0 或 HTTP 200。报告写「14 visible 中含 **9** checkout spans」与 10/14 不符。limits 20+14000，truncation 双限成立。 |
| 4 fact | 成立：e8 显示 19 行中三条 POST /api/checkout 均为 200，trace `45ce607c…` / `189ab942…` / `3fc16beb…` 均在 e6 `backend_trace_ids`。 |
| 5 fact | 成立：e11 payment `app_payment_transactions_total` increase=8.735；e9 payment ERROR=0。 |
| 6 fact | 成立：e13 checkout 日志 backend_total_hits=0。 |
| 7 counter | 成立：明确「缺 checkout ERROR 系列是 unknown 不是零」，纠正 M0-02 同类 P2。 |
| 8 counter | 成立：14/496、cart 20/77、proxy **19**/152（model_visible_hit_count=19，不是 20）。 |

gaps 与 summary 将本窗定位为「未观察到失败」而非 healthy，并披露缺 ERROR 系列、checkout 无日志、采样偏差。next_steps 为建议，未执行。

## 分层结论

- 结构：strict report-v2 通过。
- 核心正常观察：有据。独立工程窗同样 checkout UNSET≈114.8、ERROR 系列非 checkout。
- **完整质量 FAIL**：1 项 P2（claim3 可见 checkout span 写成 9，实际 PlaceOrder 10、路径 span 14）。无 P1。
- 可计入 v4「正常 2 次」的结构样本，但质量未过，保留分母。
