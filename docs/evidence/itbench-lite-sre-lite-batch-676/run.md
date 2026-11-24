# ITBench-Lite 集成批：#676 调查指导，8 题 × 2 次

类别：集成运行（真实 DeepSeek `deepseek-flash`、回放组件、真实 judge Z.ai `glm-5.3-flashx`）。用的是 [#646 集成批](../itbench-lite-sre-lite-batch-646/run.md)的同一 8 题、同一四道门，另加一门：supported 占比下降、且根因非 null 跌破 80% 时停。依据 [任务记录](../../tasks/2026-11-16-676-investigation-guidance.md)。时间 UTC，2026-11-16（括号内为太平洋时间）。

- 代码：主机主 checkout 在 `feature/676-investigation-guidance` `e8b98507`（PR #677）。16 个 summary 的版本都是 `prompt-replay-candidate-k8s-f09a033198ca` / `otel-demo-ext-c63f058afb18` / `ctx-ctx-policy-v1-a30e65ea52f4`。
- 主机、数据集、词表与 kinds 旁路、分档：同 #646 集成批与全量（复用同一台主机，没有重新生成）。
- 命令：同 #646 集成批（`driver round --round lite-batch-676 … --trials 2`），分三段：A `S12,S5`（22:14:03–22:44:23）；B1 `S14,S34,S2`（默认端口，22:50:32–23:40:21）与 B2 `S8,S105,S22`（端口 +100，22:50:32–23:28:51）并行。三段退出码都是 0。之后 `driver scores`。
- 同机条件：kind lab 开着（同机跑 F3 第 5 步）。各段 trial 都避开了 F3 Run 1 的调查窗口（Run 1 的 worker 22:45:05 起、约 22:49 完成，A 段 22:44:23 结束，B 段 22:50:32 才开始）。B 段在 Run 2 发布（23:42:48）之前就结束了。lab 每分钟读数：checkout p95 最高约 36 ms，swap 全程空闲。

## 每个 trial

| trial | 结论 | 根因（kind / namespace / name） | 换算 | load_seconds | gap_seconds | 模型轮次 | root_cause_entity_pass |
|---|---|---|---|---|---|---|---|
| S2/trial-1 | partial | Deployment / otel-demo / frontend | structured | 44.4 | 5 | 18 | 0.0 |
| S2/trial-2 | 产品拒收（REPORT_INVALID） | — | heuristic | 44.2 | 5 | 33 | 0.0 |
| S5/trial-1 | partial | Deployment / otel-demo / ad | structured | 65.7 | 5 | 27 | 0.0 |
| S5/trial-2 | supported | Deployment / otel-demo / ad | structured | 65.5 | 5 | 21 | 0.0 |
| S8/trial-1 | partial | Deployment / otel-demo / payment | structured | 59.6 | 5 | 19 | 0.0 |
| S8/trial-2 | partial | Deployment / otel-demo / payment | structured | 60.0 | 5 | 26 | 0.0 |
| S12/trial-1 | partial | null | structured | 59.8 | 5 | 18 | 0.0 |
| S12/trial-2 | partial | null | structured | 59.4 | 5 | 15 | 0.0 |
| S14/trial-1 | partial | Deployment / otel-demo / ad | structured | 49.3 | 5 | 25 | 0.0 |
| S14/trial-2 | partial | Deployment / otel-demo / ad | structured | 49.7 | 5 | 22 | 0.0 |
| S22/trial-1 | supported | Pod / otel-demo / ad-554b849958-l9ft5 | structured | 57.5 | 5 | 33 | 0.0 |
| S22/trial-2 | partial | Schedule / chaos-mesh / otel-demo-ad-memory-stress | structured | 57.4 | 5 | 18 | 1.0 |
| S34/trial-1 | partial | Pod / otel-demo / valkey-cart-58df56c79c-bf4lr | structured | 57.8 | 5 | 25 | 1.0 |
| S34/trial-2 | partial | Pod / otel-demo / valkey-cart-58df56c79c-bf4lr | structured | 58.0 | 5 | 32 | 1.0 |
| S105/trial-1 | supported | Deployment / otel-demo / product-catalog | structured | 36.7 | 5 | 16 | 1.0 |
| S105/trial-2 | 产品拒收（REPORT_INVALID） | — | heuristic | 36.8 | 5 | 17 | 0.0 |

驱动层 16/16 completed，`history` 全空，judge 全部 `ok`。

## 门

| 门 | 口径 | 读数 | 结论 |
|---|---|---|---|
| ① | `REPORT_INVALID`（含产品拒收）≤ 1/16 | 2/16（S2 t2、S105 t2） | **不过** |
| ② | 产品接受且非 `inconclusive` 的报告中 ≥ 80% 根因非 null | 12/14 = 85.7%（两份 null 都是 S12） | 过 |
| ③ | 根因不是命名空间名，换算 `reason` 全空 | name 都不是命名空间名；kind 为 Deployment 9、Pod 3、Schedule 1；`reason` 只有两个拒收是非空 | 过 |
| ④ | 结论分布明显移向 supported 而分数不升 → 不过 | supported 3、partial 11、inconclusive 0（21%；#646 批 12/15 = 80%）；root_cause_entity_pass 4/16 = 0.25（#646 批 3/16 = 0.19） | 不触发（结论移向 partial） |
| 附加 | supported 占比下降，且根因非 null < 80% → 停 | supported 下降；根因非 null 85.7% | 不触发 |

## 拒收原因（只读重放）

做法：在主机上临时起该 trial 自己的 PG（只读，用完停掉，`pg_hba.conf` 恢复原样），用产品的 `DurableStore.rebuild` → `context.rebuild_transcript` 还原当轮之前已交付的视图，再对该轮原始回复调用 `reports.validated_report`。没有改代码。

- S105 t2：第 16 轮报 `JSON parse error: Expecting value`，同时 `next_steps: schema_error (Field required)`。第 17 轮（强制终答重试）的 20 条 claim 与 `root_cause` 全部 `unknown_evidence_id`：引用的 id 被截成 `<uuid>-t<n>`，丢掉了冒号后面那一段。
- S2 t2：第 32 轮 `next_steps: schema_error (Field required)`。第 33 轮重试时，claim 15、28 报 `cites_non_ok_view`。
- 同一做法下，[冒烟](../itbench-lite-sre-lite-smoke-676/run.md) S19 t2：第 26 轮 fact 与 counter_evidence 引用了 no_data 的 `fetch_pod_logs`，报 `cites_non_ok_view`；第 27 轮有一个 id 抄错，报 `unknown_evidence_id`。
- PR #677 没有改报告示例，也没有改 `next_steps` 的说明；lite-full-1116 的 3 次拒收没有做过同样的重放，所以缺 `next_steps` 是不是新出现的，未核实。

## 费用

DeepSeek：16 个 summary 的 `run.tokens` 合计缓存命中 22,774,144、未命中 2,263,747、输出 814,945。北京时间 11-17 06:14–07:40，全部闲时。单价同 [soak 费用主数](../m3-soak-2026-11-07/cost-primary.json)，约 **CNY 5.98**。judge 费用未统计。

## 证据

16 份 summary 与 `scores.json`。入库前在主机上对 13 个已知秘密值做原文搜索：0 命中；正对照（植入一个值）命中 1。sha256 本机与主机一致。原始 ledger 不入库。
