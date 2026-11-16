# ITBench-Lite 集成批：结构化根因（#646），8 题 × 2 次

类别：集成运行（真实 DeepSeek `deepseek-flash`、回放组件、真实 judge Z.ai），不计入全量分数。依据 [任务记录](../../tasks/2026-11-15-itbench-lite-root-cause.md)「集成批（跑前写死）」与 [租机手册](../../tasks/2026-11-13-rental-runbook.md) 阶段 1–2。时间 UTC，2026-11-16（括号内为太平洋时间）。

- 主机：Vultr 新实例，IP 104.207.153.253，Ubuntu 24.04.5，8 vCPU / 32 GB / 150 GB；05:51Z（21:51 PT 11-15）开机，单价以 Vultr 账单为准。
- 代码：主 checkout 在 `feature/646-eval-root-cause` `c772eae7`（PR-B，含 PR-A `fa7c72bf`），16 个 summary 的 `itbench_lite.git.commit` 都是它，`dirty` 全为 false。版本 `prompt-replay-candidate-k8s-d387f77762f5` / `otel-demo-ext-d467c4b436b0` / `ctx-ctx-policy-v1-a30e65ea52f4`，16 个一致。
- 环境（阶段 1）：`install_linux.sh` 退出 0（health ok）；PG 17、uv、jq、`make setup` 退出 0；评测器 `14f026fc9cc3…`（`uv sync --frozen`）；数据集 `ibm-research/ITBench-Lite` revision `d0916b08…`，SRE v0.2，31,020,214,628 字节 / 3456 个文件 / 35 个场景目录，与 11-14 相同（匿名下载中途被 HF 429 限流，重试后补齐，按总字节与文件数核对）。
- 词表（#647、C4）：35 份 `S<id>.txt` 与 35 份 `S<id>.kinds.json` 由 `vocabulary --kinds-out` 在主机上重新生成；抽查 S12、S23 词表不含 `otel-demo`。分档 `tiers.json` 35 题，与 `lite-full-1114/tiers.json` 逐键相同。`select_batch tiers.json --salt 646: --per-tier 4` 输出 `12,5,14,34,2,8,105,22`，与任务记录题单一致。
- 命令：`set -a; . ~/rental-private/lite.env; set +a; ITBENCH_EVALUATIONS_DIR=~/rental-data/ITBench-Evaluations .venv/bin/python -m scripts.itbench_lite.driver round --round lite-batch-646 --dataset <数据集根> --vocabulary-dir tmp/itbench-lite/inputs/vocab --scenarios <题> --trials 2 --env-file ~/rental-private/lite.env`，分三段：A `S12,S5,S14`（07:36:51–08:19:54Z）；B1 `S34,S2,S8`（默认端口）与 B2 `S105,S22`（六个回放端口各 +100）并行，09:32:44 起，B2 09:56:29Z、B1 10:14:16Z 结束。三段退出码都是 0。之后 `driver scores --round lite-batch-646 --out scores.json`（`official_missing` 为空，`conversion_methods` structured 15、heuristic 1）。

## 同机条件（与基线不同，照实记录）

lead 同意的做法：kind lab 一直开着（同机跑 F3 第 5 步），ITBench trial 避开 F3 worker 的调查窗口（告警到报告），B 段两进程并行。基线 `lite-full-1114` 是 lab 停着、单进程跑的。lab 每分钟读数（07:36–10:14Z）：checkout p95 最高约 35 ms，swap 全程 8191/8191 MiB 空闲，可用内存不低于约 25 GB。16 个 trial 的 `gap_seconds` 都约 5.0（作废线 120 秒），`load_seconds` 36–65（4 分钟风险线以下），`history` 全空、没有作废。B1 的最后一个 trial（S34 trial-2，约 10:08–10:14Z）和 F3 Run 2 的故障窗口有重叠，但和它的调查窗口（10:16Z 起）不重叠。

## 每个 trial

| trial | 结论 | 根因（kind / namespace / name） | 换算 | load_seconds | gap_seconds | 模型轮次 | root_cause_entity_pass |
|---|---|---|---|---|---|---|---|
| S2/trial-1 | partial | Deployment / otel-demo / frontend | structured | 44.1 | 5 | 32 | 0.0 |
| S2/trial-2 | partial | Deployment / otel-demo / cart | structured | 44.1 | 5 | 24 | 0.0 |
| S5/trial-1 | supported | Deployment / otel-demo / ad | structured | 65.4 | 5 | 20 | 0.0 |
| S5/trial-2 | partial | Deployment / otel-demo / ad | structured | 65.3 | 5 | 17 | 0.0 |
| S8/trial-1 | supported | Deployment / otel-demo / payment | structured | 60.4 | 5 | 18 | 0.0 |
| S8/trial-2 | supported | Deployment / otel-demo / frontend-proxy | structured | 60.8 | 5 | 18 | 0.0 |
| S12/trial-1 | supported | null | structured | 59.7 | 5 | 14 | 0.0 |
| S12/trial-2 | supported | null | structured | 59.4 | 5 | 21 | 0.0 |
| S14/trial-1 | supported | Deployment / otel-demo / ad | structured | 49.6 | 5 | 22 | 0.0 |
| S14/trial-2 | supported | Deployment / otel-demo / ad | structured | 49.8 | 5 | 17 | 0.0 |
| S22/trial-1 | supported | Deployment / otel-demo / ad | structured | 58.7 | 5 | 23 | 0.0 |
| S22/trial-2 | supported | Deployment / otel-demo / ad | structured | 57.6 | 5 | 18 | 0.0 |
| S34/trial-1 | supported | Pod / otel-demo / valkey-cart-58df56c79c-bf4lr | structured | 57.7 | 5 | 26 | 1.0 |
| S34/trial-2 | 无报告（交接 `MODEL_UNAVAILABLE`） | — | heuristic | 57.4 | 5 | 10 | 0.0 |
| S105/trial-1 | supported | Deployment / otel-demo / product-catalog | structured | 36.4 | 5 | 22 | 1.0 |
| S105/trial-2 | supported | Deployment / otel-demo / product-catalog | structured | 36.4 | 5 | 18 | 1.0 |

全部 `status=completed`、judge `ok`。S34 trial-2：调查跑到第 10 轮时模型调用失败，Run 以 `MODEL_UNAVAILABLE` 交接（`waiting_human`），没有报告；worker 日志里只有这个原因码，没有更多细节。按 C3，原文里没有 `root_cause` 键就走 L2 兜底，`report_present=false`，实体 0 个。

## 四道门（跑前写死）

| 门 | 口径 | 读数 | 结论 |
|---|---|---|---|
| ① | `REPORT_INVALID`（含产品拒收）≤ 1/16 | 0/16；唯一无报告的是 S34 trial-2，属于模型调用失败交接，不是报告无效或被拒 | 过 |
| ② | 产品接受且非 `inconclusive` 的报告中 ≥ 80% 根因非 null | 15 份中 13 份非 null，86.7% | 过 |
| ③ | 根因不是命名空间名；kind 归一或保留原样；换算 `reason` 全空 | 13 个根因的 name 都不是命名空间名（namespace 字段为 `otel-demo`），kind 为 Deployment 12 个、Pod 1 个；`reason` 16/16 为 null | 过 |
| ④ | 结论分布明显移向 supported 而分数不升 → 不过 | 本批 supported 12、partial 3、inconclusive 0（80%）；基线同 8 题 24 个 trial 为 supported 17、partial 7（71%），全量 70/35/0（67%）。root_cause_entity_pass 本批 3/16 = 0.19（S105 两次、S34 trial-1），基线同 8 题为 0/24（③ 口径 `rescore-r638-641-645` 表，这 8 题都是 0.000） | 过：结论略偏 supported，但分数上升 |

两份 null 根因都在 S12，两次都是 supported：报告认为告警的原因是集群里没有任何 Alertmanager 实例（工作负载、Service、Endpoints 都不存在），缺失的对象没法填 `name`，模型就把根因写成了 null。提示词没有修订（门全过）。

分数只记录，不作门（任务记录）。分数表 `scores.json` 在本目录；本批没有出 `coverage` 四张表。

## 费用

- DeepSeek（调查）：16 个 summary 的 `run.tokens` 合计缓存命中 17,874,176、未命中 1,638,726、输出 620,256。单价沿用 [soak 费用主数](../m3-soak-2026-11-07/cost-primary.json)（2026-11-07 读取，本轮没有重读）：北京时间 2026-11-16（周一）15:36–18:14，基本都在高峰（14:00–18:00）。按每个 trial 的开始时刻分高峰和闲时，合计约 **CNY 8.47**。账户共用，余额差不作依据。
- judge（Z.ai）费用未统计。

## 未执行与说明

- 原始 ledger 与回放目录在主机 `tmp/itbench-lite/lite-batch-646/`、`tmp/lab-ledgers/`，不入库。
- 入库前在主机上对 13 个已知秘密值（lab 的 Prometheus 口令、webhook 与发布 token、集群 token、DeepSeek/LangSmith/Z.ai key、工作台口令）做原文搜索：0 命中；正对照（在临时副本里植入一个值）命中 1 个。17 个文件的 sha256 本机与主机一致。
