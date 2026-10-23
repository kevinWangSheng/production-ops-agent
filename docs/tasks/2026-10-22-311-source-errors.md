# #311 数据源 400/422 错误原文回传与不可用数据源可见（F3 PR-2）

- 状态：进行中
- 更新日期：2026-10-22
- 依据：GitHub issue #311；C3 §5（revision bump 规则）；PRODUCT-CONSTRAINTS（缺失工具与数据对操作员可见）；[合同预审全文](2026-10-22-311-source-errors-contract.md)
- 工作区：`/Users/shenghuikevin/dev/AI/poa-311`，分支 `feature/F3-311-source-errors`（起点 main `200172c`）；独立验收测试分支 `feature/F3-311-source-errors-tests`

## 目标与范围

1. Prometheus 工具（`PROMETHEUS_TOOLS`，含 #307 A 的发现工具）的 400/422 错误信封原文，有界、脱敏后以不可信证据挂在拒绝视图上回传给模型，让模型能修正自己写错的查询。
2. 其余 4xx 只回传数字状态码；5xx、预发送拒绝、人工决定不变。
3. 本 profile 未接入的数据源列表进入初始上下文，并在页面显示。

不做：运行期 FAILED 探测（推迟另开 issue）；不改 discipline、REPORT_CONTRACT、redaction 规则、Observer。

## 前提与完成条件

- 前提：#308 PR2 已合（main `PROJECTION_REVISION` 为 v9）；#307 A 已合。
- 完成条件：独立验收测试通过；定向测试、ruff、mypy 通过；lead 安排的 kind 窗口最小真实证据（预审「最小真实证据」）；独立审查。

## 合同（r1）

U1–U6 用户裁决：按推荐（2026-10-22）。R1–R7 可逆默认照做。完整表见[合同预审全文](2026-10-22-311-source-errors-contract.md)。

| 编号 | 裁决要点 |
|---|---|
| U1 | 只回传 `PROMETHEUS_TOOLS` 的 400/422，且 body 可解析为 `{"status":"error","errorType":str,"error":str}` 信封；按工具集合判定 |
| U2 | 其余 4xx（401/403/404/429、traces 400）只带数字 `http_status`，reason 不变（SOURCE_ERROR 等） |
| U3 | 拒绝视图新增嵌套 `source_error: {trust:"untrusted-evidence", http_status, error_type, error, error_truncated}`；外层 `trust:"gateway"`、`content:null`、`citable_as_fact:false` 不变 |
| U4 | 错误原文不登记 EvidenceRecord、无 evidence_id；只在视图（随 step 持久化、进 trace） |
| U5 | `evidence_context.data_sources`（`available` 源→工具、`not_integrated`、固定指引），按 profile 静态生成，续接路径携带；模板纳入 `tool_schema_revision`；FAILED 推迟 |
| U6 | 页面步骤表显示 `http_status` 与脱敏截断后的 error；另显示本 Run 的未接入列表 |
| R1 | 完整 UTF-8 JSON 信封 → 取解码后字符串 → `redact_credentials` → 截断；解析失败不回传原文 |
| R2 | `error` 截到 1024 code point，超出 `error_truncated=true`；`errorType` 须匹配 `^[a-z_]{1,32}$` 否则 null |
| R3 | 只处理 `sent=True` 的真实响应；预发送拒绝不变 |
| R4 | 暂停/取消落在请求途中：照旧 denied + 决定码，不附 `source_error` |
| R5 | `PROJECTION_REVISION` v9→**v10**（#308 PR2 先合，占用 v9），fixture `fixture-3`→`fixture-4`；metrics 描述 `limits` 补 400/422 语义 |
| R6 | provenance 只对 dispatched 的 (error, INVALID_PARAMS 或 SOURCE_ERROR, confirmed) 放行 `source_error`，校验形状、长度、trust；缺席合法 |
| R7 | `data_sources` 不改 evidence_context 类型版本（仍 v4，新增可选键） |

实现期可逆细节（实现者自定）：

- 适配器（`otel_demo`）负责「哪些响应可回传」和信封解析，经 `TransportResponse.http_status`/`source_error_type`/`source_error_text` 交给执行器；执行器负责校验、脱敏、截断与 trust 包装。理由：Prometheus 信封知识留在 profile，安全边界留在通用执行器，fixture 等其他传输不受影响。
- `data_sources.instruction` 是固定文本常量，投影只放行与常量逐字相同的指引。理由：防止输入里伪造的指引文本进模型上下文。

## 执行进展与证据

（实施中）

## 下一步与交接

- 实现 → 并入独立验收测试 → 定向测试 → 推送分支，回报 lead。
- 真实证据由 lead 在 kind 窗口安排。
