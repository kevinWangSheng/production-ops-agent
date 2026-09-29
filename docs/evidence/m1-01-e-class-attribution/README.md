# M1-01 e 类报告错误逐条归因（第三批全部 e 类 + 第二批、6f 核心 Run）

- 日期：2026-09-29。基线：`main` f0038dd，`opspilot/` 零改动（`git diff origin/main -- opspilot` 为空）。
- 问题：剩余 P2 几乎全是 e 类（数值区间、计数、来源写错）。每一处：模型写了什么、正确值是什么、模型输入里有没有、归哪一类、有没有因果证据。
- 范围：第三批核心 4 Run 的 6 处（逐条深查 + 重放）；第三批留出 2 Run 的 4 处、第二批核心 4 Run 的 6 处、6f 的 8 处（分类为主，见「证据强度」）。共 24 处。
- 只做归因，不改产品代码。修复方向见文末与任务记录，交用户决定。

## 一、能不能重建模型当时的输入（ledger.json 是否足够）

**结论：足够重建，唯一缺口是历史轮次的 `reasoning_content`（导出时替换为占位符，见 `ledger.json` steps[].response.assistant.reasoning_content = "[reasoning_content withheld: private protocol field, C3 §12]"）。**

- lab PG 数据目录已被误删，所以写了离线重建器 `scripts/rebuild_offline.py`：把 `ledger.json` 的 `runs / steps / inputs / incident` 转成 `DurableStore.rebuild` 同形的快照，调产品自己的 `rebuild_transcript`、`FINAL_REPORT_INSTRUCTION`、`run_coverage_message`、`report_retry_feedback`，不需要 PG，不占 lab 锁。第 1 轮输入（系统提示 + 任务）的 `input_snapshot_hash` 与记录一致（重建通过 `_check_snapshot_hash` 的第 1 步），证明起点字节一致。
- 第 2 轮起的哈希无法复算（哈希覆盖被占位的 reasoning_content），重建器对该检查做了打桩，只在脚本内，产品代码未动。
- 用 DeepSeek 返回的 `prompt_tokens` 做替代核对（`rebuild-manifest.json`、`ledger.jsonl`）：

| 请求形态 | 重放 prompt_tokens | 原始记录 | 差 | 说明 |
|---|---|---|---|---|
| normal-1 第 6 轮（强制最终，无工具） | 251,805 | 251,805 | 0 | 无工具的请求，API 不计历史 reasoning |
| fault-1 第 5 轮（强制最终，无工具） | 225,015 | 225,015 | 0 | 同上；含重建的 C2 反馈文本 |
| fault-1 第 4 轮（带工具，被拒的首次报告） | 221,114 | 222,868 | −1,754（丢失 reasoning 1,799） | 加回后差 +45 |
| normal-1 第 5 轮（带工具，被拒的首次报告） | 247,063 | 249,437 | −2,374（丢失 2,434） | 加回后差 +60 |
| fault-2 第 4 轮（带工具，自行结束） | 72,584 | 74,703 | −2,119（丢失 2,164） | 加回后差 +45 |
| normal-2 第 6 轮（带工具，自行结束） | 158,827 | 163,022 | −4,195（丢失 4,270） | 加回后差 +75 |

- 含义：**强制最终请求（2/2）token 数逐位相同；带工具请求补回丢失 reasoning 后的余差（+45/+60/+45/+75）恰为占位符本身的 token 数（15 × 占位符消息数 3/4/3/5）**，即余差来自重放里多出的占位符文本，没有其他字节差异的迹象；也没有逐字节证明（哈希不可复算）：这是「等价重建」，不是「字节级还原」。
- **带工具形态的重放里，模型看不到早期推理，而原 Run 的模型看得到**，所以这些形态的首次生成比率（E1、E2、E5 等）只是缺 reasoning 条件下的近似，不能当作原 Run 的真实比率；强制最终形态（请求逐位相同）的结论不受影响。
- 已知偏差：带工具的请求在重放里常选择继续调工具而不是写报告（fault-2 9/16、normal-2 6/8、fault-1@3 3/16），原始 Run 在同一位置选择了写报告。缺失的 reasoning 是最可能的原因（未验证）。这些样本不计入错误率分母。
- 因为强制最终请求的历史里有模型自己被拒的上一份回复，重放时必须同时重放「首次生成」那一轮（`fault-1@3`、`normal-1@4`：被拒轮的请求）和「重试」那一轮，两者结论不同（见三）。

## 二、逐条归因表（第三批核心 4 Run，6 处，逐条核查）

视图简写：`c457-t0` = `c457377c-…-t0`，其余同；`t0..tN` 是该 Run 内该批次工具调用的序号，全名见各 `ledger.json`。

| # | Run / 位置 | 模型写的原文 | 正确值 | 正确值在输入里的位置 | 归类 | 证据强度 |
|---|---|---|---|---|---|---|
| E1 | normal-1 第 6 轮（C2 重试）claim 12，引 `a6f7-t4` | "cart spans … 73-27,542 microseconds" | 73–18,908（`a6f7-t4` span_groups：cart 各分组 max 最大 18,908，min 最小 73） | 在被引视图里，`span_groups` 与原行都有。27,542 是同一个 cart `EmptyCart` 在别的视图里的 max：`3c32-t2`、`a6f7-t1/t2/t5/t6` 都含 27542，`t4` 不含 | **视图重叠**：同一批 trace 的 span 出现在多个 traces_search 视图，各视图截断不同。第 5 轮 claim 9 引用 t3–t6 四个视图，并集 max 确为 27,542；第 6 轮拆成一视图一条 claim 后沿用了并集区间 | 中：值位置逐一核对；重放只复现 1/8（首次生成 1/8，重试 0/8），单次事件 |
| E2 | fault-1 第 4→5 轮 claim 9，引 `c457-t0` | "failing frontend 'POST /api/checkout' spans 30261 to 94302" | 30624–94302 | `c457-t0` span_groups：frontend / `POST /api/checkout`，`duration_us_min=30624`。30261 是同一视图里相邻分组 `executing api route (pages) /api/checkout` 的 min | **读对了却写错**：正确值以模型最容易用的形态（分组摘要）就在被引视图里；写成了邻组的 min | 中：首次生成重放 0/13（不复现，缺 reasoning），重试重放 8/8 保留该错误（3/8 逐字相同，其余改写后仍含错值）。首次生成重放不复现，是否随机失误未确定 |
| E3 | 同上 claim 9 | "successful frontend 'POST /api/checkout' spans 128935 and 148885" | 这两个值本身正确（frontend POST /api/checkout，http 200，trace a15e6f57 / f37767a4） | 不在被引 `c457-t0`（278 行只展示 134 行，这两行被截掉）；在 `fac8-t0/t3/t4/t5` | **视图重叠**（值是真的，来源挂错视图）；与 E1、E4 同一机制 | 强：确定性数值溯源检查命中；首次生成重放 1/13，重试 8/8 保留 |
| E4 | fault-1 claim 10，引 `c457-t0`、`c457-t1` | "payment Charge calls returned rpc.grpc.status_code=0 at 9662 and 5386 microseconds" | 值正确（payment Charge，trace a15e6f57 / f37767a4） | `c457-t0` 只含 payment Charge 的 6 条 ERROR 行；成功行在 `fac8-t0/t3/t4/t5` | **视图重叠**，同 E3 | 强：同上；首次生成 1/13，重试 8/8 保留 |
| E5 | fault-1 claim 10 | "checkout STATUS_CODE_UNSET metric value (67.5) exceeds the ERROR value (9.94)" 用来论证 "failures are not total" | 数值本身对；推论错：UNSET 计的是 checkout 所有 span（GetCart、Convert、prepareOrderItems…），不是成功的 PlaceOrder 数 | `c457-t1` 有这两个数，但视图没有任何字段说明 `traces_span_metrics_calls_total` 数的是「各操作的 span」，模型自选的 `sum by (service_name, status_code)` 又抹掉了 span_name | **视图语义有歧义**：view 与工具描述都没有说明该序列数什么、UNSET 是什么 | 强：加一句系列说明后 5/20 → 0/18（p=0.048，见四） |
| E6 | fault-2 claim 4、11 | "payment … STATUS_CODE_UNSET increase 10.00 alongside its 8.33 ERROR … part of the observed span volume is non-error … not a complete failure rate" | 同 E5；且 10.0 = 内部 `charge` 8.75 + flagd `ResolveFloat` 1.25，Charge 自身 UNSET 为 0 | 已交付：`47d9-t2`（按 span_name 拆的 payment 序列）逐项给出；`a9e0-t1` 给出 PlaceOrder code 0 = 0 | **视图语义有歧义**（同 E5）。这里反例数据已在输入里，仍写错，说明没有语义说明时模型不会自己去交叉核对 | 中：同一机制的重放 0/7（有说明）对 2/7（无说明），样本小 |

第三批核心 4 Run 小结：**3 处是跨视图归属（E1、E3、E4，值真实、来源不全）；2 处是指标语义（E5、E6）；1 处是邻组误读（E2）；0 处是「正确值根本不在输入里」。**

## 三、重放实验

统一设置：DeepSeek `deepseek-flash`（与真实 Run 同模型），请求为 `rebuild_offline.py` 重建的请求，其余参数与原始一致。样本报告原文在 `samples/<变体>-<形态>-r<n>/report.txt`（不含 reasoning_content）；判定脚本 `scripts/check_samples.py`（确定性谓词）、`scripts/provenance.py`（数值溯源）、`scripts/summarize_eclass.py`（汇总，见 `summary.json`）。

### 3.1 原输入重放：错误是否复现（`base`）

| 形态 | 样本 / 出报告 / 改调工具 | E1 | E2 | E3 | E4 | E5·E6 类（UNSET 当成功数用，人工判读） | 含跨视图数字的报告 |
|---|---|---|---|---|---|---|---|
| normal-1 第 6 轮（重试） | 8 / 8 / 0 | 0/8 | | | | | 0/8 |
| normal-1 第 5 轮（首次，被拒轮） | 8 / 8 / 0 | 1/8 | | | | | 1/8 |
| fault-1 第 5 轮（重试） | 8 / 8 / 0 | | **8/8** | **8/8** | **8/8** | **8/8** | 8/8 |
| fault-1 第 4 轮（首次，被拒轮） | 16 / 13 / 3 | | 0/13 | 1/13 | 1/13 | 3/13 | 5/13 |
| fault-2 第 4 轮（自行结束） | 16 / 7 / 9 | | | | | 2/7 | 1/7 |
| normal-2 第 6 轮（自行结束，对照，原 Run 无 e） | 8 / 2 / 6 | | | | | | 1/2 |

读法：
- **重试保留错误**：fault-1 重试 8/8 保留了 E2–E5 的错误数字与 evidence_id（判定按数字与引用，文本部分被改写：含 30261 的 claim 只有 3/8 与原文逐字相同）。被拒的首次回复就在上下文里，C2 反馈只说了 "JSON parse error … char 0"，没有指出是 Markdown 围栏（`retry-feedback/fault-1.txt`）。在（缺 reasoning 的）首次生成重放里，同样四处错误每个只有 0–1/13。所以 fault-1 的 4 个 P2 更像**一次首次生成事件**，被重试保留；首次生成的真实比率未确定（见一）。
- 重试也可能**新引入**错误：normal-1 的 E1 是第 6 轮拆分 claim 时新出现的；第二批 fault-2 的 P2-1、P2-3 同为第 9 轮（重试）新增。
- 首次生成里，「有至少一个数字在别的视图、不在被引视图」的报告：fault-1 首次 5/13、fault-2 1/7、normal-2 1/2、normal-1 首次 1/8，合计 8/30（27%）。这是检查器命中数（含个别误报），审查者只抓到其中一部分。
- E2 首次生成 0/13、E1 首次 1/8、重试 0/8（均缺 reasoning）：这两处在重放里几乎不复现，**是否随机失误未确定**，无法做因果检验，不据此提修复。

### 3.2 最小上下文改动：给 `traces_span_metrics_calls_total` 视图加一句系列说明（`metric_note`）

只改一处：`metrics_range_query` 的视图里，PromQL 含 `traces_span_metrics_calls_total` 的，增加字段 `series_note`："counts spans of every operation of the service (internal, client and server spans alike), summed over the labels kept in this query; it is not a count of requests. status_code STATUS_CODE_UNSET means the span carried no status; it does not mean the call succeeded."（事实性说明，非规则；`scripts/replay_eclass.py` 的 `SERIES_NOTE`）。其余字节相同，各 16 次，形态为 fault-1 第 4 轮（首次生成，避开重试复制）与 fault-2 第 4 轮。

| 形态 | 变体 | 出报告 | UNSET 当成功数用 | 含跨视图数字 |
|---|---|---|---|---|
| fault-1 第 4 轮 | base | 13 | 3/13 | 5/13 |
| fault-1 第 4 轮 | metric_note | 11 | **0/11** | 3/11 |
| fault-2 第 4 轮 | base | 7 | 2/7 | 1/7 |
| fault-2 第 4 轮 | metric_note | 7 | **0/7** | 1/7 |
| 合计 | base → metric_note | 20 → 18 | **5/20 → 0/18**（Fisher 双侧 p=0.048） | 6/20 → 4/18（无差别） |

- 判定为人工判读（我读的，不是盲评）：命中的 5 条原文、以及 48 条含 UNSET 的对照文本在 `unset-adjudication.json`（已按样本 id 打乱、变体另存 `key`），可让独立审查者盲评复核。
- 有说明后，模型多次在报告里复述该说明（"not request counts; UNSET does not mean success"）；没有出现新的错误类型；跨视图数字率不变，说明该改动只影响语义类，不影响归属类。
- 局限：p=0.048 边缘；每个形态单独看不显著（3/13 对 0/11，p=0.22；2/7 对 0/7，p=0.46）；只有一种措辞；非盲评。

### 3.3 确定性数值溯源 + 修复反馈（`replay_repair.py`）

设想的检查：报告每条 claim 文字里的数字，必须能在该 claim 引用的视图里找到。脚本 `scripts/provenance.py`（≥4 位整数或 ≥2 位小数才检查，去掉 id 与时间戳，小数按写出的位数取整比较）。

- 真实 Run 扫描（`provenance-scan.json`，22 个已审查 Run 的已发布报告）：命中 E1、E3、E4，共 3 处（24 处 e 类错误中的 3 处）；E2、E5、E6 与第二批、6f 的区间混用类不会命中，因为那些数字都在被引视图里。另有 11 条 claim 命中（共 14 条，分布在 9 个 Run）：多为单位换算（µs→ms）、由直方图求出的分位数、四舍五入（误报）；其中第一批 fault-2 的 11.25 与第二批 fault-2 的 3400/2500/5000 数字出现在别的视图，审查者未列为 P2，未核实是否真错。
- 若强制最终请求也按 C2 的模板告诉模型「哪条 claim 的哪些数字不在被引视图里、在哪些已交付视图里」（`feedback()`，模板沿用 `REPORT_RETRY_TEMPLATE`，reason 为 `numbers_not_in_cited_views`）：对 20 个含跨视图数字的样本（12 个首次生成 + 8 个 fault-1 重试样本）各重放 1 次，**19/20 修复后跨视图数字全部通过溯源**（数字被删除，或补引了含该数字的视图），1/20 未修好并新增 1 个数字（`score-repair.json`）。fault-1 的 8 个里，E3、E4 全部消除；E5 仍 8/8 保留，E2 仍 3/8 保留：**该检查只能修跨视图归属，修不了语义与邻组误读。**
- 未验证：修复后被保留下来的数字是否真的属于那条 claim 的对象（如 E2，值在被引视图里但归错分组，溯源检查看不出）。

## 四、其余批次的归类（较弱证据：审查原文 + 抽查 ledger）

| 批次 / Run | 条目（审查原文） | 归类 | 依据 |
|---|---|---|---|
| 第二批 fault-1 F2 | Convert/GetCart/GetProduct 区间把 client 与 server span 混在一起 | 上下文有但形态难用 | 值全在 `5654-t0`、`3e92-t3` 的原始行里（已核对 1435/38044/923/22371/3623 等），当时视图无分组摘要 |
| 第二批 fault-1 F3 | 摘要 "PlaceOrder server 127–348 ms" 用了 frontend client span 的最小值 | 同上（形态） | 同视图原始行 |
| 第二批 fault-2 P2-1 | UNSET 51.35 对 ERROR 2.65 论证 "not total" | 视图语义（同 E5） | 审查者列出按 span_name 拆分的视图 `f6a7-t0` 反证 |
| 第二批 fault-2 P2-2 | 把 13:00–13:05 说成"紧邻可比窗"，漏掉自己的 13:05–13:10 视图（流量为 0） | 其他：已交付的视图没用上 | 审查原文；未复算 |
| 第二批 fault-2 P2-3 | 第 9 轮把 product-catalog "sub-10 ms" 的引用从 t2 改成 t0/t1（t0/t1 无该服务 span） | 视图重叠 / 引用挂错（重试新增） | 审查原文 |
| 第二批 fault-2 P2-4 | 被引基线视图的正则更窄，结论只被未引的 t2 支持 | 引用挂错（重试保留） | 审查原文 |
| 6f normal-1 ×2 | "four error-tagged spans" 却列了五个；摘要区间比自己的 claim 窄 | 读对了却写错（计数、摘要与 claim 不一致） | 审查原文；正确值在视图里 |
| 6f normal-2 ×2、fault-1 ×2、fault-2 ×1 | min–max 混入父/子或不同 operation 的 span | 上下文有但形态难用 | 审查原文；C3 `span_groups` 就是针对这类；替换前置实验见 `m1-01-replay-ablation`：只加 `span_groups`，e 类 20→5 |
| 6f fault-1 P2-3 | p95 值来自未被引用的视图 | 视图重叠 / 引用挂错 | 审查原文 |
| 第三批 pc-fault ×3 | 多点时间序列被写成"平坦"（实际有 220 ms、7.25 ms 尖点）×2；claim 引用的是 ok 视图，内容却是 no_data 视图 | 形态（要扫描多点序列）；引用挂错 | 审查原文 |
| 第三批 cart-fault ×1 | 把"缺失的 ERROR 序列 + UNSET 序列"写成 cart 埋点缺陷 | 视图语义（UNSET/缺失序列） | 审查原文 |

按类合计（24 处，含上表与第二节）：视图重叠/引用挂错 7；视图语义（UNSET 与缺失序列）4；上下文有但形态难用 9；读对了却写错 3；其他 1。**上下文缺失：第三批核心 6 处已逐条核对，0；其余 18 处依审查原文，未见缺失，未复算。**第二批、6f 的「形态难用」在第三批降到 pc-fault 的多点序列 2 处，`span_groups` 类型的区间混用降到 E2 一处（邻组），与 C3 的设计方向一致；但第三批核心 4 Run 的总数没降（6），因为归属类、语义类原本就在，且重试路径把它们保留下来。

## 五、对照上游 HolmesGPT（a045ec7，2026-09-28）

上游没有「逐条 claim 挂 evidence_id」的合同，最终回答是自由文本，因此没有与 E1/E3/E4（来源挂错）对应的错误类别；它在数值转述上的处理是**靠提示词与查询下推**，没有确定性数值校验：

- 提示词：`holmes/plugins/prompts/generic_ask.jinja2:42` 最终作答前自检"trace each claim to specific tool output and hedge anything unverified"（只在 TodoWrite 段启用）；`holmes/plugins/toolsets/prometheus/prometheus_instructions.jinja2:35-36`"NEVER answer based on truncated Prometheus data … Do not answer about metric values you haven't seen"；`:22` 延迟优先用 `rate(_sum)/rate(_count)` 而不是 bucket。
- 大结果的处理（对所有工具生效）：`holmes/core/tools_utils/tool_context_window_limiter.py:33` `spill_oversized_tool_result`：超过单工具 token 上限时，完整结果落盘，模型拿到文件指针加一段预览并可用 `cat`/`jq` 读全量（:76-86 附近的存储分支）；存储不可用时丢弃数据并返回错误，要求收窄查询（:131-140 附近）。Prometheus 另有 `holmes/plugins/toolsets/prometheus/prometheus.py:767`（`create_data_summary_for_large_result`）与 `:1676-1696`、`:1936-1951`，超限返回汇总而不是残缺数据。本项目的 traces_search 视图是「展示部分行 + 省略计数」，各服务的搜索各自截断且模型读不到全量，正是 E1/E3/E4 的来源；上游没有这种「多个各自截断的部分视图」。
- 聚合交给后端：`holmes/plugins/toolsets/grafana/toolset_grafana_tempo.jinja2:147-203` 指示用 TraceQL metrics（`rate()`、`quantile_over_time`、`by (...)`）让后端算分组的数量与分位数；`holmes/plugins/toolsets/grafana/trace_parser.py:103-109` 把 trace 展示为带每个 span 耗时的树。这与本项目 C3 的 `span_groups`（代码汇总）同向，本项目是在工具结果里做，上游是让模型写查询。
- 指标语义：上游仓库中检索不到 `spanmetrics` / `STATUS_CODE_UNSET`（`grep` 无结果），没有对应说明，不能作为 E5/E6 的上游做法引用。
- 上游是否把数据返回给模型：`prometheus.py:165-169` 的 `tool_calls_return_data` 默认 `True`（返回原始数据），但 `prometheus_instructions.jinja2` 说"The tool call returns no data to you"并靠图表嵌入，两处描述不一致，未确认实际行为。

## 六、证据强度与局限

- 强：E3、E4（确定性检查命中 + 首次生成/重试重放 + ledger 位置核对）；E5/E6 的因果（加说明后 5/20→0/18，边缘显著，非盲评）；「重试保留错误」（fault-1 8/8，数字与引用层面）。
- 中：E1、E2、E6（位置核对；重放只复现 0–1 次，属低频失误）；第二批 fault-1 的形态归类（值位置核对，无重放）。
- 弱：第二批其余条目、6f、pc/cart 的归类（审查原文，未逐条复算）。
- 局限：带工具形态的重放缺历史 reasoning，且模型更常选择继续调工具（分母已剔除）；错误谓词为关键词 + 人工判读；数值溯源检查有误报（单位换算、派生值）；每格 N=7–16，多数差异在噪声内，只有「重试保留错误」与「有无系列说明」差异足够大；模型是 `deepseek-flash` 同一族，结论不外推到其他模型。
- 修复建议见 [任务记录](../../tasks/2026-09-29-m1-01-e-class-attribution.md)。

## 七、费用

- 重放 116 次（96 次生成 + 20 次修复反馈），`ledger.jsonl`。按 `usage` 与官方非高峰单价（hit 0.003 / miss 0.15 / out 0.6 USD 每百万 token，1 USD=7.1 CNY）估算 5.03 CNY。
- 供应商余额差：21.49 → 13.50 CNY，**7.99 CNY**（`deepseek-balance-before/after.json`）。差额大于估算：同一账号在此时段还有其他并发调用方（本会话另有其他执行者），且未按峰谷定价拆分；无法单独归因，以余额差为对账口径，估算为下限参考。

## 目录

`README.md`；`rebuild-manifest.json`（六种请求形态的哈希、token 核对）；`retry-feedback/`（重建的 C2 反馈文本）；`ledger.jsonl`（重放调用账本）；`summary.json`、`score-base.json`、`score-metric_note.json`、`score-repair.json`（判定结果）；`provenance-scan.json`（24 个真实 Run 的溯源扫描）；`unset-adjudication.json`（待盲评的 UNSET 语句）；`samples/`（各样本 `report.txt` 与 `meta.json`，不含 reasoning_content；`repair-*/` 含反馈文本）；`deepseek-balance-before/after.json`；`scripts/`（`rebuild_offline.py`、`replay_eclass.py`、`replay_repair.py`、`check_samples.py`、`provenance.py`、`summarize_eclass.py`、`score_repair.py`、`scan_real_runs.py`、`unset_claims.py`、`balance.py`）。复现：`PYTHONPATH=. .venv/bin/python scripts/rebuild_offline.py <ledger.json> <名称>[@轮次]…`，再 `M0_ENV_FILE=<私有 .env> ECLASS_WORK=<工作目录> … replay_eclass.py --variant base|metric_note --cases … --repeats N`。
