# F13 验收与合同测试（第 1–3 步）

验收测试由未参与实现的 Agent（Codex）按 [M1-03 合同 r6](../tasks/2026-10-05-m1-03-postmortem.md)、
[F13 原文](../../feature_list.json) 第 1–3 步、预审给出的可观察判据与公开接口编写；
编写时 `opspilot/acceptance_postmortem.py` 只是接口桩（类型与字段说明，函数体抛
`NotImplementedError`），测试作者未见实现。实现者不改断言；测试与已合并公开行为不一致处由测试作者按依据改正（见任务记录「第 4 步执行」）。
F13 第 4 步（后续调查使用知识）属 M2，不测；F13 `passes` 保持 false（D10）。

## 外部入口

`postmortem_outcome(IncidentScenario, postmortem_records(KnowledgeStore, incident_id))`，
从 `opspilot.acceptance` 导出（R13，与 F6 `recovery_outcome` 同一做法、独立类型）。

| 投影字段 | 来源（只复制已提交记录） |
|---|---|
| `generation_status`、`schedule`、`recent_attempts` | `incident_postmortem`（R10；尝试只是最近 10 次窗口，R9） |
| `versions[*]`：状态、陈旧原因、水位、内容哈希、代码段、证据目录、生成/校验记录、结论（certainty、引用、争议）、提议、该版本审核动作 | `incident_postmortem` 快照 + 复盘审计（R8 时间原样） |
| `review_actions` | `audit_trail("postmortem", id)` |
| `knowledge[*]`：来源版本/提议、审核者类型与 ID、批准时间、内容哈希、替换、墓碑、`retrievable`、`freshness` | `knowledge_from_postmortem` 导航 + `knowledge_history` + `active_revision`（D3：`retrievable` 指知识读取实际返回该 revision） |
| `knowledge_actions` | 每个条目的 `audit_trail("knowledge_entry", id)` |
| 主体错绑 | 任一层记录不属于该事故 → `generation_status="unknown"`、`unknown_reasons=("SUBJECT_MISMATCH",)`，不投影部分记录（D11） |
| 一致性 | `postmortem_records` 读完后复读复盘与相关条目代际，变化则重读（最多 3 次，否则 `RECORDS_UNSTABLE`）；调度与尝试不带代际，不在边界内（D34） |

投影零模型、零遥测、无自有 SQL（R12；`tests/test_postmortem_outcome.py` 用 AST 检查导入）。

## 场景（[tests/acceptance/test_f13_postmortem.py](../../tests/acceptance/test_f13_postmortem.py)，21 例，PG opt-in）

生成走产品 `PostmortemWorker` + 测试自有确定性桩模型（驱动独立计数，与持久尝试核对，R9）；审核动作全部经工作台 HTTP（Basic Auth，D33），不调存储审核原语；改变水位用合法人工输入（经 HTTP 的 `follow_up`，R15）。每个场景在稳定代际边界内把真实 HTML 与投影逐字段比较（事故页状态/版本/调度/尝试，版本页结论/引用/争议/水位，知识页来源/审核者/状态/墓碑，D34、R14）。

| F13 步骤 | 场景 |
|---|---|
| 1 | 草稿含影响、时间线、Run、人工动作、恢复观察（结束原因/transition 在 `recovery` 段，读数在证据目录 `kind="recovery"`）、证据支持的发现与不确定陈述；引用保留 evidence_id/scope/时间窗/`citable_as_fact`；水位绑定观察会话与结束记录 |
| 1–2 | 供应商失败、输出无效（`generation_failed`）与引用失败（`citations_failed`，draft、不可审核）互相区分，均不产生知识 |
| 2 | 争议、退回、驳回、陈旧版本的批准请求被拒（422/409 按错误类别，带 `code`），版本与条目代际、内容、审计、知识不变；退回后 worker 产出 `revises_version` 新版本，原版只读 |
| 3 | 批准记录来源版本/提议、审核者（认证主体，伪造表单字段无效）、时间、内容哈希；重放幂等、过期代际 409；替换后旧 revision `superseded`、新 `active`；撤销留下理由与墓碑、知识读取为空、历史内容不变 |
| 1–3 | 无记录 `not_generated`；九类主体错绑（事故、视图、尝试、版本、结论、提议、争议、审计、知识）→ `unknown` |

实现者测试：`tests/test_postmortem_outcome.py`（无 PG，19 例：复制、未生成、13 类错绑、稳定边界重读与放弃、导入检查）与
`tests/integration/test_m1_03_postmortem_outcome_postgres.py`（PG，4 例）。

## 运行

```bash
OPSPILOT_LAB_DSN="host=127.0.0.1 port=<临时实例> dbname=m0_budget user=m0_lab" \
M1_DURABLE_POSTGRES=1 M0_B_POSTGRES=1 \
.venv/bin/python -m pytest tests/acceptance/test_f13_postmortem.py tests/integration/test_m1_03_postmortem_outcome_postgres.py tests/test_postmortem_outcome.py -q
```

CI `m0-postgres` 作业已包含 `tests/acceptance/test_f13_postmortem.py`。
这些是确定性合同与集成测试，不是真实模型或生产证明；真实验收见任务记录（D32、D24）。
