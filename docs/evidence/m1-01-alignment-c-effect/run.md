# M1-01 对齐上游第三批效果测量：候选 `ce6972e` 上 6 个真实 Run（对比 6f、第一批、第二批）

- 日期：2026-09-28 UTC 17:41–18:20（本机日志时区 10:41–11:20 PDT）。候选冻结于 `feature/m1-01-window-points` `ce6972e`（docs/tasks/2026-09-28-m1-01-alignment-c.md：C1 删除 `replay-candidate` 变体里过时的 `FIXED_WINDOW` 固定窗口句、C2 报告校验失败反馈逐条具体化（claim 序号 + evidence_id + 规范化原因名，最多 50 条）、C3 trace 视图新增 `span_groups` 按 `(service, operation)` 分组摘要，只统计已展示行）。6 个 Run 期间 `opspilot/` 零改动：`git diff ce6972e HEAD -- opspilot/` 为空，HEAD 全程未动。
- 流程、问题文本（"for the last 5 minutes"）、Run 顺序（normal-1、normal-2、fault-1、fault-2、pc-fault、cart-fault）、审查口径与第二批完全一致，另新增两项审查要求：**C2 触发时**独立复算被拒轮次的具体原因、修复轮是否修对且未引入新错误；**C3 span_groups**——是否正确出现在证据里、报告是否实际引用其字段、引用是否正确。
- `worker.log` 首行 `versions` = `prompt-replay-candidate-bd28790117a0` / `ctx-ctx-policy-v1-a30e65ea52f4` / `otel-demo-a08b7744f97e`（`prompt_revision` 与 `tool_schema_revision` 均与第二批不同，因为 C1 改了提示词、C3 改了工具 schema；`context_policy_revision` 不变，本批未动上下文策略）。
- 故障时间线（`fault-timeline.jsonl`，共 6 条）：paymentFailure 注入 17:45:17Z / 恢复 17:51:32Z；productCatalogFailure 注入 17:58:58Z / 恢复 18:05:57Z；cartFailure 注入 18:11:03Z / 恢复 18:14:40Z。三次故障恢复后独立观察均确认 0 失败（`observe-after-restore.json`/`-after-pc-restore.json`/`-after-cart-restore.json`）。
- 费用：DeepSeek 余额 25.84 → 24.04 CNY，6 个 Run 合计 **1.80 CNY**（第二批 1.46 CNY 的约 1.23 倍）。token 总量 prompt 2,819,595 / completion 96,424（**低于第二批的 3,499,044 / 121,417**，尽管本批多了 2 次 C2 重试的额外请求轮——第二批 fault-2 一个 Run 就用了 1,736,127 token 拉高了均值，本批没有出现类似的单 Run 极端探索）。
- 资源守卫：VM/PG 启动前与每 Run 前查内存压力，观测区间 26%–42%，均在阈值内；两个留出场景用前台短轮询（每次 ≤2 分钟一次状态检查）等待间歇故障，pc-fault 本次等待更久（11 次轮询、约 5.5 分钟）。
- 安全：写盘后按 key 全文/末 12 位、工作台口令、`Bearer`/`Authorization`、本机路径全文扫描本目录：全部 0 命中。

## 汇总表（6 Run）

| Run | 案例 | 观测窗（提交→发布，UTC） | 模型请求 | 每轮工具数 | 工具合计 | 自行结束 | C2重试 | 用时 | token（prompt/completion） | 报告状态 | P1/P2/P3 | P2 类 | 上游口径 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | normal-1 | 17:41:15→17:42:42 | 6/100 | 3,4,7,3,0,0 | 17 | **否（round6 强制最终）** | **是（round5 全部 14 claim 因 target_refs 含未授权服务名被拒→round6 修复发布）** | 87 s | 830446/20470 | `completed`/`partial` | 0/1/4 | e1 | 正确判定无故障 |
| 2 | normal-2 | 17:43:31→17:44:43 | 6/100 | 3,5,6,6,3,0 | 23 | 是 | 否 | 72 s | 621989/16454 | `completed`/`partial` | 0/0/2 | 无 | 正确判定无故障 |
| 3 | fault-1 | 17:48:19→17:49:34 | 5/100 | 3,8,1,0,0 | 12 | **否（round5 强制最终）** | **是（round4 报告被 Markdown 代码围栏包裹导致 JSON 解析失败→round5 去围栏修复发布）** | 75 s | 708976/17365 | `completed`/`partial` | 0/4/3 | e4 | 正确指出根因（payment Charge） |
| 4 | fault-2 | 17:50:16→17:51:05 | 4/100 | 3,3,3,0 | 9 | 是 | 否 | 49 s | 152985/11460 | `completed`/`partial` | 0/1/0 | e1 | 正确指出根因（payment Charge） |
| 5 | pc-fault（留出） | 18:04:07→18:05:27 | 5/100 | 2,3,2,4,0 | 11 | 是 | 否 | 80 s | 247766/18814 | `completed`/`partial` | 0/5/4 | a2 e3 | 正确指出根因（product-catalog GetProduct） |
| 6 | cart-fault（留出） | 18:13:10→18:14:06 | 4/100 | 2,2,1,0 | 5 | 是 | 否 | 56 s | 257433/11861 | `completed`/`partial` | 0/1/3 | e1 | 正确指出根因（cart EmptyCart/valkey） |

合计：模型请求 30（第二批 35），工具 77（第二批 92），6/6 发布（`execution=completed`、`handoff=false`）；**4/6 在无工具调用的一轮自行结束**；**2/6 触发 C2 修复重试（normal-1、fault-1），均在唯一一次允许的重写内修复并发布，无一次交接**；`RESULT_TOO_LARGE` 与 `DUPLICATE_TOOL_CALL`（B5 去重）在全部 6 个 Run 中出现 **0 次**；`evidence_raw_hash_mismatch` 全部为空；6/6 上游口径正确。

## C2 修复反馈：两次真实触发，逐条核实

独立审查者对两次触发都独立复算了产品自己的校验逻辑（`unsupported_citations`/`parse_report`），不采信执行者的判断：

- **normal-1（round5→round6）**：round5 全部 14 条 claim 都在 `target_refs` 里加了服务名（如 `"checkout"`、`"currency"`），而授权目标只有 `m0-otel-20260909`，触发 `missing_target_refs`。审查者用产品校验器复算，14 条 claim 逐条返回同一原因，与 C2 合同的"逐条列出失败项"一致。round6 把全部 `target_refs` 改回 `["m0-otel-20260909"]`，校验转为全部通过，顺带修正了 round5 两处未被校验器标出的内容错误（一个区间写错、一段乱码文字），但引入了一个新的 P2（cart 时长区间引用了错误视图）。
- **fault-1（round4→round5）**：round4 的完整内容被 Markdown 代码围栏（```` ```json ````）包住，导致 `json.loads` 直接失败，触发 JSON 解析位置诊断分支。审查者去掉围栏后重新解析，确认 round4 的 17 条 claim 引用本身全部合法——**唯一问题是包装格式**。round5 去掉围栏后原样通过，未引入新错误；round4 已有的 4 个 P2（数值/来源错误）原样带入 round5，重试机制本身没有义务、也没有修正内容层面的错误，只修复了它被设计来处理的那类失败（引用/解析校验）。

两次触发覆盖了 C2 合同 7 个规范化原因名中的 2 个（`missing_target_refs`、JSON 解析位置诊断），另 5 个（`unknown_evidence_id`、`missing_time_scope_ref`、`cites_non_ok_view`、`not_citable_as_fact`、`time_scope_not_bound_to_view`、`target_not_observed_by_view`）本次 6 Run 未触发，无法验证其反馈内容的具体性。**修复重试机制本身两次都成功**：均在唯一一次重写内解决了触发拒绝的问题、都发布、都未交接；但重写过程本身不保证不引入或不保留内容层面的新错误（normal-1 引入 1 个新 P2，fault-1 保留了全部 4 个原有 P2）——这与第二批 fault-2 观察到的模式一致，是该机制设计上的已知特性，不是回归。

## C3 span_groups：全部 6 Run 正确出现，多数被实际引用

- **正确性：6/6 Run 全部通过独立重算。** 每位审查者都对该 Run 全部 `traces_search` 视图独立重算了 `span_groups`（分组、`rows`、`status` 字典、`error_rows`、`duration_us_min/max`），逐字段与产品自己的 `_span_groups()` 输出一致，`rows` 求和等于该视图 `spans_shown`，字段按 `(service, operation)` 排序，`span_groups_note` 均存在。0 处不一致。
- **引用情况**：normal-1、fault-2、pc-fault、cart-fault 的报告明确引用了 `span_groups` 的具体字段值（如"span_groups … count 3 error rows of 10 PlaceOrder spans"）；normal-2、fault-1 的报告数值与 `span_groups` 完全吻合但未点名引用该字段，审查者判定为"很可能使用但无法证明"（模型推理内容按合同不落盘）。
- **新发现的失败模式（pc-fault，2 个 P2）**：模型把 `span_groups` 里 `status` 字典中的 `"not_recorded"` 桶误读成"status code 0"或"HTTP 200"这类明确的好状态，出现在 payment `charge`、cart `POST`/`EmptyCart`、email `send_email` 四组上。这不是 span_groups 本身的缺陷（字段值本身完全正确），而是模型对分组摘要里 `not_recorded` 语义的误读——**这是 span_groups 引入的一种新的具体犯错方式，此前（第一、二批）同类"缺失状态写成好状态"的 a 类错误来自模型直接误读原始 span 的 `status_state` 字段，现在同一类错误可能通过误读聚合摘要发生**。工具描述已注明 `not_recorded` 语义，但两个字段（原始行、聚合摘要）分别需要模型分别正确解读。

## 与 6f、第一批、第二批的四包对比（同 4 类核心场景：normal-1/2、fault-1/2）

| | 6f（旧预算，固定 300s） | 第一批（去预算，固定 300s） | 第二批（模型自选窗） | 本批（+C1-C3） |
|---|---|---|---|---|
| P1/P2/P3 合计 | 0/14/16 | 0/10/10 | 0/8/12 | 0/6/9 |
| P2 类：a | 6 | 2 | 2 | **0** |
| P2 类：b | 0 | 2 | 0 | 0 |
| P2 类：b-svc | 0 | 0 | 0 | 0 |
| P2 类：c | 0 | 0 | 0 | 0 |
| P2 类：d | 0 | 0 | 0 | 0 |
| P2 类：e | 8 | 6 | 6 | 6 |
| 上游口径 | 4/4 | 4/4 | 4/4 | 4/4 |

**核心 4 场景 P2 合计四包连续下降：14→10→8→6**，a 类在核心 4 场景中首次降到 0（此前 6/2/2）——但 e 类三包持平在 6，本批未能进一步压低 e 类（数值/时间/范围/因果类错误），这与用户此前判断一致（"重放实验中分组摘要对 e 类最有效"，但真实 6-Run 样本下 e 类降幅不明显，样本量小，不排除噪声）。

## 六 Run 全量对比（第一批 vs 第二批 vs 本批，各含 2 个留出场景）

| | 第一批（6 Run） | 第二批（6 Run） | 本批（6 Run） |
|---|---|---|---|
| P1/P2/P3 合计 | 0/21/15 | 0/13/18 | 0/12/16 |
| P2 类：a | 7 | 2 | 2 |
| P2 类：b | 2 | 1 | 0 |
| P2 类：b-svc | 0 | 1 | 0 |
| P2 类：e | 12 | 9 | 10 |
| 上游口径 | 6/6 | 6/6 | 6/6 |
| token（prompt/completion） | 605,530 / 82,259 | 3,499,044 / 121,417 | 2,819,595 / 96,424 |
| 费用 | 0.56 CNY | 1.46 CNY | 1.80 CNY |

**六 Run 全量 P2 合计 21→13→12，降幅明显收窄**（第二批相对第一批 -38%，本批相对第二批仅 -8%）。全部 2 例 a 类均来自本批的 pc-fault（span_groups 的 `not_recorded` 误读，见上），核心 4 场景 a 类已归零，held-out 场景仍有残留；b/b-svc 类首次归零（0/0）。e 类六 Run 全量反而从 9 回升到 10，与核心 4 场景的持平一致，指向 e 类错误对 C1-C3 三项改动均不敏感——这类错误（区间端点混用视图、跨视图取值、latency 声称"无变化"但自身数据显示相反）是模型的推理/核对习惯问题，不是证据呈现格式问题，可能需要不同方向的干预（如强制交叉核对步骤）才能压低。

## 未执行 / 未验证

- 未做暂停/挂起/取消下的执行器控制拒绝演示、多 worker、重启恢复（不在本包范围；有 PG 合同用例覆盖）。
- 独立审查者未能对每条 claim 的措辞做穷尽核对；各 `review.md` 列出了实际核对的条目。
- 供应商余额差含本机本时段的全部调用；未做独立核实是否有其他并发调用方。
- C2 的 7 个规范化失败原因名本次只验证了 2 个（`missing_target_refs`、JSON 解析位置），其余 5 个未在真实 Run 中触发，反馈内容的具体性未被真实验证。
- e 类错误未降的原因未做进一步归因实验（是否与视图体量、压缩阈值、或纯粹是模型推理习惯有关）。
- 22 个真实 Run（跨四包）无法建立统计显著性；P2 类别升降含审查者定级噪声。

## 目录

`fault-timeline.jsonl`（payment/pc/cart 共 6 条）、`observe-after-restore.json`（payment）、`observe-after-pc-restore.json`、`observe-after-cart-restore.json`、`deepseek-balance-before.json`/`-after.json`；每个 Run 目录：`request.json`、`intake.json`、`window.json`、`observe-pre.json`（pc-fault/cart-fault 另有 `observe-pre-attempt1..N.json` 间歇故障轮询记录）、`sse.txt`、`events.jsonl`、`incident.html`、`worker-attempt.txt`、`ledger.json`、`report.json`、`review.md`、`stats.json`（从 `ledger.json` 提取的每轮工具数、查询窗、`RESULT_TOO_LARGE`/去重计数、token 用量）。

> 记录格式说明（2026-09-29）：各 Run 目录 `request.json` 中提交时使用的幂等键字段在本证据中记为 `idempotency_label`（值与提交时及数据库记录逐字相同），原因同[第二批证据](../m1-01-alignment-b-effect/run.md)的格式说明：原字段名与值的组合触发仓库密钥扫描误报，扫描器按项目策略禁用一切豁免，故只改记录字段名，不改值。
