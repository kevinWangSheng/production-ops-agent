# ITBench-Lite 全量：结构化根因（#646），35 题 × 3 次

类别：集成运行（真实 DeepSeek `deepseek-flash`、回放组件、真实 judge Z.ai `glm-5.3-flashx`）。这是 #646 之后第一次全量，与 [lite-full-1114](../itbench-lite-sre-lite-full-1114/run.md) 对照。依据 [任务记录](../../tasks/2026-11-15-itbench-lite-root-cause.md) 与 [租机手册](../../tasks/2026-11-13-rental-runbook.md) 阶段 6。时间 UTC，2026-11-16（括号内为太平洋时间）。

- **提交 F**：`d737241714c0f699e37cd2cbfb64407fe98815cd`（main，PR-A #653 与 PR-B #662 合并后）。主 checkout 用 `--ff-only` 快进，没有已跟踪修改；`uv.lock`、`pyproject.toml` 与集成批时相同，没有跑 `make setup`。快进前，本轮之前的未跟踪证据（18 个文件，与入库版本 sha256 一致）移到了主机 `~/rental-evidence-backup/20261116T121025/`。
- **冻结版本**（主 checkout 上计算）：`otel_demo_ext_versions()` = `prompt-replay-candidate-k8s-d387f77762f5` / `otel-demo-ext-d467c4b436b0` / `ctx-ctx-policy-v1-a30e65ea52f4`（与[集成批](../itbench-lite-sre-lite-batch-646/run.md)相同）；`otel_demo_versions()` = `prompt-replay-candidate-a6b4dfd02030` / `otel-demo-a3a95a0f888e` / `ctx-ctx-policy-v1-a30e65ea52f4`（本轮不用）。
- 主机与数据：同集成批记录（Vultr，8 vCPU / 32 GB；数据集 revision `d0916b08…`，SRE v0.2）。词表、kinds 旁路和分档用集成批时在主机上生成的 `tmp/itbench-lite/inputs/`，[分档](tiers.json)与 lite-full-1114 相同。
- 开跑前：kind lab 已停（节点容器 Exited，没有其他容器），磁盘可用 89G，swap 0，可用内存 31 GB。

## 命令与分区

`set -a; . ~/rental-private/lite.env; set +a`，`ITBENCH_EVALUATIONS_DIR=~/rental-data/ITBench-Evaluations`，`.venv/bin/python -m scripts.itbench_lite.driver round --round lite-full-1116 --dataset <数据集根> --vocabulary-dir tmp/itbench-lite/inputs/vocab --scenarios <分区> --trials 3 --env-file ~/rental-private/lite.env [端口]`。两个 driver 进程并行，分区方法：两个覆盖档内各按题号排序后交替分配。

| 进程 | 题 | 端口 | 起止 | 退出 |
|---|---|---|---|---|
| p1 | S1,S11,S14,S17,S20,S25,S80 + S2,S6,S8,S13,S18,S22,S29,S33,S38,S91,S105（18 题，54 个 trial） | 默认（29110/29190/29186/29118/29143/29180） | 13:00:25–19:26:21（05:00–11:26 PT） | 0 |
| p2 | S5,S12,S16,S19,S24,S34,S83 + S4,S7,S9,S15,S21,S23,S31,S35,S81,S102（17 题，51 个 trial） | 各 +100 | 13:00:30–18:54:38 | 0 |

之后 `driver scores --round lite-full-1116 --out scores.json`（`official_missing` 为空，`conversion_methods` structured 102、heuristic 3），再对 `root_cause_entity_pass`、`root_cause_entity_f1`、`det_pass`、`det_f1` 各跑一次 `coverage --tiers tiers.json --trials 3`，出 4 张表。

## 与 lite-full-1114 的条件差异

| 项 | lite-full-1114 | 本轮 |
|---|---|---|
| 报告 | `m0-report-v2`，根因由 L2 启发式从正文猜 | `m0-report-v3`，报告给出结构化 `root_cause`，换算直接读字段（C3） |
| 提示词 | `prompt-replay-candidate-k8s-0282ee7d0e7a` | `prompt-replay-candidate-k8s-d387f77762f5` |
| 词表 | L-V7（去掉 Namespace 对象名） | #647：另外去掉所有 `metadata.namespace` 值；加 kinds 旁路（C4） |
| 进程 | 单个 driver | 两个 driver 并行（用户同意「不影响结果就并行」；集成批两进程并行时没有作废，灌入耗时与串行一致） |
| lab | 停 | 停 |
| judge | Z.ai `glm-5.3-flashx` | 同左 |

两轮的分数不是同一提示词、同一换算下的结果，只能并列，不能归因到单一改动（任务记录「lead 的三条判断」第 3 条）。

## 运行情况

- 105/105 `completed`，`history` 全空（没有作废、没有补跑机会），judge 全部 `ok`。
- `gap_seconds` 最大 5.01（作废线 120），`load_seconds` 最大 83.3（4 分钟风险线以下）。全程 swap 0；结束时磁盘可用 34G。
- 产品拒收（`REPORT_INVALID`，换算记 `REPORT_REJECTED_BY_PRODUCT`，按 #645 视为无报告）：S22 trial-1、S25 trial-3、S7 trial-2，共 3/105（lite-full-1114 为 1/105，S23 trial-1）。具体校验原因不在冻结 summary 里，原始 ledger 不入库。
- 没有因模型不可用而交接的 trial。

## 分数（④ 主表）

| 指标 | 指标+trace（14 题） | 故障窗无 trace（21 题） | 全部（35 题） | lite-full-1114 ③ 全部 |
|---|---|---|---|---|
| root_cause_entity_pass | 0.262 | 0.206 | **0.229**（24/105） | 0.029（3/105） |
| root_cause_entity_f1 | 0.262 | 0.206 | 0.229 | 0.029 |
| det_pass | 0.143 | 0.000 | 0.057 | 0.019 |
| det_f1 | 0.143 | 0.000 | 0.057 | 0.019 |

每题明细见 `table-<指标>.md`。数据残缺题 S18、S38 照旧标在表里。

## 与集成批同口径的数字

分母口径同集成批四道门：② 的分母是产品接受且结论非 `inconclusive` 的报告；④ 的结论分布只算产品接受的报告。

| 范围 | ① 产品拒收 | ② 根因非 null | 结论 supported / partial / inconclusive | supported 占比 | root_cause_entity_pass |
|---|---|---|---|---|---|
| 全部 35 题 | 3/105 | 95/102 = 93.1% | 63 / 39 / 0 | 61.8% | 24/105 = 0.229 |
| 集成批 8 题（单列） | 1/24（S22 t1） | 21/23 = 91.3% | 13 / 10 / 0 | 56.5% | 5/24 = 0.208（S34 2 次、S105 3 次） |
| 其余 27 题 | 2/81 | 74/79 = 93.7% | 50 / 29 / 0 | 63.3% | 19/81 = 0.235 |
| lite-full-1114 全部（对照） | 1/105 | 根因为 null 72/105（L2 换算，不同口径） | 70 / 35 / 0 | 66.7% | 3/105 = 0.029 |
| lite-full-1114 集成批 8 题（对照） | 0/24 | — | 17 / 7 / 0 | 70.8% | 0/24 |

- 根因为 null 的 7 份（都是产品接受、非 inconclusive）：S1 t2、S12 t2、S12 t3、S19 t1、S4 t2、S81 t1、S81 t3。
- ③ 根因 kind 分布（95 份，取报告字段原值）：Deployment 76、Pod 12、ResourceQuota 3、PodNetworkChaos 2、Service 1、Prometheus 1；其中 name 都不是命名空间名（词表按 #647 排除，结构化字段不受词表约束，本项按 kind 与 name 检查）。换算 `reason` 只有上面 3 个产品拒收为非空。
- ④ 结论分布没有移向 supported（61.8%，基线 66.7%），分数从 0.029 升到 0.229。

## 费用

- DeepSeek（调查）：105 个 summary 的 `run.tokens` 合计缓存命中 138,910,080、未命中 12,873,748、输出 4,536,015。北京时间 2026-11-16 21:00 至 11-17 03:26，全部闲时。单价沿用 [soak 费用主数](../m3-soak-2026-11-07/cost-primary.json)（闲时每百万 token 命中 0.02 / 未命中 1 / 输出 4 CNY；本轮没有重读价格页），合计 **CNY 33.8**。账户共用，余额差不作依据。
- judge（Z.ai）费用未统计。
- 主机：2026-11-16 05:51Z 开机，单价以 Vultr 账单为准。

## 证据与秘密值

- 入库：`S<id>/trial-<n>/summary.json`（105 份）、`scores.json`、4 张表（json 与 md 各一）、`tiers.json`、`tiers_detail.json`，共 116 个文件。
- 原始 ledger 与回放目录（`tmp/itbench-lite/lite-full-1116/`、`tmp/lab-ledgers/`）不入库。trial 的 `outputs/`、`*.json`、日志与 lab ledger 另拷了一份到本机仓库外（不含 PG、Prometheus、fake_k8s 数据和快照副本）。
- 入库前在主机上对 13 个已知秘密值（lab Prometheus 口令、webhook 与发布 token、集群 token、DeepSeek/LangSmith/Z.ai key、工作台口令）做原文搜索：116 个文件 0 命中；正对照（在临时副本里植入一个值）命中 1。116 个文件的 sha256 本机与主机一致。

## 未执行

- 没有重判、没有 rescore；分数直接来自本轮 judge。
- 产品拒收的 3 份报告没有展开具体校验原因。
