# M1 准备：OTel 埋点接 LangSmith，实验证据上平台

- 状态：待开始（ADR-0006 已决；门槛见 SPEC 2026-10-05 段落）
- 更新日期：2026-10-05
- 依据：[ADR-0006](../adr/0006-trace-evidence-and-backlog.md)；C3 §2（第 31 行）、§11「可观测性」「保留与删除」、第 440–441 行（私有字段与凭据不出域）；F8 第 1 步
- 工作区：开工时新建 `chore/m1-prep-trace` worktree

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
3. 模式：`OPSPILOT_TRACE=off|lab`，默认 `off` 为空实现、什么都不导出；`lab` 只用于合成流量实验环境，附模型输入输出与工具结果。任何模式下凭据、`reasoning_content` 及其他 provider 私有协议字段都不进 span（PRODUCT-CONSTRAINTS「Data flow contract」）：执行边界在写属性之前：span 属性只能经 `opspilot/tracing.py` 里的单一构造函数写入，它按显式白名单从已有的类型化对象（`ModelCall`/`ModelReply`、`ToolOutcome` 的模型可见投影）取字段，不接受原始 dict 或 provider 原始响应；白名单外的字段一律丢弃。测试断言白名单外字段（含 `reasoning_content`、Authorization 头、`.env` 中的密钥值）无法进入 span，`scripts/check_secrets.py` 扫描只作第二道检查。这是 ADR-0006 第 5 条记录的对 C3 §11 的有界偏离，只限实验环境。
4. 导出失败不影响业务路径：批量导出、有界队列、丢弃计数入日志（C3 §11 允许 trace 有界丢弃）。
5. 测试：内存 exporter 断言 span 树形与属性；空实现路径无网络调用。
6. 合并类别：涉及数据出口，用户门。

**PR 2：实验证据改造**
1. 实验脚本每轮设一个 LangSmith project（命名 `opspilot-<轮次>`），project 设最长保留。
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

- 待核实（开工时）：LangSmith 当前套餐的 trace 配额与费用；project 级保留期的设置方式（UI/API）；`gen_ai.*` 属性在当前 LangSmith 版本中的实际映射（以一次真实上传回读为准）。
