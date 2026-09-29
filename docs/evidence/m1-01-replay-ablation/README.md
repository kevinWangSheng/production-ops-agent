# M1-01 最终报告请求重放对照实验（6f 四个 Run 的上下文）

- 日期：2026-09-27 UTC 17:02–18:03。目的：只重放 6f 四个 Run 的**最终报告请求**（其余三轮与工具结果不变），判断剩余报告错误（a 类：无状态记录的 span 被写成某状态；e 类：数值/计数与视图不符）主要随哪个变量下降——提示词规则、覆盖说明、上下文形态，还是模型能力。
- 产品代码零改动：`opspilot/` 下 `git status` 为空；本目录只含实验脚本与结果。数据源为本 worktree 的 6f PostgreSQL（55431，还原完成后已 `stop`，数据保留）。
- 授权：AGENTS.md「费用与真实调用」常设授权；lead 批准的上限为 130 次模型 HTTP、8 CNY（原 5 CNY，按定价估算 100 样本超限后 lead 选择放宽）。实际 **100 次 HTTP**（`ledger.jsonl`），余额 47.52 → 40.59 CNY（`deepseek-balance-before/after.json`，after 在最后一次调用 9 分钟后查询，DeepSeek 结算有延迟），**6.93 CNY**；按官方非高峰单价从 usage 估算为 7.29 CNY（`summary.json`）。

## 方法

1. **还原最终轮输入**（`scripts/rebuild_final_requests.py`）：对每个 Run 调用产品自身的 `DurableStore.rebuild` → 去掉最终步与 conclusion 行 → `rebuild_transcript` → 按 `InvestigationLoop._final_messages` 追加 `FINAL_REPORT_INSTRUCTION` 与 `run_coverage_message(delivered)`。四个 Run 的 `messages_hash` 与最终步记录的 `input_snapshot_hash` **逐字节一致（4/4）**，整个请求体（`max_tokens=16384`、`response_format=json_object`、`thinking=enabled`、`reasoning_effort=high`、`stream=false`、无 tools）的 sha256 也与记录的 `request_sha256` 一致（4/4，`rebuild-manifest.json`）。还原的 messages 含 `reasoning_content`（C3 §12 私有协议字段），只存哈希不入库。
2. **五个实验组**（`scripts/replay.py`，每组对四个 Run 各重放 5 次 = 20 样本，共 100；除所列改动外逐字节不变，组 0 的请求 sha256 与原始记录相同）。分两批跑：先每 Run 3 次（60 样本，当时上限 5 CNY），lead 放宽后补第 4、5 次（40 样本）。
   - 组 0 基线：原样重放。
   - 组 1 规则：在 `FINAL_REPORT_INSTRUCTION` 这条 user 消息末尾追加（空格分隔）："State only values that appear in the cited view. A field that does not appear was not recorded; do not fill in a default such as status code 0 or HTTP 200."
   - 组 2 覆盖说明：同一位置追加："When a statement summarizes several rows, give the number of rows that support it out of the rows shown (for example 13 of 20 rows carry rpc.grpc.status_code 0; 7 have no status recorded), and do not use all/every/none unless every shown row supports it."
   - 组 3 分组摘要（上下文形态，`scripts/span_groups.py`）：不改任何指令；每个 `traces_search` 工具结果视图追加 `span_groups`：按 (service, operation) 分组，给 `rows`、`status`（按 `status_state`/`status_tags` 计数，如 `{"rpc.grpc.status_code=0": 13, "not_recorded": 7}`）、`error_rows`、`duration_us_min/max`；只由视图已有行计算，原行不变。请求增大 3–8 KB。
   - 组 4 换模型：消息完全不变，模型改为官方 `/models` 列出的 `deepseek-v4-pro`（DeepSeek-V4-Pro），其余参数一致。
3. **确定性检查**（`scripts/validate.py`，`validation.json`）：对每个样本套用发布前的 `parse_report` + `unsupported_citations`（用还原的 delivered views、授权目标、time policy）。
4. **盲评**（`scripts/blind_pack.py`，`review-rubric.md`）：样本随机打乱、随机四位编号，Run 别名 R1–R4 也打乱；两批共 8 个全新上下文评审 Agent（Fable 5.1；第一批 4 人各 15 个样本，第二批 4 人各 10 个），只拿评分标准、该 Run 的基线视图（不含 `span_groups`，避免泄露组别）和样本报告，不知组别与实验目的。映射 `reviews/blind-mapping.json` 在评审结束后才并入。评审结果 `reviews/reviewer-*.jsonl`，每条错误附报告原文与视图文件/行号。
5. 汇总 `scripts/summarize.py` → `summary.json`。上游口径由我按评审记录的 `verdict` 判定：normal 窗为 `no_failure`；fault 窗为 `failure_located` 且指向 payment/Charge；`other` 一律计为不正确。

## 结果（每组 20 样本；a/e 为评审计数）

| 组 | a 类总数 | e 类总数 | 含 ≥1 个 a/e 的样本 | 含 a 的样本 | 含 e 的样本 | 校验通过 | 上游口径正确 | 平均 completion token（其中 reasoning） | 估算费用 CNY |
|---|---|---|---|---|---|---|---|---|---|
| 0 基线 | **25** | 20 | **20/20** | 14 | 14 | 19/20 | 18/20 | 8,825（3,614） | 0.77 |
| 1 规则 | 6 | 14 | 12/20 | 4 | 10 | 19/20 | 18/20 | 8,922（3,456） | 0.78 |
| 2 覆盖说明 | **4** | **24** | 17/20 | 4 | 15 | 18/20 | 16/20 | 11,874（6,155） | 1.03 |
| 3 span_groups | 10 | **5** | **9/20** | 7 | 4 | 19/20 | 17/20 | 8,330（3,208） | 0.86 |
| 4 v4-pro | 13 | 2 | **9/20** | 8 | 1 | **20/20** | 18/20 | 10,650（8,051） | 3.84 |

按 Run 分列（a / e / 含错样本数，n=5）：

| 组 | normal-1 | normal-2 | fault-1 | fault-2 |
|---|---|---|---|---|
| 0 | 11 / 4 / 5 | 10 / 5 / 5 | 2 / 6 / 5 | 2 / 5 / 5 |
| 1 | 3 / 5 / 4 | 1 / 4 / 4 | 2 / 4 / 3 | 0 / 1 / 1 |
| 2 | 3 / 6 / 5 | 1 / 5 / 4 | 0 / 9 / 4 | 0 / 4 / 4 |
| 3 | 3 / 2 / 4 | 5 / 0 / 2 | 1 / 1 / 1 | 1 / 2 / 2 |
| 4 | 3 / 0 / 3 | 9 / 2 / 5 | 1 / 0 / 1 | 0 / 0 / 0 |

前 60 样本（每 Run 3 次）单独看时的次序与上表相同：a 类 15 / 2 / 3 / 2 / 11，e 类 13 / 10 / 14 / 5 / 0，含错样本 12 / 8 / 10 / 4 / 6（各 /12）。补跑的 40 样本把组 3 的 a 类从 2 抬到 10（normal-2 的 5 个来自第二批的 2 个样本），其余组的相对位置未变。

校验未通过的 5 个样本全部在前 60 内：`g0-normal-2-r3`（target_refs 写成服务名，非授权目标）、`g1-fault-1-r2`（fact 引用 `no_data` 视图）、`g2-fault-2-r2`（`finish_reason=length`，输出触顶 16384）、`g2-fault-2-r3`（JSON 语法错，20,850 字符）、`g3-fault-2-r3`（引用不存在的 evidence_id）。上游口径「不正确」的 13 个全部是 normal 窗报告被评审记为 `other`（结论 partial/inconclusive，明确不指认原因；normal-1 窗里确有 payment 的 GET/tcp.connect/dns.lookup 探针 ERROR span，报告把它排除在 Charge 路径外，第二批评审者据此一律记 `other`），无一指认错误原因；40 个 fault 窗样本全部定位到 checkout→payment `Charge`。

## 结论

- **a 类错误主要随提示词下降，不随模型能力下降。** 规则句把 a 类从 25 降到 6（组 1），覆盖说明降到 4（组 2）。换更强模型（组 4）a 类仍有 13 个：v4-pro 照样把混有 `not_recorded` 行的视图写成「spans carry gRPC status 0 or HTTP 200」（normal-2 五个样本共 9 处），说明这是对视图语义的默认填充习惯，不是能力不足。分组摘要（组 3）对 a 类只有部分效果（25 → 10）。
- **e 类错误主要随上下文形态下降。** 组 3 把每个 trace 视图的 (service, operation) 行数、状态分布和 duration 区间预先算好，e 类从 20 降到 5、含错样本 20/20 → 9/20，且未改任何指令、输出 token 最少。组 4（v4-pro）e 类 2，说明强模型自己能把数算对，但 a 类不降且费用约 5 倍。
- **两句提示词各有代价。** 组 2 输出平均多 35%（reasoning 1.7 倍），两个 fault-2 样本因此触顶或 JSON 出错，e 类反而升到 24（fault-1 一个 Run 贡献 9 个）：要求逐条报「x of N」让模型写出更多数值，也就产生更多数错的机会。组 1 对 e 类只有小幅下降（20 → 14）。
- 校验通过率各组差异在 1–2 个样本内，与错误类别无关（校验只看引用绑定与 JSON 形状，不看数值）。上游口径 87/100，不正确者均为 normal 窗的过度保守，不因组别系统性变化。
- 建议的下一步是组 1 的规则句 + 组 3 的分组摘要叠加（分别针对 a 与 e），在同一实验框架下验证；本实验没有跑叠加组。

## 局限

- 每 Run 5 次，单个 Run 的波动仍大（组 2 fault-1 的 9 个 e 类、组 4 normal-2 的 9 个 a 类各集中在一个 Run）。表中差异在 a 类（25 vs 4–6）、e 类（20/24 vs 2–5）和「含错样本比例」（20/20 vs 9/20）上足够大；组 0/1/2 之间的 e 类差别（20/14/24）与组 3/4 之间的差别在噪声内。
- 评审为 8 个独立 Agent，样本随机分配但未做交叉复评，评审者之间的定级尺度不同（每样本平均 e 类从 0.33 到 1.33 不等：reviewer-3 在 15 个样本里记了 20 个 e，reviewer-8 在 10 个里记了 10 个）；组 3 的 20 个样本有 7 个来自 reviewer-4、0 个来自 reviewer-2。评审者自报的边界判断：S3076（g0-normal-2-r1）把「没有 ERROR 状态序列」计为 e；S7640（g0-normal-1-r2）把「completed successfully (no error tags)」计为 a；reviewer-6 把「概括句写成 status 0/200、但同报告的 claim 已正确限定」计为一个 a（S7196、S1475、S3119、S7129，分属组 1/3/4）；normal-1 的 `other` 判定见上。去掉这些边界项不改变组间次序。
- 重放不是完整 Run：前三轮的工具调用与视图固定，只考察最终报告这一步；提示词句子加在最终指令上，未在系统提示或前几轮验证；分组摘要只覆盖 trace 视图。DeepSeek 对相同前缀几乎全部缓存命中（含 2 小时前 6f 的前缀），费用由输出决定。
- 上游口径判定依赖评审记录的 verdict 枚举与关键词匹配，`other` 一律计为不正确，偏保守。

## 目录

`README.md`、`review-rubric.md`、`rebuild-manifest.json`（四个 Run 的哈希核对）、`ledger.jsonl`（100 次调用的 usage 与请求 sha256）、`validation.json`、`summary.json`、`deepseek-balance-before/after.json`、`scripts/`（rebuild_final_requests、replay、span_groups、validate、blind_pack、summarize）、`samples/<group>-<case>-r<n>/{report.txt,meta.json}`（模型输出原文，不含 reasoning_content）、`reviews/`（8 份评审结果与盲评映射）。脚本以 `PYTHONPATH=. ABLATION_WORK=<工作目录> M0_ENV_FILE=<私有 .env>` 在 worktree 根目录运行，还原的 messages 只在工作目录生成。

# 第二阶段：组合组、双盲评审复评、留出场景（2026-09-27 UTC 19:00–20:45）

授权：lead 2026-09-27 第二阶段指令（费用不设硬上限、模型 HTTP 本阶段 ≤250、产品代码不改）。本阶段模型调用：组 5 重放 20、两个留出 Run 各 4（产品路径）、留出重放 40，共 68 次；重放账本累计 160 条（`ledger.jsonl`）。余额 40.59 → 38.08 CNY，本阶段 **2.51 CNY**（`heldout/deepseek-balance-after-phase2.json`）。产品代码零改动（`git status opspilot/` 为空）；55431 PG 与 colima `m0-otel` 结束后均已停止，数据保留。

## 新评审方法

- 每个样本由 **2 位**全新上下文盲评者（Fable 5.1）独立判定，评分标准原文与第一阶段完全相同（`review-rubric.md`），样本随机匿名、Run 别名 R1–R6 随机；评审者不知组别与目的。分配脚本 `scripts/blind_pack2.py`（每人 10 个样本，每样本 2 个不同评审者，按负载最低分配）。
- 不一致（a 类计数、e 类计数或 verdict 任一不同）的样本交第 3 位裁决者：拿评分标准、两份评审（匿名为 A/B）、报告与视图，逐条核对后给出最终 a/e 列表与 verdict（`scripts/adjudicate_pack.py`，结果 `reviews2/adjudicator-*.jsonl` 含 `decision_notes`）。一致的样本取共同结果。汇总 `scripts/summarize2.py` → `summary2.json`。
- 复评范围：组 0、组 1、组 3 各 20 个已有样本（只复评，不重跑模型）+ 阶段 A 组 5 的 20 个 + 阶段 B 留出 40 个 = 120 样本、240 份评审、43 次裁决（评审者 1–24，裁决者 17–19、25–27）。
- **一致率**（120 样本）：a、e 计数完全一致 80/120（67%）；错误有无一致（a≥1 与 e≥1 的判断都相同）97/120（81%）；verdict 一致 114/120（95%）。分批看：6f 复评 80 样本完全一致 61（76%），留出 40 样本 19（48%）——留出报告数值更多，评审者对区间归属的分歧更大。

## 阶段 A：组 5 = 组 1 规则句 + 组 3 span_groups（6f 四个上下文，4 Run × 5 次）

新方法下 6f 各组（每组 20 样本 = fault 10 + normal 10；a / e 为裁决后计数）：

| 组 | a 类 | e 类 | 含 ≥1 个 a/e 的样本 | fault 窗含错 | normal 窗含错 | 校验通过 | 上游口径 | 平均 completion token |
|---|---|---|---|---|---|---|---|---|
| 0 基线（复评） | 23 | 25 | 18/20 | 9/10 | 9/10 | 19/20 | 17/20 | 8,826 |
| 1 规则句（复评） | 7 | 15 | 12/20 | 5/10 | 7/10 | 19/20 | 17/20 | 8,922 |
| 3 span_groups（复评） | 17 | 5 | 13/20 | 4/10 | 9/10 | 19/20 | 18/20 | 8,330 |
| **5 规则句 + span_groups** | **6** | **4** | **6/20** | **1/10** | 5/10 | 18/20 | 17/20 | 9,256 |

- 复评与第一阶段单评的次序一致（a 类：规则句降得最多；e 类：span_groups 降得最多），但绝对数不同（基线 a 25→23、e 20→25；组 3 a 10→17），说明第一阶段的单评计数含评审者尺度噪声。
- 组 5 把两类错误同时压到最低：fault 窗 10 个样本只有 1 个含错（1 个 e），normal 窗 5/10 含错（a 6 个全在 normal-1，e 3 个）。组 5 校验未通过 2 个（`g5-fault-1-r3`、`g5-normal-1-r4`，均为引用不存在的 evidence_id）。
- 上游口径「不正确」的 12 个（各组 2–3 个）仍全部是 normal-1 报告被记为 `other`：该窗确有 payment 的 GET/tcp.connect/dns.lookup 探针 ERROR span，报告如实写出并拒绝归因，评审者按标准原文「窗内无失败调用」判为 `other`。没有任何样本指认错误原因。

## 阶段 B：留出场景（冻结产品 f8fac31 真实跑 2 个新故障）

- 环境：`heldout/lab-up.log`（colima `m0-otel` + OTel Demo 2.0.2），web 与 worker 以 `OPSPILOT_TOOL_PROFILE=otel-demo` 启动，`worker.log` 首行 versions = `prompt-replay-candidate-177e529f603d` / `ctx-ctx-policy-v1-b4b4c61e0eec` / `otel-demo-44ed0d63b0d3`，与 6f 相同。问题文本、`target_id`、流程与 6f `run_case.sh` 完全一致（`scripts/run_heldout_case.sh`）。
- 故障钩子：`scripts/heldout_fault.py`（新写，参数化 flag；捕获原字节、拒绝重复注入、恢复前核对注入快照、逐条时间线 `heldout/fault-timeline.jsonl`；未改 `scripts/m0_environment/development_fault.py`）。flagd 文件前后 SHA256 往返一致（`1ee5c025…` → 注入 → 恢复 → `1ee5c025…`，两次）。
- `productCatalogFailure`（product-catalog GetProduct 对单一商品报错）：19:05:37Z 注入。该故障对 checkout 只是间歇性的（16 分钟内 23 条 checkout trace 只有 2 条失败，`observe-pc-16min` 见 `heldout/observe-pc-pre-attempt*.json`），因此用 30 秒轮询等到 300 秒窗内 ≥2 条「checkout 失败且 product-catalog 子 span 失败」的 trace 才提交（19:48:17Z，窗内 2/9）。Run `5e571762` 4 轮发布，窗 19:48:18–19:53:18，事后独立观察窗内 2/9 失败 trace 均为 product-catalog 子 span（`heldout/pc-fault/observe-post.json`）；报告定位 checkout PlaceOrder → product-catalog `GetProduct`（gRPC 13，"Product Catalog Fail Feature Flag Enabled"）。19:54:28Z 恢复，19:55–20:00 窗独立观察 0 失败（`observe-after-pc-restore.json`）。
- `cartFailure`（cart EmptyCart 连不上 valkey）：20:00:27Z 注入，20:02:18Z 窗内 5/7 失败即提交。Run `46bf55e1` 4 轮发布，窗 20:02:18–20:07:18，事后观察 6/7 失败 trace 均为 cart 子 span；报告定位 checkout → cart `EmptyCart`（gRPC 9，"Can't access cart storage … redis"）。20:09:11Z 恢复，20:10–20:15 窗观察 0 失败（`observe-after-cart-restore.json`）。
- 两个 Run 的最终轮请求用产品路径还原，`input_snapshot_hash` 与 `request_sha256` 均与记录一致（`heldout/rebuild-manifest.json`），随后重放组 0 与组 5 各 2 Run × 10 次。

| 组（留出，2 Run × 10） | a 类 | e 类 | 含 ≥1 个 a/e 的样本 | 含 a | 含 e | 校验通过 | 上游口径 | 平均 completion token |
|---|---|---|---|---|---|---|---|---|
| 0 基线 | 21 | 46 | 19/20 | 14 | 17 | 20/20 | 20/20 | 7,920 |
| 5 规则句 + span_groups | **4** | **20** | 13/20 | 4 | 12 | **14/20** | 20/20 | 9,181 |

按 Run：pc-fault 基线 a 13 / e 18 / 含错 9/10 → 组 5 a 1 / e 10 / 6/10；cart-fault 基线 a 8 / e 28 / 10/10 → 组 5 a 3 / e 10 / 7/10。40 个样本全部正确定位到对应依赖（product-catalog GetProduct / cart EmptyCart）。

组 5 在留出上校验未通过 6 个：3 个引用不存在的 evidence_id（长 id 抄漏一个字符，如 `91b291d3-a75-…`）、2 个 fact 引用 `no_data` 视图、1 个 JSON 语法错（12,809 字符）。基线 20/20 通过。这是组 5 输出更长（+16% completion token）、引用更多 id 带来的新失败模式，第一阶段 6f 上也有苗头（组 5 6f 2/20）。

## 结论（第二阶段）

- **最优组合是组 5**：在 6f 上含错样本 18/20 → 6/20，在未见过的两个故障上 19/20 → 13/20，a 类 21 → 4、e 类 46 → 20；上游口径在留出 40/40。效果不局限于 6f 四个上下文，但留出场景的 e 类仍剩一半——留出 Run 的视图数值更密（cart 故障有大量 EmptyCart 延迟与错误计数），span_groups 只覆盖 trace 视图，metrics 视图的数值复述仍靠模型。
- 组 5 的代价是校验通过率下降（留出 14/20）：更长的报告引用更多 evidence_id，抄错一个字符即被引用校验拒绝；这类错误确定性可检，但意味着产品侧若采用组 5，需要考虑 id 校验失败时的处理（如让模型只引用短别名），这是合同层取舍，留给用户。
- 双盲一致率 67%（完全）/ 81%（有无），裁决改变了 43/120 个样本的计数；组间次序在单评与双评下相同，绝对计数应以本阶段 `summary2.json` 为准。

## 局限（第二阶段）

- 留出只有 2 个故障场景各 1 个真实 Run（10 次重放共享同一上下文），Run 级样本量为 2；未跑留出的 normal 窗。
- `productCatalogFailure` 在 checkout 上是间歇故障，提交时机由 30 秒轮询「窗内 ≥2 条失败 trace」决定，与 6f 的 paymentFailure（100%）不同质；该 Run 窗内 checkout 失败率约 2/9。
- 评审者尺度差异仍在（留出 40 样本完全一致率 48%），裁决者也是模型；normal-1 的 `other` 判定为标准原文的字面适用，不是报告错误。
- 阶段 C（组 6 官方提示词）未做，等 lead 给原文。

## 目录（第二阶段新增）

`summary2.json`、`reviews2/`（reviewer-1…24、adjudicator-17…19、25…27 的 jsonl，`blind-mapping.json`、`adjudication.json`）、`samples/g5-*`、`samples/g0-{pc,cart}-fault-r*`、`samples/g5-{pc,cart}-fault-r*`、`heldout/`（`lab-up.log`、`lab-stop.log`、`web.log`、`worker.log`、`fault-timeline.jsonl`、`observe-*.json`、`rebuild-manifest.json`、`deepseek-balance-after-phase2.json`、`pc-fault/` 与 `cart-fault/` 各含 6f 同款 `request/intake/window/observe-pre/observe-post/sse/events/incident.html/ledger.json/report.json`）、`scripts/`（新增 `heldout_fault.py`、`run_heldout_case.sh`、`blind_pack2.py`、`adjudicate_pack.py`、`summarize2.py`；`replay.py` 新增组 5 与 250 上限；`rebuild_final_requests.py` 支持 `intake:<key>=<case>`）。

# 第三阶段（阶段 C）：官方式指令、隔离上下文提案、统一 Opus 复评（2026-09-27 UTC 20:52–21:35）

授权：lead 阶段 C 指令，模型 HTTP 本任务上限 50（实际组 7 用 40 次）；产品代码零改动（`git status opspilot/` 为空）；本阶段不需要 OTel/colima，55431 PG 沿用本 worktree `tmp/` 数据，未新增启动。DeepSeek 余额：组 6/6b 生成前 38.08 → 组 6/6b 后 35.30（`heldout/deepseek-balance-after-phase-c.json`，2.78 CNY／80 次）；组 7 前 35.26 → 后 31.63（`heldout/deepseek-balance-after-group7.json`，3.63 CNY／40 次）；阶段 C 合计约 6.45 CNY／120 次。评审与裁决不消耗 DeepSeek 额度（用 Agent 工具、`model: opus`，不经 DeepSeek）。

## 两个新组

- **组 6「官方式最终指令」**（`scripts/replay.py` 的 `OFFICIAL_INSTRUCTION`）：把运行级覆盖消息（`run_coverage_message`）移到最终报告指令**之前**，最终指令整条替换为一段结构化的要点式提示（禁止默认填充状态码、按视图给出的计数/区间转述而非重新计算、把缺失/采样/截断/空/非 ok 数据当作未知或缺口、引用完整 evidence_id），系统提示与其余消息不变。
- **组 6b**：组 6 + `span_groups.py`（与组 3 同一算法，按 service+operation 预算好行数/状态分布/duration 区间）。
- **组 7「隔离上下文提案」**（`scripts/group7_transform.py`，隔离上下文 Codex 提出，原样复制、未改一字）：不改最终指令原文，而是（a）在系统提示末尾追加一段状态判定纪律段落，（b）给每个像 span 表的 JSON 工具结果追加 `model_view_index`（按 service+operation 的可见行数、`status_state`/`status_tags` 计数、`error_by_visible_tags` 为真的行数、duration 区间，以及 `parent_is_visible=true` 的可见父子边），（c）在运行级覆盖消息末尾追加一段最终自检清单（spans_shown 与 trace 计数分开、同标签行才能合并统计、逐字核对 ID 计数、父子关系要求可见父引用、每条数值/状态结论都要标 evidence_id、保留 incomplete/truncated/no_data 视图为缺口）。三处都验证了要改的锚点存在才落地（系统消息、覆盖消息前缀 `"Run coverage summary, computed from the structured fields"`），无锚点会直接报错，未触发过。

组 6/6b 各在 6f 四个上下文（4 Run × 5 次）与两个留出上下文（2 Run × 10 次）各生成 20 个样本，共 80。组 7 同规模，共 40（`ledger.jsonl` 累计 280 条）。`validation.json` 已覆盖全部 280 个样本。

## 统一复评：组 0/5/6/6b/7，Opus，200 样本

用户决定这一轮复评模型从 Fable 换成 Opus，范围收窄为组 0、5、6、6b、7（不含组 1、3），6f 与两个留出上下文各 20 个样本、共 200。方法与阶段 B 相同（`scripts/blind_pack3.py`／`adjudicate_pack3.py`／`summarize3.py`，逻辑与 `blind_pack2.py`／`adjudicate_pack.py`／`summarize2.py` 一致，只换了目录名与模型）：

- 200 个样本随机打乱、匿名编号，6 个 Run 别名（R1–R6，覆盖 6f 四个 Run 与两个留出 Run）随机重新映射，与之前各阶段的映射不复用。
- 16 位全新上下文的 Opus 评审者（Agent 工具，`model: opus`），每人 25 个样本，每个样本 2 位独立评审；评审者只拿 `review-rubric.md` 原文、该 Run 的基线视图（不含 `span_groups`／`model_view_index`，避免泄露组别）与报告原文，不知组别与实验目的。
- 两位评审在 a 类计数、e 类计数或 verdict 任一不同即视为不一致：200 个样本中 50 个不一致，交 5 位全新上下文的 Opus 裁决者（每人 10 个，重新核对报告与视图后独立给出最终计数），150 个一致样本取共同结果。
- 之前 Fable 的评审结果**保留不删**：阶段 C 样本生成后，上一个 Agent 曾用 `blind_pack2.py` 起过一轮更大范围（组 0/1/3/5/6/6b，200 样本）的 Fable 复评，中断时只完成 8/16 个评审者（评审者 25–32，各 10 个样本），没有任何样本凑够两份评审，从未产出可用结果；原文保存在 `reviews2/phase-c-fable-partial-superseded/`（含说明），**不计入**本节任何数字。本节结论以 `reviews3/`／`summary3.json`（Opus 这套）为准。

评审一致率（200 样本，`summary3.json` `overall`）：a/e 计数完全一致 152/200（76%）；错误有无一致 172/200（86%）；verdict 一致 197/200（98.5%）。分场景看：6f 100 样本完全一致 79（79%）、有无一致 88（88%）、verdict 97（97%）；留出 100 样本完全一致 73（73%）、有无一致 84（84%）、verdict 100（100%）——与阶段 B 一致，留出场景数值更密，评审者对区间/计数归属分歧更大，但比阶段 B 的 Fable 复评（6f 76%／留出 48%）更一致。

## 结果：6f（4 上下文 × 5 次，n=20／组）

| 组 | a 类 | e 类 | 含 ≥1 个 a/e 的样本 | fault 窗含错（n=10） | 校验通过 | 上游口径 | 平均 completion token |
|---|---|---|---|---|---|---|---|
| 0 基线 | 24 | 24 | 19/20 | 9/10 | 19/20 | 19/20 | 8,825 |
| 5 规则句+span_groups | 7 | **5** | 8/20 | 2/10 | 18/20 | 20/20 | 9,255 |
| 6 官方式指令 | 19 | 17 | 18/20 | 8/10 | 15/20 | 20/20 | 8,050 |
| 6b 官方式指令+span_groups | 10 | 7 | 12/20 | 7/10 | 19/20 | 18/20 | 8,666 |
| 7 隔离上下文提案 | **3** | 11 | 12/20 | 5/10 | 17/20 | 19/20 | 11,585 |

## 结果：留出（2 上下文 × 10 次，n=20／组，全部为 fault 窗）

| 组 | a 类 | e 类 | 含 ≥1 个 a/e 的样本 | 校验通过 | 上游口径 | 平均 completion token |
|---|---|---|---|---|---|---|
| 0 基线 | 18 | 56 | 19/20 | 20/20 | 20/20 | 7,920 |
| 5 规则句+span_groups | **6** | 26 | 14/20 | 14/20 | 20/20 | 9,181 |
| 6 官方式指令 | 15 | 35 | 18/20 | 19/20 | 20/20 | 8,409 |
| 6b 官方式指令+span_groups | 13 | **11** | 16/20 | 17/20 | 20/20 | 8,436 |
| 7 隔离上下文提案 | 7 | 20 | 17/20 | 16/20 | 20/20 | 11,686 |

## 校验失败原因分布（`validation.json` 的 `reason`／citation `details`）

6f：组 0 = target_ref_not_authorized×1、target_ref_not_observed_by_view×1；组 5 = unknown_evidence_id×2；组 6 = REPORT_INVALID（JSON 解析错误）×1、target_ref_not_observed_by_view×2、unknown_evidence_id×2、fact_cites_non_ok_view×2；组 6b = REPORT_INVALID×1；组 7 = OUTPUT_LENGTH（触顶 16384）×2、REPORT_INVALID×1。

留出：组 0 = 无（20/20 通过）；组 5 = target_ref_not_observed_by_view×2、unknown_evidence_id×3、fact_cites_non_ok_view×2、REPORT_INVALID×1；组 6 = target_ref_not_observed_by_view×1、unknown_evidence_id×1；组 6b = unknown_evidence_id×1、REPORT_INVALID×2；组 7 = REPORT_INVALID×2、unknown_evidence_id×2、target_ref_not_observed_by_view×1。

`unknown_evidence_id`／`target_ref_not_observed_by_view` 是引用绑定错误（多为 evidence_id 抄漏字符，或引用了该 view 未观测到的 target）；`fact_cites_non_ok_view` 是 fact 类声明引用了 `no_data`／非 ok 视图；`REPORT_INVALID`（无 `details` 的通用原因）在这里都对应 JSON 解析失败（语法错或截断）；`OUTPUT_LENGTH` 是触顶 `max_tokens=16384`。

## 结论（阶段 C）

- 没有一个组在两个场景、两类错误上同时最优。**组 5（规则句+span_groups，阶段 A/B 的最优组合）在 a 类上仍是最稳的选择**：6f 第二低（7，仅次于组 7 的 3）、留出最低（6）；e 类上 6f 最低（5），留出第二低（26，次于组 6b 的 11）。
- **组 7（隔离上下文提案）把 6f 的 a 类压到全组最低（3，低于组 5 的 7）**，留出 a 类也接近最优（7，次于组 5 的 6）——只加计算辅助与自检清单、不改最终指令原文，对状态默认填充这类错误的抑制效果不弱于重写指令。代价是 e 类没有同步改善（6f 11、留出 20，均高于组 5），平均输出 token 比组 5 高约 25–28%（6f 11,585 vs 9,255；留出 11,686 vs 9,181），且是唯一在 6f 出现 `OUTPUT_LENGTH` 触顶的组。
- **组 6b（官方式指令+span_groups）在留出场景把 e 类压到全组最低（11，低于组 5 的 26）**，但 6f 上 e 类（7）和 a 类（10）都不如组 5；说明"重写指令+计算辅助"与"保留原指令、只加规则句+计算辅助"哪个更好，会随上下文的数值密度反转——留出场景（cart-fault 的 EmptyCart 延迟/错误计数、pc-fault 的 GetProduct 系列）数值远比 6f 密集，官方式指令的分组小节表述可能更利于对齐这类密集计数。
- **组 6（只重写指令、不加计算辅助）在两个场景都不如组 6b**，且 6f 校验通过率最低（15/20，4 类引用/解析错误都出现），说明单靠更长的结构化指令、没有计算辅助支撑时，模型更容易在引用绑定上出错。
- 上游口径（verdict 正确性）在两个场景所有组都是 18–20/20，与错误类别无关，和阶段 A/B 的结论一致（normal 窗的 `other` 保守判定拉低个别组 1–2 个）。
- 建议的下一步：组 5 或组 6b 与组 7 的计算辅助/自检清单叠加，分别在 6f 与留出两种数值密度下验证是否能同时压低 a 与 e；本阶段没有跑任何三者叠加的组合。

## 局限（阶段 C）

- 每组每场景仍是 20 个样本（6f 4 Run × 5、留出 2 Run × 10），单个 Run 的波动仍可能主导某一类计数（如阶段 A/B 已观察到的模式），本阶段未重新核对每组按 Run 拆分后的分布。
- 评审改用 Opus 后一致率提高（6f 79%／留出 73% vs 阶段 B Fable 的 76%／48%），但留出场景仍明显低于 6f，复评的绝对计数应以本节裁决后的 `summary3.json` 为准，不与阶段 A/B 的 Fable 计数直接相加比较。
- 组 7 的转换函数由隔离上下文 Codex 提出，本任务只做了「锚点存在性」校验（系统消息存在、覆盖消息前缀匹配）与人工抽查（`model_view_index` 结构、guard 文本插入位置），没有额外的正确性证明；`docs/evidence/m1-01-replay-ablation/scripts/group7_transform.py` 是其原文，未改一字。
- 中断前的 Fable 部分复评（`reviews2/phase-c-fable-partial-superseded/`）范围含组 1、3，与本阶段组范围不同，且从未凑齐任何样本的两份评审，不能与本节数字互相印证或合并。

## 目录（阶段 C 新增）

`summary3.json`、`reviews3/`（reviewer-1…16、adjudicator-17…21 的 jsonl，`blind-mapping.json`、`adjudication.json`）、`reviews2/phase-c-fable-partial-superseded/`（中断前 8 个 Fable 评审者原文 + `NOTE.md` + `blind2-mapping-snapshot.json`，保留不用于比较）、`samples/g6-*`、`samples/g6b-*`、`samples/g7-*`、`heldout/deepseek-balance-after-phase-c.json`、`heldout/deepseek-balance-after-group7.json`、`scripts/`（新增 `group7_transform.py`、`blind_pack3.py`、`adjudicate_pack3.py`、`summarize3.py`；`replay.py` 新增组 6/6b/7 与 290 上限）。

# 第四阶段（阶段 D）：覆盖式转述句、短引用别名、评审稳定性核验（2026-09-27 UTC 21:35–22:24）

授权：lead 阶段 D 指令，模型 HTTP 本任务上限 90（实际 81 次：组 8/9 各 40，另 1 次 `MODEL_UNAVAILABLE` 重试）。产品代码零改动（`git status opspilot/` 为空）。DeepSeek 余额：生成前 30.80 → 生成后 28.12（`heldout/deepseek-balance-before-phase-d.json`／`deepseek-balance-after-phase-d.json`，2.68 CNY／81 次）。评审与裁决仍用 Agent 工具（`model: opus`），不耗 DeepSeek 额度。

## 两个新组

- **组 8**：组 5（`RULE_SENTENCE` + `span_groups`，原文与算法不变）在同一条最终报告指令末尾、`RULE_SENTENCE` 之后再追加一句（`GROUP8_SENTENCE`，原文固定）："When summarizing multiple rows, use the counts, ranges, and grouped summaries provided by the view; do not recalculate them from individual rows."（`scripts/replay.py` 新增 `append_sentences`，一次性把多句拼到原始 `FINAL_REPORT_INSTRUCTION` 消息，避免组 9 需要三次追加时对"已改过的消息"做精确匹配失败）。
- **组 9**：组 8 + 短引用别名的确定性变换（`build_alias_map`／`apply_alias`／`dealias_text`）。按交付顺序给每个出现过的 `evidence_id`（取自各 `tool` 消息 JSON 的 `evidence_id` 字段，去重后编号）分配 `E1`、`E2`…；把整份序列化请求文本中每个完整 id 的逐字出现（不止工具视图本身的 `evidence_id` 字段，也包括覆盖消息里引用的 id，如"Views with incomplete true: <id>"）替换为别名（按长度降序替换，避免前缀互相覆盖）；在最终指令末尾再追加一句（`GROUP9_SENTENCE`，原文固定）："Cite evidence by its short id exactly as shown (for example E3)."。模型只看到别名，看不到完整 id。回复到手后先用 `\bE\d+\b` 正则把别名映射回完整 id（未登记的别名原样保留，交给下游校验判成 `unknown_evidence_id`，等价于模型编造了一个不存在的短 id）——`report.txt`（校验与评审都用这份）永远是映射回完整 id 后的文本；模型原始输出另存 `report.aliased.txt`；`meta.json` 记录 `alias_count` 与完整的 `alias_map`。
- 抽查（`normal-1` 基线请求）：组 9 的别名数 18，替换后序列化请求里找不到任何一个完整 id（零泄漏），18 个别名全部出现；覆盖消息里的 id 引用也被正确替换。40 个组 9 样本的 `report.txt` 逐一扫描，没有一个残留未映射的 `E<n>` token——模型全程只引用了实际存在的别名，没有编造。

组 8/9 各在 6f 四个上下文（4 Run × 5 次）与两个留出上下文（2 Run × 10 次）生成 20 个样本，共 80（`ledger.jsonl` 累计 361 条，含 1 条失败重试）。`validation.json` 覆盖全部成功样本（360）。`MAX_CALLS` 上调到 370。

## 统一复评：组 5/8/9，Opus，120 样本（含组 5 稳定性核验）

方法与阶段 C 完全相同（`scripts/blind_pack4.py`／`adjudicate_pack4.py`／`summarize4.py`，逻辑与阶段 C 的 `blind_pack3.py` 等一致，只换目录名）：120 个样本（组 5 已有的 40 个 + 组 8/9 各 40 个新样本）随机打乱、匿名编号，6 个 Run 别名重新随机映射（与阶段 C 的映射不复用）；10 位全新上下文的 Opus 评审者，每人 24 个样本、每个样本 2 位独立评审；120 个样本中 27 个不一致，交 3 位全新上下文的 Opus 裁决者（每人 9 个）。

**组 5 的 40 个样本是阶段 C 已经生成、已经验证过的原始产物，这一步只是重新匿名、重新分配给全新的 Opus 评审者复评一次**，用来检验"同一批报告、不同评审者、不同匿名编号"是否给出接近的计数——不是重跑模型。

评审一致率（120 样本，`summary4.json` `overall`）：a/e 计数完全一致 93/120（77.5%）；错误有无一致 108/120（90%）；verdict 一致 120/120（100%）——均高于阶段 C 的 200 样本轮（76%／86%／98.5%）。

**组 5 稳定性**：阶段 C（`summary3.json`）到阶段 D 重评（`summary4.json`）——6f：a 7→6、e 5→5；留出：a 6→6、e 26→24。两轮独立匿名、独立评审者、a 类几乎不变，e 类差 1–2，在阶段 C 已报告的评审噪声范围内（阶段 A/B 观察到的评审者尺度差异同量级），说明这套双盲+裁决流程对同一批报告的计数是稳定的。

## 结果：6f（4 上下文 × 5 次，n=20／组）

| 组 | a 类 | e 类 | 含 ≥1 个 a/e 的样本 | fault 窗含错（n=10） | 校验通过 | 上游口径 | 平均 completion token |
|---|---|---|---|---|---|---|---|
| 5 规则句+span_groups | 6 | **5** | **7/20** | **2/10** | 18/20 | 20/20 | 9,255 |
| 8 组5+覆盖式转述句 | 6 | 16 | 14/20 | 8/10 | 18/20 | 19/20 | 8,856 |
| 9 组8+短引用别名 | **5** | 9 | 9/20 | 5/10 | 17/20 | 20/20 | **7,145** |

## 结果：留出（2 上下文 × 10 次，n=20／组，全部为 fault 窗）

| 组 | a 类 | e 类 | 含 ≥1 个 a/e 的样本 | 校验通过 | 上游口径 | 平均 completion token |
|---|---|---|---|---|---|---|
| 5 规则句+span_groups | 6 | 24 | 14/20 | 14/20 | 20/20 | 9,181 |
| 8 组5+覆盖式转述句 | **5** | 18 | 16/20 | 18/20 | 20/20 | 9,178 |
| 9 组8+短引用别名 | 11 | **11** | 13/20 | 18/20 | 20/20 | **6,388** |

## 校验失败原因分布

6f：组 5 = `unknown_evidence_id`×2；组 8 = `REPORT_INVALID`（JSON 解析错误）×1、`OUTPUT_LENGTH`×1；组 9 = `REPORT_INVALID`×1、`fact_cites_non_ok_view`×2。

留出：组 5 = `REPORT_INVALID`×1、`fact_cites_non_ok_view`×2、`target_ref_not_observed_by_view`×2、`unknown_evidence_id`×3；组 8 = `unknown_evidence_id`×1、`OUTPUT_LENGTH`×1；组 9 = `target_ref_not_authorized`×1、`target_ref_not_observed_by_view`×1、`fact_cites_non_ok_view`×1。

**`unknown_evidence_id` 在组 9 两个场景都是 0**（组 5 两场景合计 5 次、组 8 合计 1 次），对应生成阶段的独立核验（40 个组 9 样本无一残留未映射的别名 token）——短别名结构性消除了"完整 UUID 抄漏一个字符"这类引用失败，但没有消除其他引用绑定失败（`fact_cites_non_ok_view`、`target_ref_not_authorized`、`target_ref_not_observed_by_view` 仍在组 9 出现）。

## 结论（阶段 D）

- **组 5 仍是两个场景里 a/e 双指标最均衡的基线**：6f 上 a/e 都是三组最低或次低（a=6 并列最低、e=5 最低），fault 窗含错比例（2/10）也最低；留出上 a 类第二低（6，仅比组 8 的 5 高 1）。
- **组 8 的追加句在两个场景效果相反**：6f 上 e 类不降反升（5→16，接近三倍），fault 窗含错从 2/10 升到 8/10；留出上 e 类反而下降（24→18）、含错样本略升（14→16 但校验通过率提高到 18/20）。样本量小（每格 n=10 或 20），但方向相反本身是数据支持的观察，不是噪声能完全解释的（6f 的变化幅度远超阶段 C 报告的评审噪声区间）。
- **组 9（短别名）把 6f 的 e 类从组 8 的 16 拉回到 9**（仍高于组 5 的 5），**留出场景 e 类进一步压到 11（三组最低）**，但留出 a 类升到 11（三组最高，高于组 5 的 6 和组 8 的 5）——短别名似乎减轻了组 8 引入的数值转述负担，代价是留出场景的状态默认填充类错误更多。
- **`unknown_evidence_id` 按预期在组 9 消失**（两场景均 0），验证短别名替换即使在 40 个真实样本上也没有让模型引用一个不存在的别名。
- **组 9 的输出 token 明显更短**：6f 7,145（比组 5 少 23%、比组 8 少 19%），留出 6,388（比组 5 少 30%、比组 8 少 30%）——用几个字符的别名替代 70+ 字符的复合 UUID 引用，直接压低了输出长度，这本身不是错误率指标，但是产品侧若采用该方案的一个直接收益（更低的 completion token 成本）。
- 上游口径在所有格子都是 19–20/20，与阶段 A/B/C 结论一致，不因组别系统性变化。
- **组 5 双轮评审结果高度一致**（见上），支持把阶段 C 与阶段 D 报告的绝对计数当作可比数字看待，而不只是同一轮内部的相对排序。

## 局限（阶段 D）

- 每组每场景仍是 20 个样本，组 8 在 6f 上的效应（e 类三倍）主要来自 fault-2 一个 Run（`e` 从组 5 的 1 升到组 8 的 7，按 Run 拆分未在本节展开）；结论中"方向相反"的判断基于组间对比，不代表单个 Run 外的普遍规律。
- 组 9 的别名替换只处理了 `evidence_id`；`target_id`、`time_scope_ref` 等其他引用字段未做同类处理，留出场景新增的 `target_ref_not_authorized`／`target_ref_not_observed_by_view` 失败与别名机制本身无关。
- 复评仍是每样本 2 位 Opus + 不一致裁决，一致率（77.5%／90%／100%）与阶段 C 同量级；组 5 的稳定性核验只做了一次重评，不是多轮重复实验。

## 目录（阶段 D 新增）

`summary4.json`、`reviews4/`（reviewer-1…10、adjudicator-11…13 的 jsonl，`blind-mapping.json`、`adjudication.json`）、`samples/g8-*`、`samples/g9-*`（`g9-*` 额外含 `report.aliased.txt`）、`heldout/deepseek-balance-before-phase-d.json`、`heldout/deepseek-balance-after-phase-d.json`、`scripts/`（新增 `blind_pack4.py`、`adjudicate_pack4.py`、`summarize4.py`；`replay.py` 新增组 8/9、`append_sentences`、别名三件套与 370 上限）。
