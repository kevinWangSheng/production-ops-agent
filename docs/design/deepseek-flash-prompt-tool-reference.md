# DeepSeek V4.1 Flash：prompt 与 tool description 设计参考

- 状态：**参考资料**，实现时照此撰写。不是验收标准，不改变任何已批准合同。
- 日期：2026-09-15。官方事实核对于本日；模型行为随版本变化，改动前重新核对。
- 配套：[调查指令与工具接口合同](instruction-and-tool-interface-contract-2026-09-15.md)
  管**版本与归属**（谁拥有哪一层、revision 怎么生成）；本文管**怎么写**。两者不重复。
- 适用模型：`deepseek-flash`（即 DeepSeek V4.1 Flash；名字不含 "4.1"，
  `deepseek-v4.1-flash` 不是官方 id）。

---

## 1. 模型特性 → 对撰写的硬约束

每条都注明来源。标「实测」的是本项目自己跑出来的。

### 1.1 没有采样调参这条路

| 参数 | 行为 | 出处 |
|---|---|---|
| `temperature` | **无效果**（设了不报错也不生效） | 官方 Thinking Mode 指南 |
| `presence_penalty` / `frequency_penalty` | **无效果** | 同上 |
| `top_p` | 可用，但**下界 0.95** | 同上 |

**含义**：调不动输出分布。想改变行为**只能改 prompt 本身**——
措辞、结构、示例、约束句。别在"调温度试试"上浪费轮次。

### 1.2 带 tools 时必须回传全部历史 `reasoning_content`

> "the `reasoning_content` of all previous turns should be passed back to the API
> and will be concatenated into the context" —— 不回传触发 **400**。
> （官方 Thinking Mode 指南；不带 `tools` 时则可不回传，传了也会被忽略）

**含义**：调查循环的 transcript 结构被这条钉死。
- 每轮 assistant 消息必须保留 `reasoning_content` 原样带回；
- 上下文压缩不能顺手丢掉被折叠组的该字段——C3 第 5 节已规定
  「压缩或恢复后续传不兼容时，阻塞并交接，**不猜测删除协议字段**」；
- 同时它是受限协议状态：只回同 provider 同 Run，**不得进报告、知识、LangSmith 或 judge**
  （PRODUCT-CONSTRAINTS 数据流合同）。

写 prompt 时不用管它，但设计 loop 时必须管。

### 1.3 `max_tokens` 必须覆盖推理预算，否则正文为空

**实测（2026-09-15）**：证据文件 `docs/evidence/m0-model-profile-v41/probe-2026-09-15.md` 随 PR #25（模型 profile 切换）提交，**本分支尚无该文件**，合并后可按此路径查阅。

```
max_tokens=64, thinking=enabled, reasoning_effort=high
→ finish_reason = "length"
→ completion_tokens = 64，其中 reasoning_tokens = 64
→ 正文为空
```

thinking 开启时，推理 token 先从 `max_tokens` 里扣。预算给小了，
**模型不会"少想一点多写一点"，而是想完就没配额写了**，返回空正文。

**含义**：
- 最终报告轮的 `max_tokens` 必须显著大于报告本身的长度；
- 「length + 空正文」是配置问题不是模型故障，排查时先看 `reasoning_tokens`；
- 这复现了项目历史记录里的「length 空正文」。

### 1.4 JSON 输出的两条硬性要求

> "Include the word **json** in the system or user prompt, and provide an **example**
> of the desired JSON format."（官方 JSON Output 指南）

另：「the API may occasionally return **empty content**」——官方承认的已知问题，
建议改 prompt 缓解。

**含义**：报告契约里的 "JSON" 字样和示例对象**是供应商要求，不是可选的排版**。
删掉任何一个都可能直接失效。本项目的 `report_instruction()` 两者都有，别动。

### 1.5 工具轮与报告轮分开

`tools` 与 `response_format` 的交互官方未文档化。
本项目现有形态是：**工具轮送 `tools`、不送 `response_format`；
最终报告轮送 `response_format`、不送 `tools`**。
这规避了未文档化区域，实现时保持。

### 1.6 其余事实

| 项 | 值 | 出处 |
|---|---|---|
| 上下文 / 最大输出 | 1M / 384K | 官方定价页 |
| thinking 开关 | `{"thinking": {"type": "enabled"}}` + `reasoning_effort` ∈ `low/high/max`，默认 enabled/high | Thinking Mode 指南 |
| tools schema | OpenAI 兼容 | Tool Calls 指南 |
| strict 模式 | `/beta` base_url，要求全属性 `required` + `additionalProperties:false`，不支持 `minLength`/`maxLength` | 同上 |
| 官方 description 撰写指引 | **没有** | 同上 |

C3 第 5 节已定**默认关闭 strict beta**，所以 strict 的那些限制当前不适用。

---

## 2. Prompt 怎么写

### 2.1 分层，别写成一大段

| 层 | 内容 | 单一来源 |
|---|---|---|
| L1 调查纪律 | 只读边界、证据可信度、反误读约束 | 待建（M1-01 loop） |
| L2 报告契约 | 输出 schema、字段语义、JSON 示例、引用规则 | `scripts/m0_environment/report_contract.py` |
| L3 工具可见面 | 工具名、描述、参数描述 | 见第 3 节 |

版本与归属见配套合同文档。这里只说一件事：**别把 L3 的内容写进 L1**。
现有 `holmes_baseline.py` 的纪律文本里混了 8 句投影字段语义
（`actual_visible_span_count`、`error_detail_coverage` 等），
结果换个工具面就得再分叉一份 L1。那些句子属于工具描述。

### 2.2 预算句写实际数字，别写"尽量少"

```
You have at most {N} model requests and {M} tool queries;
the last request is reserved for the final report.
```

模型会按给定数字规划。给区间或模糊表述会让它要么过早收束、要么用超。
数字来自运行时，因此这句属实例层，不进 `prompt_revision`（见配套合同 1.1）。

### 2.3 反误读约束：每条都该有来源

本项目 L1 现有 24 句（另有 final-report 变体，并集 30 句），
其中多数是针对**已记录的具体误读**打的补丁，例如：

| 约束句 | 它在防什么（真实发生过） |
|---|---|
| Histogram buckets are cumulative; summing their values does not count calls. | 报告把 `sum(rate(histogram_bucket))` 当调用率 |
| Trace span counts are not unique request counts. | 报告把 spans 当订单数；把同一组 trace 当独立样本 |
| Omitted parents or fields remain unknown; do not claim a complete call chain from a sampled view. | 报告写"20 spans of 349"，实际只显示 14，把上限当实际显示数 |
| Missing metric series, including ERROR, are unknown rather than zero. | 把投影未提供的错误细节泛化成"遥测无触发信息" |
| Do not infer a latency trend without a comparable baseline. | 无 SLO/对照下用延迟数字断言健康或恶化 |
| The authorized query window is fixed by the trusted runner; do not supply start/end tool parameters. | 模型把窗口抄早 0.533447 秒——**参数被接受、无错误码、返回恰好相同**，静默错误 |

完整索引与逐条出处见配套合同第 3 节。

**写新约束句时**：先找到它要防的那次具体失败，记下来源。
没有来源的约束是候选删除项——换模型时你需要这张表来判断哪条还需要。

### 2.4 措辞原则

- **陈述禁止，不要请求**："Do not claim X without Y" 优于 "Please try to avoid X"。
- **给出替代动作**："report `actual_visible_span_count`, not `display_max_spans`" 优于
  "不要报错误的 span 数"——后者不告诉模型该报什么。
- **一句一个约束**，便于逐条回溯与删除。
- **不用测试故障类别、注入参数或答案做关键词**（ADR-0002）——
  症状类别不得成为 prompt 路由键。

---

## 3. Tool description 怎么写

官方没有指引，以下是本项目自有标准。

### 3.1 五项必填（缺一在注册期拒绝）

| # | 必填项 | 范例（取自 `replay_tools.py`） |
|---|---|---|
| D1 | 返回什么、数据源、投影形态 | "Return a frozen trace summary (trace ids and per-service span/error counts)" |
| D2 | 固定时间窗，绝对值 | "for the fixed window {start}..{end}" |
| D3 | 可用取值枚举，有限集合必须内联 | "Only these exact PromQL strings are available: [...]. Any other query returns an error." |
| D4 | 结果上限与截断语义 | "Return up to 20 frozen log rows" |
| D5 | **这个返回不能证明什么** | "Listing does not prove health." |

### 3.2 D5 是最容易省略也最值钱的一项

现有四个工具里只有 `otel_services` 写了 D5，而第 2.3 节列出的误读**多数发生在另外三个工具的返回上**。

针对 V4.1 Flash 尤其重要：1.1 说了没有采样调参的余地，
模型对返回的解读**只能靠描述里的文字纠正**。D5 就是那句纠正。

写 D5 的方法：问"模型拿到这个返回，最可能得出什么它不该得出的结论"。

- 列出服务 → 可能推出"服务健康" → "Listing does not prove health."
- 返回采样 span → 可能推出"这是全部" → "Sampled view; omitted spans remain unknown."
- 返回累计直方图 → 可能推出"这是调用数" → "Buckets are cumulative; summing them does not count calls."

### 3.3 枚举必须内联，别指望模型猜

D3 要求把可用取值写进描述。理由同上：没有别的纠偏手段。
`otel_metrics` 的描述直接内联了全部可用 PromQL 字符串，并写明
"Any other query returns an error." ——模型因此不会自己编查询。

枚举值随授权范围变化，属实例层，不进 `tool_schema_revision`（见配套合同 1.3）。

### 3.4 禁止项

- **保密**：不写凭据、认证信息、具体 `endpoint`/`base_url`/`credential_ref`。
- **权限**：不写让模型自行选择 target 或 endpoint 的语义。
- **不承诺执行器不保证的行为**：投影实际采样就别写 "returns all spans"。
- **不引用测试故障类别、注入参数或答案**。

注意：**授权范围内的服务名枚举与实例标识不属禁止项**，它们是 D1/D3 要求写的内容。
参数键的保留字规则（`RESERVED_PARAMETERS`）作用于参数名，**不可平移到描述文本**——
那会禁止掉 "Do not supply a target; the runner binds it." 这类正确描述。

### 3.5 参数描述

每个参数都要有描述，写清取值来源而不是类型：

```
"query": {"type": "string",
          "description": "Exact PromQL string from the available list."}
```

"Exact ... from the available list" 比 "The PromQL query" 有用得多。

---

## 4. 实现前的检查清单

**Prompt**

- [ ] 分层，L3 内容没混进 L1
- [ ] 预算句是实际数字
- [ ] 最终报告轮的 `max_tokens` 覆盖推理预算 + 正文（见 1.3）
- [ ] 报告契约里有 "json" 字样和示例对象（见 1.4）
- [ ] 每条反误读约束有来源记录
- [ ] 没有用症状类别做路由键

**Tool description**

- [ ] D1–D5 齐全，尤其 D5
- [ ] 有限枚举已内联
- [ ] 无凭据/endpoint/target 选择语义
- [ ] 描述与执行器实际行为一致

**Loop**

- [ ] 带 `tools` 的每轮都回传全部历史 `reasoning_content`（见 1.2）
- [ ] `reasoning_content` 不进报告/知识/trace/judge
- [ ] 工具轮与报告轮分开送 `tools` / `response_format`（见 1.5）
- [ ] 不依赖 `temperature` 等无效参数（见 1.1）

---

## 5. 本文的局限

- 官方行为随版本变化。`deepseek-flash` 是**浮动别名**，指向"最新 V4.1 Flash"，
  换代时名字不变——本文事实需定期重新核对，不能假定长期有效。
- 第 1.3 节的实测来自**一次**探针调用，证明的是「推理会吃掉 `max_tokens`」这一机制，
  不是任何性能或质量结论。
- 本文不是验收标准。按它写出来的 prompt 仍须走既有的验收与评测流程。
