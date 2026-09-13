# M0-03 m003c-normal-02 证据审查

协调者按合同只读核对。独立子代理 402 失败。未读凭据与私有协议。

## 交付真实性

- `investigation_returned`，3 HTTP / 16 工具，`m003c-normal-02-http-6`，stop。
- 报告 SHA-256 `b7bc1b5699cdb9e4d12255472316435b662d4cd2aa41f41b53288c2420ac0f0f` 与 capture 一致。
- 16/16 hash 通过；claims 引用 ID 均已交付。窗与 policy `m003c-normal-window` 同 normal-01，但是独立 Run/独立 view。

## 逐 claim

| 项 | 判定 |
|---|---|
| 1 fact | 成立：e2 checkout UNSET increase=114.806；ERROR 分解无 checkout 系列。 |
| 2 fact | 成立：PlaceOrder server code0 increase≈8.74；七条 checkout 客户端 bucket 均为 code0。 |
| 3 fact | 成立于组合引用：payment 路径成功。e7（非 e6）trace backend payment `span_count=20`、`error_spans_by_listed_status_tags=0`；e16 为交易/Charge 成功信号。e6 本 Run 是 metric 语义包装，trace 计数来自 e7/e16，不构成把 metric 当 span。 |
| 4 fact | 成立：e7 10/496/14/482，checkout 132 spans error=0；可见 PlaceOrder grpc 0。未把 20 的 query limit 写成可见数。 |
| 5 fact | 成立：e8 三条 POST /api/checkout 200，trace ID 与 checkout 窗一致。 |
| 6 fact | 成立：cart 20 条显示均为 Information。 |
| 7 hypothesis | 合理：约 8.7 PlaceOrder 与 114.8 span calls 均为 OK，且限定为已观察记录。 |
| 8 hypothesis | 合理覆盖论证：482/496 omitted，不是观察到的失败。 |
| 9 recommendation | 建议未执行；133/152 未显示日志 = 152−19，与 e8 model_visible=19 一致。 |

未认证 healthy/恢复。未发现把缺 series 写成 ERROR=0、或 20/349 类计数错误。

## 分层结论

- 结构通过。
- 核心正常观察有据，且与 normal-01 同窗独立一致。
- **完整质量通过（本 Run）**：0 P1/P2。P3 不记。
- 可作为 v4 正常样本 1/2 的质量通过项；整个包仍要故障 2 次都过。
