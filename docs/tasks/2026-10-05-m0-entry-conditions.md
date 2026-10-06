# M0 遗留入口条件——待规划

- 状态：待开始（**未到可实施程度**：每项的处理方式需用户决定）
- 更新日期：2026-10-05
- 依据：[SPEC](../../SPEC.md) 第 6 行（这些项是「候选评测或更大 M1 范围」之前必须通过的入口验收）；[M0 退出矩阵](../evidence/m0-real-investigation/m0-exit-matrix.md)
- 工作区：未创建

## 为什么现在要排

SPEC 只为 M1-01、M1-02 和 2026-10-05 的基础设施准备开了门。M1-03 复盘以及之后任何「更大 M1 范围」都要先过这些入口条件，或者由用户逐项决定豁免/改期。不处理的话，M1-02 之后没有可开工的合法范围。

## 逐项现状与提议

| 入口条件 | 现状（证据） | 提议 |
|---|---|---|
| 产品级 streaming/checkpoint 恢复、工具错误 PG 审计、压缩器配对 | 2026-10-05 核查（#91，见下节）：产品不做 streaming（`stream: False`），该子项不适用；工具错误 PG 审计已被 M1-01 覆盖（真实模型 Run 的 PG 行）；重启恢复只部分覆盖（真实 PG 加替身模型，缺真实模型 Run 中途 kill 后经产品路径恢复）；压缩配对只有确定性测试和一次非产品脚本的真实 provider 冒烟，产品 loop 在真实 Run 中从未触发压缩（59 份 `ledger.json` 中带 `compactions` 字段的 46 份（另有 45 份 `report.json`）全为 0，其余 13 份早于该字段、无此字段） | 关闭 streaming（改为豁免/不适用，需用户决定）；工具错误一项仅补一份对照证据索引；重启恢复与压缩配对各缺真实取证：一次有界真实 DeepSeek Run 在模型响应提交前后各 kill 一次并经 `loop.resume` 恢复完成，另需真实触发一次压缩并做 PG 往返，两者可合并为同一组真实 Run（见下节缺口） |
| pause/resume 与 observer 授权 | pause/resume 有 PG 合同和真实 Run（`round-07-wp23-results.md`）；observer 授权由 M1-02 交付（SPEC 2026-10-03 段） | M1-02 完成后补一份对照证据即可关闭 |
| Kubernetes RBAC 与 Holmes 宿主 OS 隔离 | 无 K8s 环境，RBAC 缺测；容器隔离只有 UID 0 的路径不可见证据 | M1-02 第 0 步建 kind 环境后，RBAC 拒绝可直接在 kind 上取证；Holmes 宿主 OS 隔离只影响上游基线对比，提议随 F14 处理 |
| 正式 judge 校准与未见保留集 | judge rubric 待人工校准（6 份独立审查样本）；保留变体盲测只有首次小规模 10/10 | 借鉴上游 `tests/llm/` 的做法（场景 fixture + pytest marker 分档 + `RUN_LIVE` 开关 + 历史结果归档），结合 ADR-0006 的 LangSmith dataset/experiment 建正式集；需要用户单人确认标注 |

## 第 1 行核查结果（#91）

核查对象：main `3d6c94f`，只读，未改代码。证据级别：**单测**=确定性替身；**真实 PG**=`tests/integration/*_postgres.py` 在本地 PostgreSQL 上、模型为脚本替身；**真实 Run**=真实 DeepSeek + 真实 PG 的产品 loop。

| 子项 | 结论 | 证据（级别） | 剩余缺口 |
|---|---|---|---|
| 1 流中断后续接 | 不适用 | `opspilot/investigation/loop.py:194` 请求体固定 `"stream": False`，`client.py` 无流式解析；整次响应成功才提交 `commit_step`（单测+真实 PG） | 产品是非流式，M0 的 `stream-interrupt.json` 场景在产品里不存在；等价风险是「请求中途断连」，由 `MODEL_UNAVAILABLE` 重试（`test_m1_investigation_loop.py::test_unavailable_retry_is_a_second_physical_request`、`test_retry_re_reserves_and_rechecks_control`，单测）和 `test_kill_before_the_model_response_commits_then_resume`（真实 PG）覆盖。需用户决定是否改为豁免 |
| 2 在途 Run 的 checkpoint/重启恢复 | 部分（真实 PG + 替身模型已覆盖；真实 DeepSeek Run 中途 kill 再经产品路径恢复无证据） | `tests/integration/test_m1_loop_resume_postgres.py`：`test_kill_before_the_model_response_commits_then_resume`、`..._after_the_response_commits_before_its_tool_runs_...`、`..._after_the_tool_result_commits_before_the_next_call_...`、`test_budget_and_active_time_continue_across_attempts`（真实 PG）；`scripts/m1_acceptance.py` 的 `worker-restart` 场景，`docs/evidence/m1-01-acceptance/acceptance-output.txt:17`（真实 PG，替身模型）；`docs/evidence/m1-01-integration-2026-09-17/integ-final.md` 的 `kill -9` + 真实 420 秒租约到期后换 worker 完成（真实 PG，fixture 模型）；`tests/test_worker_recovery.py`（单测） | 未找到「真实 DeepSeek Run 中途 kill 再恢复」的证据（未确认）；integ-final 该条走的是重新 claim 从头跑，不是 `loop.resume` 路径（其文中备注） |
| 3 工具错误 PG 审计并配回模型 | 已覆盖（缺专门的错误配对用例） | `loop.py:824-860`：无论 executor 结果状态，视图先 `commit_tool` 再作为 `role: tool` 消息按 `tool_call_id` 配对；`docs/evidence/m1-01-coverage-refusals/ledger.json` 的 PG `steps.tool_results` 里有 3 条 `traces_search` `RESULT_TOO_LARGE` 错误行，`events.jsonl` 有对应 `tool_committed status=error`，`run.md` 记录模型随后在报告 gaps 中引用拒绝（真实 Run）；`test_m1_tool_boundaries.py::test_a_transport_timeout_is_never_recorded_as_a_completed_query`（单测，执行器侧） | 未找到「错误状态视图经真实 PG 提交后重启重建仍按序配对」的专门测试（未确认）；timeout/denied 类错误的真实 Run 行未见 |
| 4 压缩器配对 | 部分 | `tests/test_m1_investigation_compaction.py`（19 条，单测，内存 store）：折叠整轮 `[assistant, tool…]`、字节级重建、压缩后重启、被拒摘要不进入可执行计划；`docs/evidence/m1-01-loop-long-horizon/compaction-smoke.json`：真实 DeepSeek 接受压缩后 transcript（2 次 HTTP 200，开发脚本 `scripts/m1_compaction_smoke.py`，非产品 loop） | `tests/integration/` 无任何压缩用例（真实 PG 重建压缩行未测）；`docs/evidence/m1-01-*/` 59 份 `ledger.json` 中带 `compactions` 字段的 46 份（另有 45 份 `report.json`）全为 0，其余 13 份早于该字段、无此字段，产品 loop 在真实 Run 中从未压缩 |

小结：子项 3 已被 M1-01 覆盖，只差证据索引；子项 2 缺一次有界真实 DeepSeek Run 在模型响应提交前后各 kill 一次、再经产品路径（`loop.resume`）恢复并完成；子项 4 的缺口是「产品 loop 真实触发一次压缩 + 压缩行真实 PG 往返」，需要一次有界真实 Run（降低 `context_tokens` 迫使触发，可与子项 2 合并为同一组真实 Run）；子项 1 是范围判断。是否补取证与是否豁免 streaming 由用户在 E1 一并决定。

## 待决

- **E1**：上表提议是否同意，尤其「Holmes 宿主 OS 隔离随 F14」是改期，需用户决定。
- **E2**：M1-03 复盘是否必须等全部入口条件关闭才开工，还是像 M1-02 一样有界开放。

## 下一步与交接

- 第 1 行只读核查已完成（#91，见上节）。待 E1 决定：是否补一组有界真实 DeepSeek Run（重启恢复 kill/resume + 压缩触发与 PG 往返），以及 streaming 子项是否豁免（#90）。
- 用户决定 E1/E2 后，把各项拆成 issue 与计划。
