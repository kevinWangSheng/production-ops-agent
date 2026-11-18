# ITBench-Lite 集成批：r3 发布，8 题 × 2 次

类别：集成运行（真实 DeepSeek `deepseek-flash`、回放组件、真实 judge Z.ai `glm-5.3-flashx`）。题目与 [#676 r2 集成批](../itbench-lite-sre-lite-batch-676r2/run.md) 相同。门按 r3 验收计划阶段 2。时间 UTC，2026-11-17。

- 代码：`b0652dbd`（同[冒烟](../itbench-lite-sre-lite-smoke-r3/run.md)）。16 个 summary 的版本都是 `prompt-replay-candidate-k8s-1137251eea4d` / `otel-demo-ext-c63f058afb18` / `ctx-ctx-policy-v1-a30e65ea52f4`。
- 命令：`driver round --round lite-batch-r3 --scenarios <题> --trials 2`，4 个进程并行：`S12,S34`（端口偏移 0）、`S5,S2`（+100）、`S14,S8`（+200）、`S105,S22`（+400）。22:13:40–22:35:30，退出码都是 0，没有作废。之后 `driver scores`（本机从原始包里的逐 trial 分数重算，`official_missing` 为空）。
- 同机条件：kind lab 开着。批在 F3 Run 2 收尾（22:12Z）之后开始，在 F3 Run 3 发布（22:41:43Z）之前结束，没有和 F3 的调查窗口重叠。lab 每分钟读数：checkout p95 最高 48 ms，swap 全程 0。

## 每个 trial

| trial | 结论 | 根因（kind / namespace / name） | load_seconds | 模型轮次 | 终答轮 | root_cause_entity_pass |
|---|---|---|---|---|---|---|
| S2/trial-1 | partial | ConfigMap / otel-demo / flagd-config | 45.3 | 21 | 1 | 1.0 |
| S2/trial-2 | partial | ConfigMap / otel-demo / flagd-config | 44.8 | 27 | 1 | 1.0 |
| S5/trial-1 | partial | ConfigMap / otel-demo / flagd-config | 65.9 | 24 | 1 | 1.0 |
| S5/trial-2 | partial | ConfigMap / otel-demo / flagd-config | 65.6 | 25 | 2 | 1.0 |
| S8/trial-1 | partial | ConfigMap / otel-demo / flagd-config | 61.6 | 22 | 1 | 1.0 |
| S8/trial-2 | partial | ConfigMap / otel-demo / flagd-config | 60.2 | 20 | 1 | 1.0 |
| S12/trial-1 | partial | null | 60.1 | 16 | 1 | 0.0 |
| S12/trial-2 | partial | null | 60.7 | 13 | 1 | 0.0 |
| S14/trial-1 | partial | ConfigMap / otel-demo / flagd-config | 49.6 | 10 | 1 | 1.0 |
| S14/trial-2 | partial | Deployment / otel-demo / ad | 48.9 | 18 | 1 | 0.0 |
| S22/trial-1 | partial | Schedule / chaos-mesh / otel-demo-ad-memory-stress | 60.7 | 17 | 1 | 1.0 |
| S22/trial-2 | partial | Schedule / chaos-mesh / otel-demo-ad-memory-stress | 59.8 | 21 | 1 | 1.0 |
| S34/trial-1 | partial | Pod / otel-demo / valkey-cart-58df56c79c-bf4lr | 58.3 | 20 | 1 | 1.0 |
| S34/trial-2 | partial | null | 58.5 | 21 | 2 | 0.0 |
| S105/trial-1 | supported | Deployment / otel-demo / product-catalog | 37.5 | 16 | 1 | 1.0 |
| S105/trial-2 | （被拒；最后一次回复写 supported） | Deployment / otel-demo / product-catalog | 37.4 | 16 | 2 | 0.0 |

驱动层 16/16 completed，`history` 全空，`gap_seconds` 都约 5.0，judge 全部 `ok`。S105 t2 的产品 Run 交接（`waiting_human`，换算 `REPORT_REJECTED_BY_PRODUCT`）。

## 门（r3 验收计划阶段 2）

| 门 | 读数 | 结论 |
|---|---|---|
| REPORT_INVALID 0 | 1/16（S105 t2） | 不过 |
| 首答被拒明显低于 1117 同 8 题 | 3/16 = 18.8%（S5 t2、S34 t2、S105 t2）；1117 基线 8/24 = 33.3% | 过 |
| S105 2/2 | 1/2 | 不过 |
| S22 2/2 | 2/2 | 过 |
| S34 ≥ 1/2 | 1/2 | 过 |

结论分布：supported 2、partial 14、inconclusive 0。0 次上下文压缩。

**1117 基线的算法**：来源是 [lite-full-1117](../itbench-lite-sre-lite-full-1117/run.md) 同 8 题的 24 份 summary：`docs/evidence/itbench-lite-sre-lite-full-1117/S{12,5,14,34,2,8,105,22}/trial-{1,2,3}/summary.json`。1117 的 ledger 不在仓库里，所以用 summary 的 `run.steps`：`:round-` 步中 status 不是 `tool_result_committed` 的记为终答轮，≥ 2 即首答被拒。这个口径在 r3 冒烟 9 个 trial 上与 ledger 精确口径（`finish_reason=stop` 且没有工具调用）逐个相同，套到 1117 全部 105 份上复现了 1117 记录的 39/105。1117 逐题终答轮：S12 1,1,1；S5 1,1,1；S14 1,2,2；S34 1,2,1；S2 1,1,2；S8 1,1,2；S105 2,1,1；S22 2,2,1。r3 本批用 ledger 精确口径。

## S105 t2 拒收（只读重放）

trial 自己的 PG 副本（只在副本上改成 trust 认证）→ `rebuild_transcript`（绑定目标 `m0-otel-20260909`，27 个已交付视图，与在线 Run 相同）→ 对两次终答各调一次 `validated_report`，与 loop 的调用相同。两次都是合法 JSON（`m0-report-v3`，supported），JSON 模式本身没有问题；拒收都在引用检查：

- 第 15 轮（首答）：每条 claim 都是 `unknown_evidence_id`，引用写成了截断的 `<uuid>-t0`，缺 `:<uuid>` 部分；D6 重试提示给出了最近的完整 id。
- 第 16 轮（强制重试）：id 已改对，但 claim 报 `missing_target_refs`：`target_refs` 写了 `otel-demo/frontend`、`otel-demo/product-catalog`、`otel-demo/product-catalog-fcf7564cf-vjj7l` 等，允许值只有 `m0-otel-20260909`（例如 claim 0 是 `["m0-otel-20260909", "otel-demo/frontend"]`）。S105 t1 的 16 条 claim 都只写了 `m0-otel-20260909`。

引用检查对每条 claim 按顺序检查（unknown_evidence_id → missing_target_refs → missing_time_scope_ref → cites_non_ok_view → …），遇到第一个失败就停（`opspilot/investigation/reports.py` 约 370–425 行，r3 未改），所以第 15 轮只能报出截断 id，多余的 target_refs 直到最后一次重试才暴露。

**lead 裁决（2026-11-17）**：按偶发失败接受，机制已识别且早于 r3（不是 r3 引入的退步），另开 #701 跟踪；首答被拒 18.8% 对 33.3% 是本次发布要改善的指标。全量里 S12 t2 的拒收是同一机制（见[全量记录](../itbench-lite-sre-lite-full-r3/run.md)「运行情况」）。

## 费用

DeepSeek 合计缓存命中 15,704,832、未命中 1,337,008、输出 534,303。北京时间 11-18 06:13–06:35，属于闲时，约 **CNY 3.79**。judge 费用未统计。

## 证据

16 份 summary 与 `scores.json`。秘密值搜索与 sha256 核对同[全量记录](../itbench-lite-sre-lite-full-r3/run.md)「证据与秘密值」。重放输出在仓库外原始包里。
