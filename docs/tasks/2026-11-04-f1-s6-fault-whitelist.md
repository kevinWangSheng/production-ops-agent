# F1 收尾 S6：保留集新开关白名单与指标型噪声确认门

- 状态：进行中
- 更新日期：2026-11-04
- 依据：F1 收尾方案合同预审（仓库外 `~/.agent/handoffs/opspilot-prereview-f1-closure-plan-2026-11-04.md`）§1 A、§2.2 S6、U2、R3、§5；功能 F1
- 工作区：`feature/F1-s6-new-fault-whitelist`，`/Users/shenghuikevin/dev/AI/poa-s6-whitelist`

## 目标与范围

v2 评测 harness 接受保留集要用的 3 个 flagd 开关，并在不产生错误 span 的噪声开关真正生效后才提交 Run。只改 `scripts/` 与测试夹具；不改答案 schema、产品代码与签名类型（R3）。

## 合同（r1，沿用预审）

- U2：paymentUnreachable 作为 checkout 路径正例的基础故障，沿用 payment 签名，不加噪声门；adManualGc、loadGeneratorFloodHomepage 只作噪声或反例。最终保留哪几个开关以可见性探针（`~/.agent/handoffs/fault-visibility-2026-11-04.md`）为准。
- R3：指标型噪声门只在 harness 里，按开关名查表（`METRIC_NOISE_RULES`）。
- §5：4 个 #432 验收文件的假 flagd ConfigMap 补成新白名单，只改夹具不改断言（lead 指派实现者改，按合同变更处理）。

## 可逆默认（实现者自定）

- 指标门比较：`[生效时刻, 现在]` 与生效前等长窗口上同一 PromQL，观测值 ≥ 倍数 × 基线且 ≥ 下限。理由：与探针的「F 对 B」判定一致，用 rate 消除窗口长度差。
- 最短观测窗 90 s：rate 至少需要几次抓取。
- 每个开关先放一个信号（adManualGc：`jvm_gc_duration_seconds_sum` 速率 ≥5×；洪泛：frontend-proxy 服务端 span 速率 ≥10×），阈值来自预审 §1 A 的探针标准；探针结果出来后按实测调整。
- 指标门通过后的提交期限 = 读到的窗口末端 + FRESHNESS（300 s）。理由：没有 span 可取最新时刻，开关开着时信号持续，与 normal_window 的期限同法。
- 遥测缺 `metric` 读取时门不通过（`METRIC_PROBE_UNAVAILABLE`，尝试记 `NOISE_NOT_OBSERVED`），不静默跳过。
- `tests/unit/test_431_injectors_impl.py` 的 ORIGINAL_V2 是同源夹具，一并补齐；它的白名单钉值测试按新白名单更新。

## 执行进展与证据

- 实现：`scripts/eval_live_lab.py`（`V2_FAULT_FLAGS`、`METRIC_NOISE_RULES`、`metric_gate`、`noise_gate`、`LabTelemetry.metric`），`scripts/eval_live.py`（噪声门分发与期限）。
- 实现侧测试：`tests/unit/test_s6_metric_noise_gate_impl.py`。
- 本机定向测试（2026-11-04）：4 个 #432 验收文件 34 passed；相关 unit/acceptance 286 passed；其余 harness 相关测试 155 passed、13 skipped（需 PG 开关）；新测试 21 passed；ruff、mypy 通过。未跑全量（本机 swap 紧张）。

## 下一步与交接

- 等可见性探针结果，删掉不可见的开关并按实测调整阈值。
- 独立测试作者的验收测试推送后 cherry-pick 进来跑。
