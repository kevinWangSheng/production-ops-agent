# M1 准备：OTel 埋点接 LangSmith，实验证据上平台

- 状态：PR 1 已合并（#108，issue #80）；PR 2 已合并（#110，issue #81）；完成条件中「全新上下文审查凭 API 读 trace」与 `otel-demo` lab Run 未执行
- 更新日期：2026-10-06
- 依据：[ADR-0006](../adr/0006-trace-evidence-and-backlog.md)；C3 §2（第 31 行）、§11「可观测性」「保留与删除」、第 440–441 行（私有字段与凭据不出域）；F8 第 1 步
- 工作区：PR 1 `../production-ops-agent-trace`（`chore/m1-prep-trace`，已合并）；PR 2 `../production-ops-agent-trace2`，分支 `chore/m1-prep-trace-evidence`

## 目标与范围

真实 Run 的模型调用、工具调用和 token/费用在 LangSmith 上按 incident/run 可查可比；实验证据从「整份 ledger 入库」改为「平台 trace + 仓库冻结摘要」。

范围内：OTel 埋点模块、LangSmith OTLP 导出、内容分级开关、实验脚本改造、审查判定写 feedback、一次真实验证 Run。
范围外：产品模式的任何导出与 PG outbox（C3 §11，outbox 建成前产品模式不导出，含业务标识）、结构化日志、Prometheus/Grafana 运行指标（F8 其余步骤）、迁移历史证据。

## 已核查的事实

- 产品代码无 trace；`langsmith==0.12.2` 已声明；`.env` 已有 `LANGSMITH_API_KEY`、`LANGSMITH_PROJECT`、`LANGSMITH_ENDPOINT`、`LANGSMITH_WORKSPACE_ID`（只核对键名）。M0 曾用白名单 DTO 上传并回读成功（`m0-exit-matrix.md` 工作包 6）。
- LangSmith OTLP：端点 `https://api.smith.langchain.com/otel`，头 `x-api-key`，可选 `Langsmith-Project`；识别 `langsmith.span.kind`、`langsmith.metadata.*`、`gen_ai.request.model`、`gen_ai.usage.input_tokens`/`output_tokens`、`gen_ai.prompt.{n}.*`/`gen_ai.completion.{n}.*`。官方文档未写属性大小上限和内置脱敏（[来源](https://docs.langchain.com/langsmith/trace-with-opentelemetry)）。
- 保留：基础 14 天；长保留自 2026-09-14 起 SaaS 最长 180 天，修改只对新 trace 生效（[来源](https://docs.langchain.com/langsmith/data-purging-compliance)）。
- 上游参照：`holmes/core/tracing.py`（抽象层与空实现）、`holmes/core/otel_tracing.py`（工具 span 与 `gen_ai.chat` span、属性按长度截断）。
- 埋点位置：模型调用 `opspilot/investigation/client.py:161` `complete()`；工具调用 `opspilot/tools/executor.py:591` `execute()`；Run 外层在 `opspilot/investigation/runner.py`；实验脚本写 ledger 见 `scripts/m1_live_runner.py:327-357`。

## 计划（两个 PR）

**PR 1：埋点与导出**
1. 新增单一模块 `opspilot/tracing.py`：未配置端点时为空实现，零开销；依赖 `opentelemetry-sdk`、`opentelemetry-exporter-otlp-proto-http`（版本开工时核对锁定）。
2. span：Run（`chain`）→ 模型调用（`llm`，模型名、token、耗时、结束原因）→ 工具调用（`tool`，工具名、目标、结果类别、字节数）。`langsmith.metadata.*` 带 incident_id、run_id、step 序号、prompt/tool schema/projection revision。
3. 模式：`OPSPILOT_TRACE=off|lab`，默认 `off` 为空实现、什么都不导出；`lab` 只用于合成流量实验环境，附模型输入输出与工具结果，且**失败即关闭**：除环境变量外，还须在启动时和每个 Run 开始时独立核实运行目标属于实验环境（工具 profile 为 `fixture` 或 `otel-demo`，且 Prometheus/Jaeger 端点为回环地址或 kind 集群内地址，LangSmith project 名以 `opspilot-lab-` 开头）；任一项不满足就整个 Run 不导出并记日志，不降级为部分导出。加测试覆盖「只设环境变量、目标不在实验环境」时无导出。任何模式下凭据、`reasoning_content` 及其他 provider 私有协议字段都不进 span（PRODUCT-CONSTRAINTS「Data flow contract」）：执行边界在写属性之前：span 属性只能经 `opspilot/tracing.py` 里的单一构造函数写入，它按显式白名单从已有的类型化对象（`ModelCall`/`ModelReply`、`ToolOutcome` 的模型可见投影）取字段，不接受原始 dict 或 provider 原始响应；白名单外的字段一律丢弃。测试断言白名单外字段（含 `reasoning_content`、Authorization 头、`.env` 中的密钥值）无法进入 span，`scripts/check_secrets.py` 扫描只作第二道检查。这是 ADR-0006 第 5 条记录的对 C3 §11 的有界偏离，只限实验环境。
4. 导出失败不影响业务路径：批量导出、有界队列、丢弃计数入日志（C3 §11 允许 trace 有界丢弃）。
5. 测试：内存 exporter 断言 span 树形与属性；空实现路径无网络调用。
6. 合并类别：涉及数据出口，用户门。

**PR 2：实验证据改造**
1. 实验脚本每轮设一个 LangSmith project（命名 `opspilot-lab-<轮次>`），project 设最长保留。
2. 仓库只写冻结摘要 `summary.json`：报告、判定、计数、费用、trace ID 与链接、原始 ledger 的 sha256；不再写 `ledger.json`。
3. 独立审查判定用 `langsmith` SDK 写成 feedback（key 如 `review_p1`/`review_p2`、评语链接审查记录）。
4. 改 AGENTS.md「验证与汇报」过渡句的生效状态（本 PR 合并后，PR 证据改附 trace 链接与冻结摘要）。
5. 合并类别：改 AGENTS.md 与证据规则，用户门。

## 前提与完成条件

- 前提：开工前先核实下方「待核实」三项并写进本记录；SPEC 门槛段落与 ADR-0006 合并；[schema 迁移](2026-10-05-m1-prep-schema-migrations.md)先合并（避免同时动 runner 与持久化）。
- 完成条件：
  - 一次有界真实 Run（`lab` 模式）在 LangSmith 可见完整 span 树，token 与 PG 记录一致；`off` 模式无任何导出。
  - span 凭据扫描测试通过；导出端点不可达时 Run 正常完成。
  - 独立审查（全新上下文）可凭 API 读到 trace 并完成审查。

## 下一步与交接

- PR 2 已合并（#110）；此后触碰调查 loop 的 PR 按 AGENTS.md 附 trace 链接与 `summary.json`。
- 未执行（两个 PR 共同）：独立审查（全新上下文）凭 API 读 trace 并用 `lab_review_feedback.py` 写判定；`otel-demo` profile 下的 lab Run。
- 本记录的第一个真实 PR 2 Run 之后，PR 2 worktree 的临时 PostgreSQL（127.0.0.1:55471）已停止并删除，原始 ledger 只在该 worktree 的 `tmp/lab-ledgers/` 下，以 sha256 识别。

## PR 1 执行（2026-10-05）

实现：`opspilot/tracing.py` 单模块；埋点在 `DeepSeekClient.complete()`、`ReadOnlyToolExecutor.execute()`、`InvestigationRunner.resume()`（每次 attempt 一个根 span）；`worker_main` 启动时 `configure(os.environ)`、退出时 `shutdown()`；`scripts/m1_live_runner.py` 同样接入并把 trace id 写入 ledger/summary。

- 依赖：`opentelemetry-sdk==1.45.0`、`opentelemetry-exporter-otlp-proto-http==1.45.0`（PyPI 2026-09-25 发布的当前最新版，与 `requests` 复用已锁定版本；`uv lock` 只新增这两项及其传递依赖 8 项）。
- 失败即关闭：`check_lab_target(env)` 在 `configure()`（启动）和 `Tracer.run()`（每个 Run 开始，读的是传入的 `os.environ` 实时映射而非启动快照）各跑一次；检查项：工具 profile ∈ {fixture, otel-demo}、Prometheus/Jaeger URL 主机为回环或 `*.svc`/`*.svc.cluster.local`、`LANGSMITH_PROJECT` 以 `opspilot-lab-` 开头、`LANGSMITH_API_KEY` 非空、`LANGSMITH_ENDPOINT` 为 LangSmith SaaS 四个区域主机之一且 https。任一失败：启动时装 `NullTracer`，Run 时该 Run 返回空 span；模型/工具 span 不在导出中的 Run span 之内一律空操作，因此不存在部分导出。
- 白名单：属性只由 `_run_attributes`/`_model_call_attributes`/`_model_reply_attributes`/`_tool_request_attributes`/`_tool_outcome_attributes` 写入，入参必须是 `ModelCall`/`ModelReply`/`ToolRequest`/`ToolOutcome`（dict 抛 `TypeError`）。消息只保留 role/content/tool_calls(id,name,arguments)/tool_call_id；`reasoning_content`、`raw`、usage 中三项 token 以外的字段、`credential_ref`、原始源字节不复制。单属性上限 20000 字符截断。结构测试断言 `set_attribute` 只出现在 `tracing.py`。
- 导出：`BatchSpanProcessor`（队列 2048、2s 批、单次导出 10s 超时）+ 计数包装器记录丢弃 span 数并写日志；端点不可达测试（127.0.0.1:9）Run 正常完成，3 个 span 计入丢弃。
- 测试：`tests/test_tracing.py` 26 项（off 空实现、8 项失败即关闭参数化、Run→llm→tool 树形与属性、错误只记 code、截断、密钥/私有字段零泄漏、raw dict 拒绝、不可达端点）。`make check` 全绿：2559 passed、301 skipped（PG 可选）、2 xfailed。
- 真实 Run（证据冻结摘要 [`docs/evidence/m1-prep-trace-pr1/summary.json`](../evidence/m1-prep-trace-pr1/summary.json)，原始 ledger 不入库、以 sha256 标识）：
  - lab：project `opspilot-lab-trace-pr1-2026-10-05`，run `03c17e41…`，published，2 次模型调用；LangSmith 回读 4 个 run（chain 根 + 2 llm + 1 tool），每次 llm 的 prompt/completion tokens（1201/125、1499/2720）与 ledger 逐项一致，根 run 聚合 2700/2845 等于 ledger 总计与 PG `run_usage`；回读内容中不含 `reasoning_content`、`Authorization`、`x-api-key`。
  - off：`OPSPILOT_TRACE` 未设，run `5a8ec65c…`（1 次模型调用，INCOMPLETE_INVESTIGATION 交接），project run 数仍为 4。
  - 费用：DeepSeek 余额 6.71 → 6.69 CNY（两次 Run 合计；ledger 上界 0.0395）。
- 待核实结论：
  1. 套餐与配额：组织 `tier=free`（Developer）；定价页：每月含 5k base trace，超出按量（[来源](https://www.langchain.com/pricing-langsmith)）；本次一个 Run = 1 个 trace。
  2. 保留期：UI 为 Projects → 项目 → Retention；API 为 `PATCH /sessions/{project_id}` body `{"trace_tier": "longlived"}`（本次对 PR1 project 已执行，回读 `trace_tier=longlived`，即 180 天；langsmith SDK 0.12.2 的 `update_project` 不暴露该字段）。extended 层按 trace 另计费。
  3. 属性映射（以本次回读为准）：`langsmith.span.kind`→run_type；`langsmith.metadata.*`→`extra.metadata.*`；非 LangSmith 前缀的属性（`opspilot.*`）→`metadata["otel.span.<属性名>"]`；`gen_ai.prompt.{n}.role/content`→`inputs.messages[n]`；`gen_ai.completion.0.*`→`outputs.messages[0]`；`gen_ai.usage.*`→`usage_metadata` 与 `prompt_tokens/completion_tokens`，并向根 run 聚合；`gen_ai.request.model/max_tokens`→`invocation_params`；`gen_ai.system`→`metadata.ls_provider`；tool span 的 `gen_ai.prompt`/`gen_ai.completion` JSON 字符串被解析为 `inputs`/`outputs` 字典。LangSmith run id 取 OTel span id 低 64 位（`00000000-0000-0000-<span_id>`），OTel trace id 存于 `metadata.OTEL_TRACE_ID`；按 OTel trace id 过滤查不到 run，需用 LangSmith trace id。
- 可逆细节自决：根 span 每次 attempt 一个（而非每个 Run 一个，重试 attempt 各自成 trace，由 `metadata.run_id` 关联）；模型/工具 span 的步序用 Run 内计数器 `model_call_seq`/`tool_call_seq` 加 `step_id`，不在 loop.py 内加埋点。
- 未执行：独立审查（全新上下文）凭 API 读 trace；`otel-demo` profile 下的 lab Run。

### 独立审查修复（2026-10-05，PR #108 第二轮）

审查（全新上下文）七项发现全部采纳，每项先写红测试再修（`tests/test_tracing.py` 的 `test_finding_*`）：
1. `_is_lab_host` 改为解析后用 `ipaddress` 判定回环、精确 `localhost`、`*.svc`/`*.svc.cluster.local` 按标签边界匹配，拒绝 userinfo，大小写与尾点归一（`127.attacker.com`、`user@127.0.0.1` 等 11 例拒绝）。
2. `check_lab_target` 含 `TRACE_MODE_NOT_LAB`：每个 Run 开始同样重验 `OPSPILOT_TRACE=lab`。
3. 被拒绝的 Run 返回 `_RefusedRunSpan`，进入时清空当前 Run 槽、退出时恢复；嵌套在导出中 Run 内的被拒 Run，其模型/工具 span 不再挂到外层 trace。
4. 工具请求属性只取工具名、目标引用、窗口 start/end 字符串和参数**名**（凭据样名称剔除，最多 32 个）；参数值不再整体复制，已接受的查询仍经 outcome 的 `opspilot.tool.query` 导出。
5. `settle()`/`error.type` 只写固定词汇码（`^[A-Z][A-Z0-9_]{1,63}$`，状态为小写标识符），其余替换为 `UNCLASSIFIED`；无 `record_exception`、无状态描述。
6. `Resource({"service.name": "opspilot"})` 直接构造，忽略 `OTEL_RESOURCE_ATTRIBUTES`/`OTEL_SERVICE_NAME`。
7. 处理器与导出器全部参数显式（含 `max_export_batch_size`、`compression`），lab 组装包在 try 内，任何异常记 `TRACE_CONFIGURE_FAILED` + 异常类型并回退 `NullTracer`。
`make check`：2584 passed、301 skipped、2 xfailed。

第二轮三项（`test_round2_*`）：
1. `LANGSMITH_ENDPOINT` 须为 `https://<SaaS 主机>` 精确形式：拒绝 userinfo、非 443 端口、路径/查询/片段，主机小写并去尾点后精确匹配。
2. 工具 span 的名称、`gen_ai.tool.name`、`gen_ai.prompt.tool/target` 只用执行器注册表解析出的工具名与目标 ID（`ToolRegistry.lookup`/`TargetRegistry.resolve`，由 `execute()` 传入）；未注册时 span 名为 `tool:unknown`、字段省略，模型产出的原始字符串不出域。
3. 模型输入/输出中 tool_calls 的 `function.arguments` 仍按计划导出（lab 模式有意导出模型输入输出），但作为第二道防线：能解析为 JSON 的，递归把凭据样键（auth/token/secret/password/api_key/cookie/credential/bearer）的值替换为 `[REDACTED]`；不能解析的按原文截断导出，再过第三轮的通用擦除。

第三轮一项（`test_round3_*`）：所有字符串属性在唯一写入点 `_write()` 经 `_scrub()`：(a) 进程环境与 `configure()` 传入映射中名为 `LANGSMITH_API_KEY`/`DEEPSEEK_API_KEY` 或以 `_API_KEY/_TOKEN/_SECRET/_PASSWORD` 结尾、长度 ≥ 8 的值按原文精确替换为 `[REDACTED]`；(b) `(authorization|bearer|api[_-]?key|token|secret|password|passwd)\s*[:=]\s*\S+` 保留键、擦值，`bearer <token>`、`sk-…`/`lsv2_…` 形状整体擦除。`scripts/check_secrets.py` 没有可导入的 Python 模式表（它运行 gitleaks 的 Go 侧默认规则，仅内嵌一条自检 canary），因此文本模式写在 `tracing.py`，gitleaks 仍是仓库侧第二道检查。结构测试断言 `tracing.py` 中 `set_attribute` 只出现在 `_write` 内一次。

三轮修复后在最终 HEAD（df60b31）复跑一次 lab 真实 Run：project `opspilot-lab-trace-pr1-final-2026-10-05`，run `d5cbb3ef…`，published，2 次模型调用；LangSmith 回读 4 个 run（chain 根 + 2 llm + `tool:metrics_range_query`，工具名来自注册表），每次 llm tokens（1201/86、1490/2441）与 ledger 逐项一致，根 run 聚合 2691/2527 等于 ledger 总计与 PG `run_usage`；回读内容不含 `reasoning_content`/`Authorization`/`x-api-key`/`Bearer`/密钥值；project 已设 `longlived`。费用上界 0.028 CNY（余额显示 6.69 → 6.69，低于显示精度）。链接与哈希见 `summary.json` 的 `final_lab_run_at_head_df60b31`。

## PR 2 执行（2026-10-06）

实现：新模块 `scripts/lab_evidence.py`（每轮 project、最长保留、冻结摘要、trace 回读）与 CLI `scripts/lab_review_feedback.py`（审查判定写 feedback 并回读）；`scripts/m1_live_runner.py` 与 `scripts/m1_live_flash_loop.py` 改为只写 `summary.json`（flash loop 同时补上 Run 根 span，lab 模式下与 runner 同样导出）。只触碰这两个真实 Run 脚本；`m1_compaction_smoke.py`/`m1_context_latency.py` 是无 Run 的基准脚本，不在本项范围。`opspilot/tracing.py` 与 `opspilot/persistence/` 未改。

1. **每轮一个 project、最长保留**：`--lab-round <名>`（或 `OPSPILOT_LAB_ROUND`）→ `LANGSMITH_PROJECT=opspilot-lab-<名>`，lab 模式下必填；Run 前 `create_project(upsert)` + `PATCH /sessions/{id} {"trace_tier":"longlived"}` 并回读（保留期改动只对新 trace 生效，所以先设后跑）。
2. **仓库只留冻结摘要**：`summary.json` 含报告（解析为 JSON）、判定（状态/原因/各 attempt/控制/事件种类或验收 outcome）、计数（HTTP、tokens、`run_usage`、证据 ID）、费用上界、trace（模式、OTel trace id、project 与 id、`trace_tier`、LangSmith run id、链接、回读状态）、原始 ledger 的 sha256/字节数/相对路径。原始 ledger 写到 `tmp/lab-ledgers/<experiment>/<run_id>/`（gitignore 的 `tmp/`，或 `OPSPILOT_LEDGER_DIR`）。证据目录里除 `summary.json` 不再写 `ledger.json`/`report.json`/`acceptance-outcome.json`；旧证据不动。
3. **trace 回读**：LangSmith run id 不能从 OTel trace id 推出，脚本用 `list_runs(filter=metadata run_id)` 轮询根 run（最多 30×2s），找到则冻结 run id 与 UI 链接，找不到记 `read_back: root_run_not_found`，不编造。
4. **审查判定写 feedback**：`lab_review_feedback.py --summary <summary.json> --key review_<项> --verdict pass|fail|insufficient --review-url <审查记录> [--comment] [--record]`；`create_feedback` 后 `read_feedback` 逐字段比对，`--record` 把 feedback id 追加进 `summary.json` 的 `review_feedback`。
5. **AGENTS.md「验证与汇报」**：过渡句改为「附 trace 链接与仓库冻结摘要 `summary.json`（ADR-0006）；原始 ledger 不入库」。
6. **测试**：`tests/test_lab_evidence.py` 11 项（用录制假 client，无网络）；`tests/acceptance/test_m1_live_flash_script_entry.py` 的断言从「`ledger.json` 在输出目录」改为「只有 `summary.json` 在证据目录、ledger 在 `OPSPILOT_LEDGER_DIR` 且哈希一致」。这是证据规则变更带来的验收断言改动，随本用户门 PR 一并决定。`make check`：2611 passed、301 skipped、2 xfailed；`check_secrets.py` 通过。

真实验证（一次，fixture profile，真实 DeepSeek Flash，临时 PG 17 @127.0.0.1:55471）：

- 冻结摘要：[`docs/evidence/m1-01-handoff-runner/live-runs/93372aa7-8fd5-4f8e-bb88-161b2cbc5d1f/summary.json`](../evidence/m1-01-handoff-runner/live-runs/93372aa7-8fd5-4f8e-bb88-161b2cbc5d1f/summary.json)；目录内仅此一文件；原始 ledger sha256 `fc578fbc…1b42`（5790 字节，不入库）。
- Run `93372aa7…`：`published`，2 次模型调用，tokens 2698/3535，费用上界 0.036875 CNY；DeepSeek 余额 6.65 → 6.65（低于显示精度）。
- LangSmith：project `opspilot-lab-trace-pr2-2026-10-06`（id `ea320431-…`），Run 前 PATCH 后回读 `trace_tier=longlived`；根 run `00000000-0000-0000-80f2-bbb9d78533c7`，[链接](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/ea320431-68a8-4ce7-95a5-8a1795c09f99/r/00000000-0000-0000-80f2-bbb9d78533c7?poll=true)；回读 4 个 run（chain 根 + 2 llm 1201/81、1497/3454 + `tool:metrics_range_query`），根 run 聚合 2698/3535 等于摘要 `counts` 与 PG `run_usage`；回读内容不含 `reasoning_content`/`Authorization`/`x-api-key`/`Bearer`/环境密钥值。
- feedback：`review_mechanism_check=pass`（实现者的机制核验，**不是独立审查**），feedback id `01a1109b-ec87-7c90-80ff-2b2bc15a9947`，`read_feedback` 逐字段一致，另以 `list_feedback(run_ids=…)` 独立列出确认；id 已记入上述 `summary.json`。
- 可逆细节自决：round 名限 `[a-z0-9][a-z0-9.-]{0,62}`；`insufficient` 判定不打分只写 value；摘要里 ledger 路径写相对仓库路径（不含本机目录）。

### 独立审查修复（2026-10-06，PR #110）

Astra（全新上下文）2 项与 Codex 机器人 3 项全部采纳，每项先写红测试再修（`tests/test_lab_evidence.py` 的 `test_finding_*`）：
1. A1 `prepare_lab_project()` 移入 `lab_evidence.py`：先 `check_lab_target(env)`（复用 tracing 的检查），任一失败 `SystemExit` 且零 LangSmith 调用，再创建 project/设保留；两个脚本改用。
2. A2 feedback CLI：写前用 `check_lab_target` 的 endpoint/key 两码校验目标；`read_run` 读到 run 后 `read_project` 核对项目名以 `opspilot-lab-` 开头，否则 `RUN_NOT_FOUND`/`RUN_NOT_IN_LAB_PROJECT` 拒绝、不写。
3. B1 runner 的 `report_content_sha256`/`evidence_ids` 改取最后一个跑过 loop 的 attempt（`final_loop`），与 `runner_report` 的最终结论同源。
4. B2 根 run 改按 `metadata.OTEL_TRACE_ID`= tracer 记录的最终 trace id 查找（每个 attempt 各自成 root、续开 Run 换 run id，`run_id` 过滤可能取到任意 root）；对已有真实 Run 实时回读命中同一 root `…80f2-bbb9d78533c7`。
5. B3 回读包在 try 内，异常记 `read_back: error:<类型>`，ledger 与 summary 照常冻结。
`make check`：2616 passed、311 skipped、2 xfailed。摘要格式未变，未再跑真实 Run；A2/B2 以只读实时调用核验。

第二轮两项（`test_finding_r2_*`）：
1. `langsmith_client()` 显式 `Client(api_url=<经 check_lab_target 校验的 LANGSMITH_ENDPOINT 或规范默认>, api_key=LANGSMITH_API_KEY, workspace_id=…)`；校验失败先拒绝。原因：无参 `Client()` 还读 `LANGCHAIN_ENDPOINT`/`LANGCHAIN_API_KEY` 等 SDK 环境配置，红测试里 `LANGSMITH_ENDPOINT` 未设、`LANGCHAIN_ENDPOINT` 指向外部主机时 SDK 已带 key 请求了该主机的 `/info`。`opspilot/tracing.py` 的 OTLP 导出器只用校验过的 `LANGSMITH_ENDPOINT` 或默认值拼 URL（第 842 行），无需改。实时核验：设 `LANGCHAIN_ENDPOINT=https://attacker.example` 时 `client.api_url` 仍为 `https://api.smith.langchain.com`。
2. feedback CLI 同时给 `--run-id` 与 `--summary` 时两者须一致，否则 `RUN_ID_MISMATCH` 拒绝、不写；`--record` 仍要求 `--summary`。
`make check`：2618 passed、311 skipped、2 xfailed。
