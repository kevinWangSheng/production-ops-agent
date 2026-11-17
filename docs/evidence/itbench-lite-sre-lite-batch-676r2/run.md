# ITBench-Lite 集成批：#676 合同 r2，8 题 × 2 次

类别：集成运行（真实 DeepSeek `deepseek-flash`、回放组件、真实 judge Z.ai `glm-5.3-flashx`）。题目与 [r1 集成批](../itbench-lite-sre-lite-batch-676/run.md)、[#646 集成批](../itbench-lite-sre-lite-batch-646/run.md) 相同。门按 [合同](../../tasks/2026-11-16-676-investigation-guidance-contract.md) r2 末尾的验证口径。时间 UTC，2026-11-17。

- 代码：`feature/676-investigation-guidance` `d96823fc`。16 个 summary 的版本都是 `prompt-replay-candidate-k8s-0b066c7cd6d1` / `otel-demo-ext-c63f058afb18` / `ctx-ctx-policy-v1-a30e65ea52f4`。
- 命令同 r1，分两个进程并行：B1 `S12,S5,S14,S34`（默认端口，01:21:56–02:16:48），B2 `S2,S8,S105,S22`（端口 +100，01:21:56–02:14:59）。同一时段还有[冒烟](../itbench-lite-sre-lite-smoke-676r2/run.md)在跑（端口 +200），共 3 个进程。退出码都是 0。之后 `driver scores`（`official_missing` 为空，16 个都是 structured）。
- 同机条件：kind lab 开着。三个进程都在 F3 Run 1 报告发布后（01:21:56）才开始；F3 Run 2 推迟到批跑完之后（02:15:21）才发布。所以 trial 没有和 F3 的调查窗口重叠。lab 每分钟读数：checkout p95 最高约 35 ms，swap 全程空闲。

## 每个 trial

| trial | 结论 | 根因（kind / namespace / name） | load_seconds | 模型轮次 | root_cause_entity_pass |
|---|---|---|---|---|---|
| S2/trial-1 | partial | Deployment / otel-demo / cart | 46.3 | 24 | 0.0 |
| S2/trial-2 | partial | Deployment / otel-demo / cart | 44.7 | 23 | 0.0 |
| S5/trial-1 | supported | Deployment / otel-demo / ad | 67.0 | 30 | 0.0 |
| S5/trial-2 | partial | Deployment / otel-demo / ad | 67.1 | 22 | 0.0 |
| S8/trial-1 | partial | ConfigMap / otel-demo / flagd-config | 62.2 | 22 | 1.0 |
| S8/trial-2 | partial | Deployment / otel-demo / payment | 61.8 | 34 | 0.0 |
| S12/trial-1 | partial | null | 60.3 | 23 | 0.0 |
| S12/trial-2 | partial | null | 60.1 | 15 | 0.0 |
| S14/trial-1 | partial | Deployment / otel-demo / ad | 49.7 | 15 | 0.0 |
| S14/trial-2 | supported | Deployment / otel-demo / ad | 50.1 | 25 | 0.0 |
| S22/trial-1 | partial | Schedule / chaos-mesh / otel-demo-ad-memory-stress | 58.2 | 15 | 1.0 |
| S22/trial-2 | partial | Schedule / chaos-mesh / otel-demo-ad-memory-stress | 59.0 | 22 | 1.0 |
| S34/trial-1 | partial | Pod / otel-demo / valkey-cart-58df56c79c-bf4lr | 58.3 | 22 | 1.0 |
| S34/trial-2 | partial | Deployment / otel-demo / valkey-cart | 58.9 | 21 | 1.0 |
| S105/trial-1 | supported | Deployment / otel-demo / product-catalog | 37.3 | 10 | 1.0 |
| S105/trial-2 | supported | Deployment / otel-demo / product-catalog | 37.4 | 18 | 1.0 |

驱动层 16/16 completed，`history` 全空，`gap_seconds` 都约 5.0，judge 全部 `ok`。

## 门（合同 r2 验证口径）

| 门 | 读数 | r1 批 | 结论 |
|---|---|---|---|
| `REPORT_INVALID` ≤ 1/16 | 0/16（冒烟 0/4） | 2/16 | 过 |
| 首答重试率不高于约 8/16 | 10/16（冒烟 4/4） | 11/16（冒烟 4/4） | 不过（约定口径为约 8/16） |
| root_cause_entity_pass ≥ 4/16 | 7/16 | 4/16 | 过 |
| 根因非 null ≥ 80% | 14/16 = 87.5%；不含 S12 为 14/14（S12 两次 null，与 #646 批相同） | 12/14 | 过 |

结论分布：supported 4、partial 12、inconclusive 0。

## 首答被拒原因（只读重放）

做法同 r1 批「拒收原因」一节。冒烟和批合计 14 次首答被拒，强制重试后全部通过：

- JSON 解析失败 5 次：JSON 前有文字（如 "The evidence chain is complete. Here is the report"）或用了 ```json 围栏。
- `cites_non_ok_view` 4 次：fact 引用了 no_data 视图来证明「没有日志」「没有某个 ConfigMap」「没有某类指标」。其中 3 次引用的是 `fetch_pod_logs`。
- `unknown_evidence_id` 4 次：引用写成 `<uuid>-t<n>`、uuid 被截断，或引用了 `-scope:` 形式的非工具 id。
- 缺 `next_steps` 1 次。

逐 trial 的表、原始回复与重放输出不入库，放在主机证据备份与本机分析目录。

## 费用

DeepSeek 合计缓存命中 21,561,344、未命中 2,247,580、输出 705,067。北京时间 11-17 09:22–10:17，属于高峰。单价同 [soak 费用主数](../m3-soak-2026-11-07/cost-primary.json) 的高峰价，约 **CNY 11.00**。judge 费用未统计。

## 证据

16 份 summary 与 `scores.json`。入库前在主机上做了秘密值原文搜索：0 命中，正对照命中 1。sha256 本机与主机一致。
