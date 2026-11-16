# F3 第 5 步审查者判定（PR-3b）

两次真实 Run 的运行过程、时间线、trace 与余额见 [f3-acceptance-live/run.md](../f3-acceptance-live/run.md)。本文件只记录审查者结论与证据的两项说明。

> 2026-11-16（#646）：`run-1/`、`run-2/` 已换成新提示词 `prompt-replay-candidate-a6b4dfd02030` 下的两次重跑（Run `e6fa260e`、`33922153`，见文末一节）。下面「审查者结论」表判的是 10-24 的两次 Run（`dbf02dca`、`1a9b37f1`），它们的 summary 移到了 [prompt-bef70649a3a8/](prompt-bef70649a3a8/)，内容未改。

## 审查者结论

每次 Run 由两名全新上下文、未参与运行的审查者独立判定，互不交流；审查者 B 使用另一模型。rubric 为冻结版 `docs/testing/f3-competing-cause-rubric.md`（sha256 `b58974b2…1d956`），answer 为 `a35361da…37a`。第 2 步「summary 与结构化分组一致」是合同 r1 第七节语义点 3 的门槛，冻结 rubric 不含此项，2026-10-24 由同一组审查者补判。

| Run | 审查者 | rubric 1–8 | 第 2 步 summary 一致性 | 记录的弱点（不作门槛） |
|---|---|---|---|---|
| Run 1 `dbf02dca` | A | PASS（8/8） | 一致 | 对部署的否定为软否定（disfavoured / cannot be excluded），证据满足 D5-a；fact 7 「returns to 0」只在 2 分钟窗口成立 |
| Run 1 `dbf02dca` | B | PASS（8/8） | 一致 | 同上软否定；summary 中「evidenced by」略强于 hypothesis 11，后文限定与 gaps 防止误读 |
| Run 2 `1a9b37f1` | A | PASS（8/8） | 一致 | summary 中「(config-only change)」强于 claim 19 的保留；若干 e 类数值误差（流量范围、新 Pod 时刻差约 1 分钟） |
| Run 2 `1a9b37f1` | B | PASS（8/8） | 一致 | 24 h 窗口含 Run 1 故障，报告标为更早 episode，只用作否定部署的一条辅助论据，去掉后结论不变 |

合计：第 5 步 2/2；第 2 步审查者门槛两次 Run 均为两名审查者判一致。无审查者分歧。

## 证据说明

- `run_versions` 取自 Run 行持久化的 `versions`（`collect.py` 于 2026-10-24 按独立审查 F2 与 Codex P1 改正；初版取的是收集时代码的 `otel_demo_versions()`）。两份 summary 未重新生成：已交叉核对两次 Run 行 `versions`（`f3-acceptance-live/run-{1,2}/summary.json` 的 `runs[].versions`）均为 `prompt-replay-candidate-bef70649a3a8` / `otel-demo-68ed2ca8f5d6` / `ctx-ctx-policy-v1-a30e65ea52f4`，与已冻结的 `run_versions` 相同。
- `alert_delivery_outcomes` 由 lead 规整为结果字符串列表以对齐判定器约定，原明细保留为 `alert_deliveries`，数据不变。
- `lab_readings` 中 `swap_below_300_mib_observed: false` 与 `p95_above_500_ms_observed: false` 表示「未观察到」，不表示「已排除」：两次 Run 期间无连续监控（`continuous_monitoring: false`），只有开跑前与结束后的点读数。lead 判定不算环境失败，独立审查同意。

## #646 重跑（2026-11-16）

- `run-1/summary.json` 对应 Run `e6fa260e`（事故 `eeceb91c`），`run-2/summary.json` 对应 Run `33922153`（事故 `2e351ada`）。两份都是收集脚本 `collect.py` 在 UI `close` 之后，用只读角色、只读快照事务生成的。`run_versions` 取自 Run 行，等于当前代码的 `otel_demo_versions()`，所以本目录的 `v11-equivalence.json` 不参与判定。`lab_readings` 是每分钟一次的连续读数（`continuous_monitoring: true`），与 10-24 只有点读数不同。
- 确定性判定：`tests/acceptance/test_f3_step5_real_runs.py` 15 条全过。
- **审查者判定（rubric 5–8、第 2 步 summary 一致性）：未做。** 这两次 Run 需要两名全新上下文审查者按冻结 rubric 重新判（U3-a）；10-24 的结论不能沿用到新 Run 上。供审查者注意的事实：两次 `root_cause` 分别写 FeatureFlag `paymentFailure` 和 Deployment `flagd`；两次的 24 小时授权窗里都有更早的 checkout 发布（lab 安装、失败尝试；Run 2 还包含 Run 1 那次发布和故障）；Run 1 晚领取约 19 分钟（授权窗终点早于晚领取）。
