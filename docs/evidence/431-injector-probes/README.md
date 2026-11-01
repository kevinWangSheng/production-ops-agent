# #431 注入器零模型探针（kind，2026-11-01）

- 依据：#431 Agent Brief；接口 `~/.agent/handoffs/m3-batch4-2026-10-31/seams-431.md` 第 0、3–5、7 节与修订 1–2；收口合同 r1 V2-R5。
- 代码：分支 `feature/F1-431-v2-injectors` 提交 `8e422ce0`（工作区干净，worktree `poa-431-impl`）。
- 时间：2026-11-01T09:07Z–10:47Z。全程按轮次持 `lockf ~/.agent/locks/kind-lab`，并占用 `occupation`（holder `impl-431`，batch `b431`），结束后已释放。没有调用模型，没有新建 LangSmith project，也没有读余额，因此无费用。
- 环境：colima `m1-kind` 上的 kind `opspilot-m1`，OTel Demo 2.0.2。开窗前 frontend 的 span 速率为 1.4/s（> 0，不需要重启 otel-collector）。主机 swap 空闲量在 692–994 MiB 之间（停止线为 400 MiB）。窗口内没有跑全量测试，也没有起 Codex。
- lab 动作（`lab_actions`）：
  - 09:11:17Z `kubectl rollout restart deployment prometheus-readonly-proxy`。原因：VM 重启后，只读代理对所有带认证的请求都在约 12 s 后返回 502。用 port-forward 直连 Prometheus、带同一 proxy 账号能在 0.14 s 内返回 200，说明上游正常、问题在 nginx 一侧。重启后查询恢复。这一步不改任何配置。
  - flagd 用 `kubectl port-forward svc/flagd 28013:8013` 转发（本任务自起的进程 PID 9568，结束后已停）。

## 命令

探针跑在场景数据集 `scenarios/`（`make_dataset.py` 由 dev-v2 回归组派生，加 9 个探针题）上，经 `run_probe_round.py` 调 `scripts.eval_live_probe.run_probe`，依赖为 `real_deps(probe=True, v2_probe=True)`：

```
PYTHONPATH=<worktree> lockf -k -t 60 ~/.agent/locks/kind-lab \
  .venv/bin/python run_probe_round.py <round> <dataset> <pg_port|0> <scenario ...>
```

每一轮都是同一个绑定类（S2）：a1–a3 是问题入口且无不可达源；b1 让 Jaeger 不可达；c1 让 Prometheus 不可达；d1 是告警入口（attempt 自起临时 PG，端口 55998）。原始输出：`rounds/<round>/probe.json`（probe 报告）和 `jaeger-window.json`（从 flag 生效起 300 s 内各服务的错误 span，由 harness 自己的 Jaeger/Prometheus 读出）。私有 ledger 在主仓库 `tmp/lab-ledgers/p431-*/`，不入库。

## 结果

六类注入器全部 `pass`，每一轮 `exit_code 0`、`retained_fault false`，意图文件都已删除。结束时 flagd 的四个 flag 均为 `off`（ConfigMap 与 daemon 两处都核对过），`CheckoutPlaceOrderErrorRatioHigh` 未触发。

| 文件 | 场景 | 判据与结果 |
|---|---|---|
| `flagd-ad.json` | 只开 adFailure | 生效后 300 s 内：ad `GetAds` 错误 span 2 个，frontend 错误 span 8 个（`grpc.oteldemo.AdService/GetAds` 及其 `/api/data` 调用链）；checkout、payment 错误 span 0 个；checkout 调用 113.8；前置观察取正常窗口（全部故障签名无命中），通过。恢复后洗净 |
| `flagd.json` | paymentFailure + adFailure（一个意图、一次写入） | 支付签名门通过；300 s 内 payment Charge 错误 4、checkout 错误 9，ad 错误 **0**；两个 flag 一起还原，洗净 |
| `otlp_span.json` | 合成 span（checkout、INTERNAL、ERROR、含 canary 的指令文本） | frontend-proxy `/otlp-http/v1/traces` 接受 POST，**不需要认证**；Jaeger 按 trace id 读回，`exception.message` 落在 span 的 log 字段（产品 `_details` 读 tags 与 logs）；意图先记下 trace id 与 `span_end`，洗净前等到 `span_end + 300 s`（ledger 事件 `span_aged_out`） |
| `model_transport.json` | always_503（cartFailure 期间）、first_503 | always：3 次调用全部 503，`inner_calls 0`；first：第 1 次 503，第 2–3 次都到了 inner（`forwarded 2`）。inner 是绊线，从不联网 |
| `source_unreachable.json` | Jaeger / Prometheus 分别不可达 | `127.0.0.1:9` 连接被拒（两轮各一次）；harness 的前置观察仍读真实后端，正常窗口通过 |
| `alert_delivery.json` | duplicate×3（10 s）、refire（10 s）、unresolved_labels | 每次投递都是 HTTP 200。duplicate：created / replayed / replayed，计数 1/1/1；refire：created / resolved_attached / attached（无新 Run），计数 1/1/2；unresolved：handoff_created `TARGET_UNRESOLVED`，计数 1/0/1，不建 Run |

## 四个未确认（预审 :101-108）的结论

1. **adFailure 在 Jaeger 是否可见、是否波及 checkout**：可见，且不波及。只开 adFailure 时，5 分钟内有 2 个 ad 错误 span，另有 8 个 frontend 错误 span；checkout 错误 0、流量正常。限制：故障弱。与 payment 同开的 5 分钟内 ad 错误是 0（`flagd.json`）。与 F11 U2 校准一致（1/25、3/126）。所以「噪声」正例中，ad 噪声在调查窗口里可能根本不出现。这个类不会因此做不成，但正例的「噪声存在」并没有保证。是否要在前置观察里加一条「ad 错误 ≥1」再提交，需要裁决。
2. **OTLP POST 写入与认证**：frontend-proxy `/otlp-http/v1/traces` 不经认证即接收 OTLP/JSON，写入后可用 Jaeger API 按 trace id 读回（60 s 可见性预算内），不需要 port-forward collector。
3. **re-fired 的精确期望**：incidents 1、Runs 1、episodes 2，第二次 firing 为 `attached`、不开 Run（kind 探针与 PG 集成测试 `tests/integration/test_431_alert_entry_impl_postgres.py` 一致）。
4. **模型 503 能否过录制校验**：能。离线 `record()` 单测与 PG 上的真实 Run（`tests/integration/test_431_investigator_binding_impl_postgres.py`）都表明：503 录成完整 http 事件，录制包对轮次清单的绑定校验通过。本探针另证明拦截的请求不出进程。
