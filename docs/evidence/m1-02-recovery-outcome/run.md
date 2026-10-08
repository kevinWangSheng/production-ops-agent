# F6 验收投影：IncidentScenario → RecoveryOutcome（#140）

- 日期：2026-10-08；分支 `feature/F6-recovery-outcome`，基于 main `c9546c9`
- 未启动实验环境、未调用模型、未查遥测；全部来自本机测试与临时 PostgreSQL 17.9（55641，数据目录在会话临时目录）。

## 接口

- `opspilot.acceptance.recovery_outcome(scenario: IncidentScenario, records: RecoveryRecords) -> RecoveryOutcome`（实现在 `opspilot/acceptance_recovery.py`，`acceptance.py` 再导出）。
- `RecoveryRecords(incident, sessions, controls, grants)`：`ObservationStore.incident_records(incident_id)` 的三项 + `ObservationStore.table_privileges()`。
- `permissions_from_grants(grants, *, human_control) -> tuple[str, ...]`。

## 字段来源

| 字段 | 来源 |
|---|---|
| incident_lifecycle / incident_mode | `opspilot_incidents` 行 |
| target | 会话行 `target`（不可变绑定） |
| recovery_verdict / recovery_confirmed / latest_sample_verdict / healthy_window_seconds | `replay_history(最新会话)` |
| recovery_reasons | 重放 integrity 码 + 最新已采纳采样的逐信号重算判定（INSUFFICIENT_TRAFFIC / REQUIRED_TELEMETRY_MISSING / MISSING_SIGNAL:x / STALE_TELEMETRY:x / DEGRADED_SIGNAL:x）+ CONTINUED_DEGRADATION / OBSERVATION_UNCONFIRMED / NO_HEALTH_PROFILE |
| used_sample_count | 会话行 `adopted_count`（重放核对） |
| observation_ended / observation_ended_reason | 会话行 state / ended_reason |
| human_interaction / handoff_reasons | ended_reason ∈ {deadline_expired, max_samples_exhausted} → handoff，原因 = 结束码 + recovery_reasons |
| recovery_samples[*] | 采样行 + 读数行（value/status/点数/query/source/窗口/raw_sha256/捆绑内 body_sha256）+ 重放逐信号判定；evidence_id = `<sample_id>:<signal>` |
| recovery_profile_revision / content | `opspilot_health_profiles` 行 |
| recovery_handled_at | 会话行 `authorized_at` |
| observation_sessions / observation_authorization / sample_jobs / handling_audit | 会话行合同字段投影 / 最新会话 / 采样行 job 集合 + 活动任务 / `opspilot_controls` 行 |
| actions | 见 development.md 规则 |
| permissions | `permissions_from_grants(实测权限, human_control=有控制行)` |
| model_requests | 恒空（观察路径无模型客户端，import 边界由 tests/test_m1_observer.py 校验） |

## 测试

- 单元 `tests/test_f6_recovery_outcome.py` 8 passed（确认恢复投影、篡改 → unknown + 完整性码、degraded / 撤流量交接与原因、缺必要信号命名、进行中会话、无会话、权限推导含越权/删除/不可读与空测量拒绝）。
- PG `tests/integration/test_m1_02_recovery_outcome_postgres.py` 5 passed：Observer 登录实测权限（samples/readings/endings SELECT+INSERT，sessions/incidents SELECT+UPDATE，profiles SELECT，runs/controls 无）；真实 Observer 采样确认恢复后的投影（投影期间桩 Prometheus 请求数不变）；次数耗尽交接；`register_remediation` 路径 → `human_control` + `record_handling`；额外 `GRANT UPDATE ON opspilot_runs` 的登录 → `investigation_write:opspilot_runs`。
- 既有 M1-02 PG 套件 + 重放套件 108 passed；`make check` 见 PR。

## 独立审查处置（2026-10-08，Codex 全新上下文，4 条 P2，全部采纳；原文主仓库 `tmp/m1-02-review/143-review-final.md`）

1. 列级权限被折叠：`table_privileges()` 改为整表 `"*"` / 精确列集合，`permissions_from_grants` 与 `OBSERVER_GRANTS`（0003 授予的精确列集合，单测 `test_the_expected_observer_grants_are_the_migrations` 与迁移源逐列核对）比较，多出列（如 `incidents(mode)`）或整表 UPDATE → `record_rewrite:opspilot_incidents(mode)` / `record_rewrite:opspilot_incidents`。
2. 扫描范围：扩到所有非系统 schema 的表/分区表、序列、schema CREATE、数据库 CREATE/TEMP、可执行的 volatile SECURITY DEFINER 函数；非产品表写权限 → `foreign_write:<schema.表>`。PG 测试给 Observer 登录额外 `GRANT UPDATE ON opspilot_runs`、`GRANT UPDATE (mode) ON opspilot_incidents`、`GRANT SELECT, UPDATE ON lab_notes`（非 opspilot 表）→ 三者都报出，`lifecycle` 列不报。限制写在 development.md。
3. actions 只记发生的动作：`read_only_query` 按捆绑里实际发出的即时查询计（detail `LEASE_BUDGET` 的本地填充不计，哨兵不计，捆绑不验证不计）；`register_remediation` 只在生命周期确实改变时追加 `advance_incident_lifecycle`（规则见 development.md，取不到前值就不写）。单测：一条 LEASE_BUDGET 部分 → 5 而非 6；上一个会话被 `authority_revoked`（生命周期未变）后的再登记不追加。
4. 原因不另算：`SessionReplay` 新增 `recovery_reasons / observation_ended / ended_reason / handoff / handoff_reasons`，逐信号原因从重放已有的 `SampleEvaluation.verdicts`（含 required 标志）算出，在线与重放同一逻辑；投影删除 profile 解析与原因拼装，只复制。可选项：信号 `evaluated_at` / `sample_time` 取捆绑记录时刻，不用窗尾冒充。

复验数字：单元 `tests/test_f6_recovery_outcome.py` 11 passed、`tests/test_m1_02_replay.py` 25 + 页面 5 + observer 47 通过；PG `tests/integration/test_m1_02_recovery_outcome_postgres.py` 5 passed；六个 M1-02/重放/投影 PG 套件合计 113 passed（55641）；`make check` 3177 passed / 469 skipped / 2 xfailed。红证明：审查项测试在修复前的产品代码上（`git apply -R` 对照）失败，见 PR 回报。
