# 上游设计对齐审计：OpsPilot 与 HolmesGPT（2026-10-22）

日期：2026-10-22。上游固定 HolmesGPT master `a0009195468ebc55a7dff7d602b61feabe3cda1b`；OpsPilot 对照 main `a4eea42`。范围只看设计与工程思路是否与上游对齐，不查代码缺陷、安全与测试。五个维度各由一名全新上下文的 Agent 读两边源码完成，本文由 lead 汇总。上游行号以该 SHA 为准；[2026-09-07 源码审计](holmes-source-audit-2026-09-07.md) 用的是旧 SHA `5e983c1`，其中引用的行号已过时（例如 `config.py:112` 现为 `:113`，`tool_calling_llm.py:1161` 现为 `:1238`），本文结论不依赖它。

分类口径：**该照搬**（上游更好，偏离是无意的）、**理由已不成立**（当初有理由，现在不再成立）、**理由仍成立**（有意偏离，保持）、**无差距**。标「未确认」的项没有在源码中核实。

## 总体结论

骨架对齐，偏离大多有理由：持久 Run 与逐轮提交、报告 schema 与代码引用校验、告警内容的不可信边界、人工审核后推送的知识、确定性投影、压缩失败即交接，这些都由 PRODUCT-CONSTRAINTS 或 C3 支撑，部分比上游更严。

差距集中在模型获取信息与调查方法上：环境事实写死在工具描述里，模型没有发现能力，没有超限后的回读通道，系统提示也没有调查方法指导。其中好几项是 C3 自己写了而没有实现。

## 一、工具与上下文获取

| 维度点 | 上游 | OpsPilot | 分类 |
|---|---|---|---|
| 指标发现 | `get_metric_names`（`match` 必填、上限 100、超限提示收窄）、`get_label_values`、`get_all_labels`、`get_series`、`get_metric_metadata`、`list_prometheus_rules`（`holmes/plugins/toolsets/prometheus/prometheus.py:890,1025-1066,1145,1264,1367,1481`）；指令要求先发现再查询（`prometheus_instructions.jinja2` Metric discovery 段） | 无发现工具，指标名与标签写死在描述中（`opspilot/tools/otel_demo.py:309-317`） | 该照搬（#307 A） |
| trace 维度发现与过滤 | `tempo_search_tag_names`/`tag_values`、按 tag 与 `min_duration` 搜索、按 trace id 取（`toolset_grafana_tempo.py:223,464,565-577,658,725,811`） | 服务列表为编译期常量 `SERVICES`（`otel_demo.py:181`），`traces_search` 只能按 service + limit + 时间窗查（`:408-440`） | 该照搬 |
| 枚举所在层 | 运行时由工具发现 | C3 §5/§8 规定枚举属实例层 L3b，`ToolDescription` 有 `{values}` 占位（`otel_demo.py:475-479`、`registry.py:239`），实际发给模型的是静态 `TOOL_SCHEMAS`（`otel_demo.py:789`） | 理由已不成立（与自家 C3 不一致） |
| 工具集组合 | `Toolset` 可插拔，自带 prerequisites、`llm_instructions`、config（`holmes/core/tools.py:787-838`） | `OPSPILOT_TOOL_PROFILE` 二选一，profile 为整体（`profiles.py:45-90`） | 单 lab 阶段理由成立；接入 K8s/日志或第二个目标前需改为按数据源组合 |
| 缺失数据源可见 | 列出 disabled/FAILED 工具集及错误（`_toolsets_instructions.jinja2:1-36`） | 模型看不到缺哪类数据源 | 该照搬（部分），对应 PRODUCT-CONSTRAINTS「缺失工具要可见」 |
| 源错误回传 | PromQL 400/429 原文回传（`prometheus.py:1974`） | 映射为固定码，正文不进模型（`executor.py:1268-1274`） | 理由已不成立（PromQL 解析错误基本只回显模型自己的查询）；建议有长度上限地回传 `errorType` 与 error |
| 结果过大 | 超限摘要（序列数、标签基数、topk 建议，`prometheus.py:767-852,1911-1950`）；通用层落盘给路径和预览（`tool_context_window_limiter.py:33-110`） | 整条拒绝 `RESULT_TOO_LARGE`（`executor.py:1674-1700`），无基数摘要，无回读工具 | 拒绝而不截断：理由成立；无回读：理由已不成立（C3 §5 承诺按 ID 和片段读取） |
| 无数据语义 | `NO_DATA` 状态 | `no_data` + cannot_prove 说明（`executor.py:1081`） | 无差距（OpsPilot 更强） |
| 大输出压缩 | 可选 `llm_summarize` transformer | 确定性投影 | 理由成立 |

## 二、调查循环

| 维度点 | 上游 | OpsPilot | 分类 |
|---|---|---|---|
| 循环骨架 | `while i < max_steps` 生成器（`tool_calling_llm.py:1231-1650`） | 每轮先提交 PG 再继续（`investigation/loop.py:617-788`） | 理由成立（持久 Run） |
| 防循环上限 | `max_steps=100`（`config.py:113`），压缩不计入 | 100 次模型请求（`limits.py:27`），压缩与 provider 重试也计入（`loop.py:681-686,1117-1133`） | 理由已不成立（小项，口径未记录） |
| 强制最终回答 | 最后一步去掉 tools（`:1238`） | 去掉 tools + 最终报告指令 + JSON 模式（`loop.py:646-647,693`） | 理由成立 |
| 结束判定 | 无 tool_calls 即返回（`:1421-1449`） | 过报告校验，失败给一次修复机会，再失败交接（`loop.py:532-564,770-781`） | 理由成立 |
| 工具并行 | 同轮多调用 `ThreadPoolExecutor(max_workers=16)`（`:1469`） | 串行（`loop.py:799-861`）；C3 §5 写「工具采用有限并行」，未找到改为串行的决定 | 该照搬 |
| 重复调用防护 | 同工具同参拒绝，压缩后重置（`:1278-1289`，`env_vars.py:156`） | B5 照搬去重，但 `_compact` 不重置 `seen_tool_calls`（`loop.py:341,807-845,931-`） | 该照搬 |
| 规划工具 | TodoWrite 默认关闭（`prompt.py:32-39`） | 无 | 无差距 |
| 模型重试、流式、中断 | 循环内不重试；不对模型流式；`cancel_event` | 有界重试（`loop.py:1117-1169`）；不流式；租约 fence 与暂停检查（`loop.py:1126-1131`） | 理由成立 |

## 三、上下文管理

| 维度点 | 上游 | OpsPilot | 分类 |
|---|---|---|---|
| 单结果超限 | 超过 `min(15% 窗口, 25000)` token 落盘，给路径、用法和预览（`tool_context_window_limiter.py:69-139`，`llm.py:364-375`） | 超过 25k 真实 token 整条拒绝（`executor.py:1563-1577,1675-1718`）；完整视图已持久存入 evidence store（`executor.py:1584-1600`），缺读回接口 | 理由一半成立：反对截断成立，反对落盘加指针不成立；应补只读「按 evidence_id 读片段」工具 |
| 上下文层第二道上限 | 无 | `visible_view` 约 233k 估算 token 的存根（`context.py:173-206,276-304`），网关已卡 25k，实际触发不到 | 理由成立（备用防线），两处单位与数值不一致，做回读时可合并 |
| 视图投影 | 原始或过滤文本 | 结构化投影与有偏抽样（`otel_demo.py:244-246,1431-1449`） | 理由成立；视图重叠（#96）来自抽样，与上限分开处理 |
| token 计数 | `litellm.token_counter`（`llm.py:620-695`） | 字节×0.25 + 供应商单调校准（`context.py:241-273`） | 理由成立（2026-10-01 对账与用户决定） |
| 压缩触发与产物 | 95% 阈值；system + 摘要 + 最后一条 user | 95% 阈值；固定前缀 + 确定性溯源 digest + 模型摘要（`loop.py:1020-1024`，`context.py:307-373`） | 无差距 / 理由成立（digest 更强） |
| 压缩失败 | 分块摘要、预算减半重发 | 交接（`loop.py:906-920,954-1019`） | 理由成立 |

## 四、提示词、知识与告警上下文

| 维度点 | 上游 | OpsPilot | 分类 |
|---|---|---|---|
| 系统提示结构 | jinja2 模板，组件可开关（`core/prompt.py:206`） | 字节冻结的 segment 序列，进内容哈希（`instructions/discipline.py:140-179`） | 理由成立（可重算 revision、前缀缓存） |
| 调查方法指导 | five whys、追上游依赖、找到根因后继续找其他原因、相邻同名实体分开报告、错误原文当证据（`generic_ask.jinja2:46-57`）；影响面四分组：受影响 / 查过健康 / 因别的原因变化 / 未检查（`:60-69`） | 只有反误读与证据纪律（`discipline.py:61,72,79`），没有调查方法 | 该照搬（与 K8s 无关的部分），属合同层变更 |
| Runbook / 知识 | skills 拉取模式，优先于通用步骤（`generic_ask.jinja2:29-30`，`skill_loader.py:71-80`） | 复盘知识推送，人工审核，作为不可信线索须本 Run 证据核实（`knowledge/store.py:960-1040`，`context.py:78-86`） | 理由成立；是否需要运维自写 runbook 渠道未确认 |
| 告警上下文 | `issue.raw` 直接拼进用户 prompt（`main.py:181-183`） | 问题模板不含 annotation，labels/annotations 脱敏截断后放进带边界的独立消息（`alertmanager.py:230-247`，`context.py:91-108`） | 理由成立（更严，不回退） |
| 环境专属知识 | 不写死，靠发现工具 | L1 不写死，但工具描述写死指标目录与服务枚举（`otel_demo.py:154-160,181-195,310-316,400-405`） | 单目标阶段成立，第二个目标前不成立（同第一节） |

## 五、报告与评测

| 维度点 | 上游 | OpsPilot | 分类 |
|---|---|---|---|
| 报告形态 | 自由文本 `ChatResponse.analysis`（`holmes/core/models.py:362-368`） | `ReportV2` + 一致性校验（`investigation/reports.py:93-140`） | 理由成立（落库、交接、复盘） |
| 证据纪律 | 只靠 prompt | 代码校验引用（`reports.py:281-470`） | 理由成立；可选借鉴影响面「查过健康 / 未查」写法 |
| 因果标注 | 无 | claim 无因果字段，评测侧由 judge 从全文重新枚举因果诊断（`docs/tasks/2026-10-12-eval-suite.md` D6） | 理由已不成立：借了上游 judge 的形式，却交给它上游没有的任务 |
| 用例与抗幻觉 | `test_case.yaml` + expected_output 只给 judge；唯一随机标识 | 场景与答案分文件 + 确定性 signature | 无差距（OpsPilot 更强） |
| 打分 | 每例一次二值判断，要素实质出现即通过（`tests/llm/utils/classifiers.py:151-173`） | 确定性 gate AND judge，judge 逐键输出诊断再给 verdict（`evaluation/judge.py:145-182,850-880`） | AND gate 成立；逐字段枚举是成本来源 |
| judge 可靠性 | 未见事前校准；多次迭代 + 事后失败分诊（eval bug / judge 误判 / 真实失败） | 盲标 + 46/48 + `(location, quote)` 精确包含 + 任一观察失败整轮不合格（`calibration_flow.py:687-692,756-797`） | 先校准再冻结（C3）成立；两条附加规则超出 C3 与上游，理由已不成立 |
| judge 模型 | 默认与被测同模型 | 要求异家族（D16） | 理由成立 |

P7 含义：有效输出上 verdict 一致率已高（FlashX 40/41、DeepSeek 46/47），失分集中在枚举完整性与输出格式；三家重叠漏同一批诊断，按上游做法会先复核标注口径。出路有三条，均需合同裁决：以 verdict 一致率为门槛、逐字段诊断降为参考；复核重叠漏诊的标签口径；把因果标注前移进报告 schema，改为确定性判据。

## 六、文档与实现漂移

- C3 §2 写 LangGraph（ADR-0004 推迟，代码未引入），C3 §5 写流式响应（实际不流式）。
- 100 次模型请求上限计入压缩请求与 provider 重试，未在任务记录中说明。
- 任务记录与旧审计引用的上游行号已过时。

## 七、后续

会改变模型可见面的项（第一节发现、回读、错误回传、缺失数据源，第四节调查方法）应合成一批、在 F3 PR-3b 冻结前完成，只 bump 一次 `tool_schema_revision` / `discipline_revision`，并与 F11 PR-4 的输入 v6 排在同一窗口。待排事项：

- #307（扩充）：指标与 trace 发现、过滤，去掉写死枚举
- #308：按证据 ID 读片段的回读工具与超限基数摘要
- #309：压缩后重置重复调用检查、同轮工具有限并行
- #310：系统提示加入调查方法指导与影响面分组
- #311：查询错误有界回传与缺失数据源可见
- #312：P7 judge 合格判定口径重新裁决（决策）
- #313：C3 与任务记录对齐实现

未确认：上游 `kubernetes/core` 完整工具清单；Robusta 服务端如何渲染告警；litellm 底层是否重试；「加即时查询能缩短描述」未实测；OpsPilot 是否需要运维自写 runbook 渠道（PRD 未核对）。
