# M1 准备：OTel 埋点接 LangSmith，实验证据上平台

- 状态：PR 1 已实现并完成真实 Run 验证，待用户门合并（issue #80）；PR 2 待开始
- 更新日期：2026-10-05
- 依据：[ADR-0006](../adr/0006-trace-evidence-and-backlog.md)；C3 §2（第 31 行）、§11「可观测性」「保留与删除」、第 440–441 行（私有字段与凭据不出域）；F8 第 1 步
- 工作区：`../production-ops-agent-trace`，分支 `chore/m1-prep-trace`

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

- 待核实三项已在「PR 1 执行」核实完毕。
- PR 2 开工前提：PR 1 合并。

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
