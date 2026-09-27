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
