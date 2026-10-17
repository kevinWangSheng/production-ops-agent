# F11 U2 发布观察校准：真实实验记录（批次 `u2-20261017`）

- 日期：2026-10-17（UTC），kind-opspilot-m1 / otel-demo，lead：lane-f11-u2
- 合同：[任务记录](../../tasks/2026-10-14-f11-u2-calibration.md) r1/r2（U2-01–U2-04、L1–L4）；[PR-2 合同](../../tasks/2026-10-13-f11-pr2-contract.md) r8
- 冻结汇总：[`summary.json`](summary.json)（`aggregate --batch u2-20261017`），逐轮哈希与原始响应哈希见下文「文件」
- 性质：工程旁路采集＋离线折叠的**未批准**校准证据（`U2_CALIBRATION_UNAPPROVED`）；不是 policy，不开放授权

## 目标与固定参数

- 目标五项身份：`integration_id=m0-otel-20260909`、`cluster_uid=c8001471-8b7b-4449-83c0-924e6f46b857`、`namespace=otel-demo`、`resource_uid=m0-otel-20260909`（注册表键）、`workload=checkout`
- HealthProfile：`otel-demo-checkout@b72bbe2e30be`，规范内容 sha256 `b72bbe2e30bea4775790519787da4ce107dbc1500ce87b87a2ee2af3f57cbfa4`；窗口 300s、freshness 90s、单查询超时 20s；cadence 60s、健康窗 600s、deadline 3600s、上限 40 次。本批未改 profile，未产生新 revision。
- 候选 M（离线折叠）：600／900／1200／1500／1800／2100 s；采集上下文 `--minimum 1200`（只决定 UNAPPROVED 实验壳，不影响候选折叠）。
- 发布方式（L1）：`kubectl patch` 设置无业务作用的 `OPSPILOT_U2_RELEASE_ID` 环境变量，R=patch 成功回执时间；每轮保存前后模板与 digest。
- 回归刺激（U2-02）：`paymentFailure` off→100%，flagd 生效后维持 900s；无关故障：`adFailure` off→on（ad 服务），维持 900s。
- 凭据：旁路只用 lab 账号、Observer 只用 observer 账号，均 `source` 进子进程环境，未打印、未写入证据。

## 基线（04:06–04:36Z，30 分钟）

scrape 周期 59.3–60s（span metrics 与 KSM 相同）；最新样本年龄 span ≈3.4s、KSM ≈8.8s；单查询耗时中位 1.5s、最大 2.1s；checkout PlaceOrder 速率中位 0.0208/s（0.0083–0.0417）；错误率 0；p95 中位 97ms（93–350）。完整计划 49 个串行查询单样本 61–91s，映射前 33 个查询 44–57s；租约 120s 减提交余量 5s，未出现 `LEASE_BUDGET`。配置发布滚动秒级完成；flagd 配置写入到 variant 生效 23–86s。

## 产品缺陷与修复（实验中发现，均经用户裁决）

1. **查询时间舍入**（`u2-probe-1`）：Prometheus 把求值时间舍入到毫秒，窗尾微秒 ≥500 时返回时间戳晚于 `evaluated_at`，整样本 `QUERY_FAILED`→unknown（预热后 9/27）。#261：`time` 参数向下截断到毫秒（r8 修订 B01）。修复后短 probe `u2-probe-2` 11 个连续 healthy、0 个 QUERY_FAILED，按设计提前停止并 `abort` 记入。
2. **after 切片覆盖口径**（`u2-f-immediate-1`／`u2-f-delayed5-1`／`u2-f-delayed15-1`）：起病时同实例新出现的 `STATUS_CODE_ERROR` 序列窗内仅 1–2 点，`min(count_over_time(...))` 被拉低判 `INSUFFICIENT_SAMPLES`→unknown，推迟检出一个周期（Prometheus 原始点核实：UNSET 序列每分钟连续，ERROR 序列起病后才出现）。#262：按 PR-2:556–559 意图改为实例内 max、跨实例 min（r8 第二条）。三轮保留为**产品缺陷轮、不作校准证据**，修复后重跑。

## 逐轮结果

时间均为 UTC；「首个错误」为 Jaeger checkout PlaceOrder server span（独立真值）；剩余为该 M 终态时的剩余次数／秒；预热后 unknown 按 harness 口径（窗口起点不早于授权）统计到 M=1500 终态为止。

| 轮次 | 场景 | R | 故障生效／恢复 | 首个错误 | M=1200 | M=1500 | M=1500 剩余 | 预热后 unknown | 轮前 swap／p95 | 分类 |
|---|---|---|---|---|---|---|---|---|---|---|
| u2-probe-1 | probe（adFailure） | 04:41:48 | 04:48:15／04:58 | — | unknown（deadline） | unknown | — | 9/27 | 11.0G／185ms | 产品缺陷 1（不作证据） |
| u2-probe-2 | probe | 07:41:13 | — | — | aborted | aborted | — | 0 | 10.4G／330ms | 修复确认，提前停止 |
| u2-f-healthy-1 | healthy | 08:07:23 | — | — | healthy 08:31:08 | healthy 08:36:06 | 28／1878s | 0 | 10.2G／377ms | **有效** |
| u2-f-immediate-1 | immediate | 09:08:21 | 09:09:40／09:25:20 | 09:11:05 | anomalous 09:17:55 | 同左 | 36／3026s | 1/2 | 9.9G／390ms | 产品缺陷 2（不作证据） |
| u2-f-delayed5-1 | delayed-5m | 10:09:56 | 10:15:20／10:30:52 | 10:16:23 | anomalous 10:21:02 | 同左 | 35／2934s | 1/3 | 10.9G／78ms | 产品缺陷 2（不作证据） |
| u2-f-delayed15-1 | delayed-15m | 11:11:12 | 11:27:25／11:43:17 | 11:28:46 | anomalous 11:33:58 | 同左 | 30／2234s | 1/8 | 10.8G／97ms | 产品缺陷 2（不作证据） |
| u2-f-unrelated-1 | unrelated（adFailure） | 12:13:09 | 12:18:37／12:33:44 | — | healthy 12:36:39 | healthy 12:41:22 | 28／1907s | 0 | 10.9G／98ms | **有效** |
| u2-f-immediate-2 | immediate | 18:09:23 | 18:10:44／18:26:40 | 18:11:37 | anomalous 18:17:51 | 同左 | 36／3092s | 0 | 11.8G／97ms | **有效** |
| u2-f-delayed5-2 | delayed-5m | 19:10:55 | 19:16:46／19:32:46 | 19:18:36 | anomalous 19:23:15 | 同左 | 34／2860s | 0 | 12.0G／170ms | **有效** |
| u2-f-delayed15-2 | delayed-15m | 20:12:28 | 20:27:39／20:43:01 | 20:28:23 | anomalous 20:33:09 | 同左 | 30／2359s | 0 | 11.9G／98ms | **有效** |
| u2-f-boundary-1 | boundary（R+1200） | 21:13:02 | 21:33:26／21:49:30 | 21:33:36 | anomalous 21:36:44 | 同左 | 29／2178s | 0 | 11.6G／95ms | **有效**（U2-01 边界） |
| u2-observer-wiring-1 | Observer 接线 | 22:15:03 | — | — | aborted（接线） | — | — | — | — | U2-04 B，不作校准 |

其他候选：M=600／900 在 delayed-15m（两次）与 boundary 中都在起病可见前健康终结（漏检）；M=1800 在 unrelated-1 因 12:43:43 一次真实低流量 `INSUFFICIENT_TRAFFIC` 重置跟踪，13:11:35 才终结、剩余 94s（<300s），M=2100 到 deadline 仍 unknown。

**`INSUFFICIENT_TRAFFIC` unknown 的合同口径（lead 未归类，交批准者判断）**：有效轮共 2 个预热后、与起病无关的此类 unknown——unrelated-1 12:43:43（after 切片流量低于 `effective_traffic.minimum` 0.008/s）与 delayed5-2 19:57:08。二者均在 M≤1500 终态之后，不计入下表 M=1200／1500 的「预热后 unknown」，只影响 M≥1800。合同原文：U2-03「预热后非预期 unknown ≤5%（全程比例另报）」（[任务记录:17](../../tasks/2026-10-14-f11-u2-calibration.md)）没有定义何为「非预期」；PR-2 合同 :563「有效流量取切片后的 `profile.effective_traffic.signal`，要求值≥`effective_traffic.minimum`」、:583「充分证据要求：……新版本有效流量合格」、:595「纯版本／流量不足、context-only degradation 使用 `no_data`。这些仍可合法 adopted、计次」。即流量不足是合同规定的合法 unknown 来源，但合同没有把它明确列为 U2-03 的「预期 unknown」。

## 判据逐项（U2-03 r2，有效轮：healthy-1、unrelated-1、immediate-2、delayed5-2、delayed15-2；boundary-1 另列）

| 判据 | M=1200 | M=1500 |
|---|---|---|
| 健康零异常误报且 healthy 终结 | 通过（healthy-1、unrelated-1 终态前 0 个 degraded） | 通过 |
| 回归全部在终态前且 deadline 内检出 | 通过（三类均 anomalous，最小剩余 2359s） | 通过 |
| 预热后非预期 unknown ≤5% | 0/35 | 0/39 |
| 健康终结剩余 ≥5 次、≥300s | 30／2176s、30／2190s | 28／1878s、28／1907s |
| 全程 unknown 比例（另报） | 16/51（均为预热窗） | 16/55（均为预热窗） |

- 余量：delayed-15m 重跑中，M=1200 的最早健康终结约为 T₀+M−W≈20:34:20，实际检出 20:33:09，余量约 70s；M=1500 余量约 6 分钟。boundary（起病 R+1234s）在 R+1422s 检出，两个 M 都在终结前。
- 全捕获（含终态后）unknown 18/165，均为预热或真实低流量。
- 小样本（每类 1 次，r2）不构成统计可靠性保证。

## healthy-1 与 unrelated-1 为何不受缺陷 2 影响（保留依据）

两轮采集与折叠代码为 `a6adfb3`（#262 之前）。缺陷 2 只在 after 实例出现新的拆分序列（起病后的 ERROR 序列）时触发。逐轮核实（Prometheus 范围查询，`tmp/u2/<轮>/errseries.json` 哈希见文件清单）：两轮 after 实例在 [R, 采集结束] 内**无任何** `STATUS_CODE_ERROR` 序列（仅发布前旧 Pod 有历史 ERROR 序列，不在切片内），因而每实例只有单一 UNSET 序列，`min(count_over_time)` 与 `min(max by (实例)(count_over_time))` 数值相同；两轮预热后 unknown 均为 0。注意：按 #262 后代码重算这两轮计划会出现查询文本不一致，因此保留的是其冻结的采集与折叠结果，不重新折叠。

## 环境事件（如实记录，未删轮）

- healthy-1 终态之后 08:57–09:05Z 4 个样本 p95 580–740ms（无故障、错误率 0），归为宿主内存压力导致的环境干扰；不影响任一候选 M 的终态，但意味着 M≥约 2900s 时健康轮会误判。
- email Pod 09:41Z OOMKilled（immediate-1 恢复期，p95 升至 9.5s）。
- 宿主 swap 全程 9.9–12.0G 已用；kind VM 为 colima 4C/8G。

## ad 故障对 checkout 的影响（L1）

probe-1（adFailure 生效 04:48:15–04:58:15）期间 5 个样本全 healthy；Jaeger checkout PlaceOrder 10 分钟桶：故障窗 13 条、之后 15／16／21／12，错误 0；unrelated-1 checkout 错误 0、after 实例无 ERROR 序列。结论：adFailure 不影响 checkout 必要信号与流量。**限制**：adFailure 是弱故障（probe-1 故障窗 ad 1/25 出错，unrelated-1 全程 3/126）。

## Observer 接线验证（U2-04 B，不作校准）

`opspilot_web_u2`（∈opspilot_web）登记并授权，临时测试清单仅在 worktree `tmp/`，`approval_ref` 标 test-only，从未进入产品清单；授权回执 `applied`。产品 Observer 以 `opspilot_observer_u2`（∈opspilot_observer）运行，采纳 14 个样本（no_data×2、failed×1、healthy×11），22:43:33Z（R+1710s）判 `healthy／healthy_confirmed`，SIGTERM 正常停止。

- 唯一的 failed 样本（sequence 3）：新 Pod 在窗口内尚无 PlaceOrder span，`after:latency_p95_milliseconds:query` 返回 `NaN` → `QUERY_FAILED`；同一样本带 `WINDOW_BEFORE_AUTHORIZATION`，属预热窗（与 harness 各轮预热窗同类 QUERY_FAILED 一致）。sequence 1–3 另有 `DEGRADED_SIGNAL:context:latency_p95_milliseconds`（整体服务 p95，context 只作上下文）。
- 前置迁移：第一次启动 Observer 报 `SCHEMA_NOT_MIGRATED`（55561 在 0016，代码 head 0017）；lead 以 owner 执行 `make migrate` 升到 0017。0017 只给 `opspilot_web` 增加列级 GRANT（`opspilot_scope_controls`／`opspilot_target_suspensions`／`opspilot_suspension_audit`），可 downgrade，数据未动。
- 启动清扫：Observer 把 harness 各轮经 `register` 登记、从未授权且已过 deadline 的 pending 发布置为 `unknown／deadline_expired`（产品清扫行为；只改实验库登记行，不影响 ledger 与旁路结论）。

## 覆盖与限制（给批准者）

- 覆盖承诺：发布后 0／5／15 分钟起病的 `paymentFailure` 类错误率回归可在终态前检出；不宣称监测满一小时，不证明版本内生回归（U2-02），只覆盖本 profile 的错误率／延迟／部署信号。
- 证据为每类 1 次（r2），无统计保证；流量约 0.02 单/秒，低流量窗会产生合法 `INSUFFICIENT_TRAFFIC` unknown。
- 无关故障只测了弱的 `adFailure`。
- 宿主内存压力下出现过无故障的延迟 degraded（终态后）。
- M 余量：delayed-15m 重跑中 M=1200 只有约 70s 余量（最早健康终结 ≈20:34:20，检出 20:33:09），样本时刻的正常抖动即可能使其漏检；M=1500 余量约 6 分钟。

## 文件与复现

- `summary.json`：`aggregate --batch u2-20261017` 冻结的紧凑批次汇总（`run_format=compact-v1`；12 轮全部列入，含缺陷轮与 aborted 轮；每轮以完整 summary 文件 sha256 绑定，并绑定 `run-starts.jsonl` 前缀哈希）。policy 的 `evidence_sha256` 指向本文件确切字节。
- `rounds.json`：逐轮摘要（场景、真值、各候选 M 终态、逐样本 verdict／原因、分类、错误序列核查）。
- `raw-response-hashes.json`：全部原始 Prometheus 响应的 sha256 清单（按轮、样本、查询键）。
- 逐轮完整 summary 与原始 ledger 不入库，在主仓库 `tmp/lab-ledgers/<轮>/`（`summary.json`、`samples/`、`raw/`、`fault.jsonl`、`timeline.jsonl`、`release.json`、`before/after.json`、`context.json`、`code-snapshots/`）。逐轮 summary 由离线折叠生成，命令（在含 `capture-start.json` 冻结代码的 checkout 上，`OPSPILOT_LAB_LEDGERS` 指向 ledger 根）：`python -m scripts.f11_u2_harness --holder lane-f11-u2 --batch u2-20261017 fold --run <轮> --minimum 600 900 1200 1500 1800 2100 --scenario <场景> --truth <真值 JSON>`；aborted 轮由 `abort --run <轮> --scenario <场景> --reason <原因>` 生成。真值 JSON 原样嵌在每轮 summary 的 `truth` 字段。fold 拒绝覆盖已有 summary；复核时在 ledger 副本（连同 `run-starts.jsonl`）上删去该轮 summary 后重跑，比对 `candidates`／`capture`／`raw_response_hashes` 等判定字段（`fold_code` 快照与时间戳每次不同，整文件哈希不要求一致）；入库 summary.json 绑定的是原始那份文件的 sha256。
