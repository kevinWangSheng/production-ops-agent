# M1-01 限制审计剩余项：整视图字节计量与 1M 上下文单次请求超时

- 状态：进行中（实现、测试与有界真实 Run 已完成；待独立审查处置、CI 与用户合并）
- 更新日期：2026-09-29
- 依据：[收口记录后续项 4](2026-09-28-m1-01-closure.md)；[限制](2026-09-28-m1-01-loop-limits.md)、[对齐第二批 B1/B4/B6](2026-09-28-m1-01-upstream-alignment-b.md)、[对齐第三批 C3](2026-09-28-m1-01-alignment-c.md)
- 工作区：`/Users/shenghuikevin/dev/AI/production-ops-agent-view-bytes`，PR 分支 `feature/m1-01-view-tokens`（从 origin/main 分层重提：文档、测试、产品代码与依赖）；工作分支 `feature/m1-01-view-bytes-timeout`（起点 origin/main f0038dd）原样保留、不推送，提交历史留作审计。换分支的原因：旧分支早期提交里的十六进制摘要标识符触发 gitleaks `generic-api-key`，CI 扫全部历史且禁止豁免，又不改写历史
- 证据：调研与延迟实测 [m1-01-view-bytes-timeout](../evidence/m1-01-view-bytes-timeout/run.md)；有界真实 Run [m1-01-view-tokens-effect](../evidence/m1-01-view-tokens-effect/run.md)

## 结论

| 子项 | 结论 | PR |
|---|---|---|
| A 工具结果按整视图、按真实 token 计量，超限直接拒绝（原 A1+A2；用户 2026-09-29 决定选 tokenizer，并决定超限对齐上游拒绝、不再截断） | 缺陷加口径对齐：见下 | PR-1（唯一 PR） |
| B 1M 上下文单次超时 | **无需修改**：最坏情形约 176 s，低于 360 s | 无 |
| estimate_tokens 少算约 1.6 倍 | 另开一项查证，不入 PR-1；tokenizer 模块将来可复用 | 待 lead 安排 |

A 与 B 是独立问题，B 无 PR。`tests/test_m1_whole_view_bytes_contract.py`（470f87e，16 项）按字节口径写成，本合同替代它，须由合同测试作者按下文重写，不由实现者改。

## A. 整视图按真实 token 计量

**现状证据**（字节口径的差距，详见 `view-overhead-measurement.txt`）
- `MAX_VIEW_BYTES = 100 * 1024`（`opspilot/tools/otel_demo.py:211`），两个工具注册都用（:492、:587）；`_fit_rows` 只约束行数组（`executor.py:1780`）；整视图收缩循环仅 `view_augment` 非空时执行（`executor.py:1473`），全仓只有 `traces_search` 声明（`otel_demo.py:1072`）。
- 送模型的是整份视图：`canonical(payload)` 作 tool 消息内容（`context.py:817-830`）。metrics 视图固定字段约 1.1 KB，1,992 字符表达式回显再加约 2 KB，最坏 103,152 B，超 752 B。
- 单位问题：100 KiB 按 4 B/token 记作"约 25k tokens"（`otel_demo.py:190-209`、`context.py:108-135`）。实测真实 token：metrics fixture 2.33–2.47 B/token，traces 3.06，合成指标视图约 2.34，即 100 KiB ≈ 33k–44k tokens。

**上游对照**（HolmesGPT `a045ec7`）
- 计量：`spill_oversized_tool_result` 对整条 tool 消息 `to_llm_message()`（`holmes/core/models.py:19-46`）用 `litellm.token_counter` 计数（`llm.py:596`），调用点 `tool_calling_llm.py:1068`。
- 上限：`min(context_window × 15%, 25000)`（`llm.py:331-342`；`env_vars.py:137-143`，`TOOL_MAX_ALLOCATED_CONTEXT_WINDOW_PCT=15`、`..._TOKENS=25000`）。窗口 ≥ 166,667 tokens 时 25,000 起作用；本项目窗口 1M，15% 为 150k，所以取 25,000。
- 超限：主路径是落盘。有落盘目录且存储可用时，完整结果写入文件，模型收到 "The tool call result is too large to return: {N}/{MAX} tokens." 加文件路径、`cat`/`jq` 读取提示和一段按字符预算截取的预览（`tool_context_window_limiter.py:76-129`，含预览预算 :118-121），模型可用读文件的方式取全量。只有存储不可用或失败时才走兜底分支：丢弃数据，置错误状态，文本为同一句 "too large" 加空行加 "Try to repeat the query but proactively narrow down the result so that the tool answer fits within the allowed number of tokens."（:131-139）。`spill_oversized_tool_result` 定义在 :33，对所有工具生效，计数与上限取值在 :47-49，句子在 :69 组装，调用点 `tool_calling_llm.py:1068`。**用户 2026-09-29 决定本项目超限时拒绝、不再截断（推翻 B1 的截断决定）**。这对应上游的兜底分支，不是主路径；主路径要求模型有读文件的工具，本项目没有，所以不做，这是对上游主路径的一处有意偏离（上游预览本身也是部分数据，只是伴随可取全量的手段）。

**Tokenizer 选型**
- 来源：Hugging Face `deepseek-ai/DeepSeek-V4.1-Flash`（定价页 2026-09-29 写明 `deepseek-flash` = DeepSeek-V4.1-Flash），MIT 许可，仓库提交 `dba1be0a40aa45a94ad051997016db3960a90277`（2026-09-10）。`tokenizer.json` 6,367,257 B，sha256 `c90dfa01249db1be4245780a052ede752e1361c612ac6d08e2bdada7d599476b`；`LICENSE` sha256 `f2c6c602815669d292889e5be8c802f2ed950653b77999b1584e8e6aed25d040`。V4-Flash 的 tokenizer.json 不同（sha256 `8f9f37ca…`，差 111 B），但在下列样本上计数相同。DeepSeek 文档页提供的 `deepseek_tokenizer.zip` 演示包未取到（链接未解析），未核对，不作依据。
- 校验（`tokenizers` 0.23.2 本地计数 vs 真实 API 的 `usage.prompt_tokens`，见 `real-view-tokens.json`）：把同一视图重复 2 次与 22 次得到的每条 assistant+tool 消息边际 token 数，减去本地内容计数，差为固定 35–37 tokens（模板与 tool_call 包装）：metrics 605 vs 570、traces 19,097 vs 19,060；合成视图 34,761/条，与 975,123 总数一致。所以本地计数在内容上与提供方一致，只欠每条消息一个常数开销，占 25k 的 0.15%。
- 加载包：PyPI `tokenizers`（HF，Apache-2.0，Rust 加载 `tokenizer.json`，无需 transformers）。放进 `[project].dependencies`（当前为 `[]`，产品运行时首次引入第三方包），因为执行器运行时使用。代价：`uv.lock` 新增约 9 个包（`tokenizers` 及其依赖 `huggingface-hub`、`filelock`、`fsspec`、`hf-xet`、`pyyaml`、`tqdm`、`httpx`、`httpcore`；`anyio`、`certifi`、`h11`、`idna`、`packaging`、`typing-extensions` 已在锁中）。`huggingface-hub` 只是 `tokenizers` 的声明依赖，本项目从不调用 `from_pretrained`，运行时不联网。`make check` 的 `uv lock --check --offline` 需重新 `uv lock --python 3.12` 并审查差异；CI 三个 job 走 `make setup`，多装约 9 个 wheel，无新的 Secret。备选：`transformers`（更重，否决）；自实现 BPE（与提供方计数偏差风险，否决）。
- Vendoring：`tokenizer.json`、`LICENSE` 与一份 `PROVENANCE`（上游仓库提交、两个 sha256、取得日期）放进仓库（建议 `opspilot/tools/tokenizer/`），共约 6.4 MB。理由：worker 运行时不得联网下载；CI 与离线机器一致。不用 Git LFS。
- 加载与失败：进程内单例，加载前校验 sha256 等于代码内固定值。加载耗时 0.11 s，编码 1 MiB 约 0.6 s，进程 RSS 约增 200–350 MB（1 MiB 编码后峰值 369 MB，本机实测）。文件缺失、哈希不符、`tokenizers` 不可导入或加载异常时，构造执行器抛 `ToolContractError("TOKENIZER_UNAVAILABLE")`，使 worker 在启动时失败，**不静默退回字节估算**，也不在 Run 中途才失败。
- 记录：估算器 `estimate_tokens`（`context.py:174`）以后可复用该模块；本 PR 不改它。

**上限取值**：`MAX_VIEW_TOKENS = 25_000`，对应上游 25,000（不采用 15% 项，理由见上）。

**A 修复合同（PR-1）**

公共接口：`ReadOnlyToolExecutor.execute(ToolRequest) -> ToolOutcome`（`model_view`、`evidence`、`status`、`reason`）；`ToolRegistration.max_view_tokens`（新字段，**替换** `max_view_bytes`）；`opspilot.tools.otel_demo.MAX_VIEW_TOKENS`（替换 `MAX_VIEW_BYTES`）；`opspilot.tools.tokens`（`TokenCounter`、`load_deepseek_counter`、`count_tokens`）；`ReadOnlyToolExecutor(..., token_counter=None)`；`otel_demo_executor_factory(..., token_counter=None)`；错误码 `TOKENIZER_UNAVAILABLE`、`INVALID_TOKEN_COUNTER`。

被计量的量：`count_tokens(canonical(view))`，其中 `view` 是包含**全部**行（及全部固定字段、`view_fields`）的完整 adopted 视图，即若不超限将原样送模型的那个字符串。不含每条消息的包装开销（约 10 tokens；`tool_call_id` 此时未知；实测每对 assistant+tool 消息共 35–37 tokens，≤0.15% 于上限），与上游含包装的差别在此记录，不在测试里断言。

**可观察行为**（对所有注册工具、所有 adopted 视图；假计数器 `lambda s: len(s.encode())` 即可验证 1–8）：
1. 不超限（计数 ≤ `max_view_tokens`，恰等于也算）：视图与今天的形状相同，全部行都在，`content` 是完整行数组，`truncated is False`、`omitted_rows == 0`、`omitted_bytes == 0`；`status`/`reason`/证据登记不变。
2. 超限（计数 > 上限，多 1 token 即算）：**不返回任何行**。`outcome.status == "error"`、`outcome.reason == "RESULT_TOO_LARGE"`、`outcome.source_contact == "confirmed"`、`outcome.evidence is None`、证据 sink 无新增记录（同现有字节超限拒绝，见下）。`model_view` 是拒绝视图（`trust == "gateway"`、`citable_as_fact is False`、`content is None`，与其他拒绝一致），并带固定的、不含源内容的明细：`max_view_tokens`（上限）、`view_tokens`（实际计数，即分母前的分子）、`message`。`message` 贴近上游：以 "The tool call result is too large to return: {view_tokens}/{max_view_tokens} tokens." 开头，随后建议缩小时间窗、加过滤或降低 limit（文字含 "narrow" 或 "window"、"limit"，不含源内容、端点或凭据）。拒绝视图不含 `returned_count`、`truncated`、`omitted_*`。
3. 三类拒绝的优先级不变：人工决定（暂停/取消）在读取途中发生时，仍以人工决定为准（`refuse_after_fetch` 的既有优先序），此时证据按现有规则作历史保留（`adopted=False`）；令牌超限只在没有更高优先的拒绝时出现。设施/校验类问题（`MALFORMED_RESULT` 等）先于令牌检查。
4. 不写证据的理由与账本：拒绝时 raw 不落库，与现有字节超限拒绝（`executor.py:898,1186`）一致——`max_view_tokens` 检查发生在证据提交之前，没有 `EvidenceRecord`、没有 `view_sha256`；操作本身照常记账（`operation` 已派发，`tool_seconds_used`、`operations_used` 累加，账本按已耗秒结算），因为源确实被读了。工具调用不另计入防循环上限：上限只有模型请求次数（100，`limits.py`），拒绝所在轮照常占一个模型请求，模型收到拒绝后的下一轮同样计数。
5. 与"非 ok 视图不可作事实引用"一致：拒绝视图 `status == "error"`、`citable_as_fact is False`、无 `evidence_id`；报告校验（`cites_non_ok_view`、`not_citable_as_fact`，`reports.py:258-259,336-338`）对它的处理与其他错误视图相同，不需要新规则。
6. 字节超限拒绝（`max_result_bytes`）的关系：**共用同一个 `reason == "RESULT_TOO_LARGE"`、同一状态与 `source_contact`、同一"不落库"语义、同一 `message` 字段与建议方向**；区别只在明细字段：字节路径带 `max_result_bytes`（现有行为完全不变，含 `test_b1_review_a...` 对 "byte"、"limit/window" 的断言），令牌路径带 `max_view_tokens` 与 `view_tokens`。两处的 `message` 文字不强求逐字相同（字节路径读到上限即中止，没有可报告的实际大小），但都包含缩小窗口/加过滤/降低 limit 的建议。理由：统一 reason 让模型侧与报告侧只需处理一种"太大"，两个检查只是先后两道闸门（原始体积先于视图体积）。
7. 计数器：`token_counter` 为 `None` 时构造执行器即加载真实计数器，失败抛 `ToolContractError("TOKENIZER_UNAVAILABLE")`（文件缺失、sha256 不符、`tokenizers` 不可导入、加载异常），不调用任何传输层、不静默退回字节估算；非 `None` 时不触碰 tokenizer 文件也不导入 `tokenizers`。返回非 `int`、`bool` 或负数的计数器在首次使用时抛 `ToolContractError("INVALID_TOKEN_COUNTER")`。真实计数器的黄金值见"计量对象与计数器接口"。
8. 注册校验：`max_view_tokens` 必须是 `int` 且 ≥ 1（`bool` 不算），否则 `VIEW_LIMIT_OUT_OF_RANGE`；不再要求不超过 `max_result_bytes`；`max_view_bytes` 是改名而非新增，传入旧字段名是 `TypeError`，不留别名。
9. 时限：计数每个 adopted 视图恰一次（不再有收缩循环），1 MiB 视图约 0.6 s（实测），不设另外的行级重算。
10. 真实路径：用真实计数器，`metrics_range_query` 与 `traces_search` 在正常输入下整视图计数 ≤ 25,000 且不拒绝（例如 `traces20heavy` 19,895 tokens）；超过（例如 1200 行指标 29,232 tokens、40 个重 span 27,630 tokens）时得到第 2 条的拒绝，`view_tokens` 与真实计数一致。
11. 模型可见文案与版本：`otel_demo` 各处的截断说明改为拒绝说明（`otel_demo.py:274、343、448、543` 一带，含 "reports omitted_rows"、"dropping trailing spans"、"Truncated at the registered max_view_bytes"），`fixture.py` 的工具描述同样改为拒绝语义。两个 profile 的工具面随之变化：`tool_registry_revision` 与 face 哈希变动（`registry.py:806` 的导出含该字段）；`versions` 里的 `tool_schema_revision` 是旧 Run 被挡住的机制：`otel-demo` 由 schema 内容哈希自然变化（实测 `otel-demo-a08b…` → `otel-demo-34bc…`），`fixture` 是手写常量，须由 `fixture-1` 升到 `fixture-2`。`PROJECTION_REVISION`（`outcomes.py:56`，v5 → v6）另行记录视图语义变化（行不再被截断），不在 `versions` 里，不起挡旧 Run 的作用。部署时旧修订下未完成的 Run 按既有规则记为 `blocked(INCOMPATIBLE_STATE)`，PR 正文注明。
12. 未采纳（人工暂停作废、只作历史保留）的视图：不带适配器的 `view_fields`（含 `span_groups`、`span_groups_note`、`traces_requested` 等），因为其中有的汇总行内容（`span_groups`），而这类视图不给模型看任何行；`content is None`。改动前 `span_groups` 在这类视图里是 `[]`。

**拒绝路径的逐项澄清**（回应测试作者）
- 错误码：并入 `RESULT_TOO_LARGE`，不新增；`status == "error"`。
- `content`：拒绝视图里 `content is None`；`model_view` 是 `_refuse()` 的拒绝视图，键为 `operation_id`、`trust="gateway"`、`status`、`citable_as_fact=False`、`reason`、`source_contact`、`tool`、`target_id`、`window`、`requested_at`、`content=None`，外加明细 `max_view_tokens`、`view_tokens`、`message`。**没有 `adopted` 键**（与所有既有拒绝视图一致），没有 `evidence_id`。
- 证据与原始字节：不登记，`outcome.evidence is None`，sink 无记录，原始字节不落库（与字节超限拒绝一致）。
- 窗口与查询回显（2026-09-29 真实 Run 审查后新增）：拒绝视图的 `window` 是本次调用实际查询的窗口（与 ok 视图的 `window` 同源：给了 `start`/`end` 则为其缩窄后的窗口；未给则为默认查询窗，即授权框架末端往前 1 小时，batch B 对齐上游 1 h 回看时定下的，不是 24 h 授权框架本身），不是授权框架窗口；并新增 `query`，即被接受的调用参数（与 ok 视图的 `query` 同源，如 `service`、`limit`、`start`、`end`、`expr`）。因此拒绝视图的键集合是原 14 个加 `query`，共 15 个。理由：5 份真实 Run 审查一致发现，缺这两项时模型分不清哪次调用被拒，并把 24 h 框架读成自己请求的窗口（"trace view keeps returning the full window"，3 个 Run 中出现）。字节超限拒绝路径不改（传输层读到上限即中止，没有可回显的已接受视图）。
- 文字：`message` 以 "The tool call result is too large to return: {view_tokens}/{max_view_tokens} tokens." 开头，后接缩小时间窗、加过滤或降低 limit 的建议；`view_tokens` 是完整视图的计数。
- 防循环上限：不计入（上限只数模型请求）；见行为 4。
- `truncated`、`omitted_rows`、`omitted_bytes`：拒绝视图中**不存在**（不是 0）；adopted 视图中保留且恒为 `False/0/0`。
- 边界：计数 == 上限则视图完整，`view_tokens` 不出现；计数 == 上限 + 1 即拒绝，且 `view_tokens == 上限 + 1`。
- `span_groups`：不再有"只汇总展示行"的特殊语义。它对适配器返回的全部行（适配器自身取样之后的行）汇总，等于视图 `content` 的全部行；拒绝时整份视图不出现，故无部分汇总。`span_groups_note` 文字随之改为不再提截断。
- 既有测试中"少保留几行"变为"整个被拒绝"的合同变更，逐条见"既有测试影响评估"第 1 项：B1 `test_b1_metrics_view_byte_cap_also_widened`（1200 行 29,232 tokens）、C3 `..._span_groups_only_summarize_shown_rows_when_truncated` 与 `..._counts_toward_the_view_byte_cap`（及 260-span 字节边界 fixture）、`test_m1_view_explicit_contract.py` 的 40 个重 span（27,630 tokens）用例；`test_m1_otel_demo_contract.py` 的约 90 span traces 用例需先测其 token 数：仍在 25,000 内则只重标定、不属合同变更，超出则同属合同变更。`traces20heavy`（19,895 tokens）不变。
- 性能：只计数一次；1 MiB 视图约 0.6 s（`tokenizers` 实测），测试上界建议宽松（如用真实计数器 ≤ 5 s），不测假计数器的耗时。

**保留的字段与语义**：视图与 `EvidenceRecord` 里的 `truncated`、`omitted_rows`、`omitted_bytes` 保留，为兼容 `context.py:99,508` 的来历字段和证据读取方；在本执行器产出的 adopted 视图里恒为 `False/0/0`（行不再因体积被丢）。`<unit>_shown` 等于 `returned_count`，`<unit>_omitted` 仍是 `backend_<unit>_returned - <unit>_shown`，仅反映适配器自己的取样（`traces_search` 的 span 取样），不受视图上限影响。`context.py` 的 25% stub 后备（`visible_view`、`view_stub`）不动：它是另一层保护，阈值约 233k tokens，在 25k 上限之下不会触发。

**收缩循环、`view_augment` 与常量的处置**
- `_fit_rows` 与整视图收缩循环（`executor.py:1780`、`1473-1490`）：**删除**。不再有按前缀丢行的路径，留着只会重新引入部分视图。
- `view_augment` 回调与 `_row_dependent_fields` 中的 augment 分支、`MALFORMED_VIEW_AUGMENT`、`TransportResponse.view_augment` 字段：**删除**。它存在的唯一理由（C3 P2-1）是 `span_groups` 必须只汇总"字节上限之后仍展示的行"，而现在展示的行就是全部行；适配器可以直接在返回响应前算好，作为 `view_fields` 的一项。因此 `traces_search` 的 `span_groups` 改为适配器在 `TransportResponse.view_fields` 里给出（对全部提取行计算，与执行器 `result_path` 抽出的行同一集合），保留 `span_groups_note`，但其文字里"只汇总已展示的行；被取样或被截断的不计"改为"只汇总被取样后展示的行"。（`view_fields` 与通用字段冲突的既有校验 `_unit_fields_problem` 仍适用。）
- `MAX_VIEW_BYTES` / `max_view_bytes`：**删除**，不保留字节兜底：字节数对上下文没有独立意义，25,000 tokens 已是更紧的界；两份限制并存会重造"哪个先触发"的双轨口径。

**不做**：不改 `estimate_tokens`、压缩阈值、`single_tool_pct`、25% stub；不改 `max_result_bytes`、`TRACE_SOURCE_READ_BYTES`；不做落盘或指针；不按工具特例；不引入 tokenizer 之外的包；不动 `traces_search` 的取样与排序；不改 `RESULT_TOO_LARGE` 的字节路径行为。

**计量对象与计数器接口**（tokenizer、注入、字段改名不变，摘要如下）
- 新模块 `opspilot.tools.tokens` 导出 `TokenCounter = Callable[[str], int]`、`load_deepseek_counter(tokenizer_path: Path | None = None) -> TokenCounter`（默认取入库文件，校验 sha256，失败抛 `TOKENIZER_UNAVAILABLE`）、`count_tokens`（默认计数器的便捷函数）。计数器须是确定的纯函数 `str -> int`；假计数器示例 `lambda s: len(s.encode())`。
- 真实 tokenizer 的集成测试（另放一个文件，随 CI 运行）：黄金值 `count_tokens("") == 0`、`"hello world" == 2`、`'{"a":1}' == 5`、`"x"*1000 == 125`、`"0.123456789"*100 == 500`、`"检查订单服务的错误率" == 5`（2026-09-29 用固定 tokenizer.json 实测）；篡改一个字节的临时拷贝经 `load_deepseek_counter(path)` 抛 `TOKENIZER_UNAVAILABLE`。
- `omitted_bytes` 保留，语义不变；不新增 `omitted_tokens`。

**既有测试影响评估**（未跑；依据代码阅读与对 3 个 fixture 的本地计数）

假定 `tests/m1_tool_support.py` 的通用注册改用 `max_view_tokens`，`build()` 注入每字节 1 token 的假计数器，默认上限 8192，并可覆盖。截断改为拒绝后，**所有"超限后保留部分行"的断言都成为合同变更**，逐条如下。

1. 合同变更（必须改断言）：
   - `test_m1_tool_outcomes.py:435` `test_view_truncation_is_marked_while_raw_evidence_stays_complete`：整个用例的前提"截断、`returned_count` 在 0 到 40 之间、`omitted_bytes > 0`、raw 仍完整"不再成立；改为超限得到 `RESULT_TOO_LARGE`、`evidence is None`、`content is None`；"raw 完整"改测不超限时 raw 与源逐字节相同。
   - `test_m1_tool_outcomes.py:458` `test_a_view_truncated_to_nothing_is_not_reported_as_no_data`：`ok` 且 `content == []` 的前提不再成立（不再有"截成空"）；改为超限给 `RESULT_TOO_LARGE`，同时保住"不是 `no_data`"的意图。
   - `test_m1_view_explicit_contract.py` 的字节截断用例（`test_b_byte_truncation_leaves_spans_omitted_positive_and_consistent` 及 40 个重 span 的 `omitted_rows == 12` 类断言，约第 325 行起）：超限变为拒绝；"`omitted_rows`、`spans_omitted` 正且自洽"改为拒绝断言，保留"适配器取样导致 `spans_omitted > 0`"的用例（若有）以维持该字段的其余语义。
   - `test_m1_alignment_c_contract.py` 的 C3 截断类用例：`test_c3_span_groups_only_summarize_shown_rows_when_truncated`（第 714 行）与 `test_c3_span_groups_counts_toward_the_view_byte_cap`（第 853 行）及字节边界 fixture（260 个 span，96,201 B）：截断路径不复存在，两者删除或改写为"超限即拒绝"；`span_groups` 对全部行汇总的正向用例保留；`view_augment` 相关断言（第 98 行的 `MAX_VIEW_BYTES` 引入等）随 API 删除而改。
   - `test_m1_upstream_alignment_b_contract.py` `test_b1_metrics_view_byte_cap_also_widened`：1200 行指标 29,232 tokens，超 25,000，现在被拒绝；断言从"不截断"改为"拒绝"，或缩小到 25k tokens 以内后保留"不截断"。`test_b1_traces_view_byte_cap_matches_the_upstream_tool_result_scale`（20 重 span，19,895 tokens）仍不超限，仅改文字里的字节口径。
   - `test_m1_tool_registry.py:76-83`：`{"max_view_bytes": 8192}` 与 `{"max_view_bytes": 1}` 不再报 `VIEW_LIMIT_OUT_OF_RANGE`；仍报错的是 0、负数、`bool`、非 `int`。第 83 行起的说明（`_fit_rows`、`EMPTY_VIEW_BYTES` 下限）随之过时。
   - `test_m1_tool_registry_binding.py:54`：字段名与修订哈希预期。
2. 需重标定或重测但语义不变：`test_m1_otel_demo_contract.py` 中 traces 未截断断言（约 90 span，需在 25k tokens 内重测）；`test_m1_investigation_compaction.py:373`（`max_view_bytes=400_000` 换算为 token；须保持"由 25% stub 而不是视图上限触发"的原意，故取大于 stub 阈值的 token 数，同时该用例中视图本身会先触发新拒绝，需要在该测试里让视图构造避开拒绝，例如经假计数器使视图计数为很小的常数）。
3. 仅重命名：`test_m1_tool_boundaries.py:652,668`、`test_m1_loop_limits_contract.py:314`（`max_result_bytes=64` 触发字节拒绝，视图上限只是陪衬）。
4. 先前试验取消门槛得到的 9 个失败（512 字节默认注册）：通用注册放宽后消失。
5. 已在树里的两份测试：`tests/test_m1_whole_view_tokens_contract.py`（001aa3a，含截断断言）与 `tests/test_m1_view_tokenizer_integration.py` 按本合同 v3 重写；`tests/test_m1_whole_view_bytes_contract.py`（470f87e）已被取代，应删除。
6. 处置：由合同测试作者依本合同重写，并逐条记录调整；实现者不改这些断言。

**有界真实 Run（本 PR 必附，用户 2026-09-29 决定）**

这个 PR 改变模型可见行为（视图与工具描述、拒绝文案），按 AGENTS.md「触碰调查 loop…附至少一次有界真实 Run」处理，并按用户要求加严：
- 环境：otel-demo profile，拿 lab 锁（`mkdir .../scratchpad/locks/lab`，见公共约束），OTel 实验环境与 PG 只在持锁期间使用；用完停止自己启动的实例并释放锁。
- 场景：至少正常 1 次、故障 1 次，流程与问题文本沿用第三批效果测量（`docs/evidence/m1-01-alignment-c-effect/run.md`，6 个 Run 为基线）。第三批 6 个 Run 中 `RESULT_TOO_LARGE` 出现 0 次，所以自然场景很可能不触发拒绝；因此再加 1 个"广域"Run（开发脚本给出的问题要求跨全部服务、24 h 的宽窗口取数，问题文本入证据，不改产品代码）以求真实模型确实撞上 25,000 tokens 上限。若该 Run 仍未出现拒绝，如实记为"未触发"，拒绝后的恢复行为则由确定性合同测试覆盖，并写明这一未验证面。
- 对比指标（每 Run 一行，与基线并列）：拒绝出现次数（含 `RESULT_TOO_LARGE` 的令牌路径与字节路径分开数）；模型收到拒绝后下一轮是否缩小查询（窗口、limit、过滤）并成功取得 ok 视图；每 Run 的模型请求轮数与 prompt/completion tokens；报告的 P2 数（独立审查口径，同基线）；是否自行结束与是否需要 C2 修复重试。
- 记录：`docs/evidence/m1-01-view-tokens-effect/`（ledger、`run.md`、每 Run 的报告与审查），费用按供应商余额差对账；证据里 `idempotency_key` 一律写成 `idempotency_label` 并在 `run.md` 说明；推送前跑 gitleaks。

## B. 1M 上下文下的单次请求超时

**现状**：`MODEL_REQUEST_TIMEOUT_SECONDS = 360.0`（`limits.py:90`），`RunLimits.model_request_timeout_seconds` 默认取它，实际值为 `min(360, 剩余 deadline, 剩余 active)`（`loop.py:1183`）；客户端整请求用 `future.result(timeout=budget)` 限制总时长，与 keep-alive 空行无关（`client.py:203-241`）。请求为非流式，thinking 开、`reasoning_effort=high`（`loop.py:188-201`）。

**实测**（详见 run.md，冷缓存，产品同款请求体）
- 预填充：约 105k / 314k / 662k / 975k prompt tokens → 2.3 / 6.4 / 12.8 / 21.0 s，近似线性。
- 解码：打满 65,536 输出 tokens，105k 上下文 151 s，975k 上下文 155 s（约 420–430 tokens/s；上下文长度对解码速率影响不明显）。
- 最坏情形（975k 冷预填充 + 65,536 输出打满）≈ 21 + 155 = 176 s，为 360 s 的 49%，余量约 2 倍。提供方接受了 975k prompt + 65,536 max_tokens（合计 1.04M）的请求。
- 未测：高峰时段是否放慢；思考型输出真实达到 65k 的频率；出错或 429 的响应时间。单次测量，没有方差估计。

**对照**：上游 `LLM_REQUEST_TIMEOUT` 默认 600 s（`holmes/common/env_vars.py:166`，`llm.py:782`）；DeepSeek 官方 Rate Limit 页（2026-09-29）：非流式请求等待期间服务端持续返回空行保活，10 分钟内未开始推理则服务端关闭连接，页面未给出总时长上限。因此提供方侧的硬界限约为 600 s，本项目 360 s 在其之内；未见官方文档说明大上下文的耗时上限，只有本次实测。

**判定：无需修改。** 依据：最坏实测 176 s < 360 s；把超时抬到上游的 600 s 只会在真实卡死时多占最长约 4 分钟，且 `RUN_WALL_SECONDS`（7200 s）和 `LEASE_SECONDS=MODEL_REQUEST_TIMEOUT+60`（`web/service.py:78`）都以它为基，改动会连带改租约。会改变结论的证据：高峰时段实测出现 >200 s 的打满输出请求，或真实 Run 出现 `MODEL_UNAVAILABLE` 且日志显示是客户端超时。

## 另开一项（用户 2026-09-29 决定，不入 PR-1）

- 估算器按 `canonical(message)` 字节 ×0.25（`context.py:174-195`），975k 真实 tokens 的请求约 2.46 MB，估算约 61 万，低估约 1.6 倍（推断，估算值未直接测）。校准因子只在每次响应后按 `usage.prompt_tokens` 上调（`context.py:197-205`），所以压缩阈值 0.95×1M 在一轮内会滞后；同一轮多个并行 tool 结果各按 ≤100 KiB 计入，最坏可在校准前越过 1M。未验证是否会在真实 Run 中出现，也未验证提供方在 >1M 时的拒绝行为。

- 更正（2026-10-01）：对账实测旧估算是供应商计数的 0.81–0.86（少算约 14–20%），「约 1.6 倍」不成立；用户决定不改代码，见 [估算器对账](2026-10-01-m1-01-token-estimator.md)。

## 结果小结（2026-09-29）

- A 已实现：整视图按 DeepSeek V4.1 Flash 真实 token 计量（vendored tokenizer + `tokenizers==0.23.2`），上限 25,000；超限整份拒绝为 `RESULT_TOO_LARGE`（对应上游兜底分支，不落盘），拒绝视图回显实际查询窗与 `query`；删除 `_fit_rows`、收缩循环、`view_augment` 与字节上限。B 无需修改。
- 有界真实 Run（[run.md](../evidence/m1-01-view-tokens-effect/run.md)）：11 次拒绝全部是 `traces_search`，无原样重试、无引用被拒数据；核心 4 场景审查 P2 7 对基线 6（无改善，审查者换为 Sonnet 有噪声）；prompt tokens 约 69 万对 231 万；`window`/`query` 回显缺陷由真实 Run 审查发现，已修但未重跑。
- 计数器加载时机：`otel_demo_executor_factory` 在构建工厂时（worker 启动时）加载并 fail-closed；`fixture_executor_factory` 没有预加载，首个 Run 构造执行器时才加载。worker 启动时统一预加载 fixture 的计数器是可选项，不在本 PR 改。
- 决策点（留给用户）：拒绝视图不进覆盖摘要（`context.py:289`），模型因此少了"在 gaps 披露被拒"的提示，normal-1 与 fault-2 的报告都没有点名拒绝原因；按合同这是设计内行为，本 PR 不改。是否在 run_coverage 消息里列出被拒绝的调用，待用户定。

## 合并 main（#61、#62）

2026-09-29 把 origin/main（5708093）merge 进 PR 分支，无冲突（`git merge origin/main`，不 rebase、不 force-push）。核查 #62 的 `opspilot/web/charts.py` 与 `service.py` 是否依赖这次删改的东西：图表按已登记证据的视图（`evidence`）画，`view.status == "ok"` 且 `content` 非空才出图，否则输出 chart-unavailable `not_ok`（`charts.py:87-94`）。超限拒绝不登记证据、没有 `evidence_id`，报告不能引用，因此拒绝视图根本到不了图表；未采纳的历史视图 `content is None`，同样走 `not_ok`。`charts.py:299` 的 `truncated`/`omitted_rows` 提示只对旧版本视图有意义（v6 起 adopted 视图恒为 False/0），保留兼容，无需改。合并后 `make check` 2498 passed。

## 待决

1. 合同测试与既有测试调整：已完成（独立测试作者按合同 v3 重写并逐条记录调整；字节口径文件已删除）。
2. 新增第三方运行时依赖（`tokenizers` 及约 9 个传递包，产品运行时首次）与 6.4 MB 入库文件：用户已批准方向；具体包与文件在 PR 简报里再列一次。
3. 推翻 B1 的截断决定、删除 `view_augment`（公共接口，C3 合同项）：已由用户决定（拒绝），`view_augment` 的删除是本合同据此作出的推论，简报里单列请用户确认。
4. PR-1 改工具面、注册修订与视图语义，属用户门；附有界真实 Run（正常、故障、广域各至少 1 次），需要 lab 锁。
