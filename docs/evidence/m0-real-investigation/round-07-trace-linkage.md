# 工作包 6：Trace 关联字段核查（round-07）

日期：2026-09-12。范围：只从已提交的 B4 trace 回读文件、M0-01 live 出口代码与其确定性测试核对 [M0 计划第 6 节](../../plans/m0-validation-plan-2026-09-07.md) 要求的关联字段；0 模型调用、0 trace 上传、0 LangSmith 读取。本文不把 trace 成功写成调查成功或权限验证通过。

## 核对对象

| 出口 | 代码 | 已提交证据 |
|---|---|---|
| B4 真实 LangSmith 白名单出口（三个 Run） | `scripts/m0_b4_trace.py` | [`round-05-b4-1-trace.json`](round-05-b4-1-trace.json)、[`round-05-b4-2-trace.json`](round-05-b4-2-trace.json)、[`round-05-b4-3-trace.json`](round-05-b4-3-trace.json)，均 `TRACE_VERIFIED`，`returned_fields = id/inputs/name/outputs/run_type/session_id`，`private_fields_persisted=false` |
| M0-01 live 白名单出口（真实单链路） | `scripts/m0/live.py`（`trace_wire`/`trace_readback_code`）+ `scripts/m0/protocol.py`（`trace_dto`） | [`trace-diagnosis`](../m0-01-live/trace-diagnosis.md) `TRACE_VERIFIED`；确定性合同 `tests/test_m0_live.py`、`tests/test_m0_trace.py`、`tests/test_m0.py` |
| 离线 TraceAdapter 替身 | `scripts/m0/trace_adapter.py` | `tests/test_m0_trace.py`（fake backend，`no_network`） |

## 逐字段表

“真实”=在已上传并回读的 B4/M0-01 DTO 中实际存在；“替身”=只在确定性测试或固定 fixture 标签中存在；“无”=任一出口都不携带。

| 计划要求 | 字段 | B4 真实 DTO | M0-01 live DTO | 判定 | 说明 |
|---|---|---|---|---|---|
| 主体关联 | `subject_id` | 无（B4-3 的 `subject_id` 仅在 PG 结果 `round-05-b4-3-incompatible.txt` 中） | 无（`subject` 为固定 fixture 标签 `m0-target-a`，不是身份） | **缺口 → 已补实验层 DTO** | `c65010d` 为 `trace_dto` 增加可选 canonical UUID `subject_id`，非 UUID/非字符串 fail-closed `TRACE_DTO_INVALID`；仅本地 fake backend 回读验证，未上传。B4 脚本与 live 出口尚未接入该字段。 |
| Run 关联 | `run_id` | 真实（`id` + `inputs.run_id`，回读比对身份） | 真实（`outputs.run_id`，`id` 比对） | 通过 | 回读 `id` 不等于 run_id 时 `TRACE_IDENTITY_MISMATCH`。 |
| attempt 关联 | `attempt` | 无 | 替身（固定 `1`） | **部分 → 已补有界校验** | `c65010d` 允许 1–8 的整数 attempt，布尔/越界拒绝；真实 Run 未携带 >1 的 attempt。 |
| 实验/分配关联 | `experiment_id` / allocation | 无（allocation `m0-05-b4-20260912` 只在 ledger） | 真实（`experiment_id`） | 部分 | B4 DTO 与 ledger 之间只能靠 run_id 手工对齐。 |
| 评分→步骤 | step id | 无（`prepared_step` 仅在 PG 输出） | 无 | **缺口** | 步骤真相在 PG StepStore；trace 不含 step id，评分记录只能经 run_id 回到 PG，再到 step。 |
| 评分→证据 | `evidence_id` / view hash | 无 | 替身（固定 `m0-evidence-a`） | **缺口（trace 层）** | v4 评分实际回溯路径是业务记录中的 evidence raw/view/hash（`round-04` 7/7 hash 复算），不经 trace；trace 层无 evidence 引用。 |
| 代码版本 | `code_sha256` | 无 | 真实（contract `code_sha256`） | 部分 | B4 出口不记录代码版本。 |
| 工具/schema 版本 | tool schema version / `report_schema_version` | 无（B4-3 版本不兼容判定只在 PG audit） | 无 | **缺口** | `report_schema_version` 在 `result-business.json`，tool 版本在 PG claim；trace 不携带。 |
| 私有字段过滤 | reasoning/凭据 | 真实（`private_fields_persisted=false`；回读只接受白名单键） | 真实（`extra` 仅允许 `ls_run_depth=0`，其余 `TRACE_EXTRA_REJECTED`） | 通过 | `test_whitelist_upload_readback_and_retry`、`test_trace_sdk_does_not_add_environment` 断言 `api_key`/`reasoning_content` 不出现。 |
| 平台不可用时业务完整 | — | 未在真实环境注入 | 替身 | 通过（替身） | `test_complete_boundary[trace-outage]`（503 → business `completed`、trace `unknown`、模型 2 次不重发）；`test_trace_storage_failure_keeps_committed_business_complete`（诊断存储不可用 → 业务已提交、不 POST）。 |
| 平台恢复后重新导出 | — | 无 | 无 | **缺口** | `m0_live_once.outbox` 保留 DTO，但仓库没有从 outbox 重放导出的脚本或测试；“恢复导出”仍未闭环。 |
| 实验数据/结果导出复现 | payload hash | 真实（`payload_hash`、`project_name_hash`） | 真实（回读逐字段等值） | 部分 | B4 只提交 hash，不提交 payload 原文；复现需私有运行目录。 |

## 结论

- 通过（有证据）：Run 身份、私有字段过滤、平台不可用时业务完整（替身）。
- 已补（实验层，未上传）：`subject_id` 与有界 `attempt` 进入白名单 DTO；`tests/test_m0_trace.py` 新增 2 组确定性测试，`183 passed`（定向 5 个测试文件）。
- 仍缺口：trace 层无 step/evidence/tool-schema 版本；平台恢复后的 outbox 重放导出没有脚本和测试；B4 真实出口尚未使用新字段。这些缺口不阻止 PostgreSQL 作为审计真相，但意味着“从评分追溯步骤/证据/版本”当前只能经 PG/业务记录完成，不能经 trace 完成。

工作包 6 状态保持**部分**；不打开 SPEC gate。
