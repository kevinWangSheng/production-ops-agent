# ADR-0006：OTel 埋点接 LangSmith；实验证据上平台；backlog 进 GitHub Issues

状态：**已决定**（用户 2026-10-05）。

## 背景

- C3 §2（第 31 行）已定 LangSmith 做 Agent trace 与 eval，§11 要求 PG 保存真相、白名单 outbox 至少一次导出。产品代码至今没有任何 trace：`langsmith==0.12.2` 已声明但只有 M0 脚本 `scripts/m0_b4_trace.py` 用过；ROADMAP 与任务记录里也没有排期。
- 开发实验证据全部入库：`docs/evidence/` 66MB、2736 个文件，单次 Run 的 `ledger.json` 约 2MB（例：`docs/evidence/m1-01-alignment-c-effect/fault-1/`）。轮次之间的对比只能人工翻文件。
- 待排事项散在 ROADMAP 单元格和各任务记录的「后续」段里，没有统一视图，LangSmith 漏排就是这样发生的。
- 上游 HolmesGPT（main `9e21560`，2026-10-04）：`holmes/core/tracing.py`、`holmes/core/otel_tracing.py` 用 OTel 埋点，未设 `OTEL_EXPORTER_OTLP_ENDPOINT` 时为空实现；span 分工具与 LLM 两类；Langfuse 经 OTLP 接入，全量内容导出需显式开关；评测结果另用 Braintrust（可选）。上游没有 outbox，也没看到脱敏。

## 决定

1. **埋点用 OTel，后端用 LangSmith。** 产品代码只依赖 OpenTelemetry SDK，默认空实现；LangSmith 经其 OTLP 端点接入（`https://api.smith.langchain.com/otel`，头 `x-api-key`、可选 `Langsmith-Project`，见[官方文档](https://docs.langchain.com/langsmith/trace-with-opentelemetry)）。属性按 LangSmith 识别的约定写：`langsmith.span.kind`（llm/tool/chain）、`gen_ai.request.model`、`gen_ai.usage.input_tokens`/`output_tokens`、`langsmith.metadata.*` 放 incident/run/step/版本 ID。换 Langfuse 等后端只改端点配置。
2. **内容分级。** 模型输入输出与工具结果只在实验环境（合成流量）开关打开时导出；凭据在任何模式下都不进 span。真实业务数据的导出仍按 C3 §11 走白名单 outbox，outbox 建成前产品模式不开内容导出。本 ADR 不修改 C3。
3. **开发实验证据上平台。** 接入后，每轮实验一个 LangSmith project，独立审查判定写成 feedback。仓库只留每次 Run 的冻结摘要（报告、判定、计数、费用、trace ID 与链接、ledger 哈希），原始 ledger 不再入库。已入库的历史证据不动，不改写 git 历史。
4. **保留期约束。** LangSmith SaaS 基础保留 14 天，长保留自 2026-09-14 起最长 180 天（[官方文档](https://docs.langchain.com/langsmith/data-purging-compliance)）。因此平台上的原始 trace 只服务审查窗口和轮次对比；PR 与验收引用的长期依据是仓库里的冻结摘要。实验 project 设为最长保留。
5. **backlog 进 GitHub Issues。** 待排事项一项一个 issue，链接 C3 条款与任务记录；ROADMAP 只留里程碑状态表并链接 issue。仓库公开，issue 不写凭据、非公开数据和实验答案。

## 后果

- 正面：轮次对比、token/费用按 Run 关联直接在平台上看；仓库不再随实验膨胀；待办集中可见。埋点和上游同构，后端可换。
- 负面：新增 OTel 依赖；原始 trace 最长 180 天后过期，过期后只能依据冻结摘要复核；独立审查 Agent 需要能读 LangSmith（API key 走环境变量，不进 prompt）。
- 过渡：trace 接入完成前，触碰调查 loop 的 PR 仍按原规则附 ledger。
- 反转条件：LangSmith 的 OTLP 映射或保留期无法满足审查需要，或需要自托管时，改用 Langfuse（同一套埋点）。
