# F3 第 5 步审查者判定（PR-3b）

两次真实 Run 的运行过程、时间线、trace 与余额见 [f3-acceptance-live/run.md](../f3-acceptance-live/run.md)。本文件只记录审查者结论与证据的两项说明。

> 2026-11-17（#695 + #690，r3 D21）：#676 合同 r2 的三份（`prompt-replay-candidate-8688f6e52275`，Run `8b06fee1`、`41b97848`、`9d3c1236`）移到了 [prompt-8688f6e52275/](prompt-8688f6e52275/)，内容未改；`run-1/`–`run-3/` 留给 r3 新 revision（`860b743085d1`）的重跑，入库前本目录无当前 revision 的 Run。
>
> 2026-11-16（#676 r2，D11）：#676 第一版（`prompt-replay-candidate-14eb2a0a6afd`）的两份移到了 [prompt-14eb2a0a6afd/](prompt-14eb2a0a6afd/)，内容未改；`run-1/`、`run-2/` 留给 r2 新 revision 的重跑，入库前本目录无当前 revision 的 Run。
>
> 2026-11-16（#676）：`run-1/`、`run-2/` 又换成了 `prompt-replay-candidate-14eb2a0a6afd` 下的两次重跑（Run `a10cf725`、`f3b73c46`），#646 那两份移到了 [prompt-a6b4dfd02030/](prompt-a6b4dfd02030/)。
>
> 2026-11-16（#646）：`run-1/`、`run-2/` 当时换成了新提示词 `prompt-replay-candidate-a6b4dfd02030` 下的两次重跑（Run `e6fa260e`、`33922153`，见文末一节）。下面「审查者结论」表判的是 10-24 的两次 Run（`dbf02dca`、`1a9b37f1`），它们的 summary 移到了 [prompt-bef70649a3a8/](prompt-bef70649a3a8/)，内容未改。

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
- **审查者判定（2026-11-16）**：两名全新上下文审查者按冻结 rubric（`b58974b2…1d956`）独立判定，审查者 B 使用另一模型。

  | Run | 审查者 | rubric 1–8 | 第 2 步 summary 一致性 | 记录的弱点（不作门槛） |
  |---|---|---|---|---|
  | Run 1 `e6fa260e` | A | PASS（8/8） | 一致 | — |
  | Run 1 `e6fa260e` | B | PASS（8/8） | 一致 | summary 用 “Best-supported cause”、“is not what broke this path”，与分组一致；flag 变更对象、操作者和精确时刻未知；trace 为偏样本（gaps 记 12/120） |
  | Run 2 `33922153` | A | PASS（8/8） | 一致 | — |
  | Run 2 `33922153` | B | PASS（8/8） | **不一致** | summary 写 “The trigger is visible” 与 “this is a flag configuration value being served at 100% fault rate”，把 hypothesis claim 13/14 的解释写成确定事实；`root_cause` 写 `Deployment/flagd`，而 claim 14 与 gaps 承认实际 flag 配置对象和变更者未知 |

  审查者 A 只回报了结论，全文未留存。合计：第 5 步 2/2；第 2 步 Run 2 审查者分歧。用户 2026-11-16 裁决按不一致处理：F3 第 2 步在当前提示词下未达成，`passes` 改为 false，不挑样重跑（U-A），跟踪 #664。
- 当时供审查者注意的事实：两次 `root_cause` 分别写 FeatureFlag `paymentFailure` 和 Deployment `flagd`；两次的 24 小时授权窗里都有更早的 checkout 发布（lab 安装、失败尝试；Run 2 还包含 Run 1 那次发布和故障）；Run 1 晚领取约 19 分钟（授权窗终点早于晚领取）。

## #676 重跑（2026-11-16）

- `prompt-14eb2a0a6afd/run-1/summary.json` 对应 Run `a10cf725`（事故 `53f5ccd3`），`prompt-14eb2a0a6afd/run-2/summary.json` 对应 Run `f3b73c46`（事故 `6ece32c3`）。收集方式同上一节（UI `close` 之后、只读角色、`lab_readings` 是连续读数）。`run_versions` 等于当前代码的 `otel_demo_versions()`。
- 确定性判定：`tests/acceptance/test_f3_step5_real_runs.py` 15 条全过。
- **审查者判定（rubric 5–8、第 2 步一致性）：待 lead 派审查者。** 运行事实见 [f3-acceptance-live/run.md](../f3-acceptance-live/run.md)「#676 重跑」。

## #676 合同 r2 重跑（2026-11-17）

- `run-1/` 对应 Run `8b06fee1`（事故 `f846b4e8`），`run-2/` 对应 `41b97848`（`7b68624a`），`run-3/` 对应 `9d3c1236`（`3b7647b4`）。r1 那两份已移到 [prompt-14eb2a0a6afd/](prompt-14eb2a0a6afd/)。收集方式同上（UI `close` 之后、只读角色、`lab_readings` 连续读数）。三份 `run_versions` 都等于当前代码的 `otel_demo_versions()`。
- 确定性判定：三份都为空；验收测试读 `run-1`、`run-2`，15 条全过。
- **审查者判定（2026-11-16）**：两名全新上下文审查者按冻结 rubric（`b58974b2…1d956`）、answer（`a35361da…37a`）独立判定三次 Run，审查者 A 为 Claude 子 Agent，审查者 B 为 Codex（另一模型）。

  | Run | 审查者 | rubric 1–8 | 第 2 步 summary 一致性 | 记录的弱点（不作门槛） |
  |---|---|---|---|---|
  | Run 1 `8b06fee1` | A | PASS（8/8） | 一致 | claim 0、16 称 PlaceOrder 错误率在 01:05:54–01:10:54 为 0，但证据 `921ba20e-t0` 该段无样本（claim 0 与 gap 3 自己写了无样本），D5-a(i) 不成立，A 认为 (ii)(iii) 单独够；对部署是软否定；「earlier」引用的 00:20 是 r1 第 2 次 Run 的故障尾部 |
  | Run 1 `8b06fee1` | B | **FAIL（第 8 项）** | 一致 | 同一处：claim 17 称部署后「error ratio stayed 0…01:05:54–01:10:54」，引用序列从 00:29:54 直接跳到 01:11:54，把未知时段写成已证实的部署后正常期，改变了否定部署的时间论证，D5-a(ii) 成立不能豁免第 8 项；C8 把 00:30:54 写成 00:35:54 |
  | Run 2 `41b97848` | A | PASS（8/8） | 一致 | summary「(0 earlier in the hour)」与 claim 5 不符但后文已写 01:37；next_steps 把前一次注入当成自愈；claim 5 区间下限应为 0.0208 |
  | Run 2 `41b97848` | B | PASS（8/8） | 一致 | C2 与 summary 称唯一错误操作是 Charge，同一视图也有 PlaceOrder ERROR |
  | Run 3 `9d3c1236` | A | PASS（8/8） | 一致 | summary 数值笔误「404.0167/s」（应为 0.0167/s）；claim 4 时刻与「through 03:36:04」两处数值误差；03:26:31 span 归给新 Pod 无 Pod 字段依据 |
  | Run 3 `9d3c1236` | B | PASS（8/8） | 一致 | 同上 404.0167/s 与几处时刻误差 |

  两名审查者都判 01:37–01:39Z 那段错误（Run 1 故障窗尾部）不影响 Run 2：它确实早于 Run 2 的部署，报告没有给它编造原因，否定部署另有部署后成功 span 与 payment Charge 错误两条依据。
- 合计：第 2 步 summary 一致性 3 次 Run 两名审查者均判一致（#664 的问题在 r2 下未再出现）；第 5 步 Run 1 审查者分歧，Run 2、Run 3 均为两名审查者 PASS。
- **裁决**：Run 1 按 FAIL 处理（用户 2026-11-17 同意 r3 计划时随计划一并认可，未单独确认；依据与 2026-11-16 U-A 一致：分歧按不通过、不挑样）。验收测试读 `run-1`、`run-2`，所以 F3 第 5 步在 `prompt-replay-candidate-8688f6e52275` 下记为未达成，`passes` 仍为 false；下一版（r3，#695/#690，PR #697）revision 变化后重跑三次。B 指出的问题（把无样本时段写成 0）正是 #690 第 2 句「无样本时段是 unknown 不是 zero」要约束的行为。
- 审查原文：审查者 B 的输出与审查者 A 的复述留在 lead 会话目录，未入库（只入结论）。运行事实见 [f3-acceptance-live/run.md](../f3-acceptance-live/run.md)「#676 合同 r2」。
