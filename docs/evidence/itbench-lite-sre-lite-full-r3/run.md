# ITBench-Lite 全量：r3 发布（#695 + #690 调查指导、#686 每轮 JSON 模式），35 题 × 3 次

类别：集成运行（真实 DeepSeek `deepseek-flash`、回放组件、真实 judge Z.ai `glm-5.3-flashx`）。这是 r3 发布的全量，与 [lite-full-1117](../itbench-lite-sre-lite-full-1117/run.md)（#676 r2）对照。依据 [r3 任务记录](../../tasks/2026-11-17-r3-prompt-release.md)。时间 UTC，2026-11-17 至 11-18（括号内为太平洋时间）。

- **提交 F**：`b0652dbd721c5a2b2361cbabba1d1d7b7a5d0e5d`（主机本地分支 `r3-combined`，`feature/695-setting-restart-guidance` `ea2bc41b` 以 `--no-ff` 合并 `feature/686-json-mode-every-round` `719919e7`，tree `8382dec5`）。它的代码树与 PR-G 最终 HEAD `d065c41e`（已含 main 上的 PR-R `c7f89dbc`）逐字节相同：`opspilot/`、`scripts/`、`evals/`、`deploy/`、`pyproject.toml`、`uv.lock` 的 `ls-tree` 哈希一致（lead 核对）。105 个 summary 的 `itbench_lite.git.commit` 都是这个提交，主 checkout 无已跟踪修改。
- **冻结版本**：`otel_demo_ext_versions()` = `prompt-replay-candidate-k8s-1137251eea4d` / `otel-demo-ext-c63f058afb18` / `ctx-ctx-policy-v1-a30e65ea52f4`（本轮用这个档位，105 个 summary 一致）；`otel_demo_versions()` = `prompt-replay-candidate-860b743085d1` / `otel-demo-a3a95a0f888e` / 同一 context policy。tool schema 与 context policy 与 1117 相同。
- 主机：新租 Vultr（Ubuntu 24.04.5，8 vCPU / 32 GB / 150 GB），环境同[租机手册](../../tasks/2026-11-13-rental-runbook.md)第 1 节；数据集 `ibm-research/ITBench-Lite` revision `d0916b08…`，SRE `v0.2`，31,020,214,628 字节、3456 个文件，与上次相同。词表、kinds 旁路与[分档](tiers.json)在本机重新生成，`tiers.json` 与 1117 的逐键相同。
- 开跑前：同机的[冒烟](../itbench-lite-sre-lite-smoke-r3/run.md)、[集成批](../itbench-lite-sre-lite-batch-r3/run.md)与 F3 三次 Run 都已结束（F3 Run 3 22:56Z 完成报告并已冻结）；kind lab、lab PG 与 F3 web 仍开着，worker 已停。

## 命令与分区

命令同 1117（`driver round --round lite-full-r3 … --trials 3 --env-file ~/rental-private/lite.env`），分区改为 4 个 driver 进程并行（1117 为 2 个）：两个覆盖档内各按题号排序后依次轮流分配，回放端口按进程加偏移。

| 进程 | 题 | 端口偏移 | 起止 | 退出 |
|---|---|---|---|---|
| p1 | S1,S14,S20,S80 + S6,S13,S22,S33,S91（9 题） | 0 | 23:49:32–02:57:18（15:49–18:57 PT） | 0 |
| p2 | S5,S16,S24,S83 + S7,S15,S23,S35,S102（9 题） | +100 | 23:49:34–02:55:12 | 0 |
| p3 | S11,S17,S25 + S2,S8,S18,S29,S38,S105（9 题） | +200 | 23:49:52–02:48:09 | 0 |
| p4 | S12,S19,S34 + S4,S9,S21,S31,S81（8 题） | +400 | 23:49:54–02:28:22 | 0 |

偏移跳过 +300：它的 Jaeger OTLP 端口 29418 是 F3 第 3 步重跑依赖的「无监听」端口。之后 `driver scores`（`official_missing` 为空，`conversion_methods` structured 104、heuristic 1），再用 `coverage --tiers tiers.json --trials 3` 出 4 张表。用时 3 小时 8 分（1117 两个进程 6 小时 38 分）。

说明：lead 在 23:49:32 用同一个 `launch.sh full` 启动，执行者 2 秒后又调用了一次；脚本按 tmux 会话名跳过已在跑的分区，每个分区只起了一次，核实主机上正好 4 个 driver 进程。

## 运行情况

- 驱动层 105/105 个 trial 记为 `completed`，`history` 全空（没有作废），judge 全部 `ok`。按产品 Run 状态分：104 个 Run 发布了报告，1 个因 `REPORT_INVALID` 交接（S12 t2）。`gap_seconds` 最大 5.01，`load_seconds` 最大 88.1。lab 每分钟读数：checkout p95 最高 48 ms，可用内存最低 17.6 GB，swap 最多用 127 MiB。结束时磁盘可用 23G。
- **首答被拒**：每个 trial 看 run ledger，终答轮（`finish_reason=stop` 且没有工具调用）≥ 2 次。本轮 29/105，1117 同一口径是 39/105。29 次里重试后通过 28 次，1 次仍被拒（S12 t2）。0 次上下文压缩。
- **S12 t2 拒收（只读重放）**：trial 自己的 PG 副本 → `rebuild_transcript`（绑定目标 `m0-otel-20260909`，33 个已交付视图）→ `validated_report`，与 loop 的调用相同。两次终答都是合法 JSON。
  - 第 15 轮：claim 6、11、12、15 的 `unknown_evidence_id`，引用 `63b1ea5c-…-90613cd4088b-t1:…`，实际交付的是 `…4088a-t1:…`（一个字符错）；D6 提示给出了正确 id。
  - 第 16 轮（强制重试）：id 已改对，claim 15 报 `cites_non_ok_view`（`0a942e21-…-t1`）。这个非 ok 视图在第 15 轮的 claim 15 里已经引用，但引用检查对每条 claim 只报第一个失败，所以直到最后一次重试才暴露。这与[集成批](../itbench-lite-sre-lite-batch-r3/run.md) S105 t2 是同一机制，按 #701 跟踪；不是 r3 引入的。

## 分数（④ 主表）

| 指标 | 指标+trace（14 题） | 故障窗无 trace（21 题） | 全部（35 题） | lite-full-1117 全部 |
|---|---|---|---|---|
| root_cause_entity_pass | 0.810 | 0.762 | **0.781**（82/105） | 0.514（54/105） |
| det_pass | 0.405 | 0.079 | 0.210 | 0.152 |

每题明细见 `table-<指标>.md`（root_cause_entity_f1、det_f1 另有表）。数据残缺题 S18、S38 照旧标出。

## 门（r3 验收计划阶段 4）

| 门 | 读数 | 结论 |
|---|---|---|
| root_cause_entity_pass ≥ 54/105 | 82/105 | 过 |
| REPORT_INVALID ≤ 2 | 1/105（S12 t2） | 过 |
| 首答被拒 < 39 | 29/105 | 过 |
| 1117 里 3/3 的 16 题逐题对比 | 15 题仍 3/3；S33 3→2（t1 根因 null，t2、t3 为 Deployment `ad`） | 列出退步：S33 |

## 逐题 root_cause_entity_pass（1117 → r3，每题 3 次）

| 题 | 1117 | r3 | | 题 | 1117 | r3 |
|---|---|---|---|---|---|---|
| S1 | 0 | 0 | | S20 | 3 | 3 |
| S2 | 0 | 2 | | S21 | 3 | 3 |
| S4 | 0 | 2 | | S22 | 3 | 3 |
| S5 | 0 | 3 | | S23 | 3 | 3 |
| S6 | 0 | 2 | | S24 | 3 | 3 |
| S7 | 0 | 3 | | S25 | 3 | 3 |
| S8 | 0 | 2 | | S29 | 3 | 3 |
| S9 | 0 | 2 | | S31 | 0 | 0 |
| S11 | 0 | 3 | | S33 | 3 | **2** |
| S12 | 0 | 0 | | S34 | 2 | 2 |
| S13 | 0 | 3 | | S35 | 3 | 3 |
| S14 | 0 | 3 | | S38 | 0 | 0 |
| S15 | 0 | 3 | | S80 | 2 | 2 |
| S16 | 3 | 3 | | S81 | 3 | 3 |
| S17 | 3 | 3 | | S83 | 3 | 3 |
| S18 | 2 | 3 | | S91 | 0 | 0 |
| S19 | 3 | 3 | | S102 | 3 | 3 |
| | | | | S105 | 3 | 3 |

升 12 题（S2、S4–S9、S11、S13–S15、S18），降 1 题（S33），其余不变。两轮都是 0 的 5 题：S1、S12、S31、S38、S91。

## 结论与根因分布

| 范围 | 产品拒收 | 根因非 null（产品接受的报告） | supported / partial / inconclusive | root_cause_entity_pass |
|---|---|---|---|---|
| r3 全部 35 题 | 1/105 | 88/104 = 84.6% | 22 / 83 / 0 | 82/105 = 0.781 |
| lite-full-1117 全部（对照） | 2/105 | 91/103 = 88.3% | 51 / 52 / 0 | 54/105 = 0.514 |

- 根因为 null 的 17 份：S1 t1/t3、S2 t2、S6 t1、S8 t2、S9 t2、S12 t1–t3（t2 为被拒的那次）、S31 t1–t3、S33 t1、S34 t2、S38 t3、S91 t2/t3（1117 为 12 份）。
- 根因 kind 分布（105 份）：Schedule 32、ConfigMap 28、Deployment 23、null 17、ResourceQuota 3、Pod 2。1117 是 Deployment 55、Schedule 28，ConfigMap 没有出现；这次设置类故障题把根因写成持有设置的 ConfigMap（#695 的目标行为）。
- 结论明显向 partial 偏移：supported 从 51 降到 22。分数上升没有伴随 supported 增多，与「防编造」方向一致，但值班读者看到的确定性结论变少；根因为 null 也从 12 升到 17。

## 费用

- DeepSeek：105 个 summary 的 `run.tokens` 合计缓存命中 126,656,896、未命中 10,620,292、输出 3,826,790。北京时间 11-18 07:49–10:57，09:00 起是高峰；按每个 trial 的开始时刻分，64 个在高峰。单价同 [soak 费用主数](../m3-soak-2026-11-07/cost-primary.json)，合计约 **CNY 42.9**（1117 约 51.9）。账户共用，余额差不作依据。
- judge（Z.ai）费用未统计。

## 证据与秘密值

- 入库：105 份 summary、`scores.json`、4 张表（json 与 md 各一）、`tiers.json`、`tiers_detail.json`，共 116 个文件。
- 原始 ledger、trial 日志、两次拒收的重放输出与主机脚本不入库，打包在仓库外 `~/rental-evidence/r3/r3-raw.tgz`（13,161,464 字节，sha256 `d78e6b74…fce5`，两端一致）。
- 入库前在主机上对 14 个已知秘密值做原文搜索：本轮 116 个证据文件 0 命中；拷回前对全部暂存的 1,232 个文件（证据加原始包内容）再搜一次，0 命中；正对照（植入一个值）命中 1。148 个 r3 证据文件按清单核对 sha256，本机与主机一致。`scores.json` 在本机从原始包里的逐 trial 分数重算，与主机产物逐字节相同。

## 未执行

- 没有重判、没有 rescore。
- 与 1117 相比，提示词（#695 的 K8s 段、#690 的 INVESTIGATION_METHOD 两句）与报告轮请求（#686 每轮 JSON 模式、U2/U3 默认值、D6 重试提示）都变了，换算方式相同；两轮分数可以并列，但不能归因到某一处改动。
- json_mode 只按代码（每个 `round-K` 请求都是 `json_mode=True`，只有压缩请求为 False）与 ledger（0 次压缩）间接核对；LangSmith span 的 `opspilot.model.json_mode` 没有逐个回读。
