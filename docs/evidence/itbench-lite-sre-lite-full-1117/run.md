# ITBench-Lite 全量：#676 调查指导（合同 r2），35 题 × 3 次

类别：集成运行（真实 DeepSeek `deepseek-flash`、回放组件、真实 judge Z.ai `glm-5.3-flashx`）。这是 #676 合并后的第一次全量，与 [lite-full-1116](../itbench-lite-sre-lite-full-1116/run.md)（#646）和 [lite-full-1114](../itbench-lite-sre-lite-full-1114/run.md) 对照。依据 [#676 任务记录](../../tasks/2026-11-16-676-investigation-guidance.md)。时间 UTC，2026-11-17（括号内为太平洋时间）。

- **提交 F**：`d9af804d0ff295a834f6031fb361aea9f69ffc99`（main，PR #677 squash 合并后）。主 checkout 用 `--ff-only` 快进，没有已跟踪修改，`uv.lock` 未变。快进前，r2 的未跟踪证据移到了主机 `~/rental-evidence-backup/676r2-20261117T044303/`。
- **冻结版本**：`otel_demo_ext_versions()` = `prompt-replay-candidate-k8s-0b066c7cd6d1` / `otel-demo-ext-c63f058afb18` / `ctx-ctx-policy-v1-a30e65ea52f4`（本轮用这个档位）；`otel_demo_versions()` = `prompt-replay-candidate-8688f6e52275` / `otel-demo-a3a95a0f888e` / 同一 context policy。
- 主机、数据集、词表与 kinds 旁路、[分档](tiers.json)：同 lite-full-1116（同一台主机，没有重新生成）。
- 开跑前：kind lab、lab PG、F3 的 web/worker/转发都停掉了（与 1116 条件一致），没有容器在跑。删掉了 r1/r2 批与冒烟的 20 份快照副本（可从数据集重建），磁盘可用 82G，swap 0。

## 命令与分区

命令、参数与分区方法都同 lite-full-1116（`driver round --round lite-full-1117 … --trials 3`，两个 driver 进程并行）：

| 进程 | 题 | 端口 | 起止 | 退出 |
|---|---|---|---|---|
| p1 | S1,S11,S14,S17,S20,S25,S80 + S2,S6,S8,S13,S18,S22,S29,S33,S38,S91,S105（18 题，54 个 trial） | 默认 | 04:43:51–11:21:37（20:43–03:21 PT） | 0 |
| p2 | S5,S12,S16,S19,S24,S34,S83 + S4,S7,S9,S15,S21,S23,S31,S35,S81,S102（17 题，51 个 trial） | 各 +100 | 04:43:56–10:35:50 | 0 |

之后 `driver scores`（`official_missing` 为空，`conversion_methods` structured 103、heuristic 2），再用 `coverage --tiers tiers.json --trials 3` 出 4 张表。用时比 1116 长（p1 6 小时 38 分，1116 是 6 小时 26 分）。

## 运行情况

- 驱动层 105/105 个 trial 记为 `completed`，`history` 全空（没有作废），judge 全部 `ok`。按产品 Run 状态分：103 个 Run 发布了报告，2 个因 `REPORT_INVALID` 交接（S18 t1、S7 t3）。`gap_seconds` 最大 5.01，`load_seconds` 最大 83.4。全程 swap 最多 2 MiB。结束时磁盘可用 28G。
- **首答重试**：每个 trial 看 run ledger，终答轮（`finish_reason=stop` 且没有工具调用）≥ 2 次，就算首答被拒后重试过。本轮 39/105，lite-full-1116 同一口径是 50/105。39 次里重试后通过 37 次，2 次仍被拒（就是上面两个产品拒收）。
- 首答被拒原因（只读重放：trial 自己的 PG → `rebuild_transcript` → `validated_report`）：JSON 前有文字或用了围栏导致解析失败 20、`unknown_evidence_id` 12、缺 `next_steps` 3、`missing_target_refs` 3（全部 claim 都缺目标引用）、`cites_non_ok_view` 1。
- 两个产品拒收的重放结果：
  - S18 t1：第 14 轮的围栏里 JSON 语法错误（缺逗号）；第 15 轮重试时，claim 17 缺 `evidence_ids`（`schema_error`）。
  - S7 t3：第 37 轮 JSON 前有文字；第 38 轮重试时，claim 11 报 `cites_non_ok_view`。

## 分数（④ 主表）

| 指标 | 指标+trace（14 题） | 故障窗无 trace（21 题） | 全部（35 题） | lite-full-1116 全部 | lite-full-1114 ③ 全部 |
|---|---|---|---|---|---|
| root_cause_entity_pass | 0.595 | 0.460 | **0.514**（54/105） | 0.229（24/105） | 0.029（3/105） |
| det_pass | 0.333 | 0.032 | 0.152 | 0.057 | 0.019 |

每题明细见 `table-<指标>.md`（root_cause_entity_f1、det_f1 另有表）。数据残缺题 S18、S38 照旧标出。

## 与集成批同口径的数字

| 范围 | 产品拒收 | 根因非 null（产品接受的报告） | supported / partial / inconclusive | root_cause_entity_pass |
|---|---|---|---|---|
| 全部 35 题 | 2/105 | 91/103 = 88.3% | 51 / 52 / 0 | 54/105 = 0.514 |
| 不含 S12 | 2/102 | 91/100 = 91.0% | 51 / 49 / 0 | 54/102 |
| 集成批 8 题（单列） | 0/24 | 19/24 = 79.2%（不含 S12：19/21） | 13 / 11 / 0 | 8/24 = 0.333 |
| 其余 27 题 | 2/81 | 72/79 = 91.1% | 38 / 41 / 0 | 46/81 = 0.568 |
| lite-full-1116 全部（对照） | 3/105 | 95/102 = 93.1% | 63 / 39 / 0 | 24/105 = 0.229 |

- 根因为 null 的 12 份：S1 t2、S12 t1–t3、S14 t2、S31 t2、S34 t3、S4 t2–t3、S9 t2、S91 t1、S91 t3。
- 根因 kind 分布（103 份中非 null 的 91 份）：Deployment 55、Schedule 28、ResourceQuota 3、Pod 2、NetworkChaos 2、StressChaos 1。1116 里 Schedule 一次都没有出现，这次 28 次。

集成批 8 题逐题 root_cause_entity_pass（1116 → 1117）：S105 3/3 → 3/3，S34 2/3 → 2/3，S22 0/3 → 3/3（三次都是 Schedule `chaos-mesh/otel-demo-ad-memory-stress`），其余 5 题两轮都是 0。

## 费用

- DeepSeek：105 个 summary 的 `run.tokens` 合计缓存命中 132,125,440、未命中 11,817,106、输出 4,303,818。北京时间 11-17 12:43–19:21，其中 14:00–18:00 是高峰。按每个 trial 的开始时刻分高峰和闲时，单价同 [soak 费用主数](../m3-soak-2026-11-07/cost-primary.json)，合计约 **CNY 51.9**。账户共用，余额差不作依据。
- judge（Z.ai）费用未统计。

## 证据与秘密值

- 入库：105 份 summary、`scores.json`、4 张表（json 与 md 各一）、`tiers.json`、`tiers_detail.json`，共 116 个文件。
- 原始 ledger、回放目录、首答被拒各轮的原文与重放输出都不入库，留在主机（之后打包拷回本机仓库外）。
- 入库前在主机上对 13 个已知秘密值做原文搜索：116 个证据文件加 6 个拒收重放文件，共 122 个文件，0 命中；正对照（植入一个值）命中 1。116 个文件的 sha256 本机与主机一致。

## 未执行

- 没有重判、没有 rescore。
- 与 lite-full-1116 相比，提示词（L1 调查指导、L2 报告段、K8s 工具描述，#676 合同 r1、r2）不同，换算方式相同；两轮分数可以并列，但不能归因到某一句改动。
