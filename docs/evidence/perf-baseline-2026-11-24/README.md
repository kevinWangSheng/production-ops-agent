# #498 性能基线（2026-11-24）

只读测量：用近期租机已冻结的 `summary.json` 里的产品内时间戳，算出报告耗时与 `traces_search` 超上限次数。不改产品代码，不新跑 Run，不花钱。依据：裁决 R8（`docs/tasks/2026-11-24-open-issues-round.md`）与预审 `docs/tasks/2026-11-24-round-prereview-b-judge-perf-itbench.md` 第二节（A498-1 到 A498-4）。

## 方法

- 出报告耗时 = `run.incident.created_at` → `run.conclusion_step.observed_at`，两者都是产品 PG 的 `clock_timestamp()`，不依赖调度器。没有结论步的 Run 单独计数（本批为 0）。
- 步间隔 = 相邻 `run.steps[].observed_at` 之差的中位数，粗略代表一轮（模型请求加工具执行）的耗时；`elapsed_seconds` 不在冻结的 summary 里，所以工具耗时与模型耗时**不能分开**（未确认单次模型请求耗时是否另有记录）。
- 超上限 = `run.tool_requests[]` 中 `reason == "RESULT_TOO_LARGE"`；`SOURCE_UNAVAILABLE` 同理按 `reason` 计。
- 分位数用线性插值。按轨道分开报告（Holmes 题集、SREGym），合计行仅作规模参考，分数与时间不跨轨道比较。
- 复现：`python3 scripts/perf_baseline_498.py docs/evidence`（stdlib，只读）。

## 输入

`docs/evidence/rental-2026-11-20/full/{opspilot,sregym}`、`rental-2026-11-20/rerun-2026-11-22/{opspilot,sregym}`、`rental-2026-11-23/full/{opspilot,sregym}` 下全部 `summary.json`，共 559 个。预审口径（11-20 Holmes 题集 174 + 11-23 的 237）对应其中 411 个，数字与预审粗算一致（`traces_search` 297 次调用、33 次超上限、31 个 Run）。HolmesGPT 侧的 summary 不在其中。11-22 重跑与 11-20 / 11-23 全量覆盖同一批题，Run 彼此独立但题目有重叠；它们是单列行，不算进两个「合计」行（合计全部含重跑，仅作规模参考）。

## 结果

| 组 | Run 数 | 有结论步 | p50 s | p95 s | max s | 步数中位 | 步间隔 p50 s | traces_search 调用 | 超上限 | 超上限 Run | SOURCE_UNAVAILABLE 调用 | 全部工具调用 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 11-20 Holmes 题集 OpsPilot | 174 | 174 | 64 | 201 | 313 | 10 | 3.6 | 98 | 0 | 0 | 738 | 3253 |
| 11-20 SREGym | 51 | 51 | 240 | 456 | 577 | 33 | 5.3 | 112 | 33 | 29 | 0 | 2795 |
| 11-22 重跑 Holmes 题集 | 94 | 94 | 88 | 214 | 283 | 13 | 3.7 | 82 | 0 | 0 | 540 | 2247 |
| 11-22 重跑 SREGym | 3 | 3 | 282 | 374 | 384 | 30 | 6.5 | 2 | 2 | 2 | 0 | 132 |
| 11-23 Holmes 题集 OpsPilot | 186 | 186 | 41 | 141 | 219 | 8 | 3.2 | 84 | 0 | 0 | 590 | 2828 |
| 11-23 SREGym | 51 | 51 | 217 | 355 | 420 | 33 | 4.7 | 115 | 33 | 31 | 9 | 2621 |
| 合计 Holmes 题集（11-20 + 11-23） | 360 | 360 | 51 | 163 | 313 | 9 | 3.4 | 182 | 0 | 0 | 1328 | 6081 |
| 合计 SREGym（11-20 + 11-23） | 102 | 102 | 230 | 420 | 577 | 33 | 5.0 | 227 | 66 | 36 | 9 | 5416 |
| 合计全部 | 559 | 559 | 74 | 296 | 577 | 12 | 4.0 | 493 | 68 | 38 | 1877 | 13876 |

超上限（RESULT_TOO_LARGE）按工具： {'elasticsearch_list_indices': (29, 474), 'elasticsearch_mappings': (13, 333), 'fetch_pod_logs': (22, 1304), 'get_series': (2, 95), 'grafana_loki_query': (18, 857), 'kubernetes_jq_query': (20, 4090), 'kubernetes_tabular_query': (8, 689), 'metrics_range_query': (1, 693), 'traces_search': (68, 493), 'traces_search_by_tags': (22, 415)}
SOURCE_UNAVAILABLE 前 8 工具： [(327, 'get_metric_names'), (327, 'execute_prometheus_instant_query'), (258, 'traces_list_services'), (256, 'traces_search'), (194, 'metrics_range_query'), (145, 'get_label_values'), (86, 'get_all_labels'), (81, 'get_prometheus_target')]

要点：
- Holmes 题集 360 个 Run：p50 51 s，p95 163 s，最大 313 s。SREGym 102 个 Run：p50 230 s，p95 420 s，最大 577 s；SREGym 的 Run 步数约为 Holmes 的 3.5 倍（中位 33 对 9），耗时与步数大致成比例，步间隔都在 3 到 5 秒。
- 11-23 比 11-20 快：Holmes p50 64 → 41 s，SREGym 240 → 217 s。原因**未确认**（这期间产品与提示词有改动，未做归因）。
- `traces_search` 超上限只出现在 SREGym：227 次调用里 66 次（29%），分布在 36 个 Run（占 SREGym 的 35%）；Holmes 题集 182 次调用 0 次。超上限的其他工具总量很小（`elasticsearch_list_indices` 29、`fetch_pod_logs` 22、`traces_search_by_tags` 22、`kubernetes_jq_query` 20、`grafana_loki_query` 18）。
- 考试历史数字（p50 138 s，共享 vCPU，老产品 `a2a68ef4`）与上表题集、主机、产品版本都不同，**不可比**。

## 热点判定

预审没有给出数值阈值，只写「p50 约 1 分钟对作品集够用、没有明显瓶颈就关闭」。按这个口径：

- Holmes 题集 p50 约 51 s、p95 约 163 s，不构成瓶颈。
- SREGym p50 约 4 分钟，慢在步数多而不是单步慢；`traces_search` 超上限每次大约多花一轮（约 5 s），每个受影响 Run 平均 2 次左右，占 SREGym 单 Run 耗时约 2% 到 5%（粗估，来自步间隔，未做逐步归因）。不是明显热点。
- 另一个候选是 `SOURCE_UNAVAILABLE`：全部 Run 里 1877 次（占工具调用约 14%，几乎全在 Holmes 题集，约 22%），集中在当题没有对应后端的工具上。每次是否多占一轮、占多少秒**未确认**（summary 没有逐调用耗时）。它超出 #498 原范围，只作记录。

结论：#498 范围内没有明显热点，可以关闭；`SOURCE_UNAVAILABLE` 如要处理，另开 issue 并随下次租机验证。

## 局限

- 主机规格、共享还是独占 vCPU：**未确认**；按 SPEC 不与 HolmesGPT 比延迟，也不与考试数字比。
- 步间隔把模型请求与工具执行合在一起，不能拆分两者各占多少。
- 各组之间产品版本不同（提示词、工具集在 11-20 与 11-23 间有变化），合计行是混合口径。
